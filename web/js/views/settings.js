// Настройки: ссылка API организаторов, ключи бесплатных моделей, состав судей. Ключи хранятся в .env на этом компьютере.
import { api } from "../api.js";
import { errorToast, esc, toast } from "../ui.js";

export async function renderSettings(root, params = [], query = null, alive = () => true) {
  const s = await api.get("/api/settings");
  if (!alive()) return;
  root.innerHTML = `
    <div class="page-head"><h1>Настройки</h1>
      ${s.ready ? `<span class="pill yes">Готово к оценке</span>` : `<span class="pill warn">Нужно заполнить ссылку и ключ</span>`}</div>
    <div class="card card-pad stack" style="max-width:860px">
      <label class="field"><span>Ссылка API организаторов (из гайда) ${s.org_api_url_set ? "— <b style='color:var(--accent-dark)'>задана</b>, можно не вводить" : ""}</span>
        <input type="text" data-k="org_api_url" placeholder="https://...powerplatform.com/powerautomate/...&sig=..."></label>
      <label class="field"><span>Ключ Gemini — бесплатно на aistudio.google.com/apikey ${s.keys.GEMINI_API_KEY ? "— <b style='color:var(--accent-dark)'>задан</b>" : ""}</span>
        <input type="password" data-k="gemini_api_key" placeholder="${s.keys.GEMINI_API_KEY ? "•••••• (оставьте пустым, чтобы не менять)" : "вставьте ключ"}" autocomplete="off"></label>
      <label class="field"><span>Ключ Cerebras — необязательно, cloud.cerebras.ai ${s.keys.CEREBRAS_API_KEY ? "— <b style='color:var(--accent-dark)'>задан</b>" : ""}</span>
        <input type="password" data-k="cerebras_api_key" placeholder="${s.keys.CEREBRAS_API_KEY ? "•••••• (оставьте пустым, чтобы не менять)" : "можно пропустить"}" autocomplete="off"></label>
      <label class="field"><span>Модели-судьи (провайдер:модель через запятую; голоса раздаются по кругу)</span>
        <div class="row"><input type="text" data-k="llm_judges" value="${esc(/gemini-3\.8-flash/.test(s.llm_judges) ? s.recommended_judges : s.llm_judges)}" style="flex:1">
        <button class="btn small" data-rec>Рекомендуемые</button></div></label>
      <div class="row">
        <label class="field"><span>Команда</span><input type="text" data-k="team_name" value="${esc(s.team_name)}"></label>
        <label class="field"><span>Голосов по умолчанию</span><select data-k="llm_votes">${[1, 3].map((v) => `<option ${v === s.llm_votes ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      </div>
      <div class="row"><button class="btn primary" data-save>Сохранить</button><button class="btn" data-ping>Проверить модели</button>
        <span class="muted small">Проверка тратит по одному запросу на модель.</span></div>
      <div data-ping-out></div>
    </div>
    <div class="card card-pad" style="max-width:860px;margin-top:16px">
      <h3>Судьи сейчас</h3>
      <table class="grid small" style="margin-top:6px"><tr><th>Модель</th><th>Тариф</th><th>Ключ</th><th>Темп</th></tr>
        ${s.judges.map((j) => `<tr><td class="mono">${esc(j.label)}</td><td>${j.local ? "локальная" : j.free ? "бесплатный" : "платный"}</td>
          <td>${j.has_key ? `<span class="pill yes">есть</span>` : `<span class="pill no">нет — судья будет пропущен</span>`}</td><td>${j.rpm ? j.rpm + " запр/мин" : "без ограничения"}</td></tr>`).join("")}</table>
      <p class="muted small">Режим в results.json: <b>${esc(s.mode)}</b>. Все критерии оцениваются одним вызовом (${esc(s.llm_mode)}), поэтому одна оценка = число голосов вызовов.
      Дневные квоты Gemini сбрасываются в 13:00 по Алматы.</p>
    </div>`;

  root.querySelector("[data-rec]").addEventListener("click", () => { root.querySelector('[data-k="llm_judges"]').value = s.recommended_judges; });
  root.querySelector("[data-save]").addEventListener("click", async (e) => {
    const body = {};
    root.querySelectorAll("[data-k]").forEach((el) => { body[el.dataset.k] = el.value; });
    e.target.disabled = true;
    try {
      const res = await api.post("/api/settings", body);
      toast(res.ready ? "Сохранено. Можно оценивать." : "Сохранено. Не хватает ссылки или ключа.", res.ready ? "ok" : "");
      renderSettings(root, params, query, alive);
    } catch (err) { errorToast(err); e.target.disabled = false; }
  });
  root.querySelector("[data-ping]").addEventListener("click", async (e) => {
    const out = root.querySelector("[data-ping-out]");
    out.innerHTML = `<div class="muted">Проверяю…</div>`;
    e.target.disabled = true;
    try {
      const res = await api.post("/api/settings/ping");
      out.innerHTML = res.map((r) => `<div class="${r.ok ? "okbox" : "errors"}" style="margin-top:6px"><b class="mono">${esc(r.label)}</b>: ${r.ok ? `отвечает за ${r.latency_sec} с` : esc(r.error)}</div>`).join("");
    } catch (err) { errorToast(err); } finally { e.target.disabled = false; }
  });
}

export { renderSettings as render };
