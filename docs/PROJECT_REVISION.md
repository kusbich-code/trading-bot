# Ревизия проекта trading-bot (версия 5.8.3)

Документ написан для агента (и человека), которому нужно быстро понять, где что лежит и за что отвечает.
Состояние кода: коммит `3306913` (v5.8.3), ветка `main`, ревизия от 2026-10-20.

Как собрана ревизия: пять параллельных проходов по коду (main.py, webapp+деплой, db.py, ML+сервисы, интерфейс+Telegram). Номера строк (`L123`) действительны для v5.8.3 и «плывут» после любой правки: перед опорой на номер ищите функцию по имени. Пометка **[не проверено]** означает гипотезу, которую нужно подтвердить чтением кода.

## Оглавление

1. Краткое резюме
2. Архитектура и карта файлов
3. Серверная часть: торговый процесс main.py
4. Серверная часть: веб-сервер webapp.py
5. База данных
6. Функциональность: как бот торгует
7. Модель (ML)
8. Аналитик, бэктест и недельный цикл
9. Интерфейс (дашборд)
10. Telegram
11. Git и версионирование
12. Деплой и эксплуатация
13. Справочник настроек
14. Реестр проблем по приоритету
15. Мёртвый код
16. Правила работы агента с этим проектом
17. Глоссарий

---

## 1. Краткое резюме

**Что это.** Бот для внутридневной торговли российскими акциями (MOEX) через T-Bank Invest API (SDK `t_tech.invest`). Работает в sandbox (виртуальные деньги) или в боевом режиме. Умеет лонг и шорт, несколько стратегий параллельно, биржевые стоп-ордера, ML-фильтр входа.

**Из чего состоит.** Два независимых процесса, которые общаются только через SQLite:
- `main.py` — торговый движок (бесконечный цикл, потоки).
- `app/webapp.py` — веб-дашборд на FastAPI (одна HTML-страница + JSON API).

**Размер.** Около 19 тыс. строк: `dashboard.js` 5028, `webapp.py` 3306, `main.py` 3037, `db.py` 2125. Остальное мелкое.

**Главное, что нужно знать до любых правок:**
1. Реальная торговая логика целиком в `process_instrument()` в `main.py`. Это одна функция на ~800 строк.
2. Настройки бот читает из БД при каждом обращении, но SL/TP кэширует в памяти при старте. После правок параметров нужен **перезапуск бота** (см. п. 6.6).
3. В дашборде **нет авторизации** в самом приложении. Все опасные действия (закрытие позиций, смена токена, очистка истории) открыты любому, кто достучится до порта.
4. ML сейчас работает хуже, чем задумано: признаки 1ч/4ч всегда нули из-за ошибки вызова, ML-порог практически не блокирует вход (раздел 7).
5. Схема БД расходится с кодом: `trades.open_time` используется, но не создаётся в `init_db`.

---

## 2. Архитектура и карта файлов

### 2.1. Схема процессов

```
                    ┌──────────────────────┐
  T-Bank API  ◄────►│  main.py (бот)       │
  (gRPC, SDK)       │  главный цикл +      │
                    │  потоки              │
                    └─────────┬────────────┘
                              │ чтение/запись
                    ┌─────────▼────────────┐
                    │ SQLite               │  data/trading_bot.sqlite3
                    │ (шина между процессами)
                    └─────────▲────────────┘
                              │ чтение/запись
                    ┌─────────┴────────────┐
  Браузер  ◄───────►│ app/webapp.py        │◄────► T-Bank API
  (dashboard.js)    │ FastAPI + uvicorn    │       (свои вызовы!)
                    └──────────────────────┘
```

- Бот и веб **не вызывают друг друга напрямую**. Бот пишет состояние в таблицу `runtime_state` (статус, API-нагрузка, статусы потоков, последний сигнал по каждой бумаге), веб читает и показывает. Веб пишет настройки в `bot_settings`/профили/стратегии, бот читает.
- Прямой «очереди команд» боту нет. Единственный канал управления процессом: `app/control.py` (`systemctl start/stop/restart` на Linux).
- Веб **сам ходит к брокеру** (закрыть позицию, отменить ордер, выставить стоп). Это происходит параллельно с работой бота, поэтому возможны гонки.
- Преимущество разделения: дашборд работает, даже если бот упал, и наоборот.

### 2.2. Карта файлов

| Файл | Строк | За что отвечает |
|---|---|---|
| `main.py` | 3037 | Торговый процесс: цикл, сигналы, ордера, стопы, ML-вызовы, параллельные воркеры |
| `app/webapp.py` | 3306 | FastAPI: HTML-оболочка, ~120 эндпоинтов, поток портфеля, управление |
| `app/db.py` | 2125 | Вся работа с SQLite: схема, настройки, профили, стратегии, сделки, статистика |
| `app/config.py` | 45 | Класс `Settings`, читает `.env` при импорте |
| `app/version.py` | 1 | `__version__ = "5.8.3"` |
| `app/control.py` | 136 | Запуск/остановка бота (systemctl на Linux, PID-файл на Windows) |
| `app/instruments.py` | 96 | `get_instrument_meta` и `round_to_price_step` (живое); `router` (мёртвый) |
| `app/stop_orders.py` | 86 | **Мёртвый модуль**, нигде не импортируется |
| `app/telegram_bot.py` | 275 | Приём команды `/status` и нажатий кнопок оптимизатора |
| `app/telegram_notify.py` | 96 | Очередь исходящих уведомлений |
| `app/telegram_health.py` | 52 | Тестовая отправка и диагностика |
| `app/weekly_scheduler.py` | 245 | Субботний цикл: отчёт недели + поиск улучшений |
| `app/services/tbank_client.py` | 751 | Обёртка над SDK: цены, свечи, ордера, стопы, портфель, операции |
| `app/services/strategy_engine.py` | 93 | Расчёт сигналов (trend / mean_reversion / breakout) |
| `app/services/backtest_engine.py` | 263 | Бэктест по свечам |
| `app/services/analyst.py` | 638 | Подбор стратегий перебором (поиск и оптимизация) |
| `app/services/healthcheck.py` | 27 | Простая проверка для `/api/health` |
| `app/ml/feature_builder.py` | 390 | Признаки для модели, запись в `ml_features` |
| `app/ml/model_trainer.py` | 374 | Обучение GradientBoosting, хранение моделей |
| `app/ml/model_predictor.py` | 415 | Решения «входить / выходить», размер лота, адаптивные SL/TP |
| `app/ml/orchestrator.py` | 408 | Ежедневный ребаланс, обновление параметров, дрейф |
| `app/ml/experience.py` | 114 | Контекст каждой сделки (`ml_trade_context`) |
| `app/ml/analyzer.py` | 140 | Качество инструмента, EWA |
| `app/ml/optimizer.py` | 153 | Устаревший координатный спуск; жива только запись лога |
| `app/ml/backtest_client.py` | 95 | Подбор режима/SL/TP бэктестом на 90 днях |
| `app/ml/multi_timeframe.py` | 94 | Свечи 1ч/4ч (**сломан**, см. п. 7.9) |
| `app/ml/strategy_selector.py` | 94 | Thompson Sampling по режимам (не используется) |
| `app/static/dashboard.js` | 5028 | Весь интерфейс (vanilla JS + Plotly) |
| `app/static/dashboard.css` | 297 | Тёмная тема |
| `scripts/backfill_ml_indicators.py` | 343 | Разовая дозаливка ML-признаков по старым сделкам |
| `check_js.py` | 16 | Ad-hoc проверка скобок в JS (в `.gitignore`, путь сервера зашит) |
| `deploy/*` | | systemd-юниты и конфиг nginx |

Не в git: `.env`, `data/*.sqlite3`, `logs/*`, `.venv`, `.claude/`, и **`plotly-2.35.2.min.js`** (его нет в репозитории, см. раздел 14).

---

## 3. Серверная часть: торговый процесс main.py

Синхронный многопоточный процесс. Asyncio нет. Все потоки — `threading.Thread(daemon=True)`.

### 3.1. Старт

При импорте (L83–L110): `load_dotenv()`, создание `logs/` и `data/`, настройка логов (файл + консоль), `init_db()`, `notifier = TelegramNotifier()`. Создаются глобалы: `_parallel_coord` (L187), `state = BotState()` (L249), `_rate = _ApiRateTracker()` (L355).

`main()` (L2875) выполняет по порядку:
1. Пишет событие `BOT_START`.
2. Сбрасывает в `runtime_state` ключ `parallel_coord` и статусы потоков `parallel_thread_{sid}`.
3. Запускает Telegram-поллинг (если включён в `.env`).
4. Запускает `start_weekly_scheduler` (субботний цикл).
5. Выбирает `SandboxClient` или `Client` по `TINVEST_USE_SANDBOX`.
6. Один раз `_fill_all_instrument_uids` — подтягивает из API тикеры/имена/uid.
7. Запускает два потока: `orders-stream` и `market-data-stream`.
8. Читает снимок портфеля, затем `_refresh_parallel_workers()`.
9. Восстанавливает открытые BOT-позиции из БД в память (после рестарта `trailing_stop=0`, `native_stop_ids={}`).
10. Берёт баланс, ставит статус `SCANNING`, входит в главный цикл.

### 3.2. Главный цикл (L2961–L3035)

