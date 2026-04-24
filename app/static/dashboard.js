const REFRESH_SUMMARY_MS = 7000;
const REFRESH_QUOTES_MS = 5000;
const REFRESH_PORTFOLIO_MS = 10000;

let instrumentSearchData = [];
let refreshTimersStarted = false;

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
    const decoded = decodeURIComponent(raw);
    return normalizeTab(decoded);
  } catch (e) {
    console.error("[tabs] decode hash error:", e, hash);
    return "главное";
  }
}

function setActiveTabButton(tab) {
  document.querySelectorAll("[data-tab-link]").forEach((el) => {
    el.classList.toggle("active", el.dataset.tabLink === tab);
  });
}

async function acceptSelectedInstruments() {
  try {
    const selectedIndexes = [...document.querySelectorAll("[data-instrument-pick]:checked")]
      .map((el) => Number(el.dataset.instrumentPick))
      .filter((idx) => Number.isInteger(idx) && instrumentSearchData[idx]);

    if (!selectedIndexes.length) {
      showToast("Отметь хотя бы один инструмент", "error");
      return;
    }

    const items = selectedIndexes.map((idx) => instrumentSearchData[idx]);

    await apiPostJson("/api/instruments/add", items);

    showToast(`Добавлено инструментов: ${items.length}`, "success");
    closeAddInstrumentModal();
    instrumentSearchData = [];
    await renderSettingsTab();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка добавления: ${e.message}`, "error");
  }
}

function setVisibleView(tab) {
  const normalized = normalizeTab(tab);
  const views = document.querySelectorAll("[data-view]");

  console.log("[tabs] switch ->", normalized, "views:", views.length);

  views.forEach((el) => {
    el.classList.add("hidden");
    el.style.display = "none";
  });

  const active = document.querySelector(`[data-view="${normalized}"]`);
  if (!active) {
    console.error("[tabs] view not found:", normalized);
    return;
  }

  active.classList.remove("hidden");
  active.style.display = "block";

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

  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return await r.json();
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
    let node = document.querySelector(`[data-view="${tab}"]`);
    if (!node) {
      node = document.createElement("section");
      node.setAttribute("data-view", tab);
      node.id = "view-" + tab;
      node.className = "block hidden";
      node.innerHTML = `<h2>${tab}</h2><p>Вкладка ${tab} подключена, но контент ещё не загружен.</p>`;
      root.appendChild(node);
      console.warn("[tabs] auto-created missing view:", tab);
    }
  });
}

function summaryValueClass(value) {
  const v = String(value || "").trim().toLowerCase();

  if (["запущен", "ведётся", "ведется", "ok", "активен", "активна"].includes(v)) {
    return "value status-ok";
  }
  if (["остановлен", "остановлена", "выкл", "disabled", "inactive"].includes(v)) {
    return "value status-off";
  }
  if (["проблема", "error", "failed", "warning", "warn"].includes(v)) {
    return "value status-problem";
  }
  return "value";
}

function summaryCard(label, value, extraClass = "") {
  return `
    <div class="card ${extraClass}">
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
    ${summaryCard("ПнЛ день", s.daily_pnl_ui)}
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

async function bootstrapDashboard() {
  console.log("[tabs] DOMContentLoaded");
  bindRouter();
  await renderSummaryCards();
  toggleSummaryCardsVisibility();
  await applyRoute();
  startRefreshLoops();
  window.selectAllInstrumentSearchRows = selectAllInstrumentSearchRows;
  window.clearAllInstrumentSearchRows = clearAllInstrumentSearchRows;
}

function helpCard(title, bullets) {
  return `
    <section class="help-card">
      <h2>Справка: ${title}</h2>
      <ul>${bullets.map((x) => `<li>${x}</li>`).join("")}</ul>
    </section>
  `;
}


async function renderMainShell() {
  const host = document.getElementById("view-main");
  if (!host) return;
  if (host.dataset.initialized === "1") return;

  host.innerHTML = `
    ${helpCard("Главное", [
      "Показывает текущее состояние бота, дневную статистику и быстрые действия.",
      "Блок 'Управление' нужен для запуска, остановки и перезапуска сервиса.",
      "Таблица инструментов показывает бумагу, лоты, SL/TP и текущую цену.",
      "Позиции и сделки обновляются отдельно от остальных вкладок."
    ])}
    <section class="block">
      <div class="row between">
        <h2>Runtime</h2>
        <div class="note">Текущее состояние бота</div>
      </div>
      <div id="runtimeBox">Загрузка...</div>
    </section>

    <section class="block">
      <div class="row between">
        <h2>Почему бот сейчас не торгует</h2>
        <div class="note">Короткое объяснение</div>
      </div>
      <div id="botExplainBox">Загрузка...</div>
    </section>
    <section class="block">
      <div class="row between">
        <h2>Управление</h2>
        <div class="note">Быстрые действия с сервисом</div>
      </div>
      <div class="row-buttons">
        <button class="btn btn-primary" id="btnStartService">Запустить</button>
        <button class="btn" id="btnStopService">Остановить</button>
        <button class="btn" id="btnRestartService">Перезапустить</button>
      </div>
    </section>

    <section class="block">
      <div class="row between">
        <h2>Инструменты</h2>
        <div class="note">Активные бумаги и котировки</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Тикер</th><th>Название</th><th>Вкл</th><th>Лоты</th><th>SL%</th><th>TP%</th><th>Цена</th><th>Время</th>
            </tr>
          </thead>
          <tbody id="mainInstrumentsBody"></tbody>
        </table>
      </div>
    </section>

    <section class="two-cols">
      <div class="block">
        <div class="row between">
          <h2>Позиции</h2>
          <div class="note">Открытые позиции</div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Тикер</th><th>Напр.</th><th>Кол-во</th><th>Вход</th><th>Тек.</th><th>ПнЛ</th><th>Открыта</th>
              </tr>
            </thead>
            <tbody id="mainPositionsBody"></tbody>
          </table>
        </div>
      </div>

      <div class="block">
        <div class="row between">
          <h2>Сделки</h2>
          <div class="note">Последние сделки</div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Время</th><th>Тикер</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Кол-во</th><th>ПнЛ</th><th>Причина</th>
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

  diffTbody(
    document.getElementById("mainInstrumentsBody"),
    (data.instruments || []).map((i) => `
      <tr>
        <td>${esc(i.ticker)}</td>
        <td>${esc(i.name)}</td>
        <td>${yesnoValue(i.enabled)}</td>
        <td>${esc(i.lotsoverride || 1)}</td>
        <td>${esc(i.stop_loss_pct_ui)}</td>
        <td>${esc(i.take_profit_pct_ui)}</td>
        <td class="live-price" data-figi="${esc(i.figi)}">${esc(i.last_price_ui)}</td>
        <td class="live-time" data-figi="${esc(i.figi)}">${esc(i.price_time)}</td>
      </tr>
    `).join("")
  );

  diffTbody(
    document.getElementById("mainPositionsBody"),
    (data.positions || []).map((p) => `
      <tr>
        <td>${esc(p.ticker)}</td>
        <td>${esc(p.direction)}</td>
        <td>${esc(p.qty)}</td>
        <td>${esc(p.entry_price_ui)}</td>
        <td>${esc(p.current_price_ui)}</td>
        <td>${esc(p.unrealized_pnl_ui)}</td>
        <td>${esc(p.opened_at)}</td>
      </tr>
    `).join("")
  );

  diffTbody(
    document.getElementById("mainTradesBody"),
    (data.trades || []).map((t) => `
      <tr>
        <td>${esc(t.time)}</td>
        <td>${esc(t.ticker)}</td>
        <td>${esc(t.direction)}</td>
        <td>${esc(t.entry_ui)}</td>
        <td>${esc(t.exit_ui)}</td>
        <td>${esc(t.qty)}</td>
        <td>${esc(t.pnl_ui)}</td>
        <td>${esc(t.reason)}</td>
      </tr>
    `).join("")
  );

  try {
    const health = await apiGet("/api/health");
    const box = document.getElementById("healthBox");
    if (box) {
      box.innerHTML = `
        <div><strong>Статус:</strong> <span class="health-${health.status === "ok" ? "ok" : "warn"}">${health.status}</span></div>
        <div style="margin-top:10px;">
          ${(health.checks || []).map(x => `<div><strong>${x.name}:</strong> ${x.status} — ${x.details}</div>`).join("")}
        </div>
      `;
    }
  } catch (e) {
    const box = document.getElementById("healthBox");
    if (box) {
      box.innerHTML = `<span class="health-error">Ошибка health-check: ${e.message}</span>`;
    }
  }
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
    if (box) {
      box.innerHTML = `
        <ul>
          ${(explain.reasons || []).map(x => `<li>${esc(x)}</li>`).join("")}
        </ul>
      `;
    }
  } catch (e) {
    const box = document.getElementById("botExplainBox");
    if (box) box.innerHTML = `<span class="health-error">Ошибка explain: ${esc(e.message)}</span>`;
  }
}

async function refreshQuotesOnly() {
  if (getTabFromHash() !== "главное") return;

  const quotes = await apiGet("/api/dashboard/quotes");
  const map = {};
  for (const q of quotes) map[q.figi] = q;

  document.querySelectorAll(".live-price[data-figi]").forEach((el) => {
    const figi = el.dataset.figi;
    if (map[figi]) el.textContent = map[figi].last_price_ui;
  });

  document.querySelectorAll(".live-time[data-figi]").forEach((el) => {
    const figi = el.dataset.figi;
    if (map[figi]) el.textContent = map[figi].price_time;
  });
}

async function renderPortfolioTab() {
  const host = document.getElementById("view-portfolio");
  if (!host) return;

  const data = await apiGet("/api/dashboard/portfolio");

  host.innerHTML = `
    ${helpCard("Портфель", [
      "Здесь показаны позиции счёта, позиции бота и активные стоп-заявки.",
      "Кнопка 'Закрыть все' отправляет команду на закрытие всех позиций бота.",
      "Блок стоп-заявок нужен для создания защитных заявок по выбранной позиции."
    ])}

    <section class="block">
      <div class="row between">
        <h2>Портфель счёта</h2>
        <div class="note">Все найденные позиции</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Тикер</th><th>FIGI</th><th>Тип</th><th>Кол-во</th><th>Средняя</th><th>Текущая</th><th>Доход</th></tr>
          </thead>
          <tbody>
            ${(data.portfolio_positions || []).map((p) => `
              <tr>
                <td>${esc(p.ticker)}</td>
                <td>${esc(p.figi)}</td>
                <td>${esc(p.instrument_type)}</td>
                <td>${esc(p.quantity_ui)}</td>
                <td>${esc(p.average_position_price_ui)}</td>
                <td>${esc(p.current_price_ui)}</td>
                <td>${esc(p.expected_yield_ui)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>
    <section class="block">
     <div class="row between">
        <h2>Активные stop orders</h2>
        <div class="note">SL / TP из T-Bank</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>FIGI</th>
              <th>Тип</th>
              <th>Направление</th>
              <th>Лоты</th>
              <th>Цена</th>
              <th>Stop</th>
              <th>Создан</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody id="activeStopOrdersBody"></tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <div class="row between">
        <h2>Позиции бота</h2>
        <div class="row-buttons">
          <button class="btn btn-danger" id="btnCloseAllPositions">Закрыть все</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Тикер</th><th>FIGI</th><th>Напр.</th><th>Кол-во</th><th>Вход</th><th>Текущая</th><th>ПнЛ</th><th>Действие</th></tr>
          </thead>
          <tbody>
            ${(data.bot_positions || []).map((p) => `
              <tr>
                <td>${esc(p.ticker)}</td>
                <td>${esc(p.figi)}</td>
                <td>${esc(p.direction)}</td>
                <td>${esc(p.qty)}</td>
                <td>${esc(p.entry_price_ui)}</td>
                <td>${esc(p.current_price_ui)}</td>
                <td>${esc(p.unrealized_pnl_ui)}</td>
                <td>
                  <button class="btn" data-create-stops
                    data-figi="${esc(p.figi)}"
                    data-qty="${esc(p.qty)}"
                    data-entry="${esc(p.entry_price_ui)}"
                    data-direction="${esc(p.direction)}">
                    SL/TP
                  </button>
                  <button class="btn btn-danger" data-close-one
                    data-figi="${esc(p.figi)}"
                    data-qty="${esc(p.qty)}"
                    data-direction="${esc(p.direction)}">
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
  try {
    const stopData = await apiGet("/api/dashboard/stop-orders");
    const stopBody = document.getElementById("activeStopOrdersBody");
    if (stopBody) {
      stopBody.innerHTML = (stopData.items || []).map(x => `
        <tr>
          <td>${esc(x.figi || "")}</td>
          <td>${esc(x.stop_order_type || "")}</td>
          <td>${esc(x.direction || "")}</td>
          <td>${esc(x.lots_requested || "")}</td>
          <td>${esc(x.price || "")}</td>
          <td>${esc(x.stop_price || "")}</td>
          <td>${esc(x.created_at || "")}</td>
          <td><button class="btn btn-danger" data-cancel-stop="${esc(x.stop_order_id || "")}">Отменить</button></td>
        </tr>
      `).join("") || `<tr><td colspan="8">Нет активных stop orders</td></tr>`;
    }
  } catch (e) {
    const stopBody = document.getElementById("activeStopOrdersBody");
    if (stopBody) {
      stopBody.innerHTML = `<tr><td colspan="8">Ошибка загрузки stop orders: ${esc(e.message)}</td></tr>`;
    }
  }
  host.querySelectorAll("[data-close-one]").forEach((btn) => {
    btn.addEventListener("click", () => closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction));
  });
  host.querySelectorAll("[data-create-stops]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const figi = btn.dataset.figi;
        const qty = btn.dataset.qty;
        const entry = btn.dataset.entry;
        const direction = btn.dataset.direction;
        await apiPostForm("/api/stop-orders/create-bundle", {
          figi,
          qty,
          entry_price: entry,
          side: direction,
          stop_pct: "0.0025",
          take_pct: "0.0050",
        });
        showToast("SL/TP bundle создан", "success");
      } catch (e) {
        showToast(`Ошибка SL/TP: ${e.message}`, "error");
      }
    });
  });
  host.querySelectorAll("[data-cancel-stop]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await apiPostForm("/api/stop-orders/cancel", {
          stop_order_id: btn.dataset.cancelStop,
        });
        showToast("Stop order отменён", "success");
        await renderPortfolioTab();
      } catch (e) {
        showToast(`Ошибка отмены stop order: ${e.message}`, "error");
      }
    });
  });
}

