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
    get_max_trades_per_day,
    get_max_daily_loss_rub,
    get_instrument_market_state_map,
    save_instrument_uid,
    fix_ticker_by_figi,
    get_instrument_sl_tp,
    get_recent_trade,
    get_ticker_daily_pnl,
    save_position_feature_id,
    get_position_feature_id,
    update_position_price,
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
# T-Bank Invest SDK логирует каждый gRPC-вызов на уровне INFO — подавляем
logging.getLogger("t_tech").setLevel(logging.WARNING)
logging.getLogger("t_tech.invest").setLevel(logging.WARNING)

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
    Координатор параллельных стратегий.
    Позволяет открывать несколько позиций одновременно, ограниченных:
      - max_open_positions (настройка бота)
      - доступным балансом (проверяется в process_instrument)

    Протокол:
      1. try_claim(sid, figi) → True если этот поток ещё не занят И не превышен лимит.
      2. release(sid) → освобождает слот после закрытия позиции.
      3. is_owner(sid) → True если этот поток держит позицию.
      4. is_free → True если нет ни одной открытой позиции.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._owners: Dict[int, str] = {}   # strategy_id → figi

    @property
    def is_free(self) -> bool:
        with self._lock:
            return len(self._owners) == 0

    def try_claim(self, sid: int, figi: str, ticker: str = "") -> bool:
        with self._lock:
            if sid in self._owners:
                return False  # этот поток уже держит позицию
            if figi in self._owners.values():
                return False  # по инструменту уже есть позиция другой стратегии: брокер ведёт одну нетто-позицию на бумагу
            max_pos = get_max_open_positions()
            if len(self._owners) >= max_pos:
                return False  # достигнут лимит позиций
            self._owners[sid] = figi
            self._persist()
            return True

    def adopt(self, sid: int, figi: str) -> bool:
        """Принять уже существующую (восстановленную из БД) позицию без проверки лимита.
        False — позицию по этому figi уже ведёт другая стратегия (одну позицию нельзя считать дважды)."""
        with self._lock:
            if sid in self._owners:
                return self._owners[sid] == figi
            if figi in self._owners.values():
                return False
            self._owners[sid] = figi
            self._persist()
            return True

    def release(self, sid: int) -> bool:
        with self._lock:
            if sid in self._owners:
                del self._owners[sid]
                self._persist()
                return True
            return False

    def _persist(self):
        try:
            import json as _j
            set_runtime("parallel_coord", _j.dumps({
                "owners": self._owners,
                # Для обратной совместимости с _handle_manual_close
                "owner_strategy_id": next(iter(self._owners), None),
                "owner_figi":        next(iter(self._owners.values()), None),
            }))
        except Exception:
            pass

    def is_owner(self, sid: int) -> bool:
        with self._lock:
            return sid in self._owners

    def owner_figi(self, sid) -> Optional[str]:
        """figi, которым владеет стратегия sid (None — не владеет)."""
        with self._lock:
            return self._owners.get(sid)

    def owned_by_other(self, figi: str, sid) -> bool:
        """True, если позицию по figi ведёт другая стратегия, не sid."""
        with self._lock:
            return any(s != sid and f == figi for s, f in self._owners.items())

    @property
    def owner_count(self) -> int:
        with self._lock:
            return len(self._owners)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "owners": dict(self._owners),
                "owner_strategy_id": next(iter(self._owners), None),
                "owner_figi":        next(iter(self._owners.values()), None),
                "owner_ticker":      None,
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


_skip_log_ts: dict = {}


def _log_skip(kind: str, message: str, ticker: str):
    """SIGNAL_SKIP пишем не чаще раза в 5 минут на (тикер, причина): иначе журнал растёт на сотни тысяч строк."""
    key = (ticker, kind)
    now_ts = time.monotonic()
    if now_ts - _skip_log_ts.get(key, 0) < 300:
        return
    _skip_log_ts[key] = now_ts
    log_event("SIGNAL_SKIP", message, ticker=ticker)


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
        self.session_total_assets = 0.0    # total portfolio value (cash + positions)
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
# figi-и позиций которые мониторинг сейчас закрывает — защита от дублей с _handle_manual_close
_closing_in_progress: Set[str] = set()
_bot_client_cls = None  # устанавливается в main() до запуска потока стрима
_30034_count: Dict[str, int] = {}  # figi → число последовательных 30034 за сессию


ORDERS_STREAM_PING_MS = 10000      # просим брокера присылать пинг-кадры раз в 10 с
ORDERS_STREAM_DEAD_SEC = 45        # столько тишины (даже без пингов) считаем обрывом соединения
# None — не знаем, шлёт ли сервер пинги; True — шлёт; False — не шлёт (сторож отключён)
_orders_stream_pings: Optional[bool] = None
_orders_stream_silent_connects = 0


def _orders_stream_watchdog(client_obj, conn: dict):
    """Сторож залипания OrdersStream: gRPC-поток может молча перестать доставлять события (после простоя,
    выходных, обрыва на стороне брокера), и тогда исполнение узнаётся только резервным опросом.
    Если от потока давно нет кадров (пинги приходят раз в ORDERS_STREAM_PING_MS), закрываем канал —
    чтение падает с ошибкой, и воркер переподключается."""
    global _orders_stream_pings, _orders_stream_silent_connects
    while not conn["stop"]:
        time.sleep(5)
        silent = time.time() - conn["last"]
        if _orders_stream_pings is False:
            return  # сервер пингов не шлёт — по тишине обрыв не определить
        if silent > ORDERS_STREAM_DEAD_SEC:
            if not conn["ping_seen"] and _orders_stream_pings is None:
                # Ни одного кадра с момента подключения: либо пинги не поддерживаются, либо поток мёртв.
                # Два подключения подряд без единого пинга — считаем, что пингов нет, и сторож отключаем
                _orders_stream_silent_connects += 1
                if _orders_stream_silent_connects >= 2:
                    _orders_stream_pings = False
                    log.info("OrdersStream: пинг-кадров нет, сторож залипания отключён")
                    return
            log.warning("OrdersStream: нет кадров %.0f с — переподключение", silent)
            try:
                client_obj._channel.close()
            except Exception as exc:
                log.warning("OrdersStream: не удалось закрыть канал: %s", exc)
            return


def _orders_stream_worker():
    """Фоновый поток: подписывается на события исполнения ордеров через OrdersStream."""
    global _orders_stream_pings, _orders_stream_silent_connects
    account_id = settings.TINVEST_ACCOUNT_ID
    while True:
        conn = {"last": time.time(), "ping_seen": False, "stop": False}
        try:
            client_obj = _bot_client_cls(settings.TINVEST_TOKEN)
            with client_obj as sc:
                log.info("OrdersStream: подключён")
                Thread(target=_orders_stream_watchdog, args=(client_obj, conn), daemon=True,
                       name="orders-stream-watchdog").start()
                for resp in sc.orders_stream.trades_stream(accounts=[account_id],
                                                           ping_delay_ms=ORDERS_STREAM_PING_MS):
                    conn["last"] = time.time()
                    ot = getattr(resp, "order_trades", None)
                    if ot is None:
                        # пинг-кадр: поток жив
                        if not conn["ping_seen"]:
                            conn["ping_seen"] = True
                            _orders_stream_pings = True
                            _orders_stream_silent_connects = 0
                        continue
                    order_id = str(getattr(ot, "order_id", "") or "")
                    q = _pending_orders.get(order_id)
                    if q is not None:
                        q.put(ot)
            log.warning("OrdersStream: сервер закрыл поток, переподключение через 5 с")
            time.sleep(5)
        except Exception as exc:
            log.warning("OrdersStream: ошибка %s, переподключение через 5 с", exc)
            time.sleep(5)
        finally:
            conn["stop"] = True


# ── MarketDataStream (Приоритет-2) ────────────────────────────────────────────
# figi → {"price": Decimal, "orderbook": {...}}   — записывается потоком стрима,
# читается process_instrument() в основном потоке.
_md: Dict[str, dict] = {}
_md_lock = Lock()
MD_STALE_SEC = 45.0  # цена/стакан из стрима старше этого считаются устаревшими → берём REST

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
                                _d = _md.setdefault(figi, {})
                                _d["price"] = Decimal(str(price))
                                _d["price_ts"] = time.monotonic()  # для проверки свежести

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
                            _d = _md.setdefault(figi, {})
                            _d["orderbook"] = {
                                "bid_price": bp, "ask_price": ap,
                                "bid_vol": bid_vol, "ask_vol": ask_vol,
                                "spread_pct": spread,
                            }
                            _d["ob_ts"] = time.monotonic()

                    if stop_ev.is_set():
                        break
        except Exception as exc:
            log.warning("MarketDataStream: ошибка %s, переподключение через 5 с", exc)
            stop_ev.set()
            time.sleep(5)


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
    # API возвращает integer (5=NORMAL) или строку — проверяем оба формата
    return is_tradable(status)


def notify(message: str, is_error: bool = False):
    telegram_errors_only = get_setting("telegram_errors_only", "0") == "1"
    if telegram_errors_only and not is_error:
        return
    notifier.send(message)


from app import notify as _notify_bridge  # noqa: E402
_notify_bridge.register(notify)