Каждая итерация:
1. `maybe_reset_daily()` — при смене даты по МСК обнуляет `daily_pnl` и `trades_today`, пишет `DAILY_RESET`, в фоне запускает `orchestrator.run_daily_rebalance`.
2. Если `bot_enabled != "1"` → статус `DISABLED`, сон 5 с.
3. Если `trades_today >= max_trades_per_day` (по умолчанию 15) → `SESSION_STOPPED_BY_LIMIT`, сон 60 с.
4. «Режим сна»: если `trade_only_session=1`, биржа закрыта и позиций нет → статус `SLEEP_UNTIL_HH:MM`, сон не более 600 с.
5. Если `daily_pnl <= -max_daily_loss_rub` (по умолчанию 200) → `SESSION_STOPPED_BY_LIMIT`, сон 60 с.
6. Раз в 30 с — `_refresh_parallel_workers()`.
7. Для каждого инструмента активной стратегии: `process_instrument(...)`.
8. `sync_portfolio_positions` — сверка с брокером.
9. Обновление баланса и запись `runtime_state`.
10. Сон `check_interval_sec` (по умолчанию 5 с).

При исключении: статус `ERROR`, событие `BOT_ERROR`, Telegram «⚠️ Ошибка бота», пауза `pause_after_error_sec` (10 с).

### 3.3. Потоки

| Поток | Что делает |
|---|---|
| main (главный) | Цикл выше |
| `orders-stream` (`_orders_stream_worker` L261) | Слушает исполнения ордеров, кладёт в очередь `_pending_orders[order_id]` |
| `market-data-stream` (`_market_data_stream_worker` L394) | Цены и стакан (глубина 10) в кэш `_md[figi]` |
| `parallel-{strategy_id}` (`_parallel_strategy_worker` L2558) | По одному на параллельную стратегию |
| Telegram polling | `run_telegram_polling` |
| weekly scheduler | Раз в 5 минут проверяет «суббота после 18:00» |
| Одноразовые | ML-ребаланс, переобучение модели, `_watcher` стрима |

### 3.4. Функции main.py по темам

**Состояние и инфраструктура**
- `_now()` L104 — время МСК без tz.
- `BotState` L219 — статус, баланс сессии, `daily_pnl`, `trades_today`, открытые позиции, кулдауны. Метод `sync_runtime` (L235) пишет всё это в `runtime_state`. `ORDER_COOLDOWN_SEC = 90`.
- `_ApiRateTracker` L303 — скользящее окно 60 с, `limit=570`, при достижении спит 1 с. Реальный лимит API — 600 запросов/мин.
- `_ParallelCoord` L115 — координатор параллельных стратегий: хранит, какая стратегия «владеет» каким figi. `try_claim` не даёт открыть больше `max_open_positions` позиций суммарно.
- `_pset` L203 — пишет статус потока стратегии в память и в `runtime_state` (для дашборда).

**Рыночные данные**
- `get_last_price` L479, `get_order_book_spread_pct` L484 (кэш 15 с), `get_candles` L893 (1-мин свечи), `get_trading_status` L920 (кэш 30 с), `get_api_indicators` L1619 (RSI/MACD/BB из `GetTechAnalysis`, кэш 90 с).

**Сессия и календарь**
- `is_moex_session_open` L956: пн–пт, 10:00–18:50 и 19:05–23:50 МСК. Праздники здесь **не** учитываются.
- `_secs_to_next_session_open` L965, `is_session_allowed` L541, `is_tradable` L1136 (разрешённые статусы торгов).
- `_generate_moex_holidays` L996, `_get_moex_holidays_cached` L1044 — фиксированные праздники РФ с переносом на понедельник.
- `short_carry_risk` L1068 — риск переноса шорта через выходные (0, 1 или 3).
- `should_force_close_short` L1105 — принудительное закрытие шорта с 22:50 МСК в пятницу/перед праздником.

**Инструменты**
- `_fill_all_instrument_uids` L1191, `load_enabled_instruments` L1219 (источник: `list_active_strategy_instruments()`).

**Ордера и стопы**
- `place_order_checked` L1284 — выставление ордера и ожидание исполнения (до 6 с по стриму, затем опрос статуса до 8 раз). Принимает цену исполнения только в пределах ±5% от запрошенной. Неисполненный ордер отменяет. Код ошибки `30034` (торги недоступны) даёт паузу 5 мин (1–2 раза), затем 1 час.
- `_place_native_stops` L1487 — ставит на бирже стоп-лосс и тейк-профит (GTC).
- `_cancel_all_orders_for_figi` L1567 — снимает все стопы и ордера по бумаге.
- `_cancel_native_stops` L1552 — мёртвый код.

**Позиции**
- `sync_portfolio_positions` L796 — перезаписывает позиции `PORTFOLIO` из портфеля брокера. BOT-позицию, которой нет у брокера (старше 90 с), считает закрытой снаружи и передаёт в `_handle_manual_close`.
- `_handle_manual_close` L608 — фиксирует сделку, закрытую биржевым стопом или вручную: берёт реальную цену из операций брокера, определяет причину (допуск ±0.5% к SL/TP, при цене рынка — ±1.5%), считает комиссию, пишет `trades`, размечает ML. **Не обновляет `daily_pnl` и `trades_today`.**
- `_get_actual_close_price` L569 — последняя исполненная операция по бумаге за 2 часа.

**Ядро**
- `process_instrument` L1733–L2556 — см. раздел 6.
- `_apply_indicator_filter` L1695 — фильтр по RSI/MACD/Bollinger.

**Параллельные стратегии**
- `_parallel_strategy_worker` L2558, `_parallel_cfg_rows` L2748, `_parallel_instruments` L2755 (кэш 60 с), `_refresh_parallel_workers` L2797 (перезапуск потоков при смене набора стратегий), `start_parallel_workers` L2838.

**Мёртвый код в main.py:** `get_ma_signal`, `calc_support_resistance`, `get_signal`, `_cancel_native_stops`, `_CANDLE_TO_INDICATOR_INTERVAL`, `_MOEX_HOLIDAYS`, `_parallel_stop_events`, `MSK_TZ` (дубль `_MSK`).

### 3.5. Режим «обычный» и «параллельный»

- **Обычный**: главный цикл обходит инструменты одной активной стратегии; торговые параметры берутся из `bot_settings` (куда при активации профиля скопированы настройки стратегии).
- **Параллельный** (`parallel_trading_enabled=1` в профиле): для каждой стратегии из `profile_parallel_strategies` запускается свой поток со своим `strategy_settings`. Лимит позиций общий через `_ParallelCoord`.
- **Важно:** в параллельном режиме главный цикл **тоже** продолжает обходить `watchlist`. Один и тот же figi может обрабатываться и главным циклом, и воркером. Состояния у них разные словари, это риск дублей входа.
- Воркеры **не проверяют** `max_daily_loss_rub` и `max_trades_per_day` (эти лимиты действуют только в главном цикле). Исключение: лимит убытка по отдельному инструменту проверяется внутри `process_instrument`.

---

## 4. Серверная часть: веб-сервер webapp.py

### 4.1. Устройство

- `app = FastAPI(...)` (L107), один плоский файл без роутеров. Запуск только через `uvicorn app.webapp:app`.
- **Авторизации в приложении нет.** Защита предполагалась на уровне nginx (basic auth), но systemd-юнит запускает uvicorn на `0.0.0.0:80`, то есть в обход nginx.
- Статика: `/static` → папка `app/static` (путь относительный, зависит от рабочего каталога).
- HTML: единственная страница `GET /dashboard/` — f-строка в `dashboard_page()` (L471–593). Шаблонизатора нет. Страница — оболочка: шапка, 8 вкладок, три модальных окна, ТВ-плеер РБК, остальное строит `dashboard.js`.
- Старт (`@app.on_event("startup")`): `init_db()`, `_purge_bad_positions()` (закрывает «битые» позиции с входной ценой в 5+ раз выше рынка — баг цены за лот в sandbox) и поток `portfolio-stream`.

### 4.2. Фоновые потоки веба

- `portfolio-stream` (`_portfolio_stream_worker` L117) — подписка на `portfolio_stream`, кладёт позиции в кэш `_portfolio_cache`. Подписан на `TINVEST_ACCOUNT_ID` на момент старта; после `sandbox/reset` остаётся на старом счёте. Свежесть кэша не проверяется.
- `backtest-worker` — запускается по `/api/backtest/start`.
- Поток `run_daily_rebalance` по `/api/ml/rebalance` (без защиты от двойного запуска).
- Потоки аналитика — внутри `analyst.py`.

### 4.3. Эндпоинты (по группам)

**Страница, новости, рынок**
- `GET /dashboard/` — оболочка.
- `GET /api/news` — RSS Коммерсантъ, кэш 300 с.
- `GET /api/news/ticker?figi&hours` — новости по бумаге из 5 RSS (Интерфакс, РБК, Finam, Коммерсантъ, Ведомости).
- `GET /api/market/session` — тип сессии, секунды до события, «волатильность» из `ml_features`.

