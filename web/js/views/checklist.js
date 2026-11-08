// Экран «Чек-лист»: версии, редактор критериев, сохранение новой версии, применение к диалогам и разница итогов.
import { api, qs } from "../api.js";
import { errorToast, esc, fmtScore, icon, jobBox, modal, notifyJobStarted, pollJob, refreshJobs, state, toast } from "../ui.js";

let draft = null;       // редактируемая копия
let baseId = null;      // id версии, от которой редактируем
let lastApply = null;   // {from, to, group}
let renderSeq = 0;
let applyRun = null;
let paintApply = null;
let savedDraft = null;
let editing = false;
let noteDraft = "";
let writing = false;
const expandedCriteria = new WeakSet();
const dirty = () => draft && savedDraft && JSON.stringify(draft) !== JSON.stringify(savedDraft);
window.addEventListener("beforeunload", (event) => {
  if (!dirty()) return;
  event.preventDefault(); event.returnValue = "";
});
const scrollTo = (element) => {
  if (!element) return;
  element.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  element.focus({ preventScroll: true });
};

const TYPE_LABEL = { binary: "Да / Нет", scale: "Шкала", flag: "Грубая ошибка (флаг)" };

function clean(cl) {
  return JSON.parse(JSON.stringify(cl, (key, value) => key.startsWith("_") ? undefined : value));
}

