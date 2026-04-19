from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from t_tech.invest import Client
from t_tech.invest.sandbox.client import SandboxClient

from app.config import settings
from app.db import (
    init_db,
    get_setting,
    set_setting,
    get_runtime,
    get_trades,
    get_logs,
    list_instruments,
    add_instrument,
    update_instrument,
    delete_instrument,
    log_event,
)
from app.instruments import find_instruments
from app.control import run_control

app = FastAPI(title="Trading Bot Dashboard")
init_db()


def get_client_cls():
    return SandboxClient if settings.TINVEST_USE_SANDBOX else Client


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def api_state():
    return {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": get_runtime("daily_pnl", "0"),
        "trades_today": get_runtime("trades_today", "0"),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": get_runtime("session_balance_start", "0"),
        "session_balance_current": get_runtime("session_balance_current", "0"),
        "bot_enabled": get_setting("bot_enabled", "1"),
    }


@app.get("/api/trades")
def api_trades(
    ticker: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100
):
    return JSONResponse(get_trades(limit=limit, ticker=ticker, date_from=date_from, date_to=date_to))


@app.get("/api/logs")
def api_logs(
    ticker: str | None = None,
    event_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200
):
    return JSONResponse(get_logs(limit=limit, ticker=ticker, event_type=event_type, date_from=date_from, date_to=date_to))


@app.get("/api/instruments")
def api_instruments():
    return JSONResponse(list_instruments())


@app.get("/api/instruments/search")
def api_instruments_search(q: str = Query(..., min_length=1)):
    client_cls = get_client_cls()
    with client_cls(settings.TINVEST_TOKEN) as client:
        items = find_instruments(client, q)
    return JSONResponse(items)


@app.post("/api/instruments/add")
def api_instruments_add(request: Request):
    data = request.query_params
    item = {
        "ticker": data.get("ticker", ""),
        "figi": data.get("figi", ""),
        "name": data.get("name", ""),
        "lot": int(data.get("lot", "1")),
        "min_price_increment": data.get("min_price_increment", "0.01"),
        "lots_override": int(data.get("lots_override", "1")),
        "stop_loss_pct": data.get("stop_loss_pct", get_setting("default_stop_loss_pct", "0.0025")),
        "take_profit_pct": data.get("take_profit_pct", get_setting("default_take_profit_pct", "0.005")),
        "enabled": 1,
    }
    add_instrument(item)
    log_event("CONFIG_CHANGED", f"Instrument added {item['ticker']}", ticker=item["ticker"])
    return {"ok": True}


@app.post("/api/instruments/update")
def api_instruments_update(
    figi: str = Form(...),
    lots_override: int = Form(...),
    stop_loss_pct: str = Form(...),
    take_profit_pct: str = Form(...),
    enabled: int = Form(...)
):
    update_instrument(figi, {
        "lots_override": lots_override,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "enabled": enabled,
    })
    log_event("CONFIG_CHANGED", f"Instrument updated figi={figi}")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    log_event("CONFIG_CHANGED", f"Instrument deleted figi={figi}")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/settings/update")
def api_settings_update(
    max_trades_per_day: str = Form(...),
    max_daily_loss_rub: str = Form(...),
    max_open_positions: str = Form(...),
    default_stop_loss_pct: str = Form(...),
    default_take_profit_pct: str = Form(...),
    bot_enabled: str = Form(...)
):
    set_setting("max_trades_per_day", max_trades_per_day)
    set_setting("max_daily_loss_rub", max_daily_loss_rub)
    set_setting("max_open_positions", max_open_positions)
    set_setting("default_stop_loss_pct", default_stop_loss_pct)
    set_setting("default_take_profit_pct", default_take_profit_pct)
    set_setting("bot_enabled", bot_enabled)
    log_event("CONFIG_CHANGED", "Global settings updated")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/control/{action}")