**Данные дашборда (только чтение)**
- `GET /api/dashboard/summary` — статус сервиса, PnL, комиссии, активы, нагрузка API.
- `GET /api/dashboard/main` — инструменты, позиции, сделки дня.
- `GET /api/dashboard/multi-candles` — свечи до 30 бумаг (последовательные запросы!).
- `GET /api/dashboard/quotes`, `/portfolio`, `/stop-orders`, `/runtime`, `/balance-check`, `/bot-explain` (причины, почему бот не торгует), `/settings`, `/history`, `/chart`.
- `GET /api/parallel/status` — статусы потоков, статистика день/неделя/месяц, сигналы, лимиты.

**Профили и стратегии** (формы)
- `POST /api/profiles/create`, `/{id}/activate`, `/{id}/set-strategy`, `/{id}/settings`, `/{id}/delete`.
- `POST /api/strategies/create`, `/{id}/settings`, `/{id}/delete`, `/set-all` (записать ключ во все стратегии).
- `GET /api/strategy/{id}/details`, `/instruments`.
- `POST /api/strategies/{id}/instruments/add|update|delete`.
- Параллельные: `POST /api/profile/{id}/parallel-toggle`, `GET|POST /api/profile/{id}/parallel-strategies`, `DELETE .../{strategy_id}`.

**Позиции, ордера, стопы** (действуют на брокера напрямую)
- `POST /api/позиции/закрыть`, `/закрыть-все`, `/очистить-локальные` (**кириллические пути**).
- `GET /api/orders/active`, `POST /api/orders/{id}/cancel`, `/cancel-all`.
- `POST /api/stop-orders/{id}/cancel`, `/cancel-all`, `/create-bundle`.

**Sandbox:** `POST /api/sandbox/pay-in`, `/reset` (закрывает счёт, открывает новый, пишет `.env`; бот при этом **не** перезапускается).

**История:** `GET /api/history/stats`, `POST /api/history/clear` (удаляет сделки/логи без защиты), `GET /api/broker-operations`.

**Бэктест:** `GET /api/backtest/instruments|strategies|status|result`, `POST /api/backtest/start|run`.

**Аналитик:** `/api/analyst/status|start|stop|sessions|results|result/{id}|save/{id}|instruments|optimize-start|optimize-stop|optimize-status|optimize-results|optimize-apply/{id}`, `POST /api/analyst/sessions/{id}/apply|reject|create-manual`.

**ML:** `GET /api/ml/summary`, `/api/ml/instrument/{figi}`, `POST /api/ml/rebalance`, `GET /api/instrument/stats/{figi}`.

**Управление и учётные данные:** `POST /api/control/{start|stop|restart|status}`, `GET /api/health`, `GET|POST /api/credentials` (токены брокера в `.env`), `GET /api/telegram/status`, `POST /api/health/telegram-test`, `GET /api/debug/search`.

**Поиск инструментов:** `GET /api/instruments/search`, `/api/instruments/top` — это `async def` с блокирующим gRPC внутри (стопорят event loop).

### 4.4. Управление ботом

`app/control.py` → `run_control(action)`:
- Linux: `systemctl status trading-bot.service` и `sudo /bin/systemctl start|stop|restart trading-bot.service`. Нужна запись NOPASSWD в sudoers для пользователя `trader` (в репозитории не документирована).
- Windows (локальная разработка): PID-файл `data/bot.pid`, `taskkill`, запуск `main.py` отдельным процессом.

Мягкая пауза без перезапуска: настройка `bot_enabled=0` (бот в цикле видит и ждёт).

### 4.5. Конфигурация (`app/config.py`)

