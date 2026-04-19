import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal

from dotenv import load_dotenv

from t_tech.invest import (
    Client,
    CandleInterval,
    OrderDirection,
    OrderType,
    Quotation,
)
from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest.utils import quotation_to_decimal

from app.config import settings
from app.instruments import (
    get_watchlist_static,
    pick_top_liquid,
    get_instrument_meta,
    round_to_price_step,
)
from app.telegram_notify import TelegramNotifier

load_dotenv()

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/trading-bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("trading-bot")


class BotState:
    def __init__(self):
        self.status = "INIT"
        self.session_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.session_balance_start = 0.0
        self.session_balance_current = 0.0
        self.daily_pnl = Decimal("0")
        self.trades_today = 0
        self.open_positions = {}
        self.closed_trades = []
        self.last_error = ""
        self.last_update = ""
        self.instrument_states = {}
        self.watchlist = []
        self.current_trade_date = str(date.today())
        self.instrument_meta = {}

    def reset_daily(self):
        self.status = "PRECHECK"
        self.session_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.session_balance_start = self.session_balance_current
        self.daily_pnl = Decimal("0")
        self.trades_today = 0
        self.open_positions = {}
        self.closed_trades = []
        self.last_error = ""
        self.instrument_states = {}
        self.watchlist = []
        self.current_trade_date = str(date.today())
        self.instrument_meta = {}

    def to_dict(self):
        return {
            "status": self.status,
            "session_started_at": self.session_started_at,
            "session_balance_start": self.session_balance_start,
            "session_balance_current": self.session_balance_current,
            "daily_pnl": float(self.daily_pnl),
            "trades_today": self.trades_today,
            "open_positions": self.open_positions,
            "closed_trades": self.closed_trades,
            "last_error": self.last_error,
            "last_update": self.last_update,
            "instrument_states": self.instrument_states,
            "watchlist": self.watchlist,
            "current_trade_date": self.current_trade_date,
        }


state = BotState()
notifier = TelegramNotifier()


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_runtime():
    state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(settings.RUNTIME_FILE, state.to_dict())
    save_json(settings.TRADES_FILE, {"trades": state.closed_trades})


def quotation_from_decimal(price: Decimal) -> Quotation:
    normalized = price.quantize(Decimal("0.000000001"))
    units = int(normalized)
    nano = int((normalized - Decimal(units)) * Decimal("1000000000"))
    return Quotation(units=units, nano=nano)


def get_last_price(client, figi: str) -> Decimal:
    resp = client.market_data.get_last_prices(figi=[figi])
    return quotation_to_decimal(resp.last_prices[0].price)


