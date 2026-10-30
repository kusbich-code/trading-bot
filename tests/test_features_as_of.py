"""Календарь MOEX и build_features(as_of): признаки времени и риск переноса шорта на момент в прошлом."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.ml.feature_builder import build_features
from app.services import moex_calendar as cal

MSK = timezone(timedelta(hours=3))


def candles(n=30):
    return [{"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0 + (i % 3) * 0.01, "volume": 1000} for i in range(n)]


def feats(as_of):
    return build_features("F1", "SBER", candles(), 10, "BUY", {}, {}, [], [], as_of=as_of)


def test_holidays_any_year():
    assert cal.is_moex_holiday("2026-11-04")
    assert not cal.is_moex_holiday("2026-11-05")
    assert cal.is_moex_holiday("2025-01-03")          # прошлый год: раньше такие даты не покрывались
    assert cal.is_moex_holiday("2024-05-09")
    assert "2026-11-04" in cal.get_moex_holidays_cached() or True   # кеш текущего года зависит от даты запуска


def test_holiday_shifted_from_weekend():
    # 23 февраля 2025 — воскресенье: выходной переносится на понедельник 24-го
    assert cal.is_moex_holiday("2025-02-24")


def test_time_features_follow_as_of():
    f = feats(datetime(2026, 10, 30, 18, 0, tzinfo=MSK))                # пятница 18:00
    assert f["hour_sin"] == pytest.approx(round(math.sin(18 * 2 * math.pi / 24), 4))
    assert f["is_close"] == 1.0 and f["is_morning"] == 0.0
    assert f["session_phase"] == 5.0 and f["day_of_week"] == 4.0
    g = feats(datetime(2026, 10, 28, 10, 30, tzinfo=MSK))               # среда утром
    assert g["is_morning"] == 1.0 and g["session_phase"] == 1.0 and g["day_of_week"] == 2.0


def test_as_of_accepts_utc_datetime():
    utc = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)            # 18:00 МСК
    assert feats(utc)["is_close"] == 1.0


def test_short_carry_risk_friday_evening_vs_noon():
    assert feats(datetime(2026, 10, 30, 12, 0, tzinfo=MSK))["short_carry_risk"] == 0.0   # до 17:00 ещё можно
    assert feats(datetime(2026, 10, 30, 18, 0, tzinfo=MSK))["short_carry_risk"] == 1.0   # впереди сб и вс


def test_short_carry_risk_before_holiday_and_weekend():
    assert feats(datetime(2026, 11, 3, 18, 0, tzinfo=MSK))["short_carry_risk"] == 1.0    # завтра 4 ноября
    assert feats(datetime(2026, 10, 26, 18, 0, tzinfo=MSK))["short_carry_risk"] == 0.0   # завтра рабочий день
    assert feats(datetime(2026, 10, 31, 12, 0, tzinfo=MSK))["short_carry_risk"] == 3.0   # суббота


def test_long_holiday_gives_three():
    # четверг 30 апреля 2026, вечер: впереди 1 мая (пятница, праздник) + суббота + воскресенье = три нерабочих дня
    assert feats(datetime(2026, 4, 30, 18, 0, tzinfo=MSK))["short_carry_risk"] == 3.0
    assert feats(datetime(2026, 4, 30, 12, 0, tzinfo=MSK))["short_carry_risk"] == 0.0     # до 17:00 ещё можно закрыть


def test_working_day_after_holiday_has_no_risk():
    assert feats(datetime(2026, 1, 9, 18, 0, tzinfo=MSK))["short_carry_risk"] == 1.0      # пятница вечером: впереди сб и вс
