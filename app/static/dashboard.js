const REFRESH_SUMMARY_MS   = 4000;   // summary cards (баланс, статус)
const REFRESH_QUOTES_MS    = 2000;   // цены в позициях главной
const REFRESH_PORTFOLIO_MS = 5000;   // вкладка портфель
const REFRESH_PARALLEL_MS  = 3000;   // тикеры с сигналами (САМОЕ ВАЖНОЕ)
const REFRESH_TRADES_MS    = 8000;   // история сделок (лёгкая)

let instrumentSearchData = [];
let refreshTimersStarted = false;
let _lastQuotesMap = {};   // последние котировки — используем после любого diffTbody

// Viewed profile in settings tab (may differ from active profile)
let viewedProfileId = null;

const ALLOWED_TABS = new Set(["главное", "портфель", "настройки", "история", "бэктест", "аналитик", "обучение"]);

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

// Обновляет tbody построчно, мигает изменившимися ячейками
function diffTbodyFlash(tbody, rows) {
  if (!tbody) return;
  rows.forEach(({ key, html, flashCols }) => {
    const existing = tbody.querySelector(`tr[data-key="${CSS.escape(key)}"]`);
    if (!existing) {
      // Новая строка — добавить в конец
      const tmp = document.createElement('tbody');
      tmp.innerHTML = html;
      const newTr = tmp.firstElementChild;
      if (newTr) tbody.appendChild(newTr);
      return;
    }
    const tmpDiv = document.createElement('tbody');
    tmpDiv.innerHTML = html;
    const newTr = tmpDiv.firstElementChild;
    if (!newTr) return;

    // Сравниваем ячейки
    const oldCells = existing.cells;
    const newCells = newTr.cells;
    let changed = false;
    for (let c = 0; c < Math.min(oldCells.length, newCells.length); c++) {
      const oldH = oldCells[c].innerHTML;
      const newH = newCells[c].innerHTML;
      if (oldH !== newH) {
        const oldNum = _numVal(oldCells[c].textContent);
        oldCells[c].innerHTML = newH;
        if (!flashCols || flashCols.includes(c)) {
          const newNum = _numVal(newCells[c].textContent);
          const dir = (oldNum !== null && newNum !== null)
            ? (newNum > oldNum ? 'up' : newNum < oldNum ? 'down' : 'neutral')
            : 'neutral';
          _flashCell(oldCells[c], dir);
          changed = true;
        }
      }
    }
    // Если изменился стиль строки
    if (existing.getAttribute('style') !== newTr.getAttribute('style')) {
      existing.setAttribute('style', newTr.getAttribute('style') || '');
    }
    _ = changed;
  });
  // Удаляем устаревшие строки
  const newKeys = new Set(rows.map(r => r.key));
  Array.from(tbody.querySelectorAll('tr[data-key]')).forEach(tr => {
    if (!newKeys.has(tr.dataset.key)) tr.remove();
  });
}

function _flashCell(cell, dir) {
  cell.classList.remove('cell-updated', 'cell-updated-up', 'cell-updated-down', 'cell-updated-neutral');
  void cell.offsetWidth;
  if (dir === 'up')          cell.classList.add('cell-updated-up');
  else if (dir === 'down')   cell.classList.add('cell-updated-down');
  else if (dir === 'neutral') cell.classList.add('cell-updated-neutral');
  else                       cell.classList.add('cell-updated-neutral');
}

function _numVal(text) {
  if (!text) return null;
  const s = String(text).replace(/[^\d.,\-+]/g, '').replace(',', '.');
  const v = parseFloat(s);
  return isNaN(v) ? null : v;
}