`.env` читается один раз при импорте. Переменные и умолчания: `TINVEST_TOKEN`, `TINVEST_ACCOUNT_ID`, `TINVEST_USE_SANDBOX=true`, `CHECK_INTERVAL_SEC=5`, `MAX_TRADES_PER_DAY=15`, `MAX_DAILY_LOSS_RUB=200`, `MAX_OPEN_POSITIONS=2`, `DEFAULT_STOP_LOSS_PCT=0.0025`, `DEFAULT_TAKE_PROFIT_PCT=0.005`, `ESTIMATED_COMMISSION_PCT=0.0004`, `TELEGRAM_ENABLED=false`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_POLLING_ENABLED=true`, `TELEGRAM_PROXY`, `DB_PATH=data/trading_bot.sqlite3`, `LOG_FILE=logs/trading-bot.log`, `TIMEZONE=Europe/Moscow`.
Не используются: `DASHBOARD_HOST/PORT`, `DASHBOARD_BASIC_AUTH_*`, `BOT_NAME`.
Дополнительно веб пишет в `.env` ключи `TINVEST_TOKEN_SANDBOX/_PROD`, `TINVEST_ACCOUNT_ID_SANDBOX/_PROD`, которые бот напрямую не читает.
**Изменение `.env` вступает в силу только после перезапуска процессов.**

### 4.6. Мелкие модули

- `app/instruments.py`: `get_instrument_meta(figi)` (тикер, lot, шаг цены, uid) и `round_to_price_step`. Подозрение: `min_price_increment` приводится через `str()` объекта Quotation, а не Decimal **[не проверено на живых данных]**. Использует боевой `Client` даже для sandbox-токена.
- `app/services/healthcheck.py`: читает настройку `botenabled` (без подчёркивания), поэтому проверка бесполезна.

---

## 5. База данных

SQLite, файл `data/trading_bot.sqlite3`. Доступ только через `app/db.py` (кроме ML-модулей и `analyst.py`, которые пишут сырым SQL).

### 5.1. Подключение

- `get_connection()` открывает новое соединение на каждую операцию, `db_cursor()` — контекстный менеджер (commit при успехе, close в `finally`, явного rollback нет).
- **WAL не включён**, `busy_timeout` не задан, внешние ключи выключены. Три конкурирующих писателя (бот, веб, ML/аналитик) → возможны ошибки `database is locked` после 5 с ожидания.
- Миграций «по версиям» нет: схема создаётся `init_db()` через `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN` в `try/except: pass`.
- Индексов (`CREATE INDEX`) нет вообще. На `trades`, `event_logs`, `ml_features` запросы будут деградировать по мере роста.

### 5.2. Таблицы

**Настройки и состояние**
| Таблица | Назначение |
|---|---|
| `bot_settings` (key, value) | «Живые» настройки, которые читает бот. При активации профиля сюда копируются настройки профиля и стратегии |
| `runtime_state` (key, value) | Состояние бота для дашборда: статус, `api_rpm`, `parallel_thread_{sid}`, `parallel_coord`, `last_signal_{figi}`, `session_balance`, `weekly_report_last_run` |

**Профили и стратегии**
| Таблица | Назначение |
|---|---|
| `profiles` (id, name, is_active, strategy_id) | Профиль. Активен один |
| `profile_settings` (profile_id, key, value) | Системные настройки профиля |
| `strategies` (id, name, parallel_enabled) | Стратегия (глобальная, не привязана к профилю) |
| `strategy_settings` (strategy_id, key, value) | Режим и риск-лимиты |
| `strategy_instruments` | Инструменты стратегии: figi, lot, лоты/авто-лоты, SL, TP (TEXT, доли: 0.0025 = 0.25%), спред, объём, лонг/шорт, приоритет, enabled, uid |
| `profile_parallel_strategies` (profile_id, strategy_id, sort_order) | Какие стратегии запущены параллельно |
| `instruments` | Старый каталог бумаг (для отображения цен) |
| `instrument_market_state` | Последняя цена, bid/ask, объём по figi |

**Торговля**
| Таблица | Назначение |
|---|---|
| `trades` | Закрытые сделки: время закрытия, тикер, направление, вход, выход, `qty` (в лотах), PnL, комиссия, причина, статус исполнения |
| `positions` | Позиции: `source` = `BOT` или `PORTFOLIO`, `status` OPEN/CLOSED, `ml_feature_id` |
| `event_logs` | Журнал событий (`BOT_START`, `ORDER_CLOSE`, `SIGNAL`, `AUTO_LOTS`, ...) |

**Оптимизация и аналитик:** `analyst_results`, `optimization_results`, `optimization_sessions` (раздел 8).

**ML:** `ml_features` (признаки + метка), `ml_models` (модели в BLOB), `ml_decisions` (лог решений), `ml_trade_context` (контекст сделки), `ml_instrument_state` (качество по бумаге), `ml_optimization_log` (лог смен параметров).

**Остаток миграции:** `strategy_instruments_v1`.

### 5.3. Профили, стратегии, активация

- **Профиль** = системные настройки + ссылка на одну основную стратегию. Активен один. **Стратегия** = режим сигнала, риск-лимиты, набор инструментов. Флага «активна» у стратегии нет — активна та, что выбрана в активном профиле.
- `activate_profile(id)`: переключает `is_active`, копирует **все** `profile_settings` в `bot_settings`, затем **все** `strategy_settings`, пишет `active_profile_id/name`, `active_strategy_id/name`.
- Порядок «профиль → стратегия» важен: ключи `trade_only_session` и `max_open_positions` есть в обоих наборах, значение стратегии перезаписывает профильное. Умолчания расходятся (профиль `trade_only_session=1`, стратегия `0`; `max_open_positions` профиль 3, стратегия 2).
- `update_profile_settings` / `update_strategy_settings` копируют значения в `bot_settings` только если профиль/стратегия активны. **Параллельные стратегии в `bot_settings` не попадают** — воркеры читают `strategy_settings` напрямую.
- `apply_optimization_result` пишет только в `strategy_settings`/`strategy_instruments`, в `bot_settings` — нет. Живое значение обновится после повторной активации.
- Каждый старт `_seed_preset_strategies` пересоздаёт 4 пресета («Голубые фишки — Возврат к средней», «Нефтяной сектор — Возврат к средней», «Пробой с объёмом», «Финансы — Скальпинг»), если их нет по имени. Удалённые вручную пресеты вернутся.

### 5.4. Функции db.py по темам (сокращённо)

- **Настройки:** `get_setting`, `set_setting`, `get_all_settings`, `get_runtime`, `set_runtime`, `get_all_runtime`.
- **Логи:** `log_event`, `get_logs`, `get_error_logs`, `get_system_logs`.
- **Сделки:** `add_trade`, `get_trades`, `get_ticker_daily_pnl`, `get_recent_trade` (защита от дублей), `clear_history`.
- **Позиции:** `upsert_position`, `update_position_price`, `close_position`, `get_open_positions`, `clear_open_positions`, `save/get_position_feature_id`.
- **SL/TP бумаги:** `get_instrument_sl_tp(figi)` — берёт **самый широкий** SL среди включённых в запущенные стратегии, нижний предел 0.5%.
- **Статистика:** `get_trade_stats_today`, `get_strategy_trade_stats`, `get_history_stats`, `get_weekly_stats`.
- **Профили:** `list_profiles`, `create_profile`, `delete_profile`, `activate_profile`, `update_profile_settings`, `set_profile_strategy`, `get_profile_setting`.
- **Стратегии:** `list_strategies`, `create_strategy`, `delete_strategy`, `update_strategy_settings`, `set_setting_all_strategies`, `auto_strategy_name`/`apply_auto_name` (автоимя «ТИКЕР Режим SL%/TP%»), `rebuild_strategy_name` (дубль с отличиями).
- **Инструменты стратегий:** `list_strategy_instruments`, `list_active_strategy_instruments`, `add/update/delete_strategy_instrument`.
- **Параллельные:** `list_profile_parallel_strategies`, `add/remove_profile_parallel_strategy`.
- **Оптимизация:** `apply_optimization_result`, `create/get/list_optimization_session(s)`, `link_results_to_session`, `get_session_results`.

### 5.5. Известные проблемы БД

1. `add_trade` пишет колонку `open_time`, которой **нет в `CREATE TABLE trades`**. На чистой БД запись сделки упадёт. Рабочая БД, видимо, изменена вручную. Так же `update_strategy_instrument` допускает поле `max_daily_loss_rub`, которого нет в схеме `strategy_instruments`.
2. Сид `seed_instruments` содержит перепутанные figi (VTBR с figi SBER и т. п.).
3. `get_strategy_trade_stats` фильтрует сделки по тикерам стратегии, а не по самой стратегии — при одном тикере в нескольких стратегиях статистика дублируется.
4. `delete_profile` не чистит `profile_parallel_strategies`; `clear_history` не чистит связанные ML-записи.
5. Смешаны часовые пояса: `event_logs` — МСК, `instrument_market_state.updated_at` — UTC, `apply_optimization_result` — локальное.
6. `optimization_sessions.notes` хранит `run_id`, но `update_session_status(..., notes="")` его затирает.
7. Нулевые сделки (`pnl = 0`) в статистике считаются убытками.

---

## 6. Функциональность: как бот торгует

### 6.1. Порядок действий `process_instrument` (L1733–L2556)

1. Бумага в `_unavailable_figis` (NOT_FOUND) → выход.
2. Цена из стрима `_md` (или REST), 50 минутных свечей, стакан. Всегда обновляется `instrument_market_state`.
3. Свечей меньше 5 → пропуск.
4. **Сигнал:** `evaluate_signal(режим, свечи)` из `strategy_engine` возвращает `{action: BUY|SELL|HOLD, score, reasons}`. Сохраняется в `runtime_state["last_signal_{figi}"]` для дашборда.
5. Кулдаун после ошибки ордера (90 с).
6. Для **нового входа**: биржа открыта, `is_session_allowed`, статус торгов, `min_volume`, `max_spread_pct`.
7. Если включён `use_api_confirm` — фильтр индикаторов (BUY блокируется при RSI>70 или MACD<signal; SELL при RSI<30 или MACD>signal; в режиме mean reversion ещё положение относительно полос Боллинджера).

### 6.2. Режимы сигнала (`strategy_engine.py`)

| Режим | Условие входа | Скор |
|---|---|---|
| `trend` | SMA9 > SMA21 и моментум за 5 баров > 0 → BUY (зеркально SELL). Нужно ≥30 свечей | среднее из `|fast−slow|/slow·1000` и `|momentum|·15` |
| `mean_reversion` | z-score по 20 закрытиям ≤ −1.8 → BUY, ≥ +1.8 → SELL. ≥25 свечей | `min(|z|·30, 100)` |
| `breakout` | Пробой максимума/минимума 20 предыдущих свечей при объёме ≥ 1.2× среднего. ≥25 свечей | зависит от абсолютной цены, не от процента |

Неизвестный режим → HOLD.
(Ранее в памяти проекта значилось, что `strategy_engine` нужен только для UI. Это устарело: сейчас бот торгует именно по нему.)

### 6.3. Вход

1. Лимит открытых позиций (`max_open_positions`, по умолчанию 3; общий через координатор).
2. Кулдаун 60 с после закрытия по этой бумаге.
3. `HOLD` → выход. `score < min_signal_score` → пропуск (`SIGNAL_SKIP`).
4. Опционально сервис сигналов T-Bank (`use_signal_service`): блокирует BUY при сигнале «продавать» и наоборот.
5. ML-фильтр (раздел 7).
6. SL/TP: из настроек бумаги, затем ML-адаптация (только расширяет), затем пол **0.5%** (`MIN_STOP_LOSS_PCT`).
7. **Размер позиции**:
   - при `auto_lots`: бюджет = `total_assets / max_open_positions`, но не больше свободных денег; `lots = max(1, int(бюджет / стоимость лота))`; при активной ML-модели масштабируется `compute_lot_scale`;
   - иначе `lots_override`.
8. Проверка баланса: `lots × lot × цена × (1 + комиссия + 0.2% запас)`.
9. Лимит убытка по бумаге за день.
10. Фильтр стакана (`use_order_book_filter`): BUY блокируется, если доля bid < 40%; SELL — если доля ask < 40%. Работает только при данных стрима.
11. Для SELL (шорт): `allow_short` и `short_carry_risk()` (блок новых шортов после 17:00 МСК перед выходными/праздниками).
12. `_coord.try_claim` → рыночный ордер → биржевые SL и TP → запись позиции и признаков → Telegram «🟢/🔴 Открытие позиции».

### 6.4. Управление позицией и выход

Пока позиция открыта, на каждом цикле:
- Расчёт нереализованного PnL (в штуках: `лоты × lot`).
- Без трейлинга: SL = вход·(1∓SL%), TP = вход·(1±TP%). С трейлингом (`trailing_stop_enabled`): уровень двигается только в сторону прибыли.
- Закрытие по `STOP_LOSS` / `TAKE_PROFIT` по текущей цене.
- `SHORT_CARRY_CLOSE` — принудительно закрывает шорт с 22:50 МСК перед выходными.
- `ML_EARLY_EXIT` — только убыточные позиции, не раньше 20 минут удержания, при P < 0.30 и «жёстком» режиме модели.
- Перед закрытием проверяется, что позиция ещё открыта в БД (гонка с синхронизацией), затем `_cancel_all_orders_for_figi` (снимаются биржевые стопы) и рыночный ордер в обратную сторону.
- Комиссия: `(вход + выход) × штук × estimated_commission_pct` (0.04%). Вычитается из PnL.
- `daily_pnl` и `trades_today` обновляются **только** в этом пути.
- Закрытие снаружи (сработал биржевой стоп): отдельный путь `_handle_manual_close` (см. 3.4).

### 6.5. Лимиты и защиты

| Защита | Где | Значение по умолчанию |
|---|---|---|
| Сделок в день | главный цикл | 15 |
| Общий убыток за день | главный цикл | 200 ₽ |
| Убыток по бумаге за день | `process_instrument` | `max_daily_loss_rub` бумаги |
| Открытых позиций | координатор / проверка | 3 |
| Кулдаун после ошибки ордера | `order_cooldowns` | 90 с |
| Кулдаун после закрытия | `close_cooldowns` | 60 с |
| Режим сна вне торгов | цикл | при `trade_only_session=1` |
| Дубли закрытия | `_closing_in_progress`, `get_recent_trade` | |
| Лимит API | `_ApiRateTracker` | 570/мин |
| Пол стоп-лосса | `MIN_STOP_LOSS_PCT` | 0.5% |

### 6.6. История churn (тесные стопы)

Симптом: сделки по ~10 секунд, вход = выход, `STOP_LOSS`, потеря = комиссия; ~34 таких в день. Причины (исправлены в v5.8.3):
1. Стоп ~0.3% был уже шума спреда — выбивало на первом цикле.
2. Один тикер состоял в нескольких стратегиях, а `get_instrument_sl_tp` брал `LIMIT 1` — случайное (тесное) значение.
3. Серверный `compute_adaptive_sl_tp` сужал стоп 0.4% → 0.3%.
4. SL/TP кэшируются при старте — после правок оптимизатора нужен **рестарт бота**.

Что осталось: периодический перечит SL/TP без рестарта; разное значение `trades.qty` (активный путь — лоты, `_handle_manual_close` — штуки); мусорные стратегии 31, 33–38, 40, 41, 44; ~34 черновые сделки в обучающей выборке.

### 6.7. Режимы sandbox и боевой

Единственная ветка: `SandboxClient` или `Client` в `main()`. Обходы особенностей sandbox в коде: проверка цены исполнения ±5%, нормализация цены в лотах («цена за лот»), ожидание статуса, запасной расчёт доходности. Режим берётся из `.env` (`TINVEST_USE_SANDBOX`), но UI показывает ещё и настройку `tinvestusesandbox` в БД — это два разных источника.

---

## 7. Модель (ML)

### 7.1. Общая схема

```
свечи 5м (+ 1ч, 4ч), стакан, индикаторы, сигнал
        │ build_features → 30 признаков
        ▼
  store_features → ml_features (при входе)
        │
  сделка закрыта → label_features (label = pnl > 0)
        ▼
  train_model / train_universal_model → ml_models (BLOB)
        ▼
  predict_probability → P(сделка прибыльная)
        ▼
  should_enter, should_exit_early, compute_lot_scale, compute_adaptive_sl_tp
