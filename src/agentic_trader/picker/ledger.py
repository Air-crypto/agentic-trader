from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from .evaluation import OutcomeMark
from .models import (
    ActiveThesis,
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
    PickerDraft,
)
from .option_models import ActiveOptionPosition, OptionDecisionPacket

DATABASE_URL_ENV = "DATABASE_URL"


def account_key(account_number: str) -> str:
    """Irreversible account identifier suitable for a shared decision ledger."""
    return hashlib.sha256(account_number.encode()).hexdigest()


def _validate_option_reservation(
    collateral_amount: float,
    share_encumbrances: dict[str, int],
    available_cash: float,
    available_shares: dict[str, int],
) -> dict[str, int]:
    if (
        not isfinite(collateral_amount)
        or not isfinite(available_cash)
        or collateral_amount < 0
        or available_cash < 0
    ):
        raise ValueError("Option collateral and available cash cannot be negative")
    normalized: dict[str, int] = {}
    for raw_symbol, quantity in share_encumbrances.items():
        symbol = raw_symbol.strip().upper()
        if (
            not symbol
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("Share encumbrances require a symbol and positive integer quantity")
        if symbol in normalized:
            raise ValueError(f"Duplicate share encumbrance: {symbol}")
        normalized[symbol] = quantity
    if collateral_amount == 0 and not normalized:
        raise ValueError("An option reservation must encumber cash or shares")
    if collateral_amount > available_cash:
        raise ValueError("Insufficient unencumbered cash for option collateral")
    if any(
        isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0
        for quantity in available_shares.values()
    ):
        raise ValueError("Available share quantities must be non-negative integers")
    return normalized


def _validate_execution_reservation(
    orders: list[tuple[str, float, bool, bool]],
    max_orders: int,
    max_notional: float,
    max_entry_orders: int,
    max_entry_notional: float,
) -> None:
    if not orders or len({ref_id for ref_id, _, _, _ in orders}) != len(orders):
        raise ValueError("Execution reservations require unique ref_ids")
    if any(
        not ref_id or not isfinite(notional) or notional <= 0
        for ref_id, notional, _, _ in orders
    ):
        raise ValueError("Execution reservation notionals must be finite and positive")
    if not 0 < max_orders <= 8 or not 0 < max_entry_orders <= min(max_orders, 6):
        raise ValueError("Execution order caps cannot relax hard limits")
    if (
        not 0 < max_notional <= 800
        or not 0 < max_entry_notional <= min(max_notional, 600)
    ):
        raise ValueError("Execution notional caps cannot relax hard limits")


def _validate_observed_execution_usage(
    observed_usage: tuple[int, float, int, float],
) -> None:
    total_orders, total_notional, entry_orders, entry_notional = observed_usage
    if (
        total_orders < 0
        or entry_orders < 0
        or entry_orders > total_orders
        or not isfinite(total_notional)
        or not isfinite(entry_notional)
        or total_notional < 0
        or entry_notional < 0
        or entry_notional > total_notional
    ):
        raise ValueError("Observed execution usage is inconsistent")


class PickerLedger(Protocol):
    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def put_evidence(self, evidence: EvidenceVersion) -> None: ...

    def put_draft(self, draft: PickerDraft) -> None: ...

    def put_critic(self, verdict: CriticVerdict) -> None: ...

    def authorize_packet(self, packet: DecisionPacket) -> None: ...

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]: ...

    def upsert_thesis(self, thesis: ActiveThesis) -> None: ...

    def active_theses(self) -> list[ActiveThesis]: ...

    def control_state(self, account_hash: str) -> dict[str, Any]: ...

    def record_equity_peak(self, account_hash: str, equity: float) -> float: ...

    def halt(self, account_hash: str, reason: str) -> None: ...

    def put_outcome(self, outcome: OutcomeMark) -> None: ...

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None: ...

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None: ...

    def latest_research_batch(self, as_of: date) -> dict[str, Any] | None: ...

    def set_batch_status(self, batch_id: str, status: str) -> None: ...

    def stage_pending_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        analyst_model_id: str,
        payload: dict[str, Any],
    ) -> None: ...

    def latest_pending_batch(self, as_of: date) -> dict[str, Any] | None: ...

    def pending_batch(self, batch_id: str) -> dict[str, Any] | None: ...

    def finalize_pending_batch(
        self,
        batch_id: str,
        status: str,
        finalized_at: datetime | None = None,
    ) -> None: ...

    def start_research_cycle(
        self,
        cycle_id: str,
        as_of: date,
        started_at: datetime,
    ) -> None: ...

    def bind_research_cycle(self, cycle_id: str, batch_id: str) -> None: ...

    def finish_research_cycle(self, cycle_id: str, status: str) -> None: ...

    def latest_unfinished_cycle(self, as_of: date) -> dict[str, Any] | None: ...

    def authorize_option_packet(self, packet: OptionDecisionPacket) -> None: ...

    def consume_option_packet(
        self, packet_id: str, consumed_at: datetime | None = None
    ) -> None: ...

    def revoke_option_packet(
        self,
        packet_id: str,
        reason: str,
        revoked_at: datetime | None = None,
    ) -> None: ...

    def valid_option_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[OptionDecisionPacket]: ...

    def option_packet(self, packet_id: str) -> OptionDecisionPacket | None: ...

    def upsert_option_position(self, position: ActiveOptionPosition) -> None: ...

    def option_positions(
        self,
        status: str | None = None,
        underlying: str | None = None,
    ) -> list[ActiveOptionPosition]: ...

    def reserve_option_collateral(
        self,
        packet_id: str,
        account_hash: str,
        collateral_amount: float,
        share_encumbrances: dict[str, int],
        *,
        available_cash: float,
        available_shares: dict[str, int],
        reserved_at: datetime | None = None,
    ) -> None: ...

    def release_option_collateral(
        self, packet_id: str, released_at: datetime | None = None
    ) -> None: ...

    def append_option_order_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        packet_id: str | None = None,
        position_id: str | None = None,
        ref_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> None: ...

    def sync_option_open(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
    ) -> None: ...

    def sync_option_close(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
        close_packet_id: str | None = None,
    ) -> None: ...

    def cancel_option_packet(
        self,
        packet_id: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> None: ...

    def reserve_execution_budget(
        self,
        account_hash: str,
        trade_date: date,
        orders: list[tuple[str, float, bool, bool]],
        *,
        max_orders: int = 8,
        max_notional: float = 800.0,
        max_entry_orders: int = 6,
        max_entry_notional: float = 600.0,
        observed_usage: tuple[int, float, int, float] = (0, 0.0, 0, 0.0),
        max_option_openings: int = 3,
        observed_open_option_positions: int = 0,
        research_batch_id: str = "",
    ) -> dict[str, float | int]: ...


class InMemoryLedger:
    """Test ledger with the same immutability and uniqueness rules as Postgres."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.evidence: dict[tuple[str, str], EvidenceVersion] = {}
        self.drafts: dict[str, PickerDraft] = {}
        self.critics: dict[str, CriticVerdict] = {}
        self.packets: dict[str, DecisionPacket] = {}
        self.theses: dict[str, ActiveThesis] = {}
        self.controls: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[tuple[str, int], OutcomeMark] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.pending_batches: dict[str, dict[str, Any]] = {}
        self.research_cycles: dict[str, dict[str, Any]] = {}
        self.option_packets: dict[str, OptionDecisionPacket] = {}
        self.option_packet_states: dict[str, dict[str, Any]] = {}
        self.option_positions_by_id: dict[str, ActiveOptionPosition] = {}
        self.option_reservations: dict[str, dict[str, Any]] = {}
        self.option_order_events: dict[str, dict[str, Any]] = {}
        self.execution_reservations: dict[str, dict[str, Any]] = {}
        self.execution_usage: dict[tuple[str, date], dict[str, float | int]] = {}

    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_id": run_id,
            "account_key": account_hash,
            "started_at": started_at,
            "as_of": as_of,
            "model_id": model_id,
            "prompt_hash": prompt_hash,
            "status": status,
            "metadata": metadata or {},
        }
        existing = self.runs.get(run_id)
        if existing is not None and existing != payload:
            raise ValueError(f"Run {run_id} already exists with different data")
        self.runs[run_id] = payload

    def put_evidence(self, evidence: EvidenceVersion) -> None:
        key = (evidence.evidence_id, evidence.document_hash)
        existing = self.evidence.get(key)
        if existing is not None and existing != evidence:
            raise ValueError(f"Evidence version {key} is immutable")
        self.evidence[key] = evidence

    def put_draft(self, draft: PickerDraft) -> None:
        if draft.run_id not in self.runs:
            raise ValueError("Draft references an unknown run")
        existing = self.drafts.get(draft.draft_id)
        if existing is not None and existing != draft:
            raise ValueError(f"Draft {draft.draft_id} is immutable")
        self.drafts[draft.draft_id] = draft

    def put_critic(self, verdict: CriticVerdict) -> None:
        if verdict.draft_id not in self.drafts:
            raise ValueError("Critic verdict references an unknown draft")
        existing = self.critics.get(verdict.draft_id)
        if existing is not None and existing != verdict:
            raise ValueError(f"Critic verdict for {verdict.draft_id} is immutable")
        self.critics[verdict.draft_id] = verdict

    def authorize_packet(self, packet: DecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize a packet with an invalid hash")
        if packet.draft_id not in self.drafts or packet.draft_id not in self.critics:
            raise ValueError("Packet requires a known draft and critic verdict")
        collision = next(
            (
                item
                for item in self.packets.values()
                if item.valid_for_date == packet.valid_for_date
                and item.symbol == packet.symbol
                and item.action == packet.action
                and item.packet_id != packet.packet_id
            ),
            None,
        )
        if collision is not None:
            raise ValueError("An authorized packet already exists for symbol/action/day")
        existing = self.packets.get(packet.packet_id)
        if existing is not None and existing != packet:
            raise ValueError(f"Packet {packet.packet_id} is immutable")
        self.packets[packet.packet_id] = packet

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        return sorted(
            [
                packet
                for packet in self.packets.values()
                if packet.valid_for_date == valid_for
                and packet.expires_at > now
                and packet.verify_hash()
            ],
            key=lambda item: (-item.rank_score, item.symbol),
        )

    def upsert_thesis(self, thesis: ActiveThesis) -> None:
        self.theses[thesis.pick_id] = thesis

    def active_theses(self) -> list[ActiveThesis]:
        return sorted(
            [
                thesis
                for thesis in self.theses.values()
                if thesis.status in {"pending_entry", "active", "expired", "invalidated"}
            ],
            key=lambda item: item.pick_id,
        )

    def control_state(self, account_hash: str) -> dict[str, Any]:
        return dict(
            self.controls.get(
                account_hash,
                {
                    "halted": False,
                    "halt_reason": None,
                    "high_water_mark": None,
                    "cooldown_until": None,
                },
            )
        )

    def record_equity_peak(self, account_hash: str, equity: float) -> float:
        if equity <= 0:
            raise ValueError("Equity must be positive")
        state = self.control_state(account_hash)
        previous = state.get("high_water_mark")
        peak = equity if previous is None else max(float(previous), equity)
        state["high_water_mark"] = peak
        self.controls[account_hash] = state
        return peak

    def halt(self, account_hash: str, reason: str) -> None:
        state = self.control_state(account_hash)
        state["halted"] = True
        state["halt_reason"] = reason
        self.controls[account_hash] = state

    def put_outcome(self, outcome: OutcomeMark) -> None:
        key = (outcome.packet_id, outcome.horizon_days)
        existing = self.outcomes.get(key)
        if existing is not None and existing != outcome:
            raise ValueError(f"Outcome {key} is immutable")
        self.outcomes[key] = outcome

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "batch_id": batch_id,
            "as_of": as_of,
            "created_at": created_at,
            "prompt_hash": prompt_hash,
            "model_id": model_id,
            "status": "staged",
            "payload": payload,
        }
        existing = self.batches.get(batch_id)
        if existing is not None and existing != record:
            raise ValueError(f"Research batch {batch_id} is immutable")
        self.batches[batch_id] = record

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None:
        eligible = [
            record
            for record in self.batches.values()
            if record["as_of"] == as_of and record["status"] == "staged"
        ]
        return max(eligible, key=lambda item: item["created_at"]) if eligible else None

    def latest_research_batch(self, as_of: date) -> dict[str, Any] | None:
        eligible = [
            record
            for record in self.batches.values()
            if record["as_of"] == as_of and record["status"] != "consumed"
        ]
        return max(eligible, key=lambda item: item["created_at"]) if eligible else None

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in {"staged", "authorized", "rejected", "consumed"}:
            raise ValueError(f"Unsupported batch status: {status}")
        if batch_id not in self.batches:
            raise ValueError(f"Unknown research batch {batch_id}")
        self.batches[batch_id]["status"] = status

    def stage_pending_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        analyst_model_id: str,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "batch_id": batch_id,
            "as_of": as_of,
            "created_at": created_at,
            "prompt_hash": prompt_hash,
            "analyst_model_id": analyst_model_id,
            "status": "pending",
            "payload": payload,
            "finalized_at": None,
        }
        existing = self.pending_batches.get(batch_id)
        if existing is not None:
            comparable = {**existing, "status": "pending", "finalized_at": None}
            if comparable != record:
                raise ValueError(f"Pending research batch {batch_id} is immutable")
            return
        self.pending_batches[batch_id] = record

    def latest_pending_batch(self, as_of: date) -> dict[str, Any] | None:
        eligible = [
            record
            for record in self.pending_batches.values()
            if record["as_of"] == as_of and record["status"] == "pending"
        ]
        return max(eligible, key=lambda item: item["created_at"]) if eligible else None

    def pending_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.pending_batches.get(batch_id)

    def finalize_pending_batch(
        self,
        batch_id: str,
        status: str,
        finalized_at: datetime | None = None,
    ) -> None:
        if status not in {"finalized", "rejected"}:
            raise ValueError("Pending batch status must be finalized or rejected")
        record = self.pending_batches.get(batch_id)
        if record is None:
            raise ValueError(f"Unknown pending research batch {batch_id}")
        if record["status"] == status:
            return
        if record["status"] != "pending":
            raise ValueError(f"Pending batch {batch_id} is already {record['status']}")
        record["status"] = status
        record["finalized_at"] = finalized_at or datetime.now(UTC)

    def start_research_cycle(
        self,
        cycle_id: str,
        as_of: date,
        started_at: datetime,
    ) -> None:
        record = {
            "cycle_id": cycle_id,
            "as_of": as_of,
            "started_at": started_at,
            "status": "running",
            "batch_id": None,
        }
        existing = self.research_cycles.get(cycle_id)
        if existing is not None and existing != record:
            raise ValueError(f"Research cycle {cycle_id} is immutable")
        self.research_cycles[cycle_id] = existing or record

    def bind_research_cycle(self, cycle_id: str, batch_id: str) -> None:
        record = self.research_cycles.get(cycle_id)
        if record is None or record["status"] not in {"running", "pending"}:
            raise ValueError(f"Research cycle {cycle_id} is not running")
        if record["batch_id"] not in {None, batch_id}:
            raise ValueError(f"Research cycle {cycle_id} is bound elsewhere")
        record["batch_id"] = batch_id
        record["status"] = "pending"

    def finish_research_cycle(self, cycle_id: str, status: str) -> None:
        if status not in {"finalized", "failed"}:
            raise ValueError("Research cycle must finish finalized or failed")
        record = self.research_cycles.get(cycle_id)
        if record is None:
            raise ValueError(f"Unknown research cycle {cycle_id}")
        record["status"] = status

    def latest_unfinished_cycle(self, as_of: date) -> dict[str, Any] | None:
        eligible = [
            record
            for record in self.research_cycles.values()
            if record["as_of"] == as_of
            and record["status"] in {"running", "pending"}
        ]
        return max(eligible, key=lambda item: item["started_at"]) if eligible else None

    def authorize_option_packet(self, packet: OptionDecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize an option packet with an invalid hash")
        existing = self.option_packets.get(packet.packet_id)
        if existing is not None and existing != packet:
            raise ValueError(f"Option packet {packet.packet_id} is immutable")
        collision = next(
            (
                item
                for packet_id, item in self.option_packets.items()
                if item.structure_fingerprint == packet.structure_fingerprint
                and item.valid_for_date == packet.valid_for_date
                and self.option_packet_states[packet_id]["status"] == "authorized"
                and item.packet_id != packet.packet_id
            ),
            None,
        )
        if collision is not None:
            raise ValueError("An option packet already exists for this structure fingerprint")
        if existing is None:
            self.option_packets[packet.packet_id] = packet
            self.option_packet_states[packet.packet_id] = {
                "status": "authorized",
                "consumed_at": None,
                "revoked_at": None,
                "revocation_reason": None,
            }

    def consume_option_packet(self, packet_id: str, consumed_at: datetime | None = None) -> None:
        state = self._option_packet_state(packet_id)
        if state["status"] == "consumed":
            return
        if state["status"] != "authorized":
            raise ValueError(f"Option packet {packet_id} cannot be consumed from revoked")
        state["status"] = "consumed"
        state["consumed_at"] = consumed_at or datetime.now(UTC)

    def revoke_option_packet(
        self,
        packet_id: str,
        reason: str,
        revoked_at: datetime | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("An option packet revocation reason is required")
        state = self._option_packet_state(packet_id)
        if state["status"] == "revoked":
            if state["revocation_reason"] != reason:
                raise ValueError(f"Option packet {packet_id} was revoked for another reason")
            return
        if state["status"] != "authorized":
            raise ValueError(f"Option packet {packet_id} cannot be revoked after consumption")
        state["status"] = "revoked"
        state["revoked_at"] = revoked_at or datetime.now(UTC)
        state["revocation_reason"] = reason

    def valid_option_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[OptionDecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        return sorted(
            [
                packet
                for packet_id, packet in self.option_packets.items()
                if self.option_packet_states[packet_id]["status"] == "authorized"
                and packet.valid_for_date == valid_for
                and packet.expires_at > now
                and packet.verify_hash()
            ],
            key=lambda item: item.packet_id,
        )

    def option_packet(self, packet_id: str) -> OptionDecisionPacket | None:
        return self.option_packets.get(packet_id)

    def upsert_option_position(self, position: ActiveOptionPosition) -> None:
        if not position.verify_hash():
            raise ValueError("Cannot store an option position with an invalid hash")
        if position.packet_id not in self.option_packets:
            raise ValueError(
                f"Unknown option packet for position {position.position_id}"
            )
        existing = self.option_positions_by_id.get(position.position_id)
        if existing is not None and (
            existing.underlying != position.underlying or existing.strategy != position.strategy
        ):
            raise ValueError(
                f"Option position {position.position_id} has immutable identity fields"
            )
        self.option_positions_by_id[position.position_id] = position

    def option_positions(
        self,
        status: str | None = None,
        underlying: str | None = None,
    ) -> list[ActiveOptionPosition]:
        return sorted(
            [
                position
                for position in self.option_positions_by_id.values()
                if (status is None or position.status == status)
                and (underlying is None or position.underlying == underlying)
            ],
            key=lambda item: item.position_id,
        )

    def reserve_option_collateral(
        self,
        packet_id: str,
        account_hash: str,
        collateral_amount: float,
        share_encumbrances: dict[str, int],
        *,
        available_cash: float,
        available_shares: dict[str, int],
        reserved_at: datetime | None = None,
    ) -> None:
        packet_state = self._option_packet_state(packet_id)
        if packet_state["status"] != "authorized":
            raise ValueError(
                f"Option packet {packet_id} cannot reserve collateral from "
                f"{packet_state['status']}"
            )
        normalized_shares = _validate_option_reservation(
            collateral_amount,
            share_encumbrances,
            available_cash,
            available_shares,
        )
        record = {
            "packet_id": packet_id,
            "account_key": account_hash,
            "collateral_amount": float(collateral_amount),
            "share_encumbrances": normalized_shares,
            "status": "active",
            "reserved_at": reserved_at or datetime.now(UTC),
            "released_at": None,
        }
        existing = self.option_reservations.get(packet_id)
        if existing is not None:
            comparable_fields = (
                "account_key",
                "collateral_amount",
                "share_encumbrances",
                "status",
            )
            if all(existing[key] == record[key] for key in comparable_fields):
                return
            raise ValueError(f"Option collateral for {packet_id} is already reserved")

        reserved_cash = sum(
            item["collateral_amount"]
            for item in self.option_reservations.values()
            if item["account_key"] == account_hash and item["status"] == "active"
        )
        if reserved_cash + collateral_amount > available_cash:
            raise ValueError("Insufficient unencumbered cash for option collateral")
        for symbol, quantity in normalized_shares.items():
            reserved_quantity = sum(
                item["share_encumbrances"].get(symbol, 0)
                for item in self.option_reservations.values()
                if item["account_key"] == account_hash and item["status"] == "active"
            )
            if reserved_quantity + quantity > available_shares.get(symbol, 0):
                raise ValueError(
                    f"Insufficient unencumbered shares for option collateral: {symbol}"
                )
        self.option_reservations[packet_id] = record

    def release_option_collateral(
        self, packet_id: str, released_at: datetime | None = None
    ) -> None:
        record = self.option_reservations.get(packet_id)
        if record is None:
            raise ValueError(f"No option collateral is reserved for {packet_id}")
        if record["status"] == "released":
            return
        record["status"] = "released"
        record["released_at"] = released_at or datetime.now(UTC)

    def append_option_order_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        packet_id: str | None = None,
        position_id: str | None = None,
        ref_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        if packet_id is not None and packet_id not in self.option_packets:
            raise ValueError(f"Unknown option packet {packet_id}")
        if position_id is not None and position_id not in self.option_positions_by_id:
            raise ValueError(f"Unknown option position {position_id}")
        record = {
            "event_id": event_id,
            "packet_id": packet_id,
            "position_id": position_id,
            "ref_id": ref_id,
            "broker_order_id": broker_order_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": dict(payload),
        }
        existing = self.option_order_events.get(event_id)
        if existing is not None and existing != record:
            raise ValueError(f"Option order event {event_id} is immutable")
        self.option_order_events[event_id] = record

    def sync_option_open(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
    ) -> None:
        packet_state = self._option_packet_state(position.packet_id)
        if packet_state["status"] not in {"authorized", "consumed"}:
            raise ValueError("Option opening packet is not authorized")
        existing_event = self.option_order_events.get(event_id)
        expected_event = {
            "event_id": event_id,
            "packet_id": position.packet_id,
            "position_id": position.position_id,
            "ref_id": ref_id,
            "broker_order_id": broker_order_id,
            "event_type": "opened",
            "occurred_at": occurred_at,
            "payload": dict(payload),
        }
        if existing_event is not None and existing_event != expected_event:
            raise ValueError(f"Option order event {event_id} is immutable")
        self.upsert_option_position(position)
        if packet_state["status"] == "authorized":
            self.consume_option_packet(position.packet_id, occurred_at)
        self.append_option_order_event(
            event_id,
            "opened",
            occurred_at,
            payload,
            packet_id=position.packet_id,
            position_id=position.position_id,
            ref_id=ref_id,
            broker_order_id=broker_order_id,
        )

    def sync_option_close(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
        close_packet_id: str | None = None,
    ) -> None:
        if position.position_id not in self.option_positions_by_id:
            raise ValueError(f"Unknown option position {position.position_id}")
        if close_packet_id and close_packet_id != position.packet_id:
            close_state = self._option_packet_state(close_packet_id)
            if close_state["status"] not in {"authorized", "consumed"}:
                raise ValueError("Option close packet is not authorized")
        existing_event = self.option_order_events.get(event_id)
        expected_event = {
            "event_id": event_id,
            "packet_id": position.packet_id,
            "position_id": position.position_id,
            "ref_id": ref_id,
            "broker_order_id": broker_order_id,
            "event_type": "closed",
            "occurred_at": occurred_at,
            "payload": dict(payload),
        }
        if existing_event is not None and existing_event != expected_event:
            raise ValueError(f"Option order event {event_id} is immutable")
        self.upsert_option_position(position)
        reservation = self.option_reservations.get(position.packet_id)
        if reservation is not None:
            self.release_option_collateral(position.packet_id, occurred_at)
        if (
            close_packet_id
            and close_packet_id != position.packet_id
            and self._option_packet_state(close_packet_id)["status"] == "authorized"
        ):
            self.consume_option_packet(close_packet_id, occurred_at)
        self.append_option_order_event(
            event_id,
            "closed",
            occurred_at,
            payload,
            packet_id=position.packet_id,
            position_id=position.position_id,
            ref_id=ref_id,
            broker_order_id=broker_order_id,
        )

    def cancel_option_packet(
        self,
        packet_id: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> None:
        occurred_at = occurred_at or datetime.now(UTC)
        state = self._option_packet_state(packet_id)
        if state["status"] == "revoked":
            if state["revocation_reason"] != reason:
                raise ValueError(f"Option packet {packet_id} was revoked for another reason")
            return
        if state["status"] != "authorized":
            raise ValueError(f"Option packet {packet_id} cannot be cancelled")
        reservation = self.option_reservations.get(packet_id)
        if reservation is not None and reservation["status"] == "active":
            self.release_option_collateral(packet_id, occurred_at)
        self.revoke_option_packet(packet_id, reason, occurred_at)

    def reserve_execution_budget(
        self,
        account_hash: str,
        trade_date: date,
        orders: list[tuple[str, float, bool, bool]],
        *,
        max_orders: int = 8,
        max_notional: float = 800.0,
        max_entry_orders: int = 6,
        max_entry_notional: float = 600.0,
        observed_usage: tuple[int, float, int, float] = (0, 0.0, 0, 0.0),
        max_option_openings: int = 3,
        observed_open_option_positions: int = 0,
        research_batch_id: str = "",
    ) -> dict[str, float | int]:
        _validate_execution_reservation(
            orders,
            max_orders,
            max_notional,
            max_entry_orders,
            max_entry_notional,
        )
        _validate_observed_execution_usage(observed_usage)
        if not 0 < max_option_openings <= 3 or observed_open_option_positions < 0:
            raise ValueError("Option opening limits cannot relax hard caps")
        if any(is_entry for _, _, is_entry, _ in orders):
            latest_batch = self.latest_research_batch(trade_date)
            if (
                not research_batch_id
                or latest_batch is None
                or latest_batch["batch_id"] != research_batch_id
                or self.latest_unfinished_cycle(trade_date) is not None
            ):
                raise RuntimeError(
                    "Execution reservation references stale research"
                )
        new_orders: list[tuple[str, float, bool, bool]] = []
        for ref_id, notional, is_entry, is_option_open in orders:
            existing = self.execution_reservations.get(ref_id)
            record = {
                "ref_id": ref_id,
                "account_key": account_hash,
                "trade_date": trade_date,
                "notional": float(notional),
                "is_entry": bool(is_entry),
                "is_option_open": bool(is_option_open),
            }
            if existing is not None:
                if existing != record:
                    raise ValueError(f"Execution reservation {ref_id} is immutable")
            else:
                new_orders.append(
                    (
                        ref_id,
                        float(notional),
                        bool(is_entry),
                        bool(is_option_open),
                    )
                )
        key = (account_hash, trade_date)
        usage = dict(
            self.execution_usage.get(
                key,
                {
                    "total_orders": 0,
                    "total_notional": 0.0,
                    "entry_orders": 0,
                    "entry_notional": 0.0,
                    "option_openings": 0,
                },
            )
        )
        observed_orders, observed_notional, observed_entry_orders, observed_entry_notional = (
            observed_usage
        )
        projected = {
            "total_orders": max(int(usage["total_orders"]), observed_orders)
            + len(new_orders),
            "total_notional": max(float(usage["total_notional"]), observed_notional)
            + sum(notional for _, notional, _, _ in new_orders),
            "entry_orders": max(int(usage["entry_orders"]), observed_entry_orders)
            + sum(is_entry for _, _, is_entry, _ in new_orders),
            "entry_notional": max(
                float(usage["entry_notional"]), observed_entry_notional
            )
            + sum(
                notional
                for _, notional, is_entry, _ in new_orders
                if is_entry
            ),
            "option_openings": int(usage["option_openings"])
            + sum(is_option_open for _, _, _, is_option_open in new_orders),
        }
        if (
            projected["total_orders"] > max_orders
            or projected["total_notional"] > max_notional
            or projected["entry_orders"] > max_entry_orders
            or projected["entry_notional"] > max_entry_notional
            or projected["option_openings"] > max_option_openings
            or observed_open_option_positions + projected["option_openings"]
            > max_option_openings
        ):
            raise RuntimeError("Durable execution budget would be exceeded")
        for ref_id, notional, is_entry, is_option_open in new_orders:
            self.execution_reservations[ref_id] = {
                "ref_id": ref_id,
                "account_key": account_hash,
                "trade_date": trade_date,
                "notional": notional,
                "is_entry": is_entry,
                "is_option_open": is_option_open,
            }
        self.execution_usage[key] = projected
        return projected

    def _option_packet_state(self, packet_id: str) -> dict[str, Any]:
        if packet_id not in self.option_packets:
            raise ValueError(f"Unknown option packet {packet_id}")
        return self.option_packet_states[packet_id]


class PostgresLedger:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("A Postgres database URL is required")
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> PostgresLedger:
        return cls(os.environ.get(DATABASE_URL_ENV, ""))

    def _connect(self):
        try:
            import psycopg
        except (
            ImportError
        ) as error:  # pragma: no cover - exercised only without optional dependency
            raise RuntimeError("Install psycopg[binary] to use PostgresLedger") from error
        try:
            return psycopg.connect(self.database_url)
        except Exception as error:  # pragma: no cover - depends on live network
            message = str(error).lower()
            if "network is unreachable" in message or "no route to host" in message:
                raise RuntimeError(
                    "Postgres connection failed with an unreachable-network error. "
                    "Cursor cloud sandboxes are IPv4-only; use the Supabase Shared "
                    "Pooler URI (host *.pooler.supabase.com, session mode :5432 or "
                    "transaction mode :6543), not the direct db.*.supabase.co host."
                ) from error
            if "password authentication failed" in message:
                raise RuntimeError(
                    "Postgres rejected DATABASE_URL credentials. For the Supabase "
                    "Shared Pooler the username must be postgres.<project-ref>, not "
                    "postgres alone; copy the Session pooler URI from the dashboard "
                    "Connect panel, URL-encode special characters in the password, "
                    "and confirm it is the database password (reset it there if "
                    "needed)."
                ) from error
            raise

    def apply_migration(self, path: str | Path) -> None:
        sql = Path(path).read_text()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql)

    @contextmanager
    def run_lock(self, lock_key: str) -> Iterator[None]:
        """Acquire a transaction-scoped global lock across cloud VMs."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            acquired = bool(cursor.fetchone()[0])
            if not acquired:
                raise RuntimeError("Another picker run holds the Postgres advisory lock")
            yield

    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_runs
                    (run_id, account_key, started_at, as_of, model_id,
                     prompt_hash, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run_id,
                    account_hash,
                    started_at,
                    as_of,
                    model_id,
                    prompt_hash,
                    status,
                    Jsonb(metadata or {}),
                ),
            )

    def put_evidence(self, evidence: EvidenceVersion) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO evidence_versions
                    (evidence_id, document_hash, published_at, first_seen_at, retrieved_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (evidence_id, document_hash) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    evidence.document_hash,
                    evidence.published_at,
                    evidence.first_seen_at,
                    evidence.retrieved_at,
                    Jsonb(evidence.to_dict()),
                ),
            )

    def put_draft(self, draft: PickerDraft) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_drafts (draft_id, run_id, symbol, created_at, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (draft_id) DO NOTHING
                """,
                (
                    draft.draft_id,
                    draft.run_id,
                    draft.symbol,
                    draft.created_at,
                    Jsonb(draft.to_dict()),
                ),
            )

    def put_critic(self, verdict: CriticVerdict) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO critic_verdicts (draft_id, created_at, verdict, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (draft_id) DO NOTHING
                """,
                (
                    verdict.draft_id,
                    verdict.created_at,
                    verdict.verdict,
                    Jsonb(verdict.to_dict()),
                ),
            )

    def authorize_packet(self, packet: DecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize a packet with an invalid hash")
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO decision_packets
                    (packet_id, run_id, draft_id, symbol, action, valid_for_date,
                     expires_at, packet_hash, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (packet_id) DO NOTHING
                """,
                (
                    packet.packet_id,
                    packet.run_id,
                    packet.draft_id,
                    packet.symbol,
                    packet.action,
                    packet.valid_for_date,
                    packet.expires_at,
                    packet.packet_hash,
                    Jsonb(packet.to_dict()),
                ),
            )

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM decision_packets
                WHERE valid_for_date = %s
                  AND expires_at > %s
                  AND status = 'authorized'
                ORDER BY (payload->>'rank_score')::double precision DESC, symbol
                """,
                (valid_for, now),
            )
            return [DecisionPacket.from_dict(row[0]) for row in cursor.fetchall()]

    def upsert_thesis(self, thesis: ActiveThesis) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO active_theses
                    (pick_id, packet_id, symbol, status, entry_date, expiry_date, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pick_id) DO UPDATE
                SET status = EXCLUDED.status,
                    expiry_date = EXCLUDED.expiry_date,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (
                    thesis.pick_id,
                    thesis.packet_id,
                    thesis.symbol,
                    thesis.status,
                    thesis.entry_date,
                    thesis.expiry_date,
                    Jsonb(thesis.to_dict()),
                ),
            )

    def active_theses(self) -> list[ActiveThesis]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM active_theses
                WHERE status IN ('pending_entry', 'active', 'expired', 'invalidated')
                ORDER BY pick_id
                """
            )
            return [ActiveThesis.from_dict(row[0]) for row in cursor.fetchall()]

    def control_state(self, account_hash: str) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT halted, halt_reason, high_water_mark, cooldown_until
                FROM picker_control_state
                WHERE account_key = %s
                """,
                (account_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return {
                    "halted": False,
                    "halt_reason": None,
                    "high_water_mark": None,
                    "cooldown_until": None,
                }
            return {
                "halted": bool(row[0]),
                "halt_reason": row[1],
                "high_water_mark": row[2],
                "cooldown_until": row[3],
            }

    def record_equity_peak(self, account_hash: str, equity: float) -> float:
        if equity <= 0:
            raise ValueError("Equity must be positive")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_control_state (account_key, high_water_mark)
                VALUES (%s, %s)
                ON CONFLICT (account_key) DO UPDATE
                SET high_water_mark = GREATEST(
                        COALESCE(
                            picker_control_state.high_water_mark,
                            EXCLUDED.high_water_mark
                        ),
                        EXCLUDED.high_water_mark
                    ),
                    updated_at = now()
                RETURNING high_water_mark
                """,
                (account_hash, equity),
            )
            return float(cursor.fetchone()[0])

    def halt(self, account_hash: str, reason: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_control_state (account_key, halted, halt_reason)
                VALUES (%s, TRUE, %s)
                ON CONFLICT (account_key) DO UPDATE
                SET halted = TRUE,
                    halt_reason = EXCLUDED.halt_reason,
                    updated_at = now()
                """,
                (account_hash, reason),
            )

    def put_outcome(self, outcome: OutcomeMark) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_outcomes
                    (packet_id, horizon_days, measured_at, raw_return,
                     spy_abnormal_return, sector_abnormal_return, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (packet_id, horizon_days) DO NOTHING
                """,
                (
                    outcome.packet_id,
                    outcome.horizon_days,
                    outcome.measured_at,
                    outcome.raw_return,
                    outcome.spy_abnormal_return,
                    outcome.sector_abnormal_return,
                    Jsonb(outcome.to_dict()),
                ),
            )

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_research_batches
                    (batch_id, as_of, created_at, prompt_hash, model_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO NOTHING
                """,
                (
                    batch_id,
                    as_of,
                    created_at,
                    prompt_hash,
                    model_id,
                    Jsonb(payload),
                ),
            )

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash, model_id, status, payload
                FROM picker_research_batches
                WHERE as_of = %s AND status = 'staged'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (as_of,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "batch_id": row[0],
                "as_of": row[1],
                "created_at": row[2],
                "prompt_hash": row[3],
                "model_id": row[4],
                "status": row[5],
                "payload": row[6],
            }

    def latest_research_batch(self, as_of: date) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash, model_id, status, payload
                FROM picker_research_batches
                WHERE as_of = %s AND status <> 'consumed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (as_of,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "batch_id": row[0],
                "as_of": row[1],
                "created_at": row[2],
                "prompt_hash": row[3],
                "model_id": row[4],
                "status": row[5],
                "payload": row[6],
            }

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in {"staged", "authorized", "rejected", "consumed"}:
            raise ValueError(f"Unsupported batch status: {status}")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE picker_research_batches
                SET status = %s
                WHERE batch_id = %s
                """,
                (status, batch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown research batch {batch_id}")

    def stage_pending_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        analyst_model_id: str,
        payload: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        values = (
            batch_id,
            as_of,
            created_at,
            prompt_hash,
            analyst_model_id,
            payload,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_pending_research_batches
                    (batch_id, as_of, created_at, prompt_hash,
                     analyst_model_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO NOTHING
                """,
                (*values[:-1], Jsonb(payload)),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash,
                       analyst_model_id, payload
                FROM picker_pending_research_batches
                WHERE batch_id = %s
                """,
                (batch_id,),
            )
            row = cursor.fetchone()
            if row is None or tuple(row) != values:
                raise ValueError(f"Pending research batch {batch_id} is immutable")

    def latest_pending_batch(self, as_of: date) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash,
                       analyst_model_id, status, payload, finalized_at
                FROM picker_pending_research_batches
                WHERE as_of = %s AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (as_of,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "batch_id": row[0],
                "as_of": row[1],
                "created_at": row[2],
                "prompt_hash": row[3],
                "analyst_model_id": row[4],
                "status": row[5],
                "payload": row[6],
                "finalized_at": row[7],
            }

    def pending_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash,
                       analyst_model_id, status, payload, finalized_at
                FROM picker_pending_research_batches
                WHERE batch_id = %s
                """,
                (batch_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "batch_id": row[0],
                "as_of": row[1],
                "created_at": row[2],
                "prompt_hash": row[3],
                "analyst_model_id": row[4],
                "status": row[5],
                "payload": row[6],
                "finalized_at": row[7],
            }

    def finalize_pending_batch(
        self,
        batch_id: str,
        status: str,
        finalized_at: datetime | None = None,
    ) -> None:
        if status not in {"finalized", "rejected"}:
            raise ValueError("Pending batch status must be finalized or rejected")
        finalized_at = finalized_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE picker_pending_research_batches
                SET status = %s, finalized_at = %s
                WHERE batch_id = %s AND status = 'pending'
                """,
                (status, finalized_at, batch_id),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                "SELECT status FROM picker_pending_research_batches "
                "WHERE batch_id = %s",
                (batch_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] != status:
                raise ValueError(f"Pending batch {batch_id} cannot become {status}")

    def start_research_cycle(
        self,
        cycle_id: str,
        as_of: date,
        started_at: datetime,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"research-execution-cycle:{as_of.isoformat()}",),
            )
            cursor.execute(
                """
                INSERT INTO picker_research_cycles
                    (cycle_id, as_of, started_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (cycle_id) DO NOTHING
                """,
                (cycle_id, as_of, started_at),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                "SELECT as_of, started_at FROM picker_research_cycles "
                "WHERE cycle_id = %s",
                (cycle_id,),
            )
            row = cursor.fetchone()
            if row is None or tuple(row) != (as_of, started_at):
                raise ValueError(f"Research cycle {cycle_id} is immutable")

    def bind_research_cycle(self, cycle_id: str, batch_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE picker_research_cycles
                SET status = 'pending', batch_id = %s, updated_at = now()
                WHERE cycle_id = %s
                  AND status IN ('running', 'pending')
                  AND (batch_id IS NULL OR batch_id = %s)
                """,
                (batch_id, cycle_id, batch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Research cycle {cycle_id} cannot bind {batch_id}")

    def finish_research_cycle(self, cycle_id: str, status: str) -> None:
        if status not in {"finalized", "failed"}:
            raise ValueError("Research cycle must finish finalized or failed")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE picker_research_cycles
                SET status = %s, updated_at = now()
                WHERE cycle_id = %s
                """,
                (status, cycle_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown research cycle {cycle_id}")

    def latest_unfinished_cycle(self, as_of: date) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cycle_id, as_of, started_at, status, batch_id
                FROM picker_research_cycles
                WHERE as_of = %s AND status IN ('running', 'pending')
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (as_of,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "cycle_id": row[0],
                "as_of": row[1],
                "started_at": row[2],
                "status": row[3],
                "batch_id": row[4],
            }

    def authorize_option_packet(self, packet: OptionDecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize an option packet with an invalid hash")
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO option_decision_packets
                    (packet_id, valid_for_date, expires_at, packet_hash,
                     structure_fingerprint, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    packet.packet_id,
                    packet.valid_for_date,
                    packet.expires_at,
                    packet.packet_hash,
                    packet.structure_fingerprint,
                    Jsonb(packet.to_dict()),
                ),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT payload FROM option_decision_packets WHERE packet_id = %s",
                    (packet.packet_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("An option packet already exists for this hash or structure")
                if OptionDecisionPacket.from_dict(row[0]) != packet:
                    raise ValueError(f"Option packet {packet.packet_id} is immutable")

    def consume_option_packet(self, packet_id: str, consumed_at: datetime | None = None) -> None:
        consumed_at = consumed_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE option_decision_packets
                SET status = 'consumed', consumed_at = %s
                WHERE packet_id = %s AND status = 'authorized'
                """,
                (consumed_at, packet_id),
            )
            if cursor.rowcount == 1:
                return
            status = self._option_packet_status(cursor, packet_id)
            if status != "consumed":
                raise ValueError(f"Option packet {packet_id} cannot be consumed from {status}")

    def revoke_option_packet(
        self,
        packet_id: str,
        reason: str,
        revoked_at: datetime | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("An option packet revocation reason is required")
        revoked_at = revoked_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE option_decision_packets
                SET status = 'revoked', revoked_at = %s, revocation_reason = %s
                WHERE packet_id = %s AND status = 'authorized'
                """,
                (revoked_at, reason, packet_id),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                """
                SELECT status, revocation_reason
                FROM option_decision_packets
                WHERE packet_id = %s
                """,
                (packet_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Unknown option packet {packet_id}")
            if row[0] != "revoked":
                raise ValueError(f"Option packet {packet_id} cannot be revoked from {row[0]}")
            if row[1] != reason:
                raise ValueError(f"Option packet {packet_id} was revoked for another reason")

    def valid_option_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[OptionDecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM option_decision_packets
                WHERE valid_for_date = %s
                  AND expires_at > %s
                  AND status = 'authorized'
                ORDER BY packet_id
                """,
                (valid_for, now),
            )
            packets = [OptionDecisionPacket.from_dict(row[0]) for row in cursor.fetchall()]
        return [packet for packet in packets if packet.verify_hash()]

    def option_packet(self, packet_id: str) -> OptionDecisionPacket | None:
        from .option_models import OptionDecisionPacket

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM option_decision_packets WHERE packet_id = %s",
                (packet_id,),
            )
            row = cursor.fetchone()
        return OptionDecisionPacket.from_dict(row[0]) if row is not None else None

    def upsert_option_position(self, position: ActiveOptionPosition) -> None:
        if not position.verify_hash():
            raise ValueError("Cannot store an option position with an invalid hash")
        from psycopg.types.json import Jsonb

        payload = position.to_dict()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO active_option_positions
                    (position_id, packet_id, underlying, strategy, status, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (position_id) DO UPDATE
                SET status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                WHERE active_option_positions.packet_id
                        IS NOT DISTINCT FROM EXCLUDED.packet_id
                  AND active_option_positions.underlying = EXCLUDED.underlying
                  AND active_option_positions.strategy = EXCLUDED.strategy
                """,
                (
                    position.position_id,
                    payload.get("packet_id"),
                    position.underlying,
                    position.strategy,
                    position.status,
                    Jsonb(payload),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Option position {position.position_id} has immutable identity fields"
                )

    def option_positions(
        self,
        status: str | None = None,
        underlying: str | None = None,
    ) -> list[ActiveOptionPosition]:
        filters: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            filters.append("status = %s")
            parameters.append(status)
        if underlying is not None:
            filters.append("underlying = %s")
            parameters.append(underlying)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT payload
                FROM active_option_positions
                {where}
                ORDER BY position_id
                """,
                parameters,
            )
            return [ActiveOptionPosition.from_dict(row[0]) for row in cursor.fetchall()]

    def reserve_option_collateral(
        self,
        packet_id: str,
        account_hash: str,
        collateral_amount: float,
        share_encumbrances: dict[str, int],
        *,
        available_cash: float,
        available_shares: dict[str, int],
        reserved_at: datetime | None = None,
    ) -> None:
        normalized_shares = _validate_option_reservation(
            collateral_amount,
            share_encumbrances,
            available_cash,
            available_shares,
        )
        reserved_at = reserved_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (account_hash,),
            )
            cursor.execute(
                "SELECT status FROM option_decision_packets WHERE packet_id = %s",
                (packet_id,),
            )
            packet_row = cursor.fetchone()
            if packet_row is None:
                raise ValueError(f"Unknown option packet {packet_id}")
            if packet_row[0] != "authorized":
                raise ValueError(
                    f"Option packet {packet_id} cannot reserve collateral from {packet_row[0]}"
                )

            cursor.execute(
                """
                SELECT account_key, collateral_amount, status
                FROM option_resource_reservations
                WHERE packet_id = %s
                """,
                (packet_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                cursor.execute(
                    """
                    SELECT symbol, quantity
                    FROM option_share_encumbrances
                    WHERE packet_id = %s
                    ORDER BY symbol
                    """,
                    (packet_id,),
                )
                existing_shares = dict(cursor.fetchall())
                if (
                    existing[0] == account_hash
                    and float(existing[1]) == float(collateral_amount)
                    and existing[2] == "active"
                    and existing_shares == normalized_shares
                ):
                    return
                raise ValueError(f"Option collateral for {packet_id} is already reserved")

            cursor.execute(
                """
                SELECT COALESCE(SUM(collateral_amount), 0)
                FROM option_resource_reservations
                WHERE account_key = %s AND status = 'active'
                """,
                (account_hash,),
            )
            reserved_cash = float(cursor.fetchone()[0])
            if reserved_cash + collateral_amount > available_cash:
                raise ValueError("Insufficient unencumbered cash for option collateral")

            if normalized_shares:
                cursor.execute(
                    """
                    SELECT shares.symbol, SUM(shares.quantity)
                    FROM option_share_encumbrances AS shares
                    JOIN option_resource_reservations AS reservations
                      ON reservations.packet_id = shares.packet_id
                    WHERE reservations.account_key = %s
                      AND reservations.status = 'active'
                      AND shares.symbol = ANY(%s)
                    GROUP BY shares.symbol
                    """,
                    (account_hash, list(normalized_shares)),
                )
                already_reserved = dict(cursor.fetchall())
                for symbol, quantity in normalized_shares.items():
                    if int(already_reserved.get(symbol, 0)) + quantity > available_shares.get(
                        symbol, 0
                    ):
                        raise ValueError(
                            f"Insufficient unencumbered shares for option collateral: {symbol}"
                        )

            cursor.execute(
                """
                INSERT INTO option_resource_reservations
                    (packet_id, account_key, collateral_amount, reserved_at)
                VALUES (%s, %s, %s, %s)
                """,
                (packet_id, account_hash, collateral_amount, reserved_at),
            )
            if normalized_shares:
                cursor.executemany(
                    """
                    INSERT INTO option_share_encumbrances
                        (packet_id, symbol, quantity)
                    VALUES (%s, %s, %s)
                    """,
                    [
                        (packet_id, symbol, quantity)
                        for symbol, quantity in normalized_shares.items()
                    ],
                )

    def release_option_collateral(
        self, packet_id: str, released_at: datetime | None = None
    ) -> None:
        released_at = released_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE option_resource_reservations
                SET status = 'released', released_at = %s
                WHERE packet_id = %s AND status = 'active'
                """,
                (released_at, packet_id),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                "SELECT status FROM option_resource_reservations WHERE packet_id = %s",
                (packet_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"No option collateral is reserved for {packet_id}")
            if row[0] != "released":
                raise ValueError(f"Unsupported option collateral status: {row[0]}")

    def append_option_order_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        packet_id: str | None = None,
        position_id: str | None = None,
        ref_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        values = (
            event_id,
            packet_id,
            position_id,
            ref_id,
            broker_order_id,
            event_type,
            occurred_at,
            payload,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO option_order_events
                    (event_id, packet_id, position_id, ref_id, broker_order_id,
                     event_type, occurred_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (*values[:-1], Jsonb(payload)),
            )
            if cursor.rowcount == 1:
                return
            cursor.execute(
                """
                SELECT event_id, packet_id, position_id, ref_id, broker_order_id,
                       event_type, occurred_at, payload
                FROM option_order_events
                WHERE event_id = %s
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            if row is None or tuple(row) != values:
                raise ValueError(f"Option order event {event_id} is immutable")

    def sync_option_open(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
    ) -> None:
        if not position.verify_hash() or position.status != "open":
            raise ValueError("Opening position must be hash-valid and open")
        from psycopg.types.json import Jsonb

        event_values = (
            event_id,
            position.packet_id,
            position.position_id,
            ref_id,
            broker_order_id,
            "opened",
            occurred_at,
            payload,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM option_decision_packets "
                "WHERE packet_id = %s FOR UPDATE",
                (position.packet_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] not in {"authorized", "consumed"}:
                raise ValueError("Option opening packet is not authorized")
            cursor.execute(
                """
                INSERT INTO active_option_positions
                    (position_id, packet_id, underlying, strategy, status, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (position_id) DO UPDATE
                SET status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                WHERE active_option_positions.packet_id = EXCLUDED.packet_id
                  AND active_option_positions.underlying = EXCLUDED.underlying
                  AND active_option_positions.strategy = EXCLUDED.strategy
                """,
                (
                    position.position_id,
                    position.packet_id,
                    position.underlying,
                    position.strategy,
                    position.status,
                    Jsonb(position.to_dict()),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Option position identity mismatch")
            if row[0] == "authorized":
                cursor.execute(
                    """
                    UPDATE option_decision_packets
                    SET status = 'consumed', consumed_at = %s
                    WHERE packet_id = %s AND status = 'authorized'
                    """,
                    (occurred_at, position.packet_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Option packet consumption raced")
            cursor.execute(
                """
                INSERT INTO option_order_events
                    (event_id, packet_id, position_id, ref_id, broker_order_id,
                     event_type, occurred_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (*event_values[:-1], Jsonb(payload)),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    """
                    SELECT event_id, packet_id, position_id, ref_id,
                           broker_order_id, event_type, occurred_at, payload
                    FROM option_order_events WHERE event_id = %s
                    """,
                    (event_id,),
                )
                existing = cursor.fetchone()
                if existing is None or tuple(existing) != event_values:
                    raise ValueError(f"Option order event {event_id} is immutable")

    def sync_option_close(
        self,
        position: ActiveOptionPosition,
        event_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        ref_id: str,
        broker_order_id: str,
        close_packet_id: str | None = None,
    ) -> None:
        if not position.verify_hash() or position.status != "closed":
            raise ValueError("Closing position must be hash-valid and closed")
        from psycopg.types.json import Jsonb

        event_values = (
            event_id,
            position.packet_id,
            position.position_id,
            ref_id,
            broker_order_id,
            "closed",
            occurred_at,
            payload,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT packet_id, underlying, strategy
                FROM active_option_positions
                WHERE position_id = %s
                FOR UPDATE
                """,
                (position.position_id,),
            )
            existing_position = cursor.fetchone()
            if existing_position is None or tuple(existing_position) != (
                position.packet_id,
                position.underlying,
                position.strategy,
            ):
                raise ValueError("Option position identity mismatch")
            if close_packet_id and close_packet_id != position.packet_id:
                cursor.execute(
                    "SELECT status FROM option_decision_packets "
                    "WHERE packet_id = %s FOR UPDATE",
                    (close_packet_id,),
                )
                close_state = cursor.fetchone()
                if close_state is None or close_state[0] not in {
                    "authorized",
                    "consumed",
                }:
                    raise ValueError("Option close packet is not authorized")
            cursor.execute(
                """
                UPDATE active_option_positions
                SET status = %s, payload = %s, updated_at = now()
                WHERE position_id = %s
                """,
                (position.status, Jsonb(position.to_dict()), position.position_id),
            )
            cursor.execute(
                """
                UPDATE option_resource_reservations
                SET status = 'released', released_at = %s
                WHERE packet_id = %s AND status = 'active'
                """,
                (occurred_at, position.packet_id),
            )
            if close_packet_id and close_packet_id != position.packet_id:
                cursor.execute(
                    """
                    UPDATE option_decision_packets
                    SET status = 'consumed', consumed_at = %s
                    WHERE packet_id = %s AND status = 'authorized'
                    """,
                    (occurred_at, close_packet_id),
                )
            cursor.execute(
                """
                INSERT INTO option_order_events
                    (event_id, packet_id, position_id, ref_id, broker_order_id,
                     event_type, occurred_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (*event_values[:-1], Jsonb(payload)),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    """
                    SELECT event_id, packet_id, position_id, ref_id,
                           broker_order_id, event_type, occurred_at, payload
                    FROM option_order_events WHERE event_id = %s
                    """,
                    (event_id,),
                )
                existing = cursor.fetchone()
                if existing is None or tuple(existing) != event_values:
                    raise ValueError(f"Option order event {event_id} is immutable")

    def cancel_option_packet(
        self,
        packet_id: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("An option packet cancellation reason is required")
        occurred_at = occurred_at or datetime.now(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, revocation_reason
                FROM option_decision_packets
                WHERE packet_id = %s
                FOR UPDATE
                """,
                (packet_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Unknown option packet {packet_id}")
            if row[0] == "revoked":
                if row[1] != reason:
                    raise ValueError(
                        f"Option packet {packet_id} was revoked for another reason"
                    )
                return
            if row[0] != "authorized":
                raise ValueError(f"Option packet {packet_id} cannot be cancelled")
            cursor.execute(
                """
                UPDATE option_resource_reservations
                SET status = 'released', released_at = %s
                WHERE packet_id = %s AND status = 'active'
                """,
                (occurred_at, packet_id),
            )
            cursor.execute(
                """
                UPDATE option_decision_packets
                SET status = 'revoked', revoked_at = %s, revocation_reason = %s
                WHERE packet_id = %s AND status = 'authorized'
                """,
                (occurred_at, reason, packet_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Option packet cancellation raced")

    def reserve_execution_budget(
        self,
        account_hash: str,
        trade_date: date,
        orders: list[tuple[str, float, bool, bool]],
        *,
        max_orders: int = 8,
        max_notional: float = 800.0,
        max_entry_orders: int = 6,
        max_entry_notional: float = 600.0,
        observed_usage: tuple[int, float, int, float] = (0, 0.0, 0, 0.0),
        max_option_openings: int = 3,
        observed_open_option_positions: int = 0,
        research_batch_id: str = "",
    ) -> dict[str, float | int]:
        _validate_execution_reservation(
            orders,
            max_orders,
            max_notional,
            max_entry_orders,
            max_entry_notional,
        )
        _validate_observed_execution_usage(observed_usage)
        if not 0 < max_option_openings <= 3 or observed_open_option_positions < 0:
            raise ValueError("Option opening limits cannot relax hard caps")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"research-execution-cycle:{trade_date.isoformat()}",),
            )
            if any(is_entry for _, _, is_entry, _ in orders):
                cursor.execute(
                    """
                    SELECT batch_id
                    FROM picker_research_batches
                    WHERE as_of = %s AND status <> 'consumed'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (trade_date,),
                )
                latest_batch = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT cycle_id
                    FROM picker_research_cycles
                    WHERE as_of = %s AND status IN ('running', 'pending')
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (trade_date,),
                )
                unfinished = cursor.fetchone()
                if (
                    not research_batch_id
                    or latest_batch is None
                    or str(latest_batch[0]) != research_batch_id
                    or unfinished is not None
                ):
                    raise RuntimeError(
                        "Execution reservation references stale research"
                    )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-budget:{account_hash}:{trade_date.isoformat()}",),
            )
            cursor.execute(
                """
                INSERT INTO execution_daily_usage (account_key, trade_date)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (account_hash, trade_date),
            )
            cursor.execute(
                """
                SELECT total_orders, total_notional,
                       entry_orders, entry_notional, option_openings
                FROM execution_daily_usage
                WHERE account_key = %s AND trade_date = %s
                FOR UPDATE
                """,
                (account_hash, trade_date),
            )
            row = cursor.fetchone()
            usage = {
                "total_orders": int(row[0]),
                "total_notional": float(row[1]),
                "entry_orders": int(row[2]),
                "entry_notional": float(row[3]),
                "option_openings": int(row[4]),
            }
            ref_ids = [ref_id for ref_id, _, _, _ in orders]
            cursor.execute(
                """
                SELECT ref_id, account_key, trade_date, notional,
                       is_entry, is_option_open
                FROM execution_plan_reservations
                WHERE ref_id = ANY(%s)
                """,
                (ref_ids,),
            )
            existing = {
                str(item[0]): (
                    str(item[1]),
                    item[2],
                    float(item[3]),
                    bool(item[4]),
                    bool(item[5]),
                )
                for item in cursor.fetchall()
            }
            new_orders: list[tuple[str, float, bool, bool]] = []
            for ref_id, notional, is_entry, is_option_open in orders:
                if ref_id in existing:
                    if existing[ref_id] != (
                        account_hash,
                        trade_date,
                        float(notional),
                        bool(is_entry),
                        bool(is_option_open),
                    ):
                        raise ValueError(
                            f"Execution reservation {ref_id} is immutable"
                        )
                else:
                    new_orders.append(
                        (
                            ref_id,
                            float(notional),
                            bool(is_entry),
                            bool(is_option_open),
                        )
                    )
            (
                observed_orders,
                observed_notional,
                observed_entry_orders,
                observed_entry_notional,
            ) = observed_usage
            projected = {
                "total_orders": max(usage["total_orders"], observed_orders)
                + len(new_orders),
                "total_notional": max(
                    usage["total_notional"], observed_notional
                )
                + sum(notional for _, notional, _, _ in new_orders),
                "entry_orders": max(
                    usage["entry_orders"], observed_entry_orders
                )
                + sum(is_entry for _, _, is_entry, _ in new_orders),
                "entry_notional": max(
                    usage["entry_notional"], observed_entry_notional
                )
                + sum(
                    notional
                    for _, notional, is_entry, _ in new_orders
                    if is_entry
                ),
                "option_openings": usage["option_openings"]
                + sum(
                    is_option_open
                    for _, _, _, is_option_open in new_orders
                ),
            }
            if (
                projected["total_orders"] > max_orders
                or projected["total_notional"] > max_notional
                or projected["entry_orders"] > max_entry_orders
                or projected["entry_notional"] > max_entry_notional
                or projected["option_openings"] > max_option_openings
                or observed_open_option_positions
                + projected["option_openings"]
                > max_option_openings
            ):
                raise RuntimeError("Durable execution budget would be exceeded")
            cursor.execute(
                """
                UPDATE execution_daily_usage
                SET total_orders = %s,
                    total_notional = %s,
                    entry_orders = %s,
                    entry_notional = %s,
                    option_openings = %s,
                    updated_at = now()
                WHERE account_key = %s AND trade_date = %s
                """,
                (
                    projected["total_orders"],
                    projected["total_notional"],
                    projected["entry_orders"],
                    projected["entry_notional"],
                    projected["option_openings"],
                    account_hash,
                    trade_date,
                ),
            )
            if new_orders:
                cursor.executemany(
                    """
                    INSERT INTO execution_plan_reservations
                        (ref_id, account_key, trade_date, notional,
                         is_entry, is_option_open)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            ref_id,
                            account_hash,
                            trade_date,
                            notional,
                            is_entry,
                            is_option_open,
                        )
                        for ref_id, notional, is_entry, is_option_open in new_orders
                    ],
                )
            return projected

    @staticmethod
    def _option_packet_status(cursor: Any, packet_id: str) -> str:
        cursor.execute(
            "SELECT status FROM option_decision_packets WHERE packet_id = %s",
            (packet_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Unknown option packet {packet_id}")
        return str(row[0])
