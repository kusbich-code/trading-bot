from fastapi import APIRouter, HTTPException
import logging

from app.config import settings
from t_tech.invest import Client

logger = logging.getLogger(__name__)
router = APIRouter()


def _instrument_to_dict(inst) -> dict:
    return {
        "ticker": getattr(inst, "ticker", "") or "",
        "figi": getattr(inst, "figi", "") or "",
        "name": getattr(inst, "name", "") or "",
        "class_code": getattr(inst, "class_code", "") or "",
        "instrument_type": str(getattr(inst, "instrument_type", "") or ""),
        "uid": getattr(inst, "uid", "") or "",
        "position_uid": getattr(inst, "position_uid", "") or "",
        "currency": getattr(inst, "currency", "") or "",
        "lot": getattr(inst, "lot", 1) or 1,
        "min_price_increment": str(getattr(inst, "min_price_increment", "0.01") or "0.01"),
    }

def get_instrument_meta(figi: str) -> dict:
    with Client(settings.TINVEST_TOKEN) as client:
        resp = client.instruments.get_instrument_by(id_type=1, id=figi)
        inst = getattr(resp, "instrument", None)
        if not inst:
            return {}
        return _instrument_to_dict(inst)


def round_to_price_step(price, step):
    from decimal import Decimal
    price = Decimal(str(price))
    step = Decimal(str(step or "0.01"))
    if step <= 0:
        return price
    return (price / step).quantize(Decimal("1")) * step

@router.get("/search")
async def search_instruments(q: str, kind: str = "shares"):
    try:
        query = (q or "").strip()
        if not query:
            return []

        with Client(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.find_instrument(query=query)

        items = [_instrument_to_dict(x) for x in getattr(resp, "instruments", [])]

        if kind == "shares":
            items = [x for x in items if x["instrument_type"] == "share"]
        elif kind == "futures":
            items = [x for x in items if x["instrument_type"] == "futures"]
        elif kind == "bonds":
            items = [x for x in items if x["instrument_type"] == "bond"]

        return items[:50]
    except Exception as e:
        logger.exception("Ошибка search_instruments")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/top")
async def get_top_instruments(limit: int = 20):
    try:
        with Client(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.shares()

        items = []
        for inst in getattr(resp, "instruments", []):
            items.append({
                "ticker": getattr(inst, "ticker", "") or "",
                "figi": getattr(inst, "figi", "") or "",
                "name": getattr(inst, "name", "") or "",
                "class_code": getattr(inst, "class_code", "") or "",
                "instrument_type": "share",
                "currency": getattr(inst, "currency", "") or "",
                "lot": getattr(inst, "lot", 1) or 1,
                "min_price_increment": str(getattr(inst, "min_price_increment", "0.01") or "0.01"),
                "api_trade_available_flag": bool(getattr(inst, "api_trade_available_flag", False)),
                "for_qual_investor_flag": bool(getattr(inst, "for_qual_investor_flag", False)),
                "liquidity_flag": bool(getattr(inst, "liquidity_flag", False)),
            })

        items = [
            x for x in items
            if x["api_trade_available_flag"] and not x["for_qual_investor_flag"]
        ]

        return items[:limit]
    except Exception as e:
        logger.exception("Ошибка get_top_instruments")
        raise HTTPException(status_code=500, detail=str(e))