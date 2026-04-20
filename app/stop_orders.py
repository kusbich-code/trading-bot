from decimal import Decimal
from t_tech.invest import (
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
    InstrumentIdType,
)
from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal


def round_price_to_step(price: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return price
    return round(price / step) * step


def get_min_price_increment(client, figi: str) -> Decimal:
    resp = client.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
        id=figi,
    )
    mpi = resp.instrument.min_price_increment
    return quotation_to_decimal(mpi)


def create_take_profit_order(client, account_id: str, figi: str, quantity: int, base_price: Decimal, percent: Decimal, side: str):
    step = get_min_price_increment(client, figi)

    if side.upper() == "BUY":
        target_price = base_price + (base_price * percent)
        direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
    else:
        target_price = base_price - (base_price * percent)
        direction = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

    target_price = round_price_to_step(target_price, step)

    response = client.stop_orders.post_stop_order(
        figi=figi,
        quantity=quantity,
        price=decimal_to_quotation(target_price),
        stop_price=decimal_to_quotation(target_price),
        direction=direction,
        account_id=account_id,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
        expire_date=None,
    )
    return response


def create_stop_loss_order(client, account_id: str, figi: str, quantity: int, base_price: Decimal, percent: Decimal, side: str):
    step = get_min_price_increment(client, figi)

    if side.upper() == "BUY":
        stop_price = base_price - (base_price * percent)
        direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
    else:
        stop_price = base_price + (base_price * percent)
        direction = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

    stop_price = round_price_to_step(stop_price, step)

    response = client.stop_orders.post_stop_order(
        figi=figi,
        quantity=quantity,
        price=decimal_to_quotation(stop_price),
        stop_price=decimal_to_quotation(stop_price),
        direction=direction,
        account_id=account_id,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
        expire_date=None,
    )
    return response


def get_stop_orders(client, account_id: str):
    response = client.stop_orders.get_stop_orders(account_id=account_id)
    return getattr(response, "stop_orders", [])


def cancel_stop_order(client, account_id: str, stop_order_id: str):
    return client.stop_orders.cancel_stop_order(
        account_id=account_id,
        stop_order_id=stop_order_id,
    )