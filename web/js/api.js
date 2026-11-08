// Единственное место, где интерфейс ходит на сервер. База берётся из window.CQA_API_BASE (для встраивания в D365).
const base = () => (window.CQA_API_BASE || "").replace(/\/$/, "");

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
    let msg = `Ошибка ${res.status}`;
    if (data && data.detail) msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    throw new Error(msg);
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
