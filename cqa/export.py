"""Выгрузка results.json в формате сдачи 1.0."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checklist import OFFICIAL_IDS
from .config import settings


def _criterion_out(row: Dict[str, Any]) -> Dict[str, Any]:
    value = row.get("value")
    if row.get("type") == "scale" and value is not None:
        value = int(value)
    return {
        "criterion_id": row["criterion_id"],
        "value": value,
        "rationale": row.get("rationale") or "",
        "evidence_turns": [int(x) for x in row.get("evidence_turns") or []],
    }


def result_record(result: Dict[str, Any], criteria_order: Optional[List[str]] = None) -> Dict[str, Any]:
    cid = result["conversation_id"]
    if result.get("status") != "ok":
        return {"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                "error": result.get("error") or "Оценка не выполнена"}
    rows = {r["criterion_id"]: r for r in result["criteria"]}
    order = criteria_order or [r["criterion_id"] for r in result["criteria"]]
    missing = [x for x in order if x not in rows]
    if missing:
        return {"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                "error": f"В оценке нет критериев: {', '.join(missing)}"}
    crit = [_criterion_out(rows[x]) for x in order]
    critical_value = next((c["value"] for c in crit if c["criterion_id"] == "critical"), None)
    return {
        "conversation_id": cid,
        "status": "ok",
        "criteria": crit,
        "score": {
            "final_score": result["score"]["final_score"],
            "critical_flag": bool(critical_value) if critical_value is not None else bool(result["score"]["critical_flag"]),
        },
    }


def build_results(results: List[Dict[str, Any]], conversation_ids: List[str], model_name: Optional[str] = None,
                  mode: Optional[str] = None, team_name: Optional[str] = None,
                  criteria_order: Optional[List[str]] = None) -> Dict[str, Any]:
    """Одна запись на каждый ID набора в исходном порядке; отсутствующие — как error."""
    by_id = {r["conversation_id"]: r for r in results}
    records = []
    for cid in conversation_ids:
        r = by_id.get(cid)
        if r is None:
            records.append({"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                            "error": "Диалог не был оценён"})
        else:
            records.append(result_record(r, criteria_order or OFFICIAL_IDS))
    return {
        "schema_version": "1.0",
        "team_name": team_name or settings.team_name,
        "model": {"mode": mode or settings.model_mode, "name": model_name or settings.llm_model},
        "results": records,
    }


def write_results(doc: Dict[str, Any], path: str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out