def _get_actual_close_price(client, figi: str, instrument_uid: str, opened_at: str,
                            direction: str = "") -> Optional[float]:
    """
    Ищет фактическую цену закрытия позиции в журнале операций брокера.
    direction — направление ПОЗИЦИИ (BUY=лонг, SELL=шорт): закрывающей считается операция
    противоположной стороны, выполненная после opened_at. Без direction берётся любая сделка.
    Возвращает float или None если не найдено.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        opened_dt = None
        try:
            # opened_at хранится в локальном (МСК) времени бота
            _opened_naive = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
            age = _now() - _opened_naive
            lookback = max(timedelta(hours=2), age + timedelta(minutes=10))
            opened_dt = (now_utc - age).replace(microsecond=0)
        except Exception:
            lookback = timedelta(hours=24)
        lookback = min(lookback, timedelta(days=7))
        ops = client.operations.get_operations(
            account_id=settings.TINVEST_ACCOUNT_ID,
            from_=now_utc - lookback,
            to=now_utc,
            figi=figi,
        )
        buy_types = {"OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_CARD", "OPERATION_TYPE_BUY_MARGIN"}
        sell_types = {"OPERATION_TYPE_SELL", "OPERATION_TYPE_SELL_CARD", "OPERATION_TYPE_SELL_MARGIN"}
        want = None
        if direction:
            want = sell_types if str(direction).upper() == "BUY" else buy_types

        candidates = []
        for op in ops.operations:
            # state/operation_type приходят как enum (с .name), число или строка
            op_type = getattr(getattr(op, "operation_type", None), "name", str(getattr(op, "operation_type", "")))
            op_state = getattr(getattr(op, "state", None), "name", str(getattr(op, "state", "")))
            if op_state not in ("OPERATION_STATE_EXECUTED", "1"):
                continue
            if op_type not in buy_types | sell_types:
                continue
            if want is not None and op_type not in want:
                continue
            price_obj = getattr(op, "price", None)
            if price_obj is None:
                continue
            price_val = float(quotation_to_decimal(price_obj))
            op_dt = getattr(op, "date", None)
            if opened_dt is not None and op_dt is not None and op_dt < opened_dt - timedelta(seconds=5):
                continue  # сделка до открытия — это не закрытие нашей позиции
            if price_val > 0:
                candidates.append((op_dt, price_val))
        if candidates:
            # Закрытием позиции считаем ПЕРВУЮ встречную сделку после открытия
            # (последняя могла относиться к уже следующей позиции по той же бумаге).
            candidates.sort(key=lambda c: c[0] or datetime.max.replace(tzinfo=timezone.utc))
            price_val = candidates[0][1]
            log.debug("Actual close price for %s: %.4f", figi[:8], price_val)
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
    qty_lots    = int(bp["qty"])
    opened_at   = bp.get("opened_at", "")

    # Защита от дублей: если трейд уже записан за последние 10 минут — пропускаем
    try:
        _cutoff = (_now() - timedelta(seconds=600)).strftime("%Y-%m-%d %H:%M:%S")
        _existing = get_recent_trade(figi, _cutoff)
        if _existing:
            log.info("_handle_manual_close %s: трейд уже записан %s, пропуск", ticker, _existing["time"])
            close_position(figi, source="BOT")
            return
    except Exception:
        pass
    # lot_size для расчёта денежных сумм — ищем по figi во ВСЕХ стратегиях
    try:
        from app.db import db_cursor as _dbc_mc
        with _dbc_mc() as _c_mc:
            _c_mc.execute(
                "SELECT lot FROM strategy_instruments WHERE figi=? AND lot>0 LIMIT 1", (figi,)
            )
            _row_mc = _c_mc.fetchone()
            _lot_sz_mc = int(_row_mc[0]) if _row_mc else 1
    except Exception:
        _lot_sz_mc = int(state.instrument_meta.get(ticker, {}).get("lot", 1))
    qty = qty_lots * _lot_sz_mc  # акции для денежных расчётов

    sl_tp  = get_instrument_sl_tp(figi)
    sl_pct = Decimal(str(sl_tp["sl_pct"]))
    tp_pct = Decimal(str(sl_tp["tp_pct"]))

    # 1. Пробуем получить фактическую цену закрытия из операций брокера
    actual_price = None
    instrument_uid = bp.get("instrument_uid", "") or ""
    if client is not None:
        actual_price = _get_actual_close_price(client, figi, instrument_uid, opened_at, direction)

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
        "MANUAL_CLOSE": "Закрыта вне бота (причина не определена)",
    }
    reason_ui = reason_labels.get(reason, reason)
    pnl_sign = "+" if float(pnl) >= 0 else ""

    add_trade({
        "time":             _now().strftime("%Y-%m-%d %H:%M:%S"),
        "open_time":        opened_at,
        "ticker":           ticker,
        "figi":             figi,
        "direction":        direction,
        "entry":            float(entry_price),
        "exit":             float(exit_price),
        "qty":              qty_lots,   # лоты, как и в основном пути закрытия (в штуках считаются только деньги)
        "gross_amount":     float(exit_price * qty),
        "commission":       float(commission),
        "pnl":              float(pnl),
        "reason":           reason,
        "close_order_id":   "",
        # estimated — цена выхода не подтверждена операциями брокера (оценка по рынку/уровням SL-TP)
        "execution_status": ("estimated" if actual_price is None
                             else ("exchange" if reason != "MANUAL_CLOSE" else "manual")),
    })
    close_position(figi, source="BOT")
    log_event("ORDER_CLOSE", f"{ticker} {reason} exit={float(exit_price):.4f} pnl={float(pnl):.2f}", ticker=ticker)

    # Дневные лимиты должны видеть и закрытия биржевым стопом/вручную (раньше считались только свои закрытия)
    try:
        state.daily_pnl += Decimal(str(pnl))
        state.trades_today += 1
    except Exception as _dpe:
        log.debug("daily counters on manual_close %s: %s", ticker, _dpe)

    # ML: размечаем признаки и этой сделки (биржевой стоп/ручное закрытие).
    # Без этого все закрытия по нативным стопам оставались бы без метки (сироты).
    try:
        from app.ml.feature_builder import label_features as _lf_mc
        from app.ml.orchestrator import on_trade_closed as _otc_mc
        _feat_id_mc = int(bp.get("ml_feature_id", 0) or 0) or get_position_feature_id(figi)
        if _feat_id_mc:
            _inv_mc = abs(float(entry_price) * qty)
            _q_mc = float(pnl) / max(0.001, _inv_mc)
            _lf_mc(_feat_id_mc, float(pnl), _q_mc)
        _otc_mc(figi, float(pnl))
        from app.ml.experience import record_exit as _re_mc
        _re_mc(figi, float(pnl))   # закрываем контекст сделки в ml_trade_context
    except Exception as _mle:
        log.debug("ML label on manual_close %s: %s", ticker, _mle)

    _gross_mc = exit_price * qty  # qty уже в акциях
    notify(
        f"{'✅' if float(pnl) >= 0 else '🔴'} {reason_ui}\n"
        f"{ticker} | {direction}\n"
        f"Вход: {float(entry_price):.4f} ₽ → Выход: {float(exit_price):.4f} ₽\n"
        f"Лотов: {qty_lots} × {_lot_sz_mc} шт = {qty} акций\n"
        f"Сумма: {float(_gross_mc):,.0f} ₽\n"
        f"PnL ≈ {pnl_sign}{float(pnl):.2f} ₽"
    )
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


def _ml_indicators(ticker: str, ind=None) -> dict:
    """RSI/MACD/BB для ML-признаков. Приоритет — свежие API-индикаторы (ind), затем instrument_meta.
    Отсутствующие значения остаются None: feature_builder подставит нейтральные, а не выдуманные 0/50."""
    meta = state.instrument_meta.get(ticker, {})
    src = ind or {}
    return {k: (src.get(k) if src.get(k) is not None else meta.get(k))
            for k in ("rsi", "macd", "macd_signal", "bb_upper", "bb_lower")}


def _broker_net_lots(client, figi: str):
    """Нетто-позиция брокера по бумаге в лотах (+лонг / −шорт, 0 — нет позиции).
    None — не удалось получить (вызывающий обязан считать это запретом на сделку)."""
    try:
        _rate.record("GetPortfolio")
        pf = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        for p in getattr(pf, "positions", []) or []:
            if getattr(p, "figi", "") == figi:
                lots = getattr(p, "quantity_lots", None)
                val = quotation_to_decimal(lots) if lots is not None and quotation_to_decimal(lots) != 0                     else quotation_to_decimal(getattr(p, "quantity", None))
                return int(val)
        return 0
    except Exception as e:
        log.warning("Не удалось получить позицию брокера по %s: %s", figi, e)
        return None


def _entry_blocked_reason(client, figi: str, ticker: str):
    """Защита от дублей: вход разрешён, только если по бумаге нет позиции ни у брокера, ни в учёте бота.
    Возвращает текст причины отказа или None. Ошибка проверки = отказ (лучше пропустить вход, чем задвоить позицию)."""
    for bp in get_open_positions(source="BOT"):
        if bp["figi"] == figi:
            return f"Позиция по {ticker} уже есть в учёте бота ({bp['direction']} {bp['qty']} лот) — вход отменён"
    net = _broker_net_lots(client, figi)
    if net is None:
        return f"Не удалось проверить позицию брокера по {ticker} — вход отменён"
    if net != 0:
        return f"У брокера уже есть позиция по {ticker} ({net:+d} лот) — вход отменён"
    return None


def _plan_close(client, figi: str, direction: str, qty_lots: int):
    """Закрывающий ордер может только УМЕНЬШАТЬ позицию. Сверяем с брокером: нет позиции — не шлём
    (иначе откроется встречная), позиция другого знака — не шлём, объёма меньше — уменьшаем.
    Возвращает (количество лотов, причина отказа); количество 0 = ордер слать нельзя."""
    net = _broker_net_lots(client, figi)
    if net is None:
        return 0, "не удалось получить позицию брокера"
    if net == 0:
        return 0, "у брокера нет позиции (уже закрыта биржевым стопом или вручную)"
    if (direction == "BUY" and net < 0) or (direction == "SELL" and net > 0):
        return 0, f"направление позиции у брокера ({net:+d}) не совпадает с учётом бота ({direction})"
    return min(int(qty_lots), abs(net)), ""


_stops_check_ts = 0.0
STOPS_CHECK_INTERVAL_SEC = 60


def _ensure_native_stops(client, broker_signed: Dict[str, int]):
    """Сторож защиты: у каждой открытой позиции бота должны быть биржевые SL/TP.
    Стопы пропадают при ручных действиях в интерфейсе, сбоях выставления и закрытиях «в ноль»;
    раз в минуту возвращаем недостающие (позиции моложе 90 с пропускаем — стопы ещё ставятся)."""
    global _stops_check_ts
    now_ts = time.time()
    if now_ts - _stops_check_ts < STOPS_CHECK_INTERVAL_SEC:
        return
    _stops_check_ts = now_ts
    try:
        resp = client.stop_orders.get_stop_orders(account_id=settings.TINVEST_ACCOUNT_ID)
        all_stops = list(getattr(resp, "stop_orders", []) or [])

        # Лишние стопы опаснее отсутствующих: два стопа на одну позицию закрывают её и открывают встречную,
        # а стоп без позиции при срабатывании сам открывает позицию. Суммарный объём каждого типа стопов
        # не должен превышать позицию брокера; стопы свежее 2 минут не трогаем (позиция ещё может не отразиться).
        now_utc = datetime.now(timezone.utc)
        groups: Dict[tuple, list] = {}
        for st in all_stops:
            groups.setdefault((getattr(st, "figi", ""), int(getattr(st, "order_type", 0) or 0)), []).append(st)
        for (g_figi, g_type), lst in groups.items():
            size = abs(int(broker_signed.get(g_figi, 0)))
            lst.sort(key=lambda x: getattr(x, "create_date", now_utc))
            acc = 0
            for st in lst:
                created = getattr(st, "create_date", None)
                if created is not None and (now_utc - created).total_seconds() < 120:
                    acc += int(getattr(st, "lots_requested", 0) or 0)
                    continue
                lots_st = int(getattr(st, "lots_requested", 0) or 0)
                if acc >= size or acc + lots_st > size:
                    try:
                        client.stop_orders.cancel_stop_order(account_id=settings.TINVEST_ACCOUNT_ID,
                                                             stop_order_id=st.stop_order_id)
                        log_event("STOP_ORDER", f"{g_figi[:8]}: снят лишний стоп (тип {g_type}, {lots_st} лот, "
                                  f"позиция {size})", level="WARNING")
                    except Exception as ce:
                        log.warning("Не удалось снять лишний стоп %s: %s", st.stop_order_id, ce)
                else:
                    acc += lots_st

        # Объём стопов по каждому figi и типу (1 — тейк, 2 — стоп): нужен, чтобы докрыть позицию,
        # если стопы поставлены на часть объёма (докупка, подхват позиции брокера, сбой выставления)
        covered_lots: Dict[tuple, int] = {}
        for st in all_stops:
            key = (getattr(st, "figi", ""), int(getattr(st, "order_type", 0) or 0))
            covered_lots[key] = covered_lots.get(key, 0) + int(getattr(st, "lots_requested", 0) or 0)
        for bp in get_open_positions(source="BOT"):
            if bp["figi"] in _closing_in_progress:
                continue
            try:
                age = (_now() - datetime.strptime(bp.get("opened_at", ""), "%Y-%m-%d %H:%M:%S")).total_seconds()
                if age < 90:
                    continue
            except Exception:
                pass
            # Целевой объём не больше реальной позиции брокера: стоп сверх позиции опасен
            target = int(bp["qty"])
            if bp["figi"] in broker_signed:
                target = min(target, abs(int(broker_signed[bp["figi"]])))
            miss_sl = target - covered_lots.get((bp["figi"], 2), 0)
            miss_tp = target - covered_lots.get((bp["figi"], 1), 0)
            if miss_sl <= 0 and miss_tp <= 0:
                continue
            meta = state.instrument_meta.get(bp["ticker"], {})
            sl_tp = get_instrument_sl_tp(bp["figi"])
            sl = max(Decimal(str(sl_tp["sl_pct"])), MIN_STOP_LOSS_PCT)
            ids = _place_native_stops(
                client, bp["ticker"], bp["figi"], meta.get("instrument_uid", "") or "",
                "BUY" if str(bp["direction"]).upper() == "BUY" else "SELL",
                target, Decimal(str(bp["entry_price"])), sl, Decimal(str(sl_tp["tp_pct"])),
                qty_sl=max(miss_sl, 0), qty_tp=max(miss_tp, 0))
            if ids.get("sl_id") or ids.get("tp_id"):
                log_event("STOP_ORDER", f"{bp['ticker']}: сторож докрыл биржевые стопы {ids} "
                          f"(не хватало SL {max(miss_sl, 0)}, TP {max(miss_tp, 0)} лот)",
                          ticker=bp["ticker"], level="WARNING")
    except Exception as e:
        log.warning("_ensure_native_stops: %s", e)


_mismatch_last_alert: Dict[str, float] = {}
MISMATCH_ALERT_INTERVAL_SEC = 1800
# Расхождение сразу после входа/выхода — норма: брокер уже показывает позицию, а запись бота
# появится после выставления стопов. Реальным считаем только расхождение, державшееся дольше.
MISMATCH_GRACE_SEC = 180
# figi → (брокер, бот, время, когда расхождение впервые замечено в таком виде)
_mismatch_first_seen: Dict[str, tuple] = {}


def _bot_signed_positions() -> tuple:
    """Нетто-позиции открытых BOT-записей по figi (лонг +, шорт −) и их тикеры."""
    bot_signed: Dict[str, int] = {}
    tickers: Dict[str, str] = {}
    for bp in get_open_positions(source="BOT"):
        sign = 1 if str(bp["direction"]).upper() == "BUY" else -1
        bot_signed[bp["figi"]] = bot_signed.get(bp["figi"], 0) + sign * int(bp["qty"])
        tickers[bp["figi"]] = bp["ticker"]
    return bot_signed, tickers


def _persistent_mismatch(figi: str, b_brk: int, b_bot: int, now_ts: float) -> bool:
    """True, если расхождение по figi в таком виде держится не меньше MISMATCH_GRACE_SEC."""
    seen = _mismatch_first_seen.get(figi)
    if seen is None or seen[0] != b_brk or seen[1] != b_bot:
        _mismatch_first_seen[figi] = (b_brk, b_bot, now_ts)
        return False
    return now_ts - seen[2] >= MISMATCH_GRACE_SEC


def _figi_in_strategies(figi: str) -> bool:
    """Есть ли бумага среди инструментов стратегий бота."""
    try:
        from app.db import db_cursor as _dbc_fs
        with _dbc_fs() as _c_fs:
            _c_fs.execute("SELECT 1 FROM strategy_instruments WHERE figi=? LIMIT 1", (figi,))
            return _c_fs.fetchone() is not None
    except Exception:
        return False


def _adopt_orphan_positions(broker_info: Dict[str, dict]):
    """Подхват позиций брокера, о которых бот не знает (ордер потерян, исполнился после таймаута,
    восстановление после сбоя): без учётной записи у позиции нет ни стопов, ни мониторинга, а входы
    по бумаге блокируются защитой от дублей. Расхождение должно продержаться MISMATCH_GRACE_SEC.
      • у бота позиции нет → создаём BOT-запись по данным брокера (стопы поставит сторож);
      • то же направление, другой объём → приводим учёт к брокеру (брокер — источник истины);
      • противоположное направление → не трогаем (неясно, что правильно), только предупреждение.
    Отключается настройкой adopt_orphan_positions=0."""
    if get_setting("adopt_orphan_positions", "1") != "1":
        return
    bot_signed, _ = _bot_signed_positions()
    bot_rows: Dict[str, list] = {}
    for bp in get_open_positions(source="BOT"):
        bot_rows.setdefault(bp["figi"], []).append(bp)
    now_ts = time.time()
    for figi, info in broker_info.items():
        if figi in _closing_in_progress:
            continue
        b_brk = info["signed"]
        b_bot = bot_signed.get(figi, 0)
        if b_brk == b_bot:
            continue
        if not _persistent_mismatch(figi, b_brk, b_bot, now_ts):
            continue
        if not _figi_in_strategies(figi):
            continue  # бумага вне стратегий бота — это не наша позиция, не трогаем
        ticker = info["ticker"]
        direction = "BUY" if b_brk > 0 else "SELL"
        rows = bot_rows.get(figi, [])
        if b_bot == 0 and not rows:
            action = "создана учётная запись"
        elif len(rows) == 1 and b_bot != 0 and (b_bot > 0) == (b_brk > 0):
            action = f"объём в учёте {abs(b_bot)} → {abs(b_brk)} лот"
        else:
            continue  # встречное направление или несколько записей — решаем вручную
        upsert_position({
            "ticker": ticker,
            "figi": figi,
            "direction": direction,
            "qty": abs(b_brk),
            "entry_price": info["avg_price"] or (rows[0]["entry_price"] if rows else 0),
            "current_price": info["current_price"],
            "unrealized_pnl": rows[0].get("unrealized_pnl", 0) if rows else 0,
            "opened_at": rows[0]["opened_at"] if rows else _now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            "source": "BOT",
        })
        _mismatch_first_seen.pop(figi, None)
        _mismatch_last_alert.pop(figi, None)
        msg = (f"{ticker}: подхвачена позиция брокера {direction} {abs(b_brk)} лот "
               f"(в учёте было {b_bot:+d}): {action}")
        log_event("POSITION_ADOPT", msg, ticker=ticker, level="WARNING")
        notify(f"ℹ️ {msg}", is_error=False)


def _reconcile_bot_vs_broker(broker_signed: Dict[str, int]):
    """Сверяет нетто-позицию брокера по каждому figi с суммой открытых BOT-позиций.
    Брокер ведёт позицию нетто по бумаге, бот — по стратегиям; расхождение значит, что
    ордер (ручной, close-all, встречный вход другой стратегии) изменил позицию мимо учёта бота.
    Ничего не торгует и не правит — только пишет POSITION_MISMATCH: не раньше чем расхождение
    продержится MISMATCH_GRACE_SEC и не чаще раза в 30 мин на figi."""
    bot_signed, tickers = _bot_signed_positions()
    now_ts = time.time()
    for figi in set(bot_signed) | set(broker_signed):
        b_bot, b_brk = bot_signed.get(figi, 0), broker_signed.get(figi, 0)
        if b_bot == b_brk:
            _mismatch_last_alert.pop(figi, None)
            _mismatch_first_seen.pop(figi, None)
            continue
        if not _persistent_mismatch(figi, b_brk, b_bot, now_ts):
            continue
        if now_ts - _mismatch_last_alert.get(figi, 0) < MISMATCH_ALERT_INTERVAL_SEC:
            continue
        _mismatch_last_alert[figi] = now_ts
        ticker = tickers.get(figi) or figi[:8]
        msg = f"{ticker}: брокер {b_brk:+d} лот, учёт бота {b_bot:+d} лот — расхождение {b_brk - b_bot:+d}"
        log_event("POSITION_MISMATCH", msg, ticker=ticker, level="WARNING")
        notify(f"⚠️ Рассинхрон позиции\n{msg}", is_error=True)


def _sync_legacy_positions():
    """Приводит словарь позиций основного цикла (state.open_positions) в соответствие с БД.
    Он восстанавливается из БД при старте и сам по себе не очищается: после закрытия позиции воркером
    запись жила дальше и «управляла» чужой позицией (отменённые закрытия AFLT с неверным направлением).
    Убираем запись, если позиции нет в БД, направление не совпадает или позицию ведёт параллельный воркер."""
    db_dir = {p["figi"]: str(p["direction"]).upper() for p in get_open_positions(source="BOT")}
    for tk in list(state.open_positions.keys()):
        pos = state.open_positions[tk]
        f = pos.get("figi", "")
        if f not in db_dir:
            why = "позиции нет в БД"
        elif str(pos.get("direction", "")).upper() != db_dir[f]:
            why = f"направление {pos.get('direction')} в памяти не совпадает с БД ({db_dir[f]})"
        elif _parallel_coord.owned_by_other(f, None):
            why = "позицию ведёт параллельный воркер"
        else:
            continue
        log.warning("Основной цикл %s: запись позиции сброшена — %s", tk, why)
        del state.open_positions[tk]
        if _parallel_coord.owner_figi(None) == f:
            _parallel_coord.release(None)


def _lot_size_by_figi(figi: str, ticker: str = "") -> int:
    """Размер лота инструмента (штук в лоте); ищем по figi во всех стратегиях, иначе 1."""
    try:
        from app.db import db_cursor as _dbc_lot
        with _dbc_lot() as _c_lot:
            _c_lot.execute(
                "SELECT lot FROM strategy_instruments WHERE figi=? AND lot>0 LIMIT 1", (figi,)
            )
            _row_lot = _c_lot.fetchone()
            if _row_lot:
                return int(_row_lot[0])
    except Exception:
        pass
    return int(state.instrument_meta.get(ticker, {}).get("lot", 1) or 1)


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
        broker_signed: Dict[str, int] = {}  # figi → нетто-позиция в лотах (+лонг / −шорт)
        broker_info: Dict[str, dict] = {}   # figi → тикер, объём и цены брокера (для подхвата позиций)
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
            broker_signed[figi] = qty
            broker_info[figi] = {"ticker": ticker, "signed": qty, "avg_price": avg_price,
                                 "current_price": current_price}

            # Вычисляем нереализованный PnL если брокер вернул 0 (часто в sandbox)
            if expected_yield == 0.0 and avg_price > 0 and current_price > 0:
                # Цена за штуку, qty в лотах — переводим в штуки через размер лота
                shares = abs(qty) * _lot_size_by_figi(figi, ticker)
                if direction == "BUY":
                    expected_yield = (current_price - avg_price) * shares
                else:
                    expected_yield = (avg_price - current_price) * shares

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
                # Мониторинг уже закрывает эту позицию — не создаём дубль
                if bp["figi"] in _closing_in_progress:
                    log.debug("sync_portfolio: %s закрывается мониторингом, пропускаем", bp["ticker"])
                    continue
                log.info("Позиция %s не найдена в портфеле — определяем причину закрытия", bp["ticker"])
                _handle_manual_close(bp, market_map, client=client)

        _adopt_orphan_positions(broker_info)
        _reconcile_bot_vs_broker(broker_signed)
        _ensure_native_stops(client, broker_signed)

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


MSK_TZ = timezone(timedelta(hours=3))


def get_max_open_positions() -> int:
    """
    Макс. число одновременных позиций и делитель банка. В параллельном режиме равно числу
    активных стратегий профиля (см. app.db.effective_max_open_positions): банк делится на них
    поровну, на каждую стратегию — по позиции.
    """
    from app.db import effective_max_open_positions
    return effective_max_open_positions()


def is_moex_session_open() -> bool:
    """True если MOEX основная (10:00-18:50) или вечерняя (19:05-23:50) сессия открыта по МСК."""
    now = datetime.now(MSK_TZ)
    if now.weekday() >= 5:           # суббота=5, воскресенье=6
        return False
    if now.strftime("%Y-%m-%d") in _get_moex_holidays_cached():   # нерабочий праздничный день
        return False
    t = now.hour * 60 + now.minute
    return (600 <= t < 1130) or (1145 <= t < 1430)


def _secs_to_next_session_open() -> tuple[int, str]:
    """Секунды до следующего открытия сессии + метка времени открытия (МСК)."""
    now = datetime.now(MSK_TZ)
    t = now.hour * 60 + now.minute

    # Следующее событие открытия в минутах от полуночи
    # Вечерняя 19:05 открывается если сейчас между 18:50 и 19:05 (перерыв)
    if now.weekday() < 5 and 1130 <= t < 1145:
        open_min, label = 1145, "19:05"
    elif now.weekday() < 5 and t < 600:
        open_min, label = 600, "10:00"
    else:
        # Следующий торговый день 10:00
        days_ahead = 1
        if now.weekday() == 4:   # пятница → понедельник
            days_ahead = 3
        elif now.weekday() == 5: # суббота → понедельник
            days_ahead = 2
        next_day = now + timedelta(days=days_ahead)
        target = next_day.replace(hour=10, minute=0, second=0, microsecond=0)
        secs = int((target - now).total_seconds())
        return max(secs, 0), "10:00"

    target_h, target_m = divmod(open_min, 60)
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    secs = int((target - now).total_seconds())
    return max(secs, 0), label


def _generate_moex_holidays(year: int) -> set:
    """
    Автоматически генерирует нерабочие дни MOEX для заданного года.
    Российские федеральные праздники + перенос выходных.
    Не требует ручного обновления — работает на любой год.
    """
    from datetime import date as _date, timedelta as _td
    holidays = set()

    def _add(d: _date):
        holidays.add(d.strftime("%Y-%m-%d"))

    def _shift_weekend(d: _date) -> _date:
        """Если праздник попадает на сб/вс — переносим на ближайший пн."""
        if d.weekday() == 5:   # суббота → понедельник
            return d + _td(days=2)
        if d.weekday() == 6:   # воскресенье → понедельник
            return d + _td(days=1)
        return d

    # ── Фиксированные даты с переносом ──
    fixed = [
        (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),  # Новый год + Рождество
        (2, 23),   # День защитника
        (3, 8),    # 8 марта
        (5, 1),    # Труда
        (5, 9),    # Победа
        (6, 12),   # День России
        (11, 4),   # Народное единство
    ]
    for m, d in fixed:
        try:
            base = _date(year, m, d)
            _add(_shift_weekend(base))
            # Добавляем длинные новогодние каникулы (1-8 января всегда нерабочие)
            if m == 1 and d == 1:
                for extra in range(1, 9):
                    _add(_date(year, 1, extra))
        except ValueError:
            pass

    # Перенос праздников: если суббота/воскресенье попадает между двумя праздниками,
    # правительство может объявить дополнительный выходной. Это сложно предсказать точно,
    # поэтому обрабатываем только базовые случаи.

    return holidays


def _get_moex_holidays_cached() -> set:
    """Кеш на 24 часа: генерирует праздники для текущего + следующего года."""
    import time as _t
    _cache = getattr(_get_moex_holidays_cached, "_cache", None)
    _ts    = getattr(_get_moex_holidays_cached, "_ts", 0)
    if _cache is not None and (_t.time() - _ts) < 86400:
        return _cache
    now_year = datetime.now(MSK_TZ).year
    result = _generate_moex_holidays(now_year) | _generate_moex_holidays(now_year + 1)
    _get_moex_holidays_cached._cache = result
    _get_moex_holidays_cached._ts    = _t.time()
    return result


_MOEX_HOLIDAYS: set = set()  # будет заполнен при первом вызове short_carry_risk()


SHORT_CARRY_BLOCK_HOUR = 17  # После этого часа МСК не открываем новые SHORT

# Минимально допустимый стоп-лосс. Стопы теснее этого выбивает рыночным шумом
# почти сразу после открытия → «черн» (позиция живёт секунды, минус = комиссия).
MIN_STOP_LOSS_PCT = Decimal("0.005")  # 0.5%


def short_carry_risk() -> int:
    """
    Риск переноса SHORT через выходные/праздники.
    0 = нет риска — SHORT разрешён
    1 = завтра нерабочий день, сейчас >= 17:00 МСК — блокируем
    3 = пятница/канун длинных выходных, сейчас >= 17:00 МСК — блокируем

    До 17:00 МСК шорт разрешён даже в пятницу — инвестор закроет его до конца дня.
    Праздники генерируются автоматически для текущего + следующего года.
    """
    holidays = _get_moex_holidays_cached()
    now = datetime.now(MSK_TZ)
    wd = now.weekday()  # 0=пн, 4=пт, 5=сб, 6=вс

    if wd >= 5:   # Выходной — торговля и так закрыта
        return 3

    # Считаем сколько нерабочих дней подряд начиная с завтра
    consecutive = 0
    for delta in range(1, 8):
        d = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        wd_d = (now + timedelta(days=delta)).weekday()
        if wd_d >= 5 or d in holidays:
            consecutive += 1
        else:
            break

    if consecutive == 0:
        return 0   # завтра рабочий день — риска нет

    # Есть нерабочие дни впереди: блокируем только после 17:00 МСК
    if now.hour < SHORT_CARRY_BLOCK_HOUR:
        return 0   # ещё можно — успеем закрыть до конца дня

    return 3 if consecutive >= 3 else 1


def should_force_close_short() -> bool:
    """
    True если открытый SHORT нужно принудительно закрыть чтобы не тащить через выходные.
    Срабатывает за 1 час до конца каждой сессии в дни с carry-риском:
      - Основная сессия 10:00-18:50: закрываем с 17:50 МСК
      - Вечерняя сессия 19:05-23:50: закрываем с 22:50 МСК
    """
    holidays = _get_moex_holidays_cached()
    now = datetime.now(MSK_TZ)
    wd = now.weekday()
    t = now.hour * 60 + now.minute

    # Есть ли carry-риск?
    has_risk = wd == 4  # пятница
    if not has_risk and wd < 5:
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        has_risk = tomorrow in holidays
    if not has_risk:
        return False

    in_eve = 1145 <= t < 1430  # 19:05-23:50

    # Закрываем ТОЛЬКО в вечерней сессии за 1 час до её конца (22:50 МСК).
    # В основной сессии и в перерыве (18:50-19:05) позицию не трогаем —
    # вечерняя сессия ещё даёт возможность закрыться.
    if in_eve and t >= 22 * 60 + 50:
        return True

    return False


def is_tradable(status) -> bool:
    # T-Bank API возвращает enum как целое число (5=NORMAL_TRADING, 1=NOT_AVAILABLE и т.д.)
    # Используем allowlist вместо denylist чтобы надёжно блокировать всё неизвестное
    status_int = int(status) if str(status).isdigit() else -1
    status_str = str(status)
    # Разрешённые статусы: NORMAL_TRADING(5), DEALER_NORMAL(14), SESSION_OPEN(13),
    # OPENING_PERIOD(2), CLOSING_PERIOD(3)
    allowed_ints = {2, 3, 5, 13, 14}
    allowed_strs = {
        "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING",
        "SECURITY_TRADING_STATUS_SESSION_OPEN",
        "SECURITY_TRADING_STATUS_OPENING_PERIOD",
        "SECURITY_TRADING_STATUS_CLOSING_PERIOD",
    }
    return status_int in allowed_ints or status_str in allowed_strs


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


def get_assets_and_buying_power(client):
    """(капитал, свободно для новых позиций) одним вызовом портфеля. Свободно = капитал − позиции по модулю,
    но не больше денег брокера: наличные включают выручку от шортов и завышают то, что можно вложить."""
    try:
        _rate.record("GetPortfolio")
        pf = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        assets = float(quotation_to_decimal(pf.total_amount_portfolio))
        cash = float(quotation_to_decimal(pf.total_amount_currencies)) if getattr(pf, "total_amount_currencies", None) else 0.0
        gross = 0.0
        for p in getattr(pf, "positions", []) or []:
            if "currency" in str(getattr(p, "instrument_type", "") or "").lower():
                continue
            qty = getattr(p, "quantity", None)
            price = getattr(p, "current_price", None)
            if qty is not None and price is not None:
                gross += abs(float(quotation_to_decimal(qty)) * float(quotation_to_decimal(price)))
        free = max(0.0, min(cash, assets - gross)) if cash else max(0.0, assets - gross)
        return assets, free
    except Exception as e:
        log.warning("Не удалось получить капитал/свободные средства: %s", e)
        return get_total_assets(client), get_money_balance(client)


def get_total_assets(client) -> float:
    """Полная стоимость портфеля: кэш + позиции (total_amount_portfolio)."""
    try:
        _rate.record("GetPortfolio")
        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        total = getattr(portfolio, "total_amount_portfolio", None)
        if total:
            return float(quotation_to_decimal(total))
    except Exception as e:
        log.warning("Не удалось получить total_assets: %s", e)
    return state.session_balance_current  # fallback to cash


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
            "auto_lots": int(item.get("auto_lots", 0) or 0),
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
    today = _now().strftime("%Y-%m-%d")  # MSK, не UTC сервера
    if state.current_trade_date != today:
        state.daily_pnl = Decimal("0")
        state.trades_today = 0
        state.current_trade_date = today
        log_event("DAILY_RESET", "Daily counters reset")
        # ML: ежедневный ребаланс параметров (запускаем в фоне)
        try:
            from app.ml.orchestrator import run_daily_rebalance as _ml_rebalance
            Thread(target=_ml_rebalance, daemon=True).start()
        except Exception:
            pass


_ORDER_STATUS_BY_CODE = {0: "UNSPECIFIED", 1: "FILL", 2: "REJECTED", 3: "CANCELLED", 4: "NEW", 5: "PARTIALLYFILL"}


def _order_status_name(raw) -> str:
    """Имя статуса исполнения ордера. str() от enum в новых версиях Python даёт просто число
    («1» вместо FILL), поэтому берём имя члена enum, а число расшифровываем по таблице."""
    name = getattr(raw, "name", None)
    if name:
        return str(name)
    text = str(raw)
    try:
        return _ORDER_STATUS_BY_CODE.get(int(text), text)
    except ValueError:
        return text


def place_order_checked(client, ticker: str, figi: str, lots: int, raw_price: Decimal, direction: OrderDirection,
                        market: bool = False):
    """market=True → рыночный ордер (для закрытия позиций, гарантирует исполнение)."""
    meta = state.instrument_meta.get(ticker)
    if not meta:
        # Fallback: собираем мету из market_state БД (нужно для закрытия позиций после рестарта)
        try:
            _mmap = get_instrument_market_state_map()
            _ms = next((v for v in _mmap.values() if v.get("ticker") == ticker), None)
            if _ms and _ms.get("figi"):
                # lot_size ищем по figi во ВСЕХ стратегиях (не только активной) —
                # иначе для параллельных инструментов lot=1 и позиция запишет акции вместо лотов
                _lot_fb, _step_fb = 1, "0.01"
                try:
                    from app.db import db_cursor as _dbc_fb
                    with _dbc_fb() as _c_fb:
                        _c_fb.execute(
                            "SELECT lot, min_price_increment FROM strategy_instruments WHERE figi=? AND lot>0 LIMIT 1",
                            (figi,)
                        )
                        _row_fb = _c_fb.fetchone()
                        if _row_fb:
                            _lot_fb = int(_row_fb[0])
                            _step_fb = str(_row_fb[1]) if _row_fb[1] else "0.01"
                except Exception:
                    pass
                meta = {
                    "figi": figi, "instrument_uid": _ms.get("instrument_uid", ""),
                    "ticker": ticker, "lot": _lot_fb,
                    "min_price_increment": _step_fb,
                    "class_code": "", "instrument_type": "share",
                }
                state.instrument_meta[ticker] = meta
        except Exception:
            pass
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
                total_filled_shares = sum(int(getattr(t, "quantity", 0)) for t in trades)
                if total_filled_shares > 0:
                    weighted = sum(
                        quotation_to_decimal(getattr(t, "price")) * int(getattr(t, "quantity", 0))
                        for t in trades
                    )
                    _ep = weighted / Decimal(str(total_filled_shares))
                    if _accept_fill_price(_ep):
                        avg_price = _ep
                    # OrdersStream trade.quantity → акции; нормализуем в лоты (округляем вниз)
                    _lot_sz = int(meta.get("lot", 1))
                    if _lot_sz > 1 and total_filled_shares > 0:
                        lots_executed = total_filled_shares // _lot_sz
                    else:
                        lots_executed = total_filled_shares
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
                    execution_report_status = _order_status_name(
                        getattr(order_state, "execution_report_status", "UNKNOWN"))
                    lots_executed_raw = getattr(order_state, "lots_executed", None)
                    if lots_executed_raw is not None:
                        _le = int(lots_executed_raw)
                        _lot_sz2 = int(meta.get("lot", 1))
                        # lots_executed у брокера уже в лотах. Делим на размер лота только если значение
                        # больше заказанного (тогда это явно штуки); иначе 38 лотов ALRS превращались в 3
                        if _lot_sz2 > 1 and _le > lots:
                            lots_executed = _le // _lot_sz2
                        else:
                            lots_executed = _le
                    # Полное исполнение без объёма в ответе (бывает в sandbox) — значит, исполнено всё
                    if execution_report_status.endswith("FILL") and "PARTIALLY" not in execution_report_status \
                            and lots_executed == 0:
                        lots_executed = lots
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
                # Ордер мог исполниться между последним опросом и отменой — смотрим состояние ещё раз,
                # иначе у брокера останется позиция, о которой бот не знает
                try:
                    _os_last = client.orders.get_order_state(
                        account_id=settings.TINVEST_ACCOUNT_ID, order_id=response_order_id)
                    _st_last = _order_status_name(getattr(_os_last, "execution_report_status", "UNKNOWN"))
                    _le_last = int(getattr(_os_last, "lots_executed", 0) or 0)
                    if _le_last > lots and int(meta.get("lot", 1)) > 1:
                        _le_last //= int(meta.get("lot", 1))  # значение в штуках, а не в лотах
                    if "FILL" in _st_last:
                        execution_report_status = _st_last
                        lots_executed = _le_last or (lots if "PARTIALLY" not in _st_last else 0)
                        log_event("ORDER_FILL",
                            f"late fill: {ticker} qty={lots_executed} status={_st_last}", ticker=ticker)
                except Exception:
                    pass
                if lots_executed == 0:
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

        _30034_count.pop(figi, None)  # успех — сбрасываем счётчик ошибок
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
        if "30034" in msg:
            # Инструмент недоступен (биржа закрыта / sandbox ограничение)
            _30034_count[figi] = _30034_count.get(figi, 0) + 1
            if _30034_count[figi] >= 3:
                # После 3 попыток — пауза 1 час, сбрасываем счётчик
                pause_sec = 3600
                _30034_count[figi] = 0
                log_event("ORDER_MARKET_CLOSED",
                          f"{ticker}: PostOrder 30034 × 3 подряд — пауза 1 час",
                          ticker=ticker, level="WARNING")
            else:
                pause_sec = 300  # 5 мин обычная пауза
                log_event("ORDER_MARKET_CLOSED",
                          f"{ticker}: PostOrder 30034 — торги недоступны, пауза 5 мин",
                          ticker=ticker, level="WARNING")
            state.order_cooldowns[figi] = _now() - timedelta(seconds=BotState.ORDER_COOLDOWN_SEC - pause_sec)
            return None
        if "figi" in msg.lower():
            log_event("INVALID_FIGI", msg, ticker=ticker, level="WARNING")
            return None
        log_event("ORDER_ERROR", msg, ticker=ticker, level="ERROR")
        return None


class _SkipStop(Exception):
    """Служебное: стоп этого типа ставить не нужно (нулевой объём)."""


def _place_native_stops(client, ticker: str, figi: str, instrument_uid: str,
                        direction: str, qty: int, entry_price: Decimal,
                        sl_pct: Decimal, tp_pct: Decimal,
                        qty_sl: Optional[int] = None, qty_tp: Optional[int] = None) -> dict:
    """
    Размещает нативные StopLoss и TakeProfit на бирже после открытия позиции.
    Возвращает dict с id ордеров: {"sl_id": ..., "tp_id": ...}
    Работает даже при зависании бота — ордера исполнятся на стороне биржи.
    qty_sl / qty_tp — объём конкретного стопа (по умолчанию qty); 0 — этот стоп не ставим
    (так сторож докрывает только недостающие лоты).
    """
    qty_sl = qty if qty_sl is None else qty_sl
    qty_tp = qty if qty_tp is None else qty_tp
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
        if qty_sl <= 0:
            raise _SkipStop()
        resp = client.stop_orders.post_stop_order(
            instrument_id=instr_id,
            quantity=qty_sl,
            stop_price=_q(sl_price),
            direction=close_dir,
            account_id=settings.TINVEST_ACCOUNT_ID,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        )
        ids["sl_id"] = getattr(resp, "stop_order_id", None)
        log_event("STOP_ORDER", f"{ticker} SL native @ {sl_price} id={ids['sl_id']}", ticker=ticker)
    except _SkipStop:
        pass
    except Exception as e:
        log.warning("Не удалось разместить нативный SL для %s (%s, %s лот, вход %s, цена стопа %s): %s",
                    ticker, direction, qty_sl, entry_price, sl_price, e)

    # TakeProfit
    try:
        if qty_tp <= 0:
            raise _SkipStop()
        resp = client.stop_orders.post_stop_order(
            instrument_id=instr_id,
            quantity=qty_tp,
            stop_price=_q(tp_price),
            direction=close_dir,
            account_id=settings.TINVEST_ACCOUNT_ID,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        )
        ids["tp_id"] = getattr(resp, "stop_order_id", None)
        log_event("STOP_ORDER", f"{ticker} TP native @ {tp_price} id={ids['tp_id']}", ticker=ticker)
    except _SkipStop:
        pass
    except Exception as e:
        log.warning("Не удалось разместить нативный TP для %s (%s, %s лот, вход %s, цена стопа %s): %s",
                    ticker, direction, qty_tp, entry_price, tp_price, e)

    return ids


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
    # Пол стопа: стоп теснее 0.5% выбивает рыночным шумом/спредом сразу после
    # открытия (черн — позиция живёт секунды и закрывается «в ноль» по комиссии).
    # Защищает и от устаревших значений в памяти, и от слишком агрессивного оптимизатора.
    stop_loss_pct = max(Decimal(str(item["stop_loss_pct"])), MIN_STOP_LOSS_PCT)
    take_profit_pct = Decimal(str(item["take_profit_pct"]))
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
            "auto_lots":         int(item.get("auto_lots", 0) or 0),
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
        # Стрим мог оборваться («Stream removed»): без проверки возраста SL/TP и входы шли бы по устаревшей цене
        _price_fresh = (time.monotonic() - stream_md.get("price_ts", 0.0)) <= MD_STALE_SEC
        price = stream_price if (stream_price and stream_price > 0 and _price_fresh) else get_last_price(client, figi, instrument_uid=instrument_uid)

        # ── Свечи: 50 баров достаточно для всех режимов strategy_engine ──────
        candle_n = 50
        candles = get_candles(client, figi, n=candle_n, instrument_uid=instrument_uid)
        # Сигнал считаем по ЗАВЕРШЁННЫМ свечам: формирующаяся перерисовывается внутри минуты (ложные пробои,
        # заниженный объём), а бэктест всегда шёл по закрытым барам. Цена входа/выхода остаётся живой.
        candles = [c for c in candles if getattr(c, "is_complete", True)]
        last_volume = get_last_candle_volume(candles)

        # ── Стакан: стрим depth=10 → REST depth=1 резервный вариант ─────────
        ob_stream = stream_md.get("orderbook", {})
        if ob_stream and (time.monotonic() - stream_md.get("ob_ts", 0.0)) > MD_STALE_SEC:
            ob_stream = {}  # стакан устарел — берём REST
        if ob_stream:
            spread_pct = ob_stream["spread_pct"]
            bid_vol: int = ob_stream["bid_vol"]
            ask_vol: int = ob_stream["ask_vol"]
        else:
            spread_pct = get_order_book_spread_pct(client, figi, instrument_uid=instrument_uid)
            bid_vol = ask_vol = 0

        # Сохраняем цену всегда — до любых дальнейших проверок
        _bid = ob_stream.get("bid_price") if ob_stream else None
        _ask = ob_stream.get("ask_price") if ob_stream else None
        upsert_instrument_market_state(
            figi=figi,
            ticker=ticker,
            last_price=price,
            price_time=_now().strftime("%Y-%m-%d %H:%M:%S"),
            volume_1m=last_volume,
            bid_price=_bid,
            ask_price=_ask,
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
    sig     = sig_result["action"]
    score   = sig_result["score"]
    reasons = sig_result.get("reasons", [])

    def _save_signal(action=None, skip_reason="", skip_filter=""):
        try:
            set_runtime(f"last_signal_{figi}", _j.dumps({
                "action": action if action is not None else sig,
                "score": score,
                "mode": tradingmode,
                "time": _now().strftime("%H:%M:%S"),
                "skip_reason": skip_reason,
                "skip_filter": skip_filter,
                "reasons": reasons,
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

    # Проверки сессии/торгуемости — только для новых входов.
    # Открытые позиции проверяются ниже независимо от статуса сессии.
    if ticker not in positions:
        if not is_moex_session_open():
            _save_signal(skip_reason="Биржа закрыта (MOEX)", skip_filter="market_hours")
            return

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
            _save_signal(skip_reason=f"API-фильтр: {rsi_str} | {macd_str} | {bb_str}",
                         skip_filter="api_confirm")

    if ticker in positions:
        pos = positions[ticker]
        if pos.get("stop_loss_pct"):
            stop_loss_pct = Decimal(str(pos["stop_loss_pct"]))
        if pos.get("take_profit_pct"):
            take_profit_pct = Decimal(str(pos["take_profit_pct"]))
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
        _lot_sz_u = int(item.get("lot", 1))
        _qty_shares_u = qty * _lot_sz_u
        if direction == "BUY":
            unrealized_pnl = (price - entry_price) * _qty_shares_u
        else:
            unrealized_pnl = (entry_price - price) * _qty_shares_u

        _still_open = update_position_price(figi, float(price), float(unrealized_pnl))
        if not _still_open:
            # Позиция закрыта снаружи (_handle_manual_close) пока мы мониторили
            log.info("%s: позиция закрыта снаружи в БД — сбрасываем из мониторинга", ticker)
            del positions[ticker]
            if _coord is not None:
                _coord.release(_strategy_id)
            return

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

        # Принудительное закрытие SHORT перед выходными/праздниками
        if not close_signal and direction == "SELL" and should_force_close_short():
            close_signal = True
            close_reason = "SHORT_CARRY_CLOSE"
            log.info("%s: принудительное закрытие SHORT (риск переноса через выходные)", ticker)

        # ML Фаза 4: досрочное закрытие при развороте
        if not close_signal:
            try:
                from app.ml.feature_builder import build_features as _bf_ex
                from app.ml.model_predictor import should_exit_early as _ml_early
                from app.ml.multi_timeframe import get_all_timeframes as _gtf_ex
                _opened_at_str = pos.get("opened_at", _now().strftime("%Y-%m-%d %H:%M:%S"))
                _pos_min = (_now() - datetime.strptime(_opened_at_str, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
                _ind_ex = _ml_indicators(ticker, locals().get("_ind"))
                _ob_ex = {"bid_vol": bid_vol, "ask_vol": ask_vol}
                _tf_ex = _gtf_ex(client, figi)
                _feat_ex = _bf_ex(figi, ticker, candles_dict, int(pos.get("signal_score") or 50), direction, _ind_ex, _ob_ex,
                                  _tf_ex.get("1hour", []), _tf_ex.get("4hour", []),
                                  position_minutes=_pos_min)
                _early, _p_ex = _ml_early(
                    figi, ticker, _strategy_id or 0, _feat_ex, direction,
                    float(entry_price), float(price), _pos_min
                )
                if _early:
                    close_signal = True
                    close_reason = "ML_EARLY_EXIT"
            except Exception:
                pass

        if close_signal:
            # Защита от гонки с sync_portfolio_positions:
            # если позиция уже закрыта в БД — очищаем память и выходим
            try:
                _open_figis = {p["figi"] for p in get_open_positions(source="BOT")}
                if figi not in _open_figis:
                    log.info("%s: позиция уже закрыта в БД (гонка с sync), очищаем", ticker)
                    del positions[ticker]
                    if _coord is not None:
                        _coord.release(_strategy_id)
                    return
            except Exception:
                pass
            # Флаг "закрытие в процессе" — не даёт _handle_manual_close создать дубль
            _closing_in_progress.add(figi)
            try:
                close_dir = OrderDirection.ORDER_DIRECTION_SELL if direction == "BUY" else OrderDirection.ORDER_DIRECTION_BUY
                # Отменяем ВСЕ ордера по figi перед закрытием (нативные стопы + лимитники)
                _close_qty, _close_why = _plan_close(client, figi, direction, qty)
                if _close_qty <= 0:
                    # Закрывать нечего/нельзя: ордер не шлём. Запись о позиции приведёт в порядок sync_portfolio_positions
                    log_event("POSITION_CLOSE", f"{ticker}: закрытие отменено — {_close_why}", ticker=ticker, level="WARNING")
                    state.order_cooldowns[figi] = _now()
                    return
                qty = _close_qty  # не больше фактической позиции брокера
                _cancel_all_orders_for_figi(client, figi, ticker)
                try:
                    order_result = place_order_checked(client, ticker, figi, qty, price, close_dir, market=True)
                except Exception:
                    order_result = None
                    _close_failed_exc = True
                else:
                    _close_failed_exc = False
                if not order_result:
                    state.order_cooldowns[figi] = _now()
                    # Биржевые SL/TP сняты ДО отправки рыночного ордера: при сбое ордера позиция осталась бы
                    # без защиты до следующего цикла — возвращаем стопы.
                    try:
                        pos["native_stop_ids"] = _place_native_stops(
                            client, ticker, figi, instrument_uid, direction, qty,
                            entry_price, stop_loss_pct, take_profit_pct)
                        log_event("STOP_ORDER", f"{ticker}: стопы восстановлены после неудачного закрытия",
                                  ticker=ticker, level="WARNING")
                    except Exception as _rse:
                        log.warning("%s: не удалось восстановить стопы после сбоя закрытия: %s", ticker, _rse)
                    if _close_failed_exc:
                        raise
                    return
            finally:
                _closing_in_progress.discard(figi)

            exit_price = Decimal(str(order_result["executed_price"]))
            exec_qty_lots = int(order_result["lots_executed"] or qty)
            _lot_sz_c = int(item.get("lot", 1))
            exec_qty_shares = exec_qty_lots * _lot_sz_c  # акции для денежных расчётов

            estimated_commission_pct = Decimal(_cfg("estimated_commission_pct", str(settings.ESTIMATED_COMMISSION_PCT)))
            gross_amount = exit_price * exec_qty_shares
            commission = (entry_price * exec_qty_shares + exit_price * exec_qty_shares) * estimated_commission_pct

            if direction == "BUY":
                pnl = (exit_price - entry_price) * exec_qty_shares - commission
            else:
                pnl = (entry_price - exit_price) * exec_qty_shares - commission

            state.daily_pnl += pnl
            state.trades_today += 1

            trade = {
                "time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "open_time": pos.get("opened_at", ""),
                "ticker": ticker,
                "figi": figi,
                "direction": direction,
                "entry": float(entry_price),
                "exit": float(exit_price),
                "qty": exec_qty_lots,
                "gross_amount": float(gross_amount),
                "commission": float(commission),
                "pnl": float(pnl),
                "reason": close_reason,
                "close_order_id": order_result["response_order_id"],
                "execution_status": order_result["execution_status"],
            }
            # ml_feature_id читаем ДО close_position: после закрытия позиция не OPEN и id не найти
            _feat_id = pos.get("ml_feature_id", 0) or get_position_feature_id(figi)
            add_trade(trade)
            close_position(figi, source="BOT")
            log_event("ORDER_CLOSE", f"{ticker} pnl={float(pnl):.2f} reason={close_reason}", ticker=ticker)
            # ML Phase 1: запись исхода
            try:
                from app.ml.experience import record_exit as _ml_exit
                from app.ml.orchestrator import on_trade_closed as _ml_otc
                _ml_exit(figi, float(pnl))
                _ml_otc(figi, float(pnl))
            except Exception:
                pass
            # ML Phase 2: разметка признаков + переобучение
            try:
                from app.ml.feature_builder import label_features as _lf
                from app.ml.model_trainer import should_retrain as _sr, train_model as _tm
                from app.ml.model_predictor import invalidate_cache as _ic
                if _feat_id:
                    _quality = float(pnl) / max(0.001, abs(float(entry_price) * exec_qty_shares))
                    _lf(_feat_id, float(pnl), _quality)
                if _sr(figi, _strategy_id or 0):
                    import threading
                    threading.Thread(target=lambda: (
                        _tm(figi, ticker, _strategy_id or 0),
                        _ic(figi, _strategy_id or 0)
                    ), daemon=True).start()
            except Exception:
                pass
            del positions[ticker]
            # Cooldown 60 сек после закрытия — защита от немедленного реверса
            state.close_cooldowns[figi] = _now()
            if _coord is not None:
                _coord.release(_strategy_id)

            _pnl_sign = "+" if float(pnl) >= 0 else ""
            # Рассчитываем % движения цены для наглядности
            _price_move_pct = (float(exit_price) - float(entry_price)) / float(entry_price) * 100
            _price_move_sign = "+" if _price_move_pct >= 0 else ""
            # Читаемое название причины закрытия
            _reason_display = {
                "STOP_LOSS":         "Стоп-лосс",
                "TAKE_PROFIT":       "Тейк-профит",
                "ML_EARLY_EXIT":     "ML: досрочный выход",
                "SHORT_CARRY_CLOSE": "⚠️ Закрытие SHORT перед выходными",
            }.get(close_reason, close_reason)
            # Пометка трейлинг-стопа
            _trailing_note = ""
            if close_reason == "STOP_LOSS" and trailing_stop_enabled:
                _ts_val = pos.get("trailing_stop", 0)
                if _ts_val and abs(float(exit_price) - float(_ts_val)) < 0.05 * float(entry_price):
                    _trailing_note = f" (трейлинг @ {float(_ts_val):.4f} ₽)"
            notify(
                f"{'✅' if float(pnl) >= 0 else '🔴'} Закрытие позиции\n"
                f"{ticker} | {direction}\n"
                f"Вход: {float(entry_price):.4f} ₽ → Выход: {float(exit_price):.4f} ₽ "
                f"({_price_move_sign}{_price_move_pct:.3f}%)\n"
                f"Лотов: {exec_qty_lots} × {_lot_sz_c} шт = {exec_qty_shares} акций\n"
                f"Сумма: {float(gross_amount):,.0f} ₽\n"
                f"PnL: {_pnl_sign}{float(pnl):.2f} ₽\n"
                f"Причина: {_reason_display}{_trailing_note}"
            )
        return

    if _coord is not None:
        # Параллельный режим: пропускаем если лимит позиций достигнут и этот поток не владелец
        if _coord.owner_count >= get_max_open_positions() and not _coord.is_owner(_strategy_id):
            return
    elif len(positions) >= get_max_open_positions():
        return

    # Защита от реверса: после закрытия позиции не входим снова 60 сек
    _cc = state.close_cooldowns.get(figi)
    if _cc and (_now() - _cc).total_seconds() < 60:
        _save_signal(skip_reason="Пауза после закрытия позиции (60с)", skip_filter="close_cooldown")
        return

    # ── Торговое решение по сигналу (вычислен выше, до проверок сессии) ────────
    if sig == "HOLD":
        return

    # Общие дневные лимиты. Главный цикл их проверял, а воркеры параллельных стратегий — нет.
    try:
        if state.trades_today >= get_max_trades_per_day():
            _save_signal(skip_reason="Достигнут лимит сделок за день", skip_filter="max_trades_per_day")
            return
        if state.daily_pnl <= -get_max_daily_loss_rub():
            _save_signal(skip_reason="Достигнут дневной лимит убытка", skip_filter="max_daily_loss")
            return
    except Exception as _dle:
        log.debug("daily limits check: %s", _dle)

    min_score = int(_cfg("min_signal_score", "0"))
    if min_score > 0 and score < min_score:
        _log_skip("min_score", f"{ticker}: score={score} < порог={min_score} [{tradingmode}], пропуск", ticker)
        _save_signal(skip_reason=f"Качество сигнала {score} < порог {min_score}", skip_filter="min_score")
        return

    log_event("SIGNAL", f"{sig} [{tradingmode}] score={score} on {ticker}", ticker=ticker)

    # ── Фильтр сервиса сигналов T-Bank ───────────────────────────────────────
    if use_signal_service:
        from app.services.tbank_client import get_tbank_signals
        analyst = get_tbank_signals([figi]).get(figi, "NEUTRAL")
        if sig == "BUY" and analyst == "SELL":
            _log_skip("analyst_buy", f"{ticker} BUY пропущен: аналитики SELL", ticker)
            _save_signal(skip_reason="Аналитики T-Bank: SELL против BUY", skip_filter="signal_service")
            return
        if sig == "SELL" and analyst == "BUY":
            _log_skip("analyst_sell", f"{ticker} SELL пропущен: аналитики BUY", ticker)
            _save_signal(skip_reason="Аналитики T-Bank: BUY против SELL", skip_filter="signal_service")
            return

    # ── ML фильтр входа (Фаза 2: soft/hard mode) ─────────────────────────────
    _ml_features_for_entry = None
    _ml_conf = None
    if sig in ("BUY", "SELL"):
        try:
            from app.ml.feature_builder import build_features as _bf_e, store_features as _sf_e
            from app.ml.model_predictor import should_enter as _ml_enter
            from app.ml.multi_timeframe import get_all_timeframes as _gtf_e
            _ml_ind_e = _ml_indicators(ticker, locals().get("_ind"))
            _ob_e = {"bid_vol": bid_vol, "ask_vol": ask_vol}
            _tf_e = _gtf_e(client, figi)
            _ml_features_for_entry = _bf_e(
                figi, ticker, candles_dict, score, sig, _ml_ind_e, _ob_e,
                _tf_e.get("1hour", []), _tf_e.get("4hour", [])
            )
            _min_score = int(_cfg("min_signal_score", "0"))
            _allow, _ml_conf, _ml_reason = _ml_enter(
                figi, ticker, _strategy_id or 0, _ml_features_for_entry, sig,
                signal_score=score, min_score=_min_score
            )
            if not _allow:
                _save_signal(skip_reason=_ml_reason, skip_filter="ml_entry_block")
                return
        except Exception:
            pass

    # ── ML адаптивные SL/TP от волатильности (Фаза 3) ────────────────────────
    if _ml_features_for_entry and sig in ("BUY", "SELL"):
        try:
            from app.ml.model_predictor import compute_adaptive_sl_tp as _ml_sltp
            _new_sl, _new_tp = _ml_sltp(_ml_features_for_entry,
                                         float(stop_loss_pct), float(take_profit_pct))
            if abs(_new_sl - float(stop_loss_pct)) > 0.0001 or abs(_new_tp - float(take_profit_pct)) > 0.0001:
                log.info("ML adaptive SL/TP %s: SL %.3f%%→%.3f%% TP %.3f%%→%.3f%%",
                         ticker, float(stop_loss_pct)*100, _new_sl*100,
                         float(take_profit_pct)*100, _new_tp*100)
                stop_loss_pct   = Decimal(str(_new_sl))
                take_profit_pct = Decimal(str(_new_tp))
        except Exception:
            pass
    # Повторно держим пол стопа — адаптивная корректировка не должна делать его теснее.
    stop_loss_pct = max(stop_loss_pct, MIN_STOP_LOSS_PCT)

    # ── Авто-расчёт лотов от суммы ИТОГО портфеля ────────────────────────────
    _ml_lot_scale = 1.0  # масштаб от ML-уверенности
    if int(item.get("auto_lots", 0) or 0) and sig in ("BUY", "SELL"):
        _lot_size_v = int(item.get("lot", 1))
        _comm_v     = Decimal(_cfg("estimated_commission_pct", "0.0004"))
        _cost_1lot  = Decimal(str(_lot_size_v)) * price * (1 + _comm_v)
        if _cost_1lot > 0:
            # Делим капитал на max_open_positions — каждая позиция получает равную долю
            # портфеля, а не "первая съедает всё". Ограничиваем доступным cash.
            _max_pos_al = get_max_open_positions()
            _assets = Decimal(str(state.session_total_assets or state.session_balance_current))
            _cash   = Decimal(str(state.session_balance_current or state.session_total_assets))
            _budget = _assets / Decimal(str(_max_pos_al))   # доля портфеля на 1 позицию
            if _budget > _cash:                              # но не больше реального cash
                _budget = _cash
            _auto   = max(1, int(_budget / _cost_1lot))
            # ML confidence-based scaling: только когда модель уверена выше порога входа.
            # Если P < ENTRY_THRESHOLD — мета-стратегия уже переопределила ML (score+trend),
            # поэтому скейлинг не применяем — используем полный auto_lots.
            if _ml_features_for_entry and _ml_conf is not None:
                try:
                    from app.ml.model_predictor import compute_lot_scale as _cls, ENTRY_THRESHOLD as _ET
                    if float(_ml_conf) >= _ET:
                        _ml_lot_scale = _cls(float(_ml_conf))
                        if _ml_lot_scale < 1.0:
                            _auto = max(1, int(_auto * _ml_lot_scale))
                    # else: P < порога → не скейлим, meta-strategy взяла управление
                except Exception:
                    pass
            if _auto != lot:
                log_event("AUTO_LOTS",
                          f"{ticker}: бюджет {float(_budget):.0f}₽ (портфель {float(_assets):.0f}₽/{_max_pos_al}) "
                          f"/ {float(_cost_1lot):.0f}₽/лот = {_auto} лотов (ML scale={_ml_lot_scale:.2f})",
                          ticker=ticker)
            lot = _auto

    # ── Проверка баланса перед ордером (для BUY и SHORT) ─────────────────────
    if sig in ("BUY", "SELL"):
        lot_size = item.get("lot", 1)
        commission_pct = Decimal(_cfg("estimated_commission_pct", "0.0004"))
        # Запас на проскальзывание рыночного ордера (исполнение по ask/bid)
        _slippage = Decimal("0.002")  # 0.2%
        required = Decimal(str(lot)) * Decimal(str(lot_size)) * price * (1 + commission_pct + _slippage)
        # Для SHORT тоже нужна маржа ≈ стоимость позиции (sandbox/T-Bank требует обеспечение)
        available = Decimal(str(state.session_balance_current or state.session_total_assets))
        if available < required:
            _reason = (f"Недостаточно средств для {'шорта' if sig=='SELL' else 'покупки'}: "
                       f"нужно {float(required):.0f} ₽, доступно {float(available):.0f} ₽")
            log_event("BALANCE_WARNING", f"{ticker}: {_reason} — пропуск", ticker=ticker)
            _save_signal(skip_reason=_reason, skip_filter="balance")
            return

    # ── Защита от неттинга: брокер ведёт позицию нетто по бумаге, встречный вход
    # закрыл бы (частично) позицию другой стратегии/ручную, а бот посчитал бы её «исчезнувшей».
    if sig in ("BUY", "SELL"):
        _opp = [
            _p for _src in ("BOT", "PORTFOLIO") for _p in get_open_positions(source=_src)
            if _p["figi"] == figi and str(_p["direction"]).upper() != sig
        ]
        if _opp:
            _reason = (f"Встречная позиция по {ticker} ({_opp[0]['direction']} {_opp[0]['qty']} лот) — "
                       f"вход {sig} нетто-закрыл бы её")
            log_event("SIGNAL_SKIP", _reason, ticker=ticker)
            _save_signal(skip_reason=_reason, skip_filter="netting")
            return

    # ── Проверка дневного лимита потерь по инструменту ───────────────────────
    _max_loss_instr = float(item.get("max_daily_loss_rub", 0) or 0)
    if _max_loss_instr > 0:
        _today = _now().strftime("%Y-%m-%d")
        _ticker_pnl = get_ticker_daily_pnl(ticker, _today)
        if _ticker_pnl <= -_max_loss_instr:
            _reason = f"Дневной лимит потерь по {ticker} исчерпан: {_ticker_pnl:.2f} ₽ (лимит -{_max_loss_instr:.0f} ₽)"
            log_event("BALANCE_WARNING", f"{ticker}: {_reason}", ticker=ticker)
            _save_signal(skip_reason=_reason, skip_filter="instrument_daily_loss")
            return

    # ── Фильтр давления стакана (только при наличии данных стрима) ───────────
    total_vol = bid_vol + ask_vol
    if use_order_book_filter and total_vol > 0:
        buy_pressure = bid_vol / total_vol   # доля объёма на стороне бид
        sell_pressure = ask_vol / total_vol
        if sig == "BUY" and buy_pressure < 0.40:
            _log_skip("ob_buy", f"{ticker} BUY пропущен: давление покупателей {buy_pressure:.0%}", ticker)
            _save_signal(skip_reason=f"Давление покупателей {buy_pressure:.0%} < 40%", skip_filter="order_book")
            return
        if sig == "SELL" and sell_pressure < 0.40:
            _log_skip("ob_sell", f"{ticker} SELL пропущен: давление продавцов {sell_pressure:.0%}", ticker)
            _save_signal(skip_reason=f"Давление продавцов {sell_pressure:.0%} < 40%", skip_filter="order_book")
            return

    if sig == "BUY":
        if not allow_long_global or not allow_long:
            return
        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return  # другой поток только что захватил первым

        _block = _entry_blocked_reason(client, figi, ticker)
        if _block:
            log_event("SIGNAL_SKIP", _block, ticker=ticker, level="WARNING")
            _save_signal(skip_reason=_block, skip_filter="duplicate_guard")
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_BUY, market=True)
        if not order_result:
            state.order_cooldowns[figi] = _now()
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        _qty_filled = int(order_result["lots_executed"])
        # Локально уменьшаем доступный cash — чтобы параллельные воркеры
        # не пересчитывали лоты от устаревшего баланса до следующего синка
        try:
            _used = _ep * _qty_filled * int(item.get("lot", 1))
            state.session_balance_current = max(0.0, state.session_balance_current - _used)
        except Exception:
            pass
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
            "signal_score": score,
            # SL/TP, с которыми реально открыта позиция (после ML-адаптации): программные выходы
            # должны совпадать с биржевыми стопами, а не пересчитываться от текущих настроек
            "stop_loss_pct": float(stop_loss_pct),
            "take_profit_pct": float(take_profit_pct),
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

        # ML Phase 1: запись контекста
        try:
            from app.ml.experience import record_entry as _ml_entry
            _ml_sid = _strategy_id or 0
            _ml_ind = _ml_indicators(ticker, locals().get("_ind"))
            _ml_entry(figi, ticker, _ml_sid, tradingmode, score, _ml_ind,
                      float(stop_loss_pct), float(take_profit_pct))
        except Exception:
            pass
        # ML Phase 2: сохранение признаков для обучения модели
        try:
            from app.ml.feature_builder import build_features as _bf2, store_features as _sf2
            from app.ml.multi_timeframe import get_all_timeframes as _gtf2
            _ml_ind2 = _ml_indicators(ticker, locals().get("_ind"))
            _ob2 = {"bid_vol": bid_vol, "ask_vol": ask_vol}
            _tf2 = _gtf2(client, figi)
            _feat2 = _bf2(figi, ticker, candles_dict, score, "BUY", _ml_ind2, _ob2,
                          _tf2.get("1hour", []), _tf2.get("4hour", []))
            _feat_id = _sf2(figi, ticker, _strategy_id or 0, _feat2, 0)
            positions[ticker]["ml_feature_id"] = _feat_id
            if _feat_id:
                save_position_feature_id(figi, _feat_id)
        except Exception:
            pass

        sl_ui = f"{float(stop_loss_pct)*100:.2f}%"
        tp_ui = f"{float(take_profit_pct)*100:.2f}%"
        _sl_price = Decimal(str(_ep)) * (1 - stop_loss_pct)
        _tp_price = Decimal(str(_ep)) * (1 + take_profit_pct)
        _lot_sz_n = int(item.get("lot", 1))
        _amt_n = _qty_filled * _lot_sz_n * _ep
        _trailing_ui = " | 🔄 Трейлинг-стоп вкл" if trailing_stop_enabled else ""
        notify(
            f"🟢 Открытие позиции\n"
            f"{ticker} | BUY\n"
            f"Цена: {_ep:.4f} ₽\n"
            f"Лотов: {_qty_filled} × {_lot_sz_n} шт = {_qty_filled * _lot_sz_n} акций\n"
            f"Сумма: {_amt_n:,.0f} ₽\n"
            f"SL: {sl_ui} ({float(_sl_price):.4f} ₽) | TP: {tp_ui} ({float(_tp_price):.4f} ₽){_trailing_ui}\n"
            f"{'✅ Стопы на бирже' if _stop_ids.get('sl_id') else '⚠️ Стопы не размещены'}"
        )

    elif sig == "SELL":
        if not allow_short_global or not allow_short:
            return

        # ── Защита от SHORT с переносом через выходные/праздники ─────────────
        _carry = short_carry_risk()
        if _carry >= 1:
            _carry_names = {1: "перенос через нерабочий день", 3: "перенос через выходные (3× стоимость)"}
            _carry_reason = _carry_names.get(_carry, f"перенос {_carry} дней")
            log.info("SELL %s заблокирован: риск переноса SHORT — %s", ticker, _carry_reason)
            _save_signal(
                skip_reason=f"Шорт заблокирован: {_carry_reason}",
                skip_filter="short_carry_risk"
            )
            return

        if _coord is not None and not _coord.try_claim(_strategy_id, figi, ticker):
            return

        _block = _entry_blocked_reason(client, figi, ticker)
        if _block:
            log_event("SIGNAL_SKIP", _block, ticker=ticker, level="WARNING")
            _save_signal(skip_reason=_block, skip_filter="duplicate_guard")
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        order_result = place_order_checked(client, ticker, figi, lot, price, OrderDirection.ORDER_DIRECTION_SELL, market=True)
        if not order_result:
            state.order_cooldowns[figi] = _now()
            if _coord is not None:
                _coord.release(_strategy_id)
            return
        _ep = float(order_result["executed_price"])
        _qty_filled = int(order_result["lots_executed"])
        # Локально резервируем маржу под шорт (см. BUY-ветку)
        try:
            _used = _ep * _qty_filled * int(item.get("lot", 1))
            state.session_balance_current = max(0.0, state.session_balance_current - _used)
        except Exception:
            pass
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
            "signal_score": score,
            # SL/TP, с которыми реально открыта позиция (после ML-адаптации): программные выходы
            # должны совпадать с биржевыми стопами, а не пересчитываться от текущих настроек
            "stop_loss_pct": float(stop_loss_pct),
            "take_profit_pct": float(take_profit_pct),
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

        # ML Phase 1: запись контекста SELL
        try:
            from app.ml.experience import record_entry as _ml_entry_s
            _ml_ind_s = _ml_indicators(ticker, locals().get("_ind"))
            _ml_entry_s(figi, ticker, _strategy_id or 0, tradingmode, score, _ml_ind_s,
                        float(stop_loss_pct), float(take_profit_pct))
        except Exception:
            pass
        # ML Phase 2: сохранение признаков SELL + id в positions
        try:
            from app.ml.feature_builder import build_features as _bf_s, store_features as _sf_s
            from app.ml.multi_timeframe import get_all_timeframes as _gtf_s
            _ml_ind_s2 = _ml_indicators(ticker, locals().get("_ind"))
            _ob_s = {"bid_vol": bid_vol, "ask_vol": ask_vol}
            _tf_s = _gtf_s(client, figi)
            _feat_s = _bf_s(figi, ticker, candles_dict, score, "SELL", _ml_ind_s2, _ob_s,
                            _tf_s.get("1hour", []), _tf_s.get("4hour", []))
            _feat_id_s = _sf_s(figi, ticker, _strategy_id or 0, _feat_s, 0)
            positions[ticker]["ml_feature_id"] = _feat_id_s
            if _feat_id_s:
                save_position_feature_id(figi, _feat_id_s)
        except Exception:
            pass

        sl_ui = f"{float(stop_loss_pct)*100:.2f}%"
        tp_ui = f"{float(take_profit_pct)*100:.2f}%"
        _sl_price = Decimal(str(_ep)) * (1 + stop_loss_pct)
        _tp_price = Decimal(str(_ep)) * (1 - take_profit_pct)
        _lot_sz_n = int(item.get("lot", 1))
        _amt_n = _qty_filled * _lot_sz_n * _ep
        _trailing_ui = " | 🔄 Трейлинг-стоп вкл" if trailing_stop_enabled else ""
        notify(
            f"🔴 Открытие позиции\n"
            f"{ticker} | SELL\n"
            f"Цена: {_ep:.4f} ₽\n"
            f"Лотов: {_qty_filled} × {_lot_sz_n} шт = {_qty_filled * _lot_sz_n} акций\n"
            f"Сумма: {_amt_n:,.0f} ₽\n"
            f"SL: {sl_ui} ({float(_sl_price):.4f} ₽) | TP: {tp_ui} ({float(_tp_price):.4f} ₽){_trailing_ui}\n"
            f"{'✅ Стопы на бирже' if _stop_ids.get('sl_id') else '⚠️ Стопы не размещены'}"
        )

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

    # Если total_assets ещё не инициализирован — обновляем из API
    if state.session_total_assets <= 0:
        try:
            from app.services.tbank_client import get_portfolio_snapshot as _gps2
            _prt = _gps2()
            state.session_total_assets = float(Decimal(str(_prt.get("total_assets", 0) or 0)))
            state.session_balance_current = float(Decimal(str(_prt.get("free_to_open", _prt.get("cash", 0)) or 0)))
        except Exception:
            pass

    # Восстанавливаем позиции из БД при старте (на случай перезапуска бота)
    positions: Dict = {}
    try:
        _instr_figis = {i["figi"] for i in _parallel_instruments(strategy_id)}
        for _bp in get_open_positions(source="BOT"):
            # Одна позиция — одна стратегия: если figi есть в нескольких стратегиях, берёт первая успевшая
            if _bp["figi"] in _instr_figis and not positions and _parallel_coord.adopt(strategy_id, _bp["figi"]):
                _tk = _bp["ticker"]
                positions[_tk] = {
                    "figi":       _bp["figi"],
                    "direction":  _bp["direction"],
                    "entry_price": float(_bp["entry_price"]),
                    "qty":        int(_bp["qty"]),
                    "opened_at":  _bp.get("opened_at", ""),
                    "trailing_stop": 0.0,
                }
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
            _db_open_pos = get_open_positions(source="BOT")
            _db_bot_figis = {p["figi"] for p in _db_open_pos}
            if positions:
                _db_dir_by_figi = {p["figi"]: str(p["direction"]).upper() for p in _db_open_pos}
                for tk in list(positions.keys()):
                    _mem_figi = positions[tk].get("figi", "")
                    if _mem_figi not in _db_bot_figis:
                        log.info("Parallel [%s] %s: позиция закрыта снаружи, сбрасываем", strat_name, tk)
                        del positions[tk]
                        _parallel_coord.release(strategy_id)
                    elif str(positions[tk].get("direction", "")).upper() != _db_dir_by_figi.get(_mem_figi):
                        # Запись в памяти устарела (старая позиция закрыта, по бумаге открыта новая
                        # в другую сторону): иначе мониторинг следит за чужим направлением и
                        # отменяет каждое закрытие. Сбрасываем — следующий цикл возьмёт позицию из БД
                        log.warning("Parallel [%s] %s: направление в памяти (%s) не совпадает с БД (%s), пересоздаём запись",
                                    strat_name, tk, positions[tk].get("direction"), _db_dir_by_figi.get(_mem_figi))
                        del positions[tk]
                        _parallel_coord.release(strategy_id)
            else:
                # Resync: если dict пуст, но в БД есть OPEN BOT позиция по нашим инструментам
                _instr_figi_set = {i["figi"] for i in instr}
                for _bp in _db_open_pos:
                    if _bp["figi"] in _instr_figi_set and _bp["figi"] not in {
                        v.get("figi") for v in positions.values()
                    } and _parallel_coord.adopt(strategy_id, _bp["figi"]):
                        _tk2 = _bp["ticker"]
                        positions[_tk2] = {
                            "figi":         _bp["figi"],
                            "direction":    _bp["direction"],
                            "entry_price":  float(_bp["entry_price"]),
                            "qty":          int(_bp["qty"]),
                            "opened_at":    _bp.get("opened_at", ""),
                            "trailing_stop": 0.0,
                            "ml_feature_id": _bp.get("ml_feature_id", 0),
                        }
                        log.info("Parallel [%s] resync: восстановлена позиция %s из БД", strat_name, _tk2)

            # Если нет открытых позиций и координатор занят — обновляем сигналы/цены,
            # но не открываем позиции (process_instrument сам проверяет _coord.try_claim)
            if not positions and _parallel_coord.owner_count >= get_max_open_positions() and not _parallel_coord.is_owner(strategy_id):
                _pset(strategy_id, "ожидание — лимит позиций достигнут")

            if get_setting("bot_enabled", "1") != "1":
                _pset(strategy_id, "бот выключен")
                stop_ev.wait(timeout=interval)
                continue

            # ── Sleep mode: если торговля только в сессию и биржа закрыта ────
            if not positions:  # с открытой позицией продолжаем мониторинг
                _pid_str = get_setting("active_profile_id", "").strip()
                _trade_session_only = "1"
                if _pid_str:
                    from app.db import get_profile_setting as _gps
                    _trade_session_only = _gps(int(_pid_str), "trade_only_session", "1")
                if _trade_session_only == "1" and not is_moex_session_open():
                    _sleep_secs, _open_label = _secs_to_next_session_open()
                    _wake_label = (datetime.now(MSK_TZ) + timedelta(seconds=_sleep_secs)).strftime("%H:%M")
                    _pset(strategy_id, f"🌙 сон до {_wake_label}")
                    log.debug("Parallel [%s] sleep mode %ds до %s", strat_name, _sleep_secs, _open_label)
                    # Спим до открытия сессии (но не дольше 10 мин — на случай смены настроек)
                    stop_ev.wait(timeout=min(_sleep_secs, 600))
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
                    # process_instrument сам проверяет координатор через _coord.try_claim()
                    # Убран break — все стратегии обновляют цены/сигналы даже когда другая в позиции
                    _rate.throttle_if_needed()  # пауза если близко к rate limit
                    _pset(strategy_id, "сканирование", item["ticker"])
                    # ML: подгружаем выученные параметры если уверенность достаточная
                    try:
                        from app.ml.orchestrator import get_ml_params as _ml_get
                        _ml_p = _ml_get(item["figi"], strategy_id)
                        if _ml_p:
                            if _ml_p.get("stop_loss_pct"):
                                item = dict(item); item["stop_loss_pct"] = Decimal(str(_ml_p["stop_loss_pct"]))
                            if _ml_p.get("take_profit_pct"):
                                item["take_profit_pct"] = Decimal(str(_ml_p["take_profit_pct"]))
                    except Exception:
                        pass
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

            if positions:
                status = "в позиции"
                ticker = list(positions.keys())[0]
            elif _parallel_coord.owner_count >= get_max_open_positions() and not _parallel_coord.is_owner(strategy_id):
                status = "лимит позиций достигнут"
                ticker = ""
            elif not is_moex_session_open():
                status = "торговля не ведётся"
                ticker = ""
            else:
                status = "ожидание сигнала"
                ticker = ""
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
            "auto_lots":         int(item.get("auto_lots", 0) or 0),
            "max_daily_loss_rub": float(item.get("max_daily_loss_rub", 0) or 0),
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

    # Дневные счётчики берём из БД: иначе любой рестарт обнуляет лимиты сделок/убытка за день.
    # Дата — по МСК (как в maybe_reset_daily), иначе вечером по UTC возможен ложный сброс при старте.
    try:
        from app.db import get_trade_stats_today as _gts
        _today_msk = _now().strftime("%Y-%m-%d")
        _st = _gts(_today_msk)
        state.current_trade_date = _today_msk
        state.trades_today = int(_st.get("trades_count") or 0)
        state.daily_pnl = Decimal(str(_st.get("total_pnl") or 0))
        log.info("Дневные счётчики восстановлены: сделок=%d, PnL=%.2f", state.trades_today, float(state.daily_pnl))
    except Exception as _dce:
        log.warning("Не удалось восстановить дневные счётчики: %s", _dce)

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

    # Инициализируем total_assets ДО запуска параллельных воркеров
    try:
        from app.services.tbank_client import get_portfolio_snapshot as _gps
        _init_port = _gps()
        state.session_total_assets = float(Decimal(str(_init_port.get("total_assets", 0) or 0)))
        state.session_balance_current = float(Decimal(str(_init_port.get("free_to_open", _init_port.get("cash", 0)) or 0)))
        log.info("Инициализация: total_assets=%.2f cash=%.2f",
                 state.session_total_assets, state.session_balance_current)
    except Exception as _ie:
        log.warning("Ошибка инициализации баланса: %s", _ie)

    _refresh_parallel_workers()
    log.info("Parallel strategy workers started")

    # Восстанавливаем BOT-позиции из БД в state.open_positions при перезапуске
    try:
        for _bp in get_open_positions(source="BOT"):
            _tk = _bp.get("ticker", "")
            if _tk and _tk not in state.open_positions:
                state.open_positions[_tk] = {
                    "figi":        _bp["figi"],
                    "direction":   _bp["direction"],
                    "entry_price": float(_bp.get("entry_price", 0)),
                    "qty":         int(_bp.get("qty", 0)),
                    "opened_at":   _bp.get("opened_at", ""),
                    "trailing_stop": 0.0,
                    "native_stop_ids": {},
                }
        if state.open_positions:
            log.info("Восстановлено %d BOT-позиций из БД: %s",
                     len(state.open_positions), list(state.open_positions))
    except Exception as _rpe:
        log.warning("Ошибка восстановления позиций из БД: %s", _rpe)

    with client_cls(settings.TINVEST_TOKEN) as client:
        state.session_total_assets, state.session_balance_start = get_assets_and_buying_power(client)
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

                if state.trades_today >= get_max_trades_per_day():
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    state.sync_runtime()
                    sync_portfolio_positions(client)  # лимит запрещает входы, но учёт позиций/закрытий должен идти
                    time.sleep(60)
                    continue

                # ── Sleep mode: если биржа закрыта и trade_only_session=1 ────
                _pid_sl = get_setting("active_profile_id", "").strip()
                _tos = get_profile_setting(int(_pid_sl), "trade_only_session", "1") if _pid_sl else "1"
                if _tos == "1" and not is_moex_session_open() and not state.open_positions:
                    _sl_secs, _sl_lbl = _secs_to_next_session_open()
                    _sl_wake = (datetime.now(MSK_TZ) + timedelta(seconds=_sl_secs)).strftime("%H:%M")
                    state.status = f"SLEEP_UNTIL_{_sl_wake}"
                    state.sync_runtime()
                    log.info("Основной цикл: sleep mode до %s (%d с)", _sl_lbl, _sl_secs)
                    time.sleep(min(_sl_secs, 600))
                    continue

                max_loss = get_max_daily_loss_rub()
                if state.daily_pnl <= -max_loss:
                    state.status = "SESSION_STOPPED_BY_LIMIT"
                    state.sync_runtime()
                    sync_portfolio_positions(client)  # см. выше: входы запрещены, учёт — нет
                    time.sleep(60)
                    continue

                # Периодически проверяем и перезапускаем параллельные воркеры
                global _last_parallel_check_ts
                now_ts = time.time()
                if now_ts - _last_parallel_check_ts > 30:
                    _last_parallel_check_ts = now_ts
                    _refresh_parallel_workers()

                watchlist = load_enabled_instruments(client)
                _sync_legacy_positions()

                for item in watchlist:
                    _rate.throttle_if_needed()
                    # В параллельном режиме передаём координатор чтобы не дублировать ордера
                    _main_coord = _parallel_coord if _parallel_threads else None
                    process_instrument(client, item, _coord=_main_coord)

                sync_portfolio_positions(client)

                state.session_total_assets, state.session_balance_current = get_assets_and_buying_power(client)
                set_runtime("session_balance", str(state.session_balance_current))
                set_runtime("session_total_assets", str(state.session_total_assets))
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