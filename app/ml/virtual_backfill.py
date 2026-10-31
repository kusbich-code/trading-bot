"""Историческая добивка виртуальных сделок: проигрыш минутных свечей теми же правилами, что и живой сканер.

Зачем. Живой контур даёт десятки эпизодов в день; история даёт тысячи эпизодов сразу, за разные дни и режимы рынка.
Модель учится на признаках «в момент сигнала» и исходе, поэтому важно, чтобы признаки и исходы считались так же,
как в бою, и не знали будущего.

Как повторяются боевые условия:
  • сигналы считаются по завершённым 1-минутным свечам за последние 55 минут (как get_candles(n=50) в боевом пути);
  • эпизоды, вход по ask/bid, комиссия, SL/TP, таймаут и конец сессии — тот же VirtualEngine, что и в живом контуре;
  • RSI/MACD/BB считаются локально по свечам за последние 3 часа. Формулы проверены на сервере: совпадают со значениями
    GetTechAnalysis до 5-го знака (так что запросы к API для индикаторов не нужны);
  • часовые и 4-часовые свечи собираются из минутных теми же окнами (96 и 160 часов), что и боевой multi_timeframe;
  • время суток, фаза сессии и риск переноса шорта считаются на момент сигнала (as_of), корреляция с SBER/GAZP —
    по свечам этих бумаг на тот же момент.

Чего в истории нет и как это учтено:
  • стакана: bid_pressure, book_depth_ratio и spread_pct пишутся как NULL (а не нейтральными значениями);
  • спреда: вход и выход считаются с фиксированным спредом SPREAD_PCT, строки помечены fill_src='synthetic';
  • порядка движения цены внутри свечи: берётся худший для сделки (стоп раньше тейка), см. VirtualEngine.on_bar.
"""
import logging
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from app.ml import virtual_trades as vt
from app.ml.feature_builder import build_features, update_sector_returns
from app.ml.multi_timeframe import _aggregate
from app.services.strategy_engine import evaluate_signal

log = logging.getLogger("ml.virtual_backfill")

MODES = ("trend", "mean_reversion", "breakout")
WINDOW_MIN = 55              # окно свечей для сигнала: столько минут назад от момента сигнала
MIN_CANDLES = 25
INDICATOR_WINDOW_H = 3       # боевой запрос индикаторов берёт последние 3 часа
SPREAD_PCT = 0.05            # спред добивки, % от цены (стакана в истории нет)
WARMUP_DAYS = 8              # истории до первой точки нужно не меньше 160 часов для 4-часовых признаков
DEFAULT_SL = 0.008
DEFAULT_TP = 0.015
ORDERBOOK_FEATURES = ("bid_pressure", "book_depth_ratio", "spread_pct")   # в истории неизвестны → NULL

UTC = timezone.utc


@dataclass
class Series:
    """Минутные свечи одной бумаги: по возрастанию времени, только завершённые."""
    figi: str
    ticker: str
    times: List[datetime] = field(default_factory=list)      # начало минуты, UTC
    dicts: List[dict] = field(default_factory=list)          # формат strategy_engine / build_features
    ohlc: List[Tuple[float, float, float, float]] = field(default_factory=list)


def _as_utc(t) -> Optional[datetime]:
    if isinstance(t, str):
        try:
            t = datetime.fromisoformat(t)
        except ValueError:
            return None
    if not isinstance(t, datetime):
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def make_series(figi: str, ticker: str, raw: List[dict]) -> Series:
    """Нормализует свечи из API (dict с time/open/high/low/close/volume/is_complete): сортировка, без дублей и неполных."""
    seen = set()
    rows = []
    for c in raw:
        if c.get("is_complete", True) is False:
            continue
        t = _as_utc(c.get("time"))
        o, h, l, cl = (float(c.get(k) or 0) for k in ("open", "high", "low", "close"))
        if t is None or t in seen or min(o, h, l, cl) <= 0:
            continue
        seen.add(t)
        rows.append((t, o, h, l, cl, int(c.get("volume") or 0)))
    rows.sort(key=lambda r: r[0])
    s = Series(figi=figi, ticker=ticker)
    for t, o, h, l, cl, v in rows:
        s.times.append(t)
        s.ohlc.append((o, h, l, cl))
        s.dicts.append({"open": o, "high": h, "low": l, "close": cl, "volume": v, "time": t.isoformat()})
    return s


# ── индикаторы: те же формулы, что у GetTechAnalysis (проверено на сервере) ───────────────

