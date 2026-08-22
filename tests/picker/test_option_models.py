from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
    OptionDraft,
)


def contract(now) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        option_id="option-1",
        contract_symbol="EXM260909C00100000",
        underlying="EXM",
        option_type="call",
        expiration_date=now.date() + timedelta(days=30),
        strike=100.0,
        bid=0.48,
        ask=0.52,
        quote_at=now - timedelta(seconds=10),
        underlying_price=100.0,
        delta=0.5,
        open_interest=500,
    )


def packet(now) -> OptionDecisionPacket:
    return OptionDecisionPacket(
        packet_id="option-packet-1",
        run_id="run-1",
        draft_id="option-draft-1",
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(seconds=60),
        underlying="EXM",
        action="long_call",
        contract=contract(now),
        quantity=1,
        side="buy",
        position_effect="open",
        limit_price=0.50,
        max_risk=50.0,
        collateral_required=0.0,
        shares_encumbered=0,
        evidence_ids=("issuer-1", "gov-1"),
        prompt_hash="a" * 64,
        model_id="option-model",
        draft_hash="b" * 64,
        horizon_trading_days=20,
        invalidation="Close if the evidence-grounded catalyst is disproven.",
    )


def test_option_draft_is_immutable_hash_bound_and_round_trips(now):
    draft = OptionDraft(
        draft_id="option-draft-1",
        run_id="run-1",
        created_at=now - timedelta(minutes=10),
        underlying="exm",
        action="long_call",
        thesis="A defined-risk call expresses the inherited bullish thesis.",
        evidence_ids=("issuer-1", "gov-1"),
        source_draft_id="draft-1",
    )

    assert draft.underlying == "EXM"
    assert draft.verify_hash()
    assert OptionDraft.from_dict(draft.to_dict()) == draft
    with pytest.raises(FrozenInstanceError):
        draft.action = "long_put"  # type: ignore[misc]

    tampered = draft.to_dict()
    tampered["action"] = "long_put"
    with pytest.raises(ValueError, match="hash mismatch"):
        OptionDraft.from_dict(tampered)


def test_standalone_option_draft_requires_horizon_and_falsifiable_fields(now):
    with pytest.raises(ValueError, match="catalyst"):
        OptionDraft(
            draft_id="standalone",
            run_id="run-1",
            created_at=now,
            underlying="EXM",
            action="long_put",
            thesis="A bearish standalone option thesis with bounded premium risk.",
            evidence_ids=("issuer-1", "gov-1"),
        )
    with pytest.raises(ValueError, match="horizon"):
        OptionDraft(
            draft_id="bad-horizon",
            run_id="run-1",
            created_at=now,
            underlying="EXM",
            action="long_put",
            thesis="A bearish standalone option thesis with bounded premium risk.",
            evidence_ids=("issuer-1", "gov-1"),
            source_draft_id="draft-1",
            horizon_trading_days=61,
        )


def test_option_contract_accepts_broker_native_aliases_and_hashes_quote(now):
    snapshot = OptionContractSnapshot.from_broker_dict(
        {
            "id": "option-1",
            "symbol": "EXM260909C00100000",
            "chain_symbol": "exm",
            "type": "call",
            "expiration_date": (now.date() + timedelta(days=30)).isoformat(),
            "strike_price": "100.00",
            "bid_price": "0.48",
            "ask_price": "0.52",
            "updated_at": (now - timedelta(seconds=10)).isoformat(),
            "underlying_price": "100.00",
            "delta": "0.50",
            "open_interest": 500,
        }
    )

    assert snapshot.option_id == "option-1"
    assert snapshot.contract_id == "option-1"
    assert snapshot.midpoint == 0.50
    assert snapshot.spread_pct_midpoint == pytest.approx(0.08)
    assert snapshot.verify_hash()
    assert OptionContractSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert not replace(snapshot, ask=0.53).verify_hash()


def test_option_packet_is_single_contract_limit_order_and_tamper_evident(now):
    decision = packet(now)

    assert decision.verify_hash()
    assert decision.strategy == "long_call"
    assert decision.option_id == "option-1"
    assert decision.order_type == "limit"
    assert decision.time_in_force == "gfd"
    assert decision.session == "regular"
    assert OptionDecisionPacket.from_dict(decision.to_dict()) == decision
    assert not replace(decision, limit_price=0.51).verify_hash()

    tampered = decision.to_dict()
    tampered["contract"]["strike"] = 105.0
    with pytest.raises(ValueError, match="hash mismatch"):
        OptionDecisionPacket.from_dict(tampered)


def test_option_packet_rejects_more_than_one_contract(now):
    with pytest.raises(ValueError, match="exactly one"):
        replace(packet(now), quantity=2, packet_hash="")


def test_active_option_position_round_trips_with_content_hash(now):
    decision = packet(now)
    position = ActiveOptionPosition(
        position_id="position-1",
        packet_id=decision.packet_id,
        underlying=decision.underlying,
        strategy=decision.action,
        option_id=decision.option_id,
        contract_symbol=decision.contract.contract_symbol,
        option_type=decision.contract.option_type,
        expiration_date=decision.contract.expiration_date,
        strike=decision.contract.strike,
        quantity=1,
        side="long",
        opened_at=now,
        average_open_price=decision.limit_price,
        premium_at_risk=decision.max_risk,
        collateral_reserved=0.0,
        shares_encumbered=0,
        status="open",
        structure_fingerprint=decision.structure_fingerprint,
    )

    assert position.verify_hash()
    assert ActiveOptionPosition.from_dict(position.to_dict()) == position
    assert not replace(position, status="closed").verify_hash()

    tampered = position.to_dict()
    tampered["premium_at_risk"] = 75.0
    with pytest.raises(ValueError, match="hash mismatch"):
        ActiveOptionPosition.from_dict(tampered)
