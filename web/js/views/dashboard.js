// Дашборд: качество оценённых диалогов, охват метрик и переходы к проверке.
import { api, qs } from "../api.js";
import { errorToast, esc, fmtMoney, fmtScore, state } from "../ui.js";

let group = "";
let criterionSort = "violations";
let renderSeq = 0;

const isScore = (value) => typeof value === "number" && Number.isFinite(value);
const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
const listHref = (groupId, filters = {}) => "#/dialogs?" + new URLSearchParams({ group_id: groupId, ...filters });

function orderedCriteria(criteria, order) {
  return [...criteria].sort((a, b) => {
    const difference = order === "rate"
      ? (isScore(a.rate) ? a.rate : Infinity) - (isScore(b.rate) ? b.rate : Infinity)
      : count(b.fail) - count(a.fail);
    return difference || String(a.name || a.criterion_id).localeCompare(String(b.name || b.criterion_id), "ru");
  });
}

export async function renderDashboard(root, params = [], query = null, alive = () => true) {
  const seq = ++renderSeq;
  const current = () => alive() && seq === renderSeq;
  if (query?.has("group_id")) group = query.get("group_id") || "";
  if (["violations", "rate"].includes(query?.get("order"))) criterionSort = query.get("order");
  const selectedGroup = group;
  root.innerHTML = `<div class="loading" role="status">Загружаем сводку…</div>`;

  let groups, stats, rows, cfg;
  try {
    // Один полный список даёт базу языковых средних и состояния загрузки без запроса каждой карточки.
    [groups, stats, rows, cfg] = await Promise.all([
      api.get("/api/groups"),
      api.get("/api/stats" + qs({ group_id: selectedGroup })),
      api.get("/api/conversations" + qs({ group_id: selectedGroup })),
      api.get("/api/config").catch(() => null),
    ]);
  } catch (error) {
    if (!current()) return;
    root.innerHTML = `<div class="page-head"><h1>Дашборд</h1></div><div class="empty" role="alert"><p>Не удалось загрузить сводку. Проверьте соединение и повторите запрос.</p><button class="btn" data-retry>Повторить запрос</button></div>`;
    root.querySelector("[data-retry]").addEventListener("click", () => renderDashboard(root, params, query, alive));
    errorToast(error);
    return;
  }
  if (!current()) return;

  const criteria = (stats.criteria || []).filter((c) => c.criterion_id !== "critical");
  const critical = rows.filter((row) => row.evaluation?.status === "ok" && row.evaluation.critical_flag);
  const numericRows = rows.filter((row) => row.evaluation?.status === "ok" && isScore(row.evaluation.final_score));
  const errors = rows.filter((row) => row.evaluation && row.evaluation.status !== "ok").length;
  const unscored = rows.filter((row) => !row.evaluation).length;
  const appealedDialogs = rows.filter((row) => count(row.open_appeals) > 0).length;
  const languageCounts = new Map();
  for (const row of numericRows) {
    const language = row.language || "—";
    languageCounts.set(language, (languageCounts.get(language) || 0) + 1);
  }
  const worst = orderedCriteria(criteria, "violations").find((c) => count(c.fail) > 0);
  // Не показываем работающий переход, пока сервер не объявил поддержку такого отбора.
  const canFilterCriterion = stats.criterion_filter_supported === true;
  const judges = Array.isArray(cfg?.judges) ? cfg.judges : [];
  const scope = selectedGroup ? `Группа ${selectedGroup}` : "Все группы";

  const criterionAction = (criterion) => canFilterCriterion
    ? `<a class="btn" data-dialog-filter href="${esc(listHref(selectedGroup, { criterion_id: criterion.criterion_id, criterion_violation: true, sort: "score_asc" }))}" aria-label="Показать диалоги с нарушением критерия ${esc(criterion.name || criterion.criterion_id)}">Показать нарушения: ${count(criterion.fail)}</a>`
    : "";

  root.innerHTML = `
    <div class="page-head"><h1>Дашборд</h1>
      <label class="field"><span>Группа диалогов</span><select data-g><option value="">Все группы</option>${groups.map((g) => `<option value="${esc(g.group_id)}" ${g.group_id === selectedGroup ? "selected" : ""}>${esc(g.group_id)}</option>`).join("")}</select></label>
    </div>
    <div class="dashboard-summary"><p><strong>${esc(scope)}</strong> · Загружено ${rows.length} · Оценено ${count(stats.evaluated)} · С ошибкой ${errors} · Без оценки ${unscored}</p>
      <p class="muted small">Сводка по последним оценкам с учётом решений по апелляциям. Отсутствие отметки «Требует проверки» не гарантирует правильность оценки ИИ.</p></div>
    ${stats.evaluated ? "" : `<div class="empty">${rows.length ? "Оценок пока нет." : "В выбранной группе ещё нет диалогов."} <a href="${esc(listHref(selectedGroup))}">${state.role === "reviewer" ? "Открыть диалоги и служебные действия" : "Открыть список диалогов"}</a>.</div>`}
    <div class="tiles dashboard-quality-tiles">
      <a class="card tile link" data-dialog-filter data-space-link href="${esc(listHref(selectedGroup, { critical: true }))}"><span class="v">${count(stats.critical_count)}</span><span class="l">Грубые ошибки →</span></a>
      <a class="card tile link" data-dialog-filter data-space-link href="${esc(listHref(selectedGroup, { needs_review: true }))}"><span class="v">${count(stats.needs_review_count)}</span><span class="l">Требует проверки →</span></a>
      <a class="card tile link" data-dialog-filter data-space-link href="${esc(listHref(selectedGroup, { open_appeals: true }))}"><span class="v">${count(stats.open_appeals)}</span><span class="l">Открытых апелляций в ${appealedDialogs} диалогах →</span></a>
      <div class="card tile"><span class="v score" title="${esc(stats.avg_score ?? "Нет числовых оценок")}">${fmtScore(stats.avg_score)}</span><span class="l">Средний итог по ${numericRows.length} оценкам с числовым баллом</span></div>
    </div>
    ${worst ? `<section class="dashboard-problem-summary" aria-labelledby="dashboard-problem"><h2 id="dashboard-problem">Чаще всего снижен балл: ${esc(worst.name || worst.criterion_id)}</h2><p>Нарушений — <strong>${count(worst.fail)}</strong>. Основание сравнения — число оценок, где критерий выполнен не полностью.</p>${criterionAction(worst)}</section>` : stats.evaluated ? `<p class="dashboard-problem-summary">В оценках этой выборки нет снижения баллов по применимым критериям. Грубые ошибки учитываются отдельно.</p>` : ""}
    <section class="card card-pad dashboard-criteria" aria-labelledby="dashboard-criteria-title">
      <div class="row"><h2 id="dashboard-criteria-title">Выполнение критериев</h2><span class="spacer"></span><label class="field"><span>Сортировать критерии</span><select data-order><option value="violations" ${criterionSort === "violations" ? "selected" : ""}>Сначала больше нарушений</option><option value="rate" ${criterionSort === "rate" ? "selected" : ""}>Сначала ниже доля баллов</option></select></label></div>
      <p class="muted small">Доля набранных баллов рассчитана только среди применимых оценок. База каждого критерия — оценки, в которых он присутствует.</p>
      ${canFilterCriterion ? "" : `<p class="muted small">Отбор диалогов по отдельному критерию пока недоступен.</p>`}
      <div class="dashboard-criterion-list" data-criteria-list></div>
    </section>
    <section class="card card-pad dashboard-languages" aria-labelledby="dashboard-language-title"><h2 id="dashboard-language-title">Средний итог по языкам</h2>
      <p class="muted small">n — число успешных оценок с числовым итогом, вошедших в среднее. Неоценённые, ошибки и пустые итоги исключены.</p>
      <div class="dashboard-language-list">${Object.entries(stats.by_language || {}).map(([language, average]) => `<div class="dashboard-language"><strong>${esc(language)}</strong><span class="score" title="${esc(average)}">${fmtScore(average)}</span><span class="muted">n = ${languageCounts.get(language) || 0}</span></div>`).join("") || `<p class="muted">Нет числовых оценок для сравнения языков.</p>`}</div>
    </section>
    <details class="card card-pad dashboard-critical"><summary>Грубые ошибки: ${critical.length} · объяснения</summary>
      <div class="dashboard-critical-list">${critical.map((row) => `<article class="dashboard-critical-item"><div class="row"><h3><a class="dialog-id" href="#/dialog/${encodeURIComponent(row.conversation_id)}">${esc(row.conversation_id)}</a></h3><span>${esc(row.language || "—")}</span><span class="spacer"></span><span class="score">${fmtScore(row.evaluation.final_score)}</span></div><p>${esc(row.evaluation.critical_reason || "Обоснование доступно в карточке диалога.")}</p></article>`).join("") || `<p class="muted">${stats.evaluated ? "В сохранённых оценках этой выборки грубых ошибок не отмечено." : "Для этой выборки пока нет оценок."}</p>`}</div>
    </details>
    <details class="card card-pad dashboard-operations"><summary>Технические сведения об оценке</summary><div class="stack">
      <p>Расчётная стоимость одной оценки: <strong>${fmtMoney(stats.avg_cost_usd)}</strong>; всей выборки: <strong>${fmtMoney(stats.total_cost_usd)}</strong>.</p>
      <p class="muted small">Расчёт по настроенным тарифам и сохранённому использованию. Это не подтверждённый счёт провайдера; $0 не доказывает бесплатность последующих проверок.</p>
      ${judges.length ? `<p>Настроенные модели: ${judges.map((judge) => esc(judge.label || judge.model || judge.name || "Модель")).join(", ")}. Фактически использованные модели, вызовы и время доступны в подробностях каждой оценки.</p>` : `<p>Фактически использованные модели, вызовы и время доступны в подробностях каждой оценки.</p>`}
      ${state.role === "reviewer" ? `<a href="#/settings">Открыть настройки оценки</a>` : ""}
    </div></details>`;

  const paintCriteria = () => {
    const box = root.querySelector("[data-criteria-list]");
    box.innerHTML = orderedCriteria(criteria, criterionSort).map((criterion) => `<article class="dashboard-criterion">
      <div class="dashboard-criterion-head"><h3>${esc(criterion.name || criterion.criterion_id)}</h3><strong>${count(criterion.fail)} нарушений</strong></div>
      <div class="dashboard-criterion-bar"><div class="track" aria-hidden="true"><i style="width:${isScore(criterion.rate) ? Math.max(0, Math.min(100, criterion.rate)) : 0}%"></i></div><span>${isScore(criterion.rate) ? `${esc(criterion.rate)}% баллов` : "Нет применимых баллов"}</span></div>
      <p class="muted small">Применим к ${count(criterion.applicable)} из ${count(criterion.n)} оценок; не применяется (N/A) — ${count(criterion.na)}.</p>
      ${criterionAction(criterion)}
    </article>`).join("") || `<p class="muted">Нет оценок критериев.</p>`;
  };
  paintCriteria();
  root.querySelector("[data-g]").addEventListener("change", (event) => {
    group = event.target.value;
    location.hash = "#/dashboard?" + new URLSearchParams({ group_id: group, order: criterionSort });
  });
  root.querySelector("[data-order]").addEventListener("change", (event) => {
    criterionSort = event.target.value;
    history.replaceState(null, "", "#/dashboard?" + new URLSearchParams({ group_id: selectedGroup, order: criterionSort }));
    paintCriteria();
  });
  root.querySelectorAll("[data-space-link]").forEach((link) => link.addEventListener("keydown", (event) => {
    if (event.key === " ") { event.preventDefault(); link.click(); }
  }));
}

export { renderDashboard as render };
