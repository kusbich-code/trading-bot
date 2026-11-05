"""Сверка с брокером: итог по операциям счёта против итога бота и объяснение разницы.

Без обращений к брокеру и БД: на вход готовые операции, сделки и цены, поэтому расчёт проверяется тестами на настоящих цифрах.
Формула: результат брокера = деньги от покупок и продажи + комиссии + прочие списания + открытые позиции по текущим ценам.
Пополнения и выводы результатом не считаются.
"""
from typing import Dict, List, Optional


def enum_name(value, enum_cls=None) -> str:
    """Имя значения перечисления. В Python 3.11 str() от IntEnum даёт число, и проверка "EXECUTED" in str(...) не срабатывает."""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    if enum_cls is not None and isinstance(value, int):
        try:
            return enum_cls(value).name
        except ValueError:
            pass
    return str(value if value is not None else "")


def operation_kind(type_name: str) -> str:
    """buy, sell, fee, deposit или other."""
    name = str(type_name).upper()
    if "MARGIN_FEE" in name:
        return "other"
    if "FEE" in name:
        return "fee"
    if name.startswith("OPERATION_TYPE_BUY"):
        return "buy"
    if name.startswith("OPERATION_TYPE_SELL"):
        return "sell"
    if name.startswith(("OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT")):
        return "deposit"
    return "other"


def net_quantities(operations: List[dict]) -> Dict[str, int]:
    """Чистое количество по бумагам: купленное минус проданное. Минус значит шорт."""
    held: Dict[str, int] = {}
    for op in operations:
        kind = operation_kind(op.get("type", ""))
        if kind in ("buy", "sell"):
            figi = op.get("figi", "")
            held[figi] = held.get(figi, 0) + (1 if kind == "buy" else -1) * int(op.get("quantity") or 0)
    return held


def _money(value) -> float:
    return round(float(value or 0.0), 2)


def summarize(operations: List[dict], trades: List[dict], bot_unrealized: float, prices: Dict[str, float]) -> dict:
    """operations: type, figi, name, quantity, payment, commission, date. trades: time, pnl, commission. prices: figi -> цена."""
    since: Optional[str] = min((op["date"][:10] for op in operations if op.get("date")), default=None)
    cash_flow = fee_ops = fee_field = other = 0.0
    has_fee_ops = False
    names: Dict[str, str] = {}
    for op in operations:
        kind = operation_kind(op.get("type", ""))
        payment = float(op.get("payment") or 0.0)
        if kind in ("buy", "sell"):
            cash_flow += payment
            fee_field += float(op.get("commission") or 0.0)
            names[op.get("figi", "")] = op.get("name") or op.get("figi", "")
        elif kind == "fee":
            has_fee_ops = True
            fee_ops += payment
        elif kind == "other":
            other += payment
    # Брокер пишет комиссию отдельной операцией и дублирует в поле сделки: считаем что-то одно
    fees = fee_ops if has_fee_ops else fee_field

    open_value = 0.0
    positions, missing = [], []
    for figi, qty in net_quantities(operations).items():
        if qty == 0:
            continue
        price = prices.get(figi)
        if price is None:
            missing.append(figi)
            positions.append({"figi": figi, "name": names.get(figi, figi), "quantity": qty, "price": None, "value": None})
            continue
        value = qty * float(price)
        open_value += value
        positions.append({"figi": figi, "name": names.get(figi, figi), "quantity": qty, "price": float(price), "value": _money(value)})

    in_period = [t for t in trades if since and str(t.get("time", "")) >= since]
    realized = sum(float(t.get("pnl") or 0.0) for t in in_period)
    bot_fee = sum(float(t.get("commission") or 0.0) for t in in_period)
    unrealized = float(bot_unrealized or 0.0)
    broker_result = cash_flow + fees + other + open_value
    bot_result = realized + unrealized
    difference = broker_result - bot_result
    gap = abs(fees) - bot_fee
    return {
        "since": since, "ops_count": len(operations), "trades_count": len(in_period),
        "cash_flow": _money(cash_flow), "broker_fees": _money(fees), "other_charges": _money(other), "open_value": _money(open_value),
        "open_positions": positions, "missing_prices": missing, "complete": not missing,
        "broker_result": _money(broker_result), "bot_realized": _money(realized), "bot_unrealized": _money(unrealized),
        "bot_commission": _money(bot_fee), "bot_result": _money(bot_result), "difference": _money(difference),
        "commission_gap": _money(gap), "unexplained": _money(difference + gap - other),
    }
