// Настройки моделей: черновик отдельно от проверки сохранённой конфигурации.
import { api } from "../api.js";
import { esc, state, toast } from "../ui.js";

let renderSeq = 0;

function validateJudge(judge, index) {
  const provider = String(judge.provider || "").trim().toLowerCase();
  const model = String(judge.model || "").trim();
  if (!/^[a-z][a-z0-9_-]*$/.test(provider)) throw new Error(`Модель ${index + 1}: укажите провайдера латиницей, без пробелов.`);
  if (!model || /[\s,#='"\u0000-\u001f]/.test(model)) throw new Error(`Модель ${index + 1}: укажите название без пробелов, запятых и кавычек.`);
  return { provider, model };
}

export function parseJudgeSpec(value) {
  const source = String(value || "").trim();
  if (!source) throw new Error("Добавьте хотя бы одну модель.");
  return source.split(",").map((item, index) => {
    const delimiter = item.indexOf(":");
    if (delimiter < 1) throw new Error(`Строка ${index + 1}: укажите провайдера и модель через двоеточие.`);
    return validateJudge({ provider: item.slice(0, delimiter), model: item.slice(delimiter + 1) }, index);
  });
}

export function serializeJudges(judges) {
  if (!judges.length) throw new Error("Добавьте хотя бы одну модель.");
  return judges.map((judge, index) => { const j = validateJudge(judge, index); return `${j.provider}:${j.model}`; }).join(",");
}

function demoReviewerLink() {
  const url = new URL(location.href);
  url.searchParams.set("role", "reviewer");
  url.searchParams.delete("conversationId");
  url.hash = "/settings";
  return url.pathname + url.search + url.hash;
}

function safeFailure(action) {
  // Ошибка провайдера может содержать URL или часть ключа; сырой текст не выводим.
  toast(`Не удалось ${action}. Проверьте соединение и сохранённые параметры сервера, затем повторите.`, "error");
}

export async function renderSettings(root, params = [], query = null, alive = () => true) {
  const seq = ++renderSeq;
  const current = () => alive() && seq === renderSeq;
  if (state.role !== "reviewer") {
    root.innerHTML = `<div class="page-head"><h1>Настройки оценки</h1></div>
      <section class="card card-pad stack"><h2>Служебная область проверяющего</h2>
      <p>Здесь настраиваются подключение к данным и модели для оценки. Для чтения диалогов и подачи апелляций менять эти параметры не нужно.</p>
      <p class="muted">Демо-роли разделяют интерфейс и не являются системой авторизации.</p>
      <div><a class="btn" href="${esc(demoReviewerLink())}">Открыть в демо-роли «Проверяющий»</a></div></section>`;
    return;
  }
  let s;
  try { s = await api.get("/api/settings"); }
  catch {
    if (!current()) return;
    root.innerHTML = `<div class="page-head"><h1>Настройки оценки</h1></div><div class="empty" role="alert">Не удалось загрузить настройки. Проверьте соединение с сервером.<p><button class="btn" data-retry>Повторить запрос</button></p></div>`;
    root.querySelector("[data-retry]").addEventListener("click", () => renderSettings(root, params, query, alive));
    return;
  }
  if (!current()) return;
  let recommended = [];
  try { recommended = parseJudgeSpec(s.recommended_judges); } catch { /* сервер может не дать рекомендаций */ }
  let judges = (s.judges || []).map(({ provider, model }) => ({ provider, model }));
  if (!judges.length) { try { judges = parseJudgeSpec(s.llm_judges); } catch { judges = [{ provider: "", model: "" }]; } }
  const suggestions = [...(s.judges || []), ...recommended];
  const providers = [...new Set(suggestions.map((j) => j.provider).filter(Boolean))];
  const savedSpec = String(s.llm_judges || "");
  const keys = s.keys || {};
  root.innerHTML = `
    <div class="page-head"><h1>Настройки оценки</h1><span class="pill ${s.ready ? "info" : "warn"}">${s.ready ? "Сохранённая конфигурация заполнена" : "Настройка подключения не завершена"}</span></div>
    <p class="muted">Сначала сохраните изменения, затем отдельно проверьте подключение. Заполненные поля сами по себе не подтверждают доступность моделей.</p>
    <form class="settings-layout stack" data-settings-form>
      <section class="card card-pad stack settings-section"><h2>Модели для проверки</h2>
        <p class="muted">Укажите провайдера и название модели. Подсказки получены из текущей серверной конфигурации.</p>
        <datalist id="judge-providers">${providers.map((p) => `<option value="${esc(p)}"></option>`).join("")}</datalist>
        <div class="judge-rows stack" data-judge-rows></div>
        <div class="row"><button class="btn" type="button" data-add-judge>Добавить модель</button>${recommended.length ? `<button class="btn" type="button" data-rec>Использовать рекомендации сервера</button>` : ""}</div>
        <div data-judge-errors role="status" aria-live="polite"></div>
        <details class="settings-advanced"><summary>Расширенный ввод</summary>
          <label class="field"><span>Список моделей: провайдер:модель через запятую</span><textarea data-k="llm_judges" rows="3" spellcheck="false"></textarea></label>
          <p class="muted small">Название модели может содержать двоеточие, например в локальном теге. Корректная строка обновляет поля выше. Сохранение выполняется отдельной кнопкой.</p>
        </details>
        <div class="row"><label class="field"><span>Команда</span><input type="text" data-k="team_name" value="${esc(s.team_name)}"></label>
          <label class="field"><span>Проверок по умолчанию</span><select data-k="llm_votes">${[1, 3].map((v) => `<option value="${v}" ${v === Number(s.llm_votes) ? "selected" : ""}>${v === 1 ? "Одна проверка" : "Три проверки"}</option>`).join("")}</select></label></div>
        <p class="muted small">Несколько проверок позволяют сопоставить ответы. Время и расход зависят от моделей и числа проверок; повторные ответы не гарантируют правильность оценки.</p>
      </section>
      <details class="card card-pad stack settings-section"><summary>Подключение к данным и ключи</summary>
        <p class="muted small">Пустые поля сохраняют существующее значение. Ключи автоматически не сохраняются и не подставляются обратно в форму.</p>
        <label class="field"><span>Ссылка API организаторов ${s.org_api_url_set ? "— сохранена" : ""}</span><input type="password" data-k="org_api_url" placeholder="https://…" autocomplete="off" spellcheck="false"></label>
        <label class="field"><span>Ключ Gemini ${keys.GEMINI_API_KEY ? "— сохранён" : ""}</span><input type="password" data-k="gemini_api_key" autocomplete="new-password" placeholder="Оставьте пустым, чтобы не менять"></label>
        <p class="small"><a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">Открыть Google AI Studio для получения ключа</a></p>
        <label class="field"><span>Ключ Cerebras ${keys.CEREBRAS_API_KEY ? "— сохранён" : ""}</span><input type="password" data-k="cerebras_api_key" autocomplete="new-password" placeholder="Оставьте пустым, чтобы не менять"></label>
        <p class="small"><a href="https://cloud.cerebras.ai" target="_blank" rel="noopener noreferrer">Открыть Cerebras для получения ключа</a></p>
      </details>
      <div class="settings-actions stack"><div class="settings-dirty-note muted" data-dirty role="status" aria-live="polite"></div>
        <div class="row"><button class="btn primary" type="submit" data-save>Сохранить настройки</button><button class="btn" type="button" data-ping>Проверить сохранённые модели</button></div>
        <p class="muted small">Проверка отправляет запросы сохранённым моделям и может расходовать квоту. Несохранённый черновик не участвует в проверке.</p>
        <div data-ping-out aria-live="polite"></div></div>
    </form>
    <details class="card card-pad settings-section" style="margin-top:20px"><summary>Сохранённая конфигурация и технические сведения</summary>
      <div class="settings-table-wrap" tabindex="0" role="region" aria-label="Сохранённые модели, таблица с горизонтальной прокруткой">
        <table class="grid small"><thead><tr><th>Провайдер / модель</th><th>Расчёт тарифа</th><th>Доступ</th><th>Настроенный темп</th></tr></thead><tbody>
        ${(s.judges || []).map((j) => `<tr><td>${esc(j.provider)} / ${esc(j.model)}</td><td>${j.local ? "Локальная модель" : j.free ? "В конфигурации: $0" : "По настроенному тарифу"}</td><td>${j.local ? "Локальный доступ" : j.has_key ? "Ключ задан" : "Ключ не задан"}</td><td>${j.rpm ? `${esc(j.rpm)} запросов/мин` : "Не задан"}</td></tr>`).join("")}</tbody></table></div>
      <p class="muted small">Режим результата: ${esc(s.mode)}. Режим запросов: ${esc(s.llm_mode)}. Стоимость по конфигурации не является подтверждённым счётом провайдера. Фактические лимиты и остаток квоты проверяйте у провайдера.</p>
    </details>`;
  const form = root.querySelector("[data-settings-form]");
  const rows = root.querySelector("[data-judge-rows]");
  const raw = root.querySelector('[data-k="llm_judges"]');
  const errors = root.querySelector("[data-judge-errors]");
  const saveButton = root.querySelector("[data-save]");
  const pingButton = root.querySelector("[data-ping]");
  const dirtyNote = root.querySelector("[data-dirty]");
  let saving = false, pinging = false, validationError = "";
  const hasDraft = () => raw.value.trim() !== savedSpec.trim() || Number(root.querySelector('[data-k="llm_votes"]').value) !== Number(s.llm_votes)
    || root.querySelector('[data-k="team_name"]').value !== String(s.team_name || "") || [...root.querySelectorAll('input[type="password"]')].some((el) => !!el.value);
  const updateState = () => {
    // Пока сохраняется снимок формы, нельзя набрать новый черновик, который затрёт ответ.
    // Проверка подключения не блокирует поля: она работает с сохранёнными моделями.
    form.querySelectorAll("input, select, textarea, button").forEach((el) => { el.disabled = saving; });
    form.setAttribute("aria-busy", String(saving));
    saveButton.disabled = saving || pinging || !!validationError;
    pingButton.disabled = saving || pinging;
    errors.innerHTML = validationError ? `<p class="errors">${esc(validationError)}</p>` : "";
    dirtyNote.textContent = saving ? "Сохраняем настройки… Поля временно недоступны." : hasDraft() ? "Есть несохранённые изменения. Проверка подключения использует прежнюю сохранённую конфигурацию." : "Показана сохранённая конфигурация.";
  };
  const fromRows = () => {
    raw.value = judges.map((j) => `${j.provider.trim()}:${j.model.trim()}`).join(",");
    try { serializeJudges(judges); validationError = ""; } catch (e) { validationError = e.message; }
    updateState();
  };
  const paintRows = () => {
    rows.innerHTML = judges.map((j, i) => `<div class="judge-row" data-judge="${i}">
      <label class="field"><span>Провайдер ${i + 1}</span><input type="text" data-provider value="${esc(j.provider)}" list="judge-providers" autocomplete="off" spellcheck="false"></label>
      <label class="field"><span>Модель ${i + 1}</span><input type="text" data-model value="${esc(j.model)}" list="judge-models-${i}" autocomplete="off" spellcheck="false"></label>
      <datalist id="judge-models-${i}">${suggestions.filter((item) => item.provider === j.provider).map((item) => `<option value="${esc(item.model)}"></option>`).join("")}</datalist>
      <button class="btn" type="button" data-remove-judge="${i}" aria-label="Удалить модель ${i + 1}${j.model ? ` ${esc(j.model)}` : ""}">Удалить</button></div>`).join("");
    rows.querySelectorAll("[data-judge]").forEach((row) => {
      const i = Number(row.dataset.judge);
      row.querySelector("[data-provider]").addEventListener("input", (e) => {
        judges[i].provider = e.target.value;
        row.querySelector("datalist").innerHTML = suggestions.filter((item) => item.provider === e.target.value.trim()).map((item) => `<option value="${esc(item.model)}"></option>`).join("");
        fromRows();
      });
      row.querySelector("[data-model]").addEventListener("input", (e) => { judges[i].model = e.target.value; fromRows(); });
      row.querySelector("[data-remove-judge]").addEventListener("click", () => {
        judges.splice(i, 1); paintRows(); fromRows();
        (rows.querySelector(`[data-judge="${Math.min(i, judges.length - 1)}"] input`) || root.querySelector("[data-add-judge]")).focus();
      });
    });
  };
  paintRows(); fromRows();
  raw.addEventListener("input", () => {
    try { judges = parseJudgeSpec(raw.value); validationError = ""; paintRows(); } catch (e) { validationError = e.message; }
    updateState();
  });
  root.querySelector("[data-add-judge]").addEventListener("click", () => { judges.push({ provider: "", model: "" }); paintRows(); fromRows(); rows.lastElementChild.querySelector("input").focus(); });
  root.querySelector("[data-rec]")?.addEventListener("click", () => { judges = recommended.map((j) => ({ ...j })); paintRows(); fromRows(); toast("Рекомендации добавлены в черновик. Сохраните настройки, чтобы применить их."); });
  form.querySelectorAll("[data-k]").forEach((el) => el.addEventListener("input", updateState));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (saving || pinging || !current()) return;
    let normalized;
    try { normalized = serializeJudges(parseJudgeSpec(raw.value)); } catch (error) { validationError = error.message; updateState(); return; }
    const body = {};
    form.querySelectorAll("[data-k]").forEach((el) => { body[el.dataset.k] = el.value; });
    body.llm_judges = normalized;
    if (body.org_api_url && !/^https?:\/\//i.test(body.org_api_url.trim())) { toast("Ссылка API должна начинаться с https:// или http://.", "error"); return; }
    if (Object.values(body).some((value) => /[\r\n]/.test(value))) { toast("Поля настроек не должны содержать переносы строк.", "error"); return; }
    saving = true; updateState();
    try {
      await api.post("/api/settings", body);
      form.querySelectorAll('input[type="password"]').forEach((el) => { el.value = ""; });
      if (!current()) return;
      toast("Настройки сохранены. Проверку подключения можно запустить отдельно.", "ok");
      await renderSettings(root, params, query, alive);
    } catch { if (current()) safeFailure("сохранить настройки"); }
    finally { saving = false; if (current()) updateState(); }
  });
  pingButton.addEventListener("click", async () => {
    if (saving || pinging || !current()) return;
    const out = root.querySelector("[data-ping-out]");
    const hadDraft = hasDraft();
    const names = (s.judges || []).map((j) => j.label || `${j.provider}:${j.model}`).join(", ");
    out.innerHTML = `<p class="muted">Проверяем сохранённые модели: ${esc(names || "модели не указаны")}. ${hadDraft ? "Черновик не отправляется." : ""}</p>`;
    pinging = true; updateState();
    try {
      const results = await api.post("/api/settings/ping");
      if (!current()) return;
      out.innerHTML = `<h2>Проверка сохранённых моделей</h2><p class="muted small">${esc(names)}${hadDraft ? ". Несохранённый черновик не проверялся." : ""}</p>`
        + (results.length ? results.map((r) => `<div class="${r.ok ? "okbox" : "errors"}"><b>${esc(r.label)}</b>: ${r.ok ? `ответ получен${Number.isFinite(r.latency_sec) ? ` за ${esc(r.latency_sec)} с` : ""}` : "подключение не удалось. Проверьте ключ, доступность модели и лимиты провайдера."}</div>`).join("") : `<p class="errors">Сервер не вернул результатов проверки. Доступность моделей не подтверждена.</p>`);
    } catch { if (current()) { out.innerHTML = `<p class="errors">Не удалось завершить проверку сохранённых моделей. Повторите запрос.</p>`; safeFailure("проверить подключение"); } }
    finally { pinging = false; if (current()) updateState(); }
  });
}

export { renderSettings as render };
