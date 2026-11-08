"""Submission guards use temporary paths, isolated storage, and offline model stubs."""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cqa import cli
from cqa.checklist import ensure_defaults, load_default
from cqa.config import settings
from cqa.dataset import records_from_upload
from cqa.evaluator import evaluate_conversation, input_hash
from cqa.llm import MockClient
from cqa.store import Store
from cqa.validate import validate_results


ERROR = "в выгрузке есть оценки заглушки"


@pytest.fixture
def sample():
    record = records_from_upload({
        "conversation_id": "submission-test", "channel": "agent chat", "language": "RU",
        "transcript": [
            {"speaker": "client", "text": "Добрый день", "timestamp": "00:00"},
            {"speaker": "operator", "text": "Здравствуйте, я Анна", "timestamp": "00:02"},
        ],
    })[0]
    checklist = ensure_defaults(load_default())
    # Real-looking provenance, generated locally: no provider is contacted.
    result = evaluate_conversation(record, checklist, MockClient("gemini:test"), votes=1)
    assert result["status"] == "ok"
    # Replace all mock provenance to simulate persisted real-provider responses.
    provenance = {"model": "gemini:test", "provider": "gemini", "local": False}
    result["model_provenance"] = [provenance]
    for row in result["criteria"]:
        for vote in row["votes"]:
            vote.update(provenance)
    return record, checklist, result


def submission_bundle(sample, checklist, count):
    record, _, template = sample
    records, results = [], []
    for index in range(count):
        conversation = copy.deepcopy(record)
        conversation["conversation_id"] = "submission-test" if count == 1 else f"submission-test-{index:03d}"
        result = copy.deepcopy(template)
        result.update(conversation_id=conversation["conversation_id"], input_hash=input_hash(conversation),
                      checklist_id=checklist["_id"], checklist_version=checklist["version"])
        records.append(conversation)
        results.append(result)
    return records, results


def with_mock(result, source):
    result = copy.deepcopy(result)
    if source == "evaluation":
        result["model"] = "mock:offline"
    elif source == "vote":
        # The panel label and the other votes still refer to a real provider.
        result["criteria"][-1]["votes"].append({"model": "mock:fallback"})
    elif source == "error":
        result.update(status="error", model="mock", criteria=[], score=None, error="Оценка не выполнена")
    return result


