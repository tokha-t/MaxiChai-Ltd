// Экран «Диалоги»: список, фильтры, загрузка данных, запуск оценки, выгрузка results.json.
import { api, qs } from "../api.js";
import { channelIcon, errorToast, esc, fmtDate, fmtMoney, fmtScore, h, jobBox, modal, pollJob, scoreClass, state, toast } from "../ui.js";

let currentJob = null;
let startingJob = false;
let renderSequence = 0;
let listObserver = null;

export async function renderDialogs(root, params = [], query = null, alive = () => true) {
  const sequence = ++renderSequence;
  const isCurrent = () => alive() && sequence === renderSequence;
  const refresh = () => {
    if (alive()) return renderDialogs(root, params, null, alive).catch(errorToast);
  };
  const observer = { busy: () => {}, tick: () => {}, done: refresh };
  listObserver = observer;
  const f = state.dialogFilters;
  if (query) {
    for (const key of ["group_id", "language", "channel", "q"]) {
      if (query.has(key)) f[key] = query.get(key);
    }
    for (const key of ["critical", "needs_review"]) {
      if (query.has(key)) f[key] = ["1", "true"].includes(query.get(key));
    }
  }
  root.setAttribute("aria-busy", "true");
  let groups, rows, cfg, st;
  try {
    [groups, rows, cfg, st] = await Promise.all([
    api.get("/api/groups"),
    api.get("/api/conversations" + qs({ group_id: f.group_id, language: f.language, channel: f.channel, critical: f.critical || undefined, needs_review: f.needs_review || undefined, q: f.q })),
    api.get("/api/config").catch(() => null),
    api.get("/api/settings").catch(() => null),
    ]);
  } catch (error) {
    if (!isCurrent()) return;
    root.removeAttribute("aria-busy");
    throw error;
  }
  if (!isCurrent()) return;
  root.removeAttribute("aria-busy");
  const selected = new Set();
  const evaluated = rows.filter((r) => r.evaluation && r.evaluation.status === "ok");
  const avg = evaluated.length ? evaluated.reduce((s, r) => s + (r.evaluation.final_score || 0), 0) / evaluated.length : null;
  const crit = evaluated.filter((r) => r.evaluation.critical_flag).length;
  const review = evaluated.filter((r) => r.evaluation.needs_review).length;
  const judges = cfg && cfg.judges ? cfg.judges.map((j) => j.model).join(" + ") : "";

  const search = document.activeElement?.matches('[data-f="q"]') ? document.activeElement : null;
  const searchSelection = search ? [search.selectionStart, search.selectionEnd] : null;
  root.innerHTML = `
    <div class="page-head">
      <h1>Диалоги</h1>
      <span class="sub">${judges ? "Судьи: " + esc(judges) : ""}</span>
      <span class="spacer"></span>
      <button class="btn" data-act="fetch">⬇ Загрузить набор из API</button>
      <button class="btn" data-act="upload">＋ Добавить диалог</button>
      <button class="btn" data-act="export">Экспорт results.json</button>
      <button class="btn" data-act="save">Сохранить в проект</button>
    </div>
    ${st && !st.ready ? `<div class="crit-banner" style="margin-bottom:12px;background:var(--warn-soft);border-color:#f0d9a8;color:#6b4a07">
      <b style="color:var(--warn)">Модели не настроены.</b> Откройте <a href="#/settings">⚙ Настройки</a>, вставьте ссылку API организаторов и бесплатный ключ Gemini — займёт минуту.</div>` : ""}
    <div id="jobArea"></div>
    <div class="summary-chips">
      <span class="chip">Диалогов: <b>${rows.length}</b></span>
      <span class="chip">Оценено: <b>${evaluated.length}</b></span>
      <span class="chip">Средний итог: <b>${avg === null ? "—" : fmtScore(Math.round(avg * 100) / 100)}</b></span>
      <button class="chip" data-act="critical" aria-pressed="${f.critical}" style="color:var(--danger)">Грубые ошибки: <b>${crit}</b></button>
      <span class="chip" style="color:var(--warn)">⚠ Нужна проверка: <b>${review}</b></span>
    </div>
    <div class="card">
      <div class="filters">
        <select data-f="group_id" aria-label="Группа"><option value="">Все группы</option>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select>
        <select data-f="language" aria-label="Язык"><option value="">Все языки</option><option>RU</option><option>KZ</option><option>EN</option></select>
        <select data-f="channel" aria-label="Канал"><option value="">Чаты и звонки</option><option value="chat">Только чаты</option><option value="call">Только звонки</option></select>
        <label class="check"><input type="checkbox" data-f="critical"> Только грубые ошибки</label>
        <label class="check"><input type="checkbox" data-f="needs_review"> Только ⚠ нужна проверка</label>
        <input type="search" data-f="q" aria-label="Поиск по ID" placeholder="Поиск по ID" style="width:170px">
        <span class="spacer"></span>
        <select data-votes title="Сколько независимых голосов моделей на диалог">
          <option value="1">1 голос (быстро, экономно)</option>
          <option value="3">3 голоса (точнее)</option>
        </select>
        <button class="btn" data-act="eval-selected" disabled>Оценить выбранные</button>
        <button class="btn primary" data-act="eval-all">${f.group_id ? "Оценить всю группу" : "Оценить все группы"}</button>
      </div>
      ${rows.length ? `<table class="grid">
        <thead><tr>
          <th style="width:28px"><input type="checkbox" data-all aria-label="Выбрать все диалоги в списке"></th>
          <th>Диалог</th><th>Канал</th><th>Язык</th><th>Итог</th><th>Грубая ошибка</th><th></th><th>Апелляции</th><th>Чек-лист</th><th>Оценён</th>
        </tr></thead>
        <tbody>${rows.map(rowHtml).join("")}</tbody></table>`
        : `<div class="empty">Диалогов нет. Нажмите «Загрузить набор из API» (группа DEV) или «Добавить диалог».</div>`}
    </div>`;

  // значения фильтров
  root.querySelectorAll("[data-f]").forEach((el) => {
    const k = el.dataset.f;
    if (el.type === "checkbox") el.checked = !!f[k]; else el.value = f[k] || "";
    const apply = () => { f[k] = el.type === "checkbox" ? el.checked : el.value.trim(); refresh(); };
    if (el.type === "search") el.addEventListener("input", apply);
    else el.addEventListener("change", apply);
  });

  // выбор строк
  const selBtn = root.querySelector('[data-act="eval-selected"]');
  const updateSel = () => { selBtn.disabled = startingJob || !!currentJob || !selected.size; selBtn.textContent = selected.size ? `Оценить выбранные (${selected.size})` : "Оценить выбранные"; };
  root.querySelectorAll("tbody tr").forEach((tr) => {
    const cb = tr.querySelector("input[type=checkbox]");
    cb.addEventListener("click", (e) => { e.stopPropagation(); cb.checked ? selected.add(tr.dataset.id) : selected.delete(tr.dataset.id); updateSel(); });
    tr.addEventListener("click", (event) => {
      if (event.target.closest("a, input, button")) return;
      location.hash = `#/dialog/${encodeURIComponent(tr.dataset.id)}`;
    });
  });
  const all = root.querySelector("[data-all]");
  if (all) all.addEventListener("change", () => {
    root.querySelectorAll("tbody tr").forEach((tr) => { const cb = tr.querySelector("input[type=checkbox]"); cb.checked = all.checked; all.checked ? selected.add(tr.dataset.id) : selected.delete(tr.dataset.id); });
    updateSel();
  });

  const votesSel = root.querySelector("[data-votes]");
  const syncBusy = () => {
    if (!isCurrent()) return;
    const busy = startingJob || !!currentJob;
    root.querySelectorAll('[data-act="eval-all"], [data-act="fetch"], [data-act="upload"], [data-act="export"], [data-act="save"], [data-votes]')
      .forEach((button) => { button.disabled = busy; });
    updateSel();
  };
  Object.assign(observer, {
    busy: syncBusy,
    tick: (job) => {
      if (!isCurrent()) return;
      const area = root.querySelector("#jobArea");
      if (area) area.innerHTML = jobBox(job);
    },
    done: refresh,
  });
  const observe = (jobId) => followJob(jobId);
  const runEval = async (ids = null) => {
    if (startingJob || currentJob) return;
    if (ids && !ids.length) return toast("Нет диалогов для оценки");
    startingJob = true;
    syncBusy();
    try {
      const body = { votes: Number(votesSel.value) };
      if (ids) body.conversation_ids = ids;
      else body.group_id = f.group_id; // Пустая группа означает все группы, независимо от фильтров таблицы.
      const { job_id } = await api.post("/api/evaluate", body);
      startingJob = false;
      await observe(job_id);
    } catch (e) { errorToast(e); }
    finally { startingJob = false; listObserver?.busy(); }
  };
  selBtn.addEventListener("click", () => runEval([...selected]));
  root.querySelector('[data-act="eval-all"]').addEventListener("click", () => runEval());
  root.querySelector('[data-act="critical"]').addEventListener("click", () => { f.critical = !f.critical; refresh(); });
  root.querySelector('[data-act="fetch"]').addEventListener("click", () => fetchModal(refresh));
  root.querySelector('[data-act="upload"]').addEventListener("click", () => uploadModal(refresh, alive));
  root.querySelector('[data-act="save"]').addEventListener("click", () => saveModal(groups, f.group_id));
  root.querySelector('[data-act="export"]').addEventListener("click", async () => {
    try {
      if (!groups.length) throw new Error("Сначала загрузите диалоги");
      if (f.group_id) await downloadResults(f.group_id);
      else if (groups.length === 1) await downloadResults(groups[0].group_id);
      else saveModal(groups, f.group_id);
    } catch (error) { errorToast(error); }
  });
  syncBusy();
  if (currentJob) observe(currentJob.id);
  if (searchSelection) {
    const input = root.querySelector('[data-f="q"]');
    input.focus();
    input.setSelectionRange(...searchSelection);
  }
}

