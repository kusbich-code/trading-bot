# Счёт C в песочнице брокера: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** тренд по фьючерсам (счёт C) сам выставляет заявки в отдельном счёте песочницы брокера и держит там те же позиции, что и бумажный счёт; всё видно и управляется на дашборде.

**Architecture:** исполнитель не повторяет стратегию: цели (вес и контракт по каждому инструменту) берутся из состояния бумажного счёта C после его недельного ребаланса. Исполнитель пересчитывает число контрактов на деньги своего счёта в песочнице, укладывает их в гарантийное обеспечение и выставляет рыночные заявки на разницу с позициями у брокера. Чистая логика (`app/ml/sandbox_c.py`) отделена от обращений к брокеру (`app/services/sandbox_c_broker.py`), чтобы проверяться тестами без SDK.

**Tech Stack:** Python 3.11, SDK `t_tech.invest` (песочница: `SandboxClient`), SQLite, FastAPI, ванильный JS.

**Spec:** решение владельца 10.11 по `docs/superpowers/plans/2026-11-10-audit.md` («Счёт C в песочнице»), правила проекта в `CLAUDE.md`.

## Global Constraints

- Только песочница: при `TINVEST_USE_SANDBOX` не равном true исполнитель ничего не отправляет и пишет причину.
- Отдельный счёт песочницы для C, его id хранится в `runtime_state` под ключом `sandbox_c_account_id`; счёт минутных стратегий не трогается.
- Заявки уходят только при включённом переключателе «Счёт C в песочнице» (`sandbox_c_enabled`, по умолчанию 0).
- Стартовый капитал счёта 1 000 000 ₽ (как `paper_futures.START_EQUITY`).
- Гарантийное обеспечение всех позиций не больше 50% денег счёта; иначе цели уменьшаются пропорционально.
- Предел убытка за день 5% денег счёта: при превышении новые заявки не выставляются до следующего дня, в Telegram уходит сообщение.
- Идентификатор заявки детерминирован: `uuid5(NAMESPACE_URL, f"C|{week}|{ticker}|{attempt}")`, чтобы повтор после перезапуска не задвоил заявку.
- Комментарии в коде, строки интерфейса, журнала и коммиты — на русском. Версия в `app/version.py` меняется в том же коммите.
- Правило владельца: всё, что делает исполнитель, видно на дашборде; управление (включить, создать счёт, закрыть все позиции) — кнопками.
- Тесты пишутся первыми (TDD); локально `.venv/Scripts/python.exe -m pytest tests -q`.

## Review Focus

1. **Перезапуск бота посреди ребаланса:** уже отправленные заявки не повторяются, недостающие отправляются. Тест `test_restart_does_not_duplicate_orders` в задаче 3.
2. **Заявку отклонил брокер** (не хватает обеспечения, торги закрыты, нет ликвидности): ошибка записывается, заявка повторяется не больше 3 раз за неделю, остальные инструменты не страдают. Тест `test_rejected_order_is_recorded_and_retried_at_most_three_times` в задаче 3.
3. **Переход на новый контракт:** старый закрывается, новый открывается в одном ребалансе. Тест `test_roll_closes_old_and_opens_new` в задаче 2.
4. **Счёт песочницы пропал** (сброс, закрыт вручную): исполнитель не создаёт счёт сам, показывает «счёта нет» и кнопку создания. Тест `test_missing_account_stops_without_orders` в задаче 3.
5. **Бумажный счёт не сделал ребаланс** на этой неделе (`no_prices`, `no_history`): исполнитель ничего не отправляет. Тест `test_no_orders_without_paper_rebalance_this_week` в задаче 3.

---

### Task 1: Проверка песочницы на фьючерсах

Без этого остальное не имеет смысла. Владелец разрешил заявки в песочнице для счёта C.

**Files:**
- Create: `scripts/sandbox_c_probe.py`

- [x] **Step 1:** скрипт по умолчанию только читает: открытые счета песочницы, справочник фьючерсов группы CNY (`paper_futures.ROOTS`), гарантийное обеспечение `client.instruments.get_futures_margin(instrument_id=uid)` для самого ликвидного контракта, статус торгов.
- [x] **Step 2:** с `--apply`: открыть отдельный счёт песочницы, пополнить на 1 000 000 ₽ (`sandbox_pay_in`), купить 1 контракт рыночной заявкой, прочитать портфель (позиция видна, цена исполнения, комиссия), продать 1 контракт, прочитать операции. Счёт не закрывать: его id напечатать.
- [x] **Step 3:** запустить на сервере в торговые часы: `.venv/bin/python scripts/sandbox_c_probe.py`, затем с `--apply`. Ожидается: позиция появилась и закрылась, обеспечение и комиссия известны. Если песочница фьючерсы не исполняет, план останавливается, итог записывается сюда и сообщается владельцу.
- [x] **Step 4:** записать в этот план фактические поля ответа (обеспечение, комиссия, единица количества) — от них зависят задачи 2 и 3. Коммит скрипта.

