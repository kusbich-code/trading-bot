from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from t_tech.invest import CandleInterval, InstrumentIdType
from t_tech.invest.utils import quotation_to_decimal


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def round_to_price_step(price: Decimal, min_price_increment: Decimal) -> Decimal:
    if not min_price_increment or min_price_increment <= 0:
        return price
    steps = (price / min_price_increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * min_price_increment).quantize(min_price_increment)


def _mpi_str(x) -> str:
    mpi = getattr(x, "min_price_increment", None)
    try:
        return str(quotation_to_decimal(mpi)) if mpi else "0.01"
    except Exception:
        return "0.01"


def get_instrument_meta(client, figi: str):
    try:
        resp = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
            id=figi,
        )
        instrument = getattr(resp, "instrument", None)
        if not instrument:
            return None

        mpi_raw = getattr(instrument, "min_price_increment", None)
        mpi = quotation_to_decimal(mpi_raw) if mpi_raw else Decimal("0.01")
        lot = getattr(instrument, "lot", 1)

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


def estimate_liquidity_score(client, figi: str, minutes: int = 30) -> int:
    try:
        now = datetime.now(timezone.utc)
        resp = client.market_data.get_candles(
            figi=figi,
            from_=now - timedelta(minutes=minutes + 5),
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
        )
        candles = getattr(resp, "candles", []) or []
        return sum(int(getattr(c, "volume", 0) or 0) for c in candles)
    except Exception:
        return 0


def find_instruments(client, query: str) -> list:
    try:
        resp = client.instruments.find_instrument(query=query)
        items = []
        for x in getattr(resp, "instruments", []):
            figi = getattr(x, "figi", "")
            if not figi:
                continue
            items.append({
                "ticker": getattr(x, "ticker", ""),
                "figi": figi,
                "name": getattr(x, "name", ""),
                "class_code": getattr(x, "class_code", ""),
                "instrument_type": str(getattr(x, "instrument_type", "")),
                "currency": getattr(x, "currency", ""),
                "lot": int(getattr(x, "lot", 1) or 1),
                "min_price_increment": _mpi_str(x),
                "использовать": False,
            })
        return items
    except Exception as e:
        print(f"[find_instruments] ERROR: {e}")
        return []


def get_last_prices_for_figis(client, figis: list) -> dict:
    if not figis:
        return {}
    try:
        resp = client.market_data.get_last_prices(figi=figis)
        result = {}
        for item in getattr(resp, "last_prices", []):
            price = quotation_to_decimal(item.price)
            t = getattr(item, "time", None)
            result[item.figi] = {
                "last_price": str(price),
                "price_time": str(t) if t else "",
            }
        return result
    except Exception as e:
        print(f"[get_last_prices_for_figis] ERROR: {e}")
        return {}


def get_popular_tickers() -> list:
    return [
        "SBER", "GAZP", "LKOH", "ROSN", "NVTK",
        "GMKN", "TATN", "VTBR", "SMLT", "MGNT",
        "YDEX", "MOEX", "CHMF", "PLZL", "ALRS",
        "SNGS", "PIKK", "AFKS", "RUAL", "IRAO",
    ]


def get_volume_top20_instruments(client) -> list:
    base_tickers = [
        "SBER", "GAZP", "LKOH", "ROSN", "NVTK",
        "GMKN", "TATN", "VTBR", "SMLT", "MGNT",
        "YDEX", "MOEX", "CHMF", "PLZL", "ALRS",
        "SNGS", "PIKK", "AFKS", "RUAL", "IRAO",
        "MAGN", "T", "HEAD", "FLOT", "MTSS",
        "BANEP", "SBERP", "TRNFP", "AFLT", "PHOR",
    ]

    candidates = []
    seen_figi = set()

    for ticker in base_tickers:
        try:
            found = find_instruments(client, ticker)
            if not found:
                continue
            exact = next(
                (x for x in found if x.get("ticker", "").upper() == ticker.upper()),
                found[0],
            )
            figi = exact.get("figi", "")
            if not figi or figi in seen_figi:
                continue
            seen_figi.add(figi)
            candidates.append(exact)
        except Exception as e:
            print(f"[get_volume_top20] ticker={ticker} ERROR: {e}")
            continue

    if not candidates:
        print("[get_volume_top20] No candidates found")
        return []

    now = datetime.now(timezone.utc)
    frm = now - timedelta(hours=1)

    scored = []
    for item in candidates:
        figi = item.get("figi", "")
        volume_sum = 0
        try:
            candles_resp = client.market_data.get_candles(
                figi=figi,
                from_=frm,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
            )
            for c in getattr(candles_resp, "candles", []) or []:
                volume_sum += int(getattr(c, "volume", 0) or 0)
        except Exception as e:
            print(f"[get_volume_top20] get_candles figi={figi} ERROR: {e}")
            volume_sum = 0

        item["volume_score"] = volume_sum
        item["использовать"] = False
        scored.append(item)

    scored.sort(key=lambda x: x.get("volume_score", 0), reverse=True)
    return scored[:20]