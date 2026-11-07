"""Сборка промптов из конфигурации чек-листа.

Порядок сообщений рассчитан на кэширование префикса у провайдера: системный промпт,
затем общий для всех критериев блок (факты + транскрипт), и только в конце — блок критерия.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .checklist import allowed_verdicts, scale_steps

SPEAKER_RU = {"client": "КЛИЕНТ", "operator": "ОПЕРАТОР", "bot": "БОТ", "system": "СИСТЕМА"}

RULES = """Ты — старший специалист контроля качества (QA) контакт-центра брокерской компании Freedom Broker.
Ты оцениваешь работу ОПЕРАТОРА в диалоге с клиентом по критериям чек-листа.

Правила:
1. Оценивай только оператора. Реплики бота (AI-ассистент AIM) и системные события — контекст: они показывают, что клиент уже сообщил и что произошло, но заслуги и ошибки бота оператору не засчитываются.
2. Опирайся только на текст диалога и на блок «Факты». Факты посчитаны кодом точно (время, номера реплик, системные события) — не пересчитывай их и не спорь с ними.
3. Не выдумывай. Если чего-то нет в диалоге, считай, что этого не было. Если данных мало для уверенного вывода, выбери вариант, который подтверждается репликами, прямо напиши, чего не хватает, и снизь confidence.
4. Применяй формулировку критерия буквально, как строгий, но справедливый эксперт. N/A ставь только если он разрешён для критерия и его условие действительно выполнено. «Нет» — действие было нужно, но оператор его пропустил или сделал неправильно.
5. Диалоги бывают на русском, казахском и английском. Понимай их одинаково хорошо. Названия разделов приложения на русском внутри казахской или английской речи — норма.
6. В звонках транскрипт получен распознаванием речи: возможны искажения слов, а имена и данные замаскированы («[Имя скрыто]», «[Персональные данные скрыты]») — маскирование не ошибка оператора. Оценивай смысл.
7. Теги и вложения в оценку не входят.
8. Реплики пронумерованы: [N]. В evidence_turns указывай только номера из транскрипта: реплики, на которых основан вывод. Если действие пропущено — номера реплик, где его ожидали. Для N/A — реплику, которая показывает, почему критерий неприменим.
9. Обоснование (rationale) — по-русски, 1–2 коротких предложения, со ссылкой на номера реплик и конкретный факт. Например: «Оператор поздоровался и представился в первой реплике [10]».
10. Каждый критерий оценивай независимо, строго по его формулировке: одна и та же ошибка оператора может влиять на несколько критериев, если это следует из их формулировок."""

SYSTEM_PROMPT = RULES + """

Оценивай один критерий. Ответ — строго JSON-объект без пояснений вокруг:
{"analysis": "<краткий разбор по шагам, до 6 предложений>", "verdict": "<одно из разрешённых значений>", "rationale": "<обоснование по-русски>", "evidence_turns": [<номера реплик>], "confidence": <число от 0 до 1>}"""

BATCH_SYSTEM_PROMPT = RULES + """

