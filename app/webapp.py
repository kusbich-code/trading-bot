import json
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings

app = FastAPI(title="Trading Bot Dashboard")


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def api_state():
    return JSONResponse(read_json(settings.RUNTIME_FILE, {}))


@app.get("/api/session")
def api_session():
    return JSONResponse(read_json(settings.SESSION_FILE, {}))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    state = read_json(settings.RUNTIME_FILE, {})
    trades = state.get("closed_trades", [])
    open_positions = state.get("open_positions", {})
    instrument_states = state.get("instrument_states", {})

    rows = ""
    for t in trades[-20:][::-1]:
        rows += f"""
        <tr>
            <td>{t.get('time', '')}</td>
            <td>{t.get('ticker', '')}</td>
            <td>{t.get('direction', '')}</td>
            <td>{t.get('entry', '')}</td>
            <td>{t.get('exit', '')}</td>
            <td>{t.get('qty', '')}</td>
            <td>{t.get('gross_amount', '')}</td>
            <td>{t.get('pnl', '')}</td>
            <td>{t.get('reason', '')}</td>
        </tr>
        """

    open_rows = ""
    for ticker, pos in open_positions.items():
        open_rows += f"""
        <tr>
            <td>{ticker}</td>
            <td>{pos.get('direction', '')}</td>
            <td>{pos.get('entry_price', '')}</td>
            <td>{pos.get('qty', '')}</td>
            <td>{pos.get('opened_at', '')}</td>
        </tr>
        """

    instrument_rows = ""
    for ticker, item in instrument_states.items():
        instrument_rows += f"""
        <tr>
            <td>{ticker}</td>
            <td>{item.get('figi', '')}</td>
            <td>{item.get('trading_status', '')}</td>
            <td>{item.get('updated_at', '')}</td>
        </tr>
        """

    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Trading Bot Dashboard</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #111827;
                color: #f3f4f6;
                margin: 0;
                padding: 16px;
            }}
            .wrap {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            .cards {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 12px;
                margin-bottom: 16px;
            }}
            .card {{
                background: #1f2937;
                border-radius: 12px;
                padding: 16px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 12px;
                background: #1f2937;
                border-radius: 12px;
                overflow: hidden;
            }}
            th, td {{
                padding: 10px;
                border-bottom: 1px solid #374151;
                text-align: left;
                font-size: 14px;
            }}
            h1, h2 {{
                margin: 12px 0;
            }}
            .muted {{
                color: #9ca3af;
            }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <h1>Trading Bot Dashboard</h1>
            <p class="muted">Обновлено: {state.get('last_update', '-')}</p>

            <div class="cards">
                <div class="card"><strong>Статус</strong><br>{state.get('status', '-')}</div>
                <div class="card"><strong>Баланс старт</strong><br>{state.get('session_balance_start', 0)} ₽</div>
                <div class="card"><strong>Баланс сейчас</strong><br>{state.get('session_balance_current', 0)} ₽</div>
                <div class="card"><strong>Дневной PnL</strong><br>{state.get('daily_pnl', 0)} ₽</div>
                <div class="card"><strong>Сделок сегодня</strong><br>{state.get('trades_today', 0)}</div>
                <div class="card"><strong>Открытых позиций</strong><br>{len(open_positions)}</div>
            </div>

            <h2>Статусы инструментов</h2>
            <table>
                <thead>
                    <tr><th>Ticker</th><th>FIGI</th><th>Trading status</th><th>Updated</th></tr>
                </thead>
                <tbody>{instrument_rows}</tbody>
            </table>

            <h2>Открытые позиции</h2>
            <table>
                <thead>
                    <tr><th>Ticker</th><th>Direction</th><th>Entry</th><th>Qty</th><th>Opened</th></tr>
                </thead>
                <tbody>{open_rows}</tbody>
            </table>

            <h2>Последние сделки</h2>
            <table>
                <thead>
                    <tr>
                        <th>Time</th><th>Ticker</th><th>Dir</th><th>Entry</th><th>Exit</th>
                        <th>Qty</th><th>Gross</th><th>PnL</th><th>Reason</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html)