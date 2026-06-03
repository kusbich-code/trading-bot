"""
Загрузка свечей нескольких таймфреймов с кешированием.
1h — обновляем каждые 30 мин, 4h — каждые 2 часа.
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger("ml.multi_timeframe")

_MSK = timezone(timedelta(hours=3))
_cache: Dict[str, tuple] = {}  # key → (fetched_ts, candles)

TF_TTL = {
    "1hour": 1800,   # 30 минут
    "4hour": 7200,   # 2 часа
    "1day":  86400,  # сутки
}


def get_candles_cached(client, figi: str, interval: str, n: int = 30) -> List[Dict]:
    """Возвращает свечи с кешированием по TTL таймфрейма."""
    key = f"{figi}_{interval}_{n}"
    now = time.monotonic()
    ttl = TF_TTL.get(interval, 1800)

    cached = _cache.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    try:
        from app.services.tbank_client import get_candles as _gc
        candles = _gc(client, figi, n=n, interval_name=interval)
        _cache[key] = (now, candles)
        return candles
    except Exception as e:
        log.debug("multi_timeframe %s %s: %s", figi, interval, e)
        return cached[1] if cached else []


def get_all_timeframes(client, figi: str) -> Dict[str, List[Dict]]:
    """Загружает 5мин/1ч/4ч свечи для одного инструмента."""
    return {
        "1hour": get_candles_cached(client, figi, "1hour", 30),
        "4hour": get_candles_cached(client, figi, "4hour", 20),
    }


def trend_direction(candles: List[Dict], fast: int = 5, slow: int = 20) -> int:
    """Направление тренда: +1 бычий, -1 медвежий, 0 нейтральный."""
    if not candles or len(candles) < slow:
        return 0
    closes = [float(c.get("close", 0)) for c in candles if c.get("close")]
    if len(closes) < slow:
        return 0
    sma_fast = sum(closes[-fast:]) / fast
    sma_slow = sum(closes[-slow:]) / slow
    if sma_fast > sma_slow * 1.001:
        return 1
    if sma_fast < sma_slow * 0.999:
        return -1
    return 0


def compute_volatility(candles: List[Dict], n: int = 20) -> float:
    """Реализованная волатильность (stddev returns × sqrt(252×n_bars_per_day))."""
    if not candles or len(candles) < n + 1:
        return 0.0
    closes = [float(c.get("close", 0)) for c in candles[-n-1:] if c.get("close")]
    if len(closes) < 2:
        return 0.0
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return var ** 0.5 * 100  # в процентах


def compute_atr(candles: List[Dict], n: int = 14) -> float:
    """ATR как % от цены."""
    if not candles or len(candles) < n:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].get("high", 0) or 0)
        l = float(candles[i].get("low", 0) or 0)
        pc = float(candles[i-1].get("close", 0) or 0)
        if h and l and pc:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    atr = sum(trs[-n:]) / min(n, len(trs))
    last_close = float(candles[-1].get("close", 1) or 1)
    return atr / last_close * 100  # в процентах
