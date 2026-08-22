from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from agentic_trader.picker.ledger import InMemoryLedger, PostgresLedger
from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
)
from agentic_trader.picker.validation import validate_picker_draft


def authorized_packet(draft, evidence, quant, now):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    return result.packet


def populated_ledger(draft, evidence, now):
    ledger = InMemoryLedger()
    ledger.put_run(
        draft.run_id,
        "account-hash",
        draft.created_at,
        now.date(),
        "gpt-5.6-sol",
        "a" * 64,
    )
    for item in evidence:
        ledger.put_evidence(item)
    ledger.put_draft(draft)
    return ledger


def postgres_conflict_ledger(monkeypatch, existing_row):
    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=()):
            self.rowcount = 0

        def fetchone(self):
            return existing_row

    cursor = Cursor()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return cursor

    ledger = PostgresLedger("postgresql://unused")
    monkeypatch.setattr(ledger, "_connect", lambda: Connection())
    return ledger


def test_prior_close_write_fails_closed_without_verified_reconstruction():
    ledger = InMemoryLedger()
    first = date(2026, 8, 20)
    metric_at = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="writes are disabled"):
        ledger.record_prior_close(
            "account",
            1000.0,
            first,
            metric_at=metric_at,
            observed_at=metric_at,
            source="robinhood_official_regular_close",
            artifact_hash="a" * 64,
        )
    assert ledger.control_state("account")["prior_close_equity"] is None


def test_authorized_packet_round_trip(draft, evidence, quant, now):
    ledger = populated_ledger(draft, evidence, now)
    packet = authorized_packet(draft, evidence, quant, now)
    ledger.authorize_packet(packet)
    assert ledger.authorized_packets(now.date(), now) == [packet]


def test_packet_requires_known_draft(draft, evidence, quant, now):
    ledger = InMemoryLedger()
    packet = authorized_packet(draft, evidence, quant, now)
    with pytest.raises(ValueError, match="known draft"):
        ledger.authorize_packet(packet)


def test_postgres_run_conflict_rejects_cross_batch_id_reuse(monkeypatch, now):
    existing = (
        "run-1",
        "account-hash",
        now,
        now.date(),
        "gpt-5.6-sol",
        "a" * 64,
        "started",
        {"batch_id": "older-batch"},
    )
    ledger = postgres_conflict_ledger(monkeypatch, existing)

    with pytest.raises(ValueError, match="different data"):
        ledger.put_run(
            "run-1",
            "account-hash",
            now,
            now.date(),
            "gpt-5.6-sol",
            "a" * 64,
            metadata={"batch_id": "newer-batch"},
        )


def test_postgres_draft_conflict_is_not_silently_reused(monkeypatch, draft):
    existing = (
        draft.draft_id,
        draft.run_id,
        draft.symbol,
        draft.created_at,
        draft.to_dict(),
    )
    ledger = postgres_conflict_ledger(monkeypatch, existing)
    changed = replace(draft, thesis="A different falsifiable thesis cannot reuse this draft ID.")

    with pytest.raises(ValueError, match="immutable"):
        ledger.put_draft(changed)


def test_postgres_packet_conflict_is_not_silently_reused(
    monkeypatch,
    draft,
    evidence,
    quant,
    now,
):
    packet = authorized_packet(draft, evidence, quant, now)
    existing = (
        packet.packet_id,
        packet.run_id,
        packet.draft_id,
        packet.symbol,
        packet.action,
        packet.valid_for_date,
        packet.expires_at,
        packet.packet_hash,
        packet.to_dict(),
    )
    ledger = postgres_conflict_ledger(monkeypatch, existing)
    changed = replace(packet, target_weight=packet.target_weight / 2).with_hash()

    with pytest.raises(ValueError, match="immutable"):
        ledger.authorize_packet(changed)


def test_duplicate_symbol_action_day_is_rejected(draft, evidence, quant, now):
    ledger = populated_ledger(draft, evidence, now)
    first = authorized_packet(draft, evidence, quant, now)
    ledger.authorize_packet(first)

    second_draft = replace(draft, draft_id="draft-2")
    ledger.put_draft(second_draft)
    second = authorized_packet(second_draft, evidence, quant, now)
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
        model_id="gpt-5.6-sol",
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
        "gpt-5.6-sol",
        payload,
    )
    ledger.set_batch_status("batch-1", "authorized")

    assert ledger.latest_staged_batch(now.date()) is None
    assert ledger.latest_research_batch(now.date())["batch_id"] == "batch-1"


def test_research_cycle_marker_blocks_until_finalized(now):
    ledger = InMemoryLedger()
    current = datetime.now(UTC)
    ledger.start_research_cycle("cycle-1", current.date(), current)
    assert ledger.latest_unfinished_cycle(current.date())["cycle_id"] == "cycle-1"
    ledger.complete_research_cycle("cycle-1", "batch-1", current.date())
    assert ledger.latest_unfinished_cycle(current.date()) is None
    assert ledger.research_cycles["cycle-1"]["batch_id"] == "batch-1"


def test_research_cycle_cannot_complete_for_another_session(now):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    with pytest.raises(ValueError, match="date does not match"):
        ledger.complete_research_cycle("cycle-1", "batch-1", now.date() + timedelta(days=1))


