"""Point-in-time learning and promotion controls for live trading research.

This module deliberately has no broker or database client.  It turns immutable
prediction batches and market closes into hash-bound forward outcomes, then
evaluates strategy arms in decision-date blocks.  Callers may persist the
objects with ``db/migrations/006_learning.sql``.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any

FORWARD_HORIZONS = (1, 3, 5, 20, 60)
EXPERIMENT_ARMS = ("factor_only", "llm_only", "hybrid", "do_nothing")
LEARNING_STATES = ("shadow", "canary", "live")
PREDICTION_ACTIONS = ("buy", "reject", "hold")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)


def _finite(value: float, name: str) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _positive(value: float, name: str) -> float:
    parsed = _finite(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_field(value: str, name: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return normalized


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class FrozenPrediction:
    """One arm's prediction for one member of the complete candidate universe."""

    prediction_id: str
    batch_id: str
    candidate_id: str
    symbol: str
    sector_benchmark: str
    arm: str
    action: str
    selected: bool
    score: float
    position_weight: float
    expected_turnover: float
    decision_date: date
    decision_at: datetime
    frozen_at: datetime
    data_cutoff_at: datetime
    model_id: str
    model_hash: str
    prompt_hash: str
    feature_hash: str
    data_snapshot_hash: str
    entry_price: float
    entry_spy_price: float
    entry_sector_price: float
    prediction_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("prediction_id", "batch_id", "candidate_id", "model_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "sector_benchmark", self.sector_benchmark.strip().upper())
        object.__setattr__(self, "arm", self.arm.strip().lower())
        object.__setattr__(self, "action", self.action.strip().lower())
        if not self.symbol or not self.sector_benchmark:
            raise ValueError("symbol and sector_benchmark are required")
        if self.arm not in EXPERIMENT_ARMS:
            raise ValueError(f"Unsupported experiment arm: {self.arm}")
        if self.action not in PREDICTION_ACTIONS:
            raise ValueError(f"Unsupported prediction action: {self.action}")

        decision_at = _utc(self.decision_at, "decision_at")
        frozen_at = _utc(self.frozen_at, "frozen_at")
        data_cutoff_at = _utc(self.data_cutoff_at, "data_cutoff_at")
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "frozen_at", frozen_at)
        object.__setattr__(self, "data_cutoff_at", data_cutoff_at)
        if self.decision_date != decision_at.date():
            raise ValueError("decision_date must match the UTC decision timestamp")
        if data_cutoff_at > frozen_at or frozen_at > decision_at:
            raise ValueError("Predictions must freeze only data available by decision time")

        score = _finite(self.score, "score")
        weight = _finite(self.position_weight, "position_weight")
        turnover = _finite(self.expected_turnover, "expected_turnover")
        if not -1.0 <= weight <= 1.0:
            raise ValueError("position_weight must be within [-1, 1]")
        if not 0.0 <= turnover <= 2.0:
            raise ValueError("expected_turnover must be within [0, 2]")
        if self.selected != (abs(weight) > 1e-12):
            raise ValueError("selected must exactly reflect a non-zero position weight")
        if not self.selected and turnover != 0:
            raise ValueError("Unselected candidates must have zero expected turnover")
        if self.action in {"reject", "hold"} and self.selected:
            raise ValueError("Rejected and hold candidates cannot carry a position")
        if self.action == "buy" and weight <= 0:
            raise ValueError("A buy prediction requires a positive position weight")
        if self.arm == "do_nothing" and (self.action != "hold" or self.selected or turnover != 0):
            raise ValueError("The do-nothing arm must hold zero exposure and zero turnover")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "position_weight", weight)
        object.__setattr__(self, "expected_turnover", turnover)
        object.__setattr__(self, "entry_price", _positive(self.entry_price, "entry_price"))
        object.__setattr__(
            self, "entry_spy_price", _positive(self.entry_spy_price, "entry_spy_price")
        )
        object.__setattr__(
            self,
            "entry_sector_price",
            _positive(self.entry_sector_price, "entry_sector_price"),
        )
        for name in ("model_hash", "prompt_hash", "feature_hash", "data_snapshot_hash"):
            object.__setattr__(self, name, _hash_field(getattr(self, name), name))

        calculated = _sha256(_canonical(self.unsigned_dict()))
        if self.prediction_hash and self.prediction_hash != calculated:
            raise ValueError("prediction_hash does not match frozen prediction content")
        object.__setattr__(self, "prediction_hash", calculated)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "batch_id": self.batch_id,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "sector_benchmark": self.sector_benchmark,
            "arm": self.arm,
            "action": self.action,
            "selected": self.selected,
            "score": self.score,
            "position_weight": self.position_weight,
            "expected_turnover": self.expected_turnover,
            "decision_date": self.decision_date.isoformat(),
            "decision_at": self.decision_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "data_cutoff_at": self.data_cutoff_at.isoformat(),
            "model_id": self.model_id,
            "model_hash": self.model_hash,
            "prompt_hash": self.prompt_hash,
            "feature_hash": self.feature_hash,
            "data_snapshot_hash": self.data_snapshot_hash,
            "entry_price": self.entry_price,
            "entry_spy_price": self.entry_spy_price,
            "entry_sector_price": self.entry_sector_price,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "prediction_hash": self.prediction_hash}


