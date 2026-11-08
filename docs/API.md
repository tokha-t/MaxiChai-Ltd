# CQA HTTP API

Справка по `server/app.py` для фронтенда. База запросов — `window.CQA_API_BASE` (по умолчанию тот же origin); все запросы интерфейса проходят через `web/js/api.js`. Тела POST и ответы — JSON, если не указано иное. Успешные ответы перечисленных методов имеют HTTP 200.

В таблицах «query» означает параметры URL, «body» — JSON-объект. Необязательные фильтры по умолчанию отсутствуют. Идентификаторы диалогов — строки, версий чек-листов, оценок и апелляций — числа. `cl_id` / `checklist_id` — идентификатор записи в БД, не номер `version`.

## Состояние и настройки

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| GET | `/api/health` | Нет | `{ok, model, judges, llm_mode, votes, db, org_api}`. `org_api` — наличие ссылки организаторов. |
| GET | `/api/config` | Нет | `{model, judges, mode, llm_mode, team_name, votes, free}`. `votes` — текущее число голосов по умолчанию. |
| GET | `/api/settings` | Нет | Настройки: `{org_api_url_set, keys, llm_judges, recommended_judges, llm_votes, llm_mode, team_name, mode, judges, ready}`. Значения ключей не возвращаются. |
| POST | `/api/settings` | body: любые из `org_api_url`, `gemini_api_key`, `cerebras_api_key`, `llm_judges`, `team_name`, `llm_votes`. Пустые, отсутствующие и `null` поля не меняют настройки. | Тот же объект, что GET `/api/settings`; сохраняет настройки в `.env` и обновляет клиент моделей. |
| POST | `/api/settings/ping` | Без тела | Массив `{label, ok, latency_sec, answer}` либо `{label, ok:false, error}` по каждому судье. Выполняет реальные запросы моделей. |

`judges`: массив `{label, provider, model, free, local, has_key, rpm}`. `keys` в настройках: `{GEMINI_API_KEY: boolean, CEREBRAS_API_KEY: boolean}`. `llm_judges` — строка судей через запятую в формате `provider:model`. `mode` относится к выгрузке (`local`, `external_api`, `hybrid`), `llm_mode` — к выполнению оценки (`batch`, `per_criterion`).

## Наборы и диалоги

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| POST | `/api/datasets/fetch` | body: `{group_id: "DEV"}`, непустая группа обязательна | `{group_id, count, ids}`. Загружает набор организаторов, сохраняет кэш и диалоги; ошибка источника — 502. |
| GET | `/api/groups` | Нет | `[{group_id, count}]`. |
| POST | `/api/conversations/upload` | body: `{group_id: "UPLOAD", payload: ...}` либо `{group_id: "UPLOAD", text: "Клиент: …\nОператор: …", conversation_id?, channel?, language?}`. `group_id` по умолчанию `UPLOAD`; при `text` ID генерируется как `manual-…`, `channel` по умолчанию `agent chat`. | `{group_id, count, ids}`. `payload` принимает JSON набора, одну запись или массив записей; `text` имеет приоритет, если непустой. |
| GET | `/api/conversations` | query: `group_id`, `language`, `channel`, `critical` (boolean), `needs_review` (boolean), `q`, `criterion_id`, `criterion_violation` (boolean, по умолчанию `false`) | Массив кратких карточек диалогов, описанный ниже. Без `group_id` или с пустым значением — все группы. `q` ищет подстроку в ID, `channel` — подстроку в названии канала; `language` сравнивается без учёта регистра. |
| GET | `/api/conversations/{cid}` | ID в пути | `{conversation, turns, facts, evaluation, history, appeals}`. Если диалога нет — 404. |

Краткая карточка: `{conversation_id, group_id, channel, language, turns, audio_url, open_appeals, evaluation}`. Здесь `turns` — число реплик, `evaluation` — `null` или `{evaluation_id, status, error, ai_score, final_score, critical_flag, needs_review, checklist_version, evaluated_at, cost_usd, critical_reason}`. В этой сводке `ai_score` — число; в полной оценке это объект.

