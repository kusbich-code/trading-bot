import os
import logging
logger = logging.getLogger(__name__)
from decimal import Decimal, InvalidOperation
from typing import Any, Dict
from t_tech.invest import Client

from fastapi import FastAPI, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.control import run_control
from app.db import (
    init_db,
    get_all_settings,
    set_setting,
    get_all_runtime,
    get_trade_stats_today,
    get_trades,
    get_logs,
    get_error_logs,
    get_system_logs,
    list_instruments,
    add_instrument,
    update_instrument,
    delete_instrument,
    get_open_positions,
    list_settings_profiles,
    create_settings_profile,
    activate_settings_profile,
    save_current_settings_to_profile,
    list_strategy_profiles,
    activate_strategy_profile,
    save_current_settings_to_strategy,
    get_instrument_market_state,
    get_instrument_market_state_map,
    get_setting,
)

from app.config import settings
from app.services.tbank_client import (
    search_instruments,
    get_top_shares,
    get_candles,
    get_active_stop_orders,
    cancel_stop_order,
    post_market_close,
    post_stop_bundle,
)
from app.services.strategy_engine import evaluate_signal
from app.services.healthcheck import dashboard_health
from decimal import Decimal
from app.telegram_health import send_telegram, health_snapshot

app = FastAPI(title="Trading Bot Dashboard v4.1")

if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


def safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def fmt_money(value: Any) -> str:
    return f"{safe_decimal(value):.2f}"


def fmt_pct_fraction(value: Any) -> str:
    return f"{safe_decimal(value) * Decimal('100'):.2f}"


def bool01(value: Any) -> str:
    return "1" if str(value) in ("1", "true", "True") else "0"