def api_control(action: str):
    result = run_control(action)
    return JSONResponse(result)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    state = {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": get_runtime("daily_pnl", "0"),
        "trades_today": get_runtime("trades_today", "0"),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": get_runtime("session_balance_start", "0"),
        "session_balance_current": get_runtime("session_balance_current", "0"),
        "bot_enabled": get_setting("bot_enabled", "1"),
    }

    trades = get_trades(limit=30)
    logs = get_logs(limit=50)
    instruments = list_instruments()

    settings_map = {
        "max_trades_per_day": get_setting("max_trades_per_day", "15"),
        "max_daily_loss_rub": get_setting("max_daily_loss_rub", "200"),
        "max_open_positions": get_setting("max_open_positions", "2"),
        "default_stop_loss_pct": get_setting("default_stop_loss_pct", "0.0025"),
        "default_take_profit_pct": get_setting("default_take_profit_pct", "0.005"),
        "bot_enabled": get_setting("bot_enabled", "1"),
    }

    trade_rows = ""
    for t in trades:
        trade_rows += f"""
        <tr>
            <td>{t['time']}</td>
            <td>{t['ticker']}</td>
            <td>{t['direction']}</td>
            <td>{t['entry']}</td>
            <td>{t['exit']}</td>
            <td>{t['qty']}</td>
            <td>{t['commission']}</td>
            <td>{t['pnl']}</td>
            <td>{t['reason']}</td>
        </tr>
        """

    log_rows = ""
    for x in logs:
        log_rows += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    instrument_blocks = ""
    for i in instruments:
        instrument_blocks += f"""
        <form method="post" action="/api/instruments/update" class="card">
            <input type="hidden" name="figi" value="{i['figi']}">
            <div><strong>{i['ticker']}</strong> — {i['name']}</div>
            <div>FIGI: {i['figi']}</div>
            <div>Биржевой лот: {i['lot']}</div>
            <div>Шаг цены: {i['min_price_increment']}</div>
            <label>Лотов бота: <input type="number" name="lots_override" value="{i['lots_override']}"></label>
            <label>Stop loss: <input type="text" name="stop_loss_pct" value="{i['stop_loss_pct']}"></label>
            <label>Take profit: <input type="text" name="take_profit_pct" value="{i['take_profit_pct']}"></label>
            <label>Enabled:
                <select name="enabled">
                    <option value="1" {'selected' if i['enabled'] == 1 else ''}>Yes</option>
                    <option value="0" {'selected' if i['enabled'] == 0 else ''}>No</option>
                </select>
            </label>
            <button type="submit">Сохранить</button>
        </form>
        <form method="post" action="/api/instruments/delete" class="card danger">
            <input type="hidden" name="figi" value="{i['figi']}">
            <button type="submit">Удалить {i['ticker']}</button>
        </form>
        """

    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Trading Bot Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #111827; color: #f3f4f6; margin: 0; padding: 16px; }}
            .wrap {{ max-width: 1400px; margin: 0 auto; }}
            .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }}
            .card {{ background: #1f2937; border-radius: 12px; padding: 16px; margin-bottom: 12px; }}
            .danger {{ border: 1px solid #7f1d1d; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 12px; background: #1f2937; border-radius: 12px; overflow: hidden; }}
            th, td {{ padding: 10px; border-bottom: 1px solid #374151; text-align: left; font-size: 14px; }}
            h1, h2 {{ margin: 16px 0 10px; }}
            input, select, button {{ margin: 6px 4px 6px 0; padding: 8px; }}
            .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
            .row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
            a {{ color: #93c5fd; }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <h1>Trading Bot Dashboard v3.3</h1>

            <div class="cards">
                <div class="card"><strong>Status</strong><br>{state['status']}</div>
                <div class="card"><strong>Daily PnL</strong><br>{state['daily_pnl']}</div>
                <div class="card"><strong>Trades today</strong><br>{state['trades_today']}</div>
                <div class="card"><strong>Start balance</strong><br>{state['session_balance_start']}</div>
                <div class="card"><strong>Current balance</strong><br>{state['session_balance_current']}</div>
                <div class="card"><strong>Bot enabled</strong><br>{state['bot_enabled']}</div>
            </div>

            <h2>Control</h2>
            <div class="card">
                <div class="row">
                    <form method="post" action="/api/control/start"><button type="submit">Start</button></form>
                    <form method="post" action="/api/control/stop"><button type="submit">Stop</button></form>
                    <form method="post" action="/api/control/restart"><button type="submit">Restart</button></form>
                </div>
            </div>

            <h2>Settings</h2>
            <form method="post" action="/api/settings/update" class="card">
                <label>Max trades/day <input type="text" name="max_trades_per_day" value="{settings_map['max_trades_per_day']}"></label>
                <label>Max daily loss <input type="text" name="max_daily_loss_rub" value="{settings_map['max_daily_loss_rub']}"></label>
                <label>Max open positions <input type="text" name="max_open_positions" value="{settings_map['max_open_positions']}"></label>
                <label>Default stop loss <input type="text" name="default_stop_loss_pct" value="{settings_map['default_stop_loss_pct']}"></label>
                <label>Default take profit <input type="text" name="default_take_profit_pct" value="{settings_map['default_take_profit_pct']}"></label>
                <label>Bot enabled
                    <select name="bot_enabled">
                        <option value="1" {'selected' if settings_map['bot_enabled'] == '1' else ''}>Yes</option>
                        <option value="0" {'selected' if settings_map['bot_enabled'] == '0' else ''}>No</option>
                    </select>
                </label>
                <button type="submit">Save settings</button>
            </form>

            <h2>Instrument search</h2>
            <div class="card">
                <p>Открой вручную в браузере API-поиск, пример:</p>
                <p><a target="_blank" rel="noopener noreferrer" href="/api/instruments/search?q=SBER">/api/instruments/search?q=SBER</a></p>
                <p>После поиска добавляй инструмент запросом вида:</p>
                <p><code>/api/instruments/add?ticker=SBER&figi=BBG004730N88&name=Sberbank&lot=10&min_price_increment=0.01</code></p>
            </div>

            <h2>Instruments</h2>
            <div class="grid2">
                {instrument_blocks}
            </div>

            <h2>Trades</h2>
            <table>
                <thead>
                    <tr>
                        <th>Time</th><th>Ticker</th><th>Dir</th><th>Entry</th><th>Exit</th>
                        <th>Qty</th><th>Commission</th><th>PnL</th><th>Reason</th>
                    </tr>
                </thead>
                <tbody>{trade_rows}</tbody>
            </table>

            <h2>Logs</h2>
            <div class="card">
                <p>Фильтр API, пример:</p>
                <p><a target="_blank" rel="noopener noreferrer" href="/api/logs?ticker=SBER&event_type=ORDER_OPEN">/api/logs?ticker=SBER&event_type=ORDER_OPEN</a></p>
            </div>
            <table>
                <thead>
                    <tr><th>Time</th><th>Type</th><th>Ticker</th><th>Level</th><th>Message</th></tr>
                </thead>
                <tbody>{log_rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html)