`criterion_id` отбирает диалоги, где последняя оценка успешна и выбранный критерий присутствует в `final.criteria`, а его `value` не равен `null` (значения `false` и `0` допустимы). N/A, неоценённые диалоги, последние оценки с ошибкой и отсутствующий критерий исключаются; предыдущая успешная оценка вместо ошибочной не подставляется. `criterion_violation=true` дополнительно оставляет только нарушения: для `flag` это `value=true`, для остальных типов — `points < max_points` (отсутствующие баллы считаются нулём). Учитываются решения по апелляциям. При одинаковой группе и без других фильтров число найденных диалогов равно соответствующему `criteria[].fail` из `/api/stats`. `criterion_violation=false` или отсутствие этого параметра не ограничивает выборку по нарушению; неизвестный `criterion_id` даёт пустой список. `criterion_violation=true` без `criterion_id` возвращает 400. Пример: `/api/conversations?group_id=DEV&criterion_id=contact&criterion_violation=true`.

В полной карточке `conversation` содержит ID, канал, язык, ссылку на аудио и группу; `turns` — массив реплик с полями `n`, `speaker`, `text`, `timestamp`, `rel_sec`, `rel`. Нумерация реплик начинается с 1. `facts` содержит рассчитанные тайминги, паузы, проверку личности и другие события; порог первой реакции берётся из чек-листа последней оценки, а при её отсутствии — из активного. `history` — краткие оценки от новой к старой: `{id, checklist_id, status, final_score, critical_flag, model, cost_usd, created_at, version}`.

Повторная загрузка того же ID обновляет диалог, не создавая дубль. При неизменных тексте и канале оценки сохраняют актуальность; при изменении входа прежние успешные оценки помечаются в истории как `stale`.

## Чек-листы

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| GET | `/api/checklists` | Нет | `[{id, name, version, note, created_at, active}]`, новые версии первыми. |
| GET | `/api/checklists/active` | Нет | Полный активный чек-лист или `null`. |
| GET | `/api/checklists/{cl_id}` | Числовой ID в пути | Полный чек-лист; 404, если не найден. |
| POST | `/api/checklists/validate` | body: `{data: {...}}` либо сам объект чек-листа | `{errors: [...], weights_sum}`. Ошибки содержатся в JSON при HTTP 200; сумма весов не включает `flag`. |
| POST | `/api/checklists` | body: `{data: {...}, note: "", activate: true}`. `note` по умолчанию `""`, `activate` — `true`. | Полный чек-лист новой версии; неверный чек-лист — 400. |
| POST | `/api/checklists/{cl_id}/activate` | ID в пути, без тела | Полный активированный чек-лист; 404, если не найден. |

Полный чек-лист: поля конфигурации (`name`, `version`, `criteria`, секции и настройки) плюс `_id`, `_note`, `_created_at`, `_active`. `first_response_threshold_sec` — целое число секунд больше нуля, по умолчанию 180. Сохранение или активация версии сами по себе не запускают оценку: для применения вызывается `/api/evaluate`.

## Оценка и задачи

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| POST | `/api/evaluate` | body: `{group_id: "DEV"}` или `{conversation_ids: ["id", ...]}`; дополнительно `checklist_id`, `votes`, `reuse: true`, `force: ["contact", ...]`. По умолчанию активный чек-лист, число голосов из `/api/config`, `reuse=true`, принудительных критериев нет. | `{job_id, total}` сразу после запуска фоновой задачи. 400 — нет диалогов для оценки; 404 — неизвестный диалог/чек-лист; 409 — конфликт с выполняющейся задачей. |
| GET | `/api/jobs/{job_id}` | ID задачи в пути | Полная задача с `results` и `elapsed_sec`; 404, если не найдена. |
| GET | `/api/jobs` | Нет | Массив задач без `results` и `elapsed_sec`: все `running` плюс до 20 последних завершённых (`done` / `failed`), общая сортировка по `started_at` убыванию. |
| GET | `/api/evaluations/{eval_id}` | Числовой ID оценки в пути | Полная оценка с исходным и итоговым результатом; 404, если не найдена. |

Непустой `conversation_ids` задаёт выборку даже при наличии `group_id`. Если ID не переданы, `group_id: ""` или `null` выбирает все группы; отсутствие обоих способов выбора даёт 400. `votes: null` или `0` также использует настройку по умолчанию. `reuse` разрешает повторно использовать подходящие оценки неизменённых критериев; изменение только веса может дать `llm_calls: 0`. `force` исключает перечисленные критерии из переиспользования.

Задача содержит:

```json
{
  "job_id": "a1b2c3d4e5",
  "status": "running",
  "group_id": "DEV",
  "conversation_ids": ["p1-ru-006"],
  "votes": 3,
  "done": 0,
  "total": 1,
  "cost_usd": 0.0,
  "llm_calls": 0,
  "started_at": 1794110400.0,
  "finished_at": null,
  "error": null,
  "checklist_id": 4,
  "checklist_version": 4
}
```

