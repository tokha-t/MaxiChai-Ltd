"""Official export regressions: only temporary SQLite files and offline responses."""
from __future__ import annotations

import copy
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cqa import export as export_module
from cqa.config import settings
from cqa.dataset import normalize_record
from cqa.evaluator import evaluate_conversation, input_hash, rescore
from cqa.llm import MockClient
from cqa.store import Store
from cqa.validate import validate_results


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Importing the server initializes its store; redirect it before import.
    monkeypatch.setattr(settings, "db_path", tmp_path / "bootstrap.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "cache")
    monkeypatch.setattr("cqa.llm.make_client", lambda *args, **kwargs: MockClient())
    module = importlib.import_module("server.app")
    database = Store(tmp_path / "test.sqlite")
    monkeypatch.setattr(module, "store", database)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "client", SimpleNamespace(model="gemini:currently-configured-C"))
    monkeypatch.setattr(settings, "model_mode", "local")
    yield module
    database.conn.close()


def response_with_provenance(record, checklist, models=None):
    """Simulate stored provider responses without ever contacting a provider."""
    models = models or [
        {"model": "gemini:actual-A", "provider": "gemini", "local": False},
        {"model": "openai:actual-B", "provider": "openai", "local": False},
    ]
    result = evaluate_conversation(record, checklist, MockClient(), votes=1)
    assert result["status"] == "ok"
    result.update(model=" + ".join(m["model"] for m in models), votes=len(models),
                  requested_votes=len(models), received_votes=len(models), valid_votes=len(models),
                  votes_complete=True, incomplete_criteria=[],
                  actual_models=[m["model"] for m in models], model_provenance=copy.deepcopy(models))
    for row in result["criteria"]:
        sample_vote = row["votes"][0]
        row["votes"] = [dict(sample_vote, **model) for model in models]
        row.update(requested_votes=len(models), received_votes=len(models), valid_votes=len(models),
                   votes_complete=True, actual_models=[m["model"] for m in models])
    return result


@pytest.fixture
def bundle(api):
    def create(count=10, group="CONTROL-2026", target="prod", checklist=None, models=None):
        checklist = checklist or api.store.active_checklist()
        records = [normalize_record({
            "conversation_id": f"{group.lower()}-{index:03d}", "channel": "agent chat", "language": "RU",
            "transcript": [
                {"speaker": "client", "text": "Добрый день", "timestamp": "00:00"},
                {"speaker": "operator", "text": "Здравствуйте, я Анна", "timestamp": "00:02"},
            ],
        }) for index in range(count)]
        api.store.upsert_conversations(records, group)
        manifest = api.store.register_dataset_manifest(records, group, source="organizer")
        template = response_with_provenance(records[0], checklist, models)
        results = []
        for record in records:
            result = copy.deepcopy(template)
            result["conversation_id"] = record["conversation_id"]
            result["input_hash"] = input_hash(record)
            evaluation_id = api.store.save_evaluation(result, checklist["_id"])
            results.append(api.store.get_evaluation(evaluation_id))
        return SimpleNamespace(records=records, results=results, checklist=checklist,
                               manifest=manifest, target=target, group=group)
    return create


def prepare(data):
    return export_module.prepare_submission(
        data.results, data.records, data.checklist, data.manifest, data.target, data.group,
    )


def download(api, data, source="ai"):
    return api.export(group_id=data.group, source=source, target=data.target, checklist_id=data.checklist["_id"])


def save(api, data):
    return api.results_save({"group_id": data.group, "target": data.target, "checklist_id": data.checklist["_id"]})


