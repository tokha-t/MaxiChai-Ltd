// Общие помощники интерфейса: экранирование, тосты, модалки, форматирование, опрос задач оценки.
import { api } from "./api.js";

export const state = {
  role: "operator",
  embed: false,
  dialogFilters: { group_id: "", language: "", channel: "", critical: false, needs_review: false, q: "" },
  lastChecklistId: null,
};

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function toast(msg, kind = "") {
  const el = h(`<div class="toast ${kind}">${esc(msg)}</div>`);
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

export function errorToast(e) {
  const text = e && e.message ? e.message : String(e);
  const safe = /Traceback|Error code:|api[_-]?key|Authorization|sk-[\w-]{16}|AIza[\w-]{20}/i.test(text)
    ? 'Операция не выполнена. Проверьте сохранённые настройки или повторите запрос.' : text;
  toast(safe, "error");
}

export function modal({ title, body, actions = [], wide = false }) {
  const root = document.getElementById("modal-root");
  const back = h(`<div class="modal-back"><div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}" style="${wide ? "width:min(960px,100%)" : ""}">
      <div class="modal-head"><h2>${esc(title)}</h2><span class="spacer"></span><button class="btn ghost" data-x aria-label="Закрыть">✕</button></div>
      <div class="modal-body"></div><div class="modal-foot"></div></div></div>`);
  const bodyEl = back.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
  const foot = back.querySelector(".modal-foot");
  const previousFocus = document.activeElement;
  const surfaces = [...document.querySelectorAll('header.topbar, main, #globalJobStatus')];
  surfaces.forEach((el) => { el.inert = true; });
  const close = () => { back.remove(); if (!root.children.length) surfaces.forEach((el) => { el.inert = false; }); if (previousFocus?.isConnected) previousFocus.focus(); };
  for (const a of actions) {
    const b = h(`<button class="btn ${a.kind || ""}">${esc(a.label)}</button>`);
    b.addEventListener("click", async () => {
      if (!a.onClick) return close();
      b.disabled = true;
      try {
        const keep = await a.onClick({ close, body: bodyEl, button: b });
        if (keep !== true) close();
      } catch (e) { errorToast(e); } finally { b.disabled = false; }
    });
    foot.appendChild(b);
  }
  back.querySelector("[data-x]").addEventListener("click", close);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  back.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    if (e.key !== "Tab") return;
    const focusable = [...back.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]')].filter((el) => el.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
  });
  root.appendChild(back);
  back.querySelector('input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [data-x]')?.focus();
  return { close, body: bodyEl, el: back };
}

export function scoreClass(v) {
  // Числовой балл не задаёт произвольный бизнес-порог нарушения.
  return "";
}

export function fmtScore(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(2).replace(/\.00$/, "");
}

export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "$0";
  return "$" + (v < 0.01 ? v.toFixed(4) : v.toFixed(3));
}

export function valuePill(row) {
  const v = row.value;
  const label = row.value_label ?? (v === null ? "N/A" : v === true ? "Да" : v === false ? "Нет" : String(v));
  if (row.type === "flag") return v ? `<span class="pill crit">Грубая ошибка</span>` : `<span class="pill yes">Нет нарушений</span>`;
  if (v === null || v === undefined) return `<span class="pill na">Не применяется (N/A)</span>`;
  if (row.type === "scale") {
    const ratio = row.max_points ? (row.points ?? 0) / row.max_points : 0;
    const cls = ratio >= 0.999 ? "yes" : "warn";
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  }
  return `<span class="pill ${v ? "yes" : "no"}">${esc(label)}</span>`;
}

export function channelIcon(ch) {
  return (ch || "").toLowerCase().includes("call") ? `${icon("phone")} звонок` : `${icon("chat")} чат`;
}

