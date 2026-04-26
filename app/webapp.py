import os
import logging
logger = logging.getLogger(__name__)
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from t_tech.invest import Client

import platform
import subprocess

from pydantic import BaseModel

from fastapi import FastAPI, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.control import run_control
from app.db import (
    init_db,
    get_all_settings,
    get_all_runtime,
    get_trade_stats_today,
    get_trades,
    get_logs,
    get_error_logs,
    get_system_logs,
    list_instruments,
    add_instrument,
    get_open_positions,
    get_instrument_market_state,
    get_instrument_market_state_map,
    get_setting,
    log_event,
    # profiles
    list_profiles,
    get_profile,
    get_profile_settings,
    create_profile,
    delete_profile,
    activate_profile,
    update_profile_settings,
    set_profile_strategy,
    # strategies
    list_strategies,
    get_strategy,
    get_strategy_settings,
    create_strategy,
    delete_strategy,
    update_strategy_settings,
    # strategy instruments
    list_strategy_instruments,
    add_strategy_instrument,
    update_strategy_instrument,
    delete_strategy_instrument,
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
    get_portfolio_snapshot,
)
from app.services.strategy_engine import evaluate_signal
from app.services.healthcheck import dashboard_health
from decimal import Decimal
from app.telegram_health import send_telegram, health_snapshot

app = FastAPI(title="Trading Bot Dashboard v4.2")

if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


def safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def quotation_to_decimal(q) -> Decimal:
    if q is None:
        return Decimal("0")
    units = getattr(q, "units", 0) or 0
    nano = getattr(q, "nano", 0) or 0
    return Decimal(str(units)) + (Decimal(str(nano)) / Decimal("1000000000"))


def fmt_money(value: Any) -> str:
    return f"{safe_decimal(value):.2f}"


def money_value_to_text(v) -> str:
    if v is None:
        return ""
    try:
        return format(quotation_to_decimal(v), "f")
    except Exception:
        return ""


def is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def fmt_pct_fraction(value: Any) -> str:
    return f"{safe_decimal(value) * Decimal('100'):.2f}"


def bool01(value: Any) -> str:
    return "1" if str(value) in ("1", "true", "True") else "0"


def get_service_status_value() -> str:
    try:
        system_name = platform.system().lower()
        if system_name == "windows":
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'main.py' } | "
                 "Select-Object -First 1 -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=5,
            )
            output = (result.stdout or "").strip()
            return "Запущен" if output else "Остановлен"

        result = run_control("status")
        raw = " ".join([str(result.get("message", "") or ""), str(result.get("output", "") or "")]).lower()
        if "active (running)" in raw or "is running" in raw or "active: active" in raw:
            return "Запущен"
        if "inactive" in raw or "dead" in raw or "stopped" in raw or "not running" in raw:
            return "Остановлен"
        return "Проблема"
    except Exception:
        return "Проблема"


