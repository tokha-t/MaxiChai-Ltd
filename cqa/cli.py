"""Командная строка.

  python -m cqa fetch --group DEV                  # скачать набор и загрузить в базу
  python -m cqa evaluate --group DEV --checklist 1 --votes 1 --out results/dev/results.json
  python -m cqa evaluate --group <PROD_ID> --checklist <ID> --votes 3 --out results/prod/results.json --expect 10
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
from .config import ROOT, _judge, settings, with_judges
from .dataset import fetch_group, load_cache, records_from_upload, save_cache
from .evaluator import evaluate_many
from . import export as result_export
from .export import build_results, has_mock_evaluations
from .llm import OpenAICompatibleClient, make_client
from .store import Store
from .validate import validate_file, validate_results


def _load_group(group: str, refresh: bool, url: str = None, file: str = None) -> List[Dict[str, Any]]:
    if file:  # набор выдали файлом
        records = records_from_upload(json.loads(Path(file).read_text(encoding="utf-8")), default_id=group.lower())
        return records
    data = None if refresh else load_cache(group)
    if not data:
        data = fetch_group(group, url=url)
    return data["records"]


def _checklist_content(checklist: Dict[str, Any]) -> str:
    def without_metadata(value):
        if isinstance(value, dict):
            return {k: without_metadata(v) for k, v in value.items() if not k.startswith("_")}
        if isinstance(value, list):
            return [without_metadata(v) for v in value]
        return value

    normalized = without_metadata(ensure_defaults(checklist))
    normalized.pop("version", None)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pick_checklist(arg: str, store: Store) -> Dict[str, Any]:
    if arg == "active":
        cl = store.active_checklist()
        if not cl:
            sys.exit("Активный чек-лист не найден")
        return cl
    if arg.isdecimal():
        cl = store.get_checklist(int(arg))
        if not cl:
            sys.exit(f"Чек-лист с ID {arg} не найден")
        return cl
    cl = ensure_defaults(load_default() if arg == "default" else load_file(arg))
    errors = validate_checklist(cl)
    if errors:
        sys.exit("Ошибки в чек-листе: " + "; ".join(errors))
    content = _checklist_content(cl)
    for saved in store.list_checklists():
        snapshot = store.get_checklist(saved["id"])
        if _checklist_content(snapshot) == content:
            return snapshot
    return store.save_checklist(cl, note="Снимок профиля CLI", activate=False)


def _import_group(args, store: Store, refresh: bool) -> List[Dict[str, Any]]:
    cached = load_cache(args.group)
    try:
        if cached and cached.get("records"):
            store.register_dataset_manifest(cached["records"], args.group, source="cache")
        records = _load_group(args.group, refresh=refresh, url=args.url, file=args.file)
        source = "file" if args.file else ("organizer" if refresh or not cached else "cache")
        store.register_dataset_manifest(records, args.group, source=source)
    except ValueError as exc:
        sys.exit(f"Набор не импортирован: {exc}")
    save_cache(args.group, {"dataset_group_id": args.group, "total_records": len(records), "records": records})
    store.upsert_conversations(records, args.group)
    return records


def _submission_target(path: Path) -> str:
    # os.replace заменяет сам файл-ссылку: разрешаем ссылки только в каталоге.
    destination = path.parent.resolve() / path.name
    for directory in {(ROOT / "results").resolve(), Path("results").resolve()}:
        if directory in destination.parents:
            relative = destination.relative_to(directory)
            if len(relative.parts) >= 2 and relative.parts[0] in ("dev", "prod"):
                return relative.parts[0]
            sys.exit("Для сдачи укажите путь внутри results/dev/ или results/prod/")
    return ""


def cmd_fetch(args) -> None:
    store = Store()
    records = _import_group(args, store, refresh=True)
    print(f"Загружено {len(records)} диалогов набора {args.group} в {settings.db_path}")


def cmd_evaluate(args) -> None:
    store = Store()
    records = _import_group(args, store, refresh=args.refresh)
    if args.ids:
        wanted = set(x.strip() for x in args.ids.split(","))
        records = [r for r in records if r["conversation_id"] in wanted]
    if args.limit:
        records = records[: args.limit]
    checklist = _pick_checklist(args.checklist, store)
    profile_hash = result_export.checklist_fingerprint(checklist)
    requested_votes = args.votes or settings.llm_votes
    cfg = with_judges(settings, args.judges) if args.judges else settings
    client = make_client(cfg)
    total = len(records)
    print(f"Группа {args.group}; профиль ID {checklist.get('_id')}, v{checklist.get('version')}, SHA-256 {profile_hash}; "
          f"запрошено проверок: {requested_votes}")
    print(f"Оцениваю {total} диалогов: настроенные судьи {client.model}; "
          f"режим {settings.llm_mode}; чек-лист «{checklist.get('name')}»")
    dead = [j.model for j in client.judges if getattr(j, "dead_reason", None)]
    if dead:
        print("Пропущены судьи:", ", ".join(dead))
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
            "checklist_id": checklist.get("_id"), "checklist_hash": profile_hash,
            "requested_votes": res.get("requested_votes", requested_votes),
            "received_votes": res.get("received_votes"), "valid_votes": res.get("valid_votes"),
            "votes_complete": res.get("votes_complete"), "actual_models": res.get("actual_models"),
            "score": score.get("final_score") if res.get("status") == "ok" else None,
            "critical_flag": score.get("critical_flag"), "usage": usage,
            "criteria": [{"id": r["criterion_id"], "value": r.get("value"), "agreement": r.get("agreement"),
                          "votes": [v.get("verdict") for v in r.get("votes") or []]} for r in res.get("criteria") or []],
        }, ensure_ascii=False) + "\n")
        log.flush()

    try:
        results = evaluate_many(records, checklist, client, votes=requested_votes, parallel_dialogs=args.parallel, on_done=on_done)
    finally:
        log.close()
    elapsed = time.time() - started
    ok = sum(1 for r in results if r.get("status") == "ok")
    incomplete = sum(1 for r in results if r.get("votes_complete") is not True)
    used = sorted({v.get("model") for r in results for c in r.get("criteria") or []
                   for v in c.get("votes") or [] if v.get("model")} |
                  {m for r in results for m in r.get("actual_models") or []})
    print(f"Обработано: {len(results)}; с ошибкой: {len(results) - ok}; "
          f"неполных или неизвестных по числу проверок: {incomplete}; "
          f"фактические модели ответов: {', '.join(used) or 'ответов нет'}")
    print(f"Готово: {ok}/{total} без ошибок за {elapsed:.0f} с, стоимость ≈ ${done['cost']:.3f} "
          f"(≈ ${done['cost'] / max(1, total):.4f} за диалог). Журнал: {log_path}")
    if args.out:
        target = Path(args.out)
        submission_target = _submission_target(target)
        if submission_target and has_mock_evaluations(results):
            sys.exit("в выгрузке есть оценки заглушки")
        if submission_target:
            doc, errors, warnings = result_export.prepare_submission(
                results, records, checklist, store.get_dataset_manifest(args.group), submission_target, args.group,
            )
        else:
            doc = build_results(results, [r["conversation_id"] for r in records],
                                model_name=" + ".join(used) if used else client.model, mode=cfg.model_mode)
            turn_counts = {r["conversation_id"]: len(r["transcript"]) for r in records}
            errors, warnings = validate_results(doc, [r["conversation_id"] for r in records], turn_counts, args.expect)
        for w in warnings:
            print("  предупреждение:", w)
        if errors:
            print("ОШИБКИ ФОРМАТА (файл не записан, прежний не изменён):")
            for e in errors:
                print("  -", e)
            sys.exit(1)
        result_export.atomic_write_results(doc, str(target))
        print(f"results.json → {target}")
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


SECRET_KEYS = ("ORG_API_URL", "GEMINI_API_KEY", "CEREBRAS_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
               "MISTRAL_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "TEAM_NAME")


def _read_env(path: Path) -> Dict[str, str]:
    from .config import _clean_value
    out: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = _clean_value(v)
    return out


def cmd_setup(args) -> None:
    """Собирает .env из свежего шаблона: спрашивает только ссылку организаторов и ключ Gemini."""
    import getpass
    from .config import ROOT
    env_path = ROOT / ".env"
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    old = _read_env(env_path)
    keep = {k: v for k, v in old.items() if k in SECRET_KEYS and v and "..." not in v}
    if args.force or not keep.get("ORG_API_URL"):
        url = input("Вставьте ссылку API организаторов (из гайда) и нажмите Enter: ").strip().strip('"').strip("'")
        if url:
            keep["ORG_API_URL"] = url
    first_time = args.force or not keep.get("GEMINI_API_KEY")
    if first_time:
        key = getpass.getpass("Вставьте ключ Gemini (символы не отображаются — это нормально) и нажмите Enter: ").strip()
        if key:
            keep["GEMINI_API_KEY"] = key
        key = getpass.getpass("Ключ Cerebras — необязательно, просто Enter чтобы пропустить: ").strip()
        if key:
            keep["CEREBRAS_API_KEY"] = key
    lines = []
    for line in template.splitlines():
        k = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else None
        if k and k in keep:
            line = f"{k}={keep[k]}"
        lines.append(line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    missing = [k for k in ("ORG_API_URL", "GEMINI_API_KEY") if not keep.get(k)]
    print(".env готов." + (f" Не заполнено: {', '.join(missing)}" if missing else ""))


def cmd_judges(args) -> None:
    cfg = with_judges(settings, args.judges) if getattr(args, "judges", None) else settings
    print(f"Режим: {cfg.llm_mode} (batch — все критерии одним вызовом), голосов: {cfg.llm_votes}, "
          f"mode для results.json: {cfg.model_mode}")
    for j in cfg.judges:
        key = "ключ есть" if j.api_key else ("ключ не нужен" if j.local else "НЕТ КЛЮЧА")
        kind = "локальная" if j.local else ("бесплатный тариф" if j.free else "платная")
        print(f"  {j.label:<45} {kind:<17} {key:<14} rpm={j.rpm or '∞'} параллельно={j.concurrency}  {j.base_url}")


def cmd_ping(args) -> None:
    panel = make_client(with_judges(settings, args.judges) if args.judges else settings)
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
    e.add_argument("--checklist", default="default", help="числовой ID версии | default | active | путь к JSON")
    e.add_argument("--expect", type=int, help="ожидаемое число записей (для prod — 10)")
    e.add_argument("--refresh", action="store_true", help="перекачать набор из API")
    e.add_argument("--url", help="другая ссылка API (без group_id), если организаторы дали новую")
    e.add_argument("--file", help="набор из локального JSON-файла вместо API")
    e.add_argument("--no-db", action="store_true", help="не сохранять оценки в базу")
    e.add_argument("--judges", help="судьи на этот прогон вместо LLM_JUDGES, например gemini:gemini-3.6-flash,cerebras:gpt-oss-120b")
    e.set_defaults(func=cmd_evaluate)

    v = sub.add_parser("validate", help="проверить results.json")
    v.add_argument("path")
    v.add_argument("--group", help="сверить ID с набором")
    v.add_argument("--expect", type=int)
    v.set_defaults(func=cmd_validate)

    st = sub.add_parser("setup", help="собрать .env: спросит ссылку организаторов и ключ Gemini")
    st.add_argument("--force", action="store_true", help="спросить заново, даже если уже заполнено")
    st.set_defaults(func=cmd_setup)

    j = sub.add_parser("judges", help="какие модели-судьи настроены")
    j.add_argument("--judges")
    j.set_defaults(func=cmd_judges)

    pg = sub.add_parser("ping", help="проверить, что каждый судья отвечает (1 запрос на модель)")
    pg.add_argument("--judges")
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
