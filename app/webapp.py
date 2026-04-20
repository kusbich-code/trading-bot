from decimal import Decimal
from datetime import datetime

from fastapi import FastAPI, Query, Request, Form
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
from app.instruments import find_instruments, get_popular_tickers, get_last_prices_for_figis
from app.control import run_control
from app.stop_orders import (
    create_take_profit_order,
    create_stop_loss_order,
    get_stop_orders,
    cancel_stop_order,
)

app = FastAPI(title="Панель управления торговым ботом v3.5")
init_db()


def get_client_cls():
    return SandboxClient if settings.TINVEST_USE_SANDBOX else Client


def get_client():
    client_cls = get_client_cls()
    return client_cls(settings.TINVEST_TOKEN)


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


@app.get("/api/главное")
def api_main():
    return {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": get_runtime("daily_pnl", "0"),
        "trades_today": get_runtime("trades_today", "0"),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": get_runtime("session_balance_start", "0"),
        "session_balance_current": get_runtime("session_balance_current", "0"),
        "bot_enabled": get_setting("bot_enabled", "1"),
        "active_profile_name": get_setting("active_profile_name", "Основной"),
        "active_strategy_name": get_setting("active_strategy_name", "Сбалансированный"),
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
    level: str | None = None,
    limit: int = 200
):
    return JSONResponse(get_logs(limit=limit, ticker=ticker, event_type=event_type, date_from=date_from, date_to=date_to, level=level))


@app.get("/api/история/торговля")
def api_trade_history(
    ticker: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200
):
    return JSONResponse(get_trades(limit=limit, ticker=ticker, date_from=date_from, date_to=date_to))


@app.get("/api/история/система")
def api_system_history(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200
):
    return JSONResponse(get_system_logs(limit=limit, date_from=date_from, date_to=date_to))


@app.get("/api/история/ошибки")
def api_error_history(
    ticker: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200
):
    return JSONResponse(get_error_logs(limit=limit, ticker=ticker, date_from=date_from, date_to=date_to))


@app.get("/api/instruments")
def api_instruments():
    return JSONResponse(list_instruments())


@app.get("/api/позиции")
def api_positions():
    return JSONResponse(get_open_positions())


@app.get("/api/портфель")
def api_portfolio():
    with get_client() as client:
        portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
        positions = []
        for p in getattr(portfolio, "positions", []) or []:
            positions.append({
                "figi": getattr(p, "figi", ""),
                "ticker": getattr(p, "ticker", ""),
                "quantity": str(getattr(p, "quantity", "")),
                "average_position_price": str(getattr(p, "average_position_price", "")),
                "current_price": str(getattr(p, "current_price", "")),
                "expected_yield": str(getattr(p, "expected_yield", "")),
                "instrument_type": getattr(p, "instrument_type", ""),
            })
        return JSONResponse({
            "positions": positions,
            "total_amount_portfolio": str(getattr(portfolio, "total_amount_portfolio", "")),
            "total_amount_shares": str(getattr(portfolio, "total_amount_shares", "")),
            "total_amount_currencies": str(getattr(portfolio, "total_amount_currencies", "")),
        })


@app.get("/api/стоп-заявки")
def api_stop_orders():
    with get_client() as client:
        orders = get_stop_orders(client, settings.TINVEST_ACCOUNT_ID)
        result = []
        for x in orders:
            result.append({
                "stop_order_id": getattr(x, "stop_order_id", ""),
                "figi": getattr(x, "figi", ""),
                "quantity": getattr(x, "lots_requested", 0),
                "currency": getattr(x, "currency", ""),
                "order_type": str(getattr(x, "stop_order_type", "")),
                "direction": str(getattr(x, "direction", "")),
            })
        return JSONResponse(result)


@app.get("/api/настройки")
def api_settings():
    return JSONResponse(get_all_settings())


@app.get("/api/профили")
def api_profiles():
    return JSONResponse(list_settings_profiles())


@app.get("/api/стратегии")
def api_strategies():
    return JSONResponse(list_strategy_profiles())


@app.get("/api/котировки")
def api_quotes():
    return JSONResponse(get_instrument_market_state())


@app.get("/api/instruments/search")
def api_instruments_search(q: str = Query("", min_length=0)):
    with get_client() as client:
        if q.strip():
            items = find_instruments(client, q.strip())
            for x in items:
                x["использовать"] = False
            return JSONResponse(items)

        popular = []
        for ticker in get_popular_tickers():
            found = find_instruments(client, ticker)
            if found:
                item = found[0]
                item["использовать"] = False
                popular.append(item)

        figis = [x["figi"] for x in popular if x.get("figi")]
        prices = get_last_prices_for_figis(client, figis)

        for item in popular:
            px = prices.get(item["figi"], {})
            item["last_price"] = px.get("last_price", "")
            item["price_time"] = px.get("price_time", "")

        return JSONResponse(popular)


