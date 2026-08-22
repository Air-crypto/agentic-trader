from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from agentic_trader.picker.validation import LivePickerPolicy, validate_picker_draft


def validate(draft, evidence, quant, critic, now, analyst_model_id="analyst-model"):
    return validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        critic,
        prompt_hash="a" * 64,
        model_id=analyst_model_id,
        now=now,
    )


def test_live_picker_default_caps_each_name_at_three_and_a_half_percent():
    assert LivePickerPolicy().max_stock_weight == 0.035


def test_grounded_independently_confirmed_draft_is_authorized(draft, evidence, quant, critic, now):
    result = validate(draft, evidence, quant, critic, now)
    assert result.accepted
    assert result.packet is not None
    assert result.packet.verify_hash()
    assert 0 < result.packet.target_weight <= 0.035
    assert result.packet.horizon_trading_days == 20


def test_one_primary_source_and_critic_pass_authorize(draft, evidence, quant, critic, now):
    single_source_draft = replace(
        draft,
        evidence_ids=("issuer-1",),
        event_quality=0.60,
        materiality=0.10,
        novelty=0.10,
        timing=0.35,
    )

    result = validate(single_source_draft, evidence[:1], quant, critic, now)

    assert result.accepted
    assert result.packet is not None
    assert result.packet.evidence_ids == ("issuer-1",)


def test_primary_source_must_bind_the_same_symbol_and_cik(draft, evidence, quant, critic, now):
    wrong_symbol = replace(evidence[0], symbol="OTHER")
    result = validate(draft, [wrong_symbol], quant, critic, now)
    assert not result.accepted
    assert "evidence_symbol_mismatch" in result.reasons
    assert "no_authoritative_primary_source" in result.reasons


def test_soft_concerns_use_structured_three_of_five_majority(draft, evidence, quant, critic, now):
    three_pass = replace(
        critic,
        soft_checks=(
            ("source_breadth", False),
            ("freshness", True),
            ("materiality", True),
            ("novelty", False),
            ("not_priced_in", True),
        ),
    )
    assert validate(draft, evidence, quant, three_pass, now).accepted

    two_pass = replace(
        critic,
        soft_checks=(
            ("source_breadth", False),
            ("freshness", True),
            ("materiality", True),
            ("novelty", False),
            ("not_priced_in", False),
        ),
    )
    result = validate(draft, evidence, quant, two_pass, now)
    assert not result.accepted
    assert "critic_verdict_policy_mismatch" in result.reasons


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


def test_critic_contradiction_hard_vetoes_even_with_pass(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, contradicted_evidence_ids=("issuer-1",)),
        now,
    )
    assert not result.accepted
    assert "critic_found_contradicted_evidence" in result.reasons


def test_structured_hard_veto_overrides_soft_majority(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, hard_vetoes=("ticker_cik_ambiguity",)),
        now,
    )
    assert not result.accepted
    assert "critic_hard_veto:ticker_cik_ambiguity" in result.reasons


def test_scheduled_independent_critic_models_can_authorize(draft, evidence, quant, critic, now):
    for model_id in ("gpt-5.5", "gpt-5.6-sol"):
        result = validate(
            draft,
            evidence,
            quant,
            replace(critic, model_id=model_id),
            now,
        )
        assert result.accepted


def test_same_allowed_model_cannot_authorize(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, model_id="gpt-5.5"),
        now,
        analyst_model_id="gpt-5.5",
    )
    assert not result.accepted
    assert "critic_model_not_independent" in result.reasons


def test_unapproved_or_prefix_spoofed_model_cannot_authorize(draft, evidence, quant, critic, now):
    result = validate(
        draft,
        evidence,
        quant,
        replace(critic, model_id="unapproved-critic"),
        now,
    )
    assert not result.accepted
    assert "critic_model_not_independent" in result.reasons
    fake_prefix = validate(
        draft,
        evidence,
        quant,
        replace(critic, model_id="gpt-5.6-sol-preview"),
        now,
    )
    assert "critic_model_not_independent" in fake_prefix.reasons


def test_unverified_quote_cannot_reach_live_packet(draft, evidence, quant, critic, now):
    evidence[0] = replace(evidence[0], quote_verified=False)
    result = validate(draft, evidence, quant, critic, now)
    assert not result.accepted
    assert "ungrounded_quote:issuer-1" in result.reasons


def test_future_evidence_is_rejected(draft, evidence, quant, critic, now):
    evidence[0] = replace(
        evidence[0],
        published_at=now + timedelta(minutes=1),
        first_seen_at=now + timedelta(minutes=1),
        retrieved_at=now + timedelta(minutes=2),
    )
    result = validate(draft, evidence, quant, critic, now)
    assert not result.accepted
    assert "future_evidence:issuer-1" in result.reasons


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


def test_future_or_agent_written_quant_snapshot_is_rejected(draft, evidence, quant, critic, now):
    future = validate(
        draft,
        evidence,
        replace(quant, as_of=now + timedelta(seconds=1)),
        critic,
        now,
    )
    untrusted = validate(
        draft,
        evidence,
        replace(quant, calculated_by="llm", data_snapshot_hash=""),
        critic,
        now,
    )
    assert "future_quant_snapshot" in future.reasons
    assert "quant_not_deterministically_computed" in untrusted.reasons


def test_llm_abstention_produces_no_packet(draft, evidence, quant, critic, now):
    result = validate(replace(draft, action="reject"), evidence, quant, critic, now)
    assert not result.accepted
    assert result.packet is None
    assert "draft_abstained" in result.reasons
