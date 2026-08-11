from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDraft,
)
from agentic_trader.picker.option_validation import (
    LiveOptionPolicy,
    select_option_contract,
    validate_option_draft,
)


def option_draft(now, action: str = "long_call") -> OptionDraft:
    return OptionDraft(
        draft_id=f"option-{action}",
        run_id="run-1",
        created_at=now - timedelta(minutes=10),
        underlying="EXM",
        action=action,
        thesis=f"The {action} structure expresses the inherited evidence with bounded sizing.",
        evidence_ids=("sec-1", "gov-1"),
        source_draft_id="draft-1",
    )


def snapshot(
    now,
    *,
    option_id: str = "option-1",
    option_type: str = "call",
    strike: float = 100.0,
    spot: float = 100.0,
    bid: float = 0.48,
    ask: float = 0.52,
    age_seconds: int = 10,
    dte: int = 30,
    delta: float | None = 0.5,
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        option_id=option_id,
        contract_symbol=option_id.upper(),
        underlying="EXM",
        option_type=option_type,
        expiration_date=now.date() + timedelta(days=dte),
        strike=strike,
        bid=bid,
        ask=ask,
        quote_at=now - timedelta(seconds=age_seconds),
        underlying_price=spot,
        delta=delta,
        open_interest=100,
    )


def authorize(
    option,
    contracts,
    evidence,
    critic,
    draft,
    now,
    *,
    account_equity=2_000.0,
    **kwargs,
):
    return validate_option_draft(
        option,
        {item.evidence_id: item for item in evidence},
        contracts,
        critic,
        prompt_hash="a" * 64,
        model_id="option-model",
        account_equity=account_equity,
        source_draft=draft,
        now=now,
        **kwargs,
    )


def test_selector_deterministically_prefers_target_delta_then_dte(now):
    contracts = [
        snapshot(now, option_id="later", dte=35, delta=0.50),
        snapshot(now, option_id="wrong-delta", dte=30, delta=0.70),
        snapshot(now, option_id="winner", dte=30, delta=0.50),
    ]

    selected = select_option_contract("long_call", reversed(contracts), now=now, underlying="EXM")

    assert selected.option_id == "winner"


def test_selector_accepts_exact_quote_and_spread_boundaries(now):
    boundary = snapshot(now, bid=0.95, ask=1.05, age_seconds=60, dte=21)

    assert (
        select_option_contract("long_call", [boundary], now=now, underlying="EXM")
        == boundary
    )


@pytest.mark.parametrize(
    ("changes", "action"),
    [
        ({"dte": 20}, "long_call"),
        ({"dte": 61}, "long_call"),
        ({"age_seconds": 61}, "long_call"),
        ({"bid": 0.0}, "long_call"),
        ({"bid": 0.45, "ask": 0.55}, "long_call"),
        ({"option_type": "put"}, "long_call"),
    ],
)
def test_selector_fails_closed_for_ineligible_contract(now, changes, action):
    with pytest.raises(ValueError, match="No eligible"):
        select_option_contract(action, [snapshot(now, **changes)], now=now, underlying="EXM")


def test_selector_requires_otm_collateralized_contracts(now):
    with pytest.raises(ValueError, match="No eligible"):
        select_option_contract(
            "covered_call",
            [snapshot(now, strike=99.0)],
            now=now,
            underlying="EXM",
        )
    with pytest.raises(ValueError, match="No eligible"):
        select_option_contract(
            "cash_secured_put",
            [snapshot(now, option_type="put", strike=101.0)],
            now=now,
            underlying="EXM",
        )


def test_grounded_long_call_authorizes_one_limit_contract(
    now, evidence, critic, draft
):
    result = authorize(option_draft(now), [snapshot(now)], evidence, critic, draft, now)

    assert result.accepted
    assert result.packet is not None
    assert result.packet.quantity == 1
    assert result.packet.side == "buy"
    assert result.packet.position_effect == "open"
    assert result.packet.limit_price == 0.50
    assert result.packet.max_risk == 50.0
    assert result.packet.verify_hash()


def test_one_inherited_primary_source_and_critic_pass_authorize(
    now, evidence, critic, draft
):
    source_draft = replace(draft, evidence_ids=("sec-1",))
    option = replace(
        option_draft(now),
        evidence_ids=("sec-1",),
        draft_hash="",
    )

    result = authorize(
        option,
        [snapshot(now)],
        evidence[:1],
        critic,
        source_draft,
        now,
    )

    assert result.accepted
    assert result.packet is not None
    assert result.packet.evidence_ids == ("sec-1",)


def test_quantity_policy_cannot_expand_single_contract_authority(
    now, evidence, critic, draft
):
    with pytest.raises(ValueError, match="quantity"):
        LiveOptionPolicy(quantity=2)


def test_packet_hash_and_id_are_deterministic(now, evidence, critic, draft):
    first = authorize(option_draft(now), [snapshot(now)], evidence, critic, draft, now)
    second = authorize(option_draft(now), [snapshot(now)], evidence, critic, draft, now)

    assert first.packet is not None
    assert second.packet is not None
    assert first.packet.packet_id == second.packet.packet_id
    assert first.packet.packet_hash == second.packet.packet_hash


def test_long_premium_trade_and_aggregate_caps_are_enforced(
    now, evidence, critic, draft
):
    expensive = authorize(
        option_draft(now),
        [snapshot(now, bid=0.72, ask=0.78)],
        evidence,
        critic,
        draft,
        now,
        account_equity=1_000.0,
    )
    aggregate = authorize(
        option_draft(now),
        [snapshot(now)],
        evidence,
        critic,
        draft,
        now,
        aggregate_open_premium_at_risk=160.0,
    )

    assert not expensive.accepted
    assert "long_premium_risk_exceeds_trade_cap" in expensive.reasons
    assert not aggregate.accepted
    assert "aggregate_long_premium_risk_exceeds_cap" in aggregate.reasons


