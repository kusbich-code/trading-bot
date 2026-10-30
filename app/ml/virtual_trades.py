"""Виртуальные сделки: сигнал отрабатывается без брокера, исход (прибыль/убыток) идёт в обучение модели.

Зачем. Метку «прибыль/убыток» раньше получали только реальные сделки, а их мало (лимиты позиций, паузы,
решения самой модели). Заблокированные моделью сигналы меток не получали, и модель перестала учиться.
Виртуальная сделка создаётся на каждый новый сигнал независимо от фильтров и решений модели, живёт по тем же
правилам SL/TP и закрывается в конце сессии или по таймауту.

Правила, от которых зависит качество данных:
  • один эпизод = одна сделка. Пока сигнал держится (повторяется каждую минуту), новых сделок не создаётся;
    новый эпизод начинается после паузы EPISODE_GAP_SEC без сигнала. Иначе «10 000 сделок» были бы сотней
    событий, повторённых много раз;
  • результат считается чистым: комиссия с обеих сторон, вход по ask/bid, выход «по рынку» с учётом спреда;
  • сделка не переносится через перерыв и ночь (гэпы и стоимость переноса шорта не моделируются);
  • сохраняются MFE/MAE, результат через 5/15/30/60 минут и поминутный путь цены: исход можно пересчитать
    для других SL/TP без новых сделок.

Модуль не зависит от SDK брокера и main.py: цены ему передают снаружи (из стрима).
"""
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ml.virtual")

MSK = timezone(timedelta(hours=3))

COMMISSION_PCT = 0.04          # комиссия за одну сторону, % (как estimated_commission_pct = 0.0004 у бота)
MAX_HOLD_MIN = 120             # максимальное время жизни виртуальной сделки, минут
EPISODE_GAP_SEC = 150          # столько секунд без сигнала — и следующий сигнал считается новым эпизодом
CHECKPOINTS_MIN = (5, 15, 30, 60)
MAX_OPEN_TOTAL = 600           # защита памяти: больше открытых виртуальных сделок не держим
MIN_LIFETIME_MIN = 5           # в последние минуты сессии новые сделки не открываем: исход не успеет сложиться
# окна, в которых сделка может жить (минуты от полуночи МСК): основная сессия и вечерняя; в конце окна — закрытие
SESSION_WINDOWS = ((10 * 60, 18 * 60 + 45), (19 * 60 + 5, 23 * 60 + 45))

CLOSE_REASONS_POLICY = ("STOP_LOSS", "TAKE_PROFIT", "TIMEOUT", "SESSION_END")


