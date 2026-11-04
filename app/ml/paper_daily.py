"""Бумажные счета дневной стратегии: проверка правил на живых ценах без ордеров брокера.

Правила зафиксированы заранее и не подбираются по результатам (см. docs/superpowers/plans/2026-11-03-*.md).
Раз в неделю, если корзина бумаг выше своей средней за 50 дней, держим лидеров по доходности за 20 дней поровну.
  Счёт A: 3 лидера из 12 бумаг, иначе кеш. Эталон: те же 12 бумаг, купить и держать.
  Счёт B: 8 лидеров из 40 самых ликвидных, иначе фонд денежного рынка. Эталон: фонд денежного рынка.
Данные передаются снаружи: модуль не знает про SDK.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

MSK = timezone(timedelta(hours=3))

MOMENTUM_DAYS = 20
SMA_DAYS = 50
TOP_N = 3
TOP_B = 8                     # счёт B: сколько лидеров
LIQUID_B = 40                 # счёт B: из скольких самых ликвидных бумаг
ADV_DAYS = 60                 # окно среднего оборота
ADV_MIN_DAYS = 40             # меньше дней истории — бумагу в список не берём
CASH_TICKER = "LQDT"          # счёт B: где лежит кеш
ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3}   # счёт C: фьючерсы, см. paper_futures.py
COST = 0.00061                # издержки от оборота: комиссия 0.05% и половина спреда 0.011%
START_EQUITY = 100000.0
WINDOW_FROM_MIN = 10 * 60 + 5   # не в первые минуты сессии
WINDOW_TO_MIN = 18 * 60 + 40

# Те же 12 бумаг, на которых правило проверялось; список не зависит от настроек стратегий
UNIVERSE = {
    "SBER": "BBG004730N88", "GMKN": "BBG004731489", "TRNFP": "BBG00475KHX6", "IRAO": "BBG004S68473",
    "MOEX": "BBG004730JJ5", "GAZP": "BBG004730RP0", "ALRS": "BBG004S68B31", "VTBR": "BBG004730ZJ9",
    "TATN": "BBG004RVFFC0", "ROSN": "BBG004731354", "NVTK": "BBG00475KKY8", "AFLT": "BBG004S683W7",
}


def pick_cash_instrument(etfs, ticker: str = CASH_TICKER):
    """Торгуемая запись фонда: у брокера у одного тикера бывает несколько записей, старые без цен. None — такой нет."""
    return next((e for e in etfs if e.ticker == ticker and e.api_trade_available_flag), None)


def select_liquid(bars: Dict[str, list], n: int = LIQUID_B) -> List[str]:
    """Самые ликвидные бумаги по среднему обороту. bars: тикер -> [(закрытие, объём в лотах, лот), ...] по дням."""
    turnover = {}
    for ticker, rows in bars.items():
        recent = rows[-ADV_DAYS:]
        if len(recent) >= ADV_MIN_DAYS:
            turnover[ticker] = sum(c * v * lot for c, v, lot in recent) / len(recent)
    return sorted(turnover, key=turnover.get, reverse=True)[:n]


def target_weights(closes: Dict[str, List[float]], top: int = TOP_N) -> Optional[Dict[str, float]]:
    """Целевые веса по закрытиям завершённых дней. None — истории не хватает, {} — кеш."""
    usable = {t: c for t, c in closes.items() if len(c) >= SMA_DAYS + 1}
    if len(usable) < top:
        return None
    # корзина с равными весами, нормированная на начало окна средней
    index = [sum(c[-SMA_DAYS + i] / c[-SMA_DAYS] for c in usable.values()) / len(usable) for i in range(SMA_DAYS)]
    if index[-1] <= sum(index) / SMA_DAYS:
        return {}
    momentum = {t: c[-1] / c[-1 - MOMENTUM_DAYS] - 1.0 for t, c in usable.items()}
    best = sorted(momentum, key=momentum.get, reverse=True)[:top]
    return {t: 1.0 / top for t in best}


def equity(state: dict, prices: Dict[str, float]) -> float:
    return state["cash"] + sum(q * prices[t] for t, q in state["holdings"].items())


def rebalance(state: dict, target: Dict[str, float], prices: Dict[str, float], cost: float = COST) -> None:
    """Приводит счёт к целевым весам; издержки берутся с фактического оборота. Нет цены — ValueError, счёт не меняется."""
    for t in set(target) | set(state["holdings"]):
        if not prices.get(t):
            raise ValueError(f"нет цены: {t}")
    total = equity(state, prices)
    holdings, cash = dict(state["holdings"]), state["cash"]
    needs = {}
    for t in set(target) | set(holdings):
        current = holdings.get(t, 0.0) * prices[t]
        wanted = target.get(t, 0.0) * total
        if abs(wanted - current) < 1e-9 * max(total, 1.0) * 1e3:
            continue                               # без сделки: копеечный оборот не считаем
        if current > wanted:
            cash += (current - wanted) * (1 - cost)
            if wanted > 0:
                holdings[t] = wanted / prices[t]
            else:
                holdings.pop(t, None)
        else:
            needs[t] = wanted - current
    need_total = sum(needs.values())
    scale = min(1.0, cash / need_total) if need_total > 0 else 0.0
    for t, need in needs.items():
        amount = need * scale
        holdings[t] = holdings.get(t, 0.0) + amount * (1 - cost) / prices[t]
        cash -= amount
    state["holdings"], state["cash"] = holdings, max(cash, 0.0)


def in_window(now: datetime) -> bool:
    n = now.astimezone(MSK)
    minutes = n.hour * 60 + n.minute
    return n.weekday() < 5 and WINDOW_FROM_MIN <= minutes <= WINDOW_TO_MIN


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(MSK).isocalendar()
    return f"{year}-{week:02d}"


# ── хранение ──────────────────────────────────────────────────────────────────

def _load(account: str = "A") -> Optional[dict]:
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT state_json FROM paper_daily_account WHERE id=?", (ACCOUNT_IDS[account],))
        row = cur.fetchone()
    return json.loads(row[0]) if row else None


def _save(name: str, account: dict, now: datetime, target: dict, prices: dict, eq: float, bench: float) -> None:
    from app.db import db_cursor
    stamp = now.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        cur.execute("INSERT OR REPLACE INTO paper_daily_account(id, state_json, updated_at) VALUES (?, ?, ?)",
                    (ACCOUNT_IDS[name], json.dumps(account), stamp))
        cur.execute("INSERT INTO paper_daily_log(ts, week_key, target_json, prices_json, equity, benchmark_equity, account) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (stamp, account["last_week"], json.dumps(target), json.dumps(prices), eq, bench, name))


def run_step(now: datetime, closes: Callable[[], Dict[str, List[float]]], live_prices: Callable[[], Dict[str, float]],
             account: str = "A", top: int = TOP_N, cash_ticker: Optional[str] = None,
             dividends: Optional[Callable[[dict, str], float]] = None) -> Optional[str]:
    """Один шаг счёта: 'rebalanced', 'no_history', 'no_prices' или None (сейчас делать нечего).

    cash_ticker — кеш лежит в этом фонде, он же эталон; без него эталон — равные веса всех бумаг.
    dividends(holdings, с_даты) — выплаты после налога за время с прошлого ребаланса, зачисляются до него.
    """
    if not in_window(now):
        return None
    name, state = account, _load(account)
    week = week_key(now)
    if state and state["last_week"] == week:
        return None
    history = closes()
    target = target_weights(history, top)
    if target is None:
        return "no_history"
    if not target and cash_ticker:
        target = {cash_ticker: 1.0}
    prices = live_prices()
    known = set(history) | set(target) | set((state or {}).get("holdings", {}))
    if cash_ticker:
        known.add(cash_ticker)
    if any(not prices.get(t) for t in known):
        return "no_prices"
    if state is None:
        state = {"cash": START_EQUITY, "holdings": {}, "last_week": None, "last_date": None,
                 "benchmark": {"cash": START_EQUITY, "holdings": {}}}
        bench = {cash_ticker: 1.0} if cash_ticker else {t: 1.0 / len(history) for t in history}
        rebalance(state["benchmark"], bench, prices)
    elif dividends and state.get("last_date"):
        state["cash"] += dividends(state["holdings"], state["last_date"])
        state["benchmark"]["cash"] += dividends(state["benchmark"]["holdings"], state["last_date"])
    rebalance(state, target, prices)
    state["last_week"] = week
    state["last_date"] = now.astimezone(MSK).strftime("%Y-%m-%d")
    _save(name, state, now, target, {t: prices[t] for t in known}, equity(state, prices), equity(state["benchmark"], prices))
    return "rebalanced"


def status(account: str = "A") -> Optional[dict]:
    """Состояние счёта на момент последнего ребаланса; None — счёт ещё не открыт."""
    from app.db import db_cursor
    state = _load(account)
    if state is None:
        return None
    with db_cursor() as cur:
        cur.execute("SELECT ts, equity, benchmark_equity FROM paper_daily_log WHERE account=? ORDER BY id DESC LIMIT 1", (account,))
        row = cur.fetchone()
    return {"holdings": state["holdings"], "cash": state["cash"], "last_week": state["last_week"],
            "ts": row[0], "equity": row[1], "benchmark_equity": row[2]}


def history(account: str = "A") -> list:
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT ts, week_key, target_json, equity, benchmark_equity FROM paper_daily_log WHERE account=? ORDER BY id",
                    (account,))
        return [dict(r) for r in cur.fetchall()]
