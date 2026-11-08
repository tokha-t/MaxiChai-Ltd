// Дашборд: сводка по оценкам, слабые места операторов, все грубые ошибки с объяснениями.
import { api, qs } from "../api.js";
import { errorToast, esc, fmtMoney, fmtScore, scoreClass, state } from "../ui.js";

let group = "";
let renderSeq = 0;

export async function renderDashboard(root, params = [], query = null, alive = () => true) {
  const seq = ++renderSeq;
  const current = () => alive() && seq === renderSeq;
  const selectedGroup = group;
  const [groups, stats, crit, cfg] = await Promise.all([
    api.get("/api/groups"),
    api.get("/api/stats" + qs({ group_id: selectedGroup })),
    api.get("/api/conversations" + qs({ group_id: selectedGroup, critical: true })),
    api.get("/api/config").catch(() => null),
  ]);
  if (!current()) return;
  const judges = cfg && cfg.judges ? cfg.judges : [];
  const crits = (stats.criteria || []).filter((c) => c.criterion_id !== "critical");
  root.innerHTML = `
    <div class="page-head"><h1>Дашборд</h1>
      <select data-g aria-label="Группа диалогов"><option value="">Все группы</option>${groups.map((g) => `<option value="${esc(g.group_id)}" ${g.group_id === selectedGroup ? "selected" : ""}>${esc(g.group_id)}</option>`).join("")}</select>
    </div>
    ${stats.evaluated ? "" : `<div class="card empty" style="margin-bottom:16px">Нет оценок — откройте <a href="#/dialogs">Диалоги</a> и нажмите «Оценить».</div>`}
    <div class="tiles">
      <div class="card tile"><div class="v">${stats.evaluated}</div><div class="l">оценено диалогов</div></div>
      <div class="card tile"><div class="v score ${scoreClass(stats.avg_score)}">${fmtScore(stats.avg_score)}</div><div class="l">средний итог</div></div>
      <div class="card tile link" role="link" tabindex="0" data-go="critical"><div class="v" style="color:var(--danger)">${stats.critical_count}</div><div class="l">с грубыми ошибками →</div></div>
      <div class="card tile link" role="link" tabindex="0" data-go="review"><div class="v" style="color:var(--warn)">${stats.needs_review_count}</div><div class="l">⚠ нужна проверка →</div></div>
      <div class="card tile link" role="link" tabindex="0" data-go="appeals"><div class="v">${stats.open_appeals}</div><div class="l">открытых апелляций →</div></div>
      <div class="card tile"><div class="v">${fmtMoney(stats.avg_cost_usd)}</div><div class="l">стоимость одной оценки${judges.length && judges.every((j) => j.free) ? " (бесплатные тарифы)" : ""}</div></div>
    </div>
    <div class="card card-pad" style="margin-bottom:16px">
      <h2>Грубые ошибки — ${crit.length}</h2>
      ${crit.length ? `<table class="grid" style="margin-top:8px"><tr><th>Диалог</th><th>Язык</th><th>Объяснение системы</th><th>Итог</th></tr>
        ${crit.map((r) => `<tr class="clickable" data-id="${esc(r.conversation_id)}"><td class="mono"><a href="#/dialog/${encodeURIComponent(r.conversation_id)}"><b>${esc(r.conversation_id)}</b></a></td><td>${esc(r.language || "")}</td>
          <td class="reason" style="max-width:none">${esc(r.evaluation.critical_reason || "")}</td><td>${fmtScore(r.evaluation.final_score)}</td></tr>`).join("")}</table>`
        : `<div class="muted">${stats.evaluated ? "Грубых ошибок не найдено." : "Оцените диалоги, чтобы увидеть нарушения."}</div>`}
    </div>
    <div class="card card-pad" style="margin-bottom:16px">
      <h2>Выполнение критериев</h2>
      <div class="muted small" style="margin-bottom:6px">Доля набранных баллов среди диалогов, где критерий применим</div>
      ${crits.map((c) => `<div class="hbar"><div>${esc(c.name || c.criterion_id)}</div>
        <div class="track"><i style="width:${Math.max(0, Math.min(100, c.rate ?? 0))}%"></i></div>
        <div class="small"><b>${c.rate === null || c.rate === undefined ? "—" : c.rate + "%"}</b> <span class="muted">· нарушений ${c.fail} · N/A ${c.na}</span></div></div>`).join("") || `<div class="muted">Нет оценок.</div>`}
    </div>
    <div class="card card-pad">
      <h2>Средний итог по языкам</h2>
      <div class="row" style="margin-top:8px">${Object.entries(stats.by_language || {}).map(([k, v]) => `<span class="chip">${esc(k)}: <b class="score ${scoreClass(v)}">${fmtScore(v)}</b></span>`).join("") || `<span class="muted">—</span>`}</div>
      ${judges.length ? `<div class="muted small" style="margin-top:12px">Модели-судьи: ${judges.map((j) => `${esc(j.label)}${j.local ? " (локально)" : j.free ? " (бесплатно)" : ""}`).join(", ")}</div>` : ""}
    </div>`;

  root.querySelector("[data-g]").addEventListener("change", async (e) => {
    group = e.target.value;
    try { await renderDashboard(root, params, query, alive); }
    catch (error) { if (alive()) errorToast(error); }
  });
  root.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/dialog/${encodeURIComponent(tr.dataset.id)}`; }));
  root.querySelectorAll("[data-go]").forEach((t) => {
    t.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); t.click(); }
    });
    t.addEventListener("click", () => {
    const f = state.dialogFilters;
    Object.assign(f, { group_id: selectedGroup, critical: false, needs_review: false, language: "", channel: "", q: "" });
    if (t.dataset.go === "critical") { f.critical = true; location.hash = "#/dialogs"; }
    else if (t.dataset.go === "review") { f.needs_review = true; location.hash = "#/dialogs"; }
    else location.hash = "#/appeals";
    });
  });
}

export { renderDashboard as render };
