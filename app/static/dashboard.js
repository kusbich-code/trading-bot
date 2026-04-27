const REFRESH_SUMMARY_MS = 7000;
const REFRESH_QUOTES_MS = 5000;
const REFRESH_PORTFOLIO_MS = 10000;

let instrumentSearchData = [];
let refreshTimersStarted = false;

// Viewed profile in settings tab (may differ from active profile)
let viewedProfileId = null;

const ALLOWED_TABS = new Set(["главное", "портфель", "настройки", "история", "график"]);

function esc(v) {
  return String(v ?? "");
}

function yesnoValue(v) {
  return String(v) === "1" ? "Да" : "Нет";
}

function normalizeTab(raw) {
  const tab = String(raw || "").trim().toLowerCase();
  return ALLOWED_TABS.has(tab) ? tab : "главное";
}

function getTabFromHash() {
  const hash = window.location.hash || "";
  if (!hash.startsWith("#/")) return "главное";
  try {
    const raw = hash.slice(2).split("?")[0].split("/")[0];
    return normalizeTab(decodeURIComponent(raw));
  } catch (e) {
    return "главное";
  }
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
    el.style.display = "none";
  });
  const active = document.querySelector(`[data-view="${normalized}"]`);
  if (active) {
    active.classList.remove("hidden");
    active.style.display = "block";
  }
  setActiveTabButton(normalized);
  const badge = document.getElementById("routeDebugBadge");
  if (badge) badge.textContent = "Вкладка: " + normalized;
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
  for (const [k, v] of Object.entries(data)) body.append(k, v);
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    credentials: "same-origin",
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return await r.json();
}

async function apiPostJson(url, data) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  let payload = null;
  const text = await res.text();
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!res.ok) {
    const message = (payload && payload.detail) || (payload && payload.error) ||
      (typeof payload === "string" && payload) || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return payload;
}

function diffTbody(tbody, rowsHtml) {
  if (!tbody) return;
  const next = rowsHtml.trim();
  if (tbody.dataset.renderedHtml === next) return;
  tbody.innerHTML = next;
  tbody.dataset.renderedHtml = next;
}

function ensureViewsExist() {
  const required = ["главное", "портфель", "настройки", "история", "график"];
  const root = document.querySelector(".app") || document.body;
  required.forEach((tab) => {
    if (!document.querySelector(`[data-view="${tab}"]`)) {
      const node = document.createElement("section");
      node.setAttribute("data-view", tab);
      node.id = "view-" + tab;
      node.className = "block hidden";
      node.innerHTML = `<h2>${tab}</h2>`;
      root.appendChild(node);
    }
  });
}

function summaryValueClass(value) {
  const v = String(value || "").trim().toLowerCase();
  if (["запущен", "ведётся", "ведется", "ok", "активен", "активна"].includes(v)) return "value status-ok";
  if (["остановлен", "остановлена", "выкл", "disabled", "inactive"].includes(v)) return "value status-off";
  if (["проблема", "error", "failed", "warning", "warn"].includes(v)) return "value status-problem";
  return "value";
}

function summaryCard(label, value) {
  return `
    <div class="card">
      <div class="label">${esc(label)}</div>
      <div class="${summaryValueClass(value)}">${esc(value ?? "—")}</div>
    </div>
  `;
}

async function renderSummaryCards() {
  const s = await apiGet("/api/dashboard/summary");
  const host = document.getElementById("summaryCards");
  if (!host) return;
  host.innerHTML = `
    ${summaryCard("Статус", s.status)}
    ${summaryCard("Торговля", s.trading_status)}
    ${summaryCard("Сделки", s.trades_today)}
    ${summaryCard("Реализованный ПнЛ", s.daily_pnl_ui)}
    ${summaryCard("Нереализованный ПнЛ", s.unrealized_pnl_ui || "0.00")}
    ${summaryCard("Комиссия", s.total_commission_ui)}
    ${summaryCard("Деньги", s.cash_rub_ui)}
    ${summaryCard("Позиции", s.positions_value_rub_ui)}
    ${summaryCard("Резерв", s.blocked_rub_ui)}
    ${summaryCard("Итого", s.total_assets_rub_ui)}
    ${summaryCard("Профиль", s.active_profile_name || "—")}
    ${summaryCard("Стратегия", s.active_strategy_name || "—")}
    ${summaryCard("Ошибка", s.last_error || "—")}
  `;
}

function helpCard(title, bullets) {
  return `
    <section class="help-card">
      <h2>Справка: ${title}</h2>
      <ul>${bullets.map((x) => `<li>${x}</li>`).join("")}</ul>
    </section>
  `;
}

function toggleSummaryCardsVisibility() {
  const host = document.getElementById("summaryCards");
  if (!host) return;
  host.style.display = getTabFromHash() === "главное" ? "grid" : "none";
}

// ── Main tab ──────────────────────────────────────────────────────────────────

async function renderMainShell() {
  const host = document.getElementById("view-main");
  if (!host || host.dataset.initialized === "1") return;
  host.innerHTML = `
    ${helpCard("Главное", [
      "<b>Карточки сверху:</b> Статус сервиса (Запущен/Остановлен), Торговля (Ведётся/Остановлена/Проблема), Сделки сегодня, Реализованный ПнЛ (чистый денежный поток из операций T-Bank за сегодня), Нереализованный ПнЛ (переоценка открытых позиций), Комиссия, баланс счёта и итого — всё берётся из T-Bank API.",
      "<b>Runtime:</b> текущий статус бота, режим API (Sandbox / Боевой), активный профиль и стратегия, последняя ошибка.",
      "<b>Почему бот не торгует:</b> диагностика — показывает причины паузы (выключен, лимит сделок/убытка, нет инструментов, нет позиций для открытия).",
      "<b>Управление:</b> Запустить / Остановить / Перезапустить — отправляет команду systemd-сервису (Linux) или процессу main.py.",
      "<b>Инструменты:</b> список из активной стратегии. Цены обновляются в реальном времени через MarketDataStream (или поллинг каждые 5 с как fallback). SL% и TP% — индивидуальные настройки инструмента.",
      "<b>Позиции:</b> открытые позиции из локальной БД (записывает сам бот при открытии сделки).",
      "<b>Сделки:</b> последние операции из T-Bank API (GetOperations за сегодня) — реальные исполненные BUY/SELL. Fallback на локальную БД если API недоступен. Суммы в колонке ПнЛ — фактический денежный поток по операции.",
    ])}
    <section class="block" id="sandboxBlock" style="display:none">
      <div class="row between">
        <div class="row">
          <h2>Sandbox</h2>
          <span class="badge" style="background:rgba(76,141,255,.2);color:#4c8dff;border:1px solid #4c8dff">Тестовый режим</span>
        </div>
        <div class="row">
          <select id="sandboxAmount" class="field" style="width:140px">
            <option value="50000">50 000 ₽</option>
            <option value="100000" selected>100 000 ₽</option>
            <option value="500000">500 000 ₽</option>
            <option value="1000000">1 000 000 ₽</option>
          </select>
          <button class="btn btn-primary" id="btnSandboxPayIn">Пополнить счёт</button>
        </div>
      </div>
    </section>
    <div id="balanceWarningsBox"></div>
    <section class="block">
      <div class="row between">
        <h2>Runtime</h2>
        <button class="btn" id="btnTelegramDiag" title="Проверить настройки Telegram и отправить тестовое сообщение">Тест Telegram</button>
      </div>
      <div id="runtimeBox">Загрузка...</div>
      <div id="telegramDiagBox"></div>
    </section>
    <section class="block">
      <div class="row between"><h2>Почему бот сейчас не торгует</h2></div>
      <div id="botExplainBox">Загрузка...</div>
    </section>
    <section class="block">
      <div class="row between"><h2>Управление</h2><div class="note">Быстрые действия с сервисом</div></div>
      <div class="row-buttons">
        <button class="btn btn-primary" id="btnStartService">Запустить</button>
        <button class="btn" id="btnStopService">Остановить</button>
        <button class="btn" id="btnRestartService">Перезапустить</button>
      </div>
    </section>
    <section class="block">
      <div class="row between"><h2>Инструменты</h2><div class="note">Активная стратегия</div></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>Название</th><th>Вкл</th><th>Лоты</th><th>SL%</th><th>TP%</th><th>Цена</th><th>Время</th></tr></thead>
          <tbody id="mainInstrumentsBody"></tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <div class="row between"><h2>Позиции</h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>Напр.</th><th>Кол-во</th><th>Вход</th><th>Тек.</th><th>ПнЛ</th><th>Открыта</th></tr></thead>
          <tbody id="mainPositionsBody"></tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <div class="row between"><h2>Сделки</h2><div class="note">Последние</div></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Время</th><th>Тикер</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Кол-во</th><th>ПнЛ</th><th>Причина</th></tr></thead>
          <tbody id="mainTradesBody"></tbody>
        </table>
      </div>
    </section>
  `;
  host.dataset.initialized = "1";
  document.getElementById("btnStartService")?.addEventListener("click", () => serviceAction("start"));
  document.getElementById("btnStopService")?.addEventListener("click", () => serviceAction("stop"));
  document.getElementById("btnRestartService")?.addEventListener("click", () => serviceAction("restart"));
  document.getElementById("btnSandboxPayIn")?.addEventListener("click", sandboxPayIn);
  document.getElementById("btnTelegramDiag")?.addEventListener("click", telegramDiag);
}

