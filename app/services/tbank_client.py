from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from t_tech.invest import (
        Client,
        CandleInterval,
        InstrumentIdType,
        OrderDirection,
        OrderType,
        PostOrderResponse,
        StopOrderDirection,
        StopOrderExpirationType,
        StopOrderType,
    )
    from t_tech.invest.sandbox.client import SandboxClient
    from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal
except Exception:
    Client = None
    SandboxClient = None
    CandleInterval = None
    InstrumentIdType = None
    OrderDirection = None
    OrderType = None
    PostOrderResponse = None
    StopOrderDirection = None
    StopOrderExpirationType = None
    StopOrderType = None
    decimal_to_quotation = None
    quotation_to_decimal = None

from app.config import settings
from app.db import log_event


def _client_cls():
    return SandboxClient if str(settings.TINVEST_USE_SANDBOX).lower() == "true" else Client


def _account_id() -> str:
    return settings.TINVEST_ACCOUNT_ID


def quotation_to_decimal_safe(q) -> Decimal:
    if quotation_to_decimal:
        return quotation_to_decimal(q)
    units = getattr(q, "units", 0)
    nano = getattr(q, "nano", 0)
    return Decimal(units) + (Decimal(nano) / Decimal("1000000000"))


def money_value_to_decimal_safe(q) -> Decimal:
    if q is None:
        return Decimal("0")
    units = getattr(q, "units", 0)
    nano = getattr(q, "nano", 0)
    return Decimal(units) + (Decimal(nano) / Decimal("1000000000"))


def decimal_to_quotation_safe(value: Decimal):
    if decimal_to_quotation:
        return decimal_to_quotation(value)
    raise RuntimeError("decimal_to_quotation unavailable")


def round_to_step(price: Decimal, step: Decimal) -> Decimal:
    if not step or step <= 0:
        return price
    return (price / step).quantize(Decimal("1")) * step


def with_client():
    cls = _client_cls()
    if cls is None:
        raise RuntimeError("T-Bank SDK not installed")
    return cls(settings.TINVEST_TOKEN)


def get_last_price(figi: str) -> Decimal:
    with with_client() as client:
        resp = client.market_data.get_last_prices(figi=[figi])
        last = resp.last_prices[0].price
        return quotation_to_decimal_safe(last)


def get_min_price_increment(figi: str) -> Decimal:
    with with_client() as client:
        resp = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
            id=figi,
        )
        return quotation_to_decimal_safe(resp.instrument.min_price_increment)


def get_top_shares(limit: int = 20) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    with with_client() as client:
        shares = client.instruments.shares().instruments
        for s in shares:
            try:
                if getattr(s, "api_trade_available_flag", False) is not True:
                    continue
                figi = getattr(s, "figi", "")
                ticker = getattr(s, "ticker", "")
                if not figi or not ticker:
                    continue
                items.append(
                    {
                        "ticker": ticker,
                        "figi": figi,
                        "name": getattr(s, "name", ""),
                        "classcode": getattr(s, "class_code", ""),
                        "instrument_type": "share",
                        "currency": getattr(s, "currency", ""),
                        "lot": getattr(s, "lot", 1),
                        "min_price_increment": str(quotation_to_decimal_safe(getattr(s, "min_price_increment", None))),
                        "for_qual_investor": getattr(s, "for_qual_investor_flag", False),
                    }
                )
            except Exception:
                continue
    return sorted(items, key=lambda x: (x["ticker"] or ""))[:limit]