@pytest.mark.parametrize("origin", ["evaluation", "vote", "provider"])
def test_official_get_and_post_reject_same_mock_after_current_model_changed(api, bundle, origin):
    data = bundle()
    result = copy.deepcopy(data.results[0])
    if origin == "evaluation":
        result["model"] = "mock:old-evaluation"
    elif origin == "vote":
        result["criteria"][0]["votes"][0]["model"] = "mock:old-vote"
    else:
        result["criteria"][0]["votes"][0]["provider"] = "mock"
    api.store.save_evaluation(result, data.checklist["_id"])
    path = api.ROOT / "results" / data.target / "results.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous accepted submission\n")

    with pytest.raises(HTTPException) as exc:
        download(api, data)
    saved = save(api, data)

    assert exc.value.status_code == 400
    assert saved["saved"] is False
    assert exc.value.detail["errors"] == saved["errors"]
    assert any("заглуш" in error for error in saved["errors"])
    assert path.read_bytes() == b"previous accepted submission\n"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("models,mode", [
    ([{"model": "ollama:qwen3", "provider": "ollama", "local": True}], "local"),
    ([{"model": "custom:private-model", "provider": "custom", "local": True}], "local"),
    ([{"model": "ollama:qwen3", "provider": "ollama", "local": True},
      {"model": "gemini:actual-A", "provider": "gemini", "local": False}], "hybrid"),
])
def test_real_local_models_and_mixed_provenance_are_accepted(api, bundle, models, mode):
    data = bundle(models=models)

    document = json.loads(download(api, data).body)

    assert document["model"] == {"mode": mode, "name": " + ".join(sorted(m["model"] for m in models))}
    assert save(api, data)["saved"] is True


def test_official_model_names_and_mode_come_from_saved_answers(api, bundle):
    data = bundle()

    document = json.loads(download(api, data).body)
    saved = save(api, data)

    assert document["model"] == {"name": "gemini:actual-A + openai:actual-B", "mode": "external_api"}
    assert saved["saved"] is True
    assert json.loads((api.ROOT / saved["path"]).read_text(encoding="utf-8")) == document
    assert set(document) == {"schema_version", "team_name", "model", "results"}
    assert all(set(row) == {"criterion_id", "value", "rationale", "evidence_turns"}
               for record in document["results"] for row in record["criteria"])


def test_unknown_legacy_model_origin_is_diagnosed_not_guessed(api, bundle):
    data = bundle(models=[{"model": "unidentified-old-model"}])
    for result in data.results:
        for row in result["criteria"]:
            for vote in row["votes"]:
                vote.pop("provider", None)
                vote.pop("local", None)

    document, errors, _ = prepare(data)

    assert document is None and errors


def test_historical_dev_profile_is_selected_even_after_experiment_is_active(api, bundle):
    data = bundle(count=65, group="DEV", target="dev")
    historical_id = data.checklist["_id"]
    experimental = copy.deepcopy(data.checklist)
    experimental["criteria"][0]["description"] += " Экспериментальное дополнительное условие."
    experimental = api.store.save_checklist(experimental, activate=True)
    latest = response_with_provenance(data.records[0], experimental, [
        {"model": "gemini:experimental-C", "provider": "gemini", "local": False},
    ])
    latest = rescore(latest, experimental, {"contact": False})
    api.store.save_evaluation(latest, experimental["_id"])
    assert api.store.active_checklist()["_id"] != historical_id

    document = json.loads(download(api, data).body)

    assert document["model"]["name"] == "gemini:actual-A + openai:actual-B"
    first = document["results"][0]
    assert next(row for row in first["criteria"] if row["criterion_id"] == "contact")["value"] is True
    assert save(api, data)["saved"] is True


def test_missing_selected_profile_is_error_record_without_using_another_profile(api, bundle):
    data = bundle()
    other = copy.deepcopy(data.checklist)
    other["criteria"][0]["description"] += " Другое правило."
    other = api.store.save_checklist(other, activate=True)
    for record in data.records[:-1]:
        api.store.save_evaluation(response_with_provenance(record, other), other["_id"])
    data.checklist = other

    document = json.loads(download(api, data).body)
    saved = save(api, data)

    assert document["results"][-1]["status"] == "error"
    assert document["results"][-1]["criteria"] == []
    assert document["results"][-1]["score"] is None
    assert saved["saved"] is True
    assert any(data.records[-1]["conversation_id"] in warning for warning in saved["warnings"])