@app.post("/api/instruments/add")
async def api_instruments_add(request: Request):
    data = await request.json()
    items = data.get("items", [])

    default_sl = get_setting("default_stop_loss_pct", "0.0025")
    default_tp = get_setting("default_take_profit_pct", "0.005")

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
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    log_event("CONFIG_CHANGED", f"Удалён инструмент figi={figi}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/профили/создать")
def api_create_profile(profile_name: str = Form(...)):
    create_settings_profile(profile_name)
    save_current_settings_to_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Создан профиль настроек {profile_name}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/профили/активировать")
def api_activate_profile(profile_name: str = Form(...)):
    activate_settings_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Активирован профиль настроек {profile_name}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/стратегии/активировать")
def api_activate_strategy(strategy_name: str = Form(...)):
    activate_strategy_profile(strategy_name)
    log_event("CONFIG_CHANGED", f"Активирована стратегия торговли {strategy_name}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/стратегии/сохранить")
def api_save_strategy(strategy_name: str = Form(...)):
    save_current_settings_to_strategy(strategy_name)
    log_event("CONFIG_CHANGED", f"Сохранена стратегия торговли {strategy_name}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/control/{action}")
def api_control(action: str):
    result = run_control(action)
    return JSONResponse(result)


@app.post("/api/позиции/закрыть")
def api_close_position(figi: str = Form(...), qty: int = Form(...), direction: str = Form(...)):
    with get_client() as client:
        response = close_position_market(client, figi, qty, direction)
    log_event("ORDER_CLOSE_MANUAL", f"Ручное закрытие позиции figi={figi}")
    return RedirectResponse(url="/dashboard?tab=портфель", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=портфель", status_code=303)


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
    return RedirectResponse(url="/dashboard?tab=портфель", status_code=303)


@app.post("/api/стоп-заявки/отменить")
def api_cancel_stop_order(stop_order_id: str = Form(...)):
    with get_client() as client:
        cancel_stop_order(client, settings.TINVEST_ACCOUNT_ID, stop_order_id)
    log_event("CONFIG_CHANGED", f"Отменена стоп-заявка {stop_order_id}")
    return RedirectResponse(url="/dashboard?tab=портфель", status_code=303)


def yesno(name: str, value) -> str:
    value = str(value)
    return f'''
    <select name="{name}" class="поле">
        <option value="1" {"selected" if value == "1" else ""}>Да</option>
        <option value="0" {"selected" if value == "0" else ""}>Нет</option>
    </select>
    '''


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(tab: str = "главное"):
    settings_map = get_all_settings()
    main_state = {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": get_runtime("daily_pnl", "0"),
        "trades_today": get_runtime("trades_today", "0"),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": get_runtime("session_balance_start", "0"),
        "session_balance_current": get_runtime("session_balance_current", "0"),
        "bot_enabled": get_setting("bot_enabled", "1"),
        "active_profile_name": get_setting("active_profile_name", "Основной"),
        "active_strategy_name": get_setting("active_strategy_name", "Сбалансированный"),
    }

    today_prefix = datetime.now().strftime("%Y-%m-%d")
    trade_stats_today = get_trade_stats_today(today_prefix)

    instruments = list_instruments()
    trades = get_trades(limit=100)
    open_positions = get_open_positions()
    quotes = get_instrument_market_state()
    quote_map = {x["figi"]: x for x in quotes}

    profiles = list_settings_profiles()
    strategies = list_strategy_profiles()
    system_logs = get_system_logs(limit=100)
    error_logs = get_error_logs(limit=100)
    common_logs = get_logs(limit=100)

    portfolio_positions = []
    stop_orders = []
    try:
        with get_client() as client:
            portfolio = client.operations.get_portfolio(account_id=settings.TINVEST_ACCOUNT_ID)
            for p in getattr(portfolio, "positions", []) or []:
                portfolio_positions.append({
                    "figi": getattr(p, "figi", ""),
                    "ticker": getattr(p, "ticker", "") or getattr(p, "figi", ""),
                    "instrument_type": getattr(p, "instrument_type", ""),
                    "quantity": str(getattr(p, "quantity", "")),
                    "average_position_price": str(getattr(p, "average_position_price", "")),
                    "current_price": str(getattr(p, "current_price", "")),
                    "expected_yield": str(getattr(p, "expected_yield", "")),
                })

            for x in get_stop_orders(client, settings.TINVEST_ACCOUNT_ID):
                stop_orders.append({
                    "stop_order_id": getattr(x, "stop_order_id", ""),
                    "figi": getattr(x, "figi", ""),
                    "quantity": getattr(x, "lots_requested", 0),
                    "currency": getattr(x, "currency", ""),
                    "order_type": str(getattr(x, "stop_order_type", "")),
                    "direction": str(getattr(x, "direction", "")),
                })
    except Exception as e:
        log_event("BOT_ERROR", f"Не удалось загрузить портфель/стоп-заявки: {e}", level="ERROR")

    nav = f"""
    <nav class="вкладки">
        <a class="вкладка {'активная' if tab == 'главное' else ''}" href="/dashboard?tab=главное">Главное</a>
        <a class="вкладка {'активная' if tab == 'портфель' else ''}" href="/dashboard?tab=портфель">Портфель</a>
        <a class="вкладка {'активная' if tab == 'настройки' else ''}" href="/dashboard?tab=настройки">Настройки</a>
        <a class="вкладка {'активная' if tab == 'история' else ''}" href="/dashboard?tab=история">История и логи</a>
    </nav>
    """

    cards = f"""
    <section class="карточки">
        <div class="карточка"><div class="метка">Статус</div><div class="значение">{main_state['status']}</div></div>
        <div class="карточка"><div class="метка">Торговля</div><div class="значение">{'Включена' if main_state['bot_enabled'] == '1' else 'Выключена'}</div></div>
        <div class="карточка"><div class="метка">Сделок сегодня</div><div class="значение">{main_state['trades_today']}</div></div>
        <div class="карточка"><div class="метка">PNL за день</div><div class="значение">{main_state['daily_pnl']}</div></div>
        <div class="карточка"><div class="метка">Комиссии за день</div><div class="значение">{trade_stats_today['total_commission']}</div></div>
        <div class="карточка"><div class="метка">Баланс на старте</div><div class="значение">{main_state['session_balance_start']}</div></div>
        <div class="карточка"><div class="метка">Текущий баланс</div><div class="значение">{main_state['session_balance_current']}</div></div>
        <div class="карточка"><div class="метка">Профиль настроек</div><div class="значение">{main_state['active_profile_name']}</div></div>
        <div class="карточка"><div class="метка">Стратегия торговли</div><div class="значение">{main_state['active_strategy_name']}</div></div>
        <div class="карточка"><div class="метка">Последняя ошибка</div><div class="значение">{main_state['last_error'] or '-'}</div></div>
    </section>
    """

    инструмент_rows = ""
    for i in instruments:
        q = quote_map.get(i["figi"], {})
        инструмент_rows += f"""
        <tr>
            <td>{i['ticker']}</td>
            <td>{i['name']}</td>
            <td>{'Да' if i['enabled'] == 1 else 'Нет'}</td>
            <td>{i['lots_override']}</td>
            <td>{i['stop_loss_pct']}</td>
            <td>{i['take_profit_pct']}</td>
            <td>{q.get('last_price', '-')}</td>
            <td>{q.get('price_time', '-')}</td>
        </tr>
        """

    deals_rows = ""
    for t in trades[:30]:
        deals_rows += f"""
        <tr>
            <td>{t['time']}</td>
            <td>{t['ticker']}</td>
            <td>{t['direction']}</td>
            <td>{t['entry']}</td>
            <td>{t['exit']}</td>
            <td>{t['qty']}</td>
            <td>{t['pnl']}</td>
            <td>{t['reason']}</td>
        </tr>
        """

    positions_rows = ""
    for p in open_positions:
        positions_rows += f"""
        <tr>
            <td>{p['ticker']}</td>
            <td>{p['direction']}</td>
            <td>{p['qty']}</td>
            <td>{p['entry_price']}</td>
            <td>{p['current_price']}</td>
            <td>{p['unrealized_pnl']}</td>
            <td>{p['opened_at']}</td>
        </tr>
        """

    главное_html = f"""
    {cards}

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Управление торговлей и сервисом</h2>
        </div>
        <div class="ряд-кнопок">
            <button class="кнопка основная" onclick="serviceAction('start')">Запустить</button>
            <button class="кнопка" onclick="serviceAction('stop')">Остановить</button>
            <button class="кнопка" onclick="serviceAction('restart')">Перезапустить</button>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Выбранные инструменты</h2>
            <div class="подпись">Последняя известная цена и время обновления</div>
        </div>
        <div class="таблица-обёртка">
            <table class="таблица" id="таблицаИнструментовГлавная">
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
                <tbody>{инструмент_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="две-колонки">
        <div class="блок">
            <div class="блок-заголовок"><h2>Открытые позиции</h2></div>
            <div class="таблица-обёртка">
                <table class="таблица">
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
                    <tbody>{positions_rows}</tbody>
                </table>
            </div>
        </div>

        <div class="блок">
            <div class="блок-заголовок"><h2>Последние сделки</h2></div>
            <div class="таблица-обёртка">
                <table class="таблица">
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
                    <tbody>{deals_rows}</tbody>
                </table>
            </div>
        </div>
    </section>
    """

    system_settings_form = f"""
    <section class="блок">
        <div class="блок-заголовок"><h2>Общие системные настройки</h2></div>
        <form method="post" action="/api/settings/system" class="сетка-формы">
            <label>Торговля включена {yesno("bot_enabled", settings_map.get("bot_enabled", "1"))}</label>
            <label>Telegram только ошибки {yesno("telegram_errors_only", settings_map.get("telegram_errors_only", "0"))}</label>
            <label>Автоперечитывание настроек {yesno("auto_reload_settings", settings_map.get("auto_reload_settings", "1"))}</label>
            <div class="ряд-кнопок"><button class="кнопка основная" type="submit">Сохранить системные настройки</button></div>
        </form>
    </section>
    """

    strategy_form = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>Стратегия торговли</h2>
            <div class="подпись">Параметры, которые реально влияют на торговую логику</div>
        </div>

        <form method="post" action="/api/settings/strategy" class="сетка-формы">
            <label>Макс. сделок в день <input class="поле" type="text" name="max_trades_per_day" value="{settings_map.get('max_trades_per_day', '15')}"></label>
            <label>Макс. дневной убыток <input class="поле" type="text" name="max_daily_loss_rub" value="{settings_map.get('max_daily_loss_rub', '250')}"></label>
            <label>Макс. открытых позиций <input class="поле" type="text" name="max_open_positions" value="{settings_map.get('max_open_positions', '2')}"></label>
            <label>Интервал проверки, сек <input class="поле" type="text" name="check_interval_sec" value="{settings_map.get('check_interval_sec', '5')}"></label>
            <label>Стоп-лосс по умолчанию <input class="поле" type="text" name="default_stop_loss_pct" value="{settings_map.get('default_stop_loss_pct', '0.0025')}"></label>
            <label>Тейк-профит по умолчанию <input class="поле" type="text" name="default_take_profit_pct" value="{settings_map.get('default_take_profit_pct', '0.005')}"></label>
            <label>Оценка комиссии <input class="поле" type="text" name="estimated_commission_pct" value="{settings_map.get('estimated_commission_pct', '0.0004')}"></label>
            <label>Разрешить Long {yesno("allow_long_global", settings_map.get("allow_long_global", "1"))}</label>
            <label>Разрешить Short {yesno("allow_short_global", settings_map.get("allow_short_global", "1"))}</label>
            <label>Только торговая сессия {yesno("trade_only_session", settings_map.get("trade_only_session", "1"))}</label>
            <label>Пауза после ошибки, сек <input class="поле" type="text" name="pause_after_error_sec" value="{settings_map.get('pause_after_error_sec', '10')}"></label>

            <div class="ряд-кнопок">
                <button class="кнопка основная" type="submit">Сохранить стратегию</button>
            </div>
        </form>
    </section>
    """

    strategy_presets_rows = ""
    for s in strategies:
        strategy_presets_rows += f"""
        <tr>
            <td>{s['strategy_name']}</td>
            <td>{'Да' if s['is_active'] == 1 else 'Нет'}</td>
            <td>{s['created_at']}</td>
            <td>
                <form method="post" action="/api/стратегии/активировать">
                    <input type="hidden" name="strategy_name" value="{s['strategy_name']}">
                    <button type="submit" class="кнопка малая">Активировать</button>
                </form>
            </td>
            <td>
                <form method="post" action="/api/стратегии/сохранить">
                    <input type="hidden" name="strategy_name" value="{s['strategy_name']}">
                    <button type="submit" class="кнопка малая">Перезаписать</button>
                </form>
            </td>
        </tr>
        """

    profile_rows = ""
    for p in profiles:
        profile_rows += f"""
        <tr>
            <td>{p['profile_name']}</td>
            <td>{'Да' if p['is_active'] == 1 else 'Нет'}</td>
            <td>{p['created_at']}</td>
            <td>
                <form method="post" action="/api/профили/активировать">
                    <input type="hidden" name="profile_name" value="{p['profile_name']}">
                    <button type="submit" class="кнопка малая">Активировать</button>
                </form>
            </td>
        </tr>
        """

    instrument_forms = ""
    for i in instruments:
        instrument_forms += f"""
        <form method="post" action="/api/instruments/update" class="карточка карточка-инструмент">
            <input type="hidden" name="figi" value="{i['figi']}">
            <div class="заголовок-инструмента">
                <div>
                    <div class="инструмент-текст">{i['ticker']} — {i['name']}</div>
                    <div class="подпись">FIGI: {i['figi']} | {i.get('instrument_type', '')} | {i.get('currency', '')}</div>
                </div>
            </div>

            <div class="сетка-формы">
                <label>Биржевой лот <input class="поле" type="text" value="{i['lot']}" readonly></label>
                <label>Шаг цены <input class="поле" type="text" value="{i['min_price_increment']}" readonly></label>
                <label>Лотов бота <input class="поле" type="number" name="lots_override" value="{i['lots_override']}"></label>
                <label>Стоп-лосс % <input class="поле" type="text" name="stop_loss_pct" value="{i['stop_loss_pct']}"></label>
                <label>Тейк-профит % <input class="поле" type="text" name="take_profit_pct" value="{i['take_profit_pct']}"></label>
                <label>Макс. спред % <input class="поле" type="text" name="max_spread_pct" value="{i.get('max_spread_pct', '0')}"></label>
                <label>Мин. объём 1м <input class="поле" type="number" name="min_volume" value="{i.get('min_volume', 0)}"></label>
                <label>Разрешить Long {yesno("allow_long", i.get("allow_long", 1))}</label>
                <label>Разрешить Short {yesno("allow_short", i.get("allow_short", 1))}</label>
                <label>Приоритет <input class="поле" type="number" name="priority" value="{i.get('priority', 100)}"></label>
                <label>Использовать {yesno("enabled", i['enabled'])}</label>
            </div>

            <div class="ряд-кнопок">
                <button type="submit" class="кнопка основная">Сохранить</button>
            </div>
        </form>

        <form method="post" action="/api/instruments/delete" class="карточка карточка-удаление">
            <input type="hidden" name="figi" value="{i['figi']}">
            <button type="submit" class="кнопка опасная">Удалить {i['ticker']}</button>
        </form>
        """

    settings_html = f"""
    {system_settings_form}
    {strategy_form}

    <section class="блок">
        <div class="блок-заголовок"><h2>Пресеты стратегий торговли</h2></div>
        <div class="таблица-обёртка">
            <table class="таблица">
                <thead>
                    <tr>
                        <th>Название</th>
                        <th>Активна</th>
                        <th>Создана</th>
                        <th>Выбрать</th>
                        <th>Сохранить в пресет</th>
                    </tr>
                </thead>
                <tbody>{strategy_presets_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок"><h2>Профили настроек</h2></div>
        <div class="ряд-кнопок">
            <form method="post" action="/api/профили/создать" class="inline-form">
                <input class="поле" type="text" name="profile_name" placeholder="Имя нового профиля" required>
                <button class="кнопка" type="submit">Создать профиль</button>
            </form>
        </div>
        <div class="таблица-обёртка">
            <table class="таблица">
                <thead>
                    <tr>
                        <th>Название</th>
                        <th>Активен</th>
                        <th>Создан</th>
                        <th>Выбрать</th>
                    </tr>
                </thead>
                <tbody>{profile_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Инструменты</h2>
            <div class="подпись">Добавление, редактирование и удаление инструментов</div>
        </div>
        <div class="ряд-кнопок">
            <button class="кнопка основная" onclick="openAddInstrumentModal()">Добавить инструмент</button>
        </div>
        {instrument_forms}
    </section>
    """

    portfolio_rows = ""
    for p in portfolio_positions:
        portfolio_rows += f"""
        <tr>
            <td>{p['ticker']}</td>
            <td>{p['figi']}</td>
            <td>{p['instrument_type']}</td>
            <td>{p['quantity']}</td>
            <td>{p['average_position_price']}</td>
            <td>{p['current_price']}</td>
            <td>{p['expected_yield']}</td>
        </tr>
        """

    close_rows = ""
    for p in open_positions:
        close_rows += f"""
        <tr>
            <td>{p['ticker']}</td>
            <td>{p['figi']}</td>
            <td>{p['direction']}</td>
            <td>{p['qty']}</td>
            <td>{p['entry_price']}</td>
            <td>{p['current_price']}</td>
            <td>{p['unrealized_pnl']}</td>
            <td>
                <form method="post" action="/api/позиции/закрыть">
                    <input type="hidden" name="figi" value="{p['figi']}">
                    <input type="hidden" name="qty" value="{p['qty']}">
                    <input type="hidden" name="direction" value="{p['direction']}">
                    <button type="submit" class="кнопка опасная">Закрыть</button>
                </form>
            </td>
        </tr>
        """

    stop_orders_rows = ""
    for s in stop_orders:
        stop_orders_rows += f"""
        <tr>
            <td>{s['stop_order_id']}</td>
            <td>{s['figi']}</td>
            <td>{s['quantity']}</td>
            <td>{s['currency']}</td>
            <td>{s['order_type']}</td>
            <td>{s['direction']}</td>
            <td>
                <form method="post" action="/api/стоп-заявки/отменить">
                    <input type="hidden" name="stop_order_id" value="{s['stop_order_id']}">
                    <button type="submit" class="кнопка опасная">Отменить</button>
                </form>
            </td>
        </tr>
        """

    stop_create_options = ""
    for p in open_positions:
        stop_create_options += f"""
        <option value="{p['figi']}|{p['qty']}|{p['direction']}|{p['entry_price']}">{p['ticker']} | {p['figi']}</option>
        """

    portfolio_html = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>Портфель по счёту</h2>
            <div class="подпись">Реальные бумаги и позиции со счёта через API</div>
        </div>
        <div class="таблица-обёртка">
            <table class="таблица фильтруемая" data-filter-target="фильтрПортфель">
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
                <tbody>{portfolio_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Открытые позиции бота</h2>
            <div class="ряд-кнопок">
                <form method="post" action="/api/позиции/закрыть-все">
                    <button type="submit" class="кнопка опасная">Закрыть все позиции</button>
                </form>
            </div>
        </div>
        <div class="таблица-обёртка">
            <table class="таблица">
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
                <tbody>{close_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="две-колонки">
        <div class="блок">
            <div class="блок-заголовок"><h2>Создать стоп-заявки</h2></div>
            <form method="post" action="/api/стоп-заявки/создать" class="сетка-формы" onsubmit="fillStopOrderFields()">
                <label>Позиция
                    <select class="поле" id="позицияДляСтопов">
                        {stop_create_options}
                    </select>
                </label>
                <input type="hidden" name="figi" id="stop_figi">
                <input type="hidden" name="qty" id="stop_qty">
                <input type="hidden" name="side" id="stop_side">
                <label>Базовая цена
                    <input class="поле" type="text" name="base_price" id="stop_base_price" required>
                </label>
                <label>Стоп-лосс %
                    <input class="поле" type="text" name="stop_loss_pct" value="0.0025">
                </label>
                <label>Тейк-профит %
                    <input class="поле" type="text" name="take_profit_pct" value="0.005">
                </label>
                <div class="ряд-кнопок"><button type="submit" class="кнопка основная">Создать стоп-заявки</button></div>
            </form>
        </div>

        <div class="блок">
            <div class="блок-заголовок"><h2>Активные стоп-заявки</h2></div>
            <div class="таблица-обёртка">
                <table class="таблица">
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
                    <tbody>{stop_orders_rows}</tbody>
                </table>
            </div>
        </div>
    </section>
    """

    common_logs_rows = ""
    for x in common_logs:
        common_logs_rows += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    system_logs_rows = ""
    for x in system_logs:
        system_logs_rows += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    error_logs_rows = ""
    for x in error_logs:
        error_logs_rows += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    trades_rows = ""
    for x in trades:
        trades_rows += f"""
        <tr>
            <td>{x['time']}</td>
            <td>{x['ticker']}</td>
            <td>{x['direction']}</td>
            <td>{x['entry']}</td>
            <td>{x['exit']}</td>
            <td>{x['qty']}</td>
            <td>{x['commission']}</td>
            <td>{x['pnl']}</td>
            <td>{x['reason']}</td>
        </tr>
        """

    history_html = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>Фильтры на странице</h2>
            <div class="подпись">Работают прямо во фронте без открытия JSON</div>
        </div>
        <div class="сетка-формы">
            <label>Фильтр торговой истории <input id="фильтрТоргов" class="поле" type="text" placeholder="Тикер, причина, направление..."></label>
            <label>Фильтр системной истории <input id="фильтрСистема" class="поле" type="text" placeholder="Тип события, текст..."></label>
            <label>Фильтр ошибок <input id="фильтрОшибки" class="поле" type="text" placeholder="Тикер, ошибка, текст..."></label>
            <label>Фильтр портфеля <input id="фильтрПортфель" class="поле" type="text" placeholder="Тикер, FIGI, тип..."></label>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок"><h2>Торговая история</h2></div>
        <div class="таблица-обёртка">
            <table class="таблица фильтруемая" data-filter-target="фильтрТоргов">
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
                <tbody>{trades_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок"><h2>Системная история</h2></div>
        <div class="таблица-обёртка">
            <table class="таблица фильтруемая" data-filter-target="фильтрСистема">
                <thead>
                    <tr>
                        <th>Время</th>
                        <th>Событие</th>
                        <th>Тикер</th>
                        <th>Уровень</th>
                        <th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>{system_logs_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок"><h2>Ошибки</h2></div>
        <div class="таблица-обёртка">
            <table class="таблица фильтруемая" data-filter-target="фильтрОшибки">
                <thead>
                    <tr>
                        <th>Время</th>
                        <th>Событие</th>
                        <th>Тикер</th>
                        <th>Уровень</th>
                        <th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>{error_logs_rows}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок"><h2>Общий журнал</h2></div>
        <div class="таблица-обёртка">
            <table class="таблица">
                <thead>
                    <tr>
                        <th>Время</th>
                        <th>Событие</th>
                        <th>Тикер</th>
                        <th>Уровень</th>
                        <th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>{common_logs_rows}</tbody>
            </table>
        </div>
    </section>
    """

    if tab == "главное":
        content = главное_html
    elif tab == "портфель":
        content = portfolio_html
    elif tab == "настройки":
        content = settings_html
    else:
        content = history_html

    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Панель управления торговым ботом v3.5</title>
        <style>
            :root {{
                --bg: #0b1020;
                --surface: #121933;
                --surface-2: #1a2345;
                --text: #edf2ff;
                --muted: #9fb0d3;
                --border: #2a3868;
                --primary: #4c8dff;
                --primary-2: #7aa8ff;
                --danger: #d95468;
                --success: #2fbf71;
                --warning: #f0b34a;
                --shadow: 0 10px 30px rgba(0,0,0,0.25);
                --radius: 16px;
            }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0;
                font-family: Inter, Arial, sans-serif;
                background: linear-gradient(180deg, #0b1020 0%, #0f1630 100%);
                color: var(--text);
            }}
            .контейнер {{
                max-width: 1580px;
                margin: 0 auto;
                padding: 24px;
            }}
            .шапка {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 16px;
                margin-bottom: 20px;
                flex-wrap: wrap;
            }}
            .заголовок {{
                font-size: 30px;
                font-weight: 800;
            }}
            .подзаголовок {{
                color: var(--muted);
                margin-top: 6px;
            }}
            .вкладки {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-bottom: 20px;
            }}
            .вкладка {{
                text-decoration: none;
                color: var(--text);
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 999px;
                padding: 10px 16px;
                transition: 0.2s;
            }}
            .вкладка:hover {{ background: var(--surface-2); }}
            .вкладка.активная {{
                background: var(--primary);
                border-color: var(--primary);
                color: white;
            }}
            .карточки {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 14px;
                margin-bottom: 20px;
            }}
            .карточка, .блок {{
                background: rgba(18, 25, 51, 0.95);
                border: 1px solid var(--border);
                border-radius: var(--radius);
                box-shadow: var(--shadow);
            }}
            .карточка {{
                padding: 18px;
            }}
            .метка {{
                color: var(--muted);
                font-size: 13px;
                margin-bottom: 8px;
            }}
            .значение {{
                font-size: 24px;
                font-weight: 700;
                line-height: 1.2;
                word-break: break-word;
            }}
            .блок {{
                padding: 20px;
                margin-bottom: 20px;
            }}
            .блок-заголовок {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 16px;
                flex-wrap: wrap;
            }}
            .блок h2 {{
                margin: 0;
                font-size: 22px;
            }}
            .подпись {{
                color: var(--muted);
                font-size: 14px;
            }}
            .ряд-кнопок {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-top: 10px;
            }}
            .кнопка, .кнопка.малая, button {{
                border: none;
                border-radius: 12px;
                padding: 10px 14px;
                font-weight: 700;
                cursor: pointer;
                background: var(--surface-2);
                color: var(--text);
            }}
            .кнопка.основная {{
                background: var(--primary);
                color: white;
            }}
            .кнопка.опасная {{
                background: var(--danger);
                color: white;
            }}
            .кнопка.малая {{
                padding: 8px 12px;
                font-size: 13px;
            }}
            .две-колонки {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }}
            .сетка-формы {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 14px;
            }}
            label {{
                display: flex;
                flex-direction: column;
                gap: 8px;
                font-size: 14px;
                color: var(--muted);
            }}
            .поле, input, select {{
                width: 100%;
                padding: 11px 12px;
                border-radius: 12px;
                border: 1px solid var(--border);
                background: #0f1630;
                color: var(--text);
            }}
            .таблица-обёртка {{
                overflow-x: auto;
            }}
            .таблица {{
                width: 100%;
                border-collapse: collapse;
            }}
            .таблица th, .таблица td {{
                text-align: left;
                padding: 12px 10px;
                border-bottom: 1px solid var(--border);
                vertical-align: top;
                font-size: 14px;
            }}
            .таблица th {{
                color: var(--primary-2);
                position: sticky;
                top: 0;
                background: rgba(18, 25, 51, 1);
            }}
            .карточка-инструмент {{
                margin-bottom: 14px;
                padding: 18px;
            }}
            .карточка-удаление {{
                margin-top: -6px;
                margin-bottom: 14px;
                padding: 14px 18px;
            }}
            .заголовок-инструмента {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                flex-wrap: wrap;
                margin-bottom: 14px;
            }}
            .инструмент-текст {{
                font-size: 18px;
                font-weight: 700;
            }}
            .inline-form {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                align-items: center;
            }}
            .модалка-фон {{
                position: fixed;
                inset: 0;
                background: rgba(5, 9, 20, 0.72);
                display: none;
                align-items: center;
                justify-content: center;
                padding: 20px;
                z-index: 999;
            }}
            .модалка {{
                width: min(1280px, 96vw);
                max-height: 90vh;
                overflow: auto;
                background: #0f1630;
                border: 1px solid var(--border);
                border-radius: 20px;
                padding: 20px;
                box-shadow: var(--shadow);
            }}
            .модалка-верх {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                margin-bottom: 16px;
            }}
            .модалка h3 {{
                margin: 0;
                font-size: 22px;
            }}
            .hint {{
                color: var(--muted);
                font-size: 13px;
            }}
            .success {{
                color: var(--success);
            }}
            .warning {{
                color: var(--warning);
            }}
            .danger {{
                color: var(--danger);
            }}
            @media (max-width: 980px) {{
                .две-колонки {{
                    grid-template-columns: 1fr;
                }}
                .значение {{
                    font-size: 20px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="контейнер">
            <div class="шапка">
                <div>
                    <div class="заголовок">Панель управления торговым ботом v3.5</div>
                    <div class="подзаголовок">Русский интерфейс, портфель, стратегии торговли, стоп-заявки, автообновление</div>
                </div>
                <div class="hint">Автообновление: каждые 7 секунд</div>
            </div>

            {nav}
            {content}
        </div>

        <div class="модалка-фон" id="modalAddInstrument">
            <div class="модалка">
                <div class="модалка-верх">
                    <div>
                        <h3>Добавление инструментов</h3>
                        <div class="hint">Введите идентификатор, например: SBER, SMLT, GAZP. Ниже также предложены 20 популярных тикеров. Поле «Использовать» по умолчанию = Ложь.</div>
                    </div>
                    <button class="кнопка" onclick="closeAddInstrumentModal()">Закрыть</button>
                </div>

                <div class="сетка-формы">
                    <label>Идентификатор:
                        <input class="поле" type="text" id="instrumentSearchInput" placeholder="SBER, SMLT, GAZP...">
                    </label>
                </div>

                <div class="ряд-кнопок">
                    <button class="кнопка основная" onclick="searchInstruments()">Поиск</button>
                    <button class="кнопка" onclick="loadPopularInstruments()">Показать популярные</button>
                    <button class="кнопка success" onclick="acceptSelectedInstruments()">Принять</button>
                </div>

                <div class="таблица-обёртка" style="margin-top:16px;">
                    <table class="таблица" id="instrumentSearchTable">
                        <thead>
                            <tr>
                                <th>Использовать</th>
                                <th>Инструмент</th>
                                <th>FIGI</th>
                                <th>Тип</th>
                                <th>Валюта</th>
                                <th>Лот</th>
                                <th>Шаг цены</th>
                                <th>Последняя цена</th>
                                <th>Время цены</th>
                            </tr>
                        </thead>
                        <tbody id="instrumentSearchBody"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            function serviceAction(action) {{
                fetch(`/api/control/${{action}}`, {{ method: 'POST' }})
                    .then(r => r.json())
                    .then(data => {{
                        alert('Результат: ' + (data.ok ? 'успешно' : 'ошибка'));
                        setTimeout(() => location.reload(), 1200);
                    }})
                    .catch(err => alert('Ошибка: ' + err));
            }}

            function openAddInstrumentModal() {{
                document.getElementById('modalAddInstrument').style.display = 'flex';
                loadPopularInstruments();
            }}

            function closeAddInstrumentModal() {{
                document.getElementById('modalAddInstrument').style.display = 'none';
            }}

            function renderInstrumentRows(items) {{
                const body = document.getElementById('instrumentSearchBody');
                body.innerHTML = '';

                for (const item of items) {{
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><input type="checkbox" data-role="use" ${'{'}item['использовать'] ? 'checked' : ''${'}'}></td>
                        <td>${'{'}item.ticker || ''${'}'} — ${'{'}item.name || ''${'}'}</td>
                        <td>${'{'}item.figi || ''${'}'}</td>
                        <td>${'{'}item.instrument_type || ''${'}'}</td>
                        <td>${'{'}item.currency || ''${'}'}</td>
                        <td>${'{'}item.lot || ''${'}'}</td>
                        <td>${'{'}item.min_price_increment || ''${'}'}</td>
                        <td>${'{'}item.last_price || ''${'}'}</td>
                        <td>${'{'}item.price_time || ''${'}'}</td>
                    `;
                    tr.dataset.item = JSON.stringify(item);
                    body.appendChild(tr);
                }}
            }}

            function searchInstruments() {{
                const q = document.getElementById('instrumentSearchInput').value.trim();
                fetch('/api/instruments/search?q=' + encodeURIComponent(q))
                    .then(r => r.json())
                    .then(data => renderInstrumentRows(data))
                    .catch(err => alert('Ошибка поиска: ' + err));
            }}

            function loadPopularInstruments() {{
                fetch('/api/instruments/search')
                    .then(r => r.json())
                    .then(data => renderInstrumentRows(data))
                    .catch(err => alert('Ошибка загрузки популярных тикеров: ' + err));
            }}

            function acceptSelectedInstruments() {{
                const rows = Array.from(document.querySelectorAll('#instrumentSearchBody tr'));
                const items = rows.map(row => {{
                    const item = JSON.parse(row.dataset.item);
                    item['использовать'] = row.querySelector('[data-role="use"]').checked;
                    return item;
                }});

                fetch('/api/instruments/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ items }})
                }})
                .then(r => r.json())
                .then(data => {{
                    alert('Добавлено: ' + data['добавлено']);
                    closeAddInstrumentModal();
                    location.reload();
                }})
                .catch(err => alert('Ошибка сохранения: ' + err));
            }}

            function fillStopOrderFields() {{
                const value = document.getElementById('позицияДляСтопов').value;
                if (!value) return;
                const parts = value.split('|');
                document.getElementById('stop_figi').value = parts[0] || '';
                document.getElementById('stop_qty').value = parts[1] || '';
                document.getElementById('stop_side').value = parts[2] || '';
                document.getElementById('stop_base_price').value = parts[3] || '';
            }}

            function attachTableFilters() {{
                const tables = document.querySelectorAll('.фильтруемая');
                tables.forEach(table => {{
                    const filterId = table.dataset.filterTarget;
                    const input = document.getElementById(filterId);
                    if (!input) return;

                    input.addEventListener('input', () => {{
                        const q = input.value.trim().toLowerCase();
                        const rows = table.querySelectorAll('tbody tr');
                        rows.forEach(row => {{
                            const text = row.innerText.toLowerCase();
                            row.style.display = !q || text.includes(q) ? '' : 'none';
                        }});
                    }});
                }});
            }}

            attachTableFilters();
            setInterval(() => {{
                location.reload();
            }}, 7000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)