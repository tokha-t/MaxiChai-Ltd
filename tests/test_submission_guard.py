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
from cqa.evaluator import evaluate_conversation
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
    return record, checklist, result


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
    record, _, result = sample
    api.store.upsert_conversations([record], "DEV")
    api.store.save_evaluation(with_mock(result, source), api.store.active_checklist()["_id"])
    path = api.ROOT / "results" / target / "results.json"
    if existing:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous submission\n")

    response = api.results_save({"group_id": "DEV", "target": target})

    assert response["saved"] is False
    assert ERROR in response["errors"]
    assert response["count"] == 1
    assert response["path"] == f"results/{target}/results.json"
    assert path.exists() is existing
    if existing:
        assert path.read_bytes() == b"previous submission\n"
    assert not path.with_name(".results.json.tmp").exists()


def test_results_save_accepts_real_provenance_and_keeps_format(api, sample):
    record, _, result = sample
    api.store.upsert_conversations([record], "DEV")
    api.store.save_evaluation(result, api.store.active_checklist()["_id"])

    response = api.results_save({"group_id": "DEV", "target": "dev"})

    assert response["saved"] is True
    assert response["errors"] == []
    doc = json.loads((api.ROOT / response["path"]).read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert set(doc) == {"schema_version", "team_name", "model", "results"}
    assert validate_results(doc, [record["conversation_id"]])[0] == []


@pytest.fixture
def run_cli(tmp_path, monkeypatch, sample):
    record, checklist, original = sample
    project = tmp_path / "project"
    project.mkdir()
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(settings, "runs_dir", tmp_path / "runs")
    monkeypatch.setattr(cli, "_load_group", lambda *args, **kwargs: [record])
    monkeypatch.setattr(cli, "Store", lambda: SimpleNamespace(upsert_conversations=lambda *args: None))
    monkeypatch.setattr(cli, "_pick_checklist", lambda *args: checklist)
    monkeypatch.setattr(cli, "make_client", lambda cfg: SimpleNamespace(model="gemini:current", judges=[]))

    def run(path, source="evaluation"):
        result = with_mock(original, source)

        def evaluate(*args, **kwargs):
            kwargs["on_done"](result)
            return [result]

        monkeypatch.setattr(cli, "evaluate_many", evaluate)
        cli.main(["evaluate", "--group", "DEV", "--votes", "1", "--no-db", "--out", str(path)])

    return run, project, working


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
    assert not target.with_name(".results.json.tmp").exists()


def test_cli_mock_rejection_does_not_create_submission(run_cli):
    run, project, _ = run_cli
    target = project / "results/dev/results.json"

    with pytest.raises(SystemExit, match=ERROR):
        run(target)

    assert not target.exists()
    assert not target.with_name(".results.json.tmp").exists()


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
    assert not target.with_name(".results.json.tmp").exists()


@pytest.mark.parametrize("source,path", [
    ("evaluation", "debug/results.json"),
    ("vote", "results-debug/results.json"),
    ("real", "results/dev/results.json"),
])
def test_cli_allows_debug_mock_and_real_submission(run_cli, source, path):
    run, _, _ = run_cli

    run(path, source)

    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert validate_results(doc, ["submission-test"])[0] == []