def get_candles(client, figi: str, n=20):
    now = datetime.now(timezone.utc)
    resp = client.market_data.get_candles(
        figi=figi,
        from_=now - timedelta(minutes=n + 5),
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    return resp.candles


def calc_support_resistance(candles):
    highs = [quotation_to_decimal(c.high) for c in candles]
    lows = [quotation_to_decimal(c.low) for c in candles]
    return min(lows), max(highs)


def get_signal(price: Decimal, support: Decimal, resistance: Decimal):
    proximity = Decimal("0.0015")
    if support > 0 and abs(price - support) / support < proximity:
        return "BUY"
    if resistance > 0 and abs(price - resistance) / resistance < proximity:
        return "SELL"
    return None


def get_trading_status(client, instrument_id: str):
    try:
        resp = client.market_data.get_trading_status(instrument_id=instrument_id)
        return getattr(resp, "trading_status", "UNKNOWN")
    except Exception as e:
        log.warning(f"Не удалось получить trading status по {instrument_id}: {e}")
        return "UNKNOWN"


def is_tradable(status) -> bool:
    status_str = str(status)
    blocked = [
        "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING",
        "SECURITY_TRADING_STATUS_DEALER_NOT_AVAILABLE_FOR_TRADING",
        "SECURITY_TRADING_STATUS_BREAK_IN_TRADING",
        "SECURITY_TRADING_STATUS_CLOSING_AUCTION",
        "SECURITY_TRADING_STATUS_SESSION_CLOSED",
    ]
    return status_str not in blocked


def get_money_balance(client) -> float:
    try:
        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        total = getattr(portfolio, "total_amount_portfolio", None)
        if total:
            return float(quotation_to_decimal(total))
    except Exception as e:
        log.warning(f"Не удалось получить баланс портфеля: {e}")
    return 0.0


def build_watchlist(client):
    base = get_watchlist_static(settings.CORE_INSTRUMENTS)
    auto = pick_top_liquid(client, settings.LIQUIDITY_CANDIDATES, settings.AUTO_PICK_COUNT)

    merged = []
    seen = set()

    for item in base + auto:
        if item["ticker"] not in seen:
            merged.append(item)
            seen.add(item["ticker"])

    state.watchlist = merged
    return merged


def load_instrument_meta(client, watchlist):
    for item in watchlist:
        ticker = item["ticker"]
        figi = item["figi"]
        meta = get_instrument_meta(client, figi)

        if not meta:
            state.instrument_states[ticker] = {
                "figi": figi,
                "trading_status": "UNKNOWN",
                "min_price_increment": "",
                "status_note": "INVALID_FIGI_OR_META_ERROR",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            continue

        state.instrument_meta[ticker] = meta
        state.instrument_states[ticker] = {
            "figi": figi,
            "trading_status": "PRECHECK",
            "min_price_increment": str(meta["min_price_increment"]),
            "status_note": "META_LOADED",
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }


def send_session_start(client, watchlist):
    state.session_balance_start = get_money_balance(client)
    state.session_balance_current = state.session_balance_start

    lines = [
        f"🚀 {settings.BOT_NAME}: старт сессии",
        f"Режим: {'SANDBOX' if settings.TINVEST_USE_SANDBOX else 'PROD'}",
        f"Баланс на старте: {state.session_balance_start:.2f} ₽",
        "Инструменты: " + ", ".join([x["ticker"] for x in watchlist]),
    ]
    notifier.send("\n".join(lines))


def maybe_reset_daily(client):
    today = str(date.today())
    if state.current_trade_date != today:
        notifier.send_session_summary(state.to_dict())
        state.reset_daily()
        state.session_balance_current = get_money_balance(client)
        save_runtime()
        return True
    return False


def place_order_checked(client, ticker: str, figi: str, lots: int, raw_price: Decimal, direction: OrderDirection):
    meta = state.instrument_meta.get(ticker)
    if not meta:
        state.instrument_states[ticker] = {
            "figi": figi,
            "trading_status": "UNKNOWN",
            "min_price_increment": "",
            "status_note": "NO_META_SKIP",
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }
        return None

    step = meta["min_price_increment"]
    rounded_price = round_to_price_step(raw_price, step)
    q = quotation_from_decimal(rounded_price)
    request_order_id = str(uuid.uuid4())

    try:
        resp = client.orders.post_order(
            figi=figi,
            quantity=lots,
            price=q,
            direction=direction,
            account_id=settings.TINVEST_ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_LIMIT,
            order_id=request_order_id,
        )
        log.info(
            f"{ticker}: post_order success | "
            f"request_order_id={request_order_id} | "
            f"response_order_id={getattr(resp, 'order_id', request_order_id)} | "
            f"price={rounded_price} | lots={lots}"
        )

        response_order_id = getattr(resp, "order_id", request_order_id)

        log.info(
            f"{ticker}: post_order success | "
            f"request_order_id={request_order_id} | "
            f"response_order_id={response_order_id} | "
            f"price={rounded_price} | lots={lots}"
        )

        execution_report_status = "ORDER_STATE_PENDING"
        avg_price = rounded_price

        for attempt in range(1, 4):
            try:
                time.sleep(1)
                order_state = client.orders.get_order_state(
                    account_id=settings.TINVEST_ACCOUNT_ID,
                    order_id=getattr(resp, "order_id", request_order_id),
                )

                execution_report_status = str(
                    getattr(order_state, "execution_report_status", "UNKNOWN")
                )

                executed_order_price = getattr(order_state, "executed_order_price", None)

                if executed_order_price is not None:
                    tmp_price = quotation_to_decimal(executed_order_price)

                    if tmp_price > 0:
                        avg_price = tmp_price
                    else:
                        log.warning(
                            f"{ticker}: get_order_state returned non-positive executed price "
                            f"({tmp_price}), fallback to requested price {rounded_price}"
                        )
                log.info(
                    f"{ticker}: get_order_state success on attempt {attempt}, "
                    f"status={execution_report_status}, price={avg_price}"
                )
                break

            except Exception as e:
                msg = str(e)

                if "50005" in msg or "Order not found" in msg:
                    log.warning(
                        f"{ticker}: get_order_state пока не найден "
                        f"(attempt {attempt}/3), order_id={response_order_id}"
                    )
                    continue

                log.warning(
                    f"{ticker}: ошибка get_order_state (attempt {attempt}/3): {e}"
                )
                continue

        response_order_id = getattr(resp, "order_id", request_order_id)

        return {
            "response_order_id": response_order_id,
            "request_order_id": request_order_id,
            "requested_price": rounded_price,
            "executed_price": avg_price,
            "execution_status": execution_report_status,
        }

    except Exception as e:
        msg = str(e)

        if "figi" in msg.lower():
            state.instrument_states[ticker] = {
                "figi": figi,
                "trading_status": "INVALID",
                "min_price_increment": str(step),
                "status_note": "INVALID_FIGI_SKIP",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            log.warning(f"{ticker}: некорректный FIGI, инструмент пропущен: {msg}")
            return None

        if "30079" in msg or "minimum price increment" in msg.lower():
            state.instrument_states[ticker]["status_note"] = "BAD_PRICE_STEP"
            log.warning(f"{ticker}: ошибка шага цены: {msg}")
            return None

        raise


def process_instrument(client, item):
    ticker = item["ticker"]
    figi = item["figi"]

    if ticker not in state.instrument_meta:
        state.instrument_states[ticker] = {
            "figi": figi,
            "trading_status": "UNKNOWN",
            "min_price_increment": "",
            "status_note": "META_NOT_FOUND_SKIP",
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }
        return

    meta = state.instrument_meta[ticker]
    lot = meta["lot"] if meta.get("lot") else item["lot"]

    trading_status = get_trading_status(client, figi)
    state.instrument_states[ticker] = {
        "figi": figi,
        "trading_status": str(trading_status),
        "min_price_increment": str(meta["min_price_increment"]),
        "status_note": "OK",
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }

    if not is_tradable(trading_status):
        log.info(f"{ticker}: статус {trading_status}, торговля пропущена")
        return

    try:
        price = get_last_price(client, figi)
        candles = get_candles(client, figi, n=20)
    except Exception as e:
        msg = str(e)
        if "figi" in msg.lower():
            state.instrument_states[ticker]["status_note"] = "INVALID_FIGI_SKIP"
            log.warning(f"{ticker}: ошибка по FIGI при market data: {msg}")
            return
        raise

    if len(candles) < 5:
        log.info(f"{ticker}: мало свечей")
        return

    support, resistance = calc_support_resistance(candles)

    if ticker in state.open_positions:
        pos = state.open_positions[ticker]
        entry_price = Decimal(str(pos["entry_price"]))
        direction = pos["direction"]
        qty = int(pos["qty"])

        if direction == "BUY":
            sl_price = entry_price * (Decimal("1") - settings.STOP_LOSS_PCT)
            tp_price = entry_price * (Decimal("1") + settings.TAKE_PROFIT_PCT)
        else:
            sl_price = entry_price * (Decimal("1") + settings.STOP_LOSS_PCT)
            tp_price = entry_price * (Decimal("1") - settings.TAKE_PROFIT_PCT)

        close_signal = False
        close_reason = ""

        if direction == "BUY":
            if price <= sl_price:
                close_signal = True
                close_reason = "STOP_LOSS"
            elif price >= tp_price:
                close_signal = True
                close_reason = "TAKE_PROFIT"
        else:
            if price >= sl_price:
                close_signal = True
                close_reason = "STOP_LOSS"
            elif price <= tp_price:
                close_signal = True
                close_reason = "TAKE_PROFIT"

        if close_signal:
            close_dir = (
                OrderDirection.ORDER_DIRECTION_SELL
                if direction == "BUY"
                else OrderDirection.ORDER_DIRECTION_BUY
            )

            order_result = place_order_checked(client, ticker, figi, qty, price, close_dir)
            if not order_result:
                return

            exit_price = Decimal(str(order_result["executed_price"]))
            gross_amount = exit_price * qty
            commission = (entry_price * qty + exit_price * qty) * settings.ESTIMATED_COMMISSION_PCT

            if direction == "BUY":
                pnl = (exit_price - entry_price) * qty - commission
            else:
                pnl = (entry_price - exit_price) * qty - commission

            state.daily_pnl += pnl
            state.trades_today += 1

            trade = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": ticker,
                "figi": figi,
                "direction": direction,
                "entry": float(entry_price),
                "exit": float(exit_price),
                "qty": qty,
                "gross_amount": float(gross_amount),
                "commission": float(commission),
                "pnl": float(pnl),
                "reason": close_reason,
                "close_order_id": order_result["response_order_id"],
                "execution_status": order_result["execution_status"],
            }
            state.closed_trades.append(trade)
            del state.open_positions[ticker]

            notifier.send(
                f"✅ Закрытие позиции\n"
                f"{ticker} | {direction}\n"
                f"Вход: {entry_price}\n"
                f"Выход: {exit_price}\n"
                f"Объём: {float(gross_amount):.2f} ₽\n"
                f"Комиссия: {float(commission):.2f} ₽\n"
                f"PnL: {float(pnl):.2f} ₽\n"
                f"Причина: {close_reason}\n"
                f"Статус: {order_result['execution_status']}"
            )
        return

    if len(state.open_positions) >= settings.MAX_OPEN_POSITIONS:
        return

    sig = get_signal(price, support, resistance)
    if not sig:
        return

    if sig == "BUY":
        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_BUY)
        if not order_result:
            return
        state.open_positions[ticker] = {
            "figi": figi,
            "direction": "BUY",
            "entry_price": float(order_result["executed_price"]),
            "qty": lot,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "request_order_id": order_result["request_order_id"],
            "execution_status": order_result["execution_status"],
        }
        notifier.send(
            f"🟢 Открытие позиции\n"
            f"{ticker} | BUY\n"
            f"Цена: {order_result['executed_price']}\n"
            f"Количество: {lot}\n"
            f"Сумма сделки: {float(Decimal(str(order_result['executed_price'])) * lot):.2f} ₽\n"
            f"Статус исполнения: {order_result['execution_status']}"
        )

    elif sig == "SELL":
        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_SELL)
        if not order_result:
            return
        state.open_positions[ticker] = {
            "figi": figi,
            "direction": "SELL",
            "entry_price": float(order_result["executed_price"]),
            "qty": lot,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "request_order_id": order_result["request_order_id"],
            "execution_status": order_result["execution_status"],
        }
        notifier.send(
            f"🔴 Открытие позиции\n"
            f"{ticker} | SELL\n"
            f"Цена: {order_result['executed_price']}\n"
            f"Количество: {lot}\n"
            f"Сумма сделки: {float(Decimal(str(order_result['executed_price'])) * lot):.2f} ₽\n"
            f"Статус исполнения: {order_result['execution_status']}"
        )


