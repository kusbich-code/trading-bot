"""Когда сканер виртуальных сделок работает вне обычной сессии MOEX.

Владелец переключает «Торговать только в сессию» в профиле. Если выключено, сканер работает и вне будничных сессий 10:00–18:50 и
19:05–23:50, но только по бумагам, которые брокер в этот момент даёт торговать: сессия выходного дня, утренняя сессия, дилерская
торговля брокера. Ночью без торгов цены застывают, и сделки по ним были бы мусором для обучения.
"""
import time
from typing import Callable, Dict, List, Optional, Tuple


def scan_mode(in_session: bool, session_only: bool) -> str:
    """session — обычная сессия, сканируется всё; off_hours — вне неё, только торгуемые бумаги; off — сканер ждёт."""
    if in_session:
        return "session"
    return "off" if session_only else "off_hours"


def session_only() -> bool:
    """Настройка активного профиля «Торговать только в сессию». Без профиля — да, как раньше."""
    from app.db import get_profile_setting, get_setting
    pid = str(get_setting("active_profile_id", "") or "").strip()
    if not pid.isdigit():
        return True
    return str(get_profile_setting(int(pid), "trade_only_session", "1")).strip() != "0"


class TradableCache:
    """Статус торгов бумаги у брокера с кешем: 12 бумаг раз в 5 минут не тратят лимит API."""

    def __init__(self, fetch: Callable[[str], bool], ttl: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._clock = fetch, ttl, clock
        self._known: Dict[str, Tuple[float, bool]] = {}

    def tradable(self, figi: str, can_request: bool = True) -> bool:
        hit = self._known.get(figi)
        now = self._clock()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        if not can_request:
            return hit[1] if hit else False
        try:
            value = bool(self._fetch(figi))
        except Exception:
            return hit[1] if hit else False
        self._known[figi] = (now, value)
        return value


def filter_universe(items: List[dict], mode: str, cache: TradableCache, can_request: Optional[Callable[[], bool]] = None) -> List[dict]:
    if mode == "session":
        return items
    if mode == "off":
        return []
    return [it for it in items if cache.tradable(it["figi"], can_request() if can_request else True)]
