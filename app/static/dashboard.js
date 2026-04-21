const ALLOWED_TABS = new Set(["главное", "портфель", "настройки", "история", "график"]);

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
  document.querySelectorAll("[data-tab-link]").forEach(el => {
    el.classList.toggle("active", el.dataset.tabLink === tab);
  });
}

function setVisibleView(tab) {
  const normalized = normalizeTab(tab);
  const views = document.querySelectorAll("[data-view]");

  console.log("[tabs] switch ->", normalized, "views:", views.length);

  views.forEach(el => {
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
  if (badge) {
    badge.textContent = "Вкладка: " + normalized;
  }
}

function ensureViewsExist() {
  const required = ["главное", "портфель", "настройки", "история", "график"];
  const root = document.querySelector(".app") || document.body;

  required.forEach(tab => {
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

function renderTabStub(tab) {
  const node = document.querySelector(`[data-view="${tab}"]`);
  if (!node) return;

  if (tab === "главное" && !node.dataset.ready) {
    node.innerHTML = `<div class="block"><h2>Главное</h2><p>Главная вкладка активна.</p></div>`;
    node.dataset.ready = "1";
  }

  if (tab === "портфель" && !node.dataset.ready) {
    node.innerHTML = `<div class="block"><h2>Портфель</h2><p>Здесь будет портфель, позиции и стоп-заявки.</p></div>`;
    node.dataset.ready = "1";
  }

  if (tab === "настройки" && !node.dataset.ready) {
    node.innerHTML = `<div class="block"><h2>Настройки</h2><p>Здесь будут режимы торговли, риск и профили.</p></div>`;
    node.dataset.ready = "1";
  }

  if (tab === "история" && !node.dataset.ready) {
    node.innerHTML = `<div class="block"><h2>История</h2><p>Здесь будут сделки, системные события и ошибки.</p></div>`;
    node.dataset.ready = "1";
  }

  if (tab === "график" && !node.dataset.ready) {
    node.innerHTML = `<div class="block"><h2>График</h2><p>Здесь будет график инструмента.</p></div>`;
    node.dataset.ready = "1";
  }
}

async function applyRoute() {
  const tab = getTabFromHash();

  try {
    console.log("[tabs] applyRoute ->", tab);
    ensureViewsExist();
    renderTabStub(tab);
    setVisibleView(tab);
    document.title = "Вкладка: " + tab;
  } catch (e) {
    console.error("[tabs] route error:", e);
  }
}

function bindRouter() {
  document.addEventListener("click", e => {
    const link = e.target.closest("[data-tab-link]");
    if (!link) return;

    e.preventDefault();
    e.stopPropagation();

    const tab = normalizeTab(link.dataset.tabLink);
    const newHash = `#/${tab}`;

    console.log("[tabs] click ->", tab);

    if (window.location.hash === newHash) {
      applyRoute();
      return;
    }

    window.location.hash = newHash;
  });

  window.addEventListener("hashchange", () => {
    console.log("[tabs] hashchange ->", window.location.hash);
    applyRoute();
  });

  if (!window.location.hash || !window.location.hash.startsWith("#/")) {
    window.location.hash = "#/главное";
  } else {
    applyRoute();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  console.log("[tabs] DOMContentLoaded");
  bindRouter();
});