```

Параллельно живёт «самооптимизация Phase 1» (`ml_trade_context`, `ml_instrument_state`, ежедневный ребаланс) — отдельный контур, не связанный с классификатором.

### 7.2. Признаки (`feature_builder.py`)

`FEATURE_NAMES` содержит 30 имён (на вкладке «Обучение» и в `/api/ml/summary` указано 31, расхождение).

| Группа | Признаки |
|---|---|
| 5-мин свечи | `z_score` (z по 20 закрытиям), `sma_gap_pct` ((SMA9−SMA21)/SMA21·100), `momentum_5`, `breakout_dist`, `vol_ratio`, `atr_pct` (14 баров) |
| 1 час | `z_score_1h`, `trend_1h` (−1/0/+1), `volatility_1h` |
| 4 часа | `trend_4h`, `momentum_4h` |
| Стакан | `bid_pressure`, `book_depth_ratio` |
| Индикаторы T-Bank | `rsi` (по умолчанию 50), `macd_diff`, `bb_position` (по умолчанию 0.5) |
| Сигнал | `signal_dir` (+1/−1/0), `signal_score` |
| Время (МСК) | `hour_sin`, `hour_cos`, `is_morning`, `is_close`, `day_of_week`, `position_minutes` |
| Прочее | `vwap_dev`, `session_phase`, `regime` (0–3), `sector_corr` (корреляция с SBER и GAZP), `ticker_hash` (CRC32 тикера % 1000 / 1000), `short_carry_risk` |

`spread_pct` вычисляется и пишется в БД, но в модель не попадает (и всегда 0).

### 7.3. Разметка

`label = 1 if pnl > 0 else 0`. Комиссия, размер позиции, соотношение риск/прибыль не учитываются: сделка с PnL +0.01 ₽ до комиссии считается «выигрышем», хотя после комиссии убыточна. Нулевые сделки — «проигрыш».

### 7.4. Обучение (`model_trainer.py`)

- Константы: `MIN_SAMPLES=30`, `RETRAIN_EVERY=10`, `TEST_RATIO=0.2`.
- Модель: `CalibratedClassifierCV(GradientBoostingClassifier, sigmoid, cv=3)`. По бумаге: 100 деревьев, lr 0.1, глубина 3, `min_samples_leaf=3`, `subsample=0.8`. Универсальная (`figi='UNIVERSAL'`): 150 деревьев, lr 0.08.
- Разбиение хронологическое 80/20. Балансировка классов (дублирование миноритарного класса) применяется к обучению. Метрики: accuracy, precision, recall (F1/AUC нет).
- Активация: precision ≥ 0.58 (по бумаге) или ≥ 0.50 (универсальная) → `active`, иначе `learning`.
- Хранение: pickle в `ml_models.model_data`.
- Запуск: после закрытия сделки, если накопилось ≥10 новых размеченных строк; также при ежедневном ребалансе и вручную кнопкой «Переобучить».

**Слабые места:**
1. Решение об активации принимается по тесту из 6 наблюдений (при 30 примерах).
2. Калибровка идёт по дублированным данным → утечка, оптимистичная калибровка.
3. Новая модель со статусом `learning` **архивирует** прежнюю `active`, и рабочей модели не остаётся.
4. Выход — другое распределение, чем вход (`position_minutes`, `signal_score=0`).
5. Обучающая выборка смещена: в «мягком» режиме вход разрешался независимо от модели.

### 7.5. Применение в торговле (`model_predictor.py`)

Пороги: `ENTRY_THRESHOLD=0.60`, `EXIT_THRESHOLD=0.30`, `MIN_EXIT_PRECISION=0.55`, `MIN_HOLD_MINUTES=20`.

- `predict_probability`: сначала модель по бумаге, потом универсальная, иначе `None`.
- **`should_enter`**: нет модели → вход разрешён («накапливаем данные»). Иначе считается «2 из 3»: условие по скору (`score ≥ max(min_score, 20)`), условие по тренду 1ч (не против сигнала) и условие ML (`P ≥ 0.60`). Нужно ≥2. Модель активна и условий не хватило → блок. Модель не активна → только логирование («мягкий» режим).
  - **Практически ML не работает как вето:** условия по скору и тренду почти всегда истинны (тренд 1ч всегда 0 из-за поломки, п. 7.9), их двух достаточно для прохода при любой вероятности.
- **`should_exit_early`**: только убыточные позиции, ≥20 минут, P < 0.30, «жёсткий» режим (precision универсальной модели ≥ 0.55).
- **`compute_lot_scale`**: P ≥ 0.90 → 1.0; ≥0.80 → 0.8; ≥0.70 → 0.65; ≥0.65 → 0.5; ≥0.60 → 0.35.
- **`compute_adaptive_sl_tp`**: если ATR-фактор > 1.5, расширяет SL (до 1.2%) и TP (до 3.5%). Сужения нет.
- Решения пишутся в `ml_decisions` и (с дедупликацией 60 с) в Telegram.
- Кэш моделей в памяти процесса, без TTL; сбрасывается `invalidate_cache`.

### 7.6. Контур самооптимизации (Phase 1)

- `experience.py`: `record_entry` пишет контекст сделки в `ml_trade_context`; `record_exit` ставит PnL и `quality_score = pnl / часы удержания`.
- `analyzer.py`: `compute_quality` (win rate, expectancy, confidence = min(1, n/30)), `update_ewa_quality` (EWA α=0.2). EWA применяется к пересчёту всей истории, `ewa_quality` в таблицу не пишется и нигде не читается.
- `orchestrator.py`:
  - `on_trade_closed` — мягкое обновление после каждой сделки.
  - `run_daily_rebalance` (при смене дня в `maybe_reset_daily` и вручную): для каждой бумаги `_deep_update`, переобучение универсальной модели, проверка дрейфа, ранжирование (фактически ничего не отключает).
  - `_deep_update`: **запускает бэктест-оптимизацию (режим/SL/TP) по часовым свечам за 90 дней и сам записывает результат в боевые `strategy_settings`/`strategy_instruments`**, без подтверждения пользователя. Пропускается для бумаг без сделок за 90 дней.
  - `_check_concept_drift`: win rate последних 20 сделок < 35% → алерт (текст «Модель переобучена» вводит в заблуждение).
  - `get_ml_params(figi, strategy_id)` — отдаёт выученные параметры воркеру (используется при confidence ≥ 0.30).
- `backtest_client.py`: сетка 3 режима × 4 SL × 4 TP, комиссия 0.04%, минимум 8 сделок, улучшение засчитывается при `quality ≥ 1.10 × базовое`. Нет разделения train/test → высокий риск переобучения; оптимизация на часовых свечах, торговля идёт на минутных.

### 7.7. Таблицы ML

| Таблица | Кто пишет | Кто читает |
|---|---|---|
| `ml_features` | `store_features`, `label_features`, backfill | trainer, drift, дашборд |
| `ml_models` | trainer | predictor, дашборд |
| `ml_decisions` | predictor | дашборд |
| `ml_trade_context` | experience | analyzer, selector, дашборд |
| `ml_instrument_state` | analyzer | orchestrator, predictor |
| `ml_optimization_log` | orchestrator через `log_optimization` | дашборд |

### 7.8. Скрипт `backfill_ml_indicators.py`

Запуск `python scripts/backfill_ml_indicators.py [--all]`. Шаг 1 дозаполняет RSI/MACD/Bollinger и признаки 1ч/4ч у строк `ml_features`, где `rsi = 50.0` (признак «не заполнено»). Шаг 2 (`--all`) создаёт строки признаков для сделок из `trades`, у которых их нет. Ограничения: многие признаки пишутся по умолчанию, `atr_pct` считается иначе, чем в живом коде, на каждую строку 3 запроса к API без пауз; опирается на `trades.open_time` (п. 5.5).

### 7.9. Критическая поломка мультитаймфрейма

`multi_timeframe.get_candles_cached` вызывает `tbank_client.get_candles(client, figi, n=n, interval_name=...)`, а реальная сигнатура — `get_candles(figi, interval_name, hours)`; интервалов `"1hour"` и `"4hour"` там тоже нет. Возникает `TypeError`, он молча проглатывается, свечи пустые. Итог: 5 признаков (`z_score_1h`, `trend_1h`, `volatility_1h`, `trend_4h`, `momentum_4h`) всегда 0; условие «тренд 1ч» в мета-проверке всегда истинно. Весь ML обучается на данных без старших таймфреймов.

Кроме того, индикаторы RSI/MACD/BB для признаков берутся из `state.instrument_meta`, которое, судя по отчёту, нигде не заполняется — модель получает значения по умолчанию (RSI 50, MACD 0) **[проверить поиском по `instrument_meta`]**.

---

## 8. Аналитик, бэктест и недельный цикл

### 8.1. Бэктест (`backtest_engine.py`)

`run_backtest(candles, mode, stop_loss_pct, take_profit_pct, commission_pct=0.0004, initial_capital=100000, qty=1, min_signal_score=0)`.
- Одна позиция одновременно, вход по закрытию бара сигнала.
- SL/TP проверяются по high/low следующих баров; если срабатывают оба в одном баре, берётся SL (консервативно).
- Остаток закрывается по концу данных (`END`).
- Метрики: win rate (%), profit factor, чистый PnL, просадка, R-multiple, Sharpe (приблизительный, для 5/15-мин неверный масштаб), комиссии.
- Отличия от живой торговли: нет трейлинга, ML-выхода, принудительных закрытий, проскальзывания; шорты разрешены всегда; нет переноса шорта.

### 8.2. Аналитик (`analyst.py`)

Два режима, запускаются из вкладки «Аналитик»:
- **Поиск** (`start` → `_worker`): перебор 20 бумаг × 3 режима × SL {0.2, 0.3, 0.4, 0.5}% × TP {0.4–1.5}% (условие `tp ≥ sl·1.5`), подбор лучшего порога скора, фильтры по числу сделок/win rate/PnL, итоговый `score = PF · winrate · штраф_просадки · 100`. Результаты в `analyst_results` (**таблица очищается при каждом новом поиске**). Из результата можно «Сохранить как стратегию».
- **Оптимизация текущих** (`start_optimize` → `_optimization_worker`): для стратегий активного профиля сравнивает текущие параметры с лучшими из сетки, пишет в `optimization_results` с `improvement_pct` (может быть отрицательным — «улучшением» считается любой найденный вариант).

### 8.3. Субботний цикл (`weekly_scheduler.py`)

Поток проверяет раз в 5 минут: суббота после 18:00 МСК и сегодня ещё не запускалось → `run_weekly_cycle`: статистика недели, создание сессии типа `weekly`, запуск `analyst.start(...)` (бюджет 50 000 ₽, интервал 15 мин, 14 дней), ожидание до 10 минут, отправка отчёта в Telegram с предложениями и кнопками «Принять все / Отклонить / Выбрать».

Проблемы:
- Метка `weekly_report_last_run` ставится только по успеху. Если цикл падает — повтор каждые 5 минут весь вечер (спам).
- Запускается воркер **поиска**, а отчёт ждёт поля вида `base_sl/best_sl/improvement_pct`, которые есть в `optimization_results`. Возможна рассогласованность **[не проверено: смотреть `link_results_to_session` и `get_session_results`]**.
- Для субботнего цикла 14 дней по 15 минут дают мало данных при десятках комбинаций — высокий риск переобучения.

### 8.4. Применение результатов

Кнопка «Применить» в сессиях аналитика → `POST /api/analyst/sessions/{id}/apply` → в `webapp.py` вызывает `apply_optimization_result`, который **не импортирован на уровне модуля** (`NameError`, попадает в список ошибок, ничего не применяется). Работает только `optimize-apply/{id}` (локальный импорт) и кнопки Telegram.

---

## 9. Интерфейс (дашборд)

Один адрес: `/dashboard/`. SPA без фреймворков: ванильный JS (`dashboard.js`), единственная библиотека — Plotly 2.35.2 (должна лежать в `/static/plotly-2.35.2.min.js`, но в репозитории её нет; без неё все графики молча не рисуются). Тема одна, тёмная.

Навигация по hash: `#/главное`, `#/портфель`, `#/настройки`, `#/история`, `#/бэктест`, `#/аналитик`, `#/обучение`, `#/справка`.

