"""HTTP API для интерфейса. Запуск: uvicorn server.app:app --reload --port 8000

Интерфейс (папка web/) отдаётся с того же сервера, поэтому CORS к API организаторов не нужен:
браузер ходит только сюда, а сервер — к организаторам.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cqa.checklist import OFFICIAL_IDS, ensure_defaults, load_default, scale_steps, validate_checklist, value_label
from cqa.config import ROOT, settings
from cqa.dataset import fetch_group, parse_plain_text, records_from_upload, save_cache
from cqa.evaluator import evaluate_many, rescore, review_appeal
from cqa.export import build_results, has_mock_evaluations, write_results
from cqa.validate import validate_file
from cqa.facts import build_turns, compute_facts
from cqa.llm import make_client
from cqa.store import Store

app = FastAPI(title="CQA — Conversation Quality Analytics", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

store = Store()
client = make_client()


def _import_cached_groups() -> None:
    """Если набор уже скачан в data/<GROUP>.json, а в базе его нет — загрузить при старте."""
    known = {g["group_id"] for g in store.groups()}
    for path in sorted(settings.data_dir.glob("*.json")):
        group = path.stem
        if group in known:
            continue
        try:
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            records = data.get("records") if isinstance(data, dict) else None
            if records:
                store.upsert_conversations(records, group)
        except Exception:  # noqa: BLE001 — битый кэш не должен ронять сервер
            continue


_import_cached_groups()
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _err(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


def _checklist_for(ev: Dict[str, Any]) -> Dict[str, Any]:
    cl = store.get_checklist(ev.get("_checklist_id")) if ev.get("_checklist_id") else None
    return cl or ensure_defaults(load_default())


def final_view(ev: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Оценка ИИ + решения по апелляциям → финальный результат (исходная оценка сохраняется)."""
    if not ev:
        return None
    out = dict(ev)
    if ev.get("status") != "ok":
        out["final"] = None
        return out
    overrides = store.overrides_for_evaluation(ev["_id"])
    final = rescore(ev, _checklist_for(ev), overrides) if overrides else ev
    out["ai_score"] = ev["score"]
    out["final"] = {"score": final["score"], "criteria": final["criteria"], "overrides": overrides}
    return out


# ---------------- служебное ----------------
def _judges_info():
    return [{"label": j.label, "provider": j.provider, "model": j.model, "free": j.free, "local": j.local,
             "has_key": bool(j.api_key) or j.local, "rpm": j.rpm} for j in settings.judges]


@app.get("/api/health")
def health():
    return {"ok": True, "model": client.model, "judges": _judges_info(), "llm_mode": settings.llm_mode,
            "votes": settings.llm_votes, "db": str(settings.db_path), "org_api": bool(settings.org_api_url)}


@app.get("/api/config")
def config():
    return {"model": client.model, "judges": _judges_info(), "mode": settings.model_mode, "llm_mode": settings.llm_mode,
            "team_name": settings.team_name, "votes": settings.llm_votes,
            "free": all(j.free for j in settings.judges)}


# ---------------- настройки (ключи вводятся в интерфейсе, хранятся в .env на этом компьютере) ----------------
ENV_PATH = ROOT / ".env"
SETTING_FIELDS = {"org_api_url": "ORG_API_URL", "gemini_api_key": "GEMINI_API_KEY", "cerebras_api_key": "CEREBRAS_API_KEY",
                  "llm_judges": "LLM_JUDGES", "team_name": "TEAM_NAME", "llm_votes": "LLM_VOTES"}


def _recommended_judges() -> str:
    try:
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line.startswith("LLM_JUDGES="):
                return line.split("=", 1)[1].split("#")[0].strip()
    except OSError:
        pass
    return "gemini:gemini-3.6-flash,gemini:gemini-3.5-flash-lite,cerebras:gpt-oss-120b"


def _write_env(updates: Dict[str, str]) -> None:
    import os
    base = ENV_PATH if ENV_PATH.exists() else ROOT / ".env.example"
    lines = base.read_text(encoding="utf-8").splitlines() if base.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in updates:
                lines[i] = f"{k}={updates[k]}"
                seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    for k, v in updates.items():
        os.environ[k] = v