def rsi_wilder(closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
    return 100.0 - 100.0 / (1 + ag / al) if al else 100.0


def macd_last(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Tuple[Optional[float], Optional[float]]:
    if len(closes) < slow + sig:
        return None, None
    kf, ks, kg = 2 / (fast + 1), 2 / (slow + 1), 2 / (sig + 1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef, es = c * kf + ef * (1 - kf), c * ks + es * (1 - ks)
        line.append(ef - es)
    s = line[0]
    for m in line[1:]:
        s = m * kg + s * (1 - kg)
    return line[-1], s


def bollinger_last(closes: List[float], n: int = 20, mult: float = 2.0) -> Tuple[Optional[float], Optional[float]]:
    if len(closes) < n:
        return None, None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((c - m) ** 2 for c in w) / n) ** 0.5
    return m + mult * sd, m - mult * sd


def indicators_from_closes(closes: List[float]) -> dict:
    macd, macd_sig = macd_last(closes)
    up, lo = bollinger_last(closes)
    return {"rsi": rsi_wilder(closes), "macd": macd, "macd_signal": macd_sig, "bb_upper": up, "bb_lower": lo}


# ── проигрыш ─────────────────────────────────────────────────────────────────

def _hour_start(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _hourly_bar(start: datetime, o, h, l, c, v) -> dict:
    return {"time": start.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": v, "_t": start}


def _window_dicts(s: Series, t_close: datetime, hi: Optional[int] = None) -> List[dict]:
    """Свечи за последние WINDOW_MIN минут до момента сигнала (включая последнюю завершённую)."""
    hi = len(s.times) if hi is None else hi
    lo = bisect_left(s.times, t_close - timedelta(minutes=WINDOW_MIN), 0, hi)
    return s.dicts[lo:hi]


def _set_sector_returns(markers: Dict[str, Series], own_figi: str, t_close: datetime) -> None:
    """Подкладывает в кеш корреляции доходности SBER/GAZP на тот же момент, что и сигнал."""
    for mfigi, ms in markers.items():
        if mfigi == own_figi:
            continue
        hi = bisect_left(ms.times, t_close)          # свечи маркера, начавшиеся до момента сигнала
        win = _window_dicts(ms, t_close, hi)
        closes = [d["close"] for d in win]
        if len(closes) >= 6:
            rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
            update_sector_returns(mfigi, rets)


def replay_instrument(item: dict, series: Series, markers: Optional[Dict[str, Series]] = None, *,
                      start: datetime, end: Optional[datetime] = None, spread_pct: float = SPREAD_PCT,
                      include_open: bool = False, keep_paths: bool = False, hold_until_exit: bool = True,
                      build: Callable[..., dict] = build_features) -> List[Tuple[vt.VTrade, dict]]:
    """Проигрывает историю одной бумаги. start/end — границы моментов сигнала (UTC); свечи до start служат разогревом.

    item: {"figi", "ticker", "levels": {режим: (sl, tp)}, "min_scores": {режим: порог}} — как у живого сканера.
    Возвращает пары (закрытая сделка, признаки); сделки, не успевшие закрыться к концу данных, отбрасываются
    (include_open=True добавляет их — для проверки отсутствия заглядывания в будущее).
    Сделки живут до SL или TP, без ограничения по времени (нет таймаута, нет конца сессии). Шорты не открываются.
    hold_until_exit оставлен для совместимости — все сделки теперь живут до SL/TP.
    """
    markers = markers or {}
    figi, ticker = item["figi"], item.get("ticker", "")
    engine = vt.VirtualEngine()
    out: List[Tuple[vt.VTrade, dict]] = []
    pending: Dict[str, dict] = {}
    hourly: List[dict] = []                      # завершённые часы
    cur: Optional[list] = None                   # формирующийся час: [start, o, h, l, c, v]

    for i, t_open in enumerate(series.times):
        o, h, l, c = series.ohlc[i]
        v = series.dicts[i]["volume"]
        t_close = t_open + timedelta(minutes=1)

        # 1) бар проживается открытыми сделками, затем закрытие шортов по концу суток
        engine.on_bar(figi, o, h, l, c, t_open + timedelta(seconds=30))
        engine.expire(t_close, {figi: c})
        for t in engine.drain_closed():
            if not keep_paths:
                t.bars = []
            out.append((t, pending.pop(t.episode_id)))

        # 2) часовые свечи (в боевом пути последняя — формирующаяся, поэтому текущий час включается как есть)
        hs = _hour_start(t_open)
        if cur is None or cur[0] != hs:
            if cur is not None:
                hourly.append(_hourly_bar(*cur))
            cur = [hs, o, h, l, c, v]
        else:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], h), min(cur[3], l), c, cur[5] + v

        # 3) новые сигналы
        if t_close < start or (end is not None and t_close > end):
            continue
        window = _window_dicts(series, t_close, i + 1)
        if len(window) < MIN_CANDLES:
            continue
        for mode in MODES:
            res = evaluate_signal(mode, window)
            action = res.get("action")
            if action != "BUY":
                continue            # шорты не открываем вообще
            ep = engine.claim(figi, mode, action, t_close)
            if not ep:
                continue
            score = float(res.get("score", 0))
            # признаки на момент сигнала: только данные до t_close
            closes_3h = [d["close"] for d in series.dicts[bisect_left(series.times, t_close - timedelta(hours=INDICATOR_WINDOW_H), 0, i + 1):i + 1]]
            ind = indicators_from_closes(closes_3h)
            hourly_now = hourly + [_hourly_bar(*cur)]
            h96 = [b for b in hourly_now if b["_t"] > t_close - timedelta(hours=96)][-30:]
            h160 = [b for b in hourly_now if b["_t"] > t_close - timedelta(hours=160)]
            _set_sector_returns(markers, figi, t_close)
            feats = build(figi, ticker, window, score, action, ind, {}, h96, _aggregate(h160, 4)[-20:], as_of=t_close)
            for k in ORDERBOOK_FEATURES:
                feats[k] = None
            sl, tp = (item.get("levels") or {}).get(mode, (DEFAULT_SL, DEFAULT_TP))
            ms = (item.get("min_scores") or {}).get(mode)
            half = spread_pct / 200.0
            t = engine.open(figi=figi, ticker=ticker, mode=mode, direction=action, episode_id=ep, score=score,
                            now=t_close, mid=c, bid=c * (1 - half), ask=c * (1 + half), sl_pct=sl, tp_pct=tp,
                            filter_stage="min_score" if ms and score < ms else "",
                            ind_missing=any(ind.get(k) is None for k in ("rsi", "macd", "bb_upper")))
            t.fill_src = "synthetic"
            pending[ep] = feats

    if include_open:
        for t in engine.open_trades():
            out.append((t, pending[t.episode_id]))
    return out
