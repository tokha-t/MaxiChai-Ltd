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
  toast(e && e.message ? e.message : String(e), "error");
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
  const close = () => { back.remove(); if (previousFocus?.isConnected) previousFocus.focus(); };
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
  if (v === null || v === undefined) return "";
  return v >= 85 ? "hi" : v >= 60 ? "mid" : "lo";
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
  if (v === null || v === undefined) return `<span class="pill na">N/A</span>`;
  if (row.type === "scale") {
    const ratio = row.max_points ? (row.points ?? 0) / row.max_points : 0;
    const cls = ratio >= 0.999 ? "yes" : ratio >= 0.5 ? "warn" : "no";
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  }
  return `<span class="pill ${v ? "yes" : "no"}">${esc(label)}</span>`;
}

export function channelIcon(ch) {
  return (ch || "").toLowerCase().includes("call") ? "📞 звонок" : "💬 чат";
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
    ${job.error ? `<div class="small" role="alert">${esc(job.error)}</div>` : ""}</div>`;
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
  if (criterion.na_allowed && criterion.type !== "flag") opts.push({ value: null, label: "N/A" });
  return opts;
}
