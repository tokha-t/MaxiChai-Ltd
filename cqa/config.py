"""Настройки из .env и переменных окружения. Работает на Python 3.9+."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _clean_value(value)
        if key and key not in os.environ:
            os.environ[key] = value


def _clean_value(value: str) -> str:
    """Убирает кавычки и комментарий в конце строки: LLM_VOTES=3   # голосов → 3."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


_load_dotenv(ROOT / ".env")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Пресеты OpenAI-совместимых провайдеров. free — бесплатный тариф (стоимость считаем 0),
# rpm — темп запросов по умолчанию (бесплатные тарифы режут частоту), local — модель на своём компьютере.
PROVIDERS = {
    "openai": {"base": "https://api.openai.com/v1", "keys": ["OPENAI_API_KEY", "LLM_API_KEY"], "rpm": 0, "free": False, "local": False},
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "rpm": 5, "free": True, "local": False},
    "cerebras": {"base": "https://api.cerebras.ai/v1", "keys": ["CEREBRAS_API_KEY"], "rpm": 5, "free": True, "local": False},
    "groq": {"base": "https://api.groq.com/openai/v1", "keys": ["GROQ_API_KEY"], "rpm": 30, "free": True, "local": False},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "keys": ["OPENROUTER_API_KEY"], "rpm": 20, "free": True, "local": False},
    "github": {"base": "https://models.github.ai/inference", "keys": ["GITHUB_TOKEN"], "rpm": 10, "free": True, "local": False},
    "mistral": {"base": "https://api.mistral.ai/v1", "keys": ["MISTRAL_API_KEY"], "rpm": 60, "free": True, "local": False},
    "ollama": {"base": "http://localhost:11434/v1", "keys": [], "rpm": 0, "free": True, "local": True, "concurrency": 1},
    "lmstudio": {"base": "http://localhost:1234/v1", "keys": [], "rpm": 0, "free": True, "local": True, "concurrency": 1},
    "custom": {"base": None, "keys": ["LLM_API_KEY"], "rpm": 0, "free": False, "local": False},
    "mock": {"base": None, "keys": [], "rpm": 0, "free": True, "local": True},
}


@dataclass
class JudgeSpec:
    provider: str
    model: str
    base_url: str
    api_key: Optional[str]
    rpm: float
    concurrency: int
    free: bool
    local: bool
    price_input_per_m: float
    price_cached_input_per_m: float
    price_output_per_m: float

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Settings:
    # API организаторов: ссылка без параметра group_id (он добавляется автоматически)
    org_api_url: Optional[str]
    # LLM (любой OpenAI-совместимый провайдер: OpenAI, OpenRouter, Ollama, LM Studio...)
    llm_provider: str
    llm_base_url: str
    llm_api_key: Optional[str]
    llm_model: str
    llm_votes: int
    llm_concurrency: int
    llm_timeout: int
    llm_max_retries: int
    llm_reasoning_effort: Optional[str]
    llm_temperature: Optional[float]
    llm_max_tokens: Optional[int]
    llm_mode: str            # batch — все критерии одним вызовом; per_criterion — вызов на критерий
    judges: List[JudgeSpec]  # судьи: голоса раздаются по кругу
    # Цены за 1M токенов — для подсчёта стоимости одной оценки
    price_input_per_m: float
    price_cached_input_per_m: float
    price_output_per_m: float
    # Сдача
    team_name: str
    model_mode: str
    # Хранилище
    data_dir: Path
    db_path: Path
    runs_dir: Path