def search_instruments(query: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    items = get_top_shares(limit=200)
    if q:
        items = [x for x in items if q in x["ticker"].lower() or q in x["name"].lower() or q in x["figi"].lower()]
    return items[:limit]


def get_candles(figi: str, interval_name: str = "1min", hours: int = 8) -> List[Dict[str, Any]]:
    interval_map = {
        "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
        "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
        "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
        "hour": CandleInterval.CANDLE_INTERVAL_HOUR,
    }
    interval = interval_map.get(interval_name, CandleInterval.CANDLE_INTERVAL_1_MIN)
    from_dt = datetime.utcnow() - timedelta(hours=hours)
    out: List[Dict[str, Any]] = []
    with with_client() as client:
        candles = client.market_data.get_candles(figi=figi, from_=from_dt, to=datetime.utcnow(), interval=interval).candles
        for c in candles:
            out.append(
                {
                    "time": c.time.isoformat() if getattr(c, "time", None) else "",
                    "open": float(quotation_to_decimal_safe(c.open)),
                    "high": float(quotation_to_decimal_safe(c.high)),
                    "low": float(quotation_to_decimal_safe(c.low)),
                    "close": float(quotation_to_decimal_safe(c.close)),
                    "volume": getattr(c, "volume", 0),
                    "is_complete": getattr(c, "is_complete", True),
                }
            )
    return out


def post_market_close(figi: str, quantity: int, direction: str):
    dir_map = {
        "BUY": OrderDirection.ORDER_DIRECTION_BUY,
        "SELL": OrderDirection.ORDER_DIRECTION_SELL,
        "LONG_CLOSE": OrderDirection.ORDER_DIRECTION_SELL,
        "SHORT_CLOSE": OrderDirection.ORDER_DIRECTION_BUY,
    }
    with with_client() as client:
        return client.orders.post_order(
            figi=figi,
            quantity=int(quantity),
            direction=dir_map[direction],
            account_id=_account_id(),
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=f"close-{figi}-{int(datetime.utcnow().timestamp())}",
        )


def post_stop_bundle(figi: str, quantity: int, entry_price: Decimal, side: str, stop_pct: Decimal, take_pct: Decimal) -> Dict[str, Optional[str]]:
    stop_side = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if side.upper() == "BUY" else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
    take_side = stop_side
    step = get_min_price_increment(figi)

    if side.upper() == "BUY":
        stop_price = round_to_step(entry_price * (Decimal("1") - stop_pct), step)
        take_price = round_to_step(entry_price * (Decimal("1") + take_pct), step)
    else:
        stop_price = round_to_step(entry_price * (Decimal("1") + stop_pct), step)
        take_price = round_to_step(entry_price * (Decimal("1") - take_pct), step)

    out = {"stop_loss_id": None, "take_profit_id": None}
    with with_client() as client:
        sl = client.stop_orders.post_stop_order(
            figi=figi,
            quantity=int(quantity),
            price=decimal_to_quotation_safe(stop_price),
            stop_price=decimal_to_quotation_safe(stop_price),
            direction=stop_side,
            account_id=_account_id(),
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            expire_date=None,
        )
        tp = client.stop_orders.post_stop_order(
            figi=figi,
            quantity=int(quantity),
            price=decimal_to_quotation_safe(take_price),
            stop_price=decimal_to_quotation_safe(take_price),
            direction=take_side,
            account_id=_account_id(),
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
            expire_date=None,
        )
        out["stop_loss_id"] = getattr(sl, "stop_order_id", None)
        out["take_profit_id"] = getattr(tp, "stop_order_id", None)
    log_event("STOP_BUNDLE", f"stop bundle created for {figi}", ticker=figi)
    return out


def get_active_stop_orders() -> List[Dict[str, Any]]:
    with with_client() as client:
        try:
            resp = client.stop_orders.get_stop_orders(account_id=_account_id())
        except Exception:
            resp = client.stop_orders.get_stop_orders(account_id=_account_id(), status=None)
        items = []
        for x in getattr(resp, "stop_orders", []):
            items.append(
                {
                    "stop_order_id": getattr(x, "stop_order_id", ""),
                    "figi": getattr(x, "figi", ""),
                    "direction": str(getattr(x, "direction", "")),
                    "stop_order_type": str(getattr(x, "stop_order_type", "")),
                    "lots_requested": getattr(x, "lots_requested", 0),
                    "price": str(quotation_to_decimal_safe(getattr(x, "price", None))),
                    "stop_price": str(quotation_to_decimal_safe(getattr(x, "stop_price", None))),
                    "created_at": getattr(x, "create_date", None).isoformat() if getattr(x, "create_date", None) else "",
                }
            )
        return items


def cancel_stop_order(stop_order_id: str):
    with with_client() as client:
        return client.stop_orders.cancel_stop_order(account_id=_account_id(), stop_order_id=stop_order_id)


def _money_value_to_decimal(item) -> Decimal:
    if item is None:
        return Decimal("0")
    units = getattr(item, "units", 0)
    nano = getattr(item, "nano", 0)
    return Decimal(units) + (Decimal(nano) / Decimal("1000000000"))


def get_portfolio_snapshot() -> Dict[str, Any]:
    with with_client() as client:
        portfolio = client.operations.get_portfolio(account_id=_account_id())

        total_amount_portfolio = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_portfolio", None)
        )
        total_amount_shares = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_shares", None)
        )
        total_amount_currencies = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_currencies", None)
        )
        total_amount_futures = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_futures", None)
        )
        total_amount_options = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_options", None)
        )
        total_amount_bonds = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_bonds", None)
        )
        total_amount_etf = quotation_to_decimal_safe(
            getattr(portfolio, "total_amount_etf", None)
        )

        blocked = Decimal("0")
        money = []

        try:
            withdraw_limits = client.operations.get_withdraw_limits(account_id=_account_id())
            for item in getattr(withdraw_limits, "money", []):
                currency = getattr(item, "currency", "") or ""
                value = _money_value_to_decimal(item)
                blocked_value = Decimal("0")

                for b in getattr(withdraw_limits, "blocked", []):
                    if (getattr(b, "currency", "") or "") == currency:
                        blocked_value = _money_value_to_decimal(b)
                        break

                money.append({
                    "currency": currency,
                    "available": value,
                    "blocked": blocked_value,
                    "total": value + blocked_value,
                })
                blocked += blocked_value
        except Exception:
            pass

        cash_total = sum((x["total"] for x in money), Decimal("0"))

        return {
            "total_assets": total_amount_portfolio,
            "positions_value": (
                total_amount_shares
                + total_amount_bonds
                + total_amount_etf
                + total_amount_futures
                + total_amount_options
            ),
            "cash": total_amount_currencies if total_amount_currencies else cash_total,
            "blocked": blocked,
            "shares": total_amount_shares,
            "bonds": total_amount_bonds,
            "etf": total_amount_etf,
            "futures": total_amount_futures,
            "options": total_amount_options,
            "money_by_currency": [
                {
                    "currency": x["currency"],
                    "available": str(x["available"]),
                    "blocked": str(x["blocked"]),
                    "total": str(x["total"]),
                }
                for x in money
            ],
            "positions_count": len(getattr(portfolio, "positions", [])),
        }