@pytest.mark.parametrize("count,group,target", [(1, "DEV", "dev"), (65, "TEST", "dev"), (1, "CONTROL", "prod")])
def test_submission_target_rejects_wrong_group_or_size(api, bundle, count, group, target):
    data = bundle(count=count, group=group, target=target)

    document, errors, _ = prepare(data)

    assert document is None and errors
    assert save(api, data)["saved"] is False


@pytest.mark.parametrize("count,group,target", [(65, "DEV", "dev"), (10, "CONTROL", "prod")])
def test_same_count_with_wrong_ids_does_not_match_import_manifest(api, bundle, count, group, target):
    data = bundle(count=count, group=group, target=target)
    data.records[0]["conversation_id"] = "wrong-id-with-correct-count"
    data.results[0]["conversation_id"] = "wrong-id-with-correct-count"

    document, errors, _ = prepare(data)

    assert document is None and errors


def test_official_export_requires_import_manifest_and_explicit_profile(api, bundle):
    data = bundle()
    manifest = data.manifest
    data.manifest = None
    document, errors, _ = prepare(data)
    assert document is None and errors
    data.manifest = manifest
    data.checklist = None
    document, errors, _ = prepare(data)
    assert document is None and any("checklist_id" in error for error in errors)


@pytest.mark.parametrize("extra", [False, True])
def test_duplicate_or_unexpected_evaluation_ids_are_not_hidden_by_builder(api, bundle, extra):
    data = bundle()
    duplicate = copy.deepcopy(data.results[0])
    if extra:
        duplicate["conversation_id"] = "not-in-original-set"
    data.results.append(duplicate)

    document, errors, _ = prepare(data)

    assert document is None and errors


def test_manifest_is_immutable_and_detects_changed_input(api, bundle):
    data = bundle()
    repeated = api.store.register_dataset_manifest(data.records, data.group, source="organizer")
    assert repeated == data.manifest
    changed = copy.deepcopy(data.records)
    changed[0]["transcript"][0]["text"] += " Изменённый вход."

    with pytest.raises(ValueError):
        api.store.register_dataset_manifest(changed, data.group, source="organizer")

    assert api.store.get_dataset_manifest(data.group) == data.manifest
    data.records = changed
    document, errors, _ = prepare(data)
    assert document is None and errors


@pytest.mark.parametrize("mutation", ["definition", "weight", "type", "version", "profile", "input"])
def test_spoofed_profile_and_input_metadata_are_rejected(api, bundle, mutation):
    data = bundle()
    result = data.results[0]
    if mutation == "definition":
        result["criteria"][0]["def_hash"] = "unrelated-definition"
    elif mutation == "weight":
        result["criteria"][0]["weight"] += 1
    elif mutation == "type":
        result["criteria"][0]["type"] = "flag"
    elif mutation == "version":
        result["checklist_version"] += 10
    elif mutation == "profile":
        result["checklist_id"] = result["_checklist_id"] = 999
    else:
        result["input_hash"] = "unrelated-input"

    document, errors, _ = prepare(data)

    assert document is None and errors
    assert any(result["conversation_id"] in error for error in errors)


