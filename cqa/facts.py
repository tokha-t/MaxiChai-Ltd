"""Детерминированные факты о диалоге: всё, что можно посчитать кодом точно, не доверяем модели.

Нумерация реплик — с 1 по исходному массиву transcript (включая клиента, бота и системные события),
как требует формат сдачи (evidence_turns).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_CHAT_TS = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2})(?:\.\d+)?$")
_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")

# Системные события (RU / KZ / EN)
P_TRANSFER = re.compile(r"эскалация на оператора|операторға жіберілді|transferred to an agent|агент присоединился|переведен от агента на представителя|agent joined", re.I)
P_IDENTITY = re.compile(r"личность клиента подтверждена|жеке басы расталды|identity verified|identity confirmed", re.I)
P_OP_CLOSED = re.compile(r"оператор закрыл чат|оператор чатты жапты|agent closed the chat|operator closed", re.I)
P_CLIENT_GONE = re.compile(
    r"клиент покинул чат|соединение с клиентом прервано|байланыс үзілді|connection to customer lost|"
    r"клиент завершил разговор|customer left|client left|customer disconnected|"
    # Между субъектом и событием может стоять пояснение: «сұрағын жазбай тұрып, ...».
    # Окончание утверждения исключает вопросы и отрицание последующей фразой.
    r"\bклиент\b[^.!?\n]*\bчаттан\s+(?:шықты|шығып\s+кетті)\b(?=\s*(?:[.!]|$))",
    re.I,
)
P_HOLD_END = re.compile(r"больше не находится на удержании|no longer on hold|removed from hold", re.I)
P_HOLD_START = re.compile(r"находится на удержании|placed on hold|is on hold", re.I)
P_REC_PAUSE = re.compile(r"приостановлен|paused|тоқтатыл", re.I)
P_REC_RESUME = re.compile(r"возобновлен|resumed|жалғастырыл", re.I)
# Предупреждение об ожидании в реплике оператора
# (оператор сам берёт паузу: «проверю», «уточню», «подождите»; просьба к клиенту «Проверьте раздел» — не предупреждение)
P_WAIT = re.compile(
    r"минут|секунд|подожд|ожида|момент|провер(?:ю|им|яю|ка)\b|уточн(?:ю|им|яю)\b|посмотр(?:ю|им)\b|"
    r"one moment|a moment|a minute|a second|let me check|let me look|i'll check|i will check|checking|please wait|hold on|bear with|"
    r"күте тұр|күтіңіз|күте тұрыңыз|тексерем|тексерейін|тексеріп жатырмын|қарап жатырмын|қарап шығайын|қарайын|сәл күт",
    re.I,
)
KZ_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def parse_ts(ts: str) -> Optional[float]:
    """MM:SS (минут может быть больше 59), H:MM:SS или ISO 8601 → секунды."""
    ts = (ts or "").strip()
    if not ts:
        return None
    m = _CHAT_TS.match(ts)
    if m:
        h, mi, s = m.groups()
        return float(int(h or 0) * 3600 + int(mi) * 60 + int(s))
    m = _ISO_TS.match(ts)
    if m:
        date, time, frac, tz = m.groups()
        frac = (frac or "")[:7]  # точка + максимум 6 цифр (Python 3.9 fromisoformat)
        if frac and len(frac) < 7:
            frac = frac.ljust(7, "0")
        tz = tz or "+00:00"
        if tz == "Z":
            tz = "+00:00"
        elif len(tz) == 5:
            tz = tz[:3] + ":" + tz[3:]
        try:
            return datetime.fromisoformat(f"{date}T{time}{frac}{tz}").timestamp()
        except ValueError:
            return None
    return None


def fmt_sec(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sec = int(round(sec))
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m:02d}:{s:02d}"


def detect_language(texts: List[str]) -> Optional[str]:
    joined = " ".join(texts)
    if not joined.strip():
        return None
    kz = sum(1 for ch in joined if ch in KZ_LETTERS)
    latin = sum(1 for ch in joined if "a" <= ch.lower() <= "z")
    cyr = sum(1 for ch in joined if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    if kz >= 2:
        return "KZ"
    if latin > cyr:
        return "EN"
    if cyr:
        return "RU"
    return None


def build_turns(conv: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Реплики с номером (с 1) и относительным временем от начала диалога."""
    raw = conv.get("transcript") or []
    abs_times = [parse_ts(t.get("timestamp", "")) for t in raw]
    base = next((x for x in abs_times if x is not None), None)
    turns = []
    for i, t in enumerate(raw, 1):
        rel = None if abs_times[i - 1] is None or base is None else abs_times[i - 1] - base
        turns.append({
            "n": i,
            "speaker": t.get("speaker", "system"),
            "text": t.get("text", ""),
            "timestamp": t.get("timestamp", ""),
            "rel_sec": rel,
            "rel": fmt_sec(rel),
        })
    return turns


