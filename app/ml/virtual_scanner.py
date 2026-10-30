"""Сканер сигналов для виртуальных сделок.

Раз в минуту (когда у бумаги появляется новая завершённая свеча) считает все три режима стратегии
(trend / mean_reversion / breakout) по тем же свечам, что и боевые воркеры, и на каждый новый эпизод сигнала
открывает виртуальную сделку. Фильтры реальной торговли, лимиты позиций и решения модели на это не влияют,
иначе выборка снова сузится до того, что модель сама одобрила.

Внешние зависимости (свечи, цены, индикаторы API, признаки, БД) передаются снаружи: модуль не знает про SDK
брокера и main.py, поэтому проверяется тестами. Запросы к API делают только эти зависимости, а лимит на них
(не больше половины лимита API и приоритет реальной торговли) соблюдает main.py.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from app.ml import virtual_trades as vt
from app.services.strategy_engine import evaluate_signal

log = logging.getLogger("ml.virtual_scanner")

MODES = ("trend", "mean_reversion", "breakout")
MIN_CANDLES = 25                      # столько завершённых свечей нужно, чтобы режимы вообще считались
MAX_BAR_AGE_MIN = 5                   # сигнал по свече старше этого возраста (малоликвидная бумага) не берём
DEFAULT_SL = 0.008                    # SL/TP по умолчанию (доли от цены входа), если для режима нет настроенной стратегии
DEFAULT_TP = 0.015


class SkipInstrument(Exception):
    """Бумагу в этом проходе пропускаем без ошибки (например, исчерпан бюджет запросов к API)."""


class VirtualScanner:
    def __init__(self, engine: vt.VirtualEngine, *,
                 universe: Callable[[], List[dict]],
                 candles: Callable[[dict], list],
                 candles_to_dicts: Callable[[list], list],
                 quote: Callable[[str], Optional[dict]],
                 indicators: Callable[[dict], Optional[dict]],
                 timeframes: Callable[[dict], dict],
                 build_features: Callable[..., dict],
                 save_features: Callable[[dict, dict, str], int],
                 predict: Callable[[str, dict], Optional[float]],
                 has_real_position: Callable[[str], bool],
                 now: Optional[Callable[[], datetime]] = None):
        self.engine = engine
        self._universe = universe
        self._candles = candles
        self._to_dicts = candles_to_dicts
        self._quote = quote
        self._indicators = indicators
        self._timeframes = timeframes
        self._build_features = build_features
        self._save_features = save_features
        self._predict = predict
        self._has_real_position = has_real_position
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_bar: Dict[str, datetime] = {}
        self.errors = 0

    # ── основной проход ───────────────────────────────────────────────────────

    def scan_once(self, scan: bool = True) -> None:
        """Один проход. scan=False — только закрытие по сроку и запись итогов (вне торговой сессии)."""
        now = self._now()
        if scan:
            for item in self._universe():
                try:
                    self._scan_item(item, now)
                except SkipInstrument as e:
                    log.debug("виртуальный сканер %s пропущен: %s", item.get("ticker", "?"), e)
                except Exception as e:
                    self.errors += 1
                    log.warning("виртуальный сканер %s: %s", item.get("ticker", "?"), e)
        self._expire_and_persist(now)

    def _scan_item(self, item: dict, now: datetime) -> None:
        figi = item["figi"]
        candles = self._candles(item)
        complete = [c for c in candles if getattr(c, "is_complete", True)]
        if len(complete) < MIN_CANDLES:
            return
        last_t = getattr(complete[-1], "time", None)
        if last_t is None or self._last_bar.get(figi) == last_t:
            return                                   # новой завершённой свечи ещё нет
        self._last_bar[figi] = last_t
        if now - last_t > timedelta(minutes=MAX_BAR_AGE_MIN):
            return                                   # данные устарели: сигнал не отражает текущий рынок
        cd = self._to_dicts(complete)
        for mode in MODES:
            res = evaluate_signal(mode, cd)
            if res.get("action") in ("BUY", "SELL"):
                try:
                    self._handle_signal(item, mode, res["action"], float(res.get("score", 0)), cd, now)
                except Exception as e:
                    self.errors += 1
                    log.warning("виртуальный сигнал %s %s %s: %s", item.get("ticker"), mode, res["action"], e)

    def _handle_signal(self, item: dict, mode: str, action: str, score: float, cd: list, now: datetime) -> None:
        figi, ticker = item["figi"], item.get("ticker", "")
        ep = self.engine.claim(figi, mode, action, now)
        if not ep:
            return
        q = self._quote(figi) or {}
        mid = q.get("price") or float(cd[-1]["close"])
        sl, tp = (item.get("levels") or {}).get(mode, (DEFAULT_SL, DEFAULT_TP))

        ind = self._indicators(item)
        ind_missing = not ind or all(ind.get(k) is None for k in ("rsi", "macd", "bb_upper"))
        tf = self._timeframes(item) or {}
        ob = {"bid_vol": q.get("bid_vol", 0), "ask_vol": q.get("ask_vol", 0)}   # так же, как в боевом пути
        features = self._build_features(figi, ticker, cd, score, action, ind or {}, ob,
                                        tf.get("1hour", []), tf.get("4hour", []))
        feature_id = self._save_features(item, features, mode)

        p = None
        try:
            p = self._predict(figi, features)
        except Exception as e:
            log.debug("virtual predict %s: %s", ticker, e)
        min_score = (item.get("min_scores") or {}).get(mode)
        stage = "min_score" if min_score and score < min_score else ""
        decision = "" if p is None else ("p_ok" if p >= 0.6 else "p_low")

        t = self.engine.open(figi=figi, ticker=ticker, mode=mode, direction=action, episode_id=ep, score=score,
                             now=now, mid=mid, bid=q.get("bid"), ask=q.get("ask"), sl_pct=sl, tp_pct=tp,
                             filter_stage=stage, ml_p=p, ml_decision=decision,
                             real_trade=bool(self._has_real_position(figi)), ind_missing=ind_missing,
                             feature_id=feature_id or None)
        vt.store_open(t)

    def _expire_and_persist(self, now: datetime) -> None:
        open_trades = self.engine.open_trades()
        if open_trades:
            prices = {}
            for figi in {t.figi for t in open_trades}:
                q = self._quote(figi)
                if q and q.get("price"):
                    prices[figi] = q["price"]
            self.engine.expire(now, prices)
        for t in self.engine.drain_closed():
            try:
                vt.store_closed(t)
            except Exception as e:
                self.errors += 1
                log.warning("запись итога виртуальной сделки %s: %s", t.id, e)
