"""Время торговли: одна разметка для тренера, проверки в тени и исследований."""
import calendar
from datetime import datetime

import pytest

from app.ml import sessions as ss


@pytest.mark.parametrize("ts,key", [
    ("2026-11-09 10:00:00", "day"), ("2026-11-09 18:39:00", "day"), ("2026-11-09 18:45:00", "other"),
    ("2026-11-09 19:00:00", "evening_open"), ("2026-11-09 19:29:00", "evening_open"), ("2026-11-09 19:30:00", "evening"),
    ("2026-11-09 23:49:00", "evening"), ("2026-11-09 23:55:00", "dealer"), ("2026-11-09 07:00:00", "morning"),
    ("2026-11-10 03:00:00", "dealer"), ("2026-11-07 12:00:00", "weekend_exchange"), ("2026-11-07 18:59:00", "weekend_exchange"),
    ("2026-11-07 19:00:00", "dealer"), ("2026-11-08 02:30:00", "dealer"), ("2026-11-08 09:59:00", "dealer"),
])
def test_key_of_str(ts, key):
    assert ss.key_of_str(ts) == key


@pytest.mark.parametrize("ts", ["2026-11-09 19:10:00", "2026-11-08 02:30:00", "2026-11-07 12:00:00", "2026-11-10 07:00:00"])
def test_epoch_matches_string(ts):
    """Тренер получает время как секунды из SQLite: МСК без пояса, прочитанное как UTC."""
    epoch = calendar.timegm(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timetuple())
    assert ss.key_of_epoch(epoch) == ss.key_of_str(ts)


def test_dealer_is_not_exchange():
    assert "dealer" not in ss.EXCHANGE and "day" in ss.EXCHANGE and set(ss.EXCHANGE) | {"dealer"} == set(ss.KEYS)