@pytest.fixture
def api(tmp_path, monkeypatch):
    # server.app initializes storage at import time; redirect it before import.
    monkeypatch.setattr(settings, "db_path", tmp_path / "bootstrap.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("cqa.llm.make_client", lambda: MockClient())
    module = importlib.import_module("server.app")
    database = Store(tmp_path / "test.sqlite")
    monkeypatch.setattr(module, "store", database)
    monkeypatch.setattr(module, "client", SimpleNamespace(model="gemini:current-real-client"))
    monkeypatch.setattr(module, "ROOT", tmp_path)
    yield module
    database.conn.close()


@pytest.mark.parametrize("source,target,existing", [
    ("evaluation", "dev", False),
    ("evaluation", "prod", True),
    ("vote", "dev", True),
    ("vote", "prod", False),
    ("error", "dev", True),
])
def test_results_save_rejects_saved_mock_without_writing(api, sample, source, target, existing):
    checklist = api.store.active_checklist()
    count, group = (65, "DEV") if target == "dev" else (10, "CONTROL")
    records, results = submission_bundle(sample, checklist, count)
    api.store.upsert_conversations(records, group)
    api.store.register_dataset_manifest(records, group, source="file")
    for index, result in enumerate(results):
        api.store.save_evaluation(with_mock(result, source) if index == 0 else result, checklist["_id"])
    path = api.ROOT / "results" / target / "results.json"
    if existing:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous submission\n")

    response = api.results_save({"group_id": group, "target": target, "checklist_id": checklist["_id"]})

    assert response["saved"] is False
    assert ERROR in response["errors"]
    assert response["count"] == count
    assert response["path"] == f"results/{target}/results.json"
    assert path.exists() is existing
    if existing:
        assert path.read_bytes() == b"previous submission\n"
    assert list(path.parent.glob(".results.json*.tmp")) == []


def test_results_save_accepts_real_provenance_and_keeps_format(api, sample):
    checklist = api.store.active_checklist()
    records, results = submission_bundle(sample, checklist, 65)
    api.store.upsert_conversations(records, "DEV")
    api.store.register_dataset_manifest(records, "DEV", source="file")
    for result in results:
        api.store.save_evaluation(result, checklist["_id"])

    response = api.results_save({"group_id": "DEV", "target": "dev", "checklist_id": checklist["_id"]})

    assert response["saved"] is True
    assert response["errors"] == []
    doc = json.loads((api.ROOT / response["path"]).read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert set(doc) == {"schema_version", "team_name", "model", "results"}
    assert doc["model"] == {"mode": "external_api", "name": "gemini:test"}
    assert len(doc["results"]) == response["count"] == response["ok"] == 65
    assert validate_results(doc, [record["conversation_id"] for record in records], expected_count=65)[0] == []


@pytest.fixture
def run_cli(tmp_path, monkeypatch, sample):
    project = tmp_path / "project"
    project.mkdir()
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(settings, "runs_dir", tmp_path / "runs")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "cache")
    database = Store(tmp_path / "cli.sqlite")
    checklist = database.active_checklist()
    monkeypatch.setattr(cli, "Store", lambda: database)
    monkeypatch.setattr(cli, "_pick_checklist", lambda *args: checklist)
    monkeypatch.setattr(cli, "make_client", lambda cfg: SimpleNamespace(model="gemini:current", judges=[]))

    def run(path, source="evaluation", count=1):
        records, results = submission_bundle(sample, checklist, count)
        results = [with_mock(result, source) for result in results]

        def import_group(args, store, refresh):
            store.register_dataset_manifest(records, args.group, source="file")
            store.upsert_conversations(records, args.group)
            return records

        def evaluate(*args, **kwargs):
            for result in results:
                kwargs["on_done"](result)
            return results

        monkeypatch.setattr(cli, "_import_group", import_group)
        monkeypatch.setattr(cli, "evaluate_many", evaluate)
        cli.main(["evaluate", "--group", "DEV", "--votes", "1", "--checklist", str(checklist["_id"]),
                  "--no-db", "--out", str(path)])

    yield run, project, working
    database.conn.close()


@pytest.mark.parametrize("source", ["evaluation", "vote"])
@pytest.mark.parametrize("path_kind", ["relative", "absolute", "normalized", "project"])
def test_cli_rejects_mock_in_submission_directories(run_cli, source, path_kind):
    run, project, working = run_cli
    paths = {
        "relative": Path("results/dev/results.json"),
        "absolute": working / "results/prod/results.json",
        "normalized": Path("debug/../results/dev/results.json"),
        "project": project / "results/prod/results.json",
    }
    path = paths[path_kind]
    target = path.resolve()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous submission\n")

    with pytest.raises(SystemExit, match=ERROR):
        run(path, source)

    assert target.read_bytes() == b"previous submission\n"
    assert list(target.parent.glob(".results.json*.tmp")) == []


def test_cli_mock_rejection_does_not_create_submission(run_cli):
    run, project, _ = run_cli
    target = project / "results/dev/results.json"

    with pytest.raises(SystemExit, match=ERROR):
        run(target)

    assert not target.exists()
    assert list(target.parent.glob(".results.json*.tmp")) == []


def test_cli_rejects_mock_when_submission_filename_is_a_symlink(run_cli):
    run, project, working = run_cli
    external = working / "debug.json"
    external.write_bytes(b"external report\n")
    target = project / "results/dev/results.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(external)

    with pytest.raises(SystemExit, match=ERROR):
        run(target)

    assert target.is_symlink()
    assert target.resolve() == external
    assert external.read_bytes() == b"external report\n"
    assert list(target.parent.glob(".results.json*.tmp")) == []


@pytest.mark.parametrize("source,path", [
    ("evaluation", "debug/results.json"),
    ("vote", "results-debug/results.json"),
])
def test_cli_allows_debug_mock_outside_submission_directories(run_cli, source, path):
    run, _, _ = run_cli

    run(path, source)

    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert validate_results(doc, ["submission-test"])[0] == []


def test_cli_rejects_incomplete_real_dev_without_replacing_previous_file(run_cli, capsys):
    run, _, _ = run_cli
    target = Path("results/dev/results.json")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous submission\n")

    with pytest.raises(SystemExit) as exc:
        run(target, "real", count=1)

    assert exc.value.code == 1
    assert "ожидалось 65" in capsys.readouterr().out
    assert target.read_bytes() == b"previous submission\n"
    assert list(target.parent.glob(".results.json*.tmp")) == []


def test_cli_accepts_complete_real_dev_and_keeps_official_schema(run_cli):
    run, _, _ = run_cli
    target = Path("results/dev/results.json")

    run(target, "real", count=65)

    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert doc["model"] == {"mode": "external_api", "name": "gemini:test"}
    assert len(doc["results"]) == 65
    assert validate_results(doc, [f"submission-test-{index:03d}" for index in range(65)], expected_count=65)[0] == []
    assert list(target.parent.glob(".results.json*.tmp")) == []
