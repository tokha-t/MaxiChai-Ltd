"""Скоринг — чистый код без LLM.

Правила из чек-листа:
- «Да» даёт полный вес, «Нет» — ноль; шкала пересчитывается пропорционально весу
  (верхняя ступень = полный вес);
- N/A исключается, его вес пропорционально перераспределяется между применимыми критериями
  (итог = набранные баллы / сумма весов применимых × 100);
- итог округляется до двух знаков (ROUND_HALF_UP, считаем в точных дробях);
- нет применимых критериев с весом → итог не рассчитывается (None);
- грубая ошибка — отдельный флаг, итог не обнуляет.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, Optional

from .checklist import scale_steps


def round2(value: Fraction) -> float:
    with localcontext() as ctx:
        ctx.prec = 50
        d = Decimal(value.numerator) / Decimal(value.denominator)
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _weight(c: Dict[str, Any]) -> Fraction:
    try:
        return Fraction(str(c.get("weight", 0)))
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def criterion_points(c: Dict[str, Any], value: Any) -> Optional[Fraction]:
    """Баллы до перераспределения; None — критерий неприменим (N/A) или без веса."""
    if c["type"] == "flag" or value is None:
        return None
    w = _weight(c)
    if w == 0:
        return None
    if c["type"] == "scale":
        top = scale_steps(c)[0]
        return Fraction(int(value), top) * w
    return w if value else Fraction(0)


def compute_score(checklist: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    earned = Fraction(0)
    applicable = Fraction(0)
    per = {}
    critical = False
    flags = []
    for c in checklist["criteria"]:
        cid = c["id"]
        value = values.get(cid)
        if c["type"] == "flag":
            if value is True:
                critical = True
                flags.append(cid)
            per[cid] = {"applicable": False, "points": None, "max_points": None, "effective_weight": None}
            continue
        pts = criterion_points(c, value)
        if pts is None:
            per[cid] = {"applicable": False, "points": None, "max_points": float(_weight(c)), "effective_weight": 0.0}
            continue
        earned += pts
        applicable += _weight(c)
        per[cid] = {"applicable": True, "points_exact": pts, "max_points": float(_weight(c))}
    for c in checklist["criteria"]:
        info = per[c["id"]]
        if info.get("applicable"):
            pts = info.pop("points_exact")
            info["points"] = round2(pts)
            # «Вес после перераспределения»: сколько итоговых баллов из 100 даёт критерий
            info["effective_weight"] = round2(_weight(c) / applicable * 100)
            info["score_contribution"] = round2(pts / applicable * 100)
    final = round2(earned / applicable * 100) if applicable > 0 else None

    sections = []
    for s in checklist.get("sections") or []:
        s_earned = Fraction(0)
        s_app = Fraction(0)
        s_flags = []
        for c in checklist["criteria"]:
            if c.get("section") != s["id"]:
                continue
            if c["type"] == "flag":
                if values.get(c["id"]) is True:
                    s_flags.append(c["id"])
                continue
            pts = criterion_points(c, values.get(c["id"]))
            if pts is not None:
                s_earned += pts
                s_app += _weight(c)
        sections.append({
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "score": round2(s_earned / s_app * 100) if s_app > 0 else None,
            "earned": round2(s_earned),
            "max": round2(s_app),
            "flags": s_flags,
        })
    return {
        "final_score": final,
        "critical_flag": critical,
        "critical_criteria": flags,
        "earned": round2(earned),
        "applicable_weight": round2(applicable),
        "criteria": per,
        "sections": sections,
    }