def test_nonfinite_risk_inputs_and_policy_limits_fail_closed(
    now, evidence, critic, draft
):
    result = authorize(
        option_draft(now),
        [snapshot(now)],
        evidence,
        critic,
        draft,
        now,
        account_equity=float("nan"),
    )
    assert not result.accepted
    assert "account_equity_not_positive" in result.reasons
    with pytest.raises(ValueError, match="hard ceiling"):
        LiveOptionPolicy(max_spread_pct_midpoint=float("nan"))


def test_covered_call_requires_and_encumbers_100_free_shares(
    now, evidence, critic, draft
):
    covered_call = option_draft(now, "covered_call")
    insufficient = authorize(
        covered_call,
        [snapshot(now, strike=105.0, delta=0.3)],
        evidence,
        critic,
        draft,
        now,
        underlying_shares=100,
        encumbered_shares=1,
    )
    accepted = authorize(
        covered_call,
        [snapshot(now, strike=105.0, delta=0.3)],
        evidence,
        critic,
        draft,
        now,
        unencumbered_shares=100,
    )

    assert not insufficient.accepted
    assert "insufficient_unencumbered_shares" in insufficient.reasons
    assert accepted.packet is not None
    assert accepted.packet.side == "sell"
    assert accepted.packet.shares_encumbered == 100


def test_csp_requires_cash_and_both_collateral_and_assignment_caps(
    now, evidence, critic, draft
):
    csp = option_draft(now, "cash_secured_put")
    contract = snapshot(
        now,
        option_type="put",
        strike=10.0,
        spot=10.0,
        delta=-0.3,
    )
    accepted = authorize(
        csp,
        [contract],
        evidence,
        critic,
        draft,
        now,
        account_equity=10_000.0,
        available_cash=1_000.0,
    )
    rejected = authorize(
        csp,
        [contract],
        evidence,
        critic,
        draft,
        now,
        account_equity=3_000.0,
        available_cash=900.0,
        current_underlying_value=100.0,
    )

    assert accepted.packet is not None
    assert accepted.packet.collateral_required == 1_000.0
    assert "csp_collateral_exceeds_equity_cap" in rejected.reasons
    assert "insufficient_cash_for_csp" in rejected.reasons
    assert "post_assignment_issuer_limit_exceeded" in rejected.reasons


def test_source_evidence_and_critic_are_inherited_exactly(
    now, evidence, critic, draft
):
    changed = replace(option_draft(now), evidence_ids=("sec-1",), draft_hash="")
    result = authorize(changed, [snapshot(now)], evidence, critic, draft, now)

    assert not result.accepted
    assert "evidence_not_inherited_exactly" in result.reasons
    assert "fewer_than_two_independent_sources" not in result.reasons


def test_critic_veto_blocks_option_authorization(now, evidence, critic, draft):
    veto = replace(critic, verdict="veto", reasons=("Option expression is unsuitable.",))
    result = authorize(option_draft(now), [snapshot(now)], evidence, veto, draft, now)

    assert not result.accepted
    assert "critic_veto" in result.reasons


def test_critic_contradiction_hard_vetoes_option_with_pass(
    now, evidence, critic, draft
):
    contradicted = replace(critic, contradicted_evidence_ids=("sec-1",))
    result = authorize(
        option_draft(now),
        [snapshot(now)],
        evidence,
        contradicted,
        draft,
        now,
    )

    assert not result.accepted
    assert "critic_found_contradicted_evidence" in result.reasons


def test_option_authorization_requires_independent_grok_critic(
    now, evidence, critic, draft
):
    same_model = replace(critic, model_id="option-model")
    result = authorize(
        option_draft(now),
        [snapshot(now)],
        evidence,
        same_model,
        draft,
        now,
    )
    assert not result.accepted
    assert "critic_model_not_independent" in result.reasons


def test_close_uses_existing_contract_and_bypasses_entry_dte(
    now, evidence, critic
):
    open_contract = snapshot(now, dte=10)
    position = ActiveOptionPosition(
        position_id="position-1",
        packet_id="packet-1",
        underlying="EXM",
        strategy="long_call",
        option_id=open_contract.option_id,
        contract_symbol=open_contract.contract_symbol,
        option_type="call",
        expiration_date=open_contract.expiration_date,
        strike=open_contract.strike,
        quantity=1,
        side="long",
        opened_at=now - timedelta(days=20),
        average_open_price=0.40,
        premium_at_risk=40.0,
        collateral_reserved=0.0,
        shares_encumbered=0,
        status="open",
        structure_fingerprint="f" * 64,
    )
    close = OptionDraft(
        draft_id="option-close",
        run_id="run-1",
        created_at=now - timedelta(minutes=2),
        underlying="EXM",
        action="close",
        thesis="Close the existing option before expiration risk increases materially.",
        evidence_ids=(),
        position_id=position.position_id,
        contract_id=position.option_id,
    )
    close_critic = replace(
        critic,
        draft_id=close.draft_id,
        created_at=now - timedelta(minutes=1),
    )

    result = validate_option_draft(
        close,
        {item.evidence_id: item for item in evidence},
        [open_contract],
        close_critic,
        prompt_hash="a" * 64,
        model_id="option-model",
        account_equity=2_000.0,
        open_positions=(position,),
        now=now,
    )

    assert result.packet is not None
    assert result.packet.action == "close"
    assert result.packet.side == "sell"
    assert result.packet.position_effect == "close"