`group_id` берётся из запроса; если поле не передано и все выбранные диалоги из одной группы — определяется по ним, иначе `null`. Пустая группа сохраняется как `null`. `votes` хранит фактически выбранное число голосов. Времена задачи — Unix seconds; `elapsed_sec` — длительность в секундах. Полный ответ задачи дополнительно содержит `results: [{conversation_id, evaluation_id, status, error, final_score, critical_flag}]`. `done` растёт после сохранения каждой оценки. `status: "done"` означает завершение обработки; ошибки отдельных диалогов нужно проверять в `results`.

При наличии `running` задачи с той же непустой группой или пересекающимися ID сервер не запускает второй поток и отвечает HTTP 409:

```json
{
  "detail": {
    "message": "Оценка этой группы или выбранных диалогов уже выполняется",
    "job_id": "a1b2c3d4e5"
  }
}
```

После обновления страницы загрузите `/api/jobs`, выберите подходящую `running` задачу по группе/ID и продолжайте опрашивать `/api/jobs/{job_id}`. При 409 используйте `detail.job_id`. Реестр задач и защита от двойного запуска действуют в памяти одного процесса сервера: обновление страницы их сохраняет, перезапуск сервера — сбрасывает. Уже записанные оценки остаются в SQLite. Задачи `done` и `failed` не мешают повторному запуску.

Полная успешная оценка содержит `_id`, `status`, `model`, `checklist_version`, `created_at`, `usage`, исходные `criteria` и `score`, а также `ai_score` и `final: {score, criteria, overrides}`. Для интерфейса используйте `final` после апелляций и показывайте `ai_score` рядом для сравнения. Если оценки ещё нет, `evaluation=null`; при ошибочной оценке `final=null`, подробность — в `error`.

Значения критерия: `binary` — `true`/`false`/`null`, `scale` — допустимое число/`null`, `flag` — `true`/`false`. `null` означает N/A и допустим только при `na_allowed`; для `flag` N/A не предусмотрено. Используйте `value_label` («Да», «Нет», «N/A») для отображения. В строке критерия также есть `points`, `max_points`, `effective_weight`, `rationale`, `evidence_turns`, `votes`, `agreement`, `confidence`, `needs_review`.

Если после очистки доказательств модели не осталось допустимых номеров реплик и использовано доказательство по умолчанию, критерий получает `evidence_fallback=true`, `needs_review=true`, `confidence` не выше 0.5. Значения и баллы при этом не меняются; `model_confidence` остаётся исходной агрегированной уверенностью модели. Маркер сохраняется при переиспользовании и пересчёте. Это внутренняя метаинформация оценки/API; в `results.json` формата 1.0 её нет.

## Апелляции

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| POST | `/api/appeals` | body: `{evaluation_id, criterion_id, comment: "", author: "оператор", ai_review: true}`. ID обязательны, остальные поля имеют указанные значения по умолчанию. | Полная апелляция со статусом `open`; при `ai_review=true` запускается фоновое мнение ИИ. Повторная открытая апелляция на тот же критерий той же оценки — 400. |
| GET | `/api/appeals` | query: необязательные `status` (`open`, `confirmed`, `changed`), `conversation_id` | Массив полных апелляций, новые первыми. Без фильтров — все. |
| GET | `/api/appeals/{appeal_id}` | Числовой ID в пути | Полная апелляция; 404, если не найдена. |
| POST | `/api/appeals/{appeal_id}/ai-review` | ID в пути, без тела | Повторно получает мнение ИИ и возвращает полную апелляцию после завершения запроса. 404, если не найдена. |
| POST | `/api/appeals/{appeal_id}/decision` | body: `{decision: "confirmed" или "changed", reason: "…", reviewer: "проверяющий", value?: ...}`. `reason` обязателен и непустой; `reviewer` имеет указанный default. Для `changed` обязателен `value`, для `confirmed` не нужен. | `{appeal, evaluation}` с пересчитанным `evaluation.final`. Неверное значение, пустая причина или уже рассмотренная апелляция — 400. |

Полная апелляция включает `{id, evaluation_id, conversation_id, criterion_id, comment, author, status, original_value, decision_value, decision_reason, reviewer, ai_review, created_at, decided_at}` и дополнения: `criterion`, `original`, `original_label`, `decision_label`, `fragment`, `ai_final_score`, `final_score`. `fragment` — реплики доказательств с соседним контекстом. `ai_review` изначально может быть `null`, затем содержит мнение модели либо `{error}`; его можно получать повторным GET. Мнение ИИ не закрывает апелляцию автоматически.

