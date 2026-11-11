"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  page: 1,
  pageSize: 20,
  source: "all",
  status: "all",
  q: "",
  remote: null,
  items: [],
  total: 0,
  pages: 0,
  loading: false,
  requestSeq: 0,
  activeIndex: -1,
  run: null,
  stats: null,
};

const SOURCE_META = {
  hh: { label: "hh.ru", cls: "border-red-500/40 bg-red-500/10 text-red-300" },
  habr: { label: "Habr Career", cls: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  remoteok: { label: "RemoteOK", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
};

const STATUS_META = {
  new: { label: "Новая", cls: "border-indigo-500/40 bg-indigo-500/10 text-indigo-300" },
  liked: { label: "Лайк", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  rejected: { label: "Скип", cls: "border-rose-500/40 bg-rose-500/10 text-rose-300" },
  applied: { label: "Отклик", cls: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
  archived: { label: "Архив", cls: "border-slate-500/40 bg-slate-500/10 text-slate-300" },
};

const TABS = [
  ["all", "Все"],
  ["new", "Новые"],
  ["liked", "Liked"],
  ["rejected", "Rejected"],
  ["applied", "Applied"],
  ["archived", "Архив"],
];

const PHASES = {
  queued: "В очереди…",
  fetch_hh: "Сбор hh.ru…",
  fetch_habr: "Сбор Habr Career…",
  fetch_remoteok: "Сбор RemoteOK…",
  save: "Сохранение в базу…",
  score: "AI-реитинг…",
  letters: "AI letters…",
  done: "Готово",
  error: "Ошибка",
};

const PHASE_PCT = {
  queued: 5,
  fetch_hh: 15,
  fetch_habr: 30,
  fetch_remoteok: 45,
  save: 60,
  score: 78,
  letters: 92,
  done: 100,
  error: 100,
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function scrapeButton() {
  return $("#btn-scrape") || $("#btn-run");
}

function healthDot() {
  return $("#health-dot") || $("#ollama-dot");
}

function healthLabel() {
  return $("#health-label") || $("#ollama-label");
}

function showProgress(show) {
  const wrap = $("#progress-wrap");
  if (wrap) wrap.classList.toggle("hidden", !show);
}

function restoreScrapeButton() {
  const button = scrapeButton();
  if (!button) return;
  button.disabled = false;
  button.textContent = "Запустить поиск";
}

function renderSourceOptions() {
  const select = $("#f-source");
  if (!select) return;
  const options = [
    ["all", "Все источники"],
    ["hh", "hh.ru"],
    ["habr", "Habr Career"],
    ["remoteok", "RemoteOK"],
  ];
  select.replaceChildren(
    ...options.map(([value, label]) => {
      const option = el("option", "", label);
      option.value = value;
      return option;
    })
  );
  select.value = state.source;
  if (!select.getAttribute("aria-label")) select.setAttribute("aria-label", "Источник вакансий");
}

function showEmpty(message) {
  const cards = $("#cards");
  if (cards) cards.replaceChildren();
  const empty = $("#empty");
  if (!empty) return;
  empty.replaceChildren(
    el("div", "text-4xl text-slate-700", "∅"),
    el("p", "text-slate-400", message)
  );
  empty.classList.remove("hidden");
  empty.classList.add("flex");
}

function ensureLayout() {
  const main = $("main") || document.body;
  const header = $("header");

  let cards = $("#cards");
  if (!cards) {
    cards = el("section", "space-y-3");
    cards.id = "cards";
    main.append(cards);
  }

  let empty = $("#empty");
  if (!empty) {
    empty = el("div", "hidden flex-col items-center justify-center gap-3 py-20 text-center");
    empty.id = "empty";
    empty.append(el("p", "text-slate-400", "Вакансий не найдено. Измените фильтры или запустите сбор."));
    cards.insertAdjacentElement("afterend", empty);
  }

  let filters = $("#filters");
  if (!filters) {
    filters = el("div", "mb-4 flex flex-wrap items-center gap-2");
    filters.id = "filters";
    main.insertBefore(filters, cards);
  }

  if (!$("#tabs")) {
    const tabs = el("nav", "flex flex-wrap gap-1 rounded-lg bg-slate-900 p-1 text-sm");
    tabs.id = "tabs";
    filters.prepend(tabs);
  }

  if (!$("#f-q")) {
    const search = el(
      "input",
      "w-56 rounded-lg border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm outline-none placeholder:text-slate-600 focus:border-indigo-500"
    );
    search.id = "f-q";
    search.type = "search";
    search.placeholder = "Поиск по названию / компании…";
    search.setAttribute("aria-label", "Поиск вакансий");
    filters.append(search);
  }

  if (!$("#f-source")) {
    const select = el(
      "select",
      "rounded-lg border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-300 outline-none focus:border-indigo-500"
    );
    select.id = "f-source";
    select.setAttribute("aria-label", "Источник вакансий");
    filters.append(select);
  }

  if (!$("#f-remote")) {
    const label = el("label", "inline-flex items-center gap-2 text-sm text-slate-400");
    const checkbox = el(
      "input",
      "h-4 w-4 rounded border-slate-700 bg-slate-900 accent-indigo-500"
    );
    checkbox.id = "f-remote";
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", "Только удалённые вакансии");
    label.append(checkbox, document.createTextNode("Удалёнка"));
    filters.append(label);
  }

  if (!$("#count")) {
    const count = el("span", "ml-auto text-xs text-slate-500");
    count.id = "count";
    filters.append(count);
  }

  let progress = $("#progress-wrap");
  if (!progress) {
    progress = el("div", "mx-auto w-full max-w-5xl px-4 pb-3");
    progress.id = "progress-wrap";
    progress.classList.add("hidden");
    (header || main).append(progress);
  }

  if (!$("#progress-phase")) {
    const phase = el("span", "text-xs text-slate-400", "…");
    phase.id = "progress-phase";
    progress.append(phase);
  }

  if (!$("#progress-count")) {
    const count = el("span", "text-xs text-slate-400", "0 собрано · 0 IT · 0 сохранено");
    count.id = "progress-count";
    progress.append(count);
  }

  if (!$("#progress-bar")) {
    const track = el("div", "mt-1 h-1.5 w-full overflow-hidden rounded bg-slate-800");
    const bar = el("div", "h-full w-0 rounded bg-indigo-500 transition-all duration-500");
    bar.id = "progress-bar";
    track.append(bar);
    progress.append(track);
  }

  let pagination = $("#pagination");
  if (!pagination) {
    pagination = el("nav", "mt-5 hidden flex-wrap items-center justify-center gap-2");
    pagination.id = "pagination";
    cards.insertAdjacentElement("afterend", pagination);
  }

  if (!$("#pg-prev")) {
    const prev = el(
      "button",
      "rounded-md border border-slate-700 px-3 py-1 text-sm text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40",
      "‹ Назад"
    );
    prev.id = "pg-prev";
    prev.type = "button";
    pagination.append(prev);
  }

  if (!$("#page-buttons")) {
    const buttons = el("div", "flex flex-wrap items-center gap-1");
    buttons.id = "page-buttons";
    pagination.append(buttons);
  }

  if (!$("#pg-next")) {
    const next = el(
      "button",
      "rounded-md border border-slate-700 px-3 py-1 text-sm text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40",
      "Вперёд ›"
    );
    next.id = "pg-next";
    next.type = "button";
    pagination.append(next);
  }

  if (!$("#page-info")) {
    const info = el("div", "mt-2 text-center text-xs text-slate-500");
    info.id = "page-info";
    pagination.insertAdjacentElement("afterend", info);
  }

  if (!$("#stats")) {
    const stats = el("div", "mt-3 text-xs text-slate-500");
    stats.id = "stats";
    const info = $("#page-info");
    if (info) {
      info.insertAdjacentElement("afterend", stats);
    } else {
      pagination.insertAdjacentElement("afterend", stats);
    }
  }

  if (!$("#toasts")) {
    const toasts = el("div", "fixed bottom-4 right-4 z-50 w-80 max-w-[90vw] space-y-2");
    toasts.id = "toasts";
    document.body.append(toasts);
  }

  let dot = healthDot();
  let label = healthLabel();
  const healthHost = header || document.body;

  if (!dot) {
    dot = el("span", "h-2.5 w-2.5 rounded-full bg-slate-600");
    dot.id = "health-dot";
    dot.title = "Статус API";
    if (label) {
      label.insertAdjacentElement("beforebegin", dot);
    } else {
      healthHost.prepend(dot);
    }
  }
  if (!label) {
    label = el("span", "text-xs text-slate-500", "Проверка…");
    label.id = "health-label";
    dot.insertAdjacentElement("afterend", label);
  }

  if (!scrapeButton()) {
    const button = el(
      "button",
      "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-500 active:bg-indigo-700",
      "Запустить поиск"
    );
    button.id = "btn-scrape";
    button.type = "button";
    (header || main).append(button);
  }

  let footer = $("footer");
  if (!footer) {
    footer = el("footer", "mx-auto max-w-5xl px-4 pb-8 text-xs text-slate-600");
    document.body.append(footer);
  }

  let attribution = $("#attribution");
  if (!attribution) {
    attribution = el("div", "text-xs text-slate-600");
    attribution.id = "attribution";
    footer.append(attribution);
  }
  const remoteLink = el("a", "underline-offset-2 hover:text-slate-400 hover:underline", "Remote OK");
  remoteLink.href = "https://remoteok.com";
  remoteLink.target = "_blank";
  remoteLink.rel = "noopener";
  attribution.replaceChildren(
    document.createTextNode("Источники: hh.ru, Habr Career, RemoteOK."),
    document.createElement("br"),
    document.createTextNode("Данные RemoteOK предоставлены "),
    remoteLink,
    document.createTextNode(".")
  );
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const payload = await res.json();
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      } else if (payload.detail) {
        detail = JSON.stringify(payload.detail);
      }
    } catch {
      detail = res.statusText || `HTTP ${res.status}`;
    }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function toast(message, type = "info") {
  const box = $("#toasts");
  if (!box) return;
  const colors = {
    ok: "border-emerald-500/50 text-emerald-200",
    err: "border-rose-500/50 text-rose-200",
    info: "border-slate-600 text-slate-200",
  };
  const node = el(
    "div",
    `fade-in rounded-xl border bg-slate-900/95 px-4 py-2.5 text-sm shadow-lg ${colors[type] || colors.info}`,
    message
  );
  node.setAttribute("role", "status");
  box.append(node);
  setTimeout(() => node.remove(), 4000);
}

function fmtSalary(v) {
  const from = v.salary_from;
  const to = v.salary_to;
  if (from === null || from === undefined) {
    if (to === null || to === undefined) return "";
  }

  const symbols = { RUB: "₽", USD: "$", EUR: "€" };
  const symbol = symbols[v.currency] || v.currency || "";
  const prefixCurrency = v.currency === "USD" || v.currency === "EUR";
  const formatter = new Intl.NumberFormat("ru-RU");
  const number = (value) => formatter.format(value);
  const money = (value) => {
    const formatted = number(value);
    if (!symbol) return formatted;
    return prefixCurrency ? `${symbol}${formatted}` : `${formatted} ${symbol}`;
  };

  let suffix = "";
  if (v.salary_period === "month") suffix = "/мес";
  else if (v.salary_period === "year") suffix = "/год";

  const hasFrom = from !== null && from !== undefined;
  const hasTo = to !== null && to !== undefined;
  if (hasFrom && hasTo) {
    if (symbol && !prefixCurrency) return `${number(from)} – ${number(to)} ${symbol}${suffix}`;
    return `${money(from)} – ${money(to)}${suffix}`;
  }
  if (hasFrom) return `от ${money(from)}${suffix}`;
  return `до ${money(to)}${suffix}`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function badge(text, cls) {
  return el(
    "span",
    `inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`,
    text
  );
}

function sourceMeta(source) {
  return (
    SOURCE_META[source] || {
      label: source || "Источник",
      cls: "border-slate-500/40 bg-slate-500/10 text-slate-300",
    }
  );
}

function statusMeta(status) {
  return (
    STATUS_META[status] || {
      label: status || "Статус",
      cls: "border-slate-500/40 bg-slate-500/10 text-slate-300",
    }
  );
}

function itClass(score) {
  if (score >= 80) return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (score >= 65) return "border-amber-500/40 bg-amber-500/10 text-amber-300";
  return "border-slate-500/40 bg-slate-500/10 text-slate-300";
}

function aiClass(score) {
  if (score >= 8) return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (score >= 6) return "border-violet-500/40 bg-violet-500/10 text-violet-300";
  return "border-slate-500/40 bg-slate-500/10 text-slate-300";
}

function aiVerdict(v) {
  const verdict = String(v.ai_verdict || "").toLowerCase();
  const labels = {
    strong: "Strong",
    stretch: "Stretch",
    good: "Stretch",
    weak: "Skip",
    skip: "Skip",
    rejected: "Skip",
  };
  return labels[verdict] || v.ai_verdict || "";
}

function card(v) {
  const article = el(
    "article",
    "fade-in rounded-2xl border border-slate-800 bg-slate-900/60 p-4"
  );
  article.dataset.id = v.id;

  const top = el("div", "flex items-start justify-between gap-3");
  const title = el(
    "a",
    "font-semibold text-slate-100 hover:text-indigo-300",
    v.title || "Без названия"
  );
  title.href = v.url || "#";
  title.target = "_blank";
  title.rel = "noopener noreferrer";
  top.append(title);

  const badges = el("div", "flex flex-wrap items-center gap-1.5");
  const source = sourceMeta(v.source);
  badges.append(badge(source.label, source.cls));
  const status = statusMeta(v.status);
  badges.append(badge(status.label, status.cls));
  if (v.remote) badges.append(badge("Удалёнка", "border-cyan-500/40 bg-cyan-500/10 text-cyan-300"));
  badges.append(badge(`IT ${v.it_score}`, itClass(v.it_score)));
  if (v.ai_score !== null && v.ai_score !== undefined) {
    badges.append(badge(`AI ${v.ai_score}`, aiClass(v.ai_score)));
  }
  const verdict = aiVerdict(v);
  if (verdict) {
    badges.append(badge(`AI · ${verdict}`, "border-fuchsia-500/40 bg-fuchsia-500/10 text-fuchsia-300"));
  }
  if (v.letter_status === "ready") {
    badges.append(badge("Cover ready", "border-amber-500/40 bg-amber-500/10 text-amber-300"));
  } else if (v.letter_status === "failed") {
    badges.append(badge("Cover failed", "border-rose-500/40 bg-rose-500/10 text-rose-300"));
  }
  article.append(top, badges);

  const metaParts = [v.company, v.location, fmtSalary(v), fmtDate(v.published_at)].filter(Boolean);
  if (metaParts.length) {
    article.append(el("div", "mt-2 text-xs text-slate-400", metaParts.join(" · ")));
  }

  const tags = Array.isArray(v.tags) ? v.tags : [];
  if (tags.length) {
    const tagRow = el("div", "mt-2 flex flex-wrap gap-1.5");
    tags.slice(0, 8).forEach((tag) => {
      tagRow.append(
        el("span", "rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-300", tag)
      );
    });
    if (tags.length > 8) {
      tagRow.append(
        el("span", "rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-400", `+${tags.length - 8}`)
      );
    }
    article.append(tagRow);
  }

  const description = String(v.description || "").replace(/\s+/g, " ").trim();
  if (description) {
    const preview = description.slice(0, 260);
    article.append(
      el(
        "p",
        "mt-2 text-sm leading-relaxed text-slate-400",
        preview + (description.length > 260 ? "…" : "")
      )
    );
  }

  const actions = el("div", "mt-3 flex flex-wrap items-center gap-2");
  const buttons = [
    ["liked", "Лайк"],
    ["rejected", "Скип"],
    ["applied", "Отклик"],
    ["archived", "Архив"],
  ];
  buttons.forEach(([value, label]) => {
    const meta = statusMeta(value);
    const active = v.status === value;
    const button = el(
      "button",
      `rounded-lg border px-2.5 py-1 text-xs transition-colors ${
        active ? meta.cls : "border-slate-700 text-slate-300 hover:bg-slate-800"
      }`,
      label
    );
    button.type = "button";
    button.addEventListener("click", () => setStatus(v.id, value));
    actions.append(button);
  });

  const open = el(
    "a",
    "ml-auto rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300 transition-colors hover:bg-slate-800",
    "Открыть ↗"
  );
  open.href = v.url || "#";
  open.target = "_blank";
  open.rel = "noopener noreferrer";
  actions.append(open);
  article.append(actions);

  return article;
}

function renderCards() {
  const cards = $("#cards");
  if (!cards) return;
  cards.replaceChildren();
  state.activeIndex = -1;

  if (!state.items.length) {
    showEmpty("Вакансий не найдено. Измените фильтры или запустите сбор.");
    return;
  }

  const empty = $("#empty");
  if (empty) {
    empty.classList.add("hidden");
    empty.classList.remove("flex");
  }

  const fragment = document.createDocumentFragment();
  state.items.forEach((item) => fragment.append(card(item)));
  cards.append(fragment);
}

function renderStats() {
  if (!state.stats) return;
  const target = $("#stats");
  if (!target) return;
  const bySource = state.stats.by_source || {};
  target.textContent =
    `Всего: ${state.stats.total || 0} · hh.ru: ${bySource.hh || 0} · Habr: ${bySource.habr || 0} · RemoteOK: ${bySource.remoteok || 0} · AI: ${state.stats.scored || 0} · Cover: ${state.stats.letters_ready || 0}`;
}

function pageNumbers(current, pages) {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, i) => i + 1);
  }
  const set = new Set([1, pages, current - 1, current, current + 1]);
  const sorted = [...set].filter((p) => p >= 1 && p <= pages).sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < sorted.length; i += 1) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) out.push("…");
    out.push(sorted[i]);
  }
  return out;
}