function ensureViewsExist() {
  const required = ["главное", "портфель", "настройки", "история", "бэктест", "аналитик", "обучение"];
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

function _crd(label, value) {
  const cls = summaryValueClass(value);
  return `<div class="crd"><div class="lbl">${label}</div><div class="val ${cls}" title="${esc(value ?? "—")}">${esc(value ?? "—")}</div></div>`;
}

async function renderSummaryCards() {
  const s = await apiGet("/api/dashboard/summary");
  const host = document.getElementById("summaryCards");
  if (!host) return;
  const wrapper = document.getElementById("mainSummaryRow");
  if (wrapper && wrapper.style.display === "none") wrapper.style.display = "block";
  const hasError = s.last_error && s.last_error !== "—";

  const newHtml = _buildSummaryHtml(s, hasError);
  const isFirst = !host.dataset.built;
  const savedNewsHtml = !isFirst ? (document.getElementById("newsWidgetInner")?.innerHTML ?? null) : null;
  // Запомним старые значения .val перед перезаписью
  const oldVals = isFirst ? [] : Array.from(host.querySelectorAll('.val')).map(e => e.textContent.trim());
  host.innerHTML = newHtml;
  host.dataset.built = "1";
  // Восстанавливаем новости (newsWidgetInner внутри summaryCards пересоздаётся при каждом рендере)
  if (savedNewsHtml !== null) {
    const nw = document.getElementById("newsWidgetInner");
    if (nw) { nw.innerHTML = savedNewsHtml; nw.dataset.newsInited = "1"; }
  } else {
    _initNewsWidget();
  }
  if (isFirst) {
    // Первый рендер: fade-in карточек
    host.querySelectorAll('.crd').forEach((c, i) => {
      c.style.animationDelay = (i * 0.03) + 's';
      c.classList.add('card-appear');
    });
  } else {
    // Повторный рендер: мигаем изменившимися значениями
    Array.from(host.querySelectorAll('.val')).forEach((el, i) => {
      const oldTxt = oldVals[i];
      const newTxt = el.textContent.trim();
      if (oldTxt === undefined || oldTxt === newTxt) return;
      const oldNum = _numVal(oldTxt), newNum = _numVal(newTxt);
      const dir = (oldNum !== null && newNum !== null)
        ? (newNum > oldNum ? 'up' : newNum < oldNum ? 'down' : 'neutral')
        : 'neutral';
      _flashCell(el, dir);
    });
  }
}

function _buildSummaryHtml(s, hasError) {
  return `<div class="sgroups" style="margin-bottom:0;height:100%">
    <div class="sgrp">
      <div class="sgrp-lbl">Сервис</div>
      <div class="sgrp-cards">
        ${_crd("Статус",    s.status)}
        ${_crd("Торговля",  s.trading_status)}
        ${_crd("Профиль",   s.active_profile_name || "—")}
        ${_crd("Стратегия", s.active_strategy_name || "—")}
      </div>
    </div>
    <div class="sgrp">
      <div class="sgrp-lbl">Счёт</div>
      <div class="sgrp-cards">
        ${_crd("Деньги",  s.cash_rub_ui)}
        ${_crd("Позиции", s.positions_value_rub_ui)}
        ${_crd("Резерв",  s.blocked_rub_ui)}
        ${_crd("Итого",   s.total_assets_rub_ui)}
      </div>
    </div>
    <div class="sgrp">
      <div class="sgrp-lbl">Сессия</div>
      <div class="sgrp-cards">
        ${_crd("Сделки",      s.trades_today)}
        ${_crd("PnL реал.",   s.daily_pnl_ui)}
        ${_crd("PnL нереал.", s.unrealized_pnl_ui || "0.00")}
        ${_crd("Комиссия",    s.total_commission_ui)}
      </div>
    </div>
    ${hasError ? `<div class="sgrp">
      <div class="sgrp-lbl" style="color:#ff7b7b;border-color:#bf4d5a">Ошибка</div>
      <div class="sgrp-cards" style="grid-template-columns:1fr">
        <div class="crd" style="background:rgba(191,77,90,.08)">
          <div class="val" style="font-size:12px;font-weight:400;color:#ff9999;white-space:normal">${esc(s.last_error)}</div>
        </div>
      </div>
    </div>` : ""}
    ${s.api_rpm != null ? (() => {
      const pct = s.api_rpm_pct || 0;
      const warn = s.api_warn;
      const color = pct >= 95 ? "#ff7b7b" : pct >= 80 ? "#f0a500" : "#2fa36b";
      const bg    = pct >= 95 ? "rgba(191,77,90,.08)" : pct >= 80 ? "rgba(240,165,0,.08)" : "rgba(47,163,107,.05)";
      const hints = {
        "GetCandles":           "Увеличить CHECK_INTERVAL_SEC в настройках",
        "GetOrderBook":         "Отключить фильтр стакана (use_order_book_filter) в стратегии",
        "GetLastPrices":        "Используется потоком — уже минимизировано",
        "GetTradingStatus":     "Кеш 30с — автоматически снижается",
        "GetPortfolio":         "Вызывается при открытии позиций — норма",
        "GetTechAnalysis/RSI":  "Отключить API-фильтр (use_api_confirm) в стратегии",
        "GetTechAnalysis/MACD": "Отключить API-фильтр (use_api_confirm) в стратегии",
        "GetTechAnalysis/BB":   "Отключить API-фильтр (use_api_confirm) в стратегии",
      };
      const breakdown = (s.api_rpm_breakdown || []);
      const breakdownHtml = breakdown.length ? `
        <div style="margin-top:8px;font-size:11px">
          <div style="opacity:.6;margin-bottom:4px">Расход по операциям (запр/мин):</div>
          ${breakdown.map(r => {
            const hint = hints[r.op] || "";
            const barPct = Math.min(r.rpm / (s.api_rpm_limit || 600) * 100, 100);
            const c = barPct >= 30 ? "#f0a500" : "#aaa";
            return `<div style="margin-bottom:5px">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
                <span style="color:#ddd;font-weight:500;min-width:200px">${esc(r.op)}</span>
                <span style="color:${c};font-weight:600;min-width:32px;text-align:right">${r.rpm}</span>
              </div>
              <div style="background:rgba(255,255,255,.07);border-radius:3px;height:3px;margin:2px 0">
                <div style="height:100%;width:${barPct.toFixed(1)}%;background:${c};border-radius:3px"></div>
              </div>
              ${hint ? `<div style="color:#888;font-size:10px;margin-top:1px">→ ${esc(hint)}</div>` : ""}
            </div>`;
          }).join("")}
        </div>` : "";
      return `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <div class="sgrp">
          <div class="sgrp-lbl" style="${warn ? 'color:#f0a500;border-color:#f0a500' : ''}">API</div>
          <div class="sgrp-cards" style="grid-template-columns:1fr">
            <div class="crd" style="background:${bg}">
              <div class="lbl">Запросов/мин <span style="opacity:.5;font-size:9px">(бот)</span></div>
              <div class="val" style="color:${color};font-size:15px">${s.api_rpm} <span style="font-size:11px;opacity:.7">/ ${s.api_rpm_limit}</span></div>
              <div style="background:rgba(255,255,255,.08);border-radius:4px;height:4px;margin-top:4px;overflow:hidden">
                <div style="height:100%;width:${Math.min(pct,100)}%;background:${color};border-radius:4px;transition:width .5s"></div>
              </div>
              ${breakdownHtml}
              ${warn ? `<div style="font-size:10px;color:#f0a500;margin-top:6px">⚠️ ${pct >= 95 ? 'Лимит исчерпан' : 'Нагрузка высокая'}</div>` : ''}
              <div style="margin-top:12px;border-top:1px solid rgba(76,141,255,.1);padding-top:10px">
                <div style="font-size:9px;font-weight:700;color:#7ab0e8;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">📺 РБК ТВ</div>
                <iframe
                  src="https://smotret.tv/rbk"
                  style="width:100%;height:220px;border:none;border-radius:6px;background:#000;display:block"
                  allowfullscreen
                  allow="autoplay; encrypted-media; fullscreen"
                  loading="lazy"
                ></iframe>
              </div>
            </div>
          </div>
        </div>
        <div id="newsWidgetInner" style="background:#0a1628;border:1px solid rgba(76,141,255,.12);border-radius:10px;overflow:hidden"></div>
      </div>`;
    })() : ""}
  </div>`;
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
  const wrapper = document.getElementById("mainSummaryRow");
  if (!wrapper) return;
  const show = getTabFromHash() === "главное";
  wrapper.style.display = show ? "block" : "none";
  if (show) _initNewsWidget();
}

function _initNewsWidget() {
  const host = document.getElementById("newsWidgetInner");
  if (!host || host.dataset.newsInited) return;
  host.dataset.newsInited = "1";
  _renderNews();
}

async function _renderNews() {
  const host = document.getElementById("newsWidgetInner");
  if (!host) return;
  try {
    const news = await apiGet("/api/news");
    if (!news || !news.length) {
      host.innerHTML = `<div style="padding:12px;color:#4a7aaa;font-size:12px">Нет новостей</div>`;
      return;
    }
    host.style.cssText = host.style.cssText; // keep existing
    host.innerHTML = `
      <div style="display:flex;flex-direction:column;height:100%;min-height:180px">
        <div style="padding:10px 12px 8px;border-bottom:1px solid rgba(76,141,255,.12);flex-shrink:0;display:flex;align-items:center;gap:8px">
          <span style="font-size:10px;font-weight:700;color:#7ab0e8;text-transform:uppercase;letter-spacing:.08em">Коммерсантъ</span>
          <span style="font-size:9px;color:#3a6080;background:rgba(76,141,255,.1);border:1px solid rgba(76,141,255,.2);border-radius:3px;padding:1px 5px">Экономика</span>
        </div>
        <div style="flex:1;overflow-y:auto;padding:8px 12px;scrollbar-width:thin;scrollbar-color:#1e3a5f #0a1628">
          ${news.map(n => `
            <div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid rgba(30,58,95,.5)">
              <a href="${esc(n.link)}" target="_blank" rel="noopener"
                 style="color:#c4dcff;font-size:11.5px;line-height:1.45;text-decoration:none;display:block">
                ${esc(n.title)}
              </a>
              <span style="color:#2e5a80;font-size:10px;margin-top:2px;display:block">${esc(n.date)}</span>
            </div>`).join("")}
        </div>
      </div>`;
  } catch(e) {
    host.innerHTML = `<div style="padding:12px;color:#4a7aaa;font-size:12px">Ошибка загрузки новостей</div>`;
  }
}

// ── Main tab ──────────────────────────────────────────────────────────────────

async function renderMainShell() {
  const host = document.getElementById("view-main");
  if (!host || host.dataset.initialized === "1") return;
  host.innerHTML = `
    <!-- ── Статус + Управление (компактный единый блок) ── -->
    <section class="block" style="padding:14px 18px">
      <div class="row between" style="flex-wrap:wrap;gap:10px;margin-bottom:10px">
        <div class="row" style="gap:6px;flex-wrap:wrap" id="statusPills">
          <span class="note">Загрузка…</span>
        </div>
        <div class="row" style="gap:6px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="serviceAction('start')">Запустить</button>
          <button class="btn" onclick="serviceAction('stop')">Остановить</button>
          <button class="btn" onclick="serviceAction('restart')">Перезапустить</button>
          <button class="btn" id="btnTelegramDiag">Telegram</button>
        </div>
      </div>
      <div id="runtimeGrid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:6px"></div>
      <div id="botExplainInline" style="margin-top:8px"></div>
      <div id="telegramDiagBox"></div>
    </section>

    <div id="balanceWarningsBox"></div>

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
          <input id="sandboxResetAmount" class="field" type="number" placeholder="Сумма сброса ₽" style="width:150px" value="59518">
          <button class="btn" style="background:#c0392b;color:#fff" onclick="sandboxResetBalance()" title="Закрывает текущий счёт и создаёт новый с указанным балансом">Сбросить баланс</button>
        </div>
      </div>
    </section>

    <!-- ── Параллельные стратегии ── -->
    <section class="block" id="parallelStatusBlock" style="display:none">
      <div class="row between" style="margin-bottom:12px">
        <h2 style="margin:0">Параллельные стратегии</h2>
        <span class="note">Одна позиция на все потоки</span>
      </div>
      <div id="parallelStatusBody"></div>
    </section>

    <!-- ── Графики инструментов (настройки + сетка) ── -->
    <section class="block" style="padding:12px 16px;margin-bottom:6px">
      <div class="row between">
        <h2 style="margin:0">Графики инструментов</h2>
        <div class="row" style="gap:6px;flex-wrap:wrap">
          <select class="field" id="mcInterval" style="width:auto">
            <option value="1min">1 мин</option>
            <option value="5min">5 мин</option>
            <option value="15min">15 мин</option>
            <option value="hour">1 час</option>
          </select>
          <select class="field" id="mcHours" style="width:auto">
            <option value="1">1 ч</option>
            <option value="4" selected>4 ч</option>
            <option value="8">8 ч</option>
          </select>
          <button class="btn" onclick="mainChartsApplySettings()">Обновить</button>
        </div>
      </div>
    </section>
    <div id="mainChartsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:6px;margin-bottom:18px"></div>

    <!-- ── Позиции ── -->
    <section class="block">
      <div class="row between"><h2>Позиции <span class="note">(API брокера)</span></h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>Направление</th><th>Лотов</th><th>Вход</th><th>Тек. цена</th><th>Изм.%</th><th>ПнЛ</th><th>Сумма</th><th>SL</th><th>TP</th><th>Действие</th></tr></thead>
          <tbody id="mainPositionsBody"></tbody>
        </table>
      </div>
    </section>

    <!-- ── Детали позиций: графики + новости ── -->
    <div id="positionDetailsBlock"></div>

    <!-- ── Сделки ── -->
    <section class="block">
      <div class="row between"><h2>Сделки</h2><div class="note">Сегодня</div></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Открыто</th><th>Закрыто</th><th>Длит.</th><th>Тикер</th><th>Напр.</th><th>Вход</th><th>Выход</th><th>Кол-во</th><th>ПнЛ</th><th>%</th><th>Причина</th></tr></thead>
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
  // Графики обновляются через refreshParallelStatus (figis берутся оттуда)

  // ── Позиции ──────────────────────────────────────────────────────────────
  diffTbody(document.getElementById("mainPositionsBody"),
    (data.positions || []).map((p) => {
      const dir = String(p.direction || "").toUpperCase();
      const dirBadge = dir === "BUY"
        ? '<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b;border:1px solid #2fa36b">Лонг</span>'
        : dir === "SELL"
          ? '<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b;border:1px solid #bf4d5a">Шорт</span>'
          : esc(dir);
      const pnlVal = parseFloat(String(p.unrealized_pnl_ui).replace(/[^0-9.,\-]/g, "").replace(",", ".")) || 0;
      const pnlColor = pnlVal >= 0 ? "#2fa36b" : "#ff7b7b";
      const pct = p.pct_change || "";
      const pctColor = pct.startsWith("+") ? "#2fa36b" : pct.startsWith("-") ? "#ff7b7b" : "#9fb3d8";
      return `<tr data-figi="${esc(p.figi)}" data-qty="${p.qty_raw||0}" data-avg="${p.avg_price_raw||0}" data-dir="${esc(p.direction)}">
        <td><b>${esc(p.ticker)}</b></td>
        <td>${dirBadge}</td>
        <td>${esc(p.qty)}</td>
        <td class="muted">${esc(p.entry_price_ui)}</td>
        <td class="live-pos-price"><b>—</b></td>
        <td class="live-pos-pct" style="color:${pctColor};font-weight:600">${esc(pct)}</td>
        <td class="live-pos-pnl" style="font-weight:700;color:${pnlColor}">${esc(p.unrealized_pnl_ui)}</td>
        <td class="live-pos-value" style="color:#9fb3d8;font-size:12px">${esc(p.position_value_ui||"—")}</td>
        <td style="font-size:12px;white-space:nowrap;color:#ff7b7b">
          ${esc(p.sl_price_ui||"—")}<br>
          <span class="muted">${esc(p.sl_pct_ui||"—")}</span>
        </td>
        <td style="font-size:12px;white-space:nowrap;color:#2fa36b">
          ${esc(p.tp_price_ui||"—")}<br>
          <span class="muted">${esc(p.tp_pct_ui||"—")}</span>
        </td>
        <td>${p.figi && p.qty && p.direction ? `
          <button class="btn btn-danger" style="padding:5px 10px"
            onclick="closeOnePosition('${esc(p.figi)}','${esc(p.qty)}','${esc(p.direction)}')">
            Закрыть
          </button>` : "—"}</td>
      </tr>`;
    }).join("")
  );

  // ── Сделки — цветные строки как в Истории ────────────────────────────────
  const displayTrades = (data.api_trades && data.api_trades.length > 0)
    ? data.api_trades : (data.trades || []);
  diffTbody(document.getElementById("mainTradesBody"),
    displayTrades.map((t) => {
      const raw = String(t.pnl_ui || "").replace(/[^0-9.,\-]/g, "").replace(",", ".");
      const pnl = parseFloat(raw) || 0;
      const bg  = pnl > 0 ? "rgba(47,163,107,.07)" : pnl < 0 ? "rgba(191,77,90,.07)" : "";
      const col = pnl >= 0 ? "#2fa36b" : "#ff7b7b";
      const dir = String(t.direction || "").toUpperCase();
      const badge = dir === "BUY"
        ? '<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b">BUY</span>'
        : dir === "SELL"
          ? '<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b">SELL</span>'
          : esc(t.direction);
      return `<tr style="background:${bg}">
        <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.open_time || "—")}</td>
        <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.time || "—")}</td>
        <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.duration_ui || "—")}</td>
        <td><b>${esc(t.ticker)}</b></td>
        <td>${badge}</td>
        <td>${esc(t.entry_ui)}</td><td>${esc(t.exit_ui)}</td>
        <td>${esc(t.qty)}</td>
        <td style="font-weight:700;color:${col}">${pnl >= 0 && pnl !== 0 ? "+" : ""}${esc(t.pnl_ui)}</td>
        <td style="font-size:12px;color:${col};font-weight:600">${t.pnl_pct != null ? (t.pnl_pct >= 0 ? "+" : "") + t.pnl_pct.toFixed(2) + "%" : ""}</td>
        <td class="muted" style="font-size:12px">${esc(t.reason)}</td>
      </tr>`;
    }).join("")
  );

  refreshParallelStatus();

  // ── Детали позиций: графики + новости ────────────────────────────────────
  renderPositionDetails(data.positions || []);

  // ── Balance check + sandbox ───────────────────────────────────────────────
  try {
    const bc = await apiGet("/api/dashboard/balance-check");
    const sandboxBlock = document.getElementById("sandboxBlock");
    if (sandboxBlock) sandboxBlock.style.display = bc.is_sandbox ? "block" : "none";
    const warnBox = document.getElementById("balanceWarningsBox");
    if (warnBox) {
      const blocked = (bc.checks || []).filter(c => !c.can_trade && c.has_price);
      warnBox.innerHTML = blocked.map(c => `
        <div class="banner-warning" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div>
            <strong>${esc(c.ticker)}</strong> — недостаточно средств.
            Нужно: <strong>${esc(c.required_ui)} ₽</strong>
            (${esc(c.lots)}${c.auto_lots ? " авто" : ""} лот × ${esc(c.lot_size)} шт × ${esc(c.price_ui)} ₽ + комиссия).
            Свободно: <strong>${esc(bc.cash_ui)} ₽</strong>. SL ${esc(c.sl_pct)}% / TP ${esc(c.tp_pct)}%.
          </div>
          ${bc.is_sandbox ? `<button class="btn btn-primary" onclick="sandboxPayIn()">Пополнить Sandbox</button>` : ""}
        </div>`).join("");
    }
  } catch {}

  // ── Runtime — компактный ─────────────────────────────────────────────────
  try {
    const rt = await apiGet("/api/dashboard/runtime");
    const isSandbox = String(rt.tinvestusesandbox || "true") === "true";
    const status = rt.status || "INIT";
    const isOk   = ["SCANNING","RUNNING","ВЕДЁТСЯ"].includes(status.toUpperCase());
    const pills = document.getElementById("statusPills");
    if (pills) {
      pills.innerHTML = [
        {label: status,                            ok: isOk,   warn: false},
        {label: isSandbox ? "Sandbox" : "Боевой", ok: !isSandbox, warn: isSandbox},
      ].map(p => {
        const bg  = p.ok ? "rgba(47,163,107,.2)" : p.warn ? "rgba(255,185,50,.2)" : "rgba(100,100,120,.2)";
        const col = p.ok ? "#2fa36b" : p.warn ? "#f0c04a" : "#9fb3d8";
        return `<span class="badge" style="background:${bg};color:${col};border:1px solid ${col};padding:5px 10px;font-size:12px">${esc(p.label)}</span>`;
      }).join("");
    }
    const grid = document.getElementById("runtimeGrid");
    if (grid) {
      const cells = [
        {k:"Профиль",         v: rt.activeprofilename  || "—"},
        {k:"Стратегия",       v: rt.activestrategyname || "—"},
        {k:"Последняя ошибка",v: rt.lasterror          || "—"},
      ];
      grid.innerHTML = cells.map(c => `
        <div style="background:rgba(255,255,255,.03);border-radius:8px;padding:8px 12px">
          <div class="note" style="font-size:11px;margin-bottom:2px">${c.k}</div>
          <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
               title="${esc(c.v)}">${esc(c.v)}</div>
        </div>`).join("");
    }
  } catch (e) {
    const pills = document.getElementById("statusPills");
    if (pills) pills.innerHTML = `<span class="note">Ошибка: ${esc(e.message)}</span>`;
  }

  // ── Диагностика (почему не торгует) ──────────────────────────────────────
  try {
    const explain = await apiGet("/api/dashboard/bot-explain");
    const box = document.getElementById("botExplainInline");
    if (box) {
      const reasons = explain.reasons || [];
      box.innerHTML = reasons.length
        ? `<details style="margin-top:4px"><summary class="note" style="cursor:pointer">
            ⚠ Диагностика: ${reasons.length} причин${reasons.length > 1 ? "ы" : "а"}</summary>
            <ul style="margin:6px 0 0 16px;padding:0">${reasons.map(x => `<li class="note">${esc(x)}</li>`).join("")}</ul>
           </details>`
        : `<span class="note" style="font-size:12px">✓ Все условия торговли выполнены</span>`;
    }
  } catch {}
}


function _applyQuotesToLiveCells(map) {
  // Обновляет live-price/live-time элементы из котировок; вызывается из обоих таймеров
  document.querySelectorAll(".live-price[data-figi]").forEach((el) => {
    const q = map[el.dataset.figi];
    if (!q || !q.last_price_ui) return;
    const prev = el.textContent.trim();
    const next = q.last_price_ui;
    if (prev === next) return;
    const prevNum = _numVal(prev);
    const nextNum = _numVal(next);
    el.textContent = next;
    if (prevNum !== null && nextNum !== null && prev !== "—") {
      _flashCell(el, nextNum > prevNum ? 'up' : nextNum < prevNum ? 'down' : 'neutral');
    }
  });
  document.querySelectorAll(".live-time[data-figi]").forEach((el) => {
    const q = map[el.dataset.figi];
    if (q) el.textContent = q.price_time || "—";
  });
}

async function refreshQuotesOnly() {
  if (getTabFromHash() !== "главное") return;
  const quotes = await apiGet("/api/dashboard/quotes");
  const map = {};
  for (const q of quotes) map[q.figi] = q;
  _lastQuotesMap = map;   // сохраняем для использования в refreshParallelStatus
  _applyQuotesToLiveCells(map);

  // ── Live positions: price / pct / pnl / value ─────────────────────────────
  document.querySelectorAll("#mainPositionsBody tr[data-figi]").forEach(row => {
    const q = map[row.dataset.figi];
    if (!q) return;
    const price = parseFloat(q.last_price_ui) || 0;
    if (!price) return;
    const qty = parseFloat(row.dataset.qty) || 0;
    const avg  = parseFloat(row.dataset.avg) || 0;
    const dir  = row.dataset.dir || "BUY";
    if (!qty || !avg) return;

    const pnl   = dir === "BUY" ? (price - avg) * qty : (avg - price) * qty;
    const pct   = (price - avg) / avg * 100;
    const value = price * qty;

    // Обновляем ячейку только если текст изменился; направление флеша по числовому значению
    const _upd = (sel, newText, color) => {
      const el = row.querySelector(sel);
      if (!el) return;
      const oldText = el.textContent.trim();
      if (color) el.style.color = color;   // цвет обновляем без флеша
      if (oldText === newText) return;      // текст не изменился — нет флеша
      const oldNum = _numVal(oldText);
      const newNum = _numVal(newText);
      el.textContent = newText;
      const dir = (oldNum !== null && newNum !== null)
        ? (newNum > oldNum ? 'up' : newNum < oldNum ? 'down' : 'neutral')
        : 'neutral';
      _flashCell(el, dir);
    };

    const priceUi = price === 0 ? "—" : price.toFixed(2);
    const pctUi   = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
    const pnlUi   = pnl.toFixed(2);
    const valUi   = _fmtSum(value);

    _upd(".live-pos-price", priceUi, null);
    _upd(".live-pos-pct",   pctUi,   pct >= 0 ? "#2fa36b" : "#ff7b7b");
    _upd(".live-pos-pnl",   pnlUi,   pnl >= 0 ? "#2fa36b" : "#ff7b7b");
    _upd(".live-pos-value", valUi,   null);
  });
}

// ── Position detail panels (chart + news per ticker) ─────────────────────────

let _posDetailFigis  = [];
let _posChartInterval = "5min";
let _posChartHours    = 4;

async function renderPositionDetails(positions) {
  const block = document.getElementById("positionDetailsBlock");
  if (!block) return;
  if (!positions || !positions.length) { block.innerHTML = ""; _posDetailFigis = []; return; }

  const figis = positions.map(p => p.figi).filter(Boolean);
  const key = figis.join(",");

  if (key !== _posDetailFigis.join(",")) {
    _posDetailFigis = figis;
    block.innerHTML = `
      <div class="row" style="gap:8px;margin:8px 0 4px;flex-wrap:wrap">
        <span class="muted" style="font-size:12px">Позиции — настройки графика:</span>
        <select id="posChartInterval" class="field" style="width:90px;padding:3px 6px;font-size:12px"
                onchange="_posChartInterval=this.value;_refreshPosCharts()">
          <option value="1min">1 мин</option>
          <option value="5min" selected>5 мин</option>
          <option value="15min">15 мин</option>
          <option value="1hour">1 час</option>
        </select>
        <select id="posChartHours" class="field" style="width:80px;padding:3px 6px;font-size:12px"
                onchange="_posChartHours=parseInt(this.value);_refreshPosCharts()">
          <option value="1">1 ч</option>
          <option value="2">2 ч</option>
          <option value="4" selected>4 ч</option>
          <option value="8">8 ч</option>
          <option value="24">24 ч</option>
        </select>
      </div>
      ${positions.map(p => `
      <section class="block" id="pos-detail-${esc(p.figi)}" style="margin-top:6px;padding:12px 16px">
        <div class="row" style="gap:8px;margin-bottom:8px">
          <h2 style="margin:0">${esc(p.ticker)}</h2>
          <span class="badge" style="background:${p.direction==='BUY'?'rgba(47,163,107,.2)':'rgba(191,77,90,.2)'};color:${p.direction==='BUY'?'#2fa36b':'#ff7b7b'}">
            ${p.direction==='BUY'?'Лонг':'Шорт'}
          </span>
          <span class="muted" style="font-size:12px">Вход: ${esc(p.entry_price_ui)}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 360px;gap:14px;align-items:start">
          <div id="pos-chart-${esc(p.figi)}" style="height:220px;width:100%;min-width:0"></div>
          <div id="pos-news-${esc(p.figi)}" style="max-height:220px;overflow-y:auto">
            <div class="muted" style="text-align:center;padding:20px;font-size:12px">Загрузка новостей…</div>
          </div>
        </div>
      </section>`).join("")}`;
  }

  await _refreshPosCharts();
}

async function _refreshPosCharts() {
  if (!_posDetailFigis.length) return;
  const figis = _posDetailFigis;
  const figiList = figis.join(",");
  try {
    const candlesData = await apiGet(
      `/api/dashboard/multi-candles?figis=${figiList}&interval=${_posChartInterval}&hours=${_posChartHours}`
    );
    for (const figi of figis) {
      _renderPosChart(figi, candlesData[figi]?.candles || []);
    }
  } catch(e) {}
  // Новости загружаем только если раньше не загружали (один раз при появлении)
  for (const figi of figis) {
    const el = document.getElementById(`pos-news-${figi}`);
    if (el && el.querySelector(".muted")) _loadPosNews(figi);
  }
}

function _renderPosChart(figi, candles) {
  const el = document.getElementById(`pos-chart-${figi}`);
  if (!el) return;
  if (!candles || !candles.length) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px;font-size:12px">Нет данных свечей</div>';
    return;
  }
  const times  = candles.map(c => c.time);
  const opens  = candles.map(c => parseFloat(c.open));
  const highs  = candles.map(c => parseFloat(c.high));
  const lows   = candles.map(c => parseFloat(c.low));
  const closes = candles.map(c => parseFloat(c.close));
  const trace = {
    type: "candlestick", x: times,
    open: opens, high: highs, low: lows, close: closes,
    increasing: { line: { color: "#2fa36b", width: 1 }, fillcolor: "rgba(47,163,107,.8)" },
    decreasing: { line: { color: "#bf4d5a", width: 1 }, fillcolor: "rgba(191,77,90,.8)" },
    showlegend: false,
    xhoverformat: "%d.%m %H:%M",
  };
  const layout = {
    paper_bgcolor: "transparent", plot_bgcolor: "transparent",
    margin: { l: 50, r: 8, t: 6, b: 28 },
    xaxis: {
      showgrid: false, color: "#666", type: "date",
      tickfont: { size: 10, color: "#9fb3d8" },
      rangeslider: { visible: false },
    },
    yaxis: {
      showgrid: true, gridcolor: "rgba(255,255,255,.07)",
      color: "#9fb3d8", tickfont: { size: 10 },
      side: "right",
    },
    dragmode: "pan",
    hovermode: "x unified",
  };
  if (window.Plotly) {
    Plotly.react(el, [trace], layout, {
      displayModeBar: true,
      modeBarButtonsToRemove: ["select2d","lasso2d","autoScale2d","toImage"],
      responsive: true,
    });
  }
}

// ── Signal score popup ────────────────────────────────────────────────────────

let _sigPopup = null;

function _renderApiFilterExplain(skip, filter, sigAction) {
  if (!skip) return "";
  // Парсим значения из skip_reason: "API-фильтр: RSI=35.0 | MACD=-0.47/Сигн=-0.01 | BB=1087..1095"
  const rsiMatch  = skip.match(/RSI=([\d.]+)/);
  const macdMatch = skip.match(/MACD=([-\d.]+)\/Сигн=([-\d.]+)/);
  const bbMatch   = skip.match(/BB=([\d.]+)\.\.([\d.]+)/);
  const rsi    = rsiMatch  ? parseFloat(rsiMatch[1])  : null;
  const macd   = macdMatch ? parseFloat(macdMatch[1]) : null;
  const macdSg = macdMatch ? parseFloat(macdMatch[2]) : null;
  const bbLow  = bbMatch   ? parseFloat(bbMatch[1])   : null;
  const bbHigh = bbMatch   ? parseFloat(bbMatch[2])   : null;

  const rows = [];

  // RSI
  if (rsi !== null) {
    let rsiZone = rsi > 70 ? "🔴 перекупленность (>70)" : rsi < 30 ? "🔴 перепроданность (<30)" : rsi > 50 ? "🟡 бычья зона (50–70)" : "🟡 медвежья зона (30–50)";
    let rsiWhy  = "";
    if (sigAction === "BUY"  && rsi > 70) rsiWhy = "→ BUY заблокирован: при RSI>70 актив перекуплен, риск разворота";
    if (sigAction === "SELL" && rsi < 30) rsiWhy = "→ SELL заблокирован: при RSI<30 актив перепродан, риск отскока";
    rows.push(`<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,.05)">
      <b style="color:#eef4ff">RSI = ${rsi}</b> — ${rsiZone}
      ${rsiWhy ? `<div style="color:#f0a500;font-size:10px;margin-top:2px">${rsiWhy}</div>` : ""}
    </div>`);
  }

  // MACD
  if (macd !== null && macdSg !== null) {
    const cross = macd > macdSg ? "🔴 бычье пересечение (MACD>Сигн)" : "🔴 медвежье пересечение (MACD<Сигн)";
    let macdWhy = "";
    if (sigAction === "BUY"  && macd < macdSg) macdWhy = "→ BUY заблокирован: нет бычьего импульса (MACD ниже сигнальной)";
    if (sigAction === "SELL" && macd > macdSg) macdWhy = "→ SELL заблокирован: нет медвежьего импульса (MACD выше сигнальной)";
    rows.push(`<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,.05)">
      <b style="color:#eef4ff">MACD = ${macd.toFixed(4)}</b>, Сигнал = ${macdSg.toFixed(4)} — ${cross}
      ${macdWhy ? `<div style="color:#f0a500;font-size:10px;margin-top:2px">${macdWhy}</div>` : ""}
    </div>`);
  }

  // BB
  if (bbLow !== null && bbHigh !== null) {
    rows.push(`<div style="padding:4px 0">
      <b style="color:#eef4ff">Bollinger Bands</b>: нижняя = ${bbLow}, верхняя = ${bbHigh}
      <div style="color:#9fb3d8;font-size:10px;margin-top:2px">Для возврата к средней нужно, чтобы цена была у полосы (BUY у нижней, SELL у верхней)</div>
    </div>`);
  }

  if (!rows.length) {
    // Нет распознанных значений — показываем raw текст
    return `<div style="margin-top:8px;padding:8px;background:rgba(240,165,0,.08);border-radius:4px;font-size:11px;color:#f0a500">⚠ ${esc(skip)}</div>`;
  }

  return `<div style="margin-top:10px">
    <div style="color:#9fb3d8;font-size:11px;margin-bottom:4px;font-weight:600">API ФИЛЬТР (T-Bank индикаторы)</div>
    <div style="font-size:12px;color:#cdd9f0">${rows.join("")}</div>
  </div>`;
}

function _showSignalPopup(sigEl, tr) {
  if (_sigPopup) { _sigPopup.remove(); _sigPopup = null; }
  const action  = tr.dataset.sigAction  || "—";
  const score   = parseInt(tr.dataset.sigScore || "0") || 0;
  const mode    = tr.dataset.sigMode    || "";
  const skip    = tr.dataset.sigSkip    || "";
  const filter  = tr.dataset.sigFilter  || "";
  const reasons = (tr.dataset.sigReasons || "").split("||").filter(Boolean);
  const ticker  = tr.querySelector("td b")?.textContent || "";
  const sigColor  = action === "BUY" ? "#2fa36b" : action === "SELL" ? "#ff7b7b" : "#9fb3d8";
  const modeLabel = { trend: "Тренд (SMA9/SMA21)", mean_reversion: "Возврат к средней (Z-score)", breakout: "Пробой (объём+диапазон)" }[mode] || mode;
  const scoreHelp = { trend: "Score = среднее((разрыв SMA)×1000, |моментум|×15), макс 100. Чем больше расхождение SMA и моментум — тем выше.", mean_reversion: "Score = min(|Z-score|×30, 100). Z — отклонение цены от 20-периодной средней в сигмах. Сигнал при |Z|≥1.8.", breakout: "Score = среднее((отрыв от диапазона)×100, коэф.объёма×30), макс 100. Нужен пробой 20-свечного диапазона с объёмом ≥1.2×средний." }[mode] || "Score 0..100";
  const popup = document.createElement("div");
  popup.id = "_sigPopup";
  popup.style.cssText = "position:fixed;z-index:9999;background:#1a2235;border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:14px 16px;min-width:290px;max-width:400px;box-shadow:0 8px 32px rgba(0,0,0,.5);font-size:13px;line-height:1.5";
  popup.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="display:flex;gap:8px;align-items:center">
        <b style="font-size:14px">${esc(ticker)}</b>
        <span style="color:${sigColor};font-weight:700">${esc(action)}</span>
        <span style="background:rgba(76,141,255,.2);color:#4c8dff;border-radius:3px;padding:1px 6px;font-size:12px">${score} pts</span>
      </div>
      <button onclick="document.getElementById('_sigPopup')?.remove()" style="background:none;border:none;color:#555;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
    <div style="color:#9fb3d8;font-size:11px;margin-bottom:10px">Режим: <b>${esc(modeLabel)}</b></div>
    ${reasons.length ? `<div style="margin-bottom:10px">${reasons.map(r=>`<div style="font-size:12px;padding:2px 0;border-bottom:1px solid rgba(255,255,255,.05);color:#cdd9f0">${esc(r)}</div>`).join("")}</div>` : ""}
    <div style="background:rgba(255,255,255,.04);border-radius:4px;padding:8px;font-size:11px;color:#9fb3d8"><b style="color:#eef4ff">Как считается:</b><br>${esc(scoreHelp)}</div>
    ${skip ? _renderApiFilterExplain(skip, filter, action) : ""}`;
  document.body.appendChild(popup);
  _sigPopup = popup;
  const rect = sigEl.getBoundingClientRect();
  let left = Math.min(rect.left, window.innerWidth - 410);
  let top  = rect.bottom + 6;
  if (top + 280 > window.innerHeight) top = rect.top - 290;
  popup.style.left = Math.max(4, left) + "px";
  popup.style.top  = Math.max(4, top)  + "px";

  // Один глобальный обработчик закрытия — переиспользуем чтобы не накапливались
  if (window._sigCloseHandler) document.removeEventListener("click", window._sigCloseHandler);
  window._sigCloseHandler = (e) => {
    if (_sigPopup && !_sigPopup.contains(e.target) && !e.target.closest(".live-sig")) {
      _sigPopup.remove(); _sigPopup = null;
      document.removeEventListener("click", window._sigCloseHandler);
      window._sigCloseHandler = null;
    }
  };
  setTimeout(() => document.addEventListener("click", window._sigCloseHandler), 50);
}

