from decimal import Decimal
from datetime import datetime

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from t_tech.invest import Client, OrderDirection, OrderType
from t_tech.invest.sandbox.client import SandboxClient

from app.config import settings
from app.db import (
    init_db,
    get_setting,
    set_setting,
    get_runtime,
    get_all_settings,
    get_trades,
    get_logs,
    get_system_logs,
    get_error_logs,
    list_instruments,
    list_enabled_instruments,
    add_instrument,
    update_instrument,
    delete_instrument,
    log_event,
    get_open_positions,
    list_settings_profiles,
    create_settings_profile,
    activate_settings_profile,
    save_current_settings_to_profile,
    get_trade_stats_today,
    list_strategy_profiles,
    activate_strategy_profile,
    save_current_settings_to_strategy,
    get_instrument_market_state,
)
from app.instruments import (
    find_instruments,
    get_last_prices_for_figis,
    get_volume_top20_instruments,
)
from app.control import run_control
from app.stop_orders import (
    create_take_profit_order,
    create_stop_loss_order,
    get_stop_orders,
    cancel_stop_order,
)

app = FastAPI(title="Панель управления торговым ботом v3.5.1")
init_db()


def get_client_cls():
    return SandboxClient if settings.TINVEST_USE_SANDBOX else Client


def get_client():
    client_cls = get_client_cls()
    return client_cls(settings.TINVEST_TOKEN)


def fmt2(value):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "0.00"


