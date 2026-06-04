"""
Бэкфил RSI/MACD/BB для строк ml_features у которых эти данные = дефолт.

Запуск (из корня проекта):
    python scripts/backfill_ml_indicators.py

Для каждой строки ml_features с rsi=50:
  - Запрашивает 5м свечи у T-Bank на момент открытия
  - Вычисляет RSI(14), MACD(12-26-9), BB(20, ±2σ), MACD_diff
  - Обновляет ml_features
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone, timedelta
from app.config import settings
from app.db import db_cursor

_MSK = timezone(timedelta(hours=3))


# ── Технические индикаторы из свечей ──────────────────────────────────────────

def _ema(closes: list, period: int) -> float:
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 2:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1 + rs), 2)


def compute_macd(closes: list, fast=12, slow=26, signal=9):
    """Возвращает (macd_line, signal_line)."""
    if len(closes) < slow + signal:
        return 0.0, 0.0

    # EMA fast и slow на всей истории
    ema_fast_series = []
    ema_slow_series = []
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    ef = closes[0]; es = closes[0]
    for c in closes:
        ef = c * k_fast + ef * (1 - k_fast)
        es = c * k_slow + es * (1 - k_slow)
        ema_fast_series.append(ef)
        ema_slow_series.append(es)

    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]

    # Signal = EMA(signal) от MACD
    k_sig = 2.0 / (signal + 1)
    sig = macd_series[0]
    for m in macd_series[1:]:
        sig = m * k_sig + sig * (1 - k_sig)

    return round(macd_series[-1], 6), round(sig, 6)


def compute_bb(closes: list, period: int = 20, multiplier: float = 2.0):
    """Возвращает (bb_upper, bb_lower)."""
    if len(closes) < period:
        return 0.0, 0.0
    window = closes[-period:]
    sma = sum(window) / period
    std = (sum((c - sma) ** 2 for c in window) / period) ** 0.5
    return round(sma + multiplier * std, 4), round(sma - multiplier * std, 4)


# ── Получение свечей из T-Bank ────────────────────────────────────────────────

def fetch_candles_at(client, figi: str, ts_str: str, uid: str = "") -> list:
    """Загружает 5м свечи за 3 часа до момента ts_str."""
    try:
        from t_tech.invest.schemas import CandleInterval
        from t_tech.invest.utils import quotation_to_decimal
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
        from_ = dt_utc - timedelta(hours=3)
        resp = client.market_data.get_candles(
            instrument_id=uid or figi,
            from_=from_,
            to=dt_utc + timedelta(minutes=5),
            interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
        )
        candles = []
        for c in (resp.candles or []):
            close = float(quotation_to_decimal(c.close)) if c.close else 0
            if close > 0:
                candles.append(close)
        return candles
    except Exception as e:
        print(f"  fetch error {figi} @ {ts_str}: {e}")
        return []


# ── Основная логика ───────────────────────────────────────────────────────────

def backfill():
    # Строки где rsi=50 (дефолт) и label не None (чтобы не трогать активные позиции)
    # Включаем и активные (label=NULL), чтобы будущие обучения были точнее
    with db_cursor() as cur:
        cur.execute("""
            SELECT id, figi, ticker, timestamp
            FROM ml_features
            WHERE rsi = 50.0
            ORDER BY id
        """)
        rows = cur.fetchall()

    if not rows:
        print("Нет строк с дефолтным RSI=50 — всё уже заполнено.")
        return

    print(f"Найдено {len(rows)} строк для бэкфила: {[dict(r)['ticker'] for r in rows]}")

    # uid из БД для каждого figi
    uid_map = {}
    with db_cursor() as cur:
        for row in rows:
            figi = dict(row)["figi"]
            if figi not in uid_map:
                cur.execute("SELECT instrument_uid FROM instrument_market_state WHERE figi=? LIMIT 1", (figi,))
                r = cur.fetchone()
                uid_map[figi] = (r[0] if r and r[0] else "") if r else ""

    from t_tech.invest import Client as TBClient
    with TBClient(settings.TINVEST_TOKEN) as client:
        for row in rows:
            r = dict(row)
            figi = r["figi"]; ticker = r["ticker"]; ts = r["timestamp"]; feat_id = r["id"]
            uid = uid_map.get(figi, "")
            print(f"\n[{feat_id}] {ticker} @ {ts}")

            closes = fetch_candles_at(client, figi, ts, uid)
            if len(closes) < 15:
                print(f"  Мало свечей ({len(closes)}) — пропускаем")
                continue

            rsi = compute_rsi(closes)
            macd_line, macd_sig = compute_macd(closes)
            macd_diff = round(macd_line - macd_sig, 6)
            bb_upper, bb_lower = compute_bb(closes)
            last_close = closes[-1]
            bb_range = bb_upper - bb_lower
            bb_position = round((last_close - bb_lower) / bb_range, 4) if bb_range > 0 else 0.5

            print(f"  RSI={rsi:.1f}  MACD_diff={macd_diff:.4f}  BB={bb_lower:.2f}..{bb_upper:.2f}  BB_pos={bb_position:.3f}")

            with db_cursor() as cur:
                cur.execute("""
                    UPDATE ml_features
                    SET rsi=?, macd_diff=?, bb_position=?
                    WHERE id=?
                """, (rsi, macd_diff, bb_position, feat_id))

            print(f"  ✓ Обновлено")

    print("\nБэкфил завершён.")


if __name__ == "__main__":
    backfill()
