"""API workflow regressions, using an isolated SQLite database and mock model."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cqa.config import settings
from cqa.evaluator import evaluate_conversation, rescore
from cqa.llm import MockClient
from cqa.store import Store


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Importing server.app initializes its store and imports cached datasets.
    # Redirect both before import so standalone runs never touch project data.
    monkeypatch.setattr(settings, "db_path", tmp_path / "bootstrap.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("cqa.llm.make_client", lambda: MockClient())
    module = importlib.import_module("server.app")
    database = Store(tmp_path / "test.sqlite")
    monkeypatch.setattr(module, "store", database)
    monkeypatch.setattr(module, "client", MockClient())
    monkeypatch.setattr(module, "JOBS", {})
    database.save_checklist({
        "name": "API workflow checklist",
        "criteria": [{
            "id": "contact", "name": "Контакт", "type": "binary", "weight": 100,
            "description": "Оператор поздоровался", "na_allowed": True,
            "na_condition": "Нет реплик оператора",
        }],
    })
    yield module
    database.conn.close()


def add_evaluation(api, cid, group, value=True, language="RU"):
    api.upload({
        "conversation_id": cid, "group_id": group, "language": language,
        "text": "[00:00] Клиент: Добрый день\n[00:02] Оператор: Здравствуйте, я Анна",
    })
    checklist = api.store.active_checklist()
    result = evaluate_conversation(api.store.get_conversation(cid), checklist, api.client, votes=1)
    assert result["status"] == "ok"
    result = rescore(result, checklist, {"contact": value})
    return api.store.save_evaluation(result, checklist["_id"])


def open_appeal(api, evaluation_id):
    return api.appeal_create({
        "evaluation_id": evaluation_id, "criterion_id": "contact",
        "comment": "Прошу проверить приветствие", "author": "Оператор",
        "ai_review": False,
    })


def test_dialog_contains_appeal_labels_and_recalculated_totals(api):
    evaluation_id = add_evaluation(api, "appealed", "DEV")
    appeal = open_appeal(api, evaluation_id)
    api.appeal_decide(appeal["id"], {
        "decision": "changed", "value": False,
        "reason": "Приветствие не соответствует критерию", "reviewer": "Проверяющий",
    })

    dialog = api.conversation("appealed")
    saved = dialog["appeals"][0]
    assert saved["original_label"] == "Да"
    assert saved["decision_label"] == "Нет"
    assert saved["criterion"]["name"] == "Контакт"
    assert saved["ai_final_score"] == 100
    assert saved["final_score"] == 0
    assert saved["decision_reason"] == "Приветствие не соответствует критерию"
    assert dialog["evaluation"]["ai_score"]["final_score"] == 100
    assert dialog["evaluation"]["final"]["score"]["final_score"] == 0
    assert api.store.get_evaluation(evaluation_id)["score"]["final_score"] == 100


def test_stats_counts_only_open_appeals_in_selected_group(api):
    open_appeal(api, add_evaluation(api, "dev-open", "DEV"))
    closed = open_appeal(api, add_evaluation(api, "dev-closed", "DEV"))
    open_appeal(api, add_evaluation(api, "prod-open", "PROD"))
    api.appeal_decide(closed["id"], {
        "decision": "confirmed", "reason": "Оценка верна", "reviewer": "Проверяющий",
    })

    assert api.stats("DEV")["open_appeals"] == 1
    assert api.stats("PROD")["open_appeals"] == 1
    assert api.stats("EMPTY")["open_appeals"] == 0
    assert api.stats()["open_appeals"] == 2


@pytest.mark.parametrize("include_scored", [False, True])
def test_stats_excludes_null_scores_from_language_averages(api, include_scored):
    add_evaluation(api, "not-applicable", "DEV", value=None)
    if include_scored:
        add_evaluation(api, "scored", "DEV", value=True)

    stats = api.stats("DEV")
    assert stats["evaluated"] == (2 if include_scored else 1)
    assert stats["avg_score"] == (100 if include_scored else None)
    assert stats["by_language"] == ({"RU": 100} if include_scored else {})
    assert stats["criteria"][0]["na"] == 1


def test_empty_group_evaluates_conversations_from_all_groups(api, monkeypatch):
    for cid, group in [("dev-dialog", "DEV"), ("prod-dialog", "PROD")]:
        api.upload({
            "conversation_id": cid, "group_id": group, "language": "RU",
            "text": "Клиент: Привет\nОператор: Здравствуйте",
        })

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    # Replace only the server's thread factory; model workers use real threads.
    monkeypatch.setattr(api, "threading", SimpleNamespace(Thread=ImmediateThread))
    response = api.evaluate({"group_id": "", "votes": 1})
    job = api.job(response["job_id"])

    assert response["total"] == 2
    assert job["status"] == "done"
    assert job["done"] == job["total"] == 2
    assert {row["conversation_id"] for row in job["results"]} == {"dev-dialog", "prod-dialog"}
    assert all(row["status"] == "ok" for row in job["results"])
    assert api.stats()["evaluated"] == 2


def criterion_appeal(api, criterion_type="binary", na_allowed=False):
    checklist = api.store.active_checklist()
    criterion = checklist["criteria"][0]
    criterion.update(type=criterion_type, na_allowed=na_allowed)
    if criterion_type == "scale":
        criterion["scale"] = [
            {"value": 0, "label": "Нет"},
            {"value": 1, "label": "Частично"},
            {"value": 2, "label": "Полностью"},
        ]
    api.checklist_save({"data": checklist, "activate": True})
    evaluation_id = add_evaluation(
        api, "decision-validation", "DEV", value=2 if criterion_type == "scale" else True,
    )
    return open_appeal(api, evaluation_id)


@pytest.mark.parametrize("criterion_type, na_allowed, supplied_value", [
    ("binary", False, {"value": 1}),
    ("binary", False, {"value": 0}),
    ("binary", False, {"value": "true"}),
    ("binary", False, {"value": None}),
    ("flag", False, {"value": 1}),
    ("flag", False, {"value": None}),
    ("scale", False, {"value": 999}),
    ("scale", False, {"value": True}),
    ("scale", False, {"value": False}),
    ("scale", False, {"value": "1"}),
    ("scale", False, {"value": None}),
    ("binary", True, {}),
])
def test_appeal_rejects_invalid_value_without_recording_decision(api, criterion_type, na_allowed, supplied_value):
    appeal = criterion_appeal(api, criterion_type, na_allowed)
    before = api.conversation("decision-validation")["evaluation"]["final"]["score"]

    with pytest.raises(HTTPException) as exc:
        api.appeal_decide(appeal["id"], {
            "decision": "changed", "reason": "Проверено", "reviewer": "Проверяющий",
            **supplied_value,
        })

    assert exc.value.status_code == 400
    assert api.store.get_appeal(appeal["id"])["status"] == "open"
    assert api.conversation("decision-validation")["evaluation"]["final"]["score"] == before


@pytest.mark.parametrize("criterion_type, na_allowed, value", [
    ("binary", False, True),
    ("binary", False, False),
    ("binary", True, None),
    ("flag", False, True),
    ("flag", False, False),
    ("scale", False, 0),
    ("scale", False, 1),
    ("scale", True, None),
])
def test_appeal_accepts_declared_values_and_allowed_na(api, criterion_type, na_allowed, value):
    appeal = criterion_appeal(api, criterion_type, na_allowed)
    response = api.appeal_decide(appeal["id"], {
        "decision": "changed", "value": value, "reason": "Проверено",
    })

    assert response["appeal"]["status"] == "changed"
    saved_value = response["evaluation"]["final"]["criteria"][0]["value"]
    assert saved_value == value
    assert type(saved_value) is type(value)
    if value is None:
        assert response["appeal"]["decision_label"] == "N/A"


def test_appeal_confirmation_does_not_require_a_replacement_value(api):
    appeal = criterion_appeal(api, "binary", na_allowed=False)
    response = api.appeal_decide(appeal["id"], {
        "decision": "confirmed", "reason": "Исходная оценка верна",
    })

    assert response["appeal"]["status"] == "confirmed"
    assert response["appeal"]["decision_value"] is True
    assert response["appeal"]["ai_final_score"] == response["appeal"]["final_score"] == 100


@pytest.mark.parametrize("changed_field, new_value", [
    ("description", "Оператор поздоровался и представился по имени"),
    ("weight", 50),
])
def test_diff_reports_changed_definition_or_weight_with_unchanged_value(api, changed_field, new_value):
    before_id = add_evaluation(api, "checklist-diff", "DEV")
    before = api.store.get_evaluation(before_id)
    old_checklist = api.store.active_checklist()
    updated = api.store.get_checklist(old_checklist["_id"])
    updated["criteria"][0][changed_field] = new_value
    new_checklist = api.checklist_save({"data": updated, "activate": True})
    after = evaluate_conversation(
        api.store.get_conversation("checklist-diff"), new_checklist, api.client,
        votes=1, previous=before,
    )
    assert after["status"] == "ok"
    assert before["criteria"][0]["value"] is after["criteria"][0]["value"] is True
    assert (before["criteria"][0]["def_hash"] != after["criteria"][0]["def_hash"]) == (changed_field == "description")
    if changed_field == "weight":
        assert after["usage"]["calls"] == 0
    api.store.save_evaluation(after, new_checklist["_id"])

    changes = api.diff("DEV", from_checklist=old_checklist["_id"], to_checklist=new_checklist["_id"])

    assert len(changes) == 1
    assert changes[0]["before"] == changes[0]["after"] == 100
    assert changes[0]["changed"] == [{"criterion_id": "contact", "before": "Да", "after": "Да"}]


def test_dialog_facts_use_evaluated_checklist_until_new_version_is_applied(api):
    add_evaluation(api, "threshold-version", "DEV")
    old_checklist = api.store.active_checklist()
    updated = api.store.get_checklist(old_checklist["_id"])
    updated["first_response_threshold_sec"] = 1
    new_checklist = api.checklist_save({"data": updated, "activate": True})

    before = api.conversation("threshold-version")
    assert before["facts"]["first_response_delay_sec"] == 2
    assert before["facts"]["threshold_sec"] == 180
    assert before["facts"]["first_response_exceeds"] is False

    result = evaluate_conversation(api.store.get_conversation("threshold-version"), new_checklist, api.client, votes=1)
    api.store.save_evaluation(result, new_checklist["_id"])
    after = api.conversation("threshold-version")
    assert after["facts"]["threshold_sec"] == 1
    assert after["facts"]["first_response_exceeds"] is True


@pytest.mark.parametrize("threshold", [0, -1, 1.5, True, "180", None])
def test_checklist_rejects_invalid_response_threshold_before_saving(api, threshold):
    checklist = api.store.active_checklist()
    active_id = checklist["_id"]
    versions_before = api.checklists()
    checklist["first_response_threshold_sec"] = threshold

    validation = api.checklist_validate({"data": checklist})
    assert validation["errors"]
    assert any("Порог первой реакции" in error for error in validation["errors"])
    with pytest.raises(HTTPException) as exc:
        api.checklist_save({"data": checklist, "activate": True})

    assert exc.value.status_code == 400
    assert api.checklists() == versions_before
    assert api.store.active_checklist()["_id"] == active_id


def test_checklist_accepts_positive_integer_response_threshold(api):
    checklist = api.store.active_checklist()
    checklist["first_response_threshold_sec"] = 1

    assert api.checklist_validate({"data": checklist})["errors"] == []
    saved = api.checklist_save({"data": checklist, "activate": True})

    assert saved["first_response_threshold_sec"] == 1
    assert api.store.active_checklist()["_id"] == saved["_id"]


@pytest.fixture
def job_dialogs(api):
    for cid, group in [("dev-one", "DEV"), ("dev-two", "DEV"), ("prod-one", "PROD"), ("prod-two", "PROD")]:
        api.upload({
            "conversation_id": cid, "group_id": group,
            "text": "Клиент: Привет\nОператор: Здравствуйте",
        })


@pytest.fixture
def pending_threads(api, monkeypatch):
    started = []

    class PendingThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            started.append(self)

    # Do not replace threading.Thread globally: concurrency tests use real threads.
    monkeypatch.setattr(api, "threading", SimpleNamespace(Thread=PendingThread))
    return started


@pytest.mark.parametrize("first,second", [
    ({"group_id": "DEV", "conversation_ids": ["dev-one"]},
     {"group_id": "DEV", "conversation_ids": ["dev-two"]}),
    ({"conversation_ids": ["dev-one"]}, {"conversation_ids": ["dev-two"]}),
    ({"group_id": "DEV", "conversation_ids": ["dev-one"]},
     {"group_id": "PROD", "conversation_ids": ["dev-one"]}),
    ({"conversation_ids": ["dev-one", "prod-one"]},
     {"conversation_ids": ["prod-one", "prod-two"]}),
])
def test_evaluate_rejects_running_group_or_overlapping_ids(api, job_dialogs, pending_threads, first, second):
    running = api.evaluate(first)

    with pytest.raises(HTTPException) as exc:
        api.evaluate(second)

    assert exc.value.status_code == 409
    assert exc.value.detail["job_id"] == running["job_id"]
    assert exc.value.detail["message"]
    assert len(api.JOBS) == len(pending_threads) == 1


def test_evaluate_allows_independent_groups_and_exposes_job_context(api, job_dialogs, pending_threads, monkeypatch):
    monkeypatch.setattr(settings, "llm_votes", 3)
    dev = api.evaluate({"group_id": "DEV"})
    prod = api.evaluate({"conversation_ids": ["prod-one"], "votes": 2})

    jobs = {row["job_id"]: row for row in api.jobs()}
    assert len(jobs) == len(pending_threads) == 2
    assert jobs[dev["job_id"]]["group_id"] == "DEV"
    assert jobs[dev["job_id"]]["votes"] == 3
    assert jobs[dev["job_id"]]["done"] == 0
    assert jobs[dev["job_id"]]["total"] == 2
    assert set(jobs[dev["job_id"]]["conversation_ids"]) == {"dev-one", "dev-two"}
    assert jobs[prod["job_id"]]["group_id"] == "PROD"
    assert jobs[prod["job_id"]]["votes"] == 2
    assert jobs[prod["job_id"]]["total"] == 1
    assert all(row["status"] == "running" and "results" not in row for row in jobs.values())


def test_evaluate_mixed_group_ids_have_no_inferred_group(api, job_dialogs, pending_threads):
    response = api.evaluate({"conversation_ids": ["dev-one", "prod-one"]})

    assert api.job(response["job_id"])["group_id"] is None


@pytest.mark.parametrize("status", ["done", "failed"])
def test_evaluate_can_restart_finished_or_failed_jobs(api, job_dialogs, pending_threads, status):
    first = api.evaluate({"group_id": "DEV"})
    api.JOBS[first["job_id"]]["status"] = status

    second = api.evaluate({"group_id": "DEV"})

    assert second["job_id"] != first["job_id"]
    assert len(pending_threads) == 2
    assert api.job(second["job_id"])["status"] == "running"


def test_jobs_reports_progress_and_effective_votes_after_each_result(api, job_dialogs, pending_threads, monkeypatch):
    monkeypatch.setattr(settings, "llm_votes", 3)
    snapshots = []

    def evaluate(convs, checklist, client, votes, previous, force, on_done):
        assert votes == 3
        for conv in convs:
            result = evaluate_conversation(conv, checklist, client, votes=1)
            on_done(result)
            snapshots.append(api.jobs()[0])

    monkeypatch.setattr(api, "evaluate_many", evaluate)
    response = api.evaluate({"group_id": "DEV"})
    thread = pending_threads[0]
    thread.target(*thread.args)

    assert [snapshot["done"] for snapshot in snapshots] == [1, 2]
    assert all(snapshot["status"] == "running" for snapshot in snapshots)
    assert all(snapshot["total"] == 2 and snapshot["votes"] == 3 for snapshot in snapshots)
    assert all(snapshot["group_id"] == "DEV" for snapshot in snapshots)
    completed = api.job(response["job_id"])
    assert completed["status"] == "done"
    assert completed["done"] == completed["total"] == 2
    assert len(completed["results"]) == 2


def test_jobs_keeps_old_running_jobs_after_many_completed_jobs(api, job_dialogs, pending_threads):
    dev = api.evaluate({"group_id": "DEV"})
    prod = api.evaluate({"group_id": "PROD"})
    api.JOBS[dev["job_id"]]["started_at"] = 1
    api.JOBS[prod["job_id"]]["started_at"] = 2
    template = api.JOBS[dev["job_id"]]
    for index in range(30):
        job_id = f"finished-{index}"
        api.JOBS[job_id] = dict(template, job_id=job_id, status="done", started_at=100 + index)

    jobs = api.jobs()

    assert len(jobs) == 22
    assert {row["job_id"] for row in jobs if row["status"] == "running"} == {dev["job_id"], prod["job_id"]}
    assert [row["started_at"] for row in jobs] == sorted((row["started_at"] for row in jobs), reverse=True)
    assert {row["job_id"] for row in jobs if row["status"] == "done"} == {f"finished-{i}" for i in range(10, 30)}


def test_evaluate_concurrent_requests_start_only_one_worker(api, job_dialogs, pending_threads, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    conv = api.store.get_conversation("dev-one")
    checklist = api.store.active_checklist()
    barrier = Barrier(2)

    def active_checklist():
        barrier.wait(timeout=5)
        return checklist

    # Readers return immutable snapshots; worker threads never open SQLite connections.
    monkeypatch.setattr(api.store, "get_conversation", lambda cid: conv)
    monkeypatch.setattr(api.store, "active_checklist", active_checklist)

    def request():
        try:
            return api.evaluate({"conversation_ids": ["dev-one"]})
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: request(), range(2)))

    successful = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(successful) == len(rejected) == 1
    assert rejected[0].status_code == 409
    assert rejected[0].detail["job_id"] == successful[0]["job_id"]
    assert len(api.JOBS) == len(pending_threads) == 1


def test_job_loading_previous_failure_does_not_block_retry(api, job_dialogs, pending_threads, monkeypatch):
    response = api.evaluate({"group_id": "DEV"})

    def broken_previous(*args, **kwargs):
        raise RuntimeError("previous read failed")

    monkeypatch.setattr(api.store, "latest_evaluation", broken_previous)
    thread = pending_threads[0]
    thread.target(*thread.args)

    failed = api.job(response["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "previous read failed"
    assert failed["finished_at"] is not None
    retry = api.evaluate({"group_id": "DEV"})
    assert retry["job_id"] != response["job_id"]
    assert len(pending_threads) == 2


def test_worker_start_failure_does_not_leave_running_job(api, job_dialogs, pending_threads, monkeypatch):
    thread_class = api.threading.Thread
    original_start = thread_class.start

    def broken_start(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(thread_class, "start", broken_start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        api.evaluate({"group_id": "DEV"})

    failed = api.jobs()[0]
    assert failed["status"] == "failed"
    assert "thread start failed" in failed["error"]
    assert failed["finished_at"] is not None
    monkeypatch.setattr(thread_class, "start", original_start)
    retry = api.evaluate({"group_id": "DEV"})
    assert retry["job_id"] != failed["job_id"]
    assert len(pending_threads) == 1
