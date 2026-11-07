"""Модели-судьи: любой OpenAI-совместимый /chat/completions.

Бесплатно работают Gemini (Google AI Studio), Cerebras, Groq, OpenRouter, GitHub Models, Mistral, а также
локальные Ollama / LM Studio. Несколько судей объединяются в панель: голоса раздаются по кругу,
поэтому лимиты бесплатных тарифов делятся между моделями, а оценка — это ансамбль независимых моделей.

- строгий JSON через response_format=json_schema, при отказе провайдера — json_object, затем просто текст;
- темп запросов (RPM) и число параллельных запросов на каждого судью;
- ретраи с паузой на 429/5xx/таймауты; судья с несуществующей моделью выключается до конца прогона;
- учёт токенов (включая кэшированные) и стоимости.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .config import JudgeSpec, Settings, settings as default_settings


class LLMError(RuntimeError):
    pass


class JudgeUnavailable(LLMError):
    """Судья недоступен (нет модели, нет ключа) — панель пропускает его."""


@dataclass
class LLMResult:
    data: Dict[str, Any]
    raw: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_sec: float = 0.0
    cost_usd: float = 0.0
    model: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Модель вернула не JSON: {text[:200]!r}")


class OpenAICompatibleClient:
    _MODES = ("json_schema", "json_object", "text")

    def __init__(self, spec: JudgeSpec, cfg: Optional[Settings] = None):
        self.spec = spec
        self.cfg = cfg or default_settings
        self.session = requests.Session()
        self._format_mode = "json_schema"
        self._temperature = self.cfg.llm_temperature
        self._reasoning = self.cfg.llm_reasoning_effort
        self._sem = threading.Semaphore(max(1, spec.concurrency))
        self._pace_lock = threading.Lock()
        self._next_slot = 0.0
        self.dead_reason: Optional[str] = None

    @property
    def model(self) -> str:
        return self.spec.label

    def _downgrade(self, sent_mode: str) -> None:
        nxt = self._MODES[min(self._MODES.index(sent_mode) + 1, len(self._MODES) - 1)]
        if self._MODES.index(self._format_mode) < self._MODES.index(nxt):
            self._format_mode = nxt

    def _pace(self) -> None:
        """Не чаще rpm запросов в минуту: бесплатные тарифы иначе отвечают 429."""
        if not self.spec.rpm or self.spec.rpm <= 0:
            return
        interval = 60.0 / self.spec.rpm
        with self._pace_lock:
            now = time.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + interval
        if slot > now:
            time.sleep(slot - now)

    def _payload(self, messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": self.spec.model, "messages": messages}
        mode = self._format_mode
        if mode == "json_schema" and not schema:
            mode = "json_object"
        if mode == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": schema}
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._reasoning:
            payload["reasoning_effort"] = self._reasoning
        if self.cfg.llm_max_tokens:
            payload["max_completion_tokens"] = self.cfg.llm_max_tokens
        payload["_mode"] = mode
        return payload

    def complete_json(self, messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None) -> LLMResult:
        if self.dead_reason:
            raise JudgeUnavailable(f"{self.model}: {self.dead_reason}")
        if not self.spec.api_key and not self.spec.local:
            self.dead_reason = "не задан API-ключ"
            raise JudgeUnavailable(f"{self.model}: не задан API-ключ провайдера {self.spec.provider}")
        with self._sem:
            return self._call(messages, schema)

    def _call(self, messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]]) -> LLMResult:
        url = f"{self.spec.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        attempt = 0
        last_error = ""
        while attempt <= self.cfg.llm_max_retries:
            payload = self._payload(messages, schema)
            sent_mode = payload.pop("_mode")
            self._pace()
            started = time.time()
            try:
                resp = self.session.post(url, headers=headers, json=payload, timeout=self.cfg.llm_timeout)
            except requests.RequestException as exc:
                last_error = f"сеть: {exc}"
                if self.spec.local and attempt == 0 and "connection" in str(exc).lower():
                    last_error = f"локальный сервер {self.spec.base_url} не отвечает (запущен ли Ollama?)"
                attempt += 1
                time.sleep(min(30, 2 ** attempt + random.random()))
                continue
            latency = time.time() - started
            low = resp.text.lower()
            if resp.status_code == 400 and "response_format" in payload and any(
                    k in low for k in ("response_format", "json_schema", "json_object", "not supported", "unsupported", "schema")):
                self._downgrade(sent_mode)  # провайдер не умеет такой формат — понижаем и сразу повторяем
                continue
            if resp.status_code == 400 and "temperature" in low and self._temperature is not None:
                self._temperature = None
                continue
            if resp.status_code == 400 and "reasoning" in low and self._reasoning:
                self._reasoning = None
                continue
            if resp.status_code in (401, 403):
                self.dead_reason = f"доступ запрещён (HTTP {resp.status_code}) — проверьте ключ"
                raise JudgeUnavailable(f"{self.model}: {self.dead_reason}: {resp.text[:200]}")
            if resp.status_code == 404:
                self.dead_reason = "модель не найдена — проверьте имя: python -m cqa models --provider " + self.spec.provider
                raise JudgeUnavailable(f"{self.model}: {self.dead_reason}")
            if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                attempt += 1
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(60, 5 * 2 ** attempt)
                time.sleep(min(90, wait + random.random()))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"{self.model}: HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            try:
                content = body["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                raise LLMError(f"{self.model}: неожиданный ответ провайдера: {str(body)[:300]}")
            try:
                data = extract_json(content)
            except LLMError as exc:
                last_error = str(exc)
                attempt += 1
                continue
            usage = body.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            cost = ((prompt_tokens - cached) * self.spec.price_input_per_m
                    + cached * self.spec.price_cached_input_per_m
                    + completion_tokens * self.spec.price_output_per_m) / 1_000_000
            return LLMResult(data=data, raw=content, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                             cached_tokens=cached, latency_sec=round(latency, 2), cost_usd=cost, model=self.model)
        raise LLMError(f"{self.model}: нет ответа после {self.cfg.llm_max_retries + 1} попыток. Последняя ошибка: {last_error}")

    def list_models(self) -> List[str]:
        headers = {"Authorization": f"Bearer {self.spec.api_key}"} if self.spec.api_key else {}
        resp = self.session.get(f"{self.spec.base_url}/models", headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") or data.get("models") or []
        return sorted(str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict))


class MockClient:
    """Офлайн-заглушка для тестов и проверки пайплайна без ключа. Это НЕ оценка."""

    dead_reason = None

    def __init__(self, label: str = "mock:mock"):
        self.model = label

    @staticmethod
    def _one(allowed: List[str], cid: str, turn: int) -> Dict[str, Any]:
        verdict = "yes"
        if allowed and all(a.lstrip("-").isdigit() or a == "na" for a in allowed):
            verdict = max((a for a in allowed if a != "na"), key=int)
        if cid == "critical":
            verdict = "no"
        return {"analysis": "mock", "verdict": verdict, "rationale": "Тестовая заглушка (mock), не оценка.",
                "evidence_turns": [turn], "confidence": 0.5}

    def complete_json(self, messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None) -> LLMResult:
        text = messages[-1]["content"]
        m = re.search(r"Первая реплика оператора: (\d+)", text)
        turn = int(m.group(1)) if m else 1
        if schema and schema.get("name") == "checklist_verdicts":
            props = schema["schema"]["properties"]["criteria"]["properties"]
            data = {"criteria": {cid: self._one(p["properties"]["verdict"].get("enum", []), cid, turn) for cid, p in props.items()}}
        else:
            allowed = schema["schema"]["properties"]["verdict"].get("enum", []) if schema else []
            cid_m = re.search(r"\(id: ([a-z0-9_]+)\)", text)
            data = self._one(allowed, cid_m.group(1) if cid_m else "", turn)
            if not schema:  # апелляция
                data = {"analysis": "mock", "recommendation": "keep", "suggested_verdict": "yes", "rationale": "mock", "evidence_turns": [turn]}
        return LLMResult(data=data, raw=json.dumps(data, ensure_ascii=False), model=self.model)


class JudgePanel:
    """Набор судей. Голоса раздаются по кругу, недоступные судьи пропускаются."""

    def __init__(self, judges: List[Any], mode: str = "external_api"):
        if not judges:
            raise ValueError("Не настроено ни одного судьи (LLM_JUDGES)")
        self.judges = judges
        self.mode = mode

    @property
    def model(self) -> str:
        return " + ".join(j.model for j in self.judges)

    def alive(self) -> List[Any]:
        live = [j for j in self.judges if not getattr(j, "dead_reason", None)]
        return live or self.judges

    def judge_for(self, i: int) -> Any:
        live = self.alive()
        return live[i % len(live)]

    def complete_json(self, messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None, start: int = 0) -> LLMResult:
        """Один вызов: пробует судей по кругу, пока кто-то не ответит."""
        last: Optional[Exception] = None
        live = self.alive()
        for k in range(len(live)):
            judge = live[(start + k) % len(live)]
            try:
                return judge.complete_json(messages, schema)
            except LLMError as exc:
                last = exc
        raise last or LLMError("Нет доступных судей")


def make_client(cfg: Optional[Settings] = None) -> JudgePanel:
    cfg = cfg or default_settings
    judges: List[Any] = []
    for spec in cfg.judges:
        judges.append(MockClient(spec.label) if spec.provider == "mock" else OpenAICompatibleClient(spec, cfg))
    return JudgePanel(judges, mode=cfg.model_mode)