function rowHtml(r) {
  const ev = r.evaluation;
  let score = `<span class="muted">не оценён</span>`;
  let critical = "";
  let warn = "";
  if (ev && ev.status === "ok") {
    const changed = ev.ai_score !== ev.final_score;
    score = `<span class="score ${scoreClass(ev.final_score)}">${fmtScore(ev.final_score)}</span>${changed ? ` <span class="ai-to-final">ИИ ${fmtScore(ev.ai_score)} → ${fmtScore(ev.final_score)}</span>` : ""}`;
    critical = ev.critical_flag ? `<span class="pill crit">Грубая ошибка</span><div class="reason">${esc((ev.critical_reason || "").slice(0, 140))}</div>` : `<span class="muted small">нет</span>`;
    warn = ev.needs_review ? `<span class="pill warn" title="Голоса моделей разошлись — стоит проверить человеку">⚠</span>` : "";
  } else if (ev && ev.status === "error") {
    score = `<span class="pill no" title="${esc(ev.error || "")}">ошибка оценки</span>`;
  }
  return `<tr class="clickable" data-id="${esc(r.conversation_id)}">
    <td><input type="checkbox" aria-label="Выбрать ${esc(r.conversation_id)}"></td>
    <td><a class="mono" href="#/dialog/${encodeURIComponent(r.conversation_id)}"><b>${esc(r.conversation_id)}</b></a><div class="muted small">${r.turns} реплик · ${esc(r.group_id || "")}</div></td>
    <td>${channelIcon(r.channel)}</td>
    <td>${esc(r.language || "—")}</td>
    <td>${score}</td>
    <td>${critical}</td>
    <td>${warn}</td>
    <td>${r.open_appeals ? `<span class="pill info">${r.open_appeals} открыта</span>` : ""}</td>
    <td>${ev && ev.checklist_version ? "v" + ev.checklist_version : ""}</td>
    <td class="small muted">${ev ? fmtDate(ev.evaluated_at) : ""}</td>
  </tr>`;
}

