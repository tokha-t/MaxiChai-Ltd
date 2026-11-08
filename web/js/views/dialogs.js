// Список диалогов: рабочая выборка оператора и отдельные служебные действия проверяющего.
import { api, qs } from "../api.js";
import { channelIcon, errorToast, esc, fmtDate, fmtScore, h, icon, jobBox, modal, state, toast, refreshJobs, hasRunningJob, notifyJobStarted, profileOptions } from "../ui.js";
import { checksSummary, profileId, selectedProfile } from "../evaluation-meta.js";
import { DIALOG_FILTER_DEFAULTS, readDialogFilters, dialogListHash, dialogScore, visibleDialogRows, summarizeDialogRows, dialogScopeCount } from "../dialog-list-model.js";

const trackedJobs = new Map();
let startingJob = false;
let jobsUnavailable = false;
let renderSequence = 0;
let listObserver = null;
let filtersOpen = false;
let serviceOpen = false;
let searchEditing = false;
const selected = new Set();
let profiles = [];
const busy = () => startingJob || trackedJobs.size > 0 || hasRunningJob();

window.addEventListener("cqa:jobs", (event) => {
  listObserver?.busy();
  if (listObserver?.alive()) for (const job of event.detail || []) if (!trackedJobs.has(job.job_id)) followJob(job.job_id, job);
});

function saveFilterUrl(filters, replace = false) {
  const hash = dialogListHash(filters);
  state.dialogListHash = hash;
  if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", `${location.pathname}${location.search}${hash}`);
}

