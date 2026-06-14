import json
import os
import logging
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
logger = logging.getLogger(__name__)
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from t_tech.invest import Client

import platform
import subprocess

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.control import run_control
from app.db import (
    init_db,
    get_all_settings,
    get_all_runtime,
    get_runtime,
    get_trade_stats_today,
    get_trades,
    get_logs,
    get_error_logs,
    get_system_logs,
    list_instruments,
    add_instrument,
    get_open_positions,
    clear_open_positions,
    get_instrument_market_state,
    get_instrument_market_state_map,
    get_setting,
    log_event,
    # profiles
    list_profiles,
    get_profile,
    get_profile_settings,
    get_profile_setting,
    create_profile,
    delete_profile,
    activate_profile,
    update_profile_settings,
    set_profile_strategy,
    # parallel
    list_profile_parallel_strategies,
    add_profile_parallel_strategy,
    remove_profile_parallel_strategy,
    set_strategy_parallel,
    list_parallel_strategies,
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
    get_history_stats,
    clear_history,
    get_strategy_trade_stats,
    apply_auto_name,
    set_setting_all_strategies,
    create_optimization_session,
    list_optimization_sessions,
    get_optimization_session,
    get_session_results,
    update_session_status,
    link_results_to_session,
    get_weekly_stats,
)

from app.config import settings
from app.version import __version__ as BOT_VERSION
from app.services.tbank_client import (
    get_candles,
    get_candles_range,
    get_active_stop_orders,
    cancel_stop_order,
    post_market_close,
    post_stop_bundle,
    get_portfolio_snapshot,
    get_operations_today,
    get_broker_positions,
    get_positions_detailed,
    get_operations_by_cursor,
    sandbox_pay_in,
    sandbox_reset_account,
    get_active_orders,
    cancel_order,
)
from app.services.strategy_engine import evaluate_signal
from app.services.backtest_engine import run_backtest, result_to_dict
import app.services.analyst as _analyst
from app.services.healthcheck import dashboard_health
from decimal import Decimal
from app.telegram_health import send_telegram, health_snapshot

app = FastAPI(title=f"Trading Bot Dashboard v{BOT_VERSION}")

if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ── Кэш потока портфеля (Приоритет-2) ────────────────────────────────────────
_portfolio_cache: Dict[str, Any] = {}   # последний снимок из OperationsStream
_portfolio_cache_lock = threading.Lock()


def _portfolio_stream_worker():
    """Фоновый поток: подписывается на portfolio_stream T-Bank и обновляет кэш."""
    from app.config import settings as _cfg
    from app.services.tbank_client import with_client
    while True:
        try:
            with with_client() as sc:
                logger.info("PortfolioStream: подключён")
                for resp in sc.operations_stream.portfolio_stream(accounts=[_cfg.TINVEST_ACCOUNT_ID]):
                    pf = getattr(resp, "portfolio", None)
                    if pf is None:
                        continue  # ping
                    positions = []
                    for pos in getattr(pf, "positions", []):
                        figi = getattr(pos, "figi", "") or ""
                        instrument_type = str(getattr(pos, "instrument_type", "") or "")
                        if not figi or "currency" in instrument_type.lower():
                            continue
                        from t_tech.invest.utils import quotation_to_decimal as _qtd
                        from app.services.tbank_client import (
                            quotation_to_decimal_safe as _qts,
                            _money_value_to_decimal as _mvd,
                        )
                        qty_raw = _qts(getattr(pos, "quantity", None))
                        qty_lots = _qts(getattr(pos, "quantity_lots", None))
                        avg_p = _mvd(getattr(pos, "average_position_price", None))
                        cur_p = _mvd(getattr(pos, "current_price", None))
                        exp_y = _qts(getattr(pos, "expected_yield", None))
                        direction = "SELL" if qty_raw < 0 else "BUY"
                        positions.append({
                            "figi": figi,
                            "instrument_type": instrument_type,
                            "direction": direction,
                            "qty": int(abs(qty_lots)),
                            "qty_shares": int(abs(qty_raw)),
                            "avg_price": str(avg_p),
                            "current_price": str(cur_p),
                            "expected_yield": str(exp_y),
                        })
                    total_assets = getattr(pf, "total_amount_portfolio", None)
                    from app.services.tbank_client import quotation_to_decimal_safe as _qts2
                    with _portfolio_cache_lock:
                        _portfolio_cache["positions"] = positions
                        _portfolio_cache["total_assets"] = str(_qts2(total_assets)) if total_assets else "0"
                        from datetime import datetime, timezone, timedelta as _td
                        _msk = timezone(_td(hours=3))
                        _portfolio_cache["updated_at"] = datetime.now(tz=_msk).replace(tzinfo=None).isoformat()
        except Exception as exc:
            logger.warning("PortfolioStream: ошибка %s, переподключение через 10 с", exc)
            import time; time.sleep(10)


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


def fmt_price(value: Any) -> str:
    """Like fmt_money but returns '—' for zero (missing fill price)."""
    d = safe_decimal(value)
    return f"{d:.2f}" if d else "—"


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


# ── Credentials helpers (.env R/W) ────────────────────────────────────────────

def _env_path() -> str:
    """Абсолютный путь к .env рядом с корнем проекта."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _read_env() -> dict:
    """Читает .env как словарь key→value."""
    result: dict = {}
    path = _env_path()
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(updates: dict) -> None:
    """Обновляет или добавляет ключи в .env, остальные строки сохраняет."""
    path = _env_path()
    lines: list = []
    updated_keys: set = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    new_lines: list = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                updated_keys.add(k)
                continue
        new_lines.append(line)
    # Добавляем новые ключи которых не было
    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "****" + token[-4:]


def _fmt_duration(open_time_str: str, close_time_str: str) -> str:
    if not open_time_str or not close_time_str:
        return "—"
    try:
        from datetime import datetime as _dt
        t0 = _dt.strptime(open_time_str, "%Y-%m-%d %H:%M:%S")
        t1 = _dt.strptime(close_time_str, "%Y-%m-%d %H:%M:%S")
        secs = int(abs((t1 - t0).total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "—"


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
    bot_enabled = is_truthy(s.get("bot_enabled", "1"))

    # В параллельном режиме показываем число активных стратегий
    active_profile_id = (s.get("active_profile_id", "") or "").strip()
    parallel_on = False
    parallel_count = 0
    if active_profile_id:
        parallel_on = get_profile_setting(int(active_profile_id), "parallel_trading_enabled", "0") == "1"
        if parallel_on:
            parallel_count = len(list_profile_parallel_strategies(int(active_profile_id)))
    if parallel_on and parallel_count >= 1:
        active_strategy_name = f"Параллельный режим · {parallel_count} стратегий"
    else:
        active_strategy_name = (s.get("active_strategy_name", "") or "").strip() or "—"

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
        # Проверяем режим сна (биржа закрыта + trade_only_session=1)
        _bot_state = get_runtime("status", "")
        if _bot_state and _bot_state.startswith("SLEEP_UNTIL_"):
            _wake_time = _bot_state.replace("SLEEP_UNTIL_", "")
            trading_status = f"🌙 Сон до {_wake_time}"
        else:
            trading_status = "Ведётся"

    api_rpm       = int(get_runtime("api_rpm", "0") or 0)
    api_rpm_limit = int(get_runtime("api_rpm_limit", "600") or 600)
    # Счётчик main.py не включает вызовы webapp.py (~30-50 req/min)
    # Предупреждение при 55% — реальная нагрузка выше отображаемой
    api_rpm_pct   = round(api_rpm / api_rpm_limit * 100) if api_rpm_limit else 0
    api_warn      = api_rpm_pct >= 55
    try:
        import json as _j
        api_rpm_breakdown = _j.loads(get_runtime("api_rpm_breakdown", "[]") or "[]")
    except Exception:
        api_rpm_breakdown = []

    return {
        "status": service_status,
        "trading_status": trading_status,
        "bot_enabled": "1" if bot_enabled else "0",
        "trades_today": st.get("trades_count", 0),
        "daily_pnl_ui": fmt_money(st.get("total_pnl", 0)),
        "total_commission_ui": fmt_money(st.get("total_commission", 0)),
        "unrealized_pnl_ui": fmt_money(sum(
            float(p.get("unrealized_pnl", 0) or 0)
            for p in get_open_positions(source="PORTFOLIO")
        )),
        "cash_rub_ui": fmt_money(portfolio.get("cash", 0)),
        "positions_value_rub_ui": fmt_money(portfolio.get("positions_value", 0)),
        "blocked_rub_ui": fmt_money(portfolio.get("blocked", 0)),
        "total_assets_rub_ui": fmt_money(portfolio.get("total_assets", 0)),
        "positions_count": portfolio.get("positions_count", 0),
        "money_by_currency": portfolio.get("money_by_currency", []),
        "active_profile_name": active_profile_name,
        "active_strategy_name": active_strategy_name,
        "last_error": s.get("last_error", "") or "—",
        "api_rpm": api_rpm,
        "api_rpm_limit": api_rpm_limit,
        "api_rpm_pct": api_rpm_pct,
        "api_warn": api_warn,
        "api_rpm_breakdown": api_rpm_breakdown,
    }


@app.on_event("startup")
def startup_event():
    init_db()
    _purge_bad_positions()
    threading.Thread(target=_portfolio_stream_worker, daemon=True, name="portfolio-stream").start()
    logger.info("PortfolioStream worker started")


def _purge_bad_positions():
    """
    Remove BOT positions whose entry_price is a per-lot value (sandbox bug).
    Detected when entry_price is ≥ 5× the instrument's last known market price.
    """
    from app.db import get_open_positions, clear_open_positions, get_instrument_market_state_map
    mmap = get_instrument_market_state_map()
    positions = get_open_positions(source="BOT")
    bad = []
    for p in positions:
        figi = p.get("figi", "")
        entry = float(p.get("entry_price", 0) or 0)
        last = float(mmap.get(figi, {}).get("last_price", 0) or 0)
        if last > 0 and entry > last * 5:
            bad.append(figi)
            logger.warning(
                "purge_bad_positions: %s entry=%.2f last=%.2f (per-lot price detected, removing)",
                p.get("ticker", figi), entry, last,
            )
    if bad:
        from app.db import db_cursor
        with db_cursor() as cur:
            for figi in bad:
                cur.execute(
                    "UPDATE positions SET status='CLOSED' WHERE figi=? AND status='OPEN' AND source='BOT'",
                    (figi,)
                )
        log_event("SERVICE_CONTROL", f"Удалено {len(bad)} позиций с некорректными ценами входа (per-lot bug)")


@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard_page():
    v = BOT_VERSION
    return HTMLResponse(f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Панель управления торговым ботом v{v}</title>
  <link rel="stylesheet" href="/static/dashboard.css?v={v}">
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div>
        <h1>Панель управления торговым ботом v{v}</h1>
        <p class="sub">Профили содержат общие настройки и выбор стратегии. Стратегия содержит параметры риска и инструменты.</p>
      </div>
      <div id="routeDebugBadge" class="route-badge">Вкладка: главное</div>
    </header>

    <nav class="tabs">
      <a href="#/главное" class="tab-link active" data-tab-link="главное">Главное</a>
      <a href="#/портфель" class="tab-link" data-tab-link="портфель">Портфель</a>
      <a href="#/настройки" class="tab-link" data-tab-link="настройки">Настройки</a>
      <a href="#/история" class="tab-link" data-tab-link="история">История</a>
      <a href="#/бэктест" class="tab-link" data-tab-link="бэктест">Бэктест</a>
      <a href="#/аналитик" class="tab-link" data-tab-link="аналитик">Аналитик</a>
      <a href="#/обучение" class="tab-link" data-tab-link="обучение">🧠 Обучение</a>
      <a href="#/справка" class="tab-link" data-tab-link="справка">📖 Справка</a>
    </nav>

    <div id="mainSummaryRow" style="display:none;margin-bottom:18px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;align-items:start;margin-bottom:6px">
      <section id="summaryCards" style="margin-bottom:0"></section>
      <div id="rbkTvColumn" style="background:#0a1628;border:1px solid rgba(76,141,255,.12);border-radius:10px;overflow:hidden;display:flex;flex-direction:column">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px 6px;border-bottom:1px solid rgba(76,141,255,.1);flex-shrink:0">
          <span style="font-size:9px;font-weight:700;color:#7ab0e8;text-transform:uppercase;letter-spacing:.08em">📺 РБК ТВ</span>
          <button onclick="(function(){{var w=document.getElementById('rbkTvWrap'),b=document.getElementById('rbkTvBtn');var h=w.style.display==='none';w.style.display=h?'block':'none';b.textContent=h?'Свернуть':'Развернуть';}})()" id="rbkTvBtn" style="font-size:10px;padding:2px 8px;background:rgba(76,141,255,.1);border:1px solid rgba(76,141,255,.25);color:#7ab0e8;border-radius:4px;cursor:pointer">Свернуть</button>
        </div>
        <div id="rbkTvWrap" style="position:relative;width:100%;padding-bottom:56.25%">
          <iframe src="https://smotret.tv/rbk"
            style="position:absolute;top:0;left:0;width:100%;height:100%;border:none;display:block"
            allowfullscreen allow="autoplay; encrypted-media; fullscreen"
            loading="lazy"></iframe>
        </div>
      </div>
      </div>
      <div id="newsWidgetInner" style="background:#0a1628;border:1px solid rgba(76,141,255,.12);border-radius:10px;overflow:hidden"></div>
    </div>

    <section id="view-main" data-view="главное"></section>
    <section id="view-portfolio" data-view="портфель" class="hidden"></section>
    <section id="view-settings" data-view="настройки" class="hidden"></section>
    <section id="view-history" data-view="история" class="hidden"></section>
    <section id="view-backtest" data-view="бэктест" class="hidden"></section>
    <section id="view-analyst" data-view="аналитик" class="hidden"></section>
    <section id="view-обучение" data-view="обучение" class="hidden"></section>
    <section id="view-справка" data-view="справка" class="hidden"></section>
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

  <script src="/static/plotly-2.35.2.min.js" defer></script>
  <script src="/static/dashboard.js?v={v}" defer></script>
</body>
</html>""")


