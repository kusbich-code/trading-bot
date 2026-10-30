"""CandleCache и StreamSubscriptions: поведение на границах минуты и объединение подписок."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.market_cache import CandleCache, StreamSubscriptions

UTC = timezone.utc
BASE = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)   # начало минуты 10:05:00


class Clock:
    def __init__(self, start):
        self.t = start
        self.mono = 0.0

    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


def candle(minute_start, complete=True):
    return SimpleNamespace(time=minute_start, is_complete=complete)


class Fetcher:
    """Имитация API: к моменту t отдаёт завершённые свечи до lag секунд назад, плюс формирующуюся."""

    def __init__(self, clock, skip_minutes=(), lag=0):
        self.clock, self.calls, self.skip, self.lag = clock, 0, set(skip_minutes), lag

    def __call__(self):
        self.calls += 1
        now = self.clock.now() - timedelta(seconds=self.lag)
        cur_min = now.replace(second=0, microsecond=0)
        out = []
        for i in range(5, 0, -1):
            m = cur_min - timedelta(minutes=i)
            if m not in self.skip:
                out.append(candle(m))
        out.append(candle(cur_min, complete=False))
        return out


def make(skip=(), lag=0):
    clock = Clock(BASE + timedelta(seconds=20))
    cache = CandleCache(now_fn=clock.now, mono_fn=clock.monotonic)
    return clock, cache, Fetcher(clock, skip, lag)


def test_second_call_in_same_minute_uses_cache():
    clock, cache, fetch = make()
    first = cache.get("SBER", fetch)
    clock.advance(5)
    second = cache.get("SBER", fetch)
    assert fetch.calls == 1 and second is first


def test_refetch_in_next_minute():
    clock, cache, fetch = make()
    cache.get("SBER", fetch)
    clock.advance(45)               # 10:06:05
    cache.get("SBER", fetch)
    assert fetch.calls == 2


def test_cache_is_per_key():
    clock, cache, fetch = make()
    cache.get(("A", 50), fetch)
    cache.get(("B", 50), fetch)
    assert fetch.calls == 2


def test_late_candle_is_retried_quickly_and_then_cached():
    # свеча минуты 10:05 публикуется с задержкой 8 с, то есть в 10:06:08
    clock = Clock(BASE + timedelta(seconds=61))
    cache = CandleCache(now_fn=clock.now, mono_fn=clock.monotonic)
    fetch = Fetcher(clock, lag=8)
    cache.get("SBER", fetch)                        # 10:06:01 — последней завершённой нет
    clock.advance(1)
    cache.get("SBER", fetch)                        # 10:06:02 — слишком рано для повтора
    assert fetch.calls == 1
    clock.advance(3)                                # 10:06:05 — повтор, свечи всё ещё нет
    cache.get("SBER", fetch)
    assert fetch.calls == 2
    clock.advance(3)                                # 10:06:08 — свеча появилась
    candles = cache.get("SBER", fetch)
    assert fetch.calls == 3
    assert [c for c in candles if c.is_complete][-1].time == BASE
    clock.advance(10)                               # 10:06:18 — теперь кеш держится до конца минуты
    cache.get("SBER", fetch)
    assert fetch.calls == 3


def test_illiquid_instrument_without_candle_is_cached_after_late_window():
    # в минуту 10:05 сделок не было — свечи нет и не будет
    clock = Clock(BASE + timedelta(seconds=60 + 20))   # 10:06:20, вне окна ожидания
    cache = CandleCache(now_fn=clock.now, mono_fn=clock.monotonic)
    fetch = Fetcher(clock, skip_minutes=[BASE])
    cache.get("VTBR", fetch)
    clock.advance(5)
    cache.get("VTBR", fetch)
    assert fetch.calls == 1                        # не долбим API каждые 5 с


def test_exception_is_not_cached():
    clock, cache, _ = make()

    def boom():
        raise RuntimeError("API")

    with pytest.raises(RuntimeError):
        cache.get("SBER", boom)
    fetch = Fetcher(clock)
    cache.get("SBER", fetch)
    assert fetch.calls == 1


def test_naive_or_missing_time_does_not_crash():
    clock = Clock(BASE + timedelta(seconds=30))
    cache = CandleCache(now_fn=clock.now, mono_fn=clock.monotonic)
    candles = [SimpleNamespace(time=None, is_complete=True), SimpleNamespace(time=BASE.replace(tzinfo=None), is_complete=True)]
    assert cache.get("X", lambda: candles) is candles


def test_subscriptions_union_and_change_flags():
    subs = StreamSubscriptions()
    assert subs.set_main([{"figi": "A", "instrument_uid": "ua"}]) is True
    assert subs.set_main([{"figi": "A", "instrument_uid": "ua"}]) is False         # без изменений
    assert subs.set_extra(7, [{"figi": "B", "instrument_uid": "ub"}]) is True
    assert subs.set_extra(7, [{"figi": "B", "instrument_uid": "ub"}]) is False
    assert subs.set_extra(9, [{"figi": "A"}, {"figi": "C"}]) is True               # C новый, A уже был
    assert {i["figi"] for i in subs.snapshot()} == {"A", "B", "C"}


def test_subscriptions_owner_removal_and_main_replacement():
    subs = StreamSubscriptions()
    subs.set_main([{"figi": "A"}])
    subs.set_extra(7, [{"figi": "B"}])
    assert subs.set_extra(7, []) is True                                           # владелец убрал инструменты
    assert {i["figi"] for i in subs.snapshot()} == {"A"}
    assert subs.set_main([]) is True
    assert subs.snapshot() == []


def test_empty_uid_does_not_overwrite_known_uid():
    subs = StreamSubscriptions()
    subs.set_extra(7, [{"figi": "A", "instrument_uid": "ua"}])
    subs.set_main([{"figi": "A", "instrument_uid": ""}])
    assert subs.snapshot() == [{"figi": "A", "instrument_uid": "ua"}]