export async function renderDialogs(root, params = [], query = null, alive = () => true) {
  const sequence = ++renderSequence;
  const isCurrent = () => alive() && sequence === renderSequence;
  const refresh = () => { if (alive()) return renderDialogs(root, params, null, alive); };
  const observer = { alive: isCurrent, busy: () => {}, tick: () => {}, done: refresh };
  listObserver = observer;
  const f = readDialogFilters(query, state.dialogFilters);
  if (f.checklist_id) state.evaluationChecklistId = profileId(f.checklist_id);
  Object.assign(state.dialogFilters, f);
  saveFilterUrl(f, true);
  const reviewer = state.role === "reviewer";
  root.setAttribute("aria-busy", "true");
  let groups, serverRows, cfg, settings, running, capability, activeChecklist, availableProfiles;
  try {
    [groups, serverRows, cfg, settings, running, capability, activeChecklist, availableProfiles] = await Promise.all([
      api.get("/api/groups"),
      api.get("/api/conversations" + qs({ group_id: f.group_id, language: f.language, channel: f.channel, critical: f.critical || undefined, needs_review: f.needs_review || undefined, q: f.q, criterion_id: f.criterion_id, criterion_violation: f.criterion_id ? f.criterion_violation : undefined })),
      reviewer ? api.get("/api/config").catch(() => null) : null,
      reviewer ? api.get("/api/settings").catch(() => null) : null,
      refreshJobs().then((jobs) => { jobsUnavailable = false; return jobs; }).catch(() => { jobsUnavailable = true; return []; }),
      f.criterion_id ? api.get("/api/stats" + qs({ group_id: f.group_id })) : null,
      api.get("/api/checklists/active").catch(() => null),
      reviewer ? api.get("/api/checklists") : [],
    ]);
  } catch {
    if (!isCurrent()) return;
    root.removeAttribute("aria-busy");
    root.innerHTML = `<section class="dialogs-page"><div class="page-head"><h1>Диалоги</h1></div><div class="empty error-state" role="alert"><h2>Не удалось загрузить диалоги</h2><p>Данные не получены. Проверьте соединение с сервером и повторите запрос.</p><button class="btn primary" data-retry>${icon("refresh")} Повторить загрузку</button></div></section>`;
    root.querySelector("[data-retry]").addEventListener("click", refresh);
    errorToast(new Error("Не удалось загрузить диалоги. Повторите запрос."));
    return;
  }
  if (!isCurrent()) return;
  profiles = availableProfiles;
  root.removeAttribute("aria-busy");
  const unsupportedCriterion = !!f.criterion_id && capability?.criterion_filter_supported !== true;
  const rows = unsupportedCriterion ? [] : visibleDialogRows(serverRows, f);
  const count = dialogScopeCount(groups, f.group_id);
  const summary = summarizeDialogRows(rows);
  const scopeName = f.group_id ? `группа ${f.group_id}` : "все группы";
  const activeConditions = [f.group_id, f.language, f.channel, f.critical, f.needs_review, f.open_appeals, f.criterion_id].filter(Boolean).length;
  const visibleIds = new Set(rows.map((row) => row.conversation_id));
  for (const id of selected) if (!visibleIds.has(id)) selected.delete(id);
  const dateSort = f.sort.startsWith("date_") ? (f.sort.endsWith("asc") ? "ascending" : "descending") : "none";
  const scoreSort = f.sort.startsWith("score_") ? (f.sort.endsWith("asc") ? "ascending" : "descending") : "none";

  const focus = root.contains(document.activeElement) ? document.activeElement : null;
  const focusKey = focus?.dataset.f || focus?.dataset.quick || focus?.dataset.sort;
  const selection = focus?.type === "search" ? [focus.selectionStart, focus.selectionEnd] : null;
  root.innerHTML = `<section class="dialogs-page">
    <div class="page-head dialogs-head"><h1>Диалоги</h1><span class="spacer"></span>
      ${reviewer ? serviceHtml(cfg, settings, f, serviceOpen) : ""}
    </div>
    <div id="jobArea" class="list-jobs"></div>
    ${jobsUnavailable ? `<div class="notice" role="status">Статус текущих задач недоступен. Запуск временно отключён. <button class="btn small" data-act="retry-jobs">Повторить</button></div>` : ""}
    <div class="list-search"><label class="field"><span>Поиск по ID</span><input type="search" data-f="q" aria-label="Поиск по ID" value="${esc(f.q)}" placeholder="Введите ID диалога" autocomplete="off"></label>${f.q ? `<button class="btn" data-act="clear-search">Очистить поиск</button>` : ""}</div>
    <div class="quick-views" aria-label="Быстрые представления">
      <button class="btn small" data-quick="low" aria-pressed="${f.sort === "score_asc"}">Сначала низкие оценки</button>
      <button class="btn small" data-quick="appeals" aria-pressed="${f.open_appeals}">Открытые апелляции</button>
      <button class="btn small" data-quick="review" aria-pressed="${f.needs_review}">Требует проверки</button>
    </div>
    <div class="list-controls">
      <details class="list-filters" ${filtersOpen ? "open" : ""}><summary>${icon("filter")} Фильтры${activeConditions ? ` <span class="filter-count">${activeConditions}</span>` : ""}</summary><div class="filter-fields">
        <label class="field"><span>Группа</span><select data-f="group_id"><option value="">Все группы</option>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select></label>
        <label class="field"><span>Язык</span><select data-f="language"><option value="">Все языки</option><option>RU</option><option>KZ</option><option>EN</option></select></label>
        <label class="field"><span>Канал</span><select data-f="channel"><option value="">Чаты и звонки</option><option value="chat">Чаты</option><option value="call">Звонки</option></select></label>
        <label class="check"><input type="checkbox" data-f="critical"> Грубые ошибки</label>
        <label class="check"><input type="checkbox" data-f="needs_review"> Требует проверки</label>
        <label class="check"><input type="checkbox" data-f="open_appeals"> Есть открытые апелляции</label>
        <button class="btn small" data-act="reset-filters">Сбросить фильтры</button>
      </div></details>
      <label class="field list-sort"><span>Порядок</span><select data-f="sort" aria-label="Порядок диалогов"><option value="date_desc">Сначала новые оценки</option><option value="date_asc">Сначала старые оценки</option><option value="score_asc">По баллу: от низкого</option><option value="score_desc">По баллу: от высокого</option></select></label>
    </div>
    ${f.criterion_id ? `<div class="active-criterion">Критерий: <b>${esc(f.criterion_id)}</b>${f.criterion_violation ? " · с нарушением" : ""} <button class="btn small" data-act="clear-criterion" aria-label="Убрать фильтр по критерию ${esc(f.criterion_id)}">Убрать условие</button></div>` : ""}
    <div class="list-summary" aria-live="polite">
      ${unsupportedCriterion ? `<p>Область данных: ${esc(scopeName)} · ${count} диалогов.</p>` : `<p><b>Показано ${rows.length} из ${count}</b><span> · ${esc(scopeName)}</span></p><p class="small muted">${summary.scored ? `Средний балл по ${summary.scored} оценённым диалогам этой выборки: <b class="score-number">${fmtScore(summary.average)}</b>.` : "Нет оценок с числовым итогом в этой выборке."} Оценено: ${summary.evaluated}; без оценки: ${summary.unevaluated}; с ошибкой оценки: ${summary.failed}.</p>`}
    </div>
    ${reviewer && rows.length ? `<div class="selection-actions"><span data-selected-count>Выбрано: ${selected.size}</span><button class="btn" data-act="eval-selected" disabled>Оценить выбранные (${selected.size})</button></div>` : ""}
    ${unsupportedCriterion ? `<div class="empty"><h2>Фильтр по критерию пока недоступен</h2><p>Сервер ещё не поддерживает эту выборку. Непроверенный список не показан, чтобы не подменять результаты перехода из дашборда.</p><button class="btn" data-act="clear-criterion">Показать диалоги без этого условия</button></div>` : rows.length ? `<div class="card dialogs-table-wrap"><table class="grid dialogs-table"><caption class="sr-only">Диалоги: ${esc(scopeName)}. Неоценённые и ошибочные результаты при сортировке по баллу идут после числовых оценок.</caption><thead><tr>
      ${reviewer ? `<th scope="col" class="select-cell"><label class="row-select"><input type="checkbox" data-all aria-label="Выбрать все диалоги в выборке"></label></th>` : ""}
      <th scope="col" class="dialog-cell">Диалог</th>
      <th scope="col" class="score-cell" aria-sort="${scoreSort}"><button class="sort-button" data-sort="score" aria-label="Итоговый балл: сортировать ${f.sort === "score_asc" ? "по убыванию" : "по возрастанию"}">Итог ${f.sort.startsWith("score_") ? icon(f.sort.endsWith("asc") ? "up" : "down") : ""}</button></th>
      <th scope="col" class="status-cell">Состояние</th><th scope="col" class="appeals-cell">Апелляции</th>
      <th scope="col" class="date-cell" aria-sort="${dateSort}"><button class="sort-button" data-sort="date" aria-label="Дата оценки: сортировать ${f.sort === "date_desc" ? "от старых к новым" : "от новых к старым"}">Дата оценки ${f.sort.startsWith("date_") ? icon(f.sort.endsWith("asc") ? "up" : "down") : ""}</button></th>
      <th scope="col" class="details-cell"><span class="sr-only">Подробности</span></th>
    </tr></thead><tbody>${rows.map((row) => rowHtml(row, reviewer, activeChecklist?.version)).join("")}</tbody></table></div>` : emptyHtml(f, count, reviewer)}
  </section>`;

  const changeFilters = (patch, replace = false) => {
    Object.assign(state.dialogFilters, patch);
    saveFilterUrl(state.dialogFilters, replace);
    refresh();
  };
  root.querySelectorAll("[data-f]").forEach((el) => {
    const key = el.dataset.f;
    if (el.type === "checkbox") el.checked = !!f[key]; else el.value = f[key] ?? "";
    const apply = () => {
      const value = el.type === "checkbox" ? el.checked : el.value;
      changeFilters({ [key]: value }, key === "q" && searchEditing);
      if (key === "q") searchEditing = true;
    };
    el.addEventListener(el.type === "search" ? "input" : "change", apply);
    if (el.type === "search") el.addEventListener("blur", () => { searchEditing = false; });
  });
  root.querySelector(".list-filters").addEventListener("toggle", (event) => { filtersOpen = event.currentTarget.open; });
  root.querySelector(".service-tools")?.addEventListener("toggle", (event) => { serviceOpen = event.currentTarget.open; });
  root.querySelector("[data-profile]")?.addEventListener("change", (event) => {
    state.evaluationChecklistId = profileId(event.target.value);
    changeFilters({ checklist_id: event.target.value });
  });
  root.querySelectorAll('[data-act="clear-search"]').forEach((button) => button.addEventListener("click", () => changeFilters({ q: "" })));
  root.querySelectorAll('[data-act="reset-filters"]').forEach((button) => button.addEventListener("click", () => changeFilters({ ...DIALOG_FILTER_DEFAULTS, sort: f.sort })));
  root.querySelectorAll('[data-act="clear-criterion"]').forEach((button) => button.addEventListener("click", () => changeFilters({ criterion_id: "", criterion_violation: false })));
  root.querySelectorAll("[data-quick]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.quick === "low") changeFilters({ sort: "score_asc" });
    if (button.dataset.quick === "appeals") changeFilters({ open_appeals: !f.open_appeals });
    if (button.dataset.quick === "review") changeFilters({ needs_review: !f.needs_review });
  }));
  root.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => {
    const field = button.dataset.sort;
    changeFilters({ sort: field === "score" ? (f.sort === "score_asc" ? "score_desc" : "score_asc") : (f.sort === "date_desc" ? "date_asc" : "date_desc") });
  }));

  const syncSelection = () => {
    root.querySelectorAll("[data-select]").forEach((input) => { input.checked = selected.has(input.dataset.select); });
    const all = root.querySelector("[data-all]");
    if (all) { all.checked = selected.size > 0 && selected.size === rows.length; all.indeterminate = selected.size > 0 && selected.size < rows.length; }
    const button = root.querySelector('[data-act="eval-selected"]');
    if (button) { button.disabled = busy() || jobsUnavailable || !selected.size; button.textContent = `Оценить выбранные (${selected.size})`; }
    const counter = root.querySelector("[data-selected-count]");
    if (counter) counter.textContent = `Выбрано: ${selected.size}`;
  };
  root.querySelectorAll("[data-select]").forEach((input) => input.addEventListener("change", () => { input.checked ? selected.add(input.dataset.select) : selected.delete(input.dataset.select); syncSelection(); }));
  root.querySelector("[data-all]")?.addEventListener("change", (event) => { for (const row of rows) event.target.checked ? selected.add(row.conversation_id) : selected.delete(row.conversation_id); syncSelection(); });
  root.querySelectorAll("tr[data-id]").forEach((row) => row.addEventListener("click", (event) => {
    if (!event.target.closest("a, button, input, label, summary, details")) location.hash = `#/dialog/${encodeURIComponent(row.dataset.id)}`;
  }));
  const syncBusy = () => {
    if (!isCurrent()) return;
    root.querySelectorAll("[data-start]").forEach((button) => { button.disabled = busy() || jobsUnavailable; });
    root.querySelector('[data-act="eval-visible"]')?.toggleAttribute("disabled", busy() || jobsUnavailable || !rows.length || unsupportedCriterion);
    root.querySelector('[data-act="eval-group"]')?.toggleAttribute("disabled", busy() || jobsUnavailable || !count);
    syncSelection();
  };
  Object.assign(observer, { busy: syncBusy, tick: () => {
    if (!isCurrent()) return;
    const area = root.querySelector("#jobArea");
    area.innerHTML = [...trackedJobs.values()].map((tracked) => tracked.error
      ? `<div class="notice" role="status">Не удалось обновить ход оценки. Запуск другой задачи временно отключён. <button class="btn small" data-retry-job="${esc(tracked.id)}">Повторить обновление</button></div>`
      : tracked.last ? reviewer ? jobBox({ ...tracked.last, error: null }, "Оценка диалогов") : `<div class="notice" role="status">Идёт оценка: ${tracked.last.done}/${tracked.last.total} диалогов. Результаты обновятся автоматически.</div>` : "").join("");
    area.querySelectorAll("[data-retry-job]").forEach((button) => button.addEventListener("click", () => followJob(button.dataset.retryJob)));
  }});
  root.querySelector('[data-act="retry-jobs"]')?.addEventListener("click", refresh);
  root.querySelector('[data-act="eval-selected"]')?.addEventListener("click", () => evaluationPreview({ count: selected.size, scope: "Выбранные диалоги", conversation_ids: [...selected] }));
  root.querySelector('[data-act="eval-visible"]')?.addEventListener("click", () => evaluationPreview({ count: rows.length, scope: "Диалоги текущей выборки с учётом всех фильтров", conversation_ids: rows.map((row) => row.conversation_id) }));
  root.querySelector('[data-act="eval-group"]')?.addEventListener("click", () => evaluationPreview({ count, scope: f.group_id ? `Вся группа ${f.group_id}, независимо от поиска и фильтров` : "Все группы, независимо от поиска и фильтров", group_id: f.group_id }));
  root.querySelectorAll('[data-act="fetch"]').forEach((button) => button.addEventListener("click", () => fetchModal(refresh)));
  root.querySelectorAll('[data-act="upload"]').forEach((button) => button.addEventListener("click", () => uploadModal(refresh, alive)));
  root.querySelector('[data-act="save"]')?.addEventListener("click", () => saveModal(groups, f.group_id));
  root.querySelector('[data-act="export"]')?.addEventListener("click", () => saveModal(groups, f.group_id));
  for (const job of running) followJob(job.job_id, job);
  observer.tick(); syncBusy();
  if (focusKey) {
    const target = [...root.querySelectorAll("[data-f], [data-quick], [data-sort]")].find((el) => el.dataset.f === focusKey || el.dataset.quick === focusKey || el.dataset.sort === focusKey);
    if (target) { target.focus({ preventScroll: true }); if (selection && target.type === "search") target.setSelectionRange(...selection); }
  }
}

