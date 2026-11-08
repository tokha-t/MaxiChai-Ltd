// Экран «Чек-лист»: версии, редактор критериев, сохранение новой версии, применение к диалогам и разница итогов.
import { api, qs } from "../api.js";
import { errorToast, esc, fmtScore, jobBox, pollJob, state, toast } from "../ui.js";

let draft = null;       // редактируемая копия
let baseId = null;      // id версии, от которой редактируем
let lastApply = null;   // {from, to, group}

const TYPE_LABEL = { binary: "Да / Нет", scale: "Шкала", flag: "Грубая ошибка (флаг)" };

function clean(cl) {
  const out = JSON.parse(JSON.stringify(cl));
  for (const k of Object.keys(out)) if (k.startsWith("_")) delete out[k];
  return out;
}

export async function renderChecklist(root, params, query, alive) {
  const [versions, active, groups] = await Promise.all([api.get("/api/checklists"), api.get("/api/checklists/active"), api.get("/api/groups")]);
  if (!alive()) return;
  if (!draft || baseId === null || !versions.some((v) => v.id === baseId)) { draft = clean(active); baseId = active._id; }
  const base = versions.find((v) => v.id === baseId) || versions[0];

  root.innerHTML = `
    <div class="page-head"><h1>Чек-лист</h1><span class="sub">Версия v${base ? base.version : "—"}${base && base.active ? " (активная)" : ""}. Изменения сохраняются как новая версия.</span></div>
    <div class="cl-layout">
      <aside class="card versions">
        <div class="card-pad"><b>Версии</b></div>
        ${versions.map((v) => `<div class="version ${v.active ? "active" : ""}">
          <div class="row"><b>v${v.version}</b>${v.active ? `<span class="pill yes">активная</span>` : ""}<span class="spacer"></span>
            <button class="btn small" data-open="${v.id}">Открыть</button>${v.active ? "" : `<button class="btn small" data-activate="${v.id}">Сделать активной</button>`}</div>
          <div class="muted small">${esc(v.note || "")}</div><div class="muted small">${esc((v.created_at || "").replace("T", " ").slice(0, 16))}</div></div>`).join("")}
      </aside>
      <section class="stack">
        <div class="sticky-bar"><div class="card card-pad stack">
          <div class="row">
            <b>Сумма весов: <span data-sum>—</span></b><span class="muted small">(итог всё равно нормируется на 100 с учётом N/A)</span>
            <span class="spacer"></span>
            <input type="text" data-note placeholder="Что поменяли (для истории версий)" style="width:280px">
            <button class="btn primary" data-act="save">Сохранить как новую версию</button>
          </div>
          <div data-errors></div>
        </div></div>
        <div id="applyArea"></div>
        <div class="card card-pad stack">
          <div class="row">
            <label class="field" style="flex:1"><span>Название чек-листа</span><input type="text" data-top="name"></label>
            <label class="field"><span>Порог первой реакции, сек</span><input type="number" min="0" data-top="first_response_threshold_sec" style="width:120px"></label>
          </div>
          <label class="field"><span>Общие правила (передаются модели)</span><textarea rows="2" data-top="notes"></textarea></label>
          <div><span class="muted small">Секции</span><div data-sections></div><button class="btn small" data-act="add-section">＋ секция</button></div>
        </div>
        <div class="card" data-criteria></div>
        <button class="btn" data-act="add-criterion">＋ Добавить критерий</button>
      </section>
    </div>`;

  const errorsBox = root.querySelector("[data-errors]");
  const sumEl = root.querySelector("[data-sum]");
  let vTimer = null;
  const validate = () => {
    clearTimeout(vTimer);
    vTimer = setTimeout(async () => {
      try {
        const res = await api.post("/api/checklists/validate", { data: draft });
        sumEl.textContent = fmtScore(res.weights_sum);
        errorsBox.innerHTML = res.errors.length ? `<div class="errors">${res.errors.map(esc).join("<br>")}</div>` : `<div class="okbox">Конфигурация корректна</div>`;
      } catch (e) { errorsBox.innerHTML = `<div class="errors">${esc(e.message)}</div>`; }
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
    box.innerHTML = (draft.sections || []).map((s, i) => `<div class="row" style="margin:6px 0">
      <input type="text" value="${esc(s.id)}" data-sec-id="${i}" style="width:140px" placeholder="id">
      <input type="text" value="${esc(s.name)}" data-sec-name="${i}" style="flex:1" placeholder="Название секции">
      <button class="btn small danger" data-sec-del="${i}">✕</button></div>`).join("");
    box.querySelectorAll("[data-sec-id]").forEach((el) => el.addEventListener("input", () => { draft.sections[+el.dataset.secId].id = el.value.trim(); validate(); }));
    box.querySelectorAll("[data-sec-name]").forEach((el) => el.addEventListener("input", () => { draft.sections[+el.dataset.secName].name = el.value; }));
    box.querySelectorAll("[data-sec-del]").forEach((el) => el.addEventListener("click", () => { draft.sections.splice(+el.dataset.secDel, 1); renderSections(); renderCriteria(); validate(); }));
  };

  const renderCriteria = () => {
    const box = root.querySelector("[data-criteria]");
    const secOpts = (sel) => `<option value="">—</option>` + (draft.sections || []).map((s) => `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(s.name || s.id)}</option>`).join("");
    box.innerHTML = draft.criteria.map((c, i) => `<div class="crit-edit" data-i="${i}">
      <div class="row" style="margin-bottom:8px"><b>${i + 1}. ${esc(c.name || "Без названия")}</b><span class="muted small mono">${esc(c.id)}</span><span class="spacer"></span>
        <button class="btn small" data-up ${i === 0 ? "disabled" : ""}>↑</button><button class="btn small" data-down ${i === draft.criteria.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn small danger" data-del>Удалить</button></div>
      <div class="grid2">
        <label class="field"><span>id (латиница)</span><input type="text" data-k="id" value="${esc(c.id)}"></label>
        <label class="field"><span>Название</span><input type="text" data-k="name" value="${esc(c.name)}"></label>
        <label class="field"><span>Тип ответа</span><select data-k="type">${Object.entries(TYPE_LABEL).map(([v, l]) => `<option value="${v}" ${c.type === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="field"><span>Вес</span><input type="number" min="0" step="1" data-k="weight" value="${c.type === "flag" ? 0 : esc(c.weight)}" ${c.type === "flag" ? "disabled" : ""}></label>
      </div>
      <div class="grid3">
        <label class="field"><span>Секция</span><select data-k="section">${secOpts(c.section)}</select></label>
        <div class="field"><span>N/A</span><div class="row">
          <label class="check"><input type="checkbox" data-k="na_allowed" ${c.na_allowed ? "checked" : ""} ${c.type === "flag" ? "disabled" : ""}> разрешить</label>
          <input type="text" data-k="na_condition" value="${esc(c.na_condition || "")}" placeholder="условие N/A" style="flex:1" ${c.na_allowed ? "" : "disabled"}></div></div>
      </div>
      <label class="field" style="margin-top:10px"><span>Формулировка (что значит «Да» / как ставить балл)</span><textarea rows="2" data-k="description">${esc(c.description || "")}</textarea></label>
      ${c.type === "scale" ? `<div style="margin-top:8px"><span class="muted small">Ступени шкалы (верхняя ступень = полный вес)</span>
        ${(c.scale || []).map((s, j) => `<div class="scale-row"><input type="number" min="0" data-sv="${j}" value="${esc(s.value)}"><input type="text" data-sl="${j}" value="${esc(s.label || "")}" placeholder="описание ступени"><button class="btn small danger" data-sdel="${j}">✕</button></div>`).join("")}
        <button class="btn small" data-sadd style="margin-top:6px">＋ ступень</button></div>` : ""}
      <label class="field" style="margin-top:10px"><span>Подсказки для ИИ (как именно проверять — уточнения, примеры, пограничные случаи)</span><textarea rows="3" data-k="guidance">${esc(c.guidance || "")}</textarea></label>
    </div>`).join("");

    box.querySelectorAll(".crit-edit").forEach((card) => {
      const i = +card.dataset.i;
      const c = draft.criteria[i];
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
          validate();
        });
      });
      card.querySelectorAll("[data-sv]").forEach((el) => el.addEventListener("input", () => { c.scale[+el.dataset.sv].value = Number(el.value); validate(); }));
      card.querySelectorAll("[data-sl]").forEach((el) => el.addEventListener("input", () => { c.scale[+el.dataset.sl].label = el.value; }));
      card.querySelectorAll("[data-sdel]").forEach((el) => el.addEventListener("click", () => { c.scale.splice(+el.dataset.sdel, 1); renderCriteria(); validate(); }));
      const sadd = card.querySelector("[data-sadd]");
      if (sadd) sadd.addEventListener("click", () => { c.scale.push({ value: 0, label: "" }); renderCriteria(); });
      card.querySelector("[data-del]").addEventListener("click", () => { if (confirm(`Удалить критерий «${c.name}»?`)) { draft.criteria.splice(i, 1); renderCriteria(); validate(); } });
      const up = card.querySelector("[data-up]");
      const down = card.querySelector("[data-down]");
      up.addEventListener("click", () => { [draft.criteria[i - 1], draft.criteria[i]] = [draft.criteria[i], draft.criteria[i - 1]]; renderCriteria(); });
      down.addEventListener("click", () => { [draft.criteria[i + 1], draft.criteria[i]] = [draft.criteria[i], draft.criteria[i + 1]]; renderCriteria(); });
    });
  };

  root.querySelector('[data-act="add-section"]').addEventListener("click", () => { draft.sections = draft.sections || []; draft.sections.push({ id: `section_${draft.sections.length + 1}`, name: "Новая секция" }); renderSections(); renderCriteria(); validate(); });
  root.querySelector('[data-act="add-criterion"]').addEventListener("click", () => {
    let n = draft.criteria.length + 1;
    while (draft.criteria.some((c) => c.id === `criterion_${n}`)) n++;
    draft.criteria.push({ id: `criterion_${n}`, name: "Новый критерий", section: (draft.sections && draft.sections[0] && draft.sections[0].id) || null, type: "binary", weight: 10, na_allowed: false, na_condition: "", description: "Да — ", guidance: "" });
    renderCriteria(); validate();
    root.querySelector(`.crit-edit[data-i="${draft.criteria.length - 1}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  root.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", async () => {
    const cl = await api.get(`/api/checklists/${b.dataset.open}`);
    draft = clean(cl); baseId = cl._id; renderChecklist(root, params, query, alive);
  }));
  root.querySelectorAll("[data-activate]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post(`/api/checklists/${b.dataset.activate}/activate`); toast("Версия активирована", "ok"); renderChecklist(root, params, query, alive); } catch (e) { errorToast(e); }
  }));

  root.querySelector('[data-act="save"]').addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const fromId = (versions.find((v) => v.active) || {}).id;
      const saved = await api.post("/api/checklists", { data: draft, note: root.querySelector("[data-note]").value.trim() || "Изменение через интерфейс", activate: true });
      toast(`Сохранена версия v${saved.version}`, "ok");
      draft = clean(saved); baseId = saved._id;
      lastApply = { from: fromId, to: saved._id, version: saved.version };
      state.lastChecklistId = saved._id;
      await renderChecklist(root, params, query, alive);
    } catch (err) { errorToast(err); e.target.disabled = false; }
  });

  renderApply(root, groups);
  renderSections();
  renderCriteria();
  validate();
}