**Итог задачи 1 (10.11, 19:57 МСК, контракт CRZ6 на юань):** песочница исполняет фьючерсы.
- Заявка: `execution_report_status` 1 (исполнена), `lots_executed` в контрактах (лот 1), `executed_order_price` в рублях за контракт (12 584 ₽ при котировке 12,584), комиссия 6,29 ₽ = 0,05% объёма.
- Стоимость контракта в рублях = котировка × `min_price_increment_amount` / `min_price_increment` (12,584 × 1 / 0,001).
- Гарантийное обеспечение: `get_futures_margin(instrument_id=uid)` → `initial_margin_on_buy` 1 104,86 ₽, `initial_margin_on_sell` 1 126,19 ₽ за контракт.
- Портфель: позиция фьючерса в контрактах, `average_position_price` и `current_price` — котировка, а не рубли. **`total_amount_portfolio` включает полный объём фьючерса** (1 012 577 ₽ при деньгах 999 994 ₽), поэтому деньги счёта = рубли на счёте + Σ (текущая − средняя) × количество × стоимость пункта.
- Операции: BUY/SELL на полный объём, отдельные BROKER_FEE.
- Счёт пробы `…6bd185` (999 987 ₽) остаётся, но для работы создаётся отдельный счёт кнопкой (задача 4).

### Task 2: Чистая логика: цели, обеспечение, заявки

**Files:**
- Create: `app/ml/sandbox_c.py`
- Test: `tests/test_sandbox_c.py`

**Interfaces:**
- Consumes: `paper_futures.to_contracts(weights: Dict[str, float], equity: float, unit: Dict[str, float]) -> Dict[str, int]`; состояние бумажного счёта `paper_daily._load("C")` с ключами `last_week`, `signals[group] = {"weight": float, "ticker": str | None, ...}`.
- Produces:
  - `targets_from_paper(state: dict, equity: float, unit: Dict[str, float]) -> Dict[str, int]` — тикер контракта → целевое число контрактов (знак = сторона).
  - `fit_margin(targets: Dict[str, int], margin: Dict[str, float], equity: float, max_share: float = 0.5) -> Dict[str, int]` — пропорциональное уменьшение до лимита, округление к нулю.
  - `plan_orders(targets: Dict[str, int], held: Dict[str, int]) -> List[dict]` — `{"ticker", "qty" (>0), "direction": "BUY"|"SELL"}`; сначала закрытия и сокращения, потом открытия.
  - `daily_loss_exceeded(equity_now: float, equity_day_start: float, limit: float = 0.05) -> bool`.
  - `order_id(week: str, ticker: str, attempt: int) -> str`.

- [ ] **Step 1: Падающие тесты** в `tests/test_sandbox_c.py`:
  - `test_targets_follow_paper_weights`: вес USD −0,4, равный юнит → знак и число как у `to_contracts`.
  - `test_fit_margin_scales_down_proportionally`: цели {A: 10, B: −10}, обеспечение по 60 000 ₽, деньги 1 000 000 → {A: 4, B: −4} (≤ 500 000).
  - `test_roll_closes_old_and_opens_new`: held {SiZ6: −5}, targets {SiH7: −5} → заявки [BUY 5 SiZ6, SELL 5 SiH7] в этом порядке.
  - `test_reduce_before_open`: held {A: 6}, targets {A: 2, B: 3} → [SELL 4 A, BUY 3 B].
  - `test_nothing_to_do`: targets == held → [].
  - `test_daily_loss`: 950 000 от 1 000 000 → True; 951 000 → False.
  - `test_order_id_is_stable`: одинаковые аргументы → одинаковый id; другой attempt → другой.
- [ ] **Step 2:** запустить, убедиться, что падают (`ModuleNotFoundError`).
- [ ] **Step 3:** реализовать функции из Interfaces в `app/ml/sandbox_c.py`.
- [ ] **Step 4:** тесты проходят.
- [ ] **Step 5:** коммит `vX.Y.Z Счёт C в песочнице: расчёт целей, обеспечения и заявок`.

### Task 3: Исполнитель и брокер

