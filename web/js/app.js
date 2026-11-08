// Точка входа: роутинг по hash, роль, режим встраивания (?embed=1, ?conversationId=...).
import { api } from "./api.js";
import { closeModals, errorToast, esc, icon, refreshJobs, state } from "./ui.js";
import { renderDialogs } from "./views/dialogs.js";
import { renderDialog } from "./views/dialog.js";
import { renderAppeals } from "./views/appeals.js";
import { renderChecklist } from "./views/checklist.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderSettings } from "./views/settings.js";

const routes = {
  dialogs: renderDialogs,
  dialog: renderDialog,
  appeals: renderAppeals,
  checklist: renderChecklist,
  dashboard: renderDashboard,
  settings: renderSettings,
};

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const [name, ...rest] = path.split("/");
  return { name: name || "dialogs", params: rest.map(decodeURIComponent), query: new URLSearchParams(query || "") };
}

let renderSeq = 0;
async function route() {
  const root = document.getElementById("app");
  const urlRole = new URLSearchParams(location.search).get('role') === 'reviewer' ? 'reviewer' : 'operator';
  if (state.role !== urlRole) setRole(urlRole);
  const seq = ++renderSeq;
  closeModals();
  document.querySelector('.topbar').classList.remove('menu-open');
  document.querySelector('.nav-toggle').setAttribute('aria-expanded', 'false');
  root.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    const { name, params, query } = parseHash();
    const view = routes[name] || routes.dialogs;
    document.querySelectorAll("#nav a").forEach((a) => {
      const active = a.dataset.route === (name === "dialog" ? "dialogs" : name);
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    await view(root, params, query, () => seq === renderSeq);
  } catch (e) {
    if (seq === renderSeq) {
      root.innerHTML = `<div class="empty" role="alert"><h1>Не удалось загрузить экран</h1><p>${esc(e.message || e)}</p><button class="btn primary" data-retry>Повторить запрос</button></div>`;
      root.querySelector('[data-retry]').addEventListener('click', route);
      errorToast(e);
    }
  }
  refreshAppealsBadge();
}

export function rerender() { route(); }

function setRole(role) {
  state.role = role;
  document.querySelectorAll("#roleSwitch button").forEach((b) => b.classList.toggle("active", b.dataset.role === role));
  document.body.dataset.role = role;
  const url = new URL(location.href);
  url.searchParams.set("role", role);
  history.replaceState(null, "", url);
  document.querySelectorAll("#roleSwitch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.role === role)));
  document.querySelectorAll('[data-reviewer-nav]').forEach((el) => { el.hidden = role !== 'reviewer'; });
}

async function refreshAppealsBadge() {
  try {
    const open = await api.get("/api/appeals?status=open");
    document.getElementById("appealsCount").textContent = open.length ? String(open.length) : "";
  } catch { /* сервер мог быть недоступен — бейдж не критичен */ }
}

function init() {
  const url = new URLSearchParams(location.search);
  setRole(url.get("role") === "reviewer" ? "reviewer" : "operator");
  if (url.get("embed") === "1") { state.embed = true; document.body.classList.add("embed"); }
  const cid = url.get("conversationId");
  if (cid) history.replaceState(null, "", `${location.pathname}${location.search}#/dialog/${encodeURIComponent(cid)}`);
  else if (!location.hash) history.replaceState(null, "", `${location.pathname}${location.search}#/dialogs`);
  document.querySelectorAll("#roleSwitch button").forEach((b) => b.addEventListener("click", () => { setRole(b.dataset.role); route(); }));
  const menu = document.querySelector('.nav-toggle');
  document.querySelector('.skip-link').addEventListener('click', (event) => { event.preventDefault(); document.getElementById('app').focus(); });
  menu.innerHTML = `${icon('menu')} Меню`;
  menu.addEventListener('click', () => {
    const open = document.querySelector('.topbar').classList.toggle('menu-open');
    menu.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.querySelector('.topbar').classList.contains('menu-open')) {
      document.querySelector('.topbar').classList.remove('menu-open');
      menu.setAttribute('aria-expanded', 'false'); menu.focus();
    }
  });
  window.addEventListener("hashchange", route);
  window.addEventListener('popstate', () => {
    const urlRole = new URLSearchParams(location.search).get('role') === 'reviewer' ? 'reviewer' : 'operator';
    if (urlRole !== state.role) route();
  });
  refreshJobs().catch(() => {});
  setInterval(() => { if (!document.hidden) refreshJobs().catch(() => {}); }, 2500);
  route();
}

init();
