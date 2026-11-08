"""Client-exit events retain their source turn and require a system assertion."""
from __future__ import annotations

import pytest

from cqa.facts import compute_facts, facts_to_text


def _conversation(event, speaker="system"):
    return {
        "channel": "agent chat",
        "language": "KZ",
        "transcript": [
            {"speaker": "client", "timestamp": "00:00", "text": "Сәлеметсіз бе!"},
            {"speaker": "operator", "timestamp": "00:10", "text": "Сәлеметсіз бе! Қалай көмектесе аламын?"},
            {"speaker": speaker, "timestamp": "00:30", "text": event},
        ],
    }


@pytest.mark.parametrize("event", [
    "Клиент операторға сұрағын жазбай тұрып, чаттан шықты.",
    "Клиент чаттан шықты.",
    "Клиент чаттан шығып кетті.",
    "Клиентпен байланыс үзілді.",
    "Клиент покинул чат.",
    "Соединение с клиентом прервано.",
    "Customer left the chat.",
    "Connection to customer lost.",
])
def test_system_exit_preserves_evidence_turn_and_prompt_fact(event):
    facts = compute_facts(_conversation(event))
    assert facts["client_left_turn"] == 3
    text = facts_to_text(facts)
    assert "Клиент ушёл / связь прервалась / клиент завершил разговор: реплика 3." in text
    assert "Системного события об уходе клиента или обрыве связи нет" not in text


@pytest.mark.parametrize("speaker", ["client", "operator", "bot"])
def test_quoted_exit_outside_system_does_not_become_event(speaker):
    facts = compute_facts(_conversation("Клиент чаттан шықты.", speaker))
    assert facts["client_left_turn"] is None


@pytest.mark.parametrize("event", [
    "Клиент чаттан шықты ма?",
    "Клиент чаттан шыққан жоқ.",
    "Клиент чаттан шықты деген ақпарат расталмады.",
    "Клиент чатқа қосылды.",
    "Чат операторға жіберілді.",
    "Оператор чаттан шықты.",
])
def test_questions_negation_and_unrelated_events_do_not_assert_exit(event):
    assert compute_facts(_conversation(event))["client_left_turn"] is None
