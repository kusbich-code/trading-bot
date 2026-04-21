const REFRESH_SUMMARY_MS = 7000;
const REFRESH_QUOTES_MS = 5000;
const REFRESH_PORTFOLIO_MS = 10000;

let instrumentSearchData = [];
let refreshTimersStarted = false;
const ALLOWED_TABS = new Set(["главное", "портфель", "настройки", "история"]);

function esc(v) {
    return String(v ?? "");
}

function yesnoValue(v) {
    return String(v) === "1" ? "Да" : "Нет";
}

function showToast(message, type = "info", timeout = 2600) {
    const host = document.getElementById("toastHost");
    if (!host) return;

    const div = document.createElement("div");
    div.className = `toast toast-${type}`;
    div.textContent = message;
    host.appendChild(div);

    requestAnimationFrame(() => div.classList.add("toast-show"));

    setTimeout(() => {
        div.classList.remove("toast-show");
        setTimeout(() => div.remove(), 250);
    }, timeout);
}

async function apiGet(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
}

async function apiPostForm(url, data) {
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(data)) {
        body.append(k, v);
    }

    const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
        credentials: "same-origin",
    });

    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
}

function normalizeTab(raw) {
    const tab = String(raw || "").trim().toLowerCase();
    return ALLOWED_TABS.has(tab) ? tab : "главное";
}

function getTabFromHash() {
    const hash = window.location.hash || "";
    if (!hash.startsWith("#/")) return "главное";
    return normalizeTab(hash.slice(2).split("?")[0].split("/")[0]);
}

function setActiveTabButton(tab) {
    document.querySelectorAll("[data-tab-link]").forEach((el) => {
        el.classList.toggle("active", el.dataset.tabLink === tab);
    });
}

function setVisibleView(tab) {
    const normalized = normalizeTab(tab);

    document.querySelectorAll("[data-view]").forEach((el) => {
        el.classList.add("hidden");
    });

    const active = document.querySelector(`[data-view="${normalized}"]`);
    if (active) active.classList.remove("hidden");

    setActiveTabButton(normalized);
}

async function applyRoute() {
    const tab = getTabFromHash();
    setVisibleView(tab);

    if (tab === "главное") {
        await renderMainShell();
        await renderMainData();
    } else if (tab === "портфель") {
        await renderPortfolioTab();
    } else if (tab === "настройки") {
        await renderSettingsTab();
    } else if (tab === "история") {
        await renderHistoryTab();
    }
}

function bindRouter() {
    document.addEventListener("click", async (e) => {
        const link = e.target.closest("[data-tab-link]");
        if (!link) return;

        e.preventDefault();
        e.stopPropagation();

        const tab = normalizeTab(link.dataset.tabLink);
        const newHash = `#/${tab}`;

        if (window.location.hash === newHash) {
            await applyRoute();
            return;
        }

        window.location.hash = newHash;
    });

    window.addEventListener("hashchange", applyRoute);

    if (!window.location.hash || !window.location.hash.startsWith("#/")) {
        window.location.hash = "#/главное";
    } else {
        applyRoute();
    }
}

function diffTbody(tbody, rowsHtml) {
    if (!tbody) return;
    const next = rowsHtml.trim();
    if (tbody.dataset.renderedHtml === next) return;
    tbody.innerHTML = next;
    tbody.dataset.renderedHtml = next;
}

async function renderSummaryCards() {
    const s = await apiGet("/api/dashboard/summary");
    const host = document.getElementById("summaryCards");
    if (!host) return;

    const html = `
        <div class="card"><div class="label">Статус</div><div class="value">${esc(s.status)}</div></div>
        <div class="card"><div class="label">Торговля</div><div class="value">${s.bot_enabled === "1" ? "Включена" : "Выключена"}</div></div>
        <div class="card"><div class="label">Сделок сегодня</div><div class="value">${esc(s.trades_today)}</div></div>
        <div class="card"><div class="label">PNL за день</div><div class="value">${esc(s.daily_pnl_ui ?? s.daily_pnl ?? "0.00")}</div></div>
        <div class="card"><div class="label">Комиссии за день</div><div class="value">${esc(s.total_commission_ui ?? s.total_commission ?? "0.00")}</div></div>
        <div class="card"><div class="label">Баланс на старте</div><div class="value">${esc(s.session_balance_start_ui ?? s.session_balance_start ?? "0.00")}</div></div>
        <div class="card"><div class="label">Текущий баланс</div><div class="value">${esc(s.session_balance_current_ui ?? s.session_balance_current ?? "0.00")}</div></div>
        <div class="card"><div class="label">Профиль настроек</div><div class="value">${esc(s.active_profile_name)}</div></div>
        <div class="card"><div class="label">Стратегия торговли</div><div class="value">${esc(s.active_strategy_name)}</div></div>
        <div class="card"><div class="label">Последняя ошибка</div><div class="value">${esc(s.last_error || "-")}</div></div>
    `;

    if (host.dataset.renderedHtml !== html) {
        host.innerHTML = html;
        host.dataset.renderedHtml = html;
    }
}

