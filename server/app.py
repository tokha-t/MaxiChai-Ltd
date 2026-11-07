"""HTTP API для интерфейса. Запуск: uvicorn server.app:app --reload --port 8000

Интерфейс (папка web/) отдаётся с того же сервера, поэтому CORS к API организаторов не нужен:
браузер ходит только сюда, а сервер — к организаторам.
"""
from __future__ import annotations

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

from cqa.checklist import OFFICIAL_IDS, ensure_defaults, load_default, validate_checklist, value_label
from cqa.config import ROOT, settings
from cqa.dataset import fetch_group, parse_plain_text, records_from_upload, save_cache
from cqa.evaluator import evaluate_many, rescore, review_appeal
from cqa.export import build_results
from cqa.facts import build_turns, compute_facts
from cqa.llm import make_client
from cqa.store import Store

app = FastAPI(title="CQA — Conversation Quality Analytics", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

store = Store()
client = make_client()
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


@app.get("/api/conversations")
def conversations(group_id: Optional[str] = None, language: Optional[str] = None, channel: Optional[str] = None,
                  critical: Optional[bool] = None, needs_review: Optional[bool] = None, q: Optional[str] = None):
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
    cl = store.active_checklist() or load_default()
    return {
        "conversation": {k: conv.get(k) for k in ("conversation_id", "channel", "language", "audio_url", "group_id")},
        "turns": build_turns(conv),
        "facts": compute_facts(conv, threshold_sec=int(cl.get("first_response_threshold_sec") or 180)),
        "evaluation": final_view(store.latest_evaluation(cid)),
        "history": store.evaluation_history(cid),
        "appeals": store.list_appeals(conversation_id=cid),
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


@app.post("/api/checklists/validate")
def checklist_validate(body: Dict[str, Any] = Body(...)):
    data = ensure_defaults(body.get("data") or body)
    return {"errors": validate_checklist(data),
            "weights_sum": sum(float(c.get("weight") or 0) for c in data.get("criteria", []) if c.get("type") != "flag")}


@app.post("/api/checklists")
def checklist_save(body: Dict[str, Any] = Body(...)):
    """{"data": {...чек-лист...}, "note": "что поменяли", "activate": true} → новая версия."""
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
    previous = {}
    if reuse:
        for c in convs:
            prev = store.latest_evaluation(c["conversation_id"], ok_only=True)
            if prev:
                previous[c["conversation_id"]] = prev

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
    if body.get("group_id") and not ids:
        ids = [c["conversation_id"] for c in store.list_conversations(body["group_id"])]
    if not ids:
        _err(400, "Нечего оценивать: передайте conversation_ids или group_id")
    convs = [store.get_conversation(i) for i in ids]
    if any(c is None for c in convs):
        _err(404, "Часть диалогов не найдена")
    checklist = store.get_checklist(body["checklist_id"]) if body.get("checklist_id") else store.active_checklist()
    if not checklist:
        _err(404, "Чек-лист не найден")
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"job_id": job_id, "status": "running", "total": len(convs), "done": 0, "results": [],
                        "cost_usd": 0.0, "llm_calls": 0, "started_at": time.time(), "finished_at": None, "error": None,
                        "checklist_id": checklist.get("_id"), "checklist_version": checklist.get("version")}
    threading.Thread(target=_run_job, args=(job_id, convs, checklist, body.get("votes"), body.get("reuse", True),
                                            body.get("force")), daemon=True).start()
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
        return sorted(({k: v for k, v in j.items() if k != "results"} for j in JOBS.values()), key=lambda x: -x["started_at"])[:20]


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


@app.get("/api/stats")
def stats(group_id: Optional[str] = None):
    evs = []
    for c in store.list_conversations(group_id):
        ev = final_view(store.latest_evaluation(c["conversation_id"]))
        if ev and ev.get("status") == "ok":
            evs.append(ev)
    per_crit = defaultdict(lambda: {"n": 0, "applicable": 0, "points": 0.0, "max": 0.0, "na": 0, "fail": 0, "name": ""})
    by_lang = defaultdict(list)
    for ev in evs:
        by_lang[(ev.get("language") or "—")].append(ev["final"]["score"]["final_score"])
        for r in ev["final"]["criteria"]:
            s = per_crit[r["criterion_id"]]
            s["name"] = r["name"]
            s["n"] += 1
            if r["type"] == "flag":
                s["fail"] += 1 if r["value"] is True else 0
                continue
            if r["value"] is None:
                s["na"] += 1
                continue
            s["applicable"] += 1
            s["points"] += r.get("points") or 0
            s["max"] += r.get("max_points") or 0
            if (r.get("points") or 0) < (r.get("max_points") or 0):
                s["fail"] += 1
    scores = [e["final"]["score"]["final_score"] for e in evs if e["final"]["score"]["final_score"] is not None]
    return {
        "evaluated": len(evs),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "critical_count": sum(1 for e in evs if e["final"]["score"]["critical_flag"]),
        "needs_review_count": sum(1 for e in evs if any(r.get("needs_review") for r in e["criteria"])),
        "open_appeals": len(store.list_appeals(status="open")),
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
            if not ra or ra.get("value") != r.get("value"):
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
