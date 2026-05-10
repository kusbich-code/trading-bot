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
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
)
from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest.utils import quotation_to_decimal
from t_tech.invest.schemas import (
    GetTechAnalysisRequest,
    IndicatorType,
    IndicatorInterval,
    TypeOfPrice,
    Deviation,
    Smoothing,
)

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
    get_open_positions,
    get_instrument_market_state_map,
    save_instrument_uid,
    fix_ticker_by_figi,
    get_instrument_sl_tp,
)
from app.instruments import get_instrument_meta, round_to_price_step
from app.telegram_notify import TelegramNotifier
from app.telegram_bot import run_telegram_polling
from app.weekly_scheduler import start_weekly_scheduler

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

_MSK = timezone(timedelta(hours=3))

def _now() -> datetime:
    """Текущее московское время (UTC+3), naive — для записи в БД и логи."""
    return datetime.now(tz=_MSK).replace(tzinfo=None)


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

# Состояние параллельных воркеров — управляется через _refresh_parallel_workers()
_parallel_threads: list = []
_parallel_main_stop_ev: Optional[threading.Event] = None
_last_parallel_sig: tuple = ()
_last_parallel_check_ts: float = 0.0


def _pset(sid: int, status: str, ticker: str = ""):
    info = {
        "status":     status,
        "ticker":     ticker,
        "updated_at": _now().strftime("%H:%M:%S"),
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
    ORDER_COOLDOWN_SEC = 90  # после ORDER_ERROR не трогать инструмент N секунд

    def __init__(self):
        self.status = "INIT"
        self.session_balance_start = 0.0
        self.session_balance_current = 0.0
        self.daily_pnl = Decimal("0")
        self.trades_today = 0
        self.open_positions = {}
        self.current_trade_date = str(date.today())
        self.instrument_meta = {}
        self.order_cooldowns: Dict[str, datetime] = {}   # figi → время последней ошибки ордера
        self.close_cooldowns: Dict[str, datetime] = {}   # figi → время закрытия (защита от немедленного реверса)

    def sync_runtime(self, last_error=""):
        set_runtime("status", self.status)
        set_runtime("daily_pnl", str(self.daily_pnl))
        set_runtime("trades_today", str(self.trades_today))
        set_runtime("last_error", last_error)
        set_runtime("session_balance_start", str(self.session_balance_start))
        set_runtime("session_balance_current", str(self.session_balance_current))
        set_runtime("current_trade_date", self.current_trade_date)
        set_runtime("api_rpm", str(_rate.rpm))
        set_runtime("api_rpm_limit", "600")
        import json as _j
        set_runtime("api_rpm_breakdown", _j.dumps(_rate.breakdown()))


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
                for resp in sc.orders_stream.trades_stream(accounts=[account_id]):
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

# Инструменты недоступные в текущей среде (sandbox NOT_FOUND) — пропускаем до рестарта
_unavailable_figis: Set[str] = set()

# ── Кеш дорогих API-вызовов (снижает нагрузку при множестве параллельных стратегий) ──
import collections as _collections

_instr_cache:   Dict[int,   tuple] = {}   # strategy_id → (ts, list)
_status_cache:  Dict[str,   tuple] = {}   # figi/uid    → (ts, status)
_tech_cache:    Dict[str,   tuple] = {}   # figi/uid    → (ts, dict)
_ob_cache:      Dict[str,   tuple] = {}   # figi/uid    → (ts, spread_pct)
_INSTR_TTL  = 60.0   # инструменты стратегии: обновляем раз в минуту
_STATUS_TTL = 30.0   # trading status: раз в 30 с
_TECH_TTL   = 90.0   # RSI/MACD/BB: раз в 90 с (индикаторы на 1-мин свечах)
_OB_TTL     = 15.0   # стакан REST: раз в 15 с (стрим обновляет чаще)

# ── Глобальный счётчик запросов к API T-Bank ─────────────────────────────────
class _ApiRateTracker:
    """Скользящее окно 60 с: считает реальные вызовы API по всем потокам."""
    def __init__(self, warn_threshold: int = 480, hard_limit: int = 570):
        self._ts: _collections.deque = _collections.deque()
        self._by_op: Dict[str, _collections.deque] = {}
        self._lock = Lock()
        self.warn  = warn_threshold
        self.limit = hard_limit

    def record(self, op: str = ""):
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
            self._ts.append(now)
            if op:
                if op not in self._by_op:
                    self._by_op[op] = _collections.deque()
                q = self._by_op[op]
                while q and q[0] < cutoff:
                    q.popleft()
                q.append(now)
            return len(self._ts)

    @property
    def rpm(self) -> int:
        now = time.monotonic()
        with self._lock:
            cutoff = now - 60.0
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
            return len(self._ts)

    def breakdown(self) -> list:
        """Топ операций по числу вызовов за последнюю минуту."""
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            result = []
            for op, q in self._by_op.items():
                while q and q[0] < cutoff:
                    q.popleft()
                if q:
                    result.append({"op": op, "rpm": len(q)})
            result.sort(key=lambda x: x["rpm"], reverse=True)
            return result

    def throttle_if_needed(self):
        if self.rpm >= self.limit:
            time.sleep(1.0)

_rate = _ApiRateTracker()
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


def get_last_price(client, figi: str, instrument_uid: str = "") -> Decimal:
    _rate.record("GetLastPrices")
    resp = client.market_data.get_last_prices(instrument_id=[instrument_uid or figi])
    return quotation_to_decimal(resp.last_prices[0].price)

def get_order_book_spread_pct(client, figi: str, instrument_uid: str = "") -> Decimal:
    instrument_id = instrument_uid or figi
    _now_ts = time.monotonic()
    _cached = _ob_cache.get(instrument_id)
    if _cached and (_now_ts - _cached[0]) < _OB_TTL:
        return _cached[1]
    try:
        _rate.record("GetOrderBook")
        book = client.market_data.get_order_book(instrument_id=instrument_id, depth=1)
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
        _ob_cache[instrument_id] = (_now_ts, spread_pct)
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
    """
    Проверяет, разрешена ли торговля в текущей сессии.
    Читает trade_only_session из настроек активного профиля (profile_settings),
    с откатом на bot_settings для обратной совместимости.
    """
    # Читаем из профиля если возможно
    active_pid_str = get_setting("active_profile_id", "").strip()
    if active_pid_str:
        trade_only_session = get_profile_setting(int(active_pid_str), "trade_only_session", "0") == "1"
    else:
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


def _get_actual_close_price(client, figi: str, instrument_uid: str, opened_at: str) -> Optional[float]:
    """
    Ищет фактическую цену закрытия позиции через операции брокера.
    Возвращает float или None если не найдено.
    """
    try:
        from_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        ops = client.operations.get_operations(
            account_id=settings.TINVEST_ACCOUNT_ID,
            from_=from_dt,
            to=datetime.now(timezone.utc),
            figi=figi,
        )
        # Берём последнюю операцию типа BROKER_REPORT (sell/buy) для этого figi
        for op in reversed(list(ops.operations)):
            op_type = str(getattr(op, "operation_type", "") or "")
            op_state = str(getattr(op, "state", "") or "")
            # Исполненные операции: OPERATION_TYPE_SELL (закрытие лонга) или OPERATION_TYPE_BUY (закрытие шорта)
            if "EXECUTED" not in op_state and "EXECUTED" not in str(getattr(op, "status", "")):
                continue
            price_obj = getattr(op, "price", None)
            if price_obj is None:
                continue
            price_val = float(quotation_to_decimal(price_obj))
            if price_val > 0:
                log.debug("Actual close price for %s: %.4f (op_type=%s)", figi[:8], price_val, op_type)
                return price_val
    except Exception as e:
        log.debug("_get_actual_close_price %s: %s", figi[:8], e)
    return None


def _handle_manual_close(bp: dict, market_map: dict, client=None):
    """
    Закрывает BOT-позицию, исчезнувшую из брокерского портфеля.
    Автоматически определяет причину: STOP_LOSS / TAKE_PROFIT / MANUAL_CLOSE.
    Фактическую цену выхода берёт из операций брокера (если доступно).
    """
    figi        = bp["figi"]
    ticker      = bp["ticker"]
    direction   = bp["direction"]
    entry_price = Decimal(str(bp["entry_price"]))
    qty         = int(bp["qty"])
    opened_at   = bp.get("opened_at", "")

    sl_tp  = get_instrument_sl_tp(figi)
    sl_pct = Decimal(str(sl_tp["sl_pct"]))
    tp_pct = Decimal(str(sl_tp["tp_pct"]))

    # 1. Пробуем получить фактическую цену закрытия из операций брокера
    actual_price = None
    instrument_uid = bp.get("instrument_uid", "") or ""
    if client is not None:
        actual_price = _get_actual_close_price(client, figi, instrument_uid, opened_at)

    # 2. Определяем причину закрытия и exit_price
    reason = "MANUAL_CLOSE"
    if actual_price is not None:
        exit_price = Decimal(str(actual_price))
        # Определяем причину по фактической цене vs теоретические SL/TP
        if direction == "BUY":
            sl_price = entry_price * (Decimal("1") - sl_pct) if sl_pct > 0 else None
            tp_price = entry_price * (Decimal("1") + tp_pct) if tp_pct > 0 else None
            if sl_price and exit_price <= sl_price * Decimal("1.005"):
                reason = "STOP_LOSS"
            elif tp_price and exit_price >= tp_price * Decimal("0.995"):
                reason = "TAKE_PROFIT"
        else:
            sl_price = entry_price * (Decimal("1") + sl_pct) if sl_pct > 0 else None
            tp_price = entry_price * (Decimal("1") - tp_pct) if tp_pct > 0 else None
            if sl_price and exit_price >= sl_price * Decimal("0.995"):
                reason = "STOP_LOSS"
            elif tp_price and exit_price <= tp_price * Decimal("1.005"):
                reason = "TAKE_PROFIT"
    else:
        # Брокерские операции недоступны — используем рыночную цену и теоретические SL/TP уровни
        mkt = market_map.get(figi, {})
        last_price = float(mkt.get("last_price", 0) or 0)
        market_price = Decimal(str(last_price)) if last_price > 0 else entry_price

        # Если рыночная цена близка к SL/TP — используем теоретическую цену как exit
        if sl_pct > 0 or tp_pct > 0:
            if direction == "BUY":
                sl_price = entry_price * (Decimal("1") - sl_pct) if sl_pct > 0 else None
                tp_price = entry_price * (Decimal("1") + tp_pct) if tp_pct > 0 else None
            else:
                sl_price = entry_price * (Decimal("1") + sl_pct) if sl_pct > 0 else None
                tp_price = entry_price * (Decimal("1") - tp_pct) if tp_pct > 0 else None

            # Проверяем теоретические уровни с допуском 1.5× SL — рынок мог отскочить
            sl_breach = sl_price and (
                (direction == "BUY"  and market_price <= sl_price * Decimal("1.015")) or
                (direction == "SELL" and market_price >= sl_price * Decimal("0.985"))
            )
            tp_breach = tp_price and (
                (direction == "BUY"  and market_price >= tp_price * Decimal("0.985")) or
                (direction == "SELL" and market_price <= tp_price * Decimal("1.015"))
            )
            if sl_breach:
                reason = "STOP_LOSS"
                exit_price = sl_price  # теоретическая цена
            elif tp_breach:
                reason = "TAKE_PROFIT"
                exit_price = tp_price  # теоретическая цена
            else:
                # Нативный стоп мог сработать пока рынок отскочил — используем SL цену как оценку
                # Признак: позиция ликвидирована, но текущая цена ~= entry (рынок вернулся)
                if sl_pct > 0 and sl_price:
                    move_from_entry = abs(float((market_price - entry_price) / entry_price))
                    if move_from_entry < float(sl_pct) * 0.5:
                        # Цена вернулась — скорее всего был SL, оцениваем по теоретическому уровню
                        reason = "STOP_LOSS"
                        exit_price = sl_price
                    else:
                        exit_price = market_price
                else:
                    exit_price = market_price
        else:
            exit_price = market_price

    commission_pct = Decimal(str(settings.ESTIMATED_COMMISSION_PCT))
    commission = (entry_price * qty + exit_price * qty) * commission_pct

    if direction == "BUY":
        pnl = (exit_price - entry_price) * qty - commission
    else:
        pnl = (entry_price - exit_price) * qty - commission

    reason_labels = {
        "STOP_LOSS":    "Стоп-лосс (нативный)",
        "TAKE_PROFIT":  "Тейк-профит (нативный)",
        "MANUAL_CLOSE": "Закрыта вручную",
    }
    reason_ui = reason_labels.get(reason, reason)
    pnl_sign = "+" if float(pnl) >= 0 else ""

    add_trade({
        "time":             _now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticker":           ticker,
        "figi":             figi,
        "direction":        direction,
        "entry":            float(entry_price),
        "exit":             float(exit_price),
        "qty":              qty,
        "gross_amount":     float(exit_price * qty),
        "commission":       float(commission),
        "pnl":              float(pnl),
        "reason":           reason,
        "close_order_id":   "",
        "execution_status": "exchange" if reason != "MANUAL_CLOSE" else "manual",
    })
    close_position(figi, source="BOT")
    log_event("ORDER_CLOSE", f"{ticker} {reason} exit={float(exit_price):.4f} pnl={float(pnl):.2f}", ticker=ticker)
    notify(f"{reason_ui}\n{ticker} | {direction}\nВход: {float(entry_price):.4f} → Выход: {float(exit_price):.4f}\nPnL ≈ {pnl_sign}{float(pnl):.2f} ₽")
    # Cooldown 60 сек после закрытия — защита от немедленного реверса
    state.close_cooldowns[figi] = _now()
    # Отменяем ВСЕ оставшиеся ордера по этому figi (второй стоп, зависшие лимитники)
    if client is not None:
        try:
            _cancel_all_orders_for_figi(client, figi, ticker)
        except Exception as _ce:
            log.warning("_cancel_all_orders_for_figi %s: %s", ticker, _ce)

    # Освобождаем координатор если он был заблокирован на этой позиции
    try:
        import json as _jc
        coord_data = _jc.loads(get_runtime("parallel_coord") or "{}")
        if coord_data.get("owner_figi") == figi:
            _parallel_coord.release(coord_data.get("owner_strategy_id"))
    except Exception:
        pass


def sync_portfolio_positions(client):
    """Синхронизирует позиции PORTFOLIO из API брокера в локальную БД (source=PORTFOLIO).
    Дополнительно детектирует BOT-позиции закрытые вручную и записывает их как MANUAL_CLOSE."""
    try:
        clear_open_positions(source="PORTFOLIO")

        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        positions = getattr(portfolio, "positions", []) or []

        # Карта figi→ticker из нашей БД (market_state) — надёжнее чем тикер от брокера
        _figi_to_ticker = {
            row["figi"]: row["ticker"]
            for row in get_instrument_market_state_map().values()
            if row.get("figi") and row.get("ticker")
        }

        broker_figis: set = set()
        for p in positions:
            figi = getattr(p, "figi", "") or ""
            instrument_type = str(getattr(p, "instrument_type", "") or "").lower()
            # Тикер из нашей БД по figi — брокер в Sandbox может вернуть неверный тикер
            ticker = _figi_to_ticker.get(figi) or getattr(p, "ticker", "") or figi[:8]

            if not figi or "currency" in instrument_type or ticker.upper().startswith("RUB"):
                continue
            broker_figis.add(figi)

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
                "opened_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "OPEN",
                "source": "PORTFOLIO",
            })
        # Детектируем BOT-позиции которых нет в реальном портфеле → закрыты вручную/SL/TP
        market_map = get_instrument_market_state_map()
        for bp in get_open_positions(source="BOT"):
            if bp["figi"] not in broker_figis:
                # Грейс-период 90 с: позиция могла ещё не появиться в портфеле брокера
                try:
                    opened_at_dt = datetime.strptime(bp.get("opened_at", ""), "%Y-%m-%d %H:%M:%S")
                    if (_now() - opened_at_dt).total_seconds() < 90:
                        continue
                except Exception:
                    pass
                log.info("Позиция %s не найдена в портфеле — определяем причину закрытия", bp["ticker"])
                _handle_manual_close(bp, market_map, client=client)

    except Exception as e:
        log.warning(f"sync_portfolio_positions error: {e}")

