"""Кеши рыночных данных, не зависящие от SDK брокера (поэтому тестируются отдельно от main.py).

CandleCache — минутные свечи. Новая завершённая свеча появляется раз в минуту, а воркеры стратегий
опрашивают инструмент каждые ~5 с: без кеша это ~12 одинаковых запросов GetCandles в минуту на бумагу.

StreamSubscriptions — какие инструменты слушает MarketDataStream: инструменты основного цикла плюс
инструменты параллельных воркеров. Раньше в подписку попадали только инструменты основного цикла, и воркеры
получали цену и стакан запросами REST (а стакан по REST давал нулевые объёмы).
"""
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

# Только что закрывшаяся свеча публикуется биржей с задержкой в несколько секунд. Пока её нет, в первые
# LATE_WINDOW_SEC секунд новой минуты перезапрашиваем каждые RETRY_SEC. Если свечи нет и позже —
# в эту минуту сделок не было (так бывает у малоликвидных бумаг), ждём следующей минуты.
LATE_WINDOW_SEC = 15
RETRY_SEC = 3.0

_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _candle_time(candle: Any) -> datetime:
    t = getattr(candle, "time", None)
    if not isinstance(t, datetime):
        return _MIN_TIME
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class CandleCache:
    """Кеш минутных свечей: запрос повторяется не чаще раза в минуту на ключ (инструмент, число свечей)."""

    def __init__(self, now_fn: Optional[Callable[[], datetime]] = None,
                 mono_fn: Optional[Callable[[], float]] = None):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._mono = mono_fn or time.monotonic
        self._data: Dict[Any, tuple] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any, fetch: Callable[[], list]) -> list:
        """Возвращает свечи из кеша или результат fetch(). Исключения fetch() не кешируются."""
        ts = self._now().timestamp()
        bucket = int(ts // 60)
        with self._lock:
            hit = self._data.get(key)
        if hit:
            h_bucket, h_mono, h_candles, h_final = hit
            if h_bucket == bucket and (h_final or self._mono() - h_mono < RETRY_SEC):
                self.hits += 1
                return h_candles
        self.misses += 1
        candles = fetch()
        expected = datetime.fromtimestamp((bucket - 1) * 60, tz=timezone.utc)  # начало только что закрытой минуты
        complete = [c for c in candles if getattr(c, "is_complete", True)]
        have_last = bool(complete) and _candle_time(complete[-1]) >= expected
        final = have_last or (ts % 60) >= LATE_WINDOW_SEC
        with self._lock:
            self._data[key] = (bucket, self._mono(), candles, final)
        return candles


class StreamSubscriptions:
    """Объединение инструментов для подписки стрима. Методы возвращают True, если набор figi изменился."""

    def __init__(self):
        self._lock = threading.Lock()
        self._main: Dict[str, dict] = {}
        self._extra: Dict[Any, Dict[str, dict]] = {}

    @staticmethod
    def _norm(items: Iterable[dict]) -> Dict[str, dict]:
        return {i["figi"]: {"figi": i["figi"], "instrument_uid": i.get("instrument_uid", "") or ""}
                for i in items if i.get("figi")}

    def _figis(self) -> set:
        out = set(self._main)
        for d in self._extra.values():
            out |= set(d)
        return out

    def set_main(self, items: Iterable[dict]) -> bool:
        """Инструменты основного цикла (заменяют прежние)."""
        with self._lock:
            before = self._figis()
            self._main = self._norm(items)
            return self._figis() != before

    def set_extra(self, owner: Any, items: Iterable[dict]) -> bool:
        """Инструменты одного владельца (например, стратегии параллельного режима); пустой список убирает владельца."""
        with self._lock:
            before = self._figis()
            norm = self._norm(items)
            if norm:
                self._extra[owner] = norm
            else:
                self._extra.pop(owner, None)
            return self._figis() != before

    def snapshot(self) -> List[dict]:
        with self._lock:
            merged: Dict[str, dict] = {}
            for d in list(self._extra.values()) + [self._main]:
                for figi, item in d.items():
                    # пустой instrument_uid не должен затирать непустой из другого источника
                    if figi in merged and merged[figi]["instrument_uid"] and not item["instrument_uid"]:
                        continue
                    merged[figi] = item
            return list(merged.values())