Решение `changed` принимает boolean для бинарного критерия/флага, число из объявленной шкалы или разрешённый `null`. `confirmed` сохраняет исходное значение. Исходная оценка ИИ остаётся для сравнения.

## Выгрузка и аналитика

| Метод | Путь | Запрос | Ответ |
| --- | --- | --- | --- |
| GET | `/api/export` | query: обязательный `group_id`, `source=ai` или `source=final` (по умолчанию `ai`) | JSON формата 1.0 с `Content-Disposition: attachment; filename="results_<group_id>.json"`. `ai` — исходная оценка, `final` — после апелляций. |
| POST | `/api/results/save` | body: `{group_id: "DEV", target: "dev" или "prod"}`. `target` обязателен; отсутствующий/пустой `group_id` выбирает все группы. | `{path, saved, count, ok, not_evaluated, errors, warnings}`. Сохраняет исходные оценки ИИ в `results/<target>/results.json`, проверяя формат; для `prod` ожидается 10 записей. |
| GET | `/api/stats` | query: необязательный `group_id`; без него или с пустым значением — все группы | `{criterion_filter_supported: true, evaluated, avg_score, critical_count, needs_review_count, open_appeals, total_cost_usd, avg_cost_usd, criteria, by_language}`. Статистика по последним оценкам со статусом `ok`, с учётом апелляций. |
| GET | `/api/diff` | query: `group_id`, `from_checklist`, `to_checklist`; все необязательны, но для сравнения нужны оба ID чек-листов | `[{conversation_id, before, after, changed: [{criterion_id, before, after}]}]`. Внешние `before`/`after` — баллы, внутри `changed` — подписи значений. Без пары оценок диалог пропускается. |

`/api/export` отдаёт загрузку, не записывает файл проекта и не блокирует оценки `mock`. Он не выполняет проверку формата перед ответом. Отсутствующие оценки представлены записями `status: "error"`.

`/api/results/save` проверяет происхождение **сохранённых оценок**, включая `model` любой записи и `model` любого голоса: если строка начинается с `mock`, возвращает HTTP 200 с `saved=false` и `errors: ["в выгрузке есть оценки заглушки"]`. Текущая настройка реальной модели не отменяет эту проверку. При блокировке заглушек файл не записывается; при ошибке проверки формата временный файл удаляется, прежний `results.json` остаётся неизменным. Фронтенд должен проверять `saved`, а не только HTTP-статус. `warnings` сами по себе не запрещают сохранение; `count` — все записи, `ok` — успешные, `not_evaluated` — диалоги без сохранённой оценки. Неверный `target` или пустая выборка дают 400.

Формат 1.0: `{schema_version: "1.0", team_name, model: {mode, name}, results}`. Успешная запись содержит `{conversation_id, status: "ok", criteria, score: {final_score, critical_flag}}`; каждый критерий содержит **только** `criterion_id`, `value`, `rationale`, `evidence_turns`. Запись ошибки содержит `{conversation_id, status: "error", criteria: [], score: null, error}`. Внутренние `votes`, `confidence`, `needs_review`, `evidence_fallback` и сведения об апелляциях в этот формат не добавляются.

В `/api/stats` массив `criteria` содержит `{criterion_id, name, n, applicable, points, max, na, fail, rate}`, `by_language` — средние баллы по языкам. `criterion_filter_supported: true` объявляет поддержку отбора нарушений по критерию в `/api/conversations`, в том числе для пустой выборки. Итоги `null` исключены из средних. В `/api/diff` изменение формулировки/веса может присутствовать в `changed` даже при одинаковых подписях значений.

## Ошибки и встроенные маршруты

Обычные ошибки приложения: `{"detail":"сообщение"}` с HTTP 400/404/502; конфликт оценки 409 имеет объект `detail`, показанный выше. Ошибки валидации типов path/query и обязательного JSON-тела возвращаются FastAPI как HTTP 422 с массивом `detail`. У проверки чек-листа и сохранения результатов есть собственные поля `errors` при HTTP 200.

При наличии каталога `web/` сервер раздаёт его по `/` (HTML и статические файлы). Встроенная документация FastAPI: `/docs`, `/redoc`, `/openapi.json`.
