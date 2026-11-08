// Экран «Апелляции»: очередь проверяющего, второе мнение ИИ, решение и пересчёт итога.
import { api } from "../api.js";
import { SPEAKER, errorToast, esc, fmtDate, fmtScore, state, toast, valueOptions } from "../ui.js";

let tab = "open";
let renderSeq = 0;
let pollTimer = null;
const drafts = new Map();
const submitting = new Set();

function rememberForms(root) {
  root.querySelectorAll("form[data-decide]").forEach((form) => {
    const fields = form.elements;
    drafts.set(form.dataset.decide, {
      decision: fields.namedItem("decision").value,
      value: fields.namedItem("value").value,
      reason: fields.namedItem("reason").value,
      reviewer: fields.namedItem("reviewer").value,
    });
  });
}

export async function renderAppeals(root, params = [], query = null, alive = () => true) {
  rememberForms(root);
  clearTimeout(pollTimer);
  const seq = ++renderSeq;
  const current = () => alive() && seq === renderSeq;
  const selectedTab = tab;
  const list = (await api.get(`/api/appeals${selectedTab === "open" ? "?status=open" : ""}`))
    .filter((a) => selectedTab === "open" || a.status !== "open");
  if (!current()) return;
  root.innerHTML = `
    <div class="page-head">
      <h1>Апелляции</h1>
      <div class="tabs"><button data-tab="open" class="${selectedTab === "open" ? "active" : ""}">Открытые</button><button data-tab="reviewed" class="${selectedTab === "reviewed" ? "active" : ""}">Рассмотренные</button></div>
      <span class="spacer"></span>
      ${state.role !== "reviewer" ? `<span class="pill warn">Решения принимает роль «Проверяющий» — переключите в шапке</span>` : `<span class="pill yes">Вы — проверяющий</span>`}
    </div>
    <div class="card">${list.length ? list.map(appealHtml).join("") : `<div class="empty">${selectedTab === "open" ? "Открытых апелляций нет. Оператор может оспорить критерий в карточке диалога." : "Рассмотренных апелляций пока нет."}</div>`}</div>`;

  const refresh = async () => {
    if (!current()) return;
    try { await renderAppeals(root, params, query, alive); }
    catch (error) { if (alive()) errorToast(error); }
  };
  root.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    tab = b.dataset.tab;
    refresh();
  }));

  // Обновляем только второе мнение: поля решения и фокус остаются на месте.
  const pending = new Set(list.filter((a) => a.status === "open" && !a.ai_review).map((a) => a.id));
  const bindReview = (scope) => scope.querySelectorAll("[data-reai]").forEach((b) => b.addEventListener("click", async () => {
    if (b.disabled) return;
    b.disabled = true;
    try {
      const appeal = await api.post(`/api/appeals/${b.dataset.reai}/ai-review`);
      if (!current()) return;
      const box = b.closest("[data-ai-review]");
      box.innerHTML = aiReviewHtml(appeal);
      bindReview(box);
      if (!appeal.ai_review) { pending.add(appeal.id); schedulePoll(); }
    } catch (error) { if (current()) errorToast(error); }
    finally { b.disabled = false; }
  }));
  const schedulePoll = () => {
    clearTimeout(pollTimer);
    if (!current() || !pending.size) return;
    pollTimer = setTimeout(async () => {
      if (!current()) return;
      try {
        const updates = await api.get("/api/appeals?status=open");
        if (!current()) return;
        for (const id of [...pending]) {
          const appeal = updates.find((a) => a.id === id);
          if (!appeal || appeal.ai_review) pending.delete(id);
          const box = root.querySelector(`[data-ai-review="${id}"]`);
          if (box && appeal && appeal.ai_review) {
            box.innerHTML = aiReviewHtml(appeal);
            bindReview(box);
          }
        }
        schedulePoll();
      } catch (error) { if (current()) errorToast(error); }
    }, 3000);
  };
  bindReview(root);
  schedulePoll();

  root.querySelectorAll("form[data-decide]").forEach((form) => {
    const id = form.dataset.decide;
    const fields = form.elements;
    const sel = fields.namedItem("value");
    const saved = drafts.get(id);
    if (saved) for (const [key, value] of Object.entries(saved)) fields.namedItem(key).value = value;
    sel.disabled = fields.namedItem("decision").value !== "changed";
    const button = form.querySelector("button[type=submit]");
    button.disabled = submitting.has(id);
    form.querySelectorAll("input[name=decision]").forEach((r) => r.addEventListener("change", () => {
      sel.disabled = fields.namedItem("decision").value !== "changed";
    }));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (submitting.has(id)) return;
      const decision = fields.namedItem("decision").value;
      const reason = fields.namedItem("reason").value.trim();
      if (!reason) return toast("Укажите причину решения", "error");
      submitting.add(id);
      button.disabled = true;
      try {
        const value = decision === "changed" ? JSON.parse(sel.value) : undefined;
        const res = await api.post(`/api/appeals/${id}/decision`, { decision, value, reason, reviewer: fields.namedItem("reviewer").value.trim() || "Проверяющий" });
        drafts.delete(id);
        if (!current()) return;
        const a = res.appeal;
        toast(`Решение сохранено. Итог: ИИ ${fmtScore(a.ai_final_score)} → ${fmtScore(a.final_score)}`, "ok");
        tab = "reviewed";
        await refresh();
        drafts.delete(id);
      } catch (error) { if (current()) errorToast(error); }
      finally { submitting.delete(id); button.disabled = false; }
    });
  });
}

