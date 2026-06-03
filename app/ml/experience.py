"""
Сбор и хранение опыта торговли.
Записывает рыночный контекст при открытии позиции и исход при закрытии.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

log = logging.getLogger("ml.experience")
_MSK = timezone(timedelta(hours=3))


def _now_msk() -> str:
    return datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def record_entry(
    figi: str,
    ticker: str,
    strategy_id: int,
    strategy_mode: str,
    signal_score: int,
    indicators: Optional[Dict[str, Any]] = None,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
) -> int:
    """
    Записывает контекст при открытии позиции.
    Возвращает ml_context_id для последующей привязки к трейду.
    """
    try:
        from app.db import db_cursor
        ind = indicators or {}
        entry_time = _now_msk()
        now_dt = datetime.now(tz=_MSK)
        with db_cursor() as cur:
            cur.execute("""
            INSERT INTO ml_trade_context(
                figi, ticker, strategy_id, strategy_mode, entry_time,
                signal_score, rsi, macd, macd_signal, bb_upper, bb_lower,
                z_score, volume_ratio, momentum_5,
                hour_of_day, day_of_week,
                stop_loss_pct, take_profit_pct
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                figi, ticker, strategy_id, strategy_mode, entry_time,
                signal_score,
                ind.get("rsi"), ind.get("macd"), ind.get("macd_signal"),
                ind.get("bb_upper"), ind.get("bb_lower"),
                ind.get("z_score"), ind.get("vol_ratio"), ind.get("momentum_5"),
                now_dt.hour, now_dt.weekday(),
                stop_loss_pct, take_profit_pct,
            ))
            return cur.lastrowid or 0
    except Exception as e:
        log.warning("record_entry %s: %s", ticker, e)
        return 0


def record_exit(figi: str, pnl: float, entry_time_str: str = "") -> None:
    """
    При закрытии позиции: обновляет последний незакрытый контекст по figi,
    вычисляет holding_hours и quality_score.
    """
    try:
        from app.db import db_cursor
        exit_time = _now_msk()
        with db_cursor() as cur:
            # Ищем последний незакрытый контекст для этого figi
            cur.execute("""
                SELECT id, entry_time, stop_loss_pct, take_profit_pct
                FROM ml_trade_context
                WHERE figi=? AND exit_time=''
                ORDER BY id DESC LIMIT 1
            """, (figi,))
            row = cur.fetchone()
            if not row:
                return
            ctx_id, entry_time, sl_pct, tp_pct = row
            # Время удержания
            try:
                t0 = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(exit_time, "%Y-%m-%d %H:%M:%S")
                holding_hours = max(0.01, (t1 - t0).total_seconds() / 3600)
            except Exception:
                holding_hours = 1.0
            # Quality score: нормализованная доходность по времени удержания
            # (грубая оценка без размера позиции — уточняется через avg_pnl_pct)
            quality_score = (pnl / holding_hours) if holding_hours > 0 else pnl
            cur.execute("""
                UPDATE ml_trade_context
                SET exit_time=?, pnl=?, holding_hours=?, quality_score=?
                WHERE id=?
            """, (exit_time, pnl, holding_hours, quality_score, ctx_id))
    except Exception as e:
        log.warning("record_exit %s: %s", figi, e)


def get_recent_contexts(figi: str, days: int = 90) -> list:
    """Возвращает контексты завершённых сделок по инструменту."""
    try:
        from app.db import db_cursor
        from datetime import timedelta as _td
        date_from = (datetime.now() - _td(days=days)).strftime("%Y-%m-%d")
        with db_cursor() as cur:
            cur.execute("""
                SELECT * FROM ml_trade_context
                WHERE figi=? AND exit_time!='' AND entry_time>=?
                ORDER BY id DESC
            """, (figi, date_from))
            return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("get_recent_contexts %s: %s", figi, e)
        return []
