from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite
from typing import Any

from .models import CriticVerdict, EvidenceVersion, PickerDraft
from .option_models import (
    OPTION_ACTIONS,
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
    OptionDraft,
)

OPTION_PACKET_NAMESPACE = uuid.UUID("5d82ddfc-0a78-4ed0-975d-d905ef35fd46")


@dataclass(frozen=True)
class LiveOptionPolicy:
    min_dte: int = 21
    max_dte: int = 60
    target_dte: int = 30
    max_quote_age_seconds: int = 60
    max_spread_pct_midpoint: float = 0.10
    quantity: int = 1
    max_long_premium_dollars: float = 75.0
    max_long_premium_equity_fraction: float = 0.05
    max_aggregate_long_premium_fraction: float = 0.10
    covered_call_shares: int = 100
    max_csp_collateral_fraction: float = 0.30
    max_post_assignment_fraction: float = 0.15
    packet_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        if self.quantity != 1:
            raise ValueError("Option policy quantity must equal one")
        if self.min_dte < 21 or self.max_dte > 60 or self.min_dte > self.max_dte:
            raise ValueError("Option policy DTE cannot relax the 21-60 day bounds")
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("target_dte must be inside the policy DTE range")
        if not 0 < self.max_quote_age_seconds <= 60:
            raise ValueError("Option quote age cannot exceed 60 seconds")
        ceilings = {
            "max_spread_pct_midpoint": (self.max_spread_pct_midpoint, 0.10),
            "max_long_premium_dollars": (self.max_long_premium_dollars, 75.0),
            "max_long_premium_equity_fraction": (
                self.max_long_premium_equity_fraction,
                0.05,
            ),
            "max_aggregate_long_premium_fraction": (
                self.max_aggregate_long_premium_fraction,
                0.10,
            ),
            "max_csp_collateral_fraction": (
                self.max_csp_collateral_fraction,
                0.30,
            ),
            "max_post_assignment_fraction": (
                self.max_post_assignment_fraction,
                0.15,
            ),
        }
        for name, (value, ceiling) in ceilings.items():
            if not isfinite(value) or value <= 0 or value > ceiling:
                raise ValueError(f"{name} cannot relax its hard ceiling")
        if not 0 < self.packet_ttl_seconds <= 300:
            raise ValueError("Option packet TTL must be between 1 and 300 seconds")


@dataclass(frozen=True)
class OptionValidationResult:
    accepted: bool
    reasons: tuple[str, ...]
    packet: OptionDecisionPacket | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "packet": self.packet.to_dict() if self.packet is not None else None,
        }


def _as_snapshot(raw: OptionContractSnapshot | dict[str, Any]) -> OptionContractSnapshot:
    if isinstance(raw, OptionContractSnapshot):
        return raw
    return OptionContractSnapshot.from_broker_dict(raw)


def _eligible_contract(
    snapshot: OptionContractSnapshot,
    action: str,
    now: datetime,
    policy: LiveOptionPolicy,
    underlying: str | None,
    contract_id: str | None,
) -> bool:
    if not snapshot.verify_hash():
        return False
    if underlying is not None and snapshot.underlying != underlying.upper():
        return False
    if contract_id is not None and snapshot.option_id != contract_id:
        return False
    quote_age = (now - snapshot.quote_at).total_seconds()
    if quote_age < 0 or quote_age > policy.max_quote_age_seconds:
        return False
    if snapshot.bid <= 0 or snapshot.ask < snapshot.bid:
        return False
    if snapshot.spread_pct_midpoint > policy.max_spread_pct_midpoint + 1e-12:
        return False
    if action != "close":
        dte = snapshot.days_to_expiration(now)
        if not policy.min_dte <= dte <= policy.max_dte:
            return False
    expected_type = {
        "long_call": "call",
        "covered_call": "call",
        "long_put": "put",
        "cash_secured_put": "put",
    }.get(action)
    if expected_type is not None and snapshot.option_type != expected_type:
        return False
    if action == "covered_call" and snapshot.strike < snapshot.underlying_price:
        return False
    if action == "cash_secured_put" and snapshot.strike > snapshot.underlying_price:
        return False
    return True


def _contract_rank(
    snapshot: OptionContractSnapshot,
    action: str,
    now: datetime,
    policy: LiveOptionPolicy,
) -> tuple[float, int, float, str, float, str]:
    target_delta = 0.50 if action in {"long_call", "long_put"} else 0.30
    moneyness = abs(snapshot.strike / snapshot.underlying_price - 1.0)
    delta_or_moneyness = (
        abs(abs(snapshot.delta) - target_delta) if snapshot.delta is not None else moneyness
    )
    return (
        delta_or_moneyness,
        abs(snapshot.days_to_expiration(now) - policy.target_dte),
        moneyness,
        snapshot.expiration_date.isoformat(),
        snapshot.strike,
        snapshot.option_id,
    )


