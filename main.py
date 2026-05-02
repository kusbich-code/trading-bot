import logging
import os
import queue as _queue
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from threading import Thread, Lock, Event
from typing import Dict, Optional, Set

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

# Классы MarketDataStream (опционально — откат к поллингу если недоступны)
try:
    from t_tech.invest import (
        MarketDataRequest,
        SubscribeLastPriceRequest,
        LastPriceInstrument,
        SubscribeOrderBookRequest,
        OrderBookInstrument,
        SubscriptionAction,
    )
    _STREAM_IMPORTS_OK = True
except Exception:
    _STREAM_IMPORTS_OK = False

from app.config import settings
from app.version import __version__ as BOT_VERSION
from app.services.strategy_engine import evaluate_signal as _evaluate_signal
from app.db import (
    init_db,
    set_runtime,
    get_runtime,
    get_setting,
    list_active_strategy_instruments,
    add_trade,
    log_event,
    upsert_position,
    close_position,
    clear_open_positions,
    upsert_instrument_market_state,
    list_parallel_strategies,
    list_profile_parallel_strategies,
    get_profile_setting,
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


# ── Координация параллельных стратегий ───────────────────────────────────────

class _ParallelCoord:
    """
    Гарантирует, что только ОДНА позиция открыта во всех потоках параллельных стратегий.

    Протокол:
      1. Перед открытием позиции: вызовите try_claim(strategy_id, figi).
         Возвращает True только если ни один другой поток не удерживает блокировку.
      2. После закрытия: вызовите release(strategy_id).
      3. Остальные потоки проверяют is_free() каждый цикл; если False и они не
         владелец, они пропускают обработку инструмента и ждут.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._owner_sid: Optional[int] = None
        self._owner_figi: Optional[str] = None
        self._owner_ticker: Optional[str] = None

    @property
    def is_free(self) -> bool:
        with self._lock:
            return self._owner_sid is None

    def try_claim(self, sid: int, figi: str, ticker: str = "") -> bool:
        with self._lock:
            if self._owner_sid is None:
                self._owner_sid    = sid
                self._owner_figi   = figi
                self._owner_ticker = ticker
                self._persist()
                return True
            return False

    def release(self, sid: int) -> bool:
        with self._lock:
            if self._owner_sid == sid:
                self._owner_sid    = None
                self._owner_figi   = None
                self._owner_ticker = None
                self._persist()
                return True
            return False

    def _persist(self):
        try:
            import json as _j
            set_runtime("parallel_coord", _j.dumps({
                "owner_strategy_id": self._owner_sid,
                "owner_figi":        self._owner_figi,
                "owner_ticker":      self._owner_ticker,
            }))
        except Exception:
            pass

    def is_owner(self, sid: int) -> bool:
        with self._lock:
            return self._owner_sid == sid

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "owner_strategy_id": self._owner_sid,
                "owner_figi":        self._owner_figi,
                "owner_ticker":      self._owner_ticker,
            }


_parallel_coord = _ParallelCoord()

# Словарь статусов по потокам: strategy_id → {status, ticker, updated_at}
_parallel_status: Dict[int, dict] = {}
_parallel_status_lock = threading.Lock()

# События остановки для каждого параллельного потока: strategy_id → Event
_parallel_stop_events: Dict[int, threading.Event] = {}


def _pset(sid: int, status: str, ticker: str = ""):
    info = {
        "status":     status,
        "ticker":     ticker,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }
    with _parallel_status_lock:
        _parallel_status[sid] = info
    # Записываем в БД, чтобы webapp.py (другой процесс) мог прочитать
    try:
        import json as _json
        set_runtime(f"parallel_thread_{sid}", _json.dumps(info))
    except Exception:
        pass


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

# ── Поток ордеров (Приоритет-1) ───────────────────────────────────────────────
# Сопоставляет response_order_id → Queue; place_order_checked() блокируется на очереди,
# _orders_stream_worker() помещает объект OrderTrades при получении исполнения.
_pending_orders: Dict[str, _queue.Queue] = {}
_bot_client_cls = None  # устанавливается в main() до запуска потока стрима


def _orders_stream_worker():
    """Фоновый поток: подписывается на события исполнения ордеров через OrdersStream."""
    account_id = settings.TINVEST_ACCOUNT_ID
    while True:
        try:
            with _bot_client_cls(settings.TINVEST_TOKEN) as sc:
                log.info("OrdersStream: подключён")
                for resp in sc.orders_stream.trade_stream(accounts=[account_id]):
                    ot = getattr(resp, "order_trades", None)
                    if ot is None:
                        continue  # пинг-фрейм
                    order_id = str(getattr(ot, "order_id", "") or "")
                    q = _pending_orders.get(order_id)
                    if q is not None:
                        q.put(ot)
        except Exception as exc:
            log.warning("OrdersStream: ошибка %s, переподключение через 5 с", exc)
            time.sleep(5)


# ── MarketDataStream (Приоритет-2) ────────────────────────────────────────────
# figi → {"price": Decimal, "orderbook": {...}}   — записывается потоком стрима,
# читается process_instrument() в основном потоке.
_md: Dict[str, dict] = {}
_md_lock = Lock()
_md_instruments: list = []           # список словарей {figi, instrument_uid}
_md_figis: Set[str] = set()          # множество figi для обнаружения изменений
_md_restart = Event()                 # установка → воркер переподключается с новыми инструментами


def _make_md_gen(instruments: list, stop_ev: Event):
    """
    Генератор, отправляющий подписки MarketDataStream, затем блокирующийся до stop_ev.
    instruments: список словарей с 'figi' и опциональным 'instrument_uid'.
    Использует instrument_uid (предпочтительно) или figi (резервный вариант) для каждой подписки.
    """
    if not instruments or not _STREAM_IMPORTS_OK:
        stop_ev.wait()
        return

    def _lp_instr(m):
        uid = m.get("instrument_uid", "")
        return LastPriceInstrument(instrument_id=uid) if uid else LastPriceInstrument(figi=m["figi"])

    def _ob_instr(m):
        uid = m.get("instrument_uid", "")
        return OrderBookInstrument(instrument_id=uid, depth=10) if uid else OrderBookInstrument(figi=m["figi"], depth=10)

    yield MarketDataRequest(
        subscribe_last_price_request=SubscribeLastPriceRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[_lp_instr(m) for m in instruments],
        )
    )
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[_ob_instr(m) for m in instruments],
        )
    )
    stop_ev.wait()


def _market_data_stream_worker():
    """Фоновый поток: последние цены в реальном времени + стакан заявок (depth=10)."""
    if not _STREAM_IMPORTS_OK:
        log.warning("MarketDataStream: импорты недоступны, используется поллинг")
        return
    while True:
        if _bot_client_cls is None:
            time.sleep(2)
            continue
        instruments = list(_md_instruments)
        if not instruments:
            time.sleep(5)
            continue
        _md_restart.clear()
        stop_ev = Event()

        def _watcher():
            _md_restart.wait()
            stop_ev.set()
        Thread(target=_watcher, daemon=True).start()

        try:
            with _bot_client_cls(settings.TINVEST_TOKEN) as sc:
                log.info("MarketDataStream: подключён (%d инструментов)", len(instruments))
                for resp in sc.market_data_stream.market_data_stream(
                    _make_md_gen(instruments, stop_ev)
                ):
                    lp = getattr(resp, "last_price", None)
                    if lp:
                        figi = getattr(lp, "figi", "")
                        price = quotation_to_decimal(getattr(lp, "price", None))
                        if figi and price > 0:
                            with _md_lock:
                                _md.setdefault(figi, {})["price"] = Decimal(str(price))

                    ob = getattr(resp, "orderbook", None)
                    if ob:
                        figi = getattr(ob, "figi", "")
                        bids = list(getattr(ob, "bids", []) or [])
                        asks = list(getattr(ob, "asks", []) or [])
                        bid_vol = sum(int(getattr(b, "quantity", 0)) for b in bids)
                        ask_vol = sum(int(getattr(a, "quantity", 0)) for a in asks)
                        bp = Decimal(str(quotation_to_decimal(bids[0].price))) if bids else Decimal("0")
                        ap = Decimal(str(quotation_to_decimal(asks[0].price))) if asks else Decimal("0")
                        mid = (bp + ap) / 2
                        spread = (ap - bp) / mid if mid > 0 else Decimal("0")
                        with _md_lock:
                            _md.setdefault(figi, {})["orderbook"] = {
                                "bid_price": bp, "ask_price": ap,
                                "bid_vol": bid_vol, "ask_vol": ask_vol,
                                "spread_pct": spread,
                            }

                    if stop_ev.is_set():
                        break
        except Exception as exc:
            log.warning("MarketDataStream: ошибка %s, переподключение через 5 с", exc)
            stop_ev.set()
            time.sleep(5)


def get_ma_signal(candles, short_period: int = 20, long_period: int = 100):
    """Пересечение двух скользящих средних. Возвращает 'BUY', 'SELL' или None."""
    if len(candles) < long_period + 1:
        return None
    closes = [float(quotation_to_decimal(c.close)) for c in candles]
    def _ma(data, n): return sum(data[-n:]) / n
    short_now  = _ma(closes,       short_period)
    long_now   = _ma(closes,       long_period)
    short_prev = _ma(closes[:-1],  short_period)
    long_prev  = _ma(closes[:-1],  long_period)
    if short_prev <= long_prev and short_now > long_now:
        return "BUY"
    if short_prev >= long_prev and short_now < long_now:
        return "SELL"
    return None


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


def _candles_to_dicts(candles) -> list:
    """Конвертирует сырые объекты свечей t_tech → List[Dict] для strategy_engine."""
    out = []
    for c in candles:
        try:
            out.append({
                "open":   float(quotation_to_decimal(c.open)),
                "high":   float(quotation_to_decimal(c.high)),
                "low":    float(quotation_to_decimal(c.low)),
                "close":  float(quotation_to_decimal(c.close)),
                "volume": int(getattr(c, "volume", 0) or 0),
                "time":   c.time.isoformat() if getattr(c, "time", None) else "",
            })
        except Exception:
            continue
    return out


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
    notifier.send(message)


def sync_portfolio_positions(client):
    """Синхронизирует позиции PORTFOLIO из API брокера в локальную БД (source=PORTFOLIO)."""
    try:
        clear_open_positions(source="PORTFOLIO")

        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        positions = getattr(portfolio, "positions", []) or []

        for p in positions:
            figi = getattr(p, "figi", "") or ""
            instrument_type = str(getattr(p, "instrument_type", "") or "").lower()
            ticker = getattr(p, "ticker", "") or figi[:8]

            if not figi or "currency" in instrument_type or ticker.upper().startswith("RUB"):
                continue

            # qty в ЛОТАХ (quantity_lots), не в штуках (quantity)
            qty_lots_obj = getattr(p, "quantity_lots", None)
            qty_shares_obj = getattr(p, "quantity", None)
            avg_obj = getattr(p, "average_position_price", None)
            cur_obj = getattr(p, "current_price", None)
            yield_obj = getattr(p, "expected_yield", None)

            qty_lots = 0
            qty_shares = 0
            try:
                if qty_lots_obj is not None:
                    qty_lots = int(quotation_to_decimal(qty_lots_obj))
                if qty_shares_obj is not None:
                    qty_shares = int(quotation_to_decimal(qty_shares_obj))
            except Exception:
                pass

            # Предпочитаем лоты; откат к штукам если quantity_lots недоступен
            qty = qty_lots if qty_lots != 0 else qty_shares
            if qty == 0:
                continue

            avg_price = float(quotation_to_decimal(avg_obj)) if avg_obj else 0.0
            current_price = float(quotation_to_decimal(cur_obj)) if cur_obj else 0.0
            expected_yield = float(quotation_to_decimal(yield_obj)) if yield_obj else 0.0

            # Согласовано с BOT: BUY = лонг, SELL = шорт
            direction = "SELL" if qty < 0 else "BUY"

            # Вычисляем нереализованный PnL если брокер вернул 0 (часто в sandbox)
            if expected_yield == 0.0 and avg_price > 0 and current_price > 0:
                lots = abs(qty)
                if direction == "BUY":
                    expected_yield = (current_price - avg_price) * lots
                else:
                    expected_yield = (avg_price - current_price) * lots

            upsert_position({
                "ticker": ticker,
                "figi": figi,
                "direction": direction,
                "qty": abs(qty),
                "entry_price": avg_price,
                "current_price": current_price,
                "unrealized_pnl": expected_yield,
                "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "OPEN",
                "source": "PORTFOLIO",
            })
    except Exception as e:
        log.warning(f"sync_portfolio_positions error: {e}")

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
    items = list_active_strategy_instruments()
    state.instrument_meta = {}

    for item in items:
        meta = get_instrument_meta(item["figi"])
        if not meta:
            log_event("INVALID_FIGI", f"Meta not loaded for figi={item['figi']}", ticker=item["ticker"], level="WARNING")
            continue

        state.instrument_meta[item["ticker"]] = {
            "figi": item["figi"],
            "instrument_uid": item.get("instrument_uid", "") or "",
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

    new_figis = {v["figi"] for v in state.instrument_meta.values()}
    if new_figis != _md_figis:
        _md_figis.clear()
        _md_figis.update(new_figis)
        _md_instruments.clear()
        _md_instruments.extend(
            {"figi": v["figi"], "instrument_uid": v.get("instrument_uid", "")}
            for v in state.instrument_meta.values()
        )
        _md_restart.set()

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
        avg_price = rounded_price   # всегда за штуку; используется как резервный
        lots_executed = lots

        def _accept_fill_price(ep: Decimal) -> bool:
            """
            Sandbox возвращает executed_order_price как цену за ЛОТ (не за штуку).
            Например, SBER lot=10, price=323.7 → sandbox вернёт 3237.
            Реальное исполнение лимитного ордера отклоняется ≤ 5% от лимитной цены.
            Если возвращённая цена отклоняется больше, это цена за лот — отклоняем,
            чтобы сохранить корректную цену за штуку rounded_price.
            """
            if ep <= 0 or rounded_price <= 0:
                return False
            ratio = ep / rounded_price
            return Decimal("0.95") <= ratio <= Decimal("1.05")

        # Быстрый путь: ждём уведомления об исполнении из OrdersStream (до 6 с)
        result_q: _queue.Queue = _queue.Queue()
        _pending_orders[response_order_id] = result_q
        try:
            order_trades = result_q.get(timeout=6)
            trades = getattr(order_trades, "trades", []) or []
            if trades:
                total_filled = sum(int(getattr(t, "quantity", 0)) for t in trades)
                if total_filled > 0:
                    weighted = sum(
                        quotation_to_decimal(getattr(t, "price")) * int(getattr(t, "quantity", 0))
                        for t in trades
                    )
                    _ep = weighted / Decimal(str(total_filled))
                    if _accept_fill_price(_ep):
                        avg_price = _ep
                    elif _ep > 0:
                        log_event("ORDER_FILL",
                            f"{ticker} stream price {_ep} отклонён (≠ per-share limit {rounded_price}), используем limit",
                            ticker=ticker)
                    lots_executed = total_filled
            execution_report_status = "ORDER_STATE_FILL"
            log_event("ORDER_FILL", f"stream fill: {ticker} qty={lots_executed} price={avg_price}", ticker=ticker)
        except _queue.Empty:
            # Резервный вариант: опрос get_order_state
            for _ in range(4):
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
                        _ep = quotation_to_decimal(executed_order_price)
                        if _accept_fill_price(_ep):
                            avg_price = _ep
                        elif _ep > 0:
                            log_event("ORDER_FILL",
                                f"{ticker} poll price {_ep} отклонён (≠ per-share limit {rounded_price}), используем limit",
                                ticker=ticker)
                    if lots_executed_raw is not None:
                        lots_executed = int(lots_executed_raw)
                    break
                except Exception as e:
                    msg = str(e)
                    if "50005" in msg or "Order not found" in msg:
                        continue
        finally:
            _pending_orders.pop(response_order_id, None)

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


def process_instrument(client, item,
                       _strategy_id: Optional[int] = None,
                       _strategy_cfg: Optional[Dict] = None,
                       _positions: Optional[Dict] = None,
                       _coord: Optional[_ParallelCoord] = None):
    """
    Обрабатывает один инструмент за один цикл стратегии.

    Параллельный режим: передайте _strategy_id, _strategy_cfg (словарь настроек),
    _positions (словарь открытых позиций потока) и _coord.
    Основной/legacy режим: оставьте все как None — использует глобальное состояние + get_setting().
    """
    def _cfg(key, default=""):
        if _strategy_cfg is not None:
            return _strategy_cfg.get(key, default)
        return get_setting(key, default)

    positions = _positions if _positions is not None else state.open_positions

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

    tradingmode           = _cfg("tradingmode", "trend")
    trailing_stop_enabled = _cfg("trailing_stop_enabled", "0") == "1"
    use_signal_service    = _cfg("use_signal_service", "0") == "1"

    try:
        # ── Цена: стрим (быстро) → REST резервный вариант ────────────────────
        with _md_lock:
            stream_md = _md.get(figi, {})
        stream_price = stream_md.get("price")
        price = stream_price if (stream_price and stream_price > 0) else get_last_price(client, figi)

        # ── Свечи: 50 баров достаточно для всех режимов strategy_engine ──────
        candle_n = 50
        candles = get_candles(client, figi, n=candle_n)
        last_volume = get_last_candle_volume(candles)

        # ── Стакан: стрим depth=10 → REST depth=1 резервный вариант ─────────
        ob_stream = stream_md.get("orderbook", {})
        if ob_stream:
            spread_pct = ob_stream["spread_pct"]
            bid_vol: int = ob_stream["bid_vol"]
            ask_vol: int = ob_stream["ask_vol"]
        else:
            spread_pct = get_order_book_spread_pct(client, figi)
            bid_vol = ask_vol = 0

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

    if ticker in positions:
        pos = positions[ticker]
        entry_price = Decimal(str(pos["entry_price"]))
        direction = pos["direction"]
        qty = int(pos["qty"])

        # Защита: если entry_price в лотах (legacy sandbox), нормализуем до цены за штуку.
        # Обнаружение: entry_price должна быть в том же диапазоне что текущая цена (в пределах 5×).
        lot_size = item.get("lot", 1)
        if lot_size > 1 and price > 0 and entry_price > price * Decimal("5"):
            entry_price = entry_price / Decimal(str(lot_size))
            pos["entry_price"] = float(entry_price)  # исправляем также в памяти

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

        if trailing_stop_enabled:
            # Трейлинг-стоп: движется вместе с ценой, никогда против прибыли
            if direction == "BUY":
                new_ts = price * (Decimal("1") - stop_loss_pct)
                cur_ts = Decimal(str(pos.get("trailing_stop") or 0))
                trailing = max(cur_ts, new_ts)
                pos["trailing_stop"] = float(trailing)
                sl_price = trailing
            else:
                new_ts = price * (Decimal("1") + stop_loss_pct)
                cur_ts = Decimal(str(pos.get("trailing_stop") or "999999"))
                trailing = min(cur_ts, new_ts)
                pos["trailing_stop"] = float(trailing)
                sl_price = trailing
        else:
            if direction == "BUY":
                sl_price = entry_price * (Decimal("1") - stop_loss_pct)
            else:
                sl_price = entry_price * (Decimal("1") + stop_loss_pct)

        if direction == "BUY":
            tp_price = entry_price * (Decimal("1") + take_profit_pct)
        else:
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
           
            estimated_commission_pct = Decimal(_cfg("estimated_commission_pct", str(settings.ESTIMATED_COMMISSION_PCT)))
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
            del positions[ticker]
            if _coord is not None:
                _coord.release(_strategy_id)

            notify(
                f"✅ Закрытие позиции\n"
                f"{ticker} | {direction}\n"
                f"Вход: {entry_price}\n"
                f"Выход: {exit_price}\n"
                f"PnL: {float(pnl):.2f} ₽\n"
                f"Причина: {close_reason}"
            )
        return

    if _coord is not None:
        # Параллельный режим: пропускаем если другая стратегия удерживает блокировку позиции
        if not _coord.is_free and not _coord.is_owner(_strategy_id):
            return
    elif len(positions) >= int(_cfg("max_open_positions", "2")):
        return

    # ── Сигнал через strategy_engine (та же логика что и бэктест) ───────────
    candles_dict = _candles_to_dicts(candles)
    sig_result = _evaluate_signal(tradingmode, candles_dict)
    sig   = sig_result["action"]   # "BUY" | "SELL" | "HOLD"
    score = sig_result["score"]

    if sig == "HOLD":
        return

    min_score = int(_cfg("min_signal_score", "0"))
    if min_score > 0 and score < min_score:
        log_event(
            "SIGNAL_SKIP",
            f"{ticker}: score={score} < порог={min_score} [{tradingmode}], пропуск",
            ticker=ticker,
        )
        return

    log_event("SIGNAL", f"{sig} [{tradingmode}] score={score} on {ticker}", ticker=ticker)

    # ── Фильтр сервиса сигналов T-Bank ───────────────────────────────────────
    if use_signal_service:
        from app.services.tbank_client import get_tbank_signals
        analyst = get_tbank_signals([figi]).get(figi, "NEUTRAL")
        if sig == "BUY" and analyst == "SELL":
            log_event("SIGNAL_SKIP", f"{ticker} BUY пропущен: аналитики SELL", ticker=ticker)
            return
        if sig == "SELL" and analyst == "BUY":
            log_event("SIGNAL_SKIP", f"{ticker} SELL пропущен: аналитики BUY", ticker=ticker)
            return

    # ── Проверка баланса перед ордером ───────────────────────────────────────
    if sig == "BUY":
        lot_size = item.get("lot", 1)
        commission_pct = Decimal(_cfg("estimated_commission_pct", "0.0004"))
        required = Decimal(str(lot)) * Decimal(str(lot_size)) * price * (1 + commission_pct)
        available = Decimal(str(state.session_balance_current))
        if available < required:
            log_event(
                "BALANCE_WARNING",
                f"{ticker}: сигнал BUY, требуется ≈{float(required):.0f} ₽ "
                f"({lot} лот × {lot_size} шт × {float(price):.2f} ₽), "
                f"доступно {float(available):.0f} ₽ — пропуск",
                ticker=ticker,
            )
            return

    # ── Фильтр давления стакана (только при наличии данных стрима) ───────────
    total_vol = bid_vol + ask_vol
    if total_vol > 0:
        buy_pressure = bid_vol / total_vol   # доля объёма на стороне бид
        sell_pressure = ask_vol / total_vol
        if sig == "BUY" and buy_pressure < 0.40:
            log_event("SIGNAL_SKIP", f"{ticker} BUY пропущен: давление покупателей {buy_pressure:.0%}", ticker=ticker)
            return
        if sig == "SELL" and sell_pressure < 0.40:
            log_event("SIGNAL_SKIP", f"{ticker} SELL пропущен: давление продавцов {sell_pressure:.0%}", ticker=ticker)
            return

    if sig == "BUY":
        if not allow_long_global or not allow_long:
            return
        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return  # другой поток только что захватил первым

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_BUY)
        if not order_result:
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        positions[ticker] = {
            "figi": figi,
            "direction": "BUY",
            "entry_price": _ep,
            "qty": int(order_result["lots_executed"] or lot),
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
            "trailing_stop": _ep * (1 - float(stop_loss_pct)),
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
        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_SELL)
        if not order_result:
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        positions[ticker] = {
            "figi": figi,
            "direction": "SELL",
            "entry_price": _ep,
            "qty": int(order_result["lots_executed"] or lot),
            "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
            "trailing_stop": _ep * (1 + float(stop_loss_pct)),
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

def _parallel_strategy_worker(strategy_id: int, strat_name: str, stop_ev: threading.Event):
    """
    Рабочий поток для одной параллельной стратегии.

    Жизненный цикл за цикл:
      1. Если другая стратегия удерживает блокировку позиции → пропустить, ждать.
      2. Иначе сканировать все активные инструменты этой стратегии.
      3. process_instrument() захватывает блокировку при открытии позиции.
      4. Поток-владелец блокировки продолжает проверять SL/TP до закрытия,
         затем освобождает блокировку и все потоки возобновляют работу.
    """
    _pset(strategy_id, "запуск")
    log.info("Parallel [%s] thread started", strat_name)

    positions: Dict = {}  # хранилище позиций потока (макс. 1 в параллельном режиме)

    while not stop_ev.is_set():
        try:
            # Перезагружаем настройки каждый цикл (отражает изменения в UI)
            cfg   = {row["key"]: row["value"]
                     for row in _parallel_cfg_rows(strategy_id)}
            instr = _parallel_instruments(strategy_id)

            interval = int(cfg.get("check_interval_sec", "5"))

            # Если нет открытых позиций и координатор занят → ждём
            if not positions and not _parallel_coord.is_free and not _parallel_coord.is_owner(strategy_id):
                _pset(strategy_id, "ожидание — другая стратегия в позиции")
                stop_ev.wait(timeout=interval)
                continue

            if get_setting("bot_enabled", "1") != "1":
                _pset(strategy_id, "бот выключен")
                stop_ev.wait(timeout=interval)
                continue

            if not instr:
                _pset(strategy_id, "нет инструментов")
                stop_ev.wait(timeout=interval)
                continue

            client_cls = _bot_client_cls
            if client_cls is None:
                stop_ev.wait(timeout=3)
                continue

            with client_cls(settings.TINVEST_TOKEN) as client:
                for item in instr:
                    if stop_ev.is_set():
                        break
                    # Проверка в середине сканирования: кто-то другой только что открыл
                    if not positions and not _parallel_coord.is_free and not _parallel_coord.is_owner(strategy_id):
                        break
                    _pset(strategy_id, "сканирование", item["ticker"])
                    try:
                        process_instrument(
                            client, item,
                            _strategy_id=strategy_id,
                            _strategy_cfg=cfg,
                            _positions=positions,
                            _coord=_parallel_coord,
                        )
                    except Exception as exc:
                        log.warning("Parallel [%s] %s: %s", strat_name, item.get("ticker", "?"), exc)

            status = "в позиции" if positions else "ожидание сигнала"
            ticker = list(positions.keys())[0] if positions else ""
            _pset(strategy_id, status, ticker)

        except Exception as exc:
            log.warning("Parallel [%s] цикл: %s", strat_name, exc)
            _pset(strategy_id, f"ошибка: {exc}")

        stop_ev.wait(timeout=int(cfg.get("check_interval_sec", "5")) if "cfg" in dir() else 5)

    _pset(strategy_id, "остановлен")
    _parallel_coord.release(strategy_id)
    log.info("Parallel [%s] thread stopped", strat_name)


def _parallel_cfg_rows(strategy_id: int) -> list:
    from app.db import db_cursor as _dbc
    with _dbc() as cur:
        cur.execute("SELECT key, value FROM strategy_settings WHERE strategy_id = ?", (strategy_id,))
        return cur.fetchall()


def _parallel_instruments(strategy_id: int) -> list:
    from app.db import list_strategy_instruments, get_instrument_meta
    rows = list_strategy_instruments(strategy_id)
    result = []
    for item in rows:
        if not str(item.get("enabled", "1")) in ("1", "true"):
            continue
        meta = get_instrument_meta(item["figi"])
        if not meta:
            continue
        result.append({
            "figi":              item["figi"],
            "instrument_uid":    item.get("instrument_uid", "") or "",
            "ticker":            item["ticker"],
            "lot":               item.get("lot", 1),
            "name":              item.get("name", ""),
            "class_code":        item.get("class_code", ""),
            "instrument_type":   item.get("instrument_type", ""),
            "currency":          item.get("currency", ""),
            "min_price_increment": Decimal(str(item.get("min_price_increment", "0.01"))),
            "lots_override":     int(item.get("lots_override", 1)),
            "stop_loss_pct":     Decimal(str(item.get("stop_loss_pct", "0.0025"))),
            "take_profit_pct":   Decimal(str(item.get("take_profit_pct", "0.005"))),
            "max_spread_pct":    Decimal(str(item.get("max_spread_pct", "0"))),
            "min_volume":        int(item.get("min_volume", 0)),
            "allow_long":        int(item.get("allow_long", 1)),
            "allow_short":       int(item.get("allow_short", 1)),
            "priority":          int(item.get("priority", 100)),
        })
    result.sort(key=lambda x: x["priority"])
    return result


def start_parallel_workers(stop_ev: threading.Event) -> list:
    """
    Запускает по одному потоку на каждую стратегию из параллельного списка активного профиля.
    Запускается только если у профиля parallel_trading_enabled=1.
    Возвращает список запущенных потоков.
    """
    active_profile_id = get_setting("active_profile_id", "").strip()
    if not active_profile_id:
        return []

    pid = int(active_profile_id)
    parallel_on = get_profile_setting(pid, "parallel_trading_enabled", "0")
    if parallel_on != "1":
        return []

    entries = list_profile_parallel_strategies(pid)
    if not entries:
        log.info("Parallel trading enabled but no strategies configured for profile %d", pid)
        return []

    threads = []
    for entry in entries:
        sid  = entry["strategy_id"]
        name = entry["name"]
        t = Thread(
            target=_parallel_strategy_worker,
            args=(sid, name, stop_ev),
            daemon=True,
            name=f"parallel-{sid}",
        )
        _parallel_stop_events[sid] = stop_ev
        t.start()
        threads.append(t)
        log.info("Parallel strategy '%s' (id=%d) started", name, sid)
    return threads


def main():

    log.info("=== Bot v%s started ===", BOT_VERSION)
    log_event("BOT_START", "Bot started")

    if settings.TELEGRAM_ENABLED and settings.TELEGRAM_POLLING_ENABLED:
        Thread(target=run_telegram_polling, daemon=True).start()

    state.status = "PRECHECK"
    state.sync_runtime()

    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client

    global _bot_client_cls
    _bot_client_cls = client_cls
    Thread(target=_orders_stream_worker, daemon=True, name="orders-stream").start()
    Thread(target=_market_data_stream_worker, daemon=True, name="market-data-stream").start()
    log.info("OrdersStream + MarketDataStream workers started")

    _parallel_stop_ev = threading.Event()
    start_parallel_workers(_parallel_stop_ev)
    log.info("Parallel strategy workers started")

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