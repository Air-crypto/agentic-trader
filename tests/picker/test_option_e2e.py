from __future__ import annotations

from datetime import timedelta

from agentic_trader.option_execution import (
    OptionAccountSnapshot,
    ProposedOptionOrder,
    evaluate_option_order,
)
from agentic_trader.option_reconcile import reconcile_option_orders
from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDraft,
)
from agentic_trader.picker.option_validation import validate_option_draft


def test_authorize_plan_reconcile_and_sync_fixture(now, evidence, draft, tmp_path):
    option_draft = OptionDraft(
        draft_id="option-draft-1",
        run_id=draft.run_id,
        created_at=now - timedelta(minutes=1),
        underlying=draft.symbol,
        action="long_call",
        thesis="A bounded long-call expression of the evidence-grounded equity thesis.",
        evidence_ids=draft.evidence_ids,
        source_draft_id=draft.draft_id,
    )
    contract = OptionContractSnapshot(
        option_id="option-contract-1",
        contract_symbol="EXM260911C00100000",
        underlying=draft.symbol,
        option_type="call",
        expiration_date=now.date() + timedelta(days=32),
        strike=100.0,
        bid=0.48,
        ask=0.52,
        quote_at=now - timedelta(seconds=5),
        underlying_price=100.0,
        delta=0.5,
        open_interest=500,
    )
    validation = validate_option_draft(
        option_draft,
        {item.evidence_id: item for item in evidence},
        [contract],
        prompt_hash="a" * 64,
        account_equity=2_000.0,
        available_cash=1_500.0,
        source_draft=draft,
        now=now,
    )
    assert validation.packet is not None
    packet = validation.packet

    ledger = InMemoryLedger()
    ledger.authorize_option_packet(packet)
    assert ledger.valid_option_packets(now.date(), now) == [packet]

    proposed = ProposedOptionOrder.from_dict(
        {
            **packet.to_dict(),
            "account_number": "111111111",
            "rationale": "Guarded option E2E fixture",
        }
    )
    account = OptionAccountSnapshot(
        account_number="111111111",
        equity=2_000.0,
        cash=1_500.0,
        option_level="option_level_2",
        agentic_allowed=True,
        orders_source="broker",
        session_is_regular=True,
    )
    decision = evaluate_option_order(proposed, account, root=tmp_path, now=now)
    assert decision.approved

    fill_price = proposed.limit_price - 0.01
    broker_fill = {
        "id": "broker-option-order-1",
        "ref_id": proposed.ref_id,
        "state": "filled",
        "quantity": "1",
        "processed_quantity": "1",
        "average_price": str(fill_price),
        "direction": "debit",
        "legs": proposed.place_parameters()["legs"],
    }
    reconciliation = reconcile_option_orders(
        [decision.to_dict()],
        [broker_fill],
        root=tmp_path,
    )
    assert reconciliation["clean"]

    position = ActiveOptionPosition(
        position_id=packet.packet_id,
        packet_id=packet.packet_id,
        underlying=packet.underlying,
        strategy=packet.action,
        option_id=packet.option_id,
        contract_symbol=packet.contract.contract_symbol,
        option_type=packet.contract.option_type,
        expiration_date=packet.contract.expiration_date,
        strike=packet.contract.strike,
        quantity=1,
        side="long",
        opened_at=now,
        average_open_price=fill_price,
        premium_at_risk=packet.max_risk,
        collateral_reserved=0.0,
        shares_encumbered=0,
        status="open",
        structure_fingerprint=packet.structure_fingerprint,
    )
    ledger.upsert_option_position(position)
    ledger.consume_option_packet(packet.packet_id, now)

    assert ledger.option_positions(status="open") == [position]
    assert ledger.valid_option_packets(now.date(), now) == []
