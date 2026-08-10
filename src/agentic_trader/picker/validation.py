from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import (
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
    PickerDraft,
    QuantSnapshot,
    content_hash,
)

PACKET_NAMESPACE = uuid.UUID("f4b9ea21-2f66-4b0f-9c71-09ec8f331194")


@dataclass(frozen=True)
class LivePickerPolicy:
    max_horizon_days: int = 60
    min_price: float = 5.0
    min_market_cap: float = 2_000_000_000.0
    min_average_dollar_volume: float = 50_000_000.0
    max_spread_bps: float = 25.0
    min_event_quality: float = 0.65
    min_materiality: float = 0.20
    min_novelty: float = 0.40
    min_timing: float = 0.40
    max_speculation: float = 0.40
    max_stock_weight: float = 0.15
    risk_per_thesis: float = 0.01
    min_stop_loss_pct: float = 0.05
    max_stop_loss_pct: float = 0.12
    sector_relative_stop_pct: float = 0.05
    packet_ttl_minutes: int = 120
    max_quant_age_minutes: int = 180


@dataclass(frozen=True)
class PickerValidationResult:
    accepted: bool
    reasons: tuple[str, ...]
    packet: DecisionPacket | None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "packet": self.packet.to_dict() if self.packet is not None else None,
        }


def _packet_id(draft: PickerDraft, valid_for: str, action: str) -> str:
    key = f"{draft.run_id}|{draft.draft_id}|{draft.symbol}|{valid_for}|{action}"
    return str(uuid.uuid5(PACKET_NAMESPACE, key))


def validate_picker_draft(
    draft: PickerDraft,
    evidence_by_id: dict[str, EvidenceVersion],
    quant: QuantSnapshot | None,
    critic: CriticVerdict,
    prompt_hash: str,
    model_id: str,
    now: datetime | None = None,
    policy: LivePickerPolicy | None = None,
) -> PickerValidationResult:
    """Convert an untrusted LLM draft into a hash-bound live decision packet."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policy = policy or LivePickerPolicy()
    reasons: list[str] = []

    if draft.action == "reject":
        reasons.append("draft_abstained")
    if draft.created_at > now:
        reasons.append("draft_timestamp_in_future")
    if draft.horizon_trading_days > policy.max_horizon_days:
        reasons.append("horizon_exceeds_live_limit")
    if critic.draft_id != draft.draft_id:
        reasons.append("critic_draft_mismatch")
    if critic.created_at < draft.created_at:
        reasons.append("critic_predates_draft")
    if "grok" not in critic.model_id.lower() or critic.model_id == model_id:
        reasons.append("critic_model_not_independent")
    if critic.verdict != "pass":
        reasons.append("critic_veto")
    if critic.contradicted_evidence_ids:
        reasons.append("critic_found_contradicted_evidence")

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

    if draft.action != "reject":
        if not any(item.primary for item in evidence):
            reasons.append("no_primary_source")
        if len({item.independence_group for item in evidence}) < 2:
            reasons.append("fewer_than_two_independent_sources")
    if draft.event_quality < policy.min_event_quality:
        reasons.append("event_quality_below_gate")
    if draft.materiality < policy.min_materiality:
        reasons.append("materiality_below_gate")
    if draft.novelty < policy.min_novelty:
        reasons.append("novelty_below_gate")
    if draft.timing < policy.min_timing:
        reasons.append("timing_below_gate")
    if draft.speculation > policy.max_speculation:
        reasons.append("speculation_above_gate")

    if quant is None:
        reasons.append("missing_quant_snapshot")
    else:
        if quant.symbol != draft.symbol:
            reasons.append("quant_symbol_mismatch")
        if now - quant.as_of > timedelta(minutes=policy.max_quant_age_minutes):
            reasons.append("stale_quant_snapshot")
        if not quant.sufficient_history:
            reasons.append("insufficient_price_history")
        if not quant.fractional_tradable:
            reasons.append("not_fractional_tradable")
        if quant.last_price < policy.min_price:
            reasons.append("price_below_liquidity_gate")
        if quant.market_cap < policy.min_market_cap:
            reasons.append("market_cap_below_gate")
        if quant.average_dollar_volume < policy.min_average_dollar_volume:
            reasons.append("dollar_volume_below_gate")
        if quant.spread_bps > policy.max_spread_bps:
            reasons.append("spread_above_gate")

    if reasons:
        return PickerValidationResult(False, tuple(dict.fromkeys(reasons)), None)

    assert quant is not None
    baseline = (quant.momentum_rank + quant.quality_rank + quant.revisions_rank) / 3.0
    catalyst = (
        0.30 * draft.event_quality
        + 0.25 * draft.materiality
        + 0.20 * draft.novelty
        + 0.15 * draft.timing
        + 0.10 * (1.0 - draft.speculation)
    )
    rank_score = 0.5 * baseline + 0.5 * catalyst
    stop_loss_pct = min(
        policy.max_stop_loss_pct,
        max(policy.min_stop_loss_pct, 2.0 * quant.atr_pct),
    )
    target_weight = min(policy.max_stock_weight, policy.risk_per_thesis / stop_loss_pct)
    action = "close" if draft.action == "close" else "buy"
    if action == "close":
        target_weight = 0.0

    packet = DecisionPacket(
        packet_id=_packet_id(draft, now.date().isoformat(), action),
        run_id=draft.run_id,
        draft_id=draft.draft_id,
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(minutes=policy.packet_ttl_minutes),
        symbol=draft.symbol,
        action=action,
        horizon_trading_days=draft.horizon_trading_days,
        target_weight=target_weight,
        stop_loss_pct=stop_loss_pct,
        sector_relative_stop_pct=policy.sector_relative_stop_pct,
        sector=quant.sector,
        rank_score=rank_score,
        thesis_hash=content_hash(draft.thesis),
        evidence_ids=draft.evidence_ids,
        prompt_hash=prompt_hash,
        model_id=model_id,
    ).with_hash()
    return PickerValidationResult(True, (), packet)
