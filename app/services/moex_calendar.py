"""Календарь нерабочих дней MOEX без зависимостей от SDK и main.py.

Праздники генерируются по правилам (федеральные даты с переносом на понедельник, новогодние каникулы 1-8 января).
Точные переносы выходных, объявляемые правительством, не предсказываются.
"""
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Set

MSK_TZ = timezone(timedelta(hours=3))


def generate_moex_holidays(year: int) -> Set[str]:
    """Нерабочие дни MOEX для заданного года в формате YYYY-MM-DD."""
    holidays: Set[str] = set()

    def _add(d: date):
        holidays.add(d.strftime("%Y-%m-%d"))

    def _shift_weekend(d: date) -> date:
        """Если праздник попадает на сб/вс — переносим на ближайший пн."""
        if d.weekday() == 5:   # суббота → понедельник
            return d + timedelta(days=2)
        if d.weekday() == 6:   # воскресенье → понедельник
            return d + timedelta(days=1)
        return d

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
            base = date(year, m, d)
            _add(_shift_weekend(base))
            # новогодние каникулы: 1-8 января всегда нерабочие
            if m == 1 and d == 1:
                for extra in range(1, 9):
                    _add(date(year, 1, extra))
        except ValueError:
            pass
    return holidays


_cache = {"value": None, "ts": 0.0}


def get_moex_holidays_cached() -> Set[str]:
    """Праздники текущего и следующего года; кеш на 24 часа."""
    if _cache["value"] is not None and (time.time() - _cache["ts"]) < 86400:
        return _cache["value"]
    year = datetime.now(MSK_TZ).year
    _cache["value"] = generate_moex_holidays(year) | generate_moex_holidays(year + 1)
    _cache["ts"] = time.time()
    return _cache["value"]


@lru_cache(maxsize=16)
def _holidays_of_year(year: int) -> frozenset:
    return frozenset(generate_moex_holidays(year))


def is_moex_holiday(day: str) -> bool:
    """Нерабочий праздничный день по дате YYYY-MM-DD, для любого года (в том числе прошлого)."""
    return day in _holidays_of_year(int(day[:4]))
