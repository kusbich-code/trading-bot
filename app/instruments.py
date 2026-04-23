
import asyncio
from fastapi import APIRouter, HTTPException
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

def _is_trade_available(instrument) -> bool:
    v1 = getattr(instrument, 'api_trade_available', None)
    v2 = getattr(instrument, 'api_trade_available_flag', None)
    return bool(v1) or bool(v2)

def _instrument_to_dict(inst) -> dict:
    figi = getattr(inst, 'figi', '') or ''
    ticker = getattr(inst, 'ticker', '') or ''
    name = getattr(inst, 'name', '') or ''
    currency = getattr(inst, 'currency', '') or ''
    itype = getattr(inst, 'instrument_type', '') or ''
    lot = getattr(inst, 'lot', 1)
    
    step_obj = getattr(inst, 'min_price_increment', None)
    if step_obj is not None:
        try:
            from t_tech.invest.utils import quotation_to_decimal
            step = float(quotation_to_decimal(step_obj))
        except:
            step = 0.0
    else:
        step = 0.0
    
    return {
        "figi": figi,
        "ticker": ticker,
        "name": name,
        "currency": currency,
        "instrument_type": itype,
        "instrument_kind": str(getattr(inst, 'instrument_kind', '') or ''),
        "lot": lot,
        "step": step,
        "api_trade_available": True,
    }

@router.get("/search")
async def search_instruments(q: str = ""):
    from bot import get_client_and_token
    try:
        client, token = get_client_and_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Нет клиента: {e}")
    
    if not q or len(q.strip()) < 1:
        return []
    
    q = q.strip()
    results = []
    
    try:
        async with client(token=token) as c:
            resp = await c.instruments.find_instrument(query=q)
            instruments = resp.instruments if resp else []
            logger.info(f"find_instrument({q!r}) вернул {len(instruments)} инструментов")
            
            for inst in instruments:
                if _is_trade_available(inst):
                    results.append(_instrument_to_dict(inst))
                    
            logger.info(f"После фильтра api_trade_available: {len(results)}")
            
    except Exception as e:
        logger.error(f"Ошибка search_instruments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    
    return results


@router.get("/top")
async def get_top_instruments():
    from bot import get_client_and_token
    try:
        client, token = get_client_and_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Нет клиента: {e}")
    
    results = []
    
    try:
        async with client(token=token) as c:
            try:
                resp = await c.instruments.futures()
                for inst in (resp.instruments or []):
                    if _is_trade_available(inst):
                        results.append(_instrument_to_dict(inst))
            except Exception as e:
                logger.warning(f"Ошибка futures: {e}")
            
            try:
                from t_tech.invest import InstrumentStatus
                resp = await c.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
                for inst in (resp.instruments or []):
                    if _is_trade_available(inst):
                        results.append(_instrument_to_dict(inst))
            except Exception as e:
                logger.warning(f"Ошибка shares: {e}")
                
        logger.info(f"Топ: {len(results)} доступных")
        return results[:50]
        
    except Exception as e:
        logger.error(f"Ошибка get_top_instruments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_instruments():
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config.get('instruments', [])
    except FileNotFoundError:
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add")
async def add_instrument(data: dict):
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    
    figi = data.get('figi', '').strip()
    if not figi:
        raise HTTPException(status_code=400, detail="figi обязателен")
    
    try:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except FileNotFoundError:
            config = {}
        
        instruments = config.get('instruments', [])
        
        if any(i.get('figi') == figi for i in instruments):
            return {"ok": True, "message": "Уже добавлен"}
        
        instruments.append(data)
        config['instruments'] = instruments
        
        with open