def get_candles(client, figi: str, n=20, instrument_uid: str = ""):
    _rate.record("GetCandles")
    now = datetime.now(timezone.utc)
    resp = client.market_data.get_candles(
        instrument_id=instrument_uid or figi,
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
    # Кеш на 30 сек: trading status меняется редко
    _now_ts = time.monotonic()
    _cached = _status_cache.get(instrument_id)
    if _cached and (_now_ts - _cached[0]) < _STATUS_TTL:
        return _cached[1]
    _rate.record("GetTradingStatus")
    try:
        resp = client.market_data.get_trading_status(instrument_id=instrument_id)
        status = getattr(resp, "trading_status", "UNKNOWN")
    except Exception as e:
        log.warning(f"Не удалось получить trading status по {instrument_id}: {e}")
        status = "UNKNOWN"
    _status_cache[instrument_id] = (_now_ts, status)
    return status


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
    """Возвращает ДОСТУПНЫЕ наличные (withdraw_limits.money) — то что брокер реально разрешит потратить.
    НЕ total_amount_portfolio — тот включает нереализованный PnL позиций и может вводить в заблуждение.
    """
    try:
        _rate.record("GetPortfolio")
        wl = client.operations.get_withdraw_limits(account_id=settings.TINVEST_ACCOUNT_ID)
        rub = next(
            (quotation_to_decimal(m) for m in getattr(wl, "money", [])
             if (getattr(m, "currency", "") or "").lower() in ("rub", "ruble", "")),
            None,
        )
        if rub is not None:
            return float(rub)
        # Fallback: total_amount_currencies из portfolio
        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        curr = getattr(portfolio, "total_amount_currencies", None)
        if curr:
            return float(quotation_to_decimal(curr))
    except Exception as e:
        log.warning(f"Не удалось получить баланс портфеля: {e}")
    return 0.0


def _fill_all_instrument_uids(client):
    """Запрашивает canonical ticker/name/uid для всех figi в БД и обновляет при расхождении."""
    from app.db import db_cursor as _dbc
    with _dbc() as cur:
        cur.execute("SELECT DISTINCT figi FROM strategy_instruments WHERE figi != ''")
        figis = [r["figi"] for r in cur.fetchall()]
    for figi in figis:
        try:
            resp = client.instruments.get_instrument_by(id_type=1, id=figi)
            inst = getattr(resp, "instrument", None)
            if not inst:
                continue
            uid          = getattr(inst, "uid", "") or ""
            api_ticker   = getattr(inst, "ticker", "") or ""
            api_name     = getattr(inst, "name", "") or ""
            # Всегда обновляем ticker/name/uid по canonical данным API
            if api_ticker:
                fix_ticker_by_figi(figi, api_ticker, api_name, uid)
                log.info("canonical: figi=%s ticker=%s uid=%s", figi, api_ticker, uid[:20])
        except Exception as e:
            msg = str(e)
            if "not found" in msg.lower() or "50002" in msg or "NOT_FOUND" in msg:
                _unavailable_figis.add(figi)
                log.warning("_fill_all_instrument_uids: %s недоступен в текущей среде", figi)
            else:
                log.warning("_fill_all_instrument_uids figi=%s: %s", figi, e)


def load_enabled_instruments(client):
    items = list_active_strategy_instruments()
    state.instrument_meta = {}

    for item in items:
        meta = get_instrument_meta(item["figi"])
        if not meta:
            log_event("INVALID_FIGI", f"Meta not loaded for figi={item['figi']}", ticker=item["ticker"], level="WARNING")
            continue

        # uid из БД или из API-ответа; сохраняем если не было
        uid = item.get("instrument_uid", "") or meta.get("uid", "")
        if uid and not item.get("instrument_uid"):
            save_instrument_uid(item["figi"], uid)

        state.instrument_meta[item["ticker"]] = {
            "figi": item["figi"],
            "instrument_uid": uid,
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


def place_order_checked(client, ticker: str, figi: str, lots: int, raw_price: Decimal, direction: OrderDirection,
                        market: bool = False):
    """market=True → рыночный ордер (для закрытия позиций, гарантирует исполнение)."""
    meta = state.instrument_meta.get(ticker)
    if not meta:
        log_event("ORDER_ERROR", "NO_META_SKIP", ticker=ticker, level="ERROR")
        return None

    step = meta["min_price_increment"]
    rounded_price = round_to_price_step(raw_price, step)
    q = quotation_from_decimal(rounded_price)
    request_order_id = str(uuid.uuid4())

    try:
        uid = meta.get("instrument_uid", "") or ""
        order_type = OrderType.ORDER_TYPE_MARKET if market else OrderType.ORDER_TYPE_LIMIT
        post_kwargs: dict = dict(
            instrument_id=uid or figi,
            quantity=lots,
            direction=direction,
            account_id=settings.TINVEST_ACCOUNT_ID,
            order_type=order_type,
            order_id=request_order_id,
        )
        if not market:
            post_kwargs["price"] = q
        resp = client.orders.post_order(**post_kwargs)

        response_order_id = getattr(resp, "order_id", request_order_id)
        log_event("ORDER_OPEN", f"post_order success request={request_order_id} response={response_order_id}", ticker=ticker)

        execution_report_status = "ORDER_STATE_PENDING"
        avg_price = rounded_price   # цена за штуку, уточняется из исполнения
        lots_executed = 0           # ← 0 по умолчанию, НЕ lots (чтобы не записать фиктивную позицию)

        def _accept_fill_price(ep: Decimal) -> bool:
            # sandbox возвращает цену за лот для некоторых инструментов — проверяем отклонение ≤5%
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
                    lots_executed = total_filled
            execution_report_status = "ORDER_STATE_FILL"
            log_event("ORDER_FILL", f"stream fill: {ticker} qty={lots_executed} price={avg_price}", ticker=ticker)
        except _queue.Empty:
            # Резервный вариант: опрос get_order_state (до 8 попыток × 1 с = 8 с)
            for _attempt in range(8):
                try:
                    time.sleep(1)
                    order_state = client.orders.get_order_state(
                        account_id=settings.TINVEST_ACCOUNT_ID,
                        order_id=response_order_id,
                    )
                    execution_report_status = str(getattr(order_state, "execution_report_status", "UNKNOWN"))
                    lots_executed_raw = getattr(order_state, "lots_executed", None)
                    if lots_executed_raw is not None:
                        lots_executed = int(lots_executed_raw)
                    executed_order_price = getattr(order_state, "executed_order_price", None)
                    if executed_order_price:
                        _ep = quotation_to_decimal(executed_order_price)
                        if _accept_fill_price(_ep):
                            avg_price = _ep

                    # Ордер исполнен (полностью или частично) — выходим
                    if "FILL" in execution_report_status:
                        log_event("ORDER_FILL",
                            f"poll fill: {ticker} qty={lots_executed} price={avg_price} status={execution_report_status}",
                            ticker=ticker)
                        break

                    # Ордер отменён или отклонён — нет смысла ждать дальше
                    if any(x in execution_report_status for x in ("CANCEL", "REJECT")):
                        log_event("ORDER_CANCEL",
                            f"{ticker} ордер {execution_report_status} qty_filled={lots_executed}",
                            ticker=ticker, level="WARNING")
                        break

                    # Ещё pending — продолжаем ждать
                except Exception as e:
                    msg = str(e)
                    if "50005" in msg or "Order not found" in msg:
                        continue
                    break

            # Если так и не заполнен — отменяем и возвращаем None
            if lots_executed == 0 and "FILL" not in execution_report_status:
                try:
                    client.orders.cancel_order(
                        account_id=settings.TINVEST_ACCOUNT_ID,
                        order_id=response_order_id,
                    )
                except Exception:
                    pass
                log_event("ORDER_CANCEL",
                    f"{ticker} ордер не исполнен ({execution_report_status}) — отменён",
                    ticker=ticker, level="WARNING")
                _pending_orders.pop(response_order_id, None)
                return None
        finally:
            _pending_orders.pop(response_order_id, None)

        # Финальная проверка: если лоты не заполнены — не создаём позицию
        if lots_executed == 0:
            log_event("ORDER_CANCEL",
                f"{ticker} ордер вернул lots_executed=0 — позиция не открыта",
                ticker=ticker, level="WARNING")
            return None

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
        return None


def _place_native_stops(client, ticker: str, figi: str, instrument_uid: str,
                        direction: str, qty: int, entry_price: Decimal,
                        sl_pct: Decimal, tp_pct: Decimal) -> dict:
    """
    Размещает нативные StopLoss и TakeProfit на бирже после открытия позиции.
    Возвращает dict с id ордеров: {"sl_id": ..., "tp_id": ...}
    Работает даже при зависании бота — ордера исполнятся на стороне биржи.
    """
    ids = {"sl_id": None, "tp_id": None}
    instr_id = instrument_uid or figi

    # Направления закрытия обратные входу
    if direction == "BUY":
        close_dir = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
        sl_price = round_to_price_step(entry_price * (Decimal("1") - sl_pct),
                                       state.instrument_meta.get(ticker, {}).get("min_price_increment", Decimal("0.01")))
        tp_price = round_to_price_step(entry_price * (Decimal("1") + tp_pct),
                                       state.instrument_meta.get(ticker, {}).get("min_price_increment", Decimal("0.01")))
    else:
        close_dir = StopOrderDirection.STOP_ORDER_DIRECTION_BUY
        sl_price = round_to_price_step(entry_price * (Decimal("1") + sl_pct),
                                       state.instrument_meta.get(ticker, {}).get("min_price_increment", Decimal("0.01")))
        tp_price = round_to_price_step(entry_price * (Decimal("1") - tp_pct),
                                       state.instrument_meta.get(ticker, {}).get("min_price_increment", Decimal("0.01")))

    def _q(price: Decimal) -> Quotation:
        units = int(price)
        nano = int((price - units) * Decimal("1000000000"))
        return Quotation(units=units, nano=nano)

    # StopLoss
    try:
        resp = client.stop_orders.post_stop_order(
            instrument_id=instr_id,
            quantity=qty,
            stop_price=_q(sl_price),
            direction=close_dir,
            account_id=settings.TINVEST_ACCOUNT_ID,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        )
        ids["sl_id"] = getattr(resp, "stop_order_id", None)
        log_event("STOP_ORDER", f"{ticker} SL native @ {sl_price} id={ids['sl_id']}", ticker=ticker)
    except Exception as e:
        log.warning("Не удалось разместить нативный SL для %s: %s", ticker, e)

    # TakeProfit
    try:
        resp = client.stop_orders.post_stop_order(
            instrument_id=instr_id,
            quantity=qty,
            stop_price=_q(tp_price),
            direction=close_dir,
            account_id=settings.TINVEST_ACCOUNT_ID,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        )
        ids["tp_id"] = getattr(resp, "stop_order_id", None)
        log_event("STOP_ORDER", f"{ticker} TP native @ {tp_price} id={ids['tp_id']}", ticker=ticker)
    except Exception as e:
        log.warning("Не удалось разместить нативный TP для %s: %s", ticker, e)

    return ids


def _cancel_native_stops(client, ticker: str, stop_ids: dict):
    """Отменяет нативные стоп-ордера при ручном закрытии позиции."""
    for key, sid in stop_ids.items():
        if not sid:
            continue
        try:
            client.stop_orders.cancel_stop_order(
                account_id=settings.TINVEST_ACCOUNT_ID,
                stop_order_id=sid,
            )
            log_event("STOP_ORDER", f"{ticker} cancel {key} id={sid}", ticker=ticker)
        except Exception as e:
            log.warning("Не удалось отменить стоп-ордер %s=%s: %s", key, sid, e)


def _cancel_all_orders_for_figi(client, figi: str, ticker: str = ""):
    """Отменяет ВСЕ активные ордера (обычные + стоп) для данного figi.
    Вызывается после закрытия позиции чтобы освободить заблокированные средства.
    """
    # Стоп-ордера
    try:
        resp = client.stop_orders.get_stop_orders(account_id=settings.TINVEST_ACCOUNT_ID)
        for s in getattr(resp, "stop_orders", []):
            if getattr(s, "figi", "") == figi or getattr(s, "instrument_uid", "") == figi:
                sid = getattr(s, "stop_order_id", None)
                if not sid:
                    continue
                try:
                    client.stop_orders.cancel_stop_order(
                        account_id=settings.TINVEST_ACCOUNT_ID,
                        stop_order_id=sid,
                    )
                    log_event("STOP_ORDER", f"{ticker or figi[:8]} auto-cancel stop {sid[:8]}", ticker=ticker)
                except Exception as e:
                    log.warning("cancel stop %s: %s", sid[:8], e)
    except Exception as e:
        log.warning("get_stop_orders in _cancel_all: %s", e)

    # Лимитные / рыночные ордера в очереди
    try:
        resp = client.orders.get_orders(account_id=settings.TINVEST_ACCOUNT_ID)
        for o in getattr(resp, "orders", []):
            if getattr(o, "figi", "") == figi or getattr(o, "instrument_uid", "") == figi:
                oid = getattr(o, "order_id", None)
                if not oid:
                    continue
                try:
                    client.orders.cancel_order(
                        account_id=settings.TINVEST_ACCOUNT_ID,
                        order_id=oid,
                    )
                    log_event("ORDER", f"{ticker or figi[:8]} auto-cancel order {oid[:8]}", ticker=ticker)
                except Exception as e:
                    log.warning("cancel order %s: %s", oid[:8], e)
    except Exception as e:
        log.warning("get_orders in _cancel_all: %s", e)


_CANDLE_TO_INDICATOR_INTERVAL = {
    CandleInterval.CANDLE_INTERVAL_1_MIN:  IndicatorInterval.INDICATOR_INTERVAL_ONE_MINUTE,
    CandleInterval.CANDLE_INTERVAL_5_MIN:  IndicatorInterval.INDICATOR_INTERVAL_FIVE_MINUTES,
    CandleInterval.CANDLE_INTERVAL_15_MIN: IndicatorInterval.INDICATOR_INTERVAL_FIFTEEN_MINUTES,
    CandleInterval.CANDLE_INTERVAL_HOUR:   IndicatorInterval.INDICATOR_INTERVAL_ONE_HOUR,
    CandleInterval.CANDLE_INTERVAL_DAY:    IndicatorInterval.INDICATOR_INTERVAL_ONE_DAY,
}


def get_api_indicators(client, instrument_uid: str, figi: str = "") -> dict:
    """
    Получает RSI(14), MACD(12/26/9) и BB(20, 2σ) из T-Bank API.
    Кеш 90 сек: индикаторы на 1-мин свечах не меняются быстрее.
    """
    instr_id = instrument_uid or figi
    _now_ts = time.monotonic()
    _cached = _tech_cache.get(instr_id)
    if _cached and (_now_ts - _cached[0]) < _TECH_TTL:
        return _cached[1]

    now        = datetime.now(timezone.utc)
    from_      = now - timedelta(hours=3)
    interval   = IndicatorInterval.INDICATOR_INTERVAL_ONE_MINUTE
    price_type = TypeOfPrice.TYPE_OF_PRICE_CLOSE
    result: dict = {}

    # RSI — значение в поле signal
    try:
        _rate.record("GetTechAnalysis/RSI")
        resp = client.market_data.get_tech_analysis(request=GetTechAnalysisRequest(
            indicator_type=IndicatorType.INDICATOR_TYPE_RSI,
            instrument_uid=instr_id,
            from_=from_, to=now,
            interval=interval,
            type_of_price=price_type,
            length=14,
        ))
        if resp.technical_indicators:
            v = resp.technical_indicators[-1].signal
            result["rsi"] = float(quotation_to_decimal(v)) if v else None
    except Exception as e:
        log.debug("GetTechAnalysis RSI %s: %s", instr_id[:8], e)

    # MACD — macd=линия MACD, signal=сигнальная линия
    try:
        _rate.record("GetTechAnalysis/MACD")
        resp = client.market_data.get_tech_analysis(request=GetTechAnalysisRequest(
            indicator_type=IndicatorType.INDICATOR_TYPE_MACD,
            instrument_uid=instr_id,
            from_=from_, to=now,
            interval=interval,
            type_of_price=price_type,
            smoothing=Smoothing(fast_length=12, slow_length=26, signal_smoothing=9),
        ))
        if resp.technical_indicators:
            last = resp.technical_indicators[-1]
            result["macd"] = float(quotation_to_decimal(last.macd)) if last.macd else None
            result["macd_signal"] = float(quotation_to_decimal(last.signal)) if last.signal else None
    except Exception as e:
        log.debug("GetTechAnalysis MACD %s: %s", instr_id[:8], e)

    # BB — upper/middle/lower bands
    try:
        _rate.record("GetTechAnalysis/BB")
        resp = client.market_data.get_tech_analysis(request=GetTechAnalysisRequest(
            indicator_type=IndicatorType.INDICATOR_TYPE_BB,
            instrument_uid=instr_id,
            from_=from_, to=now,
            interval=interval,
            type_of_price=price_type,
            length=20,
            deviation=Deviation(deviation_multiplier=Quotation(units=2, nano=0)),
        ))
        if resp.technical_indicators:
            last = resp.technical_indicators[-1]
            result["bb_upper"]  = float(quotation_to_decimal(last.upper_band))  if last.upper_band  else None
            result["bb_middle"] = float(quotation_to_decimal(last.middle_band)) if last.middle_band else None
            result["bb_lower"]  = float(quotation_to_decimal(last.lower_band))  if last.lower_band  else None
    except Exception as e:
        log.debug("GetTechAnalysis BB %s: %s", instr_id[:8], e)

    _tech_cache[instr_id] = (time.monotonic(), result)
    return result


def _apply_indicator_filter(sig: str, indicators: dict, mode: str, price: float) -> str:
    """
    Фильтрует сигнал через API-индикаторы. Возвращает HOLD если не подтверждён.

    BUY: RSI < 70, MACD выше сигнальной, для mean_reversion цена ≤ BB_lower.
    SELL: RSI > 30, MACD ниже сигнальной, для mean_reversion цена ≥ BB_upper.
    """
    rsi         = indicators.get("rsi")
    macd        = indicators.get("macd")
    macd_signal = indicators.get("macd_signal")
    bb_upper    = indicators.get("bb_upper")
    bb_lower    = indicators.get("bb_lower")

    if sig == "BUY":
        if rsi is not None and rsi > 70:
            log.debug("API-filter BUY HOLD: RSI=%.1f > 70 (перекуплен)", rsi)
            return "HOLD"
        if macd is not None and macd_signal is not None and macd < macd_signal:
            log.debug("API-filter BUY HOLD: MACD=%.5f < Signal=%.5f", macd, macd_signal)
            return "HOLD"
        if mode in ("mean", "mean_reversion", "revert") and bb_lower is not None:
            if price > bb_lower * 1.005:
                log.debug("API-filter BUY HOLD: price=%.2f > BB_lower=%.2f * 1.005", price, bb_lower)
                return "HOLD"
    elif sig == "SELL":
        if rsi is not None and rsi < 30:
            log.debug("API-filter SELL HOLD: RSI=%.1f < 30 (перепродан)", rsi)
            return "HOLD"
        if macd is not None and macd_signal is not None and macd > macd_signal:
            log.debug("API-filter SELL HOLD: MACD=%.5f > Signal=%.5f", macd, macd_signal)
            return "HOLD"
        if mode in ("mean", "mean_reversion", "revert") and bb_upper is not None:
            if price < bb_upper * 0.995:
                log.debug("API-filter SELL HOLD: price=%.2f < BB_upper=%.2f * 0.995", price, bb_upper)
                return "HOLD"
    return sig


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

    # Инструмент ранее вернул NOT_FOUND — пропускаем без API-вызовов
    if figi in _unavailable_figis:
        return
    lot = item["lots_override"]
    stop_loss_pct = item["stop_loss_pct"]
    take_profit_pct = item["take_profit_pct"]
    allow_long = int(item.get("allow_long", 1))
    allow_short = int(item.get("allow_short", 1))

    # Параллельный режим: place_order_checked ищет мету в state.instrument_meta.
    # Если инструмент там не зарегистрирован (он не из основной стратегии) — добавляем.
    if ticker not in state.instrument_meta:
        state.instrument_meta[ticker] = {
            "figi":              item["figi"],
            "instrument_uid":    item.get("instrument_uid", "") or "",
            "ticker":            ticker,
            "lot":               item.get("lot", 1),
            "name":              item.get("name", ""),
            "class_code":        item.get("class_code", ""),
            "instrument_type":   item.get("instrument_type", ""),
            "currency":          item.get("currency", ""),
            "min_price_increment": item["min_price_increment"],
            "lots_override":     int(item.get("lots_override", 1)),
            "stop_loss_pct":     item["stop_loss_pct"],
            "take_profit_pct":   item["take_profit_pct"],
            "max_spread_pct":    item.get("max_spread_pct", Decimal("0")),
            "min_volume":        int(item.get("min_volume", 0)),
            "allow_long":        int(item.get("allow_long", 1)),
            "allow_short":       int(item.get("allow_short", 1)),
            "priority":          int(item.get("priority", 100)),
        }

    allow_long_global = get_setting("allow_long_global", "1") == "1"
    allow_short_global = get_setting("allow_short_global", "1") == "1"

    tradingmode           = _cfg("tradingmode", "trend")
    trailing_stop_enabled = _cfg("trailing_stop_enabled", "0") == "1"
    use_signal_service    = _cfg("use_signal_service", "0") == "1"
    use_api_confirm         = _cfg("use_api_confirm", "0") in ("1", "true", "yes")
    use_order_book_filter   = _cfg("use_order_book_filter", "1") in ("1", "true", "yes")

    instrument_uid = item.get("instrument_uid", "") or ""
    import json as _j

    # Сохраняет skip_reason из последнего известного сигнала — вызывается до вычисления сигнала
    def _save_skip(skip_reason: str, skip_filter: str = ""):
        try:
            _prev = {}
            try:
                _prev = _j.loads(get_runtime(f"last_signal_{figi}") or "{}")
            except Exception:
                pass
            set_runtime(f"last_signal_{figi}", _j.dumps({
                "action": _prev.get("action", "HOLD"),
                "score": _prev.get("score", 0),
                "mode": tradingmode,
                "time": _now().strftime("%H:%M:%S"),
                "skip_reason": skip_reason,
                "skip_filter": skip_filter,
            }))
        except Exception:
            pass

    try:
        # ── Цена: стрим (быстро) → REST резервный вариант ────────────────────
        with _md_lock:
            stream_md = _md.get(figi, {})
        stream_price = stream_md.get("price")
        price = stream_price if (stream_price and stream_price > 0) else get_last_price(client, figi, instrument_uid=instrument_uid)

        # ── Свечи: 50 баров достаточно для всех режимов strategy_engine ──────
        candle_n = 50
        candles = get_candles(client, figi, n=candle_n, instrument_uid=instrument_uid)
        last_volume = get_last_candle_volume(candles)

        # ── Стакан: стрим depth=10 → REST depth=1 резервный вариант ─────────
        ob_stream = stream_md.get("orderbook", {})
        if ob_stream:
            spread_pct = ob_stream["spread_pct"]
            bid_vol: int = ob_stream["bid_vol"]
            ask_vol: int = ob_stream["ask_vol"]
        else:
            spread_pct = get_order_book_spread_pct(client, figi, instrument_uid=instrument_uid)
            bid_vol = ask_vol = 0

        # Сохраняем цену всегда — до любых дальнейших проверок
        upsert_instrument_market_state(
            figi=figi,
            ticker=ticker,
            last_price=price,
            price_time=_now().strftime("%Y-%m-%d %H:%M:%S"),
            volume_1m=last_volume,
        )
    except Exception as e:
        msg = str(e)
        is_not_found = "not found" in msg.lower() or "50002" in msg or "NOT_FOUND" in msg
        is_rate_limited = "resource exhausted" in msg.lower() or "RESOURCE_EXHAUSTED" in msg
        if is_rate_limited:
            _save_skip("Лимит запросов API — пропуск цикла", "rate_limit")
            time.sleep(1.0)
            return
        if "figi" in msg.lower() or is_not_found:
            if is_not_found:
                _unavailable_figis.add(figi)
                log.warning("Инструмент %s недоступен в текущей среде (NOT_FOUND) — пропускаем", ticker)
            else:
                log_event("INVALID_FIGI", msg, ticker=ticker, level="WARNING")
            return
        raise

    if len(candles) < 5:
        _save_skip(f"Нет данных свечей ({len(candles)} баров)", "no_candles")
        return

    # Вычисляем сигнал всегда — до всех проверок, чтобы дашборд показывал актуальный сигнал
    candles_dict = _candles_to_dicts(candles)
    sig_result = _evaluate_signal(tradingmode, candles_dict)
    sig   = sig_result["action"]
    score = sig_result["score"]

    def _save_signal(action=None, skip_reason="", skip_filter=""):
        try:
            set_runtime(f"last_signal_{figi}", _j.dumps({
                "action": action if action is not None else sig,
                "score": score,
                "mode": tradingmode,
                "time": _now().strftime("%H:%M:%S"),
                "skip_reason": skip_reason,
                "skip_filter": skip_filter,
            }))
        except Exception:
            pass

    _save_signal()  # начальное сохранение без причины пропуска

    # Cooldown после ORDER_ERROR
    _cd = state.order_cooldowns.get(figi)
    if _cd and (_now() - _cd).total_seconds() < BotState.ORDER_COOLDOWN_SEC:
        remaining = int(BotState.ORDER_COOLDOWN_SEC - (_now() - _cd).total_seconds())
        _save_signal(skip_reason=f"Пауза после ошибки ордера (осталось {remaining}с)", skip_filter="cooldown")
        return

    # Session и tradable-проверки
    if not is_session_allowed(client, figi):
        _save_signal(skip_reason="Торговая сессия закрыта", skip_filter="session")
        return

    try:
        trading_status = get_trading_status(client, figi)
    except Exception as _tse:
        _te_msg = str(_tse)
        if "resource exhausted" in _te_msg.lower() or "RESOURCE_EXHAUSTED" in _te_msg:
            _save_skip("Лимит запросов API (trading status)", "rate_limit")
            time.sleep(1.0)
        return
    if not is_tradable(trading_status):
        _save_signal(skip_reason=f"Торговля недоступна: {str(trading_status)[-30:]}", skip_filter="trading_status")
        return

    last_volume = get_last_candle_volume(candles)
    min_volume = int(item.get("min_volume", 0))
    if min_volume > 0 and last_volume < min_volume:
        return

    max_spread_pct = Decimal(str(item.get("max_spread_pct", "0")))
    if max_spread_pct > 0 and spread_pct > max_spread_pct:
        return

    # Блок А: подтверждение сигнала через API-индикаторы (RSI/MACD/BB)
    # Запускаем только когда есть потенциальный сигнал входа — чтобы не делать 3 лишних API-вызова на каждом HOLD
    if use_api_confirm and sig in ("BUY", "SELL") and ticker not in positions:
        try:
            _ind = get_api_indicators(client, instrument_uid, figi)
        except Exception as _aie:
            if "resource exhausted" in str(_aie).lower() or "RESOURCE_EXHAUSTED" in str(_aie):
                _save_skip("Лимит запросов API (индикаторы)", "rate_limit")
                time.sleep(1.0)
            return
        sig_before = sig
        sig = _apply_indicator_filter(sig, _ind, tradingmode, float(price))
        rsi_str  = f"RSI={_ind.get('rsi'):.1f}" if _ind.get("rsi") is not None else "RSI=n/a"
        macd_str = (f"MACD={_ind.get('macd'):.4f}/Сигн={_ind.get('macd_signal'):.4f}"
                    if _ind.get("macd") is not None else "MACD=n/a")
        bb_str   = (f"BB={_ind.get('bb_lower'):.2f}..{_ind.get('bb_upper'):.2f}"
                    if _ind.get("bb_upper") is not None else "BB=n/a")
        log.info("API-confirm %s %s→%s | %s | %s | %s",
                 ticker, sig_before, sig, rsi_str, macd_str, bb_str)
        if sig == "HOLD" and sig_before != "HOLD":
            _save_signal(skip_reason=f"API-фильтр: {rsi_str} | {macd_str}", skip_filter="api_confirm")

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
            # Отменяем ВСЕ ордера по figi перед закрытием (нативные стопы + лимитники)
            _cancel_all_orders_for_figi(client, figi, ticker)
            order_result = place_order_checked(client, ticker, figi, qty, price, close_dir, market=True)
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
                "time": _now().strftime("%Y-%m-%d %H:%M:%S"),
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
            # Cooldown 60 сек после закрытия — защита от немедленного реверса
            state.close_cooldowns[figi] = _now()
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

    # Защита от реверса: после закрытия позиции не входим снова 60 сек
    _cc = state.close_cooldowns.get(figi)
    if _cc and (_now() - _cc).total_seconds() < 60:
        _save_signal(skip_reason="Пауза после закрытия позиции (60с)", skip_filter="close_cooldown")
        return

    # ── Торговое решение по сигналу (вычислен выше, до проверок сессии) ────────
    if sig == "HOLD":
        return

    min_score = int(_cfg("min_signal_score", "0"))
    if min_score > 0 and score < min_score:
        log_event("SIGNAL_SKIP", f"{ticker}: score={score} < порог={min_score} [{tradingmode}], пропуск", ticker=ticker)
        _save_signal(skip_reason=f"Качество сигнала {score} < порог {min_score}", skip_filter="min_score")
        return

    log_event("SIGNAL", f"{sig} [{tradingmode}] score={score} on {ticker}", ticker=ticker)

    # ── Фильтр сервиса сигналов T-Bank ───────────────────────────────────────
    if use_signal_service:
        from app.services.tbank_client import get_tbank_signals
        analyst = get_tbank_signals([figi]).get(figi, "NEUTRAL")
        if sig == "BUY" and analyst == "SELL":
            log_event("SIGNAL_SKIP", f"{ticker} BUY пропущен: аналитики SELL", ticker=ticker)
            _save_signal(skip_reason="Аналитики T-Bank: SELL против BUY", skip_filter="signal_service")
            return
        if sig == "SELL" and analyst == "BUY":
            log_event("SIGNAL_SKIP", f"{ticker} SELL пропущен: аналитики BUY", ticker=ticker)
            _save_signal(skip_reason="Аналитики T-Bank: BUY против SELL", skip_filter="signal_service")
            return

    # ── Проверка баланса перед ордером ───────────────────────────────────────
    if sig == "BUY":
        lot_size = item.get("lot", 1)
        commission_pct = Decimal(_cfg("estimated_commission_pct", "0.0004"))
        required = Decimal(str(lot)) * Decimal(str(lot_size)) * price * (1 + commission_pct)
        available = Decimal(str(state.session_balance_current))
        if available < required:
            _reason = f"Недостаточно средств: нужно {float(required):.0f} ₽, доступно {float(available):.0f} ₽"
            log_event("BALANCE_WARNING",
                      f"{ticker}: {_reason} — пропуск", ticker=ticker)
            _save_signal(skip_reason=_reason, skip_filter="balance")
            return

    # ── Фильтр давления стакана (только при наличии данных стрима) ───────────
    total_vol = bid_vol + ask_vol
    if use_order_book_filter and total_vol > 0:
        buy_pressure = bid_vol / total_vol   # доля объёма на стороне бид
        sell_pressure = ask_vol / total_vol
        if sig == "BUY" and buy_pressure < 0.40:
            log_event("SIGNAL_SKIP", f"{ticker} BUY пропущен: давление покупателей {buy_pressure:.0%}", ticker=ticker)
            _save_signal(skip_reason=f"Давление покупателей {buy_pressure:.0%} < 40%", skip_filter="order_book")
            return
        if sig == "SELL" and sell_pressure < 0.40:
            log_event("SIGNAL_SKIP", f"{ticker} SELL пропущен: давление продавцов {sell_pressure:.0%}", ticker=ticker)
            _save_signal(skip_reason=f"Давление продавцов {sell_pressure:.0%} < 40%", skip_filter="order_book")
            return

    if sig == "BUY":
        if not allow_long_global or not allow_long:
            return
        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return  # другой поток только что захватил первым

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_BUY)
        if not order_result:
            state.order_cooldowns[figi] = _now()
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        _qty_filled = int(order_result["lots_executed"])
        # Нативные стоп-ордера на бирже — сработают даже при зависании бота
        _stop_ids = _place_native_stops(
            client, ticker, figi, instrument_uid, "BUY", _qty_filled,
            Decimal(str(_ep)), stop_loss_pct, take_profit_pct,
        )
        positions[ticker] = {
            "figi": figi,
            "direction": "BUY",
            "entry_price": _ep,
            "qty": _qty_filled,
            "opened_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
            "trailing_stop": _ep * (1 - float(stop_loss_pct)),
            "native_stop_ids": _stop_ids,
        }

        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": "BUY",
            "qty": _qty_filled,
            "entry_price": float(order_result["executed_price"]),
            "current_price": float(order_result["executed_price"]),
            "unrealized_pnl": 0,
            "opened_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            "source": "BOT",
        })

        sl_ui = f"{float(stop_loss_pct)*100:.2f}%"
        tp_ui = f"{float(take_profit_pct)*100:.2f}%"
        notify(f"🟢 Открытие позиции\n{ticker} | BUY\nЦена: {order_result['executed_price']}\nSL: {sl_ui} | TP: {tp_ui}\n{'✅ Стопы на бирже' if _stop_ids.get('sl_id') else '⚠️ Стопы не размещены'}")

    elif sig == "SELL":
        if not allow_short_global or not allow_short:
            return
        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return

        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_SELL)
        if not order_result:
            state.order_cooldowns[figi] = _now()
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        _qty_filled = int(order_result["lots_executed"])
        _stop_ids = _place_native_stops(
            client, ticker, figi, instrument_uid, "SELL", _qty_filled,
            Decimal(str(_ep)), stop_loss_pct, take_profit_pct,
        )
        positions[ticker] = {
            "figi": figi,
            "direction": "SELL",
            "entry_price": _ep,
            "qty": _qty_filled,
            "opened_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_order_id": order_result["response_order_id"],
            "execution_status": order_result["execution_status"],
            "trailing_stop": _ep * (1 + float(stop_loss_pct)),
            "native_stop_ids": _stop_ids,
        }

        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": "SELL",
            "qty": _qty_filled,
            "entry_price": float(order_result["executed_price"]),
            "current_price": float(order_result["executed_price"]),
            "unrealized_pnl": 0,
            "opened_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            "source": "BOT",
        })

        sl_ui = f"{float(stop_loss_pct)*100:.2f}%"
        tp_ui = f"{float(take_profit_pct)*100:.2f}%"
        notify(f"🔴 Открытие позиции\n{ticker} | SELL\nЦена: {order_result['executed_price']}\nSL: {sl_ui} | TP: {tp_ui}\n{'✅ Стопы на бирже' if _stop_ids.get('sl_id') else '⚠️ Стопы не размещены'}")

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

    # Jitter при старте: каждый воркер ждёт случайно 0–30 с,
    # чтобы 20+ потоков не атаковали API одновременно
    import random as _random
    _jitter = _random.uniform(0, 30)
    log.info("Parallel [%s] jitter %.1f с", strat_name, _jitter)
    stop_ev.wait(timeout=_jitter)
    if stop_ev.is_set():
        return

    # Восстанавливаем позиции из БД при старте (на случай перезапуска бота)
    positions: Dict = {}
    try:
        _instr_figis = {i["figi"] for i in _parallel_instruments(strategy_id)}
        for _bp in get_open_positions(source="BOT"):
            if _bp["figi"] in _instr_figis:
                _tk = _bp["ticker"]
                positions[_tk] = {
                    "figi":       _bp["figi"],
                    "direction":  _bp["direction"],
                    "entry_price": float(_bp["entry_price"]),
                    "qty":        int(_bp["qty"]),
                    "opened_at":  _bp.get("opened_at", ""),
                    "trailing_stop": 0.0,
                }
                # Восстанавливаем координатор если он был свободен
                if _parallel_coord.is_free:
                    _parallel_coord.try_claim(strategy_id, _bp["figi"], _tk)
                log.info("Parallel [%s] восстановлена позиция %s из БД", strat_name, _tk)
    except Exception as _e:
        log.warning("Parallel [%s] ошибка восстановления позиций: %s", strat_name, _e)

    while not stop_ev.is_set():
        try:
            # Перезагружаем настройки каждый цикл (отражает изменения в UI)
            cfg   = {row["key"]: row["value"]
                     for row in _parallel_cfg_rows(strategy_id)}
            instr = _parallel_instruments(strategy_id)

            interval = int(cfg.get("check_interval_sec", "5"))

            # Синхронизируем in-memory dict с БД: если позиция исчезла (закрыта вручную
            # или через sync_portfolio_positions) — забываем её и освобождаем координатор
            if positions:
                db_bot_figis = {p["figi"] for p in get_open_positions(source="BOT")}
                for tk in list(positions.keys()):
                    if positions[tk].get("figi", "") not in db_bot_figis:
                        log.info("Parallel [%s] %s: позиция закрыта снаружи, сбрасываем", strat_name, tk)
                        del positions[tk]
                        _parallel_coord.release(strategy_id)

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
                    _rate.throttle_if_needed()  # пауза если близко к rate limit
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
            exc_str = str(exc)
            log.warning("Parallel [%s] цикл: %s", strat_name, exc)
            _pset(strategy_id, f"ошибка: {exc}")
            # При rate-limit — дополнительная пауза чтобы не долбить API
            if "RESOURCE_EXHAUSTED" in exc_str or "resource exhausted" in exc_str.lower():
                stop_ev.wait(timeout=15)

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
    # Кеш на 60 с: список инструментов не меняется каждые 5 сек
    _now_ts = time.monotonic()
    _cached = _instr_cache.get(strategy_id)
    if _cached and (_now_ts - _cached[0]) < _INSTR_TTL:
        return _cached[1]

    from app.db import list_strategy_instruments
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
    _instr_cache[strategy_id] = (time.monotonic(), result)
    return result