# ── News RSS proxy ─────────────────────────────────────────────────────────────

_news_cache: Dict[str, Any] = {"data": [], "ts": 0.0}
_NEWS_TTL = 300  # 5 min

@app.get("/api/news")
def api_news():
    now = time.time()
    if now - _news_cache["ts"] < _NEWS_TTL and _news_cache["data"]:
        return JSONResponse(_news_cache["data"])
    try:
        req = urllib.request.Request(
            "https://www.kommersant.ru/RSS/section-economics.xml",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            root = ET.fromstring(resp.read())
        items = root.findall(".//item")
        news = []
        for item in items[:50]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if title:
                news.append({"title": title, "link": link, "date": pub[:16]})
        _news_cache["data"] = news
        _news_cache["ts"] = now
    except Exception as e:
        logger.warning(f"News RSS fetch failed: {e}")
    return JSONResponse(_news_cache["data"])


# ── Ticker news (Google News RSS, кэш 5 мин) ─────────────────────────────────

_ticker_news_cache: dict = {}  # figi → {"ts": float, "data": list}
_TICKER_NEWS_TTL = 300         # 5 минут

@app.get("/api/news/ticker")
def api_news_ticker(figi: str = "", hours: int = 4):
    """Новости по тикеру из нескольких российских RSS-источников."""
    from datetime import datetime, timezone, timedelta
    import email.utils as _eutils

    now = time.time()
    cache = _ticker_news_cache.get(figi)
    if cache and (now - cache["ts"]) < _TICKER_NEWS_TTL:
        return JSONResponse(cache["data"])

    # Тикер и название компании
    mmap = get_instrument_market_state_map()
    ticker = mmap.get(figi, {}).get("ticker", "") or figi[:8]
    company_name = ""
    try:
        from app.db import db_cursor as _dbc3
        with _dbc3() as _c3:
            _c3.execute("SELECT name FROM strategy_instruments WHERE figi=? AND name!='' LIMIT 1", (figi,))
            _row = _c3.fetchone()
            if _row and _row[0]:
                company_name = _row[0]
    except Exception:
        pass

    # Ключевые слова для фильтрации (тикер + первые слова названия)
    keywords = [ticker.upper()]
    if company_name:
        for w in company_name.split()[:2]:
            if len(w) > 3:
                keywords.append(w.lower())

    # Российские финансовые RSS-источники (проверены на сервере)
    RSS_SOURCES = [
        ("Интерфакс",   "https://www.interfax.ru/rss.asp"),
        ("РБК",         "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
        ("Finam",       "https://www.finam.ru/analysis/conews/rsspoint/"),
        ("Коммерсантъ", "https://www.kommersant.ru/RSS/section-economics.xml"),
        ("Ведомости",   "https://www.vedomosti.ru/rss/articles"),
    ]

    # Начало текущей торговой сессии MOEX (10:00 МСК = 07:00 UTC)
    from datetime import date as _date
    _msk = timezone(timedelta(hours=3))
    _now_msk = datetime.now(_msk)
    session_start = _now_msk.replace(hour=10, minute=0, second=0, microsecond=0)
    if _now_msk.hour < 10:
        session_start = session_start.replace(day=_now_msk.day - 1)

    # Граница по времени: берём max охват — либо N часов, либо начало сессии
    cutoff_hours = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_session = session_start.astimezone(timezone.utc)
    # Используем более раннюю границу чтобы показать больше новостей
    cutoff = min(cutoff_hours, cutoff_session)

    def _parse_date(pub_raw):
        if not pub_raw:
            return None
        try:
            parsed = _eutils.parsedate(pub_raw)
            if parsed:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
        return None

    all_news = []
    for source_name, rss_url in RSS_SOURCES:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                root = ET.fromstring(resp.read())
            for item in root.findall(".//item"):
                title   = (item.findtext("title")   or "").strip()
                link    = (item.findtext("link")    or "").strip()
                pub_raw = (item.findtext("pubDate") or "").strip()
                desc    = (item.findtext("description") or "").strip()
                if not title:
                    continue
                text_check = (title + " " + desc).lower()
                if not any(kw.lower() in text_check for kw in keywords):
                    continue
                pub_dt = _parse_date(pub_raw)
                if pub_dt and pub_dt < cutoff:
                    continue
                date_ui = pub_dt.astimezone(_msk).strftime("%d.%m %H:%M") if pub_dt else pub_raw[:16]
                all_news.append({
                    "title": title,
                    "link":  link,
                    "date":  date_ui,
                    "source": source_name,
                    "_ts": pub_dt.timestamp() if pub_dt else 0,
                })
        except Exception as e:
            logger.debug(f"News {source_name}: {e}")

    # Сортируем по времени, убираем дубли по заголовку
    seen_titles: set = set()
    news: list = []
    for n in sorted(all_news, key=lambda x: x["_ts"], reverse=True):
        t = n["title"][:60]
        if t not in seen_titles:
            seen_titles.add(t)
            news.append({"title": n["title"], "link": n["link"],
                         "date": n["date"], "source": n["source"]})
        if len(news) >= 20:
            break

    _ticker_news_cache[figi] = {"ts": now, "data": news}
    return JSONResponse(news)


# ── market session + volatility ───────────────────────────────────────────────

@app.get("/api/market/session")
def api_market_session():
    """Тип сессии MOEX, таймер и индикатор волатильности."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _msk = _tz(_td(hours=3))
    now = _dt.now(_msk)
    h, m = now.hour, now.minute
    total_min = h * 60 + m

    # Сессии MOEX (пн–пт)
    MAIN_START  = 10 * 60        # 10:00
    MAIN_END    = 18 * 60 + 50   # 18:50
    EVE_START   = 19 * 60 + 5    # 19:05
    EVE_END     = 23 * 60 + 50   # 23:50
    is_weekday  = now.weekday() < 5

    def _secs_to(target_h, target_m):
        t = _dt(now.year, now.month, now.day, target_h, target_m, tzinfo=_msk)
        diff = (t - now).total_seconds()
        if diff < 0:
            diff += 86400
        return int(diff)

    if not is_weekday:
        # Выходной → до понедельника 10:00
        days_ahead = (7 - now.weekday()) % 7 or 7
        next_open = now.replace(hour=10, minute=0, second=0, microsecond=0) + _td(days=days_ahead)
        secs = int((next_open - now).total_seconds())
        session = {"type": "выходной", "label": "Выходной", "until_label": "До открытия",
                   "seconds_left": secs, "start": "10:00 пн", "end": "18:50"}
    elif MAIN_START <= total_min < MAIN_END:
        session = {"type": "основная", "label": "Основная сессия", "until_label": "До закрытия",
                   "seconds_left": _secs_to(18, 50), "start": "10:00", "end": "18:50"}
    elif MAIN_END <= total_min < EVE_START:
        session = {"type": "перерыв", "label": "Перерыв", "until_label": "До вечерней",
                   "seconds_left": _secs_to(19, 5), "start": "18:50", "end": "19:05"}
    elif EVE_START <= total_min < EVE_END:
        session = {"type": "вечерняя", "label": "Вечерняя сессия", "until_label": "До закрытия",
                   "seconds_left": _secs_to(23, 50), "start": "19:05", "end": "23:50"}
    elif total_min < MAIN_START:
        session = {"type": "закрыта", "label": "Биржа закрыта", "until_label": "До открытия",
                   "seconds_left": _secs_to(10, 0), "start": "—", "end": "10:00"}
    else:
        session = {"type": "закрыта", "label": "Биржа закрыта", "until_label": "До открытия",
                   "seconds_left": _secs_to(10, 0) or 86400, "start": "—", "end": "10:00"}

    # Волатильность из последних ml_features (atr_pct, regime)
    volatility = {"label": "нет данных", "color": "#9fb3d8", "atr": 0.0}
    try:
        from app.db import db_cursor as _dbc_v
        with _dbc_v() as _cv:
            _cv.execute("""
                SELECT AVG(atr_pct), AVG(regime), AVG(sma_gap_pct)
                FROM ml_features ORDER BY id DESC LIMIT 10
            """)
            row = _cv.fetchone()
        if row and row[0]:
            atr = float(row[0] or 0)
            regime = float(row[1] or 0)
            gap = float(row[2] or 0)
            volatility["atr"] = round(atr, 3)
            if atr > 0.5 or regime >= 3:
                volatility["label"] = "⚡ Волатильно"
                volatility["color"] = "#f0c04a"
            elif 1.2 <= regime < 2:
                volatility["label"] = "📈 Тренд ↑"
                volatility["color"] = "#2fa36b"
            elif 1.8 <= regime < 3:
                volatility["label"] = "📉 Тренд ↓"
                volatility["color"] = "#ff7b7b"
            elif atr < 0.2 and abs(gap) < 0.05:
                volatility["label"] = "〰️ Боковик"
                volatility["color"] = "#9fb3d8"
            else:
                volatility["label"] = "〰️ Боковик"
                volatility["color"] = "#9fb3d8"
    except Exception:
        pass

    return JSONResponse({"session": session, "volatility": volatility, "server_time_msk": now.strftime("%H:%M:%S")})


# ── dashboard data endpoints ───────────────────────────────────────────────────

@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    return JSONResponse(summary_payload())


@app.get("/api/dashboard/main")
def api_dashboard_main():
    s = get_all_settings()
    active_strategy_id = (s.get("active_strategy_id", "") or "").strip()
    market_map = get_instrument_market_state_map()
    figi_ticker_map = {figi: info.get("ticker", "") for figi, info in market_map.items()}

    # Если параллельный режим — инструменты из всех стратегий профиля
    active_profile_id = (s.get("active_profile_id", "") or "").strip()
    parallel_on = False
    if active_profile_id:
        parallel_on = get_profile_setting(int(active_profile_id), "parallel_trading_enabled", "0") == "1"

    if parallel_on and active_profile_id:
        par_strats = list_profile_parallel_strategies(int(active_profile_id))
        instruments = []
        for st in par_strats:
            for instr in list_strategy_instruments(st["strategy_id"]):
                instr["_strategy_name"] = st["name"]
                instruments.append(instr)
    elif active_strategy_id:
        instruments = list_strategy_instruments(int(active_strategy_id))
    else:
        instruments = []

    # ── Positions: broker API (same source as portfolio tab) ──────────────────
    # Priority: portfolio_stream cache → get_broker_positions() REST → BOT DB
    # Размер лота по figi из strategy_instruments (все стратегии)
    from app.db import db_cursor as _dbc
    _lot_map: dict = {}
    _sl_tp_map: dict = {}
    try:
        with _dbc() as _cur:
            _cur.execute("SELECT DISTINCT figi, lot FROM strategy_instruments WHERE figi IS NOT NULL AND lot > 0")
            for _r in _cur.fetchall():
                _lot_map[_r[0]] = int(_r[1])
            # SL/TP берём только из активных стратегий профиля (не из других профилей)
            _active_strat_ids = []
            if active_profile_id:
                _cur.execute(
                    "SELECT strategy_id FROM profile_parallel_strategies WHERE profile_id=?",
                    (int(active_profile_id),)
                )
                _active_strat_ids = [r[0] for r in _cur.fetchall()]
            if active_strategy_id:
                _active_strat_ids.append(int(active_strategy_id))
            if _active_strat_ids:
                _placeholders = ",".join("?" * len(_active_strat_ids))
                _cur.execute(
                    f"SELECT figi, stop_loss_pct, take_profit_pct FROM strategy_instruments "
                    f"WHERE figi IS NOT NULL AND strategy_id IN ({_placeholders})",
                    _active_strat_ids
                )
            else:
                _cur.execute("SELECT figi, stop_loss_pct, take_profit_pct FROM strategy_instruments WHERE figi IS NOT NULL")
            for _r in _cur.fetchall():
                _sl_tp_map[_r[0]] = (float(_r[1] or 0), float(_r[2] or 0))
    except Exception:
        pass

    def _fmt_pos(figi, direction, qty, avg, cur, pnl, opened_at="", qty_shares=None):
        avg_d = safe_decimal(avg)
        mkt = market_map.get(figi, {})
        mkt_price = safe_decimal(mkt.get("last_price", 0))
        cur_d = mkt_price if mkt_price > 0 else safe_decimal(cur)
        # qty от брокера — в лотах; умножаем на лот → штуки для PnL/стоимости
        lot_size = _lot_map.get(figi, 1)
        calc_qty = qty * lot_size   # штуки = лоты × лот_size
        if direction == "BUY":
            pnl_d = (cur_d - avg_d) * calc_qty
        else:
            pnl_d = (avg_d - cur_d) * calc_qty
        pct = ((cur_d - avg_d) / avg_d * 100) if avg_d > 0 else Decimal("0")
        sl_pct, tp_pct = _sl_tp_map.get(figi, (0.0, 0.0))
        if avg_d > 0 and sl_pct > 0:
            sl_price = avg_d * Decimal(str(1 - sl_pct)) if direction == "BUY" else avg_d * Decimal(str(1 + sl_pct))
        else:
            sl_price = Decimal("0")
        if avg_d > 0 and tp_pct > 0:
            tp_price = avg_d * Decimal(str(1 + tp_pct)) if direction == "BUY" else avg_d * Decimal(str(1 - tp_pct))
        else:
            tp_price = Decimal("0")
        return {
            "ticker": market_map.get(figi, {}).get("ticker", "") or figi[:8],
            "figi": figi,
            "direction": direction,
            "qty": qty,
            "entry_price_ui": fmt_price(avg_d),
            "current_price_ui": fmt_price(cur_d),
            "unrealized_pnl_ui": fmt_money(pnl_d),
            "pct_change": f"{pct:+.2f}%",
            "pnl_positive": pnl_d >= 0,
            "opened_at": opened_at,
            "avg_price_raw": float(avg_d),
            "qty_raw": float(calc_qty),
            "position_value_ui": fmt_money(cur_d * calc_qty),
            "sl_pct_ui": f"{sl_pct*100:.2f}%" if sl_pct > 0 else "—",
            "tp_pct_ui": f"{tp_pct*100:.2f}%" if tp_pct > 0 else "—",
            "sl_price_ui": fmt_price(sl_price) if sl_price > 0 else "—",
            "tp_price_ui": fmt_price(tp_price) if tp_price > 0 else "—",
        }

    with _portfolio_cache_lock:
        cached_pos = _portfolio_cache.get("positions")

    positions = []
    if cached_pos is not None:
        for pos in cached_pos:
            if str(pos.get("figi", "")).startswith("RUB") or str(pos.get("ticker", "")).startswith("RUB"):
                continue
            positions.append(_fmt_pos(
                pos["figi"], pos["direction"], pos["qty"],
                pos["avg_price"], pos["current_price"], pos["expected_yield"],
            ))
    else:
        try:
            for pos in get_broker_positions():
                positions.append(_fmt_pos(
                    pos["figi"], pos["direction"], pos["qty"],
                    pos["avg_price"], pos["current_price"], pos["expected_yield"],
                ))
        except Exception:
            for p in get_open_positions(source="BOT"):
                positions.append(_fmt_pos(
                    p.get("figi", ""), p.get("direction", "BUY"), p.get("qty", 0),
                    p.get("entry_price", 0), p.get("current_price", 0), p.get("unrealized_pnl", 0),
                    p.get("opened_at", ""),
                ))

    # ── Trades: T-Bank operations API → local DB fallback ─────────────────────
    api_trades: list = []
    try:
        ops_data = get_operations_today()
        for op in ops_data.get("operations", []):
            if op.get("is_fee"):
                continue
            figi = op.get("figi", "")
            direction = op.get("direction", "")
            price_ui = op.get("price_ui", "")
            api_trades.append({
                "time": op.get("date", ""), "ticker": figi_ticker_map.get(figi, figi[:8]),
                "figi": figi, "direction": direction,
                "entry_ui": price_ui if direction == "BUY" else "",
                "exit_ui": price_ui if direction == "SELL" else "",
                "qty": op.get("quantity", 0),
                "pnl_ui": op.get("payment_ui", ""), "reason": "API",
            })
    except Exception:
        pass

    def _trade_pnl_pct(t):
        entry = float(t.get("entry", 0) or 0)
        qty   = int(t.get("qty", 0) or 0)
        pnl   = float(t.get("pnl", 0) or 0)
        cost  = entry * qty
        return round(pnl / cost * 100, 2) if cost else 0.0

    db_trades = [{
        **dict(t),
        "entry_ui":   fmt_price(t.get("entry", 0)),
        "exit_ui":    fmt_price(t.get("exit", 0)),
        "pnl_ui":     fmt_money(t.get("pnl", 0)),
        "pnl_pct":    _trade_pnl_pct(t),
        "pnl_positive": float(t.get("pnl", 0) or 0) >= 0,
        "open_time":  t.get("open_time", "") or "",
        "duration_ui": _fmt_duration(t.get("open_time", "") or "", t.get("time", "") or ""),
    } for t in get_trades(limit=20)]

    return JSONResponse({
        "instruments": [strategy_instrument_row(i, market_map) for i in instruments],
        "positions": positions,
        "trades": db_trades,
        "api_trades": api_trades,
        "parallel_on": parallel_on,
    })


@app.get("/api/dashboard/multi-candles")
def api_multi_candles(figis: str = "", interval: str = "1min", hours: int = 4):
    """OHLCV свечи для нескольких инструментов — мини-графики на главной."""
    figi_list = [f.strip() for f in figis.split(",") if f.strip()][:30]
    market_map = get_instrument_market_state_map()
    result: dict = {}
    for figi in figi_list:
        try:
            candles = get_candles(figi, interval_name=interval, hours=hours)
            result[figi] = {
                "ticker":  market_map.get(figi, {}).get("ticker", figi[:8]),
                "candles": candles,
            }
        except Exception:
            result[figi] = {"ticker": figi[:8], "candles": []}
    return JSONResponse(result)


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
    market_map = get_instrument_market_state_map()
    broker_error = None
    portfolio_positions = []

    # 1. Приоритет: portfolio_stream кэш (реал-тайм, нет доп. запроса к API)
    with _portfolio_cache_lock:
        cached = _portfolio_cache.get("positions")
        cache_ts = _portfolio_cache.get("updated_at", "")

    if cached is not None:
        for pos in cached:
            figi = pos["figi"]
            ticker = market_map.get(figi, {}).get("ticker", "") or figi[:12]
            portfolio_positions.append({
                "ticker": ticker, "figi": figi,
                "instrument_type": pos["instrument_type"],
                "direction": pos["direction"],
                "qty": pos["qty"],
                "quantity_ui": str(pos["qty"]),
                "average_position_price_ui": fmt_price(pos["avg_price"]),
                "current_price_ui": fmt_price(pos["current_price"]),
                "expected_yield_ui": fmt_money(pos["expected_yield"]),
            })
    else:
        # 2. Fallback: одиночный REST-вызов get_portfolio
        try:
            for pos in get_broker_positions():
                figi = pos["figi"]
                ticker = market_map.get(figi, {}).get("ticker", "") or figi[:12]
                portfolio_positions.append({
                    "ticker": ticker, "figi": figi,
                    "instrument_type": pos["instrument_type"],
                    "direction": pos["direction"],
                    "qty": pos["qty"],
                    "quantity_ui": str(pos["qty"]),
                    "average_position_price_ui": fmt_price(pos["avg_price"]),
                    "current_price_ui": fmt_price(pos["current_price"]),
                    "expected_yield_ui": fmt_money(pos["expected_yield"]),
                })
        except Exception as e:
            broker_error = str(e)
            logger.exception("get_broker_positions failed, falling back to local DB")
            for p in get_open_positions():
                portfolio_positions.append({
                    "ticker": p.get("ticker", ""), "figi": p.get("figi", ""),
                    "instrument_type": "share",
                    "direction": p.get("direction", ""),
                    "qty": p.get("qty", 0),
                    "quantity_ui": str(p.get("qty", 0)),
                    "average_position_price_ui": fmt_price(p.get("entry_price", 0)),
                    "current_price_ui": fmt_price(p.get("current_price", 0)),
                    "expected_yield_ui": fmt_money(p.get("unrealized_pnl", 0)),
                })

    # Детализация счёта из GetPositions (деньги + бумаги)
    account_detail: dict = {"money": [], "blocked": [], "securities": []}
    try:
        account_detail = get_positions_detailed()
    except Exception:
        logger.exception("get_positions_detailed failed")

    bot_positions = get_open_positions(source="BOT")
    return JSONResponse({
        "portfolio_positions": portfolio_positions,
        "broker_error": broker_error,
        "stream_updated_at": cache_ts,
        "account_money": account_detail.get("money", []),
        "account_blocked": account_detail.get("blocked", []),
        "account_securities": account_detail.get("securities", []),
        "bot_positions": [{
            "ticker": p.get("ticker", ""), "figi": p.get("figi", ""),
            "direction": p.get("direction", ""),
            "qty": p.get("qty", 0),
            "entry_price_ui": fmt_price(p.get("entry_price", 0)),
            "entry_price_raw": str(p.get("entry_price", 0)),
            "current_price_ui": fmt_price(p.get("current_price", 0)),
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

    active_profile_id_str = (settings_map.get("active_profile_id", "") or "").strip()
    active_profile_id = int(active_profile_id_str) if active_profile_id_str else None
    parallel_on = False
    parallel_count = 0
    if active_profile_id:
        parallel_on = get_profile_setting(active_profile_id, "parallel_trading_enabled", "0") == "1"
        if parallel_on:
            parallel_count = len(list_profile_parallel_strategies(active_profile_id))
    if parallel_on and parallel_count >= 1:
        strategy_display = f"Параллельный режим · {parallel_count} стратегий"
    else:
        strategy_display = (settings_map.get("active_strategy_name", "") or "").strip() or "—"

    return {
        "botenabled": settings_map.get("bot_enabled", "1"),
        "tinvestusesandbox": settings_map.get("tinvestusesandbox", "true"),
        "activeprofilename": settings_map.get("active_profile_name", ""),
        "activestrategyname": strategy_display,
        "lasterror": settings_map.get("last_error", ""),
        "status": runtime_map.get("status", settings_map.get("status", "INIT")),
        "runtime": runtime_map,
    }


@app.post("/api/sandbox/pay-in")
def api_sandbox_pay_in(amount: int = Form(100_000)):
    s = get_all_settings()
    if str(s.get("tinvestusesandbox", "true")).lower() != "true":
        raise HTTPException(status_code=400, detail="Только для Sandbox режима")
    try:
        new_balance = sandbox_pay_in(amount)
        log_event("SERVICE_CONTROL", f"Sandbox пополнение {amount} ₽, новый баланс: {new_balance:.0f} ₽")
        return JSONResponse({"ok": True, "balance_ui": fmt_money(new_balance)})
    except Exception as e:
        logger.exception("sandbox pay-in error")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sandbox/reset")
def api_sandbox_reset(amount: int = Form(100_000)):
    """Сброс sandbox-счёта: закрывает текущий, создаёт новый, пополняет на amount.
    Возвращает новый account_id — нужно обновить TINVEST_ACCOUNT_ID в .env.
    """
    s = get_all_settings()
    if str(s.get("tinvestusesandbox", "true")).lower() != "true":
        raise HTTPException(status_code=400, detail="Только для Sandbox режима")
    try:
        result = sandbox_reset_account(amount)
        new_id = result["account_id"]
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        try:
            import re as _re
            with open(env_path, "r") as _f:
                env_text = _f.read()
            env_text = _re.sub(
                r"TINVEST_ACCOUNT_ID=.*",
                f"TINVEST_ACCOUNT_ID={new_id}",
                env_text,
            )
            with open(env_path, "w") as _f:
                _f.write(env_text)
        except Exception as _ee:
            logger.warning("Не удалось обновить .env: %s", _ee)
        # Обновляем settings в памяти текущего процесса
        from app import config as _cfg
        _cfg.settings.TINVEST_ACCOUNT_ID = new_id
        log_event("SERVICE_CONTROL",
                  f"Sandbox сброс: новый счёт {new_id}, баланс {result['balance']:.0f} ₽")
        return JSONResponse({
            "ok": True,
            "account_id": new_id,
            "balance": result["balance"],
            "note": "Бот будет перезапущен для применения нового account_id",
        })
    except Exception as e:
        logger.exception("sandbox reset error")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/dashboard/balance-check")
def api_balance_check():
    """Проверка достаточности баланса для каждого инструмента активной стратегии."""
    s = get_all_settings()
    active_strategy_id = (s.get("active_strategy_id", "") or "").strip()
    is_sandbox = str(s.get("tinvestusesandbox", "true")).lower() == "true"
    commission_pct = safe_decimal(s.get("estimated_commission_pct", "0.0004"))
    market_map = get_instrument_market_state_map()

    instruments = list_strategy_instruments(int(active_strategy_id)) if active_strategy_id else []
    enabled = [x for x in instruments if str(x.get("enabled", 0)) in ("1", "true")]

    # Сумма ИТОГО портфеля (кэш + позиции) — база для расчёта авто-лотов
    cash = Decimal("0")
    try:
        portfolio = get_portfolio_snapshot()
        cash = safe_decimal(portfolio.get("total_assets", 0) or portfolio.get("cash", 0))
    except Exception:
        pass

    checks = []
    for inst in enabled:
        figi = inst["figi"]
        ticker = inst["ticker"]
        lots_override = int(inst.get("lots_override", 1))
        lot_size = int(inst.get("lot", 1))
        auto_lots = int(inst.get("auto_lots", 0) or 0)
        last_price = safe_decimal(market_map.get(figi, {}).get("last_price", 0))
        sl_pct = safe_decimal(inst.get("stop_loss_pct", "0.0025")) * 100
        tp_pct = safe_decimal(inst.get("take_profit_pct", "0.005")) * 100

        if auto_lots and last_price > 0:
            cost_per_lot = Decimal(str(lot_size)) * last_price * (1 + commission_pct)
            lots_override = max(1, int(cash / cost_per_lot)) if cost_per_lot > 0 else 1

        if last_price > 0:
            position_cost = Decimal(str(lots_override)) * Decimal(str(lot_size)) * last_price
            required = position_cost * (1 + commission_pct)
        else:
            position_cost = Decimal("0")
            required = Decimal("0")

        can_trade = (cash >= required) and required > 0
        checks.append({
            "ticker": ticker,
            "lots": lots_override,
            "lot_size": lot_size,
            "auto_lots": auto_lots,
            "price_ui": fmt_price(last_price),
            "position_cost_ui": fmt_money(position_cost),
            "required_ui": fmt_money(required),
            "sl_pct": str(sl_pct),
            "tp_pct": str(tp_pct),
            "can_trade": can_trade,
            "has_price": last_price > 0,
        })

    return JSONResponse({
        "cash_ui": fmt_money(cash),
        "is_sandbox": is_sandbox,
        "checks": checks,
        "any_blocked": any(not c["can_trade"] and c["has_price"] for c in checks),
    })


@app.get("/api/dashboard/bot-explain")
def api_dashboard_bot_explain():
    settings_map = get_all_settings()
    active_strategy_id = (settings_map.get("active_strategy_id", "") or "").strip()
    active_profile_id_str = (settings_map.get("active_profile_id", "") or "").strip()
    active_profile_id = int(active_profile_id_str) if active_profile_id_str else None

    # Parallel mode check
    parallel_on = False
    parallel_instrs_count = 0
    if active_profile_id:
        parallel_on = get_profile_setting(active_profile_id, "parallel_trading_enabled", "0") == "1"
        if parallel_on:
            for ps in list_profile_parallel_strategies(active_profile_id):
                for instr in list_strategy_instruments(ps["strategy_id"]):
                    if str(instr.get("enabled", 0)) in ("1", "true"):
                        parallel_instrs_count += 1

    if parallel_on:
        enabled_instruments_count = parallel_instrs_count
    elif active_strategy_id:
        instruments = list_strategy_instruments(int(active_strategy_id))
        enabled_instruments_count = len([x for x in instruments if str(x.get("enabled", 0)) in ("1", "true")])
    else:
        enabled_instruments_count = 0

    open_positions = get_open_positions(source="BOT")

    # trade_only_session теперь на уровне профиля
    trade_only_session = "0"
    if active_profile_id:
        trade_only_session = get_profile_setting(active_profile_id, "trade_only_session", "0")

    reasons = []
    if str(settings_map.get("bot_enabled", "1")) != "1":
        reasons.append("Бот выключен в настройках")
    if not active_profile_id:
        reasons.append("Не выбран профиль")
    elif not parallel_on and not active_strategy_id:
        reasons.append("Не выбрана стратегия")
    elif enabled_instruments_count == 0:
        reasons.append("Нет активных инструментов" + (" в параллельных стратегиях" if parallel_on else " в стратегии"))
    if trade_only_session == "1":
        # Показываем только когда сессия закрыта, а не всегда
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _msk2 = _tz2(_td2(hours=3))
        _now2 = _dt2.now(_msk2)
        _t2 = _now2.hour * 60 + _now2.minute
        _session_open = _now2.weekday() < 5 and ((600 <= _t2 < 1130) or (1145 <= _t2 < 1430))
        if not _session_open:
            reasons.append("Спящий режим — биржа закрыта (торговля только в сессию)")

    max_pos = int(str(settings_map.get("max_open_positions", "2")))
    cur_pos = len(open_positions)
    if cur_pos >= max_pos:
        reasons.append(f"Достигнут лимит открытых позиций ({cur_pos}/{max_pos})")

    max_trades = int(str(settings_map.get("max_trades_per_day", "15")))
    trades_today_cnt = int(str(settings_map.get("trades_today", "0")))
    if trades_today_cnt >= max_trades:
        reasons.append(f"Достигнут лимит сделок за день ({trades_today_cnt}/{max_trades})")

    # Параллельный координатор — показываем если заблокирован реальной позицией
    if parallel_on:
        coord_raw = get_runtime("parallel_coord") or ""
        try:
            coord = json.loads(coord_raw)
            owner_figi = coord.get("owner_figi")
            owner_ticker = coord.get("owner_ticker", "?")
            if owner_figi:
                pos_map = {p["figi"]: p for p in open_positions}
                if owner_figi in pos_map:
                    reasons.append(f"Координатор занят позицией {owner_ticker} — открытие новых заблокировано")
        except Exception:
            pass

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
                "parallel_trading_enabled": ps("parallel_trading_enabled", "0"),
                "trade_only_session": ps("trade_only_session", "1"),
                "max_open_positions": ps("max_open_positions", "3"),
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
                "trailing_stop_enabled": ss("trailing_stop_enabled", "0"),
                "use_signal_service": ss("use_signal_service", "0"),
                "use_api_confirm": ss("use_api_confirm", "0"),
                "min_signal_score": ss("min_signal_score", "0"),
            },
            "parallel_enabled": bool(int(strategy.get("parallel_enabled") or 0)),
        },
        "profiles": list_profiles(),
        "strategies": list_strategies(),
        "instruments": [strategy_instrument_row(i, market_map) for i in strat_instruments],
        "parallel_strategies": list_profile_parallel_strategies(profile.get("id") or 0) if profile.get("id") else [],
    })


@app.get("/api/history/stats")
def api_history_stats(days: int = 0, date_from: str = "", date_to: str = ""):
    """Агрегированная статистика по сделкам: кривая капитала, тикеры, причины."""
    if date_from and date_to:
        stats = get_history_stats(date_from=date_from, date_to=date_to)
    elif date_from:
        stats = get_history_stats(date_from=date_from)
    elif days > 0:
        stats = get_history_stats(days=days)
    else:
        stats = get_history_stats()
    s = stats["summary"]
    s["total_pnl_ui"]       = fmt_money(s.get("total_pnl", 0))
    s["avg_pnl_ui"]         = fmt_money(s.get("avg_pnl", 0))
    s["best_trade_ui"]      = fmt_money(s.get("best_trade", 0))
    s["worst_trade_ui"]     = fmt_money(s.get("worst_trade", 0))
    s["total_commission_ui"] = fmt_money(s.get("total_commission", 0))
    for item in stats["by_ticker"]:
        item["pnl_ui"] = fmt_money(item["pnl"])
    return JSONResponse(stats)


@app.get("/api/dashboard/history")
def api_dashboard_history(days: int = 0, date_from: str = "", date_to: str = ""):
    from datetime import datetime as _dt, timedelta as _td
    if not date_from and days > 0:
        date_from = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")

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
            "open_time": t.get("open_time", "") or "",
            "duration_ui": _fmt_duration(t.get("open_time", "") or "", t.get("time", "") or ""),
        } for t in get_trades(limit=500, date_from=date_from or None, date_to=date_to or None)],
        "system_logs": [norm(x) for x in get_system_logs(limit=200, date_from=date_from or None)],
        "error_logs":  [norm(x) for x in get_error_logs(limit=200,  date_from=date_from or None)],
        "common_logs": [norm(x) for x in get_logs(limit=500,        date_from=date_from or None)],
    })


@app.post("/api/history/clear")
async def api_history_clear(request: Request):
    body = await request.json()
    trades = bool(body.get("trades", True))
    logs   = bool(body.get("logs", False))
    result = clear_history(clear_trades=trades, clear_logs=logs)
    log_event("SERVICE_CONTROL", f"История очищена: {result}")
    return JSONResponse({"ok": True, **result})


@app.get("/api/broker-operations")
def api_broker_operations(cursor: str = "", days: int = 30, limit: int = 50):
    """Paginated broker operations history from T-Bank GetOperationsByCursor."""
    from datetime import timezone, timedelta
    market_map = get_instrument_market_state_map()
    try:
        now = __import__("datetime").datetime.now(timezone.utc)
        result = get_operations_by_cursor(
            from_dt=now - timedelta(days=days),
            to_dt=now,
            limit=limit,
            cursor=cursor,
        )
        for item in result["items"]:
            figi = item.get("figi", "")
            item["ticker"] = market_map.get(figi, {}).get("ticker", "") or figi[:8]
        return JSONResponse(result)
    except Exception as e:
        logger.exception("broker-operations error")
        raise HTTPException(status_code=500, detail=str(e))


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


# ── backtest ──────────────────────────────────────────────────────────────────

import threading as _bt_threading

_bt_lock   = _bt_threading.Lock()
_bt_state  = {"status": "idle", "progress": 0, "total": 0, "current": "", "error": ""}
_bt_result = None


def _bt_run_worker(strategy_ids: list, interval: str, days: int):
    global _bt_result
    results = {}
    try:
        for i, sid in enumerate(strategy_ids):
            sid = int(sid)
            with _bt_lock:
                _bt_state["progress"] = i
                _bt_state["current"]  = f"Загрузка стратегии {sid}…"

            strat = get_strategy(sid)
            if not strat:
                results[str(sid)] = {"error": f"Стратегия {sid} не найдена"}
                continue

            cfg        = get_strategy_settings(sid)
            mode       = cfg.get("tradingmode", "trend")
            comm_pct   = float(cfg.get("estimated_commission_pct", "0.0004"))
            strat_name = strat.get("name", f"Стратегия {sid}")

            all_instr = list_strategy_instruments(sid)
            inst_rows = [x for x in all_instr if str(x.get("enabled", 1)) in ("1", "true")]
            if not inst_rows:
                results[str(sid)] = {"error": "Нет активных инструментов", "strategy_name": strat_name}
                continue

            instr      = inst_rows[0]
            figi       = instr["figi"]
            ticker     = instr.get("ticker", "?")
            sl_pct     = float(instr.get("stop_loss_pct")  or cfg.get("default_stop_loss_pct",  "0.0025"))
            tp_pct     = float(instr.get("take_profit_pct") or cfg.get("default_take_profit_pct", "0.005"))
            lots       = max(1, int(instr.get("lots_override") or 1))
            lot_size   = max(1, int(instr.get("lot") or 1))
            qty_shares = lots * lot_size

            with _bt_lock:
                _bt_state["current"] = f"{ticker}: загрузка свечей…"
            try:
                candles = get_candles_range(figi=figi, interval_name=interval, days=days)
            except Exception as e:
                results[str(sid)] = {"error": f"Ошибка свечей {ticker}: {e}", "strategy_name": strat_name}
                with _bt_lock:
                    _bt_state["progress"] = i + 1
                continue

            if len(candles) < 30:
                results[str(sid)] = {"error": f"Мало свечей {ticker}: {len(candles)}", "strategy_name": strat_name}
                with _bt_lock:
                    _bt_state["progress"] = i + 1
                continue

            with _bt_lock:
                _bt_state["current"] = f"{ticker}: прогон стратегии…"
            try:
                res = run_backtest(candles, mode=mode, stop_loss_pct=sl_pct,
                                   take_profit_pct=tp_pct, commission_pct=comm_pct, qty=qty_shares)
                d = result_to_dict(res, candles)
                d.update({
                    "strategy_name": strat_name, "ticker": ticker, "figi": figi,
                    "mode": mode,
                    "sl_pct_ui": f"{sl_pct * 100:.3f}%", "tp_pct_ui": f"{tp_pct * 100:.3f}%",
                    "lots": lots, "lot_size": lot_size, "qty_shares": qty_shares,
                    "qty_ui": f"{lots} лот × {lot_size} шт = {qty_shares} шт",
                    "candles_loaded": len(candles),
                })
                results[str(sid)] = d
            except Exception as e:
                results[str(sid)] = {"error": str(e), "strategy_name": strat_name}

            with _bt_lock:
                _bt_state["progress"] = i + 1

        with _bt_lock:
            _bt_state["status"]  = "done"
            _bt_state["current"] = ""
        _bt_result = {"interval": interval, "days": days, "results": results}

    except Exception as e:
        with _bt_lock:
            _bt_state["status"] = "error"
            _bt_state["error"]  = str(e)
            _bt_state["current"] = ""

@app.get("/api/backtest/instruments")
def api_backtest_instruments():
    """All instruments known to the bot, for the backtest instrument picker."""
    rows = list_instruments()
    return [{"figi": r["figi"], "ticker": r.get("ticker", ""), "name": r.get("name", "")} for r in rows if r.get("figi")]


@app.get("/api/backtest/strategies")
def api_backtest_strategies():
    """Return all strategies with their key backtest-relevant settings."""
    strategies = list_strategies()
    out = []
    for s in strategies:
        sid = s["id"]
        cfg = get_strategy_settings(sid)
        # Fetch instruments assigned to this strategy
        instr = list_strategy_instruments(sid)
        out.append({
            "id": sid,
            "name": s.get("name", f"Стратегия {sid}"),
            "tradingmode": cfg.get("tradingmode", "trend"),
            "stop_loss_pct": cfg.get("default_stop_loss_pct", "0.0025"),
            "take_profit_pct": cfg.get("default_take_profit_pct", "0.005"),
            "commission_pct": cfg.get("estimated_commission_pct", "0.0004"),
            "trailing_stop_enabled": cfg.get("trailing_stop_enabled", "0"),
            "instruments": [
                {"figi": i["figi"], "ticker": i.get("ticker", ""), "name": i.get("name", "")}
                for i in instr if i.get("figi")
            ],
        })
    return out


@app.post("/api/backtest/start")
async def api_backtest_start(request: Request):
    global _bt_result
    body = await request.json()
    interval     = str(body.get("interval", "5min")).strip()
    days         = max(1, min(int(body.get("days", 7)), 30))
    strategy_ids = [int(x) for x in (body.get("strategy_ids") or [])]
    if not strategy_ids:
        raise HTTPException(400, "Выберите хотя бы одну стратегию")
    with _bt_lock:
        if _bt_state["status"] == "running":
            raise HTTPException(400, "Бэктест уже выполняется")
        _bt_state.update({"status": "running", "progress": 0,
                          "total": len(strategy_ids), "current": "", "error": ""})
    _bt_result = None
    _bt_threading.Thread(target=_bt_run_worker, args=(strategy_ids, interval, days),
                         daemon=True, name="backtest-worker").start()
    return {"ok": True, "total": len(strategy_ids)}


@app.get("/api/backtest/status")
def api_backtest_status():
    with _bt_lock:
        return dict(_bt_state)


@app.get("/api/backtest/result")
def api_backtest_result():
    global _bt_result
    if _bt_result is None:
        raise HTTPException(400, "Результат не готов")
    return _bt_result


@app.post("/api/backtest/run")
async def api_backtest_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный JSON")

    interval     = str(body.get("interval", "5min")).strip()
    days         = max(1, min(int(body.get("days", 7)), 30))
    strategy_ids = body.get("strategy_ids") or []

    if not strategy_ids:
        raise HTTPException(status_code=400, detail="Выберите хотя бы одну стратегию")

    results = {}
    for sid in strategy_ids:
        sid        = int(sid)
        strat      = get_strategy(sid)
        if not strat:
            results[str(sid)] = {"error": f"Стратегия {sid} не найдена"}
            continue

        cfg        = get_strategy_settings(sid)
        mode       = cfg.get("tradingmode", "trend")
        comm_pct   = float(cfg.get("estimated_commission_pct", "0.0004"))
        strat_name = strat.get("name", f"Стратегия {sid}")

        # Берём первый включённый инструмент стратегии
        all_instr  = list_strategy_instruments(sid)
        inst_rows  = [i for i in all_instr if str(i.get("enabled", 1)) in ("1", "true")]
        if not inst_rows:
            results[str(sid)] = {"error": "Нет активных инструментов", "strategy_name": strat_name}
            continue

        instr      = inst_rows[0]
        figi       = instr["figi"]
        ticker     = instr.get("ticker", "?")
        sl_pct     = float(instr.get("stop_loss_pct")  or cfg.get("default_stop_loss_pct",  "0.0025"))
        tp_pct     = float(instr.get("take_profit_pct") or cfg.get("default_take_profit_pct", "0.005"))
        lots       = max(1, int(instr.get("lots_override") or 1))
        lot_size   = max(1, int(instr.get("lot") or 1))
        qty_shares = lots * lot_size

        try:
            candles = get_candles_range(figi=figi, interval_name=interval, days=days)
        except Exception as e:
            results[str(sid)] = {"error": f"Ошибка свечей {ticker}: {e}", "strategy_name": strat_name}
            continue

        if len(candles) < 30:
            results[str(sid)] = {
                "error": f"Мало свечей для {ticker}: {len(candles)}, нужно ≥30",
                "strategy_name": strat_name,
            }
            continue

        try:
            res = run_backtest(
                candles=candles, mode=mode,
                stop_loss_pct=sl_pct, take_profit_pct=tp_pct,
                commission_pct=comm_pct, qty=qty_shares,
            )
            d = result_to_dict(res, candles)
            d["strategy_name"] = strat_name
            d["ticker"]        = ticker
            d["figi"]          = figi
            d["mode"]          = mode
            d["sl_pct_ui"]     = f"{sl_pct * 100:.3f}%"
            d["tp_pct_ui"]     = f"{tp_pct * 100:.3f}%"
            d["lots"]          = lots
            d["lot_size"]      = lot_size
            d["qty_shares"]    = qty_shares
            d["qty_ui"]        = f"{lots} лот × {lot_size} шт = {qty_shares} шт"
            d["candles_loaded"] = len(candles)
            results[str(sid)]  = d
        except Exception as e:
            results[str(sid)] = {"error": str(e), "strategy_name": strat_name}

    return {"interval": interval, "days": days, "results": results}


# ── parallel strategies ────────────────────────────────────────────────────────

@app.get("/api/parallel/status")
def api_parallel_status():
    """Live status of parallel strategy threads + instruments with current prices."""
    active_profile_id = get_setting("active_profile_id", "").strip()
    if not active_profile_id:
        return {"threads": [], "coord": {}, "instruments": []}

    pid = int(active_profile_id)
    parallel_strats = list_profile_parallel_strategies(pid)
    market_map = get_instrument_market_state_map()
    # Открытые позиции из ОБОИХ источников: брокер (PORTFOLIO) — истина,
    # BOT — для случаев когда синк ещё не прошёл. По figi, брокер в приоритете.
    open_pos = {}
    for _src in ("BOT", "PORTFOLIO"):
        for p in get_open_positions(source=_src):
            open_pos[p["figi"]] = p  # PORTFOLIO перезапишет BOT (брокер = истина)

    # Календарные периоды для статистики
    from datetime import datetime as _dt2, timedelta as _td2
    _now2 = _dt2.now()
    _today_str  = _now2.strftime("%Y-%m-%d")
    # Начало текущей недели (понедельник)
    _week_str   = (_now2 - _td2(days=_now2.weekday())).strftime("%Y-%m-%d")
    # Начало текущего месяца
    _month_str  = _now2.strftime("%Y-%m-01")

    # Thread statuses + stats
    result = []
    for entry in parallel_strats:
        sid  = entry["strategy_id"]
        name = entry["name"]
        raw  = get_runtime(f"parallel_thread_{sid}") or ""
        try:
            info = json.loads(raw)
        except Exception:
            info = {"status": "не запущен", "ticker": "", "updated_at": ""}
        stats = {
            "day":   get_strategy_trade_stats(sid, 1,  date_from=_today_str),
            "week":  get_strategy_trade_stats(sid, 7,  date_from=_week_str),
            "month": get_strategy_trade_stats(sid, 30, date_from=_month_str),
        }
        for st in stats.values():
            st["pnl_ui"] = fmt_money(st["pnl"])
        # Число СТОП-ЛОССОВ по инструментам стратегии за текущий месяц
        # (реальные сделки reason='STOP_LOSS', а не повторяющиеся записи в логах)
        _loss_stops_month = 0
        try:
            from app.db import db_cursor as _dbc4
            with _dbc4() as _c4:
                _c4.execute(
                    "SELECT COUNT(*) FROM trades WHERE reason='STOP_LOSS' AND time>=? "
                    "AND ticker IN (SELECT ticker FROM strategy_instruments WHERE strategy_id=?)",
                    (_month_str, sid)
                )
                _loss_stops_month = int((_c4.fetchone() or [0])[0])
        except Exception:
            pass
        result.append({"strategy_id": sid, "name": name, **info, "stats": stats,
                        "loss_stops_month": _loss_stops_month})

    # Unified instruments table across all strategies
    all_instrs = []
    # Сумма ИТОГО — база для расчёта авто-лотов
    _parallel_cash = Decimal("0")
    try:
        _port = get_portfolio_snapshot()
        _parallel_cash = safe_decimal(_port.get("total_assets", 0) or _port.get("cash", 0))
    except Exception:
        pass

    # Дневной PnL по тикерам и счётчик блокировок за месяц (из event_logs)
    _daily_pnl_cache: dict = {}  # ticker → float
    _block_count_cache: dict = {}  # ticker → int

    try:
        from app.db import db_cursor as _dbc2
        with _dbc2() as _cur2:
            # Дневной PnL по тикерам
            _cur2.execute(
                "SELECT ticker, COALESCE(SUM(pnl),0) FROM trades WHERE time>=? GROUP BY ticker",
                (_today_str,)
            )
            for _r in _cur2.fetchall():
                _daily_pnl_cache[_r[0]] = float(_r[1])
            # Стоп-лоссы за текущий месяц по тикерам (реальные сделки)
            _cur2.execute(
                "SELECT ticker, COUNT(*) FROM trades WHERE reason='STOP_LOSS' AND time>=? GROUP BY ticker",
                (_month_str,)
            )
            for _r in _cur2.fetchall():
                _block_count_cache[_r[0]] = int(_r[1])
    except Exception:
        pass

    seen_figis: set = set()
    for strat in parallel_strats:
        sid = strat["strategy_id"]
        for instr in list_strategy_instruments(sid):
            if str(instr.get("enabled", 1)) not in ("1", "true"):
                continue
            figi = instr["figi"]
            if figi in seen_figis:
                continue
            seen_figis.add(figi)
            mkt  = market_map.get(figi, {})
            pos  = open_pos.get(figi)
            # Последний сигнал strategy_engine (сохраняется ботом в runtime_state)
            sig_raw = get_runtime(f"last_signal_{figi}") or ""
            try:
                sig_info = json.loads(sig_raw)
            except Exception:
                sig_info = {}
            upnl = float(pos.get("unrealized_pnl", 0)) if pos else 0.0
            volume = int(mkt.get("volume_1m", 0) or 0)
            lot_size      = int(instr.get("lot", 1) or 1)
            lots_override = int(instr.get("lots_override", 1) or 1)
            auto_lots_on  = int(instr.get("auto_lots", 0) or 0)
            last_price    = float(mkt.get("last_price", 0) or 0)
            if auto_lots_on and last_price > 0 and _parallel_cash > 0:
                _commission_pct = safe_decimal(get_setting("estimated_commission_pct", "0.0004"))
                _cost_per_lot = Decimal(str(lot_size)) * Decimal(str(last_price)) * (1 + _commission_pct)
                lot_count = max(1, int(_parallel_cash / _cost_per_lot)) if _cost_per_lot > 0 else 1
            else:
                lot_count = lots_override
            lot_cost_rub = lot_count * lot_size * last_price
            lot_cost_ui = f"{lot_cost_rub:,.0f} ₽".replace(",", " ") if lot_cost_rub > 0 else "—"
            all_instrs.append({
                "figi":            figi,
                "ticker":          instr["ticker"],
                "strategy_id":     sid,
                "strategy_name":   strat["name"],
                "lots":            lot_count,
                "auto_lots":       auto_lots_on,
                "lot_size":        lot_size,
                "lot_cost_rub":    lot_cost_rub,
                "lot_cost_ui":     lot_cost_ui,
                "sl_pct":          f"{float(instr.get('stop_loss_pct', 0))*100:.2f}%",
                "tp_pct":          f"{float(instr.get('take_profit_pct', 0))*100:.2f}%",
                "last_price_ui":   fmt_price(mkt.get("last_price", 0)),
                "price_time":      (mkt.get("price_time", "") or "")[-8:] or "—",
                "volume_1m":       volume,
                "volume_ui":       f"{volume:,}".replace(",", " ") if volume else "—",
                "signal_action":   sig_info.get("action", "—"),
                "signal_score":    sig_info.get("score", 0),
                "signal_mode":     sig_info.get("mode", ""),
                "signal_time":     sig_info.get("time", ""),
                "signal_skip_reason":  sig_info.get("skip_reason", ""),
                "signal_skip_filter":  sig_info.get("skip_filter", ""),
                "signal_reasons":  sig_info.get("reasons", []),
                "unrealized_pnl":  upnl,
                "in_position":     pos is not None,
                # Дневной лимит потерь
                "max_daily_loss_rub": float(instr.get("max_daily_loss_rub", 0) or 0),
                "daily_pnl":       _daily_pnl_cache.get(instr["ticker"], 0.0),
                "daily_pnl_ui":    fmt_money(_daily_pnl_cache.get(instr["ticker"], 0.0)),
                "is_loss_blocked": (
                    float(instr.get("max_daily_loss_rub", 0) or 0) > 0
                    and _daily_pnl_cache.get(instr["ticker"], 0.0) <= -float(instr.get("max_daily_loss_rub", 0) or 0)
                ),
                "loss_block_count_month": _block_count_cache.get(instr["ticker"], 0),
            })

    coord_raw = get_runtime("parallel_coord") or ""
    try:
        coord = json.loads(coord_raw)
        # Автоочистка устаревшего координатора: если фиги больше нет в открытых позициях
        if coord.get("owner_figi") and coord["owner_figi"] not in open_pos:
            coord = {}
    except Exception:
        coord = {}

    _max_pos = int(get_profile_setting(pid, "max_open_positions", "3") or 3)
    return {"threads": result, "coord": coord, "instruments": all_instrs,
            "max_open_positions": _max_pos}


@app.post("/api/profile/{profile_id}/parallel-toggle")
async def api_profile_parallel_toggle(profile_id: int, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    update_profile_settings(profile_id, {"parallel_trading_enabled": "1" if enabled else "0"})
    return {"ok": True, "parallel_trading_enabled": enabled}


@app.get("/api/profile/{profile_id}/parallel-strategies")
def api_profile_parallel_list(profile_id: int):
    return list_profile_parallel_strategies(profile_id)


@app.post("/api/profile/{profile_id}/parallel-strategies")
async def api_profile_parallel_add(profile_id: int, request: Request):
    body = await request.json()
    strategy_id = int(body.get("strategy_id", 0))
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id required")
    add_profile_parallel_strategy(profile_id, strategy_id)
    return {"ok": True}


@app.delete("/api/profile/{profile_id}/parallel-strategies/{strategy_id}")
def api_profile_parallel_remove(profile_id: int, strategy_id: int):
    remove_profile_parallel_strategy(profile_id, strategy_id)
    return {"ok": True}


# ── analyst ───────────────────────────────────────────────────────────────────

@app.get("/api/analyst/status")
def api_analyst_status():
    return _analyst.get_state()


@app.post("/api/analyst/start")
async def api_analyst_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    budget        = float(body.get("budget_rub",      get_setting("analyst_budget_rub",    "60000")))
    win_rate      = float(body.get("min_win_rate",     get_setting("analyst_min_win_rate",  "45")))
    min_trades    = int(body.get("min_trades",         get_setting("analyst_min_trades",    "5")))
    days          = int(body.get("days",               get_setting("analyst_days",          "14")))
    interval      = str(body.get("interval",           get_setting("analyst_interval",      "15min")))
    min_pnl       = float(body.get("min_pnl",         get_setting("analyst_min_pnl",       "0")))
    exclude_active = bool(body.get("exclude_active", True))

    # Persist settings
    from app.db import db_cursor as _dbc
    with _dbc() as cur:
        for k, v in [
            ("analyst_budget_rub", str(budget)), ("analyst_min_win_rate", str(win_rate)),
            ("analyst_min_trades", str(min_trades)), ("analyst_days", str(days)),
            ("analyst_interval", interval), ("analyst_min_pnl", str(min_pnl)),
        ]:
            cur.execute("INSERT INTO bot_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))

    ok, msg = _analyst.start(budget, win_rate, min_trades, days, interval, min_pnl, exclude_active=exclude_active)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/analyst/stop")
def api_analyst_stop():
    ok, msg = _analyst.stop()
    return {"ok": ok, "message": msg}


@app.get("/api/analyst/sessions")
def api_analyst_sessions(limit: int = 30):
    """История сессий (поиск + оптимизация + еженедельные)."""
    from app.db import db_cursor as _dbc
    sessions = list_optimization_sessions(limit=limit)
    MODE   = {"mean_reversion": "Возврат к средней", "breakout": "Пробой", "trend": "Тренд"}
    STATUS = {"pending": "Ожидает", "accepted": "Принято", "partial": "Частично",
              "rejected": "Отклонено", "running": "Выполняется", "done": "Завершён",
              "error": "Ошибка"}
    TYPE   = {"search": "Поиск", "manual": "Оптимизация", "weekly": "Еженедельная"}

    result = []
    for s in sessions:
        sid  = s["id"]
        stype = s.get("type", "manual")

        if stype == "search":
            # Результаты из analyst_results по run_id (хранится в notes)
            run_id = (s.get("notes") or "").split("|")[0].replace("run_id=", "").strip()
            if run_id:
                with _dbc() as cur:
                    cur.execute("""
                    SELECT id, ticker, tradingmode as best_mode, sl_pct as best_sl,
                           tp_pct as best_tp, net_pnl as best_pnl, win_rate as best_win_rate,
                           total_trades as best_trades, score as improvement_pct,
                           0 as applied, '' as strategy_name, '' as base_mode,
                           0.0 as base_sl, 0.0 as base_tp, 0.0 as base_pnl,
                           0 as base_trades, 0.0 as base_win_rate
                    FROM analyst_results WHERE run_id=?
                    ORDER BY score DESC LIMIT 30
                    """, (run_id,))
                    raw_results = [dict(r) for r in cur.fetchall()]
            else:
                raw_results = []
        else:
            raw_results = get_session_results(sid)

        def fmt(r):
            return {
                **r,
                "base_mode_label": MODE.get(r.get("base_mode",""), r.get("base_mode","")),
                "best_mode_label": MODE.get(r.get("best_mode",""), r.get("best_mode","")),
                "base_sl_ui":  f"{float(r.get('base_sl',0))*100:.3f}%",
                "base_tp_ui":  f"{float(r.get('base_tp',0))*100:.3f}%",
                "best_sl_ui":  f"{float(r.get('best_sl',0))*100:.3f}%",
                "best_tp_ui":  f"{float(r.get('best_tp',0))*100:.3f}%",
                "improvement_ui": (
                    f"+{float(r.get('improvement_pct',0)):.1f}%"
                    if float(r.get("improvement_pct",0)) > 0
                    else f"{float(r.get('improvement_pct',0)):.1f}%"
                ),
            }

        result.append({
            **s,
            "type_label":    TYPE.get(stype, stype),
            "status_label":  STATUS.get(s["status"], s["status"]),
            "result_count":  len(raw_results),
            "applied_count": sum(1 for r in raw_results if r.get("applied")),
            "results":       [fmt(r) for r in raw_results],
        })
    return JSONResponse(result)


@app.post("/api/analyst/sessions/{session_id}/apply")
async def api_session_apply(session_id: int, request: Request):
    """Применяет выбранные result_ids из сессии."""
    body       = await request.json()
    result_ids = body.get("result_ids", [])  # пустой = все
    results    = get_session_results(session_id)
    if not result_ids:
        result_ids = [r["id"] for r in results]
    applied = 0
    errors  = []
    for rid in result_ids:
        try:
            apply_optimization_result(int(rid))
            applied += 1
        except Exception as e:
            errors.append(str(e))
    status = "accepted" if applied == len(result_ids) else "partial"
    update_session_status(session_id, status, f"Применено {applied} из {len(result_ids)} через дашборд")
    return JSONResponse({"ok": True, "applied": applied, "errors": errors})


@app.post("/api/analyst/sessions/{session_id}/reject")
def api_session_reject(session_id: int):
    update_session_status(session_id, "rejected", "Отклонено через дашборд")
    return JSONResponse({"ok": True})


@app.post("/api/analyst/sessions/create-manual")
def api_session_create_manual():
    """Создаёт сессию для текущего ручного запуска аналитика."""
    sid = create_optimization_session(type_="manual")
    return JSONResponse({"ok": True, "session_id": sid})


@app.post("/api/strategies/set-all")
async def api_strategies_set_all(request: Request):
    """Устанавливает значение любого ключа настроек для ВСЕХ стратегий."""
    body = await request.json()
    key   = str(body.get("key", "")).strip()
    value = str(body.get("value", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    set_setting_all_strategies(key, value)
    return JSONResponse({"ok": True, "key": key, "value": value})


@app.get("/api/analyst/instruments")
def api_analyst_instruments_list():
    """Возвращает список инструментов доступных аналитику (SEARCH_INSTRUMENTS)."""
    from app.services.analyst import SEARCH_INSTRUMENTS
    return JSONResponse([
        {
            "ticker":          i.get("ticker", ""),
            "figi":            i.get("figi", ""),
            "instrument_uid":  i.get("instrument_uid", ""),
            "name":            i.get("name", ""),
            "lot":             i.get("lot", 1),
        }
        for i in SEARCH_INSTRUMENTS
    ])


@app.post("/api/analyst/instruments/update-uid")
async def api_analyst_instruments_update_uid(request: Request):
    """Обновляет instrument_uid для инструмента в SEARCH_INSTRUMENTS и strategy_instruments."""
    body = await request.json()
    figi = (body.get("figi") or "").strip()
    uid  = (body.get("instrument_uid") or "").strip()
    if not figi:
        raise HTTPException(status_code=400, detail="figi required")
    from app.db import save_instrument_uid
    save_instrument_uid(figi, uid)
    # Обновляем in-memory список
    from app.services.analyst import SEARCH_INSTRUMENTS
    for inst in SEARCH_INSTRUMENTS:
        if inst.get("figi") == figi:
            inst["instrument_uid"] = uid
            break
    return JSONResponse({"ok": True})


@app.get("/api/analyst/results")
def api_analyst_results(limit: int = 50):
    rows = _analyst.get_results(limit=limit)
    MODE_LABELS = {"mean_reversion": "Возврат к средней", "breakout": "Пробой", "trend": "Тренд"}
    out = []
    for r in rows:
        out.append({
            "id":              r["id"],
            "ticker":          r["ticker"],
            "instrument_name": r["instrument_name"],
            "mode":            r["tradingmode"],
            "mode_label":      MODE_LABELS.get(r["tradingmode"], r["tradingmode"]),
            "interval":        r["interval"],
            "days":            r["days"],
            "sl_pct_ui":       f"{float(r['sl_pct'])*100:.3f}%",
            "tp_pct_ui":       f"{float(r['tp_pct'])*100:.3f}%",
            "net_pnl":         round(r["net_pnl"], 2),
            "win_rate":        round(r["win_rate"], 1),
            "profit_factor":   round(r["profit_factor"], 2),
            "total_trades":    r["total_trades"],
            "max_drawdown":    round(r["max_drawdown"], 2),
            "avg_r_multiple":  round(r["avg_r_multiple"], 2),
            "sharpe_ratio":    round(r["sharpe_ratio"], 2),
            "score":                 round(r["score"], 1),
            "avg_price":             round(float(r.get("avg_price") or 0), 4),
            "budget_rub":            float(r.get("budget_rub") or 0),
            "equity_curve":          json.loads(r["equity_curve"] or "[]"),
            "saved":                 r["saved_strategy_id"] is not None,
            "min_signal_score_used": int(r.get("min_signal_score_used") or 0),
        })
    return out


@app.get("/api/analyst/result/{result_id}")
def api_analyst_result_detail(result_id: int):
    r = _analyst.get_result_by_id(result_id)
    if not r:
        raise HTTPException(status_code=404, detail="Результат не найден")
    sl         = float(r["sl_pct"])
    tp         = float(r["tp_pct"])
    budget     = float(r.get("budget_rub") or 0)
    avg_price  = float(r.get("avg_price") or 0)
    lots       = max(1, int((budget * 0.95) // avg_price)) if avg_price > 0 and budget > 0 else 1
    return {
        **r,
        "equity_curve": json.loads(r.get("equity_curve") or "[]"),
        "sl_pct_ui":    f"{sl * 100:.3f}%",
        "tp_pct_ui":    f"{tp * 100:.3f}%",
        "avg_price_ui": f"{avg_price:.2f} ₽" if avg_price else "—",
        "lots_calc":    lots,
        "budget_ui":    f"{budget:,.0f} ₽",
    }


@app.post("/api/analyst/save/{result_id}")
async def api_analyst_save(result_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("strategy_name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите название стратегии")
    try:
        sid = _analyst.save_as_strategy(result_id, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "strategy_id": sid, "strategy_name": name}


@app.post("/api/analyst/optimize-start")
async def api_analyst_optimize_start(request: Request):
    body = await request.json()
    ok, msg = _analyst.start_optimize(
        budget_rub=float(body.get("budget_rub", 60000)),
        days=int(body.get("days", 14)),
        interval=body.get("interval", "15min"),
    )
    return JSONResponse({"ok": ok, "message": msg})


@app.post("/api/analyst/optimize-stop")
async def api_analyst_optimize_stop():
    ok, msg = _analyst.stop_optimize()
    return JSONResponse({"ok": ok, "message": msg})


@app.get("/api/analyst/optimize-status")
def api_analyst_optimize_status():
    return JSONResponse(_analyst.get_opt_state())


@app.get("/api/analyst/optimize-results")
def api_analyst_optimize_results():
    from app.db import get_optimization_results
    results = get_optimization_results()
    out = []
    for r in results:
        out.append({
            **r,
            "base_sl_ui": f"{r['base_sl']*100:.2f}%",
            "base_tp_ui": f"{r['base_tp']*100:.2f}%",
            "best_sl_ui": f"{r['best_sl']*100:.2f}%",
            "best_tp_ui": f"{r['best_tp']*100:.2f}%",
            "base_pnl_ui": fmt_money(r["base_pnl"]),
            "best_pnl_ui": fmt_money(r["best_pnl"]),
            "improvement_ui": f"+{r['improvement_pct']:.1f}%" if r["improvement_pct"] >= 0 else f"{r['improvement_pct']:.1f}%",
        })
    return JSONResponse(out)


@app.post("/api/analyst/optimize-apply/{result_id}")
async def api_analyst_optimize_apply(result_id: int):
    from app.db import apply_optimization_result
    try:
        apply_optimization_result(result_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    trade_only_session: str = Form("1"),
    max_open_positions: str = Form("3"),
):
    use_sandbox = "true" if runtime_mode == "sandbox" else "false"
    try:
        _mop = max(1, min(12, int(max_open_positions or 3)))
    except Exception:
        _mop = 3
    update_profile_settings(profile_id, {
        "bot_enabled": bool01(bot_enabled),
        "telegram_errors_only": bool01(telegram_errors_only),
        "auto_reload_settings": bool01(auto_reload_settings),
        "tinvestusesandbox": use_sandbox,
        "trade_only_session": bool01(trade_only_session),
        "max_open_positions": str(_mop),
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
    pause_after_error_sec: str = Form("10"),
    tradingmode: str = Form("trend"),
    errorseriespausecount: str = Form("3"),
    stopseriespausecount: str = Form("3"),
    trailing_stop_enabled: str = Form("0"),
    use_signal_service: str = Form("0"),
    use_api_confirm: str = Form("0"),
    min_signal_score: str = Form("0"),
    use_order_book_filter: str = Form("1"),
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
        "pause_after_error_sec": pause_after_error_sec,
        "tradingmode": tradingmode,
        "errorseriespausecount": errorseriespausecount,
        "stopseriespausecount": stopseriespausecount,
        "trailing_stop_enabled": bool01(trailing_stop_enabled),
        "use_signal_service": bool01(use_signal_service),
        "use_api_confirm": bool01(use_api_confirm),
        "min_signal_score": min_signal_score,
        "use_order_book_filter": bool01(use_order_book_filter),
    })
    new_name = apply_auto_name(strategy_id)
    return JSONResponse({"ok": True, "new_name": new_name})


@app.post("/api/strategies/{strategy_id}/delete")
def api_strategies_delete(strategy_id: int):
    try:
        delete_strategy(strategy_id)
        return JSONResponse({"ok": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── strategy instruments API ──────────────────────────────────────────────────

@app.get("/api/strategy/{strategy_id}/details")
def api_strategy_details(strategy_id: int):
    """Настройки + инструменты стратегии — для редактирования в параллельном режиме."""
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    raw = get_strategy_settings(strategy_id)

    def ss(key, default=""):
        return raw.get(key, default)

    market_map = get_instrument_market_state_map()
    instr = list_strategy_instruments(strategy_id)
    return JSONResponse({
        "id":   strategy_id,
        "name": strategy.get("name", ""),
        "settings": {
            "max_trades_per_day":        ss("max_trades_per_day", "15"),
            "max_daily_loss_rub":        ss("max_daily_loss_rub", "200"),
            "max_daily_loss_rub_ui":     fmt_money(ss("max_daily_loss_rub", "200")),
            "max_open_positions":        ss("max_open_positions", "2"),
            "check_interval_sec":        ss("check_interval_sec", "5"),
            "default_stop_loss_pct_ui":  fmt_pct_fraction(ss("default_stop_loss_pct", "0.0025")),
            "default_take_profit_pct_ui": fmt_pct_fraction(ss("default_take_profit_pct", "0.005")),
            "estimated_commission_pct_ui": fmt_pct_fraction(ss("estimated_commission_pct", "0.0004")),
            "allow_long_global":         ss("allow_long_global", "1"),
            "allow_short_global":        ss("allow_short_global", "1"),
            "trade_only_session":        ss("trade_only_session", "0"),
            "pause_after_error_sec":     ss("pause_after_error_sec", "10"),
            "tradingmode":               ss("tradingmode", "trend"),
            "errorseriespausecount":     ss("errorseriespausecount", "3"),
            "stopseriespausecount":      ss("stopseriespausecount", "3"),
            "trailing_stop_enabled":     ss("trailing_stop_enabled", "0"),
            "use_signal_service":        ss("use_signal_service", "0"),
            "use_api_confirm":           ss("use_api_confirm", "0"),
            "min_signal_score":          ss("min_signal_score", "0"),
        },
        "instruments": [strategy_instrument_row(i, market_map) for i in instr],
    })


@app.get("/api/strategy/{strategy_id}/instruments")
def api_strategy_instruments_get(strategy_id: int):
    """Return instruments for a strategy with market data (for parallel expand panel)."""
    market_map = get_instrument_market_state_map()
    instr = list_strategy_instruments(strategy_id)
    return {"instruments": [strategy_instrument_row(i, market_map) for i in instr]}


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

        # Всегда берём lot и min_price_increment из брокера — не доверяем фронтенду
        real_lot = int(item.get("lot") or 1)
        real_step = str(item.get("min_price_increment", item.get("minpriceincrement", "0.01")) or "0.01")
        real_uid = (item.get("uid") or item.get("instrument_uid") or "").strip()
        try:
            from app.instruments import get_instrument_meta
            _meta = get_instrument_meta(figi)
            if _meta:
                real_lot = int(_meta.get("lot") or real_lot)
                real_step = str(_meta.get("min_price_increment") or real_step)
                real_uid = real_uid or _meta.get("uid", "")
        except Exception as _e:
            logger.warning("Не удалось получить метаданные для %s: %s", figi, _e)

        inst = {
            "ticker": (item.get("ticker") or "").strip(),
            "figi": figi,
            "instrument_uid": real_uid,
            "name": item.get("name", ""),
            "class_code": item.get("class_code", item.get("classcode", "")),
            "instrument_type": item.get("instrument_type", item.get("instrumenttype", "share")),
            "currency": item.get("currency", "RUB"),
            "lot": real_lot,
            "min_price_increment": real_step,
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
    new_name = apply_auto_name(strategy_id)
    return JSONResponse({"ok": True, "добавлено": added, "new_name": new_name})


@app.post("/api/strategies/{strategy_id}/instruments/update")
def api_strategy_instruments_update(
    strategy_id: int,
    figi: str = Form(...),
    lots_override: str = Form("1"),
    auto_lots: str = Form("0"),
    max_daily_loss_rub: str = Form("0"),
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
        "auto_lots": int(bool01(auto_lots)),
        "max_daily_loss_rub": float(safe_decimal(max_daily_loss_rub)),
        "stop_loss_pct": str(safe_decimal(stop_loss_pct) / Decimal("100")),
        "take_profit_pct": str(safe_decimal(take_profit_pct) / Decimal("100")),
        "max_spread_pct": str(safe_decimal(max_spread_pct) / Decimal("100")),
        "min_volume": min_volume,
        "allow_long": int(bool01(allow_long)),
        "allow_short": int(bool01(allow_short)),
        "priority": priority,
        "enabled": int(bool01(enabled)),
    })
    new_name = apply_auto_name(strategy_id)
    return JSONResponse({"ok": True, "new_name": new_name})


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
    # Берём instrument_uid из нашей БД для этого figi
    mmap = get_instrument_market_state_map()
    uid  = next((v.get("instrument_uid", "") or "" for v in mmap.values() if v.get("figi") == figi), "")
    if not uid:
        from app.db import db_cursor as _dbc
        with _dbc() as _cur:
            _cur.execute("SELECT instrument_uid FROM strategy_instruments WHERE figi = ? LIMIT 1", (figi,))
            row = _cur.fetchone()
            uid = (row["instrument_uid"] or "") if row else ""

    # Отменяем ВСЕ ордера (стоп + лимитные) для этого figi ПЕРЕД закрытием
    cancelled_stops = 0
    cancelled_orders = 0
    try:
        active_stops = get_active_stop_orders()
        for s in active_stops:
            if s.get("figi") == figi or (uid and uid in str(s)):
                try:
                    cancel_stop_order(s["stop_order_id"])
                    cancelled_stops += 1
                except Exception as _se:
                    logger.warning("cancel stop %s: %s", s.get("stop_order_id"), _se)
        if cancelled_stops:
            log_event("STOP_ORDER", f"manual close: отменено {cancelled_stops} стоп-ордеров для {figi}", ticker=figi)
    except Exception as _e:
        logger.warning("get_active_stop_orders before close: %s", _e)
    try:
        active_orders = get_active_orders()
        for o in active_orders:
            if o.get("figi") == figi or (uid and o.get("instrument_uid") == uid):
                try:
                    cancel_order(o["order_id"])
                    cancelled_orders += 1
                except Exception as _oe:
                    logger.warning("cancel order %s: %s", o.get("order_id"), _oe)
        if cancelled_orders:
            log_event("ORDER", f"manual close: отменено {cancelled_orders} ордеров для {figi}", ticker=figi)
    except Exception as _e:
        logger.warning("get_active_orders before close: %s", _e)

    try:
        result = post_market_close(figi=figi, quantity=int(qty), direction=close_direction, instrument_uid=uid)
        log_event("POSITION_CLOSE", f"close order posted figi={figi} qty={qty} direction={close_direction}", ticker=figi)
        return {"ok": True, "message": "close order posted", "cancelled_stops": cancelled_stops,
                "order_id": getattr(result, "order_id", "")}
    except Exception as e:
        msg = str(e)
        import re as _re
        m = _re.search(r"message='([^']+)'", msg)
        readable = m.group(1) if m else msg[:120]
        log_event("ORDER_ERROR", f"manual close figi={figi}: {readable}", ticker=figi, level="ERROR")
        raise HTTPException(status_code=422, detail=readable)


@app.post("/api/позиции/очистить-локальные")
def api_clear_local_positions():
    """Удалить мусорные локальные записи позиций (не трогает брокера)."""
    clear_open_positions()
    log_event("SERVICE_CONTROL", "Local position records cleared manually")
    return JSONResponse({"ok": True})


@app.post("/api/позиции/закрыть-все")
def api_close_all_positions():
    positions = get_open_positions(source="BOT")
    from app.db import db_cursor as _dbc2
    closed = 0
    errors = []
    for p in positions:
        try:
            figi      = p.get("figi", "")
            qty       = int(p.get("qty", 0))
            direction = str(p.get("direction", "BUY")).upper()
            if not figi or qty <= 0:
                continue
            # instrument_uid из strategy_instruments
            with _dbc2() as _cur:
                _cur.execute("SELECT instrument_uid FROM strategy_instruments WHERE figi = ? LIMIT 1", (figi,))
                row = _cur.fetchone()
                uid = (row["instrument_uid"] or "") if row else ""
            close_direction = "LONG_CLOSE" if direction == "BUY" else "SHORT_CLOSE"
            post_market_close(figi=figi, quantity=qty, direction=close_direction, instrument_uid=uid)
            log_event("POSITION_CLOSE", f"close-all: figi={figi} qty={qty}", ticker=figi)
            closed += 1
        except Exception as e:
            import re as _re2
            m = _re2.search(r"message='([^']+)'", str(e))
            errors.append(m.group(1) if m else str(e)[:80])
    return JSONResponse({"ok": True, "closed": closed, "errors": errors})


# ── active orders (limit / market pending) ────────────────────────────────────

@app.get("/api/orders/active")
def api_active_orders():
    try:
        return JSONResponse({"ok": True, "items": get_active_orders()})
    except Exception as e:
        logger.exception("get_active_orders error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/orders/{order_id}/cancel")
def api_cancel_order(order_id: str):
    try:
        cancel_order(order_id)
        log_event("ORDER", f"Ордер {order_id} отменён вручную")
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)[:200])


@app.post("/api/orders/cancel-all")
def api_cancel_all_orders():
    items = get_active_orders()
    cancelled, errors = 0, []
    for o in items:
        try:
            cancel_order(o["order_id"])
            cancelled += 1
        except Exception as e:
            errors.append(str(e)[:80])
    log_event("ORDER", f"Отменено активных ордеров: {cancelled}")
    return JSONResponse({"ok": True, "cancelled": cancelled, "errors": errors})


# ── stop orders ───────────────────────────────────────────────────────────────

@app.post("/api/stop-orders/{stop_order_id}/cancel")
def api_cancel_stop_order(stop_order_id: str):
    try:
        cancel_stop_order(stop_order_id)
        log_event("STOP_ORDER", f"Стоп-ордер {stop_order_id} отменён вручную")
        return JSONResponse({"ok": True})
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)[:200])


@app.post("/api/stop-orders/cancel-all")
def api_cancel_all_stop_orders():
    """Отменяет все активные стоп-ордера на счёте."""
    items = get_active_stop_orders()
    cancelled, errors = 0, []
    for s in items:
        try:
            cancel_stop_order(s["stop_order_id"])
            cancelled += 1
        except Exception as e:
            errors.append(str(e)[:80])
    log_event("STOP_ORDER", f"Отменено всех стоп-ордеров: {cancelled}")
    return JSONResponse({"ok": True, "cancelled": cancelled, "errors": errors})


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

@app.get("/api/telegram/status")
def api_telegram_status():
    """Диагностика настроек Telegram без отправки сообщения."""
    from app.config import settings as _cfg
    s = get_all_settings()
    token = (_cfg.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (_cfg.TELEGRAM_CHAT_ID or "").strip()
    enabled = _cfg.TELEGRAM_ENABLED
    errors_only = get_setting("telegram_errors_only", "0")

    problems = []
    if not enabled:
        problems.append("TELEGRAM_ENABLED=false в .env — уведомления выключены")
    if not token:
        problems.append("TELEGRAM_BOT_TOKEN не задан в .env")
    elif len(token) < 30:
        problems.append(f"TELEGRAM_BOT_TOKEN подозрительно короткий ({len(token)} символов)")
    if not chat_id:
        problems.append("TELEGRAM_CHAT_ID не задан в .env")
    if errors_only == "1":
        problems.append("telegram_errors_only=1 в настройках профиля — отправляются только ошибки, торговые уведомления заблокированы")

    return JSONResponse({
        "enabled": enabled,
        "token_set": bool(token),
        "token_preview": f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "—",
        "chat_id": chat_id,
        "telegram_errors_only": errors_only,
        "problems": problems,
        "ok": len(problems) == 0,
    })


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


# ── Credentials API ───────────────────────────────────────────────────────────

@app.get("/api/credentials")
def api_credentials_get():
    """Возвращает маскированные токены и account_id (sandbox + prod)."""
    env = _read_env()
    is_sandbox = str(get_all_settings().get("tinvestusesandbox", "true")).lower() == "true"
    active_token = env.get("TINVEST_TOKEN", "")
    active_id    = env.get("TINVEST_ACCOUNT_ID", "")
    sb_token  = env.get("TINVEST_TOKEN_SANDBOX",  active_token if is_sandbox else "")
    sb_id     = env.get("TINVEST_ACCOUNT_ID_SANDBOX", active_id if is_sandbox else "")
    prod_token = env.get("TINVEST_TOKEN_PROD",  active_token if not is_sandbox else "")
    prod_id    = env.get("TINVEST_ACCOUNT_ID_PROD", active_id if not is_sandbox else "")
    return JSONResponse({
        "is_sandbox": is_sandbox,
        "sandbox": {
            "token_masked": _mask_token(sb_token),
            "account_id": sb_id,
            "has_token": bool(sb_token),
        },
        "prod": {
            "token_masked": _mask_token(prod_token),
            "account_id": prod_id,
            "has_token": bool(prod_token),
        },
    })


@app.post("/api/credentials")
async def api_credentials_save(request: Request):
    """Сохраняет токен и account_id в .env (не попадает в git)."""
    body = await request.json()
    mode       = body.get("mode", "sandbox")   # "sandbox" | "prod"
    token      = (body.get("token") or "").strip()
    account_id = (body.get("account_id") or "").strip()
    if not token and not account_id:
        raise HTTPException(status_code=400, detail="token or account_id required")
    is_sandbox = str(get_all_settings().get("tinvestusesandbox", "true")).lower() == "true"
    updates: dict = {}
    if mode == "sandbox":
        if token:
            updates["TINVEST_TOKEN_SANDBOX"] = token
            if is_sandbox:
                updates["TINVEST_TOKEN"] = token
        if account_id:
            updates["TINVEST_ACCOUNT_ID_SANDBOX"] = account_id
            if is_sandbox:
                updates["TINVEST_ACCOUNT_ID"] = account_id
    else:
        if token:
            updates["TINVEST_TOKEN_PROD"] = token
            if not is_sandbox:
                updates["TINVEST_TOKEN"] = token
        if account_id:
            updates["TINVEST_ACCOUNT_ID_PROD"] = account_id
            if not is_sandbox:
                updates["TINVEST_ACCOUNT_ID"] = account_id
    _write_env(updates)
    return JSONResponse({"ok": True, "updated": list(updates.keys())})


# ── ML Learning API ────────────────────────────────────────────────────────────

@app.get("/api/ml/summary")
def api_ml_summary():
    """Сводка по состоянию обучающейся модели: инструменты, лог оптимизаций, модели."""
    try:
        from app.ml.orchestrator import get_learning_summary
        data = get_learning_summary()
        # Добавляем данные Phase 2: модели и решения
        from app.db import db_cursor as _dbc_ml
        with _dbc_ml() as _c:
            # Только последняя модель на каждый figi (иначе дубли archived/active)
            _c.execute("""
                SELECT m.figi, m.ticker, m.trained_at, m.accuracy, m.precision_, m.recall,
                       m.n_training_samples, m.status, m.feature_importance
                FROM ml_models m
                JOIN (SELECT figi, MAX(id) AS mid FROM ml_models GROUP BY figi) latest
                  ON m.id = latest.mid
                ORDER BY m.id DESC LIMIT 50
            """)
            _cols = [d[0] for d in _c.description]
            data["trained_models"] = [dict(zip(_cols, r)) for r in _c.fetchall()]
            _c.execute("""
                SELECT timestamp, ticker, decision_type, model_confidence,
                       threshold, executed, reason
                FROM ml_decisions ORDER BY id DESC LIMIT 30
            """)
            _cols2 = [d[0] for d in _c.description]
            data["decisions"] = [dict(zip(_cols2, r)) for r in _c.fetchall()]
            # Статистика признаков (последние 50 размеченных)
            _c.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) as wins,
                       COUNT(CASE WHEN label IS NOT NULL THEN 1 END) as labeled
                FROM ml_features
            """)
            row = _c.fetchone()
            data["features_stats"] = {"total": row[0], "wins": row[1], "labeled": row[2]}
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e), "states": [], "log": []})