def close_position_market(client, figi: str, quantity: int, direction: str):
    if direction.upper() in ("BUY", "LONG"):
        close_direction = OrderDirection.ORDER_DIRECTION_SELL
    else:
        close_direction = OrderDirection.ORDER_DIRECTION_BUY

    return client.orders.post_order(
        figi=figi,
        quantity=int(quantity),
        direction=close_direction,
        account_id=settings.TINVEST_ACCOUNT_ID,
        order_type=OrderType.ORDER_TYPE_MARKET,
        order_id=f"close-{figi}-{int(datetime.now().timestamp())}",
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    trade_stats_today = get_trade_stats_today(today_prefix)

    return {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": fmt2(get_runtime("daily_pnl", "0")),
        "trades_today": int(float(get_runtime("trades_today", "0") or 0)),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": fmt2(get_runtime("session_balance_start", "0")),
        "session_balance_current": fmt2(get_runtime("session_balance_current", "0")),
        "bot_enabled": get_setting("bot_enabled", "1"),
        "active_profile_name": get_setting("active_profile_name", "Основной"),
        "active_strategy_name": get_setting("active_strategy_name", "Сбалансированный"),
        "total_commission": fmt2(trade_stats_today.get("total_commission", 0)),
        "history_trades_today": int(float(trade_stats_today.get("trades_count", 0) or 0)),
    }


@app.get("/api/dashboard/main")
def api_dashboard_main():
    instruments = list_enabled_instruments()
    quotes = {x["figi"]: x for x in get_instrument_market_state()}
    open_positions = get_open_positions()
    trades = get_trades(limit=30)

    result_instruments = []
    for i in instruments:
        q = quotes.get(i["figi"], {})
        result_instruments.append({
            "ticker": i["ticker"],
            "name": i["name"],
            "enabled": i["enabled"],
            "lots_override": i["lots_override"],
            "stop_loss_pct": fmt2(i["stop_loss_pct"]),
            "take_profit_pct": fmt2(i["take_profit_pct"]),
            "last_price": fmt2(q.get("last_price", 0)),
            "price_time": q.get("price_time", "-"),
        })

    result_positions = []
    for p in open_positions:
        result_positions.append({
            "ticker": p["ticker"],
            "figi": p["figi"],
            "direction": p["direction"],
            "qty": int(float(p["qty"] or 0)),
            "entry_price": fmt2(p["entry_price"]),
            "current_price": fmt2(p["current_price"]),
            "unrealized_pnl": fmt2(p["unrealized_pnl"]),
            "opened_at": p["opened_at"],
        })

    result_trades = []
    for t in trades:
        result_trades.append({
            "time": t["time"],
            "ticker": t["ticker"],
            "direction": t["direction"],
            "entry": fmt2(t["entry"]),
            "exit": fmt2(t["exit"]),
            "qty": int(float(t["qty"] or 0)),
            "pnl": fmt2(t["pnl"]),
            "reason": t["reason"],
        })

    return {
        "instruments": result_instruments,
        "positions": result_positions,
        "trades": result_trades,
    }


@app.get("/api/dashboard/portfolio")
def api_dashboard_portfolio():
    db_positions = get_open_positions()
    stop_orders_result = []

    portfolio_positions = []
    try:
        with get_client() as client:
            portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
            for p in getattr(portfolio, "positions", []) or []:
                portfolio_positions.append({
                    "figi": getattr(p, "figi", ""),
                    "ticker": getattr(p, "ticker", "") or getattr(p, "figi", ""),
                    "instrument_type": getattr(p, "instrument_type", ""),
                    "quantity": str(getattr(p, "quantity", "")),
                    "average_position_price": fmt2(getattr(p, "average_position_price", 0)),
                    "current_price": fmt2(getattr(p, "current_price", 0)),
                    "expected_yield": fmt2(getattr(p, "expected_yield", 0)),
                })

            for x in get_stop_orders(client, settings.TINVEST_ACCOUNT_ID):
                stop_orders_result.append({
                    "stop_order_id": getattr(x, "stop_order_id", ""),
                    "figi": getattr(x, "figi", ""),
                    "quantity": int(getattr(x, "lots_requested", 0) or 0),
                    "currency": getattr(x, "currency", ""),
                    "order_type": str(getattr(x, "stop_order_type", "")),
                    "direction": str(getattr(x, "direction", "")),
                })
    except Exception as e:
        log_event("BOT_ERROR", f"Ошибка загрузки портфеля: {e}", level="ERROR")

    positions = []
    for p in db_positions:
        positions.append({
            "ticker": p["ticker"],
            "figi": p["figi"],
            "direction": p["direction"],
            "qty": int(float(p["qty"] or 0)),
            "entry_price": fmt2(p["entry_price"]),
            "current_price": fmt2(p["current_price"]),
            "unrealized_pnl": fmt2(p["unrealized_pnl"]),
        })

    return {
        "portfolio_positions": portfolio_positions,
        "bot_positions": positions,
        "stop_orders": stop_orders_result,
    }


@app.get("/api/dashboard/history")
def api_dashboard_history():
    trades = get_trades(limit=200)
    system_logs = get_system_logs(limit=200)
    error_logs = get_error_logs(limit=200)
    common_logs = get_logs(limit=200)

    result_trades = []
    for x in trades:
        result_trades.append({
            "time": x["time"],
            "ticker": x["ticker"],
            "direction": x["direction"],
            "entry": fmt2(x["entry"]),
            "exit": fmt2(x["exit"]),
            "qty": int(float(x["qty"] or 0)),
            "commission": fmt2(x["commission"]),
            "pnl": fmt2(x["pnl"]),
            "reason": x["reason"],
        })

    def map_logs(rows):
        result = []
        for x in rows:
            result.append({
                "event_time": x["event_time"],
                "event_type": x["event_type"],
                "ticker": x["ticker"],
                "level": x["level"],
                "message": x["message"],
            })
        return result

    return {
        "trades": result_trades,
        "system_logs": map_logs(system_logs),
        "error_logs": map_logs(error_logs),
        "common_logs": map_logs(common_logs),
    }


@app.get("/api/dashboard/settings")
def api_dashboard_settings():
    return {
        "settings": get_all_settings(),
        "profiles": list_settings_profiles(),
        "strategies": list_strategy_profiles(),
        "instruments": list_instruments(),
    }


@app.get("/api/dashboard/quotes")
def api_dashboard_quotes():
    rows = []
    for x in get_instrument_market_state():
        rows.append({
            "figi": x["figi"],
            "ticker": x["ticker"],
            "last_price": fmt2(x["last_price"]),
            "price_time": x["price_time"],
            "volume_1m": int(float(x.get("volume_1m", 0) or 0)),
        })
    return rows


@app.get("/api/стоп-заявки")
def api_stop_orders():
    with get_client() as client:
        orders = get_stop_orders(client, settings.TINVEST_ACCOUNT_ID)
        result = []
        for x in orders:
            result.append({
                "stop_order_id": getattr(x, "stop_order_id", ""),
                "figi": getattr(x, "figi", ""),
                "quantity": int(getattr(x, "lots_requested", 0) or 0),
                "currency": getattr(x, "currency", ""),
                "order_type": str(getattr(x, "stop_order_type", "")),
                "direction": str(getattr(x, "direction", "")),
            })
        return JSONResponse(result)


@app.get("/api/instruments/search")
def api_instruments_search(q: str = Query("", min_length=0), mode: str = "popular"):
    with get_client() as client:
        if q.strip():
            items = find_instruments(client, q.strip())
            for x in items:
                x["использовать"] = False

            figis = [x["figi"] for x in items if x.get("figi")]
            prices = get_last_prices_for_figis(client, figis)
            for item in items:
                px = prices.get(item["figi"], {})
                item["last_price"] = fmt2(px.get("last_price", 0))
                item["price_time"] = px.get("price_time", "")
            return JSONResponse(items)

        if mode == "top-volume":
            items = get_volume_top20_instruments(client)
        else:
            items = get_volume_top20_instruments(client)

        figis = [x["figi"] for x in items if x.get("figi")]
        prices = get_last_prices_for_figis(client, figis)

        for item in items:
            px = prices.get(item["figi"], {})
            item["last_price"] = fmt2(px.get("last_price", 0))
            item["price_time"] = px.get("price_time", "")
            item["volume_score"] = int(item.get("volume_score", 0) or 0)

        return JSONResponse(items)


@app.post("/api/instruments/add")
async def api_instruments_add(request: Request):
    data = await request.json()
    items = data.get("items", [])

    default_sl = get_setting("default_stop_loss_pct", "0.0025")
    default_tp = get_setting("default_take_profit_pct", "0.0050")

    added = 0
    for item in items:
        if not item.get("использовать"):
            continue

        payload = {
            "ticker": item.get("ticker", ""),
            "figi": item.get("figi", ""),
            "name": item.get("name", ""),
            "class_code": item.get("class_code", ""),
            "instrument_type": item.get("instrument_type", ""),
            "currency": item.get("currency", ""),
            "lot": int(item.get("lot", 1)),
            "min_price_increment": item.get("min_price_increment", "0.01"),
            "lots_override": int(item.get("lots_override", 1)),
            "stop_loss_pct": item.get("stop_loss_pct", default_sl),
            "take_profit_pct": item.get("take_profit_pct", default_tp),
            "max_spread_pct": item.get("max_spread_pct", "0"),
            "min_volume": int(item.get("min_volume", 0)),
            "allow_long": int(item.get("allow_long", 1)),
            "allow_short": int(item.get("allow_short", 1)),
            "priority": int(item.get("priority", 100)),
            "enabled": 1,
        }
        add_instrument(payload)
        log_event("CONFIG_CHANGED", f"Добавлен инструмент {payload['ticker']}", ticker=payload["ticker"])
        added += 1

    return {"ok": True, "добавлено": added}


@app.post("/api/instruments/update")
def api_instruments_update(
    figi: str = Form(...),
    lots_override: int = Form(...),
    stop_loss_pct: str = Form(...),
    take_profit_pct: str = Form(...),
    max_spread_pct: str = Form(...),
    min_volume: int = Form(...),
    allow_long: int = Form(...),
    allow_short: int = Form(...),
    priority: int = Form(...),
    enabled: int = Form(...)
):
    update_instrument(figi, {
        "lots_override": lots_override,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "max_spread_pct": max_spread_pct,
        "min_volume": min_volume,
        "allow_long": allow_long,
        "allow_short": allow_short,
        "priority": priority,
        "enabled": enabled,
    })
    log_event("CONFIG_CHANGED", f"Обновлён инструмент figi={figi}")
    return {"ok": True}


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    log_event("CONFIG_CHANGED", f"Удалён инструмент figi={figi}")
    return {"ok": True}


@app.post("/api/settings/system")
def api_settings_system(
    bot_enabled: str = Form(...),
    telegram_errors_only: str = Form(...),
    auto_reload_settings: str = Form(...),
):
    set_setting("bot_enabled", bot_enabled)
    set_setting("telegram_errors_only", telegram_errors_only)
    set_setting("auto_reload_settings", auto_reload_settings)
    log_event("CONFIG_CHANGED", "Обновлены системные настройки")
    return {"ok": True}


@app.post("/api/settings/strategy")
def api_settings_strategy(
    max_trades_per_day: str = Form(...),
    max_daily_loss_rub: str = Form(...),
    max_open_positions: str = Form(...),
    check_interval_sec: str = Form(...),
    default_stop_loss_pct: str = Form(...),
    default_take_profit_pct: str = Form(...),
    estimated_commission_pct: str = Form(...),
    allow_long_global: str = Form(...),
    allow_short_global: str = Form(...),
    trade_only_session: str = Form(...),
    pause_after_error_sec: str = Form(...),
):
    set_setting("max_trades_per_day", max_trades_per_day)
    set_setting("max_daily_loss_rub", max_daily_loss_rub)
    set_setting("max_open_positions", max_open_positions)
    set_setting("check_interval_sec", check_interval_sec)
    set_setting("default_stop_loss_pct", default_stop_loss_pct)
    set_setting("default_take_profit_pct", default_take_profit_pct)
    set_setting("estimated_commission_pct", estimated_commission_pct)
    set_setting("allow_long_global", allow_long_global)
    set_setting("allow_short_global", allow_short_global)
    set_setting("trade_only_session", trade_only_session)
    set_setting("pause_after_error_sec", pause_after_error_sec)
    log_event("CONFIG_CHANGED", "Обновлены настройки стратегии торговли")
    return {"ok": True}


@app.post("/api/профили/создать")
def api_create_profile(profile_name: str = Form(...)):
    create_settings_profile(profile_name)
    save_current_settings_to_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Создан профиль настроек {profile_name}")
    return {"ok": True}


@app.post("/api/профили/активировать")
def api_activate_profile(profile_name: str = Form(...)):
    activate_settings_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Активирован профиль настроек {profile_name}")
    return {"ok": True}


@app.post("/api/стратегии/активировать")
def api_activate_strategy(strategy_name: str = Form(...)):
    activate_strategy_profile(strategy_name)
    log_event("CONFIG_CHANGED", f"Активирована стратегия торговли {strategy_name}")
    return {"ok": True}


@app.post("/api/стратегии/сохранить")
def api_save_strategy(strategy_name: str = Form(...)):
    save_current_settings_to_strategy(strategy_name)
    log_event("CONFIG_CHANGED", f"Сохранена стратегия торговли {strategy_name}")
    return {"ok": True}


@app.post("/api/control/{action}")
def api_control(action: str):
    return JSONResponse(run_control(action))


@app.post("/api/позиции/закрыть")
def api_close_position(figi: str = Form(...), qty: int = Form(...), direction: str = Form(...)):
    with get_client() as client:
        close_position_market(client, figi, qty, direction)
    log_event("ORDER_CLOSE_MANUAL", f"Ручное закрытие позиции figi={figi}")
    return {"ok": True}


@app.post("/api/позиции/закрыть-все")
def api_close_all_positions():
    positions = get_open_positions()
    with get_client() as client:
        for p in positions:
            try:
                close_position_market(client, p["figi"], int(p["qty"]), p["direction"])
                log_event("ORDER_CLOSE_MANUAL", f"Закрыты все: {p['ticker']}", ticker=p["ticker"])
            except Exception as e:
                log_event("BOT_ERROR", f"Ошибка закрытия {p['ticker']}: {e}", ticker=p["ticker"], level="ERROR")
    return {"ok": True}


@app.post("/api/стоп-заявки/создать")
def api_create_stop_order(
    figi: str = Form(...),
    qty: int = Form(...),
    base_price: str = Form(...),
    side: str = Form(...),
    stop_loss_pct: str = Form(...),
    take_profit_pct: str = Form(...),
):
    with get_client() as client:
        if Decimal(stop_loss_pct) > 0:
            create_stop_loss_order(
                client=client,
                account_id=settings.TINVEST_ACCOUNT_ID,
                figi=figi,
                quantity=qty,
                base_price=Decimal(base_price),
                percent=Decimal(stop_loss_pct),
                side=side,
            )
        if Decimal(take_profit_pct) > 0:
            create_take_profit_order(
                client=client,
                account_id=settings.TINVEST_ACCOUNT_ID,
                figi=figi,
                quantity=qty,
                base_price=Decimal(base_price),
                percent=Decimal(take_profit_pct),
                side=side,
            )
    log_event("CONFIG_CHANGED", f"Созданы стоп-заявки для {figi}")
    return {"ok": True}


@app.post("/api/стоп-заявки/отменить")
def api_cancel_stop_order(stop_order_id: str = Form(...)):
    with get_client() as client:
        cancel_stop_order(client, settings.TINVEST_ACCOUNT_ID, stop_order_id)
    log_event("CONFIG_CHANGED", f"Отменена стоп-заявка {stop_order_id}")
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = """
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Панель управления торговым ботом v3.5.1</title>
        <style>
            :root {
                --bg: #0b1020;
                --surface: #121933;
                --surface-2: #1a2345;
                --text: #edf2ff;
                --muted: #9fb0d3;
                --border: #2a3868;
                --primary: #4c8dff;
                --danger: #d95468;
                --success: #2fbf71;
                --shadow: 0 10px 30px rgba(0,0,0,0.25);
                --radius: 16px;
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                font-family: Inter, Arial, sans-serif;
                background: linear-gradient(180deg, #0b1020 0%, #0f1630 100%);
                color: var(--text);
            }
            .container {
                max-width: 1600px;
                margin: 0 auto;
                padding: 24px;
            }
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 16px;
                margin-bottom: 20px;
                flex-wrap: wrap;
            }
            .title {
                font-size: 30px;
                font-weight: 800;
            }
            .subtitle {
                color: var(--muted);
                margin-top: 6px;
            }
            .tabs {
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-bottom: 20px;
            }
            .tab-link {
                text-decoration: none;
                color: var(--text);
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 999px;
                padding: 10px 16px;
            }
            .tab-link.active {
                background: var(--primary);
                border-color: var(--primary);
                color: white;
            }
            .cards {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 14px;
                margin-bottom: 20px;
            }
            .card, .block {
                background: rgba(18, 25, 51, 0.95);
                border: 1px solid var(--border);
                border-radius: var(--radius);
                box-shadow: var(--shadow);
            }
            .card { padding: 18px; }
            .label { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
            .value { font-size: 24px; font-weight: 700; line-height: 1.2; word-break: break-word; }
            .block { padding: 20px; margin-bottom: 20px; }
            .block-head {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 16px;
                flex-wrap: wrap;
            }
            .block h2 { margin: 0; font-size: 22px; }
            .note { color: var(--muted); font-size: 14px; }
            .row-buttons { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
            .btn, button {
                border: none;
                border-radius: 12px;
                padding: 10px 14px;
                font-weight: 700;
                cursor: pointer;
                background: var(--surface-2);
                color: var(--text);
            }
            .btn-primary { background: var(--primary); color: white; }
            .btn-danger { background: var(--danger); color: white; }
            .btn-success { background: var(--success); color: white; }
            .two-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
            label { display: flex; flex-direction: column; gap: 8px; font-size: 14px; color: var(--muted); }
            .field, input, select {
                width: 100%;
                padding: 11px 12px;
                border-radius: 12px;
                border: 1px solid var(--border);
                background: #0f1630;
                color: var(--text);
            }
            .table-wrap { overflow-x: auto; }
            table { width: 100%; border-collapse: collapse; }
            th, td {
                text-align: left;
                padding: 12px 10px;
                border-bottom: 1px solid var(--border);
                vertical-align: top;
                font-size: 14px;
            }
            th { color: #7aa8ff; }
            .hidden { display: none; }
            .modal-bg {
                position: fixed;
                inset: 0;
                background: rgba(5, 9, 20, 0.72);
                display: none;
                align-items: center;
                justify-content: center;
                padding: 20px;
                z-index: 999;
            }
            .modal {
                width: min(1280px, 96vw);
                max-height: 90vh;
                overflow: auto;
                background: #0f1630;
                border: 1px solid var(--border);
                border-radius: 20px;
                padding: 20px;
                box-shadow: var(--shadow);
            }
            .modal-top {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                margin-bottom: 16px;
                flex-wrap: wrap;
            }
            @media (max-width: 980px) {
                .two-cols { grid-template-columns: 1fr; }
                .value { font-size: 20px; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>
                    <div class="title">Панель управления торговым ботом v3.5.1</div>
                    <div class="subtitle">Hash-router, AJAX-обновление, live-котировки, top-20 по объёму, округление до 2 знаков</div>
                </div>
                <div class="note">Карточки: 7 сек, котировки: 5 сек</div>
            </div>

            <nav class="tabs" id="tabs">
                <a href="#/главное" class="tab-link" data-tab="главное">Главное</a>
                <a href="#/портфель" class="tab-link" data-tab="портфель">Портфель</a>
                <a href="#/настройки" class="tab-link" data-tab="настройки">Настройки</a>
                <a href="#/история" class="tab-link" data-tab="история">История и логи</a>
            </nav>

            <section class="cards" id="summaryCards"></section>

            <section id="view-главное" class="view"></section>
            <section id="view-портфель" class="view hidden"></section>
            <section id="view-настройки" class="view hidden"></section>
            <section id="view-история" class="view hidden"></section>
        </div>

        <div class="modal-bg" id="modalAddInstrument">
            <div class="modal">
                <div class="modal-top">
                    <div>
                        <h3>Добавление инструментов</h3>
                        <div class="note">По умолчанию загружается реальный top-20 по объёму. Использовать = ложь, пока ты сам не отметишь нужное.</div>
                    </div>
                    <button class="btn" onclick="closeAddInstrumentModal()">Закрыть</button>
                </div>

                <div class="form-grid">
                    <label>Поиск по тикеру/названию
                        <input class="field" type="text" id="instrumentSearchInput" placeholder="SBER, GAZP, LKOH...">
                    </label>
                </div>

                <div class="row-buttons">
                    <button class="btn btn-primary" onclick="searchInstruments()">Поиск</button>
                    <button class="btn" onclick="loadTopVolumeInstruments()">Top-20 по объёму</button>
                    <button class="btn btn-success" onclick="acceptSelectedInstruments()">Принять выбранные</button>
                </div>

                <div class="table-wrap" style="margin-top:16px;">
                    <table>
                        <thead>
                            <tr>
                                <th>Использовать</th>
                                <th>Тикер</th>
                                <th>Название</th>
                                <th>FIGI</th>
                                <th>Тип</th>
                                <th>Валюта</th>
                                <th>Лот</th>
                                <th>Шаг цены</th>
                                <th>Последняя цена</th>
                                <th>Время цены</th>
                                <th>Объём score</th>
                            </tr>
                        </thead>
                        <tbody id="instrumentSearchBody"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            const REFRESH_MAIN_MS = 7000;
            const REFRESH_QUOTES_MS = 5000;
            let instrumentSearchData = [];

            function esc(v) {
                return String(v ?? '');
            }

            function yesnoValue(v) {
                return String(v) === '1' ? 'Да' : 'Нет';
            }

            function currentTab() {
                const raw = location.hash || '#/главное';
                const parts = raw.replace(/^#\\//, '').split('/');
                const tab = parts[0] || 'главное';
                const allowed = ['главное', 'портфель', 'настройки', 'история'];
                return allowed.includes(tab) ? tab : 'главное';
            }

            function showTab(tab) {
                document.querySelectorAll('.view').forEach(el => el.classList.add('hidden'));
                const target = document.getElementById(`view-${tab}`);
                if (target) target.classList.remove('hidden');

                document.querySelectorAll('.tab-link').forEach(a => {
                    a.classList.toggle('active', a.dataset.tab === tab);
                });
            }

            function initRouter() {
                if (!location.hash) {
                    location.hash = '#/главное';
                }
                showTab(currentTab());
                window.addEventListener('hashchange', () => {
                    showTab(currentTab());
                    renderCurrentTab();
                });
            }

            async function apiGet(url) {
                const r = await fetch(url);
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.json();
            }

            async function apiPostForm(url, data) {
                const body = new URLSearchParams();
                for (const [k, v] of Object.entries(data)) body.append(k, v);
                const r = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: body.toString(),
                });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.json();
            }

            async function renderSummaryCards() {
                const s = await apiGet('/api/dashboard/summary');
                const html = `
                    <div class="card"><div class="label">Статус</div><div class="value">${esc(s.status)}</div></div>
                    <div class="card"><div class="label">Торговля</div><div class="value">${s.bot_enabled === '1' ? 'Включена' : 'Выключена'}</div></div>
                    <div class="card"><div class="label">Сделок сегодня</div><div class="value">${esc(s.trades_today)}</div></div>
                    <div class="card"><div class="label">PNL за день</div><div class="value">${esc(s.daily_pnl)}</div></div>
                    <div class="card"><div class="label">Комиссии за день</div><div class="value">${esc(s.total_commission)}</div></div>
                    <div class="card"><div class="label">Баланс на старте</div><div class="value">${esc(s.session_balance_start)}</div></div>
                    <div class="card"><div class="label">Текущий баланс</div><div class="value">${esc(s.session_balance_current)}</div></div>
                    <div class="card"><div class="label">Профиль настроек</div><div class="value">${esc(s.active_profile_name)}</div></div>
                    <div class="card"><div class="label">Стратегия торговли</div><div class="value">${esc(s.active_strategy_name)}</div></div>
                    <div class="card"><div class="label">Последняя ошибка</div><div class="value">${esc(s.last_error || '-')}</div></div>
                `;
                document.getElementById('summaryCards').innerHTML = html;
            }

            async function renderMainTab() {
                const data = await apiGet('/api/dashboard/main');

                const instrumentsRows = data.instruments.map(i => `
                    <tr data-figi="${esc(i.figi || '')}">
                        <td>${esc(i.ticker)}</td>
                        <td>${esc(i.name)}</td>
                        <td>${yesnoValue(i.enabled)}</td>
                        <td>${esc(i.lots_override)}</td>
                        <td>${esc(i.stop_loss_pct)}</td>
                        <td>${esc(i.take_profit_pct)}</td>
                        <td class="live-price" data-figi="${esc(i.figi || '')}">${esc(i.last_price)}</td>
                        <td class="live-time" data-figi="${esc(i.figi || '')}">${esc(i.price_time)}</td>
                    </tr>
                `).join('');

                const positionsRows = data.positions.map(p => `
                    <tr>
                        <td>${esc(p.ticker)}</td>
                        <td>${esc(p.direction)}</td>
                        <td>${esc(p.qty)}</td>
                        <td>${esc(p.entry_price)}</td>
                        <td>${esc(p.current_price)}</td>
                        <td>${esc(p.unrealized_pnl)}</td>
                        <td>${esc(p.opened_at)}</td>
                    </tr>
                `).join('');

                const tradesRows = data.trades.map(t => `
                    <tr>
                        <td>${esc(t.time)}</td>
                        <td>${esc(t.ticker)}</td>
                        <td>${esc(t.direction)}</td>
                        <td>${esc(t.entry)}</td>
                        <td>${esc(t.exit)}</td>
                        <td>${esc(t.qty)}</td>
                        <td>${esc(t.pnl)}</td>
                        <td>${esc(t.reason)}</td>
                    </tr>
                `).join('');

                document.getElementById('view-главное').innerHTML = `
                    <section class="block">
                        <div class="block-head">
                            <h2>Управление торговлей и сервисом</h2>
                        </div>
                        <div class="row-buttons">
                            <button class="btn btn-primary" onclick="serviceAction('start')">Запустить</button>
                            <button class="btn" onclick="serviceAction('stop')">Остановить</button>
                            <button class="btn" onclick="serviceAction('restart')">Перезапустить</button>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head">
                            <h2>Выбранные инструменты</h2>
                            <div class="note">Live-котировки обновляются без полной перезагрузки страницы</div>
                        </div>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Тикер</th>
                                        <th>Название</th>
                                        <th>Использовать</th>
                                        <th>Лотов</th>
                                        <th>SL</th>
                                        <th>TP</th>
                                        <th>Последняя цена</th>
                                        <th>Время цены</th>
                                    </tr>
                                </thead>
                                <tbody id="mainInstrumentsBody">${instrumentsRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="two-cols">
                        <div class="block">
                            <div class="block-head"><h2>Открытые позиции</h2></div>
                            <div class="table-wrap">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Тикер</th>
                                            <th>Направление</th>
                                            <th>Количество</th>
                                            <th>Цена входа</th>
                                            <th>Текущая цена</th>
                                            <th>Нереализованный PNL</th>
                                            <th>Время открытия</th>
                                        </tr>
                                    </thead>
                                    <tbody>${positionsRows}</tbody>
                                </table>
                            </div>
                        </div>

                        <div class="block">
                            <div class="block-head"><h2>Последние сделки</h2></div>
                            <div class="table-wrap">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Время</th>
                                            <th>Тикер</th>
                                            <th>Направление</th>
                                            <th>Вход</th>
                                            <th>Выход</th>
                                            <th>Кол-во</th>
                                            <th>PNL</th>
                                            <th>Причина</th>
                                        </tr>
                                    </thead>
                                    <tbody>${tradesRows}</tbody>
                                </table>
                            </div>
                        </div>
                    </section>
                `;
            }

            async function renderPortfolioTab() {
                const data = await apiGet('/api/dashboard/portfolio');

                const portfolioRows = data.portfolio_positions.map(p => `
                    <tr>
                        <td>${esc(p.ticker)}</td>
                        <td>${esc(p.figi)}</td>
                        <td>${esc(p.instrument_type)}</td>
                        <td>${esc(p.quantity)}</td>
                        <td>${esc(p.average_position_price)}</td>
                        <td>${esc(p.current_price)}</td>
                        <td>${esc(p.expected_yield)}</td>
                    </tr>
                `).join('');

                const botRows = data.bot_positions.map(p => `
                    <tr>
                        <td>${esc(p.ticker)}</td>
                        <td>${esc(p.figi)}</td>
                        <td>${esc(p.direction)}</td>
                        <td>${esc(p.qty)}</td>
                        <td>${esc(p.entry_price)}</td>
                        <td>${esc(p.current_price)}</td>
                        <td>${esc(p.unrealized_pnl)}</td>
                        <td><button class="btn btn-danger" onclick="closeOnePosition('${esc(p.figi)}', '${esc(p.qty)}', '${esc(p.direction)}')">Закрыть</button></td>
                    </tr>
                `).join('');

                const stopRows = data.stop_orders.map(s => `
                    <tr>
                        <td>${esc(s.stop_order_id)}</td>
                        <td>${esc(s.figi)}</td>
                        <td>${esc(s.quantity)}</td>
                        <td>${esc(s.currency)}</td>
                        <td>${esc(s.order_type)}</td>
                        <td>${esc(s.direction)}</td>
                        <td><button class="btn btn-danger" onclick="cancelStopOrder('${esc(s.stop_order_id)}')">Отменить</button></td>
                    </tr>
                `).join('');

                const stopOptions = data.bot_positions.map(p => `
                    <option value="${esc(p.figi)}|${esc(p.qty)}|${esc(p.direction)}|${esc(p.entry_price)}">${esc(p.ticker)} | ${esc(p.figi)}</option>
                `).join('');

                document.getElementById('view-портфель').innerHTML = `
                    <section class="block">
                        <div class="block-head">
                            <h2>Портфель по счёту</h2>
                            <div class="note">Данные подтягиваются отдельно, без перерисовки всей страницы</div>
                        </div>
                        <div class="table-wrap">
                            <table id="portfolioTable">
                                <thead>
                                    <tr>
                                        <th>Тикер</th>
                                        <th>FIGI</th>
                                        <th>Тип</th>
                                        <th>Количество</th>
                                        <th>Средняя цена</th>
                                        <th>Текущая цена</th>
                                        <th>Ожидаемая доходность</th>
                                    </tr>
                                </thead>
                                <tbody>${portfolioRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head">
                            <h2>Открытые позиции бота</h2>
                            <div class="row-buttons">
                                <button class="btn btn-danger" onclick="closeAllPositionsConfirm()">Закрыть все позиции</button>
                            </div>
                        </div>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Тикер</th>
                                        <th>FIGI</th>
                                        <th>Направление</th>
                                        <th>Количество</th>
                                        <th>Цена входа</th>
                                        <th>Текущая цена</th>
                                        <th>PNL</th>
                                        <th>Действие</th>
                                    </tr>
                                </thead>
                                <tbody>${botRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="two-cols">
                        <div class="block">
                            <div class="block-head"><h2>Создать стоп-заявки</h2></div>
                            <form id="stopOrderForm" class="form-grid">
                                <label>Позиция
                                    <select class="field" id="positionForStops">${stopOptions}</select>
                                </label>
                                <label>Базовая цена
                                    <input class="field" type="text" id="stop_base_price" required>
                                </label>
                                <label>Стоп-лосс %
                                    <input class="field" type="text" id="stop_loss_pct" value="0.00">
                                </label>
                                <label>Тейк-профит %
                                    <input class="field" type="text" id="take_profit_pct" value="0.01">
                                </label>
                                <div class="row-buttons">
                                    <button type="button" class="btn btn-primary" onclick="createStopOrders()">Создать стоп-заявки</button>
                                </div>
                            </form>
                        </div>

                        <div class="block">
                            <div class="block-head"><h2>Активные стоп-заявки</h2></div>
                            <div class="table-wrap">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>ID</th>
                                            <th>FIGI</th>
                                            <th>Количество</th>
                                            <th>Валюта</th>
                                            <th>Тип</th>
                                            <th>Направление</th>
                                            <th>Действие</th>
                                        </tr>
                                    </thead>
                                    <tbody>${stopRows}</tbody>
                                </table>
                            </div>
                        </div>
                    </section>
                `;

                const select = document.getElementById('positionForStops');
                if (select && select.value) {
                    const parts = select.value.split('|');
                    document.getElementById('stop_base_price').value = parts[3] || '0.00';
                }
                if (select) {
                    select.addEventListener('change', () => {
                        const parts = select.value.split('|');
                        document.getElementById('stop_base_price').value = parts[3] || '0.00';
                    });
                }
            }

            async function renderSettingsTab() {
                const data = await apiGet('/api/dashboard/settings');
                const s = data.settings || {};
                const profiles = data.profiles || [];
                const strategies = data.strategies || [];
                const instruments = data.instruments || [];

                const profileRows = profiles.map(p => `
                    <tr>
                        <td>${esc(p.profile_name)}</td>
                        <td>${p.is_active === 1 ? 'Да' : 'Нет'}</td>
                        <td>${esc(p.created_at)}</td>
                        <td><button class="btn" onclick="activateProfile('${esc(p.profile_name)}')">Активировать</button></td>
                    </tr>
                `).join('');

                const strategyRows = strategies.map(x => `
                    <tr>
                        <td>${esc(x.strategy_name)}</td>
                        <td>${x.is_active === 1 ? 'Да' : 'Нет'}</td>
                        <td>${esc(x.created_at)}</td>
                        <td><button class="btn" onclick="activateStrategy('${esc(x.strategy_name)}')">Активировать</button></td>
                        <td><button class="btn" onclick="saveStrategy('${esc(x.strategy_name)}')">Перезаписать</button></td>
                    </tr>
                `).join('');

                const instrumentCards = instruments.map(i => `
                    <div class="block">
                        <div class="block-head">
                            <h2>${esc(i.ticker)} — ${esc(i.name)}</h2>
                            <div class="note">FIGI: ${esc(i.figi)}</div>
                        </div>
                        <form class="form-grid" onsubmit="submitInstrumentUpdate(event, '${esc(i.figi)}')">
                            <label>Лотов бота <input class="field" name="lots_override" type="number" value="${esc(i.lots_override)}"></label>
                            <label>Стоп-лосс % <input class="field" name="stop_loss_pct" type="text" value="${Number(i.stop_loss_pct || 0).toFixed(2)}"></label>
                            <label>Тейк-профит % <input class="field" name="take_profit_pct" type="text" value="${Number(i.take_profit_pct || 0).toFixed(2)}"></label>
                            <label>Макс. спред % <input class="field" name="max_spread_pct" type="text" value="${Number(i.max_spread_pct || 0).toFixed(2)}"></label>
                            <label>Мин. объём 1м <input class="field" name="min_volume" type="number" value="${esc(i.min_volume || 0)}"></label>
                            <label>Разрешить Long
                                <select class="field" name="allow_long">
                                    <option value="1" ${String(i.allow_long) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(i.allow_long) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Разрешить Short
                                <select class="field" name="allow_short">
                                    <option value="1" ${String(i.allow_short) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(i.allow_short) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Приоритет <input class="field" name="priority" type="number" value="${esc(i.priority || 100)}"></label>
                            <label>Использовать
                                <select class="field" name="enabled">
                                    <option value="1" ${String(i.enabled) === '1' || i.enabled === 1 ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(i.enabled) === '0' || i.enabled === 0 ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <div class="row-buttons">
                                <button type="submit" class="btn btn-primary">Сохранить</button>
                                <button type="button" class="btn btn-danger" onclick="deleteInstrument('${esc(i.figi)}')">Удалить</button>
                            </div>
                        </form>
                    </div>
                `).join('');

                document.getElementById('view-настройки').innerHTML = `
                    <section class="block">
                        <div class="block-head"><h2>Общие системные настройки</h2></div>
                        <form id="systemSettingsForm" class="form-grid">
                            <label>Торговля включена
                                <select class="field" name="bot_enabled">
                                    <option value="1" ${String(s.bot_enabled) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.bot_enabled) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Telegram только ошибки
                                <select class="field" name="telegram_errors_only">
                                    <option value="1" ${String(s.telegram_errors_only) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.telegram_errors_only) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Автоперечитывание настроек
                                <select class="field" name="auto_reload_settings">
                                    <option value="1" ${String(s.auto_reload_settings) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.auto_reload_settings) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <div class="row-buttons">
                                <button type="button" class="btn btn-primary" onclick="saveSystemSettings()">Сохранить системные настройки</button>
                            </div>
                        </form>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Стратегия торговли</h2></div>
                        <form id="strategySettingsForm" class="form-grid">
                            <label>Макс. сделок в день <input class="field" name="max_trades_per_day" type="text" value="${esc(s.max_trades_per_day || '15')}"></label>
                            <label>Макс. дневной убыток <input class="field" name="max_daily_loss_rub" type="text" value="${Number(s.max_daily_loss_rub || 0).toFixed(2)}"></label>
                            <label>Макс. открытых позиций <input class="field" name="max_open_positions" type="text" value="${esc(s.max_open_positions || '2')}"></label>
                            <label>Интервал проверки, сек <input class="field" name="check_interval_sec" type="text" value="${esc(s.check_interval_sec || '5')}"></label>
                            <label>Стоп-лосс по умолчанию <input class="field" name="default_stop_loss_pct" type="text" value="${Number(s.default_stop_loss_pct || 0).toFixed(2)}"></label>
                            <label>Тейк-профит по умолчанию <input class="field" name="default_take_profit_pct" type="text" value="${Number(s.default_take_profit_pct || 0).toFixed(2)}"></label>
                            <label>Оценка комиссии <input class="field" name="estimated_commission_pct" type="text" value="${Number(s.estimated_commission_pct || 0).toFixed(2)}"></label>
                            <label>Разрешить Long
                                <select class="field" name="allow_long_global">
                                    <option value="1" ${String(s.allow_long_global) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.allow_long_global) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Разрешить Short
                                <select class="field" name="allow_short_global">
                                    <option value="1" ${String(s.allow_short_global) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.allow_short_global) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Только торговая сессия
                                <select class="field" name="trade_only_session">
                                    <option value="1" ${String(s.trade_only_session) === '1' ? 'selected' : ''}>Да</option>
                                    <option value="0" ${String(s.trade_only_session) === '0' ? 'selected' : ''}>Нет</option>
                                </select>
                            </label>
                            <label>Пауза после ошибки, сек <input class="field" name="pause_after_error_sec" type="text" value="${esc(s.pause_after_error_sec || '10')}"></label>

                            <div class="row-buttons">
                                <button type="button" class="btn btn-primary" onclick="saveStrategySettings()">Сохранить стратегию</button>
                            </div>
                        </form>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Пресеты стратегий торговли</h2></div>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Название</th>
                                        <th>Активна</th>
                                        <th>Создана</th>
                                        <th>Выбрать</th>
                                        <th>Сохранить</th>
                                    </tr>
                                </thead>
                                <tbody>${strategyRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head">
                            <h2>Профили настроек</h2>
                            <div class="row-buttons">
                                <input class="field" id="newProfileName" type="text" placeholder="Имя нового профиля">
                                <button class="btn" onclick="createProfile()">Создать профиль</button>
                            </div>
                        </div>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Название</th>
                                        <th>Активен</th>
                                        <th>Создан</th>
                                        <th>Выбрать</th>
                                    </tr>
                                </thead>
                                <tbody>${profileRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head">
                            <h2>Инструменты</h2>
                            <div class="row-buttons">
                                <button class="btn btn-primary" onclick="openAddInstrumentModal()">Добавить инструмент</button>
                            </div>
                        </div>
                        ${instrumentCards}
                    </section>
                `;
            }

            async function renderHistoryTab() {
                const data = await apiGet('/api/dashboard/history');

                const tradesRows = data.trades.map(x => `
                    <tr>
                        <td>${esc(x.time)}</td>
                        <td>${esc(x.ticker)}</td>
                        <td>${esc(x.direction)}</td>
                        <td>${esc(x.entry)}</td>
                        <td>${esc(x.exit)}</td>
                        <td>${esc(x.qty)}</td>
                        <td>${esc(x.commission)}</td>
                        <td>${esc(x.pnl)}</td>
                        <td>${esc(x.reason)}</td>
                    </tr>
                `).join('');

                function mapLogs(rows) {
                    return rows.map(x => `
                        <tr>
                            <td>${esc(x.event_time)}</td>
                            <td>${esc(x.event_type)}</td>
                            <td>${esc(x.ticker)}</td>
                            <td>${esc(x.level)}</td>
                            <td>${esc(x.message)}</td>
                        </tr>
                    `).join('');
                }

                document.getElementById('view-история').innerHTML = `
                    <section class="block">
                        <div class="block-head">
                            <h2>Фильтры на странице</h2>
                            <div class="note">Фронтовая фильтрация без открытия JSON</div>
                        </div>
                        <div class="form-grid">
                            <label>Фильтр торговой истории <input id="filterTrades" class="field" type="text" placeholder="Тикер, причина, направление..."></label>
                            <label>Фильтр системной истории <input id="filterSystem" class="field" type="text" placeholder="Тип события, текст..."></label>
                            <label>Фильтр ошибок <input id="filterErrors" class="field" type="text" placeholder="Тикер, ошибка, текст..."></label>
                            <label>Фильтр общего журнала <input id="filterCommon" class="field" type="text" placeholder="Любой текст..."></label>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Торговая история</h2></div>
                        <div class="table-wrap">
                            <table data-filter-input="filterTrades">
                                <thead>
                                    <tr>
                                        <th>Время</th>
                                        <th>Тикер</th>
                                        <th>Направление</th>
                                        <th>Вход</th>
                                        <th>Выход</th>
                                        <th>Количество</th>
                                        <th>Комиссия</th>
                                        <th>PNL</th>
                                        <th>Причина</th>
                                    </tr>
                                </thead>
                                <tbody>${tradesRows}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Системная история</h2></div>
                        <div class="table-wrap">
                            <table data-filter-input="filterSystem">
                                <thead>
                                    <tr>
                                        <th>Время</th>
                                        <th>Событие</th>
                                        <th>Тикер</th>
                                        <th>Уровень</th>
                                        <th>Сообщение</th>
                                    </tr>
                                </thead>
                                <tbody>${mapLogs(data.system_logs)}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Ошибки</h2></div>
                        <div class="table-wrap">
                            <table data-filter-input="filterErrors">
                                <thead>
                                    <tr>
                                        <th>Время</th>
                                        <th>Событие</th>
                                        <th>Тикер</th>
                                        <th>Уровень</th>
                                        <th>Сообщение</th>
                                    </tr>
                                </thead>
                                <tbody>${mapLogs(data.error_logs)}</tbody>
                            </table>
                        </div>
                    </section>

                    <section class="block">
                        <div class="block-head"><h2>Общий журнал</h2></div>
                        <div class="table-wrap">
                            <table data-filter-input="filterCommon">
                                <thead>
                                    <tr>
                                        <th>Время</th>
                                        <th>Событие</th>
                                        <th>Тикер</th>
                                        <th>Уровень</th>
                                        <th>Сообщение</th>
                                    </tr>
                                </thead>
                                <tbody>${mapLogs(data.common_logs)}</tbody>
                            </table>
                        </div>
                    </section>
                `;

                attachTableFilters();
            }

            function attachTableFilters() {
                document.querySelectorAll('table[data-filter-input]').forEach(table => {
                    const inputId = table.dataset.filterInput;
                    const input = document.getElementById(inputId);
                    if (!input || input.dataset.bound === '1') return;

                    input.dataset.bound = '1';
                    input.addEventListener('input', () => {
                        const q = input.value.trim().toLowerCase();
                        table.querySelectorAll('tbody tr').forEach(row => {
                            row.style.display = (!q || row.innerText.toLowerCase().includes(q)) ? '' : 'none';
                        });
                    });
                });
            }

            async function renderCurrentTab() {
                const tab = currentTab();
                if (tab === 'главное') await renderMainTab();
                if (tab === 'портфель') await renderPortfolioTab();
                if (tab === 'настройки') await renderSettingsTab();
                if (tab === 'история') await renderHistoryTab();
            }

            async function refreshQuotesOnly() {
                if (currentTab() !== 'главное') return;
                const quotes = await apiGet('/api/dashboard/quotes');
                const map = {};
                quotes.forEach(q => { map[q.figi] = q; });

                document.querySelectorAll('.live-price[data-figi]').forEach(el => {
                    const figi = el.dataset.figi;
                    if (map[figi]) el.textContent = map[figi].last_price;
                });
                document.querySelectorAll('.live-time[data-figi]').forEach(el => {
                    const figi = el.dataset.figi;
                    if (map[figi]) el.textContent = map[figi].price_time;
                });
            }

            async function serviceAction(action) {
                try {
                    const r = await fetch(`/api/control/${action}`, { method: 'POST' });
                    const data = await r.json();
                    alert('Результат: ' + (data.ok ? 'успешно' : 'ошибка'));
                    await renderSummaryCards();
                    await renderCurrentTab();
                } catch (e) {
                    alert('Ошибка: ' + e.message);
                }
        }

            function openAddInstrumentModal() {
                document.getElementById('modalAddInstrument').style.display = 'flex';
                loadTopVolumeInstruments();
            }

            function closeAddInstrumentModal() {
                document.getElementById('modalAddInstrument').style.display = 'none';
            }

            function renderInstrumentSearchRows(items) {
                instrumentSearchData = items || [];
                document.getElementById('instrumentSearchBody').innerHTML = instrumentSearchData.map((item, idx) => `
                    <tr>
                        <td><input type="checkbox" data-idx="${idx}"></td>
                        <td>${esc(item.ticker)}</td>
                        <td>${esc(item.name)}</td>
                        <td>${esc(item.figi)}</td>
                        <td>${esc(item.instrument_type)}</td>
                        <td>${esc(item.currency)}</td>
                        <td>${esc(item.lot)}</td>
                        <td>${esc(item.min_price_increment)}</td>
                        <td>${esc(item.last_price || '0.00')}</td>
                        <td>${esc(item.price_time || '')}</td>
                        <td>${esc(item.volume_score || 0)}</td>
                    </tr>
                `).join('');
            }

            async function searchInstruments() {
                const q = document.getElementById('instrumentSearchInput').value.trim();
                const data = await apiGet('/api/instruments/search?q=' + encodeURIComponent(q));
                renderInstrumentSearchRows(data);
            }

            async function loadTopVolumeInstruments() {
                const data = await apiGet('/api/instruments/search?mode=top-volume');
                renderInstrumentSearchRows(data);
            }

            async function acceptSelectedInstruments() {
                const rows = Array.from(document.querySelectorAll('#instrumentSearchBody input[type="checkbox"]'));
                const items = instrumentSearchData.map((x, idx) => ({
                    ...x,
                    использовать: rows.find(r => Number(r.dataset.idx) === idx)?.checked || false
                }));

                const r = await fetch('/api/instruments/add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ items })
                });
                const data = await r.json();
                alert('Добавлено: ' + data['добавлено']);
                closeAddInstrumentModal();
                await renderSettingsTab();
                await renderMainTab();
            }

            async function closeOnePosition(figi, qty, direction) {
                if (!confirm('Закрыть эту позицию?')) return;
                await apiPostForm('/api/позиции/закрыть', { figi, qty, direction });
                await renderPortfolioTab();
                await renderMainTab();
                await renderSummaryCards();
            }

            async function closeAllPositionsConfirm() {
                const ok = confirm('Точно закрыть ВСЕ открытые позиции? Действие рыночное и может привести к немедленному исполнению.');
                if (!ok) return;
                await apiPostForm('/api/позиции/закрыть-все', {});
                await renderPortfolioTab();
                await renderMainTab();
                await renderSummaryCards();
            }

            async function cancelStopOrder(stop_order_id) {
                if (!confirm('Отменить стоп-заявку?')) return;
                await apiPostForm('/api/стоп-заявки/отменить', { stop_order_id });
                await renderPortfolioTab();
            }

            async function createStopOrders() {
                const value = document.getElementById('positionForStops').value;
                if (!value) {
                    alert('Нет доступной позиции');
                    return;
                }
                const parts = value.split('|');
                const figi = parts[0] || '';
                const qty = parts[1] || '0';
                const side = parts[2] || '';
                const base_price = document.getElementById('stop_base_price').value || '0';
                const stop_loss_pct = document.getElementById('stop_loss_pct').value || '0';
                const take_profit_pct = document.getElementById('take_profit_pct').value || '0';

                await apiPostForm('/api/стоп-заявки/создать', {
                    figi, qty, side, base_price, stop_loss_pct, take_profit_pct
                });
                alert('Стоп-заявки созданы');
                await renderPortfolioTab();
            }

            async function saveSystemSettings() {
                const form = document.getElementById('systemSettingsForm');
                const fd = new FormData(form);
                await apiPostForm('/api/settings/system', Object.fromEntries(fd.entries()));
                alert('Системные настройки сохранены');
                await renderSummaryCards();
            }

            async function saveStrategySettings() {
                const form = document.getElementById('strategySettingsForm');
                const fd = new FormData(form);
                await apiPostForm('/api/settings/strategy', Object.fromEntries(fd.entries()));
                alert('Стратегия сохранена');
                await renderSummaryCards();
            }

            async function createProfile() {
                const name = document.getElementById('newProfileName').value.trim();
                if (!name) {
                    alert('Укажи имя профиля');
                    return;
                }
                await apiPostForm('/api/профили/создать', { profile_name: name });
                await renderSettingsTab();
                await renderSummaryCards();
            }

            async function activateProfile(name) {
                await apiPostForm('/api/профили/активировать', { profile_name: name });
                await renderSettingsTab();
                await renderSummaryCards();
            }

            async function activateStrategy(name) {
                await apiPostForm('/api/стратегии/активировать', { strategy_name: name });
                await renderSettingsTab();
                await renderSummaryCards();
            }

            async function saveStrategy(name) {
                await apiPostForm('/api/стратегии/сохранить', { strategy_name: name });
                await renderSettingsTab();
            }

            async function submitInstrumentUpdate(event, figi) {
                event.preventDefault();
                const form = event.target;
                const fd = new FormData(form);
                fd.append('figi', figi);

                await apiPostForm('/api/instruments/update', Object.fromEntries(fd.entries()));
                alert('Инструмент сохранён');
                await renderSettingsTab();
                await renderMainTab();
            }

            async function deleteInstrument(figi) {
                if (!confirm('Удалить инструмент?')) return;
                await apiPostForm('/api/instruments/delete', { figi });
                await renderSettingsTab();
                await renderMainTab();
            }

            async function bootstrap() {
                initRouter();
                await renderSummaryCards();
                await renderCurrentTab();

                setInterval(async () => {
                    try { await renderSummaryCards(); } catch (e) {}
                }, REFRESH_MAIN_MS);

                setInterval(async () => {
                    try { await refreshQuotesOnly(); } catch (e) {}
                }, REFRESH_QUOTES_MS);
            }

            bootstrap();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)