"""
Сервис аналитика: ищет прибыльные стратегии через автоматическое тестирование.

Процесс:
  1. Строим сетку поиска: инструменты × режимы × SL × TP комбинации.
  2. Загружаем свечи один раз для каждого инструмента (кэшируется).
  3. Запускаем бэктест для каждой комбинации.
  4. Сохраняем прибыльные результаты в таблицу analyst_results.
  5. Открываем состояние для опроса дашбордом.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Состояние, разделяемое с веб-слоем ──────────────────────────────────────

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

# ── Состояние оптимизации ─────────────────────────────────────────────────────

_opt_lock = threading.Lock()
_opt_state: Dict[str, Any] = {
    "status": "idle", "run_id": None, "progress": 0, "total": 0,
    "current": "", "found": 0, "error": None,
}
_opt_stop = threading.Event()
_opt_thread: Optional[threading.Thread] = None

# ── Инструменты для перебора ─────────────────────────────────────────────────

SEARCH_INSTRUMENTS = [
    # ticker и name — canonical из T-Bank API (get_instrument_by figi)
    # ── Голубые фишки (были раньше) ──────────────────────────────────────────
    {"figi": "BBG004730N88", "ticker": "SBER",  "name": "Сбер Банк",                     "lot": 1,    "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13"},
    {"figi": "BBG004730RP0", "ticker": "GAZP",  "name": "Газпром",                       "lot": 1,    "instrument_uid": "962e2a95-02a9-4171-abd7-aa198dbe643a"},
    {"figi": "BBG004731032", "ticker": "LKOH",  "name": "ЛУКОЙЛ",                        "lot": 1,    "instrument_uid": "02cfdf61-6298-4c0f-a9ca-9cabc82afaf3"},
    {"figi": "BBG004731489", "ticker": "GMKN",  "name": "Норильский никель",              "lot": 1,    "instrument_uid": "509edd0c-129c-4ee2-934d-7f6246126da1"},
    {"figi": "BBG004731354", "ticker": "ROSN",  "name": "Роснефть",                      "lot": 1,    "instrument_uid": "fd417230-19cf-4e7b-9623-f7c9ca18ec6b"},
    {"figi": "BBG00475KHX6", "ticker": "TRNFP", "name": "Транснефть - привилегированные", "lot": 1,   "instrument_uid": "653d47e9-dbd4-407a-a1c3-47f897df4694"},
    {"figi": "BBG004S68473", "ticker": "IRAO",  "name": "ИнтерРАО",                      "lot": 100,  "instrument_uid": "2dfbc1fd-b92a-436e-b011-928c79e805f2"},
    {"figi": "BBG004730JJ5", "ticker": "MOEX",  "name": "ПАО Московская Биржа",           "lot": 1,   "instrument_uid": "5e1c2634-afc4-4e50-ad6d-f78fc14a539a"},
    {"figi": "BBG004730ZJ9", "ticker": "VTBR",  "name": "Банк ВТБ",                      "lot": 1,    "instrument_uid": "8e2b0325-0292-4654-8a18-4f63ed3b0e09"},
    {"figi": "BBG004S68B31", "ticker": "ALRS",  "name": "АЛРОСА",                        "lot": 1,    "instrument_uid": "30817fea-20e6-4fee-ab1f-d20fc1a1bb72"},
    # ── Блок C: расширенный список ───────────────────────────────────────────
    {"figi": "BBG0047315Y7", "ticker": "SBERP", "name": "Сбербанк - привилегированные",  "lot": 1,    "instrument_uid": "c190ff1f-1447-4227-b543-316332699ca5"},
    {"figi": "BBG00475KKY8", "ticker": "NVTK",  "name": "Новатэк",                       "lot": 1,    "instrument_uid": "0da66728-6c30-44c4-9264-df8fac2467ee"},
    {"figi": "BBG004RVFCY3", "ticker": "MGNT",  "name": "Магнит",                        "lot": 1,    "instrument_uid": "ca845f68-6c43-44bc-b584-330d2a1e5eb7"},
    {"figi": "BBG00475K6C3", "ticker": "CHMF",  "name": "Северсталь",                    "lot": 1,    "instrument_uid": "fa6aae10-b8d5-48c8-bbfd-d320d925d096"},
    {"figi": "BBG004S681B4", "ticker": "NLMK",  "name": "НЛМК",                          "lot": 10,   "instrument_uid": "161eb0d0-aaac-4451-b374-f5d0eeb1b508"},
    {"figi": "BBG004S681W1", "ticker": "MTSS",  "name": "МТС",                           "lot": 10,   "instrument_uid": "cd8063ad-73ad-4b31-bd0d-93138d9e99a2"},
    {"figi": "BBG000R607Y3", "ticker": "PLZL",  "name": "Полюс",                         "lot": 1,    "instrument_uid": "10620843-28ce-44e8-80c2-f26ceb1bd3e1"},
    {"figi": "BBG004S683W7", "ticker": "AFLT",  "name": "Аэрофлот",                      "lot": 10,   "instrument_uid": "1c69e020-f3b1-455c-affa-45f8b8049234"},
    {"figi": "BBG00475K2X9", "ticker": "HYDR",  "name": "РусГидро",                      "lot": 1000, "instrument_uid": "62560f05-3fd0-4d65-88f0-a27f249cc6de"},
    {"figi": "BBG004RVFFC0", "ticker": "TATN",  "name": "Татнефть",                      "lot": 1,    "instrument_uid": "88468f6c-c67a-4fb4-a006-53eed803883c"},
    {"figi": "BBG004S68507", "ticker": "MAGN",  "name": "ММК",                           "lot": 10,   "instrument_uid": "7132b1c9-ee26-4464-b5b5-1046264b61d9"},
    {"figi": "BBG004S689R0", "ticker": "PHOR",  "name": "ФосАгро",                       "lot": 1,    "instrument_uid": "9978b56f-782a-4a80-a4b1-a48cbecfd194"},
]

MODES      = ["mean_reversion", "breakout", "trend"]
SL_OPTIONS = [0.002, 0.003, 0.004, 0.005]
TP_OPTIONS = [0.004, 0.006, 0.008, 0.010, 0.015]

SCORE_THRESHOLDS = [0, 10, 20, 30, 40, 50, 60]


# ── Вспомогательная функция подбора оптимального score ───────────────────────

def _find_optimal_score(candles, mode, sl, tp, lots, budget_rub) -> int:
    """Run backtests at different score thresholds, pick the one that maximizes net_pnl × win_rate × (1-drawdown)."""
    from app.services.backtest_engine import run_backtest
    best_threshold = 0
    best_metric = -99999.0
    for threshold in SCORE_THRESHOLDS:
        try:
            res = run_backtest(
                candles=candles, mode=mode,
                stop_loss_pct=sl, take_profit_pct=tp,
                commission_pct=0.0004,
                initial_capital=float(budget_rub),
                qty=lots,
                min_signal_score=threshold,
            )
        except Exception:
            continue
        if res.total_trades < 3:
            continue
        dd_factor = 1.0 - min(res.max_drawdown_pct / 100.0, 0.9)
        metric = res.net_pnl * (res.win_rate / 100.0) * dd_factor
        if metric > best_metric:
            best_metric = metric
            best_threshold = threshold
    return best_threshold


# ── Публичный API (поиск) ────────────────────────────────────────────────────

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
    exclude_active: bool = True,
    _run_id_override: str = "",
) -> tuple[bool, str]:
    global _thread
    with _lock:
        if _state["status"] == "running":
            return False, "Аналитик уже запущен"
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker,
        args=(budget_rub, min_win_rate, min_trades, days, interval,
              min_pnl, exclude_active, _run_id_override),
        daemon=True,
        name="analyst-worker",
    )
    _thread.start()
    return True, "Запущен"


def stop() -> tuple[bool, str]:
    _stop_event.set()
    return True, "Остановка запрошена"


# ── Публичный API (оптимизация) ──────────────────────────────────────────────

def get_opt_state() -> Dict[str, Any]:
    with _opt_lock:
        return dict(_opt_state)


def start_optimize(budget_rub: float, days: int, interval: str) -> tuple[bool, str]:
    global _opt_thread
    with _opt_lock:
        if _opt_state["status"] == "running":
            return False, "Оптимизация уже запущена"
    _opt_stop.clear()
    _opt_thread = threading.Thread(
        target=_optimization_worker,
        args=(budget_rub, days, interval),
        daemon=True, name="analyst-optimize",
    )
    _opt_thread.start()
    return True, "Оптимизация запущена"


def stop_optimize() -> tuple[bool, str]:
    _opt_stop.set()
    return True, "Остановка оптимизации запрошена"


# ── Воркер (поиск) ───────────────────────────────────────────────────────────

def _upd(**kwargs):
    with _lock:
        _state.update(kwargs)


def _worker(budget_rub, min_win_rate, min_trades, days, interval, min_pnl,
            exclude_active: bool = True, _run_id_override: str = ""):
    from app.services.tbank_client import get_candles_range
    from app.services.backtest_engine import run_backtest
    from app.db import log_event, db_cursor

    run_id = _run_id_override or str(uuid.uuid4())[:8]
    _upd(status="running", run_id=run_id,
         started_at=datetime.now().isoformat(),
         progress=0, total=0, current="Инициализация…", found=0, error=None)

    try:
        # Дедуплицируем инструменты по figi
        seen: set = set()
        all_instruments = []
        for inst in SEARCH_INSTRUMENTS:
            if inst["figi"] not in seen:
                seen.add(inst["figi"])
                all_instruments.append(inst)

        # Исключаем инструменты из активных параллельных стратегий
        excluded_figis: set = set()
        if exclude_active:
            from app.db import get_setting, list_profile_parallel_strategies, list_strategy_instruments
            active_pid = get_setting("active_profile_id", "").strip()
            if active_pid:
                try:
                    for strat in list_profile_parallel_strategies(int(active_pid)):
                        for instr in list_strategy_instruments(strat["strategy_id"]):
                            excluded_figis.add(instr["figi"])
                except Exception:
                    pass

        instruments = [i for i in all_instruments if i["figi"] not in excluded_figis]
        if not instruments:
            instruments = list(all_instruments)  # fallback: don't exclude if all are excluded

        # Строим сетку поиска (только TP > SL × 1.5 для минимального risk:reward 1.5)
        combos: List[tuple] = []
        for inst in instruments:
            for mode in MODES:
                for sl in SL_OPTIONS:
                    for tp in TP_OPTIONS:
                        if tp >= sl * 1.5:
                            combos.append((inst, mode, sl, tp))

        _upd(total=len(combos))

        # Очищаем предыдущие результаты для этого нового запуска
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

            # ── Загрузка свечей (кэшируется для каждого инструмента) ──────
            if figi not in candles_cache:
                try:
                    uid = inst.get("instrument_uid", "") or ""
                    candles_cache[figi] = get_candles_range(
                        figi=uid or figi, interval_name=interval, days=days
                    )
                except Exception:
                    candles_cache[figi] = []

            candles = candles_cache.get(figi, [])
            if len(candles) < 30:
                continue

            # ── Лоты: рассчитываются из бюджета один раз для инструмента (кэш) ─
            closes    = [c["close"] for c in candles if c.get("close")]
            avg_price = round(sum(closes) / len(closes), 4) if closes else 0.0
            if avg_price > 0 and budget_rub > 0:
                lots = max(1, int((budget_rub * 0.95) // avg_price))
            else:
                lots = 1

            # ── Бэктест (использует реалистичный размер лота) ────────────
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

            # ── Фильтр ───────────────────────────────────────────────────
            if res.total_trades < min_trades:
                continue
            if res.net_pnl < min_pnl:
                continue
            if res.win_rate < min_win_rate:
                continue

            # Подбираем оптимальный score для этой комбинации
            optimal_score = _find_optimal_score(candles, mode, sl, tp, lots, float(budget_rub))

            # Комплексный скор: profit_factor × win_rate × (1 - max_dd%)
            dd_penalty = 1.0 - min(res.max_drawdown_pct / 100.0, 0.9)
            score = round(res.profit_factor * (res.win_rate / 100) * dd_penalty * 100, 2)

            # Прореживаем кривую капитала до ≤200 точек
            curve = res.equity_curve
            if len(curve) > 200:
                step = len(curve) // 200
                curve = curve[::step]
            eq_json = json.dumps([round(v, 2) for v in curve])

            uid = inst.get("instrument_uid", "") or ""
            with db_cursor() as cur:
                cur.execute("""
                INSERT INTO analyst_results(
                    run_id, found_at, tradingmode, figi, ticker, instrument_name,
                    interval, days, sl_pct, tp_pct, budget_rub,
                    net_pnl, win_rate, profit_factor, total_trades,
                    max_drawdown, avg_r_multiple, sharpe_ratio,
                    equity_curve, score, avg_price, min_signal_score_used, instrument_uid
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, datetime.now().isoformat(),
                    mode, figi, ticker, inst["name"],
                    interval, days, str(sl), str(tp), float(budget_rub),
                    res.net_pnl, res.win_rate, res.profit_factor, res.total_trades,
                    res.max_drawdown, res.avg_r_multiple, res.sharpe_ratio,
                    eq_json, score, avg_price, optimal_score, uid,
                ))

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


