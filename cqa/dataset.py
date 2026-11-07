"""Получение и нормализация диалогов: API организаторов, файлы, вставленный текст."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .config import settings

SPEAKER_ALIASES = {
    "client": "client", "customer": "client", "user": "client", "клиент": "client",
    "operator": "operator", "agent": "operator", "оператор": "operator", "агент": "operator",
    "bot": "bot", "бот": "bot", "assistant": "bot",
    "system": "system", "система": "system", "event": "system",
}


def _with_group(url: str, group_id: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
    query.append(("group_id", group_id))
    return urlunparse(parts._replace(query=urlencode(query)))


def fetch_group(group_id: str, url: Optional[str] = None, timeout: int = 90) -> Dict[str, Any]:
    """GET к API организаторов. Возвращает объект data с records."""
    base = url or settings.org_api_url
    if not base:
        raise RuntimeError("Не задан ORG_API_URL в .env (ссылка на API организаторов).")
    response = requests.get(_with_group(base, group_id), timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("isSuccess", False):
        raise RuntimeError(f"API организаторов вернул ошибку: {payload.get('error')!r}")
    data = payload.get("data") or {}
    records = data.get("records")
    if not isinstance(records, list):
        raise RuntimeError("В ответе API нет списка data.records")
    data["records"] = [normalize_record(r) for r in records]
    return data


def cache_path(group_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", group_id)
    return settings.data_dir / f"{safe}.json"


def save_cache(group_id: str, data: Dict[str, Any]) -> Path:
    path = cache_path(group_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_cache(group_id: str) -> Optional[Dict[str, Any]]:
    path = cache_path(group_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get_group(group_id: str, refresh: bool = False, url: Optional[str] = None) -> Dict[str, Any]:
    """Данные набора: из кэша, если он есть, иначе из API (и сохраняет кэш)."""
    if not refresh:
        cached = load_cache(group_id)
        if cached:
            return cached
    data = fetch_group(group_id, url=url)
    save_cache(group_id, data)
    return data


def normalize_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    transcript = rec.get("transcript") or []
    if isinstance(transcript, str):  # на всякий случай: транскрипт строкой с JSON
        try:
            transcript = json.loads(transcript)
        except json.JSONDecodeError:
            transcript = parse_plain_text(transcript)
    turns = []
    for t in transcript:
        speaker = str(t.get("speaker", "")).strip().lower()
        turns.append({
            "timestamp": str(t.get("timestamp", "") or ""),
            "speaker": SPEAKER_ALIASES.get(speaker, speaker or "system"),
            "text": str(t.get("text", "") or ""),
        })
    channel = str(rec.get("channel") or "agent chat")
    return {
        "conversation_id": str(rec.get("conversation_id") or rec.get("id") or "").strip(),
        "channel": channel,
        "language": str(rec.get("language") or "").upper() or None,
        "transcript": turns,
        "audio_url": rec.get("audio_url") or None,
    }


_LINE_RE = re.compile(
    r"^\s*(?:\[(?P<ts>[0-9:.TZ+\-]+)\]\s*)?(?P<who>клиент|client|customer|оператор|operator|agent|бот|bot|система|system)\s*[:：]\s*(?P<text>.+)$",
    re.IGNORECASE,
)


def parse_plain_text(text: str) -> List[Dict[str, str]]:
    """Простой формат для вставки: «Клиент: ...», «Оператор: ...», «[01:15] Оператор: ...»."""
    turns: List[Dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if m:
            who = SPEAKER_ALIASES.get(m.group("who").lower(), "system")
            turns.append({"timestamp": m.group("ts") or "", "speaker": who, "text": m.group("text").strip()})
        elif turns:
            turns[-1]["text"] += "\n" + line.strip()
    return turns


def records_from_upload(payload: Any, default_id: str = "upload") -> List[Dict[str, Any]]:
    """Принимает ответ API организаторов, массив записей, одну запись или {transcript: [...]}."""
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"].get("records", [])
    if isinstance(payload, dict) and "records" in payload:
        payload = payload["records"]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Ожидался JSON-объект или массив диалогов")
    out = []
    for i, rec in enumerate(payload, 1):
        if not isinstance(rec, dict):
            continue
        norm = normalize_record(rec)
        if not norm["conversation_id"]:
            norm["conversation_id"] = f"{default_id}-{i:03d}"
        if not norm["transcript"]:
            raise ValueError(f"У диалога {norm['conversation_id']} пустой transcript")
        out.append(norm)
    return out