async function renderMainShell() {
    const host = document.getElementById("view-main");
    if (!host) return;

    if (host.dataset.initialized === "1") return;

    host.innerHTML = `
        <section class="block">
            <div class="block-head">
                <h2>Управление торговлей и сервисом</h2>
            </div>
            <div class="row-buttons">
                <button class="btn btn-primary" id="btnStartService">Запустить</button>
                <button class="btn" id="btnStopService">Остановить</button>
                <button class="btn" id="btnRestartService">Перезапустить</button>
            </div>
        </section>

        <section class="block">
            <div class="block-head">
                <h2>Выбранные инструменты</h2>
                <div class="note">Обновляются только строки таблицы и котировки</div>
            </div>
            <div class="table-wrap">
                <table>
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
                    <tbody id="mainInstrumentsBody"></tbody>
                </table>
            </div>
        </section>

        <section class="two-cols">
            <div class="block">
                <div class="block-head"><h2>Открытые позиции</h2></div>
                <div class="table-wrap">
                    <table>
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
                        <tbody id="mainPositionsBody"></tbody>
                    </table>
                </div>
            </div>

            <div class="block">
                <div class="block-head"><h2>Последние сделки</h2></div>
                <div class="table-wrap">
                    <table>
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
                        <tbody id="mainTradesBody"></tbody>
                    </table>
                </div>
            </div>
        </section>
    `;

    host.dataset.initialized = "1";

    document.getElementById("btnStartService")?.addEventListener("click", () => serviceAction("start"));
    document.getElementById("btnStopService")?.addEventListener("click", () => serviceAction("stop"));
    document.getElementById("btnRestartService")?.addEventListener("click", () => serviceAction("restart"));
}

async function renderMainData() {
    const data = await apiGet("/api/dashboard/main");

    const instrumentsRows = (data.instruments || []).map((i) => `
        <tr data-figi="${esc(i.figi || "")}">
            <td>${esc(i.ticker)}</td>
            <td>${esc(i.name)}</td>
            <td>${yesnoValue(i.enabled)}</td>
            <td>${esc(i.lots_override)}</td>
            <td>${esc(i.stop_loss_pct_ui ?? i.stop_loss_pct ?? "0.00")}</td>
            <td>${esc(i.take_profit_pct_ui ?? i.take_profit_pct ?? "0.00")}</td>
            <td class="live-price" data-figi="${esc(i.figi || "")}">${esc(i.last_price_ui ?? i.last_price ?? "0.00")}</td>
            <td class="live-time" data-figi="${esc(i.figi || "")}">${esc(i.price_time || "-")}</td>
        </tr>
    `).join("");

    const positionsRows = (data.positions || []).map((p) => `
        <tr>
            <td>${esc(p.ticker)}</td>
            <td>${esc(p.direction)}</td>
            <td>${esc(p.qty)}</td>
            <td>${esc(p.entry_price_ui ?? p.entry_price ?? "0.00")}</td>
            <td>${esc(p.current_price_ui ?? p.current_price ?? "0.00")}</td>
            <td>${esc(p.unrealized_pnl_ui ?? p.unrealized_pnl ?? "0.00")}</td>
            <td>${esc(p.opened_at)}</td>
        </tr>
    `).join("");

    const tradesRows = (data.trades || []).map((t) => `
        <tr>
            <td>${esc(t.time)}</td>
            <td>${esc(t.ticker)}</td>
            <td>${esc(t.direction)}</td>
            <td>${esc(t.entry_ui ?? t.entry ?? "0.00")}</td>
            <td>${esc(t.exit_ui ?? t.exit ?? "0.00")}</td>
            <td>${esc(t.qty)}</td>
            <td>${esc(t.pnl_ui ?? t.pnl ?? "0.00")}</td>
            <td>${esc(t.reason)}</td>
        </tr>
    `).join("");

    diffTbody(document.getElementById("mainInstrumentsBody"), instrumentsRows);
    diffTbody(document.getElementById("mainPositionsBody"), positionsRows);
    diffTbody(document.getElementById("mainTradesBody"), tradesRows);
}

