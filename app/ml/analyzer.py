"""
Анализатор качества стратегий.
Вычисляет quality_score = win_rate × avg_pnl с учётом давности сделок (EWA).
"""
import logging
import math
from typing import Dict, Optional

log = logging.getLogger("ml.analyzer")

EWA_ALPHA = 0.2       # коэффициент обновления EWA (свежие сделки весят больше)
DECAY_LAMBDA = 0.9    # коэффициент забывания для старых сделок
MIN_TRADES = 5        # минимум сделок для уверенного решения
CONFIDENCE_MAX_TRADES = 30  # при N сделках confidence → 1.0


def compute_quality(contexts: list) -> Dict[str, float]:
    """
    Вычисляет качество стратегии по списку контекстов (завершённых сделок).
    Возвращает: wins, losses, win_rate, avg_pnl, quality_score, confidence
    """
    if not contexts:
        return {"wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_pnl": 0.0, "quality_score": 0.0, "confidence": 0.0}

    wins = sum(1 for c in contexts if c.get("pnl", 0) > 0)
    losses = len(contexts) - wins
    win_rate = wins / len(contexts) if contexts else 0.0
    avg_pnl = sum(c.get("pnl", 0) for c in contexts) / len(contexts)

    # quality = expectancy: win_rate × avg_win - (1-win_rate) × avg_loss
    avg_win  = sum(c["pnl"] for c in contexts if c.get("pnl", 0) > 0) / max(1, wins)
    avg_loss = abs(sum(c["pnl"] for c in contexts if c.get("pnl", 0) <= 0)) / max(1, losses)
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

    confidence = min(1.0, len(contexts) / CONFIDENCE_MAX_TRADES)
    return {
        "wins": wins, "losses": losses,
        "win_rate": round(win_rate, 4),
        "avg_pnl": round(avg_pnl, 4),
        "quality_score": round(expectancy, 4),
        "confidence": round(confidence, 4),
    }


def update_ewa_quality(figi: str, strategy_id: int, new_quality: float) -> float:
    """Обновляет EWA quality_score в ml_instrument_state после новой сделки."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute(
                "SELECT ewa_quality FROM ml_instrument_state WHERE figi=? AND strategy_id=?",
                (figi, strategy_id)
            )
            row = cur.fetchone()
            old_ewa = float(row[0]) if row else 0.0
            new_ewa = (1 - EWA_ALPHA) * old_ewa + EWA_ALPHA * new_quality
            cur.execute("""
                UPDATE ml_instrument_state SET ewa_quality=? WHERE figi=? AND strategy_id=?
            """, (new_ewa, figi, strategy_id))
            return new_ewa
    except Exception as e:
        log.warning("update_ewa_quality %s: %s", figi, e)
        return 0.0


def get_instrument_state(figi: str, strategy_id: int) -> Optional[Dict]:
    """Читает текущее состояние ML-модели для инструмента."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                SELECT * FROM ml_instrument_state WHERE figi=? AND strategy_id=?
            """, (figi, strategy_id))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
    except Exception as e:
        log.warning("get_instrument_state %s: %s", figi, e)
    return None


def upsert_instrument_state(figi: str, ticker: str, strategy_id: int,
                            stats: Dict, ml_params: Dict) -> None:
    """Создаёт или обновляет состояние ML-модели для инструмента."""
    from datetime import datetime, timezone, timedelta
    _MSK = timezone(timedelta(hours=3))
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_instrument_state(
                    figi, ticker, strategy_id,
                    ml_strategy_mode, ml_stop_loss_pct, ml_take_profit_pct, ml_min_score,
                    trades_count, wins, losses, avg_pnl, win_rate, quality_score,
                    last_updated, confidence
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(figi, strategy_id) DO UPDATE SET
                    ticker=excluded.ticker,
                    ml_strategy_mode=excluded.ml_strategy_mode,
                    ml_stop_loss_pct=excluded.ml_stop_loss_pct,
                    ml_take_profit_pct=excluded.ml_take_profit_pct,
                    ml_min_score=excluded.ml_min_score,
                    trades_count=excluded.trades_count,
                    wins=excluded.wins,
                    losses=excluded.losses,
                    avg_pnl=excluded.avg_pnl,
                    win_rate=excluded.win_rate,
                    quality_score=excluded.quality_score,
                    last_updated=excluded.last_updated,
                    confidence=excluded.confidence
            """, (
                figi, ticker, strategy_id,
                ml_params.get("strategy_mode", ""),
                ml_params.get("stop_loss_pct", 0),
                ml_params.get("take_profit_pct", 0),
                ml_params.get("min_score", 0),
                stats.get("trades_count", 0),
                stats.get("wins", 0), stats.get("losses", 0),
                stats.get("avg_pnl", 0), stats.get("win_rate", 0),
                stats.get("quality_score", 0),
                now, stats.get("confidence", 0),
            ))
    except Exception as e:
        log.warning("upsert_instrument_state %s: %s", figi, e)


def get_all_states() -> list:
    """Все состояния ML-модели для дашборда."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT * FROM ml_instrument_state ORDER BY quality_score DESC")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("get_all_states: %s", e)
        return []