def strategy_instrument_row(row: Dict[str, Any], market_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    figi = row.get("figi", "")
    market = market_map.get(figi, {})
    out = dict(row)
    out["stop_loss_pct_ui"] = fmt_pct_fraction(row.get("stop_loss_pct", 0))
    out["take_profit_pct_ui"] = fmt_pct_fraction(row.get("take_profit_pct", 0))
    out["max_spread_pct_ui"] = fmt_pct_fraction(row.get("max_spread_pct", 0))
    out["last_price"] = market.get("last_price", "0")
    out["last_price_ui"] = fmt_money(market.get("last_price", 0))
    out["price_time"] = market.get("price_time", "") or "-"
    return out


def market_row(row: Dict[str, Any], market_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return strategy_instrument_row(row, market_map)


def summary_payload() -> Dict[str, Any]:
    s = get_all_settings()
    st = get_trade_stats_today()

    active_profile_name = (s.get("active_profile_name", "") or "").strip() or "—"
    active_strategy_name = (s.get("active_strategy_name", "") or "").strip() or "—"
    bot_enabled = is_truthy(s.get("bot_enabled", "1"))

    active_strategy_id = (s.get("active_strategy_id", "") or "").strip()
    if active_strategy_id:
        strategy_instruments = list_strategy_instruments(int(active_strategy_id))
    else:
        strategy_instruments = []
    enabled_instruments = [x for x in strategy_instruments if str(x.get("enabled", 0)) in ("1", "true")]

    service_status = get_service_status_value()

    try:
        portfolio = get_portfolio_snapshot()
        portfolio_ok = True
    except Exception as e:
        logger.exception("portfolio snapshot error")
        portfolio_ok = False
        portfolio = {
            "cash": Decimal("0"), "positions_value": Decimal("0"),
            "blocked": Decimal("0"), "total_assets": Decimal("0"),
            "positions_count": 0, "money_by_currency": [],
        }
        if not s.get("last_error"):
            s["last_error"] = str(e)

    if service_status != "Запущен":
        trading_status = "Остановлена"
    elif not bot_enabled:
        trading_status = "Остановлена"
    elif active_profile_name == "—":
        trading_status = "Проблема"
    elif active_strategy_name == "—":
        trading_status = "Проблема"
    elif len(enabled_instruments) == 0:
        trading_status = "Проблема"
    elif not portfolio_ok:
        trading_status = "Проблема"
    else:
        trading_status = "Ведётся"

    return {
        "status": service_status,
        "trading_status": trading_status,
        "bot_enabled": "1" if bot_enabled else "0",
        "trades_today": st.get("trades_count", 0),
        "daily_pnl_ui": fmt_money(st.get("total_pnl", 0)),
        "total_commission_ui": fmt_money(st.get("total_commission", 0)),
        "cash_rub_ui": fmt_money(portfolio.get("cash", 0)),
        "positions_value_rub_ui": fmt_money(portfolio.get("positions_value", 0)),
        "blocked_rub_ui": fmt_money(portfolio.get("blocked", 0)),
        "total_assets_rub_ui": fmt_money(portfolio.get("total_assets", 0)),
        "positions_count": portfolio.get("positions_count", 0),
        "money_by_currency": portfolio.get("money_by_currency", []),
        "active_profile_name": active_profile_name,
        "active_strategy_name": active_strategy_name,
        "last_error": s.get("last_error", "") or "—",
    }


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse("""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Панель управления торговым ботом v4.2</title>
  <link rel="stylesheet" href="/static/dashboard.css?v=4.2">
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div>
        <h1>Панель управления торговым ботом v4.2</h1>
        <p class="sub">Профили содержат общие настройки и выбор стратегии. Стратегия содержит параметры риска и инструменты.</p>
      </div>
      <div id="routeDebugBadge" class="route-badge">Вкладка: главное</div>
    </header>

    <nav class="tabs">
      <a href="#/главное" class="tab-link active" data-tab-link="главное">Главное</a>
      <a href="#/портфель" class="tab-link" data-tab-link="портфель">Портфель</a>
      <a href="#/настройки" class="tab-link" data-tab-link="настройки">Настройки</a>
      <a href="#/история" class="tab-link" data-tab-link="история">История</a>
      <a href="#/график" class="tab-link" data-tab-link="график">График</a>
    </nav>

    <section id="summaryCards" class="summary-grid"></section>

    <section id="view-main" data-view="главное"></section>
    <section id="view-portfolio" data-view="портфель" class="hidden"></section>
    <section id="view-settings" data-view="настройки" class="hidden"></section>
    <section id="view-history" data-view="история" class="hidden"></section>
    <section id="view-chart" data-view="график" class="hidden"></section>
  </div>

  <div id="toastHost" class="toast-host"></div>

  <!-- Modal: Add instrument -->
  <div id="modalAddInstrument" class="modal hidden">
    <div class="modal-box">
      <div class="row between">
        <h2>Добавить инструменты</h2>
        <div class="row">
          <input id="instrumentSearchInput" class="field" type="text" placeholder="Тикер или название">
          <button class="btn" onclick="searchInstruments()">Поиск</button>
          <button class="btn" onclick="loadTopVolumeInstruments()">Топ</button>
          <button class="btn" onclick="selectAllInstrumentSearchRows()">Выделить все</button>
          <button class="btn" onclick="clearAllInstrumentSearchRows()">Снять все</button>
          <button class="btn btn-primary" onclick="acceptSelectedInstruments()">Добавить выбранные</button>
          <button class="btn btn-danger" onclick="closeAddInstrumentModal()">Закрыть</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Выб.</th><th>Тикер</th><th>Название</th><th>FIGI</th><th>Тип</th><th>Валюта</th><th>Лот</th><th>Шаг</th><th>Цена</th><th>Время</th><th>Скор</th></tr>
          </thead>
          <tbody id="instrumentSearchRows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Modal: Profiles -->
  <div id="modalProfiles" class="modal hidden">
    <div class="modal-box">
      <div class="row between">
        <h2>Профили</h2>
        <button class="btn btn-danger" onclick="closeProfilesModal()">Закрыть</button>
      </div>
      <div id="profilesModalBody"></div>
      <div class="row" style="margin-top:12px;">
        <input id="newProfileName" class="field" type="text" placeholder="Имя нового профиля">
        <button class="btn btn-primary" onclick="createProfile()">Создать профиль</button>
      </div>
    </div>
  </div>

  <!-- Modal: Strategies -->
  <div id="modalStrategies" class="modal hidden">
    <div class="modal-box">
      <div class="row between">
        <h2>Стратегии</h2>
        <button class="btn btn-danger" onclick="closeStrategiesModal()">Закрыть</button>
      </div>
      <div id="strategiesModalBody"></div>
      <div class="row" style="margin-top:12px;">
        <input id="newStrategyName" class="field" type="text" placeholder="Имя новой стратегии">
        <button class="btn btn-primary" onclick="createStrategy()">Создать стратегию</button>
      </div>
    </div>
  </div>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <script src="/static/dashboard.js?v=4.2"></script>
</body>
</html>""")


# ── dashboard data endpoints ───────────────────────────────────────────────────

@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    return JSONResponse(summary_payload())


@app.get("/api/dashboard/main")
def api_dashboard_main():
    s = get_all_settings()
    active_strategy_id = (s.get("active_strategy_id", "") or "").strip()
    market_map = get_instrument_market_state_map()

    if active_strategy_id:
        instruments = list_strategy_instruments(int(active_strategy_id))
    else:
        instruments = []

    return JSONResponse({
        "instruments": [strategy_instrument_row(i, market_map) for i in instruments],
        "positions": [{
            **dict(p),
            "entry_price_ui": fmt_money(p.get("entry_price", 0)),
            "current_price_ui": fmt_money(p.get("current_price", 0)),
            "unrealized_pnl_ui": fmt_money(p.get("unrealized_pnl", 0)),
            "opened_at": p.get("opened_at", ""),
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
    return JSONResponse([{
        "figi": r.get("figi", ""),
        "ticker": r.get("ticker", ""),
        "last_price_ui": fmt_money(r.get("last_price", 0)),
        "price_time": r.get("price_time", "-"),
    } for r in rows])


@app.get("/api/dashboard/portfolio")
def api_dashboard_portfolio():
    bot_positions = get_open_positions(source="BOT")
    all_positions = get_open_positions()
    return JSONResponse({
        "portfolio_positions": [{
            "ticker": p.get("ticker", ""), "figi": p.get("figi", ""), "instrument_type": "share",
            "quantity_ui": str(p.get("qty", 0)),
            "average_position_price_ui": fmt_money(p.get("entry_price", 0)),
            "current_price_ui": fmt_money(p.get("current_price", 0)),
            "expected_yield_ui": fmt_money(p.get("unrealized_pnl", 0)),
        } for p in all_positions],
        "bot_positions": [{
            "ticker": p.get("ticker", ""), "figi": p.get("figi", ""), "direction": p.get("direction", ""),
            "qty": p.get("qty", 0),
            "entry_price_ui": fmt_money(p.get("entry_price", 0)),
            "entry_price_raw": str(p.get("entry_price", 0)),
            "current_price_ui": fmt_money(p.get("current_price", 0)),
            "unrealized_pnl_ui": fmt_money(p.get("unrealized_pnl", 0)),
        } for p in bot_positions],
        "stop_orders": [],
    })


@app.get("/api/dashboard/stop-orders")
def api_dashboard_stop_orders():
    try:
        items = get_active_stop_orders()
        return {"ok": True, "items": items}
    except Exception as e:
        return {"ok": False, "items": [], "message": str(e)}


@app.get("/api/dashboard/runtime")
def api_dashboard_runtime():
    settings_map = get_all_settings()
    runtime_map = get_all_runtime()
    return {
        "botenabled": settings_map.get("bot_enabled", "1"),
        "tinvestusesandbox": settings_map.get("tinvestusesandbox", "true"),
        "activeprofilename": settings_map.get("active_profile_name", ""),
        "activestrategyname": settings_map.get("active_strategy_name", ""),
        "lasterror": settings_map.get("last_error", ""),
        "status": runtime_map.get("status", settings_map.get("status", "INIT")),
        "runtime": runtime_map,
    }


@app.get("/api/dashboard/bot-explain")
def api_dashboard_bot_explain():
    settings_map = get_all_settings()
    active_strategy_id = (settings_map.get("active_strategy_id", "") or "").strip()
    if active_strategy_id:
        instruments = list_strategy_instruments(int(active_strategy_id))
        enabled_instruments = [x for x in instruments if str(x.get("enabled", 0)) in ("1", "true")]
        strategy = get_strategy(int(active_strategy_id))
        strat_settings = get_strategy_settings(int(active_strategy_id)) if strategy else {}
    else:
        enabled_instruments = []
        strat_settings = {}
    open_positions = get_open_positions(source="BOT")

    reasons = []
    if str(settings_map.get("bot_enabled", "1")) != "1":
        reasons.append("Бот выключен в настройках")
    if not active_strategy_id:
        reasons.append("Не выбрана стратегия")
    elif not enabled_instruments:
        reasons.append("Нет активных инструментов в стратегии")
    if str(strat_settings.get("trade_only_session", "0")) == "1":
        reasons.append("Торговля ограничена торговой сессией")
    if len(open_positions) >= int(str(strat_settings.get("max_open_positions", "10"))):
        reasons.append(f"Достигнут лимит открытых позиций: {len(open_positions)} >= {int(str(strat_settings.get('max_open_positions', '10')))}")
    if int(str(strat_settings.get("max_trades_per_day", "15"))) <= int(str(settings_map.get("trades_today", "0"))):
        reasons.append("Достигнут лимит сделок за день")
    if not reasons:
        reasons.append("Бот готов искать сигнал")
    return {"ok": True, "reasons": reasons}


@app.get("/api/dashboard/settings")
def api_dashboard_settings(profile_id: Optional[int] = None):
    s = get_all_settings()
    market_map = get_instrument_market_state_map()

    active_profile_id_str = (s.get("active_profile_id", "") or "").strip()
    view_id = profile_id or (int(active_profile_id_str) if active_profile_id_str else None)

    profile = get_profile(view_id) if view_id else {}
    prof_settings = get_profile_settings(view_id) if view_id else {}

    strategy_id = profile.get("strategy_id")
    strategy = get_strategy(strategy_id) if strategy_id else {}
    strat_settings = get_strategy_settings(strategy_id) if strategy_id else {}
    strat_instruments = list_strategy_instruments(strategy_id) if strategy_id else []

    def ps(key, default=""):
        return prof_settings.get(key, default)

    def ss(key, default=""):
        return strat_settings.get(key, default)

    return JSONResponse({
        "active_profile_id": active_profile_id_str,
        "active_profile_name": s.get("active_profile_name", ""),
        "active_strategy_id": s.get("active_strategy_id", ""),
        "active_strategy_name": s.get("active_strategy_name", ""),
        "view_profile": {
            "id": profile.get("id"),
            "name": profile.get("name", ""),
            "is_active": profile.get("is_active", 0),
            "strategy_id": profile.get("strategy_id"),
            "strategy_name": profile.get("strategy_name", ""),
            "settings": {
                "bot_enabled": ps("bot_enabled", "1"),
                "telegram_errors_only": ps("telegram_errors_only", "0"),
                "auto_reload_settings": ps("auto_reload_settings", "1"),
                "tinvestusesandbox": ps("tinvestusesandbox", "true"),
            },
        },
        "view_strategy": {
            "id": strategy.get("id"),
            "name": strategy.get("name", ""),
            "settings": {
                "max_trades_per_day": ss("max_trades_per_day", "15"),
                "max_daily_loss_rub": ss("max_daily_loss_rub", "200"),
                "max_daily_loss_rub_ui": fmt_money(ss("max_daily_loss_rub", "200")),
                "max_open_positions": ss("max_open_positions", "2"),
                "check_interval_sec": ss("check_interval_sec", "5"),
                "default_stop_loss_pct_ui": fmt_pct_fraction(ss("default_stop_loss_pct", "0.0025")),
                "default_take_profit_pct_ui": fmt_pct_fraction(ss("default_take_profit_pct", "0.005")),
                "estimated_commission_pct_ui": fmt_pct_fraction(ss("estimated_commission_pct", "0.0004")),
                "allow_long_global": ss("allow_long_global", "1"),
                "allow_short_global": ss("allow_short_global", "1"),
                "trade_only_session": ss("trade_only_session", "0"),
                "pause_after_error_sec": ss("pause_after_error_sec", "10"),
                "tradingmode": ss("tradingmode", "trend"),
                "errorseriespausecount": ss("errorseriespausecount", "3"),
                "stopseriespausecount": ss("stopseriespausecount", "3"),
            },
        },
        "profiles": list_profiles(),
        "strategies": list_strategies(),
        "instruments": [strategy_instrument_row(i, market_map) for i in strat_instruments],
    })


@app.get("/api/dashboard/history")
def api_dashboard_history():
    def norm(x):
        return {
            "event_time": x.get("event_time", ""),
            "event_type": x.get("event_type", ""),
            "ticker": x.get("ticker", ""),
            "level": x.get("level", ""),
            "message": x.get("message", ""),
        }
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
    instruments = list_instruments()
    available = [{"figi": i["figi"], "ticker": i["ticker"], "name": i.get("name", "")} for i in instruments]
    selected_figi = figi or (available[0]["figi"] if available else "")
    candles = []
    if selected_figi:
        try:
            candles = get_candles(selected_figi, interval_name=interval, hours=8)
        except Exception as e:
            log_event("BOT_ERROR", f"chart candles error: {e}", level="ERROR")
    signal = {"action": "HOLD", "score": 0, "reasons": ["Нет данных"]}
    if candles:
        try:
            mode = get_setting("tradingmode", "trend")
            signal = evaluate_signal(selected_figi, candles, mode=mode)
        except Exception:
            pass
    return {
        "figi": selected_figi, "interval": interval, "candles": candles,
        "signal": signal, "available_instruments": available, "selected_figi": selected_figi,
    }


# ── control ───────────────────────────────────────────────────────────────────

@app.post("/api/control/{action}")
def api_control(action: str):
    return JSONResponse(run_control(action))


@app.get("/api/health")
def api_health():
    return dashboard_health()


# ── profiles API ──────────────────────────────────────────────────────────────

@app.post("/api/profiles/create")
def api_profiles_create(name: str = Form(...)):
    try:
        profile_id = create_profile(name.strip())
        return JSONResponse({"ok": True, "id": profile_id})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/profiles/{profile_id}/activate")
def api_profiles_activate(profile_id: int):
    try:
        activate_profile(profile_id)
        return JSONResponse({"ok": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/profiles/{profile_id}/set-strategy")
def api_profiles_set_strategy(profile_id: int, strategy_id: int = Form(...)):
    try:
        set_profile_strategy(profile_id, strategy_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/profiles/{profile_id}/settings")
def api_profiles_save_settings(
    profile_id: int,
    bot_enabled: str = Form("1"),
    telegram_errors_only: str = Form("0"),
    auto_reload_settings: str = Form("1"),
    runtime_mode: str = Form("sandbox"),
):
    use_sandbox = "true" if runtime_mode == "sandbox" else "false"
    update_profile_settings(profile_id, {
        "bot_enabled": bool01(bot_enabled),
        "telegram_errors_only": bool01(telegram_errors_only),
        "auto_reload_settings": bool01(auto_reload_settings),
        "tinvestusesandbox": use_sandbox,
    })
    return JSONResponse({"ok": True})


@app.post("/api/profiles/{profile_id}/delete")
def api_profiles_delete(profile_id: int):
    try:
        delete_profile(profile_id)
        return JSONResponse({"ok": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── strategies API ────────────────────────────────────────────────────────────

@app.post("/api/strategies/create")
def api_strategies_create(name: str = Form(...)):
    try:
        strategy_id = create_strategy(name.strip())
        return JSONResponse({"ok": True, "id": strategy_id})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/strategies/{strategy_id}/settings")
def api_strategies_save_settings(
    strategy_id: int,
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
    tradingmode: str = Form("trend"),
    errorseriespausecount: str = Form("3"),
    stopseriespausecount: str = Form("3"),
):
    update_strategy_settings(strategy_id, {
        "max_trades_per_day": max_trades_per_day,
        "max_daily_loss_rub": max_daily_loss_rub,
        "max_open_positions": max_open_positions,
        "check_interval_sec": check_interval_sec,
        "default_stop_loss_pct": str(safe_decimal(default_stop_loss_pct) / Decimal("100")),
        "default_take_profit_pct": str(safe_decimal(default_take_profit_pct) / Decimal("100")),
        "estimated_commission_pct": str(safe_decimal(estimated_commission_pct) / Decimal("100")),
        "allow_long_global": bool01(allow_long_global),
        "allow_short_global": bool01(allow_short_global),
        "trade_only_session": bool01(trade_only_session),
        "pause_after_error_sec": pause_after_error_sec,
        "tradingmode": tradingmode,
        "errorseriespausecount": errorseriespausecount,
        "stopseriespausecount": stopseriespausecount,
    })
    return JSONResponse({"ok": True})


@app.post("/api/strategies/{strategy_id}/delete")
def api_strategies_delete(strategy_id: int):
    try:
        delete_strategy(strategy_id)
        return JSONResponse({"ok": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── strategy instruments API ──────────────────────────────────────────────────

@app.post("/api/strategies/{strategy_id}/instruments/add")
async def api_strategy_instruments_add(strategy_id: int, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    items = payload if isinstance(payload, list) else payload.get("items", [])
    added = 0
    for item in items:
        figi = (item.get("figi") or "").strip()
        if not figi:
            continue
        inst = {
            "ticker": (item.get("ticker") or "").strip(),
            "figi": figi,
            "name": item.get("name", ""),
            "class_code": item.get("class_code", item.get("classcode", "")),
            "instrument_type": item.get("instrument_type", item.get("instrumenttype", "share")),
            "currency": item.get("currency", "RUB"),
            "lot": int(item.get("lot") or 1),
            "min_price_increment": str(item.get("min_price_increment", item.get("minpriceincrement", "0.01")) or "0.01"),
            "lots_override": int(item.get("lots_override", 1) or 1),
            "stop_loss_pct": str(item.get("stop_loss_pct", "0.0025") or "0.0025"),
            "take_profit_pct": str(item.get("take_profit_pct", "0.005") or "0.005"),
            "max_spread_pct": str(item.get("max_spread_pct", "0") or "0"),
            "min_volume": int(item.get("min_volume", 0) or 0),
            "allow_long": int(item.get("allow_long", 1) or 1),
            "allow_short": int(item.get("allow_short", 1) or 1),
            "priority": int(item.get("priority", 100) or 100),
            "enabled": 1,
        }
        add_strategy_instrument(strategy_id, inst)
        add_instrument(inst)  # keep catalog up to date for market data
        added += 1
    return JSONResponse({"ok": True, "добавлено": added})


@app.post("/api/strategies/{strategy_id}/instruments/update")
def api_strategy_instruments_update(
    strategy_id: int,
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
    update_strategy_instrument(strategy_id, figi, {
        "lots_override": lots_override,
        "stop_loss_pct": str(safe_decimal(stop_loss_pct) / Decimal("100")),
        "take_profit_pct": str(safe_decimal(take_profit_pct) / Decimal("100")),
        "max_spread_pct": str(safe_decimal(max_spread_pct) / Decimal("100")),
        "min_volume": min_volume,
        "allow_long": int(bool01(allow_long)),
        "allow_short": int(bool01(allow_short)),
        "priority": priority,
        "enabled": int(bool01(enabled)),
    })
    return JSONResponse({"ok": True})


@app.post("/api/strategies/{strategy_id}/instruments/delete")
def api_strategy_instruments_delete(strategy_id: int, figi: str = Form(...)):
    delete_strategy_instrument(strategy_id, figi)
    return JSONResponse({"ok": True})


# ── instrument search ─────────────────────────────────────────────────────────

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
                "currency": str(getattr(inst, "currency", "") or "").upper(),
                "lot": getattr(inst, "lot", None),
                "min_price_increment": quotation_to_decimal(getattr(inst, "min_price_increment", None)) if getattr(inst, "min_price_increment", None) else None,
                "api_trade_available_flag": bool(getattr(inst, "api_trade_available_flag", False)),
                "for_qual_investor_flag": bool(getattr(inst, "for_qual_investor_flag", False)),
                "liquidity_flag": bool(getattr(inst, "liquidity_flag", False)),
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
            if x["class_code"] == "TQBR":
                score += 500
            elif x["class_code"] == "TQTF":
                score += 200
            elif x["class_code"] == "SPBFUT":
                score += 100
            elif x["class_code"] in ("SMAL", "SPEQ", "BEB", "RDL"):
                score -= 50
            if x.get("api_trade_available_flag"):
                score += 100
            if x.get("liquidity_flag"):
                score += 80
            if x.get("for_qual_investor_flag"):
                score -= 500
            return score

        items.sort(key=lambda x: (-score_item(x), x["ticker"], x["name"]))
        selected = items[:20]
        price_map = {}
        figis = [x["figi"] for x in selected if x.get("figi")]
        if figis:
            try:
                with Client(settings.TINVEST_TOKEN) as client:
                    prices_resp = client.market_data.get_last_prices(figi=figis)
                for p in getattr(prices_resp, "last_prices", []):
                    price_map[getattr(p, "figi", "")] = {
                        "last_price": money_value_to_text(getattr(p, "price", None)),
                        "price_time": str(getattr(p, "time", "") or "")[:19].replace("T", " "),
                    }
            except Exception:
                logger.exception("Ошибка получения last_prices для поиска")

        result = []
        for x in selected:
            row = dict(x)
            row["score"] = score_item(x)
            row["classcode"] = row.get("class_code", "")
            row["instrumenttype"] = row.get("instrument_type", "")
            row["minpriceincrement"] = str(row.get("min_price_increment") or "")
            row["last_price"] = price_map.get(row.get("figi", ""), {}).get("last_price", "")
            row["price_time"] = price_map.get(row.get("figi", ""), {}).get("price_time", "")
            result.append(row)
        return result
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
                "classcode": getattr(inst, "class_code", "") or "",
                "instrument_type": "share", "instrumenttype": "share",
                "currency": str(getattr(inst, "currency", "") or "").upper(),
                "lot": getattr(inst, "lot", None),
                "min_price_increment": quotation_to_decimal(getattr(inst, "min_price_increment", None)) if getattr(inst, "min_price_increment", None) else None,
                "minpriceincrement": str(quotation_to_decimal(getattr(inst, "min_price_increment", None))) if getattr(inst, "min_price_increment", None) else "",
                "api_trade_available_flag": bool(getattr(inst, "api_trade_available_flag", False)),
                "for_qual_investor_flag": bool(getattr(inst, "for_qual_investor_flag", False)),
                "liquidity_flag": bool(getattr(inst, "liquidity_flag", False)),
                "last_price": "", "price_time": "",
            }
            items.append(item)

        filtered = [
            x for x in items
            if x["api_trade_available_flag"] and not x["for_qual_investor_flag"]
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
            if x["api_trade_available_flag"]:
                score += 50
            return score

        filtered.sort(key=lambda x: (-top_score(x), x["ticker"], x["name"]))
        selected = filtered[:limit]
        price_map = {}
        figis = [x["figi"] for x in selected if x.get("figi")]
        if figis:
            try:
                with Client(settings.TINVEST_TOKEN) as client:
                    prices_resp = client.market_data.get_last_prices(figi=figis)
                for p in getattr(prices_resp, "last_prices", []):
                    price_map[getattr(p, "figi", "")] = {
                        "last_price": money_value_to_text(getattr(p, "price", None)),
                        "price_time": str(getattr(p, "time", "") or "")[:19].replace("T", " "),
                    }
            except Exception:
                logger.exception("Ошибка получения last_prices для top")

        result = []
        for x in selected:
            row = dict(x)
            row["score"] = top_score(x)
            row["last_price"] = price_map.get(row.get("figi", ""), {}).get("last_price", "")
            row["price_time"] = price_map.get(row.get("figi", ""), {}).get("price_time", "")
            result.append(row)
        return result
    except Exception as e:
        logger.exception("Ошибка top-20 инструментов")
        raise HTTPException(status_code=500, detail=str(e))


# ── positions ─────────────────────────────────────────────────────────────────

@app.post("/api/позиции/закрыть")
def api_close_position(figi: str = Form(...), qty: int = Form(...), direction: str = Form(...)):
    close_direction = "LONG_CLOSE" if str(direction).upper() == "BUY" else "SHORT_CLOSE"
    result = post_market_close(figi=figi, quantity=int(qty), direction=close_direction)
    log_event("POSITION_CLOSE", f"close order posted figi={figi} qty={qty} direction={close_direction}", ticker=figi)
    return {"ok": True, "message": "close order posted", "order_id": getattr(result, "order_id", "")}


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


# ── stop orders ───────────────────────────────────────────────────────────────

@app.post("/api/stop-orders/create-bundle")
def api_create_stop_bundle(
    figi: str = Form(...), qty: int = Form(...), entry_price: str = Form(...),
    side: str = Form(...), stop_pct: str = Form(...), take_pct: str = Form(...),
):
    result = post_stop_bundle(
        figi=figi, quantity=int(qty), entry_price=Decimal(entry_price),
        side=side, stop_pct=Decimal(stop_pct), take_pct=Decimal(take_pct),
    )
    log_event("STOP_BUNDLE", f"bundle created figi={figi}", ticker=figi)
    return {"ok": True, **result}


# ── health / telegram ─────────────────────────────────────────────────────────

@app.post("/api/health/telegram-test")
def api_health_telegram_test():
    health = dashboard_health()
    text = health_snapshot(
        dashboard_ok=(health.get("status") == "ok"),
        broker_ok=True, target="runtime", extra="manual test"
    )
    result = send_telegram(text)
    return {"ok": True, "telegram": result}


@app.get("/api/debug/search")
def api_debug_search(q: str = "SBER"):
    import traceback
    from t_tech.invest.sandbox.client import SandboxClient
    client_cls = SandboxClient if settings.TINVEST_USE_SANDBOX else Client
    try:
        with client_cls(settings.TINVEST_TOKEN) as client:
            resp = client.instruments.find_instrument(query=q)
            instruments = getattr(resp, "instruments", [])
            return {
                "ok": True, "count": len(instruments),
                "items": [{"ticker": getattr(x, "ticker", ""), "figi": getattr(x, "figi", ""),
                           "name": getattr(x, "name", ""), "instrument_type": str(getattr(x, "instrument_type", ""))}
                          for x in instruments],
            }
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
