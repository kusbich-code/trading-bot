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

function _crd(label, value) {
  const cls = summaryValueClass(value);
  return `<div class="crd"><div class="lbl">${label}</div><div class="val ${cls}" title="${esc(value ?? "—")}">${esc(value ?? "—")}</div></div>`;
}

async function renderSummaryCards() {
  const s = await apiGet("/api/dashboard/summary");
  const host = document.getElementById("summaryCards");
  if (!host) return;
  host.style.cssText = "display:block;margin-bottom:18px";
  const hasError = s.last_error && s.last_error !== "—";
  host.innerHTML = `<div class="sgroups">
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
  const host = document.getElementById("summaryCards");
  if (!host) return;
  host.style.display = getTabFromHash() === "главное" ? "grid" : "none";
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
    <div id="mainChartsGrid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:18px"></div>

    <!-- ── Позиции ── -->
    <section class="block">
      <div class="row between"><h2>Позиции <span class="note">(API брокера)</span></h2></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Тикер</th><th>Направление</th><th>Лотов</th><th>Вход</th><th>Тек.</th><th>ПнЛ</th><th>Действие</th></tr></thead>
          <tbody id="mainPositionsBody"></tbody>
        </table>
      </div>
    </section>

    <!-- ── Сделки ── -->
    <section class="block">
      <div class="row between"><h2>Сделки</h2><div class="note">Сегодня</div></div>
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
      return `<tr>
        <td><b>${esc(p.ticker)}</b></td>
        <td>${dirBadge}</td>
        <td>${esc(p.qty)}</td>
        <td>${esc(p.entry_price_ui)}</td>
        <td>${esc(p.current_price_ui)}</td>
        <td style="font-weight:700;color:${pnlColor}">${esc(p.unrealized_pnl_ui)}</td>
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
        <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.time)}</td>
        <td><b>${esc(t.ticker)}</b></td>
        <td>${badge}</td>
        <td>${esc(t.entry_ui)}</td><td>${esc(t.exit_ui)}</td>
        <td>${esc(t.qty)}</td>
        <td style="font-weight:700;color:${col}">${pnl >= 0 && pnl !== 0 ? "+" : ""}${esc(t.pnl_ui)}</td>
        <td class="muted" style="font-size:12px">${esc(t.reason)}</td>
      </tr>`;
    }).join("")
  );

  refreshParallelStatus();

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
            (${esc(c.lots)} лот × ${esc(c.lot_size)} шт × ${esc(c.price_ui)} ₽ + комиссия).
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

async function expandParallelStrategy(strategyId, mode = "instruments") {
  const panel = document.getElementById("parallelInstrExpanded");
  const title = document.getElementById("parallelInstrTitle");
  const body  = document.getElementById("parallelInstrBody");
  if (!panel || !body) return;

  // Toggle: close if same strategy + same mode already open
  if (panel.dataset.strategyId === String(strategyId) &&
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
          <div class="row-buttons">
            <button type="button" class="btn btn-primary" id="btnSaveParallelStrat">Сохранить настройки</button>
          </div>
        </form>`;
      body.querySelector("#btnSaveParallelStrat")?.addEventListener("click", () => saveStrategySettings(strategyId));

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

let _histPeriod    = 30;   // дней (0 = всё время)
let _histActiveTab = "trades";
let _histData      = {};
let _histBrokerItems  = [];
let _histBrokerCursor = "";

async function renderHistoryTab() {
  const host = document.getElementById("view-history");
  if (!host) return;

  host.innerHTML = _histShell();

  // Параллельная загрузка статистики и сделок/логов
  try {
    await Promise.all([_histLoadStats(), _histLoadTradesAndLogs()]);
  } catch (e) {
    console.error("history load:", e);
  }
}