def _reload_settings() -> None:
    global client
    from cqa.config import load_settings
    fresh = load_settings()
    settings.__dict__.update(fresh.__dict__)
    client = make_client(settings)


def _settings_view() -> Dict[str, Any]:
    url_ok = bool(settings.org_api_url) and "..." not in (settings.org_api_url or "")
    judges = _judges_info()
    return {
        "org_api_url_set": url_ok,
        "keys": {name: any(j.api_key for j in settings.judges if j.provider == prov)
                 for name, prov in (("GEMINI_API_KEY", "gemini"), ("CEREBRAS_API_KEY", "cerebras"))},
        "llm_judges": ",".join(j["label"] for j in judges),
        "recommended_judges": _recommended_judges(),
        "llm_votes": settings.llm_votes,
        "llm_mode": settings.llm_mode,
        "team_name": settings.team_name,
        "mode": settings.model_mode,
        "judges": judges,
        "ready": url_ok and any(j["has_key"] for j in judges),
    }


@app.get("/api/settings")
def settings_get():
    return _settings_view()


@app.post("/api/settings")
def settings_save(body: Dict[str, Any] = Body(...)):
    """Пустые поля не трогаем: ключ не стирается, если его не ввели заново."""
    updates = {}
    for field, env_key in SETTING_FIELDS.items():
        value = body.get(field)
        if value is None:
            continue
        value = str(value).strip().strip('"').strip("'")
        if not value:
            continue
        if field == "org_api_url" and not value.startswith("http"):
            _err(400, "Ссылка API должна начинаться с https://")
        updates[env_key] = value
    if updates.get("GEMINI_API_KEY") or updates.get("CEREBRAS_API_KEY"):
        updates.setdefault("GEMINI_RPM", "5")
        updates.setdefault("CEREBRAS_RPM", "5")
    if updates:
        _write_env(updates)
        _reload_settings()
    return _settings_view()


@app.post("/api/settings/ping")
def settings_ping():
    msgs = [{"role": "system", "content": "Отвечай строго JSON-объектом."},
            {"role": "user", "content": 'Верни {"ok": true, "answer": "<одно слово по-казахски: спасибо>"}'}]
    out = []
    for judge in client.judges:
        started = time.time()
        try:
            r = judge.complete_json(msgs, None)
            out.append({"label": judge.model, "ok": True, "latency_sec": round(time.time() - started, 1), "answer": r.data})
        except Exception as exc:  # noqa: BLE001
            out.append({"label": judge.model, "ok": False, "error": str(exc)[:300]})
    return out


# ---------------- данные ----------------
@app.post("/api/datasets/fetch")
def dataset_fetch(body: Dict[str, Any] = Body(...)):
    group = (body.get("group_id") or "").strip()
    if not group:
        _err(400, "Укажите group_id")
    try:
        data = fetch_group(group)
    except Exception as exc:  # noqa: BLE001
        _err(502, f"Не удалось получить набор: {exc}")
    save_cache(group, data)
    n = store.upsert_conversations(data["records"], group)
    return {"group_id": group, "count": n, "ids": [r["conversation_id"] for r in data["records"]]}


@app.get("/api/groups")
def groups():
    return store.groups()


@app.post("/api/conversations/upload")
def upload(body: Dict[str, Any] = Body(...)):
    """{"group_id": "UPLOAD", "payload": <JSON организаторов / запись / массив>} или {"text": "Клиент: ...", "conversation_id": "..."}"""
    group = (body.get("group_id") or "UPLOAD").strip()
    try:
        if body.get("text"):
            turns = parse_plain_text(body["text"])
            if not turns:
                _err(400, "Не нашёл реплик. Формат строк: «Клиент: …», «Оператор: …», можно с [мм:сс] в начале")
            cid = (body.get("conversation_id") or f"manual-{uuid.uuid4().hex[:6]}").strip()
            records = records_from_upload({"conversation_id": cid, "channel": body.get("channel") or "agent chat",
                                           "language": body.get("language"), "transcript": turns})
        else:
            records = records_from_upload(body.get("payload"), default_id=group.lower())
    except ValueError as exc:
        _err(400, str(exc))
    store.upsert_conversations(records, group)
    return {"group_id": group, "count": len(records), "ids": [r["conversation_id"] for r in records]}