def main():
    log.info("=== Bot v3.2 started ===")
    state.status = "PRECHECK"
    save_runtime()

    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client

    with client_cls(settings.TINVEST_TOKEN) as client:
        watchlist = build_watchlist(client)
        load_instrument_meta(client, watchlist)
        send_session_start(client, watchlist)
        state.status = "SCANNING"
        save_runtime()

        while True:
            try:
                if maybe_reset_daily(client):
                    watchlist = build_watchlist(client)
                    load_instrument_meta(client, watchlist)
                    send_session_start(client, watchlist)

                if state.trades_today >= settings.MAX_TRADES_PER_DAY:
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    notifier.send("⛔ Лимит сделок за день достигнут. Бот остановил входы.")
                    save_runtime()
                    time.sleep(3600)
                    continue

                if state.daily_pnl <= -settings.MAX_DAILY_LOSS_RUB:
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    notifier.send(f"⛔ Дневной лимит убытка достигнут: {float(state.daily_pnl):.2f} ₽")
                    save_runtime()
                    time.sleep(3600)
                    continue

                for item in watchlist:
                    process_instrument(client, item)

                state.session_balance_current = get_money_balance(client)
                state.status = "SCANNING"
                save_runtime()
                time.sleep(settings.CHECK_INTERVAL_SEC)

            except KeyboardInterrupt:
                state.status = "SESSION_FINISHED"
                state.session_balance_current = get_money_balance(client)
                save_runtime()
                notifier.send_session_summary(state.to_dict())
                break
            except Exception as e:
                state.status = "ERROR"
                state.last_error = str(e)
                log.exception("Ошибка основного цикла")
                notifier.send(f"⚠️ Ошибка бота: {e}")
                save_runtime()
                time.sleep(10)


if __name__ == "__main__":
    main()