def msk_str(dt: datetime) -> str:
    return dt.astimezone(MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def session_deadline(now: datetime) -> Optional[datetime]:
    """Момент закрытия текущей сессии (МСК) или None, если сейчас вне окон жизни сделок (выходной не проверяется)."""
    n = now.astimezone(MSK)
    minutes = n.hour * 60 + n.minute
    for start, end in SESSION_WINDOWS:
        if start <= minutes < end:
            return n.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    return None


def net_pnl_pct(side: int, entry_fill: float, exit_fill: float, commission_pct: float = COMMISSION_PCT) -> float:
    """Чистый результат в % от цены входа: движение цены минус комиссия за вход и выход."""
    gross = side * (exit_fill - entry_fill) / entry_fill * 100.0
    return gross - 2.0 * commission_pct


def decide_label(pnl_pct: float) -> int:
    """1 — заработали после издержек, 0 — нет. Так же размечаются реальные сделки (pnl > 0)."""
    return 1 if pnl_pct > 0 else 0


@dataclass
class VTrade:
    figi: str
    ticker: str
    mode: str
    direction: str                    # BUY / SELL
    episode_id: str
    score: float
    opened_at: datetime
    deadline: datetime
    deadline_reason: str              # TIMEOUT / SESSION_END
    entry_mid: float
    entry_fill: float
    fill_src: str                     # book / mid
    spread_pct: Optional[float]
    sl_pct: float
    tp_pct: float
    commission_pct: float = COMMISSION_PCT
    filter_stage: str = ""
    ml_p: Optional[float] = None
    ml_decision: str = ""
    real_trade: bool = False
    ind_missing: bool = False
    feature_id: Optional[int] = None
    id: Optional[int] = None
    status: str = "OPEN"
    # состояние в ходе жизни
    hi: float = 0.0
    lo: float = 0.0
    last_price: float = 0.0
    bars: List[list] = field(default_factory=list)            # [минута_epoch, open, high, low, close]
    checkpoints: Dict[int, Optional[float]] = field(default_factory=lambda: {m: None for m in CHECKPOINTS_MIN})
    # итог
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    hold_min: Optional[float] = None
    label: Optional[int] = None

    @property
    def side(self) -> int:
        return 1 if self.direction == "BUY" else -1

    @property
    def sl_price(self) -> float:
        return self.entry_fill * (1 - self.side * self.sl_pct)

    @property
    def tp_price(self) -> float:
        return self.entry_fill * (1 + self.side * self.tp_pct)


class VirtualEngine:
    """Хранит открытые виртуальные сделки и эпизоды, обрабатывает тики цены. Потокобезопасен."""

    def __init__(self, commission_pct: float = COMMISSION_PCT, max_hold_min: int = MAX_HOLD_MIN,
                 episode_gap_sec: int = EPISODE_GAP_SEC):
        self.commission_pct = commission_pct
        self.max_hold_min = max_hold_min
        self.episode_gap_sec = episode_gap_sec
        self._lock = threading.RLock()
        self._open: Dict[str, List[VTrade]] = {}
        self._episodes: Dict[Tuple[str, str, str], dict] = {}
        self._closed: List[VTrade] = []
        self.stats = {"signals": 0, "episodes": 0, "opened": 0, "closed": 0, "skipped_cap": 0, "skipped_session": 0}

    # ── открытие ──────────────────────────────────────────────────────────────

    def claim(self, figi: str, mode: str, direction: str, now: datetime) -> Optional[str]:
        """Учитывает сигнал. Возвращает id эпизода, если по нему нужно открыть сделку, иначе None.

        Вызывать до построения признаков: так дорогие запросы (индикаторы API) делаются только для нового эпизода.
        """
        key = (figi, mode, direction)
        with self._lock:
            self.stats["signals"] += 1
            ep = self._episodes.get(key)
            if ep is None or (now - ep["last"]).total_seconds() > self.episode_gap_sec:
                ep = {"id": f"{figi}:{mode}:{direction}:{int(now.timestamp())}", "opened": False, "last": now}
                self._episodes[key] = ep
                self.stats["episodes"] += 1
            ep["last"] = now
            if ep["opened"]:
                return None
            deadline = session_deadline(now)
            if deadline is None or (deadline - now).total_seconds() < MIN_LIFETIME_MIN * 60:
                self.stats["skipped_session"] += 1
                return None            # эпизод остаётся «неоткрытым»: сделку можно открыть в начале следующей сессии
            if sum(len(v) for v in self._open.values()) >= MAX_OPEN_TOTAL:
                self.stats["skipped_cap"] += 1
                return None
            ep["opened"] = True
            return ep["id"]

    def open(self, *, figi: str, ticker: str, mode: str, direction: str, episode_id: str, score: float, now: datetime,
             mid: float, sl_pct: float, tp_pct: float, bid: Optional[float] = None, ask: Optional[float] = None,
             filter_stage: str = "", ml_p: Optional[float] = None, ml_decision: str = "", real_trade: bool = False,
             ind_missing: bool = False, feature_id: Optional[int] = None) -> VTrade:
        """Регистрирует сделку после claim(). Вход: BUY по ask, SELL по bid; нет стакана — по последней цене."""
        side = 1 if direction == "BUY" else -1
        book = bool(bid and ask and bid > 0 and ask > 0)
        fill = (ask if side == 1 else bid) if book else mid
        spread = (ask - bid) / mid * 100.0 if book and mid > 0 else None
        sess = session_deadline(now)
        hold_deadline = now + timedelta(minutes=self.max_hold_min)
        if sess is not None and sess < hold_deadline:
            deadline, reason = sess, "SESSION_END"
        else:
            deadline, reason = hold_deadline, "TIMEOUT"
        t = VTrade(figi=figi, ticker=ticker, mode=mode, direction=direction, episode_id=episode_id, score=score,
                   opened_at=now, deadline=deadline, deadline_reason=reason, entry_mid=mid, entry_fill=fill,
                   fill_src="book" if book else "mid", spread_pct=spread, sl_pct=sl_pct, tp_pct=tp_pct,
                   commission_pct=self.commission_pct, filter_stage=filter_stage, ml_p=ml_p, ml_decision=ml_decision,
                   real_trade=real_trade, ind_missing=ind_missing, feature_id=feature_id)
        t.hi = t.lo = t.last_price = fill
        with self._lock:
            self._open.setdefault(figi, []).append(t)
            self.stats["opened"] += 1
        return t

    # ── цены ──────────────────────────────────────────────────────────────────

    def on_tick(self, figi: str, price: float, now: datetime,
                bid: Optional[float] = None, ask: Optional[float] = None) -> None:
        """Тик цены по инструменту: обновляет экстремумы и закрывает сделки по SL/TP. Дёшево, без обращений к БД."""
        if price is None or price <= 0:
            return
        with self._lock:
            trades = self._open.get(figi)
            if not trades:
                return
            for t in list(trades):
                self._update(t, price, now, bid, ask)
                if t.status != "OPEN":
                    trades.remove(t)
            if not trades:
                self._open.pop(figi, None)

    def on_bar(self, figi: str, o: float, h: float, l: float, c: float, now: datetime) -> None:
        """Минутная свеча вместо тиков (историческая добивка). Порядок движения внутри свечи неизвестен, поэтому
        пессимистичный: для покупки сначала минимум (стоп), потом максимум (тейк); для продажи наоборот.
        Так при касании и стопа, и тейка в одной свече засчитывается стоп, и добивка не завышает долю прибыльных."""
        with self._lock:
            trades = self._open.get(figi)
            if not trades:
                return
            for t in list(trades):
                for price in ((o, l, h, c) if t.side == 1 else (o, h, l, c)):
                    if price and price > 0:
                        self._update(t, price, now, None, None)
                    if t.status != "OPEN":
                        break
                if t.status != "OPEN":
                    trades.remove(t)
            if not trades:
                self._open.pop(figi, None)

    def expire(self, now: datetime, prices: Optional[Dict[str, float]] = None) -> int:
        """Закрывает сделки, у которых наступил срок (таймаут или конец сессии). prices — последние цены по figi."""
        prices = prices or {}
        n = 0
        with self._lock:
            for figi in list(self._open):
                trades = self._open[figi]
                for t in list(trades):
                    if now >= t.deadline:
                        price = prices.get(figi) or t.last_price or t.entry_mid
                        self._close(t, t.deadline_reason, price, now, market=True)
                        trades.remove(t)
                        n += 1
                if not trades:
                    self._open.pop(figi, None)
        return n

    def drain_closed(self) -> List[VTrade]:
        with self._lock:
            out, self._closed = self._closed, []
            return out

    def open_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._open.values())

    def open_trades(self) -> List[VTrade]:
        with self._lock:
            return [t for v in self._open.values() for t in v]

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _update(self, t: VTrade, price: float, now: datetime, bid: Optional[float], ask: Optional[float]) -> None:
        t.last_price = price
        t.hi = max(t.hi, price)
        t.lo = min(t.lo, price)
        minute = int(now.timestamp() // 60)
        if t.bars and t.bars[-1][0] == minute:
            bar = t.bars[-1]
            bar[2], bar[3], bar[4] = max(bar[2], price), min(bar[3], price), price
        else:
            t.bars.append([minute, price, price, price, price])
        elapsed_min = (now - t.opened_at).total_seconds() / 60.0
        for cp in CHECKPOINTS_MIN:
            if t.checkpoints.get(cp) is None and elapsed_min >= cp:
                t.checkpoints[cp] = t.side * (price - t.entry_fill) / t.entry_fill * 100.0
        if t.side == 1:
            if price <= t.sl_price:
                self._close(t, "STOP_LOSS", price, now, market=True, bid=bid, ask=ask)
            elif price >= t.tp_price:
                self._close(t, "TAKE_PROFIT", t.tp_price, now, market=False)
        else:
            if price >= t.sl_price:
                self._close(t, "STOP_LOSS", price, now, market=True, bid=bid, ask=ask)
            elif price <= t.tp_price:
                self._close(t, "TAKE_PROFIT", t.tp_price, now, market=False)

    def _close(self, t: VTrade, reason: str, price: float, now: datetime, market: bool,
               bid: Optional[float] = None, ask: Optional[float] = None) -> None:
        exit_fill = price
        if market:
            # выход «по рынку»: BUY продаём по bid, SELL выкупаем по ask; нет стакана — половина спреда на входе
            if t.side == 1 and bid and bid > 0:
                exit_fill = bid
            elif t.side == -1 and ask and ask > 0:
                exit_fill = ask
            elif t.spread_pct:
                exit_fill = price * (1 - t.side * t.spread_pct / 200.0)
        t.status = "CLOSED"
        t.closed_at = now
        t.exit_price = exit_fill
        t.exit_reason = reason
        t.pnl_pct = net_pnl_pct(t.side, t.entry_fill, exit_fill, t.commission_pct)
        fav, adv = (t.hi, t.lo) if t.side == 1 else (t.lo, t.hi)
        t.mfe_pct = t.side * (fav - t.entry_fill) / t.entry_fill * 100.0
        t.mae_pct = t.side * (adv - t.entry_fill) / t.entry_fill * 100.0
        t.hold_min = (now - t.opened_at).total_seconds() / 60.0
        t.label = decide_label(t.pnl_pct)
        self._closed.append(t)
        self.stats["closed"] += 1


# ── хранение ──────────────────────────────────────────────────────────────────

def store_open(t: VTrade) -> int:
    """Записывает открытую сделку; возвращает её id."""
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, episode_id, signal_score,
                filter_stage, ml_p, ml_decision, real_trade, entry_mid, entry_price, fill_src, spread_pct,
                sl_pct, tp_pct, commission_pct, ind_missing, status, feature_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)
        """, (msk_str(t.opened_at), t.figi, t.ticker, t.mode, t.direction, t.episode_id, t.score, t.filter_stage,
              t.ml_p, t.ml_decision, int(t.real_trade), t.entry_mid, t.entry_fill, t.fill_src, t.spread_pct,
              t.sl_pct, t.tp_pct, t.commission_pct, int(t.ind_missing), t.feature_id))
        t.id = cur.lastrowid
        if t.feature_id:
            cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (t.id, t.feature_id))
    return t.id or 0


def store_closed(t: VTrade) -> None:
    """Записывает итог сделки, путь цены и метку в строку признаков."""
    from app.db import db_cursor
    if not t.id:
        return
    with db_cursor() as cur:
        cur.execute("""
            UPDATE ml_virtual_trades SET status='CLOSED', closed_at=?, exit_price=?, exit_reason=?, pnl_pct=?,
                mfe_pct=?, mae_pct=?, hold_min=?, ret_5m=?, ret_15m=?, ret_30m=?, ret_60m=?, label=?
            WHERE id=?
        """, (msk_str(t.closed_at), t.exit_price, t.exit_reason, t.pnl_pct, t.mfe_pct, t.mae_pct, t.hold_min,
              t.checkpoints.get(5), t.checkpoints.get(15), t.checkpoints.get(30), t.checkpoints.get(60),
              t.label, t.id))
        cur.execute("INSERT OR REPLACE INTO ml_virtual_paths(virtual_id, path_json) VALUES (?, ?)",
                    (t.id, json.dumps(t.bars, separators=(",", ":"))))
        if t.feature_id:
            cur.execute("UPDATE ml_features SET pnl=?, quality_score=?, label=?, exit_reason=? WHERE id=?",
                        (t.pnl_pct, t.pnl_pct / 100.0, t.label, t.exit_reason, t.feature_id))


def abort_open_on_start() -> int:
    """Сделки, оставшиеся OPEN после остановки бота, исход не получат: помечаем ABORTED (метки нет)."""
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE ml_virtual_trades SET status='ABORTED', closed_at=? WHERE status='OPEN'",
                    (msk_str(datetime.now(timezone.utc)),))
        return cur.rowcount or 0


def prune_paths(days: int = 30) -> int:
    """Удаляет поминутные пути закрытых сделок старше days дней (итоги и MFE/MAE остаются)."""
    from app.db import db_cursor
    cutoff = msk_str(datetime.now(timezone.utc) - timedelta(days=days))
    with db_cursor() as cur:
        cur.execute("""DELETE FROM ml_virtual_paths WHERE virtual_id IN
                       (SELECT id FROM ml_virtual_trades WHERE closed_at IS NOT NULL AND closed_at < ?)""", (cutoff,))
        return cur.rowcount or 0


_LIVE = "COALESCE(source, 'LIVE') = 'LIVE'"       # живые сделки; историческая добивка (BACKFILL) в срезах дашборда отдельно


def recent(limit: int = 100) -> list:
    """Последние живые виртуальные сделки для дашборда (новые сверху)."""
    from app.db import db_cursor
    limit = max(1, min(int(limit), 500))
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT id, created_at, ticker, strategy_mode, direction, signal_score, filter_stage, ml_p, ml_decision,
                   real_trade, entry_price, fill_src, sl_pct, tp_pct, status, closed_at, exit_price, exit_reason,
                   pnl_pct, mfe_pct, mae_pct, hold_min, label, ind_missing
            FROM ml_virtual_trades WHERE {_LIVE} ORDER BY id DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


_SUMMARY_COLS = """COUNT(*) n, COUNT(DISTINCT episode_id) episodes, SUM(status='CLOSED') closed,
    SUM(label=1) wins, SUM(label=0) losses,
    AVG(CASE WHEN status='CLOSED' THEN pnl_pct END) avg_pnl,
    AVG(CASE WHEN status='CLOSED' THEN mfe_pct END) avg_mfe,
    AVG(CASE WHEN status='CLOSED' THEN mae_pct END) avg_mae,
    AVG(CASE WHEN status='CLOSED' THEN hold_min END) avg_hold"""


def summary() -> dict:
    """Срезы виртуальных сделок: по режимам, причинам выхода, фильтрам, решению модели и бумагам."""
    from app.db import db_cursor

    def group(cur, expr: str, extra: str = "", limit: int = 50) -> list:
        where = f"WHERE {_LIVE}" + (f" AND {extra}" if extra else "")
        cur.execute(f"SELECT {expr} AS grp, {_SUMMARY_COLS} FROM ml_virtual_trades {where} "
                    f"GROUP BY grp ORDER BY n DESC LIMIT {int(limit)}")
        return [dict(r) for r in cur.fetchall()]

    with db_cursor() as cur:
        return {
            "by_mode": group(cur, "strategy_mode"),
            "by_reason": group(cur, "exit_reason", "status='CLOSED'"),
            # прошёл ли сигнал фильтры реальной торговли: ответ на вопрос «не отсекаем ли мы прибыльное»
            "by_filter": group(cur, "CASE WHEN filter_stage='' THEN 'passed' ELSE filter_stage END"),
            # что думала модель: p_ok / p_low / '' (модели не было) — «вето было право?»
            "by_ml": group(cur, "CASE WHEN ml_decision='' THEN 'none' ELSE ml_decision END"),
            "by_ticker": group(cur, "ticker", "", 20),
        }


def counters() -> dict:
    """Сводка для дашборда: сколько живых виртуальных сделок, эпизодов и меток, и отдельно историческая добивка."""
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute(f"""SELECT COUNT(*) n, SUM(status='OPEN') open_n, SUM(status='CLOSED') closed_n,
                               SUM(label=1) wins, SUM(label=0) losses, COUNT(DISTINCT episode_id) episodes
                        FROM ml_virtual_trades WHERE {_LIVE}""")
        r = cur.fetchone()
        out = {k: (r[k] or 0) for k in r.keys()}
        cur.execute("""SELECT COUNT(*) n, SUM(label=1) wins, SUM(label=0) losses, MIN(created_at) first, MAX(created_at) last
                       FROM ml_virtual_trades WHERE source = 'BACKFILL'""")
        b = cur.fetchone()
        out.update(backfill_n=b["n"] or 0, backfill_wins=b["wins"] or 0, backfill_losses=b["losses"] or 0,
                   backfill_first=b["first"], backfill_last=b["last"])
        return out


def store_backfill_batch(items: List[Tuple["VTrade", dict]]) -> int:
    """Пакетная запись закрытых сделок исторической добивки: признаки + сделка + путь, одной транзакцией.

    Повторный запуск не плодит дубликаты: эпизод с таким episode_id и source='BACKFILL' пропускается.
    Возвращает число записанных сделок.
    """
    from app.db import db_cursor
    written = 0
    with db_cursor() as cur:
        for t, features in items:
            cur.execute("SELECT 1 FROM ml_virtual_trades WHERE episode_id=? AND source='BACKFILL' LIMIT 1",
                        (t.episode_id,))
            if cur.fetchone():
                continue
            cols = list(features.keys())
            cur.execute(
                f"INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, trade_id, source, strategy_mode, "
                f"pnl, quality_score, label, exit_reason, {','.join(cols)}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,{','.join('?' * len(cols))})",
                [t.figi, t.ticker, 0, msk_str(t.opened_at), 0, "BACKFILL", t.mode, t.pnl_pct,
                 (t.pnl_pct or 0.0) / 100.0, t.label, t.exit_reason] + [features[c] for c in cols])
            fid = cur.lastrowid
            cur.execute("""
                INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, episode_id,
                    signal_score, filter_stage, ml_p, ml_decision, real_trade, entry_mid, entry_price, fill_src,
                    spread_pct, sl_pct, tp_pct, commission_pct, ind_missing, status, closed_at, exit_price,
                    exit_reason, pnl_pct, mfe_pct, mae_pct, hold_min, ret_5m, ret_15m, ret_30m, ret_60m, label,
                    feature_id, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CLOSED',?,?,?,?,?,?,?,?,?,?,?,?,?,'BACKFILL')
            """, (msk_str(t.opened_at), t.figi, t.ticker, t.mode, t.direction, t.episode_id, t.score, t.filter_stage,
                  t.ml_p, t.ml_decision, int(t.real_trade), t.entry_mid, t.entry_fill, t.fill_src, t.spread_pct,
                  t.sl_pct, t.tp_pct, t.commission_pct, int(t.ind_missing), msk_str(t.closed_at), t.exit_price,
                  t.exit_reason, t.pnl_pct, t.mfe_pct, t.mae_pct, t.hold_min, t.checkpoints.get(5),
                  t.checkpoints.get(15), t.checkpoints.get(30), t.checkpoints.get(60), t.label, fid))
            vid = cur.lastrowid
            cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (vid, fid))
            cur.execute("INSERT OR REPLACE INTO ml_virtual_paths(virtual_id, path_json) VALUES (?, ?)",
                        (vid, json.dumps(t.bars, separators=(",", ":"))))
            written += 1
    return written