def _criterion_failed(row: Dict[str, Any]) -> bool:
    """Единое определение нарушения для списка диалогов и счётчиков дашборда."""
    if row.get("value") is None:
        return False
    if row["type"] == "flag":
        return row["value"] is True
    return (row.get("points") or 0) < (row.get("max_points") or 0)


@app.get("/api/conversations")
def conversations(group_id: Optional[str] = None, language: Optional[str] = None, channel: Optional[str] = None,
                  critical: Optional[bool] = None, needs_review: Optional[bool] = None, q: Optional[str] = None,
                  criterion_id: Optional[str] = None, criterion_violation: bool = False):
    if criterion_violation and not criterion_id:
        _err(400, "Для отбора нарушений укажите criterion_id")
    open_appeals = Counter(a["conversation_id"] for a in store.list_appeals(status="open"))
    rows = []
    for c in store.list_conversations(group_id):
        if language and (c.get("language") or "").upper() != language.upper():
            continue
        if channel and channel.lower() not in (c.get("channel") or "").lower():
            continue
        if q and q.lower() not in c["conversation_id"].lower():
            continue
        ev = final_view(store.latest_evaluation(c["conversation_id"]))
        if criterion_id:
            if not ev or ev.get("status") != "ok":
                continue
            criterion = next((r for r in ev["final"]["criteria"] if r["criterion_id"] == criterion_id), None)
            if criterion is None or criterion.get("value") is None:
                continue
            if criterion_violation and not _criterion_failed(criterion):
                continue
        summary = None
        if ev:
            ok = ev.get("status") == "ok"
            summary = {
                "evaluation_id": ev["_id"], "status": ev.get("status"), "error": ev.get("error"),
                "ai_score": ev["score"]["final_score"] if ok else None,
                "final_score": ev["final"]["score"]["final_score"] if ok else None,
                "critical_flag": ev["final"]["score"]["critical_flag"] if ok else None,
                "needs_review": any(r.get("needs_review") for r in ev.get("criteria") or []),
                "checklist_version": ev.get("checklist_version"), "evaluated_at": ev.get("created_at"),
                "cost_usd": (ev.get("usage") or {}).get("cost_usd"),
                "critical_reason": next((r.get("rationale") for r in (ev["final"]["criteria"] if ok else [])
                                         if r.get("type") == "flag" and r.get("value") is True), None),
            }
        if critical is not None and (not summary or bool(summary.get("critical_flag")) != critical):
            continue
        if needs_review is not None and (not summary or bool(summary.get("needs_review")) != needs_review):
            continue
        c["evaluation"] = summary
        c["open_appeals"] = open_appeals.get(c["conversation_id"], 0)
        rows.append(c)
    return rows


@app.get("/api/conversations/{cid}")
def conversation(cid: str):
    conv = store.get_conversation(cid)
    if not conv:
        _err(404, "Диалог не найден")
    ev = store.latest_evaluation(cid)
    cl = _checklist_for(ev) if ev else store.active_checklist() or load_default()
    return {
        "conversation": {k: conv.get(k) for k in ("conversation_id", "channel", "language", "audio_url", "group_id")},
        "turns": build_turns(conv),
        "facts": compute_facts(conv, threshold_sec=int(cl.get("first_response_threshold_sec") or 180)),
        "evaluation": final_view(ev),
        "history": store.evaluation_history(cid),
        "appeals": [_appeal_full(a) for a in store.list_appeals(conversation_id=cid)],
    }


# ---------------- чек-листы ----------------
@app.get("/api/checklists")
def checklists():
    return store.list_checklists()


@app.get("/api/checklists/active")
def checklist_active():
    return store.active_checklist()


@app.get("/api/checklists/{cl_id}")
def checklist_get(cl_id: int):
    cl = store.get_checklist(cl_id)
    if not cl:
        _err(404, "Чек-лист не найден")
    return cl


def _threshold_errors(data: Dict[str, Any]) -> List[str]:
    threshold = data.get("first_response_threshold_sec", 180)
    if type(threshold) is not int or threshold < 1:
        return ["Порог первой реакции должен быть целым числом секунд больше нуля"]
    return []


@app.post("/api/checklists/validate")
def checklist_validate(body: Dict[str, Any] = Body(...)):
    data = ensure_defaults(body.get("data") or body)
    return {"errors": validate_checklist(data) + _threshold_errors(data),
            "weights_sum": sum(float(c.get("weight") or 0) for c in data.get("criteria", []) if c.get("type") != "flag")}


