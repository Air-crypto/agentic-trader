from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
)
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


def test_prior_close_is_durable_and_immutable_within_session():
    ledger = InMemoryLedger()
    first = date(2026, 8, 20)
    assert ledger.record_prior_close("account", 1000.0, first) == 1000.0
    assert ledger.record_prior_close("account", 1000.0, first) == 1000.0
    with pytest.raises(ValueError, match="immutable"):
        ledger.record_prior_close("account", 999.0, first)
    assert ledger.record_prior_close("account", 1010.0, date(2026, 8, 21)) == 1010.0
    state = ledger.control_state("account")
    assert state["prior_close_equity"] == 1010.0
    assert state["prior_close_date"] == date(2026, 8, 21)


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


def option_packet(now, packet_id="option-packet-1", strike=100.0):
    contract = OptionContractSnapshot(
        option_id=f"contract-{strike}",
        contract_symbol=f"AAPL-{strike}-C",
        underlying="AAPL",
        option_type="call",
        expiration_date=now.date() + timedelta(days=30),
        strike=strike,
        bid=1.0,
        ask=1.05,
        quote_at=now,
        underlying_price=100.0,
    )
    return OptionDecisionPacket(
        packet_id=packet_id,
        run_id="run-options",
        draft_id="draft-options",
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(minutes=5),
        underlying="AAPL",
        action="long_call",
        contract=contract,
        quantity=1,
        side="buy",
        position_effect="open",
        limit_price=1.05,
        max_risk=105.0,
        collateral_required=0.0,
        shares_encumbered=0,
        evidence_ids=("evidence-1",),
        prompt_hash="p" * 64,
        model_id="option-model",
        draft_hash="d" * 64,
        horizon_trading_days=20,
        invalidation="Close if the option thesis is invalidated.",
    )


def option_position(packet, now, status="open"):
    return ActiveOptionPosition(
        position_id="option-position-1",
        packet_id=packet.packet_id,
        underlying=packet.underlying,
        strategy=packet.strategy,
        option_id=packet.option_id,
        contract_symbol=packet.contract.contract_symbol,
        option_type=packet.contract.option_type,
        expiration_date=packet.contract.expiration_date,
        strike=packet.contract.strike,
        quantity=1,
        side="long",
        opened_at=now,
        average_open_price=packet.limit_price,
        premium_at_risk=packet.max_risk,
        collateral_reserved=packet.collateral_required,
        shares_encumbered=packet.shares_encumbered,
        status=status,
        structure_fingerprint=packet.structure_fingerprint,
    )


def test_option_packet_lifecycle_filters_valid_packets(now):
    ledger = InMemoryLedger()
    consumed = option_packet(now)
    revoked = option_packet(now, packet_id="option-packet-2", strike=105.0)
    ledger.authorize_option_packet(consumed)
    ledger.authorize_option_packet(revoked)

    assert ledger.valid_option_packets(now.date(), now) == [consumed, revoked]
    ledger.consume_option_packet(consumed.packet_id, now)
    ledger.consume_option_packet(consumed.packet_id, now)
    ledger.revoke_option_packet(revoked.packet_id, "risk limit changed", now)
    ledger.revoke_option_packet(revoked.packet_id, "risk limit changed", now)

    assert ledger.valid_option_packets(now.date(), now) == []
    assert ledger.option_packet(consumed.packet_id) == consumed
    assert ledger.option_packet("missing") is None
    with pytest.raises(ValueError, match="after consumption"):
        ledger.revoke_option_packet(consumed.packet_id, "too late", now)


def test_option_packet_is_hash_checked_and_structure_unique(now):
    ledger = InMemoryLedger()
    packet = option_packet(now)
    ledger.authorize_option_packet(packet)

    invalid = replace(packet, packet_id="invalid-packet")
    with pytest.raises(ValueError, match="invalid hash"):
        ledger.authorize_option_packet(invalid)

    duplicate_structure = replace(packet, packet_id="option-packet-2").with_hash()
    with pytest.raises(ValueError, match="structure fingerprint"):
        ledger.authorize_option_packet(duplicate_structure)

    ledger.consume_option_packet(packet.packet_id, now)
    next_day = replace(
        packet,
        packet_id="option-packet-next-day",
        created_at=packet.created_at + timedelta(days=1),
        valid_for_date=packet.valid_for_date + timedelta(days=1),
        expires_at=packet.expires_at + timedelta(days=1),
        packet_hash="",
    )
    ledger.authorize_option_packet(next_day)