function renderPagination() {
  const pagination = $("#pagination");
  const info = $("#page-info");
  const prev = $("#pg-prev");
  const next = $("#pg-next");
  const buttons = $("#page-buttons");
  if (!pagination || !info || !prev || !next || !buttons) return;

  const hide = state.pages <= 1;
  pagination.classList.toggle("hidden", hide);
  pagination.classList.toggle("flex", !hide);
  prev.disabled = state.page <= 1;
  next.disabled = state.page >= state.pages;
  buttons.replaceChildren();

  if (state.pages > 1) {
    pageNumbers(state.page, state.pages).forEach((page) => {
      if (page === "…") {
        buttons.append(el("span", "px-1 text-slate-600", "…"));
        return;
      }
      const active = page === state.page;
      const button = el(
        "button",
        `rounded-md px-2.5 py-1 text-sm transition-colors ${
          active ? "bg-indigo-600 text-white" : "border border-slate-700 text-slate-300 hover:bg-slate-800"
        }`,
        String(page)
      );
      button.type = "button";
      if (active) button.setAttribute("aria-current", "page");
      button.addEventListener("click", () => goToPage(page));
      buttons.append(button);
    });
  }

  info.textContent = state.total
    ? `Страница ${state.page} из ${state.pages} · всего ${state.total}`
    : "Нет вакансий";
}