**Files:**
- Create: `app/services/sandbox_c_broker.py`, `app/ml/sandbox_c_runner.py`
- Modify: `app/db.py` (таблица `sandbox_c_orders`)
- Test: `tests/test_sandbox_c_runner.py`

**Interfaces:**
- Consumes: функции задачи 2; `paper_daily._load("C")`, `paper_daily.week_key(now)`, `paper_daily.in_window(now)`.
- Produces:
  - Класс `SandboxCBroker(client, account_id)`: `equity() -> float`, `positions() -> Dict[str, int]`, `margin(tickers) -> Dict[str, float]`, `units(tickers) -> Dict[str, float]`, `post_market(ticker, qty, direction, order_id) -> dict` (`{"status": "filled"|"rejected", "price", "error"}`).
  - Функция `create_account(client) -> str`: открыть и пополнить на 1 000 000 ₽, вернуть id; вызывается только кнопкой.
  - `run_step(now, broker_factory) -> str | None` в `sandbox_c_runner.py`: `"disabled"`, `"no_account"`, `"not_sandbox"`, `"waiting_paper"`, `"loss_limit"`, `"done"`, `"partial"` или None (не время).
  - Таблица `sandbox_c_orders(id, week, ticker, qty, direction, order_id UNIQUE, attempt, status, price, error, created_at)`.

- [ ] **Step 1: Падающие тесты** с поддельным брокером (словарь позиций, список заявок):
  - `test_no_orders_without_paper_rebalance_this_week`: `last_week` бумажного счёта прошлая → `"waiting_paper"`, заявок 0.
  - `test_orders_bring_positions_to_target`: после шага позиции брокера равны целям, в таблице статус filled.
  - `test_restart_does_not_duplicate_orders`: два шага подряд в одну неделю → заявки отправлены один раз.
  - `test_rejected_order_is_recorded_and_retried_at_most_three_times`: брокер отклоняет A → ошибка записана, попыток 3, B исполнен; 4-й шаг A не шлёт.
  - `test_missing_account_stops_without_orders`: id счёта не задан → `"no_account"`.
  - `test_disabled_or_not_sandbox_sends_nothing`.
  - `test_loss_limit_blocks_new_orders`.
- [ ] **Step 2:** тесты падают.
- [ ] **Step 3:** реализовать брокера (SDK вызовы из задачи 1) и `run_step`.
- [ ] **Step 4:** тесты проходят, полный прогон зелёный.
- [ ] **Step 5:** коммит.

### Task 4: Подключение, управление и экран

**Files:**
- Modify: `main.py` (в `_paper_daily_worker` после строки с `report("C", ...)` вызвать `sandbox_c_runner.run_step`), `app/ml/paper_view.py` (`MODE_CONTROLS` += `sandbox_c_enabled`), `app/webapp.py`, `app/static/dashboard.js`, справка.
- Test: `tests/test_strategy_view.py`

**Interfaces:**
- API: `GET /api/sandbox-c/status` → `{"enabled", "account_id", "equity", "positions": {тикер: {"held", "target"}}, "orders": [последние 20], "last_result", "loss_limit_hit"}`; `POST /api/sandbox-c/create-account`; `POST /api/sandbox-c/close-all` (рыночные заявки на закрытие всех позиций счёта C, с подтверждением на странице).

- [ ] **Step 1: Падающие тесты:** набор ключей `controls` включает `sandbox_c_enabled` (обновить `test_overview_lists_controls_with_state`); маршруты отвечают и есть в `dashboard.js` (`test_every_api_route_is_used_by_the_interface`); `close-all` без счёта отвечает понятной ошибкой 400.
- [ ] **Step 2:** реализация. Блок «Песочница брокера» на вкладке «Стратегия» в разделе счёта C: состояние, деньги у брокера и у бумажного счёта рядом, таблица «у брокера / цель» по контрактам, последние заявки с ошибками, кнопки «Создать счёт», «Закрыть все позиции». Справка: раздел о песочнице, пределах и кнопках.
- [ ] **Step 3:** тесты проходят, полный прогон зелёный.
- [ ] **Step 4:** коммит, выкладка по `CLAUDE.md`, проверка настоящей страницы.

### Task 5: Запуск и наблюдение

- [ ] Владелец нажимает «Создать счёт» и включает переключатель. Первый ребаланс — ближайший рабочий понедельник после 10:05 МСК.
- [ ] После ребаланса: позиции у брокера равны целям или расхождение объяснено в таблице; в журнале нет ошибок; итог виден на странице.
- [ ] Через 12 недель сравнить итог песочницы с бумажным счётом C: разница — это исполнение (цены заявок, комиссия, обеспечение).
