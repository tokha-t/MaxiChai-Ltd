"""Оценка диалога: факты кодом → по вызову модели на критерий (с голосованием) → валидация → скоринг кодом."""
from __future__ import annotations

import copy
import hashlib
import json
import statistics
import time
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .checklist import criterion_hash, ensure_defaults, scale_steps, value_label, verdict_to_value
from .config import settings
from .facts import build_turns, compute_facts, facts_to_text
from .llm import JudgePanel, LLMError, LLMResult
from .prompts import (batch_response_schema, build_appeal_messages, build_batch_messages, build_messages,
                      response_schema)
from .scoring import compute_score

_shared_pool: Optional[ThreadPoolExecutor] = None


def llm_pool() -> ThreadPoolExecutor:
    global _shared_pool
    if _shared_pool is None:
        _shared_pool = ThreadPoolExecutor(max_workers=settings.llm_concurrency, thread_name_prefix="llm")
    return _shared_pool


def _clean_turns(raw: Any, n_turns: int) -> List[int]:
    out = []
    for x in raw or []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= n_turns and n not in out:
            out.append(n)
    return sorted(out)


def _default_evidence(cid: str, facts: Dict[str, Any]) -> List[int]:
    if cid == "closure":
        for key in ("last_operator_turn", "operator_closed_turn", "client_left_turn"):
            if facts.get(key):
                return [facts[key]]
    if cid == "identification" and facts.get("identity_verified_turns"):
        return list(facts["identity_verified_turns"])
    if facts.get("first_operator_turn"):
        return [facts["first_operator_turn"]]
    return [1] if facts.get("n_turns") else []


def _vote_metadata(c: Dict[str, Any], votes: List[Dict[str, Any]], requested: int) -> Dict[str, Any]:
    valid = sum(1 for vote in votes if verdict_to_value(c, vote.get("verdict"))[1])
    complete = valid >= requested
    verb = "Получена" if valid % 10 == 1 and valid % 100 != 11 else "Получено"
    noun = "проверки" if requested % 10 == 1 and requested % 100 != 11 else "проверок"
    return {
        "requested_votes": requested, "received_votes": len(votes), "valid_votes": valid,
        "votes_complete": complete,
        "review_reason": None if complete else f"{verb} {valid} из {requested} {noun}",
        "actual_models": sorted({v["model"] for v in votes if v.get("model")}),
    }


def aggregate_votes(c: Dict[str, Any], votes: List[LLMResult], facts: Dict[str, Any],
                    requested_votes: Optional[int] = None) -> Dict[str, Any]:
    parsed = []
    for v in votes:
        value, ok = verdict_to_value(c, v.data.get("verdict"))
        if ok:
            parsed.append((value, v))
    if not parsed:
        raise LLMError(f"Модель не дала допустимого ответа по критерию {c['id']}")

    if c["type"] == "scale":
        nums = sorted(v for v, _ in parsed if v is not None)
        na_count = len(parsed) - len(nums)
        if na_count > len(nums):
            final = None
        else:
            final = statistics.median_low(nums)
            steps = scale_steps(c)
            final = min(steps, key=lambda s: (abs(s - final), s))
    else:
        counts = Counter(v for v, _ in parsed)
        ranked = counts.most_common()
        best_n = ranked[0][1]
        tied = [val for val, n in ranked if n == best_n]
        if len(tied) == 1:
            final = tied[0]
        else:
            def avg_conf(val):
                cs = [float(v.data.get("confidence") or 0) for x, v in parsed if x == val]
                return sum(cs) / len(cs)
            final = max(tied, key=avg_conf)

    agreeing = [v for x, v in parsed if x == final]
    rep = max(agreeing, key=lambda v: float(v.data.get("confidence") or 0)) if agreeing else parsed[0][1]
    agreement = len(agreeing) / len(parsed)
    self_conf = [float(v.data.get("confidence") or 0) for v in agreeing] or [0.0]
    model_conf = sum(self_conf) / len(self_conf)
    evidence = _clean_turns(rep.data.get("evidence_turns"), facts.get("n_turns") or 0)
    evidence_fallback = not evidence
    confidence = round(agreement * 0.7 + max(0.0, min(1.0, model_conf)) * 0.3, 2)
    if evidence_fallback:
        evidence = _default_evidence(c["id"], facts)
        confidence = min(confidence, 0.5)
    vote_rows = [
        {"verdict": v.data.get("verdict"), "rationale": v.data.get("rationale"),
         "evidence_turns": v.data.get("evidence_turns"), "confidence": v.data.get("confidence"), "model": v.model,
         **{k: value for k, value in (v.extra.get("provenance") or {}).items() if k in ("provider", "local")}}
        for v in votes
    ]
    metadata = _vote_metadata(c, vote_rows, requested_votes if requested_votes is not None else len(votes))
    return {
        "value": final,
        "verdict": rep.data.get("verdict"),
        "rationale": (rep.data.get("rationale") or "").strip() or "Обоснование не получено.",
        "analysis": (rep.data.get("analysis") or "").strip(),
        "evidence_turns": evidence,
        "evidence_fallback": evidence_fallback,
        "agreement": round(agreement, 2),
        "model_confidence": round(max(0.0, min(1.0, model_conf)), 2),
        "confidence": confidence,
        "needs_review": evidence_fallback or agreement < 1.0 or model_conf < 0.6 or not metadata["votes_complete"],
        "votes": vote_rows,
        **metadata,
    }


