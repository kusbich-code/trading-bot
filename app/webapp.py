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
    get_all_settings,
    get_all_runtime,
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
)
from app.instruments import find_instruments
from app.control import run_control

app = FastAPI(title="Панель управления торговым ботом")
init_db()


def get_client_cls():
    return SandboxClient if settings.TINVEST_USE_SANDBOX else Client


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/состояние")
def api_state():
    return {
        "статус": get_runtime("status", "UNKNOWN"),
        "дневной_pnl": get_runtime("daily_pnl", "0"),
        "сделок_сегодня": get_runtime("trades_today", "0"),
        "последняя_ошибка": get_runtime("last_error", ""),
        "баланс_на_старте": get_runtime("session_balance_start", "0"),
        "текущий_баланс": get_runtime("session_balance_current", "0"),
        "торговля_включена": get_setting("bot_enabled", "1"),
        "активный_профиль": get_setting("active_profile_name", "Основной"),
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


@app.get("/api/настройки")
def api_settings():
    return JSONResponse(get_all_settings())


@app.get("/api/профили")
def api_profiles():
    return JSONResponse(list_settings_profiles())


@app.get("/api/instruments/search")
def api_instruments_search(q: str = Query(..., min_length=1)):
    client_cls = get_client_cls()
    with client_cls(settings.TINVEST_TOKEN) as client:
        items = find_instruments(client, q)
    return JSONResponse(items)


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
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    log_event("CONFIG_CHANGED", f"Удалён инструмент figi={figi}")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/settings/update")
def api_settings_update(
    bot_enabled: str = Form(...),
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
    telegram_errors_only: str = Form(...),
    auto_reload_settings: str = Form(...),
):
    set_setting("bot_enabled", bot_enabled)
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
    set_setting("telegram_errors_only", telegram_errors_only)
    set_setting("auto_reload_settings", auto_reload_settings)

    log_event("CONFIG_CHANGED", "Обновлены общие настройки")
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


@app.post("/api/профили/сохранить")
def api_save_profile(profile_name: str = Form(...)):
    save_current_settings_to_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Сохранены текущие настройки в профиль {profile_name}")
    return RedirectResponse(url="/dashboard?tab=настройки", status_code=303)


@app.post("/api/control/{action}")
def api_control(action: str):
    result = run_control(action)
    return JSONResponse(result)


def render_boolean_select(name: str, value) -> str:
    value = str(value)
    return f"""
    <select name="{name}">
        <option value="1" {'selected' if value == '1' else ''}>Да</option>
        <option value="0" {'selected' if value == '0' else ''}>Нет</option>
    </select>
    """


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(tab: str = "главное"):
    state = {
        "status": get_runtime("status", "UNKNOWN"),
        "daily_pnl": get_runtime("daily_pnl", "0"),
        "trades_today": get_runtime("trades_today", "0"),
        "last_error": get_runtime("last_error", ""),
        "session_balance_start": get_runtime("session_balance_start", "0"),
        "session_balance_current": get_runtime("session_balance_current", "0"),
        "bot_enabled": get_setting("bot_enabled", "1"),
        "active_profile_name": get_setting("active_profile_name", "Основной"),
    }

    settings_map = get_all_settings()
    runtime_map = get_all_runtime()
    instruments = list_instruments()
    trades = get_trades(limit=50)
    open_positions = get_open_positions()
    logs = get_logs(limit=80)
    system_logs = get_system_logs(limit=80)
    error_logs = get_error_logs(limit=80)
    profiles = list_settings_profiles()

    nav = f"""
    <nav class="вкладки">
        <a class="вкладка {'активная' if tab == 'главное' else ''}" href="/dashboard?tab=главное">Главное</a>
        <a class="вкладка {'активная' if tab == 'настройки' else ''}" href="/dashboard?tab=настройки">Настройки</a>
        <a class="вкладка {'активная' if tab == 'история' else ''}" href="/dashboard?tab=история">История и логи</a>
    </nav>
    """

    cards = f"""
    <section class="карточки">
        <div class="карточка"><div class="метка">Статус</div><div class="значение">{state['status']}</div></div>
        <div class="карточка"><div class="метка">Торговля</div><div class="значение">{'Включена' if state['bot_enabled'] == '1' else 'Выключена'}</div></div>
        <div class="карточка"><div class="метка">Сделок сегодня</div><div class="значение">{state['trades_today']}</div></div>
        <div class="карточка"><div class="метка">Ежедневный PNL</div><div class="значение">{state['daily_pnl']}</div></div>
        <div class="карточка"><div class="метка">Баланс на старте</div><div class="значение">{state['session_balance_start']}</div></div>
        <div class="карточка"><div class="метка">Текущий баланс</div><div class="значение">{state['session_balance_current']}</div></div>
        <div class="карточка"><div class="метка">Активный профиль</div><div class="значение">{state['active_profile_name']}</div></div>
        <div class="карточка"><div class="метка">Последняя ошибка</div><div class="значение">{state['last_error'] or '-'}</div></div>
    </section>
    """

    позиции_html = ""
    for p in open_positions:
        позиции_html += f"""
        <tr>
            <td>{p['ticker']}</td>
            <td>{p['direction']}</td>
            <td>{p['qty']}</td>
            <td>{p['entry_price']}</td>
            <td>{p['current_price']}</td>
            <td>{p['unrealized_pnl']}</td>
            <td>{p['opened_at']}</td>
            <td>{p['status']}</td>
        </tr>
        """

    сделки_html = ""
    for t in trades:
        сделки_html += f"""
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

    инструменты_html = ""
    for i in instruments:
        инструменты_html += f"""
        <form method="post" action="/api/instruments/update" class="карточка карточка-инструмент">
            <input type="hidden" name="figi" value="{i['figi']}">
            <div class="заголовок-инструмента">
                <div>
                    <div class="инструмент-текст">{i['ticker']} — {i['name']}</div>
                    <div class="подпись">FIGI: {i['figi']}</div>
                </div>
                <div class="подпись">{i.get('instrument_type', '')} / {i.get('currency', '')}</div>
            </div>

            <div class="сетка-формы">
                <label>Биржевой лот
                    <input type="text" value="{i['lot']}" readonly>
                </label>
                <label>Шаг цены
                    <input type="text" value="{i['min_price_increment']}" readonly>
                </label>
                <label>Лотов бота
                    <input type="number" name="lots_override" value="{i['lots_override']}">
                </label>
                <label>Стоп-лосс %
                    <input type="text" name="stop_loss_pct" value="{i['stop_loss_pct']}">
                </label>
                <label>Тейк-профит %
                    <input type="text" name="take_profit_pct" value="{i['take_profit_pct']}">
                </label>
                <label>Макс. спред %
                    <input type="text" name="max_spread_pct" value="{i.get('max_spread_pct', '0')}">
                </label>
                <label>Мин. объём
                    <input type="number" name="min_volume" value="{i.get('min_volume', 0)}">
                </label>
                <label>Разрешить Long
                    {render_boolean_select("allow_long", i.get("allow_long", 1))}
                </label>
                <label>Разрешить Short
                    {render_boolean_select("allow_short", i.get("allow_short", 1))}
                </label>
                <label>Приоритет
                    <input type="number" name="priority" value="{i.get('priority', 100)}">
                </label>
                <label>Использовать
                    {render_boolean_select("enabled", i['enabled'])}
                </label>
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

    профили_html = ""
    for p in profiles:
        профили_html += f"""
        <tr>
            <td>{p['profile_name']}</td>
            <td>{'Да' if p['is_active'] == 1 else 'Нет'}</td>
            <td>{p['created_at']}</td>
            <td>
                <form method="post" action="/api/профили/активировать">
                    <input type="hidden" name="profile_name" value="{p['profile_name']}">
                    <button type="submit" class="кнопка малая">Сделать активным</button>
                </form>
            </td>
            <td>
                <form method="post" action="/api/профили/сохранить">
                    <input type="hidden" name="profile_name" value="{p['profile_name']}">
                    <button type="submit" class="кнопка малая">Сохранить текущие в профиль</button>
                </form>
            </td>
        </tr>
        """

    системные_логи_html = ""
    for x in system_logs:
        системные_логи_html += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    ошибки_html = ""
    for x in error_logs:
        ошибки_html += f"""
        <tr>
            <td>{x['event_time']}</td>
            <td>{x['event_type']}</td>
            <td>{x['ticker']}</td>
            <td>{x['level']}</td>
            <td>{x['message']}</td>
        </tr>
        """

    история_торгов_html = ""
    for x in trades:
        история_торгов_html += f"""
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

    главное = f"""
    {cards}

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Управление торговлей и сервисом</h2>
            <div class="подпись">Включение/выключение торговли и управление сервисом бота</div>
        </div>

        <div class="ряд-кнопок">
            <form method="post" action="/api/settings/update">
                <input type="hidden" name="bot_enabled" value="1">
                <input type="hidden" name="max_trades_per_day" value="{settings_map.get('max_trades_per_day', '15')}">
                <input type="hidden" name="max_daily_loss_rub" value="{settings_map.get('max_daily_loss_rub', '200')}">
                <input type="hidden" name="max_open_positions" value="{settings_map.get('max_open_positions', '2')}">
                <input type="hidden" name="check_interval_sec" value="{settings_map.get('check_interval_sec', '5')}">
                <input type="hidden" name="default_stop_loss_pct" value="{settings_map.get('default_stop_loss_pct', '0.0025')}">
                <input type="hidden" name="default_take_profit_pct" value="{settings_map.get('default_take_profit_pct', '0.005')}">
                <input type="hidden" name="estimated_commission_pct" value="{settings_map.get('estimated_commission_pct', '0.0004')}">
                <input type="hidden" name="allow_long_global" value="{settings_map.get('allow_long_global', '1')}">
                <input type="hidden" name="allow_short_global" value="{settings_map.get('allow_short_global', '1')}">
                <input type="hidden" name="trade_only_session" value="{settings_map.get('trade_only_session', '0')}">
                <input type="hidden" name="pause_after_error_sec" value="{settings_map.get('pause_after_error_sec', '10')}">
                <input type="hidden" name="telegram_errors_only" value="{settings_map.get('telegram_errors_only', '0')}">
                <input type="hidden" name="auto_reload_settings" value="{settings_map.get('auto_reload_settings', '1')}">
                <button type="submit" class="кнопка основная">Включить торговлю</button>
            </form>

            <form method="post" action="/api/settings/update">
                <input type="hidden" name="bot_enabled" value="0">
                <input type="hidden" name="max_trades_per_day" value="{settings_map.get('max_trades_per_day', '15')}">
                <input type="hidden" name="max_daily_loss_rub" value="{settings_map.get('max_daily_loss_rub', '200')}">
                <input type="hidden" name="max_open_positions" value="{settings_map.get('max_open_positions', '2')}">
                <input type="hidden" name="check_interval_sec" value="{settings_map.get('check_interval_sec', '5')}">
                <input type="hidden" name="default_stop_loss_pct" value="{settings_map.get('default_stop_loss_pct', '0.0025')}">
                <input type="hidden" name="default_take_profit_pct" value="{settings_map.get('default_take_profit_pct', '0.005')}">
                <input type="hidden" name="estimated_commission_pct" value="{settings_map.get('estimated_commission_pct', '0.0004')}">
                <input type="hidden" name="allow_long_global" value="{settings_map.get('allow_long_global', '1')}">
                <input type="hidden" name="allow_short_global" value="{settings_map.get('allow_short_global', '1')}">
                <input type="hidden" name="trade_only_session" value="{settings_map.get('trade_only_session', '0')}">
                <input type="hidden" name="pause_after_error_sec" value="{settings_map.get('pause_after_error_sec', '10')}">
                <input type="hidden" name="telegram_errors_only" value="{settings_map.get('telegram_errors_only', '0')}">
                <input type="hidden" name="auto_reload_settings" value="{settings_map.get('auto_reload_settings', '1')}">
                <button type="submit" class="кнопка вторичная">Выключить торговлю</button>
            </form>

            <button class="кнопка вторичная" onclick="управлениеСервисом('start')">Старт сервиса</button>
            <button class="кнопка вторичная" onclick="управлениеСервисом('stop')">Стоп сервиса</button>
            <button class="кнопка вторичная" onclick="управлениеСервисом('restart')">Перезапуск сервиса</button>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Бумаги в наличии / открытые позиции</h2>
            <div class="подпись">Текущие открытые позиции бота</div>
        </div>
        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Инструмент</th><th>Направление</th><th>Количество</th><th>Цена входа</th>
                        <th>Текущая цена</th><th>Нереализованный PNL</th><th>Открыта</th><th>Статус</th>
                    </tr>
                </thead>
                <tbody>{позиции_html or '<tr><td colspan="8">Открытых позиций нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Инструменты в работе</h2>
            <div class="подпись">Краткая информация о торгуемых инструментах и настройках</div>
        </div>
        <div class="список-инструментов">{инструменты_html or '<div class="карточка">Инструменты пока не добавлены</div>'}</div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Последние сделки</h2>
            <div class="подпись">Последние закрытые сделки</div>
        </div>
        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Инструмент</th><th>Направление</th><th>Вход</th><th>Выход</th>
                        <th>Количество</th><th>Комиссия</th><th>PNL</th><th>Причина</th>
                    </tr>
                </thead>
                <tbody>{сделки_html or '<tr><td colspan="9">Сделок пока нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>
    """

    настройки = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>Общие настройки</h2>
            <div class="подпись">Все изменения должны реально влиять на работу бота</div>
        </div>

        <form method="post" action="/api/settings/update" class="сетка-формы большая-форма">
            <label>Торговля включена
                {render_boolean_select("bot_enabled", settings_map.get("bot_enabled", "1"))}
            </label>
            <label>Максимум сделок в день
                <input type="text" name="max_trades_per_day" value="{settings_map.get('max_trades_per_day', '15')}">
            </label>
            <label>Максимальный дневной убыток, ₽
                <input type="text" name="max_daily_loss_rub" value="{settings_map.get('max_daily_loss_rub', '200')}">
            </label>
            <label>Максимум открытых позиций
                <input type="text" name="max_open_positions" value="{settings_map.get('max_open_positions', '2')}">
            </label>
            <label>Интервал опроса, сек
                <input type="text" name="check_interval_sec" value="{settings_map.get('check_interval_sec', '5')}">
            </label>
            <label>Стоп-лосс по умолчанию
                <input type="text" name="default_stop_loss_pct" value="{settings_map.get('default_stop_loss_pct', '0.0025')}">
            </label>
            <label>Тейк-профит по умолчанию
                <input type="text" name="default_take_profit_pct" value="{settings_map.get('default_take_profit_pct', '0.005')}">
            </label>
            <label>Оценочная комиссия
                <input type="text" name="estimated_commission_pct" value="{settings_map.get('estimated_commission_pct', '0.0004')}">
            </label>
            <label>Глобально разрешить Long
                {render_boolean_select("allow_long_global", settings_map.get("allow_long_global", "1"))}
            </label>
            <label>Глобально разрешить Short
                {render_boolean_select("allow_short_global", settings_map.get("allow_short_global", "1"))}
            </label>
            <label>Торговать только в сессию
                {render_boolean_select("trade_only_session", settings_map.get("trade_only_session", "0"))}
            </label>
            <label>Пауза после ошибки, сек
                <input type="text" name="pause_after_error_sec" value="{settings_map.get('pause_after_error_sec', '10')}">
            </label>
            <label>Telegram только по ошибкам
                {render_boolean_select("telegram_errors_only", settings_map.get("telegram_errors_only", "0"))}
            </label>
            <label>Автоперечитывание настроек
                {render_boolean_select("auto_reload_settings", settings_map.get("auto_reload_settings", "1"))}
            </label>

            <div class="ряд-кнопок">
                <button type="submit" class="кнопка основная">Сохранить общие настройки</button>
            </div>
        </form>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Профили настроек</h2>
            <div class="подпись">Можно сохранить текущий набор параметров под именем и потом быстро переключать</div>
        </div>

        <div class="сетка-2">
            <form method="post" action="/api/профили/создать" class="карточка">
                <label>Имя нового профиля
                    <input type="text" name="profile_name" placeholder="Например: Агрессивный режим">
                </label>
                <button type="submit" class="кнопка основная">Создать профиль из текущих настроек</button>
            </form>

            <div class="карточка">
                <div class="подпись">Активный профиль: <strong>{state['active_profile_name']}</strong></div>
            </div>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Профиль</th><th>Активный</th><th>Создан</th><th>Активировать</th><th>Пересохранить</th>
                    </tr>
                </thead>
                <tbody>{профили_html}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Работа с инструментами</h2>
            <div class="подпись">Список инструментов с настройками по инструменту, добавление через поиск</div>
        </div>

        <div class="ряд-кнопок">
            <button class="кнопка основная" onclick="открытьМодальноеОкно()">Добавить инструмент</button>
        </div>

        <div class="список-инструментов">{инструменты_html or '<div class="карточка">Инструменты пока не добавлены</div>'}</div>
    </section>
    """

    история = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>Торговая история</h2>
            <div class="подпись">Закрытые сделки</div>
        </div>

        <div class="фильтры">
            <input id="фильтрСделкиТикер" placeholder="Инструмент, например SBER">
            <input id="фильтрСделкиОт" placeholder="Дата от, например 2026-04-20 00:00:00">
            <input id="фильтрСделкиДо" placeholder="Дата до, например 2026-04-20 23:59:59">
            <button class="кнопка малая" onclick="открытьСделкиAPI()">Открыть через API</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Инструмент</th><th>Направление</th><th>Вход</th><th>Выход</th>
                        <th>Количество</th><th>Комиссия</th><th>PNL</th><th>Причина</th>
                    </tr>
                </thead>
                <tbody>{история_торгов_html or '<tr><td colspan="9">История сделок пока пуста</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Системная история</h2>
            <div class="подпись">Запуски, остановки, изменения конфигурации и служебные события</div>
        </div>

        <div class="фильтры">
            <input id="фильтрСистемаОт" placeholder="Дата от">
            <input id="фильтрСистемаДо" placeholder="Дата до">
            <button class="кнопка малая" onclick="открытьСистемнуюИсториюAPI()">Открыть через API</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr><th>Время</th><th>Тип события</th><th>Инструмент</th><th>Уровень</th><th>Сообщение</th></tr>
                </thead>
                <tbody>{системные_логи_html or '<tr><td colspan="5">Системная история пуста</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Ошибки</h2>
            <div class="подпись">Ошибки и исключения</div>
        </div>

        <div class="фильтры">
            <input id="фильтрОшибкаТикер" placeholder="Инструмент, например SBER">
            <input id="фильтрОшибкаОт" placeholder="Дата от">
            <input id="фильтрОшибкаДо" placeholder="Дата до">
            <button class="кнопка малая" onclick="открытьОшибкиAPI()">Открыть через API</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr><th>Время</th><th>Тип события</th><th>Инструмент</th><th>Уровень</th><th>Сообщение</th></tr>
                </thead>
                <tbody>{ошибки_html or '<tr><td colspan="5">Ошибок пока нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>
    """

    current_tab_html = главное
    if tab == "настройки":
        current_tab_html = настройки
    elif tab == "история":
        current_tab_html = история

    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Панель управления торговым ботом v3.4</title>
        <style>
            :root {{
                --фон: #0b1220;
                --панель: #131c2f;
                --панель-2: #19243b;
                --текст: #eef4ff;
                --подпись: #a3b3d9;
                --граница: #2a3958;
                --акцент: #5bb3ff;
                --акцент-2: #2dd4bf;
                --опасно: #ef4444;
                --успех: #22c55e;
                --тень: 0 16px 40px rgba(0,0,0,.25);
                --радиус: 18px;
            }}

            * {{ box-sizing: border-box; }}
            body {{
                margin: 0;
                background: linear-gradient(180deg, #08101d 0%, #0b1220 100%);
                color: var(--текст);
                font-family: Inter, Arial, sans-serif;
            }}
            .обёртка {{
                max-width: 1520px;
                margin: 0 auto;
                padding: 24px;
            }}
            .шапка {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 16px;
                margin-bottom: 18px;
            }}
            .заголовок {{
                font-size: 32px;
                font-weight: 800;
            }}
            .подзаголовок {{
                color: var(--подпись);
                margin-top: 6px;
            }}
            .вкладки {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-bottom: 22px;
            }}
            .вкладка {{
                text-decoration: none;
                color: var(--текст);
                padding: 12px 16px;
                border-radius: 999px;
                background: rgba(255,255,255,.04);
                border: 1px solid var(--граница);
            }}
            .вкладка.активная {{
                background: linear-gradient(90deg, rgba(91,179,255,.25), rgba(45,212,191,.20));
                border-color: rgba(91,179,255,.5);
            }}
            .карточки {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
                gap: 14px;
                margin-bottom: 20px;
            }}
            .карточка, .блок {{
                background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.02));
                border: 1px solid var(--граница);
                border-radius: var(--радиус);
                box-shadow: var(--тень);
            }}
            .карточка {{
                padding: 18px;
            }}
            .метка {{
                color: var(--подпись);
                font-size: 13px;
                margin-bottom: 8px;
            }}
            .значение {{
                font-size: 24px;
                font-weight: 800;
                word-break: break-word;
            }}
            .блок {{
                padding: 18px;
                margin-bottom: 18px;
            }}
            .блок-заголовок {{
                margin-bottom: 16px;
            }}
            .блок-заголовок h2 {{
                margin: 0 0 6px 0;
                font-size: 22px;
            }}
            .подпись {{
                color: var(--подпись);
                font-size: 14px;
            }}
            .ряд-кнопок {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                align-items: center;
            }}
            .кнопка, button {{
                border: 0;
                cursor: pointer;
                border-radius: 12px;
                padding: 11px 16px;
                font-weight: 700;
            }}
            .кнопка.основная, .основная {{
                background: linear-gradient(90deg, var(--акцент), #6ee7ff);
                color: #08101d;
            }}
            .кнопка.вторичная, .вторичная {{
                background: #243452;
                color: var(--текст);
                border: 1px solid var(--граница);
            }}
            .кнопка.опасная, .опасная {{
                background: rgba(239,68,68,.15);
                color: #ffd2d2;
                border: 1px solid rgba(239,68,68,.35);
            }}
            .кнопка.малая, .малая {{
                padding: 8px 12px;
                font-size: 13px;
            }}
            .сетка-2 {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 16px;
            }}
            .сетка-формы {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 14px;
            }}
            .большая-форма label,
            .сетка-формы label {{
                display: flex;
                flex-direction: column;
                gap: 8px;
                color: var(--подпись);
                font-size: 14px;
            }}
            input, select {{
                background: #0e1729;
                color: var(--текст);
                border: 1px solid var(--граница);
                border-radius: 12px;
                padding: 11px 12px;
                width: 100%;
            }}
            .таблица-обёртка {{
                overflow: auto;
                border: 1px solid var(--граница);
                border-radius: 16px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                min-width: 800px;
            }}
            th, td {{
                padding: 12px;
                border-bottom: 1px solid rgba(255,255,255,.06);
                text-align: left;
                vertical-align: top;
                font-size: 14px;
            }}
            th {{
                background: rgba(255,255,255,.03);
                color: #dce8ff;
                position: sticky;
                top: 0;
            }}
            .список-инструментов {{
                display: grid;
                grid-template-columns: 1fr;
                gap: 14px;
            }}
            .карточка-инструмент {{
                padding: 16px;
            }}
            .заголовок-инструмента {{
                display: flex;
                justify-content: space-between;
                gap: 12px;
                margin-bottom: 14px;
            }}
            .инструмент-текст {{
                font-size: 18px;
                font-weight: 800;
            }}
            .карточка-удаление {{
                padding: 16px;
                border-color: rgba(239,68,68,.25);
            }}
            .фильтры {{
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-bottom: 14px;
            }}

            .модалка-фон {{
                position: fixed;
                inset: 0;
                background: rgba(0,0,0,.6);
                display: none;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                padding: 16px;
            }}
            .модалка {{
                width: min(1100px, 100%);
                max-height: 90vh;
                overflow: auto;
                background: #101827;
                border: 1px solid var(--граница);
                border-radius: 22px;
                box-shadow: var(--тень);
                padding: 20px;
            }}
            .модалка-шапка {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 14px;
                gap: 12px;
            }}
            .модалка-заголовок {{
                font-size: 22px;
                font-weight: 800;
            }}

            .результаты-поиска {{
                margin-top: 16px;
                border: 1px solid var(--граница);
                border-radius: 16px;
                overflow: auto;
            }}
            .результаты-поиска table {{
                min-width: 980px;
            }}

            .служебная-плашка {{
                padding: 10px 14px;
                border-radius: 12px;
                background: rgba(45,212,191,.12);
                border: 1px solid rgba(45,212,191,.22);
                color: #ccfbf1;
                margin-bottom: 12px;
                display: none;
            }}

            @media (max-width: 900px) {{
                .сетка-2 {{
                    grid-template-columns: 1fr;
                }}
                .заголовок-инструмента {{
                    flex-direction: column;
                }}
                .обёртка {{
                    padding: 16px;
                }}
                .заголовок {{
                    font-size: 26px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="обёртка">
            <header class="шапка">
                <div>
                    <div class="заголовок">Панель управления торговым ботом v3.4</div>
                    <div class="подзаголовок">Полностью русский интерфейс, вкладки, профили настроек, история, логи и управление инструментами</div>
                </div>
            </header>

            {nav}
            <div id="служебноеСообщение" class="служебная-плашка"></div>

            {current_tab_html}
        </div>

        <div id="модальноеОкно" class="модалка-фон">
            <div class="модалка">
                <div class="модалка-шапка">
                    <div>
                        <div class="модалка-заголовок">Добавление инструментов</div>
                        <div class="подпись">Введите идентификатор, например SBER, SMLT, VTBR</div>
                    </div>
                    <button class="кнопка вторичная" onclick="закрытьМодальноеОкно()">Закрыть</button>
                </div>

                <div class="сетка-формы">
                    <label>Идентификатор:
                        <input id="поисковыйЗапрос" type="text" placeholder="Например: SBER">
                    </label>
                </div>

                <div class="ряд-кнопок" style="margin-top: 12px;">
                    <button class="кнопка основная" onclick="поискИнструмента()">Поиск</button>
                    <button class="кнопка вторичная" onclick="принятьИнструменты()">Принять</button>
                </div>

                <div class="результаты-поиска">
                    <table>
                        <thead>
                            <tr>
                                <th>Использовать</th>
                                <th>Инструмент</th>
                                <th>FIGI</th>
                                <th>Тип</th>
                                <th>Класс</th>
                                <th>Валюта</th>
                                <th>Лот</th>
                                <th>Шаг цены</th>
                                <th>Лотов бота</th>
                                <th>Стоп-лосс</th>
                                <th>Тейк-профит</th>
                            </tr>
                        </thead>
                        <tbody id="таблицаПоискаИнструментов">
                            <tr><td colspan="11">Результаты поиска пока отсутствуют</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            let найденныеИнструменты = [];

            function показатьСообщение(text) {{
                const el = document.getElementById('служебноеСообщение');
                el.style.display = 'block';
                el.textContent = text;
                setTimeout(() => {{
                    el.style.display = 'none';
                    el.textContent = '';
                }}, 4000);
            }}

            function открытьМодальноеОкно() {{
                document.getElementById('модальноеОкно').style.display = 'flex';
            }}

            function закрытьМодальноеОкно() {{
                document.getElementById('модальноеОкно').style.display = 'none';
            }}

            async function поискИнструмента() {{
                const q = document.getElementById('поисковыйЗапрос').value.trim();
                if (!q) {{
                    alert('Введите идентификатор инструмента');
                    return;
                }}

                const resp = await fetch(`/api/instruments/search?q=${{encodeURIComponent(q)}}`);
                const data = await resp.json();
                найденныеИнструменты = data || [];

                const body = document.getElementById('таблицаПоискаИнструментов');
                if (!найденныеИнструменты.length) {{
                    body.innerHTML = '<tr><td colspan="11">Ничего не найдено</td></tr>';
                    return;
                }}

                body.innerHTML = найденныеИнструменты.map((x, index) => `
                    <tr>
                        <td><input type="checkbox" id="использовать_${{index}}" checked></td>
                        <td>${{x.ticker || ''}} — ${{x.name || ''}}</td>
                        <td>${{x.figi || ''}}</td>
                        <td>${{x.instrument_type || ''}}</td>
                        <td>${{x.class_code || ''}}</td>
                        <td>${{x.currency || ''}}</td>
                        <td>${{x.lot || 1}}</td>
                        <td>${{x.min_price_increment || '0.01'}}</td>
                        <td><input type="number" id="lots_${{index}}" value="1" min="1"></td>
                        <td><input type="text" id="sl_${{index}}" value="${settings_map.get('default_stop_loss_pct', '0.0025')}"></td>
                        <td><input type="text" id="tp_${{index}}" value="${settings_map.get('default_take_profit_pct', '0.005')}"></td>
                    </tr>
                `).join('');
            }}

            async function принятьИнструменты() {{
                if (!найденныеИнструменты.length) {{
                    alert('Сначала выполните поиск');
                    return;
                }}

                const payload = найденныеИнструменты.map((x, index) => {{
                    return {{
                        использовать: document.getElementById(`использовать_${{index}}`)?.checked ? true : false,
                        ticker: x.ticker || '',
                        figi: x.figi || '',
                        name: x.name || '',
                        class_code: x.class_code || '',
                        instrument_type: x.instrument_type || '',
                        currency: x.currency || '',
                        lot: x.lot || 1,
                        min_price_increment: x.min_price_increment || '0.01',
                        lots_override: parseInt(document.getElementById(`lots_${{index}}`)?.value || '1'),
                        stop_loss_pct: document.getElementById(`sl_${{index}}`)?.value || '${settings_map.get('default_stop_loss_pct', '0.0025')}',
                        take_profit_pct: document.getElementById(`tp_${{index}}`)?.value || '${settings_map.get('default_take_profit_pct', '0.005')}'
                    }};
                }});

                const resp = await fetch('/api/instruments/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ items: payload }})
                }});

                const data = await resp.json();
                if (data.ok) {{
                    показатьСообщение(`Инструментов добавлено: ${{data.добавлено}}`);
                    закрытьМодальноеОкно();
                    setTimeout(() => window.location.href = '/dashboard?tab=настройки', 600);
                }} else {{
                    alert('Не удалось добавить инструменты');
                }}
            }}

            async function управлениеСервисом(action) {{
                const resp = await fetch(`/api/control/${{action}}`, {{ method: 'POST' }});
                const data = await resp.json();
                if (data.ok) {{
                    показатьСообщение(`Команда "${{action}}" выполнена успешно`);
                }} else {{
                    alert('Ошибка выполнения команды: ' + (data.message || data.stderr || 'неизвестная ошибка'));
                }}
            }}

            function открытьСделкиAPI() {{
                const ticker = document.getElementById('фильтрСделкиТикер').value.trim();
                const df = document.getElementById('фильтрСделкиОт').value.trim();
                const dt = document.getElementById('фильтрСделкиДо').value.trim();
                const url = `/api/история/торговля?ticker=${{encodeURIComponent(ticker)}}&date_from=${{encodeURIComponent(df)}}&date_to=${{encodeURIComponent(dt)}}`;
                window.open(url, '_blank');
            }}

            function открытьСистемнуюИсториюAPI() {{
                const df = document.getElementById('фильтрСистемаОт').value.trim();
                const dt = document.getElementById('фильтрСистемаДо').value.trim();
                const url = `/api/история/система?date_from=${{encodeURIComponent(df)}}&date_to=${{encodeURIComponent(dt)}}`;
                window.open(url, '_blank');
            }}

            function открытьОшибкиAPI() {{
                const ticker = document.getElementById('фильтрОшибкаТикер').value.trim();
                const df = document.getElementById('фильтрОшибкаОт').value.trim();
                const dt = document.getElementById('фильтрОшибкаДо').value.trim();
                const url = `/api/история/ошибки?ticker=${{encodeURIComponent(ticker)}}&date_from=${{encodeURIComponent(df)}}&date_to=${{encodeURIComponent(dt)}}`;
                window.open(url, '_blank');
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)