async function refreshQuotesOnly() {
    if (getTabFromHash() !== "главное") return;

    const quotes = await apiGet("/api/dashboard/quotes");
    const map = {};
    for (const q of quotes) {
        map[q.figi] = q;
    }

    document.querySelectorAll(".live-price[data-figi]").forEach((el) => {
        const figi = el.dataset.figi;
        if (map[figi]) el.textContent = map[figi].last_price_ui ?? map[figi].last_price ?? "0.00";
    });

    document.querySelectorAll(".live-time[data-figi]").forEach((el) => {
        const figi = el.dataset.figi;
        if (map[figi]) el.textContent = map[figi].price_time ?? "-";
    });
}

async function renderPortfolioTab() {
    const host = document.getElementById("view-portfolio");
    if (!host) return;

    const data = await apiGet("/api/dashboard/portfolio");

    const portfolioRows = (data.portfolio_positions || []).map((p) => `
        <tr>
            <td>${esc(p.ticker)}</td>
            <td>${esc(p.figi)}</td>
            <td>${esc(p.instrument_type)}</td>
            <td>${esc(p.quantity_ui ?? p.quantity ?? "0.00")}</td>
            <td>${esc(p.average_position_price_ui ?? p.average_position_price ?? "0.00")}</td>
            <td>${esc(p.current_price_ui ?? p.current_price ?? "0.00")}</td>
            <td>${esc(p.expected_yield_ui ?? p.expected_yield ?? "0.00")}</td>
        </tr>
    `).join("");

    const botRows = (data.bot_positions || []).map((p) => `
        <tr>
            <td>${esc(p.ticker)}</td>
            <td>${esc(p.figi)}</td>
            <td>${esc(p.direction)}</td>
            <td>${esc(p.qty)}</td>
            <td>${esc(p.entry_price_ui ?? p.entry_price ?? "0.00")}</td>
            <td>${esc(p.current_price_ui ?? p.current_price ?? "0.00")}</td>
            <td>${esc(p.unrealized_pnl_ui ?? p.unrealized_pnl ?? "0.00")}</td>
            <td>
                <button class="btn btn-danger" data-close-one data-figi="${esc(p.figi)}" data-qty="${esc(p.qty)}" data-direction="${esc(p.direction)}">Закрыть</button>
            </td>
        </tr>
    `).join("");

    const stopRows = (data.stop_orders || []).map((s) => `
        <tr>
            <td>${esc(s.stop_order_id)}</td>
            <td>${esc(s.figi)}</td>
            <td>${esc(s.quantity)}</td>
            <td>${esc(s.currency)}</td>
            <td>${esc(s.order_type)}</td>
            <td>${esc(s.direction)}</td>
            <td><button class="btn btn-danger" data-cancel-stop data-stop-id="${esc(s.stop_order_id)}">Отменить</button></td>
        </tr>
    `).join("");

    const stopOptions = (data.bot_positions || []).map((p) => `
        <option value="${esc(p.figi)}|${esc(p.qty)}|${esc(p.direction)}|${esc(p.entry_price_raw ?? p.entry_price ?? "0")}">
            ${esc(p.ticker)} | ${esc(p.figi)}
        </option>
    `).join("");

    host.innerHTML = `
        <section class="block">
            <div class="block-head">
                <h2>Портфель по счёту</h2>
                <div class="note">Обновляется отдельно, без полной перезагрузки dashboard</div>
            </div>
            <div class="table-wrap">
                <table>
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
                    <tbody>${portfolioRows}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head">
                <h2>Открытые позиции бота</h2>
                <div class="row-buttons">
                    <button class="btn btn-danger" id="btnCloseAllPositions">Закрыть все позиции</button>
                </div>
            </div>
            <div class="table-wrap">
                <table>
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
                    <tbody>${botRows}</tbody>
                </table>
            </div>
        </section>

        <section class="two-cols">
            <div class="block">
                <div class="block-head"><h2>Создать стоп-заявки</h2></div>
                <form id="stopOrderForm" class="form-grid">
                    <label>Позиция
                        <select class="field" id="positionForStops">${stopOptions}</select>
                    </label>
                    <label>Базовая цена
                        <input class="field" type="text" id="stop_base_price" required>
                    </label>
                    <label>Стоп-лосс %
                        <input class="field" type="text" id="stop_loss_pct" value="0.25">
                    </label>
                    <label>Тейк-профит %
                        <input class="field" type="text" id="take_profit_pct" value="0.50">
                    </label>
                    <div class="row-buttons">
                        <button type="button" class="btn btn-primary" id="btnCreateStops">Создать стоп-заявки</button>
                    </div>
                </form>
            </div>

            <div class="block">
                <div class="block-head"><h2>Активные стоп-заявки</h2></div>
                <div class="table-wrap">
                    <table>
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
                        <tbody>${stopRows}</tbody>
                    </table>
                </div>
            </div>
        </section>
    `;

    const select = document.getElementById("positionForStops");
    const basePrice = document.getElementById("stop_base_price");
    if (select && basePrice) {
        const fill = () => {
            const parts = (select.value || "").split("|");
            basePrice.value = parts[3] || "0";
        };
        fill();
        select.addEventListener("change", fill);
    }

    document.getElementById("btnCloseAllPositions")?.addEventListener("click", closeAllPositionsConfirm);
    document.getElementById("btnCreateStops")?.addEventListener("click", createStopOrders);

    host.querySelectorAll("[data-close-one]").forEach((btn) => {
        btn.addEventListener("click", () => {
            closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction);
        });
    });

    host.querySelectorAll("[data-cancel-stop]").forEach((btn) => {
        btn.addEventListener("click", () => {
            cancelStopOrder(btn.dataset.stopId);
        });
    });
}

