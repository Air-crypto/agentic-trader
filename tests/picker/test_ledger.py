from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.validation import validate_picker_draft


def authorized_packet(draft, evidence, quant, critic, now):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        critic,
        prompt_hash="a" * 64,
        model_id="analyst-model",
        now=now,
    )
    assert result.packet is not None
    return result.packet


def populated_ledger(draft, evidence, critic, now):
    ledger = InMemoryLedger()
    ledger.put_run(
        draft.run_id,
        "account-hash",
        draft.created_at,
        now.date(),
        "analyst-model",
        "a" * 64,
    )
    for item in evidence:
        ledger.put_evidence(item)
    ledger.put_draft(draft)
    ledger.put_critic(critic)
    return ledger


def test_authorized_packet_round_trip(draft, evidence, quant, critic, now):
    ledger = populated_ledger(draft, evidence, critic, now)
    packet = authorized_packet(draft, evidence, quant, critic, now)
    ledger.authorize_packet(packet)
    assert ledger.authorized_packets(now.date(), now) == [packet]


def test_packet_requires_draft_and_critic(draft, evidence, quant, critic, now):
    ledger = InMemoryLedger()
    packet = authorized_packet(draft, evidence, quant, critic, now)
    with pytest.raises(ValueError, match="known draft and critic"):
        ledger.authorize_packet(packet)


def test_duplicate_symbol_action_day_is_rejected(draft, evidence, quant, critic, now):
    ledger = populated_ledger(draft, evidence, critic, now)
    first = authorized_packet(draft, evidence, quant, critic, now)
    ledger.authorize_packet(first)

    second_draft = replace(draft, draft_id="draft-2")
    second_critic = replace(critic, draft_id="draft-2")
    ledger.put_draft(second_draft)
    ledger.put_critic(second_critic)
    second = authorized_packet(second_draft, evidence, quant, second_critic, now)
    with pytest.raises(ValueError, match="already exists"):
        ledger.authorize_packet(second)
