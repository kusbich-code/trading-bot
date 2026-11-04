# Только чтение: секторы, даты дивидендов, даты отчётности и история фьючерсов. Каждая запись — строка JSON в stdout.
import collections, json, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from t_tech.invest import CandleInterval, InstrumentStatus
from t_tech.invest.schemas import GetAssetReportsRequest
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal as q
from app.services.tbank_client import with_client

START = datetime(2018, 1, 1, tzinfo=timezone.utc)
now = datetime.now(timezone.utc)
PAUSE = 0.6
ROOTS = ("Brent", "USD/RUB", "EUR/RUB", "CNY/RUB", "Юань", "IMOEX", "Индекс московской", "Индекс РТС", "RTS", "Золото", "Gold", "GOLD", "Серебро", "Природный газ", "Газ (США)")


def emit(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)


def d(x):
    return x.strftime("%Y-%m-%d") if x and x.year > 1971 else None


with with_client() as c:
    tq = [s for s in c.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments if s.class_code == "TQBR" and s.currency == "rub"]
    print("акций", len(tq), file=sys.stderr, flush=True)
    rep_err = 0
    for n, s in enumerate(tq):
        emit({"kind": "meta", "ticker": s.ticker, "sector": s.sector, "dlong": float(q(s.dlong)), "dshort": float(q(s.dshort)), "short": bool(s.short_enabled_flag)})
        try:
            r = c.instruments.get_dividends(instrument_id=s.uid, from_=START, to=now)
            emit({"kind": "div2", "ticker": s.ticker, "rows": [
                {"net": float(money_to_decimal(x.dividend_net)), "cur": x.dividend_net.currency, "declared": d(x.declared_date), "last_buy": d(x.last_buy_date),
                 "created": d(x.created_at)} for x in r.dividends]})
        except Exception as e:
            print("див", s.ticker, type(e).__name__, file=sys.stderr, flush=True)
        time.sleep(PAUSE)
        try:
            r = c.instruments.get_asset_reports(request=GetAssetReportsRequest(instrument_id=s.uid, from_=START, to=now))
            emit({"kind": "report", "ticker": s.ticker, "rows": [{"date": d(x.report_date), "year": x.period_year, "num": x.period_num, "type": str(x.period_type)} for x in r.events]})
        except Exception as e:
            rep_err += 1
            if rep_err < 4:
                print("отчёты", s.ticker, type(e).__name__, str(e)[:120], file=sys.stderr, flush=True)
        time.sleep(PAUSE)
        if n % 50 == 0:
            print(f"акции {n}/{len(tq)}", file=sys.stderr, flush=True)
    fu = c.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments
    roots = collections.Counter(f.basic_asset for f in fu)
    print("корни с >=8 контрактами:", [(k, v) for k, v in roots.most_common(60) if v >= 8], file=sys.stderr, flush=True)
    picked = [f for f in fu if any(r.lower() in f.basic_asset.lower() for r in ROOTS) and "волатильн" not in f.basic_asset.lower()
              and START < f.expiration_date]
    print("контрактов к загрузке", len(picked), collections.Counter(f.basic_asset for f in picked).most_common(20), file=sys.stderr, flush=True)
    for n, f in enumerate(picked):
        bars = []
        try:
            exp = min(f.expiration_date, now)
            r = c.market_data.get_candles(instrument_id=f.uid, from_=exp - timedelta(days=200), to=exp, interval=CandleInterval.CANDLE_INTERVAL_DAY)
            bars = [[x.time.strftime("%Y-%m-%d"), float(q(x.open)), float(q(x.close)), int(x.volume)] for x in r.candles if x.is_complete]
        except Exception as e:
            print("фьюч", f.ticker, type(e).__name__, file=sys.stderr, flush=True)
        emit({"kind": "fut", "ticker": f.ticker, "root": f.basic_asset, "exp": d(f.expiration_date), "bars": bars})
        time.sleep(PAUSE)
        if n % 50 == 0:
            print(f"фьючерсы {n}/{len(picked)}", file=sys.stderr, flush=True)
print("готово", file=sys.stderr, flush=True)