function renderTabs() {
  const tabs = $("#tabs");
  if (!tabs) return;
  tabs.replaceChildren();
  TABS.forEach(([value, label]) => {
    const active = state.status === value;
    const button = el(
      "button",
      `rounded-md px-3 py-1.5 text-sm transition-colors ${
        active ? "bg-slate-700 text-slate-50" : "text-slate-400 hover:bg-slate-800"
      }`,
      label
    );
    button.type = "button";
    button.addEventListener("click", () => {
      state.status = value;
      state.page = 1;
      renderTabs();
      loadPage();
    });
    tabs.append(button);
  });
}

async function loadPage() {
  const seq = ++state.requestSeq;
  state.loading = true;
  const count = $("#count");
  if (count) count.textContent = "Загрузка…";

  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
  });
  if (state.source !== "all") params.set("source", state.source);
  if (state.status !== "all") params.set("status", state.status);
  if (state.q) params.set("q", state.q);
  if (state.remote === true) params.set("remote", "true");

  try {
    const data = await api(`/api/vacancies?${params.toString()}`);
    if (seq !== state.requestSeq) return;

    state.items = Array.isArray(data.items) ? data.items : [];
    state.total = Number(data.total) || 0;
    state.pages = Number(data.pages) || 0;
    state.page = Number(data.page) || state.page;

    if (!state.items.length && state.page > 1 && (!state.pages || state.page > state.pages)) {
      state.page = state.pages || 1;
      await loadPage();
      return;
    }

    renderCards();
    renderPagination();
    renderStats();
    if (count) count.textContent = `Найдено: ${state.total}`;
  } catch (error) {
    if (seq !== state.requestSeq) return;
    state.items = [];
    state.total = 0;
    state.pages = 0;
    showEmpty(error.message);
    renderPagination();
    renderStats();
    if (count) count.textContent = "Ошибка загрузки";
    toast(error.message, "err");
  } finally {
    if (seq === state.requestSeq) state.loading = false;
  }
}