def test_postgres_cycle_completion_takes_date_lock_before_finalizing(monkeypatch, now):
    calls = []

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=()):
            calls.append((" ".join(str(query).split()), params))
            self.rowcount = 1 if "UPDATE picker_research_cycles" in str(query) else 0

        def fetchone(self):
            raise AssertionError("Successful completion must not need a fallback read")

    cursor = Cursor()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return cursor

    ledger = PostgresLedger("postgresql://unused")
    monkeypatch.setattr(ledger, "_connect", lambda: Connection())

    ledger.complete_research_cycle("cycle-1", "batch-1", now.date())

    assert calls[0] == (
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"research-execution-cycle:{now.date().isoformat()}",),
    )
    assert calls[1][0].startswith("UPDATE picker_research_cycles")


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
        "gpt-5.6-sol",
        {"drafts": [], "option_drafts": []},
    )
    usage = ledger.reserve_execution_budget(
        account_hash,
        now.date(),
        [
            ("entry-1", 100.0, True, False),
            ("entry-2", 100.0, True, False),
        ],
        observed_usage=(0, 0.0, 0, 0.0),
        research_batch_id="research-batch",
    )
    assert usage == {
        "total_orders": 2,
        "total_notional": 200.0,
        "entry_orders": 2,
        "entry_notional": 200.0,
        "option_openings": 0,
    }
    assert ledger.execution_budget_usage(account_hash, now.date()) == usage
    # Exact retries are idempotent.
    assert (
        ledger.reserve_execution_budget(
            account_hash,
            now.date(),
            [
                ("entry-1", 100.0, True, False),
                ("entry-2", 100.0, True, False),
            ],
            observed_usage=(0, 0.0, 0, 0.0),
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
        observed_usage=(0, 0.0, 0, 0.0),
    )
    assert exits["total_orders"] == 4
    assert exits["total_notional"] == 400.0
    assert exits["entry_orders"] == 2
    over_cap_exit = ledger.reserve_execution_budget(
        account_hash,
        now.date(),
        [("risk-reducing-exit", 250.0, False, False)],
    )
    assert over_cap_exit["total_orders"] == 5
    assert over_cap_exit["total_notional"] == 650.0

    with pytest.raises(RuntimeError, match="budget"):
        ledger.reserve_execution_budget(
            account_hash,
            now.date(),
            [("blocked-entry", 25.0, True, False)],
            research_batch_id="research-batch",
        )


def test_durable_control_distinguishes_entry_and_all_order_halts():
    ledger = InMemoryLedger()
    ledger.halt("account-hash", "drawdown")
    assert ledger.control_state("account-hash")["halt_scope"] == "entries"
    ledger.halt("account-hash", "reconciliation", scope="all")
    assert ledger.control_state("account-hash")["halt_scope"] == "all"
    with pytest.raises(ValueError, match="scope"):
        ledger.halt("account-hash", "invalid", scope="unknown")


def test_cloud_reservation_binding_grants_one_placement_claim(now):
    ledger = InMemoryLedger()
    ledger.stage_batch(
        "research-batch",
        now.date(),
        now,
        "a" * 64,
        "gpt-5.6-sol",
        {"drafts": [], "option_drafts": []},
    )
    order = [("ref-1", 100.0, True, False)]
    bindings = {
        "ref-1": (
            "plan-1",
            "confirmation-1",
            "attempt-1",
            now,
            "b" * 64,
            "c" * 64,
        )
    }

    first = ledger.reserve_execution_budget(
        "account-hash",
        now.date(),
        order,
        max_entry_orders=2,
        max_entry_notional=300.0,
        research_batch_id="research-batch",
        reservation_bindings=bindings,
    )
    second = ledger.reserve_execution_budget(
        "account-hash",
        now.date(),
        order,
        max_entry_orders=2,
        max_entry_notional=300.0,
        research_batch_id="research-batch",
        reservation_bindings=bindings,
    )

    assert first["newly_reserved_ref_ids"] == ["ref-1"]
    assert first["already_reserved_ref_ids"] == []
    assert second["newly_reserved_ref_ids"] == []
    assert second["already_reserved_ref_ids"] == ["ref-1"]
    assert ledger.execution_reservations["ref-1"]["attempt_id"] == "attempt-1"
    assert ledger.execution_reservations["ref-1"]["authority_fingerprint_hash"] == "c" * 64

    # The budget ledger recognizes a fresh-snapshot retry as the same spend but
    # leaves the freshness mutation to the cloud store's guarded CAS.
    refreshed_bindings = {
        "ref-1": (
            "plan-1",
            "confirmation-1",
            "attempt-1",
            now + timedelta(seconds=20),
            "d" * 64,
            "c" * 64,
        )
    }
    refreshed = ledger.reserve_execution_budget(
        "account-hash",
        now.date(),
        order,
        max_entry_orders=2,
        max_entry_notional=300.0,
        research_batch_id="research-batch",
        reservation_bindings=refreshed_bindings,
    )
    assert refreshed["newly_reserved_ref_ids"] == []
    assert refreshed["already_reserved_ref_ids"] == ["ref-1"]
    assert ledger.execution_reservations["ref-1"]["validation_snapshot_hash"] == "b" * 64

    changed_authority = {
        "ref-1": (*refreshed_bindings["ref-1"][:5], "e" * 64),
    }
    with pytest.raises(ValueError, match="immutable"):
        ledger.reserve_execution_budget(
            "account-hash",
            now.date(),
            order,
            max_entry_orders=2,
            max_entry_notional=300.0,
            research_batch_id="research-batch",
            reservation_bindings=changed_authority,
        )


def test_durable_budget_atomically_limits_concurrent_option_openings(now):
    ledger = InMemoryLedger()
    ledger.stage_batch(
        "option-research-batch",
        now.date(),
        now,
        "a" * 64,
        "gpt-5.6-sol",
        {"drafts": [], "option_drafts": []},
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