### 9.1. Вкладки

**Главное** (`renderMainShell` L443, `renderMainData` L613)
- Сводные карточки, сгруппированные: «Сервис» (статус, торговля, профиль, стратегия), «Счёт» (деньги, позиции, резерв, итого), «Сессия» (сделки, PnL реал./нереал., комиссия, тип сессии и таймер, волатильность и ATR), «API» (запросов в минуту против лимита, разбивка по операциям; красный от 95%).
- Рядом ТВ-плеер РБК (iframe, сворачиваемый) и бегущая строка новостей.
- Блок управления: статус, кнопки «Запустить/Остановить/Перезапустить», кнопка «Telegram» (диагностика/тест), сетка «Профиль/Стратегия/Последняя ошибка», выпадающая «Диагностика: N причин» (`/api/dashboard/bot-explain`).
- Предупреждения о нехватке денег (с кнопкой пополнения sandbox).
- Блок Sandbox: пополнение (50k/100k/500k/1M) и сброс баланса.
- «Параллельные стратегии и инструменты»: итоговая строка PnL за день/неделю/месяц, сетка **карточек инструментов** (4 в ряд): тикер, статус потока, сигнал (клик открывает расшифровку: причины, формула скора, разбор RSI/MACD/BB), цена, SL/TP, лоты, мини-график свечей, статистика д/н/м, лимит дня.
- Таблицы «Позиции (API брокера)» с кнопкой «Закрыть» и «Сделки (сегодня)».

**Портфель** (`renderPortfolioTab` L1924): деньги по валютам, позиции брокера, активные ордера и стоп-ордера (с «Отменить» и «Отменить все»), позиции бота (кнопки «Закрыть все», «Очистить записи»).

**Настройки** (`renderSettingsTab` L954):
- Профиль: форма системных настроек, смена токена/счёта (пишет в `.env` на сервере), переключатель «Параллельная торговля».
- При включённом параллельном режиме — таблица стратегий (настройки, инструменты, убрать, удалить).
- Иначе — одна стратегия: форма риск-параметров и список инструментов с формами SL/TP/лотов/авто-лотов/спреда/объёма/лонг/шорт.
- Модальные окна: профили, стратегии, добавление инструментов (поиск/топ).

**История** (`renderHistoryTab` L2204): выбор периода (месяцы, кварталы, годы, «Всё»), 5 карточек сводки, графики Plotly (накопленный PnL с барами сделок, по тикерам, по причинам), подвкладки «Сделки», «Операции брокера» (постранично), «Журнал», «Ошибки», «Система», кнопка «Очистить сделки».

**Бэктест** (`renderBacktestTab` L3141): выбор интервала (5/15 мин, 1 час) и числа дней (1–30), карточки стратегий с чекбоксами, запуск/остановка, прогресс, таблица сравнения 15 метрик, кривая капитала, список сделок. Бэктест берёт только **первый** инструмент стратегии.

**Аналитик** (`renderAnalystTab` L4134): режимы «Поиск новых» и «Оптимизация текущих» (формы параметров, прогресс, таблицы результатов, детали с графиком, «Сохранить как стратегию», «Применить»), история сессий, справочник бумаг с uid.

**Обучение** (`renderLearningTab` L3861): отчёт о ML: статус модели, пайплайн из 6 этапов, каталог признаков, баланс классов, метрики, модели по бумагам, последние решения, лог самооптимизации; единственное действие — «Переобучить модель сейчас».

**Справка** (`renderHelpTab` L3488): статический текст (разделы ML, Оптимизация, Стратегии, Позиции, Настройки, FAQ). Содержит устаревшие числа («36 сделок, accuracy 77.8%»).

### 9.2. Устройство dashboard.js

- **Утилиты:** `apiGet`/`apiPostForm`/`apiPostJson`, `showToast`, `diffTbody`/`diffTbodyFlash` (обновление без полной перерисовки, подсветка изменений), `esc` (L16).
- **Роутинг:** `applyRoute` L4878, `bindRouter` L4909, `bootstrapDashboard` L4982 (экспортирует функции в `window` для inline-`onclick`).
- **Опрос:** `startRefreshLoops` L4928: параллельный статус 3 с, сводка 4 с, котировки 2 с, портфель 5 с, история 8 с, новости 5 мин, сессия 60 с; бэктест 1.2 с, аналитик 2 с на время работы. Таймеры работают только на «своей» вкладке.
- **Состояние:** глобальные переменные и `dataset.*` в DOM.
- **Графики:** Plotly, `Plotly.newPlot` только при смене набора бумаг или по кнопке, чтобы не мигали.

### 9.3. CSS

`dashboard.css` (297 строк): переменные `:root` (`--bg`, `--panel`, `--line`, `--text`, `--muted`, `--blue`, `--green`, `--red`), адаптивные сетки (`auto-fit`), карточки, таблицы со sticky-шапкой (`table{min-width:1400px}` для всех таблиц), toast, модалки. Сетка карточек инструментов и анимации вставляются **из JS** (`<style>` из кода), а не из CSS-файла. Много мёртвых классов и повторяющихся inline-стилей.

