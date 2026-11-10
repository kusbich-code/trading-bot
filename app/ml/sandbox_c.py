"""Счёт C в песочнице брокера: расчёт без обращений к брокеру.

Цели берутся из бумажного счёта C (его недельный ребаланс уже посчитал вес по каждому инструменту и выбрал контракт), число
контрактов пересчитывается на деньги своего счёта, укладывается в гарантийное обеспечение и превращается в заявки на разницу.
"""
import uuid
from typing import Dict, List

from app.ml import paper_futures


def targets_from_paper(state: dict, equity: float, unit: Dict[str, float]) -> Dict[str, int]:
    """Тикер контракта → целевое число контрактов (знак — сторона). unit — стоимость контракта в рублях."""
    weights = {s["ticker"]: s["weight"] for s in (state.get("signals") or {}).values() if s.get("ticker")}
    wanted = paper_futures.to_contracts(weights, equity, unit)
    return {t: q for t, q in wanted.items() if q}


def fit_margin(targets: Dict[str, int], margin: Dict[str, float], equity: float, max_share: float = 0.5) -> Dict[str, int]:
    """Пропорционально уменьшает цели, чтобы обеспечение всех позиций не превышало max_share денег счёта."""
    need = sum(abs(q) * margin.get(t, 0.0) for t, q in targets.items())
    limit = max_share * equity
    if need <= limit or need <= 0:
        return dict(targets)
    scale = limit / need
    return {t: int(q * scale) for t, q in targets.items()}         # int() округляет к нулю


def plan_orders(targets: Dict[str, int], held: Dict[str, int]) -> List[dict]:
    """Заявки на разницу. Сначала сокращения и закрытия, они освобождают обеспечение, потом открытия."""
    reduce, opens = [], []
    for t in sorted(set(targets) | set(held)):
        want, have = targets.get(t, 0), held.get(t, 0)
        diff = want - have
        if not diff:
            continue
        order = {"ticker": t, "qty": abs(diff), "direction": "BUY" if diff > 0 else "SELL"}
        shrinking = have and (abs(want) < abs(have) or want * have < 0)
        (reduce if shrinking else opens).append(order)
    return reduce + opens


def daily_loss_exceeded(equity_now: float, equity_day_start: float, limit: float = 0.05) -> bool:
    return equity_day_start > 0 and (equity_day_start - equity_now) / equity_day_start >= limit


def order_id(week: str, ticker: str, attempt: int) -> str:
    """Один и тот же номер при повторе после перезапуска: брокер не исполнит заявку дважды."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"C|{week}|{ticker}|{attempt}"))
