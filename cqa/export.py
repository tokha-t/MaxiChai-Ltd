"""Выгрузка results.json в формате сдачи 1.0."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .checklist import OFFICIAL_IDS, criterion_hash, ensure_defaults, load_default, scale_steps
from .config import settings


def has_mock_evaluations(results: List[Dict[str, Any]]) -> bool:
    """Проверяем происхождение до выгрузки: формат 1.0 не содержит моделей отдельных голосов."""
    for result in results:
        if (result.get("model") or "").startswith("mock"):
            return True
        for criterion in result.get("criteria") or []:
            if any((vote.get("model") or "").startswith("mock") or vote.get("provider") == "mock"
                   for vote in criterion.get("votes") or []):
                return True
        if any((p.get("model") or "").startswith("mock") or p.get("provider") == "mock"
               for p in result.get("model_provenance") or []):
            return True
    return False


def checklist_fingerprint(checklist: Dict[str, Any]) -> str:
    def without_metadata(value):
        if isinstance(value, dict):
            return {k: without_metadata(v) for k, v in value.items() if not k.startswith("_")}
        if isinstance(value, list):
            return [without_metadata(v) for v in value]
        return value

    snapshot = without_metadata(ensure_defaults(checklist))
    snapshot.pop("version", None)
    content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _profile_errors(checklist: Dict[str, Any]) -> List[str]:
    criteria = checklist.get("criteria") or []
    if sorted(c.get("id", "") for c in criteria) != sorted(OFFICIAL_IDS):
        return ["Выбранный профиль должен содержать ровно 8 официальных критериев"]
    official = {c["id"]: c for c in ensure_defaults(load_default())["criteria"]}
    errors = []
    for criterion in criteria:
        base = official[criterion["id"]]
        if (any(criterion.get(k) != base.get(k) for k in ("type", "weight", "na_allowed"))
                or scale_steps(criterion) != scale_steps(base)):
            errors.append(f"Профиль: {criterion['id']} отличается от официальных весов, типов, шкал или N/A")
    return errors


def _saved_model_info(results: List[Dict[str, Any]]) -> Tuple[str, str, List[str], List[str]]:
    """Происхождение ответов, а не текущая конфигурация клиента или перечень плановых судей."""
    models: Dict[str, bool] = {}
    errors: List[str] = []
    warnings: List[str] = []
    external = {"openai", "gemini", "cerebras", "groq", "openrouter", "github", "mistral"}
    local = {"ollama", "lmstudio"}
    for result in results:
        cid = result["conversation_id"]
        votes = [v for c in result.get("criteria") or [] for v in c.get("votes") or []]
        if result.get("status") == "ok":
            if any(not c.get("votes") for c in result.get("criteria") or []):
                errors.append(f"{cid}: нет сохранённых ответов для определения происхождения оценки")
        elif not votes:
            provenance = result.get("model_provenance") or []
            actual = set(result.get("actual_models") or [])
            votes = [p for p in provenance if p.get("model") in actual] if actual else provenance
            if votes and not actual:
                warnings.append(f"{cid}: ответов нет; происхождение указано по сохранённым попыткам")
            if not votes:
                warnings.append(f"{cid}: происхождение ошибочной оценки не сохранено")
        for vote in votes:
            name = vote.get("model")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{cid}: в сохранённом ответе неизвестна модель")
                continue
            provider = vote.get("provider") or name.partition(":")[0]
            is_local = vote.get("local")
            if type(is_local) is not bool:
                is_local = True if provider in local else False if provider in external else None
            if is_local is None:
                errors.append(f"{cid}: неизвестно происхождение модели; нужны сохранённые provider/local")
                continue
            if name in models and models[name] != is_local:
                errors.append(f"{cid}: противоречивое происхождение сохранённой модели")
            models[name] = is_local
    if not models:
        errors.append("Происхождение выгрузки неизвестно: нет сохранённых ответов или сведений о попытках")
    mode = "local" if models and all(models.values()) else "hybrid" if any(models.values()) else "external_api"
    return " + ".join(sorted(models)), mode, errors, warnings


def prepare_submission(results: List[Dict[str, Any]], records: List[Dict[str, Any]],
                       checklist: Optional[Dict[str, Any]], manifest: Optional[Dict[str, Any]],
                       target: str, group_id: str) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """Единая подготовка официального AI-экспорта: ничего не пишет и не обращается к модели."""
    from .evaluator import input_hash
    from .validate import validate_results
    errors: List[str] = []
    warnings: List[str] = []
    if has_mock_evaluations(results):
        errors.append("в выгрузке есть оценки заглушки")
    if target not in ("dev", "prod"):
        errors.append("Для официальной выгрузки укажите target: dev или prod")
    if target == "dev" and group_id != "DEV":
        errors.append("В results/dev разрешён только исходный набор DEV")
    if not checklist or not checklist.get("_id"):
        errors.append("Явно выберите сохранённый профиль: checklist_id")
    if not manifest or manifest.get("group_id") != group_id:
        errors.append("Нет манифеста исходного набора: загрузите набор из API или исходного файла")
    if errors:
        return None, errors, warnings
    checklist = ensure_defaults(checklist)
    errors.extend(_profile_errors(checklist))
    expected = manifest["conversation_ids"]
    expected_count = 65 if target == "dev" else 10
    if len(expected) != expected_count or len(set(expected)) != len(expected):
        errors.append(f"Манифест {group_id}: ожидалось {expected_count} уникальных ID, сейчас {len(expected)}")
    record_ids = [r.get("conversation_id") for r in records]
    if len(record_ids) != len(expected) or set(record_ids) != set(expected):
        errors.append("ID диалогов не совпадают с исходным манифестом набора")
    for record in records:
        cid = record.get("conversation_id")
        if manifest["input_hashes"].get(cid) != input_hash(record):
            errors.append(f"{cid}: входные данные отличаются от исходного набора")
    result_ids = [r.get("conversation_id") for r in results]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) - set(expected):
        errors.append("Повторные или посторонние ID в выбранных оценках")
    by_criterion = {c["id"]: c for c in checklist["criteria"]}
    for result in results:
        cid = result["conversation_id"]
        if result.get("status") != "ok":
            continue
        if result.get("_stored_status") == "stale":
            errors.append(f"{cid}: выбранная оценка устарела после изменения входных данных")
        actual_id = result.get("_checklist_id", result.get("checklist_id"))
        if actual_id != checklist["_id"] or result.get("checklist_version") != checklist["version"]:
            errors.append(f"{cid}: оценка профиля {actual_id}/v{result.get('checklist_version')}, выбран {checklist['_id']}/v{checklist['version']}")
        if result.get("input_hash") != manifest["input_hashes"].get(cid):
            errors.append(f"{cid}: вход оценки не подтверждён хэшем исходного набора")
        rows = result.get("criteria") or []
        ids = [r.get("criterion_id") for r in rows]
        if len(ids) != len(OFFICIAL_IDS) or set(ids) != set(OFFICIAL_IDS):
            errors.append(f"{cid}: нужны все 8 критериев без повторов и дополнительных критериев")
            continue
        for row in rows:
            criterion = by_criterion.get(row["criterion_id"])
            if criterion and (row.get("def_hash") != criterion_hash(criterion, checklist)
                              or any(row.get(k) != criterion.get(k) for k in ("weight", "type"))):
                errors.append(f"{cid}/{row['criterion_id']}: определение или вес не соответствуют выбранному профилю")
        if result.get("votes_complete") is False:
            warnings.append(f"{cid}: получены не все запрошенные проверки; требуется проверяющий")
    if errors:
        return None, errors, warnings
    model_name, mode, origin_errors, origin_warnings = _saved_model_info(results)
    errors.extend(origin_errors)
    warnings.extend(origin_warnings)
    if errors:
        return None, errors, warnings
    # Проверяем сырые значения ДО int() и нормализации result_record, включая critical_flag.
    raw_records = []
    for result in results:
        if result.get("status") == "ok":
            raw_records.append({k: result.get(k) for k in ("conversation_id", "status", "criteria", "score")})
        else:
            raw_records.append(result_record(result))
    raw_doc = {"schema_version": "1.0", "team_name": settings.team_name,
               "model": {"name": model_name, "mode": mode}, "results": raw_records}
    raw_errors, _ = validate_results(raw_doc, turn_counts=manifest["turn_counts"], checklist=checklist)
    if raw_errors:
        return None, raw_errors, warnings
    doc = build_results(results, expected, model_name=model_name, mode=mode)
    validation_errors, validation_warnings = validate_results(doc, expected, manifest["turn_counts"],
                                                              expected_count, checklist=checklist)
    return (None if validation_errors else doc), validation_errors, warnings + validation_warnings


def atomic_write_results(doc: Dict[str, Any], path: str) -> Path:
    """Вызывается после проверки: уникальный файл рядом, атомарная замена, очистка при ошибке."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(target.parent),
                                         prefix="." + target.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(doc, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


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
