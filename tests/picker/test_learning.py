from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from agentic_trader.picker.learning import (
    EXPERIMENT_ARMS,
    FORWARD_HORIZONS,
    ForwardOutcome,
    FrozenPrediction,
    MarketClose,
    PredictionBatch,
    PromotionPolicy,
    build_promotion_report,
    decide_promotion,
    mark_available_outcomes,
)
from agentic_trader.picker.learning_store import (
    build_shadow_batch,
    market_close_from_dict,
    prediction_batch_from_dict,
)
from agentic_trader.picker.models import QuantSnapshot

SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
RETURNS = (0.04, 0.02, -0.02, -0.04)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _timestamp(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _batch(day: date, batch_number: int = 0) -> PredictionBatch:
    batch_id = f"batch-{batch_number}"
    decision_at = _timestamp(day, 20)
    frozen_at = decision_at - timedelta(minutes=1)
    candidates = tuple(f"candidate-{batch_number}-{index}" for index in range(len(SYMBOLS)))
    chosen = {
        "factor_only": {3: 1.0},
        "llm_only": {1: 1.0},
        "hybrid": {0: 0.5, 1: 0.5},
        "do_nothing": {},
    }
    predictions: list[FrozenPrediction] = []
    for arm in EXPERIMENT_ARMS:
        for index, (candidate_id, symbol) in enumerate(zip(candidates, SYMBOLS, strict=True)):
            weight = chosen[arm].get(index, 0.0)
            selected = weight > 0
            predictions.append(
                FrozenPrediction(
                    prediction_id=f"{batch_id}:{arm}:{candidate_id}",
                    batch_id=batch_id,
                    candidate_id=candidate_id,
                    symbol=symbol,
                    sector_benchmark="XLK",
                    arm=arm,
                    action=("hold" if arm == "do_nothing" else "buy" if selected else "reject"),
                    selected=selected,
                    score=float(len(SYMBOLS) - index),
                    position_weight=weight,
                    expected_turnover=weight,
                    decision_date=day,
                    decision_at=decision_at,
                    frozen_at=frozen_at,
                    data_cutoff_at=frozen_at - timedelta(minutes=1),
                    model_id=f"{arm}-v1",
                    model_hash=_digest(f"model:{arm}"),
                    prompt_hash=_digest(f"prompt:{arm}"),
                    feature_hash=_digest("features:v1"),
                    data_snapshot_hash=_digest(f"snapshot:{batch_id}"),
                    entry_price=100.0,
                    entry_spy_price=100.0,
                    entry_sector_price=100.0,
                )
            )
    return PredictionBatch(
        batch_id=batch_id,
        decision_date=day,
        decision_at=decision_at,
        frozen_at=frozen_at,
        expected_candidate_ids=candidates,
        predictions=tuple(predictions),
    )


def _close(day: date, *, available_delay: timedelta = timedelta(minutes=5)) -> MarketClose:
    observed_at = _timestamp(day, 21)
    return MarketClose(
        session_date=day,
        observed_at=observed_at,
        available_at=observed_at + available_delay,
        prices={
            **{
                symbol: 100.0 * (1.0 + forward_return)
                for symbol, forward_return in zip(SYMBOLS, RETURNS, strict=True)
            },
            "SPY": 101.0,
            "XLK": 102.0,
        },
    )


def _evaluation_sample() -> tuple[list[PredictionBatch], tuple[ForwardOutcome, ...], datetime]:
    batches = []
    outcomes: list[ForwardOutcome] = []
    base = date(2026, 6, 1)
    for index in range(6):
        decision_day = base + timedelta(days=7 * index)
        batch = _batch(decision_day, index)
        close = _close(decision_day + timedelta(days=1))
        as_of = close.available_at + timedelta(minutes=1)
        batches.append(batch)
        outcomes.extend(mark_available_outcomes(batch, [close], as_of))
    return batches, tuple(outcomes), as_of


def _passing_policy() -> PromotionPolicy:
    return PromotionPolicy(
        horizon_sessions=1,
        minimum_candidates_per_arm=20,
        minimum_decision_dates=5,
        minimum_coverage=1.0,
        minimum_cost_bps=20.0,
        maximum_drawdown=0.05,
        maximum_turnover_per_block=1.0,
        minimum_hybrid_ic=0.9,
        minimum_hybrid_ic_lower_bound=0.9,
        minimum_hybrid_net_return=0.02,
        minimum_hybrid_net_lower_bound=0.02,
        minimum_paired_advantage=0.005,
        minimum_paired_advantage_lower_bound=0.005,
        confidence=0.9,
        bootstrap_samples=100,
        bootstrap_seed=19,
        minimum_shadow_dates=5,
        minimum_canary_dates=3,
    )


def test_batch_requires_every_candidate_in_every_arm() -> None:
    complete = _batch(date(2026, 6, 1))
    missing_reject = tuple(
        prediction
        for prediction in complete.predictions
        if not (
            prediction.arm == "hybrid"
            and prediction.candidate_id == complete.expected_candidate_ids[-1]
        )
    )

    with pytest.raises(ValueError, match="complete candidate universe"):
        replace(complete, predictions=missing_reject, batch_hash="")

    altered = list(complete.predictions)
    target = next(
        index
        for index, prediction in enumerate(altered)
        if prediction.arm == "hybrid"
        and prediction.candidate_id == complete.expected_candidate_ids[0]
    )
    altered[target] = replace(altered[target], symbol="ZZZ", prediction_hash="")
    with pytest.raises(ValueError, match="same market identity"):
        replace(complete, predictions=tuple(altered), batch_hash="")


def test_learning_json_adapters_preserve_hash_bound_models() -> None:
    batch = _batch(date(2026, 6, 1))
    close = _close(date(2026, 6, 2))

    assert prediction_batch_from_dict(batch.to_dict()) == batch
    assert (
        market_close_from_dict(
            {
                "session_date": close.session_date.isoformat(),
                "observed_at": close.observed_at.isoformat(),
                "available_at": close.available_at.isoformat(),
                "prices": dict(close.prices),
            }
        )
        == close
    )


def test_shadow_batch_builder_creates_complete_bounded_arms() -> None:
    as_of = _timestamp(date(2026, 6, 1), 14)
    snapshots = [
        QuantSnapshot.from_dict(
            {
                "symbol": symbol,
                "as_of": as_of.isoformat(),
                "last_price": 100 + index,
                "market_cap": 5_000_000_000,
                "average_dollar_volume": 100_000_000,
                "spread_bps": 5,
                "sector": "Technology",
                "fractional_tradable": True,
                "sufficient_history": True,
                "momentum_rank": (index + 1) / len(SYMBOLS),
                "quality_rank": 0.5,
                "revisions_rank": 0.5,
                "volatility_63d": 0.2,
                "beta_252d": 1.0,
                "atr_pct": 0.03,
                "data_snapshot_hash": _digest(f"snapshot:{symbol}"),
                "feature_version": "picker_features_v1",
                "calculated_by": "agentic_trader.picker.features",
            }
        )
        for index, symbol in enumerate(SYMBOLS)
    ]
    research = {
        "model_id": "shadow-model-v1",
        "prompt_version": "morning-shadow-v1",
        "benchmark_prices": {"SPY": 500, "XLK": 250},
        "candidates": [
            {
                "symbol": symbol,
                "action": "buy" if index < 2 else "reject",
                "catalyst_score": 1 - index / len(SYMBOLS),
                "sector_benchmark": "XLK",
            }
            for index, symbol in enumerate(SYMBOLS)
        ],
    }

    batch = build_shadow_batch(snapshots, research, _timestamp(date(2026, 6, 1), 15))

    assert len(batch.predictions) == len(SYMBOLS) * len(EXPERIMENT_ARMS)
    assert {
        item.symbol for item in batch.predictions if item.arm == "llm_only" and item.selected
    } == {"AAA", "BBB"}
    assert (
        sum(item.position_weight for item in batch.predictions if item.arm == "factor_only") <= 0.9
    )
    with pytest.raises(ValueError, match="timezone"):
        build_shadow_batch(snapshots, research, datetime(2026, 6, 1, 15))


def test_prediction_freeze_and_hashes_reject_future_or_unversioned_data() -> None:
    prediction = _batch(date(2026, 6, 1)).predictions[0]

    with pytest.raises(ValueError, match="available by decision time"):
        replace(
            prediction,
            data_cutoff_at=prediction.frozen_at + timedelta(seconds=1),
            prediction_hash="",
        )
    with pytest.raises(ValueError, match="model_hash must be a SHA-256"):
        replace(prediction, model_hash="not-a-hash", prediction_hash="")
    with pytest.raises(ValueError, match="prediction_hash does not match"):
        replace(prediction, prediction_hash="0" * 64)


def test_marks_all_candidates_and_rejects_at_exact_forward_sessions() -> None:
    batch = _batch(date(2026, 6, 1))
    closes = [
        _close(batch.decision_date + timedelta(days=session))
        for session in range(1, max(FORWARD_HORIZONS) + 1)
    ]
    as_of = closes[-1].available_at + timedelta(minutes=1)

    outcomes = mark_available_outcomes(batch, closes, as_of, cost_bps=20.0)

    assert len(outcomes) == len(batch.predictions) * len(FORWARD_HORIZONS)
    assert {outcome.horizon_sessions for outcome in outcomes} == set(FORWARD_HORIZONS)
    assert all(outcome.mark_available_at <= outcome.recorded_at for outcome in outcomes)
    reject_prediction = next(
        prediction
        for prediction in batch.predictions
        if prediction.arm == "hybrid" and prediction.action == "reject"
    )
    rejected_outcome = next(
        outcome
        for outcome in outcomes
        if outcome.prediction_id == reject_prediction.prediction_id
        and outcome.horizon_sessions == 1
    )
    assert rejected_outcome.gross_return == pytest.approx(-0.02)
    assert rejected_outcome.spy_relative_return == pytest.approx(-0.03)
    assert rejected_outcome.sector_relative_return == pytest.approx(-0.04)
    assert rejected_outcome.strategy_net_return == 0.0


def test_unavailable_or_incomplete_target_session_is_not_shifted_forward() -> None:
    batch = _batch(date(2026, 6, 1))
    first = _close(batch.decision_date + timedelta(days=1), available_delay=timedelta(days=5))
    second = _close(batch.decision_date + timedelta(days=2))
    as_of = second.available_at + timedelta(minutes=1)

    assert mark_available_outcomes(batch, [first, second], as_of) == ()

    incomplete = MarketClose(
        session_date=first.session_date,
        observed_at=first.observed_at,
        available_at=first.observed_at + timedelta(minutes=5),
        prices={key: value for key, value in first.prices.items() if key != "CCC"},
    )
    outcomes = mark_available_outcomes(batch, [incomplete, second], as_of)
    missing_candidate = {
        prediction.prediction_id for prediction in batch.predictions if prediction.symbol == "CCC"
    }
    assert {outcome.horizon_sessions for outcome in outcomes} == {1}
    assert not missing_candidate & {outcome.prediction_id for outcome in outcomes}


def test_forward_outcome_rejects_unavailable_marks_and_inconsistent_costs() -> None:
    batch = _batch(date(2026, 6, 1))
    close = _close(batch.decision_date + timedelta(days=1))
    outcome = mark_available_outcomes(
        batch,
        [close],
        close.available_at + timedelta(minutes=1),
    )[0]

    with pytest.raises(ValueError, match="after it becomes available"):
        replace(
            outcome,
            recorded_at=outcome.mark_available_at - timedelta(seconds=1),
            outcome_hash="",
        )
    with pytest.raises(ValueError, match="cost_return does not match"):
        replace(outcome, cost_return=outcome.cost_return + 0.01, outcome_hash="")


def test_promotion_report_is_deterministic_and_uses_date_blocks() -> None:
    batches, outcomes, as_of = _evaluation_sample()
    policy = _passing_policy()

    first = build_promotion_report(batches, outcomes, as_of, policy)
    second = build_promotion_report(batches, reversed(outcomes), as_of, policy)

    assert first.report_hash == second.report_hash
    assert first.passed
    assert first.arm_metrics["hybrid"]["candidates"] == 24
    assert first.arm_metrics["hybrid"]["covered_candidates"] == 24
    assert first.arm_metrics["hybrid"]["decision_dates"] == 6
    assert first.arm_metrics["hybrid"]["rank_ic_dates"] == 6
    assert first.arm_metrics["hybrid"]["mean_rank_ic"] == pytest.approx(1.0)
    assert first.arm_metrics["hybrid"]["mean_net_return"] == pytest.approx(0.028)
    assert first.arm_metrics["hybrid"]["maximum_turnover"] == pytest.approx(1.0)
    assert first.comparisons["hybrid_vs_do_nothing"]["paired_decision_dates"] == 6
    assert first.comparisons["hybrid_vs_do_nothing"]["mean_advantage"] == pytest.approx(0.028)


def test_missing_coverage_or_tampered_binding_fails_closed() -> None:
    batches, outcomes, as_of = _evaluation_sample()
    policy = _passing_policy()
    incomplete = outcomes[1:]

    report = build_promotion_report(batches, incomplete, as_of, policy)

    assert not report.passed
    assert not report.gates[f"{outcomes[0].arm}:coverage"]

    tampered = replace(outcomes[0], candidate_id="different", outcome_hash="")
    with pytest.raises(ValueError, match="binding does not match"):
        build_promotion_report(batches, (tampered, *outcomes[1:]), as_of, policy)

    inconsistent_mark = replace(
        outcomes[0],
        gross_return=outcomes[0].gross_return + 0.01,
        spy_relative_return=outcomes[0].spy_relative_return + 0.01,
        sector_relative_return=outcomes[0].sector_relative_return + 0.01,
        outcome_hash="",
    )
    with pytest.raises(ValueError, match="same candidate market outcome"):
        build_promotion_report(
            batches,
            (inconsistent_mark, *outcomes[1:]),
            as_of,
            policy,
        )

    future = replace(
        outcomes[0],
        recorded_at=as_of + timedelta(seconds=1),
        outcome_hash="",
    )
    with pytest.raises(ValueError, match="unavailable at as_of"):
        build_promotion_report(batches, (future, *outcomes[1:]), as_of, policy)


def test_shadow_canary_live_promotions_advance_one_gated_stage() -> None:
    batches, outcomes, as_of = _evaluation_sample()
    report = build_promotion_report(batches, outcomes, as_of, _passing_policy())

    shadow_to_canary = decide_promotion("shadow", "canary", report, observed_stage_dates=5)
    assert shadow_to_canary.approved
    assert shadow_to_canary.resulting_state == "canary"

    jump = decide_promotion("shadow", "live", report, observed_stage_dates=100)
    assert not jump.approved
    assert jump.resulting_state == "shadow"
    assert "promotion_must_advance_exactly_one_state" in jump.reasons

    too_soon = decide_promotion("canary", "live", report, observed_stage_dates=2)
    assert not too_soon.approved
    assert too_soon.resulting_state == "canary"

    failed_report = build_promotion_report(batches, outcomes[1:], as_of, _passing_policy())
    failed = decide_promotion("shadow", "canary", failed_report, observed_stage_dates=100)
    assert not failed.approved
    assert failed.resulting_state == "shadow"


def test_learning_migration_declares_append_only_and_point_in_time_guards() -> None:
    migration = Path("db/migrations/006_learning.sql").read_text(encoding="utf-8")

    assert "BEFORE UPDATE OR DELETE" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "data_cutoff_at <= frozen_at AND frozen_at <= decision_at" in migration
    assert "model_hash" in migration
    assert "mark_available_at <= recorded_at" in migration
    assert "learning_prediction_preserves_complete_batch" in migration
    assert "learning_promotion_fails_closed" in migration
