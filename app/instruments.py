from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from t_tech.invest import CandleInterval, InstrumentIdType
from t_tech.invest.utils import quotation_to_decimal


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


def get_instrument_meta(client, figi: str):
    try:
        resp = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
            id=figi
        )
        instrument = getattr(resp, "instrument", None)
        if not instrument:
            return None

        min_price_increment = getattr(instrument, "min_price_increment", None)
        mpi = quotation_to_decimal(min_price_increment) if min_price_increment else Decimal("0.01")

        lot = getattr(instrument, "lot", None)
        return {
            "figi": figi,
            "ticker": getattr(instrument, "ticker", ""),
            "name": getattr(instrument, "name", ""),
            "class_code": getattr(instrument, "class_code", ""),
            "instrument_type": getattr(instrument, "instrument_type", ""),
            "currency": getattr(instrument, "currency", ""),
            "lot": int(lot) if lot else 1,
            "min_price_increment": mpi,
        }
    except Exception:
        return None


def round_to_price_step(price: Decimal, min_price_increment: Decimal) -> Decimal:
    if not min_price_increment or min_price_increment <= 0:
        return price
    steps = (price / min_price_increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * min_price_increment).quantize(min_price_increment)


def find_instruments(client, query: str):
    try:
        resp = client.instruments.find_instrument(query=query)
        items = []
        for x in getattr(resp, "instruments", []):
            mpi = getattr(x, "min_price_increment", None)
            items.append({
                "ticker": getattr(x, "ticker", ""),
                "figi": getattr(x, "figi", ""),
                "name": getattr(x, "name", ""),
                "class_code": getattr(x, "class_code", ""),
                "instrument_type": getattr(x, "instrument_type", ""),
                "currency": getattr(x, "currency", ""),
                "lot": getattr(x, "lot", 1),
                "min_price_increment": str(quotation_to_decimal(mpi)) if mpi else "0.01",
            })
        return items
    except Exception:
        return []