async function renderMainData() {
  const data = await apiGet("/api/dashboard/main");

  diffTbody(document.getElementById("mainInstrumentsBody"),
    (data.instruments || []).map((i) => `
      <tr>
        <td>${esc(i.ticker)}</td><td>${esc(i.name)}</td>
        <td>${yesnoValue(i.enabled)}</td>
        <td>${esc(i.lots_override || 1)}</td>
        <td>${esc(i.stop_loss_pct_ui)}</td>
        <td>${esc(i.take_profit_pct_ui)}</td>
        <td class="live-price" data-figi="${esc(i.figi)}">${esc(i.last_price_ui)}</td>
        <td class="live-time" data-figi="${esc(i.figi)}">${esc(i.price_time)}</td>
      </tr>
    `).join("")
  );

  diffTbody(document.getElementById("mainPositionsBody"),
    (data.positions || []).map((p) => `
      <tr>
        <td>${esc(p.ticker)}</td><td>${esc(p.direction)}</td><td>${esc(p.qty)}</td>
        <td>${esc(p.entry_price_ui)}</td><td>${esc(p.current_price_ui)}</td>
        <td>${esc(p.unrealized_pnl_ui)}</td><td>${esc(p.opened_at)}</td>
      </tr>
    `).join("")
  );

  const displayTrades = (data.api_trades && data.api_trades.length > 0)
    ? data.api_trades
    : (data.trades || []);
  diffTbody(document.getElementById("mainTradesBody"),
    displayTrades.map((t) => `
      <tr>
        <td>${esc(t.time)}</td><td>${esc(t.ticker)}</td><td>${esc(t.direction)}</td>
        <td>${esc(t.entry_ui)}</td><td>${esc(t.exit_ui)}</td>
        <td>${esc(t.qty)}</td><td>${esc(t.pnl_ui)}</td><td>${esc(t.reason)}</td>
      </tr>
    `).join("")
  );

  // Balance check + sandbox visibility
  try {
    const bc = await apiGet("/api/dashboard/balance-check");

    // Show/hide sandbox block
    const sandboxBlock = document.getElementById("sandboxBlock");
    if (sandboxBlock) sandboxBlock.style.display = bc.is_sandbox ? "block" : "none";

    // Balance warnings
    const warnBox = document.getElementById("balanceWarningsBox");
    if (warnBox) {
      const blocked = (bc.checks || []).filter(c => !c.can_trade && c.has_price);
      if (blocked.length > 0) {
        warnBox.innerHTML = blocked.map(c => `
          <div class="banner-warning" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <div>
              <strong>${esc(c.ticker)}</strong> — недостаточно средств для открытия позиции.
              Нужно: <strong>${esc(c.required_ui)} ₽</strong>
              (${esc(c.lots)} лот × ${esc(c.lot_size)} шт × ${esc(c.price_ui)} ₽ + комиссия).
              Свободно: <strong>${esc(bc.cash_ui)} ₽</strong>.
              SL ${esc(c.sl_pct)}% / TP ${esc(c.tp_pct)}%.
            </div>
            ${bc.is_sandbox ? `<button class="btn btn-primary" onclick="sandboxPayIn()">Пополнить Sandbox</button>` : ""}
          </div>
        `).join("");
      } else {
        warnBox.innerHTML = "";
      }
    }
  } catch (e) { /* balance check non-critical */ }

  try {
    const runtime = await apiGet("/api/dashboard/runtime");
    const box = document.getElementById("runtimeBox");
    if (box) {
      box.innerHTML = `
        <div><strong>Статус:</strong> ${esc(runtime.status || "INIT")}</div>
        <div><strong>API:</strong> ${String(runtime.tinvestusesandbox || "true") === "true" ? "Sandbox" : "Боевой"}</div>
        <div><strong>Профиль:</strong> ${esc(runtime.activeprofilename || "—")}</div>
        <div><strong>Стратегия:</strong> ${esc(runtime.activestrategyname || "—")}</div>
        <div><strong>Последняя ошибка:</strong> ${esc(runtime.lasterror || "—")}</div>
      `;
    }
  } catch (e) {
    const box = document.getElementById("runtimeBox");
    if (box) box.innerHTML = `<span class="health-error">Ошибка runtime: ${esc(e.message)}</span>`;
  }

  try {
    const explain = await apiGet("/api/dashboard/bot-explain");
    const box = document.getElementById("botExplainBox");
    if (box) box.innerHTML = `<ul>${(explain.reasons || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
  } catch (e) {
    const box = document.getElementById("botExplainBox");
    if (box) box.innerHTML = `<span class="health-error">Ошибка: ${esc(e.message)}</span>`;
  }
}

async function refreshQuotesOnly() {
  if (getTabFromHash() !== "главное") return;
  const quotes = await apiGet("/api/dashboard/quotes");
  const map = {};
  for (const q of quotes) map[q.figi] = q;
  document.querySelectorAll(".live-price[data-figi]").forEach((el) => {
    if (map[el.dataset.figi]) el.textContent = map[el.dataset.figi].last_price_ui;
  });
  document.querySelectorAll(".live-time[data-figi]").forEach((el) => {
    if (map[el.dataset.figi]) el.textContent = map[el.dataset.figi].price_time;
  });
}

// ── Settings tab ──────────────────────────────────────────────────────────────

async function renderSettingsTab() {
  const host = document.getElementById("view-settings");
  if (!host) return;

  const url = viewedProfileId
    ? `/api/dashboard/settings?profile_id=${viewedProfileId}`
    : "/api/dashboard/settings";
  const data = await apiGet(url);

  const prof = data.view_profile || {};
  const profSettings = prof.settings || {};
  const strat = data.view_strategy || {};
  const stratSettings = strat.settings || {};

  const isActiveProfile = String(prof.id) === String(data.active_profile_id);
  const viewingBanner = (!isActiveProfile && prof.id)
    ? `<div class="banner-warning">Просмотр профиля <strong>${esc(prof.name)}</strong> (активен: <strong>${esc(data.active_profile_name || "—")}</strong>). Изменения сохраняются в просматриваемый профиль.</div>`
    : "";

  const sandboxSelected = String(profSettings.tinvestusesandbox || "true") === "true";

  host.innerHTML = `
    ${helpCard("Настройки", [
      "<b>Профиль</b> — системный слой. Содержит: режим торговли (Sandbox/Боевой), включение бота, Telegram-режим (все уведомления или только ошибки), автоперечитка настроек. Профилей может быть несколько, активен один. «Изменить профиль» — открывает список всех профилей с действиями Открыть / Активировать / Удалить. При активации профиля его настройки и настройки его стратегии копируются в bot_settings — бот подхватывает их на следующем цикле.",
      "<b>Стратегия</b> — торговый слой. Содержит параметры риска (максимум сделок/день, лимит убытка, максимум позиций, интервал проверки), глобальные разрешения (лонг/шорт, только сессия, пауза после ошибки). Стратегии глобальные — одна стратегия может быть привязана к нескольким профилям. «Изменить стратегию» — список всех стратегий, действия Выбрать для профиля / Удалить / Создать.",
      "<b>Режим торговли</b> задаёт алгоритм сигнала: <i>Тренд</i> — вход у уровней поддержки/сопротивления (±0.15% от min/max последних 20 свечей); <i>MA Кроссовер</i> — BUY когда MA20 пробивает MA100 снизу, SELL — сверху (требует 120 свечей для расчёта); <i>Возврат к средней / Пробой</i> — режимы из strategy_engine (используются только для отображения на вкладке График).",
      "<b>Трейлинг-стоп</b> — когда включён, стоп-уровень движется вслед за ценой в сторону прибыли: для Лонг — max(текущий_стоп, цена × (1−SL%)), для Шорт — min(текущий_стоп, цена × (1+SL%)). При выключенном — фиксированный стоп от цены входа.",
      "<b>Фильтр аналитиков T-Bank</b> — при включении бот не открывает позицию если направление сигнала противоположно рекомендации аналитиков T-Bank (SignalService). Работает только если сервис доступен (недоступен в Sandbox).",
      "<b>Инструменты стратегии</b> — список бумаг с индивидуальными настройками: лоты, SL%/TP%/спред (в %, конвертируются в доли при сохранении), объём, разрешение лонг/шорт, приоритет, вкл/выкл. Инструменты хранятся только в стратегии — при смене стратегии список меняется целиком.",
      "<b>Добавить инструмент</b> — поиск по тикеру/названию с оценкой ликвидности (score), цены берутся из T-Bank API. После добавления instrument_uid сохраняется для использования в MarketDataStream.",
    ])}

    ${viewingBanner}

    <!-- PROFILE SECTION -->
    <section class="block">
      <div class="row between">
        <div class="row">
          <h2>Профиль:</h2>
          <span class="profile-name-label">${esc(prof.name || "—")}</span>
          ${isActiveProfile ? '<span class="badge badge-active">активен</span>' : ''}
        </div>
        <button class="btn" id="btnOpenProfiles">Изменить профиль</button>
      </div>

      <form id="profileSettingsForm" class="form-grid" style="margin-top:12px;">
        <input type="hidden" name="profile_id_val" value="${esc(prof.id || "")}">
        <label>Торговля
          <select class="field" name="bot_enabled">
            <option value="1" ${profSettings.bot_enabled === "1" ? "selected" : ""}>Вкл</option>
            <option value="0" ${profSettings.bot_enabled === "0" ? "selected" : ""}>Выкл</option>
          </select>
        </label>
        <label>ТГ только ошибки
          <select class="field" name="telegram_errors_only">
            <option value="1" ${profSettings.telegram_errors_only === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${profSettings.telegram_errors_only === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Автоперечитка
          <select class="field" name="auto_reload_settings">
            <option value="1" ${profSettings.auto_reload_settings === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${profSettings.auto_reload_settings === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Режим API
          <select class="field" name="runtime_mode">
            <option value="sandbox" ${sandboxSelected ? "selected" : ""}>Sandbox</option>
            <option value="prod" ${!sandboxSelected ? "selected" : ""}>Боевой</option>
          </select>
        </label>
        <div class="row-buttons">
          <button type="button" class="btn btn-primary" id="btnSaveProfileSettings">Сохранить</button>
        </div>
      </form>
    </section>

    <!-- STRATEGY SECTION -->
    <section class="block">
      <div class="row between">
        <div class="row">
          <h2>Стратегия:</h2>
          <span class="profile-name-label">${esc(strat.name || "—")}</span>
        </div>
        <button class="btn" id="btnOpenStrategies">Изменить стратегию</button>
      </div>

      ${strat.id ? `
      <form id="strategySettingsForm" class="form-grid" style="margin-top:12px;">
        <input type="hidden" name="strategy_id_val" value="${esc(strat.id)}">
        <label>Сделок/день<input class="field" name="max_trades_per_day" value="${esc(stratSettings.max_trades_per_day || 15)}"></label>
        <label>Лимит убытка<input class="field" name="max_daily_loss_rub" value="${esc(stratSettings.max_daily_loss_rub_ui || 0)}"></label>
        <label>Позиций макс<input class="field" name="max_open_positions" value="${esc(stratSettings.max_open_positions || 2)}"></label>
        <label>Интервал, сек<input class="field" name="check_interval_sec" value="${esc(stratSettings.check_interval_sec || 5)}"></label>
        <label>SL % (умолч.)<input class="field" name="default_stop_loss_pct" value="${esc(stratSettings.default_stop_loss_pct_ui || 0.25)}"></label>
        <label>TP % (умолч.)<input class="field" name="default_take_profit_pct" value="${esc(stratSettings.default_take_profit_pct_ui || 0.50)}"></label>
        <label>Комиссия %<input class="field" name="estimated_commission_pct" value="${esc(stratSettings.estimated_commission_pct_ui || 0.04)}"></label>
        <label>Лонг разрешён
          <select class="field" name="allow_long_global">
            <option value="1" ${stratSettings.allow_long_global === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${stratSettings.allow_long_global === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Шорт разрешён
          <select class="field" name="allow_short_global">
            <option value="1" ${stratSettings.allow_short_global === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${stratSettings.allow_short_global === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Только сессия
          <select class="field" name="trade_only_session">
            <option value="1" ${stratSettings.trade_only_session === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${stratSettings.trade_only_session === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Пауза после ошибки, сек<input class="field" name="pause_after_error_sec" value="${esc(stratSettings.pause_after_error_sec || 10)}"></label>
        <label>Режим торговли
          <select class="field" name="tradingmode">
            <option value="trend" ${stratSettings.tradingmode === "trend" ? "selected" : ""}>Тренд (поддержка/сопротивление)</option>
            <option value="ma_crossover" ${stratSettings.tradingmode === "ma_crossover" ? "selected" : ""}>MA Кроссовер (MA20/MA100)</option>
            <option value="mean_reversion" ${stratSettings.tradingmode === "mean_reversion" ? "selected" : ""}>Возврат к средней</option>
            <option value="breakout" ${stratSettings.tradingmode === "breakout" ? "selected" : ""}>Пробой</option>
          </select>
        </label>
        <label>Пауза после ошибок подряд<input class="field" name="errorseriespausecount" value="${esc(stratSettings.errorseriespausecount || 3)}"></label>
        <label>Пауза после стопов подряд<input class="field" name="stopseriespausecount" value="${esc(stratSettings.stopseriespausecount || 3)}"></label>
        <label>Трейлинг-стоп
          <select class="field" name="trailing_stop_enabled">
            <option value="1" ${stratSettings.trailing_stop_enabled === "1" ? "selected" : ""}>Вкл — стоп двигается за ценой</option>
            <option value="0" ${stratSettings.trailing_stop_enabled !== "1" ? "selected" : ""}>Выкл — фиксированный стоп</option>
          </select>
        </label>
        <label>Фильтр аналитиков T-Bank
          <select class="field" name="use_signal_service">
            <option value="1" ${stratSettings.use_signal_service === "1" ? "selected" : ""}>Вкл — не входить против сигнала</option>
            <option value="0" ${stratSettings.use_signal_service !== "1" ? "selected" : ""}>Выкл</option>
          </select>
        </label>
        <div class="row-buttons">
          <button type="button" class="btn btn-primary" id="btnSaveStrategySettings">Сохранить настройки стратегии</button>
        </div>
      </form>

      <!-- INSTRUMENTS -->
      <div style="margin-top:20px;">
        <div class="row between">
          <h2>Инструменты стратегии</h2>
          <button class="btn" id="btnOpenAddInstrument">Добавить инструмент</button>
        </div>
        ${(data.instruments || []).length === 0
          ? '<p class="note">Инструменты не добавлены.</p>'
          : (data.instruments || []).map((i) => `
            <form class="form-grid instrument-form block" data-strategy-id="${esc(strat.id)}" data-figi="${esc(i.figi)}">
              <label>Тикер<input class="field" value="${esc(i.ticker)}" disabled></label>
              <label>Название<input class="field" value="${esc(i.name)}" disabled></label>
              <label>Лоты<input class="field" name="lots_override" value="${esc(i.lots_override || 1)}"></label>
              <label>SL %<input class="field" name="stop_loss_pct" value="${esc(i.stop_loss_pct_ui)}"></label>
              <label>TP %<input class="field" name="take_profit_pct" value="${esc(i.take_profit_pct_ui)}"></label>
              <label>Спред %<input class="field" name="max_spread_pct" value="${esc(i.max_spread_pct_ui)}"></label>
              <label>Мин. объём<input class="field" name="min_volume" value="${esc(i.min_volume || 0)}"></label>
              <label>Лонг
                <select class="field" name="allow_long">
                  <option value="1" ${String(i.allow_long) === "1" ? "selected" : ""}>Да</option>
                  <option value="0" ${String(i.allow_long) === "0" ? "selected" : ""}>Нет</option>
                </select>
              </label>
              <label>Шорт
                <select class="field" name="allow_short">
                  <option value="1" ${String(i.allow_short) === "1" ? "selected" : ""}>Да</option>
                  <option value="0" ${String(i.allow_short) === "0" ? "selected" : ""}>Нет</option>
                </select>
              </label>
              <label>Приоритет<input class="field" name="priority" value="${esc(i.priority || 100)}"></label>
              <label>Вкл
                <select class="field" name="enabled">
                  <option value="1" ${String(i.enabled) === "1" ? "selected" : ""}>Да</option>
                  <option value="0" ${String(i.enabled) === "0" ? "selected" : ""}>Нет</option>
                </select>
              </label>
              <label>Цена<input class="field" value="${esc(i.last_price_ui)}" disabled></label>
              <div class="row-buttons">
                <button type="submit" class="btn btn-primary">Сохранить</button>
                <button type="button" class="btn btn-danger" data-delete-instrument="${esc(i.figi)}" data-strategy-id="${esc(strat.id)}">Удалить</button>
              </div>
            </form>
          `).join("")
        }
      </div>
      ` : '<p class="note" style="margin-top:12px;">Стратегия не выбрана. Нажмите «Изменить стратегию», чтобы выбрать или создать стратегию.</p>'}
    </section>
  `;

  // Bind profile settings save
  document.getElementById("btnSaveProfileSettings")?.addEventListener("click", () => saveProfileSettings(prof.id));

  // Bind strategy settings save
  document.getElementById("btnSaveStrategySettings")?.addEventListener("click", () => saveStrategySettings(strat.id));

  // Bind instrument forms
  host.querySelectorAll(".instrument-form").forEach((form) => {
    const stratId = form.dataset.strategyId;
    const figi = form.dataset.figi;
    form.addEventListener("submit", (e) => submitInstrumentUpdate(e, stratId, figi));
  });
  host.querySelectorAll("[data-delete-instrument]").forEach((btn) => {
    btn.addEventListener("click", () => deleteStrategyInstrument(btn.dataset.strategyId, btn.dataset.deleteInstrument));
  });

  // Bind modal buttons
  document.getElementById("btnOpenProfiles")?.addEventListener("click", () => openProfilesModal(data));
  document.getElementById("btnOpenStrategies")?.addEventListener("click", () => openStrategiesModal(data));
  document.getElementById("btnOpenAddInstrument")?.addEventListener("click", () => openAddInstrumentModal(strat.id));
}

// ── Profile modal ─────────────────────────────────────────────────────────────

function openProfilesModal(data) {
  const profiles = data.profiles || [];
  const activeId = data.active_profile_id;
  const viewedId = viewedProfileId || activeId;

  const body = document.getElementById("profilesModalBody");
  if (body) {
    body.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Название</th><th>Статус</th><th>Стратегия</th><th>Создан</th><th>Действия</th></tr>
          </thead>
          <tbody>
            ${profiles.map((p) => {
              const isActive = String(p.id) === String(activeId);
              const isViewed = String(p.id) === String(viewedId);
              return `
                <tr ${isViewed ? 'class="row-highlighted"' : ''}>
                  <td><strong>${esc(p.name)}</strong></td>
                  <td>${isActive ? '<span class="badge badge-active">Активен</span>' : ''}</td>
                  <td>${esc(p.strategy_name || "—")}</td>
                  <td>${esc((p.created_at || "").slice(0, 16))}</td>
                  <td class="row">
                    <button class="btn" data-view-profile="${esc(p.id)}" title="Открыть для просмотра и редактирования">Открыть</button>
                    ${!isActive ? `<button class="btn btn-primary" data-activate-profile="${esc(p.id)}">Активировать</button>` : ''}
                    ${!isActive ? `<button class="btn btn-danger" data-delete-profile="${esc(p.id)}" data-profile-name="${esc(p.name)}">Удалить</button>` : ''}
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;

    body.querySelectorAll("[data-view-profile]").forEach((btn) => {
      btn.addEventListener("click", () => {
        viewedProfileId = parseInt(btn.dataset.viewProfile);
        closeProfilesModal();
        renderSettingsTab();
      });
    });
    body.querySelectorAll("[data-activate-profile]").forEach((btn) => {
      btn.addEventListener("click", () => activateProfileAction(parseInt(btn.dataset.activateProfile)));
    });
    body.querySelectorAll("[data-delete-profile]").forEach((btn) => {
      btn.addEventListener("click", () => deleteProfileAction(parseInt(btn.dataset.deleteProfile), btn.dataset.profileName));
    });
  }

  document.getElementById("modalProfiles")?.classList.remove("hidden");
}

function closeProfilesModal() {
  document.getElementById("modalProfiles")?.classList.add("hidden");
}

async function createProfile() {
  const name = document.getElementById("newProfileName")?.value?.trim() || "";
  if (!name) { showToast("Укажи имя профиля", "error"); return; }
  try {
    await apiPostForm("/api/profiles/create", { name });
    showToast(`Профиль "${name}" создан`, "success");
    document.getElementById("newProfileName").value = "";
    const data = await apiGet("/api/dashboard/settings");
    openProfilesModal(data);
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function activateProfileAction(profileId) {
  try {
    await apiPostForm(`/api/profiles/${profileId}/activate`, {});
    showToast("Профиль активирован", "success");
    viewedProfileId = profileId;
    closeProfilesModal();
    await renderSettingsTab();
    await renderSummaryCards();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function deleteProfileAction(profileId, name) {
  if (!confirm(`Удалить профиль "${name}"? Действие необратимо.`)) return;
  try {
    await apiPostForm(`/api/profiles/${profileId}/delete`, {});
    showToast(`Профиль "${name}" удалён`, "success");
    if (viewedProfileId === profileId) viewedProfileId = null;
    const data = await apiGet("/api/dashboard/settings");
    openProfilesModal(data);
    await renderSettingsTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function saveProfileSettings(profileId) {
  if (!profileId) { showToast("Профиль не выбран", "error"); return; }
  try {
    const fd = new FormData(document.getElementById("profileSettingsForm"));
    const data = Object.fromEntries(fd.entries());
    delete data.profile_id_val;
    await apiPostForm(`/api/profiles/${profileId}/settings`, data);
    showToast("Настройки профиля сохранены", "success");
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

// ── Strategy modal ────────────────────────────────────────────────────────────

function openStrategiesModal(data) {
  const strategies = data.strategies || [];
  const prof = data.view_profile || {};
  const profId = prof.id;
  const currentStrategyId = prof.strategy_id;

  const body = document.getElementById("strategiesModalBody");
  if (body) {
    body.innerHTML = `
      <p class="note">Выберите стратегию для профиля <strong>${esc(prof.name || "—")}</strong>:</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Название</th><th>Создана</th><th>Действия</th></tr>
          </thead>
          <tbody>
            ${strategies.map((s) => {
              const isCurrent = String(s.id) === String(currentStrategyId);
              return `
                <tr ${isCurrent ? 'class="row-highlighted"' : ''}>
                  <td><strong>${esc(s.name)}</strong> ${isCurrent ? '<span class="badge badge-active">выбрана</span>' : ''}</td>
                  <td>${esc((s.created_at || "").slice(0, 16))}</td>
                  <td class="row">
                    ${!isCurrent ? `<button class="btn btn-primary" data-select-strategy="${esc(s.id)}" data-profile-id="${esc(profId)}">Выбрать</button>` : ''}
                    ${!isCurrent ? `<button class="btn btn-danger" data-delete-strategy="${esc(s.id)}" data-strategy-name="${esc(s.name)}">Удалить</button>` : ''}
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;

    body.querySelectorAll("[data-select-strategy]").forEach((btn) => {
      btn.addEventListener("click", () => selectStrategyForProfile(
        parseInt(btn.dataset.profileId),
        parseInt(btn.dataset.selectStrategy)
      ));
    });
    body.querySelectorAll("[data-delete-strategy]").forEach((btn) => {
      btn.addEventListener("click", () => deleteStrategyAction(
        parseInt(btn.dataset.deleteStrategy), btn.dataset.strategyName
      ));
    });
  }

  document.getElementById("modalStrategies")?.classList.remove("hidden");
}

function closeStrategiesModal() {
  document.getElementById("modalStrategies")?.classList.add("hidden");
}

async function createStrategy() {
  const name = document.getElementById("newStrategyName")?.value?.trim() || "";
  if (!name) { showToast("Укажи имя стратегии", "error"); return; }
  try {
    await apiPostForm("/api/strategies/create", { name });
    showToast(`Стратегия "${name}" создана`, "success");
    document.getElementById("newStrategyName").value = "";
    const data = await apiGet("/api/dashboard/settings");
    openStrategiesModal(data);
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function selectStrategyForProfile(profileId, strategyId) {
  try {
    await apiPostForm(`/api/profiles/${profileId}/set-strategy`, { strategy_id: strategyId });
    showToast("Стратегия выбрана", "success");
    closeStrategiesModal();
    await renderSettingsTab();
    await renderSummaryCards();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function deleteStrategyAction(strategyId, name) {
  if (!confirm(`Удалить стратегию "${name}"? Действие необратимо.`)) return;
  try {
    await apiPostForm(`/api/strategies/${strategyId}/delete`, {});
    showToast(`Стратегия "${name}" удалена`, "success");
    const data = await apiGet("/api/dashboard/settings");
    openStrategiesModal(data);
    await renderSettingsTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function saveStrategySettings(strategyId) {
  if (!strategyId) { showToast("Стратегия не выбрана", "error"); return; }
  try {
    const fd = new FormData(document.getElementById("strategySettingsForm"));
    const data = Object.fromEntries(fd.entries());
    delete data.strategy_id_val;
    await apiPostForm(`/api/strategies/${strategyId}/settings`, data);
    showToast("Настройки стратегии сохранены", "success");
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

// ── Strategy instruments ──────────────────────────────────────────────────────

async function submitInstrumentUpdate(event, strategyId, figi) {
  event.preventDefault();
  try {
    const fd = new FormData(event.target);
    fd.append("figi", figi);
    await apiPostForm(`/api/strategies/${strategyId}/instruments/update`, Object.fromEntries(fd.entries()));
    showToast("Инструмент сохранён", "success");
    await renderSettingsTab();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function deleteStrategyInstrument(strategyId, figi) {
  if (!confirm("Удалить инструмент из стратегии?")) return;
  try {
    await apiPostForm(`/api/strategies/${strategyId}/instruments/delete`, { figi });
    showToast("Инструмент удалён", "success");
    await renderSettingsTab();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

// ── Add instrument modal ──────────────────────────────────────────────────────

let addInstrumentTargetStrategyId = null;

function openAddInstrumentModal(strategyId) {
  addInstrumentTargetStrategyId = strategyId;
  document.getElementById("modalAddInstrument")?.classList.remove("hidden");
  const input = document.getElementById("instrumentSearchInput");
  if (input) input.value = "";
  loadTopVolumeInstruments();
}

function closeAddInstrumentModal() {
  document.getElementById("modalAddInstrument")?.classList.add("hidden");
  addInstrumentTargetStrategyId = null;
}

async function acceptSelectedInstruments() {
  try {
    const selectedIndexes = [...document.querySelectorAll("#instrumentSearchRows [data-instrument-pick]:checked")]
      .map((el) => Number(el.dataset.instrumentPick))
      .filter((idx) => Number.isInteger(idx) && instrumentSearchData[idx]);

    if (!selectedIndexes.length) { showToast("Отметь хотя бы один инструмент", "error"); return; }
    const items = selectedIndexes.map((idx) => instrumentSearchData[idx]);

    const stratId = addInstrumentTargetStrategyId;
    if (!stratId) { showToast("Стратегия не определена", "error"); return; }

    const result = await apiPostJson(`/api/strategies/${stratId}/instruments/add`, items);
    showToast(`Добавлено инструментов: ${result?.добавлено ?? items.length}`, "success");
    closeAddInstrumentModal();
    instrumentSearchData = [];
    await renderSettingsTab();
    await renderMainData();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка добавления: ${e.message}`, "error");
  }
}

function normalizeInstrumentForAdd(item) {
  return {
    ticker: item.ticker || "",
    figi: item.figi || "",
    name: item.name || "",
    classcode: item.class_code || item.classcode || "",
    instrumenttype: item.instrument_type || item.instrumenttype || "share",
    currency: item.currency || "",
    lot: Number(item.lot || 1),
    minpriceincrement: String(item.min_price_increment || item.minpriceincrement || "0.01"),
    last_price: item.last_price || "",
    price_time: item.price_time || "",
    score: item.score ?? "",
    lots_override: 1,
    stop_loss_pct: "0.0025",
    take_profit_pct: "0.005",
    max_spread_pct: "0",
    min_volume: 0,
    allow_long: 1,
    allow_short: 1,
    priority: 100,
    enabled: 1,
  };
}

function renderInstrumentSearchRows(items) {
  const host = document.getElementById("instrumentSearchRows");
  if (!host) return;
  instrumentSearchData = Array.isArray(items) ? items.map(normalizeInstrumentForAdd) : [];
  if (!instrumentSearchData.length) {
    host.innerHTML = `<tr><td colspan="11" class="note">Ничего не найдено</td></tr>`;
    return;
  }
  host.innerHTML = instrumentSearchData.map((item, idx) => `
    <tr>
      <td style="width:72px;text-align:center;"><input type="checkbox" class="checkbox" data-instrument-pick="${idx}"></td>
      <td><strong>${esc(item.ticker || "—")}</strong></td>
      <td style="min-width:260px;">${esc(item.name || "—")}</td>
      <td class="muted" style="font-size:12px;">${esc(item.figi || "—")}</td>
      <td><span class="pill">${esc(item.instrumenttype || "—")}</span></td>
      <td>${esc(item.currency || "—")}</td>
      <td>${esc(item.lot ?? "—")}</td>
      <td>${esc(item.minpriceincrement || "—")}</td>
      <td>${esc(item.last_price || "—")}</td>
      <td>${esc(item.price_time || "—")}</td>
      <td>${esc(item.score ?? "—")}</td>
    </tr>
  `).join("");
}

async function searchInstruments() {
  try {
    const q = document.getElementById("instrumentSearchInput")?.value?.trim() || "";
    if (!q) { showToast("Введи тикер или название", "error"); return; }
    const items = await apiGet(`/api/instruments/search?q=${encodeURIComponent(q)}&kind=shares`);
    renderInstrumentSearchRows(items || []);
  } catch (e) {
    showToast(`Ошибка поиска: ${e.message}`, "error");
  }
}

async function loadTopVolumeInstruments() {
  try {
    const items = await apiGet("/api/instruments/top?limit=20");
    renderInstrumentSearchRows(items || []);
  } catch (e) {
    showToast(`Ошибка загрузки топ-20: ${e.message}`, "error");
  }
}

function selectAllInstrumentSearchRows() {
  document.querySelectorAll("[data-instrument-pick]").forEach((el) => { el.checked = true; });
}

function clearAllInstrumentSearchRows() {
  document.querySelectorAll("[data-instrument-pick]").forEach((el) => { el.checked = false; });
}

// ── Portfolio tab ─────────────────────────────────────────────────────────────

async function renderPortfolioTab() {
  const host = document.getElementById("view-portfolio");
  if (!host) return;

  const data = await apiGet("/api/dashboard/portfolio");
  host.innerHTML = `
    ${helpCard("Портфель", [
      "<b>Деньги на счёте (GetPositions):</b> точный остаток по каждой валюте напрямую из T-Bank API — не расчётный, а фактический баланс счёта.",
      "<b>Портфель счёта:</b> реальные открытые позиции из T-Bank. Источник данных (по приоритету): 1) PortfolioStream — обновляется мгновенно при любом изменении портфеля; 2) REST get_portfolio — одиночный запрос если стрим не подключён; 3) локальная БД как крайний fallback. Время последнего обновления стрима показано в заголовке блока. Каждая позиция имеет признак <b>Лонг / Шорт</b> и кнопку <b>Закрыть</b> — отправляет рыночный ордер на закрытие через T-Bank API.",
      "<b>Активные stop orders:</b> стоп-заявки от T-Bank API (get_stop_orders). Кнопка «Отменить» удаляет заявку у брокера.",
      "<b>Позиции бота:</b> позиции которые бот открыл сам (source=BOT в локальной БД). <b>Закрыть</b> — рыночный ордер на конкретную позицию. <b>Закрыть все</b> — рыночные ордера на все BOT-позиции. <b>Очистить записи</b> — удаляет локальные записи из БД без обращения к брокеру (нужно если записи устарели и брокер возвращает «Not enough balance»).",
      "<b>Ошибка «30034 Not enough balance»:</b> локальная БД содержит записи о позициях, которых нет в реальном счёте. Нажмите «Очистить записи» для сброса локальных данных.",
    ])}
    <section class="block">
      <div class="row between">
        <h2>Деньги на счёте <span class="note">(GetPositions)</span></h2>
        ${data.stream_updated_at ? `<span class="note">Портфель стрим: ${esc(data.stream_updated_at.slice(0,19).replace("T"," "))}</span>` : '<span class="note muted">PortfolioStream не подключён</span>'}
      </div>
      ${(data.account_money || []).length === 0
        ? '<p class="note">Нет данных</p>'
        : `<div class="row" style="gap:16px;flex-wrap:wrap;margin-bottom:6px;">
            ${(data.account_money || []).map(m => `
              <div class="card" style="min-width:140px;padding:12px;">
                <div class="label">${esc(m.currency)}</div>
                <div class="value">${esc(m.value_ui)}</div>
              </div>`).join("")}
           </div>`}
    </section>
    <section class="block">
      <div class="row between"><h2>Портфель счёта <span class="note">(API брокера)</span></h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>FIGI</th><th>Тип</th><th>Направление</th><th>Кол-во</th><th>Средняя</th><th>Текущая</th><th>Доход</th><th>Действие</th></tr></thead>
          <tbody>
            ${(data.portfolio_positions || []).map((p) => {
              const dir = String(p.direction || "").toUpperCase();
              const dirBadge = dir === "BUY"
                ? '<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b;border:1px solid #2fa36b">Лонг</span>'
                : dir === "SELL"
                  ? '<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b;border:1px solid #bf4d5a">Шорт</span>'
                  : "—";
              return `
                <tr>
                  <td>${esc(p.ticker)}</td><td>${esc(p.figi)}</td><td>${esc(p.instrument_type)}</td>
                  <td>${dirBadge}</td>
                  <td>${esc(p.quantity_ui)}</td><td>${esc(p.average_position_price_ui)}</td>
                  <td>${esc(p.current_price_ui)}</td><td>${esc(p.expected_yield_ui)}</td>
                  <td>
                    ${p.figi && p.qty && p.direction ? `
                      <button class="btn btn-danger" data-close-portfolio
                        data-figi="${esc(p.figi)}" data-qty="${esc(p.qty)}" data-direction="${esc(p.direction)}">
                        Закрыть
                      </button>` : "—"}
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <div class="row between"><h2>Активные stop orders</h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>FIGI</th><th>Тип</th><th>Направление</th><th>Лоты</th><th>Цена</th><th>Stop</th><th>Создан</th><th>Действие</th></tr></thead>
          <tbody id="activeStopOrdersBody"></tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <div class="row between">
        <h2>Позиции бота</h2>
        <div class="row">
          <button class="btn btn-danger" id="btnCloseAllPositions">Закрыть все</button>
          <button class="btn" id="btnClearLocalPositions" title="Удалить мусорные локальные записи (брокера не трогает)">Очистить записи</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>FIGI</th><th>Напр.</th><th>Кол-во</th><th>Вход</th><th>Текущая</th><th>ПнЛ</th><th>Действие</th></tr></thead>
          <tbody>
            ${(data.bot_positions || []).map((p) => `
              <tr>
                <td>${esc(p.ticker)}</td><td>${esc(p.figi)}</td><td>${esc(p.direction)}</td>
                <td>${esc(p.qty)}</td><td>${esc(p.entry_price_ui)}</td>
                <td>${esc(p.current_price_ui)}</td><td>${esc(p.unrealized_pnl_ui)}</td>
                <td>
                  <button class="btn btn-danger" data-close-one
                    data-figi="${esc(p.figi)}" data-qty="${esc(p.qty)}" data-direction="${esc(p.direction)}">
                    Закрыть
                  </button>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;

  document.getElementById("btnCloseAllPositions")?.addEventListener("click", closeAllPositionsConfirm);
  document.getElementById("btnClearLocalPositions")?.addEventListener("click", clearLocalPositions);
  host.querySelectorAll("[data-close-one]").forEach((btn) => {
    btn.addEventListener("click", () => closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction));
  });
  host.querySelectorAll("[data-close-portfolio]").forEach((btn) => {
    btn.addEventListener("click", () => closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction));
  });

  if (data.broker_error) {
    showToast(`Портфель: данные из локальной БД (ошибка API: ${data.broker_error.slice(0, 80)})`, "error", 6000);
  }

  try {
    const stopData = await apiGet("/api/dashboard/stop-orders");
    const stopBody = document.getElementById("activeStopOrdersBody");
    if (stopBody) {
      stopBody.innerHTML = (stopData.items || []).map(x => `
        <tr>
          <td>${esc(x.figi || "")}</td><td>${esc(x.stop_order_type || "")}</td>
          <td>${esc(x.direction || "")}</td><td>${esc(x.lots_requested || "")}</td>
          <td>${esc(x.price || "")}</td><td>${esc(x.stop_price || "")}</td>
          <td>${esc(x.created_at || "")}</td>
          <td><button class="btn btn-danger" data-cancel-stop="${esc(x.stop_order_id || "")}">Отменить</button></td>
        </tr>
      `).join("") || `<tr><td colspan="8">Нет активных stop orders</td></tr>`;
    }
  } catch (e) {
    const stopBody = document.getElementById("activeStopOrdersBody");
    if (stopBody) stopBody.innerHTML = `<tr><td colspan="8">Ошибка: ${esc(e.message)}</td></tr>`;
  }
}

async function closeOnePosition(figi, qty, direction) {
  if (!confirm(`Закрыть позицию ${figi} (${qty} лот)?`)) return;
  try {
    const res = await apiPostForm("/api/позиции/закрыть", { figi, qty, direction });
    showToast(`Заявка отправлена брокеру: ${res.order_id || "ok"}`, "success");
    await renderPortfolioTab();
    await renderMainData();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка закрытия: ${e.message}`, "error", 8000);
  }
}

async function clearLocalPositions() {
  if (!confirm("Удалить все локальные записи позиций бота?\nБрокера это не затрагивает — только локальная БД.")) return;
  try {
    await apiPostForm("/api/позиции/очистить-локальные", {});
    showToast("Локальные записи позиций очищены", "success");
    await renderPortfolioTab();
    await renderMainData();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function closeAllPositionsConfirm() {
  if (!confirm("Точно закрыть все позиции?")) return;
  try {
    await apiPostForm("/api/позиции/закрыть-все", {});
    showToast("Команда закрытия всех позиций отправлена", "success");
    await renderPortfolioTab();
    await renderMainData();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

// ── History tab ───────────────────────────────────────────────────────────────

async function renderHistoryTab() {
  const host = document.getElementById("view-history");
  if (!host) return;

  const data = await apiGet("/api/dashboard/history");
  const mapLogs = (rows) => (rows || []).map((x) => `
    <tr>
      <td>${esc(x.event_time)}</td><td>${esc(x.event_type)}</td>
      <td>${esc(x.ticker)}</td><td>${esc(x.level)}</td><td>${esc(x.message)}</td>
    </tr>
  `).join("");

  host.innerHTML = `
    ${helpCard("История", [
      "<b>Операции брокера (GetOperationsByCursor):</b> реальные исполненные операции из T-Bank API с курсорной пагинацией. Выбери период (7/30/90 дней) → «Загрузить» → «Загрузить ещё» для следующей страницы. Показываются только торговые операции (BUY/SELL), комиссии скрыты. Цена вычисляется из суммы и количества если T-Bank не вернул цену (маркет-ордер). Это самый точный источник — данные от брокера, не из локальной БД.",
      "<b>Сделки (локальная БД):</b> записи которые бот создаёт при закрытии позиции. Содержит: Вход и Выход (цены исполнения), Кол-во лотов, Комиссия (расчётная), ПнЛ, Причина закрытия (STOP_LOSS / TAKE_PROFIT / TRAILING_STOP). Если цена входа или выхода = «—» — ордер исполнился по нулевой цене (sandbox-артефакт), актуальные данные смотреть в «Операции брокера».",
      "<b>Система:</b> ключевые события бота — BOT_START, BOT_STOP, BOT_ERROR, DAILY_RESET, CONFIG_CHANGED. Используй для диагностики перезапусков и сбоев.",
      "<b>Ошибки:</b> только события уровня ERROR — ошибки API, неверные FIGI, проблемы с ордерами.",
      "<b>Журнал:</b> полный лог всех событий (SIGNAL, ORDER_OPEN, ORDER_CLOSE, SIGNAL_SKIP, ORDER_FILL и т.д.). SIGNAL_SKIP — сигнал был, но не исполнен (фильтр давления стакана или фильтр аналитиков). ORDER_FILL — ордер исполнен через OrdersStream (не через поллинг).",
      "<b>Фильтры:</b> живая фильтрация по тексту в каждом блоке — вводи тикер, тип события или ключевое слово.",
    ])}
    <section class="block">
      <div class="row between">
        <h2>Операции брокера <span class="note">(GetOperationsByCursor)</span></h2>
        <div class="row">
          <select id="brokerOpsDays" class="field" style="width:auto">
            <option value="7">7 дней</option>
            <option value="30" selected>30 дней</option>
            <option value="90">90 дней</option>
          </select>
          <button class="btn" id="btnLoadBrokerOps">Загрузить</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Дата</th><th>Тикер</th><th>Направление</th><th>Кол-во</th><th>Цена</th><th>Сумма</th><th>Комиссия</th><th>Тип</th></tr></thead>
          <tbody id="brokerOpsBody"><tr><td colspan="8" class="note">Нажмите «Загрузить»</td></tr></tbody>
        </table>
      </div>
      <div id="brokerOpsPager" class="row" style="margin-top:8px;"></div>
    </section>
    <section class="block">
      <h2>Фильтры</h2>
      <div class="form-grid">
        <label>Сделки<input id="filterTrades" class="field" placeholder="тикер, причина, направление"></label>
        <label>Система<input id="filterSystem" class="field" placeholder="тип события, текст"></label>
        <label>Ошибки<input id="filterErrors" class="field" placeholder="ошибка, текст"></label>
        <label>Журнал<input id="filterCommon" class="field" placeholder="любой текст"></label>
      </div>
    </section>
    <section class="block">
      <h2>Сделки</h2>
      <div class="table-wrap">
        <table data-filter-input="filterTrades">
          <thead><tr><th>Время</th><th>Тикер</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Кол-во</th><th>Комиссия</th><th>ПнЛ</th><th>Причина</th></tr></thead>
          <tbody>
            ${(data.trades || []).map((t) => `
              <tr>
                <td>${esc(t.time)}</td><td>${esc(t.ticker)}</td><td>${esc(t.direction)}</td>
                <td>${esc(t.entry_ui)}</td><td>${esc(t.exit_ui)}</td><td>${esc(t.qty)}</td>
                <td>${esc(t.commission_ui)}</td><td>${esc(t.pnl_ui)}</td><td>${esc(t.reason)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <h2>Система</h2>
      <div class="table-wrap">
        <table data-filter-input="filterSystem">
          <thead><tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr></thead>
          <tbody>${mapLogs(data.system_logs)}</tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <h2>Ошибки</h2>
      <div class="table-wrap">
        <table data-filter-input="filterErrors">
          <thead><tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr></thead>
          <tbody>${mapLogs(data.error_logs)}</tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <h2>Журнал</h2>
      <div class="table-wrap">
        <table data-filter-input="filterCommon">
          <thead><tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr></thead>
          <tbody>${mapLogs(data.common_logs)}</tbody>
        </table>
      </div>
    </section>
  `;
  attachTableFilters();

  // Broker operations loader
  let _brokerCursor = "";
  async function _loadBrokerOps(cursor = "") {
    const days = document.getElementById("brokerOpsDays")?.value || "30";
    const body = document.getElementById("brokerOpsBody");
    const pager = document.getElementById("brokerOpsPager");
    if (!body) return;
    body.innerHTML = `<tr><td colspan="8" class="note">Загрузка...</td></tr>`;
    try {
      const data = await apiGet(`/api/broker-operations?cursor=${encodeURIComponent(cursor)}&days=${days}&limit=50`);
      const rows = (data.items || []).filter(x => !x.is_fee);
      body.innerHTML = rows.length === 0
        ? `<tr><td colspan="8" class="note">Нет операций</td></tr>`
        : rows.map(op => `
            <tr>
              <td>${esc(op.date)}</td>
              <td>${esc(op.ticker || op.figi)}</td>
              <td>${op.direction === "BUY"
                ? '<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b">BUY</span>'
                : op.direction === "SELL"
                  ? '<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b">SELL</span>'
                  : esc(op.direction)}</td>
              <td>${esc(op.quantity)}</td>
              <td>${esc(op.price_ui)}</td>
              <td>${esc(op.payment_ui)}</td>
              <td>${esc(op.commission_ui)}</td>
              <td class="muted" style="font-size:11px">${esc(op.type)}</td>
            </tr>`).join("");
      _brokerCursor = data.next_cursor || "";
      if (pager) {
        pager.innerHTML = data.has_next
          ? `<button class="btn" id="btnBrokerOpsNext">Загрузить ещё</button>`
          : `<span class="note">Все операции загружены</span>`;
        document.getElementById("btnBrokerOpsNext")?.addEventListener("click", () => _loadBrokerOps(_brokerCursor));
      }
    } catch (e) {
      body.innerHTML = `<tr><td colspan="8" class="health-error">Ошибка: ${esc(e.message)}</td></tr>`;
    }
  }
  document.getElementById("btnLoadBrokerOps")?.addEventListener("click", () => _loadBrokerOps(""));
}

function attachTableFilters() {
  document.querySelectorAll("table[data-filter-input]").forEach((table) => {
    const input = document.getElementById(table.dataset.filterInput);
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

// ── Chart tab ─────────────────────────────────────────────────────────────────

async function renderChartTab() {
  const host = document.getElementById("view-chart");
  if (!host) return;

  const currentFigi = document.getElementById("chartFigiSelect")?.value || "";
  const currentInterval = document.getElementById("chartIntervalSelect")?.value || "1min";
  const data = await apiGet(`/api/dashboard/chart?figi=${encodeURIComponent(currentFigi)}&interval=${encodeURIComponent(currentInterval)}`);

  host.innerHTML = `
    <section class="help-card">
      <h2>Справка: График</h2>
      <ul>
        <li><b>Свечной график (OHLCV):</b> данные из T-Bank API за последние 8 часов. Выбери инструмент из списка (все инструменты из каталога market data) и интервал: 1 мин / 5 мин / 15 мин / 1 час. Зелёные свечи — рост, красные — падение. «Обновить» — перезагружает данные.</li>
        <li><b>Инструменты в списке:</b> все бумаги из таблицы instrument_market_state (наполняется ботом при работе и при добавлении инструментов в стратегию).</li>
        <li><b>Score сигнала:</b> вычисляется strategy_engine на основе технических индикаторов (MA, RSI, Bollinger, объём). Используется только для отображения — реальная торговля идёт через logic в main.py (поддержка/сопротивление или MA-кроссовер в зависимости от tradingmode). Action: BUY / SELL / HOLD. Score выше 0 — бычий, ниже 0 — медвежий.</li>
        <li><b>Причины сигнала:</b> список факторов за и против входа по каждому индикатору — удобно для ручного анализа перед добавлением инструмента в стратегию.</li>
      </ul>
    </section>
    <section class="block">
      <div class="row between">
        <h2>Свечной график</h2>
        <div class="row">
          <select class="field" id="chartFigiSelect"></select>
          <select class="field" id="chartIntervalSelect">
            <option value="1min">1 минута</option>
            <option value="5min">5 минут</option>
            <option value="15min">15 минут</option>
            <option value="hour">1 час</option>
          </select>
          <button class="btn" id="btnReloadChart">Обновить</button>
        </div>
      </div>
      <div id="chartBox" class="chart-box"></div>
      <div id="signalScoreBox" class="score-box"></div>
    </section>
  `;

  const figiSelect = document.getElementById("chartFigiSelect");
  figiSelect.innerHTML = (data.available_instruments || []).map(x =>
    `<option value="${x.figi}">${x.ticker} — ${x.name}</option>`
  ).join("");
  if (data.selected_figi) figiSelect.value = data.selected_figi;

  const intervalSelect = document.getElementById("chartIntervalSelect");
  if (data.interval) intervalSelect.value = data.interval;

  renderCandlesAndScore(data);

  document.getElementById("btnReloadChart")?.addEventListener("click", async () => {
    const figi = document.getElementById("chartFigiSelect")?.value || "";
    const interval = document.getElementById("chartIntervalSelect")?.value || "1min";
    const newData = await apiGet(`/api/dashboard/chart?figi=${encodeURIComponent(figi)}&interval=${encodeURIComponent(interval)}`);
    if (figiSelect && newData.available_instruments) {
      figiSelect.innerHTML = newData.available_instruments.map(x =>
        `<option value="${x.figi}">${x.ticker} — ${x.name}</option>`
      ).join("");
      figiSelect.value = newData.selected_figi || figi;
    }
    renderCandlesAndScore(newData);
  });
}

function renderCandlesAndScore(data) {
  const candles = data.candles || [];
  const signal = data.signal || { action: "HOLD", score: 0, reasons: ["Нет данных"] };
  const scoreBox = document.getElementById("signalScoreBox");
  if (scoreBox) {
    scoreBox.innerHTML = `
      <div class="score-pill">Action: ${signal.action}</div>
      <div class="score-pill">Score: ${signal.score}</div>
      <div class="score-reasons">${(signal.reasons || []).map(x => `<div>• ${x}</div>`).join("")}</div>
    `;
  }
  if (!window.Plotly) return;
  if (!candles.length) {
    Plotly.newPlot("chartBox", [], {
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: "#eef4ff" },
      annotations: [{ text: "Нет свечей", xref: "paper", yref: "paper", x: 0.5, y: 0.5, showarrow: false, font: { size: 16 } }]
    }, { displayModeBar: false, responsive: true });
    return;
  }
  Plotly.newPlot("chartBox", [{
    x: candles.map(c => c.time), open: candles.map(c => c.open),
    high: candles.map(c => c.high), low: candles.map(c => c.low),
    close: candles.map(c => c.close), type: "candlestick",
    increasing: { line: { color: "#2ecc71" } }, decreasing: { line: { color: "#ff5c5c" } }
  }], {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#eef4ff" }, margin: { t: 10, r: 20, b: 40, l: 40 },
    xaxis: { rangeslider: { visible: false } },
  }, { displayModeBar: false, responsive: true });
}

// ── Sandbox pay-in ────────────────────────────────────────────────────────────

async function telegramDiag() {
  const box = document.getElementById("telegramDiagBox");
  if (box) box.innerHTML = '<span class="note">Проверяю...</span>';
  try {
    const status = await apiGet("/api/telegram/status");
    const problems = status.problems || [];
    let html = "";
    if (problems.length > 0) {
      html = problems.map(p =>
        `<div class="banner-warning" style="margin-top:8px;padding:8px 12px">${esc(p)}</div>`
      ).join("");
    } else {
      // Send a real test message
      try {
        const res = await fetch("/api/health/telegram-test", { method: "POST", credentials: "same-origin" });
        const data = await res.json();
        const tg = data.telegram || {};
        if (tg.ok) {
          html = `<div style="margin-top:8px;padding:8px 12px;border-radius:8px;background:rgba(47,163,107,.15);border:1px solid #2fa36b;color:#2fa36b">
            Тестовое сообщение отправлено ✓ (chat_id: ${esc(status.chat_id)})
          </div>`;
        } else {
          html = `<div class="banner-warning" style="margin-top:8px">
            Telegram API вернул ошибку: ${esc(JSON.stringify(tg))}<br>
            Проверь: бот добавлен в чат? Токен верный? Chat ID верный?
          </div>`;
        }
      } catch (e) {
        html = `<div class="banner-warning" style="margin-top:8px">Ошибка теста: ${esc(e.message)}</div>`;
      }
    }
    html += `<div class="note" style="margin-top:6px">
      Токен: ${esc(status.token_preview)} · Chat ID: ${esc(status.chat_id)} ·
      Только ошибки: ${status.telegram_errors_only === "1" ? "<b>Да</b>" : "Нет"}
    </div>`;
    if (box) box.innerHTML = html;
  } catch (e) {
    if (box) box.innerHTML = `<div class="banner-warning" style="margin-top:8px">Ошибка: ${esc(e.message)}</div>`;
  }
}

async function sandboxPayIn() {
  const select = document.getElementById("sandboxAmount");
  const amount = select ? parseInt(select.value) : 100000;
  try {
    const res = await apiPostForm("/api/sandbox/pay-in", { amount });
    showToast(`Sandbox пополнен. Новый баланс: ${res.balance_ui} ₽`, "success", 4000);
    await renderSummaryCards();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка пополнения: ${e.message}`, "error", 6000);
  }
}

// ── Service control ───────────────────────────────────────────────────────────

async function serviceAction(action) {
  try {
    const r = await fetch(`/api/control/${action}`, { method: "POST", credentials: "same-origin" });
    const data = await r.json();
    showToast(data.ok ? "Команда выполнена" : "Ошибка команды", data.ok ? "success" : "error");
    await renderSummaryCards();
    await applyRoute();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

// ── Router ────────────────────────────────────────────────────────────────────

async function applyRoute() {
  const tab = getTabFromHash();
  try {
    ensureViewsExist();
    setVisibleView(tab);
    toggleSummaryCardsVisibility();
    document.title = tab;
    if (tab === "главное") {
      await renderMainShell();
      await renderMainData();
    } else if (tab === "портфель") {
      await renderPortfolioTab();
    } else if (tab === "настройки") {
      await renderSettingsTab();
    } else if (tab === "история") {
      await renderHistoryTab();
    } else if (tab === "график") {
      await renderChartTab();
    }
  } catch (e) {
    console.error("[tabs] route error", e);
    showToast(e.message, "error", 5000);
  }
}

function bindRouter() {
  document.addEventListener("click", async (e) => {
    const link = e.target.closest("[data-tab-link]");
    if (!link) return;
    e.preventDefault();
    e.stopPropagation();
    const tab = normalizeTab(link.dataset.tabLink);
    const newHash = "#/" + encodeURIComponent(tab);
    if (window.location.hash === newHash) { await applyRoute(); return; }
    window.location.hash = newHash;
  });
  window.addEventListener("hashchange", () => applyRoute());
  if (!window.location.hash || !window.location.hash.startsWith("#/")) {
    window.location.hash = "#/" + encodeURIComponent("главное");
  } else {
    applyRoute();
  }
}

function startRefreshLoops() {
  if (refreshTimersStarted) return;
  refreshTimersStarted = true;
  setInterval(async () => {
    try { if (getTabFromHash() === "главное") await renderSummaryCards(); } catch {}
  }, REFRESH_SUMMARY_MS);
  setInterval(async () => {
    try { await refreshQuotesOnly(); } catch {}
  }, REFRESH_QUOTES_MS);
  setInterval(async () => {
    try { if (getTabFromHash() === "портфель") await renderPortfolioTab(); } catch {}
  }, REFRESH_PORTFOLIO_MS);
}

async function bootstrapDashboard() {
  bindRouter();
  await renderSummaryCards();
  await applyRoute();
  startRefreshLoops();

  window.searchInstruments = searchInstruments;
  window.loadTopVolumeInstruments = loadTopVolumeInstruments;
  window.acceptSelectedInstruments = acceptSelectedInstruments;
  window.openAddInstrumentModal = openAddInstrumentModal;
  window.closeAddInstrumentModal = closeAddInstrumentModal;
  window.selectAllInstrumentSearchRows = selectAllInstrumentSearchRows;
  window.clearAllInstrumentSearchRows = clearAllInstrumentSearchRows;
  window.closeProfilesModal = closeProfilesModal;
  window.closeStrategiesModal = closeStrategiesModal;
  window.createProfile = createProfile;
  window.createStrategy = createStrategy;
}

document.addEventListener("DOMContentLoaded", bootstrapDashboard);
