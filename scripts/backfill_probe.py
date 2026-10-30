"""Проба перед исторической добивкой (только чтение, БД не трогает).

Запуск на сервере из каталога проекта:  .venv/bin/python scripts/backfill_probe.py [TICKER_FIGI]

Выясняет:
  1) на какую глубину доступны минутные свечи (по одному дню на выбранных отступах назад);
  2) отдаёт ли GetTechAnalysis ряд значений за прошлые даты, сколько точек и сколько запросов это стоит;
  3) насколько значения API (RSI/MACD/BB) совпадают с локальным расчётом по свечам.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from t_tech.invest import CandleInterval  # noqa: E402
from t_tech.invest.schemas import (  # noqa: E402
    Deviation, GetTechAnalysisRequest, IndicatorInterval, IndicatorType, Smoothing, TypeOfPrice,
)
from t_tech.invest import Quotation  # noqa: E402
from t_tech.invest.utils import quotation_to_decimal  # noqa: E402

from app.services.tbank_client import with_client  # noqa: E402

PAUSE = 0.7   # секунд между запросами: проба укладывается в ~90 запросов в минуту


def weekday_back(days: int) -> datetime:
    d = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(days=days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_day(client, figi, start):
    return client.market_data.get_candles(instrument_id=figi, from_=start, to=start + timedelta(hours=17),
                                          interval=CandleInterval.CANDLE_INTERVAL_1_MIN).candles


def num(q):
    return float(quotation_to_decimal(q)) if q is not None else None


# ── локальный расчёт (для сравнения) ─────────────────────────────────────────

def rsi_simple(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    l = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[-n:]) / n, sum(l[-n:]) / n
    return 100.0 - 100.0 / (1 + ag / al) if al else 100.0


def rsi_wilder(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
    return 100.0 - 100.0 / (1 + ag / al) if al else 100.0


def macd_last(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None, None
    kf, ks, kg = 2 / (fast + 1), 2 / (slow + 1), 2 / (sig + 1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef, es = c * kf + ef * (1 - kf), c * ks + es * (1 - ks)
        line.append(ef - es)
    s = line[0]
    for m in line[1:]:
        s = m * kg + s * (1 - kg)
    return line[-1], s


def bb_last(closes, n=20, mult=2.0):
    if len(closes) < n:
        return None, None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((c - m) ** 2 for c in w) / n) ** 0.5
    return m + mult * sd, m - mult * sd


def tech(client, figi, kind, start, end, **kw):
    req = GetTechAnalysisRequest(indicator_type=kind, instrument_uid=figi, from_=start, to=end,
                                 interval=IndicatorInterval.INDICATOR_INTERVAL_ONE_MINUTE,
                                 type_of_price=TypeOfPrice.TYPE_OF_PRICE_CLOSE, **kw)
    return client.market_data.get_tech_analysis(request=req).technical_indicators


def main():
    figi = sys.argv[1] if len(sys.argv) > 1 else "BBG004730N88"   # SBER
    with with_client() as client:
        print("== 1. Глубина минутных свечей (один день на каждом отступе)")
        for days in (7, 30, 60, 90, 180, 365, 730):
            start = weekday_back(days)
            try:
                cs = fetch_day(client, figi, start)
                if cs:
                    print(f"  {days:4d} дн назад ({start.date()}): {len(cs):4d} свечей, "
                          f"{cs[0].time.strftime('%H:%M')}..{cs[-1].time.strftime('%H:%M')} UTC")
                else:
                    print(f"  {days:4d} дн назад ({start.date()}): 0 свечей")
            except Exception as e:
                print(f"  {days:4d} дн назад ({start.date()}): ОШИБКА {str(e)[:150]}")
            time.sleep(PAUSE)

        print("\n== 2. GetTechAnalysis за прошлые даты (один день, 1-минутный интервал)")
        day = weekday_back(5)
        end = day + timedelta(hours=17)
        candles = fetch_day(client, figi, day)
        closes = [num(c.close) for c in candles if c.is_complete]
        print(f"  день {day.date()}: {len(closes)} завершённых свечей, закрытие последней {closes[-1] if closes else None}")
        series = {}
        for name, kind, kw in (
            ("RSI", IndicatorType.INDICATOR_TYPE_RSI, {"length": 14}),
            ("MACD", IndicatorType.INDICATOR_TYPE_MACD, {"smoothing": Smoothing(fast_length=12, slow_length=26, signal_smoothing=9)}),
            ("BB", IndicatorType.INDICATOR_TYPE_BB, {"length": 20, "deviation": Deviation(deviation_multiplier=Quotation(units=2, nano=0))}),
        ):
            time.sleep(PAUSE)
            try:
                pts = tech(client, figi, kind, day, end, **kw)
                series[name] = pts
                if pts:
                    print(f"  {name}: {len(pts)} точек, {pts[0].timestamp.strftime('%H:%M')}..{pts[-1].timestamp.strftime('%H:%M')} UTC")
                else:
                    print(f"  {name}: 0 точек")
            except Exception as e:
                print(f"  {name}: ОШИБКА {str(e)[:200]}")

        print("\n== 3. Длинный диапазон (3 суток) одним запросом: потолок точек")
        time.sleep(PAUSE)
        try:
            pts = tech(client, figi, IndicatorType.INDICATOR_TYPE_RSI, day - timedelta(days=2), end, length=14)
            print(f"  RSI за 3 суток: {len(pts)} точек, {pts[0].timestamp} .. {pts[-1].timestamp}" if pts else "  0 точек")
        except Exception as e:
            print(f"  ОШИБКА {str(e)[:200]}")

        print("\n== 4. Сравнение API с локальным расчётом (последние 60 точек дня)")
        by_time = {c.time: num(c.close) for c in candles if c.is_complete}
        times = sorted(by_time)
        closes_by_t = [by_time[t] for t in times]
        def at(idx):  # значения закрытий до свечи idx включительно
            return closes_by_t[: idx + 1]
        pos = {t: i for i, t in enumerate(times)}
        diffs = {"RSI simple": [], "RSI wilder": [], "MACD": [], "MACD signal": [], "BB upper": [], "BB lower": []}
        for p in (series.get("RSI") or [])[-60:]:
            i = pos.get(p.timestamp)
            if i is None:
                continue
            api = num(p.signal)
            for k, f in (("RSI simple", rsi_simple), ("RSI wilder", rsi_wilder)):
                v = f(at(i))
                if v is not None and api is not None:
                    diffs[k].append(abs(api - v))
        for p in (series.get("MACD") or [])[-60:]:
            i = pos.get(p.timestamp)
            if i is None:
                continue
            m, s = macd_last(at(i))
            if m is not None:
                diffs["MACD"].append(abs(num(p.macd) - m))
                diffs["MACD signal"].append(abs(num(p.signal) - s))
        for p in (series.get("BB") or [])[-60:]:
            i = pos.get(p.timestamp)
            if i is None:
                continue
            u, lo = bb_last(at(i))
            if u is not None:
                diffs["BB upper"].append(abs(num(p.upper_band) - u))
                diffs["BB lower"].append(abs(num(p.lower_band) - lo))
        for k, v in diffs.items():
            print(f"  {k:12s}: точек {len(v):3d}" + (f", средняя |разница| {sum(v) / len(v):.5f}, максимум {max(v):.5f}" if v else ""))
        if series.get("RSI"):
            p = series["RSI"][-1]
            print("  пример последней точки RSI:", p.timestamp, num(p.signal))


if __name__ == "__main__":
    main()