@dataclass(frozen=True)
class PredictionBatch:
    """A complete four-arm candidate universe frozen before any forward mark."""

    batch_id: str
    decision_date: date
    decision_at: datetime
    frozen_at: datetime
    expected_candidate_ids: tuple[str, ...]
    predictions: tuple[FrozenPrediction, ...]
    batch_hash: str = ""

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ValueError("batch_id is required")
        decision_at = _utc(self.decision_at, "batch decision_at")
        frozen_at = _utc(self.frozen_at, "batch frozen_at")
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "frozen_at", frozen_at)
        if self.decision_date != decision_at.date() or frozen_at > decision_at:
            raise ValueError("Batch must freeze by its UTC decision timestamp")
        candidates = tuple(sorted(str(item) for item in self.expected_candidate_ids))
        if (
            not candidates
            or len(set(candidates)) != len(candidates)
            or any(not item for item in candidates)
        ):
            raise ValueError("expected_candidate_ids must be unique and non-empty")
        object.__setattr__(self, "expected_candidate_ids", candidates)
        predictions = tuple(sorted(self.predictions, key=lambda item: item.prediction_id))
        object.__setattr__(self, "predictions", predictions)
        if len({item.prediction_id for item in predictions}) != len(predictions):
            raise ValueError("prediction_id must be globally unique within a batch")
        if len({item.prediction_hash for item in predictions}) != len(predictions):
            raise ValueError("prediction_hash must be globally unique within a batch")
        if any(
            item.batch_id != self.batch_id
            or item.decision_date != self.decision_date
            or item.decision_at != decision_at
            or item.frozen_at > frozen_at
            for item in predictions
        ):
            raise ValueError("Prediction batch identity or freeze time is inconsistent")

        expected = set(candidates)
        for arm in EXPERIMENT_ARMS:
            observed = {item.candidate_id for item in predictions if item.arm == arm}
            if observed != expected:
                raise ValueError(f"Arm {arm} does not contain the complete candidate universe")
        identities = {(item.candidate_id, item.arm) for item in predictions}
        if len(identities) != len(candidates) * len(EXPERIMENT_ARMS):
            raise ValueError("Every candidate must have exactly one prediction per arm")
        for candidate_id in candidates:
            market_identities = {
                (
                    item.symbol,
                    item.sector_benchmark,
                    item.entry_price,
                    item.entry_spy_price,
                    item.entry_sector_price,
                )
                for item in predictions
                if item.candidate_id == candidate_id
            }
            if len(market_identities) != 1:
                raise ValueError("A candidate must have the same market identity in every arm")
        for arm in EXPERIMENT_ARMS:
            gross_exposure = sum(
                abs(item.position_weight) for item in predictions if item.arm == arm
            )
            if gross_exposure > 1.0 + 1e-12:
                raise ValueError(f"Arm {arm} gross exposure exceeds 100%")

        calculated = _sha256(_canonical(self.unsigned_dict()))
        if self.batch_hash and self.batch_hash != calculated:
            raise ValueError("batch_hash does not match frozen batch content")
        object.__setattr__(self, "batch_hash", calculated)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "decision_date": self.decision_date.isoformat(),
            "decision_at": self.decision_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "expected_candidate_ids": list(self.expected_candidate_ids),
            "prediction_hashes": [item.prediction_hash for item in self.predictions],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.unsigned_dict(),
            "batch_hash": self.batch_hash,
            "predictions": [item.to_dict() for item in self.predictions],
        }