// У задачи один опрос, а подписчик заменяется при смене фильтра/возвращении на экран.
function followJob(jobId) {
  if (!currentJob || currentJob.id !== jobId) currentJob = { id: jobId, task: null, last: null };
  const tracked = currentJob;
  listObserver?.busy();
  if (tracked.last) listObserver?.tick(tracked.last);
  if (!tracked.task) {
    tracked.task = (async () => {
      let job = null;
      try {
        job = await pollJob(jobId, (snapshot) => {
          tracked.last = snapshot;
          listObserver?.tick(snapshot);
        });
        const errs = (job.results || []).filter((r) => r.status !== "ok");
        if (job.status === "failed") toast(`Оценка не завершена: ${job.error || "ошибка сервера"}`.slice(0, 300), "error");
        else if (errs.length) toast(`Готово с ошибками: ${errs.length}. ${errs[0].error || ""}`.slice(0, 300), "error");
        else toast(`Оценено диалогов: ${job.done} · ${fmtMoney(job.cost_usd)}`, "ok");
      } catch (error) { errorToast(error); }
      finally {
        if (currentJob === tracked) currentJob = null;
        listObserver?.busy();
        await listObserver?.done();
      }
      return job;
    })();
  }
  return tracked.task;
}

async function downloadResults(group) {
  if (!group) throw new Error("Выберите группу для экспорта");
  const data = await api.get("/api/export" + qs({ group_id: group }));
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "results.json";
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
        const res = await api.post("/api/datasets/fetch", { group_id: g });
        toast(`Загружено диалогов: ${res.count} (${res.group_id})`, "ok");
        state.dialogFilters.group_id = res.group_id;
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
    <label class="check"><input type="checkbox" data-now checked> Сразу оценить</label>
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
      label: "Загрузить", kind: "primary", onClick: async () => {
        if (startingJob || currentJob) throw new Error("Дождитесь завершения текущей оценки");
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
          Object.assign(state.dialogFilters, { group_id: res.group_id, language: "", channel: "", critical: false, needs_review: false, q: "" });
          if (body.querySelector("[data-now]").checked) {
            try {
              const { job_id } = await api.post("/api/evaluate", { conversation_ids: res.ids, votes: 1 });
              startingJob = false;
              if (res.ids.length === 1 && alive()) {
                location.hash = `#/dialog/${encodeURIComponent(res.ids[0])}?job=${job_id}`;
              } else {
                followJob(job_id);
                await onDone();
              }
            } catch (error) {
              errorToast(error);
              await onDone(); // Загруженные данные доступны даже если запуск оценки не удался.
            }
          } else await onDone();
        } finally {
          startingJob = false;
          listObserver?.busy();
        }
      },
    }],
  });
}

