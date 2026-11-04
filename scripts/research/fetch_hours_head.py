# Только чтение: часовые свечи самых ликвидных фьючерсов. Каждая запись — строка JSON в stdout.
import json, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from t_tech.invest import CandleInterval, InstrumentStatus
from t_tech.invest.utils import quotation_to_decimal as q
from app.services.tbank_client import with_client

NEED = {}
PAUSE = 0.6
with with_client() as c:
    by = {}
    for f in c.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments:
        if f.ticker in NEED:
            by[f.ticker] = f.uid
    print("найдено контрактов", len(by), "из", len(NEED), file=sys.stderr, flush=True)
    for n, (t, (g, a, b)) in enumerate(NEED.items()):
        if t not in by:
            continue
        cur = datetime.strptime(a, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(b, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        bars = []
        while cur < end:
            nx = min(cur + timedelta(days=7), end)
            for attempt in range(3):
                try:
                    r = c.market_data.get_candles(instrument_id=by[t], from_=cur, to=nx, interval=CandleInterval.CANDLE_INTERVAL_HOUR)
                    bars += [[x.time.isoformat(), float(q(x.open)), float(q(x.high)), float(q(x.low)), float(q(x.close)), int(x.volume)] for x in r.candles if x.is_complete]
                    break
                except Exception as e:
                    print(t, cur.date(), type(e).__name__, file=sys.stderr, flush=True)
                    time.sleep(5 * (attempt + 1))
            time.sleep(PAUSE)
            cur = nx
        print(json.dumps({"root": g, "ticker": t, "a": a, "b": b, "bars": bars}), flush=True)
        if n % 10 == 0:
            print(f"{n}/{len(NEED)} {t} свечей {len(bars)}", file=sys.stderr, flush=True)
print("готово", file=sys.stderr, flush=True)