async function renderSettingsTab() {
    const host = document.getElementById("view-settings");
    if (!host) return;

    const data = await apiGet("/api/dashboard/settings");
    const s = data.settings || {};
    const profiles = data.profiles || [];
    const strategies = data.strategies || [];
    const instruments = data.instruments || [];

    const profileRows = profiles.map((p) => `
        <tr>
            <td>${esc(p.profile_name)}</td>
            <td>${p.is_active === 1 ? "Да" : "Нет"}</td>
            <td>${esc(p.created_at)}</td>
            <td><button class="btn" data-activate-profile="${esc(p.profile_name)}">Активировать</button></td>
        </tr>
    `).join("");

    const strategyRows = strategies.map((x) => `
        <tr>
            <td>${esc(x.strategy_name)}</td>
            <td>${x.is_active === 1 ? "Да" : "Нет"}</td>
            <td>${esc(x.created_at)}</td>
            <td><button class="btn" data-activate-strategy="${esc(x.strategy_name)}">Активировать</button></td>
            <td><button class="btn" data-save-strategy="${esc(x.strategy_name)}">Перезаписать</button></td>
        </tr>
    `).join("");

    const instrumentCards = instruments.map((i) => `
        <div class="block">
            <div class="block-head">
                <h2>${esc(i.ticker)} — ${esc(i.name)}</h2>
                <div class="note">FIGI: ${esc(i.figi)}</div>
            </div>
            <form class="form-grid instrument-form" data-figi="${esc(i.figi)}">
                <label>Лотов бота <input class="field" name="lots_override" type="number" value="${esc(i.lots_override)}"></label>
                <label>Стоп-лосс % <input class="field" name="stop_loss_pct" type="text" value="${esc(i.stop_loss_pct_ui ?? i.stop_loss_pct ?? "0.00")}"></label>
                <label>Тейк-профит % <input class="field" name="take_profit_pct" type="text" value="${esc(i.take_profit_pct_ui ?? i.take_profit_pct ?? "0.00")}"></label>
                <label>Макс. спред % <input class="field" name="max_spread_pct" type="text" value="${esc(i.max_spread_pct_ui ?? i.max_spread_pct ?? "0.00")}"></label>
                <label>Мин. объём 1м <input class="field" name="min_volume" type="number" value="${esc(i.min_volume || 0)}"></label>
                <label>Разрешить Long
                    <select class="field" name="allow_long">
                        <option value="1" ${String(i.allow_long) === "1" || i.allow_long === 1 ? "selected" : ""}>Да</option>
                        <option value="0" ${String(i.allow_long) === "0" || i.allow_long === 0 ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Разрешить Short
                    <select class="field" name="allow_short">
                        <option value="1" ${String(i.allow_short) === "1" || i.allow_short === 1 ? "selected" : ""}>Да</option>
                        <option value="0" ${String(i.allow_short) === "0" || i.allow_short === 0 ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Приоритет <input class="field" name="priority" type="number" value="${esc(i.priority || 100)}"></label>
                <label>Использовать
                    <select class="field" name="enabled">
                        <option value="1" ${String(i.enabled) === "1" || i.enabled === 1 ? "selected" : ""}>Да</option>
                        <option value="0" ${String(i.enabled) === "0" || i.enabled === 0 ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <div class="row-buttons">
                    <button type="submit" class="btn btn-primary">Сохранить</button>
                    <button type="button" class="btn btn-danger" data-delete-instrument="${esc(i.figi)}">Удалить</button>
                </div>
            </form>
        </div>
    `).join("");

    host.innerHTML = `
        <section class="block">
            <div class="block-head"><h2>Общие системные настройки</h2></div>
            <form id="systemSettingsForm" class="form-grid">
                <label>Торговля включена
                    <select class="field" name="bot_enabled">
                        <option value="1" ${String(s.bot_enabled) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.bot_enabled) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Telegram только ошибки
                    <select class="field" name="telegram_errors_only">
                        <option value="1" ${String(s.telegram_errors_only) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.telegram_errors_only) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Автоперечитывание настроек
                    <select class="field" name="auto_reload_settings">
                        <option value="1" ${String(s.auto_reload_settings) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.auto_reload_settings) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <div class="row-buttons">
                    <button type="button" class="btn btn-primary" id="btnSaveSystemSettings">Сохранить системные настройки</button>
                </div>
            </form>
        </section>

        <section class="block">
            <div class="block-head"><h2>Стратегия торговли</h2></div>
            <form id="strategySettingsForm" class="form-grid">
                <label>Макс. сделок в день <input class="field" name="max_trades_per_day" type="text" value="${esc(s.max_trades_per_day || "15")}"></label>
                <label>Макс. дневной убыток <input class="field" name="max_daily_loss_rub" type="text" value="${esc(s.max_daily_loss_rub_ui ?? s.max_daily_loss_rub ?? "0.00")}"></label>
                <label>Макс. открытых позиций <input class="field" name="max_open_positions" type="text" value="${esc(s.max_open_positions || "2")}"></label>
                <label>Интервал проверки, сек <input class="field" name="check_interval_sec" type="text" value="${esc(s.check_interval_sec || "5")}"></label>
                <label>Стоп-лосс по умолчанию <input class="field" name="default_stop_loss_pct" type="text" value="${esc(s.default_stop_loss_pct_ui ?? s.default_stop_loss_pct ?? "0.00")}"></label>
                <label>Тейк-профит по умолчанию <input class="field" name="default_take_profit_pct" type="text" value="${esc(s.default_take_profit_pct_ui ?? s.default_take_profit_pct ?? "0.00")}"></label>
                <label>Оценка комиссии <input class="field" name="estimated_commission_pct" type="text" value="${esc(s.estimated_commission_pct_ui ?? s.estimated_commission_pct ?? "0.00")}"></label>
                <label>Разрешить Long
                    <select class="field" name="allow_long_global">
                        <option value="1" ${String(s.allow_long_global) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.allow_long_global) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Разрешить Short
                    <select class="field" name="allow_short_global">
                        <option value="1" ${String(s.allow_short_global) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.allow_short_global) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Только торговая сессия
                    <select class="field" name="trade_only_session">
                        <option value="1" ${String(s.trade_only_session) === "1" ? "selected" : ""}>Да</option>
                        <option value="0" ${String(s.trade_only_session) === "0" ? "selected" : ""}>Нет</option>
                    </select>
                </label>
                <label>Пауза после ошибки, сек <input class="field" name="pause_after_error_sec" type="text" value="${esc(s.pause_after_error_sec || "10")}"></label>
                <div class="row-buttons">
                    <button type="button" class="btn btn-primary" id="btnSaveStrategySettings">Сохранить стратегию</button>
                </div>
            </form>
        </section>

        <section class="block">
            <div class="block-head"><h2>Пресеты стратегий торговли</h2></div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Название</th>
                            <th>Активна</th>
                            <th>Создана</th>
                            <th>Выбрать</th>
                            <th>Сохранить</th>
                        </tr>
                    </thead>
                    <tbody>${strategyRows}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head">
                <h2>Профили настроек</h2>
                <div class="row-buttons">
                    <input class="field" id="newProfileName" type="text" placeholder="Имя нового профиля">
                    <button class="btn" id="btnCreateProfile">Создать профиль</button>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Название</th>
                            <th>Активен</th>
                            <th>Создан</th>
                            <th>Выбрать</th>
                        </tr>
                    </thead>
                    <tbody>${profileRows}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head">
                <h2>Инструменты</h2>
                <div class="row-buttons">
                    <button class="btn btn-primary" id="btnOpenAddInstrument">Добавить инструмент</button>
                </div>
            </div>
            ${instrumentCards}
        </section>
    `;

    document.getElementById("btnSaveSystemSettings")?.addEventListener("click", saveSystemSettings);
    document.getElementById("btnSaveStrategySettings")?.addEventListener("click", saveStrategySettings);
    document.getElementById("btnCreateProfile")?.addEventListener("click", createProfile);
    document.getElementById("btnOpenAddInstrument")?.addEventListener("click", openAddInstrumentModal);

    host.querySelectorAll("[data-activate-profile]").forEach((btn) => {
        btn.addEventListener("click", () => activateProfile(btn.dataset.activateProfile));
    });

    host.querySelectorAll("[data-activate-strategy]").forEach((btn) => {
        btn.addEventListener("click", () => activateStrategy(btn.dataset.activateStrategy));
    });

    host.querySelectorAll("[data-save-strategy]").forEach((btn) => {
        btn.addEventListener("click", () => saveStrategy(btn.dataset.saveStrategy));
    });

    host.querySelectorAll(".instrument-form").forEach((form) => {
        form.addEventListener("submit", (e) => submitInstrumentUpdate(e, form.dataset.figi));
    });

    host.querySelectorAll("[data-delete-instrument]").forEach((btn) => {
        btn.addEventListener("click", () => deleteInstrument(btn.dataset.deleteInstrument));
    });
}

