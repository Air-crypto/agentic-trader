from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from agentic_trader.picker.validation import validate_picker_draft


def validate(draft, evidence, quant, critic, now):
    return validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        critic,
        prompt_hash="a" * 64,
        model_id="analyst-model",
        now=now,
    )


def test_grounded_independently_confirmed_draft_is_authorized(draft, evidence, quant, critic, now):
    result = validate(draft, evidence, quant, critic, now)
    assert result.accepted
    assert result.packet is not None
    assert result.packet.verify_hash()
    assert 0 < result.packet.target_weight <= 0.15
    assert result.packet.horizon_trading_days == 20


def test_critic_veto_fails_closed(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, verdict="veto", reasons=("Source contradicts issuer",)),
        now,
    )
    assert not result.accepted
    assert "critic_veto" in result.reasons


def test_same_model_or_non_grok_critic_cannot_authorize(
    draft, evidence, quant, critic, now
):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, model_id="analyst-model"),
        now,
    )
    assert not result.accepted
    assert "critic_model_not_independent" in result.reasons


def test_unverified_quote_cannot_reach_live_packet(draft, evidence, quant, critic, now):
    evidence[0] = replace(evidence[0], quote_verified=False)
    result = validate(draft, evidence, quant, critic, now)
    assert not result.accepted
    assert "ungrounded_quote:sec-1" in result.reasons


def test_future_evidence_is_rejected(draft, evidence, quant, critic, now):
    evidence[0] = replace(
        evidence[0],
        published_at=now + timedelta(minutes=1),
        first_seen_at=now + timedelta(minutes=1),
        retrieved_at=now + timedelta(minutes=2),
    )
    result = validate(draft, evidence, quant, critic, now)
    assert not result.accepted
    assert "future_evidence:sec-1" in result.reasons


def test_stale_quant_snapshot_is_rejected(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        replace(quant, as_of=now - timedelta(hours=4)),
        critic,
        now,
    )
    assert not result.accepted
    assert "stale_quant_snapshot" in result.reasons


def test_llm_abstention_produces_no_packet(draft, evidence, quant, critic, now):
    result = validate(replace(draft, action="reject"), evidence, quant, critic, now)
    assert not result.accepted
    assert result.packet is None
    assert "draft_abstained" in result.reasons