def select_option_contract(
    action: str | OptionDraft,
    snapshots: Iterable[OptionContractSnapshot | dict[str, Any]],
    *,
    now: datetime | None = None,
    policy: LiveOptionPolicy | None = None,
    underlying: str | None = None,
    contract_id: str | None = None,
) -> OptionContractSnapshot:
    """Select exactly one broker contract with deterministic, code-owned ranking."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policy = policy or LiveOptionPolicy()
    if isinstance(action, OptionDraft):
        draft = action
        action_name = draft.action
        underlying = underlying or draft.underlying
        contract_id = contract_id or draft.contract_id
    else:
        action_name = str(action).lower()
    if action_name not in OPTION_ACTIONS - {"reject"}:
        raise ValueError(f"Unsupported option selection action: {action_name}")

    eligible: list[OptionContractSnapshot] = []
    for raw in snapshots:
        try:
            snapshot = _as_snapshot(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if _eligible_contract(
            snapshot,
            action_name,
            now,
            policy,
            underlying,
            contract_id,
        ):
            eligible.append(snapshot)
    if not eligible:
        raise ValueError("No eligible broker option contract")
    return min(
        eligible,
        key=lambda item: _contract_rank(item, action_name, now, policy),
    )


def _limit_price(snapshot: OptionContractSnapshot) -> float:
    return float(
        Decimal(str(snapshot.midpoint)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _packet_id(
    draft: OptionDraft,
    contract: OptionContractSnapshot,
    valid_for: str,
    side: str,
    position_effect: str,
) -> str:
    key = "|".join(
        (
            draft.run_id,
            draft.draft_id,
            draft.draft_hash,
            valid_for,
            draft.action,
            contract.contract_fingerprint,
            side,
            position_effect,
        )
    )
    return str(uuid.uuid5(OPTION_PACKET_NAMESPACE, key))


def _append_evidence_reasons(
    reasons: list[str],
    draft: OptionDraft,
    evidence_by_id: dict[str, EvidenceVersion],
    now: datetime,
) -> None:
    evidence: list[EvidenceVersion] = []
    for evidence_id in draft.evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            reasons.append(f"unknown_evidence:{evidence_id}")
            continue
        evidence.append(item)
        if item.first_seen_at > now or item.published_at > now:
            reasons.append(f"future_evidence:{evidence_id}")
        if not item.quote_verified:
            reasons.append(f"ungrounded_quote:{evidence_id}")
    if draft.action not in {"close", "reject"}:
        if not any(item.primary for item in evidence):
            reasons.append("no_primary_source")
        if len({item.independence_group for item in evidence}) < 2:
            reasons.append("fewer_than_two_independent_sources")


def _append_inheritance_reasons(
    reasons: list[str],
    draft: OptionDraft,
    critic: CriticVerdict,
    source_draft: PickerDraft | None,
) -> None:
    critic_target = draft.draft_id
    earliest_critic_time = draft.created_at
    if draft.source_draft_id is not None:
        if source_draft is None:
            reasons.append("missing_source_picker_draft")
        else:
            critic_target = source_draft.draft_id
            earliest_critic_time = source_draft.created_at
            if source_draft.draft_id != draft.source_draft_id:
                reasons.append("source_draft_id_mismatch")
            if source_draft.symbol != draft.underlying:
                reasons.append("source_symbol_mismatch")
            if source_draft.evidence_ids != draft.evidence_ids:
                reasons.append("evidence_not_inherited_exactly")
            if source_draft.created_at > draft.created_at:
                reasons.append("option_draft_predates_source")
    elif source_draft is not None:
        reasons.append("unexpected_source_picker_draft")

    if critic.draft_id != critic_target:
        reasons.append("critic_draft_mismatch")
    if critic.created_at < earliest_critic_time:
        reasons.append("critic_predates_draft")
    if critic.verdict != "pass":
        reasons.append("critic_veto")
    if critic.contradicted_evidence_ids:
        reasons.append("critic_found_contradicted_evidence")


def _open_positions(
    positions: Iterable[ActiveOptionPosition],
) -> tuple[ActiveOptionPosition, ...]:
    return tuple(
        position
        for position in positions
        if position.status in {"pending_open", "open", "closing"}
    )


def validate_option_draft(
    draft: OptionDraft,
    evidence_by_id: dict[str, EvidenceVersion],
    contracts: Iterable[OptionContractSnapshot | dict[str, Any]],
    critic: CriticVerdict,
    prompt_hash: str,
    model_id: str,
    account_equity: float,
    *,
    available_cash: float = 0.0,
    open_positions: Iterable[ActiveOptionPosition] = (),
    source_draft: PickerDraft | None = None,
    underlying_shares: int = 0,
    encumbered_shares: int = 0,
    unencumbered_shares: int | None = None,
    current_underlying_value: float = 0.0,
    aggregate_open_premium_at_risk: float | None = None,
    now: datetime | None = None,
    policy: LiveOptionPolicy | None = None,
) -> OptionValidationResult:
    """Authorize a single-contract limit packet from evidence and live broker quotes."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policy = policy or LiveOptionPolicy()
    reasons: list[str] = []
    positions = _open_positions(open_positions)

    if not draft.verify_hash():
        reasons.append("draft_hash_mismatch")
    if draft.action == "reject":
        reasons.append("draft_abstained")
    if draft.created_at > now:
        reasons.append("draft_timestamp_in_future")
    if not isfinite(account_equity) or account_equity <= 0:
        reasons.append("account_equity_not_positive")
    if policy.quantity != 1:
        reasons.append("quantity_must_equal_one")
    if not isfinite(available_cash) or available_cash < 0:
        reasons.append("available_cash_negative")
    if not isfinite(current_underlying_value) or current_underlying_value < 0:
        reasons.append("underlying_value_negative")
    if any(not position.verify_hash() for position in positions):
        reasons.append("active_position_hash_mismatch")

    _append_inheritance_reasons(reasons, draft, critic, source_draft)
    _append_evidence_reasons(reasons, draft, evidence_by_id, now)

    matching_position: ActiveOptionPosition | None = None
    contract_id = draft.contract_id
    if draft.action == "close":
        candidates = [
            item
            for item in positions
            if item.underlying == draft.underlying
            and (draft.position_id is None or item.position_id == draft.position_id)
            and (draft.contract_id is None or item.option_id == draft.contract_id)
        ]
        if len(candidates) != 1:
            reasons.append("close_position_not_unique")
        else:
            matching_position = candidates[0]
            contract_id = matching_position.option_id

    contract: OptionContractSnapshot | None = None
    if draft.action != "reject":
        try:
            contract = select_option_contract(
                draft.action,
                contracts,
                now=now,
                policy=policy,
                underlying=draft.underlying,
                contract_id=contract_id,
            )
        except ValueError:
            reasons.append("no_eligible_contract")

    limit_price = _limit_price(contract) if contract is not None else 0.0
    max_risk = 0.0
    collateral_required = 0.0
    shares_encumbered = 0

    if draft.action in {"long_call", "long_put"} and contract is not None:
        max_risk = limit_price * 100 * policy.quantity
        trade_cap = min(
            policy.max_long_premium_dollars,
            policy.max_long_premium_equity_fraction * account_equity,
        )
        if max_risk > trade_cap:
            reasons.append("long_premium_risk_exceeds_trade_cap")
        existing_risk = aggregate_open_premium_at_risk
        if existing_risk is None:
            existing_risk = sum(
                item.premium_at_risk
                for item in positions
                if item.strategy in {"long_call", "long_put"}
            )
        if not isfinite(existing_risk) or existing_risk < 0:
            reasons.append("aggregate_open_premium_negative")
        elif max_risk + existing_risk > (
            policy.max_aggregate_long_premium_fraction * account_equity
        ):
            reasons.append("aggregate_long_premium_risk_exceeds_cap")

    if draft.action == "covered_call":
        free_shares = (
            int(unencumbered_shares)
            if unencumbered_shares is not None
            else int(underlying_shares) - int(encumbered_shares)
        )
        if free_shares < policy.covered_call_shares:
            reasons.append("insufficient_unencumbered_shares")
        shares_encumbered = policy.covered_call_shares

    if draft.action == "cash_secured_put" and contract is not None:
        collateral_required = contract.strike * 100 * policy.quantity
        if collateral_required > policy.max_csp_collateral_fraction * account_equity:
            reasons.append("csp_collateral_exceeds_equity_cap")
        if collateral_required > available_cash:
            reasons.append("insufficient_cash_for_csp")
        post_assignment_value = current_underlying_value + collateral_required
        if post_assignment_value > policy.max_post_assignment_fraction * account_equity:
            reasons.append("post_assignment_issuer_limit_exceeded")

    if reasons:
        return OptionValidationResult(False, tuple(dict.fromkeys(reasons)), None)

    assert contract is not None
    if draft.action == "close":
        assert matching_position is not None
        side = "sell" if matching_position.side == "long" else "buy"
        position_effect = "close"
    elif draft.action in {"long_call", "long_put"}:
        side = "buy"
        position_effect = "open"
    else:
        side = "sell"
        position_effect = "open"

    packet = OptionDecisionPacket(
        packet_id=_packet_id(
            draft,
            contract,
            now.date().isoformat(),
            side,
            position_effect,
        ),
        run_id=draft.run_id,
        draft_id=draft.draft_id,
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(seconds=policy.packet_ttl_seconds),
        underlying=draft.underlying,
        action=draft.action,
        contract=contract,
        quantity=policy.quantity,
        side=side,
        position_effect=position_effect,
        limit_price=limit_price,
        max_risk=max_risk,
        collateral_required=collateral_required,
        shares_encumbered=shares_encumbered,
        evidence_ids=draft.evidence_ids,
        prompt_hash=prompt_hash,
        model_id=model_id,
        draft_hash=draft.draft_hash,
        horizon_trading_days=draft.horizon_trading_days,
        invalidation=draft.invalidation or (
            source_draft.invalidation if source_draft is not None else draft.thesis
        ),
    )
    return OptionValidationResult(True, (), packet)


authorize_option_draft = validate_option_draft