function renderApply(root, groups) {
  const area = root.querySelector("#applyArea");
  if (!lastApply) { area.innerHTML = ""; return; }
  area.innerHTML = `<div class="card card-pad stack">
    <div class="row"><b>Применить версию v${lastApply.version} к диалогам</b>
      <span class="muted small">Неизменённые критерии переиспользуются, смена веса пересчитывается без модели — перегоняются только изменённые формулировки.</span></div>
    <div class="row">
      <select data-ag>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select>
      <select data-av><option value="1">1 голос</option><option value="3">3 голоса</option></select>
      <button class="btn primary" data-apply>Применить</button>
    </div>
    <div data-ajob></div><div data-diff></div></div>`;
  area.querySelector("[data-apply]").addEventListener("click", async (e) => {
    const group = area.querySelector("[data-ag]").value;
    e.target.disabled = true;
    try {
      const { job_id } = await api.post("/api/evaluate", { group_id: group, checklist_id: lastApply.to, votes: Number(area.querySelector("[data-av]").value) });
      const job = await pollJob(job_id, (j) => { area.querySelector("[data-ajob]").innerHTML = jobBox(j, `Применение v${lastApply.version}`); });
      toast(`Готово: ${job.done} диалогов, вызовов модели ${job.llm_calls}`, "ok");
      const diff = lastApply.from ? await api.get("/api/diff" + qs({ group_id: group, from_checklist: lastApply.from, to_checklist: lastApply.to })) : [];
      area.querySelector("[data-diff]").innerHTML = diffHtml(diff);
    } catch (err) { errorToast(err); } finally { e.target.disabled = false; }
  });
}

