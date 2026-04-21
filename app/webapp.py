import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.control import run_control
from app.db import (
    init_db,
    get_all_settings,
    get_setting,
    set_setting,
    get_all_runtime,
    get_runtime,
    set_runtime,
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
    get_position_history,
    list_settings_profiles,
    create_settings_profile,
    activate_settings_profile,
    save_current_settings_to_profile,
    list_strategy_profiles,
    activate_strategy_profile,
    save_current_settings_to_strategy,
    get_instrument_market_state,
    get_instrument_market_state_map,
    list_enabled_instruments,
)

app = FastAPI(title="Trading Bot Dashboard v3.5.2")

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


def normalize_instrument_row(row: Dict[str, Any], market_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    figi = row.get("figi", "")
    market = market_map.get(figi, {})
    out = dict(row)

    out["stop_loss_pct_ui"] = fmt_pct_fraction(row.get("stop_loss_pct", 0))
    out["take_profit_pct_ui"] = fmt_pct_fraction(row.get("take_profit_pct", 0))
    out["max_spread_pct_ui"] = fmt_pct_fraction(row.get("max_spread_pct", 0))
    out["last_price"] = market.get("last_price", "0")
    out["last_price_ui"] = fmt_money(market.get("last_price", 0))
    out["price_time"] = market.get("price_time", "") or market.get("pricetime", "") or "-"
    return out


def settings_payload() -> Dict[str, Any]:
    s = get_all_settings()

    return {
        "bot_enabled": s.get("botenabled", "1"),
        "telegram_errors_only": s.get("telegramerrorsonly", "0"),
        "auto_reload_settings": s.get("autoreloadsettings", "1"),
        "max_trades_per_day": s.get("maxtradesperday", "15"),
        "max_daily_loss_rub": s.get("maxdailylossrub", "200"),
        "max_daily_loss_rub_ui": fmt_money(s.get("maxdailylossrub", 0)),
        "max_open_positions": s.get("maxopenpositions", "2"),
        "check_interval_sec": s.get("checkintervalsec", "5"),
        "default_stop_loss_pct": s.get("defaultstoplosspct", "0.0025"),
        "default_stop_loss_pct_ui": fmt_pct_fraction(s.get("defaultstoplosspct", 0)),
        "default_take_profit_pct": s.get("defaulttakeprofitpct", "0.005"),
        "default_take_profit_pct_ui": fmt_pct_fraction(s.get("defaulttakeprofitpct", 0)),
        "estimated_commission_pct": s.get("estimatedcommissionpct", "0.0004"),
        "estimated_commission_pct_ui": fmt_pct_fraction(s.get("estimatedcommissionpct", 0)),
        "allow_long_global": s.get("allowlongglobal", "1"),
        "allow_short_global": s.get("allowshortglobal", "1"),
        "trade_only_session": s.get("tradeonlysession", "0"),
        "pause_after_error_sec": s.get("pauseaftererrorsec", "10"),
        "active_profile_name": s.get("activeprofilename", ""),
        "active_strategy_name": s.get("activestrategyname", ""),
    }


def summary_payload() -> Dict[str, Any]:
    bot_settings = get_all_settings()
    runtime = get_all_runtime()
    stats = get_trade_stats_today()

    return {
        "status": bot_settings.get("status", "INIT"),
        "bot_enabled": bot_settings.get("botenabled", "1"),
        "trades_today": stats.get("trades_count", 0),
        "daily_pnl": str(stats.get("total_pnl", 0)),
        "daily_pnl_ui": fmt_money(stats.get("total_pnl", 0)),
        "total_commission": str(stats.get("total_commission", 0)),
        "total_commission_ui": fmt_money(stats.get("total_commission", 0)),
        "session_balance_start": runtime.get("sessionbalancestart", "0"),
        "session_balance_start_ui": fmt_money(runtime.get("sessionbalancestart", 0)),
        "session_balance_current": runtime.get("sessionbalancecurrent", "0"),
        "session_balance_current_ui": fmt_money(runtime.get("sessionbalancecurrent", 0)),
        "active_profile_name": bot_settings.get("activeprofilename", ""),
        "active_strategy_name": bot_settings.get("activestrategyname", ""),
        "last_error": bot_settings.get("lasterror", ""),
    }


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Панель управления торговым ботом v3.5.2</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {{
            --bg: #081224;
            --bg2: #0d1830;
            --card: #11203c;
            --card2: #15284b;
            --text: #eef4ff;
            --muted: #9fb3d8;
            --line: #29446d;
            --blue: #4c8dff;
            --blue2: #2d67d3;
            --green: #2fa36b;
            --red: #bf4d5a;
            --yellow: #d0a23d;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Inter, Arial, sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, #091a49 0%, transparent 28%),
                linear-gradient(180deg, #060c19 0%, #0a1430 48%, #081224 100%);
        }}
        .container {{
            width: min(1400px, calc(100% - 32px));
            margin: 0 auto;
            padding: 28px 0 48px;
        }}
        h1 {{
            margin: 0 0 8px;
            font-size: 28px;
            font-weight: 800;
        }}
        .subtitle {{
            color: var(--muted);
            margin-bottom: 24px;
        }}
        .tabs {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 22px;
        }}
        .tab-link {{
            color: var(--text);
            text-decoration: none;
            border: 1px solid var(--line);
            background: rgba(17, 32, 60, 0.75);
            padding: 12px 18px;
            border-radius: 999px;
            transition: .18s ease;
        }}
        .tab-link:hover {{
            border-color: #4f79c9;
            background: rgba(23, 43, 77, 0.95);
        }}
        .tab-link.active {{
            background: linear-gradient(180deg, #204a99 0%, #173974 100%);
            border-color: #4c8dff;
            box-shadow: 0 0 0 1px rgba(76,141,255,.2) inset;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
        }}
        .card, .block {{
            background: linear-gradient(180deg, rgba(17,32,60,.96) 0%, rgba(14,27,52,.96) 100%);
            border: 1px solid var(--line);
            border-radius: 18px;
            box-shadow: 0 12px 28px rgba(0,0,0,.18);
        }}
        .card {{
            padding: 16px 18px;
            min-height: 96px;
        }}
        .card .label {{
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 10px;
        }}
        .card .value {{
            font-weight: 800;
            font-size: 22px;
            line-height: 1.2;
            word-break: break-word;
        }}
        .block {{
            padding: 18px;
            margin-bottom: 18px;
        }}
        .block-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 14px;
            flex-wrap: wrap;
        }}
        .block h2 {{
            margin: 0;
            font-size: 20px;
        }}
        .note {{
            color: var(--muted);
            font-size: 13px;
        }}
        .hidden {{
            display: none !important;
        }}
        .two-cols {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 18px;
        }}
        .row-buttons {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .btn {{
            border: 1px solid var(--line);
            background: rgba(22, 41, 77, .95);
            color: var(--text);
            padding: 10px 14px;
            border-radius: 12px;
            cursor: pointer;
            transition: .18s ease;
        }}
        .btn:hover {{
            transform: translateY(-1px);
            border-color: #4f79c9;
        }}
        .btn-primary {{
            background: linear-gradient(180deg, var(--blue) 0%, var(--blue2) 100%);
            border-color: #5d98ff;
        }}
        .btn-danger {{
            background: linear-gradient(180deg, #c65a69 0%, #a63f4f 100%);
            border-color: #d36d7a;
        }}
        .field {{
            width: 100%;
            padding: 11px 12px;
            border-radius: 12px;
            border: 1px solid var(--line);
            background: rgba(10, 20, 38, .95);
            color: var(--text);
            outline: none;
        }}
        .field::placeholder {{
            color: #7d93bb;
        }}
        .field:focus {{
            border-color: #4c8dff;
            box-shadow: 0 0 0 3px rgba(76,141,255,.16);
        }}
        .form-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
        }}
        .form-grid label {{
            display: flex;
            flex-direction: column;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
        }}
        .table-wrap {{
            overflow: auto;
            border: 1px solid rgba(41,68,109,.6);
            border-radius: 14px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 760px;
        }}
        th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid rgba(41,68,109,.55);
            text-align: left;
            vertical-align: top;
        }}
        th {{
            color: #dbe8ff;
            font-size: 13px;
            background: rgba(18, 31, 58, .98);
            position: sticky;
            top: 0;
            z-index: 1;
        }}
        td {{
            color: #eef4ff;
            font-size: 14px;
        }}
        tr:hover td {{
            background: rgba(25, 45, 80, .35);
        }}
        .modal {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(2, 6, 15, .72);
            z-index: 30;
            padding: 18px;
            align-items: center;
            justify-content: center;
        }}
        .modal-box {{
            width: min(1200px, 100%);
            max-height: 92vh;
            overflow: auto;
            background: linear-gradient(180deg, rgba(17,32,60,.99) 0%, rgba(14,27,52,.99) 100%);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
        }}
        .toast-host {{
            position: fixed;
            top: 16px;
            right: 16px;
            z-index: 50;
            display: flex;
            flex-direction: column;
            gap: 10px;
            width: min(360px, calc(100vw - 32px));
        }}
        .toast {{
            opacity: 0;
            transform: translateY(-8px);
            transition: .2s ease;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid var(--line);
            background: #132648;
            color: white;
            box-shadow: 0 14px 28px rgba(0,0,0,.2);
        }}
        .toast-show {{
            opacity: 1;
            transform: translateY(0);
        }}
        .toast-success {{ border-color: #2fa36b; }}
        .toast-error {{ border-color: #bf4d5a; }}
        .toast-info {{ border-color: #4c8dff; }}
        @media (max-width: 980px) {{
            .two-cols {{
                grid-template-columns: 1fr;
            }}
            .container {{
                width: min(100%, calc(100% - 20px));
            }}
            h1 {{
                font-size: 24px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Панель управления торговым ботом v3.5.2</h1>
        <div class="subtitle">
            Stabilizer: tabs fix, diff-updates, toasts, external JS, raw/ui values, hash fallback
        </div>

        <nav class="tabs">
            <a href="#/главное" class="tab-link active" data-tab-link="главное">Главное</a>
            <a href="#/портфель" class="tab-link" data-tab-link="портфель">Портфель</a>
            <a href="#/настройки" class="tab-link" data-tab-link="настройки">Настройки</a>
            <a href="#/история" class="tab-link" data-tab-link="история">История и логи</a>
        </nav>

        <section id="summaryCards" class="summary-grid"></section>

        <section id="view-main" data-view="главное"></section>
        <section id="view-portfolio" data-view="портфель" class="hidden"></section>
        <section id="view-settings" data-view="настройки" class="hidden"></section>
        <section id="view-history" data-view="история" class="hidden"></section>
    </div>

    <div id="toastHost" class="toast-host"></div>

    <div id="modalAddInstrument" class="modal">
        <div class="modal-box">
            <div class="block-head">
                <h2>Добавить инструменты</h2>
                <div class="row-buttons">
                    <input id="instrumentSearchInput" class="field" type="text" placeholder="Тикер или название">
                    <button class="btn" onclick="searchInstruments()">Поиск</button>
                    <button class="btn" onclick="loadTopVolumeInstruments()">Top-20 по объёму</button>
                    <button class="btn btn-primary" onclick="acceptSelectedInstruments()">Применить выбор</button>
                    <button class="btn btn-danger" onclick="closeAddInstrumentModal()">Закрыть</button>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Выбрать</th>
                            <th>Тикер</th>
                            <th>Название</th>
                            <th>FIGI</th>
                            <th>Тип</th>
                            <th>Валюта</th>
                            <th>Лот</th>
                            <th>Шаг цены</th>
                            <th>Цена</th>
                            <th>Время</th>
                            <th>Объём/скор</th>
                        </tr>
                    </thead>
                    <tbody id="instrumentSearchBody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script src="/static/dashboard.js?v=3.5.2-r2"></script>
</body>
</html>
"""
    )


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    return JSONResponse(summary_payload())


@app.get("/api/dashboard/main")
def api_dashboard_main():
    instruments = list_instruments()
    market_map = get_instrument_market_state_map()
    positions = get_open_positions()
    trades = get_trades(limit=20)

    return JSONResponse({
        "instruments": [normalize_instrument_row(i, market_map) for i in instruments],
        "positions": [
            {
                **dict(p),
                "entry_price_ui": fmt_money(p.get("entryprice", 0)),
                "current_price_ui": fmt_money(p.get("currentprice", 0)),
                "unrealized_pnl_ui": fmt_money(p.get("unrealizedpnl", 0)),
                "entry_price_raw": str(p.get("entryprice", 0)),
                "entry_price": str(p.get("entryprice", 0)),
                "current_price": str(p.get("currentprice", 0)),
                "unrealized_pnl": str(p.get("unrealizedpnl", 0)),
                "opened_at": p.get("openedat", ""),
            }
            for p in positions
        ],
        "trades": [
            {
                **dict(t),
                "entry_ui": fmt_money(t.get("entry", 0)),
                "exit_ui": fmt_money(t.get("exit", 0)),
                "pnl_ui": fmt_money(t.get("pnl", 0)),
            }
            for t in trades
        ],
    })


@app.get("/api/dashboard/quotes")
def api_dashboard_quotes():
    rows = get_instrument_market_state()
    return JSONResponse([
        {
            "figi": r.get("figi", ""),
            "ticker": r.get("ticker", ""),
            "last_price": r.get("lastprice", "0"),
            "last_price_ui": fmt_money(r.get("lastprice", 0)),
            "price_time": r.get("pricetime", "") or "-",
        }
        for r in rows
    ])


@app.get("/api/dashboard/portfolio")
def api_dashboard_portfolio():
    bot_positions = get_open_positions(source="BOT")
    all_positions = get_open_positions()
    return JSONResponse({
        "portfolio_positions": [
            {
                "ticker": p.get("ticker", ""),
                "figi": p.get("figi", ""),
                "instrument_type": "share",
                "quantity": str(p.get("qty", 0)),
                "quantity_ui": str(p.get("qty", 0)),
                "average_position_price": str(p.get("entryprice", 0)),
                "average_position_price_ui": fmt_money(p.get("entryprice", 0)),
                "current_price": str(p.get("currentprice", 0)),
                "current_price_ui": fmt_money(p.get("currentprice", 0)),
                "expected_yield": str(p.get("unrealizedpnl", 0)),
                "expected_yield_ui": fmt_money(p.get("unrealizedpnl", 0)),
            }
            for p in all_positions
        ],
        "bot_positions": [
            {
                "ticker": p.get("ticker", ""),
                "figi": p.get("figi", ""),
                "direction": p.get("direction", ""),
                "qty": p.get("qty", 0),
                "entry_price": str(p.get("entryprice", 0)),
                "entry_price_raw": str(p.get("entryprice", 0)),
                "entry_price_ui": fmt_money(p.get("entryprice", 0)),
                "current_price": str(p.get("currentprice", 0)),
                "current_price_ui": fmt_money(p.get("currentprice", 0)),
                "unrealized_pnl": str(p.get("unrealizedpnl", 0)),
                "unrealized_pnl_ui": fmt_money(p.get("unrealizedpnl", 0)),
            }
            for p in bot_positions
        ],
        "stop_orders": [],
    })


@app.get("/api/dashboard/settings")
def api_dashboard_settings():
    market_map = get_instrument_market_state_map()

    profiles = [
        {
            "profile_name": x.get("profilename", ""),
            "is_active": x.get("isactive", 0),
            "created_at": x.get("createdat", ""),
        }
        for x in list_settings_profiles()
    ]

    strategies = [
        {
            "strategy_name": x.get("strategyname", ""),
            "is_active": x.get("isactive", 0),
            "created_at": x.get("createdat", ""),
        }
        for x in list_strategy_profiles()
    ]

    return JSONResponse({
        "settings": settings_payload(),
        "profiles": profiles,
        "strategies": strategies,
        "instruments": [normalize_instrument_row(i, market_map) for i in list_instruments()],
    })


@app.get("/api/dashboard/history")
def api_dashboard_history():
    trades = get_trades(limit=200)
    common_logs = get_logs(limit=300)
    system_logs = get_system_logs(limit=200)
    error_logs = get_error_logs(limit=200)

    def normalize_log(x: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "event_time": x.get("eventtime", ""),
            "event_type": x.get("eventtype", ""),
            "ticker": x.get("ticker", ""),
            "level": x.get("level", ""),
            "message": x.get("message", ""),
        }

    return JSONResponse({
        "trades": [
            {
                **dict(t),
                "entry_ui": fmt_money(t.get("entry", 0)),
                "exit_ui": fmt_money(t.get("exit", 0)),
                "commission_ui": fmt_money(t.get("commission", 0)),
                "pnl_ui": fmt_money(t.get("pnl", 0)),
            }
            for t in trades
        ],
        "system_logs": [normalize_log(x) for x in system_logs],
        "error_logs": [normalize_log(x) for x in error_logs],
        "common_logs": [normalize_log(x) for x in common_logs],
    })


@app.post("/api/control/{action}")
def api_control(action: str):
    return JSONResponse(run_control(action))


@app.post("/api/settings/system")
def api_settings_system(
    bot_enabled: str = Form("1"),
    telegram_errors_only: str = Form("0"),
    auto_reload_settings: str = Form("1"),
):
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
    return JSONResponse({"ok": True})


@app.post("/api/профили/создать")
def api_create_profile(profile_name: str = Form(...)):
    create_settings_profile(profile_name.strip())
    save_current_settings_to_profile(profile_name.strip())
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
def api_instruments_search(
    q: str = Query("", description="Тикер или название"),
    mode: str = Query("", description="top-volume"),
):
    base = list_instruments()
    market_map = get_instrument_market_state_map()
    rows = [normalize_instrument_row(x, market_map) for x in base]

    if mode == "top-volume":
        rows = sorted(rows, key=lambda x: safe_decimal(x.get("last_price", "0")), reverse=True)[:20]
    elif q.strip():
        needle = q.strip().lower()
        rows = [
            x for x in rows
            if needle in str(x.get("ticker", "")).lower() or needle in str(x.get("name", "")).lower()
        ]

    for idx, row in enumerate(rows):
        row["volume_score"] = len(rows) - idx

    return JSONResponse([
        {
            "ticker": x.get("ticker", ""),
            "name": x.get("name", ""),
            "figi": x.get("figi", ""),
            "instrument_type": x.get("instrumenttype", x.get("instrument_type", "")),
            "currency": x.get("currency", ""),
            "lot": x.get("lot", 1),
            "min_price_increment": x.get("minpriceincrement", x.get("min_price_increment", "0.01")),
            "last_price": x.get("last_price", "0"),
            "last_price_ui": x.get("last_price_ui", "0.00"),
            "price_time": x.get("price_time", "-"),
            "volume_score": x.get("volume_score", 0),
        }
        for x in rows
    ])


@app.post("/api/instruments/add")
async def api_instruments_add(request: Request):
    payload = await request.json()
    items = payload.get("items", [])
    added = 0

    for item in items:
        if not item.get("использовать"):
            continue
        add_instrument({
            "ticker": item.get("ticker", ""),
            "figi": item.get("figi", ""),
            "name": item.get("name", ""),
            "classcode": "",
            "instrumenttype": item.get("instrument_type", ""),
            "currency": item.get("currency", ""),
            "lot": item.get("lot", 1),
            "minpriceincrement": item.get("min_price_increment", "0.01"),
            "lotsoverride": 1,
            "stoplosspct": "0.0025",
            "takeprofitpct": "0.005",
            "maxspreadpct": "0",
            "minvolume": 0,
            "allowlong": 1,
            "allowshort": 1,
            "priority": 100,
            "enabled": 1,
        })
        added += 1

    return JSONResponse({"ok": True, "добавлено": added})


@app.post("/api/instruments/update")
def api_instruments_update(
    figi: str = Form(...),
    lots_override: str = Form("1"),
    stop_loss_pct: str = Form("0.25"),
    take_profit_pct: str = Form("0.50"),
    max_spread_pct: str = Form("0"),
    min_volume: str = Form("0"),
    allow_long: str = Form("1"),
    allow_short: str = Form("1"),
    priority: str = Form("100"),
    enabled: str = Form("1"),
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


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    return JSONResponse({"ok": True})


@app.post("/api/позиции/закрыть")
def api_close_one_position(figi: str = Form(...), qty: str = Form(...), direction: str = Form(...)):
    return JSONResponse({"ok": True, "message": f"close requested: {figi} {qty} {direction}"})


@app.post("/api/позиции/закрыть-все")
def api_close_all_positions():
    return JSONResponse({"ok": True, "message": "close all requested"})


@app.post("/api/стоп-заявки/создать")
def api_create_stop_orders(
    figi: str = Form(...),
    qty: str = Form(...),
    side: str = Form(...),
    base_price: str = Form(...),
    stop_loss_pct: str = Form("0.25"),
    take_profit_pct: str = Form("0.50"),
):
    return JSONResponse({
        "ok": True,
        "message": f"stops create requested for {figi}",
        "figi": figi,
        "qty": qty,
        "side": side,
        "base_price": base_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
    })


@app.post("/api/стоп-заявки/отменить")
def api_cancel_stop_order(stop_order_id: str = Form(...)):
    return JSONResponse({"ok": True, "message": f"stop cancel requested: {stop_order_id}"})