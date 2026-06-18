"""
Бэктест-оптимизатор параметров: гоняет реальный движок бэктеста на
исторических свечах брокера и подбирает лучшие mode/SL/TP по сетке.
Используется дневным ребалансом (orchestrator) по всем инструментам.
"""
import logging
from typing import Dict, Optional, List

log = logging.getLogger("ml.backtest_client")

BACKTEST_DAYS = 90          # период истории для бэктеста
MIN_BT_TRADES = 8           # минимум сделок в бэктесте чтобы доверять результату
IMPROVE_MARGIN = 1.10       # новые параметры должны быть на 10%+ лучше

# Сетка перебора
_MODES = ["mean_reversion", "breakout", "trend"]
_SL_GRID = [0.003, 0.005, 0.008, 0.012]
_TP_GRID = [0.006, 0.010, 0.015, 0.020]


def _quality(res) -> float:
    """
    Метрика качества бэктеста: чистый PnL с поправкой на число сделок.
    Возвращает -1e9 если сделок мало (ненадёжно).
    """
    try:
        n = int(getattr(res, "total_trades", 0) or 0)
        if n < MIN_BT_TRADES:
            return -1e9
        return float(getattr(res, "net_pnl", 0) or 0)
    except Exception:
        return -1e9


def _run(candles, mode, sl, tp, min_score=0):
    from app.services.backtest_engine import run_backtest
    return run_backtest(
        candles=candles, mode=mode,
        stop_loss_pct=sl, take_profit_pct=tp,
        commission_pct=0.0004, qty=1, min_signal_score=min_score,
    )


def optimize_instrument(figi: str, ticker: str,
                        current_mode: str, current_sl: float,
                        current_tp: float) -> Optional[Dict]:
    """
    Полный перебор mode×SL×TP на исторических свечах (свечи грузятся один раз).
    Возвращает лучшую комбинацию если она ощутимо лучше текущей, иначе None.
    """
    try:
        from app.services.tbank_client import get_candles_range
        candles = get_candles_range(figi=figi, interval_name="hour", days=BACKTEST_DAYS)
        if len(candles) < 40:
            log.info("[BT-opt] %s: мало свечей (%d) — пропуск", ticker, len(candles))
            return None

        # База — текущие параметры
        base_res = _run(candles, current_mode, current_sl, current_tp)
        base_q = _quality(base_res)

        best = {"mode": current_mode, "sl": current_sl, "tp": current_tp,
                "quality": base_q, "win_rate": getattr(base_res, "win_rate", 0),
                "trades": getattr(base_res, "total_trades", 0)}
        n_combos = 0

        for mode in _MODES:
            for sl in _SL_GRID:
                for tp in _TP_GRID:
                    if tp <= sl:
                        continue  # TP должен быть больше SL
                    n_combos += 1
                    res = _run(candles, mode, sl, tp)
                    q = _quality(res)
                    if q > best["quality"]:
                        best = {"mode": mode, "sl": sl, "tp": tp, "quality": q,
                                "win_rate": getattr(res, "win_rate", 0),
                                "trades": getattr(res, "total_trades", 0)}

        best["n_combos"] = n_combos
        best["base_quality"] = base_q
        best["candles"] = len(candles)

        # Улучшение засчитываем только если ощутимо лучше базы
        changed = (best["mode"] != current_mode or
                   abs(best["sl"] - current_sl) > 1e-6 or
                   abs(best["tp"] - current_tp) > 1e-6)
        improved = changed and best["quality"] > 0 and (
            base_q <= 0 or best["quality"] >= base_q * IMPROVE_MARGIN
        )
        best["improved"] = improved
        return best
    except Exception as e:
        log.warning("optimize_instrument %s: %s", ticker, e)
        return None
