from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Trade:
    entry_idx: int
    entry_price: float
    direction: str
    qty: int
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0
    commission: float = 0.0
    entry_time: str = ""
    exit_time: str = ""


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_r_multiple: float = 0.0
    sharpe_ratio: float = 0.0
    total_commission: float = 0.0
    candles_tested: int = 0


def run_backtest(
    candles: List[Dict[str, Any]],
    mode: str = "trend",
    stop_loss_pct: float = 0.0025,
    take_profit_pct: float = 0.005,
    commission_pct: float = 0.0004,
    initial_capital: float = 100_000.0,
    qty: int = 1,
) -> BacktestResult:
    """
    Event-driven backtest on OHLCV candles using strategy_engine signals.
    Mirrors live bot logic: any BUY/SELL action opens a position (no score gate).

    Per bar:
      1. If position open — check SL/TP against bar high/low.
      2. If no position — evaluate signal on candles[0..i].
      3. On BUY/SELL signal — open position at bar close.
    One position at a time; commission charged on entry and exit.
    """
    from app.services.strategy_engine import evaluate_signal

    result = BacktestResult()
    result.candles_tested = len(candles)

    min_candles = {"trend": 30, "mean_reversion": 25, "breakout": 25}.get(mode, 30)

    position: Optional[Trade] = None
    capital = initial_capital
    equity_curve: List[float] = [capital]

    for i in range(min_candles, len(candles)):
        bar = candles[i]
        bar_high = float(bar.get("high") or 0)
        bar_low = float(bar.get("low") or 0)
        bar_close = float(bar.get("close") or 0)
        bar_time = bar.get("time", "")

        if bar_close <= 0:
            equity_curve.append(capital)
            continue

        # ── Check SL / TP on open position ───────────────────────────────────
        if position is not None:
            ep = position.entry_price
            if position.direction == "BUY":
                sl = ep * (1.0 - stop_loss_pct)
                tp = ep * (1.0 + take_profit_pct)
                hit_sl = bar_low <= sl
                hit_tp = bar_high >= tp
            else:
                sl = ep * (1.0 + stop_loss_pct)
                tp = ep * (1.0 - take_profit_pct)
                hit_sl = bar_high >= sl
                hit_tp = bar_low <= tp

            exit_price: Optional[float] = None
            exit_reason = ""
            if hit_sl:
                exit_price, exit_reason = sl, "SL"
            elif hit_tp:
                exit_price, exit_reason = tp, "TP"

            if exit_price is not None:
                if position.direction == "BUY":
                    raw_pnl = (exit_price - ep) * position.qty
                else:
                    raw_pnl = (ep - exit_price) * position.qty
                exit_comm = exit_price * position.qty * commission_pct
                net_pnl = raw_pnl - exit_comm
                position.exit_idx = i
                position.exit_price = round(exit_price, 4)
                position.exit_reason = exit_reason
                position.exit_time = bar_time
                position.commission += exit_comm
                position.pnl = net_pnl
                capital += net_pnl
                result.trades.append(position)
                position = None
                equity_curve.append(capital)
                continue

        equity_curve.append(capital)

        # ── Open new position if none ─────────────────────────────────────────
        if position is None:
            window = candles[: i + 1]
            sig = evaluate_signal(mode, window)
            action = sig.get("action", "HOLD")

            if action in ("BUY", "SELL"):
                entry_comm = bar_close * qty * commission_pct
                position = Trade(
                    entry_idx=i,
                    entry_price=bar_close,
                    direction=action,
                    qty=qty,
                    commission=entry_comm,
                    entry_time=bar_time,
                )
                capital -= entry_comm

    # Close any leftover position at the last bar
    if position is not None and candles:
        last_close = float(candles[-1].get("close") or 0)
        if last_close > 0:
            if position.direction == "BUY":
                raw_pnl = (last_close - position.entry_price) * position.qty
            else:
                raw_pnl = (position.entry_price - last_close) * position.qty
            exit_comm = last_close * position.qty * commission_pct
            position.exit_idx = len(candles) - 1
            position.exit_price = round(last_close, 4)
            position.exit_reason = "END"
            position.exit_time = candles[-1].get("time", "")
            position.commission += exit_comm
            position.pnl = raw_pnl - exit_comm
            capital += position.pnl
            result.trades.append(position)

    result.equity_curve = equity_curve

    # ── Metrics ───────────────────────────────────────────────────────────────
    trades = result.trades
    result.total_trades = len(trades)
    if trades:
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        result.win_trades = len(wins)
        result.loss_trades = len(losses)
        result.win_rate = round(len(wins) / len(trades) * 100, 1)
        result.gross_profit = round(sum(t.pnl for t in wins), 2)
        result.gross_loss = round(abs(sum(t.pnl for t in losses)), 2)
        result.profit_factor = round(
            result.gross_profit / max(0.0001, result.gross_loss), 2
        )
        result.net_pnl = round(sum(t.pnl for t in trades), 2)
        result.total_commission = round(sum(t.commission for t in trades), 2)

        # R-multiple: pnl / (entry × sl_pct × qty)
        r_list = []
        for t in trades:
            risk = t.entry_price * stop_loss_pct * t.qty
            if risk > 0:
                r_list.append(t.pnl / risk)
        result.avg_r_multiple = round(sum(r_list) / len(r_list), 2) if r_list else 0.0

        # Max drawdown
        peak = initial_capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown = round(max_dd, 2)
        result.max_drawdown_pct = round(max_dd / initial_capital * 100, 2)

        # Sharpe (annualised, bar-level returns, 252*390 min-bars/year → use count)
        if len(equity_curve) > 1:
            rets = [
                (equity_curve[i] - equity_curve[i - 1]) / max(0.0001, equity_curve[i - 1])
                for i in range(1, len(equity_curve))
            ]
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
            std_r = var ** 0.5
            bars_per_year = 252 * 7  # rough for intraday bars
            result.sharpe_ratio = round(
                (mean_r / std_r) * (bars_per_year ** 0.5) if std_r > 0 else 0.0, 2
            )

    return result