function _histShell() {
  const periods = [{d:7,l:"7 дней"},{d:30,l:"30 дней"},{d:90,l:"90 дней"},{d:0,l:"Всё время"}];
  const tabs = [
    ["trades","Сделки"],["broker","Операции брокера"],
    ["journal","Журнал"],["errors","Ошибки"],["system","Система"],
  ];
  return `
    <div class="block" style="padding:10px 16px;margin-bottom:12px">
      <div class="row between">
        <div class="row" style="gap:6px">
          ${periods.map(p => `
            <button class="btn${_histPeriod===p.d?" btn-primary":""}" id="histPBtn${p.d}"
                    onclick="histSetPeriod(${p.d})">${p.l}</button>`).join("")}
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
  _histPeriod = days;
  document.querySelectorAll("[id^='histPBtn']").forEach(b => {
    const d = parseInt(b.id.replace("histPBtn",""));
    b.classList.toggle("btn-primary", d === days);
  });
  _histLoadStats();
  _histLoadTradesAndLogs();
  if (_histActiveTab === "broker") _histLoadBroker(true);
}

async function _histLoadStats() {
  try {
    document.getElementById("histStatus").textContent = "Загрузка…";
    const st = await apiGet(`/api/history/stats?days=${_histPeriod}`);
    _histRenderSummary(st.summary || {});
    _histRenderEquity(st.equity_curve || []);
    _histRenderTickerChart(st.by_ticker || []);
    _histRenderReasonChart(st.by_reason || {});
    const label = _histPeriod > 0 ? `${_histPeriod} дней` : "всё время";
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
  const tmplLine = "<b>%{customdata[0]}</b> %{customdata[1]}<br>" +
                   "Сделка: %{customdata[2]:+.2f} ₽<br>" +
                   "Накопл.: %{y:.2f} ₽<br>" +
                   "Причина: %{customdata[4]}<extra>Накопл. PnL</extra>";
  const tmplBar  = "<b>%{customdata[0]}</b> %{customdata[1]}<br>" +
                   "Сделка: %{y:+.2f} ₽<br>" +
                   "Накопл.: %{customdata[3]:.2f} ₽<br>" +
                   "Причина: %{customdata[4]}<extra>PnL сделки</extra>";
  Plotly.newPlot(el, [
    {x:times, y:cumPnl, type:"scatter", mode:"lines+markers", name:"Накопл. PnL",
     line:{color:"#4c8dff",width:2}, marker:{size:5, color:"#4c8dff"},
     fill:"tozeroy", fillcolor:"rgba(76,141,255,.07)",
     customdata:cd, hovertemplate:tmplLine},
    {x:times, y:perTrade, type:"bar", name:"PnL сделки", yaxis:"y2",
     marker:{color:barColors}, opacity:.85,
     customdata:cd, hovertemplate:tmplBar},
  ], {
    paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
    font:{color:"#eef4ff",size:11},
    margin:{t:10,r:60,b:60,l:70},
    legend:{orientation:"h",y:-0.25},
    hovermode:"closest",
    hoverlabel:{bgcolor:"#0e1b34", bordercolor:"#4c8dff",
                font:{color:"#ffffff",size:12}, align:"left"},
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
    const data = await apiGet(`/api/dashboard/history?days=${_histPeriod}`);
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
      <td class="muted" style="font-size:12px;white-space:nowrap">${esc(t.time)}</td>
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
          <th>Время</th><th>Тикер</th><th>Напр.</th>
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

let _mainChartInterval = "1min";
let _mainChartHours    = 4;
let _mainChartFigis    = [];  // [{figi, ticker}]

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

    // ── Таблица стратегий ─────────────────────────────────────────────────
    const stratRows = threads.map(t => {
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
        <td class="muted" style="font-size:11px">${esc(t.updated_at||"")}</td>
      </tr>`;
    }).join("");

    // ── Инструменты: сортировка по unrealized_pnl (прибыльные сверху) ─────
    const sorted = [...instruments].sort((a,b) => b.unrealized_pnl - a.unrealized_pnl);
    const n      = sorted.length;

    // Градиент: top → зелёный, bottom → красный (только если есть позиции)
    const hasPos = sorted.some(i => i.in_position);
    const rowBg  = (idx) => {
      if (!hasPos || n < 2) return "";
      const t = n === 1 ? 0.5 : idx / (n - 1);  // 0 = top (green), 1 = bottom (red)
      const r = Math.round(47  + (191-47)  * t);
      const g = Math.round(163 + (77-163)  * t);
      const b = Math.round(107 + (90-107)  * t);
      return `background:rgba(${r},${g},${b},.08)`;
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
      return `<tr style="${rowBg(idx)}">
        <td>${tickerCell}</td>
        <td>${esc(i.lots)} <span class="muted" style="font-size:10px">(${esc(i.lot_cost_ui || "")})</span></td>
        <td class="muted">${esc(i.sl_pct)}</td>
        <td class="muted">${esc(i.tp_pct)}</td>
        <td>${esc(i.last_price_ui)}</td>
        <td class="muted" style="font-size:11px">${esc(i.price_time)}</td>
        <td class="muted" style="font-size:12px">${esc(i.volume_ui)}</td>
        <td>
          <span style="color:${sigColor};font-weight:700;font-size:12px">${sigLabel}</span>
          ${score ? `<span class="muted" style="font-size:11px;margin-left:4px">${score > 0 ? "+" : ""}${score}</span>` : ""}
          ${i.signal_time ? `<span class="muted" style="font-size:10px;margin-left:4px">${esc(i.signal_time)}</span>` : ""}
        </td>
      </tr>`;
    }).join("");

    body.innerHTML = `
      <div class="table-wrap" style="margin-bottom:12px">
        <table><thead><tr>
          <th>Стратегия</th><th>Статус</th>
          <th>PnL день</th><th>PnL нед.</th><th>PnL мес.</th>
          <th>Win% мес.</th><th>Сделок мес.</th><th>Обновлено</th>
        </tr></thead><tbody>${stratRows}</tbody></table>
      </div>
      <div class="table-wrap">
        <table><thead><tr>
          <th>Тикер</th><th>Лоты (стоимость)</th><th>SL%</th><th>TP%</th>
          <th>Цена</th><th>Обновлено</th><th>Объём 1м</th><th>Сигнал</th>
        </tr></thead><tbody>${instrRows}</tbody></table>
      </div>
      ${coord.owner_strategy_id != null ? `<div class="note" style="margin-top:8px;color:#f5a623">
        &#9679; Открыта позиция по ${esc(coord.owner_ticker || coord.owner_figi || "?")} — новые ордера заблокированы до её закрытия
      </div>` : ""}`;

    // Обновляем figis для графиков
    _mainChartFigis = instruments.map(i => ({figi: i.figi, ticker: i.ticker}));
    await _renderMainCharts();
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
  const figis = _mainChartFigis.slice(0, 10);

  // Плейсхолдеры
  grid.innerHTML = figis.map(f => `
    <div class="block" style="padding:12px;margin-bottom:0">
      <div class="row between" style="margin-bottom:6px">
        <span style="font-size:13px;font-weight:700">${esc(f.ticker)}</span>
        <span class="note" style="font-size:11px" id="mc-price-${f.figi}"></span>
      </div>
      <div id="mc-${f.figi}" style="height:160px"></div>
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
      const lotsInfo = r.qty_ui ? ` · ${esc(r.qty_ui)}` : (r.lots ? ` · ${r.lots} лот` : "");
      const mode = r.mode ? `<div class="note" style="font-weight:400;font-size:11px">${esc(r.mode)} · SL ${esc(r.sl_pct_ui || "")} · TP ${esc(r.tp_pct_ui || "")}${lotsInfo}</div>` : "";
      return `<th style="min-width:140px">${esc(name)}${mode}</th>`;
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
    `;
    host.dataset.initialized = "1";
    _analystApplyModeStyle();
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