def _refresh_parallel_workers():
    """
    Сравнивает текущую конфигурацию параллельных стратегий в БД с запущенными потоками.
    При изменении конфигурации или гибели потоков — перезапускает воркеры.
    Вызывается из основного цикла раз в ~30 секунд.
    """
    global _parallel_threads, _parallel_main_stop_ev, _last_parallel_sig

    active_profile_id = get_setting("active_profile_id", "").strip()
    if not active_profile_id:
        new_sig: tuple = ()
    else:
        pid = int(active_profile_id)
        parallel_on = get_profile_setting(pid, "parallel_trading_enabled", "0")
        if parallel_on == "1":
            entries = list_profile_parallel_strategies(pid)
            new_sig = (pid, "1", tuple(sorted(e["strategy_id"] for e in entries)))
        else:
            new_sig = (pid, "0")

    alive = [t for t in _parallel_threads if t.is_alive()]

    if new_sig == _last_parallel_sig and len(alive) == len(_parallel_threads):
        return  # Конфигурация и потоки не изменились

    # Конфигурация изменилась или потоки умерли — останавливаем старые
    if new_sig != _last_parallel_sig and _parallel_main_stop_ev is not None:
        log.info("Parallel config changed — останавливаем старые воркеры")
        _parallel_main_stop_ev.set()
        _parallel_threads = []

    _last_parallel_sig = new_sig

    # Запускаем воркеры если параллельный режим включён и потоков нет
    if new_sig and len(new_sig) >= 2 and new_sig[1] == "1" and not _parallel_threads:
        _parallel_main_stop_ev = threading.Event()
        _parallel_threads = start_parallel_workers(_parallel_main_stop_ev)
        if _parallel_threads:
            log.info("Parallel workers (пере)запущены: %d потоков", len(_parallel_threads))


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

    # Сбрасываем устаревшие данные координатора и потоков из предыдущей сессии
    import json as _jstart
    set_runtime("parallel_coord", _jstart.dumps(
        {"owner_strategy_id": None, "owner_figi": None, "owner_ticker": None}
    ))
    # Статусы потоков тоже сбрасываем — старые значения вводят в заблуждение
    try:
        from app.db import list_profile_parallel_strategies, get_setting as _gs
        _pid_str = _gs("active_profile_id", "").strip()
        if _pid_str:
            for _e in list_profile_parallel_strategies(int(_pid_str)):
                set_runtime(f"parallel_thread_{_e['strategy_id']}", _jstart.dumps(
                    {"status": "запуск", "ticker": "", "updated_at": ""}
                ))
    except Exception:
        pass

    if settings.TELEGRAM_ENABLED and settings.TELEGRAM_POLLING_ENABLED:
        Thread(target=run_telegram_polling, daemon=True).start()

    start_weekly_scheduler(notifier=notifier)

    state.status = "PRECHECK"
    state.sync_runtime()

    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client

    # Заполняем instrument_uid для всех инструментов при старте (однократно)
    try:
        with client_cls(settings.TINVEST_TOKEN) as _uid_client:
            _fill_all_instrument_uids(_uid_client)
    except Exception as _e:
        log.warning("_fill_all_instrument_uids error: %s", _e)

    global _bot_client_cls
    _bot_client_cls = client_cls
    Thread(target=_orders_stream_worker, daemon=True, name="orders-stream").start()
    Thread(target=_market_data_stream_worker, daemon=True, name="market-data-stream").start()
    log.info("OrdersStream + MarketDataStream workers started")

    _refresh_parallel_workers()
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

                # Периодически проверяем и перезапускаем параллельные воркеры
                global _last_parallel_check_ts
                now_ts = time.time()
                if now_ts - _last_parallel_check_ts > 30:
                    _last_parallel_check_ts = now_ts
                    _refresh_parallel_workers()

                watchlist = load_enabled_instruments(client)

                for item in watchlist:
                    _rate.throttle_if_needed()
                    process_instrument(client, item)

                sync_portfolio_positions(client)

                state.session_balance_current = get_money_balance(client)
                set_runtime("session_balance", str(state.session_balance_current))
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