function serviceHtml(cfg, settings, filters, open) {
  return `<details class="service-tools" ${open ? "open" : ""}><summary class="btn">${icon("settings")} Служебные действия</summary><div class="service-content">
    <label class="field"><span>Профиль для запуска и выгрузки</span><select data-profile>${profileOptions(profiles, state.evaluationChecklistId)}</select></label>
    <p class="small muted">Выберите сохранённую версию явно. Этот профиль будет указан в запуске и выгрузке; смена активной версии не подменяет выбор.</p>
    <p class="small muted">Импорт, запуск оценки и JSON для сдачи. Демо-режим проверяющего не заменяет авторизацию.</p>
    <div class="service-actions"><button class="btn" data-start data-act="fetch">${icon("download")} Загрузить набор из API</button><button class="btn" data-start data-act="upload">${icon("plus")} Добавить диалог</button>
      <button class="btn" data-start data-act="eval-visible">Оценить текущую выборку</button><button class="btn" data-start data-act="eval-group">${filters.group_id ? "Оценить всю группу" : "Оценить все группы"}</button>
      <button class="btn" data-act="export">${icon("download")} Скачать результаты</button><button class="btn" data-act="save">Сохранить JSON в проект</button></div>
    <details class="technical-details"><summary>Настройки оценки</summary><p>${esc((cfg?.judges || []).map((judge) => judge.model).join(" + ") || cfg?.model || "Настройки не получены")}</p><a href="#/settings">Открыть настройки моделей</a></details>
    ${settings && !settings.ready ? `<p class="notice">Перед запуском оценки проверьте <a href="#/settings">настройки моделей</a>.</p>` : ""}
  </div></details>`;
}