Оцени диалог сразу по всем перечисленным критериям. Ответ — строго JSON-объект без пояснений вокруг:
{"criteria": {"<id критерия>": {"analysis": "<краткий разбор, до 4 предложений>", "verdict": "<одно из разрешённых значений этого критерия>", "rationale": "<обоснование по-русски>", "evidence_turns": [<номера реплик>], "confidence": <число от 0 до 1>}, ...}}
Ключи в "criteria" — ровно id перечисленных критериев, по одному разу."""


def render_transcript(turns: List[Dict[str, Any]], max_chars_per_turn: int = 1500) -> str:
    lines = []
    for t in turns:
        text = (t["text"] or "").replace("\n", " ").strip()
        if len(text) > max_chars_per_turn:
            text = text[:max_chars_per_turn] + " …[обрезано]"
        who = SPEAKER_RU.get(t["speaker"], t["speaker"].upper())
        lines.append(f"[{t['n']}] {t['rel']} {who}: {text}")
    return "\n".join(lines)


def render_context(conv: Dict[str, Any], turns: List[Dict[str, Any]], facts_text: str, checklist: Dict[str, Any]) -> str:
    notes = (checklist.get("notes") or "").strip()
    head = [
        f"ДИАЛОГ {conv.get('conversation_id')} — канал: {conv.get('channel')}, язык: {conv.get('language') or '—'}.",
    ]
    if notes:
        head.append(f"Общие правила чек-листа: {notes}")
    return "\n".join(head) + "\n\nФАКТЫ (посчитаны кодом, точны):\n" + facts_text + "\n\nТРАНСКРИПТ:\n" + render_transcript(turns)


def render_criterion(c: Dict[str, Any], batch: bool = False) -> str:
    lines = [f"КРИТЕРИЙ «{c['name']}» (id: {c['id']})."]
    if c["type"] == "scale":
        lines.append(f"Тип: шкала. {c.get('description', '').strip()}")
        lines.append("Ступени шкалы (выбери ровно одну):")
        for s in sorted(c.get("scale") or [], key=lambda x: -int(x["value"])):
            lines.append(f"  {int(s['value'])} — {s.get('label', '').strip()}")
    elif c["type"] == "flag":
        lines.append("Тип: флаг грубой ошибки (Да/Нет), баллов не даёт.")
        lines.append(f"Формулировка: {c.get('description', '').strip()}")
    else:
        lines.append("Тип: Да/Нет" + ("/N/A" if c.get("na_allowed") else "") + ".")
        lines.append(f"Формулировка: {c.get('description', '').strip()}")
    if c.get("na_allowed") and c["type"] != "flag":
        lines.append(f"N/A разрешён только если: {c.get('na_condition', '').strip()}.")
    else:
        lines.append("N/A для этого критерия НЕ разрешён — выбери ответ по существу.")
    if (c.get("guidance") or "").strip():
        lines.append(f"Как оценивать: {c['guidance'].strip()}")
    verdicts = allowed_verdicts(c)
    legend = []
    if c["type"] != "scale":
        legend.append('"yes" = Да, "no" = Нет')
    if "na" in verdicts:
        legend.append('"na" = N/A')
    lines.append(f"Разрешённые значения verdict: {', '.join(verdicts)}" + (f" ({'; '.join(legend)})" if legend else "") + ".")
    if not batch:
        lines.append("Сначала найди нужные реплики, потом вынеси вердикт. Верни только JSON.")
    return "\n".join(lines)


def build_messages(conv: Dict[str, Any], turns: List[Dict[str, Any]], facts_text: str, checklist: Dict[str, Any], criterion: Dict[str, Any]) -> List[Dict[str, str]]:
    context = render_context(conv, turns, facts_text, checklist)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context + "\n\n=====\n" + render_criterion(criterion)},
    ]


def _verdict_object(criterion: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "verdict": {"type": "string", "enum": allowed_verdicts(criterion)},
            "rationale": {"type": "string"},
            "evidence_turns": {"type": "array", "items": {"type": "integer"}},
            "confidence": {"type": "number"},
        },
        "required": ["analysis", "verdict", "rationale", "evidence_turns", "confidence"],
        "additionalProperties": False,
    }


def response_schema(criterion: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": "criterion_verdict", "strict": True, "schema": _verdict_object(criterion)}


def build_batch_messages(conv: Dict[str, Any], turns: List[Dict[str, Any]], facts_text: str, checklist: Dict[str, Any],
                         criteria: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Все критерии одним вызовом: в 8 раз меньше запросов — важно для бесплатных тарифов."""
    context = render_context(conv, turns, facts_text, checklist)
    blocks = [f"--- {i}. " + render_criterion(c, batch=True) for i, c in enumerate(criteria, 1)]
    ids = ", ".join(c["id"] for c in criteria)
    tail = (f"Верни JSON, где в \"criteria\" ровно ключи: {ids}. "
            "Для каждого критерия сначала найди нужные реплики, потом вынеси вердикт.")
    return [
        {"role": "system", "content": BATCH_SYSTEM_PROMPT},
        {"role": "user", "content": context + "\n\n=====\nКРИТЕРИИ ЧЕК-ЛИСТА:\n\n" + "\n\n".join(blocks) + "\n\n" + tail},
    ]


def batch_response_schema(criteria: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "name": "checklist_verdicts",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "object",
                    "properties": {c["id"]: _verdict_object(c) for c in criteria},
                    "required": [c["id"] for c in criteria],
                    "additionalProperties": False,
                }
            },
            "required": ["criteria"],
            "additionalProperties": False,
        },
    }


APPEAL_PROMPT = """Ты — старший QA-эксперт, который рассматривает апелляцию оператора на оценку одного критерия.
Перечитай диалог, исходную оценку и аргумент апелляции. Реши, права ли исходная оценка по формулировке критерия.
Не соглашайся с апелляцией только потому, что оператор настаивает; не защищай исходную оценку, если она противоречит репликам.
Ответ — строго JSON:
{"analysis": "<разбор>", "recommendation": "keep" | "change", "suggested_verdict": "<одно из разрешённых значений>", "rationale": "<1–2 предложения по-русски для проверяющего>", "evidence_turns": [<номера реплик>]}"""


def build_appeal_messages(conv: Dict[str, Any], turns: List[Dict[str, Any]], facts_text: str, checklist: Dict[str, Any],
                          criterion: Dict[str, Any], original: Dict[str, Any], comment: str) -> List[Dict[str, str]]:
    context = render_context(conv, turns, facts_text, checklist)
    orig = (f"ИСХОДНАЯ ОЦЕНКА: verdict={original.get('verdict')}; обоснование: {original.get('rationale')}; "
            f"реплики: {original.get('evidence_turns')}.")
    return [
        {"role": "system", "content": APPEAL_PROMPT},
        {"role": "user", "content": context + "\n\n=====\n" + render_criterion(criterion) + "\n\n" + orig +
         f"\nАРГУМЕНТ АПЕЛЛЯЦИИ: {comment.strip() or '(без комментария)'}"},
    ]