async function _loadPosNews(figi) {
  const el = document.getElementById(`pos-news-${figi}`);
  if (!el) return;
  try {
    const news = await apiGet(`/api/news/ticker?figi=${figi}&hours=4`);
    if (!news || !news.length) {
      el.innerHTML = '<div class="muted" style="text-align:center;padding:20px;font-size:12px">Новостей за 4 часа не найдено</div>';
      return;
    }
    el.innerHTML = news.map(n => `
      <div style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)">
        <a href="${esc(n.link)}" target="_blank" rel="noopener"
           style="color:#9fb3d8;text-decoration:none;font-size:12px;line-height:1.4;display:block"
           onmouseover="this.style.color='#cdd9f0'" onmouseout="this.style.color='#9fb3d8'">
          ${esc(n.title)}
        </a>
        <div style="font-size:10px;color:#555;margin-top:2px">${esc(n.date)}${n.source ? ' · ' + esc(n.source) : ''}</div>
      </div>`).join("");
  } catch(e) {
    el.innerHTML = '<div class="muted" style="font-size:11px;padding:8px">Ошибка загрузки новостей</div>';
  }
}

function _fmtSum(v) {
  if (!v && v !== 0) return "—";
  const n = Math.round(parseFloat(v));
  return n.toLocaleString("ru-RU") + " ₽";
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
        <label>Только сессия MOEX
          <select class="field" name="trade_only_session">
            <option value="1" ${profSettings.trade_only_session === "1" ? "selected" : ""}>Да — только в торговую сессию</option>
            <option value="0" ${profSettings.trade_only_session !== "1" ? "selected" : ""}>Нет — торговать всегда</option>
          </select>
        </label>
        <div class="row-buttons">
          <button type="button" class="btn btn-primary" id="btnSaveProfileSettings">Сохранить</button>
        </div>
      </form>

      <!-- CREDENTIALS BLOCK -->
      <div id="credentialsBlock" style="border-top:1px solid rgba(255,255,255,.08);margin-top:12px;padding-top:12px">
        <div class="row between" style="margin-bottom:8px">
          <div class="row" style="gap:8px">
            <b>Токен API</b>
            <span id="credModeLabel" class="badge" style="background:rgba(76,141,255,.2);color:#4c8dff"></span>
          </div>
          <button class="btn" style="padding:4px 10px;font-size:12px" onclick="toggleCredEdit()">Изменить</button>
        </div>
        <div id="credDisplay" style="font-size:13px;display:grid;grid-template-columns:120px 1fr;gap:6px 12px;align-items:center">
          <span class="muted">Токен:</span><span id="credTokenMasked" style="font-family:monospace"></span>
          <span class="muted">Account ID:</span><span id="credAccountId"></span>
        </div>
        <div id="credEdit" style="display:none;margin-top:10px">
          <div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">
            <label>Новый токен
              <input class="field" id="credTokenInput" type="password" placeholder="Введите токен T-Invest API" autocomplete="new-password">
            </label>
            <label>Account ID
              <input class="field" id="credAccountInput" type="text" placeholder="Идентификатор счёта">
            </label>
          </div>
          <div class="row" style="gap:8px;margin-top:8px">
            <button class="btn btn-primary" style="font-size:13px" onclick="saveCredentials()">Сохранить</button>
            <button class="btn" style="font-size:13px" onclick="toggleCredEdit()">Отмена</button>
            <span class="note" style="font-size:11px">Токен хранится на сервере в .env (не в git)</span>
          </div>
        </div>
      </div>

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
                  <button class="btn btn-small" onclick="expandParallelStrategy(${ps.strategy_id},'settings')">Настройки</button>
                  <button class="btn btn-small" style="margin-left:4px" onclick="expandParallelStrategy(${ps.strategy_id},'instruments')">Инструменты</button>
                  <button class="btn btn-small btn-danger" style="margin-left:4px"
                    onclick="removeParallelStrategy(${esc(prof.id)}, ${ps.strategy_id})">Убрать</button>
                  <button class="btn btn-small btn-danger" style="margin-left:4px;opacity:.7"
                    onclick="deleteStrategyFull(${ps.strategy_id}, '${esc(ps.name)}')">Удалить</button>
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
        <label>Подтверждение RSI/MACD/BB (API)
          <select class="field" name="use_api_confirm">
            <option value="1" ${stratSettings.use_api_confirm === "1" ? "selected" : ""}>Вкл — RSI&lt;70/&gt;30 + MACD + BB</option>
            <option value="0" ${stratSettings.use_api_confirm !== "1" ? "selected" : ""}>Выкл</option>
          </select>
        </label>
        <label>Фильтр стакана (давление 40%)
          <select class="field" name="use_order_book_filter">
            <option value="1" ${(stratSettings.use_order_book_filter ?? "1") !== "0" ? "selected" : ""}>Вкл — проверять давление покупателей/продавцов</option>
            <option value="0" ${stratSettings.use_order_book_filter === "0" ? "selected" : ""}>Выкл — пропустить фильтр (экономит ~156 req/min)</option>
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

  // Load credentials
  loadCredentials();

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
      <label>Лоты
        <div style="display:flex;align-items:center;gap:6px;margin-top:4px">
          <input class="field" name="lots_override" type="number" min="1"
                 value="${esc(i.lots_override || 1)}"
                 style="width:72px;opacity:${i.auto_lots ? '.45' : '1'}">
          <input type="hidden" name="auto_lots" class="auto-lots-hidden" value="${i.auto_lots ? '1' : '0'}">
          <label style="font-weight:normal;white-space:nowrap;cursor:pointer;display:flex;align-items:center;gap:4px">
            <input type="checkbox" ${i.auto_lots ? 'checked' : ''}
                   onchange="(function(cb){
                     const f=cb.closest('.instrument-form');
                     f.querySelector('.auto-lots-hidden').value=cb.checked?'1':'0';
                     const inp=f.querySelector('[name=lots_override]');
                     inp.style.opacity=cb.checked?'.45':'1';
                   })(this)">
            Авто
          </label>
        </div>
      </label>
      <label>SL %<input class="field" name="stop_loss_pct" value="${esc(i.stop_loss_pct_ui)}"></label>
      <label>TP %<input class="field" name="take_profit_pct" value="${esc(i.take_profit_pct_ui)}"></label>
      <label>Спред %<input class="field" name="max_spread_pct" value="${esc(i.max_spread_pct_ui)}"></label>
      <label>Лимит убытка/день ₽<input class="field" name="max_daily_loss_rub" type="number" min="0" step="50" value="${esc(i.max_daily_loss_rub || 0)}" title="0 = без лимита"></label>
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