export async function renderChecklist(root, params = [], query = null, alive = () => true) {
  const seq = ++renderSeq;
  const current = () => alive() && seq === renderSeq;
  const [versions, active, groups] = await Promise.all([api.get("/api/checklists"), api.get("/api/checklists/active"), api.get("/api/groups")]);
  if (!current()) return;
  if (!draft || baseId === null || !versions.some((v) => v.id === baseId)) {
    draft = clean(active); savedDraft = clean(active); baseId = active._id;
  }
  const base = versions.find((v) => v.id === baseId) || versions[0];

  const reviewer = state.role === "reviewer";
  const edit = reviewer && editing;
  const previous = versions.find((v) => v.version < base.version);
  const target = { from: lastApply?.to === baseId ? lastApply.from : previous?.id, to: baseId, version: base.version };
  const versionInfo = `Открыта версия правил v${base.version}. Сейчас активна v${active.version}.`;
  const rerender = () => renderChecklist(root, params, query, alive);
  const bindVersions = () => {
    let switching = false;
    root.querySelectorAll("[data-open], [data-activate]").forEach((button) => button.addEventListener("click", async () => {
      if (switching || writing || !current()) return;
      if (dirty() && !confirm("Есть несохранённые изменения правил. Отбросить их и открыть другую версию?")) return;
      switching = true;
      root.querySelectorAll("[data-open], [data-activate]").forEach((b) => { b.disabled = true; });
      try {
        const cl = button.dataset.activate
          ? await api.post(`/api/checklists/${button.dataset.activate}/activate`)
          : await api.get(`/api/checklists/${button.dataset.open}`);
        if (!current()) return;
        draft = clean(cl); savedDraft = clean(cl); baseId = cl._id; noteDraft = ""; lastApply = null;
        if (button.dataset.activate) toast("Версия активирована. Сохранённые оценки не изменились.", "ok");
        await rerender();
      } catch (error) { if (alive()) errorToast(error); }
      finally { if (current()) root.querySelectorAll("[data-open], [data-activate]").forEach((b) => { b.disabled = false; }); switching = false; }
    }));
  };
  if (!edit) {
    renderReader(root, savedDraft, base, active, versions, reviewer);
    root.querySelector('[data-edit-rules]')?.addEventListener("click", () => { editing = true; rerender(); });
    bindVersions();
    if (reviewer) renderApply(root, groups, current, target, versions, savedDraft);
    return;
  }

  root.innerHTML = `
    <div class="page-head"><h1>Редактор правил</h1><span class="sub">${versionInfo} Изменения сохраняются как новая версия.</span>
      <button class="btn" data-read-rules>Читать правила</button></div>
    <div class="cl-layout">
      <aside class="card versions">
        <div class="card-pad"><h2>Версии правил</h2></div>
        ${versions.map((v) => `<div class="version ${v.active ? "active" : ""}">
          <div class="row"><b>v${v.version}</b>${v.active ? `<span class="pill yes">активная</span>` : ""}<span class="spacer"></span>
            <button class="btn small" data-open="${v.id}" aria-label="Открыть версию правил ${v.version}">Открыть</button>${v.active ? "" : `<button class="btn small" data-activate="${v.id}" aria-label="Сделать версию правил ${v.version} активной">Сделать активной</button>`}</div>
          <div class="muted small">${esc(v.note || "")}</div><div class="muted small">${esc((v.created_at || "").replace("T", " ").slice(0, 16))}</div></div>`).join("")}
      </aside>
      <section class="stack">
        <div class="sticky-bar"><div class="card card-pad stack">
          <div class="row">
            <b>Сумма весов: <span data-sum>—</span></b><span class="muted small">(итог всё равно нормируется на 100 с учётом N/A)</span>
            <span class="spacer"></span>
            <label class="field"><span>Что поменяли</span><input type="text" data-note value="${esc(noteDraft)}" placeholder="Краткая заметка для истории версий"></label>
            <button class="btn primary" data-act="save">Сохранить как новую версию</button>
          </div>
          <div data-errors></div>
        </div></div>
        <div id="applyArea"></div>
        <div class="card card-pad stack">
          <div class="row">
            <label class="field" style="flex:1"><span>Название правил</span><input type="text" data-top="name"></label>
            <label class="field"><span>Порог первой реакции, сек</span><input type="number" min="1" step="1" data-top="first_response_threshold_sec" style="width:120px"></label>
          </div>
          <label class="field"><span>Общие правила (передаются модели)</span><textarea rows="2" data-top="notes"></textarea></label>
          <div><span class="muted small">Секции</span><div data-sections></div><button class="btn small" data-act="add-section">${icon('plus')} Добавить секцию</button></div>
        </div>
        <div class="card" data-criteria></div>
        <button class="btn" data-act="add-criterion">${icon('plus')} Добавить критерий</button>
      </section>
    </div>`;

  root.querySelector('[data-read-rules]').addEventListener("click", () => { editing = false; rerender(); });
  root.querySelector('[data-note]').addEventListener("input", (event) => { noteDraft = event.target.value; });
  bindVersions();
  const errorsBox = root.querySelector("[data-errors]");
  const sumEl = root.querySelector("[data-sum]");
  const saveButton = root.querySelector('[data-act="save"]');
  let vTimer = null;
  let validationSeq = 0;
  let saving = false;
  const validate = () => {
    clearTimeout(vTimer);
    const revision = ++validationSeq;
    saveButton.disabled = true;
    errorsBox.innerHTML = `<div class="muted small">Проверяем конфигурацию…</div>`;
    vTimer = setTimeout(async () => {
      if (!current()) return;
      try {
        const res = await api.post("/api/checklists/validate", { data: draft });
        if (!current() || revision !== validationSeq) return;
        sumEl.textContent = fmtScore(res.weights_sum);
        saveButton.disabled = saving || res.errors.length > 0;
        errorsBox.innerHTML = res.errors.length ? `<div class="errors">${res.errors.map(esc).join("<br>")}</div>` : `<div class="okbox">Конфигурация корректна</div>`;
      } catch (e) {
        if (!current() || revision !== validationSeq) return;
        errorsBox.innerHTML = `<div class="errors">${esc(e.message)}</div>`;
        errorToast(e);
      }
    }, 250);
  };

  // верхние поля
  root.querySelectorAll("[data-top]").forEach((el) => {
    const k = el.dataset.top;
    el.value = draft[k] ?? "";
    el.addEventListener("input", () => { draft[k] = el.type === "number" ? Number(el.value) : el.value; validate(); });
  });

  const renderSections = () => {
    const box = root.querySelector("[data-sections]");
    box.innerHTML = (draft.sections || []).map((s, i) => `<div class="row section-editor-row">
      <label class="field"><span>ID секции ${i + 1}</span><input type="text" value="${esc(s.id)}" data-sec-id="${i}"></label>
      <label class="field"><span>Название секции ${i + 1}</span><input type="text" value="${esc(s.name)}" data-sec-name="${i}"></label>
      <div class="row rules-mode-actions">
        <button class="btn small" data-sec-up="${i}" aria-label="Поднять секцию ${esc(s.name || s.id)}" ${i === 0 ? "disabled" : ""}>Поднять</button>
        <button class="btn small" data-sec-down="${i}" aria-label="Опустить секцию ${esc(s.name || s.id)}" ${i === draft.sections.length - 1 ? "disabled" : ""}>Опустить</button>
        <button class="btn small danger" data-sec-del="${i}" aria-label="Удалить секцию ${esc(s.name || s.id)}">Удалить</button>
      </div></div>`).join("");
    const refreshSectionNames = (el, section) => {
      for (const [action, label] of [["up", "Поднять"], ["down", "Опустить"], ["del", "Удалить"]]) {
        el.closest('.section-editor-row').querySelector(`[data-sec-${action}]`).setAttribute('aria-label', `${label} секцию ${section.name || section.id}`);
      }
    };
    box.querySelectorAll("[data-sec-id]").forEach((el) => el.addEventListener("input", () => {
      const section = draft.sections[+el.dataset.secId];
      const oldId = section.id;
      section.id = el.value.trim();
      draft.criteria.forEach((c) => { if (c.section === oldId) c.section = section.id; });
      refreshSectionNames(el, section);
      renderCriteria(); validate();
    }));
    box.querySelectorAll("[data-sec-name]").forEach((el) => el.addEventListener("input", () => {
      const section = draft.sections[+el.dataset.secName];
      section.name = el.value;
      refreshSectionNames(el, section);
      renderCriteria(); validate();
    }));
    for (const [action, delta] of [["up", -1], ["down", 1]]) {
      box.querySelectorAll(`[data-sec-${action}]`).forEach((el) => el.addEventListener("click", () => {
        const index = Number(el.getAttribute(`data-sec-${action}`));
        const next = index + delta;
        if (next < 0 || next >= draft.sections.length) return;
        [draft.sections[index], draft.sections[next]] = [draft.sections[next], draft.sections[index]];
        renderSections(); renderCriteria(); validate();
        root.querySelector(`[data-sec-name="${next}"]`)?.focus();
      }));
    }
    box.querySelectorAll("[data-sec-del]").forEach((el) => el.addEventListener("click", () => {
      const [removed] = draft.sections.splice(+el.dataset.secDel, 1);
      draft.criteria.forEach((c) => { if (c.section === removed.id) c.section = null; });
      renderSections(); renderCriteria(); validate(); root.querySelector('[data-act="add-section"]').focus();
    }));
  };

  const renderCriteria = () => {
    const box = root.querySelector("[data-criteria]");
    const secOpts = (sel) => `<option value="">—</option>` + (draft.sections || []).map((s) => `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(s.name || s.id)}</option>`).join("");
    box.innerHTML = draft.criteria.map((c, i) => `<details class="crit-edit criterion-editor" data-i="${i}" ${expandedCriteria.has(c) ? "open" : ""}>
      <summary><span data-criterion-heading>${i + 1}. ${esc(c.name || "Без названия")}</span><span class="muted small">${esc(TYPE_LABEL[c.type])}${c.type === "flag" ? "" : ` · вес ${esc(c.weight)}`}</span></summary>
      <div class="criterion-fields stack">
        <div class="row rules-mode-actions">
          <button class="btn small" data-up aria-label="Поднять критерий ${esc(c.name)}" ${i === 0 ? "disabled" : ""}>Поднять</button>
          <button class="btn small" data-down aria-label="Опустить критерий ${esc(c.name)}" ${i === draft.criteria.length - 1 ? "disabled" : ""}>Опустить</button>
          <button class="btn small danger" data-del aria-label="Удалить критерий ${esc(c.name)}">Удалить</button>
        </div>
        <div class="grid2">
          <label class="field"><span>Название</span><input type="text" data-k="name" value="${esc(c.name)}"></label>
          <label class="field"><span>Тип ответа</span><select data-k="type">${Object.entries(TYPE_LABEL).map(([v, l]) => `<option value="${v}" ${c.type === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <label class="field"><span>Секция</span><select data-k="section">${secOpts(c.section)}</select></label>
          <label class="field ${c.type === "flag" ? "hidden" : ""}"><span>Вес</span><input type="number" min="0" step="1" data-k="weight" value="${c.type === "flag" ? 0 : esc(c.weight)}" ${c.type === "flag" ? "disabled" : ""}></label>
        </div>
        <label class="check"><input type="checkbox" data-k="na_allowed" ${c.na_allowed ? "checked" : ""} ${c.type === "flag" ? "disabled" : ""}> Разрешить «Не применяется (N/A)»</label>
        <label class="field"><span>Условие неприменимости (N/A)</span><input type="text" data-k="na_condition" value="${esc(c.na_condition || "")}" ${c.na_allowed ? "" : "disabled"}></label>
        <label class="field"><span>Формулировка — что требуется для выполнения</span><textarea rows="3" data-k="description">${esc(c.description || "")}</textarea></label>
        ${c.type === "scale" ? `<div class="stack"><h3>Ступени шкалы</h3><p class="muted small">Верхняя ступень соответствует полному весу.</p>
          ${(c.scale || []).map((s, j) => `<div class="scale-row">
            <label class="field"><span>Баллы, ступень ${j + 1}</span><input type="number" min="0" data-sv="${j}" value="${esc(s.value)}"></label>
            <label class="field"><span>Описание ступени ${j + 1}</span><input type="text" data-sl="${j}" value="${esc(s.label || "")}"></label>
            <button class="btn small danger" data-sdel="${j}" aria-label="Удалить ступень ${j + 1} критерия ${esc(c.name)}">Удалить</button></div>`).join("")}
          <button class="btn small" data-sadd>Добавить ступень</button></div>` : ""}
        <details class="advanced-settings"><summary>Расширенные настройки</summary><div class="stack">
          <label class="field"><span>ID критерия (латиница)</span><input type="text" data-k="id" value="${esc(c.id)}"></label>
          <label class="field"><span>Подсказки для ИИ — уточнения и пограничные случаи</span><textarea rows="4" data-k="guidance">${esc(c.guidance || "")}</textarea></label>
        </div></details>
      </div>
    </details>`).join("");

    box.querySelectorAll(".crit-edit").forEach((card) => {
      const i = +card.dataset.i;
      const c = draft.criteria[i];
      card.addEventListener("toggle", () => { if (card.open) expandedCriteria.add(c); else expandedCriteria.delete(c); });
      card.querySelectorAll("[data-k]").forEach((el) => {
        const k = el.dataset.k;
        const ev = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input";
        el.addEventListener(ev, () => {
          if (el.type === "checkbox") c[k] = el.checked;
          else if (el.type === "number") c[k] = Number(el.value);
          else c[k] = el.value;
          if (k === "type") {
            if (c.type === "flag") { c.weight = 0; c.na_allowed = false; }
            if (c.type === "scale" && !(c.scale && c.scale.length)) c.scale = [{ value: 10, label: "Полностью выполнено" }, { value: 5, label: "Частично" }, { value: 0, label: "Не выполнено" }];
            renderCriteria();
          }
          if (k === "na_allowed") renderCriteria();
          if (k === "name") {
            card.querySelector('[data-criterion-heading]').textContent = `${i + 1}. ${c.name || "Без названия"}`;
            for (const [act, label] of [["up", "Поднять"], ["down", "Опустить"], ["del", "Удалить"]]) card.querySelector(`[data-${act}]`).setAttribute("aria-label", `${label} критерий ${c.name}`);
          }
          if (k === "type" || k === "na_allowed") root.querySelector(`.crit-edit[data-i="${i}"] [data-k="${k}"]`)?.focus();
          validate();
        });
      });
      card.querySelectorAll("[data-sv]").forEach((el) => el.addEventListener("input", () => { c.scale[+el.dataset.sv].value = Number(el.value); validate(); }));
      card.querySelectorAll("[data-sl]").forEach((el) => el.addEventListener("input", () => { c.scale[+el.dataset.sl].label = el.value; validate(); }));
      card.querySelectorAll("[data-sdel]").forEach((el) => el.addEventListener("click", () => { c.scale.splice(+el.dataset.sdel, 1); renderCriteria(); validate(); root.querySelector(`.crit-edit[data-i="${i}"] [data-sadd]`)?.focus(); }));
      const sadd = card.querySelector("[data-sadd]");
      if (sadd) sadd.addEventListener("click", () => { c.scale.push({ value: 0, label: "" }); renderCriteria(); validate(); root.querySelector(`.crit-edit[data-i="${i}"] [data-sv="${c.scale.length - 1}"]`)?.focus(); });
      card.querySelector("[data-del]").addEventListener("click", () => { if (confirm(`Удалить критерий «${c.name}»?`)) { draft.criteria.splice(i, 1); renderCriteria(); validate(); (root.querySelector(`.crit-edit[data-i="${Math.min(i, draft.criteria.length - 1)}"] summary`) || root.querySelector('[data-act="add-criterion"]')).focus(); } });
      const up = card.querySelector("[data-up]");
      const down = card.querySelector("[data-down]");
      up.addEventListener("click", () => { [draft.criteria[i - 1], draft.criteria[i]] = [draft.criteria[i], draft.criteria[i - 1]]; renderCriteria(); validate(); root.querySelector(`.crit-edit[data-i="${i - 1}"] summary`)?.focus(); });
      down.addEventListener("click", () => { [draft.criteria[i + 1], draft.criteria[i]] = [draft.criteria[i], draft.criteria[i + 1]]; renderCriteria(); validate(); root.querySelector(`.crit-edit[data-i="${i + 1}"] summary`)?.focus(); });
    });
  };

  root.querySelector('[data-act="add-section"]').addEventListener("click", () => {
    draft.sections = draft.sections || [];
    let number = draft.sections.length + 1;
    while (draft.sections.some((section) => section.id === `section_${number}`)) number++;
    draft.sections.push({ id: `section_${number}`, name: "Новая секция" });
    renderSections(); renderCriteria(); validate();
    root.querySelector(`[data-sec-name="${draft.sections.length - 1}"]`)?.focus();
  });
  root.querySelector('[data-act="add-criterion"]').addEventListener("click", () => {
    let n = draft.criteria.length + 1;
    while (draft.criteria.some((c) => c.id === `criterion_${n}`)) n++;
    draft.criteria.push({ id: `criterion_${n}`, name: "Новый критерий", section: (draft.sections && draft.sections[0] && draft.sections[0].id) || null, type: "binary", weight: 10, na_allowed: false, na_condition: "", description: "Да — ", guidance: "" });
    expandedCriteria.add(draft.criteria[draft.criteria.length - 1]);
    renderCriteria(); validate();
    scrollTo(root.querySelector(`.crit-edit[data-i="${draft.criteria.length - 1}"] summary`));
  });

  root.querySelector('[data-act="save"]').addEventListener("click", async (e) => {
    if (saving || writing || !current()) return;
    saving = true; writing = true;
    root.querySelectorAll('input, select, textarea, button').forEach((control) => { control.disabled = true; });
    try {
      const fromId = baseId;
      const saved = await api.post("/api/checklists", { data: clean(draft), note: root.querySelector("[data-note]").value.trim() || "Изменение через интерфейс", activate: true });
      draft = clean(saved); savedDraft = clean(saved); baseId = saved._id; noteDraft = "";
      lastApply = { from: fromId, to: saved._id, version: saved.version };
      state.lastChecklistId = saved._id;
      if (current()) { toast(`Сохранена версия v${saved.version}`, "ok"); await rerender(); }
    } catch (err) { if (current()) { errorToast(err); await rerender(); } }
    finally { saving = false; writing = false; }
  });

  renderApply(root, groups, current, target, versions, savedDraft);
  renderSections();
  renderCriteria();
  validate();
}

function versionList(versions, reviewer) {
  return `<details class="card versions"><summary>Версии правил</summary>${versions.map((v) => `<div class="version ${v.active ? "active" : ""}">
    <div class="row"><b>v${v.version}</b>${v.active ? `<span class="pill yes">активная</span>` : ""}
      <button class="btn small" data-open="${v.id}" aria-label="Открыть версию правил ${v.version}">Открыть</button>
      ${reviewer && !v.active ? `<button class="btn small" data-activate="${v.id}" aria-label="Сделать версию правил ${v.version} активной">Сделать активной</button>` : ""}</div>
    <p class="muted small">${esc(v.note || "Без заметки")} · ${esc((v.created_at || "").replace("T", " ").slice(0, 16))}</p></div>`).join("")}</details>`;
}

function renderReader(root, cl, base, active, versions, reviewer) {
  const sections = [...(cl.sections || [])];
  const known = new Set(sections.map((section) => section.id));
  if (cl.criteria.some((criterion) => !known.has(criterion.section))) sections.push({ id: null, name: "Другие критерии" });
  root.innerHTML = `<div class="page-head"><h1>Правила оценки</h1><span class="sub">${esc(cl.name)} · версия v${base.version}${base.id === active._id ? " (активная)" : `; сейчас активна v${active.version}`}</span>
    ${reviewer ? `<button class="btn primary" data-edit-rules>${dirty() ? "Вернуться к черновику" : "Редактировать правила"}</button>` : ""}</div>
    <div class="rules-reader stack">
      <p class="muted">Здесь показана сохранённая версия правил. Смена активной версии сама по себе не пересчитывает исторические оценки.</p>
      ${dirty() ? `<p class="notice">Есть несохранённый черновик редактора. Ниже показаны сохранённые правила v${base.version}.</p>` : ""}
      <nav class="rules-nav" aria-label="Разделы правил"><label class="field"><span>Перейти к разделу или критерию</span><select data-rule-nav>
        <option value="">Выберите раздел или критерий</option>
        ${sections.map((section, index) => `<optgroup label="${esc(section.name)}"><option value="rule-section-${index}">${esc(section.name)}</option>${cl.criteria.filter((criterion) => section.id === null ? !known.has(criterion.section) : criterion.section === section.id).map((criterion) => `<option value="rule-${cl.criteria.indexOf(criterion)}">${esc(criterion.name)}</option>`).join("")}</optgroup>`).join("")}
      </select></label></nav>
      ${sections.map((section, index) => `<section class="stack"><h2 id="rule-section-${index}" tabindex="-1">${esc(section.name)}</h2>
        ${cl.criteria.filter((criterion) => section.id === null ? !known.has(criterion.section) : criterion.section === section.id).map((criterion) => `<article class="card card-pad rule-reading-card stack" id="rule-${cl.criteria.indexOf(criterion)}" tabindex="-1">
          <h3>${esc(criterion.name)}</h3><div class="row"><span class="pill">${esc(TYPE_LABEL[criterion.type])}</span>${criterion.type !== "flag" ? `<span>Вес: ${fmtScore(criterion.weight)}</span>` : `<span>Отдельный признак грубой ошибки</span>`}</div>
          <p>${esc(criterion.description || "Формулировка не указана")}</p>
          ${criterion.type === "scale" ? `<ul>${(criterion.scale || []).map((step) => `<li><b>${esc(step.value)}</b> — ${esc(step.label)}</li>`).join("")}</ul>` : ""}
          <p class="muted">${criterion.na_allowed ? `<b>Не применяется (N/A):</b> ${esc(criterion.na_condition || "Условие не указано")}` : "Применяется ко всем оцениваемым диалогам."}</p>
          ${criterion.guidance ? `<details class="advanced-settings"><summary>Уточнения правила</summary><p>${esc(criterion.guidance)}</p></details>` : ""}
        </article>`).join("")}</section>`).join("")}
      <details class="card card-pad"><summary>Общие правила и технические сведения</summary><div class="stack"><p>${esc(cl.notes || "Дополнительные общие правила не указаны.")}</p><p>Порог первой реакции: ${esc(cl.first_response_threshold_sec ?? "не указан")} с.</p><p class="muted small">Техническое название: чек-лист, версия v${base.version}.</p></div></details>
      ${versionList(versions, reviewer)}
      ${reviewer ? `<div id="applyArea"></div>` : ""}
    </div>`;
  root.querySelector('[data-rule-nav]').addEventListener("change", (event) => {
    if (event.target.value) scrollTo(root.querySelector(`[id="${event.target.value}"]`));
  });
}

function renderApply(root, groups, current, target, versions, saved) {
  const area = root.querySelector("#applyArea");
  area.innerHTML = `<details class="card card-pad rules-apply" ${applyRun?.running ? "open" : ""}><summary>Применить сохранённую версию v${target.version} и сравнить результаты</summary><div class="stack">
    <p>Будет применена сохранённая версия <b>v${target.version}</b>. Несохранённые изменения редактора в запуск не входят.</p>
    <p class="muted small">При переиспользовании сервер проверяет совместимость прошлых оценок: изменение только веса пересчитывает итог без модели. Для нового анализа выберите запуск без переиспользования.</p>
    <div class="row">
      <label class="field"><span>Группа для применения</span><select data-ag><option value="">Все группы</option>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select></label>
      <label class="field"><span>Проверки</span><select data-av><option value="1">Одна проверка</option><option value="3">Три проверки</option></select></label>
      <label class="field"><span>Режим запуска</span><select data-reuse><option value="true">Пересчёт с переиспользованием</option><option value="false">Новый анализ</option></select></label>
      <button class="btn primary" data-apply>Применить v${target.version}</button>
    </div>
    <p class="muted small">Три проверки позволяют сравнить ответы и могут занять больше времени. Время и стоимость зависят от моделей и числа проверок.</p>
    <div data-ajob aria-live="polite"></div><div data-diff></div>
    <details class="advanced-settings"><summary>Сравнение определений правил</summary><div class="stack">
      <label class="field"><span>Сравнить v${target.version} с версией</span><select data-compare>${versions.filter((v) => v.id !== target.to).map((v) => `<option value="${v.id}" ${v.id === target.from ? "selected" : ""}>v${v.version}</option>`).join("")}</select></label>
      <button class="btn" data-compare-rules ${versions.length < 2 ? "disabled" : ""}>Сравнить определения</button><div data-rule-diff></div>
    </div></details>
  </div></details>`;
  paintApply = () => {
    if (!current()) return;
    area.querySelectorAll("[data-apply], [data-ag], [data-av], [data-reuse]").forEach((el) => { el.disabled = !!applyRun?.running || !groups.length; });
    if (applyRun?.to !== target.to) return;
    if (applyRun.job) area.querySelector("[data-ajob]").innerHTML = jobBox(applyRun.job, `Применение v${target.version}`);
    if (applyRun.diff) area.querySelector("[data-diff]").innerHTML = diffHtml(applyRun.diff);
  };
  if (applyRun?.to === target.to) area.querySelector("[data-ag]").value = applyRun.group;
  paintApply();
  let preparing = false;
  area.querySelector("[data-apply]").addEventListener("click", async () => {
    if (preparing || applyRun?.running || !current()) return;
    preparing = true;
    try {
      if ((await refreshJobs()).length) { toast("Уже выполняется оценка. Дождитесь завершения текущей задачи.", "error"); return; }
      if (!current() || state.role !== "reviewer") return;
      const group = area.querySelector("[data-ag]").value;
      const votes = Number(area.querySelector("[data-av]").value);
      const reuse = area.querySelector("[data-reuse]").value === "true";
      const count = group ? groups.find((g) => g.group_id === group)?.count : groups.reduce((sum, g) => sum + g.count, 0);
      modal({ title: `Применить правила v${target.version}`, body: `<p>Охват: <b>${group ? `вся группа ${esc(group)}` : "все группы"}</b>. Количество диалогов: ${count}.</p><p>Сохранённая версия правил: <b>v${target.version}</b>. Режим: <b>${reuse ? "пересчёт с переиспользованием по правилам сервера" : "новый анализ без переиспользования"}</b>. ${votes === 3 ? "Три проверки" : "Одна проверка"}.</p><p class="muted">Стоимость и время зависят от выбранных моделей. Фильтры списка не ограничивают этот запуск.</p>`, actions: [
        { label: "Отмена" }, { label: "Начать применение", kind: "primary", onClick: async () => {
          if (applyRun?.running || (await refreshJobs()).length) { toast("Уже выполняется оценка. Повторный запуск отменён.", "error"); return; }
          if (!current() || state.role !== "reviewer") return;
          const run = applyRun = { ...target, group, running: true, job: null, diff: null };
          area.querySelector("[data-diff]").innerHTML = ""; paintApply();
          try {
            const { job_id } = await api.post("/api/evaluate", { group_id: group, checklist_id: target.to, votes, reuse });
            notifyJobStarted();
            const finish = async () => {
              try {
                const job = await pollJob(job_id, (j) => { run.job = j; paintApply?.(); });
                if (job.status === "failed") throw new Error(job.error || "Не удалось применить правила");
                const errors = (job.results || []).filter((r) => r.status !== "ok");
                toast(errors.length ? `Применение завершено с ошибками: ${errors.length}.` : `Готово. Диалогов обработано: ${job.done}.`, errors.length ? "error" : "ok");
                run.diff = target.from ? await api.get("/api/diff" + qs({ group_id: group, from_checklist: target.from, to_checklist: target.to })) : [];
              } catch (err) { errorToast(err); }
              finally { run.running = false; paintApply?.(); notifyJobStarted(); }
            };
            finish();
          } catch (error) { run.running = false; paintApply?.(); throw error; }
        } },
      ] });
    } catch (error) { errorToast(error); }
    finally { preparing = false; }
  });
  area.querySelector('[data-compare-rules]').addEventListener("click", async (event) => {
    const id = area.querySelector('[data-compare]').value;
    if (!id) return;
    event.target.disabled = true;
    try {
      const other = await api.get(`/api/checklists/${encodeURIComponent(id)}`);
      if (current()) area.querySelector('[data-rule-diff]').innerHTML = rulesDiffHtml(other, saved);
    } catch (error) { errorToast(error); }
    finally { if (current()) event.target.disabled = false; }
  });
}

function rulesDiffHtml(before, after) {
  const labels = { name: "название", section: "секция", type: "тип", weight: "вес", description: "формулировка", scale: "шкала", na_allowed: "допустимость N/A", na_condition: "условие N/A", guidance: "подсказки ИИ" };
  const previous = new Map(before.criteria.map((criterion) => [criterion.id, criterion]));
  const rows = after.criteria.map((criterion) => {
    const old = previous.get(criterion.id); previous.delete(criterion.id);
    const fields = old ? Object.keys(labels).filter((key) => JSON.stringify(old[key]) !== JSON.stringify(criterion[key])).map((key) => labels[key]) : ["добавлен"];
    return fields.length ? `<li><b>${esc(criterion.name)}</b>: ${esc(fields.join(", "))}</li>` : "";
  });
  for (const criterion of previous.values()) rows.push(`<li><b>${esc(criterion.name)}</b>: удалён</li>`);
  if (JSON.stringify(before.criteria.map((criterion) => criterion.id)) !== JSON.stringify(after.criteria.map((criterion) => criterion.id))) rows.push("<li>Изменён состав или порядок критериев.</li>");
  for (const [key, label] of [["name", "название правил"], ["notes", "общие правила"], ["first_response_threshold_sec", "порог первой реакции"], ["sections", "секции"]]) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) rows.push(`<li>Изменено: ${label}</li>`);
  }
  return `<p class="muted small">Сравниваются определения сохранённых правил. Это не сравнение баллов диалогов.</p>${rows.some(Boolean) ? `<ul>${rows.join("")}</ul>` : "<p>Определения правил совпадают.</p>"}`;
}

function diffHtml(diff) {
  if (!diff.length) return `<p class="muted small">Нет оценок обеих версий для сравнения результатов.</p>`;
  const changed = diff.filter((d) => d.before !== d.after || d.changed.some((c) => c.before !== c.after));
  const affected = diff.filter((d) => d.changed.length);
  diff = [...changed, ...diff.filter((d) => !changed.includes(d))];
  return `<h3>Сравнение результатов оценивания</h3><p>Изменился итог или значение критерия: ${changed.length} из ${diff.length} диалогов. Отличия критериев (включая определение и вес): ${affected.length}.</p>
    <div class="table-scroll" tabindex="0" role="region" aria-label="Результаты применения правил"><table class="grid small"><thead><tr><th>Диалог</th><th>Итог до</th><th>Итог после</th><th>Отличия критериев</th></tr></thead><tbody>
    ${diff.map((d) => `<tr><td><a class="mono" href="#/dialog/${encodeURIComponent(d.conversation_id)}">${esc(d.conversation_id)}</a></td>
      <td>${fmtScore(d.before)}</td><td><b>${fmtScore(d.after)}</b></td>
      <td>${d.changed.map((c) => `${esc(c.criterion_id)}: ${esc(c.before)} → ${esc(c.after)}${c.before === c.after ? " (значение не изменилось; отличается определение или вес)" : ""}`).join("; ") || "Нет отличий"}</td></tr>`).join("")}</tbody></table></div>`;
}

export { renderChecklist as render };