def compute_facts(conv: Dict[str, Any], threshold_sec: int = 180, long_pause_sec: int = 120) -> Dict[str, Any]:
    turns = build_turns(conv)
    is_call = "call" in (conv.get("channel") or "").lower() or "voice" in (conv.get("channel") or "").lower()

    def first(pred, start: int = 0) -> Optional[Dict[str, Any]]:
        return next((t for t in turns[start:] if pred(t)), None)

    transfer = first(lambda t: t["speaker"] == "system" and bool(P_TRANSFER.search(t["text"])))
    first_client = first(lambda t: t["speaker"] == "client")
    first_op = first(lambda t: t["speaker"] == "operator")

    # Сегменты удержания (звонки): реплики оператора там — внутренняя консультация
    hold_ranges: List[Tuple[int, int]] = []
    hold_start = None
    for t in turns:
        if t["speaker"] != "system":
            continue
        if P_HOLD_END.search(t["text"]):
            if hold_start is not None:
                hold_ranges.append((hold_start, t["n"]))
                hold_start = None
        elif P_HOLD_START.search(t["text"]):
            hold_start = t["n"]
    if hold_start is not None:
        hold_ranges.append((hold_start, turns[-1]["n"] if turns else hold_start))

    def in_hold(n: int) -> bool:
        return any(a < n < b for a, b in hold_ranges)

    rec_gaps: List[Tuple[int, Optional[int]]] = []
    pause_at = None
    for t in turns:
        if t["speaker"] == "system" and P_REC_PAUSE.search(t["text"]):
            pause_at = t["n"]
        elif t["speaker"] == "system" and P_REC_RESUME.search(t["text"]) and pause_at is not None:
            rec_gaps.append((pause_at, t["n"]))
            pause_at = None
    if pause_at is not None:
        rec_gaps.append((pause_at, None))

    # Первая реакция оператора
    ref = transfer if transfer and (not first_op or transfer["n"] < first_op["n"]) else first_client
    first_delay = None
    if first_op and ref and first_op["rel_sec"] is not None and ref["rel_sec"] is not None:
        first_delay = max(0.0, first_op["rel_sec"] - ref["rel_sec"])

    # Паузы: от самого раннего неотвеченного сообщения клиента до ответа оператора
    pauses = []
    pending = None
    last_op_before = None
    start_idx = (first_op["n"] - 1) if first_op else len(turns)
    for t in turns[start_idx:]:
        if in_hold(t["n"]):
            continue
        if t["speaker"] == "client" and pending is None:
            pending = t
        elif t["speaker"] == "operator":
            if pending and t["rel_sec"] is not None and pending["rel_sec"] is not None:
                delay = t["rel_sec"] - pending["rel_sec"]
                if delay >= long_pause_sec:
                    warned = bool(last_op_before and P_WAIT.search(last_op_before["text"]))
                    pauses.append({
                        "client_turn": pending["n"], "operator_turn": t["n"], "delay_sec": round(delay),
                        "warned": warned, "warn_turn": last_op_before["n"] if warned and last_op_before else None,
                        "exceeds_threshold": delay > threshold_sec,
                    })
            pending = None
            last_op_before = t

    identity = [t["n"] for t in turns if t["speaker"] == "system" and P_IDENTITY.search(t["text"])]
    op_closed = first(lambda t: t["speaker"] == "system" and bool(P_OP_CLOSED.search(t["text"])))
    client_gone = first(lambda t: t["speaker"] == "system" and bool(P_CLIENT_GONE.search(t["text"])))
    op_turns = [t for t in turns if t["speaker"] == "operator" and not in_hold(t["n"])]
    last_op = op_turns[-1] if op_turns else None
    client_turns = [t for t in turns if t["speaker"] == "client"]
    last_client = client_turns[-1] if client_turns else None
    known_before_op = [t["n"] for t in turns if t["speaker"] == "client" and (not first_op or t["n"] < first_op["n"])]

    detected = detect_language([t["text"] for t in client_turns])
    return {
        "is_call": is_call,
        "channel": conv.get("channel"),
        "language": conv.get("language"),
        "client_language_detected": detected,
        "n_turns": len(turns),
        "threshold_sec": threshold_sec,
        "transfer_turn": transfer["n"] if transfer else None,
        "transfer_time": transfer["rel"] if transfer else None,
        "first_client_turn": first_client["n"] if first_client else None,
        "first_operator_turn": first_op["n"] if first_op else None,
        "first_operator_time": first_op["rel"] if first_op else None,
        "first_response_ref_turn": ref["n"] if ref else None,
        "first_response_delay_sec": round(first_delay) if first_delay is not None else None,
        "first_response_exceeds": bool(first_delay is not None and first_delay > threshold_sec),
        "long_pauses": pauses,
        "identity_verified_turns": identity,
        "operator_closed_turn": op_closed["n"] if op_closed else None,
        "client_left_turn": client_gone["n"] if client_gone else None,
        "last_operator_turn": last_op["n"] if last_op else None,
        "last_turn_speaker": turns[-1]["speaker"] if turns else None,
        "operator_after_last_client": bool(last_op and last_client and last_op["n"] > last_client["n"]),
        "client_turns_before_operator": known_before_op,
        "hold_ranges": [list(r) for r in hold_ranges],
        "recording_gaps": [list(r) for r in rec_gaps],
        "operator_turn_count": len(op_turns),
    }


