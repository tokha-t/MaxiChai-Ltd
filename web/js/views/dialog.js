// Карточка диалога: транскрипт, оценка с обоснованиями и переходом к репликам, апелляции, переоценка.
import { api } from "../api.js";
import { SPEAKER, channelIcon, errorToast, esc, fmtDate, fmtMoney, fmtScore, h, icon, jobBox, modal, notifyJobStarted, pollJob, scoreClass, state, toast, valuePill } from "../ui.js";

const runningJobs = new Map();
let renderVersion = 0;

export async function renderDialog(root, params = [], query = null, alive = () => true) {
  const version = ++renderVersion;
  const isCurrent = () => alive() && version === renderVersion;
  const cid = params[0];
  const [data, activeChecklist] = await Promise.all([
    api.get(`/api/conversations/${encodeURIComponent(cid)}`),
    api.get("/api/checklists/active").catch(() => null),
  ]);
  if (!isCurrent()) return;
  const { conversation: conv, turns, facts, evaluation: ev, history: hist, appeals } = data;
  const isCall = (conv.channel || "").toLowerCase().includes("call");
  const audioUrl = safeAudioUrl(conv.audio_url);
  const ok = ev && ev.status === "ok";
  const finalRows = ok ? ev.final.criteria : [];
  const finalScore = ok ? ev.final.score : null;
  const evidenceSet = new Set(finalRows.flatMap((r) => r.evidence_turns || []));
  const appealsByCrit = {};
  for (const a of appeals || []) if (ok && a.evaluation_id === ev._id) (appealsByCrit[a.criterion_id] = appealsByCrit[a.criterion_id] || []).push(a);
  const holdRanges = facts.hold_ranges || [];
  const inHold = (n) => holdRanges.some(([a, b]) => n > a && n < b);
  const recordingGaps = facts.recording_gaps || [];
  const reload = async () => {
    if (!isCurrent()) return;
    try { await renderDialog(root, params, null, alive); }
    catch (err) { if (alive()) errorToast(err); }
  };

  const versionLabel = ok ? `Оценено по v${ev.checklist_version ?? "—"}${activeChecklist?.version && activeChecklist.version !== ev.checklist_version ? `; сейчас активна v${activeChecklist.version}` : ""}` : ev?.status === "error" ? "Последняя попытка оценки не завершена" : "Оценка ещё не выполнена";
  root.innerHTML = `
    <div class="page-head dialog-heading">
      ${state.embed ? "" : `<a href="#/dialogs" class="dialog-back">К списку диалогов</a>`}
      <h1>Диалог <span class="mono dialog-id" title="${esc(conv.conversation_id)}">${esc(conv.conversation_id)}</span></h1>
    </div>
    <div class="dialog-context">
      <span>${channelIcon(conv.channel)}</span><span>${esc(conv.language || "Язык не указан")}</span>
      ${conv.group_id ? `<span>Группа ${esc(conv.group_id)}</span>` : ""}
      <span>${esc(versionLabel)}</span>${ok ? `<span>${fmtDate(ev.created_at)}</span>` : ""}
    </div>
    <div id="jobArea" aria-live="polite"></div>
    <div class="dialog-tabs tabs" role="tablist" aria-label="Содержимое диалога" hidden>
      <button id="dialog-score-tab" type="button" role="tab" aria-controls="evalCol" aria-selected="true" data-dialog-tab="score">Оценка</button>
      <button id="dialog-transcript-tab" type="button" role="tab" aria-controls="transcript" aria-selected="false" tabindex="-1" data-dialog-tab="transcript">Транскрипт</button>
    </div>
    <div class="dialog-layout">
      <section class="stack dialog-score" id="evalCol" aria-labelledby="dialog-score-title">
        ${ok ? evaluationHtml(ev, finalScore, finalRows, appealsByCrit) : ev && ev.status === "error"
          ? `<div class="card card-pad" role="status"><h2 id="dialog-score-title">Оценка не удалась</h2><p class="muted">Не удалось получить результат оценки. Проверяющий может повторить запуск в служебных действиях.</p><p>Нового результата нет. Предыдущие оценки доступны в истории.</p></div>`
          : `<div class="card card-pad"><h2 id="dialog-score-title">Нет оценки</h2><p>Диалог ещё не оценён.${state.role === "reviewer" ? " Запустите оценку в служебных действиях ниже." : " Запуск оценки доступен проверяющему."}</p></div>`}
        ${appealsHtml(appeals || [])}
        ${factsHtml(facts)}
        ${historyHtml(hist || [])}
        ${ok ? technicalHtml(ev) : ""}
        ${state.role === "reviewer" ? `<details class="card card-pad dialog-service"><summary>Служебные действия</summary>
          <h2>Запуск оценки</h2><p class="muted">Новый анализ этого диалога по активной версии правил. Предыдущая оценка сохранится в истории.</p>
          <div class="row"><label class="field"><span>Число проверок</span><select data-votes><option value="1">Одна проверка</option><option value="3">Три проверки</option></select></label>
          <button class="btn primary" data-act="reeval">${icon("refresh")}${ok ? "Переоценить диалог" : "Оценить диалог"}</button></div>
          <p class="muted small">Несколько проверок помогают увидеть расхождения между ответами моделей. Время и стоимость зависят от моделей и числа проверок; согласие не означает вероятность правильности.</p>
        </details>` : ""}
      </section>
      <section class="card transcript dialog-transcript" id="transcript" aria-labelledby="dialog-transcript-title">
        <div class="transcript-head">
          <div class="row"><h2 id="dialog-transcript-title">Транскрипт</h2><span class="muted small">${turns.length} реплик</span></div>
          <button class="btn dialog-return" data-return-criterion hidden>Вернуться к критерию</button>
          ${isCall ? `<div class="call-context">${audioUrl ? `<audio controls preload="none" src="${esc(audioUrl)}" id="player" aria-label="Запись звонка"></audio>
            <p class="small" id="audio-status" role="status" hidden></p>
            <div class="small muted">Клик по реплике перематывает запись. Если плеер не играет — <a href="${esc(audioUrl)}" target="_blank" rel="noopener">Открыть запись</a>.</div>` : `<div class="small muted">Запись недоступна.</div>`}
            <p class="small muted">Транскрипт звонка распознан автоматически; удержание и паузы записи отмечены.</p></div>` : ""}
        </div>
        <div class="turns">${turns.map((t) => segmentHtml(t.n, holdRanges, recordingGaps) + turnHtml(t, inHold(t.n), evidenceSet.has(t.n), isCall)).join("") || `<div class="empty">В диалоге пока нет реплик.</div>`}</div>
      </section>
    </div>`;

  // На узком экране оценка открыта первой; скрытая панель исключена из обхода Tab.
  const narrow = window.matchMedia("(max-width: 900px)");
  const tablist = root.querySelector(".dialog-tabs");
  const panels = { score: root.querySelector("#evalCol"), transcript: root.querySelector("#transcript") };
  const tabs = [...root.querySelectorAll("[data-dialog-tab]")];
  let activePanel = "score";
  const setPanel = (name) => {
    activePanel = name;
    tablist.hidden = !narrow.matches;
    for (const [key, panel] of Object.entries(panels)) {
      panel.hidden = narrow.matches && key !== activePanel;
      if (narrow.matches) { panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `dialog-${key}-tab`); }
      else { panel.removeAttribute("role"); panel.setAttribute("aria-labelledby", `dialog-${key}-title`); }
    }
    tabs.forEach((tab) => { const selected = tab.dataset.dialogTab === activePanel; tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1; });
  };
  root._dialogMediaCleanup?.();
  const resized = () => {
    if (!isCurrent()) { narrow.removeEventListener("change", resized); return; }
    setPanel(activePanel);
  };
  narrow.addEventListener("change", resized);
  root._dialogMediaCleanup = () => narrow.removeEventListener("change", resized);
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => setPanel(tab.dataset.dialogTab));
    tab.addEventListener("keydown", (event) => {
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : null;
      if (next !== null) { event.preventDefault(); setPanel(tabs[next].dataset.dialogTab); tabs[next].focus(); }
    });
  });
  setPanel(activePanel);

  const player = root.querySelector("#player");
  const audioStatus = root.querySelector("#audio-status");
  const showAudioError = () => {
    if (!isCurrent() || !audioStatus) return;
    audioStatus.textContent = "Не удалось воспроизвести запись. Попробуйте открыть её по ссылке ниже. Транскрипт остаётся доступен.";
    audioStatus.hidden = false;
  };
  if (player) {
    player.addEventListener("error", showAudioError);
    player.addEventListener("loadedmetadata", () => {
      if (!isCurrent() || !audioStatus) return;
      audioStatus.textContent = "";
      audioStatus.hidden = true;
    });
    if (player.error) showAudioError();
  }
  const returnButton = root.querySelector("[data-return-criterion]");
  const criterionNodes = new Map([...root.querySelectorAll("[data-criterion-id]")].map((node) => [node.dataset.criterionId, node]));
  let returnCriterion = null;
  const flashTimers = new WeakMap();
  const scrollBehavior = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  const jump = (n, origin) => {
    if (!isCurrent()) return;
    const el = root.querySelector(`#turn-${n}`);
    if (!el) return;
    if (origin && criterionNodes.has(origin)) {
      returnCriterion = origin;
      returnButton.hidden = false;
      const row = finalRows.find((r) => r.criterion_id === origin);
      returnButton.textContent = `Вернуться к критерию «${row?.name || origin}»`;
    }
    setPanel("transcript");
    el.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
    el.focus({ preventScroll: true });
    clearTimeout(flashTimers.get(el));
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    flashTimers.set(el, setTimeout(() => el.classList.remove("flash"), 2000));
    const t = turns.find((x) => x.n === n);
    if (player && t && t.rel_sec !== null && t.rel_sec !== undefined && Number.isFinite(Number(t.rel_sec))) {
      const seek = () => {
        if (!isCurrent() || player.error) return;
        try { player.currentTime = Math.max(0, Number(t.rel_sec)); }
        catch { /* запись недоступна; текст остаётся доступным */ }
      };
      if (player.readyState >= 1) seek();
      else { player.onloadedmetadata = seek; player.load(); }
    }
  };
  root.querySelectorAll("[data-ev]").forEach((button) => button.addEventListener("click", () => jump(Number(button.dataset.ev), button.dataset.origin)));
  const openCriterion = (id) => {
    const criterion = criterionNodes.get(id);
    if (!criterion) return;
    setPanel("score");
    criterion.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
    criterion.querySelector("h3")?.focus({ preventScroll: true });
  };
  returnButton.addEventListener("click", () => openCriterion(returnCriterion));
  root.querySelectorAll("[data-open-criterion]").forEach((button) => button.addEventListener("click", () => openCriterion(button.dataset.openCriterion)));
  root.querySelectorAll(".turn[data-n]").forEach((turn) => {
    turn.addEventListener("click", () => jump(Number(turn.dataset.n)));
    turn.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); jump(Number(turn.dataset.n)); }
    });
  });

  // апелляция
  root.querySelectorAll("[data-appeal]").forEach((b) => b.addEventListener("click", () => {
    const row = finalRows.find((r) => r.criterion_id === b.dataset.appeal);
    appealModal(ev, row, reload);
  }));

  // Одно наблюдение за серверной задачей на диалог, даже после смены роли/экрана.
  const votesSel = root.querySelector("[data-votes]");
  const evalButton = root.querySelector('[data-act="reeval"]');
  const clearJobQuery = () => {
    if (!isCurrent()) return;
    const url = new URL(location.href);
    const [path, raw = ""] = url.hash.split("?");
    const search = new URLSearchParams(raw);
    search.delete("job");
    url.hash = path + (search.size ? `?${search}` : "");
    window.history.replaceState(null, "", url);
  };
  const observe = async (task) => {
    if (evalButton) evalButton.disabled = true;
    if (votesSel) votesSel.disabled = true;
    task.onTick = (job) => {
      if (isCurrent()) root.querySelector("#jobArea").innerHTML = jobBox(job, "Оценка диалога");
    };
    if (task.snapshot) task.onTick(task.snapshot);
    try {
      const job = await task.promise;
      if (!isCurrent()) return;
      if (runningJobs.get(cid) === task) runningJobs.delete(cid);
      clearJobQuery();
      const err = (job.results || []).find((r) => r.status !== "ok");
      if (job.status === "failed" || err) toast("Не удалось завершить оценку. Проверьте сохранённые настройки моделей и повторите запуск.", "error");
      else toast(`Оценено · ${fmtMoney(job.cost_usd)} · ${job.elapsed_sec} с`, "ok");
      await reload();
    } catch (err) {
      if (isCurrent()) {
        if (runningJobs.get(cid) === task) runningJobs.delete(cid);
        clearJobQuery();
        errorToast(err);
        if (evalButton) evalButton.disabled = false;
        if (votesSel) votesSel.disabled = false;
      }
    }
  };
  evalButton?.addEventListener("click", () => {
    if (!isCurrent() || runningJobs.has(cid)) return;
    const task = trackJob(cid, async () => {
      const jobs = await api.get("/api/jobs");
      if (jobs.some((job) => job.status === "running")) throw new Error("Оценка уже выполняется. Дождитесь завершения текущей задачи.");
      const result = await api.post("/api/evaluate", { conversation_ids: [cid], votes: Number(votesSel.value), reuse: false });
      notifyJobStarted();
      return result;
    });
    observe(task);
  });

  const jobId = query && query.get("job");
  let task = runningJobs.get(cid);
  if (!task && jobId) task = trackJob(cid, () => Promise.resolve({ job_id: jobId }));
  if (task) observe(task);
}