@app.post("/api/checklists")
def checklist_save(body: Dict[str, Any] = Body(...)):
    """{"data": {...чек-лист...}, "note": "что поменяли", "activate": true} → новая версия."""
    errors = _threshold_errors(body.get("data") or {})
    if errors:
        _err(400, errors[0])
    try:
        return store.save_checklist(body.get("data") or {}, note=body.get("note") or "", activate=body.get("activate", True))
    except ValueError as exc:
        _err(400, str(exc))


@app.post("/api/checklists/{cl_id}/activate")
def checklist_activate(cl_id: int):
    if not store.get_checklist(cl_id):
        _err(404, "Чек-лист не найден")
    return store.activate_checklist(cl_id)


# ---------------- оценка ----------------
def _run_job(job_id: str, convs: List[Dict[str, Any]], checklist: Dict[str, Any], votes: Optional[int], reuse: bool,
             force: Optional[List[str]]):
    def on_done(res):
        eval_id = store.save_evaluation(res, checklist.get("_id"))
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["done"] += 1
            job["cost_usd"] += (res.get("usage") or {}).get("cost_usd") or 0
            job["llm_calls"] += (res.get("usage") or {}).get("calls") or 0
            job["results"].append({"conversation_id": res["conversation_id"], "evaluation_id": eval_id,
                                   "status": res.get("status"), "error": res.get("error"),
                                   "final_score": (res.get("score") or {}).get("final_score"),
                                   "critical_flag": (res.get("score") or {}).get("critical_flag")})

    try:
        previous = {}
        if reuse:
            for c in convs:
                prev = store.latest_evaluation(c["conversation_id"], ok_only=True)
                if prev:
                    previous[c["conversation_id"]] = prev
        evaluate_many(convs, checklist, client, votes=votes, previous=previous, force=force, on_done=on_done)
        status = "done"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        with JOBS_LOCK:
            JOBS[job_id]["error"] = str(exc)
    with JOBS_LOCK:
        JOBS[job_id]["status"] = status
        JOBS[job_id]["finished_at"] = time.time()


@app.post("/api/evaluate")
def evaluate(body: Dict[str, Any] = Body(...)):
    """{"conversation_ids": [...]} или {"group_id": "DEV"}; checklist_id (по умолчанию активный);
    reuse=true — переиспользовать неизменённые критерии прошлой оценки (смена веса — без вызова модели);
    force=["contact"] — перегнать эти критерии в любом случае."""
    ids = body.get("conversation_ids") or []
    if "group_id" in body and not ids:
        ids = [c["conversation_id"] for c in store.list_conversations(body["group_id"] or None)]
    if not ids:
        _err(400, "Нечего оценивать: передайте conversation_ids или group_id")
    convs = [store.get_conversation(i) for i in ids]
    if any(c is None for c in convs):
        _err(404, "Часть диалогов не найдена")
    checklist = store.get_checklist(body["checklist_id"]) if body.get("checklist_id") else store.active_checklist()
    if not checklist:
        _err(404, "Чек-лист не найден")
    group_id = body.get("group_id") or None
    if "group_id" not in body:
        groups = {c.get("group_id") for c in convs}
        if len(groups) == 1:
            group_id = groups.pop() or None
    votes = body.get("votes") or settings.llm_votes
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        for current in JOBS.values():
            if current["status"] != "running":
                continue
            same_group = group_id is not None and current.get("group_id") == group_id
            if same_group or set(ids).intersection(current.get("conversation_ids") or []):
                raise HTTPException(status_code=409, detail={
                    "message": "Оценка этой группы или выбранных диалогов уже выполняется",
                    "job_id": current["job_id"],
                })
        JOBS[job_id] = {"job_id": job_id, "status": "running", "total": len(convs), "done": 0, "results": [],
                        "cost_usd": 0.0, "llm_calls": 0, "started_at": time.time(), "finished_at": None, "error": None,
                        "group_id": group_id, "conversation_ids": list(ids), "votes": votes,
                        "checklist_id": checklist.get("_id"), "checklist_version": checklist.get("version")}
    try:
        threading.Thread(target=_run_job, args=(job_id, convs, checklist, votes, body.get("reuse", True),
                                                body.get("force")), daemon=True).start()
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", error=str(exc), finished_at=time.time())
        raise
    return {"job_id": job_id, "total": len(convs)}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            _err(404, "Задача не найдена")
        out = dict(j)
        out["results"] = list(j["results"])
    out["elapsed_sec"] = round((out["finished_at"] or time.time()) - out["started_at"], 1)
    return out


