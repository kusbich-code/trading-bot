"""Загрузка данных для недельного исследования (только чтение, запускать на сервере, токен берётся из .env).

  .venv/bin/python scripts/research_fetch.py > market.jsonl

Каждая строка — JSON с полем kind: share (дневные свечи акции TQBR), gone (бумага вне основного списка),
div (дивиденды), etf (фонды-эталоны), spread (снимок спреда). Ничего не отправляет брокеру и не пишет в БД.
Лимит API общий с ботом: около 100 запросов в минуту, полный прогон занимает примерно 40 минут.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t_tech.invest import CandleInterval, InstrumentStatus  # noqa: E402
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal  # noqa: E402

from app.services.tbank_client import with_client  # noqa: E402

START = datetime(2018, 1, 1, tzinfo=timezone.utc)
PAUSE = 0.6
ETFS = {"LQDT", "SBMM", "AKMM", "TMON", "TMOS", "SBMX", "EQMX", "AKME"}


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def candles(client, uid: str, now: datetime) -> list:
    """Завершённые дневные свечи частями по 360 дней: [дата, open, high, low, close, volume]."""
    bars, t = [], START
    while t < now:
        t2 = min(t + timedelta(days=360), now)
        for attempt in range(4):
            try:
                r = client.market_data.get_candles(instrument_id=uid, from_=t, to=t2, interval=CandleInterval.CANDLE_INTERVAL_DAY)
                for c in r.candles:
                    if c.is_complete:
                        bars.append([c.time.strftime("%Y-%m-%d"), float(quotation_to_decimal(c.open)), float(quotation_to_decimal(c.high)),
                                     float(quotation_to_decimal(c.low)), float(quotation_to_decimal(c.close)), int(c.volume)])
                break
            except Exception as e:
                print(f"свечи {uid} {t.date()} попытка {attempt}: {type(e).__name__}", file=sys.stderr, flush=True)
                time.sleep(5 * (attempt + 1))
        time.sleep(PAUSE)
        t = t2
    return bars


def main() -> None:
    now = datetime.now(timezone.utc)
    with with_client() as client:
        base = {s.uid for s in client.instruments.shares().instruments}
        every = client.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments
        tqbr = [s for s in every if s.class_code == "TQBR" and s.currency == "rub"]
        print(f"TQBR всего {len(tqbr)}, вне основного списка {sum(s.uid not in base for s in tqbr)}", file=sys.stderr, flush=True)
        for s in tqbr:
            emit({"kind": "share" if s.uid in base else "gone", "ticker": s.ticker, "figi": s.figi, "lot": s.lot,
                  "bars": candles(client, s.uid, now)})
        for s in tqbr:
            try:
                r = client.instruments.get_dividends(instrument_id=s.uid, from_=START, to=now)
                emit({"kind": "div", "ticker": s.ticker, "rows": [
                    {"net": float(money_to_decimal(d.dividend_net)), "cur": d.dividend_net.currency,
                     "last_buy": d.last_buy_date.strftime("%Y-%m-%d") if d.last_buy_date else None} for d in r.dividends]})
            except Exception as e:
                print(f"дивиденды {s.ticker}: {type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(PAUSE)
        for e in client.instruments.etfs(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments:
            if e.ticker in ETFS and e.currency == "rub":
                emit({"kind": "etf", "ticker": e.ticker, "bars": candles(client, e.uid, now)})
        for s in (x for x in tqbr if x.uid in base):
            try:
                ob = client.market_data.get_order_book(instrument_id=s.uid, depth=1)
                if ob.bids and ob.asks:
                    b, a = float(quotation_to_decimal(ob.bids[0].price)), float(quotation_to_decimal(ob.asks[0].price))
                    if a > 0 and b > 0:
                        emit({"kind": "spread", "ticker": s.ticker, "pct": (a - b) / ((a + b) / 2) * 100})
            except Exception as e:
                print(f"стакан {s.ticker}: {type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(0.25)
    print("готово", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