function emptyHtml(filters, count, reviewer) {
  if (count > 0) return `<div class="empty"><h2>${filters.q ? `По запросу «${esc(filters.q)}» ничего не найдено` : "По выбранным условиям ничего не найдено"}</h2><p>Диалоги загружены. Измените поиск или условия выборки.</p><div class="row">${filters.q ? `<button class="btn" data-act="clear-search">Очистить поиск</button>` : ""}<button class="btn" data-act="reset-filters">Сбросить фильтры</button></div></div>`;
  return `<div class="empty"><h2>${filters.group_id ? "В этой группе ещё нет диалогов" : "Диалоги ещё не загружены"}</h2><p>${reviewer ? "Загрузите набор из API или добавьте диалог." : "Проверяющий может добавить диалоги для оценки."}</p>${reviewer ? `<div class="row"><button class="btn" data-start data-act="fetch">Загрузить набор</button><button class="btn" data-start data-act="upload">Добавить диалог</button></div>` : ""}</div>`;
}

function rowHtml(row, reviewer, activeVersion) {
  const ev = row.evaluation;
  const score = dialogScore(row);
  const id = String(row.conversation_id);
  const version = ev?.checklist_version;
  const versionContext = version != null
    ? `${ev.status === "ok" ? "Оценено" : "Запуск"} по v${esc(version)}${activeVersion != null && String(version) !== String(activeVersion) ? `; сейчас активна v${esc(activeVersion)}` : ""}`
    : "Не оценён";
  let scoreHtml = `<span class="muted">Не оценён</span>`;
  if (ev?.status === "ok") scoreHtml = score === null ? `<span class="muted">Нет числового итога</span>` : `<span class="score score-number">${fmtScore(score)}</span>${typeof ev.ai_score === "number" && ev.ai_score !== score ? `<div class="ai-to-final">ИИ <span class="score-number">${fmtScore(ev.ai_score)}</span> → <span class="score-number">${fmtScore(score)}</span></div>` : ""}`;
  else if (ev) scoreHtml = `<span class="pill no">Ошибка оценки</span>`;
  const critical = ev?.critical_flag;
  const issue = critical ? `<span class="pill crit">${icon("alert")} Грубая ошибка</span>${ev.critical_reason ? `<p class="reason-preview">${esc(ev.critical_reason.slice(0, 90))}${ev.critical_reason.length > 90 ? "…" : ""}</p><details class="reason-disclosure"><summary>Полное обоснование</summary><p>${esc(ev.critical_reason)}</p></details>` : ""}` : "";
  const review = ev?.needs_review ? `<span class="pill warn">Требует проверки</span>` : "";
  const clear = ev?.status === "ok" && !critical && !ev.needs_review ? `<span class="sr-only">Грубая ошибка не отмечена; дополнительная проверка не запрошена.</span><span class="muted" aria-hidden="true">—</span>` : "";
  return `<tr data-id="${esc(id)}" class="dialog-row clickable">
    ${reviewer ? `<td class="select-cell" data-label="Выбор"><label class="row-select"><input type="checkbox" data-select="${esc(id)}" aria-label="Выбрать диалог ${esc(id)}"></label></td>` : ""}
    <td class="dialog-cell" data-label="Диалог"><a class="dialog-id-link mono" href="#/dialog/${encodeURIComponent(id)}" aria-label="Открыть диалог ${esc(id)}">${esc(id)}</a>
      ${id.length > 24 ? `<details class="id-disclosure"><summary>Полный ID</summary><div class="id-full mono" tabindex="0" aria-label="Полный ID диалога">${esc(id)}</div></details>` : ""}
      <div class="dialog-meta">${channelIcon(row.channel)} <span>${esc(row.language || "Язык не указан")}</span></div></td>
    <td class="score-cell" data-label="Итог">${scoreHtml}</td>
    <td class="status-cell" data-label="Состояние">${issue}${review}${clear}${!ev ? `<span class="muted small">Ожидает оценки</span>` : ev.status !== "ok" ? `<span class="muted small">Результат недоступен</span>` : ""}</td>
    <td class="appeals-cell" data-label="Апелляции">${row.open_appeals ? `<span class="pill info">Открытых: ${row.open_appeals}</span>` : `<span class="muted small">Нет открытых</span>`}</td>
    <td class="date-cell" data-label="Дата оценки">${ev?.evaluated_at ? `<time datetime="${esc(ev.evaluated_at)}">${esc(fmtDate(ev.evaluated_at))}</time>` : `<span class="muted">—</span>`}</td>
    <td class="details-cell" data-label="Подробности"><details class="row-details"><summary aria-label="Подробности диалога ${esc(id)}">Подробности</summary><dl><dt>Полный ID</dt><dd class="id-full mono" tabindex="0">${esc(id)}</dd><dt>Версия правил</dt><dd>${versionContext}</dd><dt>Группа</dt><dd>${esc(row.group_id || "—")}</dd><dt>Реплик</dt><dd>${row.turns ?? "—"}</dd></dl></details></td>
  </tr>`;
}

