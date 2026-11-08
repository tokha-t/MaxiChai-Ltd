// Экран «Апелляции»: очередь проверяющего, второе мнение ИИ, решение и пересчёт итога.
import { api } from "../api.js";
import { SPEAKER, errorToast, esc, fmtDate, fmtScore, state, toast, valueOptions } from "../ui.js";

let tab = "open";

export async function renderAppeals(root, params, query, alive) {
  const list = await api.get(`/api/appeals${tab === "open" ? "?status=open" : ""}`);
  if (!alive()) return;
  root.innerHTML = `
    <div class="page-head">
      <h1>Апелляции</h1>
      <div class="tabs"><button data-tab="open" class="${tab === "open" ? "active" : ""}">Открытые</button><button data-tab="all" class="${tab === "all" ? "active" : ""}">Все</button></div>
      <span class="spacer"></span>
      ${state.role !== "reviewer" ? `<span class="pill warn">Решения принимает роль «Проверяющий» — переключите в шапке</span>` : `<span class="pill yes">Вы — проверяющий</span>`}
    </div>
    <div class="card">${list.length ? list.map(appealHtml).join("") : `<div class="empty">${tab === "open" ? "Открытых апелляций нет. Оператор может оспорить критерий в карточке диалога." : "Апелляций пока нет."}</div>`}</div>`;

  root.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; renderAppeals(root, params, query, alive); }));

  // второе мнение ИИ готовится в фоне — подождём и перерисуем
  if (list.some((a) => a.status === "open" && !a.ai_review)) {
    setTimeout(() => { if (alive() && location.hash.startsWith("#/appeals")) renderAppeals(root, params, query, alive); }, 3000);
  }

  root.querySelectorAll("[data-reai]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api.post(`/api/appeals/${b.dataset.reai}/ai-review`); renderAppeals(root, params, query, alive); } catch (e) { errorToast(e); b.disabled = false; }
  }));

  root.querySelectorAll("form[data-decide]").forEach((form) => {
    const id = form.dataset.decide;
    const radios = form.querySelectorAll("input[name=decision]");
    const sel = form.querySelector("select[name=value]");
    radios.forEach((r) => r.addEventListener("change", () => { sel.disabled = form.decision.value !== "changed"; }));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const decision = form.decision.value;
      const reason = form.reason.value.trim();
      if (!reason) return toast("Укажите причину решения", "error");
      const value = decision === "changed" ? JSON.parse(sel.value) : undefined;
      try {
        const res = await api.post(`/api/appeals/${id}/decision`, { decision, value, reason, reviewer: form.reviewer.value.trim() || "Проверяющий" });
        const a = res.appeal;
        toast(`Решение сохранено. Итог: ИИ ${fmtScore(a.ai_final_score)} → ${fmtScore(a.final_score)}`, "ok");
        tab = "all";
        renderAppeals(root, params, query, alive);
      } catch (err) { errorToast(err); }
    });
  });
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
        <div class="ai-review">${ai ? (ai.error ? `<span class="muted small">Второе мнение ИИ недоступно: ${esc(ai.error)}</span>` :
          `<div class="row"><b>Второе мнение ИИ:</b> ${ai.recommendation === "change" ? `<span class="pill warn">изменить на «${esc(ai.suggested_label)}»</span>` : `<span class="pill yes">оставить оценку</span>`}</div>
           <div style="margin-top:4px">${esc(ai.rationale || "")}</div><div class="muted small">${esc(ai.model || "")}</div>`)
          : `<span class="muted small">Второе мнение ИИ готовится…</span>`}
          ${a.status === "open" ? ` <button class="btn small ghost" data-reai="${a.id}">↻ запросить заново</button>` : ""}</div>
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
              <select name="value" disabled>${opts.map((o) => `<option value='${esc(JSON.stringify(o.value))}' ${ai && ai.suggested_value === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>
            </div>
            <input type="text" name="reason" placeholder="Причина решения (обязательно)">
            <div class="row"><input type="text" name="reviewer" value="Проверяющий" style="width:160px"><span class="spacer"></span><button class="btn primary" type="submit">Сохранить решение</button></div>
          </form>` : `<div class="muted small">Решение принимает проверяющий.</div>`)
        : `<div class="decision"><b>Решение:</b> ${a.status === "changed" ? `оценка изменена на «${esc(a.decision_label)}»` : "оценка подтверждена"}<br>
            <span class="muted small">Причина: ${esc(a.decision_reason || "")} · ${esc(a.reviewer || "")} · ${fmtDate(a.decided_at)}</span></div>`}
      </div>
    </div>
  </div>`;
}