def test_option_positions_upsert_and_filter_without_changing_identity(now):
    ledger = InMemoryLedger()
    packet = option_packet(now)
    ledger.authorize_option_packet(packet)
    position = option_position(packet, now)
    ledger.upsert_option_position(position)

    closing = replace(position, status="closing").with_hash()
    ledger.upsert_option_position(closing)
    assert ledger.option_positions(status="closing", underlying="AAPL") == [closing]
    assert ledger.option_positions(status="open") == []

    changed_identity = replace(closing, underlying="MSFT").with_hash()
    with pytest.raises(ValueError, match="immutable identity"):
        ledger.upsert_option_position(changed_identity)


def test_option_collateral_and_shares_are_reserved_atomically(now):
    ledger = InMemoryLedger()
    first = option_packet(now)
    second = option_packet(now, packet_id="option-packet-2", strike=105.0)
    ledger.authorize_option_packet(first)
    ledger.authorize_option_packet(second)
    ledger.reserve_option_collateral(
        first.packet_id,
        "account-hash",
        100.0,
        {"aapl": 100},
        available_cash=150.0,
        available_shares={"AAPL": 100},
        reserved_at=now,
    )
    ledger.reserve_option_collateral(
        first.packet_id,
        "account-hash",
        100.0,
        {"AAPL": 100},
        available_cash=150.0,
        available_shares={"AAPL": 100},
        reserved_at=now + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="cash"):
        ledger.reserve_option_collateral(
            second.packet_id,
            "account-hash",
            51.0,
            {"MSFT": 1},
            available_cash=150.0,
            available_shares={"MSFT": 1},
            reserved_at=now,
        )
    assert second.packet_id not in ledger.option_reservations

    ledger.release_option_collateral(first.packet_id, now)
    ledger.release_option_collateral(first.packet_id, now)
    ledger.reserve_option_collateral(
        second.packet_id,
        "account-hash",
        51.0,
        {"AAPL": 100},
        available_cash=150.0,
        available_shares={"AAPL": 100},
        reserved_at=now,
    )


def test_option_share_overencumbrance_and_event_immutability(now):
    ledger = InMemoryLedger()
    first = option_packet(now)
    second = option_packet(now, packet_id="option-packet-2", strike=105.0)
    ledger.authorize_option_packet(first)
    ledger.authorize_option_packet(second)
    ledger.reserve_option_collateral(
        first.packet_id,
        "account-hash",
        0.0,
        {"AAPL": 100},
        available_cash=0.0,
        available_shares={"AAPL": 100},
        reserved_at=now,
    )
    with pytest.raises(ValueError, match="shares"):
        ledger.reserve_option_collateral(
            second.packet_id,
            "account-hash",
            0.0,
            {"AAPL": 1},
            available_cash=0.0,
            available_shares={"AAPL": 100},
            reserved_at=now,
        )

    ledger.append_option_order_event(
        "event-1",
        "submitted",
        now,
        {"state": "queued"},
        packet_id=first.packet_id,
        ref_id="ref-1",
    )
    ledger.append_option_order_event(
        "event-1",
        "submitted",
        now,
        {"state": "queued"},
        packet_id=first.packet_id,
        ref_id="ref-1",
    )
    with pytest.raises(ValueError, match="immutable"):
        ledger.append_option_order_event(
            "event-1",
            "filled",
            now,
            {"state": "filled"},
            packet_id=first.packet_id,
            ref_id="ref-1",
        )


def test_latest_research_batch_remains_available_after_stock_authorization(now):
    ledger = InMemoryLedger()
    payload = {"drafts": [], "option_drafts": [{"draft_id": "option-1"}]}
    ledger.stage_batch(
        "batch-1",
        now.date(),
        now,
        "a" * 64,
        "model",
        payload,
    )
    ledger.set_batch_status("batch-1", "authorized")

    assert ledger.latest_staged_batch(now.date()) is None
    assert ledger.latest_research_batch(now.date())["batch_id"] == "batch-1"


def test_pending_batch_requires_separate_finalization(now):
    ledger = InMemoryLedger()
    payload = {"drafts": [{"draft_id": "draft-1"}], "option_drafts": []}
    ledger.stage_pending_batch(
        "pending-1",
        now.date(),
        now,
        "a" * 64,
        "claude-sonnet",
        payload,
    )
    assert ledger.latest_pending_batch(now.date())["batch_id"] == "pending-1"
    ledger.finalize_pending_batch("pending-1", "finalized", now)
    assert ledger.latest_pending_batch(now.date()) is None
    ledger.finalize_pending_batch("pending-1", "finalized", now)


