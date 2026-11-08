"""Проверки ядра: скоринг по правилам чек-листа, факты, формат выгрузки. Запуск: python -m pytest -q"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ["LLM_PROVIDER"] = "mock"
os.environ["CQA_DATA_DIR"] = str(Path(__file__).parent / "_tmp_data")
os.environ["CQA_RUNS_DIR"] = str(Path(__file__).parent / "_tmp_runs")

from cqa.checklist import ensure_defaults, load_default, validate_checklist, verdict_to_value  # noqa: E402
from cqa.dataset import normalize_record, parse_plain_text, records_from_upload  # noqa: E402
from cqa.evaluator import aggregate_votes, evaluate_conversation, rescore  # noqa: E402
from cqa.export import build_results  # noqa: E402
from cqa.facts import compute_facts, parse_ts  # noqa: E402
from cqa.llm import JudgePanel, LLMResult, MockClient  # noqa: E402
from cqa.scoring import compute_score  # noqa: E402
from cqa.validate import validate_results  # noqa: E402

CL = ensure_defaults(load_default())
FIXTURE = json.loads((Path(__file__).parent / "fixture_chats.json").read_text(encoding="utf-8"))
RECORDS = [normalize_record(r) for r in FIXTURE["data"]["records"]]
BY_ID = {r["conversation_id"]: r for r in RECORDS}


def test_default_checklist_valid():
    assert validate_checklist(CL) == []
    assert sum(c["weight"] for c in CL["criteria"]) == 100


def test_score_example_from_appendix():
    # Пример из приложения к гайду: итог 85.38
    values = {"contact": True, "discovery": None, "identification": None, "consultation": 15,
              "empathy": True, "dialogue": 7, "closure": True, "critical": False}
    s = compute_score(CL, values)
    assert s["final_score"] == 85.38
    assert s["critical_flag"] is False
    assert s["applicable_weight"] == 65


def test_score_all_yes_and_critical_does_not_zero():
    values = {"contact": True, "discovery": True, "identification": True, "consultation": 20,
              "empathy": True, "dialogue": 10, "closure": True, "critical": True}
    s = compute_score(CL, values)
    assert s["final_score"] == 100.0
    assert s["critical_flag"] is True


def test_score_no_applicable_weight():
    cl = {"criteria": [{"id": "a_b", "name": "A", "type": "binary", "weight": 10, "na_allowed": True}]}
    assert compute_score(cl, {"a_b": None})["final_score"] is None


def test_round_half_up():
    # 68.5 из 80 = 85.625 → 85.63 (а не банковское 85.62)
    cl = {"criteria": [
        {"id": "aa", "name": "A", "type": "scale", "weight": 70, "scale": [{"value": 140}, {"value": 0}], "na_allowed": False},
        {"id": "bb", "name": "B", "type": "binary", "weight": 10, "na_allowed": False},
    ]}
    s = compute_score(cl, {"aa": 117, "bb": True})  # 117/140*70 = 58.5; +10 = 68.5 из 80
    assert s["final_score"] == 85.63


def test_verdict_mapping():
    c = next(x for x in CL["criteria"] if x["id"] == "consultation")
    assert verdict_to_value(c, "15") == (15, True)
    assert verdict_to_value(c, "na") == (None, True)
    assert verdict_to_value(c, "13") == (15, True)
    contact = next(x for x in CL["criteria"] if x["id"] == "contact")
    assert verdict_to_value(contact, "na") == (None, False)
    assert verdict_to_value(contact, "yes") == (True, True)


def test_timestamps():
    assert parse_ts("01:15") == 75
    assert parse_ts("75:30") == 4530
    assert parse_ts("1:02:03") == 3723
    a = parse_ts("2026-11-03T13:10:00.6388245+00:00")
    b = parse_ts("2026-11-03T13:10:15.5558925+00:00")
    assert round(b - a, 1) == 14.9


def test_facts_identity_and_timing():
    f = compute_facts(BY_ID["p1-ru-006"])
    assert f["transfer_turn"] == 9
    assert f["first_operator_turn"] == 10
    assert f["first_response_delay_sec"] == 132
    assert f["first_response_exceeds"] is False
    assert f["identity_verified_turns"] == [12]
    f2 = compute_facts(BY_ID["p1-ru-025"])
    assert f2["client_left_turn"] == 11
    f3 = compute_facts(BY_ID["p1-ru-028"])
    assert f3["long_pauses"] and f3["long_pauses"][0]["warned"] is True
    f4 = compute_facts(BY_ID["p1-en-013"])
    assert f4["operator_closed_turn"] == 14


def test_plain_text_upload():
    turns = parse_plain_text("[00:01] Клиент: Привет\nОператор: Здравствуйте, меня зовут Аня\nКлиент: Спасибо")
    assert [t["speaker"] for t in turns] == ["client", "operator", "client"]
    recs = records_from_upload({"conversation_id": "x-1", "transcript": turns})
    assert recs[0]["conversation_id"] == "x-1"


def test_pipeline_mock_and_export_validate():
    client = JudgePanel([MockClient("mock:a"), MockClient("mock:b"), MockClient("mock:c")])
    results = [evaluate_conversation(r, CL, client, votes=2) for r in RECORDS]
    assert all(r["status"] == "ok" for r in results)
    doc = build_results(results, [r["conversation_id"] for r in RECORDS], model_name="mock", team_name="T")
    turn_counts = {r["conversation_id"]: len(r["transcript"]) for r in RECORDS}
    errors, _ = validate_results(doc, [r["conversation_id"] for r in RECORDS], turn_counts, expected_count=len(RECORDS))
    assert errors == []
    # переиспользование: смена веса не вызывает модель
    cl2 = json.loads(json.dumps(CL))
    next(c for c in cl2["criteria"] if c["id"] == "closure")["weight"] = 25
    again = evaluate_conversation(RECORDS[0], cl2, client, votes=2, previous=results[0])
    assert again["usage"]["calls"] == 0
    assert all(r["reused"] for r in again["criteria"])
    # замена формулировки одного критерия — перегоняется только он
    cl3 = json.loads(json.dumps(CL))
    next(c for c in cl3["criteria"] if c["id"] == "empathy")["description"] = "Новая формулировка"
    third = evaluate_conversation(RECORDS[0], cl3, client, votes=2, previous=results[0])
    assert third["usage"]["calls"] == 2  # пакетный режим: 2 голоса × 1 вызов только с изменённым критерием
    assert [r["criterion_id"] for r in third["criteria"] if not r["reused"]] == ["empathy"]
    # голоса раздаются разным судьям
    models = {v["model"] for r in results[0]["criteria"] for v in r["votes"]}
    assert len(models) == 2


def test_per_criterion_mode():
    client = JudgePanel([MockClient("mock:a")])
    res = evaluate_conversation(RECORDS[1], CL, client, votes=1, mode="per_criterion")
    assert res["status"] == "ok" and res["usage"]["calls"] == len(CL["criteria"])


def test_rescore_with_override():
    res = evaluate_conversation(RECORDS[0], CL, MockClient(), votes=1)
    before = res["score"]["final_score"]
    after = rescore(res, CL, {"empathy": False})
    assert after["score"]["final_score"] < before
    row = next(r for r in after["criteria"] if r["criterion_id"] == "empathy")
    assert row["overridden"] is True and row["ai_value"] is True


def test_validator_catches_errors():
    bad = {"schema_version": "1.0", "team_name": "T", "model": {"mode": "local", "name": "m"}, "results": [
        {"conversation_id": "a", "status": "ok", "criteria": [{"criterion_id": "contact", "value": None, "rationale": "x", "evidence_turns": [1]}],
         "score": {"final_score": 50, "critical_flag": False}},
        {"conversation_id": "a", "status": "error", "criteria": [], "score": None, "error": ""},
    ]}
    errors, _ = validate_results(bad, ["a"], expected_count=1)
    text = " ".join(errors)
    assert "повтор" in text and "8 критериев" in text and "status=error" in text and "Ожидалось записей" in text


def test_reuse_requires_same_input_and_enough_votes():
    client = JudgePanel([MockClient("mock:a"), MockClient("mock:b"), MockClient("mock:c")])
    first = evaluate_conversation(RECORDS[0], CL, client, votes=1)
    assert first["status"] == "ok" and first["input_hash"]
    # больше голосов, чем было, — оценка заново, а не старый один голос под видом трёх
    more = evaluate_conversation(RECORDS[0], CL, client, votes=3, previous=first)
    assert more["usage"]["calls"] == 3
    assert not any(r["reused"] for r in more["criteria"])
    # меньше или столько же голосов — переиспользование
    same = evaluate_conversation(RECORDS[0], CL, client, votes=1, previous=more)
    assert same["usage"]["calls"] == 0
    # тот же ID, другой текст — оценка заново
    changed = json.loads(json.dumps(RECORDS[0]))
    changed["transcript"][-1]["text"] += " (изменено)"
    fresh = evaluate_conversation(changed, CL, client, votes=1, previous=first)
    assert fresh["usage"]["calls"] == 1
    assert fresh["input_hash"] != first["input_hash"]


def test_reimport_with_changed_transcript_marks_old_evaluation_stale(tmp_path):
    from cqa.store import Store
    store = Store(tmp_path / "t.db")
    rec = json.loads(json.dumps(RECORDS[0]))
    store.upsert_conversations([rec], "G")
    res = evaluate_conversation(rec, CL, MockClient(), votes=1)
    store.save_evaluation(res, None)
    assert store.latest_evaluation(rec["conversation_id"], ok_only=True) is not None
    store.upsert_conversations([rec], "G")  # то же содержание — оценка остаётся актуальной
    assert store.latest_evaluation(rec["conversation_id"], ok_only=True) is not None
    rec2 = json.loads(json.dumps(rec))
    rec2["transcript"][0]["text"] = "Другой диалог с тем же ID"
    store.upsert_conversations([rec2], "G")
    assert store.latest_evaluation(rec["conversation_id"], ok_only=True) is None


@pytest.mark.parametrize("evidence", [[999], [], None, [0, -1, "invalid"]])
def test_fallback_evidence_requires_review_and_caps_confidence(evidence):
    criterion = next(c for c in CL["criteria"] if c["id"] == "contact")
    facts = compute_facts(BY_ID["p1-ru-006"])
    vote = LLMResult(data={
        "verdict": "yes", "rationale": "Оператор поздоровался.",
        "evidence_turns": evidence, "confidence": 0.99,
    }, raw="", model="test:evidence")

    row = aggregate_votes(criterion, [vote], facts)

    assert row["value"] is True
    assert row["verdict"] == "yes"
    assert row["rationale"] == vote.data["rationale"]
    assert row["evidence_turns"] == [10]
    assert row["evidence_fallback"] is True
    assert row["needs_review"] is True
    assert row["confidence"] <= 0.5
    assert row["model_confidence"] == 0.99
    assert row["votes"][0]["evidence_turns"] == evidence


@pytest.mark.parametrize("evidence", [[10], [999, 10]])
def test_valid_evidence_does_not_gain_fallback_or_review(evidence):
    criterion = next(c for c in CL["criteria"] if c["id"] == "contact")
    facts = compute_facts(BY_ID["p1-ru-006"])
    vote = LLMResult(data={
        "verdict": "yes", "rationale": "Оператор поздоровался.",
        "evidence_turns": evidence, "confidence": 0.99,
    }, raw="", model="test:evidence")

    row = aggregate_votes(criterion, [vote], facts)

    assert row["evidence_turns"] == [10]
    assert row["evidence_fallback"] is False
    assert row["needs_review"] is False
    assert row["confidence"] > 0.5


class EvidenceClient(MockClient):
    """Offline model response with controlled evidence and high self-confidence."""

    def __init__(self, evidence):
        super().__init__("mock:evidence")
        self.evidence = evidence

    def complete_json(self, messages, schema=None):
        result = super().complete_json(messages, schema)
        rows = result.data["criteria"].values() if "criteria" in result.data else [result.data]
        for row in rows:
            row["evidence_turns"] = list(self.evidence)
            row["confidence"] = 0.99
        return result


@pytest.mark.parametrize("mode", ["batch", "per_criterion"])
def test_pipeline_fallback_preserves_values_scores_and_export_schema(mode):
    record = BY_ID["p1-ru-006"]
    valid = evaluate_conversation(record, CL, EvidenceClient([10]), votes=1, mode=mode)
    fallback = evaluate_conversation(record, CL, EvidenceClient([999]), votes=1, mode=mode)

    assert fallback["status"] == valid["status"] == "ok"
    assert fallback["score"] == valid["score"]
    assert [(r["criterion_id"], r["value"], r["points"]) for r in fallback["criteria"]] == [
        (r["criterion_id"], r["value"], r["points"]) for r in valid["criteria"]
    ]
    for row in fallback["criteria"]:
        assert row["evidence_fallback"] is True
        assert row["needs_review"] is True
        assert row["confidence"] <= 0.5
        assert row["evidence_turns"]
        assert all(1 <= n <= len(record["transcript"]) for n in row["evidence_turns"])
    contact = next(r for r in fallback["criteria"] if r["criterion_id"] == "contact")
    assert contact["evidence_turns"] == [10]

    doc = build_results([fallback], [record["conversation_id"]], model_name="test:evidence")
    assert doc["schema_version"] == "1.0"
    assert all(set(row) == {"criterion_id", "value", "rationale", "evidence_turns"}
               for row in doc["results"][0]["criteria"])
    assert validate_results(doc, [record["conversation_id"]], {record["conversation_id"]: len(record["transcript"])})[0] == []


def test_fallback_marker_survives_storage_weight_reuse_and_rescore(tmp_path, monkeypatch):
    from cqa.store import Store

    record = BY_ID["p1-ru-006"]
    client = EvidenceClient([999])
    first = evaluate_conversation(record, CL, client, votes=1)
    store = Store(tmp_path / "evidence.sqlite")
    try:
        store.upsert_conversations([record], "DEV")
        evaluation_id = store.save_evaluation(first, None)
        previous = store.get_evaluation(evaluation_id)
        assert all(row["evidence_fallback"] is True for row in previous["criteria"])

        changed = json.loads(json.dumps(CL))
        next(c for c in changed["criteria"] if c["id"] == "closure")["weight"] = 25

        def unexpected_call(*args, **kwargs):
            pytest.fail("Смена веса не должна вызывать модель")

        monkeypatch.setattr(client, "complete_json", unexpected_call)
        reused = evaluate_conversation(record, changed, client, votes=1, previous=previous)
        rescored = rescore(reused, changed, {"empathy": False})

        assert reused["usage"]["calls"] == 0
        assert all(row["reused"] for row in reused["criteria"])
        assert next(row for row in rescored["criteria"] if row["criterion_id"] == "empathy")["value"] is False
        for result in (reused, rescored):
            assert result["status"] == "ok"
            assert all(row["evidence_fallback"] is True for row in result["criteria"])
            assert all(row["needs_review"] is True and row["confidence"] <= 0.5 for row in result["criteria"])
    finally:
        store.conn.close()
