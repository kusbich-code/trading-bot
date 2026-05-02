const REFRESH_SUMMARY_MS = 7000;
const REFRESH_QUOTES_MS = 5000;
const REFRESH_PORTFOLIO_MS = 10000;

let instrumentSearchData = [];
let refreshTimersStarted = false;

// Viewed profile in settings tab (may differ from active profile)
let viewedProfileId = null;

const ALLOWED_TABS = new Set(["главное", "портфель", "настройки", "история", "график", "бэктест", "аналитик"]);

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
  const required = ["главное", "портфель", "настройки", "история", "график", "бэктест", "аналитик"];
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
        <button class="btn btn-primary" onclick="serviceAction('start')">Запустить</button>
        <button class="btn" onclick="serviceAction('stop')">Остановить</button>
        <button class="btn" onclick="serviceAction('restart')">Перезапустить</button>
      </div>
    </section>
    <section class="block" id="parallelStatusBlock" style="display:none">
      <div class="row between">
        <h2>Параллельные стратегии</h2>
        <span class="note">Потоки работают независимо, одна позиция за раз</span>
      </div>
      <div id="parallelStatusBody"></div>
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
      <div class="row between"><h2>Позиции <span class="note">(API брокера)</span></h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>Направление</th><th>Лотов</th><th>Вход</th><th>Тек.</th><th>ПнЛ</th><th>Действие</th></tr></thead>
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
    (data.positions || []).map((p) => {
      const dir = String(p.direction || "").toUpperCase();
      const dirBadge = dir === "BUY"
        ? '<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b;border:1px solid #2fa36b">Лонг</span>'
        : dir === "SELL"
          ? '<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b;border:1px solid #bf4d5a">Шорт</span>'
          : esc(dir);
      const pnlClass = parseFloat(p.unrealized_pnl_ui) >= 0 ? "color:#2fa36b" : "color:#ff7b7b";
      return `<tr>
        <td><b>${esc(p.ticker)}</b></td>
        <td>${dirBadge}</td>
        <td>${esc(p.qty)}</td>
        <td>${esc(p.entry_price_ui)}</td>
        <td>${esc(p.current_price_ui)}</td>
        <td style="${pnlClass}">${esc(p.unrealized_pnl_ui)}</td>
        <td>${p.figi && p.qty && p.direction ? `
          <button class="btn btn-danger" style="padding:5px 10px"
            onclick="closeOnePosition('${esc(p.figi)}','${esc(p.qty)}','${esc(p.direction)}')">
            Закрыть
          </button>` : "—"}</td>
      </tr>`;
    }).join("")
  );

  // close buttons use inline onclick — no addEventListener needed

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

  refreshParallelStatus();

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
      "<b>Профиль</b> — системный слой: режим API (Sandbox/Боевой), включение бота, Telegram-режим. Профилей несколько, активен один. При активации настройки копируются в bot_settings — бот подхватывает на следующем цикле.",
      "<b>Стратегия</b> — торговый слой: лимиты риска, режим торговли, инструменты. Стратегия глобальная, можно привязать к нескольким профилям.",
      "<b>Режимы торговли</b> (одинаковы в боте и бэктесте): <i>Возврат к средней</i> — лучший для голубых фишек РФ, входит когда цена отклонилась от 20-периодной средней на ≥1.8 σ (Z-score); <i>Пробой</i> — вход при пробое максимума/минимума 20 свечей с объёмом выше среднего; <i>Тренд</i> — SMA9 пересекает SMA21 с положительным/отрицательным моментумом.",
      "<b>Мин. quality сигнала (score)</b> — фильтр «мусорных» сделок. Бот входит только если качество сигнала ≥ порога. <i>Возврат к средней</i>: рекомендуется 50–55 (при z=1.8 score≈54). <i>Пробой</i>: 30–40. <i>Тренд</i>: оставьте 0 — score всегда низкий из-за специфики расчёта.",
      "<b>Трейлинг-стоп</b> — стоп движется вслед за ценой в сторону прибыли. Лонг: max(стоп, цена × (1−SL%)). Шорт: min(стоп, цена × (1+SL%)). Подходит для стратегии Пробой.",
      "<b>Фильтр аналитиков T-Bank</b> — бот не открывает позицию против сигнала аналитиков SignalService. Недоступен в Sandbox.",
      "<b>Предустановленные стратегии</b> для РФ-рынка: <i>Голубые фишки — Возврат к средней</i> (SBER/GAZP/LKOH, MeanRev, score≥50); <i>Нефтяной сектор</i> (LKOH/ROSN/TATN/NVTK, MeanRev); <i>Пробой с объёмом</i> (SBER/GMKN/MOEX, Breakout, score≥40); <i>Финансы — Скальпинг</i> (SBER/VTBR/MTSS, MeanRev, score≥55).",
      "<b>Инструменты стратегии</b> — SL%/TP%/спред на уровне инструмента переопределяют настройки стратегии. instrument_uid сохраняется для MarketDataStream.",
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

      <!-- PARALLEL TOGGLE inside profile section -->
      <div style="display:flex;align-items:center;gap:14px;padding:12px 0 4px;border-top:1px solid rgba(255,255,255,.08);margin-top:4px;flex-wrap:wrap">
        <div>
          <b>Параллельная торговля</b>
          <div class="note" style="margin-top:2px;max-width:480px">Запускает несколько стратегий одновременно. Один счёт — одна позиция за раз: когда одна стратегия открывает позицию, остальные ждут.</div>
        </div>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="parallelTradingToggle"
            ${profSettings.parallel_trading_enabled === "1" ? "checked" : ""}
            onchange="onParallelToggle(${esc(prof.id)}, this.checked)"
            style="width:18px;height:18px;cursor:pointer">
          <span>${profSettings.parallel_trading_enabled === "1" ? "Включена" : "Выключена"}</span>
        </label>
      </div>
    </section>

    ${profSettings.parallel_trading_enabled === "1" ? `
    <!-- ── PARALLEL STRATEGIES TABLE ── -->
    <section class="block" id="parallelStrategiesSection">
      <div class="row between" style="margin-bottom:12px">
        <h2>Параллельные стратегии</h2>
        <div class="row" style="gap:10px">
          <select id="parallelStratSelect" class="field" style="min-width:220px">
            <option value="">Выберите стратегию...</option>
            ${(data.strategies || [])
              .filter(s => !(data.parallel_strategies || []).some(ps => ps.strategy_id === s.id))
              .map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`)
              .join("")}
          </select>
          <button class="btn btn-primary" onclick="addParallelStrategy(${esc(prof.id)})">+ Добавить</button>
        </div>
      </div>

      ${(data.parallel_strategies || []).length === 0 ? `
        <p class="note">Стратегии не добавлены. Выберите стратегию из списка и нажмите «Добавить».</p>
        <p class="note" style="margin-top:4px">После изменений перезапустите бота чтобы новые потоки стартовали.</p>
      ` : `
      <div class="table-wrap" style="margin-bottom:0">
        <table class="table">
          <thead>
            <tr>
              <th>Стратегия</th><th>Режим</th><th>SL / TP</th>
              <th>Инструментов</th><th>Действия</th>
            </tr>
          </thead>
          <tbody>
            ${(data.parallel_strategies || []).map(ps => {
              const modeLabels = {trend:"Тренд", mean_reversion:"MeanRev", breakout:"Breakout"};
              const sl = ps.sl_pct ? (parseFloat(ps.sl_pct)*100).toFixed(3)+"%" : "—";
              const tp = ps.tp_pct ? (parseFloat(ps.tp_pct)*100).toFixed(3)+"%" : "—";
              return `<tr>
                <td><b>${esc(ps.name)}</b></td>
                <td style="color:#a8c8ff;font-size:12px">${esc(modeLabels[ps.tradingmode] || ps.tradingmode)}</td>
                <td class="mono" style="font-size:12px">${esc(sl)} / ${esc(tp)}</td>
                <td>${ps.instrument_count}</td>
                <td style="white-space:nowrap">
                  <button class="btn btn-small" onclick="expandParallelStrategy(${ps.strategy_id})">Инструменты</button>
                  <button class="btn btn-small" style="margin-left:6px;color:#ff7b7b;border-color:#bf4d5a"
                    onclick="removeParallelStrategy(${esc(prof.id)}, ${ps.strategy_id})">Убрать</button>
                </td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
      <p class="note" style="margin-top:8px">После изменений перезапустите бота чтобы потоки применили изменения.</p>
      `}

      <!-- Expanded instruments section (shown below table when user clicks "Инструменты") -->
      <div id="parallelInstrExpanded" style="display:none;margin-top:20px;border-top:1px solid rgba(255,255,255,.1);padding-top:16px">
        <div class="row between" style="margin-bottom:10px">
          <h2 id="parallelInstrTitle">Инструменты стратегии</h2>
          <div class="row" style="gap:8px">
            <button class="btn" id="parallelBtnAddInstr">Добавить инструмент</button>
            <button class="btn" onclick="document.getElementById('parallelInstrExpanded').style.display='none'">✕ Закрыть</button>
          </div>
        </div>
        <div id="parallelInstrBody"></div>
      </div>
    </section>
    ` : `
    <!-- ── SINGLE STRATEGY SECTION ── -->
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
            <option value="trend" ${stratSettings.tradingmode === "trend" ? "selected" : ""}>Тренд (SMA9/SMA21 + моментум)</option>
            <option value="mean_reversion" ${stratSettings.tradingmode === "mean_reversion" ? "selected" : ""}>Возврат к средней (Z-score ±1.8)</option>
            <option value="breakout" ${stratSettings.tradingmode === "breakout" ? "selected" : ""}>Пробой (20 баров + объём)</option>
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
        <label>Мин. качество сигнала (score)
          <input class="field" type="number" name="min_signal_score" min="0" max="100" step="1"
            value="${esc(stratSettings.min_signal_score || 0)}">
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
        ${_renderInstrumentForms(data.instruments || [], strat.id)}
      </div>
      ` : '<p class="note" style="margin-top:12px;">Стратегия не выбрана. Нажмите «Изменить стратегию».</p>'}
    </section>
    `}
  `;

  // Profile settings save
  document.getElementById("btnSaveProfileSettings")?.addEventListener("click", () => saveProfileSettings(prof.id));

  // Strategy settings (non-parallel mode only)
  document.getElementById("btnSaveStrategySettings")?.addEventListener("click", () => saveStrategySettings(strat.id));

  // Instrument forms (non-parallel mode)
  _bindInstrumentForms(host, strat.id);

  // Modal buttons
  document.getElementById("btnOpenProfiles")?.addEventListener("click", () => openProfilesModal(data));
  document.getElementById("btnOpenStrategies")?.addEventListener("click", () => openStrategiesModal(data));
  document.getElementById("btnOpenAddInstrument")?.addEventListener("click", () => openAddInstrumentModal(strat.id));

  // Parallel: "Add instrument" inside expanded section
  document.getElementById("parallelBtnAddInstr")?.addEventListener("click", () => {
    const sid = document.getElementById("parallelInstrExpanded")?.dataset.strategyId;
    if (sid) openAddInstrumentModal(sid);
  });
}

