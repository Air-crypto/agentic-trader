from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from agentic_trader.picker.models import RESEARCH_MODEL_ID
from agentic_trader.picker.validation import LivePickerPolicy, validate_picker_draft


def validate(draft, evidence, quant, now):
    return validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )


def test_live_picker_default_caps_each_name_at_three_and_a_half_percent():
    assert LivePickerPolicy().max_stock_weight == 0.035


def test_grounded_draft_is_authorized_by_deterministic_gates(draft, evidence, quant, now):
    result = validate(draft, evidence, quant, now)
    assert result.accepted
    assert result.packet is not None
    assert result.packet.verify_hash()
    assert result.packet.model_id == RESEARCH_MODEL_ID
    assert 0 < result.packet.target_weight <= 0.035
    assert result.packet.horizon_trading_days == 20


def test_one_authoritative_primary_source_can_authorize(draft, evidence, quant, now):
    single_source_draft = replace(draft, evidence_ids=("issuer-1",))
    result = validate(single_source_draft, evidence[:1], quant, now)
    assert result.accepted
    assert result.packet is not None
    assert result.packet.evidence_ids == ("issuer-1",)


def test_primary_source_must_bind_the_same_symbol_and_cik(draft, evidence, quant, now):
    wrong_symbol = replace(evidence[0], symbol="OTHER")
    result = validate(draft, [wrong_symbol], quant, now)
    assert not result.accepted
    assert "evidence_symbol_mismatch" in result.reasons
    assert "no_authoritative_primary_source" in result.reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("event_quality", 0.59, "event_quality_below_gate"),
        ("materiality", 0.19, "materiality_below_gate"),
        ("novelty", 0.39, "novelty_below_gate"),
        ("timing", 0.34, "timing_below_gate"),
        ("speculation", 0.41, "speculation_above_gate"),
    ],
)
def test_structured_research_scores_fail_closed(draft, evidence, quant, now, field, value, reason):
    result = validate(replace(draft, **{field: value}), evidence, quant, now)
    assert not result.accepted
    assert reason in result.reasons


@pytest.mark.parametrize(
    "field",
    (
        "catalyst",
        "materiality_basis",
        "novelty_basis",
        "priced_in_analysis",
        "counter_thesis",
        "invalidation",
    ),
)
def test_required_research_analysis_cannot_be_blank(draft, evidence, quant, now, field):
    result = validate(replace(draft, **{field: "  "}), evidence, quant, now)
    assert not result.accepted
    assert f"missing_{field}" in result.reasons


def test_unverified_quote_cannot_reach_live_packet(draft, evidence, quant, now):
    evidence[0] = replace(evidence[0], quote_verified=False)
    result = validate(draft, evidence, quant, now)
    assert not result.accepted
    assert "ungrounded_quote:issuer-1" in result.reasons


def test_future_evidence_is_rejected(draft, evidence, quant, now):
    evidence[0] = replace(
        evidence[0],
        published_at=now + timedelta(minutes=1),
        first_seen_at=now + timedelta(minutes=1),
        retrieved_at=now + timedelta(minutes=2),
    )
    result = validate(draft, evidence, quant, now)
    assert not result.accepted
    assert "future_evidence:issuer-1" in result.reasons


def test_stale_quant_snapshot_is_rejected(draft, evidence, quant, now):
    result = validate(draft, evidence, replace(quant, as_of=now - timedelta(hours=4)), now)
    assert not result.accepted
    assert "stale_quant_snapshot" in result.reasons


def test_future_or_agent_written_quant_snapshot_is_rejected(draft, evidence, quant, now):
    future = validate(draft, evidence, replace(quant, as_of=now + timedelta(seconds=1)), now)
    untrusted = validate(
        draft,
        evidence,
        replace(quant, calculated_by="llm", data_snapshot_hash=""),
        now,
    )
    assert "future_quant_snapshot" in future.reasons
    assert "quant_not_deterministically_computed" in untrusted.reasons


def test_llm_abstention_produces_no_packet(draft, evidence, quant, now):
    result = validate(replace(draft, action="reject"), evidence, quant, now)
    assert not result.accepted
    assert result.packet is None
    assert "draft_abstained" in result.reasons