def market_row(row: Dict[str, Any], market_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    figi = row.get("figi", "")
    market = market_map.get(figi, {})
    out = dict(row)
    out["stop_loss_pct_ui"] = fmt_pct_fraction(row.get("stoplosspct", 0))
    out["take_profit_pct_ui"] = fmt_pct_fraction(row.get("takeprofitpct", 0))
    out["max_spread_pct_ui"] = fmt_pct_fraction(row.get("maxspreadpct", 0))
    out["last_price"] = market.get("lastprice", "0")
    out["last_price_ui"] = fmt_money(market.get("lastprice", 0))
    out["price_time"] = market.get("pricetime", "") or "-"
    return out


def summary_payload() -> Dict[str, Any]:
    s = get_all_settings()
    r = get_all_runtime()
    st = get_trade_stats_today()
    return {
        "status": s.get("status", "INIT"),
        "bot_enabled": s.get("botenabled", "1"),
        "trades_today": st.get("trades_count", 0),
        "daily_pnl_ui": fmt_money(st.get("total_pnl", 0)),
        "total_commission_ui": fmt_money(st.get("total_commission", 0)),
        "session_balance_start_ui": fmt_money(r.get("sessionbalancestart", 0)),
        "session_balance_current_ui": fmt_money(r.get("sessionbalancecurrent", 0)),
        "active_profile_name": s.get("activeprofilename", "—"),
        "active_strategy_name": s.get("activestrategyname", "—"),
        "last_error": s.get("lasterror", ""),
    }


def settings_payload() -> Dict[str, Any]:
    s = get_all_settings()
    return {
        "bot_enabled": s.get("botenabled", "1"),
        "telegram_errors_only": s.get("telegramerrorsonly", "0"),
        "auto_reload_settings": s.get("autoreloadsettings", "1"),
        "max_trades_per_day": s.get("maxtradesperday", "15"),
        "max_daily_loss_rub_ui": fmt_money(s.get("maxdailylossrub", 0)),
        "max_open_positions": s.get("maxopenpositions", "2"),
        "check_interval_sec": s.get("checkintervalsec", "5"),
        "default_stop_loss_pct_ui": fmt_pct_fraction(s.get("defaultstoplosspct", 0)),
        "default_take_profit_pct_ui": fmt_pct_fraction(s.get("defaulttakeprofitpct", 0)),
        "estimated_commission_pct_ui": fmt_pct_fraction(s.get("estimatedcommissionpct", 0)),
        "allow_long_global": s.get("allowlongglobal", "1"),
        "allow_short_global": s.get("allowshortglobal", "1"),
        "trade_only_session": s.get("tradeonlysession", "0"),
        "pause_after_error_sec": s.get("pauseaftererrorsec", "10"),
        "tinvestusesandbox": s.get("tinvestusesandbox", "true"),
        "tradingmode": s.get("tradingmode", "trend"),
        "errorseriespausecount": s.get("errorseriespausecount", "3"),
        "stopseriespausecount": s.get("stopseriespausecount", "3"),
    }


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return HTMLResponse("""<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Панель управления торговым ботом v4.0</title>
  <link rel=\"stylesheet\" href=\"/static/dashboard.css?v=4.0\">
</head>
<body>
  <div class=\"app\">
    <header class=\"topbar\">
      <div>
        <h1>Панель управления торговым ботом v4.0</h1>
        <p class=\"sub\">Русский интерфейс, отдельная справка на каждой вкладке, стабильные переходы и подготовка к режимам торговли.</p>
      </div>
      <div id=\"routeDebugBadge\" class=\"route-badge\">Вкладка: главное</div>
    </header>

    <nav class=\"tabs\">
      <a href=\"#/главное\" class=\"tab-link active\" data-tab-link=\"главное\">Главное</a>
      <a href=\"#/портфель\" class=\"tab-link\" data-tab-link=\"портфель\">Портфель</a>
      <a href=\"#/настройки\" class=\"tab-link\" data-tab-link=\"настройки\">Настройки</a>
      <a href=\"#/история\" class=\"tab-link\" data-tab-link=\"история\">История</a>
      <a href=\"#/график\" class=\"tab-link\" data-tab-link=\"график\">График</a>
    </nav>

    <section id=\"summaryCards\" class=\"summary-grid\"></section>

    <section id=\"view-main\" data-view=\"главное\"></section>
    <section id=\"view-portfolio\" data-view=\"портфель\" class=\"hidden\"></section>
    <section id=\"view-settings\" data-view=\"настройки\" class=\"hidden\"></section>
    <section id=\"view-history\" data-view=\"история\" class=\"hidden\"></section>
    <section id=\"view-chart\" data-view=\"график\" class=\"hidden\"></section>
  </div>

  <div id=\"toastHost\" class=\"toast-host\"></div>

  <div id=\"modalAddInstrument\" class=\"modal hidden\">
    <div class=\"modal-box\">
      <div class=\"row between\">
        <h2>Добавить инструменты</h2>
        <div class=\"row\">
          <input id=\"instrumentSearchInput\" class=\"field\" type=\"text\" placeholder=\"Тикер или название\">
          <button class=\"btn\" onclick=\"searchInstruments()\">Поиск</button>
          <button class=\"btn\" onclick=\"loadTopVolumeInstruments()\">Топ</button>
          <button class=\"btn btn-primary\" onclick=\"acceptSelectedInstruments()\">Добавить</button>
          <button class=\"btn btn-danger\" onclick=\"closeAddInstrumentModal()\">Закрыть</button>
        </div>
      </div>
      <div class=\"table-wrap\">
        <table>
          <thead>
            <tr><th>Выб.</th><th>Тикер</th><th>Название</th><th>FIGI</th><th>Тип</th><th>Валюта</th><th>Лот</th><th>Шаг</th><th>Цена</th><th>Время</th><th>Скор</th></tr>
          </thead>
          <tbody id=\"instrumentSearchBody\"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>
  <script src=\"/static/dashboard.js?v=4.0\"></script>
</body>
</html>""")


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    return JSONResponse(summary_payload())


@app.get("/api/dashboard/main")
def api_dashboard_main():
    market_map = get_instrument_market_state_map()
    return JSONResponse({
        "instruments": [market_row(i, market_map) for i in list_instruments()],
        "positions": [{
            **dict(p),
            "entry_price_ui": fmt_money(p.get("entryprice", 0)),
            "current_price_ui": fmt_money(p.get("currentprice", 0)),
            "unrealized_pnl_ui": fmt_money(p.get("unrealizedpnl", 0)),
            "opened_at": p.get("openedat", ""),
        } for p in get_open_positions()],
        "trades": [{
            **dict(t),
            "entry_ui": fmt_money(t.get("entry", 0)),
            "exit_ui": fmt_money(t.get("exit", 0)),
            "pnl_ui": fmt_money(t.get("pnl", 0)),
        } for t in get_trades(limit=20)],
    })


@app.get("/api/dashboard/quotes")
def api_dashboard_quotes():
    rows = get_instrument_market_state()
    return JSONResponse([{"figi": r.get("figi", ""), "ticker": r.get("ticker", ""), "last_price_ui": fmt_money(r.get("lastprice", 0)), "price_time": r.get("pricetime", "-")} for r in rows])


@app.get("/api/dashboard/portfolio")
def api_dashboard_portfolio():
    bot_positions = get_open_positions(source="BOT")
    all_positions = get_open_positions()
    return JSONResponse({
        "portfolio_positions": [{
            "ticker": p.get("ticker", ""), "figi": p.get("figi", ""), "instrument_type": "share",
            "quantity_ui": str(p.get("qty", 0)), "average_position_price_ui": fmt_money(p.get("entryprice", 0)),
            "current_price_ui": fmt_money(p.get("currentprice", 0)), "expected_yield_ui": fmt_money(p.get("unrealizedpnl", 0))
        } for p in all_positions],
        "bot_positions": [{
            "ticker": p.get("ticker", ""), "figi": p.get("figi", ""), "direction": p.get("direction", ""),
            "qty": p.get("qty", 0), "entry_price_ui": fmt_money(p.get("entryprice", 0)),
            "entry_price_raw": str(p.get("entryprice", 0)), "current_price_ui": fmt_money(p.get("currentprice", 0)),
            "unrealized_pnl_ui": fmt_money(p.get("unrealizedpnl", 0))
        } for p in bot_positions],
        "stop_orders": []
    })

@app.post("/api/stop-orders/create-bundle")
def api_create_stop_bundle(
    figi: str = Form(...),
    qty: int = Form(...),
    entry_price: str = Form(...),
    side: str = Form(...),
    stop_pct: str = Form(...),
    take_pct: str = Form(...),
):
    result = post_stop_bundle(
        figi=figi,
        quantity=int(qty),
        entry_price=Decimal(entry_price),
        side=side,
        stop_pct=Decimal(stop_pct),
        take_pct=Decimal(take_pct),
    )
    log_event("STOP_BUNDLE", f"bundle created figi={figi}", ticker=figi)
    return {"ok": True, **result}

@app.get("/api/dashboard/stop-orders")
def api_dashboard_stop_orders():
    return {"items": get_active_stop_orders()}

@app.get("/api/dashboard/settings")
def api_dashboard_settings():
    market_map = get_instrument_market_state_map()
    return JSONResponse({
        "settings": settings_payload(),
        "profiles": [{"profile_name": x.get("profilename", ""), "is_active": x.get("isactive", 0), "created_at": x.get("createdat", "")} for x in list_settings_profiles()],
        "strategies": [{"strategy_name": x.get("strategyname", ""), "is_active": x.get("isactive", 0), "created_at": x.get("createdat", "")} for x in list_strategy_profiles()],
        "instruments": [market_row(i, market_map) for i in list_instruments()],
    })


@app.get("/api/dashboard/history")
def api_dashboard_history():
    def norm(x: Dict[str, Any]) -> Dict[str, Any]:
        return {"event_time": x.get("eventtime", ""), "event_type": x.get("eventtype", ""), "ticker": x.get("ticker", ""), "level": x.get("level", ""), "message": x.get("message", "")}
    return JSONResponse({
        "trades": [{
            **dict(t),
            "entry_ui": fmt_money(t.get("entry", 0)),
            "exit_ui": fmt_money(t.get("exit", 0)),
            "commission_ui": fmt_money(t.get("commission", 0)),
            "pnl_ui": fmt_money(t.get("pnl", 0)),
            "time": t.get("time", "") or "",
        } for t in get_trades(limit=200)],
        "system_logs": [norm(x) for x in get_system_logs(limit=200)],
        "error_logs": [norm(x) for x in get_error_logs(limit=200)],
        "common_logs": [norm(x) for x in get_logs(limit=300)],
    })


@app.get("/api/dashboard/chart")
def api_dashboard_chart(figi: str = "", interval: str = "1min"):
    from app.db import get_setting
    instruments = list_instruments()
    available = [{"figi": i["figi"], "ticker": i["ticker"], "name": i.get("name", "")} for i in instruments]

    selected_figi = figi
    if not selected_figi and available:
        selected_figi = available[0]["figi"]

    candles = []
    if selected_figi:
        try:
            candles = get_candles(selected_figi, interval_name=interval, hours=8)
        except Exception as e:
            log_event("BOT_ERROR", f"chart candles error: {e}", level="ERROR")

    signal = {"action": "HOLD", "score": 0, "reasons": ["Нет данных"]}
    if candles:
        try:
            mode = get_setting("trading_mode", "trend")
            signal = evaluate_signal(selected_figi, candles, mode=mode)
        except Exception:
            pass

    return {
        "figi": selected_figi,
        "interval": interval,
        "candles": candles,
        "signal": signal,
        "available_instruments": available,
        "selected_figi": selected_figi,
    }


@app.post("/api/control/{action}")
def api_control(action: str):
    return JSONResponse(run_control(action))


@app.get("/api/health")
def api_health():
    return dashboard_health()


@app.post("/api/settings/system")
def api_settings_system(bot_enabled: str = Form("1"), telegram_errors_only: str = Form("0"), auto_reload_settings: str = Form("1")):
    set_setting("botenabled", bool01(bot_enabled))
    set_setting("telegramerrorsonly", bool01(telegram_errors_only))
    set_setting("autoreloadsettings", bool01(auto_reload_settings))
    return JSONResponse({"ok": True})


@app.post("/api/settings/strategy")
def api_settings_strategy(
    max_trades_per_day: str = Form("15"),
    max_daily_loss_rub: str = Form("200"),
    max_open_positions: str = Form("2"),
    check_interval_sec: str = Form("5"),
    default_stop_loss_pct: str = Form("0.25"),
    default_take_profit_pct: str = Form("0.50"),
    estimated_commission_pct: str = Form("0.04"),
    allow_long_global: str = Form("1"),
    allow_short_global: str = Form("1"),
    trade_only_session: str = Form("0"),
    tradingmode: str = Form("trend"),
    pause_after_error_sec: str = Form("10"),
):
    set_setting("maxtradesperday", max_trades_per_day)
    set_setting("maxdailylossrub", max_daily_loss_rub)
    set_setting("maxopenpositions", max_open_positions)
    set_setting("checkintervalsec", check_interval_sec)
    set_setting("defaultstoplosspct", str(safe_decimal(default_stop_loss_pct) / Decimal("100")))
    set_setting("defaulttakeprofitpct", str(safe_decimal(default_take_profit_pct) / Decimal("100")))
    set_setting("estimatedcommissionpct", str(safe_decimal(estimated_commission_pct) / Decimal("100")))
    set_setting("allowlongglobal", bool01(allow_long_global))
    set_setting("allowshortglobal", bool01(allow_short_global))
    set_setting("tradeonlysession", bool01(trade_only_session))
    set_setting("pauseaftererrorsec", pause_after_error_sec)
    set_setting("tradingmode", tradingmode)
    return JSONResponse({"ok": True})


@app.post("/api/settings/runtime-mode")
def api_runtime_mode(mode: str = Form(...)):
    mode = (mode or "sandbox").strip().lower()
    use_sandbox = "true" if mode == "sandbox" else "false"
    set_setting("tinvestusesandbox", use_sandbox)
    log_event("RUNTIME_MODE", f"mode switched to {mode}")
    return {"ok": True, "mode": mode, "tinvest_use_sandbox": use_sandbox}


@app.post("/api/профили/создать")
def api_create_profile(profile_name: str = Form(...)):
    name = profile_name.strip()
    create_settings_profile(name)
    save_current_settings_to_profile(name)
    return JSONResponse({"ok": True})


@app.post("/api/профили/активировать")
def api_activate_profile(profile_name: str = Form(...)):
    activate_settings_profile(profile_name.strip())
    return JSONResponse({"ok": True})


@app.post("/api/стратегии/активировать")
def api_activate_strategy(strategy_name: str = Form(...)):
    activate_strategy_profile(strategy_name.strip())
    return JSONResponse({"ok": True})


@app.post("/api/стратегии/сохранить")
def api_save_strategy(strategy_name: str = Form(...)):
    save_current_settings_to_strategy(strategy_name.strip())
    return JSONResponse({"ok": True})


@app.get("/api/instruments/search")
async def api_instruments_search(q: str, kind: str = "shares"):
    try:
        query = (q or "").strip()
        if not query:
            return []

        with Client(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.find_instrument(query=query)

        raw_items = []
        for inst in getattr(resp, "instruments", []):
            raw_items.append({
                "ticker": getattr(inst, "ticker", "") or "",
                "figi": getattr(inst, "figi", "") or "",
                "name": getattr(inst, "name", "") or "",
                "class_code": getattr(inst, "class_code", "") or "",
                "instrument_type": str(getattr(inst, "instrument_type", "") or ""),
                "uid": getattr(inst, "uid", "") or "",
                "position_uid": getattr(inst, "position_uid", "") or "",
            })

        if kind == "shares":
            raw_items = [x for x in raw_items if x["instrument_type"] == "share"]
        elif kind == "futures":
            raw_items = [x for x in raw_items if x["instrument_type"] == "futures"]
        elif kind == "bonds":
            raw_items = [x for x in raw_items if x["instrument_type"] == "bond"]

        seen = set()
        items = []
        for x in raw_items:
            key = (x["ticker"], x["class_code"], x["instrument_type"], x["figi"])
            if key in seen:
                continue
            seen.add(key)
            items.append(x)

        q_upper = query.upper()

        def score_item(x):
            score = 0
            if x["ticker"].upper() == q_upper:
                score += 1000
            elif q_upper in x["ticker"].upper():
                score += 300
            elif q_upper in x["name"].upper():
                score += 100

            if x["instrument_type"] == "share":
                score += 200
            elif x["instrument_type"] == "futures":
                score += 50

            if x["class_code"] == "TQBR":
                score += 500
            elif x["class_code"] == "TQTF":
                score += 200
            elif x["class_code"] == "SPBFUT":
                score += 100
            elif x["class_code"] in ("SMAL", "SPEQ", "BEB", "RDL"):
                score -= 50

            return score

        items.sort(key=lambda x: (-score_item(x), x["ticker"], x["name"]))
        return items[:20]

    except Exception as e:
        logger.exception("Ошибка поиска инструментов")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/instruments/top")
async def api_instruments_top(limit: int = 20):
    try:
        with Client(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.shares()

        items = []
        for inst in getattr(resp, "instruments", []):
            item = {
                "ticker": getattr(inst, "ticker", "") or "",
                "figi": getattr(inst, "figi", "") or "",
                "name": getattr(inst, "name", "") or "",
                "class_code": getattr(inst, "class_code", "") or "",
                "instrument_type": "share",
                "api_trade_available_flag": bool(getattr(inst, "api_trade_available_flag", False)),
                "for_qual_investor_flag": bool(getattr(inst, "for_qual_investor_flag", False)),
                "liquidity_flag": bool(getattr(inst, "liquidity_flag", False)),
            }
            items.append(item)

        filtered = [
            x for x in items
            if x["api_trade_available_flag"]
            and not x["for_qual_investor_flag"]
            and x["class_code"] in ("TQBR", "TQTF", "TQTD", "TQTE")
        ]

        priority_tickers = [
            "SBER", "SBERP", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "TATN",
            "YDEX", "VTBR", "T", "MOEX", "SNGS", "SNGSP", "MAGN", "CHMF",
            "ALRS", "PLZL", "IRAO", "MTSS"
        ]
        priority_map = {ticker: idx for idx, ticker in enumerate(priority_tickers)}

        def top_score(x):
            score = 0
            if x["ticker"] in priority_map:
                score += 10000 - priority_map[x["ticker"]]
            if x["class_code"] == "TQBR":
                score += 500
            if x["liquidity_flag"]:
                score += 100
            return score

        filtered.sort(key=lambda x: (-top_score(x), x["ticker"], x["name"]))
        return filtered[:limit]

    except Exception as e:
        logger.exception("Ошибка top-20 инструментов")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/instruments/add")
async def api_instruments_add(request: Request):
    payload = await request.json()
    items = payload.get("items", [])
    added = 0
    for item in items:
        if not item.get("использовать"):
            continue
        add_instrument({
            "ticker": item.get("ticker", ""), "figi": item.get("figi", ""), "name": item.get("name", ""),
            "classcode": "", "instrumenttype": item.get("instrument_type", ""), "currency": item.get("currency", ""),
            "lot": item.get("lot", 1), "minpriceincrement": item.get("min_price_increment", "0.01"),
            "lotsoverride": 1, "stoplosspct": "0.0025", "takeprofitpct": "0.005", "maxspreadpct": "0",
            "minvolume": 0, "allowlong": 1, "allowshort": 1, "priority": 100, "enabled": 1,
        })
        added += 1
    return JSONResponse({"ok": True, "добавлено": added})


@app.post("/api/instruments/update")
def api_instruments_update(
    figi: str = Form(...), lots_override: str = Form("1"), stop_loss_pct: str = Form("0.25"), take_profit_pct: str = Form("0.50"),
    max_spread_pct: str = Form("0"), min_volume: str = Form("0"), allow_long: str = Form("1"), allow_short: str = Form("1"),
    priority: str = Form("100"), enabled: str = Form("1"),
):
    update_instrument(figi, {
        "lotsoverride": lots_override,
        "stoplosspct": str(safe_decimal(stop_loss_pct) / Decimal("100")),
        "takeprofitpct": str(safe_decimal(take_profit_pct) / Decimal("100")),
        "maxspreadpct": str(safe_decimal(max_spread_pct) / Decimal("100")),
        "minvolume": min_volume,
        "allowlong": int(bool01(allow_long)),
        "allowshort": int(bool01(allow_short)),
        "priority": priority,
        "enabled": int(bool01(enabled)),
    })
    return JSONResponse({"ok": True})


@app.post("/api/health/telegram-test")
def api_health_telegram_test():
    health = dashboard_health()
    text = health_snapshot(
        dashboard_ok=(health.get("status") == "ok"),
        broker_ok=True,
        target="runtime",
        extra="manual test"
    )
    result = send_telegram(text)
    return {"ok": True, "telegram": result}


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    return JSONResponse({"ok": True})


@app.post("/api/позиции/закрыть")
def api_close_position(figi: str = Form(...), qty: int = Form(...), direction: str = Form(...)):
    close_direction = "LONG_CLOSE" if str(direction).upper() == "BUY" else "SHORT_CLOSE"
    result = post_market_close(figi=figi, quantity=int(qty), direction=close_direction)
    log_event("POSITION_CLOSE", f"close order posted figi={figi} qty={qty} direction={close_direction}", ticker=figi)
    return {"ok": True, "message": "close order posted", "order_id": getattr(result, "order_id", "")}

@app.post("/api/stop-orders/create-bundle")
def api_create_stop_bundle(
    figi: str = Form(...),
    qty: int = Form(...),
    entry_price: str = Form(...),
    side: str = Form(...),
    stop_pct: str = Form(...),
    take_pct: str = Form(...),
):
    result = post_stop_bundle(
        figi=figi,
        quantity=int(qty),
        entry_price=Decimal(entry_price),
        side=side,
        stop_pct=Decimal(stop_pct),
        take_pct=Decimal(take_pct),
    )
    log_event("STOP_BUNDLE", f"bundle created figi={figi}", ticker=figi)
    return {"ok": True, **result}

@app.post("/api/позиции/закрыть-все")
def api_close_all_positions():
    positions = get_open_positions(source="BOT")
    closed = 0
    errors = []
    for p in positions:
        try:
            figi = p.get("figi", "")
            qty = int(p.get("qty", 0))
            direction = str(p.get("direction", "BUY")).upper()
            if not figi or qty <= 0:
                continue
            close_direction = "LONG_CLOSE" if direction == "BUY" else "SHORT_CLOSE"
            post_market_close(figi=figi, quantity=qty, direction=close_direction)
            log_event("POSITION_CLOSE", f"close-all: figi={figi} qty={qty}", ticker=figi)
            closed += 1
        except Exception as e:
            errors.append(str(e))
    return JSONResponse({"ok": True, "closed": closed, "errors": errors})


@app.post("/api/стоп-заявки/создать")
def api_create_stop_orders(figi: str = Form(...), qty: str = Form(...), side: str = Form(...), base_price: str = Form(...), stop_loss_pct: str = Form("0.25"), take_profit_pct: str = Form("0.50")):
    return JSONResponse({"ok": True, "message": f"stops create requested for {figi}", "figi": figi, "qty": qty, "side": side, "base_price": base_price, "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct})


@app.post("/api/стоп-заявки/отменить")
def api_cancel_stop_order(stop_order_id: str = Form(...)):
    return JSONResponse({"ok": True, "message": f"stop cancel requested: {stop_order_id}"})


@app.get("/api/debug/search")
def api_debug_search(q: str = "SBER"):
    import traceback
    from app.config import settings
    from t_tech.invest import Client
    from t_tech.invest.sandbox.client import SandboxClient

    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client
    try:
        with client_cls(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.find_instrument(query=q)
            instruments = getattr(resp, "instruments", [])
            # Показываем все инструменты с их типами
            all_items = []
            for x in instruments:
                all_items.append({
                    "ticker": getattr(x, "ticker", ""),
                    "figi": getattr(x, "figi", ""),
                    "name": getattr(x, "name", ""),
                    "instrument_type": str(getattr(x, "instrument_type", "")),
                    "instrument_kind": str(getattr(x, "instrument_kind", "")),
                    "api_trade_available": getattr(x, "api_trade_available_flag", False),
                })
            return {
                "ok": True,
                "count": len(instruments),
                "items": all_items,
            }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }