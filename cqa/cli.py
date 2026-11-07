"""Командная строка.

  python -m cqa fetch --group DEV                  # скачать набор и загрузить в базу
  python -m cqa evaluate --group DEV --out results/dev/results.json
  python -m cqa evaluate --group <PROD_ID> --out results/prod/results.json --expect 10
  python -m cqa validate results/prod/results.json --group <PROD_ID> --expect 10
  python -m cqa show p1-ru-001                     # последняя оценка диалога в консоли
  python -m cqa judges | ping | models --provider gemini --filter flash
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .checklist import ensure_defaults, load_default, load_file, validate_checklist
from .config import _judge, settings
from .dataset import get_group, records_from_upload, save_cache
from .evaluator import evaluate_many
from .export import build_results, write_results
from .llm import OpenAICompatibleClient, make_client
from .store import Store
from .validate import validate_file


def _load_group(group: str, refresh: bool, url: str = None, file: str = None) -> List[Dict[str, Any]]:
    if file:  # набор выдали файлом
        records = records_from_upload(json.loads(Path(file).read_text(encoding="utf-8")), default_id=group.lower())
        save_cache(group, {"dataset_group_id": group, "total_records": len(records), "records": records})
        return records
    data = get_group(group, refresh=refresh, url=url)
    return data["records"]


def _pick_checklist(arg: str, store: Store) -> Dict[str, Any]:
    if arg == "default":
        cl = load_default()
        db_default = next((c for c in store.list_checklists() if c["version"] == 1), None)
        cl["_id"] = db_default["id"] if db_default else None
        return cl
    if arg == "active":
        return store.active_checklist()
    cl = load_file(arg)
    errors = validate_checklist(ensure_defaults(cl))
    if errors:
        sys.exit("Ошибки в чек-листе: " + "; ".join(errors))
    return cl


def cmd_fetch(args) -> None:
    records = _load_group(args.group, refresh=True, url=args.url, file=args.file)
    store = Store()
    store.upsert_conversations(records, args.group)
    print(f"Загружено {len(records)} диалогов набора {args.group} в {settings.db_path}")


def cmd_evaluate(args) -> None:
    records = _load_group(args.group, refresh=args.refresh, url=args.url, file=args.file)
    if args.ids:
        wanted = set(x.strip() for x in args.ids.split(","))
        records = [r for r in records if r["conversation_id"] in wanted]
    if args.limit:
        records = records[: args.limit]
    store = Store()
    store.upsert_conversations(records, args.group)
    checklist = _pick_checklist(args.checklist, store)
    client = make_client()
    total = len(records)
    print(f"Оцениваю {total} диалогов: судьи {client.model}; голосов {args.votes or settings.llm_votes}; "
          f"режим {settings.llm_mode}; чек-лист «{checklist.get('name')}» v{checklist.get('version')}")
    started = time.time()
    done = {"n": 0, "cost": 0.0}
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.runs_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{args.group}.jsonl"
    log = log_path.open("w", encoding="utf-8")
    lock = threading.Lock()

    def on_done(res: Dict[str, Any]) -> None:
        with lock:
            _on_done(res)

    def _on_done(res: Dict[str, Any]) -> None:
        done["n"] += 1
        usage = res.get("usage") or {}
        done["cost"] += usage.get("cost_usd") or 0
        if not args.no_db:
            store.save_evaluation(res, checklist.get("_id"))
        score = (res.get("score") or {})
        flag = " ГРУБАЯ ОШИБКА" if score.get("critical_flag") else ""
        status = f"{score.get('final_score')}{flag}" if res.get("status") == "ok" else f"ОШИБКА: {res.get('error')}"
        print(f"[{done['n']}/{total}] {res['conversation_id']}: {status}  ({usage.get('latency_sec', '?')} с, ${usage.get('cost_usd', 0):.4f})", flush=True)
        log.write(json.dumps({
            "conversation_id": res["conversation_id"], "status": res.get("status"), "error": res.get("error"),
            "model": res.get("model"), "checklist_version": res.get("checklist_version"), "votes": res.get("votes"),
            "score": score.get("final_score") if res.get("status") == "ok" else None,
            "critical_flag": score.get("critical_flag"), "usage": usage,
            "criteria": [{"id": r["criterion_id"], "value": r.get("value"), "agreement": r.get("agreement"),
                          "votes": [v.get("verdict") for v in r.get("votes") or []]} for r in res.get("criteria") or []],
        }, ensure_ascii=False) + "\n")
        log.flush()

    results = evaluate_many(records, checklist, client, votes=args.votes, parallel_dialogs=args.parallel, on_done=on_done)
    log.close()
    elapsed = time.time() - started
    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"Готово: {ok}/{total} без ошибок за {elapsed:.0f} с, стоимость ≈ ${done['cost']:.3f} "
          f"(≈ ${done['cost'] / max(1, total):.4f} за диалог). Журнал: {log_path}")
    if args.out:
        doc = build_results(results, [r["conversation_id"] for r in records], model_name=client.model, mode=settings.model_mode)
        path = write_results(doc, args.out)
        print(f"results.json → {path}")
        turn_counts = {r["conversation_id"]: len(r["transcript"]) for r in records}
        errors, warnings = validate_file(str(path), [r["conversation_id"] for r in records], turn_counts, args.expect)
        for w in warnings:
            print("  предупреждение:", w)
        if errors:
            print("ОШИБКИ ФОРМАТА:")
            for e in errors:
                print("  -", e)
            sys.exit(1)
        print("Формат проверен: ошибок нет.")


def cmd_validate(args) -> None:
    expected_ids = None
    turn_counts = None
    if args.group:
        records = _load_group(args.group, refresh=False)
        expected_ids = [r["conversation_id"] for r in records]
        turn_counts = {r["conversation_id"]: len(r["transcript"]) for r in records}
    errors, warnings = validate_file(args.path, expected_ids, turn_counts, args.expect)
    for w in warnings:
        print("предупреждение:", w)
    if errors:
        print("ОШИБКИ:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    print("OK: файл соответствует формату 1.0")


def cmd_show(args) -> None:
    store = Store()
    ev = store.latest_evaluation(args.id)
    if not ev:
        sys.exit("Оценок для этого диалога нет")
    if ev.get("status") != "ok":
        sys.exit(f"Оценка с ошибкой: {ev.get('error')}")
    print(f"{ev['conversation_id']}: итог {ev['score']['final_score']}, грубая ошибка: {ev['score']['critical_flag']}")
    for r in ev["criteria"]:
        mark = " ⚠" if r.get("needs_review") else ""
        print(f"  {r['criterion_id']:<15} {r['value_label']:<10} голоса {[v.get('verdict') for v in r.get('votes') or []]}{mark}")
        print(f"      {r['rationale']}  реплики {r['evidence_turns']}")


def cmd_judges(args) -> None:
    print(f"Режим: {settings.llm_mode} (batch — все критерии одним вызовом), голосов: {settings.llm_votes}, "
          f"mode для results.json: {settings.model_mode}")
    for j in settings.judges:
        key = "ключ есть" if j.api_key else ("ключ не нужен" if j.local else "НЕТ КЛЮЧА")
        kind = "локальная" if j.local else ("бесплатный тариф" if j.free else "платная")
        print(f"  {j.label:<45} {kind:<17} {key:<14} rpm={j.rpm or '∞'} параллельно={j.concurrency}  {j.base_url}")


def cmd_ping(args) -> None:
    panel = make_client()
    msgs = [{"role": "system", "content": "Отвечай строго JSON-объектом."},
            {"role": "user", "content": 'Верни {"ok": true, "answer": "<одно слово по-казахски: спасибо>"}'}]
    bad = 0
    for judge in panel.judges:
        t = time.time()
        try:
            r = judge.complete_json(msgs, None)
            print(f"OK    {judge.model}: {time.time() - t:.1f} с, токены {r.prompt_tokens}/{r.completion_tokens}, ответ {r.data}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"FAIL  {judge.model}: {exc}")
    if bad:
        sys.exit(1)


def cmd_models(args) -> None:
    client = OpenAICompatibleClient(_judge(args.provider, "-", 1))
    try:
        ids = client.list_models()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Не удалось получить список моделей: {exc}")
    for m in ids:
        if not args.filter or args.filter.lower() in m.lower():
            print(m)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m cqa", description="Conversation Quality Analytics")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="скачать набор из API организаторов и загрузить в базу")
    f.add_argument("--group", required=True)
    f.add_argument("--url", help="другая ссылка API (без group_id), если организаторы дали новую")
    f.add_argument("--file", help="набор из локального JSON-файла вместо API")
    f.set_defaults(func=cmd_fetch)

    e = sub.add_parser("evaluate", help="оценить набор и выгрузить results.json")
    e.add_argument("--group", required=True)
    e.add_argument("--out", help="путь для results.json")
    e.add_argument("--ids", help="только эти ID через запятую")
    e.add_argument("--limit", type=int)
    e.add_argument("--votes", type=int, help=f"голосов на критерий (по умолчанию {settings.llm_votes})")
    e.add_argument("--parallel", type=int, default=4, help="диалогов параллельно")
    e.add_argument("--checklist", default="default", help="default | active | путь к JSON")
    e.add_argument("--expect", type=int, help="ожидаемое число записей (для prod — 10)")
    e.add_argument("--refresh", action="store_true", help="перекачать набор из API")
    e.add_argument("--url", help="другая ссылка API (без group_id), если организаторы дали новую")
    e.add_argument("--file", help="набор из локального JSON-файла вместо API")
    e.add_argument("--no-db", action="store_true", help="не сохранять оценки в базу")
    e.set_defaults(func=cmd_evaluate)

    v = sub.add_parser("validate", help="проверить results.json")
    v.add_argument("path")
    v.add_argument("--group", help="сверить ID с набором")
    v.add_argument("--expect", type=int)
    v.set_defaults(func=cmd_validate)

    j = sub.add_parser("judges", help="какие модели-судьи настроены")
    j.set_defaults(func=cmd_judges)

    pg = sub.add_parser("ping", help="проверить, что каждый судья отвечает")
    pg.set_defaults(func=cmd_ping)

    md = sub.add_parser("models", help="список моделей провайдера (точные имена для LLM_JUDGES)")
    md.add_argument("--provider", default="gemini")
    md.add_argument("--filter", help="подстрока, например flash")
    md.set_defaults(func=cmd_models)

    s = sub.add_parser("show", help="показать последнюю оценку диалога")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