function diffHtml(diff) {
  if (!diff.length) return `<div class="muted small">Нет оценок прошлой версии для сравнения.</div>`;
  const changed = diff.filter((d) => d.before !== d.after || d.changed.length);
  diff = [...changed, ...diff.filter((d) => !changed.includes(d))];
  return `<h3 style="margin-top:8px">Что изменилось: ${changed.length} из ${diff.length} диалогов</h3>
    <table class="grid small"><tr><th>Диалог</th><th>Итог до</th><th>Итог после</th><th>Изменённые критерии</th></tr>
    ${diff.map((d) => `<tr><td><a class="mono" href="#/dialog/${encodeURIComponent(d.conversation_id)}">${esc(d.conversation_id)}</a></td>
      <td>${fmtScore(d.before)}</td><td><b>${fmtScore(d.after)}</b>${d.after !== d.before ? ` <span class="${d.after > d.before ? "pill yes" : "pill no"}">${d.after > d.before ? "+" : ""}${fmtScore(Math.round((d.after - d.before) * 100) / 100)}</span>` : ""}</td>
      <td>${d.changed.map((c) => `${esc(c.criterion_id)}: ${esc(c.before)} → ${esc(c.after)}`).join("; ") || "—"}</td></tr>`).join("")}</table>`;
}