function evaluationPreview(scope) {
  if (busy() || jobsUnavailable || !scope.count || state.role !== "reviewer") return;
  const body = h(`<div class="stack"><p><b>Диалогов: ${scope.count}</b> · ${esc(scope.scope)}</p>
    <label class="field"><span>Сохранённый профиль</span><select data-profile>${profileOptions(profiles, state.evaluationChecklistId)}</select></label>
    <label class="field"><span>Режим оценки</span><select data-mode><option value="reuse">Переиспользовать подходящие сохранённые результаты</option><option value="new">Новый анализ всех критериев</option></select></label>
    <p class="small muted" data-mode-description>Сервер переиспользует подходящие ответы для неизменённых критериев и пересчитает итог. Новые или изменённые критерии потребуют проверки моделью.</p>
    <label class="field"><span>Число проверок</span><select data-votes><option value="3" selected>Три проверки</option><option value="1">Одна проверка</option></select></label>
    <p class="small muted">Несколько проверок помогают увидеть расхождения моделей и могут занять больше времени. Время и стоимость зависят от моделей, числа проверок и переиспользованных результатов.</p></div>`);
  body.querySelector("[data-mode]").addEventListener("change", (event) => { body.querySelector("[data-mode-description]").textContent = event.target.value === "new" ? "Будет выполнен новый анализ всех критериев. Сохранённые ответы не переиспользуются." : "Сервер переиспользует подходящие ответы для неизменённых критериев и пересчитает итог. Новые или изменённые критерии потребуют проверки моделью."; });
  modal({ title: "Запуск оценки", body, actions: [{ label: "Отмена" }, { label: `Начать оценку (${scope.count})`, kind: "primary", onClick: async () => {
    if (busy() || state.role !== "reviewer") throw new Error("Дождитесь завершения текущей оценки");
    const checklist = requireProfile(body.querySelector("[data-profile]").value);
    rememberProfile(checklist.id);
    startingJob = true; listObserver?.busy();
    try {
      const running = await refreshJobs();
      if (running.length) throw new Error("Оценка уже выполняется. Дождитесь завершения текущей задачи.");
      const request = { checklist_id: checklist.id, votes: Number(body.querySelector("[data-votes]").value), reuse: body.querySelector("[data-mode]").value === "reuse" };
      if (scope.conversation_ids) request.conversation_ids = scope.conversation_ids;
      else request.group_id = scope.group_id;
      const result = await api.post("/api/evaluate", request);
      notifyJobStarted(); followJob(result.job_id);
    } finally { startingJob = false; listObserver?.busy(); }
  } }] });
}

