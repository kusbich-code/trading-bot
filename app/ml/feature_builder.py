"""
Сборщик признаков для ML-модели.
Вычисляет 30 признаков из разных источников данных.
"""
import logging
import math
from typing import Dict, List, Optional, Any

log = logging.getLogger("ml.feature_builder")


def _sma(values: list, n: int) -> float:
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail) if tail else 0.0


def _stddev(values: list, n: int) -> float:
    tail = values[-n:] if len(values) >= n else values
    if len(tail) < 2:
        return 0.0
    mean = sum(tail) / len(tail)
    return (sum((x - mean) ** 2 for x in tail) / len(tail)) ** 0.5


def build_features(
    figi: str,
    ticker: str,
    candles_5m: List[Dict],
    signal_score: int = 0,
    signal_action: str = "HOLD",
    indicators: Optional[Dict] = None,   # RSI, MACD, BB из T-Bank API
    orderbook: Optional[Dict] = None,    # bid/ask volumes, spread
    candles_1h: Optional[List[Dict]] = None,
    candles_4h: Optional[List[Dict]] = None,
    position_minutes: float = 0.0,      # сколько минут открыта позиция
) -> Dict[str, float]:
    """
    Собирает 25 признаков. Все числовые, NaN заменяются на 0.
    """
    from datetime import datetime, timezone, timedelta
    _MSK = timezone(timedelta(hours=3))
    now_msk = datetime.now(tz=_MSK)

    ind = indicators or {}
    ob  = orderbook or {}
    c5  = candles_5m or []
    c1h = candles_1h or []
    c4h = candles_4h or []

    closes_5m = [float(c.get("close", 0)) for c in c5 if c.get("close")]
    highs_5m  = [float(c.get("high",  0)) for c in c5 if c.get("high")]
    lows_5m   = [float(c.get("low",   0)) for c in c5 if c.get("low")]
    vols_5m   = [float(c.get("volume", 0)) for c in c5]

    last_close = closes_5m[-1] if closes_5m else 0.0

    # ── 5-мин технические признаки ───────────────────────────────────────────
    # Z-score (mean reversion)
    avg20  = _sma(closes_5m, 20)
    std20  = _stddev(closes_5m, 20)
    z_score = (last_close - avg20) / std20 if std20 > 0 else 0.0

    # SMA gap (trend)
    sma9  = _sma(closes_5m, 9)
    sma21 = _sma(closes_5m, 21)
    sma_gap_pct = (sma9 - sma21) / sma21 * 100 if sma21 > 0 else 0.0

    # Momentum
    momentum_5 = (closes_5m[-1] / closes_5m[-6] - 1) * 100 if len(closes_5m) >= 6 else 0.0

    # Breakout (20-бар диапазон)
    prev_high = max(highs_5m[-21:-1]) if len(highs_5m) >= 21 else (max(highs_5m[:-1]) if len(highs_5m) > 1 else 0)
    prev_low  = min(lows_5m[-21:-1])  if len(lows_5m)  >= 21 else (min(lows_5m[:-1])  if len(lows_5m)  > 1 else 0)
    price_range = prev_high - prev_low if prev_high > prev_low else 1
    breakout_dist = (last_close - prev_high) / price_range if last_close > prev_high else \
                    (prev_low  - last_close) / price_range if last_close < prev_low  else 0.0

    # Объём
    avg_vol20 = _sma(vols_5m, 20) if vols_5m else 1
    vol_ratio = vols_5m[-1] / avg_vol20 if (avg_vol20 > 0 and vols_5m) else 1.0

    # ATR (5-мин)
    trs_5m = []
    for i in range(1, len(c5)):
        h = float(c5[i].get("high", 0) or 0)
        l = float(c5[i].get("low", 0) or 0)
        pc = float(c5[i-1].get("close", 0) or 0)
        if h and l and pc:
            trs_5m.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_pct = (sum(trs_5m[-14:]) / min(14, len(trs_5m))) / last_close * 100 if (trs_5m and last_close) else 0.0

    # ── 1-час контекст ────────────────────────────────────────────────────────
    closes_1h = [float(c.get("close", 0)) for c in c1h if c.get("close")]
    z_score_1h = 0.0
    trend_1h   = 0.0
    volatility_1h = 0.0
    if len(closes_1h) >= 20:
        avg1h = _sma(closes_1h, 20)
        std1h = _stddev(closes_1h, 20)
        z_score_1h = (closes_1h[-1] - avg1h) / std1h if std1h > 0 else 0.0
        sma5_1h  = _sma(closes_1h, 5)
        sma20_1h = _sma(closes_1h, 20)
        trend_1h = 1.0 if sma5_1h > sma20_1h * 1.001 else (-1.0 if sma5_1h < sma20_1h * 0.999 else 0.0)
        returns_1h = [(closes_1h[i] - closes_1h[i-1]) / closes_1h[i-1] for i in range(1, len(closes_1h))]
        volatility_1h = _stddev(returns_1h, min(20, len(returns_1h))) * 100

    # ── 4-час макро ───────────────────────────────────────────────────────────
    closes_4h = [float(c.get("close", 0)) for c in c4h if c.get("close")]
    trend_4h   = 0.0
    momentum_4h = 0.0
    if len(closes_4h) >= 10:
        sma5_4h  = _sma(closes_4h, 5)
        sma10_4h = _sma(closes_4h, 10)
        trend_4h = 1.0 if sma5_4h > sma10_4h * 1.001 else (-1.0 if sma5_4h < sma10_4h * 0.999 else 0.0)
        momentum_4h = (closes_4h[-1] / closes_4h[-5] - 1) * 100 if len(closes_4h) >= 5 else 0.0

    # ── Стакан ордеров ────────────────────────────────────────────────────────
    bid_vol      = float(ob.get("bid_vol", 0) or 0)
    ask_vol      = float(ob.get("ask_vol", 0) or 0)
    bid_price    = float(ob.get("bid_price", 0) or 0)
    ask_price    = float(ob.get("ask_price", 0) or 0)
    total_vol    = bid_vol + ask_vol
    bid_pressure = bid_vol / total_vol if total_vol > 0 else 0.5
    mid_price    = (bid_price + ask_price) / 2 if (bid_price and ask_price) else last_close
    spread_pct   = (ask_price - bid_price) / mid_price * 100 if (mid_price > 0 and bid_price and ask_price) else 0.0
    book_depth_ratio = bid_vol / ask_vol if ask_vol > 0 else 1.0

    # ── T-Bank API индикаторы ─────────────────────────────────────────────────
    rsi        = float(ind.get("rsi", 50) or 50)
    macd       = float(ind.get("macd", 0) or 0)
    macd_sig   = float(ind.get("macd_signal", 0) or 0)
    macd_diff  = macd - macd_sig
    bb_upper   = float(ind.get("bb_upper", 0) or 0)
    bb_lower   = float(ind.get("bb_lower", 0) or 0)
    bb_range   = bb_upper - bb_lower
    bb_position = (last_close - bb_lower) / bb_range if bb_range > 0 else 0.5

    # ── Сигнал стратегии ──────────────────────────────────────────────────────
    signal_dir = 1.0 if signal_action == "BUY" else (-1.0 if signal_action == "SELL" else 0.0)

    # ── Временны́е признаки ───────────────────────────────────────────────────
    hour = now_msk.hour + now_msk.minute / 60
    hour_sin   = math.sin(hour * 2 * math.pi / 24)
    hour_cos   = math.cos(hour * 2 * math.pi / 24)
    is_morning = 1.0 if 10 <= now_msk.hour < 12 else 0.0
    is_close   = 1.0 if 17 <= now_msk.hour < 19 else 0.0
    day_of_week = float(now_msk.weekday())  # 0=пн, 4=пт

    # ── Итоговый вектор ───────────────────────────────────────────────────────
    # ── VWAP отклонение ──────────────────────────────────────────────────────
    vwap_dev = 0.0
    if c5 and last_close:
        try:
            _vwap_vol = sum(float(c.get("volume", 0) or 0) for c in c5[-20:])
            if _vwap_vol > 0:
                _vwap_sum = sum(
                    float(c.get("close", 0)) * float(c.get("volume", 0) or 0)
                    for c in c5[-20:] if c.get("close")
                )
                vwap = _vwap_sum / _vwap_vol
                vwap_dev = (last_close - vwap) / vwap * 100 if vwap > 0 else 0.0
        except Exception:
            pass

    # ── Сессионная фаза ───────────────────────────────────────────────────────
    h = now_msk.hour
    session_phase = (0 if h < 10 else
                     1 if h == 10 else
                     2 if 11 <= h <= 12 else
                     3 if 13 <= h <= 14 else
                     4 if 15 <= h <= 16 else
                     5 if h >= 17 else 4)

    # ── Cross-instrument корреляция ───────────────────────────────────────────
    sector_corr = 0.0
    if len(closes_5m) >= 6:
        returns_5m = [(closes_5m[i] - closes_5m[i-1]) / closes_5m[i-1]
                      for i in range(1, len(closes_5m))]
        # Обновляем кеш если это маркерный инструмент
        if figi in _SECTOR_MARKERS:
            update_sector_returns(figi, returns_5m)
        sector_corr = compute_sector_correlation(figi, returns_5m)

    # ── Рыночный режим (упрощённый, без отдельного модуля) ────────────────────
    regime = 0.0
    if len(closes_5m) >= 20:
        _adx_moves = [abs(closes_5m[i] - closes_5m[i-1]) / closes_5m[i-1] * 100
                      for i in range(1, min(15, len(closes_5m)))]
        _avg_move = sum(_adx_moves) / len(_adx_moves) if _adx_moves else 0
        _trend_str = abs(sma_gap_pct)
        if _avg_move > 0.3 and _trend_str > 0.2:
            regime = 1.0 if sma_gap_pct > 0 else 2.0   # trending up/down
        elif _avg_move > 0.5:
            regime = 3.0   # volatile
        # else 0 = ranging

    features = {
        # 5-мин сигнал
        "z_score":        round(z_score, 4),
        "sma_gap_pct":    round(sma_gap_pct, 4),
        "momentum_5":     round(momentum_5, 4),
        "breakout_dist":  round(breakout_dist, 4),
        "vol_ratio":      round(vol_ratio, 4),
        "atr_pct":        round(atr_pct, 4),
        # 1ч контекст
        "z_score_1h":     round(z_score_1h, 4),
        "trend_1h":       trend_1h,
        "volatility_1h":  round(volatility_1h, 4),
        # 4ч макро
        "trend_4h":       trend_4h,
        "momentum_4h":    round(momentum_4h, 4),
        # Стакан
        "bid_pressure":   round(bid_pressure, 4),
        "spread_pct":     round(spread_pct, 4),
        "book_depth_ratio": round(min(book_depth_ratio, 10), 4),
        # T-Bank API
        "rsi":            round(rsi, 2),
        "macd_diff":      round(macd_diff, 4),
        "bb_position":    round(bb_position, 4),
        # Сигнал
        "signal_dir":     signal_dir,
        "signal_score":   float(signal_score),
        # Время
        "hour_sin":       round(hour_sin, 4),
        "hour_cos":       round(hour_cos, 4),
        "is_morning":     is_morning,
        "is_close":       is_close,
        "day_of_week":    day_of_week,
        # Позиция
        "position_minutes": position_minutes,
        # Новые признаки
        "vwap_dev":      round(vwap_dev, 4),
        "session_phase": float(session_phase),
        "regime":        regime,
        "sector_corr":   round(sector_corr, 4),
        "ticker_hash":   get_ticker_code(ticker),
    }
    return features


