/* AI Job Hunter — дашборд (vanilla JS, без фреймворков).
 * Триаж: лайк/скип, письмо генерируется по кнопке «Сгенерировать письмо»,
 * копирование, прогресс прогона, горячие клавиши J/K/A/R/C/O.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  vacancies: [],
  filter: { status: "all", minScore: 0, q: "" },
  run: null,          // { id, pollTimer }
  activeIndex: -1,
};

const PHASES = {
  init: "Запуск…",
  fetch: "Сбор вакансий с hh.ru…",
  done: "Готово",
};
const LETTER_LABEL = { none: "нет письма", pending: "генерация…", ready: "готово", failed: "ошибка" };
const TABS = [
  ["all", "Все"], ["new", "Новые"], ["liked", "Лайки"],
  ["rejected", "Скипы"], ["applied", "Откликнулся"], ["archived", "Архив"],
];
const CURRENCY = { RUB: "₽", USD: "$", EUR: "€" };

/* ------------------------------------------------------------- helpers */

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* не json */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function toast(msg, type = "info") {
  const colors = {
    ok: "border-emerald-500/50 text-emerald-200",
    err: "border-rose-500/50 text-rose-200",
    info: "border-slate-600 text-slate-200",
  };
  const box = el("div", `fade-in rounded-xl border bg-slate-900/95 px-4 py-2.5 text-sm shadow-lg ${colors[type]}`, msg);
  $("#toasts").append(box);
  setTimeout(() => box.remove(), 3500);
}

