import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from threading import Thread

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
from app.db import (
    init_db,
    set_runtime,
    get_runtime,
    get_setting,
    list_instruments,
    add_trade,
    log_event,
    upsert_position,
    close_position,
    clear_open_positions,
    upsert_instrument_market_state,
)
from app.instruments import get_instrument_meta, round_to_price_step
from app.telegram_notify import TelegramNotifier
from app.telegram_bot import run_telegram_polling

load_dotenv()

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(settings.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("trading-bot")

init_db()
notifier = TelegramNotifier()


class BotState:
    def __init__(self):
        self.status = "INIT"
        self.session_balance_start = 0.0
        self.session_balance_current = 0.0
        self.daily_pnl = Decimal("0")
        self.trades_today = 0
        self.open_positions = {}
        self.current_trade_date = str(date.today())
        self.instrument_meta = {}

    def sync_runtime(self, last_error=""):
        set_runtime("status", self.status)
        set_runtime("daily_pnl", str(self.daily_pnl))
        set_runtime("trades_today", str(self.trades_today))
        set_runtime("last_error", last_error)
        set_runtime("session_balance_start", str(self.session_balance_start))
        set_runtime("session_balance_current", str(self.session_balance_current))
        set_runtime("current_trade_date", self.current_trade_date)


state = BotState()


def quotation_from_decimal(price: Decimal) -> Quotation:
    normalized = price.quantize(Decimal("0.000000001"))
    units = int(normalized)
    nano = int((normalized - Decimal(units)) * Decimal("1000000000"))
    return Quotation(units=units, nano=nano)


def get_last_price(client, figi: str) -> Decimal:
    resp = client.market_data.get_last_prices(figi=[figi])
    return quotation_to_decimal(resp.last_prices[0].price)

def get_order_book_spread_pct(client, figi: str) -> Decimal:
    try:
        book = client.market_data.get_order_book(figi=figi, depth=1)
        bids = getattr(book, "bids", []) or []
        asks = getattr(book, "asks", []) or []

        if not bids or not asks:
            return Decimal("0")

        best_bid = quotation_to_decimal(bids[0].price)
        best_ask = quotation_to_decimal(asks[0].price)

        if best_bid <= 0 or best_ask <= 0:
            return Decimal("0")

        mid = (best_bid + best_ask) / Decimal("2")
        if mid <= 0:
            return Decimal("0")

        spread_pct = (best_ask - best_bid) / mid
        return spread_pct
    except Exception:
        return Decimal("0")


def get_last_candle_volume(candles) -> int:
    if not candles:
        return 0
    last_candle = candles[-1]
    return int(getattr(last_candle, "volume", 0) or 0)


def is_session_allowed(client, figi: str) -> bool:
    trade_only_session = get_setting("trade_only_session", "0") == "1"
    if not trade_only_session:
        return True

    status = get_trading_status(client, figi)
    status_str = str(status)

    allowed_statuses = {
        "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING",
        "SECURITY_TRADING_STATUS_SESSION_OPEN",
        "SECURITY_TRADING_STATUS_OPENING_PERIOD",
        "SECURITY_TRADING_STATUS_CLOSING_PERIOD",
    }

    return status_str in allowed_statuses or "NORMAL_TRADING" in status_str or "SESSION_OPEN" in status_str


def notify(message: str, is_error: bool = False):
    telegram_errors_only = get_setting("telegram_errors_only", "0") == "1"
    if telegram_errors_only and not is_error:
        return
    notify(message)


def sync_portfolio_positions(client):
    try:
        clear_open_positions(source="PORTFOLIO")

        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        positions = getattr(portfolio, "positions", []) or []

        for p in positions:
            figi = getattr(p, "figi", "") or ""
            instrument_type = str(getattr(p, "instrument_type", "") or "").lower()
            ticker = getattr(p, "ticker", "") or figi

            if not figi:
                continue

            if "currency" in instrument_type:
                continue

            if ticker.upper().startswith("RUB"):
                continue

            qty_obj = getattr(p, "quantity", None)
            avg_obj = getattr(p, "average_position_price", None)
            cur_obj = getattr(p, "current_price", None)

            qty = 0
            try:
                qty = int(quotation_to_decimal(qty_obj)) if qty_obj else 0
            except Exception:
                qty = 0

            if qty == 0:
                continue

            avg_price = float(quotation_to_decimal(avg_obj)) if avg_obj else 0.0
            current_price = float(quotation_to_decimal(cur_obj)) if cur_obj else 0.0

            direction = "LONG"
            if qty < 0:
                direction = "SHORT"

            unrealized_pnl = 0.0
            if avg_price and current_price:
                if qty > 0:
                    unrealized_pnl = (current_price - avg_price) * qty
                else:
                    unrealized_pnl = (avg_price - current_price) * abs(qty)

            upsert_position({
                "ticker": ticker,
                "figi": figi,
                "direction": direction,
                "qty": abs(qty),
                "entry_price": avg_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "OPEN",
                "source": "PORTFOLIO",
            })
    except Exception as e:
        log.warning(f"Не удалось синхронизировать позиции портфеля: {e}")

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


def load_enabled_instruments(client):
    items = list_instruments(enabled_only=True)
    state.instrument_meta = {}

    for item in items:
        meta = get_instrument_meta(client, item["figi"])
        if not meta:
            log_event("INVALID_FIGI", f"Meta not loaded for figi={item['figi']}", ticker=item["ticker"], level="WARNING")
            continue

        state.instrument_meta[item["ticker"]] = {
            "figi": item["figi"],
            "ticker": item["ticker"],
            "lot": item["lot"],
            "name": item["name"],
            "class_code": item.get("class_code", ""),
            "instrument_type": item.get("instrument_type", ""),
            "currency": item.get("currency", ""),
            "min_price_increment": Decimal(str(item["min_price_increment"])),
            "lots_override": int(item["lots_override"]),
            "stop_loss_pct": Decimal(str(item["stop_loss_pct"])),
            "take_profit_pct": Decimal(str(item["take_profit_pct"])),
            "max_spread_pct": Decimal(str(item.get("max_spread_pct", "0"))),
            "min_volume": int(item.get("min_volume", 0)),
            "allow_long": int(item.get("allow_long", 1)),
            "allow_short": int(item.get("allow_short", 1)),
            "priority": int(item.get("priority", 100)),
        }

    return list(state.instrument_meta.values())


def maybe_reset_daily():
    today = str(date.today())
    if state.current_trade_date != today:
        state.daily_pnl = Decimal("0")
        state.trades_today = 0
        state.current_trade_date = today
        log_event("DAILY_RESET", "Daily counters reset")


def place_order_checked(client, ticker: str, figi: str, lots: int, raw_price: Decimal, direction: OrderDirection):
    meta = state.instrument_meta.get(ticker)
    if not meta:
        log_event("ORDER_ERROR", "NO_META_SKIP", ticker=ticker, level="WARNING")
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

        response_order_id = getattr(resp, "order_id", request_order_id)
        log_event("ORDER_OPEN", f"post_order success request={request_order_id} response={response_order_id}", ticker=ticker)

        execution_report_status = "ORDER_STATE_PENDING"
        avg_price = rounded_price
        lots_executed = lots

        for attempt in range(1, 4):
            try:
                time.sleep(1)
                order_state = client.orders.get_order_state(
                    account_id=settings.TINVEST_ACCOUNT_ID,
                    order_id=response_order_id,
                )
                execution_report_status = str(getattr(order_state, "execution_report_status", "UNKNOWN"))
                executed_order_price = getattr(order_state, "executed_order_price", None)
                lots_executed_raw = getattr(order_state, "lots_executed", None)

                if executed_order_price:
                    avg_price = quotation_to_decimal(executed_order_price)

                if lots_executed_raw is not None:
                    lots_executed = int(lots_executed_raw)

                break
            except Exception as e:
                msg = str(e)
                if "50005" in msg or "Order not found" in msg:
                    continue

        return {
            "response_order_id": response_order_id,
            "request_order_id": request_order_id,
            "requested_price": rounded_price,
            "executed_price": avg_price,
            "execution_status": execution_report_status,
            "lots_executed": lots_executed,
        }

    except Exception as e:
        msg = str(e)
        if "figi" in msg.lower():
            log_event("INVALID_FIGI", msg, ticker=ticker, level="WARNING")
            return None
        log_event("ORDER_ERROR", msg, ticker=ticker, level="ERROR")
        raise


def process_instrument(client, item):
    ticker = item["ticker"]
    figi = item["figi"]
    lot = item["lots_override"]
    stop_loss_pct = item["stop_loss_pct"]
    take_profit_pct = item["take_profit_pct"]
    allow_long = int(item.get("allow_long", 1))
    allow_short = int(item.get("allow_short", 1))

    allow_long_global = get_setting("allow_long_global", "1") == "1"
    allow_short_global = get_setting("allow_short_global", "1") == "1"

    if not is_session_allowed(client, figi):
        return

    trading_status = get_trading_status(client, figi)
    if not is_tradable(trading_status):
        return

    try:
        price = get_last_price(client, figi)
        candles = get_candles(client, figi, n=20)
        spread_pct = get_order_book_spread_pct(client, figi)
        last_volume = get_last_candle_volume(candles)
        upsert_instrument_market_state(
            figi=figi,
            ticker=ticker,
            last_price=price,
            price_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            volume_1m=last_volume,
        )
    except Exception as e:
        msg = str(e)
        if "figi" in msg.lower():
            log_event("INVALID_FIGI", msg, ticker=ticker, level="WARNING")
            return
        raise

    if len(candles) < 5:
        return
    
    last_volume = get_last_candle_volume(candles)
    min_volume = int(item.get("min_volume", 0))
    if min_volume > 0 and last_volume < min_volume:
        return

    max_spread_pct = Decimal(str(item.get("max_spread_pct", "0")))
    if max_spread_pct > 0 and spread_pct > max_spread_pct:
        return
    support, resistance = calc_support_resistance(candles)

    if ticker in state.open_positions:
        pos = state.open_positions[ticker]
        entry_price = Decimal(str(pos["entry_price"]))
        direction = pos["direction"]
        qty = int(pos["qty"])

        unrealized_pnl = Decimal("0")
        if direction == "BUY":
            unrealized_pnl = (price - entry_price) * qty
        else:
            unrealized_pnl = (entry_price - price) * qty

        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": direction,
            "qty": qty,
            "entry_price": float(entry_price),
            "current_price": float(price),
            "unrealized_pnl": float(unrealized_pnl),
            "opened_at": pos["opened_at"],
            "status": "OPEN",
            "source": "BOT",
        })

        if direction == "BUY":
            sl_price = entry_price * (Decimal("1") - stop_loss_pct)
            tp_price = entry_price * (Decimal("1") + take_profit_pct)
        else:
            sl_price = entry_price * (Decimal("1") + stop_loss_pct)
            tp_price = entry_price * (Decimal("1") - take_profit_pct)

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
            close_dir = OrderDirection.ORDER_DIRECTION_SELL if direction == "BUY" else OrderDirection.ORDER_DIRECTION_BUY
            order_result = place_order_checked(client, ticker, figi, qty, price, close_dir)
            if not order_result:
                return

            exit_price = Decimal(str(order_result["executed_price"]))
            exec_qty = int(order_result["lots_executed"] or qty)
            gross_amount = exit_price * exec_qty
           
            estimated_commission_pct = Decimal(get_setting("estimated_commission_pct", str(settings.ESTIMATED_COMMISSION_PCT)))
            commission = (entry_price * exec_qty + exit_price * exec_qty) * estimated_commission_pct

            if direction == "BUY":
                pnl = (exit_price - entry_price) * exec_qty - commission
            else:
                pnl = (entry_price - exit_price) * exec_qty - commission

            state.daily_pnl += pnl
            state.trades_today += 1

            trade = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": ticker,
                "figi": figi,
                "direction": direction,
                "entry": float(entry_price),
                "exit": float(exit_price),
                "qty": exec_qty,
                "gross_amount": float(gross_amount),
                "commission": float(commission),
                "pnl": float(pnl),
                "reason": close_reason,
                "close_order_id": order_result["response_order_id"],
                "execution_status": order_result["execution_status"],
            }
            add_trade(trade)
            close_position(figi, source="BOT")
            log_event("ORDER_CLOSE", f"{ticker} pnl={float(pnl):.2f} reason={close_reason}", ticker=ticker)
            del state.open_positions[ticker]

            notify(
                f"✅ Закрытие позиции\n"
                f"{ticker} | {direction}\n"
                f"Вход: {entry_price}\n"
                f"Выход: {exit_price}\n"
                f"PnL: {float(pnl):.2f} ₽\n"
                f"Причина: {close_reason}"
            )
        return

    if len(state.open_positions) >= int(get_setting("max_open_positions", "2")):
        return

    sig = get_signal(price, support, resistance)
    if not sig:
        return

    log_event("SIGNAL", f"{sig} on {ticker}", ticker=ticker)

    if sig == "BUY":
        if not allow_long_global or not allow_long:
            return

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_BUY)
        if not order_result:
            return
        state.open_positions[ticker] = {
            "figi": figi,
            "direction": "BUY",
            "entry_price": float(order_result["executed_price"]),
            "qty": int(order_result["lots_executed"] or lot),
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
        }

        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": "BUY",
            "qty": int(order_result["lots_executed"] or lot),
            "entry_price": float(order_result["executed_price"]),
            "current_price": float(order_result["executed_price"]),
            "unrealized_pnl": 0,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            "source": "BOT",
        })

        notify(f"🟢 Открытие позиции\n{ticker} | BUY\nЦена: {order_result['executed_price']}")

    elif sig == "SELL":
        if not allow_short_global or not allow_short:
            return

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_SELL)
        if not order_result:
            return
        state.open_positions[ticker] = {
            "figi": figi,
            "direction": "SELL",
            "entry_price": float(order_result["executed_price"]),
            "qty": int(order_result["lots_executed"] or lot),
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
        }

        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": "SELL",
            "qty": int(order_result["lots_executed"] or lot),
            "entry_price": float(order_result["executed_price"]),
            "current_price": float(order_result["executed_price"]),
            "unrealized_pnl": 0,
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            "source": "BOT",
        })

        notify(f"🔴 Открытие позиции\n{ticker} | SELL\nЦена: {order_result['executed_price']}")