def result_to_dict(res: BacktestResult, candles: List[Dict]) -> Dict[str, Any]:
    """Serialize BacktestResult to a JSON-safe dict."""
    trades_out = []
    for t in res.trades:
        entry_time = t.entry_time or (candles[t.entry_idx].get("time", "") if t.entry_idx < len(candles) else "")
        exit_time = t.exit_time or (candles[t.exit_idx].get("time", "") if t.exit_idx is not None and t.exit_idx < len(candles) else "")
        trades_out.append({
            "entry_time": entry_time,
            "exit_time": exit_time,
            "direction": t.direction,
            "entry_price": round(t.entry_price, 4),
            "exit_price": round(t.exit_price, 4) if t.exit_price else None,
            "exit_reason": t.exit_reason,
            "qty": t.qty,
            "pnl": round(t.pnl, 2),
            "commission": round(t.commission, 2),
        })

    # Downsample equity curve to ≤500 points to keep response small
    curve = res.equity_curve
    if len(curve) > 500:
        step = len(curve) // 500
        curve = curve[::step]

    return {
        "total_trades": res.total_trades,
        "win_trades": res.win_trades,
        "loss_trades": res.loss_trades,
        "win_rate": res.win_rate,
        "gross_profit": res.gross_profit,
        "gross_loss": res.gross_loss,
        "profit_factor": res.profit_factor,
        "net_pnl": res.net_pnl,
        "max_drawdown": res.max_drawdown,
        "max_drawdown_pct": res.max_drawdown_pct,
        "avg_r_multiple": res.avg_r_multiple,
        "sharpe_ratio": res.sharpe_ratio,
        "total_commission": res.total_commission,
        "candles_tested": res.candles_tested,
        "equity_curve": [round(v, 2) for v in curve],
        "trades": trades_out,
    }