### 9.4. Проблемы интерфейса

1. `esc()` **не экранирует HTML** (только приводит к строке). XSS через имена стратегий/тикеры/RSS-новости; `href="${esc(n.link)}"` допускает `javascript:`; inline-`onclick` ломается апострофом в имени.
2. Таблицы позиций и сделок на «Главном» не обновляются по таймеру (только живые цены), новая сделка появится после перезахода.
3. Несколько действий (`removeParallelStrategy`, `analystStop`, `histClearTrades` и др.) используют голый `fetch` без проверки `r.ok` и показывают «успех» при HTTP-ошибке.
4. Опросы без защиты от наложения запросов; `renderPortfolioTab` перерисовывает DOM каждые 5 с.
5. В справке двойной атрибут `class` на кнопках навигации.
6. Дублирование форм настроек стратегии (основной и параллельный режим) с разными полями.
7. `document.title` затирается названием вкладки.

---

## 10. Telegram

### 10.1. Компоненты

- `telegram_notify.TelegramNotifier` — очередь исходящих (200 сообщений), поток-отправщик, не чаще 1 сообщения в 1.1 с, 3 попытки, учитывает `retry_after` при 429. Читает настройки один раз при создании (изменение `.env` требует рестарта). Отправляет простым текстом.
- `telegram_bot.py` — long polling `getUpdates`. Команда **только одна: `/status`** (статус, PnL, сделок, инструменты, ошибка). Остальной текст игнорируется.
- Кнопки оптимизатора (callback с префиксом `opt_`): `opt_accept_all`, `opt_reject`, `opt_select`, `opt_toggle`, `opt_apply_sel`. Состояние выбора хранится в `callback_data` (лимит Telegram 64 байта — риск при многих выбранных).
- `telegram_health.py` — `send_telegram`, `health_snapshot`. Реального фонового мониторинга здоровья нет.
- `weekly_scheduler` шлёт сообщения напрямую через `requests` (в обход очереди).

### 10.2. Авторизация

Только сравнением `chat_id` с `TELEGRAM_CHAT_ID`. Если это групповой чат — управлять сможет любой участник.

### 10.3. Уведомления

Через `notify(message, is_error)` в `main.py`. Настройка `telegram_errors_only=1` оставляет только ошибки.
- Открытие позиции (🟢 BUY / 🔴 SELL): тикер, цена, лоты, сумма, SL/TP, трейлинг.
- Закрытие позиции (✅/🔴): вход → выход, PnL, причина.
- «⚠️ Ошибка бота».
- ML: решения модели, «Универсальная ML-модель готова», ранжирование, дрейф.
- Недельный отчёт и предложения оптимизатора.

### 10.4. Проблемы

Ошибки парсинга callback и `_api` проглатываются на уровне debug; повторное нажатие «Принять» не проверяет статус сессии (возможно повторное применение); `telegram-test` всегда возвращает `ok:true`, результат отправки лежит во вложенном поле; сообщения при исчерпании попыток молча теряются.

---

## 11. Git и версионирование

### 11.1. Репозиторий

- Удалённый: `origin` = `https://github.com/kusbich-code/scalper.git`, основная ветка `main`.
- 294 коммита, первый — 2026-04-18 («Zagruzka pervoi versii scalpera»), последний — 2026-06-18 (v5.8.3). Основная активность: 2026-05-02…2026-06-18.
- Тегов нет. Версия живёт только в `app/version.py` и в префиксе сообщения коммита.
- Ветки: `main` (рабочая), `feature/v3.1`, `copilot/worktree-2026-04-24T17-20-51`, `copilot/worktree-2026-04-24T17-21-12`, `worktree-agent-a26aa13b80eba98d1` (рабочая копия агента в `.claude/worktrees/`, заблокирована). Остальные ветки устарели.
- Состояние на начало ревизии: чисто, кроме неотслеживаемой папки `.claude/`.

### 11.2. Соглашения

