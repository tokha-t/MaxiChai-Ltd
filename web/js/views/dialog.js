// Карточка диалога: транскрипт, оценка с обоснованиями и переходом к репликам, апелляции, переоценка.
import { api } from "../api.js";
import { SPEAKER, channelIcon, errorToast, esc, fmtDate, fmtMoney, fmtScore, h, jobBox, modal, pollJob, scoreClass, state, toast, valuePill } from "../ui.js";

const runningJobs = new Map();
let renderVersion = 0;

export async function renderDialog(root, params = [], query = null, alive = () => true) {
  const version = ++renderVersion;
  const isCurrent = () => alive() && version === renderVersion;
  const cid = params[0];
  const data = await api.get(`/api/conversations/${encodeURIComponent(cid)}`);
  if (!isCurrent()) return;
  const { conversation: conv, turns, facts, evaluation: ev, history: hist, appeals } = data;
  const isCall = (conv.channel || "").toLowerCase().includes("call");
  const ok = ev && ev.status === "ok";
  const finalRows = ok ? ev.final.criteria : [];
  const finalScore = ok ? ev.final.score : null;
  const evidenceSet = new Set(finalRows.flatMap((r) => r.evidence_turns || []));
  const appealsByCrit = {};
  for (const a of appeals || []) if (ok && a.evaluation_id === ev._id) (appealsByCrit[a.criterion_id] = appealsByCrit[a.criterion_id] || []).push(a);
  const holdRanges = facts.hold_ranges || [];
  const inHold = (n) => holdRanges.some(([a, b]) => n > a && n < b);
  const reload = async () => {
    if (!isCurrent()) return;
    try { await renderDialog(root, params, null, alive); }
    catch (err) { if (alive()) errorToast(err); }
  };

  root.innerHTML = `
    <div class="page-head">
      ${state.embed ? "" : `<a href="#/dialogs">← Диалоги</a>`}
      <h1 class="mono">${esc(conv.conversation_id)}</h1>
      <span class="pill info">${channelIcon(conv.channel)}</span>
      <span class="pill na">${esc(conv.language || "—")}</span>
      <span class="muted small">${esc(conv.group_id || "")}</span>
      <span class="spacer"></span>
      <select data-votes aria-label="Число голосов"><option value="1">1 голос</option><option value="3">3 голоса</option></select>
      <button class="btn primary" data-act="reeval">${ok ? "↻ Переоценить" : "▶ Оценить"}</button>
    </div>
    <div id="jobArea"></div>
    <div class="dialog-layout">
      <section class="card transcript" id="transcript">
        <div class="transcript-head">
          <div class="row"><b>Транскрипт</b><span class="muted small">${turns.length} реплик · номера [n] — как в результатах</span></div>
          ${isCall ? `<div style="margin-top:8px">${conv.audio_url ? `<audio controls preload="none" src="${esc(conv.audio_url)}" id="player"></audio>
            <div class="small muted">Клик по номеру реплики перематывает запись. Если плеер не играет — <a href="${esc(conv.audio_url)}" target="_blank" rel="noopener">открыть запись</a>.</div>` : ""}
            <div class="small muted">Транскрипт звонка распознан автоматически; удержание и паузы записи отмечены.</div></div>` : ""}
        </div>
        <div class="turns">${turns.map((t) => turnHtml(t, inHold(t.n), evidenceSet.has(t.n), isCall)).join("") || `<div class="empty">В диалоге пока нет реплик.</div>`}</div>
      </section>
      <section class="stack" id="evalCol">
        ${ok ? evaluationHtml(ev, finalScore, finalRows, appealsByCrit) : ev && ev.status === "error"
          ? `<div class="card card-pad"><b style="color:var(--danger)">Оценка не удалась</b><p class="muted">${esc(ev.error || "")}</p></div>`
          : `<div class="card empty">Диалог ещё не оценён. Нажмите «Оценить».</div>`}
        ${factsHtml(facts)}
        ${appealsHtml(appeals || [])}
        ${historyHtml(hist || [])}
      </section>
    </div>`;

  // переход к реплике
  const player = root.querySelector("#player");
  const jump = (n) => {
    if (!isCurrent()) return;
    const el = root.querySelector(`#turn-${n}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 2000);
    const t = turns.find((x) => x.n === n);
    if (player && t && t.rel_sec !== null && t.rel_sec !== undefined) {
      try { player.currentTime = Math.max(0, t.rel_sec - 1); player.play().catch(() => {}); } catch { /* запись недоступна */ }
    }
  };
  root.querySelectorAll("[data-ev]").forEach((b) => b.addEventListener("click", () => jump(Number(b.dataset.ev))));
  root.querySelectorAll(".turn[data-n]").forEach((turn) => {
    turn.addEventListener("click", () => jump(Number(turn.dataset.n)));
    turn.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        jump(Number(turn.dataset.n));
      }
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
    evalButton.disabled = true;
    votesSel.disabled = true;
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
      if (job.status === "failed" || err) toast(`Ошибка оценки: ${job.error || err?.error || "задача не завершена"}`.slice(0, 300), "error");
      else toast(`Оценено · ${fmtMoney(job.cost_usd)} · ${job.elapsed_sec} с`, "ok");
      await reload();
    } catch (err) {
      if (isCurrent()) {
        if (runningJobs.get(cid) === task) runningJobs.delete(cid);
        clearJobQuery();
        errorToast(err);
        evalButton.disabled = false;
        votesSel.disabled = false;
      }
    }
  };
  evalButton.addEventListener("click", () => {
    if (!isCurrent() || runningJobs.has(cid)) return;
    const task = trackJob(cid, () => api.post("/api/evaluate", {
      conversation_ids: [cid], votes: Number(votesSel.value), reuse: false,
    }));
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

function turnHtml(t, hold, isEvidence, isCall) {
  return `<div class="turn ${esc(t.speaker)} ${hold ? "hold" : ""} ${isEvidence ? "evidence" : ""}" id="turn-${t.n}" data-n="${t.n}" role="button" tabindex="0" aria-label="${isCall ? "Перемотать к реплике" : "Выделить реплику"} ${t.n}">
    <div class="n">[${t.n}]</div>
    <div><div class="who">${esc(SPEAKER[t.speaker] || t.speaker)}<span class="t">${esc(t.rel || "")}</span>${hold ? ` <span class="hold-note">удержание: внутренняя консультация</span>` : ""}</div>
    <div class="txt">${esc(t.text)}</div></div></div>`;
}

function evaluationHtml(ev, score, rows, appealsByCrit) {
  const aiScore = ev.ai_score ? ev.ai_score.final_score : null;
  const changed = aiScore !== score.final_score;
  const crit = score.critical_flag && rows.find((r) => r.type === "flag" && r.value === true);
  const usage = ev.usage || {};
  const originals = new Map((ev.criteria || []).map((r) => [r.criterion_id, r]));
  const sectionNames = new Map((score.sections || []).map((s) => [s.id, s.name]));
  return `
    ${crit ? `<div class="crit-banner"><b>⛔ Грубая ошибка.</b> ${esc(crit.rationale)} ${(crit.evidence_turns || []).map((n) => `<button class="ev" data-ev="${n}">[${n}]</button>`).join(" ")}</div>` : ""}
    <div class="card card-pad">
      <div class="score-hero">
        <div class="score-big ${scoreClass(score.final_score)}">${fmtScore(score.final_score)}<small> / 100</small></div>
        <div>
          <div><b>Итоговый балл</b>${changed ? ` <span class="pill info">после апелляции</span>` : ""}</div>
          ${changed ? `<div class="muted small">Оценка ИИ: ${fmtScore(aiScore)} → после решения проверяющего: ${fmtScore(score.final_score)}</div>` : ""}
          <div class="muted small">Набрано ${fmtScore(score.earned)} из ${fmtScore(score.applicable_weight)} баллов применимых критериев; вес N/A перераспределён.</div>
        </div>
      </div>
      <div class="sections">${(score.sections || []).map((s) => `<div class="section-box"><div class="name">${esc(s.name)}</div>
        ${s.score === null ? (s.flags && s.flags.length ? `<div class="val" style="color:var(--danger)">нарушение</div>` : `<div class="val muted">${rows.some((r) => r.section === s.id && r.type !== "flag") ? "N/A" : "нет нарушений"}</div>`)
          : `<div class="val">${fmtScore(s.score)}%</div><div class="mini-bar"><i style="width:${Math.max(0, Math.min(100, s.score))}%"></i></div><div class="muted small">${fmtScore(s.earned)} из ${fmtScore(s.max)}</div>`}
        </div>`).join("")}</div>
    </div>
    <div class="card">${rows.map((r) => criterionHtml(r, appealsByCrit[r.criterion_id] || [], originals.get(r.criterion_id), sectionNames.get(r.section) || r.section)).join("")}</div>
    <div class="card card-pad small muted">
      Модели: ${esc(ev.model || "")} · голосов ${ev.votes ?? "—"} · вызовов ${usage.calls ?? 0} · ${usage.latency_sec ?? "—"} с · стоимость ${fmtMoney(usage.cost_usd)}
      · токены ${usage.prompt_tokens ?? 0}/${usage.completion_tokens ?? 0} · чек-лист v${ev.checklist_version ?? "—"} · ${fmtDate(ev.created_at)}
    </div>`;
}

function criterionHtml(r, appeals, original, sectionName) {
  const open = appeals.find((a) => a.status === "open");
  const decided = appeals.filter((a) => a.status !== "open");
  const isFlag = r.type === "flag";
  const pts = isFlag ? "без баллов" : r.value === null ? `вес ${r.weight} перераспределён` : `${fmtScore(r.points)} из ${fmtScore(r.max_points)}`;
  const evLabel = r.value === null ? "почему неприменим" : (r.value === false || (r.type === "scale" && r.points < r.max_points)) ? "проверенный участок" : (isFlag && r.value) ? "нарушение" : "основание";
  const agree = r.votes && r.votes.length > 1 ? `${Math.round((r.agreement || 0) * r.votes.length)}/${r.votes.length} голосов согласны` : r.votes && r.votes.length === 1 ? "1 голос" : "";
  const voteModels = (r.votes || []).map((v) => `${esc((v.model || "").split(":").pop())}: ${esc(v.verdict)}`).join(" · ");
  return `<div class="criterion">
    <div class="criterion-head">
      ${valuePill(r)}
      <span class="title">${esc(r.name)}</span>
      <span class="muted small">${esc(sectionName)}</span>
      <span class="weight">${isFlag ? "" : `вес ${r.weight} · после N/A ${fmtScore(r.effective_weight)} · ${pts}`}</span>
      ${r.needs_review ? `<span class="pill warn" title="Голоса моделей разошлись или модель не уверена">⚠ проверить</span>` : ""}
      ${r.overridden ? `<span class="pill info">изменено проверяющим</span>` : ""}
      <span class="spacer"></span>
      ${open ? `<span class="pill info">апелляция на рассмотрении</span>` : state.role === "operator" ? `<button class="btn small" data-appeal="${esc(r.criterion_id)}">Оспорить</button>` : ""}
    </div>
    <div class="rationale">${esc(r.rationale)}</div>
    <div class="meta">
      <span>${evLabel}:</span> ${(r.evidence_turns || []).map((n) => `<button class="ev" data-ev="${n}">[${n}]</button>`).join(" ") || "—"}
      ${agree ? `<span>· ${agree}</span>` : ""}
      ${voteModels ? `<span title="Голоса моделей">· ${voteModels}</span>` : ""}
    </div>
    ${r.overridden ? `<div class="override">ИИ: ${esc(original?.value_label ?? labelOf(r.ai_value))} → решение проверяющего: ${esc(r.value_label)}</div>` : ""}
    ${decided.map((a) => `<div class="override">Апелляция: «${esc(a.comment || "")}» → ${a.status === "changed" ? "оценка изменена" : "оценка подтверждена"}${a.decision_reason ? ` · причина: ${esc(a.decision_reason)}` : ""}</div>`).join("")}
  </div>`;
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
  return `<details class="card card-pad facts"><summary>Факты, посчитанные кодом (без модели)</summary><ul class="facts-list">${items.map((x) => `<li>${x}</li>`).join("")}</ul></details>`;
}

function appealsHtml(appeals) {
  if (!appeals.length) return "";
  return `<div class="card card-pad"><h3>Апелляции</h3>${appeals.map((a) => `<div style="margin-top:8px">
    <span class="pill ${a.status === "open" ? "info" : a.status === "changed" ? "warn" : "yes"}">${a.status === "open" ? "на рассмотрении" : a.status === "changed" ? "оценка изменена" : "оценка подтверждена"}</span>
    <b>${esc((a.criterion && a.criterion.name) || a.criterion_id)}</b> · исходно: ${esc(a.original_label)}${a.decision_label ? ` → ${esc(a.decision_label)}` : ""}
    <div class="muted small">«${esc(a.comment || "")}» — ${esc(a.author || "")}${a.decision_reason ? ` · решение: ${esc(a.decision_reason)}` : ""}
    ${a.final_score !== undefined ? ` · итог: ИИ ${fmtScore(a.ai_final_score)} → ${fmtScore(a.final_score)}` : ""}</div></div>`).join("")}</div>`;
}

function historyHtml(hist) {
  if (!hist.length) return "";
  return `<details class="card card-pad"><summary class="muted">История оценок (${hist.length})</summary>
    <table class="grid small" style="margin-top:8px"><tr><th>#</th><th>Итог</th><th>Чек-лист</th><th>Модель</th><th>Когда</th></tr>
    ${hist.map((x) => `<tr><td>${x.id}</td><td>${x.status === "ok" ? fmtScore(x.final_score) : "ошибка"}${x.critical_flag ? " ⛔" : ""}</td><td>v${x.version ?? "—"}</td><td>${esc(x.model || "")}</td><td>${fmtDate(x.created_at)}</td></tr>`).join("")}</table></details>`;
}

function appealModal(ev, row, onDone) {
  const original = (ev.criteria || []).find((r) => r.criterion_id === row.criterion_id) || row;
  const body = h(`<div class="stack">
    <div><b>${esc(row.name)}</b> — исходная оценка ИИ: ${valuePill(original)}</div>
    ${row.overridden ? `<div>Текущая оценка: ${valuePill(row)}</div>` : ""}
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