# ── Воркер оптимизации ───────────────────────────────────────────────────────

def _opt_upd(**kwargs):
    with _opt_lock:
        _opt_state.update(kwargs)


def _optimization_worker(budget_rub: float, days: int, interval: str):
    from app.services.tbank_client import get_candles_range
    from app.services.backtest_engine import run_backtest
    from app.db import (db_cursor, get_setting, log_event,
                        list_profile_parallel_strategies, list_strategy_instruments,
                        get_strategy_settings)

    run_id = str(uuid.uuid4())[:8]
    _opt_upd(status="running", run_id=run_id, progress=0, total=0,
             current="Инициализация…", found=0, error=None)

    try:
        # Get active profile's parallel strategies and instruments
        active_pid = get_setting("active_profile_id", "").strip()
        if not active_pid:
            _opt_upd(status="error", error="Нет активного профиля")
            return

        parallel_strats = list_profile_parallel_strategies(int(active_pid))
        if not parallel_strats:
            _opt_upd(status="error", error="Нет параллельных стратегий в профиле")
            return

        # Build work items: (strategy_id, strategy_name, instrument, current_settings)
        work_items = []
        for strat in parallel_strats:
            sid = strat["strategy_id"]
            strat_settings = get_strategy_settings(sid)
            instruments = list_strategy_instruments(sid)
            for instr in instruments:
                if str(instr.get("enabled", 1)) not in ("1", "true"):
                    continue
                work_items.append({
                    "strategy_id": sid,
                    "strategy_name": strat["name"],
                    "figi": instr["figi"],
                    "ticker": instr["ticker"],
                    "name": instr.get("name", instr["ticker"]),
                    "current_mode": strat_settings.get("tradingmode", "trend"),
                    "current_sl": float(strat_settings.get("default_stop_loss_pct", "0.0025")),
                    "current_tp": float(strat_settings.get("default_take_profit_pct", "0.005")),
                })

        _opt_upd(total=len(work_items))

        # Clear old optimization results
        with db_cursor() as cur:
            cur.execute("DELETE FROM optimization_results")

        candles_cache: Dict[str, list] = {}

        # Param grid for alternatives
        opt_modes = ["mean_reversion", "breakout", "trend"]
        opt_sl = [0.002, 0.003, 0.004, 0.005]
        opt_tp = [0.004, 0.006, 0.008, 0.010, 0.015]

        found = 0
        for idx, item in enumerate(work_items):
            if _opt_stop.is_set():
                _opt_upd(status="stopped", current="Остановлено пользователем")
                return

            figi = item["figi"]
            ticker = item["ticker"]
            _opt_upd(progress=idx + 1, current=f"{ticker} — загрузка свечей…")

            # Load candles (cached per figi)
            if figi not in candles_cache:
                try:
                    candles_cache[figi] = get_candles_range(figi=figi, interval_name=interval, days=days)
                except Exception:
                    candles_cache[figi] = []
            candles = candles_cache.get(figi, [])
            if len(candles) < 30:
                continue

            # Calculate lots
            closes = [c["close"] for c in candles if c.get("close")]
            avg_price = round(sum(closes) / len(closes), 4) if closes else 0.0
            lots = max(1, int((budget_rub * 0.95) // avg_price)) if avg_price > 0 else 1

            # Baseline with current settings
            _opt_upd(current=f"{ticker} — бэктест с текущими настройками…")
            try:
                base_res = run_backtest(
                    candles=candles,
                    mode=item["current_mode"],
                    stop_loss_pct=item["current_sl"],
                    take_profit_pct=item["current_tp"],
                    commission_pct=0.0004,
                    initial_capital=float(budget_rub),
                    qty=lots,
                )
            except Exception:
                base_res = None

            base_pnl = base_res.net_pnl if base_res else 0.0
            base_trades = base_res.total_trades if base_res else 0
            base_wr = base_res.win_rate if base_res else 0.0

            # Grid search for best alternative
            best_res = None
            best_mode_found = item["current_mode"]
            best_sl_found = item["current_sl"]
            best_tp_found = item["current_tp"]
            best_score_found = 0
            best_metric = -99999.0

            combos_count = sum(1 for m in opt_modes for sl in opt_sl for tp in opt_tp if tp >= sl * 1.5)
            processed = 0

            for mode in opt_modes:
                for sl in opt_sl:
                    for tp in opt_tp:
                        if _opt_stop.is_set():
                            break
                        if tp < sl * 1.5:
                            continue
                        processed += 1
                        _opt_upd(current=f"{ticker} — {mode} SL={sl*100:.1f}% TP={tp*100:.1f}% ({processed}/{combos_count})")
                        try:
                            res = run_backtest(
                                candles=candles, mode=mode,
                                stop_loss_pct=sl, take_profit_pct=tp,
                                commission_pct=0.0004,
                                initial_capital=float(budget_rub),
                                qty=lots,
                            )
                        except Exception:
                            continue
                        if res.total_trades < 3:
                            continue
                        dd_factor = 1.0 - min(res.max_drawdown_pct / 100.0, 0.9)
                        metric = res.profit_factor * (res.win_rate / 100) * dd_factor * res.net_pnl
                        if metric > best_metric:
                            best_metric = metric
                            best_res = res
                            best_mode_found = mode
                            best_sl_found = sl
                            best_tp_found = tp

            if best_res is None:
                continue

            # Find optimal score for best settings
            _opt_upd(current=f"{ticker} — подбор оптимального score…")
            best_score_found = _find_optimal_score(candles, best_mode_found, best_sl_found, best_tp_found, lots, float(budget_rub))

            improvement_pct = 0.0
            if abs(base_pnl) > 0.01:
                improvement_pct = round((best_res.net_pnl - base_pnl) / abs(base_pnl) * 100, 1)
            elif best_res.net_pnl > 0:
                improvement_pct = 100.0

            with db_cursor() as cur:
                cur.execute("""
                INSERT INTO optimization_results(
                    run_id, created_at, strategy_id, strategy_name, figi, ticker, instrument_name,
                    base_mode, base_sl, base_tp, base_pnl, base_trades, base_win_rate,
                    best_mode, best_sl, best_tp, best_pnl, best_trades, best_win_rate,
                    best_profit_factor, best_min_signal_score, improvement_pct
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, datetime.now().isoformat(),
                    item["strategy_id"], item["strategy_name"],
                    figi, ticker, item["name"],
                    item["current_mode"], item["current_sl"], item["current_tp"],
                    base_pnl, base_trades, base_wr,
                    best_mode_found, best_sl_found, best_tp_found,
                    best_res.net_pnl, best_res.total_trades, best_res.win_rate,
                    best_res.profit_factor, best_score_found, improvement_pct,
                ))
            found += 1
            _opt_upd(found=found)

        _opt_upd(status="done", current=f"Оптимизация завершена. Проверено: {len(work_items)}")
        log_event("ANALYST", f"Оптимизация завершена, run_id={run_id}, улучшено={found}")

    except Exception as exc:
        _opt_upd(status="error", error=str(exc))
        try:
            from app.db import log_event
            log_event("ANALYST", f"Ошибка оптимизации: {exc}", level="ERROR")
        except Exception:
            pass


# ── Вспомогательные функции для результатов ──────────────────────────────────

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
    """Создаёт новую стратегию из результата аналитика. Возвращает strategy_id."""
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
    min_score  = int(result.get("min_signal_score_used") or 0)

    # Лоты, помещающиеся в бюджет с запасом безопасности 5%, гарантирующим покрытие
    if avg_price > 0 and budget_rub > 0:
        lots = max(1, int((budget_rub * 0.95) // avg_price))
    else:
        lots = 1

    settings = {
        "tradingmode":              mode,
        "default_stop_loss_pct":    str(sl),
        "default_take_profit_pct":  str(tp),
        "estimated_commission_pct": "0.0004",
        "min_signal_score":         str(min_score),
        "max_trades_per_day":       "10",
        "max_open_positions":       "2",
        "max_daily_loss_rub":       "500",
        "check_interval_sec":       "5",
        "allow_long_global":        "1",
        "allow_short_global":       "0",
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

        # Добавляем инструмент с instrument_uid
        uid = result.get("instrument_uid", "") or ""
        cur.execute("""
        INSERT OR IGNORE INTO strategy_instruments(
            strategy_id, figi, ticker, name,
            lot, lots_override, stop_loss_pct, take_profit_pct,
            allow_long, allow_short, enabled, priority, instrument_uid
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 1, 1, 1, 100, ?)
        """, (sid, result["figi"], result["ticker"], result["instrument_name"],
              lots, str(sl), str(tp), uid))

        # Помечаем результат как сохранённый
        cur.execute(
            "UPDATE analyst_results SET saved_strategy_id = ? WHERE id = ?",
            (sid, result_id)
        )

    return sid