async function renderSettingsTab() {
  const host = document.getElementById("view-settings");
  if (!host) return;

  const data = await apiGet("/api/dashboard/settings");
  const s = data.settings || {};

  host.innerHTML = `
    ${helpCard("Настройки", [
      "Эта вкладка управляет риском, профилями и режимами торговли.",
      "Сохраняй стратегию после изменения лимитов и процентных параметров.",
      "Профиль — это набор общих настроек. Стратегия — режим торговли с собственными параметрами.",
      "У каждого инструмента можно отдельно задать лоты, SL/TP, спред и приоритет."
    ])}

    <section class="block">
      <h2>Общие</h2>
      <form id="systemSettingsForm" class="form-grid">
        <label>Торговля
          <select class="field" name="bot_enabled">
            <option value="1" ${String(s.bot_enabled) === "1" ? "selected" : ""}>Вкл</option>
            <option value="0" ${String(s.bot_enabled) === "0" ? "selected" : ""}>Выкл</option>
          </select>
        </label>

        <label>ТГ только ошибки
          <select class="field" name="telegram_errors_only">
            <option value="1" ${String(s.telegram_errors_only) === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${String(s.telegram_errors_only) === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>

        <label>Автоперечитка
          <select class="field" name="auto_reload_settings">
            <option value="1" ${String(s.auto_reload_settings) === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${String(s.auto_reload_settings) === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>

        <label>Режим API
          <select class="field" name="runtime_mode" id="runtimeModeSelect">
            <option value="sandbox" ${(String(s.tinvestusesandbox || "true") === "true") ? "selected" : ""}>Sandbox</option>
            <option value="prod" ${(String(s.tinvestusesandbox || "true") === "false") ? "selected" : ""}>Боевой</option>
          </select>
        </label>

        <div class="row-buttons">
          <button type="button" class="btn btn-primary" id="btnSaveSystemSettings">Сохранить</button>
        </div>
      </form>
    </section>

    <section class="block">
      <h2>Риск и стратегия</h2>
      <form id="strategySettingsForm" class="form-grid">
        <label>Сделок/день<input class="field" name="max_trades_per_day" value="${esc(s.max_trades_per_day || 15)}"></label>
        <label>Лимит убытка<input class="field" name="max_daily_loss_rub" value="${esc(s.max_daily_loss_rub_ui || 0)}"></label>
        <label>Позиций макс<input class="field" name="max_open_positions" value="${esc(s.max_open_positions || 2)}"></label>
        <label>Пауза, сек<input class="field" name="pause_after_error_sec" value="${esc(s.pause_after_error_sec || 10)}"></label>
        <label>SL %<input class="field" name="default_stop_loss_pct" value="${esc(s.default_stop_loss_pct_ui || 0.25)}"></label>
        <label>TP %<input class="field" name="default_take_profit_pct" value="${esc(s.default_take_profit_pct_ui || 0.50)}"></label>
        <label>Комиссия %<input class="field" name="estimated_commission_pct" value="${esc(s.estimated_commission_pct_ui || 0.04)}"></label>
        <label>Режим торговли
          <select class="field" name="tradingmode">
            <option value="trend" ${(String(s.tradingmode || "trend") === "trend") ? "selected" : ""}>Тренд</option>
            <option value="mean_reversion" ${(String(s.tradingmode || "trend") === "mean_reversion") ? "selected" : ""}>Возврат к средней</option>
            <option value="breakout" ${(String(s.tradingmode || "trend") === "breakout") ? "selected" : ""}>Пробой</option>
          </select>
        </label>
        <label>Лонг
          <select class="field" name="allow_long_global">
            <option value="1" ${String(s.allow_long_global) === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${String(s.allow_long_global) === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Шорт
          <select class="field" name="allow_short_global">
            <option value="1" ${String(s.allow_short_global) === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${String(s.allow_short_global) === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Только сессия
          <select class="field" name="trade_only_session">
            <option value="1" ${String(s.trade_only_session) === "1" ? "selected" : ""}>Да</option>
            <option value="0" ${String(s.trade_only_session) === "0" ? "selected" : ""}>Нет</option>
          </select>
        </label>
        <label>Интервал, сек<input class="field" name="check_interval_sec" value="${esc(s.check_interval_sec || 5)}"></label>
        <label>Пауза после ошибок
          <input class="field" name="errorseriespausecount" value="${esc(s.errorseriespausecount || 3)}">
        </label>
        <label>Пауза после стопов
          <input class="field" name="stopseriespausecount" value="${esc(s.stopseriespausecount || 3)}">
        </label>
        <div class="row-buttons">
          <button type="button" class="btn btn-primary" id="btnSaveStrategySettings">Сохранить стратегию</button>
        </div>
      </form>
    </section>

    <section class="block">
      <div class="row between">
        <h2>Профили</h2>
        <div class="row">
          <input class="field" id="newProfileName" type="text" placeholder="Имя профиля">
          <button class="btn" id="btnCreateProfile">Создать профиль</button>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Профиль</th><th>Активен</th><th>Создан</th><th>Действие</th></tr>
          </thead>
          <tbody>
            ${(data.profiles || []).map((p) => `
              <tr>
                <td>${esc(p.profile_name)}</td>
                <td>${p.is_active === 1 ? "Да" : "Нет"}</td>
                <td>${esc(p.created_at)}</td>
                <td><button class="btn" data-activate-profile="${esc(p.profile_name)}">Активировать</button></td>
                <td><button class="btn btn-danger" data-delete-profile="${esc(p.profile_name)}" ${p.is_active === 1 ? "disabled title='Нельзя удалить активный профиль'" : ""} style="background:#c0392b;opacity:${p.is_active === 1 ? '0.4' : '1'}">Удалить</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2>Стратегии</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Стратегия</th><th>Активна</th><th>Создана</th><th>Выбрать</th><th>Сохранить</th></tr>
          </thead>
          <tbody>
            ${(data.strategies || []).map((x) => `
              <tr>
                <td>${esc(x.strategy_name)}</td>
                <td>${x.is_active === 1 ? "Да" : "Нет"}</td>
                <td>${esc(x.created_at)}</td>
                <td><button class="btn" data-activate-strategy="${esc(x.strategy_name)}">Активировать</button></td>
                <td><button class="btn" data-save-strategy="${esc(x.strategy_name)}">Сохранить</button></td>
                <td><button class="btn btn-danger" data-delete-strategy="${esc(x.strategy_name)}" ${x.is_active === 1 ? "disabled title='Нельзя удалить активную стратегию'" : ""} style="background:#c0392b;opacity:${x.is_active === 1 ? '0.4' : '1'}">Удалить</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2>Инструменты</h2>
      <button class="btn" id="btnOpenAddInstrument">Добавить инструмент</button>
      ${(data.instruments || []).map((i) => `
        <form class="form-grid instrument-form block" data-figi="${esc(i.figi)}">
          <label>Тикер<input class="field" value="${esc(i.ticker)}" disabled></label>
          <label>Лоты<input class="field" name="lots_override" value="${esc(i.lotsoverride || 1)}"></label>
          <label>SL %<input class="field" name="stop_loss_pct" value="${esc(i.stop_loss_pct_ui)}"></label>
          <label>TP %<input class="field" name="take_profit_pct" value="${esc(i.take_profit_pct_ui)}"></label>
          <label>Спред %<input class="field" name="max_spread_pct" value="${esc(i.max_spread_pct_ui)}"></label>
          <label>Мин. объём<input class="field" name="min_volume" value="${esc(i.minvolume || 0)}"></label>
          <label>Лонг
            <select class="field" name="allow_long">
              <option value="1" ${(String(i.allowlong) === "1" || i.allowlong === 1) ? "selected" : ""}>Да</option>
              <option value="0" ${(String(i.allowlong) === "0" || i.allowlong === 0) ? "selected" : ""}>Нет</option>
            </select>
          </label>
          <label>Шорт
            <select class="field" name="allow_short">
              <option value="1" ${(String(i.allowshort) === "1" || i.allowshort === 1) ? "selected" : ""}>Да</option>
              <option value="0" ${(String(i.allowshort) === "0" || i.allowshort === 0) ? "selected" : ""}>Нет</option>
            </select>
          </label>
          <label>Приоритет<input class="field" name="priority" value="${esc(i.priority || 100)}"></label>
          <label>Вкл
            <select class="field" name="enabled">
              <option value="1" ${(String(i.enabled) === "1" || i.enabled === 1) ? "selected" : ""}>Да</option>
              <option value="0" ${(String(i.enabled) === "0" || i.enabled === 0) ? "selected" : ""}>Нет</option>
            </select>
          </label>
          <div class="row-buttons">
            <button type="submit" class="btn btn-primary">Сохранить</button>
            <button type="button" class="btn btn-danger" data-delete-instrument="${esc(i.figi)}">Удалить</button>
          </div>
        </form>
      `).join("")}
    </section>
  `;

  document.getElementById("btnSaveSystemSettings")?.addEventListener("click", saveSystemSettings);
  document.getElementById("btnSaveStrategySettings")?.addEventListener("click", saveStrategySettings);
  document.getElementById("btnCreateProfile")?.addEventListener("click", createProfile);
  document.getElementById("btnOpenAddInstrument")?.addEventListener("click", openAddInstrumentModal);
  document.getElementById("runtimeModeSelect")?.addEventListener("change", async (e) => {
    try {
      const mode = e.target.value;
      await apiPostForm("/api/settings/runtime-mode", { mode });
      showToast(`Режим API переключён: ${mode}`, "success");
    } catch (err) {
      showToast(`Ошибка переключения режима: ${err.message}`, "error");
    }
  });

host.querySelectorAll("[data-activate-profile]").forEach((btn) => {
  btn.addEventListener("click", () => activateProfile(btn.dataset.activateProfile));
});
host.querySelectorAll("[data-delete-profile]").forEach((btn) => {
  btn.addEventListener("click", () => deleteProfile(btn.dataset.deleteProfile));
});
host.querySelectorAll("[data-activate-strategy]").forEach((btn) => {
  btn.addEventListener("click", () => activateStrategy(btn.dataset.activateStrategy));
});
host.querySelectorAll("[data-save-strategy]").forEach((btn) => {
  btn.addEventListener("click", () => saveStrategy(btn.dataset.saveStrategy));
});
host.querySelectorAll("[data-delete-strategy]").forEach((btn) => {
  btn.addEventListener("click", () => deleteStrategy(btn.dataset.deleteStrategy));
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
    ${helpCard("История", [
      "Вкладка показывает сделки, системные события, ошибки и общий журнал.",
      "Используй фильтры поиска по таблицам, чтобы быстро искать причину входа, ошибки и сигналы.",
      "Журнал нужен для разбора работы стратегии и сбоев сервиса."
    ])}

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
          <thead>
            <tr><th>Время</th><th>Тикер</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Кол-во</th><th>Комиссия</th><th>ПнЛ</th><th>Причина</th></tr>
          </thead>
          <tbody>
            ${(data.trades || []).map((t) => `
              <tr>
                <td>${esc(t.time)}</td>
                <td>${esc(t.ticker)}</td>
                <td>${esc(t.direction)}</td>
                <td>${esc(t.entry_ui)}</td>
                <td>${esc(t.exit_ui)}</td>
                <td>${esc(t.qty)}</td>
                <td>${esc(t.commission_ui)}</td>
                <td>${esc(t.pnl_ui)}</td>
                <td>${esc(t.reason)}</td>
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
          <thead>
            <tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr>
          </thead>
          <tbody>${mapLogs(data.system_logs)}</tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2>Ошибки</h2>
      <div class="table-wrap">
        <table data-filter-input="filterErrors">
          <thead>
            <tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr>
          </thead>
          <tbody>${mapLogs(data.error_logs)}</tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2>Журнал</h2>
      <div class="table-wrap">
        <table data-filter-input="filterCommon">
          <thead>
            <tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr>
          </thead>
          <tbody>${mapLogs(data.common_logs)}</tbody>
        </table>
      </div>
    </section>
  `;

  attachTableFilters();
}

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
        <li>Показывает свечной график по выбранному инструменту из T-Bank API.</li>
        <li>Можно переключать инструмент и таймфрейм.</li>
        <li>Показывается score сигнала и причины входа или пропуска сделки.</li>
      </ul>
    </section>

    <section class="block">
      <div class="row between">
        <h2>Health-check</h2>
        <div class="note">Состояние dashboard</div>
      </div>
      <div id="healthBox">Загрузка...</div>
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
  const intervalSelect = document.getElementById("chartIntervalSelect");

  const available = data.available_instruments || [];
  figiSelect.innerHTML = available.map(x => `<option value="${x.figi}">${x.ticker} — ${x.name}</option>`).join("");

  if (data.selected_figi) {
    figiSelect.value = data.selected_figi;
  }
  if (data.interval) {
    intervalSelect.value = data.interval;
  }

  renderCandlesAndScore(data);
  // Обновляем health-check для вкладки "График"
  try {
    const health = await apiGet("/api/health");
    const box = document.getElementById("healthBox");
    if (box) {
      box.innerHTML = `
        <div><strong>Статус:</strong> <span class="health-${health.status === "ok" ? "ok" : "warn"}">${health.status}</span></div>
        <div style="margin-top:10px;">
          ${(health.checks || []).map(x => `<div><strong>${x.name}:</strong> ${x.status} — ${x.details}</div>`).join("")}
        </div>
      `;
    }
  } catch (e) {
    const box = document.getElementById("healthBox");
    if (box) box.innerHTML = `<span class="health-error">Ошибка health-check: ${e.message}</span>`;
  }

  document.getElementById("btnReloadChart")?.addEventListener("click", async () => {
    await renderChartTabWithParams(
      document.getElementById("chartFigiSelect")?.value || "",
      document.getElementById("chartIntervalSelect")?.value || "1min"
    );
  });
}

async function renderChartTabWithParams(figi, interval) {
  const data = await apiGet(`/api/dashboard/chart?figi=${encodeURIComponent(figi)}&interval=${encodeURIComponent(interval)}`);

  const figiSelect = document.getElementById("chartFigiSelect");
  const intervalSelect = document.getElementById("chartIntervalSelect");

  if (figiSelect && data.available_instruments) {
    figiSelect.innerHTML = data.available_instruments
      .map(x => `<option value="${x.figi}">${x.ticker} — ${x.name}</option>`)
      .join("");
    figiSelect.value = data.selected_figi || figi;
  }

  if (intervalSelect) {
    intervalSelect.value = data.interval || interval;
  }

  renderCandlesAndScore(data);
}

function toggleSummaryCardsVisibility() {
  const host = document.getElementById("summaryCards");
  if (!host) return;

  const isMain = getTabFromHash() === "главное";
  host.style.display = isMain ? "grid" : "none";
}

function renderCandlesAndScore(data) {
  const candles = data.candles || [];
  const signal = data.signal || { action: "HOLD", score: 0, reasons: ["Нет данных"] };

  const scoreBox = document.getElementById("signalScoreBox");
  if (scoreBox) {
    scoreBox.innerHTML = `
      <div class="score-pill">Action: ${signal.action}</div>
      <div class="score-pill">Score: ${signal.score}</div>
      <div class="score-reasons">
        ${(signal.reasons || []).map(x => `<div>• ${x}</div>`).join("")}
      </div>
    `;
  }

  if (!window.Plotly) return;

  if (!candles.length) {
    Plotly.newPlot("chartBox", [], {
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: "#eef4ff" },
      annotations: [{
        text: "Нет свечей для отображения",
        xref: "paper",
        yref: "paper",
        x: 0.5,
        y: 0.5,
        showarrow: false,
        font: { size: 16, color: "#cfd8f6" }
      }]
    }, { displayModeBar: false, responsive: true });
    return;
  }

  Plotly.newPlot(
    "chartBox",
    [{
      x: candles.map(c => c.time),
      open: candles.map(c => c.open),
      high: candles.map(c => c.high),
      low: candles.map(c => c.low),
      close: candles.map(c => c.close),
      type: "candlestick",
      increasing: { line: { color: "#2ecc71" } },
      decreasing: { line: { color: "#ff5c5c" } }
    }],
    {
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: "#eef4ff" },
      margin: { t: 10, r: 20, b: 40, l: 40 },
      xaxis: { rangeslider: { visible: false } },
    },
    { displayModeBar: false, responsive: true }
  );
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

async function serviceAction(action) {
  try {
    const r = await fetch(`/api/control/${action}`, {
      method: "POST",
      credentials: "same-origin",
    });
    const data = await r.json();
    showToast(data.ok ? "Команда выполнена" : "Команда вернула ошибку", data.ok ? "success" : "error");
    await renderSummaryCards();
    await applyRoute();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
  }
}

function openAddInstrumentModal() {
  document.getElementById("modalAddInstrument")?.classList.remove("hidden");
  const input = document.getElementById("instrumentSearchInput");
  if (input) input.value = "";
  loadTopVolumeInstruments();
}

function closeAddInstrumentModal() {
  document.getElementById("modalAddInstrument")?.classList.add("hidden");
}


function normalizeInstrumentForAdd(item) {
  const classCode = item.class_code || item.classcode || "";
  const instrumentType = item.instrument_type || item.instrumenttype || "share";

  return {
    ticker: item.ticker || "",
    figi: item.figi || "",
    name: item.name || "",
    classcode: classCode,
    instrumenttype: instrumentType,
    currency: item.currency || "",
    lot: Number(item.lot || 1),
    minpriceincrement: String(item.min_price_increment || item.minpriceincrement || "0.01"),
    last_price: item.last_price || item.price || "",
    price_time: item.price_time || item.updated_at || "",
    score: item.score ?? "",
    lotsoverride: 1,
    stoplosspct: "0.0025",
    takeprofitpct: "0.0050",
    maxspreadpct: "0",
    minvolume: 0,
    allowlong: 1,
    allowshort: 1,
    priority: 100,
    enabled: 1,
  };
}

async function searchInstruments() {
  try {
    const q = document.getElementById("instrumentSearchInput")?.value?.trim() || "";
    const instrumentKind = document.getElementById("instrumentKindSelect")?.value || "shares";

    if (!q) {
      showToast("Введи тикер или название", "error");
      return;
    }

    const items = await apiGet(`/api/instruments/search?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(instrumentKind)}`);
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
    showToast(`Ошибка загрузки top-20: ${e.message}`, "error");
  }
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
      <td style="width:72px;text-align:center;">
        <input
          type="checkbox"
          class="checkbox"
          data-instrument-pick="${idx}"
        >
      </td>
      <td><strong>${esc(item.ticker || "—")}</strong></td>
      <td style="min-width:260px;">${esc(item.name || "—")}</td>
      <td class="muted" style="font-size:12px;">${esc(item.figi || "—")}</td>
      <td><span class="pill">${esc(item.instrumenttype || item.instrument_type || "—")}</span></td>
      <td>${esc(item.currency || "—")}</td>
      <td>${esc(item.lot ?? "—")}</td>
      <td>${esc(item.minpriceincrement || item.min_price_increment || "—")}</td>
      <td>${esc(item.last_price || item.price || "—")}</td>
      <td>${esc(item.price_time || item.updated_at || "—")}</td>
      <td>${esc(item.score ?? "—")}</td>
    </tr>
  `).join("");
}

function selectAllInstrumentSearchRows() {
  document.querySelectorAll("[data-instrument-pick]").forEach((el) => {
    el.checked = true;
  });
}

function clearAllInstrumentSearchRows() {
  document.querySelectorAll("[data-instrument-pick]").forEach((el) => {
    el.checked = false;
  });
}

async function closeOnePosition(figi, qty, direction) {
  if (!confirm("Закрыть эту позицию?")) return;

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
  if (!confirm("Точно закрыть все позиции?")) return;

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

async function saveSystemSettings() {
  try {
    const fd = new FormData(document.getElementById("systemSettingsForm"));
    await apiPostForm("/api/settings/system", Object.fromEntries(fd.entries()));
    showToast("Системные настройки сохранены", "success");
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка сохранения: ${e.message}`, "error");
  }
}

async function saveStrategySettings() {
  try {
    const fd = new FormData(document.getElementById("strategySettingsForm"));
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
   await fetch('/api/профили/активировать', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_name: profileName })
      });
    showToast(`Профиль "${name}" активирован`, "success");
    await renderSettingsTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка активации профиля: ${e.message}`, "error");
  }
}

async function deleteProfile(name) {
  if (!confirm(`Удалить профиль "${name}" и все его настройки? Это действие необратимо.`)) return;
  try {
    const fd = new FormData();
    fd.append("profile_name", name);
    const r = await fetch("/api/профили/удалить", { method: "POST", body: fd });
    const d = await r.json();
    if (d.ok) {
      showToast(`Профиль "${name}" удалён`, "success");
      loadSettingsTab();
    } else {
      showToast(d.detail || "Ошибка удаления профиля", "error");
    }
  } catch (e) {
    showToast("Ошибка сети", "error");
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

async function deleteStrategy(name) {
  if (!confirm(`Удалить стратегию "${name}"? Это действие необратимо.`)) return;
  try {
    const fd = new FormData();
    fd.append("strategy_name", name);
    const r = await fetch("/api/стратегии/удалить", { method: "POST", body: fd });
    const d = await r.json();
    if (d.ok) {
      showToast(`Стратегия "${name}" удалена`, "success");
      loadSettingsTab();
    } else {
      showToast(d.detail || "Ошибка удаления стратегии", "error");
    }
  } catch (e) {
    showToast("Ошибка сети", "error");
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
    const fd = new FormData(event.target);
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
  if (!confirm("Удалить инструмент?")) return;

  try {
    await apiPostForm("/api/instruments/delete", { figi });
    showToast("Инструмент удалён", "success");
    await renderSettingsTab();
    await renderMainData();
  } catch (e) {
    showToast(`Ошибка удаления инструмента: ${e.message}`, "error");
  }
}

async function applyRoute() {
  const tab = getTabFromHash();
  try {
    console.log("[tabs] applyRoute ->", tab);
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

    console.log("[tabs] click ->", tab, "hash:", newHash);

    if (window.location.hash === newHash) {
      await applyRoute();
      return;
    }

    window.location.hash = newHash;
  });

  window.addEventListener("hashchange", () => {
    console.log("[tabs] hashchange ->", window.location.hash);
    applyRoute();
  });

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
    try {
      if (getTabFromHash() === "главное") {
        await renderSummaryCards();
      }
    } catch {}
  }, REFRESH_SUMMARY_MS);

  setInterval(async () => {
    try {
      await refreshQuotesOnly();
    } catch {}
  }, REFRESH_QUOTES_MS);

  setInterval(async () => {
    try {
      if (getTabFromHash() === "портфель") await renderPortfolioTab();
    } catch {}
  }, REFRESH_PORTFOLIO_MS);
}

async function bootstrapDashboard() {
  console.log("[tabs] DOMContentLoaded");
  bindRouter();
  await renderSummaryCards();
  await applyRoute();
  startRefreshLoops();

  window.searchInstruments = searchInstruments;
  window.loadTopVolumeInstruments = loadTopVolumeInstruments;
  window.acceptSelectedInstruments = acceptSelectedInstruments;
  window.openAddInstrumentModal = openAddInstrumentModal;
  window.closeAddInstrumentModal = closeAddInstrumentModal;
}

document.addEventListener("DOMContentLoaded", bootstrapDashboard);