@app.get("/api/jobs")
def jobs():
    with JOBS_LOCK:
        recent = sorted(({k: v for k, v in j.items() if k != "results"} for j in JOBS.values()),
                        key=lambda x: -x["started_at"])
    running = [j for j in recent if j["status"] == "running"]
    completed = [j for j in recent if j["status"] != "running"][:20]
    return sorted(running + completed, key=lambda x: -x["started_at"])


@app.get("/api/evaluations/{eval_id}")
def evaluation(eval_id: int):
    ev = store.get_evaluation(eval_id)
    if not ev:
        _err(404, "Оценка не найдена")
    return final_view(ev)


# ---------------- апелляции ----------------
def _fragment(conv: Dict[str, Any], turns_idx: List[int], pad: int = 1) -> List[Dict[str, Any]]:
    turns = build_turns(conv)
    keep = set()
    for n in turns_idx or []:
        for k in range(n - pad, n + pad + 1):
            if 1 <= k <= len(turns):
                keep.add(k)
    return [t for t in turns if t["n"] in keep]


def _appeal_full(ap: Dict[str, Any]) -> Dict[str, Any]:
    ev = store.get_evaluation(ap["evaluation_id"])
    conv = store.get_conversation(ap["conversation_id"])
    row = next((r for r in (ev or {}).get("criteria") or [] if r["criterion_id"] == ap["criterion_id"]), None)
    cl = _checklist_for(ev) if ev else ensure_defaults(load_default())
    crit = next((c for c in cl["criteria"] if c["id"] == ap["criterion_id"]), None)
    out = dict(ap)
    out["criterion"] = crit
    out["original"] = row
    out["original_label"] = value_label(crit, ap["original_value"]) if crit else str(ap["original_value"])
    out["decision_label"] = value_label(crit, ap["decision_value"]) if crit and ap["status"] != "open" else None
    out["fragment"] = _fragment(conv, (row or {}).get("evidence_turns") or []) if conv else []
    view = final_view(ev)
    if view and view.get("status") == "ok":
        out["ai_final_score"] = view["ai_score"]["final_score"]
        out["final_score"] = view["final"]["score"]["final_score"]
    return out


def _ai_review_bg(appeal_id: int):
    try:
        ap = store.get_appeal(appeal_id)
        ev = store.get_evaluation(ap["evaluation_id"])
        conv = store.get_conversation(ap["conversation_id"])
        row = next(r for r in ev["criteria"] if r["criterion_id"] == ap["criterion_id"])
        review = review_appeal(conv, _checklist_for(ev), client, ap["criterion_id"], row, ap["comment"] or "")
        store.set_appeal_ai_review(appeal_id, review)
    except Exception as exc:  # noqa: BLE001
        store.set_appeal_ai_review(appeal_id, {"error": str(exc)})


@app.post("/api/appeals")
def appeal_create(body: Dict[str, Any] = Body(...)):
    """{"evaluation_id": 12, "criterion_id": "empathy", "comment": "...", "author": "Оператор Алина"}"""
    try:
        ap = store.create_appeal(int(body["evaluation_id"]), body["criterion_id"], body.get("comment") or "",
                                 body.get("author") or "оператор")
    except (KeyError, ValueError) as exc:
        _err(400, str(exc))
    if body.get("ai_review", True):
        threading.Thread(target=_ai_review_bg, args=(ap["id"],), daemon=True).start()
    return _appeal_full(ap)


@app.get("/api/appeals")
def appeals(status: Optional[str] = None, conversation_id: Optional[str] = None):
    return [_appeal_full(a) for a in store.list_appeals(status=status, conversation_id=conversation_id)]


@app.get("/api/appeals/{appeal_id}")
def appeal_get(appeal_id: int):
    ap = store.get_appeal(appeal_id)
    if not ap:
        _err(404, "Апелляция не найдена")
    return _appeal_full(ap)