def test_experimental_weights_are_not_an_official_profile(api, bundle):
    checklist = api.store.active_checklist()
    checklist["criteria"][0]["weight"] += 1
    checklist = api.store.save_checklist(checklist, activate=False)
    data = bundle(checklist=checklist)

    document, errors, _ = prepare(data)

    assert document is None and errors


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "fractional_value", "bool_value", "string_turn", "out_of_bounds_turn", "critical_flag", "arithmetic"])
def test_raw_invalid_data_is_not_silently_corrected_by_export_builder(api, bundle, mutation):
    data = bundle()
    result = data.results[0]
    if mutation in ("duplicate", "extra"):
        extra = copy.deepcopy(result["criteria"][0])
        if mutation == "extra":
            extra["criterion_id"] = "extra_criterion"
        result["criteria"].append(extra)
    elif mutation in ("fractional_value", "bool_value"):
        next(row for row in result["criteria"] if row["criterion_id"] == "consultation")["value"] = 15.5 if mutation == "fractional_value" else False
    elif mutation in ("string_turn", "out_of_bounds_turn"):
        result["criteria"][0]["evidence_turns"] = ["1"] if mutation == "string_turn" else [999]
    elif mutation == "critical_flag":
        result["score"]["critical_flag"] = not result["score"]["critical_flag"]
    else:
        result["score"]["final_score"] = 12.34

    document, errors, _ = prepare(data)

    assert document is None and errors


def test_valid_error_records_remain_exportable_and_missing_records_are_honest(api, bundle):
    data = bundle()
    data.results[0].update(status="error", criteria=[], score=None, error="Provider unavailable")
    data.results.pop()

    document, errors, warnings = prepare(data)

    assert errors == []
    assert len(document["results"]) == 10
    assert document["results"][0]["status"] == document["results"][-1]["status"] == "error"
    assert all(row["criteria"] == [] and row["score"] is None and row["error"]
               for row in (document["results"][0], document["results"][-1]))
    assert validate_results(document, [record["conversation_id"] for record in data.records], expected_count=10)[0] == []
    assert warnings


def test_all_error_records_with_actual_attempt_provenance_are_structurally_valid(api, bundle):
    data = bundle()
    for result in data.results:
        result.update(status="error", criteria=[], score=None, error="No valid verdict")

    document, errors, warnings = prepare(data)

    assert errors == []
    assert all(record["status"] == "error" for record in document["results"])
    assert document["model"]["name"] == "gemini:actual-A + openai:actual-B"
    assert warnings


def test_diagnostic_final_export_does_not_replace_original_ai_result(api, bundle):
    data = bundle()
    evaluation_id = data.results[0]["_id"]
    appeal = api.appeal_create({"evaluation_id": evaluation_id, "criterion_id": "contact",
                               "comment": "Перепроверить", "ai_review": False})
    api.appeal_decide(appeal["id"], {"decision": "changed", "value": False, "reason": "Подтверждённое исправление"})

    official = json.loads(download(api, data).body)
    final = json.loads(download(api, data, source="final").body)

    assert next(row for row in official["results"][0]["criteria"] if row["criterion_id"] == "contact")["value"] is True
    assert next(row for row in final["results"][0]["criteria"] if row["criterion_id"] == "contact")["value"] is False


def test_atomic_write_failure_preserves_previous_file_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "results.json"
    path.write_bytes(b"previous accepted submission\n")

    def failed_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(export_module.os, "replace", failed_replace)
    with pytest.raises(OSError, match="replace failed"):
        export_module.atomic_write_results({"results": []}, str(path))

    assert path.read_bytes() == b"previous accepted submission\n"
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_atomic_writes_use_distinct_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "results.json"
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    sources = []
    original_replace = export_module.os.replace

    def synchronized_replace(source, destination):
        with lock:
            sources.append(Path(source))
        barrier.wait(timeout=5)
        return original_replace(source, destination)

    monkeypatch.setattr(export_module.os, "replace", synchronized_replace)
    documents = [{"marker": "A", "payload": ["a"] * 100}, {"marker": "B", "payload": ["b"] * 100}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda document: export_module.atomic_write_results(document, str(path)), documents))

    assert len(set(sources)) == 2
    assert all(source.parent == path.parent for source in sources)
    assert json.loads(path.read_text(encoding="utf-8")) in documents
    assert list(tmp_path.iterdir()) == [path]