async function deleteStrategyFull(strategyId, name) {
  if (!confirm(`Удалить стратегию "${name}" полностью?\n\nЭто удалит стратегию, её настройки и инструменты. Действие необратимо.`)) return;
  try {
    await apiPostForm(`/api/strategies/${strategyId}/delete`, {});
    showToast(`Стратегия "${name}" удалена`, "success");
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

async function expandParallelStrategy(strategyId, mode = "instruments", forceReopen = false) {
  const panel = document.getElementById("parallelInstrExpanded");
  const title = document.getElementById("parallelInstrTitle");
  const body  = document.getElementById("parallelInstrBody");
  if (!panel || !body) return;

  // Toggle: close if same strategy + same mode already open (unless forced reopen after save)
  if (!forceReopen && panel.dataset.strategyId === String(strategyId) &&
      panel.dataset.mode === mode && panel.style.display !== "none") {
    panel.style.display = "none";
    return;
  }

  panel.dataset.strategyId = strategyId;
  panel.dataset.mode = mode;
  body.innerHTML = '<span class="note">Загрузка…</span>';
  panel.style.display = "block";

  // "Добавить инструмент" всегда привязан к текущей стратегии
  const btnAdd = document.getElementById("parallelBtnAddInstr");
  if (btnAdd) btnAdd.onclick = () => openAddInstrumentModal(strategyId);

  try {
    const det = await apiGet(`/api/strategy/${strategyId}/details`);
    const name = det.name || `#${strategyId}`;

    if (mode === "settings") {
      if (title) title.textContent = `Настройки — ${name}`;
      if (btnAdd) btnAdd.style.display = "none";
      const s = det.settings || {};
      body.innerHTML = `
        <form class="form-grid" id="parallelStratForm">
          <input type="hidden" name="strategy_id_val" value="${strategyId}">
          <label>Сделок/день<input class="field" name="max_trades_per_day" value="${esc(s.max_trades_per_day||15)}"></label>
          <label>Лимит убытка ₽<input class="field" name="max_daily_loss_rub" value="${esc(s.max_daily_loss_rub_ui||200)}"></label>
          <label>Позиций макс<input class="field" name="max_open_positions" value="${esc(s.max_open_positions||2)}"></label>
          <label>Интервал, сек<input class="field" name="check_interval_sec" value="${esc(s.check_interval_sec||5)}"></label>
          <label>SL %<input class="field" name="default_stop_loss_pct" value="${esc(s.default_stop_loss_pct_ui||0.25)}"></label>
          <label>TP %<input class="field" name="default_take_profit_pct" value="${esc(s.default_take_profit_pct_ui||0.5)}"></label>
          <label>Комиссия %<input class="field" name="estimated_commission_pct" value="${esc(s.estimated_commission_pct_ui||0.04)}"></label>
          <label>Мин. score сигнала<input class="field" type="number" name="min_signal_score" min="0" max="100" value="${esc(s.min_signal_score||0)}"></label>
          <label>Режим торговли
            <select class="field" name="tradingmode">
              <option value="trend" ${s.tradingmode==="trend"?"selected":""}>Тренд</option>
              <option value="mean_reversion" ${s.tradingmode==="mean_reversion"?"selected":""}>Возврат к средней</option>
              <option value="breakout" ${s.tradingmode==="breakout"?"selected":""}>Пробой</option>
            </select>
          </label>
          <label>Лонг разрешён
            <select class="field" name="allow_long_global">
              <option value="1" ${s.allow_long_global!=="0"?"selected":""}>Да</option>
              <option value="0" ${s.allow_long_global==="0"?"selected":""}>Нет</option>
            </select>
          </label>
          <label>Шорт разрешён
            <select class="field" name="allow_short_global">
              <option value="1" ${s.allow_short_global==="1"?"selected":""}>Да</option>
              <option value="0" ${s.allow_short_global!=="1"?"selected":""}>Нет</option>
            </select>
          </label>
          <label>Трейлинг-стоп
            <select class="field" name="trailing_stop_enabled">
              <option value="1" ${s.trailing_stop_enabled==="1"?"selected":""}>Вкл</option>
              <option value="0" ${s.trailing_stop_enabled!=="1"?"selected":""}>Выкл</option>
            </select>
          </label>
          <label>Фильтр аналитиков T-Bank
            <select class="field" name="use_signal_service">
              <option value="1" ${s.use_signal_service==="1"?"selected":""}>Вкл</option>
              <option value="0" ${s.use_signal_service!=="1"?"selected":""}>Выкл</option>
            </select>
          </label>
          <label>RSI/MACD/BB подтверждение (API)
            <select class="field" name="use_api_confirm">
              <option value="1" ${s.use_api_confirm==="1"?"selected":""}>Вкл</option>
              <option value="0" ${s.use_api_confirm!=="1"?"selected":""}>Выкл</option>
            </select>
          </label>
          <label>Фильтр стакана (давление 40%)
            <select class="field" name="use_order_book_filter">
              <option value="1" ${(s.use_order_book_filter ?? "1") !== "0" ? "selected" : ""}>Вкл</option>
              <option value="0" ${s.use_order_book_filter === "0" ? "selected" : ""}>Выкл (−156 req/min)</option>
            </select>
          </label>
          <div class="row-buttons">
            <button type="button" class="btn btn-primary" id="btnSaveParallelStrat">Сохранить настройки</button>
          </div>
        </form>`;
      body.querySelector("#btnSaveParallelStrat")?.addEventListener("click", () => saveStrategySettings(strategyId, "parallelStratForm"));

    } else {
      if (title) title.textContent = `Инструменты — ${name}`;
      if (btnAdd) btnAdd.style.display = "";
      const instruments = det.instruments || [];
      body.innerHTML = instruments.length
        ? _renderInstrumentForms(instruments, strategyId)
        : '<p class="note">Инструменты не добавлены. Нажмите «Добавить инструмент».</p>';
      _bindInstrumentForms(panel, strategyId);
    }
  } catch (e) {
    body.innerHTML = `<span class="note" style="color:#ff7b7b">Ошибка: ${esc(e.message)}</span>`;
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

// ── Credentials UI ────────────────────────────────────────────────────────────

let _credMode = "sandbox";

async function loadCredentials() {
  try {
    const c = await apiGet("/api/credentials");
    _credMode = c.is_sandbox ? "sandbox" : "prod";
    const info = c.is_sandbox ? c.sandbox : c.prod;
    const label = document.getElementById("credModeLabel");
    if (label) label.textContent = c.is_sandbox ? "Sandbox" : "Боевой";
    const masked = document.getElementById("credTokenMasked");
    if (masked) masked.textContent = info.token_masked || (info.has_token ? "****" : "—не задан—");
    const accEl = document.getElementById("credAccountId");
    if (accEl) accEl.textContent = info.account_id || "—";
    const inp = document.getElementById("credAccountInput");
    if (inp) inp.value = info.account_id || "";
  } catch(e) {}
}

function toggleCredEdit() {
  const ed = document.getElementById("credEdit");
  const di = document.getElementById("credDisplay");
  if (!ed) return;
  const showing = ed.style.display !== "none";
  ed.style.display = showing ? "none" : "block";
  if (di) di.style.display = showing ? "grid" : "none";
  if (!showing) document.getElementById("credTokenInput")?.focus();
}

async function saveCredentials() {
  const token     = document.getElementById("credTokenInput")?.value.trim() || "";
  const accountId = document.getElementById("credAccountInput")?.value.trim() || "";
  if (!token && !accountId) { showToast("Введите токен или Account ID", "error"); return; }
  try {
    await apiPostJson("/api/credentials", { mode: _credMode, token, account_id: accountId });
    showToast("Данные сохранены в .env на сервере", "success");
    document.getElementById("credTokenInput").value = "";
    toggleCredEdit();
    await loadCredentials();
  } catch(e) {
    showToast("Ошибка: " + e.message, "error");
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

async function saveStrategySettings(strategyId, formId = "strategySettingsForm") {
  if (!strategyId) { showToast("Стратегия не выбрана", "error"); return; }
  try {
    const form = document.getElementById(formId)
               || document.getElementById("parallelStratForm")
               || document.getElementById("strategySettingsForm");
    if (!form) { showToast("Форма не найдена", "error"); return; }
    const fd = new FormData(form);
    const data = Object.fromEntries(fd.entries());
    delete data.strategy_id_val;
    const resp = await apiPostForm(`/api/strategies/${strategyId}/settings`, data);
    showToast(`Настройки сохранены${resp.new_name ? ` → ${resp.new_name}` : ""}`, "success");
    // Перезагружаем панель чтобы подтвердить сохранение из БД
    if (formId === "parallelStratForm") {
      await expandParallelStrategy(strategyId, "settings", true);
    } else {
      await renderSettingsTab();
    }
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
      <div class="row between">
        <h2>Активные ордера <span class="note">(лимитные / рыночные в очереди)</span></h2>
        <button class="btn btn-danger" id="btnCancelAllOrders" style="font-size:12px">Отменить все</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер/FIGI</th><th>Тип</th><th>Направление</th><th>Лотов</th><th>Исп.</th><th>Цена</th><th>Сумма, ₽</th><th>Создан</th><th>Действие</th></tr></thead>
          <tbody id="activeOrdersBody"></tbody>
        </table>
      </div>
    </section>
    <section class="block">
      <div class="row between">
        <h2>Активные stop orders</h2>
        <button class="btn btn-danger" id="btnCancelAllStops" style="font-size:12px">Отменить все</button>
      </div>
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
  document.getElementById("btnCancelAllOrders")?.addEventListener("click", cancelAllActiveOrders);
  document.getElementById("btnCancelAllStops")?.addEventListener("click", cancelAllStopOrders);
  host.querySelectorAll("[data-close-one]").forEach((btn) => {
    btn.addEventListener("click", () => closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction));
  });
  host.querySelectorAll("[data-close-portfolio]").forEach((btn) => {
    btn.addEventListener("click", () => closeOnePosition(btn.dataset.figi, btn.dataset.qty, btn.dataset.direction));
  });

  if (data.broker_error) {
    showToast(`Портфель: данные из локальной БД (ошибка API: ${data.broker_error.slice(0, 80)})`, "error", 6000);
  }

  // Активные ордера (лимитные/рыночные в очереди)
  try {
    const ordersData = await apiGet("/api/orders/active");
    const ordersBody = document.getElementById("activeOrdersBody");
    if (ordersBody) {
      const dirLabel = d => String(d).includes("BUY") ? '<span style="color:#2fa36b">BUY</span>' : String(d).includes("SELL") ? '<span style="color:#ff7b7b">SELL</span>' : esc(d);
      ordersBody.innerHTML = (ordersData.items || []).map(x => `
        <tr>
          <td><b>${esc(x.figi || "")}</b></td>
          <td>${esc((x.order_type || "").replace("ORDER_TYPE_",""))}</td>
          <td>${dirLabel(x.direction)}</td>
          <td>${esc(x.lots_requested || "")}</td>
          <td>${esc(x.lots_executed || "0")}</td>
          <td>${esc(x.price || "")}</td>
          <td><b>${esc(x.total_amount || "")}</b></td>
          <td style="font-size:11px">${esc((x.created_at || "").slice(0,19).replace("T"," "))}</td>
          <td><button class="btn btn-danger" onclick="cancelActiveOrder('${esc(x.order_id)}')">Отменить</button></td>
        </tr>
      `).join("") || `<tr><td colspan="9" class="muted">Нет активных ордеров — резерв средств отсутствует</td></tr>`;
    }
  } catch (e) {
    const ordersBody = document.getElementById("activeOrdersBody");
    if (ordersBody) ordersBody.innerHTML = `<tr><td colspan="9">Ошибка: ${esc(e.message)}</td></tr>`;
  }

  // Активные stop orders
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
          <td><button class="btn btn-danger" onclick="cancelStopOrder('${esc(x.stop_order_id || "")}')">Отменить</button></td>
        </tr>
      `).join("") || `<tr><td colspan="8" class="muted">Нет активных stop orders</td></tr>`;
    }
  } catch (e) {
    const stopBody = document.getElementById("activeStopOrdersBody");
    if (stopBody) stopBody.innerHTML = `<tr><td colspan="8">Ошибка: ${esc(e.message)}</td></tr>`;
  }

  // Навешиваем обработчики на data-cancel-stop (legacy)
  host.querySelectorAll("[data-cancel-stop]").forEach(btn => {
    btn.addEventListener("click", () => cancelStopOrder(btn.dataset.cancelStop));
  });
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

async function cancelActiveOrder(orderId) {
  if (!confirm(`Отменить ордер ${orderId}?`)) return;
  try {
    await apiPostForm(`/api/orders/${encodeURIComponent(orderId)}/cancel`, {});
    showToast("Ордер отменён", "success");
    await renderPortfolioTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error", 6000);
  }
}

async function cancelAllActiveOrders() {
  if (!confirm("Отменить все активные ордера? Это освободит заблокированные средства.")) return;
  try {
    const r = await apiPostForm("/api/orders/cancel-all", {});
    showToast(`Отменено ордеров: ${r.cancelled || 0}`, "success");
    await renderPortfolioTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error", 6000);
  }
}

async function cancelStopOrder(stopId) {
  if (!confirm(`Отменить стоп-ордер ${stopId}?`)) return;
  try {
    await apiPostForm(`/api/stop-orders/${encodeURIComponent(stopId)}/cancel`, {});
    showToast("Стоп-ордер отменён", "success");
    await renderPortfolioTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error", 6000);
  }
}

async function cancelAllStopOrders() {
  if (!confirm("Отменить все стоп-ордера?")) return;
  try {
    const r = await apiPostForm("/api/stop-orders/cancel-all", {});
    showToast(`Отменено стопов: ${r.cancelled || 0}`, "success");
    await renderPortfolioTab();
    await renderSummaryCards();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error", 6000);
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

let _histPeriod    = 30;          // legacy, не используется
let _histDateFrom  = "";          // YYYY-MM-DD
let _histDateTo    = "";          // YYYY-MM-DD (не включительно)
let _histPeriodKey = "month_cur"; // ключ активного периода
let _histActiveTab = "trades";
let _histData      = {};
let _histBrokerItems  = [];
let _histBrokerCursor = "";

async function renderHistoryTab() {
  const host = document.getElementById("view-history");
  if (!host) return;

  // Инициализируем текущий месяц при первом открытии
  if (!_histDateFrom) {
    const now = new Date();
    const yr = now.getFullYear(), mo = now.getMonth();
    _histDateFrom = `${yr}-${String(mo+1).padStart(2,"0")}-01`;
    const nxt = new Date(yr, mo+1, 1);
    _histDateTo   = `${nxt.getFullYear()}-${String(nxt.getMonth()+1).padStart(2,"0")}-01`;
    _histPeriodKey = `m_${yr}_${mo}`;
  }

  host.innerHTML = _histShell();

  // Параллельная загрузка статистики и сделок/логов
  try {
    await Promise.all([_histLoadStats(), _histLoadTradesAndLogs()]);
  } catch (e) {
    console.error("history load:", e);
  }
}

function _histBuildPeriods() {
  const now  = new Date();
  const yr   = now.getFullYear();
  const mo   = now.getMonth(); // 0-based
  const months = ["Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"];
  const items = [];

  // Все 12 месяцев текущего года
  for (let m = 0; m <= 11; m++) {
    const from = `${yr}-${String(m+1).padStart(2,"0")}-01`;
    const toMo = new Date(yr, m+1, 1);
    const to   = `${toMo.getFullYear()}-${String(toMo.getMonth()+1).padStart(2,"0")}-01`;
    items.push({key:`m_${yr}_${m}`, label: months[m], from, to, group:"months"});
  }

  // Все 4 квартала текущего года
  for (let q = 0; q <= 3; q++) {
    const qStart = q * 3;
    const from = `${yr}-${String(qStart+1).padStart(2,"0")}-01`;
    const qEnd = new Date(yr, qStart+3, 1);
    const to   = `${qEnd.getFullYear()}-${String(qEnd.getMonth()+1).padStart(2,"0")}-01`;
    items.push({key:`q_${yr}_${q+1}`, label:`Q${q+1}`, from, to, group:"quarters"});
  }

  // Годы (2025 и далее + следующий год)
  for (let y = 2025; y <= yr + 1; y++) {
    items.push({key:`y_${y}`, label: String(y),
      from: `${y}-01-01`, to: `${y+1}-01-01`, group:"years"});
  }

  // Всё время
  items.push({key:"all", label:"Всё", from:"", to:"", group:"all"});
  return items;
}

function _histPeriodBtns() {
  const all = _histBuildPeriods();
  const groups = [
    {id:"months",   label:"Месяц", items: all.filter(p=>p.group==="months")},
    {id:"quarters", label:"Квартал", items: all.filter(p=>p.group==="quarters")},
    {id:"years",    label:"Год",   items: all.filter(p=>p.group==="years")},
    {id:"all",      label:"",      items: all.filter(p=>p.group==="all")},
  ];
  return groups.map(g => `
    <div class="row" style="gap:4px;align-items:center">
      ${g.label ? `<span class="muted" style="font-size:11px;min-width:52px">${g.label}:</span>` : ""}
      ${g.items.map(p => `<button class="btn${_histPeriodKey===p.key?" btn-primary":""}" style="padding:3px 9px;font-size:12px"
              onclick="histSetPeriodRange('${p.key}','${p.from}','${p.to}')">${p.label}</button>`).join("")}
    </div>`).join("");
}

function _histShell() {
  const tabs = [
    ["trades","Сделки"],["broker","Операции брокера"],
    ["journal","Журнал"],["errors","Ошибки"],["system","Система"],
  ];
  return `
    <div class="block" style="padding:10px 16px;margin-bottom:12px">
      <div class="row between" style="align-items:flex-start;gap:10px;flex-wrap:wrap">
        <div style="display:flex;flex-direction:column;gap:5px" id="histPeriodBtns">
          ${_histPeriodBtns()}
        </div>
        <div class="row" style="gap:8px">
          <span class="note" id="histStatus">Загрузка…</span>
          <button class="btn btn-danger" onclick="histClearTrades()"
                  style="font-size:12px;padding:6px 12px">🗑 Очистить сделки</button>
        </div>
      </div>
    </div>

    <div class="summary-grid" id="histSummary" style="margin-bottom:18px">
      ${Array(5).fill(0).map(() =>
        `<div class="card"><div class="label">…</div><div class="value">—</div></div>`).join("")}
    </div>

    <div class="block" style="padding:14px">
      <div class="row between" style="margin-bottom:4px">
        <h2 style="margin:0">Динамика капитала</h2>
        <span class="note">Накопленный PnL по сделкам</span>
      </div>
      <div id="histEquity" style="height:260px"></div>
    </div>

    <div class="two-cols">
      <div class="block" style="padding:14px">
        <h2 style="margin:0 0 4px">По инструментам</h2>
        <div id="histTicker" style="height:210px"></div>
      </div>
      <div class="block" style="padding:14px">
        <h2 style="margin:0 0 4px">По причине закрытия</h2>
        <div id="histReason" style="height:210px"></div>
      </div>
    </div>

    <div class="block">
      <div class="row" style="gap:6px;margin-bottom:14px;flex-wrap:wrap">
        ${tabs.map(([t,l]) => `
          <button class="btn${_histActiveTab===t?" btn-primary":""}" id="histTBtn-${t}"
                  onclick="histShowTab('${t}')">${l}</button>`).join("")}
      </div>
      <div id="histTabContent"></div>
    </div>`;
}

async function histClearTrades() {
  if (!confirm("Удалить ВСЕ сделки из локальной базы?\n\nЭто только локальный журнал бота — данные у брокера не затрагиваются.")) return;
  try {
    const r = await fetch("/api/history/clear", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({trades: true, logs: false}),
    });
    const d = await r.json();
    showToast(`Удалено сделок: ${d.trades_deleted ?? 0}`, "success");
    await renderHistoryTab();
  } catch (e) {
    showToast("Ошибка: " + e.message, "error");
  }
}

function histSetPeriod(days) {
  // Legacy: конвертируем days в диапазон
  if (days === 0) { histSetPeriodRange("all", "", ""); return; }
  const to = new Date(); to.setDate(to.getDate() + 1);
  const from = new Date(); from.setDate(from.getDate() - days);
  histSetPeriodRange(`days_${days}`, from.toISOString().slice(0,10), to.toISOString().slice(0,10));
}

function histSetPeriodRange(key, from, to) {
  _histPeriodKey  = key;
  _histDateFrom   = from;
  _histDateTo     = to;
  // Перерисовываем кнопки
  const btnsEl = document.getElementById("histPeriodBtns");
  if (btnsEl) btnsEl.innerHTML = _histPeriodBtns();
  _histLoadStats();
  _histLoadTradesAndLogs();
  if (_histActiveTab === "broker") _histLoadBroker(true);
}

async function _histLoadStats() {
  try {
    document.getElementById("histStatus").textContent = "Загрузка…";
    const qs = _histDateFrom ? `date_from=${_histDateFrom}&date_to=${_histDateTo}` : "";
    const st = await apiGet(`/api/history/stats?${qs}`);
    _histRenderSummary(st.summary || {});
    _histRenderEquity(st.equity_curve || []);
    _histRenderTickerChart(st.by_ticker || []);
    _histRenderReasonChart(st.by_reason || {});
    const label = _histDateFrom ? `${_histDateFrom} — ${_histDateTo || "…"}` : "всё время";
    document.getElementById("histStatus").textContent = `Период: ${label}`;
  } catch (e) {
    const el = document.getElementById("histStatus");
    if (el) el.textContent = "Ошибка статистики: " + e.message;
  }
}

function _histRenderSummary(s) {
  const pnl = parseFloat(s.total_pnl || 0);
  const avg = parseFloat(s.avg_pnl || 0);
  const wr  = parseFloat(s.win_rate || 0);
  const cards = [
    {label:"Сделок всего", value: s.trades_count || 0, cls:""},
    {label:"Общий PnL",    value: s.total_pnl_ui || "—",
     cls: pnl > 0 ? "status-ok" : pnl < 0 ? "status-problem" : ""},
    {label:"Win Rate",     value: wr.toFixed(1) + "%",
     cls: wr >= 50 ? "status-ok" : wr > 0 ? "status-problem" : ""},
    {label:"Средний PnL",  value: s.avg_pnl_ui || "—",
     cls: avg > 0 ? "status-ok" : avg < 0 ? "status-problem" : ""},
    {label:"Комиссии уплачено", value: s.total_commission_ui || "—", cls:""},
  ];
  const el = document.getElementById("histSummary");
  if (el) el.innerHTML = cards.map(c =>
    `<div class="card"><div class="label">${c.label}</div>
     <div class="value ${c.cls}">${c.value}</div></div>`).join("");
}

function _histRenderEquity(curve) {
  const el = document.getElementById("histEquity");
  if (!el || !window.Plotly) return;
  if (!curve.length) {
    Plotly.newPlot(el, [], {
      paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)", font:{color:"#eef4ff"},
      annotations:[{text:"Нет сделок за период",xref:"paper",yref:"paper",x:.5,y:.5,showarrow:false,font:{size:14}}]
    }, {displayModeBar:false,responsive:true});
    return;
  }
  const times    = curve.map(p => p.time);
  const cumPnl   = curve.map(p => p.cumulative_pnl);
  const perTrade = curve.map(p => p.pnl);
  const barColors = perTrade.map(v => v >= 0 ? "rgba(47,163,107,.8)" : "rgba(191,77,90,.8)");
  // customdata: [ticker, direction, per_trade_pnl, cumulative_pnl, reason]
  const cd = curve.map(p => [p.ticker, p.direction, p.pnl, p.cumulative_pnl, p.reason || "—"]);
  const hl = {font:{color:"#111111", size:12}, align:"left"};
  const tmplLine = "<b>%{customdata[0]}</b> %{customdata[1]}<br>" +
                   "Сделка: %{customdata[2]:+.2f} ₽<br>" +
                   "Накопл.: %{y:.2f} ₽<br>" +
                   "Причина: %{customdata[4]}<extra></extra>";
  const tmplBar  = "<b>%{customdata[0]}</b> %{customdata[1]}<br>" +
                   "Сделка: %{y:+.2f} ₽<br>" +
                   "Накопл.: %{customdata[3]:.2f} ₽<br>" +
                   "Причина: %{customdata[4]}<extra></extra>";
  Plotly.newPlot(el, [
    {x:times, y:cumPnl, type:"scatter", mode:"lines+markers", name:"Накопл. PnL",
     line:{color:"#4c8dff",width:2}, marker:{size:5, color:"#4c8dff"},
     fill:"tozeroy", fillcolor:"rgba(76,141,255,.07)",
     customdata:cd, hovertemplate:tmplLine, hoverlabel:hl},
    {x:times, y:perTrade, type:"bar", name:"PnL сделки", yaxis:"y2",
     marker:{color:barColors}, opacity:.85,
     customdata:cd, hovertemplate:tmplBar, hoverlabel:hl},
  ], {
    paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
    font:{color:"#eef4ff",size:11},
    margin:{t:10,r:60,b:60,l:70},
    legend:{orientation:"h",y:-0.25},
    hovermode:"closest",
    hoverlabel: hl,
    xaxis:{gridcolor:"rgba(255,255,255,.05)", type:"date",
           tickformat:"%d.%m\n%H:%M", tickfont:{size:10}},
    yaxis:{title:"Накопл. PnL (₽)", gridcolor:"rgba(255,255,255,.06)",
           zeroline:true, zerolinecolor:"rgba(255,255,255,.25)", tickfont:{size:10}},
    yaxis2:{title:"PnL сделки (₽)", overlaying:"y", side:"right",
            showgrid:false, zeroline:false, tickfont:{size:10}},
  }, {displayModeBar:false, responsive:true});
}

function _histRenderTickerChart(byTicker) {
  const el = document.getElementById("histTicker");
  if (!el || !window.Plotly) return;
  if (!byTicker.length) {
    el.innerHTML = `<div class="note" style="text-align:center;padding:70px 0">Нет данных</div>`;
    return;
  }
  const sorted = [...byTicker].sort((a,b) => a.pnl - b.pnl);
  Plotly.newPlot(el, [{
    x: sorted.map(t => t.pnl),
    y: sorted.map(t => t.ticker),
    type:"bar", orientation:"h",
    text: sorted.map(t => `${t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(0)} ₽`),
    textposition:"auto",
    marker:{color: sorted.map(t => t.pnl >= 0 ? "rgba(47,163,107,.8)" : "rgba(191,77,90,.8)")},
    hovertemplate:"%{y}: %{x:.2f} ₽<extra></extra>",
  }], {
    paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
    font:{color:"#eef4ff",size:11}, margin:{t:6,r:10,b:30,l:60},
    hoverlabel:{bgcolor:"rgba(14,27,52,.95)", bordercolor:"rgba(76,141,255,.5)",
                font:{color:"#eef4ff",size:12}},
    xaxis:{gridcolor:"rgba(255,255,255,.05)", zeroline:true, zerolinecolor:"rgba(255,255,255,.2)"},
    yaxis:{gridcolor:"rgba(255,255,255,.05)"},
  }, {displayModeBar:false, responsive:true});
}

function _histRenderReasonChart(byReason) {
  const el = document.getElementById("histReason");
  if (!el || !window.Plotly) return;
  const keys = Object.keys(byReason);
  if (!keys.length) {
    el.innerHTML = `<div class="note" style="text-align:center;padding:70px 0">Нет данных</div>`;
    return;
  }
  const palette = {TAKE_PROFIT:"rgba(47,163,107,.85)", STOP_LOSS:"rgba(191,77,90,.85)",
                   TRAILING_STOP:"rgba(255,185,50,.85)"};
  Plotly.newPlot(el, [{
    labels: keys,
    values: keys.map(k => byReason[k].count),
    type:"pie", hole:.5,
    marker:{colors: keys.map(k => palette[k] || "rgba(76,141,255,.8)")},
    textinfo:"label+percent", textfont:{size:11},
    customdata: keys.map(k => byReason[k].pnl),
    hovertemplate:"%{label}: %{value} сд.<br>PnL: %{customdata:.2f} ₽<extra></extra>",
  }], {
    paper_bgcolor:"rgba(0,0,0,0)", font:{color:"#eef4ff",size:11},
    margin:{t:10,r:10,b:30,l:10},
    legend:{orientation:"h", y:-0.1},
  }, {displayModeBar:false, responsive:true});
}

async function _histLoadTradesAndLogs() {
  try {
    const qs2 = _histDateFrom ? `date_from=${_histDateFrom}&date_to=${_histDateTo}` : "";
    const data = await apiGet(`/api/dashboard/history?${qs2}`);
    _histData = data;
    _histRefreshActiveTab();
  } catch (e) {
    console.error("history trades/logs:", e);
  }
}

function _histRefreshActiveTab() {
  switch (_histActiveTab) {
    case "trades":  _histRenderTrades(_histData.trades || []); break;
    case "journal": _histRenderLogs(_histData.common_logs || [], "Нет событий в журнале"); break;
    case "errors":  _histRenderLogs(_histData.error_logs  || [], "Ошибок нет"); break;
    case "system":  _histRenderLogs(_histData.system_logs || [], "Нет системных событий"); break;
  }
}

function histShowTab(name) {
  _histActiveTab = name;
  document.querySelectorAll("[id^='histTBtn-']").forEach(b => {
    const t = b.id.replace("histTBtn-","");
    b.classList.toggle("btn-primary", t === name);
  });
  if (name === "broker") { _histLoadBroker(false); return; }
  _histRefreshActiveTab();
}

function _histRenderTrades(trades) {
  const content = document.getElementById("histTabContent");
  if (!content) return;
  const rows = trades.map(t => {
    const pnl = parseFloat(t.pnl || 0);
    const bg  = pnl > 0 ? "rgba(47,163,107,.07)" : pnl < 0 ? "rgba(191,77,90,.07)" : "";
    const col = pnl >= 0 ? "#2fa36b" : "#ff7b7b";
    const badge = t.direction === "BUY"
      ? `<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b">BUY</span>`
      : `<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b">SELL</span>`;
    return `<tr style="background:${bg}">
      <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.open_time || "—")}</td>
      <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.time || "—")}</td>
      <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.duration_ui || "—")}</td>
      <td><b>${esc(t.ticker)}</b></td>
      <td>${badge}</td>
      <td>${esc(t.entry_ui)}</td><td>${esc(t.exit_ui)}</td><td>${esc(t.qty)}</td>
      <td class="muted">${esc(t.commission_ui)}</td>
      <td style="font-weight:700;color:${col}">${pnl >= 0 ? "+" : ""}${esc(t.pnl_ui)}</td>
      <td class="muted" style="font-size:12px">${esc(t.reason)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="note" style="text-align:center;padding:20px">
    Нет сделок за период</td></tr>`;
  content.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <input class="field" id="hfTrades" placeholder="Фильтр: тикер, направление, причина" style="flex:1">
    </div>
    <div class="table-wrap">
      <table id="hTrades">
        <thead><tr>
          <th>Открыто</th><th>Закрыто</th><th>Длит.</th><th>Тикер</th><th>Напр.</th>
          <th>Вход</th><th>Выход</th><th>Лоты</th>
          <th>Комиссия</th><th>PnL</th><th>Причина</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  document.getElementById("hfTrades")?.addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#hTrades tbody tr").forEach(r => {
      r.style.display = !q || r.innerText.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

function _histRenderLogs(rows, emptyMsg) {
  const content = document.getElementById("histTabContent");
  if (!content) return;
  const tbody = rows.map(r => {
    const isErr = r.level === "ERROR";
    return `<tr style="${isErr ? "background:rgba(191,77,90,.07)" : ""}">
      <td class="muted" style="font-size:12px;white-space:nowrap">${esc(r.event_time)}</td>
      <td>${esc(r.event_type)}</td>
      <td>${esc(r.ticker)}</td>
      <td style="color:${isErr ? "#ff7b7b" : ""}">${esc(r.level)}</td>
      <td style="font-size:12px">${esc(r.message)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="5" class="note" style="text-align:center;padding:20px">${emptyMsg}</td></tr>`;
  content.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <input class="field" id="hfLogs" placeholder="Фильтр по тексту" style="flex:1">
    </div>
    <div class="table-wrap">
      <table id="hLogs">
        <thead><tr><th>Время</th><th>Событие</th><th>Тикер</th><th>Уровень</th><th>Сообщение</th></tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>`;
  document.getElementById("hfLogs")?.addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#hLogs tbody tr").forEach(r => {
      r.style.display = !q || r.innerText.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

async function _histLoadBroker(reset = false) {
  const content = document.getElementById("histTabContent");
  if (!content) return;
  if (reset || !document.getElementById("hBrokerBody")) {
    _histBrokerCursor = "";
    _histBrokerItems  = [];
    content.innerHTML = `
      <div class="row" style="margin-bottom:10px;gap:8px;flex-wrap:wrap">
        <input class="field" id="hfBroker" placeholder="Фильтр: тикер, направление" style="flex:1">
        <select class="field" id="hBrokerDays" style="width:auto">
          <option value="7">7 дней</option>
          <option value="30" selected>30 дней</option>
          <option value="90">90 дней</option>
          <option value="180">180 дней</option>
        </select>
        <button class="btn" onclick="_histLoadBroker(true)">Обновить</button>
      </div>
      <div class="table-wrap">
        <table id="hBrokerTable">
          <thead><tr>
            <th>Дата</th><th>Тикер</th><th>Направление</th>
            <th>Кол-во</th><th>Цена</th><th>Сумма</th><th>Комиссия</th>
          </tr></thead>
          <tbody id="hBrokerBody"><tr><td colspan="7" class="note" style="text-align:center;padding:20px">Загрузка…</td></tr></tbody>
        </table>
      </div>
      <div id="hBrokerPager" style="margin-top:10px"></div>`;
    document.getElementById("hfBroker")?.addEventListener("input", e => {
      const q = e.target.value.trim().toLowerCase();
      document.querySelectorAll("#hBrokerTable tbody tr").forEach(r => {
        r.style.display = !q || r.innerText.toLowerCase().includes(q) ? "" : "none";
      });
    });
    // Auto-sync period selector
    const sel = document.getElementById("hBrokerDays");
    if (sel && _histPeriod > 0) sel.value = String(_histPeriod);
  }
  const days = document.getElementById("hBrokerDays")?.value || 30;
  const body = document.getElementById("hBrokerBody");
  const pager= document.getElementById("hBrokerPager");
  if (body) body.innerHTML = `<tr><td colspan="7" class="note" style="text-align:center;padding:20px">Загрузка…</td></tr>`;
  try {
    const data = await apiGet(
      `/api/broker-operations?cursor=${encodeURIComponent(_histBrokerCursor)}&days=${days}&limit=50`);
    const items = (data.items || []).filter(x => !x.is_fee);
    _histBrokerItems = _histBrokerCursor === "" ? items : [..._histBrokerItems, ...items];
    _histBrokerCursor = data.next_cursor || "";
    if (body) {
      body.innerHTML = _histBrokerItems.length === 0
        ? `<tr><td colspan="7" class="note" style="text-align:center;padding:20px">Нет операций за период</td></tr>`
        : _histBrokerItems.map(op => {
            const badge = op.direction === "BUY"
              ? `<span class="badge" style="background:rgba(47,163,107,.2);color:#2fa36b">BUY</span>`
              : `<span class="badge" style="background:rgba(191,77,90,.2);color:#ff7b7b">SELL</span>`;
            return `<tr>
              <td class="muted" style="font-size:12px;white-space:nowrap">${esc(op.date)}</td>
              <td><b>${esc(op.ticker || op.figi)}</b></td>
              <td>${badge}</td>
              <td>${esc(op.quantity)}</td>
              <td>${esc(op.price_ui)}</td>
              <td>${esc(op.payment_ui)}</td>
              <td class="muted">${esc(op.commission_ui)}</td>
            </tr>`;
          }).join("");
    }
    if (pager) {
      pager.innerHTML = data.has_next
        ? `<button class="btn" onclick="_histLoadBroker(false)">Загрузить ещё (${_histBrokerItems.length} загружено)</button>`
        : `<span class="note">Всего операций: ${_histBrokerItems.length}</span>`;
    }
  } catch (e) {
    if (body) body.innerHTML =
      `<tr><td colspan="7" style="color:#ff7b7b;text-align:center;padding:20px">Ошибка: ${esc(e.message)}</td></tr>`;
  }
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

// renderChartTab removed — chart tab deleted

function renderCandlesAndScore_unused(data) {
  // kept as dead code stub to avoid reference errors if called somewhere
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

async function sandboxResetBalance() {
  const inp = document.getElementById("sandboxResetAmount");
  const amount = inp ? parseInt(inp.value) || 59518 : 59518;
  if (!confirm(`Сбросить Sandbox счёт? Текущий счёт будет закрыт, создан новый с балансом ${amount.toLocaleString("ru")} ₽. Бот будет перезапущен.`)) return;
  try {
    showToast("Сбрасываю счёт...", "info", 3000);
    const res = await apiPostForm("/api/sandbox/reset", { amount });
    showToast(`Новый счёт создан. Баланс: ${Math.round(res.balance).toLocaleString("ru")} ₽. Перезапуск бота...`, "success", 6000);
    await apiPost("/api/bot/restart");
    setTimeout(() => { renderSummaryCards(); renderMainData(); }, 4000);
  } catch (e) {
    showToast(`Ошибка сброса: ${e.message}`, "error", 6000);
  }
}

// ── Parallel strategy status ──────────────────────────────────────────────────

let _mainChartInterval = "1min";
let _mainChartHours    = 4;
let _mainChartFigis    = [];  // [{figi, ticker}]
const _prevSignals     = {};  // figi → {action, time}
let _psLastUpdate      = 0;   // timestamp последнего обновления тикеров
let _psLastFigis       = "";  // список figi для определения нужности перерисовки графиков

// CSS-анимации (вставляются один раз)
(function() {
  if (!document.getElementById('sig-flash-style')) {
    const s = document.createElement('style');
    s.id = 'sig-flash-style';
    s.textContent = [
      // Строка при смене сигнала BUY/SELL — синяя волна
      `@keyframes sigFlash{0%{background:rgba(76,141,255,.4)}60%{background:rgba(76,141,255,.15)}100%{background:inherit}}`,
      `.sig-flash{animation:sigFlash .9s ease-out 2}`,
      // Ячейка при изменении значения — зелёный акцент
      `@keyframes cellUpdUp{0%{background:rgba(47,163,107,.45)}100%{background:transparent}}`,
      `@keyframes cellUpdDown{0%{background:rgba(191,77,90,.45)}100%{background:transparent}}`,
      `@keyframes cellUpdNeutral{0%{background:rgba(255,255,255,.18)}100%{background:transparent}}`,
      `.cell-updated-up{animation:cellUpdUp .6s ease-out forwards}`,
      `.cell-updated-down{animation:cellUpdDown .6s ease-out forwards}`,
      `.cell-updated-neutral{animation:cellUpdNeutral .5s ease-out forwards}`,
      `.cell-updated{animation:cellUpdUp .6s ease-out forwards}`,
      // Фиксированная высота строк таблицы инструментов — нет прыжков
      `#_psInstrBody tr{height:36px}`,
      `#_psInstrBody td{white-space:nowrap;overflow:hidden;max-width:200px;text-overflow:ellipsis;vertical-align:middle}`,
      `#_psInstrBody .live-sig{white-space:nowrap;cursor:pointer}`,
      // Мигающая точка «live» — пульс
      `@keyframes liveDot{0%,100%{opacity:1}50%{opacity:.25}}`,
      `.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#2fa36b;animation:liveDot 1.4s ease-in-out infinite;vertical-align:middle;margin-right:5px}`,
      `.live-dot.stale{background:#888;animation:none}`,
      // Fade-in для карточек при первом рендере
      `@keyframes fadeInUp{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}`,
      `.card-appear{animation:fadeInUp .25s ease-out}`,
    ].join('');
    document.head.appendChild(s);
  }
})();

async function refreshParallelStatus() {
  try {
    const data    = await apiGet("/api/parallel/status");
    const threads = data.threads || [];
    const block   = document.getElementById("parallelStatusBlock");
    const body    = document.getElementById("parallelStatusBody");
    if (!block || !body) return;
    if (!threads.length) { block.style.display = "none"; return; }
    block.style.display = "block";

    const coord       = data.coord || {};
    const instruments = data.instruments || [];

    // Цвет статуса потока
    const statusColor = {
      "ожидание сигнала":                      "#9fb3d8",
      "сканирование":                          "#4c8dff",
      "в позиции":                             "#2ecc71",
      "ожидание — другая стратегия в позиции": "#f0c04a",
      "остановлен": "#555", "бот выключен": "#555", "не запущен": "#555",
    };
    const threadStatus = {};
    threads.forEach(t => { threadStatus[t.strategy_id] = t; });

    const fmtStat = (st, f) => {
      if (!st || !st.trades) return `<span class="muted">—</span>`;
      if (f === "pnl") { const v = st.pnl||0; const c = v>=0?"#2fa36b":"#ff7b7b"; return `<span style="color:${c};font-weight:600">${v>=0?"+":""}${esc(st.pnl_ui)}</span>`; }
      if (f === "wr")  return `${st.win_rate??0}%`;
      if (f === "cnt") return `${st.trades}`;
    };

    // ── Метки дат для заголовков ──────────────────────────────────────────
    const _now = new Date();
    const _pad = n => String(n).padStart(2, "0");
    const _fmt = d => `${_pad(d.getDate())}.${_pad(d.getMonth()+1)}`;
    const _dayLabel   = _fmt(_now);                                   // 02.06
    const _wdIdx = _now.getDay() === 0 ? 6 : _now.getDay() - 1;     // 0=пн
    const _wkSt = new Date(_now); _wkSt.setDate(_now.getDate() - _wdIdx);
    const _wkEn = new Date(_wkSt); _wkEn.setDate(_wkSt.getDate() + 6);
    const _weekLabel  = `${_fmt(_wkSt)}–${_fmt(_wkEn)}`;             // 26.05–01.06
    const _months = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                     "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"];
    const _monthLabel = _months[_now.getMonth()];                     // Июнь

    // ── Таблица стратегий: сортировка по дневному PnL (убывание) ─────────
    const sortedThreads = [...threads].sort((a,b) => {
      const pa = (a.stats?.day?.pnl) || 0;
      const pb = (b.stats?.day?.pnl) || 0;
      return pb - pa;
    });
    // Считаем итоги по всем стратегиям
    let totDay = 0, totWeek = 0, totMonth = 0, totCnt = 0, totWins = 0, totStops = 0;
    sortedThreads.forEach(t => {
      const s = t.stats || {};
      totDay   += (s.day?.pnl)   || 0;
      totWeek  += (s.week?.pnl)  || 0;
      totMonth += (s.month?.pnl) || 0;
      totCnt   += (s.month?.trades) || 0;
      totWins  += Math.round(((s.month?.win_rate||0) / 100) * (s.month?.trades||0));
      totStops += (t.loss_stops_month) || 0;
    });
    const totWR = totCnt > 0 ? Math.round(totWins / totCnt * 100 * 10) / 10 : 0;
    const fmtTot = (v) => {
      if (v === 0) return `<span class="muted">—</span>`;
      const c = v >= 0 ? "#2fa36b" : "#ff7b7b";
      return `<span style="color:${c};font-weight:700">${v >= 0 ? "+" : ""}${v.toFixed(2)}</span>`;
    };

    const stratRows = sortedThreads.map(t => {
      const col  = statusColor[t.status] || "#eef4ff";
      const tick = t.ticker ? ` · ${esc(t.ticker)}` : "";
      const s    = t.stats || {};
      return `<tr>
        <td><b>${esc(t.name)}</b></td>
        <td><span style="display:inline-flex;align-items:center;gap:5px">
          <span style="width:7px;height:7px;border-radius:50%;background:${col};flex-shrink:0"></span>
          <span style="color:${col};font-size:12px">${esc(t.status)}${tick}</span>
        </span></td>
        <td>${fmtStat(s.day,"pnl")}</td><td>${fmtStat(s.week,"pnl")}</td><td>${fmtStat(s.month,"pnl")}</td>
        <td class="muted">${fmtStat(s.month,"wr")}</td>
        <td class="muted">${fmtStat(s.month,"cnt")}</td>
        <td class="muted" style="font-size:12px">${
          (t.loss_stops_month > 0)
            ? `<span style="color:#ff7b7b;font-weight:600">${t.loss_stops_month}</span>`
            : `<span class="muted">0</span>`
        }</td>
        <td class="muted" style="font-size:11px">${esc(t.updated_at||"")}</td>
      </tr>`;
    }).join("") + `<tr style="border-top:2px solid rgba(255,255,255,.15);background:rgba(255,255,255,.03)">
      <td colspan="2" style="font-weight:700;font-size:13px;padding:6px 8px">ИТОГО</td>
      <td>${fmtTot(totDay)}</td>
      <td>${fmtTot(totWeek)}</td>
      <td>${fmtTot(totMonth)}</td>
      <td class="muted">${totCnt > 0 ? totWR + "%" : "—"}</td>
      <td class="muted">${totCnt || "—"}</td>
      <td class="muted" style="font-size:12px">${totStops > 0 ? `<span style="color:#ff7b7b;font-weight:600">${totStops}</span>` : "0"}</td>
      <td></td>
    </tr>`;

    // ── Инструменты: сортировка по score сигнала (выше score → выше в таблице) ─
    const sorted = [...instruments].sort((a,b) => (b.signal_score || 0) - (a.signal_score || 0));
    const n      = sorted.length;

    // Градиент по score: высокий score (сверху) → зелёный, нулевой (снизу) → нейтральный
    const maxScore = Math.max(...sorted.map(i => i.signal_score || 0), 1);
    const rowBg  = (idx) => {
      const score = sorted[idx]?.signal_score || 0;
      if (score <= 0 || n < 2) return "";
      const t = 1 - score / maxScore;  // 0 = top score (green), 1 = lowest score
      const r = Math.round(47  + (100-47)  * t);
      const g = Math.round(163 + (120-163) * t);
      const b = Math.round(107 + (80-107)  * t);
      return `background:rgba(${r},${g},${b},.09)`;
    };

    const instrRows = sorted.map((i, idx) => {
      const action = i.signal_action || "—";
      const score  = i.signal_score  || 0;
      const sigColor = action === "BUY"  ? "#2fa36b"
                     : action === "SELL" ? "#ff7b7b"
                     : "#9fb3d8";
      const sigLabel = action === "HOLD" ? "HOLD" : action;
      const isCoordOwner = coord.owner_figi && coord.owner_figi === i.figi;
      const tickerCell = isCoordOwner
        ? `<b>${esc(i.ticker)}</b> <span style="color:#f5a623;font-size:10px">&#9679; позиция</span>`
        : `<b>${esc(i.ticker)}</b>`;
      // Ячейка дневного лимита потерь
      const maxLoss = i.max_daily_loss_rub || 0;
      const dailyPnl = i.daily_pnl || 0;
      const blocked  = i.is_loss_blocked;
      const blockCnt = i.loss_block_count_month || 0;
      const pnlColor = dailyPnl >= 0 ? "#2fa36b" : "#ff7b7b";
      const pnlFmt   = dailyPnl !== 0 ? (dailyPnl >= 0 ? "+" : "") + dailyPnl.toFixed(0) + " ₽" : "";
      let lossCell;
      if (maxLoss > 0) {
        const limitFmt = maxLoss.toLocaleString("ru-RU");
        lossCell = `
          <div style="font-size:11px;line-height:1.5">
            ${blocked
              ? `<span style="background:rgba(191,77,90,.2);color:#ff7b7b;border:1px solid #bf4d5a;border-radius:3px;padding:1px 5px;font-size:10px;font-weight:700">⛔ СТОП</span>`
              : `<span class="muted" style="font-size:10px">Лимит: −${limitFmt} ₽</span>`
            }
            ${pnlFmt ? `<div><span style="color:${pnlColor}">${pnlFmt}</span> сегодня</div>` : ""}
            ${blockCnt > 0 ? `<div class="muted" style="font-size:10px">Стопов: ${blockCnt}×</div>` : ""}
          </div>`;
      } else if (pnlFmt) {
        // Нет лимита, но есть дневной PnL — показываем только его
        lossCell = `<span style="color:${pnlColor};font-size:11px">${pnlFmt}</span><span class="muted" style="font-size:10px"> сег.</span>`;
      } else {
        lossCell = '<span class="muted" style="font-size:11px">—</span>';
      }
      // Данные сигнала в data-атрибутах — обновляются отдельно без diffTbody
      return `<tr data-figi="${esc(i.figi)}"
                  data-sig-action="${esc(action)}"
                  data-sig-score="${score}"
                  data-sig-skip="${esc(i.signal_skip_reason||"")}"
                  data-sig-filter="${esc(i.signal_skip_filter||"")}"
                  data-sig-mode="${esc(i.signal_mode||"")}"
                  data-sig-reasons="${esc((i.signal_reasons||[]).join("||"))}"
                  style="${rowBg(idx)}${blocked ? ';opacity:.7' : ''}">
        <td>${tickerCell}</td>
        <td>${esc(i.lots)} <span class="muted" style="font-size:10px">(${esc(i.lot_cost_ui || "")})</span></td>
        <td class="muted" style="white-space:nowrap">
          <span style="color:#ff7b7b;font-size:11px">▼${esc(i.sl_pct)}</span>
          <span style="color:#888;margin:0 2px">/</span>
          <span style="color:#2fa36b;font-size:11px">▲${esc(i.tp_pct)}</span>
        </td>
        <td class="live-price" data-figi="${esc(i.figi)}" style="font-size:13px;font-weight:600">—</td>
        <td class="live-time muted" data-figi="${esc(i.figi)}" style="font-size:11px">—</td>
        <td class="live-vol muted" data-figi="${esc(i.figi)}" style="font-size:12px">—</td>
        <td>${lossCell}</td>
        <td class="live-sig" data-figi="${esc(i.figi)}" style="cursor:pointer" title="Нажмите для расшифровки сигнала">—</td>
      </tr>`;
    }).join("");

    // ── Инициализация структуры тела (один раз) ──────────────────────────────
    if (!body.dataset.built) {
      body.dataset.built = "1";
      body.innerHTML = `
        <div class="table-wrap" style="margin-bottom:12px">
          <table><thead><tr>
            <th>Стратегия</th><th>Статус</th>
            <th id="_psThDay">PnL день</th><th id="_psThWeek">PnL нед.</th><th id="_psThMonth">PnL мес.</th>
            <th id="_psThWrMonth">Win% мес.</th><th id="_psThCntMonth">Сделок мес.</th><th>Стопов мес.</th><th>Обновлено</th>
          </tr></thead><tbody id="_psStratBody"></tbody></table>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin:6px 0 4px">
          <span class="live-dot" id="_psLiveDot"></span>
          <span style="font-size:11px;color:#9fb3d8" id="_psUpdTime"></span>
        </div>
        <div class="table-wrap">
          <table><thead><tr>
            <th>Тикер</th><th>Лоты (стоимость)</th><th>SL/TP</th>
            <th>Цена</th><th>Обновлено</th><th>Объём 1м</th><th>Лимит/день</th><th>Сигнал</th>
          </tr></thead><tbody id="_psInstrBody"></tbody></table>
        </div>
        <div id="_psCoordNote"></div>`;
    }

    // ── Заголовки с датами (обновляются каждый refresh) ──────────────────────
    const _thDay = document.getElementById('_psThDay');
    const _thWk  = document.getElementById('_psThWeek');
    const _thMo  = document.getElementById('_psThMonth');
    const _thWr  = document.getElementById('_psThWrMonth');
    const _thCnt = document.getElementById('_psThCntMonth');
    if (_thDay)  _thDay.innerHTML  = `PnL день<br><span class="muted" style="font-size:10px;font-weight:400">${_dayLabel}</span>`;
    if (_thWk)   _thWk.innerHTML   = `PnL нед.<br><span class="muted" style="font-size:10px;font-weight:400">${_weekLabel}</span>`;
    if (_thMo)   _thMo.innerHTML   = `PnL мес.<br><span class="muted" style="font-size:10px;font-weight:400">${_monthLabel}</span>`;
    if (_thWr)   _thWr.innerHTML   = `Win%<br><span class="muted" style="font-size:10px;font-weight:400">${_monthLabel}</span>`;
    if (_thCnt)  _thCnt.innerHTML  = `Сделок<br><span class="muted" style="font-size:10px;font-weight:400">${_monthLabel}</span>`;

    // ── Стратегии (diff) ──────────────────────────────────────────────────────
    diffTbody(document.getElementById('_psStratBody'), stratRows);

    // ── Инструменты: тихий diff, потом обновляем live-ячейки ────────────────
    const instrTbody = document.getElementById('_psInstrBody');
    if (instrTbody) {
      const hadRows = instrTbody.querySelector('tr[data-figi]') !== null;
      diffTbody(instrTbody, instrRows);
      // Привязываем клик на .live-sig только для новых строк
      if (!hadRows) {
        instrTbody.addEventListener("click", (e) => {
          const sigEl = e.target.closest(".live-sig");
          if (!sigEl) return;
          const tr = sigEl.closest("tr[data-figi]");
          if (tr) _showSignalPopup(sigEl, tr);
        });
      }

      // Восстанавливаем цены из кэша (иначе будут "—" после diffTbody)
      if (Object.keys(_lastQuotesMap).length) {
        _applyQuotesToLiveCells(_lastQuotesMap);
      }

      // Обновляем сигнал и объём из data-атрибутов строки
      instrTbody.querySelectorAll('tr[data-figi]').forEach(tr => {
        const figi     = tr.dataset.figi;
        const instr    = instruments.find(x => x.figi === figi);
        if (!instr) return;

        // Объём (без flash)
        const volEl = tr.querySelector('.live-vol');
        if (volEl) volEl.textContent = instr.volume_ui || "—";

        // Сигнал — обновляем только при реальном изменении action/score/skip
        const sigEl = tr.querySelector('.live-sig');
        if (!sigEl) return;
        const newAction = tr.dataset.sigAction || "—";
        const newScore  = parseInt(tr.dataset.sigScore  || "0") || 0;
        const newSkip   = tr.dataset.sigSkip   || "";
        const newFilter = tr.dataset.sigFilter  || "";
        const prevScore  = parseInt(sigEl.dataset.score  !== undefined ? sigEl.dataset.score  : "-999");
        const prevAction = sigEl.dataset.action || "";
        const firstRender = sigEl.dataset.score === undefined;
        // Пропускаем если ничего не изменилось — НЕТ flash, НЕТ обновления DOM
        if (!firstRender && prevScore === newScore && prevAction === newAction && sigEl.dataset.skip === newSkip) return;
        const sigC = newAction === "BUY" ? "#2fa36b" : newAction === "SELL" ? "#ff7b7b" : "#9fb3d8";
        // Всё в одну строку — никаких div, высота строки не меняется
        sigEl.innerHTML = `<span style="color:${sigC};font-weight:700;font-size:12px">${esc(newAction)}</span>`
          + (newScore ? ` <span class="muted" style="font-size:11px">${newScore > 0 ? "+" : ""}${newScore}</span>` : "")
          + (newSkip  ? ` <span style="color:#f0a500;font-size:11px" title="${esc(newSkip)}">⚠</span>` : "");
        sigEl.dataset.score  = String(newScore);
        sigEl.dataset.action = newAction;
        sigEl.dataset.skip   = newSkip;
        // Flash только при значимых изменениях (не при первом рендере)
        if (!firstRender && newScore !== prevScore && newScore !== 0) {
          _flashCell(sigEl, newScore > prevScore ? 'up' : 'down');
        } else if (!firstRender && prevAction !== newAction && (newAction === "BUY" || newAction === "SELL")) {
          _flashCell(sigEl, newAction === "BUY" ? 'up' : 'down');
        }
      });
    }

    // ── Синяя волна строки при смене action BUY/SELL ────────────────────────
    instruments.forEach(i => {
      const prev = _prevSignals[i.figi];
      const newAction = i.signal_action || "—";
      const actionChanged = prev && prev.action !== newAction && newAction !== "HOLD" && newAction !== "—";
      _prevSignals[i.figi] = { action: newAction };
      if (actionChanged && instrTbody) {
        const tr = instrTbody.querySelector(`tr[data-figi="${i.figi}"]`);
        if (tr) { tr.classList.remove('sig-flash'); void tr.offsetWidth; tr.classList.add('sig-flash'); }
      }
    });

    // ── Заметка о координаторе позиции ────────────────────────────────────────
    const coordNote = document.getElementById('_psCoordNote');
    if (coordNote) {
      coordNote.innerHTML = coord.owner_strategy_id != null
        ? `<div class="note" style="margin-top:6px;color:#f5a623">&#9679; Открыта позиция по ${esc(coord.owner_ticker || coord.owner_figi || "?")} — новые ордера заблокированы</div>`
        : "";
    }

    // ── Live-dot + "обновлено X сек назад" ────────────────────────────────────
    const now = Date.now();
    _psLastUpdate = now;
    const dot = document.getElementById('_psLiveDot');
    if (dot) { dot.classList.remove('stale'); }

    // ── Графики: только если список инструментов изменился ────────────────────
    const newFigis = instruments.map(i => i.figi).join(',');
    if (newFigis !== (_psLastFigis || "")) {
      _psLastFigis = newFigis;
      _mainChartFigis = instruments.map(i => ({figi: i.figi, ticker: i.ticker}));
      await _renderMainCharts();
    } else {
      _mainChartFigis = instruments.map(i => ({figi: i.figi, ticker: i.ticker}));
    }
  } catch (e) { console.error("refreshParallelStatus:", e); }
}

async function mainChartsApplySettings() {
  _mainChartInterval = document.getElementById("mcInterval")?.value || "1min";
  _mainChartHours    = parseInt(document.getElementById("mcHours")?.value || "4");
  await _renderMainCharts();
}

async function _renderMainCharts() {
  const grid = document.getElementById("mainChartsGrid");
  if (!grid || !window.Plotly || !_mainChartFigis.length) {
    if (grid) grid.innerHTML = "";
    return;
  }
  const figis = _mainChartFigis; // показываем ВСЕ, без ограничения в 10

  // Плейсхолдеры
  grid.innerHTML = figis.map(f => `
    <div class="block" style="padding:8px;margin-bottom:0">
      <div class="row between" style="margin-bottom:4px">
        <span style="font-size:12px;font-weight:700">${esc(f.ticker)}</span>
        <span class="note" style="font-size:10px" id="mc-price-${f.figi}"></span>
      </div>
      <div id="mc-${f.figi}" style="height:120px"></div>
    </div>`).join("");

  try {
    const url = `/api/dashboard/multi-candles?figis=${figis.map(f=>f.figi).join(",")}&interval=${_mainChartInterval}&hours=${_mainChartHours}`;
    const resp = await apiGet(url);

    for (const {figi, ticker} of figis) {
      const el  = document.getElementById(`mc-${figi}`);
      if (!el) continue;
      const item    = resp[figi] || {};
      const candles = item.candles || [];
      if (!candles.length) {
        Plotly.newPlot(el, [], {
          paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)", font:{color:"#eef4ff"},
          margin:{t:0,r:0,b:20,l:40},
          annotations:[{text:"Нет данных",xref:"paper",yref:"paper",x:.5,y:.5,showarrow:false,font:{size:12,color:"#9fb3d8"}}],
        }, {displayModeBar:false, responsive:true});
        continue;
      }
      // Последняя цена в заголовок
      const lastC = candles[candles.length-1];
      const priceEl = document.getElementById(`mc-price-${figi}`);
      if (priceEl && lastC) priceEl.textContent = lastC.close?.toFixed(2) ?? "";

      Plotly.newPlot(el, [{
        x:    candles.map(c => c.time),
        open:  candles.map(c => c.open),
        high:  candles.map(c => c.high),
        low:   candles.map(c => c.low),
        close: candles.map(c => c.close),
        type: "candlestick",
        increasing: {line:{color:"#2ecc71"}, fillcolor:"rgba(46,204,113,.7)"},
        decreasing: {line:{color:"#ff5c5c"}, fillcolor:"rgba(255,92,92,.7)"},
        hoverinfo: "x+y",
      }], {
        paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
        font:{color:"#eef4ff",size:10},
        margin:{t:0,r:8,b:24,l:44},
        xaxis:{rangeslider:{visible:false}, gridcolor:"rgba(255,255,255,.05)", tickformat:"%H:%M", tickfont:{size:9}},
        yaxis:{gridcolor:"rgba(255,255,255,.05)", tickfont:{size:9}},
        hoverlabel:{bgcolor:"rgba(14,27,52,.95)", bordercolor:"rgba(76,141,255,.5)", font:{color:"#eef4ff",size:11}},
      }, {displayModeBar:false, responsive:true, scrollZoom:false});
    }
  } catch(e) { console.error("multi-candles:", e); }
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

let _backtestStrategies  = [];
let _backtestLastResult  = null;
let _btPollTimer         = null;

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
    _backtestStrategies = await apiGet("/api/backtest/strategies");
  } catch {
    _backtestStrategies = [];
  }

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
      <div class="note" style="margin-bottom:12px">Инструмент и объём берутся из настроек каждой стратегии автоматически</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:20px">
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

      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:4px">
        <button class="btn btn-primary" id="btBtnStart" onclick="runBacktest()">▶ Запустить бэктест</button>
        <button class="btn" id="btBtnStop" onclick="stopBacktest()" style="display:none">⏹ Остановить</button>
        <span id="btStatus" class="note"></span>
      </div>
      <div id="btProgressBlock" style="display:none;margin-top:10px">
        <div style="background:rgba(255,255,255,.08);border-radius:6px;height:8px;overflow:hidden;margin-bottom:6px">
          <div id="btProgressBar" style="height:100%;background:#4c8dff;width:0%;transition:width .3s"></div>
        </div>
        <div id="btProgressLabel" class="note" style="font-size:11px"></div>
        <div id="btProgressCurrent" class="note" style="font-size:11px;color:#a8c8ff"></div>
      </div>
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
  const interval    = (document.getElementById("btInterval") || {}).value || "15min";
  const days        = parseInt((document.getElementById("btDays") || {}).value || "7");
  const strategyIds = Array.from(document.querySelectorAll(".bt-strat-cb:checked"))
                          .map(cb => parseInt(cb.value));

  if (!strategyIds.length) { showToast("Отметьте хотя бы одну стратегию", "error"); return; }

  try {
    await apiPostJson("/api/backtest/start", { interval, days, strategy_ids: strategyIds });
  } catch (e) {
    showToast("Ошибка запуска: " + e.message, "error"); return;
  }

  _btSetRunning(true);
  document.getElementById("btResults").style.display = "none";
  if (_btPollTimer) clearInterval(_btPollTimer);
  _btPollTimer = setInterval(_btPoll, 1200);
}

async function stopBacktest() {
  if (_btPollTimer) { clearInterval(_btPollTimer); _btPollTimer = null; }
  _btSetRunning(false);
}

function _btSetRunning(on) {
  const btnStart = document.getElementById("btBtnStart");
  const btnStop  = document.getElementById("btBtnStop");
  const prog     = document.getElementById("btProgressBlock");
  if (btnStart) btnStart.style.display = on ? "none" : "";
  if (btnStop)  btnStop.style.display  = on ? "" : "none";
  if (prog)     prog.style.display     = on ? "block" : "none";
  if (!on) {
    const bar = document.getElementById("btProgressBar");
    if (bar) bar.style.width = "0%";
  }
}

async function _btPoll() {
  try {
    const s = await apiGet("/api/backtest/status");
    const bar     = document.getElementById("btProgressBar");
    const label   = document.getElementById("btProgressLabel");
    const current = document.getElementById("btProgressCurrent");
    const status  = document.getElementById("btStatus");

    if (s.total > 0 && bar) {
      const pct = Math.round(s.progress / s.total * 100);
      bar.style.width = pct + "%";
      if (label) label.textContent = `${s.progress} из ${s.total} стратегий (${pct}%)`;
    }
    if (current) current.textContent = s.current || "";
    if (status)  status.textContent  = s.status === "running" ? "Выполняется…" : "";

    if (s.status === "done") {
      clearInterval(_btPollTimer); _btPollTimer = null;
      _btSetRunning(false);
      if (status) status.textContent = "Готово";
      const data = await apiGet("/api/backtest/result");
      _backtestLastResult = data;
      _renderBacktestResults(data);
      document.getElementById("btResults").style.display = "block";
    } else if (s.status === "error") {
      clearInterval(_btPollTimer); _btPollTimer = null;
      _btSetRunning(false);
      showToast("Ошибка бэктеста: " + (s.error || "неизвестно"), "error", 8000);
    }
  } catch (e) {
    clearInterval(_btPollTimer); _btPollTimer = null;
    _btSetRunning(false);
    showToast("Ошибка опроса: " + e.message, "error");
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
      const tickerBadge = r.ticker ? `<span class="badge badge-active" style="font-size:11px;margin-left:4px">${esc(r.ticker)}</span>` : "";
      const lotsInfo = r.qty_ui ? ` · ${esc(r.qty_ui)}` : (r.lots ? ` · ${r.lots} лот` : "");
      const mode = r.mode ? `<div class="note" style="font-weight:400;font-size:11px">${esc(r.mode)} · SL ${esc(r.sl_pct_ui || "")} · TP ${esc(r.tp_pct_ui || "")}${lotsInfo}</div>` : "";
      return `<th style="min-width:160px">${esc(name)}${tickerBadge}${mode}</th>`;
    }).join("");
  }

  // ── Metrics rows ──
  const metrics = [
    ["Свечей",              r => String(r.candles_tested ?? "—")],
    ["Объём позиции",       r => r.qty_ui || (r.lots != null ? `${r.lots} лот` : "1 лот")],
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
let _analystMode = "search"; // "search" | "optimize"
let _analystOptPollTimer = null;

// ── ML Learning Tab ────────────────────────────────────────────────────────────

async function renderLearningTab() {
  const host = document.getElementById("view-обучение");
  if (!host) return;

  host.innerHTML = `<div style="text-align:center;padding:40px;color:#9fb3d8">Загрузка данных обучения…</div>`;

  try {
    const data = await apiGet("/api/ml/summary");
    const states  = data.states  || [];
    const logData = data.log     || [];
    const totalTrades = data.total_trades || 0;
    const avgConf     = data.avg_confidence || 0;
    const lastReb     = data.last_rebalance || "никогда";

    const confColor = (c) => c >= 0.65 ? "#2fa36b" : c >= 0.3 ? "#f0c04a" : "#9fb3d8";
    const fmtQ = (q) => {
      const c = q >= 0 ? "#2fa36b" : "#ff7b7b";
      return `<span style="color:${c};font-weight:600">${q >= 0 ? "+" : ""}${Number(q).toFixed(2)}</span>`;
    };

    const instrRows = states.map(s => {
      const conf = s.confidence || 0;
      const wr   = ((s.win_rate || 0) * 100).toFixed(1);
      const mode = s.ml_strategy_mode || "—";
      const sl   = s.ml_stop_loss_pct   ? (s.ml_stop_loss_pct   * 100).toFixed(2) + "%" : "—";
      const tp   = s.ml_take_profit_pct ? (s.ml_take_profit_pct * 100).toFixed(2) + "%" : "—";
      const sc   = s.ml_min_score || "—";
      return `<tr>
        <td><b>${esc(s.ticker)}</b></td>
        <td class="muted" style="font-size:12px">${esc(mode)}</td>
        <td style="font-size:12px">${sl}</td>
        <td style="font-size:12px">${tp}</td>
        <td class="muted" style="font-size:12px">${sc}</td>
        <td style="font-size:12px">${wr}% (${s.wins||0}/${s.trades_count||0})</td>
        <td>${fmtQ(s.quality_score || 0)}</td>
        <td>
          <div style="width:80px;height:6px;background:rgba(255,255,255,.1);border-radius:3px">
            <div style="width:${Math.round(conf*100)}%;height:100%;background:${confColor(conf)};border-radius:3px"></div>
          </div>
          <span class="muted" style="font-size:10px">${Math.round(conf*100)}%</span>
        </td>
      </tr>`;
    }).join("") || `<tr><td colspan="8" class="muted" style="text-align:center;padding:20px">Данных пока нет — модель начнёт учиться после первых сделок</td></tr>`;

    const logRows = logData.map(l => `<tr>
      <td class="muted" style="font-size:11px">${esc((l.timestamp||"").slice(0,16))}</td>
      <td><b>${esc(l.ticker)}</b></td>
      <td class="muted" style="font-size:12px">${esc(l.param_changed)}</td>
      <td style="font-size:12px">${esc(l.value_before)} → <b>${esc(l.value_after)}</b></td>
      <td class="muted" style="font-size:11px;max-width:300px">${esc(l.reason)}</td>
      <td style="font-size:12px">${fmtQ(l.quality_before || 0)} → ${fmtQ(l.quality_after || 0)}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted" style="text-align:center;padding:16px">История изменений пуста</td></tr>`;

    // Phase 2 данные
    const models    = data.trained_models || [];
    const decisions = data.decisions || [];
    const fStat     = data.features_stats || {};

    const univModel  = models.find(m => m.figi === "UNIVERSAL" || m.ticker === "ALL");
    const instrModels = models.filter(m => m.figi !== "UNIVERSAL" && m.ticker !== "ALL");
    const univActive = univModel?.status === "active";
    const activeModels = models.filter(m => m.status === "active").length;

    const fmtModel = (m) => {
      const ready = m.status === "active";
      let topFeatures = [];
      try {
        const imp = typeof m.feature_importance === "string"
          ? JSON.parse(m.feature_importance) : (m.feature_importance || {});
        topFeatures = Object.entries(imp)
          .sort((a, b) => b[1] - a[1]).slice(0, 5)
          .map(([name, val]) => ({ name, val: Number(val) }));
      } catch(e) {}
      const topHtml = topFeatures.length
        ? `<div style="margin-top:12px">
            <div class="label" style="font-size:11px;margin-bottom:4px">Топ-признаки</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              ${topFeatures.map(f => `
                <div style="font-size:11px;background:rgba(255,255,255,.06);padding:3px 8px;border-radius:10px">
                  ${esc(f.name)} <span style="color:#9fb3d8">${(f.val*100).toFixed(1)}%</span>
                </div>`).join("")}
            </div>
          </div>` : "";
      return `
        <div style="padding:10px 0">
          <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center">
            <div>
              <div class="label" style="font-size:11px">Статус</div>
              <div>${ready
                ? '<span style="color:#2fa36b;font-weight:700">✅ Активна — влияет на сделки</span>'
                : '<span style="color:#f0c04a">⏳ Учится — мягкий режим (precision &lt; 55%)</span>'}</div>
            </div>
            <div>
              <div class="label" style="font-size:11px">Обучено на</div>
              <div style="font-size:14px;font-weight:600">${m.n_training_samples || 0} сделок</div>
            </div>
            <div>
              <div class="label" style="font-size:11px">Precision</div>
              <div style="font-size:14px;font-weight:600;color:${m.precision_>=0.55?'#2fa36b':'#f0c04a'}">${m.precision_ ? (m.precision_*100).toFixed(1)+"%" : "—"}</div>
            </div>
            <div>
              <div class="label" style="font-size:11px">Accuracy</div>
              <div style="font-size:14px;font-weight:600">${m.accuracy ? (m.accuracy*100).toFixed(1)+"%" : "—"}</div>
            </div>
            <div>
              <div class="label" style="font-size:11px">Обновлена</div>
              <div class="muted" style="font-size:12px">${esc((m.trained_at||"").slice(0,16))}</div>
            </div>
          </div>
          ${topHtml}
        </div>`;
    };

    const univHtml = univModel
      ? fmtModel(univModel)
      : `<div class="muted" style="padding:16px;text-align:center">Нет данных — нужно 30+ сделок суммарно по всем инструментам</div>`;

    const instrModelRows = instrModels.slice(0,10).map(m => {
      const ready = m.status === "active";
      return `<tr>
        <td><b>${esc(m.ticker)}</b></td>
        <td>${ready
          ? '<span style="color:#2fa36b;font-weight:700">✅ Активна</span>'
          : '<span style="color:#f0c04a">⏳ Учится</span>'}</td>
        <td class="muted" style="font-size:12px">${m.n_training_samples || 0} сделок</td>
        <td style="font-size:12px">${m.precision_ ? (m.precision_*100).toFixed(1)+"%" : "—"}</td>
        <td style="font-size:12px">${m.accuracy   ? (m.accuracy   *100).toFixed(1)+"%" : "—"}</td>
        <td class="muted" style="font-size:11px">${esc((m.trained_at||"").slice(0,16))}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="6" class="muted" style="text-align:center;padding:12px">Нет</td></tr>`;

    const decisionRows = decisions.map(d => {
      const typeColor = d.decision_type.includes("block") ? "#ff7b7b"
                       : d.decision_type.includes("exit") ? "#f0c04a" : "#2fa36b";
      const typeLabel = {
        "entry_blocked":    "🚫 Вход заблокирован",
        "entry_allowed":    "✅ Вход разрешён",
        "entry_soft_block": "📊 Мягкий блок (учусь)",
        "entry_soft_allow": "📊 Мягкий вход (учусь)",
        "early_exit":       "⚡ Ранний выход",
        "early_exit_soft":  "📊 Мягкий выход (учусь)",
      }[d.decision_type] || d.decision_type;
      return `<tr>
        <td class="muted" style="font-size:11px">${esc((d.timestamp||"").slice(11,19))}</td>
        <td><b>${esc(d.ticker)}</b></td>
        <td style="font-size:12px;color:${typeColor}">${typeLabel}</td>
        <td style="font-size:12px">${d.model_confidence ? (d.model_confidence*100).toFixed(0)+"%" : "—"}</td>
        <td class="muted" style="font-size:11px;max-width:250px">${esc(d.reason||"")}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="5" class="muted" style="text-align:center;padding:12px">Решений пока нет</td></tr>`;

    host.innerHTML = `
      <!-- Сводка -->
      <div class="summary-grid" style="margin-bottom:18px">
        <div class="card"><div class="label">Признаков собрано</div><div class="value">${fStat.total||0}</div></div>
        <div class="card"><div class="label">Размечено сделок</div><div class="value">${fStat.labeled||0}</div></div>
        <div class="card"><div class="label">Универсальная модель</div><div class="value" style="color:${univActive?'#2fa36b':'#f0c04a'};font-size:13px">${univActive?"✅ Активна":"⏳ Учится"}</div></div>
        <div class="card"><div class="label">Решений принято</div><div class="value">${decisions.length}</div></div>
      </div>

      <!-- Статус + управление -->
      <div class="block" style="padding:10px 16px;margin-bottom:12px">
        <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
          <div style="font-size:13px">
            ${univActive
              ? `<span style="color:#2fa36b;font-weight:700">🟢 Модель активна — влияет на сделки</span>`
              : `<span style="color:#f0c04a">⏳ Накапливаю данные — нужно 30+ сделок суммарно по всем инструментам</span>`}
          </div>
          <button class="btn btn-primary" onclick="mlRebalance()">⚡ Запустить ребаланс</button>
        </div>
      </div>

      <!-- Универсальная ML-модель -->
      <section class="block" style="margin-bottom:14px">
        <h2 style="margin-bottom:6px">🤖 Универсальная ML-модель (GradientBoosting)</h2>
        <div class="muted" style="font-size:12px;margin-bottom:10px">Одна общая модель, обученная на всех инструментах. При достаточном числе сделок по конкретному тикеру — дополняется специализированной.</div>
        ${univHtml}
      </section>

      ${instrModels.length > 0 ? `
      <!-- Специализированные модели по инструментам -->
      <section class="block" style="margin-bottom:14px">
        <h2 style="margin-bottom:10px">📌 Специализированные модели по инструментам</h2>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Тикер</th><th>Статус</th><th>Обучено</th><th>Precision</th><th>Accuracy</th><th>Обновлена</th>
            </tr></thead>
            <tbody>${instrModelRows}</tbody>
          </table>
        </div>
      </section>` : ""}

      <!-- Лог решений -->
      <section class="block" style="margin-bottom:14px">
        <h2 style="margin-bottom:10px">📋 Последние решения ML</h2>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Время</th><th>Тикер</th><th>Решение</th><th>Уверенность</th><th>Причина</th>
            </tr></thead>
            <tbody>${decisionRows}</tbody>
          </table>
        </div>
      </section>

      <!-- Выученные параметры (Phase 1) -->
      <section class="block" style="margin-bottom:14px">
        <h2 style="margin-bottom:10px">📊 Оптимизированные параметры (Phase 1)</h2>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Тикер</th><th>Режим</th><th>SL</th><th>TP</th><th>Score</th>
              <th>Win% (W/T)</th><th>Quality</th><th>Уверенность</th>
            </tr></thead>
            <tbody>${instrRows}</tbody>
          </table>
        </div>
      </section>

      <!-- Лог оптимизаций (Phase 1) -->
      <section class="block">
        <h2 style="margin-bottom:10px">📜 История параметров</h2>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Время</th><th>Тикер</th><th>Параметр</th><th>Изменение</th><th>Причина</th><th>Quality</th>
            </tr></thead>
            <tbody>${logRows}</tbody>
          </table>
        </div>
      </section>
    `;
  } catch(e) {
    host.innerHTML = `<div class="banner-warning">Ошибка загрузки: ${esc(e.message)}</div>`;
  }
}

async function mlRebalance() {
  try {
    showToast("Запускаю ребаланс…", "info", 2000);
    await apiPost("/api/ml/rebalance");
    showToast("Ребаланс запущен в фоне. Обновите страницу через 30 секунд.", "success", 5000);
  } catch(e) {
    showToast("Ошибка: " + e.message, "error");
  }
}

async function renderAnalystTab() {
  const host = document.getElementById("view-analyst");
  if (!host) return;

  if (host.dataset.initialized !== "1") {
    host.innerHTML = `
      ${helpCard("Аналитик", [
        "<b>Поиск новых стратегий:</b> перебирает комбинации инструментов × режимов × SL × TP, запускает бэктест на каждой и сохраняет прибыльные. Поиск идёт в фоне.",
        "<b>Оптимизация текущих стратегий:</b> анализирует инструменты из активных параллельных стратегий профиля, находит лучшие режим/SL/TP/score и предлагает применить.",
        "<b>Score-оптимизация:</b> для каждой найденной комбинации аналитик автоматически подбирает оптимальный порог min_signal_score (0–60).",
        "<b>Важно:</b> бэктест не учитывает проскальзывание. Перед боевым запуском проверьте стратегию в Sandbox хотя бы 1–2 дня.",
      ])}

      <!-- Mode switcher -->
      <div style="display:flex;gap:8px;margin-bottom:16px">
        <button class="btn" id="anTabSearch" onclick="analystSwitchMode('search')"
          style="font-weight:600">Поиск новых</button>
        <button class="btn" id="anTabOptimize" onclick="analystSwitchMode('optimize')">Оптимизация текущих</button>
      </div>

      <!-- ── SEARCH MODE ── -->
      <div id="anModeSearch">
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
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
              <input type="checkbox" id="anExcludeActive" checked style="width:15px;height:15px">
              Исключить активные инструменты
            </label>
          </div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px">
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

        <!-- Текущие результаты поиска (активный прогон) -->
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
                    <th>PnL (₽)</th><th>Profit Factor</th><th>Drawdown</th><th>Min Score</th>
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
              <button class="btn btn-primary" onclick="analystSave()">Сохранить как стратегию</button>
            </div>
          </div>
        </div>

        <!-- История сессий оптимизации -->
        <div class="block" style="margin-top:20px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h2>История оптимизаций <span id="anSessionCount" class="note"></span></h2>
            <button class="btn" onclick="_analystLoadSessions()">Обновить</button>
          </div>
          <div id="anSessionsList">
            <p class="note">Загрузка…</p>
          </div>
        </div>
      </div>

      <!-- ── OPTIMIZE MODE ── -->
      <div id="anModeOptimize" style="display:none">
        <div class="block" style="margin-bottom:16px">
          <h2>Параметры оптимизации</h2>
          <p class="note" style="margin-bottom:12px">Анализирует инструменты из параллельных стратегий активного профиля. Подбирает оптимальные режим/SL/TP/score для каждого инструмента.</p>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:16px">
            <label class="field-label">Бюджет (₽)
              <input id="anOptBudget" class="field" type="number" min="1000" step="1000" value="60000">
            </label>
            <label class="field-label">Интервал свечей
              <select id="anOptInterval" class="field">
                <option value="5min">5 минут</option>
                <option value="15min" selected>15 минут</option>
                <option value="hour">1 час</option>
              </select>
            </label>
            <label class="field-label">Период бэктеста (дней)
              <select id="anOptDays" class="field">
                <option value="7">7 дней</option>
                <option value="14" selected>14 дней</option>
                <option value="30">30 дней</option>
              </select>
            </label>
          </div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <button class="btn btn-primary" id="anOptBtnStart" onclick="analystOptStart()">▶ Запустить оптимизацию</button>
            <button class="btn" id="anOptBtnStop" onclick="analystOptStop()" style="display:none">⏹ Остановить</button>
            <span id="anOptStatusText" class="note"></span>
          </div>
        </div>

        <div class="block" id="anOptProgressBlock" style="display:none;margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <b>Прогресс оптимизации</b>
            <span id="anOptProgressLabel" class="note"></span>
          </div>
          <div style="background:rgba(255,255,255,.08);border-radius:8px;height:12px;overflow:hidden">
            <div id="anOptProgressBar" style="height:100%;background:#2ecc71;border-radius:8px;width:0%;transition:width .4s"></div>
          </div>
          <div id="anOptCurrentCombo" class="note" style="margin-top:6px"></div>
        </div>

        <div id="anOptResultsBlock" style="display:none">
          <div class="block">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <h2>Результаты оптимизации <span id="anOptResultCount" class="note"></span></h2>
              <span class="note">Сортировка по улучшению PnL</span>
            </div>
            <div style="overflow-x:auto">
              <table class="table">
                <thead>
                  <tr>
                    <th>Инструмент</th><th>Стратегия</th>
                    <th>Текущий режим</th><th>Текущий SL/TP</th><th>Текущий PnL</th>
                    <th>Лучший режим</th><th>Лучший SL/TP</th><th>Лучший PnL</th>
                    <th>Улучшение</th><th>Min Score</th>
                    <th>Статус</th><th></th>
                  </tr>
                </thead>
                <tbody id="anOptResultsTbody"></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- ── ИЗВЕСТНЫЕ ИНСТРУМЕНТЫ ── -->
      <div class="block" style="margin-top:20px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h2>Известные инструменты <span id="anInstrCount" class="note"></span></h2>
          <button class="btn" onclick="_analystLoadInstruments()">Обновить</button>
        </div>
        <div style="overflow-x:auto">
          <table class="table" id="anInstrTable">
            <thead>
              <tr>
                <th>Тикер</th>
                <th>Название</th>
                <th>FIGI</th>
                <th>Instrument UID</th>
                <th>Лот</th>
              </tr>
            </thead>
            <tbody id="anInstrTbody">
              <tr><td colspan="5" class="note" style="text-align:center">Загрузка…</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    `;
    host.dataset.initialized = "1";
    _analystApplyModeStyle();
    _analystLoadInstruments();
    _analystLoadSessions();
  }

  if (_analystMode === "search") {
    await _analystRefresh();
  } else {
    await _analystOptRefresh();
  }
}

function _analystApplyModeStyle() {
  const btnSearch   = document.getElementById("anTabSearch");
  const btnOptimize = document.getElementById("anTabOptimize");
  const divSearch   = document.getElementById("anModeSearch");
  const divOptimize = document.getElementById("anModeOptimize");
  if (!btnSearch) return;
  const isSearch = _analystMode === "search";
  btnSearch.style.background   = isSearch   ? "var(--accent, #4c8dff)" : "";
  btnSearch.style.color        = isSearch   ? "#fff" : "";
  btnOptimize.style.background = !isSearch  ? "var(--accent, #4c8dff)" : "";
  btnOptimize.style.color      = !isSearch  ? "#fff" : "";
  if (divSearch)   divSearch.style.display   = isSearch  ? "" : "none";
  if (divOptimize) divOptimize.style.display = !isSearch ? "" : "none";
}

async function _analystLoadSessions() {
  const container = document.getElementById("anSessionsList");
  const countEl   = document.getElementById("anSessionCount");
  if (!container) return;
  container.innerHTML = '<p class="note">Загрузка…</p>';
  try {
    const sessions = await apiGet("/api/analyst/sessions?limit=30");
    if (countEl) countEl.textContent = `(${sessions.length})`;
    if (!sessions.length) {
      container.innerHTML = '<p class="note">История пуста. Запустите поиск или оптимизацию.</p>';
      return;
    }
    const STATUS_COLOR = {
      pending:  "#a8c8ff",
      accepted: "#2fa36b",
      partial:  "#f0a500",
      rejected: "#ff7b7b",
      running:  "#a8c8ff",
    };
    container.innerHTML = sessions.map(s => {
      const sc = STATUS_COLOR[s.status] || "#aaa";
      const isWeekly = s.type === "weekly";
      const weeklyInfo = isWeekly && s.weekly_trades > 0
        ? `<div class="note" style="margin-top:4px">
             Сделок: <b>${s.weekly_trades}</b> &nbsp;|&nbsp;
             PnL: <b style="color:${s.weekly_pnl>=0?'#2fa36b':'#ff7b7b'}">${s.weekly_pnl>=0?'+':''}${s.weekly_pnl.toFixed(2)} ₽</b> &nbsp;|&nbsp;
             Баланс: ${s.balance_start.toFixed(0)} → ${s.balance_end.toFixed(0)} ₽
             ${s.balance_start>0 ? `(<b>${((s.balance_end-s.balance_start)/s.balance_start*100).toFixed(2)}%</b>)` : ''}
           </div>` : '';

      const resultsHtml = s.results && s.results.length ? `
        <div style="overflow-x:auto;margin-top:10px">
          <table class="table" style="font-size:12px">
            <thead><tr>
              <th>Тикер</th><th>Стратегия</th>
              <th>Было</th><th>Предложение</th>
              <th>Улучшение</th><th>Статус</th><th></th>
            </tr></thead>
            <tbody>
              ${s.results.map(r => `<tr style="${r.applied?'opacity:.6':''}">
                <td><b>${esc(r.ticker)}</b></td>
                <td style="font-size:11px;max-width:120px;overflow:hidden">${esc((r.strategy_name||'').substring(0,22))}</td>
                <td class="mono" style="font-size:11px">${esc(r.base_mode_label)} ${esc(r.base_sl_ui)}/${esc(r.base_tp_ui)}</td>
                <td class="mono" style="font-size:11px;color:#a8c8ff">${esc(r.best_mode_label)} ${esc(r.best_sl_ui)}/${esc(r.best_tp_ui)}</td>
                <td style="color:${parseFloat(r.improvement_ui)>=0?'#2fa36b':'#ff7b7b'};font-weight:600">${esc(r.improvement_ui)}</td>
                <td style="font-size:11px">${r.applied ? '<span style="color:#2fa36b">✅ Применено</span>' : '<span class="note">Ожидает</span>'}</td>
                <td>
                  ${!r.applied ? `<button class="btn btn-small" onclick="_anApplyOne(${s.id},${r.id})">Применить</button>` : ''}
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${s.status === 'pending' ? `
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="btn btn-primary btn-small" onclick="_anApplySelected(${s.id})">✅ Применить выбранные</button>
          <button class="btn btn-small" style="color:#ff7b7b" onclick="_anRejectSession(${s.id})">❌ Отклонить</button>
        </div>` : ''}
      ` : '<p class="note" style="margin-top:6px">Нет предложений в этой сессии.</p>';

      return `
        <div class="block" style="margin-bottom:12px;padding:14px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:6px">
            <div>
              <span style="font-weight:600">${esc(s.type_label)}</span>
              <span class="note" style="margin-left:8px">${esc(s.created_at ? s.created_at.substring(0,16) : '')}</span>
              ${isWeekly ? `<span class="note" style="margin-left:6px">Нед. ${esc(s.week_start||'')} – ${esc(s.week_end||'')}</span>` : ''}
            </div>
            <div style="display:flex;gap:8px;align-items:center">
              <span style="font-size:12px;color:${sc};font-weight:600">${esc(s.status_label)}</span>
              <span class="note">${s.result_count} предл. / ${s.applied_count} прим.</span>
            </div>
          </div>
          ${weeklyInfo}
          ${s.notes ? `<div class="note" style="margin-top:4px;font-size:11px">${esc(s.notes)}</div>` : ''}
          <details style="margin-top:8px">
            <summary style="cursor:pointer;font-size:13px;color:rgba(255,255,255,.7)">Показать предложения (${s.result_count})</summary>
            ${resultsHtml}
          </details>
        </div>`;
    }).join("");
  } catch (e) {
    container.innerHTML = `<p class="note" style="color:#ff7b7b">Ошибка: ${esc(e.message)}</p>`;
  }
}

async function _anApplyOne(sessionId, resultId) {
  try {
    await apiPostJson(`/api/analyst/sessions/${sessionId}/apply`, { result_ids: [resultId] });
    showToast("Применено", "success");
    _analystLoadSessions();
  } catch(e) { showToast(`Ошибка: ${e.message}`, "error"); }
}

async function _anApplySelected(sessionId) {
  try {
    await apiPostJson(`/api/analyst/sessions/${sessionId}/apply`, { result_ids: [] });
    showToast("Все предложения сессии применены", "success");
    _analystLoadSessions();
  } catch(e) { showToast(`Ошибка: ${e.message}`, "error"); }
}

async function _anRejectSession(sessionId) {
  if (!confirm("Отклонить все предложения этой сессии?")) return;
  try {
    await apiPostJson(`/api/analyst/sessions/${sessionId}/reject`, {});
    showToast("Отклонено", "success");
    _analystLoadSessions();
  } catch(e) { showToast(`Ошибка: ${e.message}`, "error"); }
}

async function _analystLoadInstruments() {
  const tbody = document.getElementById("anInstrTbody");
  const count = document.getElementById("anInstrCount");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="note" style="text-align:center">Загрузка…</td></tr>`;
  try {
    const list = await apiGet("/api/analyst/instruments");
    if (count) count.textContent = `(${list.length})`;
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="note" style="text-align:center">Нет инструментов</td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(inst => {
      const uid = inst.instrument_uid || "";
      const uidShort = uid ? uid.substring(0, 8) + "…" : "—";
      const uidFull  = uid || "—";
      return `<tr>
        <td><b>${esc(inst.ticker)}</b></td>
        <td style="font-size:12px;color:rgba(255,255,255,.7)">${esc(inst.name)}</td>
        <td class="mono" style="font-size:12px;color:#a8c8ff;user-select:all">${esc(inst.figi)}</td>
        <td class="mono" style="font-size:11px;max-width:220px">
          <span title="${esc(uidFull)}" style="cursor:default">${esc(uidShort)}</span>
          ${uid ? `<button class="btn btn-small" style="margin-left:6px;padding:2px 6px;font-size:10px"
            onclick="navigator.clipboard.writeText('${esc(uid)}').then(()=>showToast('UID скопирован','success'))">
            Копировать</button>` : `<span class="note">не задан</span>`}
        </td>
        <td style="font-size:12px;text-align:center">${inst.lot}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="note" style="color:#ff7b7b">Ошибка: ${esc(e.message)}</td></tr>`;
  }
}

function analystSwitchMode(mode) {
  _analystMode = mode;
  _analystApplyModeStyle();
  if (mode === "search") {
    _analystRefresh();
  } else {
    _analystOptRefresh();
  }
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
    // Обновляем историю сессий при появлении результатов
    if (results.length) _analystLoadSessions();

    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="note">Нет результатов. Попробуйте снизить фильтры или увеличить период.</td></tr>`;
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
      const minScore = r.min_signal_score_used != null ? r.min_signal_score_used : 0;
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
        <td class="mono">${minScore}</td>
        <td>${saved}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    console.error("analyst results error", e);
  }
}

async function analystStart() {
  const budget       = parseFloat((document.getElementById("anBudget")    || {}).value || "60000");
  const interval     = (document.getElementById("anInterval")  || {}).value || "15min";
  const days         = parseInt((document.getElementById("anDays")      || {}).value || "14");
  const winRate      = parseFloat((document.getElementById("anWinRate")   || {}).value || "45");
  const minTrades    = parseInt((document.getElementById("anMinTrades") || {}).value || "5");
  const minPnl       = parseFloat((document.getElementById("anMinPnl")    || {}).value || "0");
  const excludeActive = !!(document.getElementById("anExcludeActive") || {}).checked;

  try {
    await apiPostJson("/api/analyst/start", { budget_rub: budget, min_win_rate: winRate, min_trades: minTrades, days, interval, min_pnl: minPnl, exclude_active: excludeActive });
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

async function analystOptStart() {
  const budget   = parseFloat((document.getElementById("anOptBudget")   || {}).value || "60000");
  const interval = (document.getElementById("anOptInterval") || {}).value || "15min";
  const days     = parseInt((document.getElementById("anOptDays")     || {}).value || "14");
  try {
    const res = await apiPostJson("/api/analyst/optimize-start", { budget_rub: budget, days, interval });
    if (res.ok) {
      showToast("Оптимизация запущена", "success");
      const block = document.getElementById("anOptResultsBlock");
      if (block) block.style.display = "none";
      await _analystOptRefresh();
    } else {
      showToast(res.message || "Ошибка", "error");
    }
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error", 6000);
  }
}

async function analystOptStop() {
  try {
    await fetch("/api/analyst/optimize-stop", { method: "POST", credentials: "same-origin" });
    showToast("Остановка оптимизации запрошена", "info");
  } catch {}
}

async function _analystOptRefresh() {
  try {
    const s = await apiGet("/api/analyst/optimize-status");
    _analystOptUpdateStatus(s);
    if (s.status === "running") {
      if (!_analystOptPollTimer) {
        _analystOptPollTimer = setInterval(async () => {
          if (getTabFromHash() !== "аналитик") { _analystOptStopPoll(); return; }
          try {
            const st = await apiGet("/api/analyst/optimize-status");
            _analystOptUpdateStatus(st);
            if (st.status !== "running") { _analystOptStopPoll(); await _analystOptLoadResults(); }
          } catch {}
        }, 2000);
      }
    } else {
      _analystOptStopPoll();
      if (s.status === "done" || s.status === "stopped") await _analystOptLoadResults();
    }
  } catch {}
}

function _analystOptStopPoll() {
  if (_analystOptPollTimer) { clearInterval(_analystOptPollTimer); _analystOptPollTimer = null; }
}

function _analystOptUpdateStatus(s) {
  const prog     = document.getElementById("anOptProgressBlock");
  const bar      = document.getElementById("anOptProgressBar");
  const label    = document.getElementById("anOptProgressLabel");
  const combo    = document.getElementById("anOptCurrentCombo");
  const txt      = document.getElementById("anOptStatusText");
  const btnStart = document.getElementById("anOptBtnStart");
  const btnStop  = document.getElementById("anOptBtnStop");

  const running = s.status === "running";
  if (prog)     prog.style.display     = running ? "block" : "none";
  if (btnStart) btnStart.style.display = running ? "none"  : "";
  if (btnStop)  btnStop.style.display  = running ? ""      : "none";

  if (s.total > 0 && bar) {
    const pct = Math.round(s.progress / s.total * 100);
    bar.style.width = pct + "%";
    if (label) label.textContent = `${s.progress} / ${s.total} (${pct}%) · улучшено: ${s.found}`;
  }
  if (combo) combo.textContent = s.current || "";

  const statusMap = {
    idle: "Ожидание", running: "Идёт оптимизация…", done: "Завершена",
    stopped: "Остановлена", error: "Ошибка",
  };
  if (txt) {
    txt.textContent = statusMap[s.status] || s.status;
    if (s.error) txt.textContent += ": " + s.error;
  }
}

async function _analystOptLoadResults() {
  try {
    const results = await apiGet("/api/analyst/optimize-results");
    const block = document.getElementById("anOptResultsBlock");
    const tbody = document.getElementById("anOptResultsTbody");
    const cnt   = document.getElementById("anOptResultCount");
    if (!tbody) return;
    if (block) block.style.display = results.length ? "block" : "none";
    if (cnt) cnt.textContent = `(${results.length})`;
    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="note">Нет результатов. Убедитесь что у профиля есть параллельные стратегии.</td></tr>`;
      return;
    }
    const modeLabels = {mean_reversion: "Возврат к средней", breakout: "Пробой", trend: "Тренд"};
    const modeColors = {mean_reversion: "#a8c8ff", breakout: "#2ecc71", trend: "#f0c04a"};
    tbody.innerHTML = results.map(r => {
      const impColor = r.improvement_pct > 0 ? "#2ecc71" : r.improvement_pct < 0 ? "#ff7b7b" : "#aaa";
      const applied = r.applied ? `<span class="badge badge-active">Применено</span>` :
        `<button class="btn btn-small btn-primary" onclick="analystOptApply(${r.id})">Применить</button>`;
      return `<tr>
        <td><b>${esc(r.ticker)}</b><div class="note" style="font-size:11px">${esc(r.instrument_name)}</div></td>
        <td style="font-size:12px">${esc(r.strategy_name)}</td>
        <td style="color:${modeColors[r.base_mode]||'#eef4ff'};font-size:12px">${esc(modeLabels[r.base_mode]||r.base_mode)}</td>
        <td class="mono" style="font-size:12px">${esc(r.base_sl_ui)} / ${esc(r.base_tp_ui)}</td>
        <td class="mono ${r.base_pnl>=0?'pnl-pos':'pnl-neg'}">${esc(r.base_pnl_ui)}</td>
        <td style="color:${modeColors[r.best_mode]||'#eef4ff'};font-size:12px">${esc(modeLabels[r.best_mode]||r.best_mode)}</td>
        <td class="mono" style="font-size:12px">${esc(r.best_sl_ui)} / ${esc(r.best_tp_ui)}</td>
        <td class="mono ${r.best_pnl>=0?'pnl-pos':'pnl-neg'}">${esc(r.best_pnl_ui)}</td>
        <td style="color:${impColor};font-weight:600">${esc(r.improvement_ui)}</td>
        <td class="mono">${r.best_min_signal_score}</td>
        <td>${applied}</td>
        <td></td>
      </tr>`;
    }).join("");
  } catch (e) {
    console.error("opt results error", e);
  }
}

async function analystOptApply(resultId) {
  if (!confirm("Применить найденные параметры к стратегии?")) return;
  try {
    await fetch(`/api/analyst/optimize-apply/${resultId}`, { method: "POST", credentials: "same-origin" });
    showToast("Параметры применены", "success");
    await _analystOptLoadResults();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`, "error");
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
      ["Мин. score сигнала", r.min_signal_score_used != null ? r.min_signal_score_used : 0],
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
    } else if (tab === "бэктест") {
      await renderBacktestTab();
    } else if (tab === "аналитик") {
      await renderAnalystTab();
    } else if (tab === "обучение") {
      await renderLearningTab();
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

  // ── Тикеры + сигналы (самое важное — 3 сек) ──────────────────────────────
  setInterval(async () => {
    try { if (getTabFromHash() === "главное") await refreshParallelStatus(); } catch {}
  }, REFRESH_PARALLEL_MS);

  // ── Summary cards — баланс, статус (4 сек) ──────────────────────────────
  setInterval(async () => {
    try { if (getTabFromHash() === "главное") await renderSummaryCards(); } catch {}
  }, REFRESH_SUMMARY_MS);

  // ── Цены в позициях (2 сек) ───────────────────────────────────────────────
  setInterval(async () => {
    try { await refreshQuotesOnly(); } catch {}
  }, REFRESH_QUOTES_MS);

  // ── Портфель (5 сек когда вкладка активна) ────────────────────────────────
  setInterval(async () => {
    try { if (getTabFromHash() === "портфель") await renderPortfolioTab(); } catch {}
  }, REFRESH_PORTFOLIO_MS);

  // ── История: только сделки за сегодня (8 сек когда вкладка активна) ───────
  setInterval(async () => {
    try {
      if (getTabFromHash() === "история") {
        await _histLoadStats();
        await _histLoadTradesAndLogs();
      }
    } catch {}
  }, REFRESH_TRADES_MS);

  // ── Новости (5 мин) ───────────────────────────────────────────────────────
  setInterval(() => {
    try { if (getTabFromHash() === "главное") _renderNews(); } catch {}
  }, 300000);

  // ── "Live dot" + счётчик "X сек назад" (каждую секунду) ──────────────────
  setInterval(() => {
    const el = document.getElementById('_psUpdTime');
    const dot = document.getElementById('_psLiveDot');
    if (!el) return;
    const age = _psLastUpdate ? Math.round((Date.now() - _psLastUpdate) / 1000) : null;
    if (age === null) { el.textContent = ""; return; }
    el.textContent = age < 5 ? "только что" : `${age} сек назад`;
    if (dot) {
      if (age > 10) dot.classList.add('stale');
      else dot.classList.remove('stale');
    }
  }, 1000);
}

async function bootstrapDashboard() {
  // Экспортируем все обработчики в глобальный scope ДО любых await,
  // чтобы кнопки работали даже если API-запросы упадут с ошибкой.
  window.serviceAction    = serviceAction;
  window.closeOnePosition = closeOnePosition;
  window.closeAllPositionsConfirm = closeAllPositionsConfirm;
  window.clearLocalPositions = clearLocalPositions;
  window.runBacktest = runBacktest;
  window.stopBacktest = stopBacktest;
  window._showBacktestTrades = _showBacktestTrades;
  window.mainChartsApplySettings = mainChartsApplySettings;
  window.histSetPeriod   = histSetPeriod;
  window.histShowTab     = histShowTab;
  window.histClearTrades = histClearTrades;
  window._histLoadBroker = _histLoadBroker;
  window.toggleParallel = toggleParallel;
  window.onParallelToggle = onParallelToggle;
  window.addParallelStrategy = addParallelStrategy;
  window.removeParallelStrategy = removeParallelStrategy;
  window.expandParallelStrategy = expandParallelStrategy;
  window.analystStart = analystStart;
  window.analystStop  = analystStop;
  window.analystDetail = analystDetail;
  window.analystSave  = analystSave;
  window.analystSwitchMode = analystSwitchMode;
  window.analystOptStart = analystOptStart;
  window.analystOptStop  = analystOptStop;
  window.analystOptApply = analystOptApply;
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
