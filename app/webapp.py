from datetime import datetime

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
    get_trade_stats_today,
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
    return JSONResponse(
        get_logs(
            limit=limit,
            ticker=ticker,
            event_type=event_type,
            date_from=date_from,
            date_to=date_to,
            level=level,
        )
    )


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
    return JSONResponse({
        "bot": get_open_positions(source="BOT"),
        "portfolio": get_open_positions(source="PORTFOLIO"),
    })


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
    enabled: int = Form(...),
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
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


@app.post("/api/instruments/delete")
def api_instruments_delete(figi: str = Form(...)):
    delete_instrument(figi)
    log_event("CONFIG_CHANGED", f"Удалён инструмент figi={figi}")
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


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
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


@app.post("/api/профили/создать")
def api_create_profile(profile_name: str = Form(...)):
    create_settings_profile(profile_name)
    save_current_settings_to_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Создан профиль настроек {profile_name}")
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


@app.post("/api/профили/активировать")
def api_activate_profile(profile_name: str = Form(...)):
    activate_settings_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Активирован профиль настроек {profile_name}")
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


@app.post("/api/профили/сохранить")
def api_save_profile(profile_name: str = Form(...)):
    save_current_settings_to_profile(profile_name)
    log_event("CONFIG_CHANGED", f"Сохранены текущие настройки в профиль {profile_name}")
    return RedirectResponse(url="/dashboard#настройки", status_code=303)


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
def dashboard():
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
    open_positions = get_open_positions(source="BOT")
    portfolio_positions = get_open_positions(source="PORTFOLIO")
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    trade_stats_today = get_trade_stats_today(today_prefix)
    logs = get_logs(limit=80)
    system_logs = get_system_logs(limit=80)
    error_logs = get_error_logs(limit=80)
    profiles = list_settings_profiles()

    nav = """
    <nav class="вкладки">
        <button type="button" class="вкладка" onclick="показатьВкладку('главное')" id="tabbtn-главное">Главное</button>
        <button type="button" class="вкладка" onclick="показатьВкладку('настройки')" id="tabbtn-настройки">Настройки</button>
        <button type="button" class="вкладка" onclick="показатьВкладку('история')" id="tabbtn-история">История и логи</button>
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
        <div class="карточка"><div class="метка">Комиссии за сегодня</div><div class="значение">{trade_stats_today['total_commission']}</div></div>
        <div class="карточка"><div class="метка">Сделок по истории за сегодня</div><div class="значение">{trade_stats_today['trades_count']}</div></div>
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
            <td>{round(float(p['entry_price']), 4)}</td>
            <td>{round(float(p['current_price']), 4)}</td>
            <td>{round(float(p['unrealized_pnl']), 4)}</td>
            <td>{p['opened_at']}</td>
            <td>{p['status']}</td>
        </tr>
        """

    портфель_html = ""
    for p in portfolio_positions:
        портфель_html += f"""
        <tr>
            <td>{p['ticker']}</td>
            <td>{p['direction']}</td>
            <td>{p['qty']}</td>
            <td>{round(float(p['entry_price']), 4)}</td>
            <td>{round(float(p['current_price']), 4)}</td>
            <td>{round(float(p['unrealized_pnl']), 4)}</td>
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

    default_sl = settings_map.get("default_stop_loss_pct", "0.0025")
    default_tp = settings_map.get("default_take_profit_pct", "0.005")

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
            <button class="кнопка основная" onclick="управление('start')">Запустить</button>
            <button class="кнопка" onclick="управление('stop')">Остановить</button>
            <button class="кнопка" onclick="управление('restart')">Перезапустить</button>
            <button class="кнопка" onclick="управление('reload')">Перечитать конфиг</button>
        </div>

        <div id="служебноеСообщение" class="служебное-сообщение" style="display:none;"></div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Открытые позиции бота</h2>
            <div class="подпись">Здесь показываются только сделки, открытые самим ботом</div>
        </div>
        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Инструмент</th><th>Направление</th><th>Количество</th><th>Цена входа</th>
                        <th>Текущая цена</th><th>Нереализованный PNL</th><th>Открыта</th><th>Статус</th>
                    </tr>
                </thead>
                <tbody>{позиции_html or '<tr><td colspan="8">Открытых позиций бота нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Портфель / бумаги на счёте</h2>
            <div class="подпись">Отдельный блок по данным портфеля брокерского счёта</div>
        </div>
        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Инструмент</th><th>Направление</th><th>Количество</th><th>Средняя цена</th>
                        <th>Текущая цена</th><th>Нереализованный PNL</th><th>Обновлено</th><th>Статус</th>
                    </tr>
                </thead>
                <tbody>{портфель_html or '<tr><td colspan="8">Данных портфеля нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Последние сделки</h2>
            <div class="подпись">Последние записи из торговой истории</div>
        </div>
        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Инструмент</th><th>Направление</th><th>Вход</th>
                        <th>Выход</th><th>Количество</th><th>Комиссия</th><th>PNL</th><th>Причина</th>
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
            <div class="подпись">Параметры торгового бота и поведения сервиса</div>
        </div>

        <form method="post" action="/api/settings/update">
            <div class="сетка-формы">
                <label>Торговля включена
                    {render_boolean_select("bot_enabled", settings_map.get("bot_enabled", "1"))}
                </label>

                <label>Макс. сделок в день
                    <input type="text" name="max_trades_per_day" value="{settings_map.get('max_trades_per_day', '15')}">
                </label>

                <label>Макс. дневной убыток, RUB
                    <input type="text" name="max_daily_loss_rub" value="{settings_map.get('max_daily_loss_rub', '200')}">
                </label>

                <label>Макс. открытых позиций
                    <input type="text" name="max_open_positions" value="{settings_map.get('max_open_positions', '2')}">
                </label>

                <label>Интервал проверки, сек
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

                <label>Разрешить Long глобально
                    {render_boolean_select("allow_long_global", settings_map.get("allow_long_global", "1"))}
                </label>

                <label>Разрешить Short глобально
                    {render_boolean_select("allow_short_global", settings_map.get("allow_short_global", "1"))}
                </label>

                <label>Торговать только в сессию
                    {render_boolean_select("trade_only_session", settings_map.get("trade_only_session", "0"))}
                </label>

                <label>Пауза после ошибки, сек
                    <input type="text" name="pause_after_error_sec" value="{settings_map.get('pause_after_error_sec', '10')}">
                </label>

                <label>В Telegram только ошибки
                    {render_boolean_select("telegram_errors_only", settings_map.get("telegram_errors_only", "0"))}
                </label>

                <label>Автоперезагрузка настроек
                    {render_boolean_select("auto_reload_settings", settings_map.get("auto_reload_settings", "1"))}
                </label>
            </div>

            <div class="ряд-кнопок">
                <button type="submit" class="кнопка основная">Сохранить общие настройки</button>
            </div>
        </form>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Профили настроек</h2>
            <div class="подпись">Сохранение и переключение профилей параметров</div>
        </div>

        <form method="post" action="/api/профили/создать" class="ряд-кнопок">
            <input type="text" name="profile_name" placeholder="Название профиля" required>
            <button type="submit" class="кнопка основная">Создать профиль</button>
        </form>

        <div class="таблица-обёртка" style="margin-top:16px;">
            <table>
                <thead>
                    <tr>
                        <th>Профиль</th><th>Активный</th><th>Создан</th><th>Активировать</th><th>Сохранить текущие</th>
                    </tr>
                </thead>
                <tbody>{профили_html or '<tr><td colspan="5">Профилей пока нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Инструменты</h2>
            <div class="подпись">Управление инструментами для торговли</div>
        </div>

        <div class="ряд-кнопок">
            <button type="button" class="кнопка основная" onclick="открытьМодальноеОкно()">Добавить инструменты</button>
        </div>

        <div style="margin-top:18px;">
            {инструменты_html or '<div class="карточка">Инструменты пока не добавлены</div>'}
        </div>
    </section>
    """

    история = f"""
    <section class="блок">
        <div class="блок-заголовок">
            <h2>История торгов</h2>
            <div class="подпись">Сделки с фильтрами по тикеру и датам</div>
        </div>

        <div class="сетка-фильтров">
            <input id="фильтрСделкиТикер" type="text" placeholder="Тикер">
            <input id="фильтрСделкиОт" type="text" placeholder="Дата от, например 2026-04-20">
            <input id="фильтрСделкиДо" type="text" placeholder="Дата до, например 2026-04-20 23:59:59">
            <button class="кнопка" onclick="открытьСделкиAPI()">Открыть JSON</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Инструмент</th><th>Направление</th><th>Вход</th>
                        <th>Выход</th><th>Количество</th><th>Комиссия</th><th>PNL</th><th>Причина</th>
                    </tr>
                </thead>
                <tbody>{история_торгов_html or '<tr><td colspan="9">История торгов пуста</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Системная история</h2>
            <div class="подпись">Служебные события бота и панели</div>
        </div>

        <div class="сетка-фильтров">
            <input id="фильтрСистемаОт" type="text" placeholder="Дата от">
            <input id="фильтрСистемаДо" type="text" placeholder="Дата до">
            <button class="кнопка" onclick="открытьСистемнуюИсториюAPI()">Открыть JSON</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Тип</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>{системные_логи_html or '<tr><td colspan="5">Системных записей нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>

    <section class="блок">
        <div class="блок-заголовок">
            <h2>Ошибки</h2>
            <div class="подпись">Лог ошибок с фильтрацией</div>
        </div>

        <div class="сетка-фильтров">
            <input id="фильтрОшибкаТикер" type="text" placeholder="Тикер">
            <input id="фильтрОшибкаОт" type="text" placeholder="Дата от">
            <input id="фильтрОшибкаДо" type="text" placeholder="Дата до">
            <button class="кнопка" onclick="открытьОшибкиAPI()">Открыть JSON</button>
        </div>

        <div class="таблица-обёртка">
            <table>
                <thead>
                    <tr>
                        <th>Время</th><th>Тип</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th>
                    </tr>
                </thead>
                <tbody>{ошибки_html or '<tr><td colspan="5">Ошибок пока нет</td></tr>'}</tbody>
            </table>
        </div>
    </section>
    """

    current_tab_html = f"""
    <div id="вкладка-главное" class="вкладка-контент">{главное}</div>
    <div id="вкладка-настройки" class="вкладка-контент" style="display:none;">{настройки}</div>
    <div id="вкладка-история" class="вкладка-контент" style="display:none;">{история}</div>
    """

    html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <title>Панель управления торговым ботом v3.4</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            :root {{
                --фон: #08111f;
                --фон-2: #0d1a2d;
                --карточка: rgba(14, 30, 52, 0.88);
                --карточка-2: rgba(18, 38, 63, 0.9);
                --граница: rgba(110, 168, 254, 0.28);
                --текст: #eaf2ff;
                --подпись: #9ab0d1;
                --акцент: #58b6ff;
                --акцент-2: #1f8fff;
                --успех: #26d07c;
                --ошибка: #ff5d73;
                --тень: 0 20px 60px rgba(0,0,0,.35);
            }}

            * {{
                box-sizing: border-box;
            }}

            body {{
                margin: 0;
                font-family: Inter, Arial, sans-serif;
                background:
                    radial-gradient(circle at top left, rgba(40,100,200,.18), transparent 28%),
                    radial-gradient(circle at top right, rgba(0,160,255,.14), transparent 25%),
                    linear-gradient(180deg, #06101c 0%, #08111f 100%);
                color: var(--текст);
            }}

            .контейнер {{
                width: min(1400px, calc(100% - 32px));
                margin: 28px auto 48px;
            }}

            .шапка {{
                margin-bottom: 22px;
            }}

            h1 {{
                margin: 0 0 10px 0;
                font-size: 28px;
                line-height: 1.15;
            }}

            .подзаголовок {{
                color: var(--подпись);
                font-size: 16px;
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
                cursor: pointer;
            }}

            .вкладка.активная {{
                background: linear-gradient(180deg, rgba(88,182,255,.20), rgba(88,182,255,.10));
                border-color: rgba(88,182,255,.5);
                box-shadow: 0 0 0 1px rgba(88,182,255,.08) inset, 0 10px 25px rgba(31,143,255,.18);
            }}

            .вкладка-контент {{
                display: block;
            }}

            .карточки {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
                gap: 14px;
                margin-bottom: 22px;
            }}

            .карточка, .блок {{
                background: linear-gradient(180deg, rgba(16, 33, 55, 0.92), rgba(9, 20, 37, 0.96));
                border: 1px solid var(--граница);
                border-radius: 18px;
                box-shadow: var(--тень);
            }}

            .карточка {{
                padding: 16px 18px;
            }}

            .метка {{
                color: var(--подпись);
                font-size: 13px;
                margin-bottom: 8px;
            }}

            .значение {{
                font-size: 24px;
                font-weight: 700;
                word-break: break-word;
            }}

            .блок {{
                padding: 18px;
                margin-bottom: 18px;
            }}

            .блок-заголовок {{
                margin-bottom: 14px;
            }}

            .блок-заголовок h2 {{
                margin: 0 0 6px 0;
                font-size: 20px;
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

            .кнопка {{
                background: rgba(255,255,255,.04);
                color: var(--текст);
                border: 1px solid var(--граница);
                border-radius: 12px;
                padding: 11px 16px;
                cursor: pointer;
            }}

            .кнопка:hover {{
                background: rgba(255,255,255,.08);
            }}

            .кнопка.основная {{
                background: linear-gradient(180deg, rgba(31,143,255,.95), rgba(16,102,197,.95));
                border-color: rgba(88,182,255,.55);
            }}

            .кнопка.опасная {{
                background: linear-gradient(180deg, rgba(180,40,66,.95), rgba(130,20,40,.95));
                border-color: rgba(255,93,115,.45);
            }}

            .кнопка.малая {{
                padding: 8px 12px;
                border-radius: 10px;
            }}

            .сетка-формы {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 14px;
            }}

            .сетка-фильтров {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 12px;
                margin-bottom: 12px;
            }}

            label {{
                display: flex;
                flex-direction: column;
                gap: 8px;
                color: var(--подпись);
                font-size: 14px;
            }}

            input, select {{
                width: 100%;
                background: rgba(255,255,255,.04);
                border: 1px solid rgba(110,168,254,.25);
                border-radius: 12px;
                color: var(--текст);
                padding: 11px 12px;
                outline: none;
            }}

            .таблица-обёртка {{
                overflow-x: auto;
                border: 1px solid rgba(110,168,254,.16);
                border-radius: 16px;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                min-width: 900px;
            }}

            th, td {{
                padding: 12px 14px;
                border-bottom: 1px solid rgba(110,168,254,.14);
                text-align: left;
                vertical-align: top;
                font-size: 14px;
            }}

            th {{
                color: var(--текст);
                background: rgba(255,255,255,.03);
            }}

            tr:hover td {{
                background: rgba(255,255,255,.02);
            }}

            .карточка-инструмент {{
                margin-bottom: 14px;
            }}

            .карточка-удаление {{
                margin-top: -4px;
                margin-bottom: 18px;
                padding-top: 0;
                background: transparent;
                border: 0;
                box-shadow: none;
            }}

            .заголовок-инструмента {{
                display: flex;
                justify-content: space-between;
                gap: 16px;
                align-items: center;
                margin-bottom: 14px;
                flex-wrap: wrap;
            }}

            .инструмент-текст {{
                font-size: 18px;
                font-weight: 700;
            }}

            .служебное-сообщение {{
                margin-top: 14px;
                padding: 12px 14px;
                border-radius: 12px;
                background: rgba(88,182,255,.10);
                border: 1px solid rgba(88,182,255,.25);
            }}

            .модальное-окно-фон {{
                position: fixed;
                inset: 0;
                background: rgba(0,0,0,.55);
                display: none;
                align-items: center;
                justify-content: center;
                padding: 20px;
                z-index: 1000;
            }}

            .модальное-окно {{
                width: min(1200px, 100%);
                max-height: 90vh;
                overflow: auto;
                background: linear-gradient(180deg, rgba(16, 33, 55, 0.98), rgba(9, 20, 37, 0.99));
                border: 1px solid var(--граница);
                border-radius: 18px;
                box-shadow: var(--тень);
                padding: 18px;
            }}

            .модальное-окно-шапка {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 14px;
            }}

            .модальное-окно-шапка h3 {{
                margin: 0;
            }}

            @media (max-width: 768px) {{
                .контейнер {{
                    width: min(100% - 16px, 100%);
                    margin: 16px auto 28px;
                }}

                h1 {{
                    font-size: 24px;
                }}

                .значение {{
                    font-size: 20px;
                }}

                .блок, .карточка {{
                    border-radius: 14px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="контейнер">
            <header class="шапка">
                <h1>Панель управления торговым ботом v3.4</h1>
                <div class="подзаголовок">
                    Полностью русский интерфейс, вкладки, профили настроек, история, логи и управление инструментами
                </div>
            </header>

            {nav}
            {current_tab_html}
        </div>

        <div id="модальноеОкноФон" class="модальное-окно-фон">
            <div class="модальное-окно">
                <div class="модальное-окно-шапка">
                    <h3>Добавление инструментов</h3>
                    <button type="button" class="кнопка" onclick="закрытьМодальноеОкно()">Закрыть</button>
                </div>

                <div class="сетка-фильтров">
                    <input id="поисковыйЗапрос" type="text" placeholder="Введите тикер, FIGI или название">
                    <button type="button" class="кнопка основная" onclick="поискИнструмента()">Найти</button>
                    <button type="button" class="кнопка" onclick="принятьИнструменты()">Добавить выбранные</button>
                </div>

                <div class="таблица-обёртка">
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
                                <th>SL</th>
                                <th>TP</th>
                            </tr>
                        </thead>
                        <tbody id="таблицаПоискаИнструментов">
                            <tr><td colspan="11">Сначала выполните поиск</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            let найденныеИнструменты = [];

            function показатьВкладку(имя) {{
                const tabs = ['главное', 'настройки', 'история'];

                tabs.forEach(function(tab) {{
                    const content = document.getElementById('вкладка-' + tab);
                    const btn = document.getElementById('tabbtn-' + tab);

                    if (content) {{
                        content.style.display = (tab === имя) ? 'block' : 'none';
                    }}

                    if (btn) {{
                        if (tab === имя) {{
                            btn.classList.add('активная');
                        }} else {{
                            btn.classList.remove('активная');
                        }}
                    }}
                }});

                window.location.hash = имя;
            }}

            function показатьСообщение(text) {{
                const el = document.getElementById('служебноеСообщение');
                if (!el) return;
                el.style.display = 'block';
                el.textContent = text;
                setTimeout(function() {{
                    el.style.display = 'none';
                }}, 3000);
            }}

            async function управление(action) {{
                try {{
                    const resp = await fetch('/api/control/' + action, {{
                        method: 'POST'
                    }});
                    const data = await resp.json();
                    показатьСообщение(data.message || ('Команда выполнена: ' + action));
                }} catch (e) {{
                    alert('Ошибка управления: ' + e);
                }}
            }}

            function открытьМодальноеОкно() {{
                const el = document.getElementById('модальноеОкноФон');
                if (el) el.style.display = 'flex';
            }}

            function закрытьМодальноеОкно() {{
                const el = document.getElementById('модальноеОкноФон');
                if (el) el.style.display = 'none';
            }}

            async function поискИнструмента() {{
                const q = document.getElementById('поисковыйЗапрос').value.trim();
                if (!q) {{
                    alert('Введите идентификатор инструмента');
                    return;
                }}

                const resp = await fetch('/api/instruments/search?q=' + encodeURIComponent(q));
                const data = await resp.json();
                найденныеИнструменты = data || [];

                const body = document.getElementById('таблицаПоискаИнструментов');
                if (!найденныеИнструменты.length) {{
                    body.innerHTML = '<tr><td colspan="11">Ничего не найдено</td></tr>';
                    return;
                }}

                let htmlRows = '';

                найденныеИнструменты.forEach(function(x, index) {{
                    htmlRows += '<tr>';
                    htmlRows += '<td><input type="checkbox" id="использовать_' + index + '" checked></td>';
                    htmlRows += '<td>' + (x.ticker || '') + ' — ' + (x.name || '') + '</td>';
                    htmlRows += '<td>' + (x.figi || '') + '</td>';
                    htmlRows += '<td>' + (x.instrument_type || '') + '</td>';
                    htmlRows += '<td>' + (x.class_code || '') + '</td>';
                    htmlRows += '<td>' + (x.currency || '') + '</td>';
                    htmlRows += '<td>' + (x.lot || 1) + '</td>';
                    htmlRows += '<td>' + (x.min_price_increment || '0.01') + '</td>';
                    htmlRows += '<td><input type="number" id="lots_' + index + '" value="1" min="1"></td>';
                    htmlRows += '<td><input type="text" id="sl_' + index + '" value="{default_sl}"></td>';
                    htmlRows += '<td><input type="text" id="tp_' + index + '" value="{default_tp}"></td>';
                    htmlRows += '</tr>';
                }});

                body.innerHTML = htmlRows;
            }}

            async function принятьИнструменты() {{
                if (!найденныеИнструменты.length) {{
                    alert('Сначала выполните поиск');
                    return;
                }}

                const payload = найденныеИнструменты.map(function(x, index) {{
                    return {{
                        использовать: document.getElementById('использовать_' + index)?.checked ? true : false,
                        ticker: x.ticker || '',
                        figi: x.figi || '',
                        name: x.name || '',
                        class_code: x.class_code || '',
                        instrument_type: x.instrument_type || '',
                        currency: x.currency || '',
                        lot: x.lot || 1,
                        min_price_increment: x.min_price_increment || '0.01',
                        lots_override: parseInt(document.getElementById('lots_' + index)?.value || '1'),
                        stop_loss_pct: document.getElementById('sl_' + index)?.value || '{default_sl}',
                        take_profit_pct: document.getElementById('tp_' + index)?.value || '{default_tp}'
                    }};
                }});

                const resp = await fetch('/api/instruments/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ items: payload }})
                }});

                const data = await resp.json();
                if (data.ok) {{
                    показатьСообщение('Инструментов добавлено: ' + data.добавлено);
                    закрытьМодальноеОкно();
                    setTimeout(function() {{
                        показатьВкладку('настройки');
                        window.location.reload();
                    }}, 600);
                }} else {{
                    alert('Не удалось добавить инструменты');
                }}
            }}

            function открытьСделкиAPI() {{
                const ticker = document.getElementById('фильтрСделкиТикер').value.trim();
                const df = document.getElementById('фильтрСделкиОт').value.trim();
                const dt = document.getElementById('фильтрСделкиДо').value.trim();

                const url = '/api/история/торговля?ticker=' + encodeURIComponent(ticker)
                    + '&date_from=' + encodeURIComponent(df)
                    + '&date_to=' + encodeURIComponent(dt);

                window.open(url, '_blank');
            }}

            function открытьСистемнуюИсториюAPI() {{
                const df = document.getElementById('фильтрСистемаОт').value.trim();
                const dt = document.getElementById('фильтрСистемаДо').value.trim();

                const url = '/api/история/система?date_from=' + encodeURIComponent(df)
                    + '&date_to=' + encodeURIComponent(dt);

                window.open(url, '_blank');
            }}

            function открытьОшибкиAPI() {{
                const ticker = document.getElementById('фильтрОшибкаТикер').value.trim();
                const df = document.getElementById('фильтрОшибкаОт').value.trim();
                const dt = document.getElementById('фильтрОшибкаДо').value.trim();

                const url = '/api/история/ошибки?ticker=' + encodeURIComponent(ticker)
                    + '&date_from=' + encodeURIComponent(df)
                    + '&date_to=' + encodeURIComponent(dt);

                window.open(url, '_blank');
            }}

            (function() {{
                const hash = (window.location.hash || '#главное').replace('#', '');
                if (['главное', 'настройки', 'история'].includes(hash)) {{
                    показатьВкладку(hash);
                }} else {{
                    показатьВкладку('главное');
                }}
            }})();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)