def _judge(provider: str, model: str, concurrency_default: int) -> JudgeSpec:
    provider = provider.strip().lower()
    preset = PROVIDERS.get(provider, PROVIDERS["custom"])
    up = provider.upper()
    base = _env(f"{up}_BASE_URL") or preset["base"] or _env("LLM_BASE_URL", "https://api.openai.com/v1")
    key = None
    for name in preset["keys"]:
        key = _env(name)
        if key:
            break
    free = preset["free"]
    return JudgeSpec(
        provider=provider,
        model=model.strip(),
        base_url=(base or "").rstrip("/"),
        api_key=key,
        rpm=float(_env_float(f"{up}_RPM", preset["rpm"]) or 0),
        concurrency=max(1, _env_int(f"{up}_CONCURRENCY", preset.get("concurrency", concurrency_default))),
        free=free,
        local=preset["local"],
        price_input_per_m=0.0 if free else (_env_float("PRICE_INPUT_PER_M", 1.0) or 0.0),
        price_cached_input_per_m=0.0 if free else (_env_float("PRICE_CACHED_INPUT_PER_M", 0.05) or 0.0),
        price_output_per_m=0.0 if free else (_env_float("PRICE_OUTPUT_PER_M", 5.0) or 0.0),
    )


def parse_judges(spec: Optional[str], fallback_provider: str, fallback_model: str, concurrency: int) -> List[JudgeSpec]:
    """LLM_JUDGES="gemini:gemini-3.6-flash,cerebras:gpt-oss-120b,ollama:gpt-oss:20b" (модель — всё после первого двоеточия)."""
    judges = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            provider, model = item.split(":", 1)
        else:
            provider, model = fallback_provider, item
        judges.append(_judge(provider, model, concurrency))
    if not judges:
        judges.append(_judge(fallback_provider, fallback_model, concurrency))
    return judges


def mode_for(judges: List[JudgeSpec]) -> str:
    if all(j.local for j in judges):
        return "local"
    if any(j.local for j in judges):
        return "hybrid"
    return "external_api"


def with_judges(cfg: "Settings", spec: str) -> "Settings":
    """Копия настроек с другим набором судей (флаг --judges у CLI)."""
    import dataclasses
    judges = parse_judges(spec, cfg.llm_provider, "", cfg.llm_concurrency)
    return dataclasses.replace(cfg, judges=judges, llm_model=" + ".join(j.label for j in judges),
                               model_mode=_env("MODEL_MODE") or mode_for(judges))


def load_settings() -> Settings:
    data_dir = Path(_env("CQA_DATA_DIR", str(ROOT / "data")))
    max_tokens_raw = _env("LLM_MAX_TOKENS")
    provider = (_env("LLM_PROVIDER", "gemini") or "gemini").lower()
    model = _env("LLM_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"
    concurrency = max(1, _env_int("LLM_CONCURRENCY", 8))
    judges = parse_judges(_env("LLM_JUDGES"), provider, model, concurrency)
    if provider == "mock":
        judges = [_judge("mock", "mock", concurrency)]
    default_mode = mode_for(judges)
    return Settings(
        org_api_url=_env("ORG_API_URL"),
        llm_provider=provider,
        llm_base_url=(_env("LLM_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
        llm_model=" + ".join(j.label for j in judges),
        llm_votes=max(1, _env_int("LLM_VOTES", 3)),
        llm_concurrency=concurrency,
        llm_timeout=max(10, _env_int("LLM_TIMEOUT", 120)),
        llm_max_retries=max(0, _env_int("LLM_MAX_RETRIES", 4)),
        llm_reasoning_effort=_env("LLM_REASONING_EFFORT"),
        llm_temperature=_env_float("LLM_TEMPERATURE", None),
        llm_max_tokens=int(max_tokens_raw) if max_tokens_raw and max_tokens_raw.isdigit() else None,
        llm_mode=(_env("LLM_MODE", "batch") or "batch").lower(),
        judges=judges,
        price_input_per_m=_env_float("PRICE_INPUT_PER_M", 1.0) or 0.0,
        price_cached_input_per_m=_env_float("PRICE_CACHED_INPUT_PER_M", 0.05) or 0.0,
        price_output_per_m=_env_float("PRICE_OUTPUT_PER_M", 5.0) or 0.0,
        team_name=_env("TEAM_NAME", "MaxiChai") or "MaxiChai",
        model_mode=_env("MODEL_MODE", default_mode) or default_mode,
        data_dir=data_dir,
        db_path=Path(_env("CQA_DB_PATH", str(data_dir / "cqa.db"))),
        runs_dir=Path(_env("CQA_RUNS_DIR", str(ROOT / "runs"))),
    )


settings = load_settings()