@app.post("/api/appeals/{appeal_id}/ai-review")
def appeal_ai_review(appeal_id: int):
    if not store.get_appeal(appeal_id):
        _err(404, "Апелляция не найдена")
    _ai_review_bg(appeal_id)
    return _appeal_full(store.get_appeal(appeal_id))


@app.post("/api/appeals/{appeal_id}/decision")
def appeal_decide(appeal_id: int, body: Dict[str, Any] = Body(...)):
    """{"decision": "confirmed" | "changed", "value": <true|false|null|число>, "reason": "...", "reviewer": "..."}"""
    if body.get("decision") == "changed":
        ap = store.get_appeal(appeal_id)
        if not ap:
            _err(404, "Апелляция не найдена")
        ev = store.get_evaluation(ap["evaluation_id"])
        criterion = next((c for c in _checklist_for(ev)["criteria"] if c["id"] == ap["criterion_id"]), None)
        if not criterion or "value" not in body:
            _err(400, "Выберите новое значение критерия")
        value = body["value"]
        if value is None:
            valid = criterion["type"] != "flag" and criterion.get("na_allowed", False)
        elif criterion["type"] == "scale":
            valid = type(value) in (int, float) and value in scale_steps(criterion)
        else:
            valid = type(value) is bool
        if not valid:
            _err(400, "Значение не входит в допустимые варианты критерия")
    try:
        ap = store.decide_appeal(appeal_id, body.get("decision"), body.get("value"), body.get("reason") or "",
                                 body.get("reviewer") or "проверяющий")
    except ValueError as exc:
        _err(400, str(exc))
    return {"appeal": _appeal_full(ap), "evaluation": final_view(store.get_evaluation(ap["evaluation_id"]))}


# ---------------- выгрузка и аналитика ----------------
@app.get("/api/export")
def export(group_id: str, source: str = Query("ai", pattern="^(ai|final)$")):
    """results.json по набору. source=ai — оценка системы (для сдачи); final — с учётом решений по апелляциям."""
    ids = [c["conversation_id"] for c in store.list_conversations(group_id)]
    results = []
    for cid in ids:
        ev = final_view(store.latest_evaluation(cid))
        if not ev:
            continue
        if source == "final" and ev.get("status") == "ok":
            ev = dict(ev, criteria=ev["final"]["criteria"], score=ev["final"]["score"])
        results.append(ev)
    doc = build_results(results, ids, model_name=client.model, mode=settings.model_mode, criteria_order=OFFICIAL_IDS)
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="results_{group_id}.json"'})


@app.post("/api/results/save")
def results_save(body: Dict[str, Any] = Body(...)):
    """{"group_id": "DEV", "target": "dev" | "prod"} → results/<target>/results.json (оценка системы) + проверка формата."""
    group = (body.get("group_id") or "").strip()
    target = (body.get("target") or "").strip()
    if target not in ("dev", "prod"):
        _err(400, "target: dev или prod")
    ids = [c["conversation_id"] for c in store.list_conversations(group)]
    if not ids:
        _err(400, f"В группе {group!r} нет диалогов")
    results = [r for r in (store.latest_evaluation(cid) for cid in ids) if r]
    path = ROOT / "results" / target / "results.json"
    if has_mock_evaluations(results):
        return {"path": str(path.relative_to(ROOT)), "saved": False, "count": len(ids),
                "ok": sum(1 for r in results if r.get("status") == "ok"),
                "not_evaluated": len(ids) - len(results),
                "errors": ["в выгрузке есть оценки заглушки"], "warnings": []}
    used = sorted({v.get("model") for r in results if r.get("status") == "ok" for c in r["criteria"]
                   for v in c.get("votes") or [] if v.get("model")})
    doc = build_results(results, ids, model_name=" + ".join(used) if used else client.model, mode=settings.model_mode,
                        criteria_order=OFFICIAL_IDS)
    turn_counts = {}
    for cid in ids:
        conv = store.get_conversation(cid)
        turn_counts[cid] = len(conv.get("transcript") or []) if conv else 0
    # сначала пишем во временный файл рядом и проверяем; прежний results.json заменяется только проверенным
    tmp = path.with_name(".results.json.tmp")
    write_results(doc, str(tmp))
    errors, warnings = validate_file(str(tmp), ids, turn_counts, 10 if target == "prod" else None)
    saved = not errors
    if saved:
        os.replace(tmp, path)
    else:
        tmp.unlink(missing_ok=True)
    ok = sum(1 for r in doc["results"] if r["status"] == "ok")
    return {"path": str(path.relative_to(ROOT)), "saved": saved, "count": len(doc["results"]), "ok": ok,
            "not_evaluated": len(ids) - len(results), "errors": errors, "warnings": warnings}