async function renderHistoryTab() {
    const host = document.getElementById("view-history");
    if (!host) return;

    const data = await apiGet("/api/dashboard/history");

    const tradesRows = (data.trades || []).map((x) => `
        <tr>
            <td>${esc(x.time)}</td>
            <td>${esc(x.ticker)}</td>
            <td>${esc(x.direction)}</td>
            <td>${esc(x.entry_ui ?? x.entry ?? "0.00")}</td>
            <td>${esc(x.exit_ui ?? x.exit ?? "0.00")}</td>
            <td>${esc(x.qty)}</td>
            <td>${esc(x.commission_ui ?? x.commission ?? "0.00")}</td>
            <td>${esc(x.pnl_ui ?? x.pnl ?? "0.00")}</td>
            <td>${esc(x.reason)}</td>
        </tr>
    `).join("");

    const mapLogs = (rows) => (rows || []).map((x) => `
        <tr>
            <td>${esc(x.event_time)}</td>
            <td>${esc(x.event_type)}</td>
            <td>${esc(x.ticker)}</td>
            <td>${esc(x.level)}</td>
            <td>${esc(x.message)}</td>
        </tr>
    `).join("");

    host.innerHTML = `
        <section class="block">
            <div class="block-head">
                <h2>Фильтры на странице</h2>
                <div class="note">Работают во фронте без открытия JSON</div>
            </div>
            <div class="form-grid">
                <label>Фильтр торговой истории <input id="filterTrades" class="field" type="text" placeholder="Тикер, причина, направление..."></label>
                <label>Фильтр системной истории <input id="filterSystem" class="field" type="text" placeholder="Тип события, текст..."></label>
                <label>Фильтр ошибок <input id="filterErrors" class="field" type="text" placeholder="Тикер, ошибка, текст..."></label>
                <label>Фильтр общего журнала <input id="filterCommon" class="field" type="text" placeholder="Любой текст..."></label>
            </div>
        </section>

        <section class="block">
            <div class="block-head"><h2>Торговая история</h2></div>
            <div class="table-wrap">
                <table data-filter-input="filterTrades">
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
                    <tbody>${tradesRows}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head"><h2>Системная история</h2></div>
            <div class="table-wrap">
                <table data-filter-input="filterSystem">
                    <thead>
                        <tr>
                            <th>Время</th>
                            <th>Событие</th>
                            <th>Тикер</th>
                            <th>Уровень</th>
                            <th>Сообщение</th>
                        </tr>
                    </thead>
                    <tbody>${mapLogs(data.system_logs)}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head"><h2>Ошибки</h2></div>
            <div class="table-wrap">
                <table data-filter-input="filterErrors">
                    <thead>
                        <tr>
                            <th>Время</th>
                            <th>Событие</th>
                            <th>Тикер</th>
                            <th>Уровень</th>
                            <th>Сообщение</th>
                        </tr>
                    </thead>
                    <tbody>${mapLogs(data.error_logs)}</tbody>
                </table>
            </div>
        </section>

        <section class="block">
            <div class="block-head"><h2>Общий журнал</h2></div>
            <div class="table-wrap">
                <table data-filter-input="filterCommon">
                    <thead>
                        <tr>
                            <th>Время</th>
                            <th>Событие</th>
                            <th>Тикер</th>
                            <th>Уровень</th>
                            <th>Сообщение</th>
                        </tr>
                    </thead>
                    <tbody>${mapLogs(data.common_logs)}</tbody>
                </table>
            </div>
        </section>
    `;

    attachTableFilters();
}