FEATURE_NAMES = [
    "z_score", "sma_gap_pct", "momentum_5", "breakout_dist", "vol_ratio", "atr_pct",
    "z_score_1h", "trend_1h", "volatility_1h",
    "trend_4h", "momentum_4h",
    "bid_pressure", "spread_pct", "book_depth_ratio",
    "rsi", "macd_diff", "bb_position",
    "signal_dir", "signal_score",
    "hour_sin", "hour_cos", "is_morning", "is_close", "day_of_week",
    "position_minutes",
    # Новые признаки
    "vwap_dev",        # отклонение от VWAP в %
    "session_phase",   # 0=pre, 1=open, 2=morning, 3=lunch, 4=afternoon, 5=close
    "regime",          # 0=ranging, 1=trending_up, 2=trending_down, 3=volatile
    "sector_corr",     # корреляция с SBER/GAZP
    "ticker_hash",     # числовой код тикера (для универсальной модели)
]

# Словарь тикер → числовой код
_TICKER_CODES: dict = {}

def get_ticker_code(ticker: str) -> float:
    """Стабильный числовой код тикера для универсальной модели (CRC32 — не зависит от PYTHONHASHSEED)."""
    if ticker not in _TICKER_CODES:
        import binascii
        _TICKER_CODES[ticker] = float((binascii.crc32(ticker.encode()) & 0xFFFFFFFF) % 1000) / 1000
    return _TICKER_CODES[ticker]