def _criterion_row(c: Dict[str, Any], agg: Dict[str, Any], def_hash: str, reused: bool) -> Dict[str, Any]:
    row = {
        "criterion_id": c["id"],
        "name": c["name"],
        "section": c.get("section"),
        "type": c["type"],
        "weight": c.get("weight", 0),
        "na_allowed": bool(c.get("na_allowed")),
        "def_hash": def_hash,
        "reused": reused,
    }
    row.update(agg)
    row["value_label"] = value_label(c, row["value"])
    return row


def attach_scores(result: Dict[str, Any], checklist: Dict[str, Any]) -> Dict[str, Any]:
    values = {r["criterion_id"]: r["value"] for r in result["criteria"]}
    score = compute_score(checklist, values)
    per = score.pop("criteria")
    for r in result["criteria"]:
        r.update(per.get(r["criterion_id"], {}))
    result["score"] = score
    return result


def _batch_items(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ответ пакетного вызова → {id критерия: ответ}. Терпимо к вариантам формы ответа."""
    items = data.get("criteria", data)
    if isinstance(items, list):
        items = {str(x.get("id") or x.get("criterion_id")): x for x in items if isinstance(x, dict)}
    return items if isinstance(items, dict) else {}


def input_hash(conv: Dict[str, Any]) -> str:
    """Отпечаток входа: роли, тексты, метки времени и порядок реплик, канал."""
    payload = [conv.get("channel") or ""] + [[t.get("speaker"), t.get("text"), t.get("timestamp")]
                                            for t in (conv.get("transcript") or [])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _reusable(previous: Optional[Dict[str, Any]], client: Any, votes: int, in_hash: str) -> bool:
    """Прошлую оценку можно переиспользовать, только если совпадают модель и вход, а голосов было не меньше."""
    if not previous or previous.get("status") != "ok":
        return False
    if previous.get("model") != getattr(client, "model", None):
        return False
    prev_hash = previous.get("input_hash")
    return prev_hash is None or prev_hash == in_hash


def evaluate_conversation(conv: Dict[str, Any], checklist: Dict[str, Any], client: Any,
                          votes: Optional[int] = None, previous: Optional[Dict[str, Any]] = None,
                          force: Optional[List[str]] = None, mode: Optional[str] = None) -> Dict[str, Any]:
    """Оценивает один диалог. previous — прошлый результат: критерии с тем же хэшем формулировки
    переиспользуются без вызова модели (замена одного критерия перегоняет только его; смена веса — ничего)."""
    started = time.time()
    checklist = ensure_defaults(checklist)
    votes = votes or settings.llm_votes
    turns = build_turns(conv)
    facts = compute_facts(conv, threshold_sec=int(checklist.get("first_response_threshold_sec") or 180))
    facts_text = facts_to_text(facts)
    in_hash = input_hash(conv)
    prev_rows = {r["criterion_id"]: r for r in previous.get("criteria", [])} if _reusable(previous, client, votes, in_hash) else {}
    force = set(force or [])

    pool = llm_pool()
    panel = client if isinstance(client, JudgePanel) else JudgePanel([client])
    mode = (mode or settings.llm_mode or "batch").lower()
    offset = zlib.crc32(conv["conversation_id"].encode("utf-8"))  # разные диалоги начинают с разных судей
    rows: Dict[str, Dict[str, Any]] = {}
    todo: List[Dict[str, Any]] = []
    hashes: Dict[str, str] = {}
    for c in checklist["criteria"]:
        h = criterion_hash(c, checklist)
        hashes[c["id"]] = h
        prev = prev_rows.get(c["id"])
        saved_votes = (prev or {}).get("votes") or []
        enough_votes = (isinstance(saved_votes, list) and all(isinstance(v, dict) for v in saved_votes)
                        and _vote_metadata(c, saved_votes, votes)["votes_complete"])
        if prev and prev.get("def_hash") == h and c["id"] not in force and enough_votes:
            keep = {k: prev.get(k) for k in ("value", "verdict", "rationale", "analysis", "evidence_turns", "agreement",
                                              "model_confidence", "confidence", "needs_review", "votes")}
            keep["evidence_fallback"] = bool(prev.get("evidence_fallback"))
            keep.update(_vote_metadata(c, saved_votes, votes))
            keep["attempt_errors"] = list(prev.get("attempt_errors") or [])
            keep["needs_review"] = (keep["evidence_fallback"] or (keep.get("agreement") or 0) < 1.0
                                    or (keep.get("model_confidence") or 0) < 0.6)
            rows[c["id"]] = _criterion_row(c, keep, h, reused=True)
        else:
            todo.append(c)

    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}
    errors: List[str] = []
    attempt_errors: List[Dict[str, Any]] = []
    errors_by_cid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    model_provenance: List[Dict[str, Any]] = []
    votes_by_cid: Dict[str, List[LLMResult]] = defaultdict(list)

    def remember_provenance(value: Dict[str, Any]) -> None:
        provenance = {k: value[k] for k in ("model", "provider", "local") if k in value}
        if provenance.get("model") and provenance not in model_provenance:
            model_provenance.append(provenance)

    def record_errors(label: str, values: List[Dict[str, Any]]) -> None:
        for value in values:
            error = {k: value[k] for k in ("model", "provider", "local", "error_type") if k in value}
            error["scope"] = label
            attempt_errors.append(error)
            remember_provenance(error)
            for criterion in todo if label == "batch" else [c for c in todo if c["id"] == label]:
                errors_by_cid[criterion["id"]].append(error)

    def account(r: LLMResult) -> None:
        usage["calls"] += 1
        usage["prompt_tokens"] += r.prompt_tokens
        usage["completion_tokens"] += r.completion_tokens
        usage["cached_tokens"] += r.cached_tokens
        usage["cost_usd"] += r.cost_usd

    def collect(label: str, futures) -> List[LLMResult]:
        got: List[LLMResult] = []
        for f in futures:
            try:
                r = f.result()
                account(r)
                remember_provenance(r.extra.get("provenance") or {"model": r.model})
                record_errors(label, r.extra.get("attempt_errors") or [])
                got.append(r)
            except Exception as exc:  # noqa: BLE001 — фиксируем любую ошибку провайдера
                record_errors(label, getattr(exc, "attempt_errors", None) or [{"error_type": type(exc).__name__}])
                errors.append(f"{label}: запрос модели не выполнен ({type(exc).__name__})")
        return got

    if todo and mode == "batch":
        messages = build_batch_messages(conv, turns, facts_text, checklist, todo)
        schema = batch_response_schema(todo)
        for r in collect("batch", [pool.submit(panel.complete_json, messages, schema, offset + i) for i in range(votes)]):
            items = _batch_items(r.data)
            for c in todo:
                item = items.get(c["id"])
                votes_by_cid[c["id"]].append(LLMResult(data=item if isinstance(item, dict) else {}, raw="",
                                                       model=r.model, extra=r.extra))
    elif todo:
        futures = {}
        for c in todo:
            messages = build_messages(conv, turns, facts_text, checklist, c)
            futures[c["id"]] = [pool.submit(panel.complete_json, messages, response_schema(c), offset + i) for i in range(votes)]
        for cid, fs in futures.items():
            votes_by_cid[cid].extend(collect(cid, fs))

    for c in todo:
        results = votes_by_cid[c["id"]]
        for rnd in range(3):  # голоса пакета + до двух дозапросов по одному критерию, если ответы недопустимы или упали
            if results:
                try:
                    aggregate = aggregate_votes(c, results, facts, requested_votes=votes)
                    aggregate["attempt_errors"] = list(errors_by_cid[c["id"]])
                    rows[c["id"]] = _criterion_row(c, aggregate, hashes[c["id"]], reused=False)
                    break
                except LLMError as exc:
                    errors.append(str(exc))
            if rnd == 2:
                break
            messages = build_messages(conv, turns, facts_text, checklist, c)
            need = max(1, votes // 2 + 1)
            results = results + collect(c["id"], [pool.submit(panel.complete_json, messages, response_schema(c), offset + 3 + rnd * need + k)
                                                  for k in range(need)])
        votes_by_cid[c["id"]] = results

    vote_counts = {}
    for c in checklist["criteria"]:
        row = rows.get(c["id"])
        if row is not None:
            vote_counts[c["id"]] = {k: row[k] for k in ("requested_votes", "received_votes", "valid_votes", "votes_complete", "review_reason")}
            for vote in row["votes"]:
                remember_provenance(vote)
        else:
            vote_counts[c["id"]] = _vote_metadata(c, [dict(v.data, model=v.model) for v in votes_by_cid[c["id"]]], votes)
    incomplete = [cid for cid, counts in vote_counts.items() if not counts["votes_complete"]]
    actual_models = sorted({v["model"] for row in rows.values() for v in row["votes"] if v.get("model")}
                           | {v.model for values in votes_by_cid.values() for v in values if v.model})

    base = {
        "conversation_id": conv["conversation_id"],
        "channel": conv.get("channel"),
        "language": conv.get("language"),
        "checklist_name": checklist.get("name"),
        "checklist_version": checklist.get("version"),
        "checklist_id": checklist.get("_id"),
        "model": getattr(client, "model", "unknown"),
        "votes": votes,
        "requested_votes": votes,
        "received_votes": min((x["received_votes"] for x in vote_counts.values()), default=0),
        "valid_votes": min((x["valid_votes"] for x in vote_counts.values()), default=0),
        "votes_complete": not incomplete,
        "incomplete_criteria": incomplete,
        "vote_counts": vote_counts,
        "actual_models": actual_models,
        "model_provenance": model_provenance,
        "attempt_errors": attempt_errors,
        "needs_review": bool(incomplete) or any(row.get("needs_review") for row in rows.values()),
        "review_reason": vote_counts[incomplete[0]]["review_reason"] if incomplete else None,
        "input_hash": in_hash,
        "llm_mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "facts": facts,
    }
    usage["cost_usd"] = round(usage["cost_usd"], 5)
    usage["latency_sec"] = round(time.time() - started, 2)
    base["usage"] = usage
    missing = [c["id"] for c in checklist["criteria"] if c["id"] not in rows]
    if missing:
        base.update({"status": "error", "criteria": [], "score": None,
                     "error": "; ".join(errors) or f"Нет оценки по критериям: {', '.join(missing)}"})
        return base
    base.update({"status": "ok", "error": None, "criteria": [rows[c["id"]] for c in checklist["criteria"]]})
    return attach_scores(base, checklist)


def evaluate_many(convs: List[Dict[str, Any]], checklist: Dict[str, Any], client: Any, votes: Optional[int] = None,
                  previous: Optional[Dict[str, Dict[str, Any]]] = None, force: Optional[List[str]] = None,
                  parallel_dialogs: int = 4, on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
                  attempts: int = 2) -> List[Dict[str, Any]]:
    """Оценка набора. Каждый диалог — до `attempts` попыток; результаты в исходном порядке."""
    previous = previous or {}

    def one(conv):
        result = None
        for _ in range(max(1, attempts)):
            try:
                result = evaluate_conversation(conv, checklist, client, votes, previous.get(conv["conversation_id"]), force)
            except Exception as exc:  # noqa: BLE001
                result = {"conversation_id": conv["conversation_id"], "status": "error", "criteria": [], "score": None,
                          "error": f"Оценка не завершена ({type(exc).__name__})",
                          "requested_votes": votes or settings.llm_votes, "votes_complete": False,
                          "needs_review": True, "review_reason": "Проверки не завершены: ошибка оценки",
                          "actual_models": [], "model_provenance": [],
                          "attempt_errors": [{"scope": "evaluation", "error_type": type(exc).__name__}]}
            if result.get("status") == "ok":
                break
        if on_done:
            on_done(result)
        return result

    with ThreadPoolExecutor(max_workers=max(1, parallel_dialogs), thread_name_prefix="dialog") as ex:
        return list(ex.map(one, convs))


def rescore(result: Dict[str, Any], checklist: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Пересчёт итога без модели: новые веса чек-листа и/или ручные решения по апелляциям."""
    out = copy.deepcopy(result)
    if out.get("status") != "ok":
        return out
    checklist = ensure_defaults(checklist)
    by_id = {c["id"]: c for c in checklist["criteria"]}
    out["criteria"] = [r for r in out["criteria"] if r["criterion_id"] in by_id]
    for r in out["criteria"]:
        c = by_id[r["criterion_id"]]
        r["weight"] = c.get("weight", 0)
        if overrides and r["criterion_id"] in overrides:
            r["ai_value"] = r.get("ai_value", r["value"])
            r["value"] = overrides[r["criterion_id"]]
            r["overridden"] = True
        r["value_label"] = value_label(c, r["value"])
    return attach_scores(out, checklist)


def review_appeal(conv: Dict[str, Any], checklist: Dict[str, Any], client: Any, criterion_id: str,
                  original_row: Dict[str, Any], comment: str) -> Dict[str, Any]:
    """Второе мнение ИИ для проверяющего. Решение всё равно принимает человек."""
    checklist = ensure_defaults(checklist)
    c = next(x for x in checklist["criteria"] if x["id"] == criterion_id)
    turns = build_turns(conv)
    facts = compute_facts(conv, threshold_sec=int(checklist.get("first_response_threshold_sec") or 180))
    messages = build_appeal_messages(conv, turns, facts_to_text(facts), checklist, c, original_row, comment)
    res = client.complete_json(messages, None)
    value, ok = verdict_to_value(c, res.data.get("suggested_verdict"))
    return {
        "recommendation": res.data.get("recommendation"),
        "suggested_value": value if ok else original_row.get("value"),
        "suggested_label": value_label(c, value if ok else original_row.get("value")),
        "rationale": res.data.get("rationale"),
        "evidence_turns": _clean_turns(res.data.get("evidence_turns"), facts["n_turns"]),
        "model": getattr(client, "model", "unknown"),
        "cost_usd": round(res.cost_usd, 5),
    }
