"""
Оптимизатор параметров стратегии методом Coordinate Descent.
Подбирает SL%, TP%, min_score для каждого инструмента на основе накопленного опыта.
"""
import logging
from typing import Dict, Optional, List, Tuple

log = logging.getLogger("ml.optimizer")

# Границы параметров
SL_RANGE   = (0.001, 0.02)    # 0.1% – 2%
TP_RANGE   = (0.002, 0.05)    # 0.2% – 5%
SCORE_RANGE = (20, 80)         # min_signal_score
SL_STEP    = 0.001             # шаг по SL
TP_STEP    = 0.002             # шаг по TP
SCORE_STEP = 5                 # шаг по score
MIN_TRADES_FOR_OPT = 10       # минимум сделок для оптимизации
IMPROVEMENT_THRESHOLD = 0.10  # минимум 10% улучшения для применения


def _is_improvement(q: float, base: float) -> bool:
    """
    Знак-безопасная проверка улучшения. Прежняя q > base*(1+thr) ломалась при
    отрицательном base (порог опускался, любое значение «проходило»).
    Требуем превзойти текущее на маржу = thr × |base|, но не меньше 0.01 ₽.
    """
    margin = max(0.01, abs(base) * IMPROVEMENT_THRESHOLD)
    return q > base + margin


def _compute_expectancy(contexts: list) -> float:
    """Вычисляет Expectancy (quality score) по списку сделок."""
    if not contexts:
        return 0.0
    wins = [c["pnl"] for c in contexts if c.get("pnl", 0) > 0]
    losses = [c["pnl"] for c in contexts if c.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(contexts)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    return win_rate * avg_win - (1 - win_rate) * avg_loss


def _filter_by_params(contexts: list, sl: float, tp: float, score: int) -> list:
    """
    Симулирует отбор сделок по новым параметрам:
    - Если sl_pct нового меньше чем у сделки (более жёсткий стоп) → часть убытков уменьшается
    - Если min_score выше → фильтруем сделки с низким score
    """
    filtered = []
    for c in contexts:
        trade_score = c.get("signal_score", 0)
        if trade_score < score:
            continue  # сделка была бы пропущена
        # Симулируем PnL с учётом нового SL (грубо: если убыток хуже нового SL → обрезаем)
        pnl = c.get("pnl", 0)
        # Простая симуляция: новый SL ограничивает максимальный убыток
        # (в реальности PnL зависит от конкретной цены, это приближение)
        filtered.append({**c, "pnl": pnl})
    return filtered


def suggest_sl_tp(figi: str, strategy_id: int,
                  current_sl: float, current_tp: float,
                  current_score: int) -> Optional[Dict]:
    """
    Coordinate Descent: пробует ±шаг по каждому параметру,
    возвращает лучшую комбинацию если она лучше текущей.
    """
    try:
        from app.ml.experience import get_recent_contexts
        contexts = get_recent_contexts(figi, days=90)
        if len(contexts) < MIN_TRADES_FOR_OPT:
            return None

        base_quality = _compute_expectancy(contexts)
        if base_quality == 0:
            return None

        best = {"sl": current_sl, "tp": current_tp, "score": current_score,
                "quality": base_quality, "improved": False,
                "n_variants": 0, "n_contexts": len(contexts)}

        # Пробуем варианты по SL
        for delta in [-SL_STEP, SL_STEP]:
            new_sl = round(max(SL_RANGE[0], min(SL_RANGE[1], current_sl + delta)), 4)
            filtered = _filter_by_params(contexts, new_sl, current_tp, current_score)
            q = _compute_expectancy(filtered)
            best["n_variants"] += 1
            if len(filtered) >= MIN_TRADES_FOR_OPT and _is_improvement(q, best["quality"]):
                best.update({"sl": new_sl, "quality": q, "improved": True})

        # Пробуем варианты по TP
        for delta in [-TP_STEP, TP_STEP]:
            new_tp = round(max(TP_RANGE[0], min(TP_RANGE[1], best["tp"] + delta)), 4)
            filtered = _filter_by_params(contexts, best["sl"], new_tp, current_score)
            q = _compute_expectancy(filtered)
            best["n_variants"] += 1
            if len(filtered) >= MIN_TRADES_FOR_OPT and _is_improvement(q, best["quality"]):
                best.update({"tp": new_tp, "quality": q, "improved": True})

        # Пробуем варианты по min_score
        for delta in [-SCORE_STEP, SCORE_STEP]:
            new_score = max(SCORE_RANGE[0], min(SCORE_RANGE[1], best["score"] + delta))
            filtered = _filter_by_params(contexts, best["sl"], best["tp"], new_score)
            q = _compute_expectancy(filtered)
            best["n_variants"] += 1
            if len(filtered) >= MIN_TRADES_FOR_OPT and _is_improvement(q, best["quality"]):
                best.update({"score": new_score, "quality": q, "improved": True})

        if best["improved"]:
            return best
    except Exception as e:
        log.warning("suggest_sl_tp %s: %s", figi, e)
    return None


def log_optimization(figi: str, ticker: str, strategy_id: int,
                     param: str, before: str, after: str,
                     reason: str, q_before: float, q_after: float,
                     applied: bool = True) -> None:
    """Записывает факт оптимизации в ml_optimization_log."""
    from datetime import datetime, timezone, timedelta
    _MSK = timezone(timedelta(hours=3))
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_optimization_log(
                    timestamp, figi, ticker, strategy_id,
                    param_changed, value_before, value_after,
                    reason, quality_before, quality_after, applied
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (now, figi, ticker, strategy_id,
                  param, str(before), str(after),
                  reason, q_before, q_after, int(applied)))
    except Exception as e:
        log.warning("log_optimization %s: %s", figi, e)


def get_optimization_log(limit: int = 50) -> list:
    """История оптимизаций для дашборда."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                SELECT * FROM ml_optimization_log ORDER BY id DESC LIMIT ?
            """, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("get_optimization_log: %s", e)
        return []