function followJob(jobId, initial = null) {
  let tracked = trackedJobs.get(jobId);
  if (tracked?.task) return tracked.task;
  if (!tracked) { tracked = { id: jobId, last: initial, task: null, error: false }; trackedJobs.set(jobId, tracked); }
  tracked.error = false;
  listObserver?.busy(); listObserver?.tick();
  tracked.task = (async () => {
    try {
      for (;;) {
        const job = await api.get(`/api/jobs/${encodeURIComponent(jobId)}`);
        tracked.last = job; listObserver?.tick();
        if (job.status !== "running") {
          trackedJobs.delete(jobId);
          await notifyJobStarted();
          const errors = (job.results || []).filter((row) => row.status !== "ok");
          const incomplete = job.incomplete_count ?? (typeof job.incomplete === "number" ? job.incomplete : (job.results || []).filter((row) => checksSummary(row, job.votes).incomplete).length);
          if (job.status === "failed" || errors.length) toast("Оценка завершилась с ошибками. Откройте карточки диалогов, чтобы проверить результат.", "error");
          else if (incomplete) toast(`Обработано: ${job.done}. Неполные проверки: ${incomplete}; откройте карточки для проверки.`, "error");
          else toast(`Оценено диалогов: ${job.done}`, "ok");
          listObserver?.busy(); await listObserver?.done();
          return job;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } catch {
      tracked.error = true;
      errorToast(new Error("Не удалось обновить ход оценки. Повторите обновление; новая оценка не запущена."));
    } finally { tracked.task = null; listObserver?.busy(); listObserver?.tick(); }
  })();
  return tracked.task;
}

function requireProfile(value) {
  const checklist = selectedProfile(profiles, value);
  if (!checklist) throw new Error("Выберите сохранённый профиль правил");
  return checklist;
}

function rememberProfile(id) {
  state.evaluationChecklistId = id;
  state.dialogFilters.checklist_id = String(id);
  if (location.hash.startsWith("#/dialogs")) saveFilterUrl(state.dialogFilters, true);
  const selector = document.querySelector(".service-tools [data-profile]");
  if (selector) selector.value = String(id);
}

async function downloadResults(group, target, checklistId, source = "ai") {
  if (!group) throw new Error("Выберите группу для экспорта");
  const data = await api.get("/api/export" + qs({ group_id: group, target, checklist_id: checklistId, source }));
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = source === "final" ? "diagnostic_final_results.json" : "results.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function fetchModal(onDone) {
  const body = h(`<div class="stack">
    <p class="muted">Скачать набор диалогов по group_id из API организаторов. Для разработки — DEV, для контроля — значение, которое сообщат организаторы.</p>
    <label class="field"><span>group_id</span><input type="text" value="DEV" data-g></label></div>`);
  modal({
    title: "Загрузить набор из API", body,
    actions: [{ label: "Отмена" }, {
      label: "Загрузить", kind: "primary", onClick: async () => {
        const g = body.querySelector("[data-g]").value.trim();
        if (!g) throw new Error("Укажите группу набора");
        let res;
        try { res = await api.post("/api/datasets/fetch", { group_id: g }); }
        catch { throw new Error("Не удалось загрузить набор из API. Проверьте группу и подключение в настройках."); }
        toast(`Загружено диалогов: ${res.count} (${res.group_id})`, "ok");
        Object.assign(state.dialogFilters, { ...DIALOG_FILTER_DEFAULTS, group_id: res.group_id });
        onDone();
      },
    }],
  });
}

function uploadModal(onDone, alive) {
  const body = h(`<div class="stack">
    <div class="tabs"><button class="active" data-tab="text">Текст</button><button data-tab="json">JSON</button></div>
    <div data-pane="text" class="stack">
      <div class="row">
        <label class="field" style="flex:1"><span>ID диалога</span><input type="text" data-id placeholder="manual-001"></label>
        <label class="field"><span>Канал</span><select data-ch><option value="agent chat">чат</option><option value="voice call">звонок</option></select></label>
        <label class="field"><span>Язык</span><select data-lang><option>RU</option><option>KZ</option><option>EN</option></select></label>
      </div>
      <label class="field"><span>Реплики: по одной в строке — «[мм:сс] Клиент: …», «Оператор: …», «Бот: …», «Система: …»</span>
        <textarea rows="12" data-text placeholder="[00:00] Клиент: Здравствуйте, где посмотреть мой тариф?
[00:40] Оператор: Здравствуйте! Меня зовут Алина. Тариф указан в профиле, раздел «Тариф».
[01:05] Клиент: Нашла, спасибо.
[01:20] Оператор: Рада помочь! Если появятся вопросы — пишите. Хорошего дня!"></textarea></label>
    </div>
    <div data-pane="json" class="stack hidden">
      <label class="field"><span>Группа</span><input type="text" data-group value="UPLOAD"></label>
      <label class="field"><span>JSON: ответ API организаторов, массив записей или одна запись с transcript</span>
        <textarea rows="12" data-json placeholder='{"conversation_id": "x-1", "channel": "agent chat", "language": "RU", "transcript": [{"timestamp": "00:01", "speaker": "client", "text": "..."}]}'></textarea></label>
      <label class="field"><span>или файл .json</span><input type="file" accept=".json,application/json" data-file></label>
    </div>
    <label class="check"><input type="checkbox" data-now checked> После загрузки выбрать параметры оценки</label>
  </div>`);
  let tab = "text";
  body.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    tab = b.dataset.tab;
    body.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
    body.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab));
  }));
  body.querySelector("[data-file]").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    try { if (file) body.querySelector("[data-json]").value = await file.text(); }
    catch { errorToast(new Error("Не удалось прочитать JSON-файл")); }
  });
  modal({
    title: "Добавить диалог", body, wide: true,
    actions: [{ label: "Отмена" }, {
      label: "Загрузить", kind: "primary", onClick: async ({ close }) => {
        if (busy()) throw new Error("Дождитесь завершения текущей оценки");
        let request;
        if (tab === "text") {
          const text = body.querySelector("[data-text]").value.trim();
          if (!text) throw new Error("Добавьте текст диалога");
          request = {
            text,
            conversation_id: body.querySelector("[data-id]").value.trim() || undefined,
            channel: body.querySelector("[data-ch]").value,
            language: body.querySelector("[data-lang]").value,
            group_id: "MANUAL",
          };
        } else {
          let payload;
          try { payload = JSON.parse(body.querySelector("[data-json]").value); }
          catch { throw new Error("Невалидный JSON"); }
          const records = payload?.data?.records ?? payload?.records ?? payload;
          if (!records || typeof records !== "object" || (Array.isArray(records) && (!records.length || records.some((record) => !record || typeof record !== "object" || Array.isArray(record))))) {
            throw new Error("JSON должен содержать запись диалога или непустой массив записей");
          }
          request = { group_id: body.querySelector("[data-group]").value.trim() || "UPLOAD", payload };
        }
        startingJob = true;
        listObserver?.busy();
        try {
          const res = await api.post("/api/conversations/upload", request);
          if (!res.count || !res.ids?.length) throw new Error("В файле нет диалогов для загрузки");
          toast(`Добавлено диалогов: ${res.count}`, "ok");
          Object.assign(state.dialogFilters, { ...DIALOG_FILTER_DEFAULTS, group_id: res.group_id });
          const offerEvaluation = body.querySelector("[data-now]").checked;
          startingJob = false;
          close();
          await onDone();
          if (offerEvaluation && alive()) evaluationPreview({ count: res.count, scope: `Только что загруженные диалоги · ${res.group_id}`, conversation_ids: res.ids });
          return true;
        } finally {
          startingJob = false;
          listObserver?.busy();
        }
      },
    }],
  });
}

