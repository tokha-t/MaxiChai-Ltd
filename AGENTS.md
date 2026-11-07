# AGENTS.md — правила для Codex и других агентов

Проект: **CQA — Conversation Quality Analytics** для хакатона Freedom Broker.
Система оценивает работу оператора в чатах и звонках по настраиваемому чек-листу (LLM + детерминированные правила),
показывает баллы с обоснованием и ссылкой на реплику, поддерживает апелляции и пересмотр.

## Структура

```
cqa/                 ЯДРО (Python, протестировано). Не переписывать без необходимости.
  config.py          настройки из .env
  dataset.py         API организаторов, загрузка файлов/текста, нормализация
  facts.py           факты кодом: тайминги, первая реакция, паузы, системные события
  checklist.py       чек-лист как конфиг: проверка, хэши критериев, вердикт → значение
  prompts.py         сборка промптов из конфига критерия
  llm.py             модели-судьи: OpenAI-совместимые провайдеры, панель судей, mock для тестов
  evaluator.py       оценка: все критерии одним вызовом × голоса судей → агрегация → скоринг; апелляции ИИ
  scoring.py         скоринг чистым кодом (веса, N/A, округление ROUND_HALF_UP)
  export.py          results.json формата 1.0
  validate.py        валидатор results.json
  store.py           SQLite: диалоги, версии чек-листов, оценки, апелляции
  cli.py             python -m cqa fetch|evaluate|validate|show
server/app.py        FastAPI: REST API + раздача web/
web/                 ИНТЕРФЕЙС — делаешь ты (HTML + CSS + vanilla JS, без сборки)
config/checklists/default.json   исходный чек-лист организаторов (8 критериев)
results/dev|prod/results.json    файлы сдачи
tests/               pytest
```

## Жёсткие правила

1. **Не меняй логику ядра** (`cqa/scoring.py`, `cqa/export.py`, `cqa/validate.py`, `cqa/evaluator.py`) без явной просьбы.
   Если для интерфейса не хватает данных — добавь эндпоинт в `server/app.py`, используя функции ядра.
2. После любых изменений Python запускай `python -m pytest -q` — все тесты должны проходить.
3. Интерфейс: **только HTML + CSS + JavaScript (ES-модули), без сборщика, без npm, без CDN.**
   Причина: интерфейс потом встраивается в Dynamics 365 как HTML web resource.
   - Все запросы к серверу — через один модуль `web/js/api.js`. База API берётся из `window.CQA_API_BASE` (по умолчанию `""`, тот же origin).
   - Роутинг через hash: `#/dialogs`, `#/dialog/<id>`, `#/checklist`, `#/appeals`, `#/dashboard`.
   - Экраны оформлены как независимые модули `web/js/views/*.js` с функцией `render(root, params)`.
   - Параметры можно передать через URL: `index.html?conversationId=...` открывает карточку диалога (так это будет работать внутри D365).
   - Без localStorage для важных данных: всё состояние хранится на сервере.
4. Язык интерфейса — русский. Термины: «Да», «Нет», «N/A», «Грубая ошибка», «Апелляция», «Проверяющий».
5. Не коммить `.env`, `data/`, ключи API.
6. Код должен работать на Python 3.9+ (`from __future__ import annotations`, без `match`, без `X | None` в рантайме).

## Запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить ORG_API_URL и GEMINI_API_KEY
uvicorn server.app:app --reload --port 8000
# интерфейс: http://localhost:8000   документация API: http://localhost:8000/docs
```

Для разработки интерфейса без ключа: `LLM_PROVIDER=mock` в `.env` (оценки будут заглушками).

## Модель данных, которую получает интерфейс

`GET /api/conversations/{id}` возвращает:

```jsonc
{
  "conversation": {"conversation_id": "p1-ru-006", "channel": "agent chat", "language": "RU", "audio_url": null, "group_id": "DEV"},
  "turns": [{"n": 1, "speaker": "client", "text": "...", "timestamp": "00:00", "rel_sec": 0, "rel": "00:00"}],
  "facts": {"first_operator_turn": 10, "first_response_delay_sec": 132, "first_response_exceeds": false,
            "long_pauses": [], "identity_verified_turns": [12], "hold_ranges": [], "recording_gaps": [], "...": "..."},
  "evaluation": {
    "_id": 7, "status": "ok", "model": "gpt-...", "checklist_version": 1, "created_at": "...",
    "usage": {"calls": 24, "cost_usd": 0.03, "latency_sec": 14.2},
    "ai_score": {"final_score": 95.0, "critical_flag": false, "sections": [...]},
    "final": {"score": {...после апелляций...}, "criteria": [...], "overrides": {"empathy": false}},
    "criteria": [{
      "criterion_id": "contact", "name": "Установление контакта", "section": "start", "type": "binary",
      "weight": 10, "value": true, "value_label": "Да", "points": 10.0, "max_points": 10.0,
      "effective_weight": 12.5, "applicable": true,
      "rationale": "Оператор поздоровался и представился в первой реплике [10].",
      "evidence_turns": [10], "agreement": 1.0, "confidence": 0.97, "needs_review": false,
      "votes": [{"verdict": "yes", "rationale": "...", "evidence_turns": [10], "confidence": 0.95}]
    }]
  },
  "history": [...], "appeals": [...]
}
```

- `value`: binary → `true/false/null` (null = N/A); scale → число или null; flag (грубая ошибка) → `true/false`.
- `effective_weight` — вес после перераспределения N/A (сколько баллов из 100 даёт критерий).
- Показывай `final` (после апелляций), а исходную оценку ИИ — рядом для сравнения.