def main():

    log.info("=== Bot v4.2 started  610 line ===")
    log_event("BOT_START", "Bot started")

    if settings.TELEGRAM_ENABLED and settings.TELEGRAM_POLLING_ENABLED:
        Thread(target=run_telegram_polling, daemon=True).start()

    state.status = "PRECHECK"
    state.sync_runtime()

    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client

    with client_cls(settings.TINVEST_TOKEN) as client:
        state.session_balance_start = get_money_balance(client)
        state.session_balance_current = state.session_balance_start
        state.status = "SCANNING"
        state.sync_runtime()

        while True:
            try:
                maybe_reset_daily()

                if get_setting("bot_enabled", "1") != "1":
                    state.status = "DISABLED"
                    state.sync_runtime()
                    time.sleep(5)
                    continue

                if state.trades_today >= int(get_setting("max_trades_per_day", "15")):
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    state.sync_runtime()
                    time.sleep(60)
                    continue

                max_loss = Decimal(get_setting("max_daily_loss_rub", "200"))
                if state.daily_pnl <= -max_loss:
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    state.sync_runtime()
                    time.sleep(60)
                    continue

                watchlist = load_enabled_instruments(client)

                for item in watchlist:
                    process_instrument(client, item)

                sync_portfolio_positions(client)

                state.session_balance_current = get_money_balance(client)
                state.status = "SCANNING"
                state.sync_runtime()
                sleep_sec = int(get_setting("check_interval_sec", str(settings.CHECK_INTERVAL_SEC)))
                time.sleep(sleep_sec)

            except KeyboardInterrupt:
                state.status = "STOPPED"
                state.sync_runtime()
                log_event("BOT_STOP", "Bot stopped by keyboard interrupt")
                break
            except Exception as e:
                state.status = "ERROR"
                state.sync_runtime(last_error=str(e))
                log.exception("Ошибка основного цикла")
                log_event("BOT_ERROR", str(e), level="ERROR")
                notify(f"⚠️ Ошибка бота: {e}", is_error=True)

                pause_after_error_sec = int(get_setting("pause_after_error_sec", "10"))
                time.sleep(pause_after_error_sec)

if __name__ == "__main__":
    main()