"""Когда сканер виртуальных сделок работает вне обычной сессии: по настройке владельца и только по бумагам, которые брокер даёт торговать."""
import pytest

from app.ml import scan_gate as sg


@pytest.mark.parametrize("in_session,session_only,expected", [
    (True, True, "session"), (True, False, "session"),
    (False, True, "off"), (False, False, "off_hours"),
])
def test_mode(in_session, session_only, expected):
    assert sg.scan_mode(in_session, session_only) == expected


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_status_is_cached_and_refreshed():
    calls = []
    clock = Clock()
    cache = sg.TradableCache(lambda figi: calls.append(figi) or True, ttl=300, clock=clock)
    assert cache.tradable("F1") is True and cache.tradable("F1") is True
    assert calls == ["F1"]
    clock.t += 301
    cache.tradable("F1")
    assert calls == ["F1", "F1"]


def test_failed_check_keeps_last_known_or_says_no():
    clock = Clock()
    answers = iter([True])

    def fetch(figi):
        return next(answers)                # второй вызов падает: брокер не ответил

    cache = sg.TradableCache(fetch, ttl=10, clock=clock)
    assert cache.tradable("F1") is True
    clock.t += 11
    assert cache.tradable("F1") is True    # прошлое известное значение
    assert sg.TradableCache(lambda f: (_ for _ in ()).throw(RuntimeError("нет связи")), clock=clock).tradable("F2") is False


def test_without_budget_no_request_is_made():
    calls = []
    cache = sg.TradableCache(lambda figi: calls.append(figi) or True, clock=Clock())
    assert cache.tradable("F1", can_request=False) is False and calls == []


def test_filter_universe_only_off_hours():
    cache = sg.TradableCache(lambda figi: figi == "OPEN", clock=Clock())
    items = [{"figi": "OPEN"}, {"figi": "CLOSED"}]
    assert sg.filter_universe(items, "session", cache) == items
    assert sg.filter_universe(items, "off_hours", cache) == [{"figi": "OPEN"}]
    assert sg.filter_universe(items, "off", cache) == []


def test_owner_setting_is_read_from_active_profile(tmp_db):
    assert sg.session_only() is True                                    # по умолчанию только сессия
    tmp_db.set_setting("active_profile_id", "1")
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT OR REPLACE INTO profile_settings(profile_id, key, value) VALUES (1, 'trade_only_session', '0')")
    assert sg.session_only() is False