@app.get("/api/ml/instrument/{figi}")
def api_ml_instrument(figi: str):
    """Детальная статистика ML по конкретному инструменту."""
    try:
        from app.ml.analyzer import get_instrument_state, get_all_states
        from app.ml.strategy_selector import get_mode_stats
        from app.ml.experience import get_recent_contexts
        contexts = get_recent_contexts(figi, days=90)
        mode_stats = get_mode_stats(figi)
        return JSONResponse({
            "figi": figi,
            "contexts_count": len(contexts),
            "mode_stats": mode_stats,
            "recent_trades": [
                {"time": c.get("entry_time", ""), "pnl": c.get("pnl", 0),
                 "quality": round(c.get("quality_score", 0), 4),
                 "mode": c.get("strategy_mode", ""),
                 "score": c.get("signal_score", 0),
                 "holding_h": round(c.get("holding_hours", 0), 2)}
                for c in contexts[:20]
            ],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/instrument/stats/{figi}")
def api_instrument_stats(figi: str):
    """Котировки + диапазоны дня/недели/месяца/года для блока позиции."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from app.services.tbank_client import get_candles_range as _gcr
    _msk = _tz(timedelta(hours=3))

    _stats_cache = getattr(api_instrument_stats, "_cache", {})
    _now_ts = time.time()
    cached = _stats_cache.get(figi)
    if cached and _now_ts - cached["ts"] < 60:
        return JSONResponse(cached["data"])

    try:
        candles = _gcr(figi=figi, interval_name="day", days=365)
        if not candles:
            return JSONResponse({"error": "no data"})

        def _f(v):
            return float(v) if v is not None else None

        today_str = _dt.now(_msk).strftime("%Y-%m-%d")
        today_c  = [c for c in candles if str(c.get("time", "")).startswith(today_str)]
        week_c   = candles[-5:]  if len(candles) >= 5  else candles
        month_c  = candles[-22:] if len(candles) >= 22 else candles
        year_c   = candles

        prev_close = _f(candles[-2]["close"]) if len(candles) >= 2 else None
        open_today = _f(today_c[0]["open"])   if today_c else None
        day_high   = max((_f(c["high"])  for c in today_c if c.get("high")),  default=None)
        day_low    = min((_f(c["low"])   for c in today_c if c.get("low")),   default=None)
        week_high  = max((_f(c["high"])  for c in week_c  if c.get("high")),  default=None)
        week_low   = min((_f(c["low"])   for c in week_c  if c.get("low")),   default=None)
        month_high = max((_f(c["high"])  for c in month_c if c.get("high")),  default=None)
        month_low  = min((_f(c["low"])   for c in month_c if c.get("low")),   default=None)
        year_high  = max((_f(c["high"])  for c in year_c  if c.get("high")),  default=None)
        year_low   = min((_f(c["low"])   for c in year_c  if c.get("low")),   default=None)
        avg_vol    = int(sum(_f(c["volume"]) or 0 for c in candles[-20:]) / min(20, len(candles))) if candles else 0

        # Капитализация и мультипликаторы — из таблицы strategy_instruments (если есть)
        mkt_cap = None
        try:
            with __import__("app.db", fromlist=["db_cursor"]).db_cursor() as _c:
                _c.execute("SELECT market_cap, pe_ratio, pb_ratio, ps_ratio FROM strategy_instruments WHERE figi=? LIMIT 1", (figi,))
                _row = _c.fetchone()
                if _row:
                    mkt_cap = _row[0]
        except Exception:
            pass

        data = {
            "prev_close":  prev_close,
            "open_today":  open_today,
            "day_high":    day_high,   "day_low":    day_low,
            "week_high":   week_high,  "week_low":   week_low,
            "month_high":  month_high, "month_low":  month_low,
            "year_high":   year_high,  "year_low":   year_low,
            "avg_vol":     avg_vol,
            "market_cap":  mkt_cap,
        }
        _stats_cache[figi] = {"ts": _now_ts, "data": data}
        api_instrument_stats._cache = _stats_cache
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.post("/api/ml/rebalance")
async def api_ml_rebalance():
    """Запускает принудительный ребаланс всех инструментов."""
    import threading
    try:
        from app.ml.orchestrator import run_daily_rebalance
        threading.Thread(target=run_daily_rebalance, daemon=True).start()
        return JSONResponse({"ok": True, "message": "Ребаланс запущен в фоне"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