def test_research_cycle_marker_blocks_until_finalized(now):
    ledger = InMemoryLedger()
    current = datetime.now(UTC)
    ledger.start_research_cycle("cycle-1", current.date(), current)
    assert ledger.latest_unfinished_cycle(current.date())["cycle_id"] == "cycle-1"
    ledger.bind_research_cycle("cycle-1", "batch-1")
    assert ledger.latest_unfinished_cycle(current.date())["status"] == "pending"
    ledger.finish_research_cycle("cycle-1", "finalized")
    assert ledger.latest_unfinished_cycle(current.date()) is None


def test_stale_research_cycle_is_reaped_instead_of_deadlocking():
    ledger = InMemoryLedger()
    today = datetime.now(UTC).date()
    ledger.start_research_cycle(
        "stale-cycle",
        today,
        datetime.now(UTC) - timedelta(hours=7),
    )

    assert ledger.latest_unfinished_cycle(today) is None
    assert ledger.research_cycles["stale-cycle"]["status"] == "failed"


def test_durable_execution_budget_reserves_entry_and_exit_capacity(now):
    ledger = InMemoryLedger()
    account_hash = "account-hash"
    ledger.stage_batch(
        "research-batch",
        now.date(),
        now,
        "a" * 64,
        "claude-sonnet",
        {"drafts": [], "option_drafts": [], "critics": []},
    )
    usage = ledger.reserve_execution_budget(
        account_hash,
        now.date(),
        [
            ("entry-1", 100.0, True, False),
            ("entry-2", 100.0, True, False),
        ],
        observed_usage=(4, 400.0, 4, 400.0),
        research_batch_id="research-batch",
    )
    assert usage == {
        "total_orders": 6,
        "total_notional": 600.0,
        "entry_orders": 6,
        "entry_notional": 600.0,
        "option_openings": 0,
    }
    # Exact retries are idempotent.
    assert (
        ledger.reserve_execution_budget(
            account_hash,
            now.date(),
            [
                ("entry-1", 100.0, True, False),
                ("entry-2", 100.0, True, False),
            ],
            observed_usage=(4, 400.0, 4, 400.0),
            research_batch_id="research-batch",
        )
        == usage
    )
    exits = ledger.reserve_execution_budget(
        account_hash,
        now.date(),
        [
            ("exit-1", 100.0, False, False),
            ("exit-2", 100.0, False, False),
        ],
        observed_usage=(4, 400.0, 4, 400.0),
    )
    assert exits["total_orders"] == 8
    assert exits["total_notional"] == 800.0
    assert exits["entry_orders"] == 6
    over_cap_exit = ledger.reserve_execution_budget(
        account_hash,
        now.date(),
        [("risk-reducing-exit", 250.0, False, False)],
    )
    assert over_cap_exit["total_orders"] == 9
    assert over_cap_exit["total_notional"] == 1050.0

    with pytest.raises(RuntimeError, match="budget"):
        ledger.reserve_execution_budget(
            account_hash,
            now.date(),
            [("blocked-entry", 25.0, True, False)],
            research_batch_id="research-batch",
        )


def test_durable_budget_atomically_limits_concurrent_option_openings(now):
    ledger = InMemoryLedger()
    ledger.stage_batch(
        "option-research-batch",
        now.date(),
        now,
        "a" * 64,
        "claude-sonnet",
        {"drafts": [], "option_drafts": [], "critics": []},
    )
    ledger.reserve_execution_budget(
        "account-hash",
        now.date(),
        [("option-open-1", 50.0, True, True)],
        observed_open_option_positions=2,
        research_batch_id="option-research-batch",
    )
    with pytest.raises(RuntimeError, match="budget"):
        ledger.reserve_execution_budget(
            "account-hash",
            now.date(),
            [("option-open-2", 50.0, True, True)],
            observed_open_option_positions=2,
            research_batch_id="option-research-batch",
        )


def test_option_exit_reservation_is_not_blocked_by_opening_position_cap(now):
    ledger = InMemoryLedger()
    ledger.execution_usage[("account-hash", now.date())] = {
        "total_orders": 1,
        "total_notional": 50.0,
        "entry_orders": 1,
        "entry_notional": 50.0,
        "option_openings": 1,
    }

    result = ledger.reserve_execution_budget(
        "account-hash",
        now.date(),
        [("option-exit", 50.0, False, False)],
        observed_open_option_positions=3,
    )

    assert result["total_orders"] == 2
