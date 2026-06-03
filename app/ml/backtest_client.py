"""
Интеграция с существующим бэктест-модулем для валидации параметров.
Вызывает run_backtest() напрямую (без HTTP), передавая новые параметры.
"""
import logging
from typing import Dict, Optional

log = logging.getLogger("ml.backtest_client")

BACKTEST_DAYS = 90  # период валидации


def validate_params(figi: str, strategy_mode: str,
                    sl_pct: float, tp_pct: float,
                    lots: int = 1) -> Optional[Dict]:
    """
    Запускает бэктест с указанными параметрами.
    Возвращает результат (total_pnl, win_rate, trades) или None при ошибке.
    """
    try:
        from app.services.tbank_client import get_candles_range
        from app.services.strategy_engine import evaluate_signal
        from app.backtest import run_backtest

        candles = get_candles_range(figi=figi, interval_name="1hour", days=BACKTEST_DAYS)
        if len(candles) < 30:
            return None

        result = run_backtest(
            candles=candles,
            mode=strategy_mode,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            commission_pct=0.0004,
            qty=lots,
        )
        return {
            "total_pnl": result.get("total_pnl", 0),
            "win_rate": result.get("win_rate", 0),
            "trades": result.get("trades", 0),
            "max_drawdown": result.get("max_drawdown", 0),
            "quality_score": _compute_backtest_quality(result),
        }
    except Exception as e:
        log.warning("validate_params %s: %s", figi, e)
        return None


def _compute_backtest_quality(result: Dict) -> float:
    """Quality score из результата бэктеста: expectancy нормализованная."""
    trades = result.get("trades", 0)
    if trades < 5:
        return 0.0
    win_rate = result.get("win_rate", 0) / 100
    avg_pnl = result.get("total_pnl", 0) / max(1, trades)
    # Упрощённый quality: avg_pnl × win_rate
    return round(avg_pnl * win_rate, 4)


def compare_and_validate(figi: str, ticker: str, strategy_id: int,
                         current_params: Dict, new_params: Dict) -> bool:
    """
    Сравнивает бэктест с текущими и новыми параметрами.
    Возвращает True если новые параметры лучше на 10%+.
    """
    try:
        mode = new_params.get("strategy_mode") or current_params.get("strategy_mode", "mean_reversion")

        result_current = validate_params(
            figi, mode,
            current_params.get("stop_loss_pct", 0.003),
            current_params.get("take_profit_pct", 0.006),
        )
        result_new = validate_params(
            figi, mode,
            new_params.get("stop_loss_pct", 0.003),
            new_params.get("take_profit_pct", 0.006),
        )
        if not result_current or not result_new:
            return True  # нет данных → применяем (осторожно)

        q_curr = result_current.get("quality_score", 0)
        q_new  = result_new.get("quality_score", 0)

        from app.ml.optimizer import log_optimization
        log_optimization(
            figi, ticker, strategy_id,
            "backtest_validation",
            str(current_params), str(new_params),
            f"BT quality: {q_curr:.4f} → {q_new:.4f}",
            q_curr, q_new,
            applied=(q_new > q_curr * 1.05)
        )
        return q_new >= q_curr * 1.05  # минимум 5% улучшение
    except Exception as e:
        log.warning("compare_and_validate %s: %s", figi, e)
        return False