def features_to_vector(features: Dict[str, float]) -> list:
    """Конвертирует dict признаков в вектор для sklearn."""
    return [features.get(name, 0.0) for name in FEATURE_NAMES]


def store_features(figi: str, ticker: str, strategy_id: int,
                   features: Dict[str, float], trade_id: int = 0) -> int:
    """Сохраняет признаки в ml_features. Возвращает id записи."""
    from datetime import datetime, timezone, timedelta
    _MSK = timezone(timedelta(hours=3))
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cols = list(features.keys())
            vals = list(features.values())
            placeholders = ",".join(["?"] * len(cols))
            col_names = ",".join(cols)
            cur.execute(f"""
                INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, trade_id, {col_names})
                VALUES (?, ?, ?, ?, ?, {placeholders})
            """, [figi, ticker, strategy_id, now, trade_id] + vals)
            return cur.lastrowid or 0
    except Exception as e:
        log.warning("store_features %s: %s", ticker, e)
        return 0


def label_features(feature_id: int, pnl: float, quality_score: float) -> None:
    """
    Размечает результат сделки. Градуированные метки:
      pnl >> 0  → label=2  (отличная сделка, выше среднего TP)
      pnl > 0   → label=1  (хорошая)
      pnl < 0 (небольшой) → label=-1 (плохая)
      pnl << 0  → label=-2 (очень плохая, хуже -1%)
    Но для бинарной классификации sklearn используем 0/1, поэтому
    сохраняем оба формата.
    """
    # Бинарный label для sklearn
    label = 1 if pnl > 0 else 0
    # Градуированный quality_score для future regression model
    # quality_score уже передаётся снаружи (pnl / invested)
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                UPDATE ml_features SET pnl=?, quality_score=?, label=? WHERE id=?
            """, (pnl, quality_score, label, feature_id))
    except Exception as e:
        log.warning("label_features %d: %s", feature_id, e)


# ── Cross-instrument корреляция ────────────────────────────────────────────────
_sector_returns_cache: dict = {}  # figi → (ts, returns_list)
_SECTOR_MARKERS = ["BBG004730N88", "BBG004730RP0"]  # SBER, GAZP как рыночные маркеры


def update_sector_returns(figi: str, returns: list) -> None:
    """Обновляет кеш доходностей для рыночных маркеров."""
    import time
    _sector_returns_cache[figi] = (time.monotonic(), returns[-20:])


def compute_sector_correlation(figi: str, own_returns: list) -> float:
    """Корреляция инструмента с рыночными маркерами (SBER, GAZP)."""
    if len(own_returns) < 5:
        return 0.0
    corr_sum = 0.0
    count = 0
    for marker_figi in _SECTOR_MARKERS:
        if marker_figi == figi:
            continue
        cached = _sector_returns_cache.get(marker_figi)
        if not cached:
            continue
        marker_ret = cached[1]
        n = min(len(own_returns), len(marker_ret))
        if n < 5:
            continue
        a = own_returns[-n:]
        b = marker_ret[-n:]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
        std_a = (sum((x - mean_a)**2 for x in a) / n)**0.5
        std_b = (sum((x - mean_b)**2 for x in b) / n)**0.5
        if std_a > 0 and std_b > 0:
            corr_sum += cov / (std_a * std_b)
            count += 1
    return round(corr_sum / count, 4) if count > 0 else 0.0
