// Единственное место, где интерфейс ходит на сервер. База берётся из window.CQA_API_BASE (для встраивания в D365).
const base = () => (window.CQA_API_BASE || "").replace(/\/$/, "");

export function apiErrorMessage(data, status) {
  const detail = data?.detail;
  let msg = typeof detail === "string" ? detail
    : Array.isArray(detail?.errors) && detail.errors.length ? detail.errors.join("\n")
    : typeof detail?.message === "string" ? detail.message
    : Array.isArray(detail) ? detail.map((item) => item.msg || "Неверные параметры запроса").join("\n")
    : `Ошибка ${status}`;
  if (status >= 500) msg = "Сервер не смог выполнить запрос. Попробуйте ещё раз.";
  return msg.replace(/(api[_-]?key|authorization|token|secret)([\s"':=]+)[^\s,"'}]+/gi, "$1: [скрыто]")
    .replace(/\b(?:AIza[\w-]{20,}|sk-[\w-]{16,})\b/g, "[ключ скрыт]");
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(base() + path, opts);
  } catch (e) {
    throw new Error("Сервер недоступен. Запущен ли start.sh?");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const error = new Error(apiErrorMessage(data, res.status));
    error.status = res.status;
    error.jobId = data?.detail?.job_id;
    throw error;
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body === undefined ? {} : body),
  url: (path) => base() + path,
};

export function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    p.set(k, v === true ? "true" : String(v));
  }
  const s = p.toString();
  return s ? "?" + s : "";
}