// ── Settings tab helpers ──────────────────────────────────────────────────────

function _renderInstrumentForms(instruments, stratId) {
  if (!instruments.length) return '<p class="note">Инструменты не добавлены.</p>';
  return instruments.map(i => `
    <form class="form-grid instrument-form block" data-strategy-id="${esc(stratId)}" data-figi="${esc(i.figi)}">
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
        <button type="button" class="btn btn-danger"
          data-delete-instrument="${esc(i.figi)}" data-strategy-id="${esc(stratId)}">Удалить</button>
      </div>
    </form>
  `).join("");
}

function _bindInstrumentForms(host, stratId) {
  host.querySelectorAll(".instrument-form").forEach((form) => {
    const sid  = form.dataset.strategyId;
    const figi = form.dataset.figi;
    form.addEventListener("submit", (e) => submitInstrumentUpdate(e, sid, figi));
  });
  host.querySelectorAll("[data-delete-instrument]").forEach((btn) => {
    btn.addEventListener("click", () =>
      deleteStrategyInstrument(btn.dataset.strategyId, btn.dataset.deleteInstrument)
    );
  });
}

// ── Parallel trading management ───────────────────────────────────────────────

async function onParallelToggle(profileId, enabled) {
  try {
    await apiPostJson(`/api/profile/${profileId}/parallel-toggle`, { enabled });
    await renderSettingsTab();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function addParallelStrategy(profileId) {
  const sel = document.getElementById("parallelStratSelect");
  const strategyId = sel ? parseInt(sel.value) : 0;
  if (!strategyId) { showToast("Выберите стратегию", "error"); return; }
  try {
    await apiPostJson(`/api/profile/${profileId}/parallel-strategies`, { strategy_id: strategyId });
    showToast("Стратегия добавлена", "success");
    await renderSettingsTab();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function removeParallelStrategy(profileId, strategyId) {
  if (!confirm("Убрать стратегию из параллельного списка?")) return;
  try {
    await fetch(`/api/profile/${profileId}/parallel-strategies/${strategyId}`,
      { method: "DELETE", credentials: "same-origin" });
    showToast("Стратегия убрана", "success");
    await renderSettingsTab();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

async function expandParallelStrategy(strategyId) {
  const panel = document.getElementById("parallelInstrExpanded");
  const title = document.getElementById("parallelInstrTitle");
  const body  = document.getElementById("parallelInstrBody");
  if (!panel || !body) return;

  // Toggle: close if same strategy already expanded
  if (panel.dataset.strategyId === String(strategyId) && panel.style.display !== "none") {
    panel.style.display = "none";
    return;
  }

  panel.dataset.strategyId = strategyId;
  body.innerHTML = '<span class="note">Загрузка...</span>';
  panel.style.display = "block";

  try {
    const data = await apiGet(`/api/dashboard/settings?profile_id=${viewedProfileId || ""}`);
    // Find instruments for this strategy in parallel_strategies list
    const ps = (data.parallel_strategies || []).find(p => p.strategy_id === strategyId);
    if (title) title.textContent = `Инструменты — ${ps ? ps.name : "#" + strategyId}`;

    // Load full instrument list with market data
    const instrResp = await apiGet(`/api/strategy/${strategyId}/instruments`);
    const instruments = instrResp.instruments || [];

    body.innerHTML = instruments.length
      ? _renderInstrumentForms(instruments, strategyId)
      : '<p class="note">Инструменты не добавлены. Нажмите «Добавить инструмент».</p>';

    // Bind forms
    _bindInstrumentForms(panel, strategyId);

    // Update "Add instrument" button
    const btn = document.getElementById("parallelBtnAddInstr");
    if (btn) {
      btn.onclick = () => openAddInstrumentModal(strategyId);
    }
  } catch (e) {
    body.innerHTML = `<span class="note">Ошибка: ${esc(e.message)}</span>`;
  }
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

// ── Parallel strategy status ──────────────────────────────────────────────────

async function refreshParallelStatus() {
  try {
    const data = await apiGet("/api/parallel/status");
    const threads = data.threads || [];
    const block = document.getElementById("parallelStatusBlock");
    const body  = document.getElementById("parallelStatusBody");
    if (!block || !body) return;

    if (!threads.length) { block.style.display = "none"; return; }
    block.style.display = "block";

    const coord = data.coord || {};
    const statusColors = {
      "ожидание сигнала": "#9fb3d8",
      "сканирование": "#4c8dff",
      "в позиции": "#2ecc71",
      "ожидание — другая стратегия в позиции": "#f0c04a",
      "остановлен": "#666",
      "бот выключен": "#666",
    };

    body.innerHTML = threads.map(t => {
      const color = statusColors[t.status] || "#eef4ff";
      const ticker = t.ticker ? ` · ${esc(t.ticker)}` : "";
      const updated = t.updated_at ? `<span class="note" style="font-size:11px">${esc(t.updated_at)}</span>` : "";
      return `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.07)">
        <div style="width:10px;height:10px;border-radius:50%;background:${color};flex-shrink:0"></div>
        <div style="flex:1">
          <b>${esc(t.name)}</b>
          <span class="note" style="margin-left:8px">${esc(t.status)}${ticker}</span>
        </div>
        ${updated}
      </div>`;
    }).join("");

    if (coord.owner_strategy_id != null) {
      body.innerHTML += `<div class="note" style="margin-top:8px">
        Позиция занята: стратегия id=${esc(coord.owner_strategy_id)}, инструмент ${esc(coord.owner_ticker || coord.owner_figi || "?")}
      </div>`;
    }
  } catch {}
}

async function toggleParallel(strategyId, enabled) {
  const label = document.getElementById("parallelToggleLabel");
  try {
    await apiPostJson(`/api/strategy/${strategyId}/parallel`, { enabled });
    if (label) label.textContent = enabled ? "Включена" : "Выключена";
    showToast(enabled ? "Параллельный режим включён. Перезапустите бота." : "Параллельный режим выключен.", "success", 4000);
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
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

// ── Backtesting tab ───────────────────────────────────────────────────────────

let _backtestInstruments = [];
let _backtestStrategies  = [];
let _backtestLastResult  = null;

const _BT_MODE_LABELS = {
  trend: "Trend (SMA9/21)", mean_reversion: "Mean Reversion", breakout: "Breakout",
};

async function renderBacktestTab() {
  const host = document.getElementById("view-backtest");
  if (!host) return;

  if (host.dataset.initialized !== "1") {
    host.innerHTML = `<div class="note" style="padding:24px">Загрузка стратегий…</div>`;
    await _btReloadForm(host);
    host.dataset.initialized = "1";
  }
}

async function _btReloadForm(host) {
  try {
    [_backtestInstruments, _backtestStrategies] = await Promise.all([
      apiGet("/api/backtest/instruments"),
      apiGet("/api/backtest/strategies"),
    ]);
  } catch {
    _backtestInstruments = [];
    _backtestStrategies = [];
  }

  const instrOptions = _backtestInstruments.length
    ? _backtestInstruments.map(i =>
        `<option value="${esc(i.figi)}">${esc(i.ticker)} — ${esc(i.name)}</option>`
      ).join("")
    : `<option value="">Нет инструментов (добавьте в Настройках)</option>`;

  const modeHints = { trend: "Trend", mean_reversion: "MeanRev", breakout: "Breakout" };
  const stratCards = _backtestStrategies.length
    ? _backtestStrategies.map(s => {
        const sl  = (parseFloat(s.stop_loss_pct)  * 100).toFixed(3);
        const tp  = (parseFloat(s.take_profit_pct) * 100).toFixed(3);
        const com = (parseFloat(s.commission_pct)  * 100).toFixed(4);
        const modeLabel = modeHints[s.tradingmode] || s.tradingmode;
        return `
          <label class="bt-strat-card" style="display:flex;align-items:flex-start;gap:10px;padding:10px 14px;
            border:1px solid rgba(76,141,255,.3);border-radius:10px;cursor:pointer;
            background:rgba(76,141,255,.04);margin-bottom:8px">
            <input type="checkbox" class="bt-strat-cb" value="${s.id}" checked style="margin-top:3px;cursor:pointer">
            <div>
              <div style="font-weight:600;color:#eef4ff">${esc(s.name)}</div>
              <div class="note" style="margin-top:2px">
                Режим: <b>${esc(modeLabel)}</b> &nbsp;·&nbsp;
                SL: <b>${sl}%</b> &nbsp;·&nbsp;
                TP: <b>${tp}%</b> &nbsp;·&nbsp;
                Комиссия: <b>${com}%</b>
              </div>
              ${s.instruments.length ? `<div class="note" style="margin-top:2px">
                Инструменты: ${s.instruments.map(i => `<span class="badge badge-active" style="margin-right:4px;font-size:11px">${esc(i.ticker)}</span>`).join("")}
              </div>` : ""}
            </div>
          </label>`;
      }).join("")
    : `<div class="note">Нет стратегий. Создайте хотя бы одну в разделе Настройки.</div>`;

  host.innerHTML = `
    ${helpCard("Бэктест", [
      "<b>Бэктестинг</b> — прогон ваших реальных стратегий на исторических данных T-Bank API. Использует ту же логику сигналов что и живой бот (strategy_engine). SL/TP/комиссия берутся из настроек каждой стратегии.",
      "<b>Как пользоваться:</b> выберите инструмент, интервал и период → отметьте стратегии → «Запустить бэктест». Параметры каждой стратегии видны на карточке (режим, SL, TP, комиссия).",
      "<b>Режимы:</b> <i>Возврат к средней</i> — вход при z-score ≥1.8 (цена далеко от 20-периодной средней); <i>Пробой</i> — пробой 20-барного max/min с объёмом выше среднего; <i>Тренд</i> — SMA9/SMA21 + моментум.",
      "<b>Метрики:</b> Win Rate — % прибыльных; Profit Factor >1 = стратегия в плюсе; Max Drawdown — просадка; R-Multiple >0 = прибыль > риска; Sharpe >1 = хорошее соотношение.",
      "<b>Рекомендации РФ-рынок:</b> SBER/GAZP/LKOH — Mean Reversion на 5–15 мин; нефтяной сектор — Mean Reversion; утренние пробои — Breakout; только на часовых и выше — Trend.",
      "<b>Ограничения:</b> одна позиция за раз, вход по Close, нет учёта проскальзывания и стакана. Историческая Sandbox == боевые данные.",
    ])}

    <div class="block" style="margin-bottom:16px">
      <h2>Параметры бэктеста</h2>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:20px">
        <label class="field-label">
          Инструмент
          <select id="btFigi" class="field" style="width:100%">${instrOptions}</select>
        </label>
        <label class="field-label">
          Интервал свечей
          <select id="btInterval" class="field" style="width:100%">
            <option value="5min">5 минут</option>
            <option value="15min" selected>15 минут</option>
            <option value="hour">1 час</option>
          </select>
        </label>
        <label class="field-label">
          Период (дней)
          <select id="btDays" class="field" style="width:100%">
            <option value="1">1 день</option>
            <option value="3">3 дня</option>
            <option value="7" selected>7 дней</option>
            <option value="14">14 дней</option>
            <option value="30">30 дней</option>
          </select>
        </label>
      </div>

      <div style="margin-bottom:16px">
        <div style="font-weight:600;margin-bottom:10px;color:#eef4ff">
          Стратегии для сравнения
          <span class="note" style="font-weight:400;margin-left:8px">SL, TP и режим берутся из настроек каждой стратегии</span>
        </div>
        <div id="btStratList">${stratCards}</div>
      </div>

      <button class="btn btn-primary" onclick="runBacktest()">▶ Запустить бэктест</button>
      <span id="btStatus" class="note" style="margin-left:12px"></span>
    </div>

    <div id="btResults" style="display:none">
      <div class="block" style="margin-bottom:16px">
        <h2>Сравнение стратегий</h2>
        <div style="overflow-x:auto">
          <table class="table">
            <thead><tr id="btCompareHead"><th>Метрика</th></tr></thead>
            <tbody id="btCompareTbody"></tbody>
          </table>
        </div>
      </div>

      <div class="block" style="margin-bottom:16px">
        <h2>Кривая капитала</h2>
        <div id="btEquityChart" style="height:280px"></div>
      </div>

      <div class="block">
        <h2>Сделки — <span id="btTradesStratName" style="color:#a8c8ff"></span></h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px" id="btStratButtons"></div>
        <div style="overflow-x:auto">
          <table class="table">
            <thead>
              <tr>
                <th>Вход</th><th>Выход</th><th>Направление</th>
                <th>Цена входа</th><th>Цена выхода</th><th>Причина</th>
                <th>PnL ₽</th><th>Комиссия ₽</th>
              </tr>
            </thead>
            <tbody id="btTradesTbody"></tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

async function runBacktest() {
  const figi     = (document.getElementById("btFigi")     || {}).value || "";
  const interval = (document.getElementById("btInterval") || {}).value || "15min";
  const days     = parseInt((document.getElementById("btDays") || {}).value || "7");

  const strategyIds = Array.from(
    document.querySelectorAll(".bt-strat-cb:checked")
  ).map(cb => parseInt(cb.value));

  if (!figi)               { showToast("Выберите инструмент", "error"); return; }
  if (!strategyIds.length) { showToast("Отметьте хотя бы одну стратегию", "error"); return; }

  const status  = document.getElementById("btStatus");
  const results = document.getElementById("btResults");
  if (status)  status.textContent = "Загружаю свечи и прогоняю стратегии…";
  if (results) results.style.display = "none";

  try {
    const data = await apiPostJson("/api/backtest/run", { figi, interval, days, strategy_ids: strategyIds });
    _backtestLastResult = data;
    const n = data.candles_loaded;
    if (status) status.textContent = `Готово. Свечей: ${n}, период: ${data.days} дн.`;
    _renderBacktestResults(data);
    if (results) results.style.display = "block";
  } catch (e) {
    if (status) status.textContent = "";
    showToast(`Ошибка бэктеста: ${e.message}`, "error", 8000);
  }
}

function _renderBacktestResults(data) {
  const results = data.results || {};
  const sids    = Object.keys(results);

  // ── Header row ──
  const head = document.getElementById("btCompareHead");
  if (head) {
    head.innerHTML = `<th>Метрика</th>` + sids.map(sid => {
      const r = results[sid];
      const name = r.strategy_name || `Стратегия ${sid}`;
      const lotsInfo = r.lots ? ` · ${r.lots} лот` : "";
      const mode = r.mode ? `<div class="note" style="font-weight:400;font-size:11px">${esc(r.mode)} · SL ${esc(r.sl_pct_ui || "")} · TP ${esc(r.tp_pct_ui || "")}${lotsInfo}</div>` : "";
      return `<th style="min-width:140px">${esc(name)}${mode}</th>`;
    }).join("");
  }

  // ── Metrics rows ──
  const metrics = [
    ["Свечей",              r => String(r.candles_tested ?? "—")],
    ["Лотов в позиции",     r => r.lots != null ? String(r.lots) : "1"],
    ["Сделок",              r => String(r.total_trades ?? "—")],
    ["Прибыл. / убыт.",     r => `${r.win_trades ?? 0} / ${r.loss_trades ?? 0}`],
    ["Win Rate",            r => r.win_rate    != null ? `${r.win_rate.toFixed(1)}%` : "—"],
    ["Profit Factor",       r => r.profit_factor != null ? r.profit_factor.toFixed(2) : "—"],
    ["Чистый PnL (₽)",     r => {
      if (r.net_pnl == null) return "—";
      return `<span class="${r.net_pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${r.net_pnl.toFixed(2)}</span>`;
    }],
    ["Брутто-прибыль (₽)", r => r.gross_profit != null ? r.gross_profit.toFixed(2) : "—"],
    ["Брутто-убыток (₽)",  r => r.gross_loss   != null ? r.gross_loss.toFixed(2) : "—"],
    ["Max Drawdown (₽)",   r => r.max_drawdown  != null ? r.max_drawdown.toFixed(2) : "—"],
    ["Max Drawdown (%)",   r => r.max_drawdown_pct != null ? `${r.max_drawdown_pct.toFixed(2)}%` : "—"],
    ["Avg R-Multiple",     r => r.avg_r_multiple != null ? r.avg_r_multiple.toFixed(2) : "—"],
    ["Sharpe Ratio",       r => r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : "—"],
    ["Комиссии (₽)",       r => r.total_commission != null ? r.total_commission.toFixed(2) : "—"],
  ];

  const tbody = document.getElementById("btCompareTbody");
  if (tbody) {
    tbody.innerHTML = metrics.map(([label, fn]) => `
      <tr>
        <td><b>${label}</b></td>
        ${sids.map(sid => {
          const r = results[sid];
          if (!r || r.error) return `<td style="color:#ff5c5c;font-size:12px">${esc(r?.error || "Ошибка")}</td>`;
          return `<td>${fn(r)}</td>`;
        }).join("")}
      </tr>`).join("");
  }

  // ── Equity chart ──
  if (window.Plotly) {
    const traces = sids
      .filter(sid => results[sid] && !results[sid].error && results[sid].equity_curve)
      .map(sid => ({
        y: results[sid].equity_curve,
        mode: "lines",
        name: results[sid].strategy_name || `#${sid}`,
        line: { width: 2 },
      }));
    Plotly.newPlot("btEquityChart", traces, {
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: "#eef4ff" }, margin: { t: 10, r: 20, b: 40, l: 60 },
      yaxis: { title: "Капитал (₽)" }, xaxis: { title: "Бар" },
      legend: { orientation: "h", y: -0.15 },
    }, { displayModeBar: false, responsive: true });
  }

  // ── Strategy selector buttons for trade list ──
  const btns = document.getElementById("btStratButtons");
  if (btns) {
    btns.innerHTML = sids.map(sid =>
      `<button class="btn btn-small" onclick="_showBacktestTrades('${sid}')">${
        esc(results[sid]?.strategy_name || `Стратегия ${sid}`)
      }</button>`
    ).join("");
  }

  if (sids.length > 0) _showBacktestTrades(sids[0]);
}

function _showBacktestTrades(sid) {
  const nameEl = document.getElementById("btTradesStratName");
  const tbody  = document.getElementById("btTradesTbody");
  if (!tbody || !_backtestLastResult) return;

  const result = (_backtestLastResult.results || {})[sid];
  const label  = result?.strategy_name || `Стратегия ${sid}`;
  if (nameEl) nameEl.textContent = label;

  if (!result || result.error || !result.trades) {
    tbody.innerHTML = `<tr><td colspan="8" class="note">${esc(result?.error || "Нет данных")}</td></tr>`;
    return;
  }

  const trades = result.trades;
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="note">Сделок нет — сигналы не сработали на этом периоде.</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const pnlCls   = t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
    const dirBadge = t.direction === "BUY"
      ? `<span class="badge badge-buy">Лонг</span>`
      : `<span class="badge badge-sell">Шорт</span>`;
    const exitHtml = t.exit_reason === "SL"  ? `<span style="color:#ff5c5c">SL</span>`
      : t.exit_reason === "TP"  ? `<span style="color:#2ecc71">TP</span>`
      : t.exit_reason === "END" ? `<span style="color:#aaa">Конец</span>`
      : esc(t.exit_reason);
    return `<tr>
      <td class="mono">${esc((t.entry_time || "").slice(0,19).replace("T"," "))}</td>
      <td class="mono">${esc((t.exit_time  || "").slice(0,19).replace("T"," "))}</td>
      <td>${dirBadge}</td>
      <td class="mono">${t.entry_price?.toFixed(4) ?? "—"}</td>
      <td class="mono">${t.exit_price?.toFixed(4)  ?? "—"}</td>
      <td>${exitHtml}</td>
      <td class="mono ${pnlCls}">${t.pnl?.toFixed(2) ?? "—"}</td>
      <td class="mono">${t.commission?.toFixed(2) ?? "—"}</td>
    </tr>`;
  }).join("");
}

// ── Analyst tab ───────────────────────────────────────────────────────────────

let _analystPollTimer = null;
let _analystViewingId = null;

async function renderAnalystTab() {
  const host = document.getElementById("view-analyst");
  if (!host) return;

  if (host.dataset.initialized !== "1") {
    host.innerHTML = `
      ${helpCard("Аналитик", [
        "<b>Что делает:</b> перебирает комбинации инструментов × режимов торговли × SL × TP, запускает бэктест на каждой и сохраняет те где PnL положительный. Поиск идёт в фоне — вы можете пользоваться другими вкладками.",
        "<b>Инструменты поиска:</b> SBER, GAZP, LKOH, GMKN, ROSN, TATN, NVTK, MTSS, MOEX, VTBR, ALRS. Режимы: Mean Reversion, Breakout, Trend. Комбинации SL (0.2–0.5%) × TP (0.4–1.5%). Итого до 600 бэктестов.",
        "<b>Бюджет:</b> сумма используется как начальный капитал в бэктесте — PnL будет реалистичен для вашего депозита.",
        "<b>Фильтры:</b> мин. Win Rate, мин. количество сделок, мин. PnL. Результаты сортируются по составному score = Profit Factor × Win Rate × (1 − Drawdown%).",
        "<b>Как сохранить:</b> нажмите «Подробнее» — проверьте кривую капитала и сделки. Если всё устраивает — введите название и нажмите «Сохранить как стратегию». Стратегия появится в Настройках.",
        "<b>Важно:</b> бэктест не учитывает проскальзывание. Перед боевым запуском проверьте стратегию в Sandbox хотя бы 1–2 дня.",
      ])}

      <div class="block" style="margin-bottom:16px">
        <h2>Параметры поиска</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:16px">
          <label class="field-label">Бюджет (₽)
            <input id="anBudget" class="field" type="number" min="1000" step="1000" value="60000">
          </label>
          <label class="field-label">Интервал свечей
            <select id="anInterval" class="field">
              <option value="5min">5 минут</option>
              <option value="15min" selected>15 минут</option>
              <option value="hour">1 час</option>
            </select>
          </label>
          <label class="field-label">Период бэктеста (дней)
            <select id="anDays" class="field">
              <option value="7">7 дней</option>
              <option value="14" selected>14 дней</option>
              <option value="30">30 дней</option>
            </select>
          </label>
          <label class="field-label">Мин. Win Rate (%)
            <input id="anWinRate" class="field" type="number" min="0" max="100" value="45">
          </label>
          <label class="field-label">Мин. сделок
            <input id="anMinTrades" class="field" type="number" min="1" value="5">
          </label>
          <label class="field-label">Мин. PnL (₽)
            <input id="anMinPnl" class="field" type="number" value="0">
          </label>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="btn btn-primary" id="anBtnStart" onclick="analystStart()">▶ Запустить поиск</button>
          <button class="btn" id="anBtnStop" onclick="analystStop()" style="display:none">⏹ Остановить</button>
          <span id="anStatusText" class="note"></span>
        </div>
      </div>

      <div class="block" id="anProgressBlock" style="display:none;margin-bottom:16px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <b>Прогресс поиска</b>
          <span id="anProgressLabel" class="note"></span>
        </div>
        <div style="background:rgba(255,255,255,.08);border-radius:8px;height:12px;overflow:hidden">
          <div id="anProgressBar" style="height:100%;background:#4c8dff;border-radius:8px;width:0%;transition:width .4s"></div>
        </div>
        <div id="anCurrentCombo" class="note" style="margin-top:6px"></div>
      </div>

      <div id="anResultsBlock" style="display:none">
        <div class="block" style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h2>Найденные стратегии <span id="anResultCount" class="note"></span></h2>
            <span class="note">Сортировка по составному score</span>
          </div>
          <div style="overflow-x:auto">
            <table class="table">
              <thead>
                <tr>
                  <th>Score</th><th>Инструмент</th><th>Режим</th>
                  <th>SL / TP</th><th>Лотов</th><th>Сделок</th><th>Win Rate</th>
                  <th>PnL (₽)</th><th>Profit Factor</th><th>Drawdown</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="anResultsTbody"></tbody>
            </table>
          </div>
        </div>

        <!-- Detail panel -->
        <div class="block" id="anDetailBlock" style="display:none">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
            <h2 id="anDetailTitle">Детали</h2>
            <button class="btn" onclick="document.getElementById('anDetailBlock').style.display='none'">✕</button>
          </div>
          <div id="anDetailMeta" style="margin-bottom:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px"></div>
          <div id="anDetailChart" style="height:240px;margin-bottom:16px"></div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
            <input id="anSaveName" class="field" placeholder="Название новой стратегии" style="flex:1;min-width:220px">
            <button class="btn btn-primary" onclick="analystSave()">💾 Сохранить как стратегию</button>
          </div>
        </div>
      </div>
    `;
    host.dataset.initialized = "1";
  }

  await _analystRefresh();
}

async function _analystRefresh() {
  try {
    const s = await apiGet("/api/analyst/status");
    _analystUpdateStatus(s);
    if (s.status === "running") {
      if (!_analystPollTimer) {
        _analystPollTimer = setInterval(async () => {
          if (getTabFromHash() !== "аналитик") { _analystStopPoll(); return; }
          try {
            const st = await apiGet("/api/analyst/status");
            _analystUpdateStatus(st);
            if (st.status !== "running") { _analystStopPoll(); await _analystLoadResults(); }
          } catch {}
        }, 2000);
      }
    } else {
      _analystStopPoll();
      if (s.status === "done" || s.status === "stopped") await _analystLoadResults();
    }
  } catch {}
}

function _analystStopPoll() {
  if (_analystPollTimer) { clearInterval(_analystPollTimer); _analystPollTimer = null; }
}

function _analystUpdateStatus(s) {
  const prog  = document.getElementById("anProgressBlock");
  const bar   = document.getElementById("anProgressBar");
  const label = document.getElementById("anProgressLabel");
  const combo = document.getElementById("anCurrentCombo");
  const txt   = document.getElementById("anStatusText");
  const btnStart = document.getElementById("anBtnStart");
  const btnStop  = document.getElementById("anBtnStop");

  const running = s.status === "running";
  if (prog)  prog.style.display  = running ? "block" : "none";
  if (btnStart) btnStart.style.display = running ? "none"  : "";
  if (btnStop)  btnStop.style.display  = running ? ""      : "none";

  if (s.total > 0 && bar) {
    const pct = Math.round(s.progress / s.total * 100);
    bar.style.width = pct + "%";
    if (label) label.textContent = `${s.progress} / ${s.total} (${pct}%) · найдено: ${s.found}`;
  }
  if (combo) combo.textContent = s.current || "";

  const statusMap = {
    idle: "Ожидание", running: "Идёт поиск…", done: "Завершён",
    stopped: "Остановлен", error: "Ошибка",
  };
  if (txt) {
    txt.textContent = statusMap[s.status] || s.status;
    if (s.error) txt.textContent += ": " + s.error;
    if (s.started_at) txt.textContent += ` (запуск: ${s.started_at.slice(0,19).replace("T"," ")})`;
  }
}

async function _analystLoadResults() {
  try {
    const results = await apiGet("/api/analyst/results?limit=50");
    const block = document.getElementById("anResultsBlock");
    const tbody = document.getElementById("anResultsTbody");
    const cnt   = document.getElementById("anResultCount");
    if (!tbody) return;
    if (block) block.style.display = results.length ? "block" : "none";
    if (cnt) cnt.textContent = `(${results.length})`;

    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="note">Нет результатов. Попробуйте снизить фильтры или увеличить период.</td></tr>`;
      return;
    }

    const modeColors = {mean_reversion: "#a8c8ff", breakout: "#2ecc71", trend: "#f0c04a"};
    tbody.innerHTML = results.map(r => {
      const saved = r.saved ? `<span class="badge badge-active">Сохранена</span>` : `<button class="btn btn-small" onclick="analystDetail(${r.id})">Подробнее</button>`;
      const scoreColor = r.score >= 30 ? "#2ecc71" : r.score >= 15 ? "#f0c04a" : "#aaa";
      const modeColor  = modeColors[r.mode] || "#eef4ff";
      const lotsCalc   = (r.avg_price > 0 && r.budget_rub > 0)
        ? Math.max(1, Math.floor(r.budget_rub * 0.95 / r.avg_price))
        : "—";
      return `<tr>
        <td><b style="color:${scoreColor}">${r.score.toFixed(1)}</b></td>
        <td><b>${esc(r.ticker)}</b><div class="note" style="font-size:11px">${esc(r.instrument_name)}</div></td>
        <td style="color:${modeColor};font-size:12px">${esc(r.mode_label)}</td>
        <td class="mono" style="font-size:12px">${esc(r.sl_pct_ui)} / ${esc(r.tp_pct_ui)}</td>
        <td class="mono">${lotsCalc}</td>
        <td>${r.total_trades}</td>
        <td class="${r.win_rate >= 50 ? "pnl-pos" : ""}">${r.win_rate.toFixed(1)}%</td>
        <td class="mono ${r.net_pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${r.net_pnl.toFixed(2)}</td>
        <td class="mono ${r.profit_factor >= 1 ? "pnl-pos" : "pnl-neg"}">${r.profit_factor.toFixed(2)}</td>
        <td class="mono">${r.max_drawdown.toFixed(2)}</td>
        <td>${saved}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    console.error("analyst results error", e);
  }
}

async function analystStart() {
  const budget    = parseFloat((document.getElementById("anBudget")    || {}).value || "60000");
  const interval  = (document.getElementById("anInterval")  || {}).value || "15min";
  const days      = parseInt((document.getElementById("anDays")      || {}).value || "14");
  const winRate   = parseFloat((document.getElementById("anWinRate")   || {}).value || "45");
  const minTrades = parseInt((document.getElementById("anMinTrades") || {}).value || "5");
  const minPnl    = parseFloat((document.getElementById("anMinPnl")    || {}).value || "0");

  try {
    await apiPostJson("/api/analyst/start", { budget_rub: budget, min_win_rate: winRate, min_trades: minTrades, days, interval, min_pnl: minPnl });
    showToast("Поиск запущен", "success");
    // Reset results block
    const block = document.getElementById("anResultsBlock");
    if (block) block.style.display = "none";
    const detail = document.getElementById("anDetailBlock");
    if (detail) detail.style.display = "none";
    await _analystRefresh();
  } catch (e) {
    showToast(`Ошибка запуска: ${e.message}`, "error", 6000);
  }
}

async function analystStop() {
  try {
    await fetch("/api/analyst/stop", { method: "POST", credentials: "same-origin" });
    showToast("Остановка запрошена", "info");
  } catch {}
}

async function analystDetail(id) {
  _analystViewingId = id;
  const detail = document.getElementById("anDetailBlock");
  const title  = document.getElementById("anDetailTitle");
  const meta   = document.getElementById("anDetailMeta");
  const chart  = document.getElementById("anDetailChart");
  const nameIn = document.getElementById("anSaveName");
  if (!detail) return;

  try {
    const r = await apiGet(`/api/analyst/result/${id}`);
    const modeLabels = {mean_reversion:"Возврат к средней", breakout:"Пробой", trend:"Тренд"};

    if (title) title.textContent = `${r.ticker} — ${modeLabels[r.tradingmode] || r.tradingmode}`;
    if (nameIn) nameIn.value = `${r.ticker} ${modeLabels[r.tradingmode] || r.tradingmode} ${r.sl_pct_ui}/${r.tp_pct_ui}`;

    const metaItems = [
      ["Режим",         modeLabels[r.tradingmode] || r.tradingmode],
      ["SL / TP",       `${r.sl_pct_ui} / ${r.tp_pct_ui}`],
      ["Интервал",      `${r.interval}, ${r.days} дн.`],
      ["Сделок",        r.total_trades],
      ["Win Rate",      `${r.win_rate?.toFixed(1)}%`],
      ["Чистый PnL",   `${r.net_pnl?.toFixed(2)} ₽`],
      ["Profit Factor", r.profit_factor?.toFixed(2)],
      ["Max Drawdown",  `${r.max_drawdown?.toFixed(2)} ₽`],
      ["R-Multiple",    r.avg_r_multiple?.toFixed(2)],
      ["Sharpe",        r.sharpe_ratio?.toFixed(2)],
      ["Score",          r.score?.toFixed(1)],
      ["Ср. цена инстр.", r.avg_price_ui || "—"],
      ["Лотов (бюджет)",  r.lots_calc != null ? `${r.lots_calc} лот` : "—"],
      ["Бюджет",          r.budget_ui || "—"],
    ];
    if (meta) {
      meta.innerHTML = metaItems.map(([k, v]) => `
        <div style="background:rgba(255,255,255,.05);border-radius:8px;padding:8px 12px">
          <div class="note" style="font-size:11px">${esc(k)}</div>
          <div style="font-weight:600">${esc(String(v))}</div>
        </div>`).join("");
    }

    if (window.Plotly && chart && r.equity_curve?.length) {
      Plotly.newPlot(chart, [{
        y: r.equity_curve, mode: "lines",
        line: { color: "#4c8dff", width: 2 },
        name: "Капитал",
      }], {
        paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: "#eef4ff" }, margin: { t: 10, r: 20, b: 30, l: 60 },
        yaxis: { title: "Капитал (₽)" }, xaxis: { title: "Бар" },
      }, { displayModeBar: false, responsive: true });
    }

    detail.style.display = "block";
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showToast(`Ошибка загрузки: ${e.message}`, "error");
  }
}

async function analystSave() {
  if (!_analystViewingId) { showToast("Сначала откройте детали стратегии", "error"); return; }
  const nameIn = document.getElementById("anSaveName");
  const name   = (nameIn || {}).value?.trim() || "";
  if (!name) { showToast("Введите название стратегии", "error"); return; }

  try {
    const res = await apiPostJson(`/api/analyst/save/${_analystViewingId}`, { strategy_name: name });
    showToast(`Стратегия «${res.strategy_name}» сохранена`, "success", 5000);
    document.getElementById("anDetailBlock").style.display = "none";
    _analystViewingId = null;
    await _analystLoadResults();
  } catch (e) {
    showToast(`Ошибка сохранения: ${e.message}`, "error", 7000);
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
    } else if (tab === "бэктест") {
      await renderBacktestTab();
    } else if (tab === "аналитик") {
      await renderAnalystTab();
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
  // Экспортируем все обработчики в глобальный scope ДО любых await,
  // чтобы кнопки работали даже если API-запросы упадут с ошибкой.
  window.serviceAction    = serviceAction;
  window.closeOnePosition = closeOnePosition;
  window.closeAllPositionsConfirm = closeAllPositionsConfirm;
  window.clearLocalPositions = clearLocalPositions;
  window.runBacktest = runBacktest;
  window._showBacktestTrades = _showBacktestTrades;
  window.toggleParallel = toggleParallel;
  window.onParallelToggle = onParallelToggle;
  window.addParallelStrategy = addParallelStrategy;
  window.removeParallelStrategy = removeParallelStrategy;
  window.expandParallelStrategy = expandParallelStrategy;
  window.analystStart = analystStart;
  window.analystStop  = analystStop;
  window.analystDetail = analystDetail;
  window.analystSave  = analystSave;
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

  bindRouter();
  try { await renderSummaryCards(); } catch (e) { console.error("renderSummaryCards:", e); }
  try { await applyRoute(); } catch (e) { console.error("applyRoute:", e); }
  startRefreshLoops();
}

document.addEventListener("DOMContentLoaded", bootstrapDashboard);
