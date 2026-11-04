"""Бумажный счёт C: тренд по фьючерсам на живых ценах, без ордеров брокера.

Правило зафиксировано заранее (docs/superpowers/plans/2026-11-04-beat-money-market-5x.md). Раз в неделю по каждому инструменту
берём знак доходности за 20 и за 60 дней и усредняем; размер обратно волатильности, риск 20% в год на портфель инструментов
при независимых движениях. Кеш в LQDT, эталон LQDT. Позиции в целых контрактах, издержки 0.06% от оборота, ролл оплачивается
с обеих сторон. Данные передаются снаружи: модуль не знает про SDK.
"""
import math
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from app.ml import paper_daily

LOOKS = (20, 60)
VOL_DAYS = 60
TARGET = 0.20
MAX_WEIGHT = 3.0
COST = 0.0006
START_EQUITY = 1_000_000.0    # при таком капитале целых контрактов хватает на большинство инструментов
MARGIN_SHARE = 0.30           # такая доля объёма считается занятой под обеспечение и дохода не приносит
ROLL_DAYS = 5                 # за столько дней до экспирации контракт в сигнале уже не участвует
MIN_EXP_DAYS = 10             # контракт для позиции должен прожить до следующего ребаланса
MIN_GROUPS = 3
ACCOUNT = "C"

# Базовый актив у брокера -> инструмент счёта
ROOTS = {"Brent": "Brent", "Газ (США)": "NatGas", "Серебро": "Silver", "USD/RUB": "USD", "EUR/RUB": "EUR", "CNY/RUB": "CNY",
         "Индекс московской биржи": "IMOEX", "IMOEX": "IMOEX", "RTSI": "RTS", "Золото в долларах": "Gold"}


def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def stitch_returns(contracts: List[dict]) -> Dict[str, float]:
    """Дневные доходности инструмента: на каждый день берётся контракт с большим оборотом за прошлые дни.

    Доходность считается только внутри одного контракта, поэтому разница цен между контрактами в неё не попадает.
    contracts: [{"ticker", "exp", "bars": [(дата, закрытие, объём), ...]}, ...]
    """
    best: Dict[str, tuple] = {}
    for c in contracts:
        cutoff = (_as_date(c["exp"]) - timedelta(days=ROLL_DAYS)).isoformat()
        bars = sorted(c["bars"], key=lambda b: str(b[0]))
        for i in range(1, len(bars)):
            day, close, volume = str(bars[i][0])[:10], bars[i][1], bars[i][2]
            prev = bars[i - 1][1]
            if day > cutoff or volume <= 0 or close <= 0 or prev <= 0:
                continue
            ret = close / prev - 1
            if abs(ret) >= 0.5:
                continue
            window = [b[2] for b in bars[max(0, i - 5):i]]
            past = sum(window) / len(window)
            if day not in best or past > best[day][0]:
                best[day] = (past, ret)
    return {d: best[d][1] for d in sorted(best)}


def active_contract(contracts: List[dict], today: date) -> Optional[dict]:
    """Контракт для позиции: самый ликвидный за 5 дней среди тех, что доживут до следующего ребаланса."""
    limit = today + timedelta(days=MIN_EXP_DAYS)
    alive = [c for c in contracts if _as_date(c["exp"]) > limit and c["bars"]]
    if not alive:
        return None
    return max(alive, key=lambda c: sum(b[2] for b in sorted(c["bars"], key=lambda b: str(b[0]))[-5:]))


def _table(returns: Dict[str, Dict[str, float]]) -> Dict[str, list]:
    calendar = sorted({d for r in returns.values() for d in r})
    return {g: [r.get(d) for d in calendar] for g, r in returns.items()}


def _eligible(rows: list) -> bool:
    longest = rows[-max(LOOKS):]
    return (sum(v is not None for v in longest) >= 0.8 * max(LOOKS)
            and sum(v is not None for v in rows[-VOL_DAYS:]) >= 40)


def _sign_of(rows: list, window: int) -> float:
    total = 1.0
    for v in rows[-window:]:
        total *= 1 + (v or 0.0)
    return float((total > 1) - (total < 1))