function attachTableFilters() {
    document.querySelectorAll("table[data-filter-input]").forEach((table) => {
        const inputId = table.dataset.filterInput;
        const input = document.getElementById(inputId);
        if (!input || input.dataset.bound === "1") return;

        input.dataset.bound = "1";
        input.addEventListener("input", () => {
            const q = input.value.trim().toLowerCase();
            table.querySelectorAll("tbody tr").forEach((row) => {
                row.style.display = (!q || row.innerText.toLowerCase().includes(q)) ? "" : "none";
            });
        });
    });
}

async function serviceAction(action) {
    try {
        const r = await fetch(`/api/control/${action}`, { method: "POST", credentials: "same-origin" });
        const data = await r.json();
        showToast(data.ok ? "Команда выполнена" : "Команда вернула ошибку", data.ok ? "success" : "error");
        await renderSummaryCards();
        await applyRoute();
    } catch (e) {
        showToast(`Ошибка: ${e.message}`, "error");
    }
}

function openAddInstrumentModal() {
    const modal = document.getElementById("modalAddInstrument");
    if (modal) modal.style.display = "flex";
    loadTopVolumeInstruments();
}

function closeAddInstrumentModal() {
    const modal = document.getElementById("modalAddInstrument");
    if (modal) modal.style.display = "none";
}