def facts_to_text(facts: Dict[str, Any]) -> str:
    """Факты для промпта (по-русски, кратко)."""
    lines = []
    kind = "звонок (транскрипт устной речи, возможны ошибки распознавания)" if facts["is_call"] else "чат"
    lines.append(f"- Канал: {kind}. Язык диалога по данным: {facts.get('language') or '—'}; язык клиента по тексту: {facts.get('client_language_detected') or '—'}.")
    if facts.get("transfer_turn"):
        lines.append(f"- Перевод на оператора: реплика {facts['transfer_turn']} ({facts['transfer_time']}).")
    if facts.get("first_operator_turn"):
        delay = facts.get("first_response_delay_sec")
        verdict = "ПРЕВЫШЕН порог" if facts.get("first_response_exceeds") else "в пределах порога"
        lines.append(
            f"- Первая реплика оператора: {facts['first_operator_turn']} ({facts['first_operator_time']}); "
            f"первая реакция через {delay} с после реплики {facts['first_response_ref_turn']} (порог {facts['threshold_sec']} с) — {verdict}."
        )
    else:
        lines.append("- Оператор в диалоге не написал ни одной реплики.")
    if facts.get("client_turns_before_operator"):
        lines.append(f"- Сообщения клиента до первой реплики оператора (уже известные детали): реплики {', '.join(map(str, facts['client_turns_before_operator']))}.")
    if facts.get("long_pauses"):
        for p in facts["long_pauses"]:
            warn = f"оператор предупредил об ожидании в реплике {p['warn_turn']}" if p["warned"] else "предупреждения об ожидании не было"
            over = ", превышает порог" if p["exceeds_threshold"] else ""
            lines.append(f"- Долгая пауза: ответ оператора {p['operator_turn']} через {p['delay_sec']} с после реплики клиента {p['client_turn']}{over}; {warn}.")
    else:
        lines.append("- Пауз ответа оператора длиннее 2 минут нет.")
    if facts.get("identity_verified_turns"):
        lines.append(f"- Системное подтверждение личности клиента: реплика {', '.join(map(str, facts['identity_verified_turns']))}.")
    else:
        lines.append("- Системного сообщения о подтверждении личности нет.")
    if facts.get("operator_closed_turn"):
        lines.append(f"- Оператор закрыл чат: реплика {facts['operator_closed_turn']}.")
    if facts.get("client_left_turn"):
        lines.append(f"- Клиент ушёл / связь прервалась / клиент завершил разговор: реплика {facts['client_left_turn']}.")
    else:
        lines.append("- Системного события об уходе клиента или обрыве связи нет: у оператора была возможность завершить диалог.")
    if facts.get("last_operator_turn"):
        tail = "после неё клиент больше не писал" if facts.get("operator_after_last_client") else "после неё были сообщения клиента без ответа оператора"
        lines.append(f"- Последняя реплика оператора: {facts['last_operator_turn']} ({tail}). Последняя реплика диалога — {facts.get('last_turn_speaker')}.")
    for a, b in facts.get("hold_ranges") or []:
        lines.append(f"- Клиент на удержании между репликами {a} и {b}: реплики «оператора» там — внутренняя консультация с коллегой, клиент их не слышит; их не оценивай как общение с клиентом.")
    for a, b in facts.get("recording_gaps") or []:
        lines.append(f"- Запись приостанавливалась с реплики {a} до {b or 'конца'}: часть разговора могла не попасть в транскрипт.")
    return "\n".join(lines)
