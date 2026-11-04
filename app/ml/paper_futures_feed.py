"""Данные брокера для бумажного счёта C: только чтение справочника, свечей, цен и обеспечения. Ордеров нет."""
import time
from datetime import datetime, timedelta, timezone

from app.ml import paper_daily, paper_futures

MSK = timedelta(hours=3)
HISTORY_DAYS = 200
PAUSE = 0.6                   # запросы раз в неделю, но лимит API общий с ботом


def load_market(client, held: list, record=None) -> dict:
    """Собирает всё для paper_futures.run_step. held — тикеры текущих позиций, record(имя) — учёт запросов в лимите бота."""
    from t_tech.invest import CandleInterval, InstrumentStatus
    from t_tech.invest.utils import quotation_to_decimal as q

    note = record or (lambda name: None)
    now = datetime.now(timezone.utc)
    today = (now + MSK).date()
    every = client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL).instruments
    uid_of = {f.ticker: f.uid for f in every}
    by_group = {}
    for f in every:
        group = paper_futures.ROOTS.get(f.basic_asset)
        if group and now - timedelta(days=HISTORY_DAYS + 10) < f.expiration_date < now + timedelta(days=HISTORY_DAYS):
            by_group.setdefault(group, []).append(f)

    returns, active = {}, {}
    for group, items in by_group.items():
        contracts = []
        for f in items:
            end = min(f.expiration_date, now)
            note("GetCandles")
            candles = client.market_data.get_candles(instrument_id=f.uid, from_=end - timedelta(days=HISTORY_DAYS), to=end,
                                                     interval=CandleInterval.CANDLE_INTERVAL_DAY).candles
            bars = [((c.time + MSK).strftime("%Y-%m-%d"), float(q(c.close)), int(c.volume)) for c in candles
                    if c.is_complete and (c.time + MSK).weekday() < 5]
            if bars:
                contracts.append({"ticker": f.ticker, "exp": f.expiration_date.date().isoformat(), "bars": bars})
            time.sleep(PAUSE)
        returns[group] = paper_futures.stitch_returns(contracts)
        chosen = paper_futures.active_contract(contracts, today)
        if chosen:
            active[group] = {"ticker": chosen["ticker"]}

    wanted = sorted({a["ticker"] for a in active.values()} | set(held))
    note("GetLastPrices")
    last = client.market_data.get_last_prices(instrument_id=[uid_of[t] for t in wanted if t in uid_of]).last_prices
    ticker_of = {uid: t for t, uid in uid_of.items()}
    quotes = {ticker_of[p.instrument_uid]: float(q(p.price)) for p in last if p.instrument_uid in ticker_of and float(q(p.price)) > 0}

    for group in list(active):
        ticker = active[group]["ticker"]
        if ticker not in quotes:
            del active[group]
            continue
        margin = client.instruments.get_futures_margin(instrument_id=uid_of[ticker])
        step, value = float(q(margin.min_price_increment)), float(q(margin.min_price_increment_amount))
        active[group].update(price=quotes[ticker], unit=quotes[ticker] / step * value if step else 0.0)
        time.sleep(PAUSE)

    fund = paper_daily.pick_cash_instrument(client.instruments.etfs().instruments)
    note("GetLastPrices")
    lq = float(q(client.market_data.get_last_prices(instrument_id=[fund.uid]).last_prices[0].price)) if fund else None
    return {"returns": returns, "active": active, "quotes": quotes, "lq": lq}