function renderInstrumentSearchRows(items) {
    instrumentSearchData = items || [];
    const body = document.getElementById("instrumentSearchBody");
    if (!body) return;

    body.innerHTML = instrumentSearchData.map((item, idx) => `
        <tr>
            <td><input type="checkbox" data-idx="${idx}"></td>
            <td>${esc(item.ticker)}</td>
            <td>${esc(item.name)}</td>
            <td>${esc(item.figi)}</td>
            <td>${esc(item.instrument_type)}</td>
            <td>${esc(item.currency)}</td>
            <td>${esc(item.lot)}</td>
            <td>${esc(item.min_price_increment)}</td>
            <td>${esc(item.last_price_ui ?? item.last_price ?? "0.00")}</td>
            <td>${esc(item.price_time || "")}</td>
            <td>${esc(item.volume_score || 0)}</td>
        </tr>
    `).join("");
}

async function searchInstruments() {
    try {
        const q = document.getElementById("instrumentSearchInput")?.value?.trim() || "";
        const data = await apiGet(`/api/instruments/search?q=${encodeURIComponent(q)}`);
        renderInstrumentSearchRows(data);
    } catch (e) {
        showToast(`Ошибка поиска: ${e.message}`, "error");
    }
}

async function loadTopVolumeInstruments() {
    try {
        const data = await apiGet("/api/instruments/search?mode=top-volume");
        renderInstrumentSearchRows(data);
    } catch (e) {
        showToast(`Ошибка загрузки top-20: ${e.message}`, "error");
    }
}

async function acceptSelectedInstruments() {
    try {
        const rows = Array.from(document.querySelectorAll('#instrumentSearchBody input[type="checkbox"]'));
        const items = instrumentSearchData.map((x, idx) => ({
            ...x,
            использовать: rows.find(r => Number(r.dataset.idx) === idx)?.checked || false
        }));

        const r = await fetch("/api/instruments/add", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
            credentials: "same-origin",
        });

        const data = await r.json();
        showToast(`Добавлено: ${data["добавлено"]}`, "success");
        closeAddInstrumentModal();

        if (getTabFromHash() === "настройки") await renderSettingsTab();
        await renderMainShell();
        await renderMainData();
    } catch (e) {
        showToast(`Ошибка сохранения: ${e.message}`, "error");
    }
}

async function closeOnePosition(figi, qty, direction) {
    const ok = confirm("Закрыть эту позицию?");
    if (!ok) return;

    try {
        await apiPostForm("/api/позиции/закрыть", { figi, qty, direction });
        showToast("Позиция отправлена на закрытие", "success");
        await renderPortfolioTab();
        await renderMainData();
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка закрытия: ${e.message}`, "error");
    }
}

async function closeAllPositionsConfirm() {
    const ok = confirm("Точно закрыть ВСЕ открытые позиции? Это рыночное действие.");
    if (!ok) return;

    try {
        await apiPostForm("/api/позиции/закрыть-все", {});
        showToast("Команда закрытия всех позиций отправлена", "success");
        await renderPortfolioTab();
        await renderMainData();
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка закрытия всех позиций: ${e.message}`, "error");
    }
}

async function cancelStopOrder(stop_order_id) {
    const ok = confirm("Отменить стоп-заявку?");
    if (!ok) return;

    try {
        await apiPostForm("/api/стоп-заявки/отменить", { stop_order_id });
        showToast("Стоп-заявка отменена", "success");
        await renderPortfolioTab();
    } catch (e) {
        showToast(`Ошибка отмены: ${e.message}`, "error");
    }
}

async function createStopOrders() {
    try {
        const select = document.getElementById("positionForStops");
        if (!select || !select.value) {
            showToast("Нет доступной позиции для стоп-заявок", "error");
            return;
        }

        const parts = select.value.split("|");
        const figi = parts[0] || "";
        const qty = parts[1] || "0";
        const side = parts[2] || "";
        const base_price = document.getElementById("stop_base_price")?.value || "0";
        const stop_loss_pct = document.getElementById("stop_loss_pct")?.value || "0";
        const take_profit_pct = document.getElementById("take_profit_pct")?.value || "0";

        await apiPostForm("/api/стоп-заявки/создать", {
            figi, qty, side, base_price, stop_loss_pct, take_profit_pct
        });

        showToast("Стоп-заявки созданы", "success");
        await renderPortfolioTab();
    } catch (e) {
        showToast(`Ошибка стоп-заявок: ${e.message}`, "error");
    }
}