@app.get("/api/stats")
def stats(group_id: Optional[str] = None):
    evs = []
    conversations_in_group = store.list_conversations(group_id)
    conversation_ids = {c["conversation_id"] for c in conversations_in_group}
    for c in conversations_in_group:
        ev = final_view(store.latest_evaluation(c["conversation_id"]))
        if ev and ev.get("status") == "ok":
            evs.append(ev)
    per_crit = defaultdict(lambda: {"n": 0, "applicable": 0, "points": 0.0, "max": 0.0, "na": 0, "fail": 0, "name": ""})
    by_lang = defaultdict(list)
    for ev in evs:
        score = ev["final"]["score"]["final_score"]
        if score is not None:
            by_lang[(ev.get("language") or "—")].append(score)
        for r in ev["final"]["criteria"]:
            s = per_crit[r["criterion_id"]]
            s["name"] = r["name"]
            s["n"] += 1
            s["fail"] += int(_criterion_failed(r))
            if r["type"] == "flag":
                continue
            if r["value"] is None:
                s["na"] += 1
                continue
            s["applicable"] += 1
            s["points"] += r.get("points") or 0
            s["max"] += r.get("max_points") or 0
    scores = [e["final"]["score"]["final_score"] for e in evs if e["final"]["score"]["final_score"] is not None]
    return {
        "criterion_filter_supported": True,
        "evaluated": len(evs),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "critical_count": sum(1 for e in evs if e["final"]["score"]["critical_flag"]),
        "needs_review_count": sum(1 for e in evs if any(r.get("needs_review") for r in e["criteria"])),
        "open_appeals": sum(1 for a in store.list_appeals(status="open") if a["conversation_id"] in conversation_ids),
        "total_cost_usd": round(sum((e.get("usage") or {}).get("cost_usd") or 0 for e in evs), 4),
        "avg_cost_usd": round(sum((e.get("usage") or {}).get("cost_usd") or 0 for e in evs) / len(evs), 5) if evs else None,
        "criteria": [{"criterion_id": k, **{kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()},
                      "rate": round(v["points"] / v["max"] * 100, 1) if v["max"] else None} for k, v in per_crit.items()],
        "by_language": {k: round(sum(v) / len(v), 2) for k, v in by_lang.items() if v},
    }


@app.get("/api/diff")
def diff(group_id: Optional[str] = None, from_checklist: Optional[int] = None, to_checklist: Optional[int] = None):
    """Как изменились итоги между двумя версиями чек-листа (по последним оценкам каждой версии)."""
    out = []
    for c in store.list_conversations(group_id):
        a = store.latest_evaluation(c["conversation_id"], checklist_id=from_checklist, ok_only=True) if from_checklist else None
        b = store.latest_evaluation(c["conversation_id"], checklist_id=to_checklist, ok_only=True) if to_checklist else None
        if not a or not b:
            continue
        changed = []
        rows_a = {r["criterion_id"]: r for r in a["criteria"]}
        for r in b["criteria"]:
            ra = rows_a.get(r["criterion_id"])
            if not ra or any(ra.get(k) != r.get(k) for k in ("value", "def_hash", "weight", "type", "name")):
                changed.append({"criterion_id": r["criterion_id"], "before": ra.get("value_label") if ra else "—",
                                "after": r.get("value_label")})
        for cid_a in rows_a:
            if cid_a not in {r["criterion_id"] for r in b["criteria"]}:
                changed.append({"criterion_id": cid_a, "before": rows_a[cid_a].get("value_label"), "after": "удалён"})
        out.append({"conversation_id": c["conversation_id"], "before": a["score"]["final_score"],
                    "after": b["score"]["final_score"], "changed": changed})
    return out


# ---------------- интерфейс ----------------
WEB_DIR = ROOT / "web"
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