function saveModal(groups, selectedGroup = "") {
  if (!groups.length) { errorToast(new Error("Сначала загрузите диалоги")); return; }
  const body = h(`<div class="stack">
    <p class="muted">Для сдачи выбираются исходные оценки ИИ по указанному профилю. Скачивание и сохранение проходят одинаковую проверку происхождения, состава набора и формата 1.0.</p>
    <label class="field"><span>Сохранённый профиль</span><select data-profile>${profileOptions(profiles, state.evaluationChecklistId)}</select></label>
    <p class="small muted">Для исторических результатов DEV можно явно выбрать v1. Выбор версии не пересчитывает оценки и не переименовывает их.</p>
    <div class="row">
      <label class="field"><span>Группа</span><select data-g>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select></label>
      <label class="field"><span>Куда</span><select data-t><option value="dev">results/dev/results.json</option><option value="prod">results/prod/results.json</option></select></label>
    </div>
    <div data-out></div></div>`);
  if (selectedGroup) body.querySelector("[data-g]").value = selectedGroup;
  const exportRequest = (b) => {
    const checklist = requireProfile(b.querySelector("[data-profile]").value);
    rememberProfile(checklist.id);
    return { group_id: b.querySelector("[data-g]").value, target: b.querySelector("[data-t]").value, checklist_id: checklist.id };
  };
  const showError = (b, error) => {
    b.querySelector("[data-out]").innerHTML = `<div class="errors" role="alert"><b>Выгрузка не выполнена.</b><p>${esc(error.message).replace(/\n/g, "<br>")}</p></div>`;
  };
  modal({
    title: "Результаты в формате JSON для сдачи", body,
    actions: [
      { label: "Скачать для сдачи", onClick: async ({ body: b }) => {
        try {
          const request = exportRequest(b);
          await downloadResults(request.group_id, request.target, request.checklist_id);
          b.querySelector("[data-out]").innerHTML = `<div class="okbox">Проверенный файл подготовлен к скачиванию.</div>`;
        } catch (error) { showError(b, error); }
        return true;
      } },
      { label: "Диагностика после апелляций", onClick: async ({ body: b }) => {
        try {
          const request = exportRequest(b);
          await downloadResults(request.group_id, undefined, request.checklist_id, "final");
          b.querySelector("[data-out]").innerHTML = `<div class="notice">Диагностический файл после апелляций подготовлен к скачиванию. Он не предназначен для официальной сдачи.</div>`;
        } catch (error) { showError(b, error); }
        return true;
      } },
      {
        label: "Сохранить в проект", kind: "primary", onClick: async ({ body: b }) => {
          let res;
          try { res = await api.post("/api/results/save", exportRequest(b)); }
          catch (error) { showError(b, error); return true; }
          const out = b.querySelector("[data-out]");
          const errors = Array.isArray(res.errors) ? res.errors : [];
          const warnings = Array.isArray(res.warnings) ? res.warnings : [];
          out.innerHTML = res.saved === true
            ? `<div class="okbox">Сохранено: <b>${esc(res.path)}</b> — записей ${res.count}, оценено ${res.ok}${res.not_evaluated ? `, не оценено ${res.not_evaluated}` : ""}. Формат проверен.</div>`
            : `<div class="errors"><b>${res.saved === false ? "Файл НЕ сохранён. Прежний results.json не изменён." : "Сохранение файла не подтверждено сервером."}</b><br>${errors.length ? errors.map(esc).join("<br>") : "Сервер не сообщил причину. Повторите сохранение после проверки результатов."}</div>`;
          if (warnings.length) out.innerHTML += `<div class="muted small" style="margin-top:6px">${warnings.map(esc).join("<br>")}</div>`;
          return true;
        },
      },
    ],
  });
}

export { renderDialogs as render };