function trackJob(cid, start) {
  const task = { snapshot: null, onTick: null, promise: null };
  runningJobs.set(cid, task);
  task.promise = (async () => {
    const { job_id } = await start();
    return pollJob(job_id, (job) => {
      task.snapshot = job;
      task.onTick?.(job);
    });
  })();
  return task;
}

function safeAudioUrl(raw) {
  if (!raw || typeof raw !== "string") return null;
  try {
    const url = new URL(raw, location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function segmentHtml(n, holds, gaps) {
  const labels = [];
  for (const [start, end] of holds) {
    if (n === start) labels.push("Начало удержания · внутренняя консультация");
    if (n === end) labels.push("Конец удержания");
  }
  for (const [start, end] of gaps) {
    if (n === start) labels.push(end == null ? "Пауза записи · возобновление не отмечено" : "Начало паузы записи");
    if (n === end) labels.push("Запись возобновлена");
  }
  return labels.map((label) => `<div class="segment-sep">${esc(label)}</div>`).join("");
}

function turnHtml(t, hold, isEvidence, isCall) {
  return `<div class="turn ${esc(t.speaker)} ${hold ? "hold" : ""} ${isEvidence ? "evidence" : ""}" id="turn-${t.n}" data-n="${t.n}" role="button" tabindex="0" aria-label="${isCall ? "Перемотать к реплике" : "Выделить реплику"} ${t.n}">
    <div class="n">[${t.n}]</div>
    <div><div class="who">${esc(SPEAKER[t.speaker] || t.speaker)}<span class="t">${esc(t.rel || "")}</span>${hold ? ` <span class="hold-note">удержание: внутренняя консультация</span>` : ""}</div>
    <div class="txt">${esc(t.text)}</div></div></div>`;
}

function evidenceButtons(turns, criterionId) {
  return (turns || []).map((n) => `<button class="ev" type="button" data-ev="${n}" data-origin="${esc(criterionId || "")}" aria-label="Перейти к реплике ${n}">[${n}]</button>`).join(" ");
}

function displayValue(row) {
  return row.value == null ? `<span class="pill na">Не применяется (N/A)</span>` : valuePill(row);
}

function lossRows(rows) {
  return rows.flatMap((row) => {
    if (row.type === "flag" || row.value == null || row.applicable === false || row.weight === 0 || row.effective_weight === 0) return [];
    const hasParts = Number.isFinite(row.effective_weight) && Number.isFinite(row.score_contribution);
    const loss = hasParts ? row.effective_weight - row.score_contribution : null;
    const reduced = hasParts ? loss > 0 : row.value === false || (row.type === "scale" && Number.isFinite(row.points) && Number.isFinite(row.max_points) && row.points < row.max_points);
    return reduced ? [{ row, loss }] : [];
  }).sort((a, b) => (b.loss ?? -1) - (a.loss ?? -1));
}

function currentRationale(row, appeals) {
  if (!row.overridden) return row.rationale || "Обоснование не передано.";
  // Сервер применяет последнее changed по id; confirmed не отменяет это решение.
  // appeals уже ограничены текущей оценкой в renderDialog.
  const decision = (appeals || []).filter((appeal) => appeal.status === "changed" && appeal.criterion_id === row.criterion_id)
    .reduce((latest, appeal) => !latest || Number(appeal.id) > Number(latest.id) ? appeal : latest, null);
  return `Причина проверяющего: ${decision?.decision_reason || "Причина решения не передана."}`;
}

function evaluationHtml(ev, score, rows, appealsByCrit) {
  const aiScore = ev.ai_score?.final_score;
  const changed = aiScore !== score.final_score;
  const critical = rows.filter((row) => row.type === "flag" && row.value === true);
  const losses = lossRows(rows);
  const originals = new Map((ev.criteria || []).map((r) => [r.criterion_id, r]));
  const sectionNames = new Map((score.sections || []).map((section) => [section.id, section.name]));
  const formula = Number.isFinite(score.earned) && Number.isFinite(score.applicable_weight) && score.applicable_weight > 0 && Number.isFinite(score.final_score);
  return `
    <section class="card card-pad score-summary" aria-labelledby="dialog-score-title">
      <h2 id="dialog-score-title">Итоговая оценка</h2>
      <div class="score-hero"><div class="score-big ${scoreClass(score.final_score)}">${fmtScore(score.final_score)}<small> / 100</small></div>
        <div>${changed ? `<span class="pill info">После апелляции</span><p>Исходная оценка ИИ: <span class="score">${fmtScore(aiScore)}</span></p>` : `<span class="muted">${score.final_score == null ? "Нет применимых критериев с баллами" : "По применимым критериям"}</span>`}</div>
      </div>
    </section>
    <section class="loss-summary" aria-labelledby="loss-title">
      <h2 id="loss-title">Что снизило балл</h2>
      ${losses.length ? `<ul class="loss-list">${losses.slice(0, 3).map(({ row, loss }) => `<li class="loss-item">
        <div class="row"><button class="btn ghost loss-title" data-open-criterion="${esc(row.criterion_id)}">${esc(row.name)}</button>${loss === null ? "" : `<span class="loss-amount">−${fmtScore(loss)} балл.</span>`}</div>
        <p>${esc(currentRationale(row, appealsByCrit[row.criterion_id]).slice(0, 220))}${currentRationale(row, appealsByCrit[row.criterion_id]).length > 220 ? "…" : ""}</p>
        <div class="criterion-evidence">${evidenceButtons(row.evidence_turns, row.criterion_id) || `<span class="muted">Реплики не указаны</span>`}</div>
      </li>`).join("")}</ul>${losses.length > 3 ? `<p class="muted">Ещё замечаний: ${losses.length - 3}. Все критерии доступны ниже.</p>` : ""}` : `<p class="muted">${score.final_score == null ? "Нет применимых критериев для расчёта балла." : score.final_score < 100 ? "Данных для выделения основных причин недостаточно. Обоснования критериев доступны ниже." : "Снижение по применимым критериям не выявлено."}</p>`}
    </section>
    ${score.critical_flag ? `<section class="crit-banner" aria-labelledby="critical-title"><h2 id="critical-title">Грубая ошибка</h2>
      ${critical.map((row) => `<p>${esc(currentRationale(row, appealsByCrit[row.criterion_id]))}</p><div class="criterion-evidence">${evidenceButtons(row.evidence_turns, row.criterion_id)}</div>`).join("") || `<p>В оценке отмечена грубая ошибка; подробности не переданы.</p>`}
      <p class="small">Это отдельный признак, без числового штрафа к показанному итогу.</p></section>` : ""}
    <section class="criteria-section" aria-labelledby="criteria-title"><h2 id="criteria-title">Оценка по критериям</h2>
      <div class="card">${rows.map((row) => criterionHtml(row, appealsByCrit[row.criterion_id] || [], originals.get(row.criterion_id), sectionNames.get(row.section) || row.section)).join("")}</div>
    </section>
    <details class="card card-pad calculation-details"><summary>Как рассчитан итог</summary>
      <h2>Расчёт и разделы оценки</h2>
      <p>${formula ? `<span class="score">${fmtScore(score.earned)} ÷ ${fmtScore(score.applicable_weight)} × 100 = ${fmtScore(score.final_score)}</span>` : score.applicable_weight === 0 ? "Для расчёта итога нет применимых критериев с ненулевым весом." : "Числовые составляющие расчёта не переданы."}</p>
      <p class="muted small">Критерии «Не применяется (N/A)» исключаются из расчёта, их вес распределяется между применимыми критериями. Из-за округления сумма показанных потерь может отличаться в последнем знаке.</p>
      <div class="sections">${(score.sections || []).map((section) => `<div class="section-box"><h3 class="name">${esc(section.name)}</h3>
        ${section.score == null ? `<div class="val muted">${section.flags?.length ? "Грубая ошибка" : rows.some((row) => row.section === section.id && row.type !== "flag") ? "Не применяется (N/A)" : "Нет нарушений"}</div>` : `<div class="val score">${fmtScore(section.score)}%</div><p class="muted small">${fmtScore(section.earned)} из ${fmtScore(section.max)} баллов</p>`}
      </div>`).join("")}</div>
    </details>`;
}

function criterionHtml(row, appeals, original, sectionName) {
  const open = appeals.find((appeal) => appeal.status === "open");
  const decided = appeals.filter((appeal) => appeal.status !== "open");
  const isFlag = row.type === "flag";
  const evidenceLabel = row.value == null ? "Почему не применяется" : row.value === false || (row.type === "scale" && row.points < row.max_points) ? "Проверенный участок" : isFlag && row.value ? "Нарушение" : "Доказательства";
  const checks = row.votes || [];
  const agreement = checks.length > 1 && Number.isFinite(row.agreement) ? `${Math.round(row.agreement * checks.length)} из ${checks.length} проверок дали согласованный результат.` : checks.length === 1 ? "Выполнена одна проверка." : "Число проверок не указано.";
  return `<article class="criterion" data-criterion-id="${esc(row.criterion_id)}">
    <div class="criterion-main"><h3 class="title" tabindex="-1">${esc(row.name)}</h3>
      <div class="criterion-result">${displayValue(row)}${row.needs_review ? `<span class="pill warn">Требует проверки</span>` : ""}${row.overridden ? `<span class="pill info">Изменено проверяющим</span>` : ""}</div>
      <p class="rationale">${esc(currentRationale(row, appeals))}</p>
      <div class="criterion-evidence"><span class="muted small">${evidenceLabel}:</span>${evidenceButtons(row.evidence_turns, row.criterion_id) || `<span class="muted">Реплики не указаны</span>`}</div>
      ${row.overridden ? `<p class="override">ИИ: ${esc(original?.value_label ?? labelOf(row.ai_value))} → решение проверяющего: ${esc(row.value_label)}</p>` : ""}
    </div>
    <footer class="criterion-action">
      ${open ? `<span class="pill info">Апелляция на рассмотрении</span>` : state.role === "operator" ? `<button class="btn" data-appeal="${esc(row.criterion_id)}" aria-label="Оспорить критерий ${esc(row.name)}">Оспорить</button>` : `<span class="muted small">${decided.length ? "Решение по апелляции сохранено" : "Апелляции нет"}</span>`}
    </footer>
    <details class="calculation-details"><summary>Как рассчитано</summary>
      ${row.overridden ? `<p><b>Исходное обоснование ИИ:</b> ${esc(original?.rationale || row.rationale || "Обоснование не передано.")}</p>` : ""}
      <dl class="kv"><dt>Раздел</dt><dd>${esc(sectionName)}</dd><dt>Результат</dt><dd>${esc(row.value_label ?? labelOf(row.value))}</dd>
        ${isFlag ? `<dt>Учёт в баллах</dt><dd>Отдельный признак; без числового штрафа</dd>` : `<dt>Баллы критерия</dt><dd class="score">${fmtScore(row.points)} из ${fmtScore(row.max_points)}</dd><dt>Исходный вес</dt><dd class="score">${fmtScore(row.weight)}</dd><dt>Вес после N/A</dt><dd class="score">${fmtScore(row.effective_weight)}</dd><dt>Вклад в итог из 100</dt><dd class="score">${fmtScore(row.score_contribution)}</dd>`}
      </dl><p>${agreement} Согласие моделей не является измеренной вероятностью правильности.</p>
      ${checks.length ? `<ul class="check-responses">${checks.map((vote, index) => `<li><b>Проверка ${index + 1}</b>${vote.model ? ` · ${esc(vote.model)}` : ""}: ${esc(vote.verdict || "—")}<p>${esc(vote.rationale || "Обоснование не передано.")}</p>${evidenceButtons(vote.evidence_turns, row.criterion_id)}</li>`).join("")}</ul>` : ""}
      ${decided.map((appeal) => `<p class="override">Апелляция: «${esc(appeal.comment || "")}» → ${appeal.status === "changed" ? "оценка изменена" : "оценка подтверждена"}${appeal.decision_reason ? ` · причина: ${esc(appeal.decision_reason)}` : ""}</p>`).join("")}
    </details>
  </article>`;
}

function technicalHtml(ev) {
  const usage = ev.usage || {};
  return `<details class="card card-pad dialog-technical"><summary>Технические сведения</summary><h2>Происхождение оценки</h2>
    <dl class="kv"><dt>Модели</dt><dd>${esc(ev.model || "Не указаны")}</dd><dt>Проверок</dt><dd>${ev.votes ?? "—"}</dd>
      <dt>Вызовов модели</dt><dd>${usage.calls ?? "—"}</dd><dt>Время оценки</dt><dd>${usage.latency_sec ?? "—"} с</dd>
      <dt>Расчётная стоимость</dt><dd>${fmtMoney(usage.cost_usd)}</dd><dt>Токены вход / выход</dt><dd>${usage.prompt_tokens ?? "—"} / ${usage.completion_tokens ?? "—"}</dd>
      <dt>Версия чек-листа</dt><dd>v${ev.checklist_version ?? "—"}</dd><dt>Дата оценки</dt><dd>${fmtDate(ev.created_at)}</dd>
    </dl><p class="small muted">Стоимость рассчитана по настройкам приложения и не является проверенным счётом провайдера.</p></details>`;
}

function labelOf(v) {
  if (v === null || v === undefined) return "N/A";
  if (typeof v === "number") return String(v);
  return v ? "Да" : "Нет";
}

function factsHtml(f) {
  if (!f) return "";
  const items = [];
  if (f.first_operator_turn) items.push(`Первая реплика оператора — [${f.first_operator_turn}] в ${esc(f.first_operator_time)}; первая реакция через ${f.first_response_delay_sec ?? "—"} с (порог ${f.threshold_sec} с) — ${f.first_response_delay_sec == null ? "время реакции не определено" : f.first_response_exceeds ? "<b style='color:var(--danger)'>превышен</b>" : "в пределах"}.`);
  else items.push("Оператор не написал ни одной реплики.");
  for (const p of f.long_pauses || []) items.push(`Пауза ${p.delay_sec} с перед репликой [${p.operator_turn}] — ${p.warned ? `оператор предупредил в [${p.warn_turn}]` : "без предупреждения"}${p.exceeds_threshold ? ", превышает порог" : ""}.`);
  items.push(f.identity_verified_turns && f.identity_verified_turns.length ? `Личность подтверждена: [${f.identity_verified_turns.join("], [")}].` : "Системного подтверждения личности нет.");
  if (f.operator_closed_turn) items.push(`Оператор закрыл чат: [${f.operator_closed_turn}].`);
  if (f.client_left_turn) items.push(`Клиент ушёл / связь прервалась: [${f.client_left_turn}].`);
  for (const [a, b] of f.hold_ranges || []) items.push(`Удержание клиента между [${a}] и [${b}].`);
  for (const [a, b] of f.recording_gaps || []) items.push(b == null ? `Пауза записи с [${a}], возобновление не отмечено.` : `Пауза записи между [${a}] и [${b}].`);
  return `<details class="card card-pad facts"><summary>Показатели диалога</summary><h2>Временные и системные события</h2><ul class="facts-list">${items.map((x) => `<li>${x}</li>`).join("")}</ul></details>`;
}

function appealsHtml(appeals) {
  if (!appeals.length) return "";
  return `<section class="card card-pad"><h2>Апелляции</h2>${appeals.map((a) => `<div style="margin-top:8px">
    <span class="pill ${a.status === "open" ? "info" : a.status === "changed" ? "warn" : "yes"}">${a.status === "open" ? "Ожидает решения проверяющего" : a.status === "changed" ? "Оценка изменена" : "Оценка подтверждена"}</span>
    <b>${esc((a.criterion && a.criterion.name) || a.criterion_id)}</b> · исходно: ${esc(a.original_label)}${a.decision_label ? ` → ${esc(a.decision_label)}` : ""}
    <div class="muted small">«${esc(a.comment || "")}» — ${esc(a.author || "")}${a.decision_reason ? ` · решение: ${esc(a.decision_reason)}` : ""}
    ${a.final_score !== undefined ? ` · итог: ИИ ${fmtScore(a.ai_final_score)} → ${fmtScore(a.final_score)}` : ""}</div></div>`).join("")}</section>`;
}

function historyHtml(hist) {
  if (!hist.length) return "";
  return `<details class="card card-pad dialog-history"><summary>История оценок (${hist.length})</summary><h2>Предыдущие результаты</h2><div class="table-scroll" tabindex="0" role="region" aria-label="История оценок">
    <table class="grid small" style="margin-top:8px"><tr><th>#</th><th>Итог</th><th>Версия правил</th><th>Модель</th><th>Когда</th></tr>
    ${hist.map((x) => `<tr><td>${x.id}</td><td>${x.status === "ok" ? fmtScore(x.final_score) : "ошибка"}${x.critical_flag ? " · Грубая ошибка" : ""}</td><td>v${x.version ?? "—"}</td><td>${esc(x.model || "")}</td><td>${fmtDate(x.created_at)}</td></tr>`).join("")}</table></div></details>`;
}

function appealModal(ev, row, onDone) {
  const original = (ev.criteria || []).find((r) => r.criterion_id === row.criterion_id) || row;
  const body = h(`<div class="stack">
    <div><b>${esc(row.name)}</b> — исходная оценка ИИ: ${displayValue(original)}</div>
    ${row.overridden ? `<div>Текущая оценка: ${displayValue(row)}</div>` : ""}
    <div class="quote">${esc(row.rationale)}</div>
    <label class="field"><span>Ваше имя</span><input type="text" data-author value="Оператор"></label>
    <label class="field"><span>Почему оценка неверна</span><textarea rows="4" data-comment placeholder="Например: я представился в первой реплике, имя указано в подписи"></textarea></label>
    <p class="muted small">Проверяющий увидит оценку, ваш комментарий, фрагмент диалога и второе мнение ИИ.</p></div>`);
  modal({
    title: "Апелляция на оценку", body,
    actions: [{ label: "Отмена" }, {
      label: "Отправить апелляцию", kind: "primary", onClick: async () => {
        const comment = body.querySelector("[data-comment]").value.trim();
        if (!comment) throw new Error("Добавьте комментарий");
        await api.post("/api/appeals", { evaluation_id: ev._id, criterion_id: row.criterion_id, comment, author: body.querySelector("[data-author]").value.trim() || "Оператор" });
        toast("Апелляция отправлена проверяющему", "ok");
        onDone();
      },
    }],
  });
}

export { renderDialog as render };