function fmtSalary(v) {
  if (!v.salary_from && !v.salary_to) return "";
  const cur = CURRENCY[v.currency] || (v.currency || "");
  return `${v.salary_from ?? "?"}–${v.salary_to ?? "?"} ${cur}`.trim();
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString("ru-RU");
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

/* ------------------------------------------------------------- данные */

async function loadVacancies() {
  const p = new URLSearchParams();
  if (state.filter.status !== "all") p.set("status", state.filter.status);
  if (state.filter.minScore) p.set("min_score", state.filter.minScore);
  if (state.filter.q) p.set("q", state.filter.q);
  try {
    state.vacancies = await api(`/api/vacancies?${p.toString()}`);
    render();
  } catch (e) {
    toast(`Не удалось загрузить вакансии: ${e.message}`, "err");
  }
}

/* ------------------------------------------------------------- рендер */

function card(v) {
  const card = el("article", "fade-in rounded-2xl border border-slate-800 bg-slate-900/60 p-4");
  card.dataset.id = v.id;

  /* --- шапка: заголовок + мета */
  const head = el("div", "flex items-start gap-3");
  const left = el("div", "min-w-0 flex-1");
  const title = el("h3", "font-semibold text-slate-100 leading-snug", v.title || "Без названия");
  const meta = el("div", "mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400");
  [v.company, fmtSalary(v), v.area, v.published_at ? `от ${fmtDate(v.published_at)}` : ""]
    .filter(Boolean)
    .forEach((t) => meta.append(el("span", "", t)));
  left.append(title, meta);
  head.append(left);
  card.append(head);

  /* --- действия */
  const actions = el("div", "mt-3 flex flex-wrap items-center gap-2");
  const mkBtn = (label, status, activeCls) => {
    const b = el("button", `rounded-lg border px-3 py-1.5 text-xs transition-colors ${
      v.status === status ? activeCls : "border-slate-700 text-slate-300 hover:bg-slate-800"}`, label);
    b.onclick = () => setStatus(v.id, status);
    return b;
  };
  actions.append(
    mkBtn("Лайк", "liked", "border-emerald-500/60 bg-emerald-500/10 text-emerald-300"),
    mkBtn("Скип", "rejected", "border-rose-500/60 bg-rose-500/10 text-rose-300"),
    mkBtn("Откликнулся", "applied", "border-sky-500/60 bg-sky-500/10 text-sky-300"),
    mkBtn("В архив", "archived", "border-slate-500/60 bg-slate-700/40 text-slate-300"),
  );
  if (v.url) {
    const a = el("a", "text-xs text-indigo-400 hover:text-indigo-300 underline underline-offset-2", "Открыть на hh.ru ↗");
    a.href = v.url;
    a.target = "_blank";
    a.rel = "noopener";
    actions.append(a);
  }
  card.append(actions);

  /* --- письмо (аккордеон): генерация по кнопке + копирование */
  const letterBox = el("div", "mt-3 rounded-xl border border-slate-800 bg-slate-950/60");
  const lhead = el("div", "flex flex-wrap items-center gap-2 px-3 py-2 cursor-pointer select-none");
  const lstatus = el("span", "text-xs text-slate-500", LETTER_LABEL[v.letter_status] || "нет письма");
  lhead.append(
    el("span", "text-sm font-medium text-slate-200", "Сопроводительное письмо"),
    lstatus,
    el("span", "ml-auto text-slate-600 text-xs", "▾"),
  );
  const lbody = el("div", "hidden px-3 pb-3 pt-1");
  const ltext = el("div", "letter-text text-sm leading-relaxed text-slate-300");
  ltext.textContent = v.cover_letter
    || (v.letter_status === "failed" ? "Не удалось сгенерировать письмо. Попробуйте ещё раз."
        : v.letter_status === "pending" ? "Генерация письма…"
        : "Письмо не генерировалось. Нажмите «Сгенерировать письмо».");
  const lbtns = el("div", "mt-3 flex flex-wrap gap-2");
  const gen = el("button", "rounded-lg border border-indigo-500/50 px-3 py-1.5 text-xs text-indigo-300 hover:bg-indigo-500/10", "Сгенерировать письмо");
  const cp = el("button", "rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800", "Скопировать");
  cp.disabled = !v.cover_letter;
  cp.className += cp.disabled ? " opacity-40 cursor-not-allowed" : "";
  cp.onclick = () => copyLetter(v);
  gen.onclick = () => generateLetter(v, gen, ltext, lstatus, cp);
  lbtns.append(gen, cp);
  lbody.append(ltext, lbtns);
  lhead.onclick = () => lbody.classList.toggle("hidden");
  letterBox.append(lhead, lbody);
  if (v.letter_status === "ready" && v.cover_letter) lbody.classList.remove("hidden");
  card.append(letterBox);

  return card;
}

function render() {
  const box = $("#cards");
  box.innerHTML = "";
  state.activeIndex = -1;
  const list = state.vacancies;
  $("#empty").classList.toggle("hidden", list.length > 0);
  $("#empty").classList.toggle("flex", list.length === 0);
  $("#count").textContent = `Вакансий: ${list.length}`;
  list.forEach((v) => box.append(card(v)));
}

/* ------------------------------------------------------------- действия */

async function setStatus(id, status) {
  try {
    await api(`/api/vacancies/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    await loadVacancies();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function copyLetter(v) {
  if (!v.cover_letter) { toast("Письмо ещё не готово", "err"); return; }
  await copyText(v.cover_letter);
  toast("Письмо скопировано", "ok");
}

async function generateLetter(v, genBtn, ltext, lstatus, cpBtn) {
  genBtn.disabled = true;
  genBtn.classList.add("opacity-60");
  genBtn.textContent = "Генерация…";
  lstatus.textContent = "генерация…";
  try {
    const res = await api(`/api/vacancies/${v.id}/generate-letter`, { method: "POST" });
    v.cover_letter = res.letter;
    v.letter_status = "ready";
    ltext.textContent = res.letter;
    lstatus.textContent = "готово";
    if (cpBtn) {
      cpBtn.disabled = false;
      cpBtn.classList.remove("opacity-40", "cursor-not-allowed");
    }
    toast("Письмо готово", "ok");
  } catch (e) {
    v.letter_status = "failed";
    lstatus.textContent = "ошибка";
    ltext.textContent = `Не удалось сгенерировать письмо: ${e.message}`;
    toast(e.message, "err");
  } finally {
    genBtn.disabled = false;
    genBtn.classList.remove("opacity-60");
    genBtn.textContent = "Сгенерировать письмо";
  }
}

/* ------------------------------------------------------------- прогон */

function showProgress(show) {
  $("#progress-wrap").classList.toggle("hidden", !show);
  if (show) {
    $("#progress-bar").style.width = "0%";
    $("#progress-count").textContent = "0/0";
  }
}

function renderProgress(run) {
  const phase = PHASES[run.phase] || run.phase || "…";
  $("#progress-phase").textContent = phase;
  const total = run.total || 0;
  const done = run.processed || 0;
  $("#progress-count").textContent = `${done}/${total}`;
  const pct = total > 0 ? Math.round((done / total) * 100) : (phase === "Запуск…" ? 8 : 12);
  $("#progress-bar").style.width = `${pct}%`;
  if (run.finished_at) {
    $("#last-run").classList.remove("hidden");
    $("#last-run").textContent = run.error ? `последний прогон: ошибка` : `последний прогон: ${done} за ${phase === "Готово" ? "ок" : "…"}`;
  }
}

async function startRun() {
  const btn = $("#btn-run");
  btn.disabled = true;
  btn.textContent = "Запуск…";
  try {
    const res = await api("/api/run-agent", { method: "POST" });
    if (!res.started) {
      toast(res.message || "прогон уже идёт", "info");
      pollRun(res.run_id);
      return;
    }
    showProgress(true);
    pollRun(res.run_id);
  } catch (e) {
    toast(`Ошибка запуска: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Запустить поиск";
  }
}

function pollRun(runId) {
  if (state.run) clearTimeout(state.run.pollTimer);
  state.run = { id: runId };
  const tick = async () => {
    try {
      const run = await api(`/api/run/${runId}`);
      renderProgress(run);
      if (!run.finished_at) {
        state.run.pollTimer = setTimeout(tick, 1500);
        return;
      }
      state.run = null;
      showProgress(false);
      if (run.error) toast(`Прогон завершён с ошибкой: ${run.error}`, "err");
      else toast(`Поиск завершён: ${run.fetched ?? 0} новых вакансий`, "ok");
      await loadVacancies();
    } catch (e) {
      state.run = null;
      showProgress(false);
      toast(e.message, "err");
    }
  };
  state.run.pollTimer = setTimeout(tick, 800);
}

/* ------------------------------------------------------------- health */

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const dot = $("#ollama-dot");
    dot.className = `h-2.5 w-2.5 rounded-full ${h.ollama_online ? "bg-emerald-500" : "bg-rose-500"}`;
    $("#ollama-label").textContent = h.ollama_online
      ? `Ollama: ${h.model}`
      : "Ollama: офлайн";
    $("#ollama-banner").classList.toggle("hidden", h.ollama_online);
    if (h.last_run) {
      $("#last-run").classList.remove("hidden");
      $("#last-run").textContent = `последний прогон: ${new Date(h.last_run.started_at).toLocaleString("ru-RU")}`;
    }
  } catch { /* сервер недоступен — игнорируем */ }
}