- Формат сообщения: `vX.Y.Z Краткое описание на английском` (старые коммиты частично на русском). Для багфиксов часто префикс `Fix ...`.
- Версия повышается вручную в `app/version.py`. **Правка файла только инструментом записи, не `sed`** (ранее «бампы через sed» не срабатывали и версия отстала с 5.7.0 до 5.8.2).
- Версия подставляется в заголовок страницы и в `?v=` для CSS/JS (сброс кэша браузера).
- Коммиты с участием Claude завершаются строкой `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Коммитить только по просьбе пользователя.

### 11.3. Хронология версий

| Период | Версии | Содержание |
|---|---|---|
| 04-18…04-21 | 3.x–4.1 | Первая загрузка, daily reset, basic auth, настройки в дашборде, портфель |
| 04-22…04-28 | 4.2–4.3 | Профили/стратегии, Telegram, вкладка «Аналитик» |
| 05-02…05-05 | 4.4–4.8 | Параллельные стратегии, МСК-время, координатор, нативные стоп-ордера SL/TP на бирже, восстановление позиций при рестарте |
| 05-10 | 4.9.x | Фикс сделок от неисполненных ордеров, self-host Plotly, стакан в БД |
| 05-26…06-02 | 4.9.x | Расчёт PnL в штуках, время/длительность сделок, переработка главной |
| 06-03…06-04 | 5.0–5.3 | Гонки при закрытии, ML-аудит, ТВ и новости, универсальная модель, балансировка классов |
| 06-04…06-05 | 5.3–5.5 | Режим сна, защита шортов от переноса, мульти-позиции, код-ревью (13 багов) |
| 06-08…06-14 | 5.5–5.7 | Вкладка «Обучение», бэктест-оптимизатор, фикс %PnL |
| 06-14…06-18 | 5.8.x | Карточки инструментов, автопереименование стратегий, пол стопа 0.5% (v5.8.3) |

### 11.4. Проблемы гигиены репозитория

- В `.gitignore` строка `.vscode/*.claude/` склеена из двух правил (пропущен перевод строки), поэтому `.claude/` не игнорируется — отсюда `?? .claude/` в статусе. Нужно разнести на `.vscode/*` и `.claude/`.
- `check_js.py` числится в `.gitignore` — разовый скрипт для сервера.
- Ранее случайно коммитились `.claude/worktrees` (убрано коммитом `6652a8a`).
- Нет CI, тестов, `Dockerfile`, `Makefile`, скриптов деплоя.
- `requirements.txt` без закреплённых версий и без `scikit-learn`/`numpy`, хотя ML их использует **[проверить установку на сервере]**.

---

## 12. Деплой и эксплуатация

### 12.1. Окружения

- Разработка: Windows, ручной запуск (`python main.py`, `uvicorn app.webapp:app`).
- Продакшн: Linux-сервер (по записи памяти проекта 72.56.37.140), пользователь `trader`, каталог `/home/trader/apps/trading-bot`, venv `.venv`.

### 12.2. systemd (`deploy/`)

- `trading-bot.service`: `python main.py`, `EnvironmentFile=.env`, `Restart=always`, `RestartSec=10`, логи в `logs/systemd-bot-*.log`.
- `dashboard.service`: `uvicorn app.webapp:app --host 0.0.0.0 --port 80`, `Restart=always`. Порт 80 непривилегированному пользователю без `CAP_NET_BIND_SERVICE` недоступен **[проверить, как это работает в реальном деплое]**.
- `nginx-dashboard.conf`: `/dashboard/` и `/api/` с basic auth → `127.0.0.1:8080`; `/health` и `/static/` без авторизации. Не согласован с юнитом (юнит на 80, nginx ждёт 8080); маршрута `/health` в приложении нет; TLS нет; нет `proxy_read_timeout`.

Вывод: в репозитории два несовместимых описания деплоя. Фактическая рабочая схема неизвестна: либо дашборд открыт наружу на :80 без авторизации, либо юнит правили вручную на сервере.

### 12.3. Как обновить сервер

Скриптов нет. По-видимому, вручную: `git pull`, при необходимости `pip install -r requirements.txt`, `systemctl restart trading-bot dashboard` **[подтвердить у владельца]**. После правок параметров стратегий/оптимизатора обязательно перезапускать бот (кэш SL/TP).

### 12.4. Файлы данных

БД `data/trading_bot.sqlite3`; логи `logs/trading-bot.log`. Резервного копирования в репозитории нет.

---

## 13. Справочник настроек

### 13.1. Профиль (`profile_settings`)

| Ключ | Назначение |
|---|---|
| `bot_enabled` | Бот торгует (1) или стоит на паузе (0) |
| `telegram_errors_only` | Слать в Telegram только ошибки |
| `auto_reload_settings` | Автоперечитывание настроек |
| `tinvestusesandbox` | Sandbox/боевой (в UI «runtime_mode») |
| `parallel_trading_enabled` | Параллельный режим |
| `trade_only_session` | Спать вне торговых часов |
| `max_open_positions` | Лимит открытых позиций (1–12 в UI) |

### 13.2. Стратегия (`strategy_settings`)

`max_trades_per_day` (15), `max_daily_loss_rub` (200), `max_open_positions`, `check_interval_sec` (5), `default_stop_loss_pct` (0.0025), `default_take_profit_pct` (0.005), `estimated_commission_pct` (0.0004), `allow_long_global`, `allow_short_global`, `trade_only_session`, `pause_after_error_sec` (10), `tradingmode` (`trend`/`mean_reversion`/`breakout`), `errorseriespausecount`, `stopseriespausecount`, `trailing_stop_enabled`, `use_signal_service`, `min_signal_score`, `use_api_confirm`, `use_order_book_filter`.
Для `use_api_confirm` и `use_order_book_filter` нет сидов, при создании профиля/стратегии записывается пустая строка `""`.

### 13.3. Инструмент в стратегии (`strategy_instruments`)

`lots_override`, `auto_lots`, `stop_loss_pct`, `take_profit_pct`, `max_spread_pct`, `min_volume`, `allow_long`, `allow_short`, `priority`, `enabled`, `max_daily_loss_rub`, `lot`, `min_price_increment`.
Единицы: проценты хранятся **долями** (0.005 = 0.5%); в UI вводятся в процентах и делятся на 100.

### 13.4. Единицы измерения (частая ловушка)

- `trades.qty` и `positions.qty` — в **лотах**; PnL считается по штукам (`лоты × lot`). В `_handle_manual_close` `qty` пишется в штуках — несогласованно.
- Время: `event_logs` и сделки — МСК строкой; `updated_at` в market state — UTC.

---

## 14. Реестр проблем по приоритету

### Критические (деньги / безопасность)

1. **Нет авторизации на дашборде** и открытый порт 80 в обход nginx. Через API можно закрыть позиции, сбросить sandbox, сменить токены брокера в `.env`, очистить историю, остановить бот.
2. **Два контура обработки одного figi** (главный цикл + параллельные воркеры) → риск дублирующего входа.
3. **Лимиты дня не работают для воркеров и для биржевых стопов**: воркеры не проверяют `max_daily_loss_rub`/`max_trades_per_day`; сделки, закрытые биржевым стопом (`_handle_manual_close`), не увеличивают `daily_pnl`.
4. **Окно без защиты при закрытии**: стопы снимаются до отправки рыночного ордера; при сбое ордера позиция остаётся без биржевого SL/TP до следующего цикла.
5. **Автоматическая перезапись боевых параметров** ML-оптимизацией (`_deep_update`) без подтверждения и без out-of-sample проверки.
6. **XSS** в интерфейсе (`esc` не экранирует).

### Серьёзные (некорректная работа)

7. Мультитаймфрейм сломан (п. 7.9) — 5 признаков всегда 0.
8. ML-порог практически не блокирует вход (п. 7.5).
9. Неудачное переобучение архивирует рабочую модель.
10. Схема БД не содержит `trades.open_time` и `strategy_instruments.max_daily_loss_rub`.
11. SQLite без WAL при трёх писателях.
12. Кнопка «Применить» в сессиях аналитика не работает (`NameError`).
13. Субботний цикл: спам при ошибке, возможная рассогласованность формата результатов.
14. `plotly-2.35.2.min.js` не в репозитории.
15. Таблицы позиций/сделок на главной не обновляются по таймеру.
16. `is_moex_session_open` не учитывает праздники (хотя функция праздников есть).
17. Метка `pnl > 0` без комиссий; малая выборка (6 в тесте) решает активацию модели.

### Средние / низкие

18. `healthcheck` читает неверный ключ `botenabled`; nginx `/health` отдаёт 404.
19. `sandbox/reset` не перезапускает бот и поток портфеля.
20. Баг `session_start.replace(day=day-1)` в новостях 1-го числа месяца.
21. `AVG` + `LIMIT` в `/api/market/session` считает по всей таблице.
22. Несколько путей API на кириллице.
23. `get_strategy_trade_stats` дублирует сделки между стратегиями с общим тикером.
24. Пресеты стратегий пересоздаются при каждом старте.
25. `async def` эндпоинты с блокирующим gRPC; `multi-candles` делает до 30 последовательных запросов (расход лимита API, веб-запросы не учитываются счётчиком бота).
26. Захардкоженный `'_ml_conf' in dir()` в `main.py` (хрупкая проверка).
27. Паузы 5 мин/1 час кодируются сдвигом метки времени относительно 90 с (неочевидно).
28. Устаревшие названия и числа (`BOT_NAME`, справка, заголовки юнитов).

---

## 15. Мёртвый код

Можно удалять после проверки grep-ом:
- `app/stop_orders.py` целиком.
- `app/instruments.py`: `router`, `/search`, `/top` (не подключён).
- `main.py`: `get_ma_signal`, `calc_support_resistance`, `get_signal`, `_cancel_native_stops`, `_CANDLE_TO_INDICATOR_INTERVAL`, `_MOEX_HOLIDAYS`, `_parallel_stop_events`, `MSK_TZ`.
- `app/ml/optimizer.py`: `suggest_sl_tp`, `_filter_by_params`, `_compute_expectancy` (живы `log_optimization`, `get_optimization_log`).
- `app/ml/strategy_selector.select_best_strategy_mode`, `multi_timeframe.trend_direction/compute_volatility/compute_atr`, `orchestrator._apply_strategy_score`, `analyzer.DECAY_LAMBDA/MIN_TRADES`, `model_predictor.rank_instruments` (флаг отключения всегда False).
- `db.py`: `list_enabled_instruments`, вероятно `get_all_runtime`, `get_position_history`, `list_parallel_strategies`, `set_strategy_parallel`, таблица `strategy_instruments_v1`.
- `dashboard.js`: `summaryCard`, `toggleParallel`, `renderCandlesAndScore_unused`, `attachTableFilters`, `histSetPeriod`, таймер на `#_psUpdTime`.
- `dashboard.css`: `.score-*`, `.chart-box`, `.health-*`, `.instrument-pick-*`, дубли.
- Настройки: `DASHBOARD_HOST/PORT/BASIC_AUTH_*`, `BOT_NAME`; зависимости `jinja2`, `httpx` (вероятно не нужны).
- Папка `.claude/worktrees/agent-.../` — устаревшая копия проекта v4.5.6 (не мусорить поиском по ней).

---

## 16. Правила работы агента с этим проектом

1. Прежде чем что-то менять, найдите функцию **по имени** (номера строк в этом документе плывут).
2. Любые данные, нужные обоим процессам, — только через `app/db.py` (и `runtime_state`). Объекты между процессами не передаются.
3. Правки торговой логики делаются в `process_instrument()` (`main.py`). `strategy_engine.py` используется и ботом, и UI/бэктестом: меняя его, меняете реальные сигналы.
4. После изменения SL/TP, режимов, стратегий — напомнить пользователю про **перезапуск бота**.
5. Версию правим файлом `app/version.py` напрямую (инструмент записи), не `sed`.
6. Не трогать `.env`, токены и боевой режим без явной просьбы. Любые действия, которые двигают деньги или удаляют данные (`history/clear`, закрытие позиций, смена токена), требуют отдельного подтверждения пользователя.
7. Не выдумывать поведение: помеченное **[не проверено]** сначала прочитать в коде.
8. Новые таблицы/колонки добавлять в `init_db()` с `ALTER TABLE ... ADD COLUMN` в `try/except`; не забывать про дрейф схемы (п. 5.5).
9. Проценты в БД — доли, в UI — проценты. Количество в позициях — лоты, в PnL — штуки.
10. Время — МСК; не смешивать с `datetime.now()` и `CURRENT_TIMESTAMP`.
11. Для JS после правок проверять синтаксис (`node --check app/static/dashboard.js`) вместо `check_js.py`.
12. Не коммитить без просьбы; формат коммита — `vX.Y.Z Описание`, окончание с `Co-Authored-By`.

---

## 17. Глоссарий

- **figi / uid** — идентификаторы бумаги в T-Bank API. figi — старый, uid (`instrument_uid`) — новый.
- **Лот** — минимальная единица заявки (например, 10 акций). `qty` в позициях — число лотов.
- **SL / TP** — стоп-лосс / тейк-профит; в БД доли от цены входа.
- **Трейлинг-стоп** — стоп, который тянется за ценой в сторону прибыли.
- **Нативный стоп** — стоп-ордер, выставленный на бирже (срабатывает без бота).
- **Лонг / шорт** — покупка / продажа без бумаги. Шорт рискован через выходные («carry»).
- **Профиль / стратегия** — см. п. 5.3.
- **Параллельная торговля** — несколько стратегий одновременно с общим лимитом позиций.
- **Координатор** — `_ParallelCoord`, распределяет слоты позиций между стратегиями.
- **Churn** — множество бессмысленных коротких сделок, съедающих комиссию.
- **Sandbox** — виртуальный счёт T-Bank.
- **Z-score** — отклонение цены от среднего в стандартных отклонениях.
- **Breakout / mean reversion / trend** — пробой / возврат к среднему / следование тренду.
- **Concept drift** — деградация модели, когда рынок меняется.
- **EWA** — экспоненциальное сглаживание.
- **Precision** — доля реально прибыльных сделок среди тех, что модель назвала прибыльными.
- **Phase 1 / Phase 2** — старый (опыт сделок, `ml_trade_context`) и новый (классификатор, `ml_features`) контуры ML.
- **МСК** — UTC+3, основной часовой пояс проекта.