async function saveSystemSettings() {
    try {
        const form = document.getElementById("systemSettingsForm");
        const fd = new FormData(form);
        await apiPostForm("/api/settings/system", Object.fromEntries(fd.entries()));
        showToast("Системные настройки сохранены", "success");
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка сохранения: ${e.message}`, "error");
    }
}

async function saveStrategySettings() {
    try {
        const form = document.getElementById("strategySettingsForm");
        const fd = new FormData(form);
        await apiPostForm("/api/settings/strategy", Object.fromEntries(fd.entries()));
        showToast("Стратегия сохранена", "success");
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка сохранения стратегии: ${e.message}`, "error");
    }
}

async function createProfile() {
    const name = document.getElementById("newProfileName")?.value?.trim() || "";
    if (!name) {
        showToast("Укажи имя профиля", "error");
        return;
    }

    try {
        await apiPostForm("/api/профили/создать", { profile_name: name });
        showToast("Профиль создан", "success");
        await renderSettingsTab();
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка создания профиля: ${e.message}`, "error");
    }
}

async function activateProfile(name) {
    try {
        await apiPostForm("/api/профили/активировать", { profile_name: name });
        showToast(`Профиль "${name}" активирован`, "success");
        await renderSettingsTab();
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка активации профиля: ${e.message}`, "error");
    }
}

async function activateStrategy(name) {
    try {
        await apiPostForm("/api/стратегии/активировать", { strategy_name: name });
        showToast(`Стратегия "${name}" активирована`, "success");
        await renderSettingsTab();
        await renderSummaryCards();
    } catch (e) {
        showToast(`Ошибка активации стратегии: ${e.message}`, "error");
    }
}

async function saveStrategy(name) {
    try {
        await apiPostForm("/api/стратегии/сохранить", { strategy_name: name });
        showToast(`Стратегия "${name}" сохранена`, "success");
        await renderSettingsTab();
    } catch (e) {
        showToast(`Ошибка сохранения стратегии: ${e.message}`, "error");
    }
}

async function submitInstrumentUpdate(event, figi) {
    event.preventDefault();

    try {
        const form = event.target;
        const fd = new FormData(form);
        fd.append("figi", figi);

        await apiPostForm("/api/instruments/update", Object.fromEntries(fd.entries()));
        showToast("Инструмент сохранён", "success");
        await renderSettingsTab();
        await renderMainData();
    } catch (e) {
        showToast(`Ошибка сохранения инструмента: ${e.message}`, "error");
    }
}

async function deleteInstrument(figi) {
    const ok = confirm("Удалить инструмент?");
    if (!ok) return;

    try {
        await apiPostForm("/api/instruments/delete", { figi });
        showToast("Инструмент удалён", "success");
        await renderSettingsTab();
        await renderMainData();
    } catch (e) {
        showToast(`Ошибка удаления инструмента: ${e.message}`, "error");
    }
}

function startRefreshLoops() {
    if (refreshTimersStarted) return;
    refreshTimersStarted = true;

    setInterval(async () => {
        try {
            await renderSummaryCards();
        } catch (_) {}
    }, REFRESH_SUMMARY_MS);

    setInterval(async () => {
        try {
            await refreshQuotesOnly();
        } catch (_) {}
    }, REFRESH_QUOTES_MS);

    setInterval(async () => {
        try {
            if (getTabFromHash() === "портфель") {
                await renderPortfolioTab();
            }
        } catch (_) {}
    }, REFRESH_PORTFOLIO_MS);
}

async function bootstrapDashboard() {
    bindRouter();
    await renderSummaryCards();
    await applyRoute();
    startRefreshLoops();

    window.dashboardNavigate = async (tab) => {
        const normalized = normalizeTab(tab);
        const newHash = `#/${normalized}`;
        if (window.location.hash === newHash) {
            await applyRoute();
        } else {
            window.location.hash = newHash;
        }
    };

    window.openAddInstrumentModal = openAddInstrumentModal;
    window.closeAddInstrumentModal = closeAddInstrumentModal;
    window.searchInstruments = searchInstruments;
    window.loadTopVolumeInstruments = loadTopVolumeInstruments;
    window.acceptSelectedInstruments = acceptSelectedInstruments;
}

document.addEventListener("DOMContentLoaded", bootstrapDashboard);