def target_weights(returns: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Вес инструмента как доля капитала, лонг или шорт. Только прошлые данные."""
    table = _table(returns)
    n = max(sum(_eligible(rows) for rows in table.values()), 1)
    weights = {}
    for group, rows in table.items():
        weights[group] = 0.0
        if not _eligible(rows):
            continue
        recent = [v for v in rows[-VOL_DAYS:] if v is not None]
        mean = sum(recent) / len(recent)
        vol = math.sqrt(sum((v - mean) ** 2 for v in recent) / (len(recent) - 1)) * math.sqrt(252)
        if vol <= 0:
            continue
        signal = sum(_sign_of(rows, k) for k in LOOKS) / len(LOOKS)
        raw = signal * (TARGET / math.sqrt(n)) / vol
        weights[group] = max(-MAX_WEIGHT, min(MAX_WEIGHT, raw))
    return weights


def to_contracts(weights: Dict[str, float], equity: float, unit: Dict[str, float]) -> Dict[str, int]:
    """Целые контракты. unit — объём одного контракта в рублях."""
    return {g: int(round(w * equity / unit[g])) if unit.get(g) else 0 for g, w in weights.items()}


def run_step(now: datetime, load_market: Callable[[list], dict]) -> Optional[str]:
    """Один шаг: 'rebalanced', 'no_history', 'no_prices' или None (сейчас делать нечего).

    load_market(тикеры позиций) -> {"returns": {инструмент: {дата: доходность}}, "active": {инструмент: {ticker, price, unit}},
    "quotes": {тикер: цена}, "lq": цена фонда денежного рынка}.
    """
    if not paper_daily.in_window(now):
        return None
    state = paper_daily._load(ACCOUNT)
    week = paper_daily.week_key(now)
    if state and state["last_week"] == week:
        return None
    positions = (state or {}).get("positions", {})
    market = load_market(sorted(p["ticker"] for p in positions.values()))
    table = _table(market["returns"])
    if sum(_eligible(rows) for rows in table.values()) < MIN_GROUPS:
        return "no_history"
    active, quotes, lq = market["active"], market["quotes"], market.get("lq")
    needed = {p["ticker"] for p in positions.values()} | {a["ticker"] for a in active.values()}
    if not lq or any(not quotes.get(t) for t in needed):
        return "no_prices"

    if state is None:
        equity, lq_start = START_EQUITY, lq
    else:
        equity, lq_start = state["equity"], state["lq_start"]
        exposure = sum(abs(p["qty"]) * p["unit"] for p in positions.values())
        pnl = sum(p["qty"] * p["unit"] * (quotes[p["ticker"]] / p["price"] - 1) for p in positions.values())
        income = max(equity - MARGIN_SHARE * exposure, 0.0) * (lq / state["lq"] - 1)
        equity += pnl + income

    weights = target_weights(market["returns"])
    wanted = to_contracts({g: w for g, w in weights.items() if g in active}, max(equity, 0.0),
                          {g: a["unit"] for g, a in active.items()})
    cost = 0.0
    new_positions = {}
    for group in set(positions) | set(wanted):
        old, qty = positions.get(group), wanted.get(group, 0)
        target = active.get(group)
        if old and target and old["ticker"] == target["ticker"]:
            cost += abs(qty - old["qty"]) * target["unit"] * COST
        else:
            if old:
                cost += abs(old["qty"]) * old["unit"] * (quotes[old["ticker"]] / old["price"]) * COST
            if target:
                cost += abs(qty) * target["unit"] * COST
        if qty and target:
            new_positions[group] = {"ticker": target["ticker"], "qty": qty, "price": quotes[target["ticker"]], "unit": target["unit"]}
    equity -= cost
    benchmark = START_EQUITY * lq / lq_start
    new_state = {"equity": equity, "cash": equity, "positions": new_positions, "lq": lq, "lq_start": lq_start,
                 "benchmark_equity": benchmark, "last_week": week, "last_date": now.astimezone(paper_daily.MSK).strftime("%Y-%m-%d"),
                 "holdings": {p["ticker"]: p["qty"] for p in new_positions.values()}}
    paper_daily._save(ACCOUNT, new_state, now, {g: p["qty"] for g, p in new_positions.items()},
                      {t: quotes[t] for t in needed}, equity, benchmark)
    return "rebalanced"


def status() -> Optional[dict]:
    """Состояние счёта на момент последнего ребаланса; None — счёт ещё не открыт."""
    state = paper_daily._load(ACCOUNT)
    if state is None:
        return None
    return {"positions": state["positions"], "equity": state["equity"], "benchmark_equity": state["benchmark_equity"],
            "last_week": state["last_week"], "last_date": state["last_date"]}