function aiReviewHtml(a) {
  const ai = a.ai_review;
  return `${ai ? (ai.error ? `<span class="muted small">Второе мнение ИИ недоступно: ${esc(ai.error)}</span>` :
    `<div class="row"><b>Второе мнение ИИ:</b> ${ai.recommendation === "change" ? `<span class="pill warn">изменить на «${esc(ai.suggested_label)}»</span>` : `<span class="pill yes">оставить оценку</span>`}</div>
    <div style="margin-top:4px">${esc(ai.rationale || "")}</div><div class="muted small">${esc(ai.model || "")}</div>`)
    : `<span class="muted small">Второе мнение ИИ готовится…</span>`}
    ${a.status === "open" ? ` <button class="btn small ghost" data-reai="${a.id}">↻ Запросить заново</button>` : ""}`;
}

function appealHtml(a) {
  const c = a.criterion || {};
  const orig = a.original || {};
  const evidence = new Set(orig.evidence_turns || []);
  const ai = a.ai_review;
  const opts = valueOptions(c);
  const statusPill = a.status === "open" ? `<span class="pill info">на рассмотрении</span>` : a.status === "changed" ? `<span class="pill warn">оценка изменена</span>` : `<span class="pill yes">оценка подтверждена</span>`;
  return `<div class="appeal">
    <div class="row">
      ${statusPill}
      <b>${esc(c.name || a.criterion_id)}</b>
      <span class="muted">в диалоге</span> <a class="mono" href="#/dialog/${encodeURIComponent(a.conversation_id)}">${esc(a.conversation_id)}</a>
      <span class="spacer"></span><span class="muted small">${esc(a.author || "")} · ${fmtDate(a.created_at)}</span>
    </div>
    <div class="appeal-grid">
      <div class="stack">
        <div><span class="muted small">Оценка ИИ:</span> <b>${esc(a.original_label)}</b>
          ${a.final_score !== undefined ? `<span class="muted small"> · итог диалога: ИИ ${fmtScore(a.ai_final_score)}${a.status !== "open" ? ` → ${fmtScore(a.final_score)}` : ""}</span>` : ""}</div>
        <div class="quote"><span class="muted small">Обоснование ИИ:</span><br>${esc(orig.rationale || "")}</div>
        <div class="quote"><span class="muted small">Комментарий оператора:</span><br>${esc(a.comment || "—")}</div>
        <div class="ai-review" data-ai-review="${a.id}">${aiReviewHtml(a)}</div>
      </div>
      <div class="stack">
        <div class="fragment"><div class="muted small">Фрагмент диалога (подсвечены реплики-основания):</div>
          ${(a.fragment || []).map((t) => `<div class="turn ${esc(t.speaker)} ${evidence.has(t.n) ? "flash" : ""}"><div class="n">[${t.n}]</div><div><div class="who">${esc(SPEAKER[t.speaker] || t.speaker)}<span class="t">${esc(t.rel || "")}</span></div><div class="txt">${esc(t.text)}</div></div></div>`).join("") || `<div class="muted small">—</div>`}
        </div>
        ${a.status === "open" ? (state.role === "reviewer" ? `
          <form class="decision stack" data-decide="${a.id}">
            <div class="row">
              <label class="check"><input type="radio" name="decision" value="confirmed" checked> Подтвердить оценку</label>
              <label class="check"><input type="radio" name="decision" value="changed"> Изменить на</label>
              <select name="value" aria-label="Новое значение критерия" disabled>${opts.map((o) => `<option value='${esc(JSON.stringify(o.value))}' ${ai && ai.suggested_value === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>
            </div>
            <input type="text" name="reason" required aria-label="Причина решения" placeholder="Причина решения (обязательно)">
            <div class="row"><input type="text" name="reviewer" aria-label="Проверяющий" value="Проверяющий" style="width:160px"><span class="spacer"></span><button class="btn primary" type="submit">Сохранить решение</button></div>
          </form>` : `<div class="muted small">Решение принимает проверяющий.</div>`)
        : `<div class="decision"><b>Решение:</b> ${a.status === "changed" ? `«${esc(a.original_label)}» → «${esc(a.decision_label)}»` : `«${esc(a.original_label)}» — оценка подтверждена`}<br>
            <span class="muted small">Причина: ${esc(a.decision_reason || "")} · ${esc(a.reviewer || "")} · ${fmtDate(a.decided_at)}</span></div>`}
      </div>
    </div>
  </div>`;
}

export { renderAppeals as render };