async function loadStats() {
  try {
    state.stats = await api("/api/stats");
    renderStats();
  } catch (error) {
    console.warn("Не удалось загрузить статистику", error);
  }
}

async function loadHealth() {
  const dot = healthDot();
  const label = healthLabel();
  try {
    const data = await api("/api/health");
    if (dot) {
      dot.classList.remove("bg-slate-600", "bg-emerald-500", "bg-rose-500");
      dot.classList.add(data.status === "ok" ? "bg-emerald-500" : "bg-rose-500");
    }
    const aiStatus = data.ai_status || "unknown";
    if (label) label.textContent = `Вакансий: ${data.total || 0} · AI: ${aiStatus}`;

    const last = $("#last-run");
    if (last) {
      if (data.last_run) {
        last.textContent = `Последний сбор: ${fmtDate(data.last_run.started_at)}`;
        last.classList.remove("hidden");
      } else {
        last.classList.add("hidden");
        last.textContent = "";
      }
    }
  } catch (error) {
    if (dot) {
      dot.classList.remove("bg-slate-600", "bg-emerald-500", "bg-rose-500");
      dot.classList.add("bg-rose-500");
    }
    if (label) label.textContent = "Нет связи";
    console.warn("Не удалось проверить API", error);
  }
}

async function setStatus(id, status) {
  try {
    await api(`/api/vacancies/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    const index = state.items.findIndex((item) => item.id === id);
    if (index >= 0) state.items[index] = { ...state.items[index], status };
    await loadPage();
    await loadStats();
    if (!state.items.length && state.page > 1) {
      state.page -= 1;
      await loadPage();
    }
    toast("Статус обновлён", "ok");
  } catch (error) {
    toast(error.message, "err");
  }
}

function goToPage(page) {
  if (page < 1 || (state.pages && page > state.pages)) return;
  state.page = page;
  loadPage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showProgressRun(run) {
  const phase = $("#progress-phase");
  const bar = $("#progress-bar");
  const count = $("#progress-count");
  if (phase) phase.textContent = PHASES[run.phase] || run.phase || "…";
  if (bar) {
    const percent = PHASE_PCT[run.phase] === undefined ? 0 : PHASE_PCT[run.phase];
    bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }
  if (count) {
    count.textContent =
      `${run.fetched || 0} собрано · ${run.filtered || 0} IT · ${run.saved || 0} сохран · ${
        run.known || 0
      } known · ${run.analyzed || 0} AI · ${run.letters || 0} cover`;
  }
  showProgress(true);
}

function pollRun(runId) {
  if (state.run && state.run.timer) clearTimeout(state.run.timer);
  state.run = { id: runId, timer: null };

  const tick = async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      showProgressRun(run);
      if (!run.finished_at) {
        state.run.timer = setTimeout(tick, 1000);
        return;
      }

      state.run = null;
      showProgress(false);
      restoreScrapeButton();

      if (run.error) {
        toast(`Сбор завершён с ошибкой: ${run.error}`, "err");
      } else {
        toast(
          `Сбор завершён: сохранено ${run.saved || 0} из ${run.fetched || 0}; AI ${run.analyzed || 0}; cover ${run.letters || 0}`,
          "ok"
        );
      }

      try {
        const errors = JSON.parse(run.errors_json || "{}");
        if (errors && typeof errors === "object" && Object.keys(errors).length) {
          toast("Часть источников недоступна", "info");
        }
      } catch (error) {
        console.warn("Некорректный errors_json", error);
      }

      state.page = 1;
      await loadPage();
      await loadStats();
      await loadHealth();
    } catch (error) {
      state.run = null;
      showProgress(false);
      restoreScrapeButton();
      toast(error.message, "err");
    }
  };

  tick();
}

async function startScrape() {
  const button = scrapeButton();
  if (button) {
    button.disabled = true;
    button.textContent = "Запуск…";
  }
  showProgress(true);
  showProgressRun({ phase: "queued", fetched: 0, filtered: 0, saved: 0, known: 0, analyzed: 0, letters: 0 });

  try {
    const result = await api("/api/scrape", { method: "POST" });
    if (result.started === false) toast("Сбор уже идёт", "info");
    pollRun(result.run_id);
  } catch (error) {
    restoreScrapeButton();
    showProgress(false);
    toast(error.message, "err");
  }
}

function moveActive(delta) {
  const cards = [...document.querySelectorAll("#cards article")];
  if (!cards.length) return;

  let index = state.activeIndex;
  if (index < 0) {
    index = delta > 0 ? 0 : cards.length - 1;
  } else {
    index += delta;
  }
  index = Math.max(0, Math.min(cards.length - 1, index));

  cards.forEach((node) => node.classList.remove("card-active"));
  cards[index].classList.add("card-active");
  cards[index].scrollIntoView({ block: "nearest", behavior: "smooth" });
  state.activeIndex = index;
}

function clearActive() {
  document.querySelectorAll("#cards article.card-active").forEach((node) => {
    node.classList.remove("card-active");
  });
  state.activeIndex = -1;
}

function onKeyDown(event) {
  const key = event.key;

  if (key === "Escape") {
    const search = $("#f-q");
    if (search && (document.activeElement === search || event.target === search)) {
      search.value = "";
      state.q = "";
      state.page = 1;
      loadPage();
      return;
    }
    clearActive();
    return;
  }

  const target = event.target;
  if (
    target &&
    (target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.tagName === "SELECT" ||
      target.isContentEditable)
  ) {
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;

  const lower = key.toLowerCase();
  if (key === "ArrowDown" || lower === "j") {
    event.preventDefault();
    moveActive(1);
    return;
  }
  if (key === "ArrowUp" || lower === "k") {
    event.preventDefault();
    moveActive(-1);
    return;
  }
  if (lower === "a" || lower === "r") {
    const vacancy = state.items[state.activeIndex];
    if (vacancy) setStatus(vacancy.id, lower === "a" ? "liked" : "rejected");
    return;
  }
  if (lower === "o") {
    const vacancy = state.items[state.activeIndex];
    if (vacancy && vacancy.url) {
      window.open(vacancy.url, "_blank", "noopener,noreferrer");
    }
    return;
  }
  if (key === "/") {
    event.preventDefault();
    const search = $("#f-q");
    if (search) search.focus();
  }
}

function init() {
  ensureLayout();
  renderSourceOptions();

  const banner = $("#ollama-banner");
  if (banner) banner.classList.add("hidden");

  renderTabs();

  const search = $("#f-q");
  if (search) {
    search.addEventListener(
      "input",
      debounce(() => {
        state.q = search.value.trim();
        state.page = 1;
        loadPage();
      }, 300)
    );
  }

  const source = $("#f-source");
  if (source) {
    source.addEventListener("change", () => {
      state.source = source.value;
      state.page = 1;
      loadPage();
    });
  }

  const remote = $("#f-remote");
  if (remote) {
    remote.checked = state.remote === true;
    remote.addEventListener("change", () => {
      state.remote = remote.checked ? true : null;
      state.page = 1;
      loadPage();
    });
  }

  const button = scrapeButton();
  if (button) button.addEventListener("click", startScrape);

  const prev = $("#pg-prev");
  if (prev) {
    prev.addEventListener("click", () => {
      if (!prev.disabled) goToPage(state.page - 1);
    });
  }

  const next = $("#pg-next");
  if (next) {
    next.addEventListener("click", () => {
      if (!next.disabled) goToPage(state.page + 1);
    });
  }

  document.addEventListener("keydown", onKeyDown);

  loadPage();
  loadStats();
  loadHealth();
  setInterval(loadHealth, 20000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
