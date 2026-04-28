"""
Analyst service: searches for profitable strategies via automated backtesting.

Flow:
  1. Build a search grid: instruments × modes × SL × TP combinations.
  2. Load candles once per instrument (cached).
  3. Run backtest for every combination.
  4. Store profitable results in analyst_results table.
  5. Expose state for the dashboard to poll.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── State shared with the web layer ──────────────────────────────────────────

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "status": "idle",       # idle | running | stopped | done | error
    "run_id": None,
    "started_at": None,
    "progress": 0,
    "total": 0,
    "current": "",
    "found": 0,
    "error": None,
}
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None

# ── Instruments to search across ─────────────────────────────────────────────

SEARCH_INSTRUMENTS = [
    {"figi": "BBG004730N88", "ticker": "SBER", "name": "Сбербанк",         "lot": 1},
    {"figi": "BBG004730RP0", "ticker": "GAZP", "name": "Газпром",          "lot": 1},
    {"figi": "BBG004731032", "ticker": "LKOH", "name": "Лукойл",           "lot": 1},
    {"figi": "BBG004731489", "ticker": "GMKN", "name": "Норникель",        "lot": 1},
    {"figi": "BBG004731354", "ticker": "ROSN", "name": "Роснефть",         "lot": 1},
    {"figi": "BBG004RVFN70", "ticker": "TATN", "name": "Татнефть",         "lot": 1},
    {"figi": "BBG00475KHX6", "ticker": "NVTK", "name": "НОВАТЭК",          "lot": 1},
    {"figi": "BBG004S68473", "ticker": "MTSS", "name": "МТС",              "lot": 1},
    {"figi": "BBG004730JJ5", "ticker": "MOEX", "name": "Московская биржа", "lot": 1},
    {"figi": "BBG004730ZJ9", "ticker": "VTBR", "name": "ВТБ",              "lot": 1},
    {"figi": "BBG004S68B31", "ticker": "ALRS", "name": "АЛРОСА",           "lot": 1},
    {"figi": "BBG004731354", "ticker": "ROSN", "name": "Роснефть",         "lot": 1},
]

MODES      = ["mean_reversion", "breakout", "trend"]
SL_OPTIONS = [0.002, 0.003, 0.004, 0.005]
TP_OPTIONS = [0.004, 0.006, 0.008, 0.010, 0.015]


# ── Public API ────────────────────────────────────────────────────────────────

def get_state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def start(
    budget_rub: float,
    min_win_rate: float,
    min_trades: int,
    days: int,
    interval: str,
    min_pnl: float,
) -> tuple[bool, str]:
    global _thread
    with _lock:
        if _state["status"] == "running":
            return False, "Аналитик уже запущен"
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker,
        args=(budget_rub, min_win_rate, min_trades, days, interval, min_pnl),
        daemon=True,
        name="analyst-worker",
    )
    _thread.start()
    return True, "Запущен"


def stop() -> tuple[bool, str]:
    _stop_event.set()
    return True, "Остановка запрошена"


# ── Worker ────────────────────────────────────────────────────────────────────

def _upd(**kwargs):
    with _lock:
        _state.update(kwargs)


def _worker(budget_rub, min_win_rate, min_trades, days, interval, min_pnl):
    from app.services.tbank_client import get_candles_range
    from app.services.backtest_engine import run_backtest
    from app.db import log_event, db_cursor

    run_id = str(uuid.uuid4())[:8]
    _upd(status="running", run_id=run_id,
         started_at=datetime.now().isoformat(),
         progress=0, total=0, current="Инициализация…", found=0, error=None)

    try:
        # Deduplicate instruments by figi
        seen: set = set()
        instruments = []
        for inst in SEARCH_INSTRUMENTS:
            if inst["figi"] not in seen:
                seen.add(inst["figi"])
                instruments.append(inst)

        # Build search grid (only TP > SL × 1.5 for min risk:reward 1.5)
        combos: List[tuple] = []
        for inst in instruments:
            for mode in MODES:
                for sl in SL_OPTIONS:
                    for tp in TP_OPTIONS:
                        if tp >= sl * 1.5:
                            combos.append((inst, mode, sl, tp))

        _upd(total=len(combos))

        # Clear previous results for this fresh run
        with db_cursor() as cur:
            cur.execute("DELETE FROM analyst_results")

        candles_cache: Dict[str, list] = {}
        found = 0

        for i, (inst, mode, sl, tp) in enumerate(combos):
            if _stop_event.is_set():
                _upd(status="stopped", current="Остановлено пользователем")
                return

            figi   = inst["figi"]
            ticker = inst["ticker"]
            _upd(progress=i + 1,
                 current=f"{ticker} | {mode} | SL={sl*100:.2f}% TP={tp*100:.2f}%")

            # ── Load candles (cached per instrument) ──────────────────────
            if figi not in candles_cache:
                try:
                    candles_cache[figi] = get_candles_range(
                        figi=figi, interval_name=interval, days=days
                    )
                except Exception:
                    candles_cache[figi] = []

            candles = candles_cache.get(figi, [])
            if len(candles) < 30:
                continue

            # ── Lots: calculated from budget once per instrument (cached) ─
            closes    = [c["close"] for c in candles if c.get("close")]
            avg_price = round(sum(closes) / len(closes), 4) if closes else 0.0
            if avg_price > 0 and budget_rub > 0:
                lots = max(1, int((budget_rub * 0.95) // avg_price))
            else:
                lots = 1

            # ── Backtest (uses realistic lot size) ───────────────────────
            try:
                res = run_backtest(
                    candles=candles,
                    mode=mode,
                    stop_loss_pct=sl,
                    take_profit_pct=tp,
                    commission_pct=0.0004,
                    initial_capital=float(budget_rub),
                    qty=lots,
                )
            except Exception:
                continue

            # ── Filter ───────────────────────────────────────────────────
            if res.total_trades < min_trades:
                continue
            if res.net_pnl < min_pnl:
                continue
            if res.win_rate < min_win_rate:
                continue

            # Composite score: profit_factor × win_rate × (1 - max_dd%)
            dd_penalty = 1.0 - min(res.max_drawdown_pct / 100.0, 0.9)
            score = round(res.profit_factor * (res.win_rate / 100) * dd_penalty * 100, 2)

            # Downsample equity curve to ≤200 points
            curve = res.equity_curve
            if len(curve) > 200:
                step = len(curve) // 200
                curve = curve[::step]
            eq_json = json.dumps([round(v, 2) for v in curve])

            with db_cursor() as cur:
                cur.execute("""
                INSERT INTO analyst_results(
                    run_id, found_at, tradingmode, figi, ticker, instrument_name,
                    interval, days, sl_pct, tp_pct, budget_rub,
                    net_pnl, win_rate, profit_factor, total_trades,
                    max_drawdown, avg_r_multiple, sharpe_ratio,
                    equity_curve, score, avg_price
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, datetime.now().isoformat(),
                    mode, figi, ticker, inst["name"],
                    interval, days, str(sl), str(tp), float(budget_rub),
                    res.net_pnl, res.win_rate, res.profit_factor, res.total_trades,
                    res.max_drawdown, res.avg_r_multiple, res.sharpe_ratio,
                    eq_json, score, avg_price,
                ))
            # lots stored in avg_price → recalculated on save; no extra column needed

            found += 1
            _upd(found=found)
            time.sleep(0.005)

        _upd(status="done", current=f"Завершено. Найдено: {found}")
        log_event("ANALYST", f"Поиск завершён, run_id={run_id}, найдено={found}")

    except Exception as exc:
        _upd(status="error", error=str(exc))
        try:
            from app.db import log_event
            log_event("ANALYST", f"Ошибка: {exc}", level="ERROR")
        except Exception:
            pass


