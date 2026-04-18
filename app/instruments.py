from datetime import datetime, timezone, timedelta

from t_tech.invest import CandleInterval

INSTRUMENTS = {
    "SBER": {"ticker": "SBER", "figi": "BBG004730N88", "lot": 10},
    "SMLT": {"ticker": "SMLT", "figi": "BBG012N7X2Z0", "lot": 1},
    "GAZP": {"ticker": "GAZP", "figi": "BBG004730RP0", "lot": 10},
    "LKOH": {"ticker": "LKOH", "figi": "BBG004731032", "lot": 1},
    "ROSN": {"ticker": "ROSN", "figi": "BBG004731354", "lot": 1},
    "TATN": {"ticker": "TATN", "figi": "BBG004RVFFC0", "lot": 1},
    "VTBR": {"ticker": "VTBR", "figi": "BBG004730ZJ9", "lot": 10000},
    "NVTK": {"ticker": "NVTK", "figi": "BBG00475KKY8", "lot": 1},
    "MOEX": {"ticker": "MOEX", "figi": "BBG004730JJ5", "lot": 10},
}


def get_by_ticker(ticker):
    return INSTRUMENTS.get(ticker.upper())


def get_watchlist_static(tickers):
    result = []
    for t in tickers:
        item = get_by_ticker(t)
        if item:
            result.append(item)
    return result


def estimate_liquidity_score(client, figi: str, minutes=30):
    now = datetime.now(timezone.utc)
    resp = client.market_data.get_candles(
        figi=figi,
        from_=now - timedelta(minutes=minutes + 5),
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    candles = resp.candles
    if not candles:
        return 0
    return sum(getattr(c, "volume", 0) or 0 for c in candles)


def pick_top_liquid(client, candidate_tickers, count=2):
    scored = []
    for ticker in candidate_tickers:
        item = get_by_ticker(ticker)
        if not item:
            continue
        try:
            score = estimate_liquidity_score(client, item["figi"], minutes=30)
            scored.append((score, item))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:count]]