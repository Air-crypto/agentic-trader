from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .options import OptionStructure, analyze_option_structure

INSTRUMENT_TYPES = {"stock", "etf", "leveraged_etf", "option"}


@dataclass(frozen=True)
class ProposalLeg:
    instrument_type: str
    symbol: str
    direction: str
    target_weight: float
    evidence_ids: tuple[str, ...]
    option_structure: OptionStructure | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProposalLeg:
        instrument_type = str(raw["instrument_type"]).lower()
        direction = str(raw["direction"]).lower()
        if instrument_type not in INSTRUMENT_TYPES:
            raise ValueError(f"Unsupported instrument type: {instrument_type}")
        if direction not in {"long", "short"}:
            raise ValueError("Direction must be long or short")
        target_weight = float(raw.get("target_weight", 0.0))
        if target_weight < 0:
            raise ValueError("Target weight cannot be negative")
        option_raw = raw.get("option_structure")
        option_structure = OptionStructure.from_dict(option_raw) if option_raw is not None else None
        if instrument_type == "option" and option_structure is None:
            raise ValueError("Option legs require an option_structure")
        if instrument_type != "option" and option_structure is not None:
            raise ValueError("Only option legs may include an option_structure")
        return cls(
            instrument_type=instrument_type,
            symbol=str(raw["symbol"]).upper(),
            direction=direction,
            target_weight=target_weight,
            evidence_ids=tuple(str(value) for value in raw.get("evidence_ids", [])),
            option_structure=option_structure,
        )


@dataclass(frozen=True)
class ResearchProposal:
    proposal_id: str
    created_at: datetime
    thesis: str
    horizon_days: int
    legs: tuple[ProposalLeg, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchProposal:
        created_at = datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("Proposal timestamp must include a timezone")
        horizon_days = int(raw["horizon_days"])
        if not 1 <= horizon_days <= 252:
            raise ValueError("Proposal horizon must be between 1 and 252 days")
        legs = tuple(ProposalLeg.from_dict(value) for value in raw["legs"])
        if not legs:
            raise ValueError("A proposal requires at least one leg")
        return cls(
            proposal_id=str(raw["proposal_id"]),
            created_at=created_at,
            thesis=str(raw["thesis"]),
            horizon_days=horizon_days,
            legs=legs,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> ResearchProposal:
        return cls.from_dict(json.loads(Path(path).read_text()))


def validate_proposal(
    proposal: ResearchProposal,
    analysis: pd.DataFrame,
    capital: float,
    known_evidence_ids: set[str] | None = None,
) -> dict[str, object]:
    """Fail closed on data quality and portfolio risk while preserving LLM freedom."""
    if capital <= 0:
        raise ValueError("Capital must be positive")
    analysis_by_symbol = analysis.set_index("symbol").to_dict(orient="index")
    reasons: list[str] = []
    leg_results: list[dict[str, object]] = []
    total_spot_weight = 0.0
    total_option_max_loss = 0.0

    if proposal.created_at > datetime.now(UTC):
        reasons.append("proposal_timestamp_is_in_the_future")
    if known_evidence_ids is None:
        reasons.append("evidence_registry_not_provided")

    for index, leg in enumerate(proposal.legs):
        leg_reasons: list[str] = []
        if not leg.evidence_ids:
            leg_reasons.append("missing_evidence")
        elif known_evidence_ids is not None:
            unknown = set(leg.evidence_ids) - known_evidence_ids
            if unknown:
                leg_reasons.append("unknown_evidence_ids")
        symbol_analysis = analysis_by_symbol.get(leg.symbol)
        if symbol_analysis is None or not bool(symbol_analysis.get("sufficient_history", False)):
            leg_reasons.append("insufficient_price_history")

        option_report: dict[str, object] | None = None
        if leg.instrument_type == "option":
            option_report = analyze_option_structure(leg.option_structure)
            if not bool(option_report["defined_risk"]):
                leg_reasons.append("undefined_option_loss")
            maximum_loss = option_report["maximum_loss"]
            if maximum_loss is None:
                leg_reasons.append("option_maximum_loss_unknown")
            else:
                maximum_loss = float(maximum_loss)
                total_option_max_loss += maximum_loss
                if maximum_loss > capital * 0.01:
                    leg_reasons.append("option_loss_exceeds_1_percent_of_capital")
        else:
            total_spot_weight += leg.target_weight
            if leg.target_weight > 0.25:
                leg_reasons.append("position_weight_exceeds_25_percent")
            if leg.instrument_type == "leveraged_etf" and leg.target_weight > 0.10:
                leg_reasons.append("leveraged_etf_weight_exceeds_10_percent")
            if leg.direction == "short":
                leg_reasons.append("unbounded_short_equity_loss")

        reasons.extend(f"leg_{index}:{reason}" for reason in leg_reasons)
        leg_results.append(
            {
                "symbol": leg.symbol,
                "instrument_type": leg.instrument_type,
                "accepted": not leg_reasons,
                "reasons": leg_reasons,
                "option_analysis": option_report,
            }
        )

    if total_spot_weight > 1.0 + 1e-9:
        reasons.append("total_spot_weight_exceeds_100_percent")
    if total_option_max_loss > capital * 0.05:
        reasons.append("aggregate_option_loss_exceeds_5_percent_of_capital")

    return {
        "proposal_id": proposal.proposal_id,
        "mode": "RESEARCH_ONLY_NO_ORDER",
        "accepted_for_shadow_research": not reasons,
        "reasons": reasons,
        "total_spot_weight": total_spot_weight,
        "total_option_max_loss": total_option_max_loss,
        "legs": leg_results,
        "note": ("Passing validates bounded risk and data availability, not expected return."),
    }
