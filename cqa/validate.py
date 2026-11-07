"""Проверка results.json по правилам приложения «Как сдать результаты» (формат 1.0)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .checklist import OFFICIAL_IDS, ensure_defaults, load_default
from .scoring import compute_score

ALLOWED = {
    "contact": ("bool",),
    "discovery": ("bool", "null"),
    "identification": ("bool", "null"),
    "consultation": ("scale", "null"),
    "empathy": ("bool",),
    "dialogue": ("scale",),
    "closure": ("bool", "null"),
    "critical": ("bool",),
}
SCALES = {"consultation": {0, 5, 10, 15, 20}, "dialogue": {0, 4, 7, 10}}
MODES = {"local", "external_api", "hybrid"}


def _value_ok(cid: str, value: Any) -> bool:
    kinds = ALLOWED[cid]
    if value is None:
        return "null" in kinds
    if isinstance(value, bool):
        return "bool" in kinds
    if isinstance(value, (int, float)) and "scale" in kinds:
        return float(value).is_integer() and int(value) in SCALES[cid]
    return False


def validate_results(doc: Any, expected_ids: Optional[List[str]] = None,
                     turn_counts: Optional[Dict[str, int]] = None, expected_count: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """Возвращает (ошибки, предупреждения)."""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(doc, dict):
        return ["Корень JSON должен быть объектом"], warnings
    if doc.get("schema_version") != "1.0":
        errors.append('schema_version должен быть "1.0"')
    if not isinstance(doc.get("team_name"), str) or not doc["team_name"].strip():
        errors.append("team_name пустой")
    model = doc.get("model")
    if not isinstance(model, dict) or model.get("mode") not in MODES or not str(model.get("name") or "").strip():
        errors.append("model должен быть {mode: local|external_api|hybrid, name: ...}")
    results = doc.get("results")
    if not isinstance(results, list):
        return errors + ["results должен быть массивом"], warnings

    checklist = ensure_defaults(load_default())
    seen = set()
    n_error = 0
    for i, r in enumerate(results, 1):
        where = f"results[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{where}: не объект")
            continue
        cid = r.get("conversation_id")
        where = f"{where} ({cid})"
        if not isinstance(cid, str) or not cid:
            errors.append(f"{where}: нет conversation_id")
        elif cid in seen:
            errors.append(f"{where}: повтор conversation_id")
        seen.add(cid)
        status = r.get("status")
        if status == "error":
            n_error += 1
            if r.get("criteria") != [] or r.get("score") is not None or not str(r.get("error") or "").strip():
                errors.append(f"{where}: для status=error нужны criteria=[], score=null и непустой error")
            continue
        if status != "ok":
            errors.append(f"{where}: status должен быть ok или error")
            continue
        crit = r.get("criteria")
        if not isinstance(crit, list):
            errors.append(f"{where}: criteria не массив")
            continue
        ids = [c.get("criterion_id") for c in crit if isinstance(c, dict)]
        if sorted(ids) != sorted(OFFICIAL_IDS) or len(ids) != len(set(ids)):
            errors.append(f"{where}: нужны все 8 критериев ровно по одному разу, сейчас: {ids}")
        values = {}
        for c in crit:
            if not isinstance(c, dict):
                continue
            k = c.get("criterion_id")
            if k not in ALLOWED:
                continue
            v = c.get("value")
            if not _value_ok(k, v):
                errors.append(f"{where}: недопустимое значение {k}={v!r}")
            values[k] = v
            if not isinstance(c.get("rationale"), str) or not c["rationale"].strip():
                errors.append(f"{where}: пустой rationale у {k}")
            ev = c.get("evidence_turns")
            if not isinstance(ev, list) or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in ev):
                errors.append(f"{where}: evidence_turns у {k} должен быть массивом номеров от 1")
            elif turn_counts and cid in turn_counts and any(x > turn_counts[cid] for x in ev):
                errors.append(f"{where}: evidence_turns у {k} выходит за длину транскрипта ({turn_counts[cid]})")
            elif not ev:
                warnings.append(f"{where}: пустой evidence_turns у {k}")
        score = r.get("score")
        if not isinstance(score, dict):
            errors.append(f"{where}: score должен быть объектом")
            continue
        fs = score.get("final_score")
        if fs is not None and (not isinstance(fs, (int, float)) or isinstance(fs, bool) or math.isnan(fs) or not 0 <= fs <= 100):
            errors.append(f"{where}: final_score вне диапазона 0..100")
        elif fs is not None and round(fs, 2) != fs:
            errors.append(f"{where}: final_score должен быть округлён до двух знаков")
        if score.get("critical_flag") is not values.get("critical"):
            errors.append(f"{where}: critical_flag должен совпадать с value критерия critical")
        if len(values) == len(ALLOWED) and all(_value_ok(k, v) for k, v in values.items()):
            recomputed = compute_score(checklist, values)["final_score"]
            if recomputed != fs:
                errors.append(f"{where}: final_score={fs}, а по правилам чек-листа выходит {recomputed}")
    if expected_ids is not None:
        missing = [x for x in expected_ids if x not in seen]
        extra = [x for x in seen if x not in set(expected_ids)]
        if missing:
            errors.append(f"Нет записей для ID: {missing}")
        if extra:
            errors.append(f"Лишние ID, которых нет в наборе: {extra}")
    if expected_count is not None and len(results) != expected_count:
        errors.append(f"Ожидалось записей: {expected_count}, сейчас: {len(results)}")
    if n_error:
        warnings.append(f"Записей со status=error: {n_error}")
    return errors, warnings


def validate_file(path: str, expected_ids: Optional[List[str]] = None, turn_counts: Optional[Dict[str, int]] = None,
                  expected_count: Optional[int] = None) -> Tuple[List[str], List[str]]:
    p = Path(path)
    try:
        raw = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return ["Файл не в UTF-8"], []
    try:
        doc = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"недопустимая константа {c}")))
    except ValueError as exc:
        return [f"Невалидный JSON: {exc}"], []
    return validate_results(doc, expected_ids, turn_counts, expected_count)
