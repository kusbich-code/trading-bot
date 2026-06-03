"""
Thompson Sampling для выбора режима стратегии.
Для каждого инструмента ведём Beta-распределение побед/поражений по каждому режиму.
"""
import logging
import random
from typing import Dict, Optional

log = logging.getLogger("ml.strategy_selector")

STRATEGY_MODES = ["trend", "mean_reversion", "breakout"]
MIN_TRADES_PER_MODE = 5  # минимум до вывода


def _sample_beta(wins: int, losses: int) -> float:
    """Сэмплирует из Beta(wins+1, losses+1)."""
    # Используем гамма-распределение для симуляции Beta
    alpha = wins + 1
    beta = losses + 1
    x = random.gammavariate(alpha, 1.0)
    y = random.gammavariate(beta, 1.0)
    return x / (x + y) if (x + y) > 0 else 0.5


def select_best_strategy_mode(figi: str, current_mode: str) -> Optional[str]:
    """
    Выбирает лучший режим стратегии через Thompson Sampling.
    Возвращает None если данных недостаточно (оставляем текущий режим).
    """
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            # Статистика по каждому режиму из контекстов сделок
            mode_stats: Dict[str, Dict] = {}
            for mode in STRATEGY_MODES:
                cur.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins
                    FROM ml_trade_context
                    WHERE figi=? AND strategy_mode=? AND exit_time!=''
                """, (figi, mode))
                row = cur.fetchone()
                total = row[0] or 0
                wins = row[1] or 0
                mode_stats[mode] = {"total": total, "wins": wins, "losses": total - wins}

        # Нужен хотя бы MIN_TRADES_PER_MODE по одному из режимов
        if not any(s["total"] >= MIN_TRADES_PER_MODE for s in mode_stats.values()):
            return None

        # Thompson Sampling: сэмплируем и выбираем максимум
        samples = {mode: _sample_beta(s["wins"], s["losses"])
                   for mode, s in mode_stats.items()}
        best_mode = max(samples, key=samples.get)

        # Возвращаем только если уверены (лучший режим имеет достаточно данных)
        if mode_stats[best_mode]["total"] >= MIN_TRADES_PER_MODE:
            return best_mode
    except Exception as e:
        log.warning("select_best_strategy_mode %s: %s", figi, e)
    return None


def get_mode_stats(figi: str) -> Dict[str, Dict]:
    """Возвращает статистику по всем режимам для инструмента (для дашборда)."""
    try:
        from app.db import db_cursor
        stats = {}
        with db_cursor() as cur:
            for mode in STRATEGY_MODES:
                cur.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                           COALESCE(AVG(pnl),0) as avg_pnl,
                           COALESCE(AVG(quality_score),0) as avg_quality
                    FROM ml_trade_context
                    WHERE figi=? AND strategy_mode=? AND exit_time!=''
                """, (figi, mode))
                row = cur.fetchone()
                total, wins, avg_pnl, avg_quality = row
                total = total or 0
                wins = wins or 0
                stats[mode] = {
                    "total": total,
                    "wins": wins,
                    "losses": total - wins,
                    "win_rate": round(wins / total, 3) if total > 0 else 0.0,
                    "avg_pnl": round(float(avg_pnl or 0), 2),
                    "avg_quality": round(float(avg_quality or 0), 4),
                }
        return stats
    except Exception as e:
        log.warning("get_mode_stats %s: %s", figi, e)
        return {}
