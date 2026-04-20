from decimal import Decimal
from datetime import datetime

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(title="Панель управления торговым ботом v3.5.2")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
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


def raw_and_ui(value):
    return {
        "raw": str(value if value is not None else "0"),
        "ui": fmt2(value),
    }


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
        "daily_pnl": raw_and_ui(get_runtime("daily_pnl", "0"))["raw"],
        "daily_pnl_ui": raw_and_ui(get_runtime("daily_pnl", "0"))["ui"],
        "trades_today": int(float(get_runtime("trades_today", "0") or 0)),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": raw_and_ui(get_runtime("session_balance_start", "0"))["raw"],
        "session_balance_start_ui": raw_and_ui(get_runtime("session_balance_start", "0"))["ui"],
        "session_balance_current": raw_and_ui(get_runtime("session_balance_current", "0"))["raw"],
        "session_balance_current_ui": raw_and_ui(get_runtime("session_balance_current", "0"))["ui"],
        "bot_enabled": get_setting("bot_enabled", "1"),
        "active_profile_name": get_setting("active_profile_name", "Основной"),
        "active_strategy_name": get_setting("active_strategy_name", "Сбалансированный"),
        "total_commission": raw_and_ui(trade_stats_today.get("total_commission", 0))["raw"],
        "total_commission_ui": raw_and_ui(trade_stats_today.get("total_commission", 0))["ui"],
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
            "figi": i["figi"],
            "ticker": i["ticker"],
            "name": i["name"],
            "enabled": i["enabled"],
            "lots_override": i["lots_override"],
            "stop_loss_pct": str(i["stop_loss_pct"]),
            "stop_loss_pct_ui": fmt2(i["stop_loss_pct"]),
            "take_profit_pct": str(i["take_profit_pct"]),
            "take_profit_pct_ui": fmt2(i["take_profit_pct"]),
            "last_price": str(q.get("last_price", "0")),
            "last_price_ui": fmt2(q.get("last_price", "0")),
            "price_time": q.get("price_time", "-"),
        })

    result_positions = []
    for p in open_positions:
        result_positions.append({
            "ticker": p["ticker"],
            "figi": p["figi"],
            "direction": p["direction"],
            "qty": int(float(p["qty"] or 0)),
            "entry_price_raw": str(p["entry_price"]),
            "entry_price_ui": fmt2(p["entry_price"]),
            "current_price_raw": str(p["current_price"]),
            "current_price_ui": fmt2(p["current_price"]),
            "unrealized_pnl_raw": str(p["unrealized_pnl"]),
            "unrealized_pnl_ui": fmt2(p["unrealized_pnl"]),
            "opened_at": p["opened_at"],
        })

    result_trades = []
    for t in trades:
        result_trades.append({
            "time": t["time"],
            "ticker": t["ticker"],
            "direction": t["direction"],
            "entry_raw": str(t["entry"]),
            "entry_ui": fmt2(t["entry"]),
            "exit_raw": str(t["exit"]),
            "exit_ui": fmt2(t["exit"]),
            "qty": int(float(t["qty"] or 0)),
            "pnl_raw": str(t["pnl"]),
            "pnl_ui": fmt2(t["pnl"]),
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
                    "quantity_raw": str(getattr(p, "quantity", "")),
                    "quantity_ui": fmt2(getattr(p, "quantity", 0)),
                    "average_position_price_raw": str(getattr(p, "average_position_price", "")),
                    "average_position_price_ui": fmt2(getattr(p, "average_position_price", 0)),
                    "current_price_raw": str(getattr(p, "current_price", "")),
                    "current_price_ui": fmt2(getattr(p, "current_price", 0)),
                    "expected_yield_raw": str(getattr(p, "expected_yield", "")),
                    "expected_yield_ui": fmt2(getattr(p, "expected_yield", 0)),
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
            "entry_price_raw": str(p["entry_price"]),
            "entry_price_ui": fmt2(p["entry_price"]),
            "current_price_raw": str(p["current_price"]),
            "current_price_ui": fmt2(p["current_price"]),
            "unrealized_pnl_raw": str(p["unrealized_pnl"]),
            "unrealized_pnl_ui": fmt2(p["unrealized_pnl"]),
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
            "entry_raw": str(x["entry"]),
            "entry_ui": fmt2(x["entry"]),
            "exit_raw": str(x["exit"]),
            "exit_ui": fmt2(x["exit"]),
            "qty": int(float(x["qty"] or 0)),
            "commission_raw": str(x["commission"]),
            "commission_ui": fmt2(x["commission"]),
            "pnl_raw": str(x["pnl"]),
            "pnl_ui": fmt2(x["pnl"]),
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
    s = get_all_settings()

    for key in [
        "max_daily_loss_rub",
        "default_stop_loss_pct",
        "default_take_profit_pct",
        "estimated_commission_pct",
    ]:
        s[f"{key}_ui"] = fmt2(s.get(key, "0"))

    instruments = list_instruments()
    for i in instruments:
        i["stop_loss_pct_ui"] = fmt2(i.get("stop_loss_pct", 0))
        i["take_profit_pct_ui"] = fmt2(i.get("take_profit_pct", 0))
        i["max_spread_pct_ui"] = fmt2(i.get("max_spread_pct", 0))

    return {
        "settings": s,
        "profiles": list_settings_profiles(),
        "strategies": list_strategy_profiles(),
        "instruments": instruments,
    }


@app.get("/api/dashboard/quotes")
def api_dashboard_quotes():
    rows = []
    for x in get_instrument_market_state():
        rows.append({
            "figi": x["figi"],
            "ticker": x["ticker"],
            "last_price": str(x["last_price"]),
            "last_price_ui": fmt2(x["last_price"]),
            "price_time": x["price_time"],
            "volume_1m": int(float(x.get("volume_1m", 0) or 0)),
        })
    return rows


@app.get("/api/instruments/search")
def api_instruments_search(q: str = Query("", min_length=0), mode: str = "top-volume"):
    with get_client() as client:
        if q.strip():
            items = find_instruments(client, q.strip())
            for x in items:
                x["использовать"] = False

            figis = [x["figi"] for x in items if x.get("figi")]
            prices = get_last_prices_for_figis(client, figis)
            for item in items:
                px = prices.get(item["figi"], {})
                item["last_price"] = str(px.get("last_price", "0"))
                item["last_price_ui"] = fmt2(px.get("last_price", "0"))
                item["price_time"] = px.get("price_time", "")
            return JSONResponse(items)

        items = get_volume_top20_instruments(client)
        figis = [x["figi"] for x in items if x.get("figi")]
        prices = get_last_prices_for_figis(client, figis)

        for item in items:
            px = prices.get(item["figi"], {})
            item["last_price"] = str(px.get("last_price", "0"))
            item["last_price_ui"] = fmt2(px.get("last_price", "0"))
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
    return HTMLResponse("""
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Панель управления торговым ботом v3.5.2</title>
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
            cursor: pointer;
            user-select: none;
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
        .toast-host {
            position: fixed;
            top: 16px;
            right: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            z-index: 2000;
        }
        .toast {
            min-width: 260px;
            max-width: 420px;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: #121933;
            color: var(--text);
            opacity: 0;
            transform: translateY(-8px);
            transition: opacity .2s ease, transform .2s ease;
            box-shadow: var(--shadow);
        }
        .toast-show {
            opacity: 1;
            transform: translateY(0);
        }
        .toast-success { border-color: #2fbf71; }
        .toast-error { border-color: #d95468; }
        .toast-info { border-color: #4c8dff; }
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
                <div class="title">Панель управления торговым ботом v3.5.2</div>
                <div class="subtitle">Stabilizer: tabs fix, diff-updates, toasts, external JS, raw/ui values, hash fallback</div>
            </div>
            <div class="note">Табы больше не зависят от серверного переключения</div>
        </div>

        <nav class="tabs">
            <a href="#/главное" class="tab-link" data-tab-link="главное">Главное</a>
            <a href="#/портфель" class="tab-link" data-tab-link="портфель">Портфель</a>
            <a href="#/настройки" class="tab-link" data-tab-link="настройки">Настройки</a>
            <a href="#/история" class="tab-link" data-tab-link="история">История и логи</a>
        </nav>

        <section class="cards" id="summaryCards"></section>

        <section id="view-main" data-view="главное"></section>
        <section id="view-portfolio" data-view="портфель" class="hidden"></section>
        <section id="view-settings" data-view="настройки" class="hidden"></section>
        <section id="view-history" data-view="история" class="hidden"></section>
    </div>

    <div class="modal-bg" id="modalAddInstrument">
        <div class="modal">
            <div class="modal-top">
                <div>
                    <h3>Добавление инструментов</h3>
                    <div class="note">По умолчанию грузится реальный top-20 по объёму. Использовать = ложь.</div>
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

    <div class="toast-host" id="toastHost"></div>

    <script src="/static/dashboard.js"></script>
</body>
</html>
    """)