function saveModal(groups, selectedGroup = "") {
  const body = h(`<div class="stack">
    <p class="muted">Сохраняет оценки системы (без ручных правок по апелляциям) в формате сдачи 1.0 прямо в папку проекта и проверяет формат.</p>
    <div class="row">
      <label class="field"><span>Группа</span><select data-g>${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)} (${g.count})</option>`).join("")}</select></label>
      <label class="field"><span>Куда</span><select data-t><option value="dev">results/dev/results.json</option><option value="prod">results/prod/results.json</option></select></label>
    </div>
    <div data-out></div></div>`);
  if (selectedGroup) body.querySelector("[data-g]").value = selectedGroup;
  modal({
    title: "Сохранить results.json", body,
    actions: [
      { label: "Скачать файл", onClick: async ({ body: b }) => { await downloadResults(b.querySelector("[data-g]").value); return true; } },
      {
        label: "Сохранить в проект", kind: "primary", onClick: async ({ body: b }) => {
          const res = await api.post("/api/results/save", { group_id: b.querySelector("[data-g]").value, target: b.querySelector("[data-t]").value });
          const out = b.querySelector("[data-out]");
          out.innerHTML = res.errors.length
            ? `<div class="errors"><b>Есть ошибки формата — файл не сохранён, прежний results.json не изменён:</b><br>${res.errors.map(esc).join("<br>")}</div>`
            : `<div class="okbox">Сохранено: <b>${esc(res.path)}</b> — записей ${res.count}, оценено ${res.ok}${res.not_evaluated ? `, не оценено ${res.not_evaluated}` : ""}. Формат проверен.</div>`;
          if (res.warnings.length) out.innerHTML += `<div class="muted small" style="margin-top:6px">${res.warnings.map(esc).join("<br>")}</div>`;
          return true;
        },
      },
    ],
  });
}

export { renderDialogs as render };