/* ------------------------------------------------------------- hotkeys */

function activeVacancy() {
  return state.activeIndex >= 0 ? state.vacancies[state.activeIndex] : null;
}

function moveActive(delta) {
  const cards = [...$("#cards").querySelectorAll("article")];
  if (!cards.length) return;
  state.activeIndex = Math.max(0, Math.min(cards.length - 1, state.activeIndex + delta));
  cards.forEach((c, i) => c.classList.toggle("card-active", i === state.activeIndex));
  cards[state.activeIndex].scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = e.target && e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const k = e.key.toLowerCase();
  if (k === "j" || k === "arrowdown") { e.preventDefault(); moveActive(1); }
  else if (k === "k" || k === "arrowup") { e.preventDefault(); moveActive(-1); }
  else if (k === "a") { const v = activeVacancy(); if (v) setStatus(v.id, "liked"); }
  else if (k === "r") { const v = activeVacancy(); if (v) setStatus(v.id, "rejected"); }
  else if (k === "c") { const v = activeVacancy(); if (v) copyLetter(v); }
  else if (k === "o") { const v = activeVacancy(); if (v && v.url) window.open(v.url, "_blank", "noopener"); }
});

/* ------------------------------------------------------------- инициализация */

function renderTabs() {
  const nav = $("#tabs");
  nav.innerHTML = "";
  TABS.forEach(([status, label]) => {
    const b = el("button", `rounded-md px-3 py-1.5 transition-colors ${
      state.filter.status === status
        ? "bg-slate-700 text-slate-50"
        : "text-slate-400 hover:bg-slate-800"}`, label);
    b.onclick = () => {
      state.filter.status = status;
      renderTabs();
      loadVacancies();
    };
    nav.append(b);
  });
}

async function loadProfileLine() {
  try {
    const res = await fetch("/api/profile").then((r) => (r.ok ? r.json() : null));
    if (res && res.profile) {
      const p = res.profile;
      $("#profile-line").textContent =
        `${p.role || "Профиль"} · ${p.city || "—"} · ${p.remote_ok ? "удалёнка" : ""} · профиль: profile.yaml`;
    }
  } catch { /* нет эндпоинта профиля — не критично */ }
}

document.addEventListener("DOMContentLoaded", () => {
  renderTabs();
  $("#btn-run").addEventListener("click", startRun);
  $("#btn-recheck").addEventListener("click", () => { toast("Проверяю Ollama…", "info"); checkHealth(); });
  $("#f-q").addEventListener("input", debounce(() => { state.filter.q = $("#f-q").value.trim(); loadVacancies(); }, 300));

  checkHealth();
  loadVacancies();
  loadProfileLine();
  setInterval(checkHealth, 5000);
});