@dataclass(frozen=True)
class MarketClose:
    """A session close annotated with when every included price became usable."""

    session_date: date
    observed_at: datetime
    available_at: datetime
    prices: Mapping[str, float]

    def __post_init__(self) -> None:
        observed_at = _utc(self.observed_at, "observed_at")
        available_at = _utc(self.available_at, "available_at")
        if observed_at > available_at:
            raise ValueError("A market close cannot be available before it was observed")
        if self.session_date != observed_at.date():
            raise ValueError("session_date must match the UTC observed timestamp")
        normalized = {
            str(symbol).upper(): _positive(price, f"price:{symbol}")
            for symbol, price in self.prices.items()
        }
        if not normalized:
            raise ValueError("MarketClose requires at least one price")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "prices", normalized)


@dataclass(frozen=True)
class ForwardOutcome:
    outcome_id: str
    prediction_id: str
    prediction_hash: str
    batch_hash: str
    candidate_id: str
    arm: str
    decision_date: date
    horizon_sessions: int
    mark_session_date: date
    mark_observed_at: datetime
    mark_available_at: datetime
    recorded_at: datetime
    gross_return: float
    spy_relative_return: float
    sector_relative_return: float
    strategy_gross_return: float
    turnover: float
    cost_bps: float
    cost_return: float
    strategy_net_return: float
    outcome_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("outcome_id", "prediction_id", "candidate_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "arm", self.arm.strip().lower())
        if self.arm not in EXPERIMENT_ARMS:
            raise ValueError(f"Unsupported experiment arm: {self.arm}")
        if self.horizon_sessions not in FORWARD_HORIZONS:
            raise ValueError("Unsupported forward horizon")
        mark_at = _utc(self.mark_observed_at, "mark_observed_at")
        available_at = _utc(self.mark_available_at, "mark_available_at")
        recorded_at = _utc(self.recorded_at, "recorded_at")
        if self.decision_date >= self.mark_session_date:
            raise ValueError("A forward outcome must use a later market session")
        if self.mark_session_date != mark_at.date():
            raise ValueError("mark_session_date must match the UTC market timestamp")
        if not mark_at <= available_at <= recorded_at:
            raise ValueError("An outcome can use a mark only after it becomes available")
        object.__setattr__(self, "mark_observed_at", mark_at)
        object.__setattr__(self, "mark_available_at", available_at)
        object.__setattr__(self, "recorded_at", recorded_at)
        for name in (
            "gross_return",
            "spy_relative_return",
            "sector_relative_return",
            "strategy_gross_return",
            "turnover",
            "cost_bps",
            "cost_return",
            "strategy_net_return",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.turnover < 0 or self.cost_bps < 0 or self.cost_return < 0:
            raise ValueError("Turnover and trading costs cannot be negative")
        if self.gross_return < -1:
            raise ValueError("gross_return cannot lose more than the full entry value")
        expected_cost = self.turnover * self.cost_bps / 10_000.0
        if abs(self.cost_return - expected_cost) > 1e-12:
            raise ValueError("cost_return does not match turnover and cost_bps")
        if abs(self.strategy_net_return - (self.strategy_gross_return - self.cost_return)) > 1e-12:
            raise ValueError("strategy_net_return does not match gross return less costs")
        object.__setattr__(
            self,
            "prediction_hash",
            _hash_field(self.prediction_hash, "prediction_hash"),
        )
        object.__setattr__(self, "batch_hash", _hash_field(self.batch_hash, "batch_hash"))
        calculated = _sha256(_canonical(self.unsigned_dict()))
        if self.outcome_hash and self.outcome_hash != calculated:
            raise ValueError("outcome_hash does not match forward outcome content")
        object.__setattr__(self, "outcome_hash", calculated)

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("outcome_hash", None)
        payload["decision_date"] = self.decision_date.isoformat()
        payload["mark_session_date"] = self.mark_session_date.isoformat()
        payload["mark_observed_at"] = self.mark_observed_at.isoformat()
        payload["mark_available_at"] = self.mark_available_at.isoformat()
        payload["recorded_at"] = self.recorded_at.isoformat()
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "outcome_hash": self.outcome_hash}


def mark_available_outcomes(
    batch: PredictionBatch,
    closes: Sequence[MarketClose],
    as_of: datetime,
    *,
    cost_bps: float = 20.0,
) -> tuple[ForwardOutcome, ...]:
    """Mark only horizons whose Nth subsequent market session is already available."""
    as_of = _utc(as_of, "as_of")
    cost_bps = _finite(cost_bps, "cost_bps")
    if cost_bps < 0:
        raise ValueError("cost_bps cannot be negative")
    ordered = sorted(closes, key=lambda item: item.session_date)
    if len({item.session_date for item in ordered}) != len(ordered):
        raise ValueError("Market closes must contain at most one record per session")
    subsequent = [
        item
        for item in ordered
        if item.session_date > batch.decision_date and item.observed_at > batch.frozen_at
    ]
    outcomes: list[ForwardOutcome] = []
    for horizon in FORWARD_HORIZONS:
        if len(subsequent) < horizon:
            continue
        mark = subsequent[horizon - 1]
        if mark.available_at > as_of:
            continue
        for prediction in batch.predictions:
            required = (prediction.symbol, "SPY", prediction.sector_benchmark)
            if any(symbol not in mark.prices for symbol in required):
                continue
            gross = mark.prices[prediction.symbol] / prediction.entry_price - 1.0
            spy = mark.prices["SPY"] / prediction.entry_spy_price - 1.0
            sector = mark.prices[prediction.sector_benchmark] / prediction.entry_sector_price - 1.0
            turnover = prediction.expected_turnover if prediction.selected else 0.0
            cost_return = turnover * cost_bps / 10_000.0
            strategy_gross = prediction.position_weight * gross
            outcome_id = _sha256(
                f"{prediction.prediction_hash}|{horizon}|{mark.session_date.isoformat()}"
            )
            outcomes.append(
                ForwardOutcome(
                    outcome_id=outcome_id,
                    prediction_id=prediction.prediction_id,
                    prediction_hash=prediction.prediction_hash,
                    batch_hash=batch.batch_hash,
                    candidate_id=prediction.candidate_id,
                    arm=prediction.arm,
                    decision_date=prediction.decision_date,
                    horizon_sessions=horizon,
                    mark_session_date=mark.session_date,
                    mark_observed_at=mark.observed_at,
                    mark_available_at=mark.available_at,
                    recorded_at=as_of,
                    gross_return=gross,
                    spy_relative_return=gross - spy,
                    sector_relative_return=gross - sector,
                    strategy_gross_return=strategy_gross,
                    turnover=turnover,
                    cost_bps=cost_bps,
                    cost_return=cost_return,
                    strategy_net_return=strategy_gross - cost_return,
                )
            )
    return tuple(outcomes)


@dataclass(frozen=True)
class PromotionPolicy:
    horizon_sessions: int = 20
    minimum_candidates_per_arm: int = 300
    minimum_decision_dates: int = 30
    minimum_coverage: float = 0.95
    minimum_cost_bps: float = 20.0
    maximum_drawdown: float = 0.12
    maximum_turnover_per_block: float = 1.0
    minimum_hybrid_ic: float = 0.0
    minimum_hybrid_ic_lower_bound: float = 0.0
    minimum_hybrid_net_return: float = 0.0
    minimum_hybrid_net_lower_bound: float = 0.0
    minimum_paired_advantage: float = 0.0
    minimum_paired_advantage_lower_bound: float = 0.0
    confidence: float = 0.95
    bootstrap_samples: int = 2_000
    bootstrap_seed: int = 7
    minimum_shadow_dates: int = 60
    minimum_canary_dates: int = 20

    def __post_init__(self) -> None:
        integer_fields = (
            "horizon_sessions",
            "minimum_candidates_per_arm",
            "minimum_decision_dates",
            "bootstrap_samples",
            "bootstrap_seed",
            "minimum_shadow_dates",
            "minimum_canary_dates",
        )
        if any(type(getattr(self, name)) is not int for name in integer_fields):
            raise ValueError("Promotion count, horizon, and seed fields must be integers")
        if self.horizon_sessions not in FORWARD_HORIZONS:
            raise ValueError("Promotion horizon is unsupported")
        if self.minimum_candidates_per_arm <= 0 or self.minimum_decision_dates <= 1:
            raise ValueError("Promotion sample gates must be positive")
        if self.minimum_shadow_dates <= 0 or self.minimum_canary_dates <= 0:
            raise ValueError("Promotion stages require positive observation periods")
        numeric_fields = (
            "minimum_coverage",
            "minimum_cost_bps",
            "maximum_drawdown",
            "maximum_turnover_per_block",
            "minimum_hybrid_ic",
            "minimum_hybrid_ic_lower_bound",
            "minimum_hybrid_net_return",
            "minimum_hybrid_net_lower_bound",
            "minimum_paired_advantage",
            "minimum_paired_advantage_lower_bound",
            "confidence",
        )
        for name in numeric_fields:
            value = _finite(getattr(self, name), name)
            object.__setattr__(self, name, value)
        for name in ("minimum_coverage", "confidence"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be within (0, 1]")
        if self.bootstrap_samples < 100:
            raise ValueError("bootstrap_samples must be at least 100")
        if not 0 <= self.maximum_drawdown <= 1:
            raise ValueError("maximum_drawdown must be within [0, 1]")
        if self.maximum_turnover_per_block < 0 or self.minimum_cost_bps < 0:
            raise ValueError("Cost and turnover gates cannot be negative")
        if not -1 <= self.minimum_hybrid_ic <= 1 or not (
            -1 <= self.minimum_hybrid_ic_lower_bound <= 1
        ):
            raise ValueError("IC gates must be within [-1, 1]")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = rank
        index = end
    return result


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    x = _ranks(left)
    y = _ranks(right)
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    left_ss = sum((value - mean_x) ** 2 for value in x)
    right_ss = sum((value - mean_y) ** 2 for value in y)
    if left_ss == 0 or right_ss == 0:
        return None
    return numerator / (left_ss * right_ss) ** 0.5


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_mean(
    values: Sequence[float], policy: PromotionPolicy, seed_offset: int
) -> tuple[float, float] | None:
    if not values:
        return None
    generator = random.Random(policy.bootstrap_seed + seed_offset)
    estimates = []
    for _ in range(policy.bootstrap_samples):
        sample = [values[generator.randrange(len(values))] for _ in values]
        estimates.append(sum(sample) / len(sample))
    alpha = (1.0 - policy.confidence) / 2.0
    return _percentile(estimates, alpha), _percentile(estimates, 1.0 - alpha)


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= max(1.0 + value, 0.0)
        peak = max(peak, equity)
        maximum = max(maximum, 1.0 - equity / peak if peak else 1.0)
    return maximum


@dataclass(frozen=True)
class PromotionReport:
    generated_at: datetime
    horizon_sessions: int
    policy: Mapping[str, Any]
    arm_metrics: Mapping[str, Mapping[str, Any]]
    comparisons: Mapping[str, Mapping[str, Any]]
    gates: Mapping[str, bool]
    passed: bool
    report_hash: str = ""

    def __post_init__(self) -> None:
        generated_at = _utc(self.generated_at, "generated_at")
        object.__setattr__(self, "generated_at", generated_at)
        expected_policy_fields = set(PromotionPolicy.__dataclass_fields__)
        if set(self.policy) != expected_policy_fields:
            raise ValueError("Promotion report policy is incomplete or has unknown fields")
        try:
            parsed_policy = PromotionPolicy(**dict(self.policy))
        except (TypeError, ValueError) as error:
            raise ValueError("Promotion report policy is invalid") from error
        if self.horizon_sessions != parsed_policy.horizon_sessions:
            raise ValueError("Promotion report horizon does not match its policy")
        expected_arms = set(EXPERIMENT_ARMS)
        expected_comparisons = {
            "hybrid_vs_factor_only",
            "hybrid_vs_llm_only",
            "hybrid_vs_do_nothing",
        }
        expected_gates = {
            f"{arm}:{gate}"
            for arm in EXPERIMENT_ARMS
            for gate in ("sample", "dates", "coverage", "cost_assumption")
        } | {
            "hybrid:drawdown",
            "hybrid:turnover",
            "hybrid:ic",
            "hybrid:net_return",
            "hybrid_vs_factor_only:advantage",
            "hybrid_vs_llm_only:advantage",
            "hybrid_vs_do_nothing:advantage",
        }
        if set(self.arm_metrics) != expected_arms:
            raise ValueError("Promotion report must compare every experiment arm")
        if set(self.comparisons) != expected_comparisons:
            raise ValueError("Promotion report is missing a paired hybrid comparison")
        if set(self.gates) != expected_gates or any(
            type(value) is not bool for value in self.gates.values()
        ):
            raise ValueError("Promotion report gate set is incomplete or invalid")
        if type(self.passed) is not bool or self.passed != all(self.gates.values()):
            raise ValueError("Promotion report pass status must equal all required gates")
        calculated = _sha256(_canonical(self.unsigned_dict()))
        if self.report_hash and self.report_hash != calculated:
            raise ValueError("report_hash does not match report content")
        object.__setattr__(self, "report_hash", calculated)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "horizon_sessions": self.horizon_sessions,
            "policy": dict(self.policy),
            "arm_metrics": {key: dict(value) for key, value in self.arm_metrics.items()},
            "comparisons": {key: dict(value) for key, value in self.comparisons.items()},
            "gates": dict(self.gates),
            "passed": self.passed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "report_hash": self.report_hash}


def build_promotion_report(
    batches: Sequence[PredictionBatch],
    outcomes: Iterable[ForwardOutcome],
    as_of: datetime,
    policy: PromotionPolicy | None = None,
) -> PromotionReport:
    """Evaluate arms using decision dates as the independent bootstrap blocks."""
    policy = policy or PromotionPolicy()
    as_of = _utc(as_of, "as_of")
    if len({batch.batch_id for batch in batches}) != len(batches):
        raise ValueError("Evaluation batches must have unique batch IDs")
    if len({batch.batch_hash for batch in batches}) != len(batches):
        raise ValueError("Evaluation batches must have unique batch hashes")
    if len({batch.decision_date for batch in batches}) != len(batches):
        raise ValueError("Evaluation requires one complete batch per decision date")
    predictions = [item for batch in batches for item in batch.predictions]
    if not predictions or any(batch.frozen_at > as_of for batch in batches):
        raise ValueError("Promotion evaluation requires already-frozen prediction batches")
    prediction_by_id = {item.prediction_id: item for item in predictions}
    batch_hash_by_prediction = {
        prediction.prediction_id: batch.batch_hash
        for batch in batches
        for prediction in batch.predictions
    }
    if len(prediction_by_id) != len(predictions):
        raise ValueError("Prediction IDs must be unique across evaluation batches")
    selected_outcomes: dict[str, ForwardOutcome] = {}
    selected_outcome_ids: set[str] = set()
    selected_outcome_hashes: set[str] = set()
    market_marks: dict[tuple[str, str, int], tuple[Any, ...]] = {}
    for outcome in outcomes:
        if outcome.horizon_sessions != policy.horizon_sessions:
            continue
        prediction = prediction_by_id.get(outcome.prediction_id)
        if prediction is None:
            raise ValueError("Outcome references a prediction outside the evaluation set")
        if (
            outcome.prediction_hash != prediction.prediction_hash
            or outcome.batch_hash != batch_hash_by_prediction[prediction.prediction_id]
            or outcome.candidate_id != prediction.candidate_id
            or outcome.arm != prediction.arm
            or outcome.decision_date != prediction.decision_date
            or abs(
                outcome.strategy_gross_return - prediction.position_weight * outcome.gross_return
            )
            > 1e-12
            or abs(
                outcome.turnover - (prediction.expected_turnover if prediction.selected else 0.0)
            )
            > 1e-12
        ):
            raise ValueError("Outcome binding does not match its frozen prediction")
        if outcome.recorded_at > as_of:
            raise ValueError("Evaluation cannot use outcomes unavailable at as_of")
        if outcome.prediction_id in selected_outcomes:
            raise ValueError("Duplicate prediction/horizon outcome")
        if (
            outcome.outcome_id in selected_outcome_ids
            or outcome.outcome_hash in selected_outcome_hashes
        ):
            raise ValueError("Outcome IDs and hashes must be unique")
        mark_key = (outcome.batch_hash, outcome.candidate_id, outcome.horizon_sessions)
        market_signature = (
            outcome.mark_session_date,
            outcome.mark_observed_at,
            outcome.mark_available_at,
            outcome.gross_return,
            outcome.spy_relative_return,
            outcome.sector_relative_return,
        )
        if mark_key in market_marks and market_marks[mark_key] != market_signature:
            raise ValueError("Experiment arms must share the same candidate market outcome")
        market_marks[mark_key] = market_signature
        selected_outcomes[outcome.prediction_id] = outcome
        selected_outcome_ids.add(outcome.outcome_id)
        selected_outcome_hashes.add(outcome.outcome_hash)

    metrics: dict[str, dict[str, Any]] = {}
    blocks_by_arm: dict[str, dict[date, dict[str, float]]] = {}
    for arm_index, arm in enumerate(EXPERIMENT_ARMS):
        arm_predictions = [item for item in predictions if item.arm == arm]
        covered = [item for item in arm_predictions if item.prediction_id in selected_outcomes]
        by_date: dict[date, list[FrozenPrediction]] = {}
        for prediction in covered:
            by_date.setdefault(prediction.decision_date, []).append(prediction)
        blocks: dict[date, dict[str, float]] = {}
        daily_ics: list[float] = []
        for decision_date in sorted(by_date):
            items = by_date[decision_date]
            marked = [selected_outcomes[item.prediction_id] for item in items]
            ic = _correlation(
                [item.score for item in items],
                [item.sector_relative_return for item in marked],
            )
            if ic is not None:
                daily_ics.append(ic)
            blocks[decision_date] = {
                "net": sum(item.strategy_net_return for item in marked),
                "gross": sum(item.strategy_gross_return for item in marked),
                "turnover": sum(item.turnover for item in marked),
                "cost": sum(item.cost_return for item in marked),
            }
        blocks_by_arm[arm] = blocks
        net_values = [blocks[key]["net"] for key in sorted(blocks)]
        turnovers = [blocks[key]["turnover"] for key in sorted(blocks)]
        costs = [blocks[key]["cost"] for key in sorted(blocks)]
        net_ci = _bootstrap_mean(net_values, policy, arm_index * 17)
        ic_ci = _bootstrap_mean(daily_ics, policy, arm_index * 17 + 1)
        metrics[arm] = {
            "candidates": len(arm_predictions),
            "covered_candidates": len(covered),
            "coverage": len(covered) / len(arm_predictions) if arm_predictions else 0.0,
            "decision_dates": len(blocks),
            "selected_candidates": sum(item.selected for item in arm_predictions),
            "mean_net_return": sum(net_values) / len(net_values) if net_values else None,
            "net_return_ci": list(net_ci) if net_ci else None,
            "mean_turnover": sum(turnovers) / len(turnovers) if turnovers else None,
            "maximum_turnover": max(turnovers) if turnovers else None,
            "mean_cost_return": sum(costs) / len(costs) if costs else None,
            "max_drawdown": _max_drawdown(net_values) if net_values else None,
            "mean_rank_ic": sum(daily_ics) / len(daily_ics) if daily_ics else None,
            "rank_ic_dates": len(daily_ics),
            "rank_ic_ci": list(ic_ci) if ic_ci else None,
            "minimum_observed_cost_bps": min(
                (selected_outcomes[item.prediction_id].cost_bps for item in covered),
                default=None,
            ),
        }

    comparisons: dict[str, dict[str, Any]] = {}
    hybrid_blocks = blocks_by_arm["hybrid"]
    for offset, comparator in enumerate(("factor_only", "llm_only", "do_nothing"), start=1):
        other = blocks_by_arm[comparator]
        common_dates = sorted(set(hybrid_blocks) & set(other))
        differences = [hybrid_blocks[item]["net"] - other[item]["net"] for item in common_dates]
        interval = _bootstrap_mean(differences, policy, 100 + offset)
        comparisons[f"hybrid_vs_{comparator}"] = {
            "paired_decision_dates": len(common_dates),
            "mean_advantage": sum(differences) / len(differences) if differences else None,
            "advantage_ci": list(interval) if interval else None,
        }

    gates: dict[str, bool] = {}
    for arm in EXPERIMENT_ARMS:
        arm_metrics = metrics[arm]
        gates[f"{arm}:sample"] = arm_metrics["candidates"] >= policy.minimum_candidates_per_arm
        gates[f"{arm}:dates"] = arm_metrics["decision_dates"] >= policy.minimum_decision_dates
        gates[f"{arm}:coverage"] = arm_metrics["coverage"] >= policy.minimum_coverage
        observed_cost = arm_metrics["minimum_observed_cost_bps"]
        gates[f"{arm}:cost_assumption"] = (
            observed_cost is not None and observed_cost >= policy.minimum_cost_bps
        )
    hybrid = metrics["hybrid"]
    gates["hybrid:drawdown"] = (
        hybrid["max_drawdown"] is not None and hybrid["max_drawdown"] <= policy.maximum_drawdown
    )
    gates["hybrid:turnover"] = (
        hybrid["maximum_turnover"] is not None
        and hybrid["maximum_turnover"] <= policy.maximum_turnover_per_block
    )
    gates["hybrid:ic"] = (
        hybrid["mean_rank_ic"] is not None
        and hybrid["rank_ic_dates"] >= policy.minimum_decision_dates
        and hybrid["mean_rank_ic"] >= policy.minimum_hybrid_ic
        and hybrid["rank_ic_ci"] is not None
        and hybrid["rank_ic_ci"][0] >= policy.minimum_hybrid_ic_lower_bound
    )
    gates["hybrid:net_return"] = (
        hybrid["mean_net_return"] is not None
        and hybrid["mean_net_return"] >= policy.minimum_hybrid_net_return
        and hybrid["net_return_ci"] is not None
        and hybrid["net_return_ci"][0] > policy.minimum_hybrid_net_lower_bound
    )
    for name, comparison in comparisons.items():
        gates[f"{name}:advantage"] = (
            comparison["mean_advantage"] is not None
            and comparison["paired_decision_dates"] >= policy.minimum_decision_dates
            and comparison["mean_advantage"] >= policy.minimum_paired_advantage
            and comparison["advantage_ci"] is not None
            and comparison["advantage_ci"][0] > policy.minimum_paired_advantage_lower_bound
        )
    return PromotionReport(
        generated_at=as_of,
        horizon_sessions=policy.horizon_sessions,
        policy=policy.to_dict(),
        arm_metrics=metrics,
        comparisons=comparisons,
        gates=gates,
        passed=all(gates.values()),
    )


@dataclass(frozen=True)
class PromotionDecision:
    current_state: str
    requested_state: str
    resulting_state: str
    approved: bool
    reasons: tuple[str, ...]
    report_hash: str


def decide_promotion(
    current_state: str,
    requested_state: str,
    report: PromotionReport,
    *,
    observed_stage_dates: int,
) -> PromotionDecision:
    """Allow one-step promotion only after a passing, hash-valid report."""
    if type(observed_stage_dates) is not int or observed_stage_dates < 0:
        raise ValueError("observed_stage_dates must be a non-negative integer")
    current = current_state.lower()
    requested = requested_state.lower()
    if current not in LEARNING_STATES or requested not in LEARNING_STATES:
        raise ValueError("Unknown learning state")
    current_index = LEARNING_STATES.index(current)
    requested_index = LEARNING_STATES.index(requested)
    if requested_index <= current_index:
        return PromotionDecision(current, requested, requested, True, (), report.report_hash)
    reasons: list[str] = []
    if requested_index != current_index + 1:
        reasons.append("promotion_must_advance_exactly_one_state")
    try:
        report_valid = report.report_hash == _sha256(_canonical(report.unsigned_dict()))
    except (TypeError, ValueError):
        report_valid = False
    if not report.passed or not report_valid:
        reasons.append("promotion_report_failed_or_invalid")
    policy = report.policy
    minimum_date_key = "minimum_shadow_dates" if current == "shadow" else "minimum_canary_dates"
    minimum_dates = policy.get(minimum_date_key)
    if type(minimum_dates) is not int:
        if "promotion_report_failed_or_invalid" not in reasons:
            reasons.append("promotion_report_failed_or_invalid")
    elif observed_stage_dates < minimum_dates:
        reasons.append("insufficient_observation_dates_in_current_state")
    return PromotionDecision(
        current_state=current,
        requested_state=requested,
        resulting_state=current if reasons else requested,
        approved=not reasons,
        reasons=tuple(reasons),
        report_hash=report.report_hash,
    )