const ICONS = {
  chat: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5a9.5 9.5 0 0 1 19 0Z"/><path d="M7 9h10M7 13h7"/>',
  phone: '<path d="m5 3 4 1 1 5-3 2a16 16 0 0 0 6 6l2-3 5 1 1 4c0 2-2 3-4 2C9 19 5 15 3 7 2 5 3 3 5 3Z"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  back: '<path d="M19 12H5m6-6-6 6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  up: '<path d="m6 15 6-6 6 6"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  refresh: '<path d="M20 7V3l-3 3a8 8 0 1 0 3 9M20 7h-5"/>',
  alert: '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v1"/>',
  filter: '<path d="M3 4h18l-7 8v7l-4 2V12L3 4Z"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>',
};

export function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ICONS.arrow}</svg>`;
}

let runningJobs = [];
let jobsRequest = null;
export async function refreshJobs() {
  if (jobsRequest) return jobsRequest;
  jobsRequest = api.get('/api/jobs').then((jobs) => {
    runningJobs = jobs.filter((job) => job.status === 'running');
    const box = document.getElementById('globalJobStatus');
    if (box) {
      box.hidden = runningJobs.length === 0;
      box.innerHTML = runningJobs.map((job) => `<div>${icon('refresh')} <b>Идёт оценка: ${job.done}/${job.total}</b> <span>Версия правил v${esc(job.checklist_version ?? '—')}. Задача продолжится при переходе на другой экран.</span></div>`).join('');
    }
    window.dispatchEvent(new CustomEvent('cqa:jobs', { detail: runningJobs }));
    return runningJobs;
  }).finally(() => { jobsRequest = null; });
  return jobsRequest;
}
export function hasRunningJob() { return runningJobs.length > 0; }
export function notifyJobStarted() { return refreshJobs().catch(() => runningJobs); }

export function closeModals() {
  document.getElementById('modal-root').replaceChildren();
  document.querySelectorAll('header.topbar, main, #globalJobStatus').forEach((el) => { el.inert = false; });
}

export const SPEAKER = { client: "Клиент", operator: "Оператор", bot: "Бот", system: "Система" };

// Опрос задачи оценки: onTick(job) вызывается раз в секунду, промис завершается по окончании задачи
export async function pollJob(jobId, onTick) {
  for (;;) {
    const job = await api.get(`/api/jobs/${encodeURIComponent(jobId)}`);
    onTick && onTick(job);
    if (job.status !== "running") return job;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

export function jobBox(job, title = "Оценка") {
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  const errs = (job.results || []).filter((r) => r.status !== "ok").length;
  const status = job.status === "running" ? "идёт" : job.status === "done" ? "готово" : "ошибка";
  return `<div class="progress">
    <div class="row"><b>${esc(title)}: ${status}</b><span class="spacer"></span>
      <span class="small">${job.done}/${job.total} диалогов · вызовов модели ${job.llm_calls ?? 0} · ${job.elapsed_sec ?? 0} с · ${fmtMoney(job.cost_usd)}${errs ? ` · <span style="color:var(--danger)">ошибок ${errs}</span>` : ""}</span></div>
    <div class="bar" role="progressbar" aria-label="${esc(title)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
    ${job.error ? `<div class="small" role="alert">Оценка прервана. Проверьте настройки и повторите запрос.</div>` : ""}</div>`;
}

// Выбор значения критерия для решения по апелляции
export function valueOptions(criterion) {
  if (!criterion) return [];
  const opts = [];
  if (criterion.type === "scale") {
    const steps = (criterion.scale || []).map((s) => Number(s.value)).sort((a, b) => b - a);
    const top = steps[0];
    for (const s of steps) opts.push({ value: s, label: `${s} из ${top}` });
  } else {
    opts.push({ value: true, label: criterion.type === "flag" ? "Да — грубая ошибка" : "Да" });
    opts.push({ value: false, label: criterion.type === "flag" ? "Нет — нарушений нет" : "Нет" });
  }
  if (criterion.na_allowed && criterion.type !== "flag") opts.push({ value: null, label: "Не применяется (N/A)" });
  return opts;
}
