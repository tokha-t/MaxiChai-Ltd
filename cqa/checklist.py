"""Чек-лист как конфигурация: загрузка, проверка, преобразование вердиктов модели в значения."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ROOT

DEFAULT_PATH = ROOT / "config" / "checklists" / "default.json"
TYPES = ("binary", "scale", "flag")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# Коды критериев из формата сдачи 1.0 — в этом порядке выгружаются в results.json
OFFICIAL_IDS = ["contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"]


def load_default() -> Dict[str, Any]:
    return json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))


def load_file(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scale_steps(c: Dict[str, Any]) -> List[int]:
    steps = []
    for s in c.get("scale") or []:
        try:
            steps.append(int(s["value"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(set(steps), reverse=True)


def validate_checklist(cl: Dict[str, Any]) -> List[str]:
    """Возвращает список ошибок (пустой — всё хорошо)."""
    errors: List[str] = []
    criteria = cl.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return ["В чек-листе нет критериев"]
    section_ids = {s.get("id") for s in cl.get("sections") or []}
    seen = set()
    for i, c in enumerate(criteria, 1):
        cid = c.get("id")
        where = f"Критерий {i} ({cid or 'без id'})"
        if not isinstance(cid, str) or not ID_RE.match(cid):
            errors.append(f"{where}: id — латиница в нижнем регистре, цифры и _, от 2 символов")
        elif cid in seen:
            errors.append(f"{where}: id повторяется")
        seen.add(cid)
        if not (c.get("name") or "").strip():
            errors.append(f"{where}: нет названия")
        if not (c.get("description") or "").strip():
            errors.append(f"{where}: нет формулировки (что значит «Да» или как ставить балл)")
        ctype = c.get("type")
        if ctype not in TYPES:
            errors.append(f"{where}: тип должен быть одним из {TYPES}")
        try:
            weight = float(c.get("weight", 0))
            if weight < 0:
                errors.append(f"{where}: вес не может быть отрицательным")
        except (TypeError, ValueError):
            errors.append(f"{where}: вес должен быть числом")
        if ctype == "scale":
            steps = scale_steps(c)
            if len(steps) < 2:
                errors.append(f"{where}: у шкалы должно быть минимум две ступени")
            elif steps[-1] < 0:
                errors.append(f"{where}: ступени шкалы не могут быть отрицательными")
            elif steps[0] <= 0:
                errors.append(f"{where}: верхняя ступень шкалы должна быть больше нуля")
        if ctype == "flag" and c.get("na_allowed"):
            errors.append(f"{where}: у флага грубой ошибки N/A не предусмотрен")
        if c.get("na_allowed") and not (c.get("na_condition") or "").strip():
            errors.append(f"{where}: N/A разрешён, но не задано условие N/A")
        if section_ids and c.get("section") and c.get("section") not in section_ids:
            errors.append(f"{where}: секция {c.get('section')!r} не описана в sections")
    return errors


def criterion_hash(c: Dict[str, Any], cl: Optional[Dict[str, Any]] = None) -> str:
    """Хэш всего, что влияет на ответ модели (вес не влияет: он пересчитывается кодом)."""
    relevant = {k: c.get(k) for k in ("id", "name", "type", "description", "na_allowed", "na_condition", "scale", "guidance")}
    if cl is not None:
        relevant["_threshold"] = cl.get("first_response_threshold_sec")
        relevant["_notes"] = cl.get("notes")
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def allowed_verdicts(c: Dict[str, Any]) -> List[str]:
    if c["type"] == "scale":
        verdicts = [str(v) for v in scale_steps(c)]
    else:
        verdicts = ["yes", "no"]
    if c.get("na_allowed") and c["type"] != "flag":
        verdicts.append("na")
    return verdicts


def verdict_to_value(c: Dict[str, Any], verdict: Any) -> Tuple[Any, bool]:
    """Вердикт модели → значение формата сдачи. Возвращает (value, ok).

    binary: True/False/None(N/A); scale: int/None; flag: True/False.
    """
    v = str(verdict).strip().lower() if verdict is not None else ""
    v = {"да": "yes", "нет": "no", "n/a": "na", "null": "na", "none": "na", "true": "yes", "false": "no", "иә": "yes", "жоқ": "no"}.get(v, v)
    if v == "na":
        if c.get("na_allowed") and c["type"] != "flag":
            return None, True
        return None, False
    if c["type"] == "scale":
        try:
            num = float(v)
        except ValueError:
            return None, False
        steps = scale_steps(c)
        if not steps:
            return None, False
        # ближайшая ступень; при равенстве — нижняя (консервативно)
        best = min(steps, key=lambda s: (abs(s - num), s))
        return int(best), True
    if v in ("yes", "no"):
        return v == "yes", True
    return None, False


def value_label(c: Dict[str, Any], value: Any) -> str:
    if value is None:
        return "N/A"
    if c["type"] == "scale":
        return f"{value} из {scale_steps(c)[0]}"
    return "Да" if value else "Нет"


def ensure_defaults(cl: Dict[str, Any]) -> Dict[str, Any]:
    cl = copy.deepcopy(cl)
    cl.setdefault("first_response_threshold_sec", 180)
    cl.setdefault("sections", [])
    for c in cl.get("criteria", []):
        c.setdefault("na_allowed", False)
        c.setdefault("na_condition", "")
        c.setdefault("guidance", "")
        c.setdefault("section", None)
        if c.get("type") == "flag":
            c["weight"] = 0
    return cl
