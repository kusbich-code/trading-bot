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
        resp = client.market_data.get_last_prices(instrument_id=[figi])
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


def get_candles_range(
    figi: str,
    interval_name: str = "5min",
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Загружает свечи за диапазон дат, автоматически разбивая на чанки,
    принимаемые API T-Bank (1 день для минутных интервалов, 7 дней для часовых).
    Возвращает свечи отсортированные по времени по возрастанию.
    """
    interval_map = {
        "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
        "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
        "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
        "hour": CandleInterval.CANDLE_INTERVAL_HOUR,
        "day": CandleInterval.CANDLE_INTERVAL_DAY,
    }
    interval = interval_map.get(interval_name, CandleInterval.CANDLE_INTERVAL_5_MIN)

    # Размер чанка: минутные интервалы → 1 день, час → 7 дней, день → 365 дней
    if interval_name in ("1min", "5min", "15min"):
        chunk_hours = 24
    elif interval_name == "hour":
        chunk_hours = 24 * 7
    else:
        chunk_hours = 24 * 365

    from datetime import timezone as _tz
    now = datetime.now(_tz.utc).replace(tzinfo=None)
    if to_dt is None:
        to_dt = now
    if from_dt is None:
        from_dt = now - timedelta(days=days)

    chunks: List[Tuple[datetime, datetime]] = []
    cur = from_dt
    while cur < to_dt:
        end = min(cur + timedelta(hours=chunk_hours), to_dt)
        chunks.append((cur, end))
        cur = end

    out: List[Dict[str, Any]] = []
    seen_times: set = set()
    with with_client() as client:
        for chunk_from, chunk_to in chunks:
            try:
                resp = client.market_data.get_candles(
                    instrument_id=figi,
                    from_=chunk_from,
                    to=chunk_to,
                    interval=interval,
                ).candles
            except Exception:
                continue
            for c in resp:
                t = c.time.isoformat() if getattr(c, "time", None) else ""
                if t in seen_times:
                    continue
                seen_times.add(t)
                out.append({
                    "time": t,
                    "open": float(quotation_to_decimal_safe(c.open)),
                    "high": float(quotation_to_decimal_safe(c.high)),
                    "low": float(quotation_to_decimal_safe(c.low)),
                    "close": float(quotation_to_decimal_safe(c.close)),
                    "volume": getattr(c, "volume", 0),
                    "is_complete": getattr(c, "is_complete", True),
                })

    out.sort(key=lambda x: x["time"])
    return out


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
        candles = client.market_data.get_candles(instrument_id=figi, from_=from_dt, to=datetime.utcnow(), interval=interval).candles
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


def post_market_close(figi: str, quantity: int, direction: str, instrument_uid: str = ""):
    import uuid as _uuid
    dir_map = {
        "BUY": OrderDirection.ORDER_DIRECTION_BUY,
        "SELL": OrderDirection.ORDER_DIRECTION_SELL,
        "LONG_CLOSE": OrderDirection.ORDER_DIRECTION_SELL,
        "SHORT_CLOSE": OrderDirection.ORDER_DIRECTION_BUY,
    }
    if direction not in dir_map:
        raise ValueError(f"Неизвестное направление: {direction}")
    order_direction = dir_map[direction]
    order_id = str(_uuid.uuid4())
    instrument_id = instrument_uid or figi
    with with_client() as client:
        resp = client.orders.post_order(
            instrument_id=instrument_id,
            quantity=int(quantity),
            direction=order_direction,
            account_id=_account_id(),
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=order_id,
        )
        log_event(
            "ORDER_CLOSE",
            f"market close posted figi={figi} qty={quantity} dir={direction} order_id={order_id}",
            ticker=figi,
        )
        return resp


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


def get_operations_today() -> Dict[str, Any]:
    """Загружает исполненные операции сегодня из API T-Bank и рассчитывает реальный PnL."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    operations: List[Dict[str, Any]] = []
    total_pnl = Decimal("0")
    total_commission = Decimal("0")
    trade_count = 0

    with with_client() as client:
        resp = client.operations.get_operations(
            account_id=_account_id(),
            from_=today_start,
            to=now,
        )

        for op in getattr(resp, "operations", []):
            state_str = str(getattr(op, "state", "") or "")
            if "EXECUTED" not in state_str:
                continue

            op_type = str(
                getattr(op, "operation_type", "")
                or getattr(op, "type", "")
                or ""
            )
            payment = _money_value_to_decimal(getattr(op, "payment", None))
            price = _money_value_to_decimal(getattr(op, "price", None))
            quantity = getattr(op, "quantity", 0) or 0
            figi = getattr(op, "figi", "") or ""
            date_val = getattr(op, "date", None)
            date_str = ""
            if date_val:
                try:
                    date_str = date_val.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    date_str = str(date_val)[:19].replace("T", " ")

            is_fee = any(x in op_type for x in ("FEE", "COMMISSION", "SERVICE_FEE", "MARGIN_FEE"))
            is_trade = any(x in op_type for x in ("BUY", "SELL")) and not is_fee

            total_pnl += payment
            if is_fee:
                total_commission += payment

            if is_trade or is_fee:
                direction = "SELL" if "SELL" in op_type else ("BUY" if "BUY" in op_type else "")
                # Когда API возвращает price=0, вычисляем из payment/quantity (часто для маркет-ордеров)
                if price == 0 and quantity > 0 and payment != 0:
                    price = abs(payment) / Decimal(str(quantity))
                price_ui = f"{price:.2f}" if price else "—"
                operations.append({
                    "figi": figi,
                    "op_type": op_type,
                    "direction": direction,
                    "payment": str(payment),
                    "payment_ui": f"{payment:.2f}",
                    "price": str(price),
                    "price_ui": price_ui,
                    "quantity": quantity,
                    "date": date_str,
                    "is_fee": is_fee,
                })
                if is_trade:
                    trade_count += 1

    return {
        "pnl": total_pnl,
        "pnl_ui": f"{total_pnl:.2f}",
        "total_commission": total_commission,
        "total_commission_ui": f"{total_commission:.2f}",
        "trades_count": trade_count,
        "operations": operations,
    }


def sandbox_pay_in(amount_rub: int = 100_000) -> Decimal:
    """
    Пополнение виртуального счёта в Sandbox.
    Возвращает новый баланс (RUB).
    Работает только когда TINVEST_USE_SANDBOX=true.
    """
    try:
        from t_tech.invest import MoneyValue
        amount = MoneyValue(currency="RUB", units=int(amount_rub), nano=0)
    except Exception:
        amount = None

    with with_client() as client:
        if amount is not None:
            resp = client.sandbox.sandbox_pay_in(account_id=_account_id(), amount=amount)
        else:
            resp = client.sandbox.sandbox_pay_in(account_id=_account_id(), amount=amount_rub)
        balance = getattr(resp, "balance", None)
        return _money_value_to_decimal(balance) if balance else Decimal("0")


def get_operations_by_cursor(
    from_dt=None,
    to_dt=None,
    limit: int = 50,
    cursor: str = "",
) -> Dict[str, Any]:
    """
    Постраничная история операций брокера через GetOperationsByCursor.
    Возвращает исполненные операции BUY/SELL/FEE с ценами исполнения.
    """
    from datetime import timezone, timedelta
    now = datetime.now(timezone.utc)
    if from_dt is None:
        from_dt = now - timedelta(days=90)
    if to_dt is None:
        to_dt = now

    with with_client() as client:
        resp = client.operations.get_operations_by_cursor(
            account_id=_account_id(),
            from_=from_dt,
            to=to_dt,
            limit=limit,
            cursor=cursor,
        )
        items = []
        for op in getattr(resp, "items", []):
            state_str = str(getattr(op, "state", "") or "")
            if "EXECUTED" not in state_str:
                continue
            op_type = str(getattr(op, "type", "") or "")
            payment    = _money_value_to_decimal(getattr(op, "payment", None))
            price      = _money_value_to_decimal(getattr(op, "price", None))
            commission = _money_value_to_decimal(getattr(op, "commission", None))
            quantity   = int(getattr(op, "quantity", 0) or 0)
            figi       = getattr(op, "figi", "") or ""
            uid        = getattr(op, "instrument_uid", "") or ""
            date_val   = getattr(op, "date", None)
            date_str   = date_val.strftime("%Y-%m-%d %H:%M:%S") if date_val else ""
            name       = getattr(op, "name", "") or ""

            if price == Decimal("0") and quantity > 0 and payment != Decimal("0"):
                price = abs(payment) / Decimal(str(quantity))

            is_fee   = any(x in op_type for x in ("FEE", "COMMISSION", "SERVICE", "TAX"))
            is_trade = any(x in op_type for x in ("BUY", "SELL")) and not is_fee
            direction = "SELL" if "SELL" in op_type else ("BUY" if "BUY" in op_type else "")

            items.append({
                "date": date_str,
                "type": op_type,
                "name": name,
                "direction": direction,
                "figi": figi,
                "instrument_uid": uid,
                "quantity": quantity,
                "payment_ui": f"{payment:.2f}",
                "price_ui": f"{price:.2f}" if price else "—",
                "commission_ui": f"{commission:.2f}",
                "is_fee": is_fee,
                "is_trade": is_trade,
            })

        return {
            "items": items,
            "has_next": bool(getattr(resp, "has_next", False)),
            "next_cursor": str(getattr(resp, "next_cursor", "") or ""),
        }


def get_tbank_signals(figis: List[str]) -> Dict[str, str]:
    """
    Загружает аналитические сигналы из SignalService T-Bank.
    Возвращает figi → 'BUY' | 'SELL' | 'NEUTRAL'.
    Возвращает {} без ошибки при любой проблеме (сервис может быть недоступен в sandbox).
    """
    if not figis:
        return {}
    try:
        with with_client() as client:
            resp = client.signal.get_signals(instrument_id=figis)
            result: Dict[str, str] = {}
            for s in getattr(resp, "signals", []):
                figi = (
                    getattr(s, "instrument_uid", "")
                    or getattr(s, "figi", "")
                    or ""
                )
                direction_raw = str(getattr(s, "direction", "") or "").upper()
                if not figi:
                    continue
                if "BUY" in direction_raw:
                    result[figi] = "BUY"
                elif "SELL" in direction_raw:
                    result[figi] = "SELL"
                else:
                    result[figi] = "NEUTRAL"
            return result
    except Exception:
        return {}


def get_positions_detailed() -> Dict[str, Any]:
    """GetPositions: точный кэш по валютам + ценные бумаги на счёте."""
    with with_client() as client:
        resp = client.operations.get_positions(account_id=_account_id())
        money = []
        for m in getattr(resp, "money", []):
            v = _money_value_to_decimal(m)
            money.append({
                "currency": (getattr(m, "currency", "") or "").upper(),
                "value": str(v),
                "value_ui": f"{v:.2f}",
            })
        blocked_money = []
        for b in getattr(resp, "blocked", []):
            v = _money_value_to_decimal(b)
            blocked_money.append({
                "currency": (getattr(b, "currency", "") or "").upper(),
                "value": str(v),
                "value_ui": f"{v:.2f}",
            })
        securities = []
        for sec in getattr(resp, "securities", []):
            figi = getattr(sec, "figi", "") or ""
            balance = int(getattr(sec, "balance", 0) or 0)  # в штуках (акциях), не в лотах
            blocked_qty = int(getattr(sec, "blocked", 0) or 0)
            if figi and abs(balance) > 0:
                securities.append({
                    "figi": figi,
                    "balance": balance,
                    "blocked": blocked_qty,
                })
        return {"money": money, "blocked": blocked_money, "securities": securities}


def get_broker_positions() -> List[Dict[str, Any]]:
    """Возвращает фактические открытые позиции из API брокера T-Bank."""
    with with_client() as client:
        portfolio = client.operations.get_portfolio(account_id=_account_id())
        result = []
        for pos in getattr(portfolio, "positions", []):
            figi = getattr(pos, "figi", "") or ""
            if not figi:
                continue
            quantity = quotation_to_decimal_safe(getattr(pos, "quantity", None))
            quantity_lots = quotation_to_decimal_safe(getattr(pos, "quantity_lots", None))
            avg_price = _money_value_to_decimal(getattr(pos, "average_position_price", None))
            current_price = _money_value_to_decimal(getattr(pos, "current_price", None))
            expected_yield = quotation_to_decimal_safe(getattr(pos, "expected_yield", None))
            instrument_type = str(getattr(pos, "instrument_type", "") or "")
            direction = "SELL" if quantity < 0 else "BUY"
            lots = int(abs(quantity_lots)) if quantity_lots else int(abs(quantity))
            result.append({
                "figi": figi,
                "instrument_type": instrument_type,
                "direction": direction,
                "qty": lots,
                "avg_price": avg_price,
                "current_price": current_price,
                "expected_yield": expected_yield,
            })
        return result


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

        # Нереализованный P&L: сумма expected_yield по всем открытым позициям
        unrealized_pnl = Decimal("0")
        for pos in getattr(portfolio, "positions", []):
            unrealized_pnl += quotation_to_decimal_safe(getattr(pos, "expected_yield", None))

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
            "unrealized_pnl": unrealized_pnl,
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