# ── Results helpers ───────────────────────────────────────────────────────────

def get_results(limit: int = 50) -> List[Dict[str, Any]]:
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM analyst_results
        WHERE saved_strategy_id IS NULL
        ORDER BY score DESC
        LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_result_by_id(result_id: int) -> Optional[Dict[str, Any]]:
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT * FROM analyst_results WHERE id = ?", (result_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def save_as_strategy(result_id: int, strategy_name: str) -> int:
    """Create a new Strategy from an analyst result. Returns strategy_id."""
    from app.db import db_cursor, STRATEGY_SETTING_KEYS

    result = get_result_by_id(result_id)
    if not result:
        raise ValueError(f"Результат {result_id} не найден")

    name = (strategy_name or "").strip()
    if not name:
        raise ValueError("Имя стратегии не может быть пустым")

    sl         = float(result["sl_pct"])
    tp         = float(result["tp_pct"])
    mode       = result["tradingmode"]
    budget_rub = float(result.get("budget_rub") or 0)
    avg_price  = float(result.get("avg_price") or 0)

    # Lots that fit in budget with a 5% safety margin so money is guaranteed to cover
    if avg_price > 0 and budget_rub > 0:
        lots = max(1, int((budget_rub * 0.95) // avg_price))
    else:
        lots = 1

    settings = {
        "tradingmode":              mode,
        "default_stop_loss_pct":    str(sl),
        "default_take_profit_pct":  str(tp),
        "estimated_commission_pct": "0.0004",
        "min_signal_score":         "50" if mode == "mean_reversion" else ("40" if mode == "breakout" else "0"),
        "max_trades_per_day":       "10",
        "max_open_positions":       "2",
        "max_daily_loss_rub":       "500",
        "check_interval_sec":       "5",
        "allow_long_global":        "1",
        "allow_short_global":       "0",
        "trade_only_session":       "1",
        "pause_after_error_sec":    "10",
        "errorseriespausecount":    "3",
        "stopseriespausecount":     "3",
        "trailing_stop_enabled":    "1" if mode == "breakout" else "0",
        "use_signal_service":       "0",
    }

    with db_cursor() as cur:
        cur.execute("SELECT id FROM strategies WHERE name = ?", (name,))
        if cur.fetchone():
            raise ValueError(f"Стратегия «{name}» уже существует")

        cur.execute("INSERT INTO strategies(name) VALUES (?)", (name,))
        sid = cur.lastrowid

        for key, value in settings.items():
            if key in STRATEGY_SETTING_KEYS:
                cur.execute("""
                INSERT INTO strategy_settings(strategy_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(strategy_id, key) DO UPDATE SET value = excluded.value
                """, (sid, key, value))

        # Add the instrument
        cur.execute("""
        INSERT OR IGNORE INTO strategy_instruments(
            strategy_id, figi, ticker, name,
            lot, lots_override, stop_loss_pct, take_profit_pct,
            allow_long, allow_short, enabled, priority
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 1, 1, 1, 100)
        """, (sid, result["figi"], result["ticker"], result["instrument_name"],
              lots, str(sl), str(tp)))

        # Mark result as saved
        cur.execute(
            "UPDATE analyst_results SET saved_strategy_id = ? WHERE id = ?",
            (sid, result_id)
        )

    return sid
