"""Время торговли по МСК. От него зависят издержки (спред) и то, где у модели был плюс.

Ключи:
  day — будни 10:00–18:40; evening_open — будни 19:00–19:30 (с 19:00 до 19:05 аукцион открытия, бот торгует с 19:05);
  evening — будни 19:30–23:50; morning — утренняя сессия будней до 10:00; weekend_exchange — сессия выходного дня 10:00–19:00;
  dealer — вне биржевых часов, когда торгует только брокер (ночь, выходные вне сессии): спред в 5–10 раз шире;
  other — будни 18:40–19:00, конец основной сессии и перерыв.
"""
from datetime import datetime

KEYS = ("day", "evening_open", "evening", "morning", "weekend_exchange", "dealer", "other")
EXCHANGE = frozenset(KEYS) - {"dealer"}


def key_of(weekday: int, minute: int) -> str:
    """weekday: 0 — понедельник; minute: минута суток по МСК."""
    if weekday >= 5:
        return "weekend_exchange" if 600 <= minute < 1140 else "dealer"
    if 600 <= minute < 1120:
        return "day"
    if 1140 <= minute < 1170:
        return "evening_open"
    if 1170 <= minute < 1430:
        return "evening"
    if 390 <= minute < 600:
        return "morning"
    if minute < 390 or minute >= 1430:
        return "dealer"
    return "other"


def key_of_str(ts: str) -> str:
    t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    return key_of(t.weekday(), t.hour * 60 + t.minute)


def key_of_epoch(ts: int) -> str:
    """Секунды времени МСК без пояса, прочитанного как UTC (так их даёт strftime('%s') в SQLite)."""
    ts = int(ts)
    return key_of((ts // 86400 + 3) % 7, (ts % 86400) // 60)
