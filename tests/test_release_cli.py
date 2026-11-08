"""Release CLI regressions; all storage, executables, and datasets are temporary."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cqa import cli
from cqa.checklist import load_default
from cqa.config import settings
from cqa.store import Store


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / "release.sqlite")
    yield store
    store.conn.close()


def test_default_selects_matching_snapshot_instead_of_assigning_first_version(database, monkeypatch):
    old = database.active_checklist()
    updated = copy.deepcopy(old)
    updated["criteria"][0]["description"] += " Новая формулировка."
    saved = database.save_checklist(updated, activate=False)
    monkeypatch.setattr(cli, "load_default", lambda: updated)

    selected = cli._pick_checklist("default", database)

    assert selected["_id"] == saved["_id"] != old["_id"]
    assert selected["version"] == saved["version"]
    assert database.active_checklist()["_id"] == old["_id"]


def test_new_default_creates_inactive_snapshot_and_subsequent_selection_reuses_it(database, monkeypatch):
    old = database.active_checklist()
    updated = copy.deepcopy(old)
    updated["criteria"][0]["description"] += " Не сохранённое ранее правило."
    monkeypatch.setattr(cli, "load_default", lambda: updated)

    selected = cli._pick_checklist("default", database)
    again = cli._pick_checklist("default", database)

    assert selected["_id"] != old["_id"]
    assert selected["_active"] is False
    assert selected["_id"] == again["_id"]
    assert database.active_checklist()["_id"] == old["_id"]


def test_file_metadata_does_not_change_profile_identity_and_numeric_id_is_exact(database, tmp_path):
    snapshot = database.active_checklist()
    supplied = copy.deepcopy(snapshot)
    supplied.update(version=999, _id=999, _note="Внешняя копия")
    supplied["criteria"][0]["_editor"] = "ignored metadata"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(supplied), encoding="utf-8")

    assert cli._pick_checklist(str(path), database)["_id"] == snapshot["_id"]
    assert cli._pick_checklist(str(snapshot["_id"]), database) == snapshot
    assert cli._pick_checklist("active", database) == snapshot
    with pytest.raises(SystemExit, match="не найден"):
        cli._pick_checklist("999", database)


def test_cli_reports_requested_checks_actual_models_and_partial_results(tmp_path, monkeypatch, capsys):
    record = {"conversation_id": "test", "transcript": []}
    checklist = dict(load_default(), _id=7, version=9)
    monkeypatch.setattr(cli, "Store", lambda: object())
    monkeypatch.setattr(cli, "_import_group", lambda *args, **kwargs: [record])
    monkeypatch.setattr(cli, "_pick_checklist", lambda *args: checklist)
    monkeypatch.setattr(cli, "make_client", lambda *args: SimpleNamespace(model="current:C", judges=[]))
    monkeypatch.setattr(settings, "llm_votes", 1)
    monkeypatch.setattr(settings, "runs_dir", tmp_path / "runs")

    def evaluate(records, selected, client, votes, parallel_dialogs, on_done):
        assert votes == 3
        assert selected["_id"] == 7
        result = {
            "conversation_id": "test", "status": "ok", "score": {"final_score": 100},
            "criteria": [], "model": "old:A", "actual_models": ["old:A", "old:B"],
            "votes": 3, "requested_votes": 3, "received_votes": 1, "valid_votes": 1,
            "votes_complete": False, "usage": {},
        }
        on_done(result)
        return [result]

    monkeypatch.setattr(cli, "evaluate_many", evaluate)
    cli.main(["evaluate", "--group", "CONTROL", "--checklist", "7", "--votes", "3", "--no-db"])

    text = capsys.readouterr().out
    assert "Группа CONTROL; профиль ID 7, v9, SHA-256" in text
    assert "запрошено проверок: 3" in text
    assert "Обработано: 1; с ошибкой: 0; неполных или неизвестных по числу проверок: 1" in text
    assert "фактические модели ответов: old:A, old:B" in text
    log = json.loads(next((tmp_path / "runs").glob("*.jsonl")).read_text(encoding="utf-8"))
    assert log["checklist_id"] == 7
    assert log["checklist_hash"] == cli.result_export.checklist_fingerprint(checklist)
    assert log["requested_votes"] == 3 and log["valid_votes"] == 1
    assert log["votes_complete"] is False


def test_incompatible_import_preserves_existing_cache_and_dialogs(database, tmp_path, monkeypatch):
    original = {"conversation_id": "one", "channel": "agent chat", "transcript": [
        {"speaker": "client", "timestamp": "00:00", "text": "Исходный запрос"},
    ]}
    monkeypatch.setattr(settings, "data_dir", tmp_path / "cache")
    cli.save_cache("CONTROL", {"records": [original]})
    cached_path = settings.data_dir / "CONTROL.json"
    previous_bytes = cached_path.read_bytes()
    database.register_dataset_manifest([original], "CONTROL", source="organizer")
    database.upsert_conversations([original], "CONTROL")
    changed = copy.deepcopy(original)
    changed["transcript"][0]["text"] = "Другой запрос"
    monkeypatch.setattr(cli, "_load_group", lambda *args, **kwargs: [changed])
    args = SimpleNamespace(group="CONTROL", url=None, file="supplied.json")

    with pytest.raises(SystemExit, match="Набор не импортирован"):
        cli._import_group(args, database, refresh=True)

    assert cached_path.read_bytes() == previous_bytes
    assert database.get_conversation("one")["transcript"] == original["transcript"]


def test_cli_registers_complete_import_before_debug_limit(database, tmp_path, monkeypatch):
    records = [{"conversation_id": cid, "channel": "agent chat", "transcript": [
        {"speaker": "client", "text": "Привет", "timestamp": "00:00"},
    ]} for cid in ("one", "two")]
    monkeypatch.setattr(settings, "data_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "runs_dir", tmp_path / "runs")
    monkeypatch.setattr(cli, "Store", lambda: database)
    monkeypatch.setattr(cli, "_load_group", lambda *args, **kwargs: records)
    monkeypatch.setattr(cli, "make_client", lambda *args: SimpleNamespace(model="mock", judges=[]))
    processed = []

    def evaluate(selected_records, *args, **kwargs):
        processed.extend(record["conversation_id"] for record in selected_records)
        return []

    monkeypatch.setattr(cli, "evaluate_many", evaluate)
    cli.main(["evaluate", "--group", "CONTROL", "--limit", "1", "--no-db"])

    assert processed == ["one"]
    assert database.get_dataset_manifest("CONTROL")["conversation_ids"] == ["one", "two"]
    assert {row["conversation_id"] for row in database.list_conversations("CONTROL")} == {"one", "two"}


@pytest.fixture
def release_harness(tmp_path):
    project = tmp_path / "release-copy"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts" / "prod_run.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / "prod_run.sh", script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.jsonl"
    executable = f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ["COMMAND_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps([name] + sys.argv[1:]) + "\\n")
if name == "python" and len(sys.argv) > 3 and sys.argv[3] == os.environ.get("FAIL_STEP"):
    sys.exit(7)
'''
    for name in ("python", "git"):
        path = fake_bin / name
        path.write_text(executable, encoding="utf-8")
        path.chmod(0o755)

    def run(args, fail_step=""):
        env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                   COMMAND_LOG=str(log), LLM_VOTES="1", FAIL_STEP=fail_step)
        result = subprocess.run(["/bin/bash", str(script)] + args, env=env, capture_output=True, text=True)
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, commands

    return run


def test_release_script_pins_profile_three_checks_and_submission_destination(release_harness):
    result, commands = release_harness(["CONTROL", "--checklist", "4"])

    assert result.returncode == 0, result.stderr
    assert commands[0] == ["python", "-m", "cqa", "evaluate", "--group", "CONTROL", "--checklist", "4",
                           "--votes", "3", "--refresh", "--out", "results/prod/results.json", "--expect", "10"]
    assert commands[1] == ["python", "-m", "cqa", "validate", "results/prod/results.json", "--group", "CONTROL", "--expect", "10"]
    assert commands[-1] == ["git", "push"]


def test_release_script_preserves_quoted_optional_arguments(release_harness):
    result, commands = release_harness([
        "CONTROL", "--checklist", "4", "--file", "/tmp/control with spaces.json",
        "--judges", "ollama:local-model", "--parallel", "2",
    ])

    assert result.returncode == 0, result.stderr
    assert commands[0][-6:] == ["--file", "/tmp/control with spaces.json", "--judges", "ollama:local-model", "--parallel", "2"]


@pytest.mark.parametrize("args", [
    ["CONTROL"],
    ["CONTROL", "--checklist", "active"],
    ["CONTROL", "--checklist", "4", "--votes", "1"],
    ["CONTROL", "--checklist", "4", "--out", "debug.json"],
    ["CONTROL", "--checklist", "4", "--expect", "1"],
    ["CONTROL", "--checklist", "4", "--checklist", "1"],
    ["CONTROL", "--checklist", "4", "--group", "DEV"],
    ["CONTROL", "--checklist", "4", "--file"],
    ["CONTROL", "--checklist", "4", "--file", "one.json", "--url", "https://example.test"],
    ["CONTROL", "--checklist", "4", "--parallel", "0"],
])
def test_release_script_rejects_unsafe_or_ambiguous_arguments_before_any_command(release_harness, args):
    result, commands = release_harness(args)

    assert result.returncode != 0
    assert commands == []


@pytest.mark.parametrize("fail_step,command_count", [("evaluate", 1), ("validate", 2)])
def test_release_script_never_publishes_after_process_or_validation_failure(release_harness, fail_step, command_count):
    result, commands = release_harness(["CONTROL", "--checklist", "4"], fail_step=fail_step)

    assert result.returncode == 7
    assert len(commands) == command_count
    assert not any(command[0] == "git" for command in commands)
