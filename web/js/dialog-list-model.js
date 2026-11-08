// Чистые преобразования полного списка: URL, сортировка и база расчёта.
export const DIALOG_FILTER_DEFAULTS = Object.freeze({
  group_id: "", language: "", channel: "", q: "", critical: false, needs_review: false,
  open_appeals: false, criterion_id: "", criterion_violation: false, sort: "date_desc", checklist_id: "",
});
const SORTS = new Set(["score_asc", "score_desc", "date_asc", "date_desc"]);
const BOOLS = new Set(["critical", "needs_review", "open_appeals", "criterion_violation"]);

export function readDialogFilters(query, fallback = {}) {
  const explicit = query && query.toString();
  const filters = { ...DIALOG_FILTER_DEFAULTS, ...(explicit ? {} : fallback) };
  if (explicit) {
    for (const key of Object.keys(DIALOG_FILTER_DEFAULTS)) {
      if (query.has(key)) filters[key] = BOOLS.has(key) ? ["1", "true"].includes(query.get(key)) : query.get(key);
    }
  }
  if (!SORTS.has(filters.sort)) filters.sort = DIALOG_FILTER_DEFAULTS.sort;
  if (!filters.criterion_id) filters.criterion_violation = false;
  return filters;
}

export function dialogListHash(filters) {
  const query = new URLSearchParams();
  for (const key of Object.keys(DIALOG_FILTER_DEFAULTS)) {
    const value = filters[key];
    if (value !== "" && value !== false && value != null) query.set(key, value === true ? "1" : String(value));
  }
  if (!query.has("sort")) query.set("sort", DIALOG_FILTER_DEFAULTS.sort);
  return "#/dialogs?" + query.toString();
}

export function dialogScore(row) {
  const ev = row.evaluation;
  return ev?.status === "ok" && typeof ev.final_score === "number" && Number.isFinite(ev.final_score) ? ev.final_score : null;
}

export function sortDialogRows(rows, sort = "date_desc") {
  const byScore = sort.startsWith("score_");
  const direction = sort.endsWith("asc") ? 1 : -1;
  const value = (row) => {
    if (byScore) return dialogScore(row);
    const raw = row.evaluation?.evaluated_at;
    const time = raw ? Date.parse(raw) : NaN;
    return Number.isFinite(time) ? time : null;
  };
  return [...rows].sort((a, b) => {
    const left = value(a), right = value(b);
    if (left === null && right !== null) return 1;
    if (right === null && left !== null) return -1;
    if (left !== right) return (left - right) * direction;
    return String(a.conversation_id).localeCompare(String(b.conversation_id), "ru", { numeric: true });
  });
}

export function visibleDialogRows(rows, filters) {
  return sortDialogRows(filters.open_appeals ? rows.filter((row) => row.open_appeals > 0) : rows, filters.sort);
}

export function summarizeDialogRows(rows) {
  const scored = rows.map(dialogScore).filter((value) => value !== null);
  return {
    loaded: rows.length,
    evaluated: rows.filter((row) => row.evaluation?.status === "ok").length,
    scored: scored.length,
    failed: rows.filter((row) => row.evaluation && row.evaluation.status !== "ok").length,
    unevaluated: rows.filter((row) => !row.evaluation).length,
    average: scored.length ? scored.reduce((sum, value) => sum + value, 0) / scored.length : null,
  };
}

export function dialogScopeCount(groups, groupId) {
  return groups.filter((group) => !groupId || group.group_id === groupId)
    .reduce((sum, group) => sum + (Number(group.count) || 0), 0);
}
