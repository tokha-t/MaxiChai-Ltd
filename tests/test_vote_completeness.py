"""Actual valid responses, safe failure metadata, and cache eligibility; no network."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from cqa.checklist import ensure_defaults, load_default
from cqa.dataset import records_from_upload
from cqa.evaluator import evaluate_conversation, evaluate_many
from cqa.export import build_results
from cqa.llm import JudgePanel, LLMError, MockClient


@pytest.fixture
def record():
    return records_from_upload({
        "conversation_id": "vote-completeness", "channel": "agent chat", "language": "RU",
        "transcript": [{"speaker": "client", "text": "Добрый день", "timestamp": "00:00"},
                       {"speaker": "operator", "text": "Здравствуйте, я Анна", "timestamp": "00:02"}],
    })[0]


@pytest.fixture
def checklist():
    return ensure_defaults({"name": "Counts", "criteria": [
        {"id": "contact", "name": "Контакт", "description": "Поздоровался", "type": "binary", "weight": 50},
        {"id": "empathy", "name": "Эмпатия", "description": "Проявил эмпатию", "type": "binary", "weight": 50},
    ]})


class ScriptedPanel(JudgePanel):
    def __init__(self, script):
        super().__init__([MockClient("test:configured")])
        self.script = script
        self.calls = 0
        self.lock = threading.Lock()

    def complete_json(self, messages, schema=None, start=0):
        with self.lock:
            index = self.calls
            self.calls += 1
        action = self.script[min(index, len(self.script) - 1)]
        if isinstance(action, Exception):
            raise action
        result = MockClient(action.get("model", "gemini:actual")).complete_json(messages, schema)
        rows = result.data.get("criteria")
        if rows is None:
            rows = {"single": result.data}
        for cid in list(rows):
            if cid in action.get("missing", []):
                del rows[cid]
                continue
            row = rows[cid]
            row["confidence"] = action.get("confidence", 0.99)
            row["evidence_turns"] = action.get("evidence", [2])
            row["verdict"] = action.get("verdicts", {}).get(cid, action.get("verdict", row["verdict"]))
        result.extra["provenance"] = {"model": result.model, "provider": "gemini", "local": False}
        return result


def assert_counts(result, requested, received, valid, complete):
    assert result["requested_votes"] == requested
    assert result["received_votes"] == received
    assert result["valid_votes"] == valid
    assert result["votes_complete"] is complete


def test_three_valid_responses_are_complete_without_new_review(record, checklist):
    panel = ScriptedPanel([{"model": "gemini:a"}, {"model": "gemini:b"}, {"model": "gemini:c"}])

    result = evaluate_conversation(record, checklist, panel, votes=3)

    assert result["status"] == "ok"
    assert panel.calls == 3
    assert_counts(result, 3, 3, 3, True)
    assert result["incomplete_criteria"] == []
    assert result["review_reason"] is None
    assert result["actual_models"] == ["gemini:a", "gemini:b", "gemini:c"]
    for row in result["criteria"]:
        assert_counts(row, 3, 3, 3, True)
        assert row["needs_review"] is False
        assert row["review_reason"] is None


def test_one_response_out_of_three_is_partial_without_retry_storm_or_score_change(record, checklist):
    secret_error = "request failed https://provider.invalid?api_key=DO_NOT_STORE"
    panel = ScriptedPanel([{}, RuntimeError(secret_error), RuntimeError(secret_error)])

    partial = evaluate_conversation(record, checklist, panel, votes=3)
    full = evaluate_conversation(record, checklist, ScriptedPanel([{}]), votes=3)

    assert partial["status"] == "ok"
    assert panel.calls == 3
    assert partial["score"] == full["score"]
    assert_counts(partial, 3, 1, 1, False)
    assert partial["review_reason"] == "Получена 1 из 3 проверок"
    assert partial["incomplete_criteria"] == ["contact", "empathy"]
    assert partial["needs_review"] is True
    assert len(partial["attempt_errors"]) == 2
    assert "DO_NOT_STORE" not in json.dumps(partial)
    for row, original in zip(partial["criteria"], full["criteria"]):
        assert_counts(row, 3, 1, 1, False)
        assert row["agreement"] == 1.0
        assert row["value"] == original["value"]
        assert row["confidence"] == original["confidence"]
        assert row["needs_review"] is True
        assert row["review_reason"] == "Получена 1 из 3 проверок"
        assert len(row["attempt_errors"]) == 2


@pytest.mark.parametrize("bad_response", [{"verdicts": {"contact": "invalid"}}, {"missing": ["contact"]}])
def test_successful_responses_are_counted_separately_from_valid_criterion_votes(record, checklist, bad_response):
    panel = ScriptedPanel([{}, bad_response, bad_response])

    result = evaluate_conversation(record, checklist, panel, votes=3)

    assert result["status"] == "ok"
    assert panel.calls == 3
    by_id = {row["criterion_id"]: row for row in result["criteria"]}
    assert_counts(by_id["contact"], 3, 3, 1, False)
    assert by_id["contact"]["needs_review"] is True
    assert_counts(by_id["empathy"], 3, 3, 3, True)
    assert by_id["empathy"]["needs_review"] is False
    assert result["incomplete_criteria"] == ["contact"]
    assert result["vote_counts"]["contact"]["valid_votes"] == 1


def test_repeat_three_checks_does_not_reuse_partial_rows_but_reuses_complete_rows(record, checklist):
    partial_panel = ScriptedPanel([{}, {"verdicts": {"contact": "invalid"}}, {"verdicts": {"contact": "invalid"}}])
    first = evaluate_conversation(record, checklist, partial_panel, votes=3)
    assert first["votes"] == 3
    fresh_panel = ScriptedPanel([{}])

    repeated = evaluate_conversation(record, checklist, fresh_panel, votes=3, previous=first)

    assert repeated["status"] == "ok"
    assert fresh_panel.calls == repeated["usage"]["calls"] == 3
    by_id = {row["criterion_id"]: row for row in repeated["criteria"]}
    assert by_id["contact"]["reused"] is False
    assert by_id["empathy"]["reused"] is True
    assert repeated["votes_complete"] is True
    assert by_id["contact"]["review_reason"] is None


@pytest.mark.parametrize("saved_votes", [None, [], 3, [{"verdict": "invalid"}]])
def test_unknown_or_invalid_historic_vote_count_never_satisfies_cache(record, checklist, saved_votes):
    first = evaluate_conversation(record, checklist, ScriptedPanel([{}]), votes=3)
    for row in first["criteria"]:
        row["votes"] = saved_votes
    first["votes"] = 999
    panel = ScriptedPanel([{}])

    repeated = evaluate_conversation(record, checklist, panel, votes=3, previous=first)

    assert panel.calls == 3
    assert not any(row["reused"] for row in repeated["criteria"])
    assert repeated["votes_complete"] is True


def test_weight_only_reuse_uses_actual_saved_votes_and_preserves_fallback(record, checklist):
    panel = ScriptedPanel([{"evidence": [999]}])
    first = evaluate_conversation(record, checklist, panel, votes=3)
    first.pop("votes")  # Historical top-level count is unnecessary when actual votes exist.
    changed = copy.deepcopy(checklist)
    changed["criteria"][0]["weight"] = 75

    repeated = evaluate_conversation(record, changed, panel, votes=3, previous=first)

    assert repeated["usage"]["calls"] == 0
    assert panel.calls == 3
    assert_counts(repeated, 3, 3, 3, True)
    for row in repeated["criteria"]:
        assert row["reused"] is True
        assert row["evidence_fallback"] is True
        assert row["confidence"] <= 0.5
        assert row["needs_review"] is True


def test_smaller_request_can_reuse_sufficient_partial_votes_without_stale_reason(record, checklist):
    first = evaluate_conversation(record, checklist, ScriptedPanel([{}, RuntimeError("fail"), RuntimeError("fail")]), votes=3)
    panel = ScriptedPanel([{}])

    repeated = evaluate_conversation(record, checklist, panel, votes=1, previous=first)

    assert panel.calls == repeated["usage"]["calls"] == 0
    assert_counts(repeated, 1, 1, 1, True)
    assert repeated["review_reason"] is None
    assert all(row["reused"] and row["review_reason"] is None and not row["needs_review"] for row in repeated["criteria"])


class FailedJudge:
    dead_reason = None

    def __init__(self, model="gemini:unavailable"):
        self.model = model
        self.spec = SimpleNamespace(provider="gemini", local=False)

    def complete_json(self, messages, schema=None):
        raise LLMError("provider body contains api_key=DO_NOT_STORE")


class LocalJudge(MockClient):
    def __init__(self):
        super().__init__("ollama:actual")
        self.spec = SimpleNamespace(provider="ollama", local=True)


def test_fallback_repeats_record_actual_single_model_and_safe_attempt_provenance(record, checklist):
    panel = JudgePanel([FailedJudge(), LocalJudge()])

    result = evaluate_conversation(record, checklist, panel, votes=3)

    assert result["status"] == "ok"
    assert_counts(result, 3, 3, 3, True)
    assert result["actual_models"] == ["ollama:actual"]
    assert {entry["model"] for entry in result["model_provenance"]} == {"gemini:unavailable", "ollama:actual"}
    assert result["attempt_errors"]
    assert all(error["provider"] == "gemini" and error["local"] is False for error in result["attempt_errors"])
    for row in result["criteria"]:
        assert len(row["votes"]) == 3
        assert all(vote["model"] == "ollama:actual" and vote["local"] is True and vote["provider"] == "ollama"
                   for vote in row["votes"])
    assert "DO_NOT_STORE" not in json.dumps(result)


def test_no_valid_response_remains_error_with_bounded_attempts_and_counts(record, checklist):
    panel = ScriptedPanel([{"verdict": "invalid"}])

    result = evaluate_conversation(record, checklist, panel, votes=3)

    assert result["status"] == "error"
    assert result["criteria"] == []
    assert result["score"] is None
    assert panel.calls == 3 + 2 * 2 * len(checklist["criteria"])
    assert_counts(result, 3, 7, 0, False)
    assert result["incomplete_criteria"] == ["contact", "empathy"]
    assert all(counts["valid_votes"] == 0 and not counts["votes_complete"] for counts in result["vote_counts"].values())
    assert result["actual_models"] == ["gemini:actual"]


def test_all_failed_attempts_preserve_provider_metadata_without_secret_messages(record, checklist):
    result = evaluate_conversation(record, checklist, JudgePanel([FailedJudge()]), votes=3)

    assert result["status"] == "error"
    assert_counts(result, 3, 0, 0, False)
    assert result["actual_models"] == []
    assert result["model_provenance"] == [{"model": "gemini:unavailable", "provider": "gemini", "local": False}]
    assert result["attempt_errors"]
    assert "DO_NOT_STORE" not in json.dumps(result)
    assert "api_key" not in result["error"]


def test_vote_metadata_never_extends_official_schema(record):
    checklist = ensure_defaults(load_default())
    result = evaluate_conversation(record, checklist, ScriptedPanel([{}, RuntimeError("fail"), RuntimeError("fail")]), votes=3)

    doc = build_results([result], [record["conversation_id"]], model_name="gemini:actual")

    assert doc["schema_version"] == "1.0"
    assert set(doc["results"][0]) == {"conversation_id", "status", "criteria", "score"}
    assert all(set(row) == {"criterion_id", "value", "rationale", "evidence_turns"} for row in doc["results"][0]["criteria"])


def test_outer_evaluation_failure_does_not_persist_raw_exception_text(record, checklist):
    panel = ScriptedPanel([{"confidence": "https://provider.invalid?api_key=DO_NOT_STORE"}])

    result = evaluate_many([record], checklist, panel, votes=3, attempts=1)[0]

    assert result["status"] == "error"
    assert result["votes_complete"] is False
    assert result["needs_review"] is True
    assert result["attempt_errors"] == [{"scope": "evaluation", "error_type": "ValueError"}]
    assert "DO_NOT_STORE" not in json.dumps(result)
