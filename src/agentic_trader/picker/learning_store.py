"""Durable persistence and strict JSON adapters for point-in-time learning."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from typing import Any

from .learning import (
    ForwardOutcome,
    FrozenPrediction,
    MarketClose,
    PredictionBatch,
    PromotionReport,
)
from .ledger import DATABASE_URL_ENV
from .models import QuantSnapshot


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _datetime(value: Any) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )


def prediction_from_dict(raw: dict[str, Any]) -> FrozenPrediction:
    return FrozenPrediction(
        prediction_id=str(raw["prediction_id"]),
        batch_id=str(raw["batch_id"]),
        candidate_id=str(raw["candidate_id"]),
        symbol=str(raw["symbol"]),
        sector_benchmark=str(raw["sector_benchmark"]),
        arm=str(raw["arm"]),
        action=str(raw["action"]),
        selected=raw["selected"],
        score=float(raw["score"]),
        position_weight=float(raw["position_weight"]),
        expected_turnover=float(raw["expected_turnover"]),
        decision_date=_date(raw["decision_date"]),
        decision_at=_datetime(raw["decision_at"]),
        frozen_at=_datetime(raw["frozen_at"]),
        data_cutoff_at=_datetime(raw["data_cutoff_at"]),
        model_id=str(raw["model_id"]),
        model_hash=str(raw["model_hash"]),
        prompt_hash=str(raw["prompt_hash"]),
        feature_hash=str(raw["feature_hash"]),
        data_snapshot_hash=str(raw["data_snapshot_hash"]),
        entry_price=float(raw["entry_price"]),
        entry_spy_price=float(raw["entry_spy_price"]),
        entry_sector_price=float(raw["entry_sector_price"]),
        prediction_hash=str(raw.get("prediction_hash", "")),
    )


def prediction_batch_from_dict(raw: dict[str, Any]) -> PredictionBatch:
    predictions = tuple(prediction_from_dict(item) for item in raw["predictions"])
    candidate_ids = raw.get("expected_candidate_ids")
    if candidate_ids is None:
        candidate_ids = sorted({item.candidate_id for item in predictions})
    return PredictionBatch(
        batch_id=str(raw["batch_id"]),
        decision_date=_date(raw["decision_date"]),
        decision_at=_datetime(raw["decision_at"]),
        frozen_at=_datetime(raw["frozen_at"]),
        expected_candidate_ids=tuple(str(item) for item in candidate_ids),
        predictions=predictions,
        batch_hash=str(raw.get("batch_hash", "")),
    )


def market_close_from_dict(raw: dict[str, Any]) -> MarketClose:
    return MarketClose(
        session_date=_date(raw["session_date"]),
        observed_at=_datetime(raw["observed_at"]),
        available_at=_datetime(raw["available_at"]),
        prices={str(key): float(value) for key, value in raw["prices"].items()},
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def build_shadow_batch(
    snapshots: list[QuantSnapshot],
    research: dict[str, Any],
    decision_at: datetime | None = None,
) -> PredictionBatch:
    """Build all experiment arms in code from quant and bounded catalyst inputs."""
    decision_at = decision_at or datetime.now(UTC)
    if decision_at.tzinfo is None:
        raise ValueError("Learning decision_at must include a timezone")
    decision_at = decision_at.astimezone(UTC)
    if not snapshots or len({item.symbol for item in snapshots}) != len(snapshots):
        raise ValueError("Learning quant snapshots must be non-empty and symbol-unique")
    research_rows = research.get("candidates")
    benchmark_prices = research.get("benchmark_prices")
    if not isinstance(research_rows, list) or not isinstance(benchmark_prices, dict):
        raise ValueError("Learning research requires candidates and benchmark_prices")
    by_symbol = {str(item.get("symbol", "")).upper(): item for item in research_rows}
    quant_by_symbol = {item.symbol: item for item in snapshots}
    if set(by_symbol) != set(quant_by_symbol):
        raise ValueError("Learning research must cover the complete quant candidate universe")
    if any(item.as_of > decision_at for item in snapshots):
        raise ValueError("Learning quant data cannot postdate the decision")
    model_id = str(research.get("model_id", "")).strip()
    prompt_version = str(research.get("prompt_version", "")).strip()
    if not model_id or not prompt_version:
        raise ValueError("Learning research requires model_id and prompt_version")

    candidate_data: dict[str, dict[str, Any]] = {}
    for symbol, snapshot in quant_by_symbol.items():
        raw = by_symbol[symbol]
        action = str(raw.get("action", "")).lower()
        catalyst_score = float(raw.get("catalyst_score", float("nan")))
        sector_benchmark = str(raw.get("sector_benchmark", "")).upper()
        if action not in {"buy", "reject"}:
            raise ValueError(f"Learning action for {symbol} must be buy or reject")
        if not 0 <= catalyst_score <= 1:
            raise ValueError(f"Learning catalyst score for {symbol} must be within [0, 1]")
        if sector_benchmark not in benchmark_prices or "SPY" not in benchmark_prices:
            raise ValueError(f"Learning benchmark prices are incomplete for {symbol}")
        factor_score = (
            snapshot.momentum_rank + snapshot.quality_rank + snapshot.revisions_rank
        ) / 3.0
        candidate_data[symbol] = {
            "snapshot": snapshot,
            "research_action": action,
            "catalyst_score": catalyst_score,
            "factor_score": factor_score,
            "hybrid_score": (factor_score + catalyst_score) / 2.0,
            "sector_benchmark": sector_benchmark,
        }

    def chosen(score_name: str, eligible: set[str]) -> set[str]:
        ranked = sorted(
            eligible,
            key=lambda symbol: (-candidate_data[symbol][score_name], symbol),
        )
        return set(ranked[:6])

    eligible = {
        symbol for symbol, item in candidate_data.items() if item["research_action"] == "buy"
    }
    selections = {
        "factor_only": chosen("factor_score", set(candidate_data)),
        "llm_only": chosen("catalyst_score", eligible),
        "hybrid": chosen("hybrid_score", eligible),
        "do_nothing": set(),
    }
    score_names = {
        "factor_only": "factor_score",
        "llm_only": "catalyst_score",
        "hybrid": "hybrid_score",
        "do_nothing": "factor_score",
    }
    frozen_at = decision_at
    decision_day = decision_at.date()
    data_cutoff_at = max(item.as_of for item in snapshots)
    model_hash = _digest({"model_id": model_id})
    prompt_hash = _digest({"prompt_version": prompt_version})
    feature_hash = _digest(
        sorted({(item.feature_version, item.calculated_by) for item in snapshots})
    )
    data_snapshot_hash = _digest(
        [quant_by_symbol[symbol].to_dict() for symbol in sorted(quant_by_symbol)]
    )
    batch_id = _digest(
        {
            "decision_at": decision_at.isoformat(),
            "data_snapshot_hash": data_snapshot_hash,
            "model_hash": model_hash,
            "prompt_hash": prompt_hash,
        }
    )
    predictions: list[FrozenPrediction] = []
    for arm, selected_symbols in selections.items():
        weight = min(0.15, 0.9 / len(selected_symbols)) if selected_symbols else 0.0
        for symbol in sorted(candidate_data):
            item = candidate_data[symbol]
            selected = symbol in selected_symbols
            candidate_id = f"{decision_day.isoformat()}:{symbol}"
            prediction_id = _digest(
                {"batch_id": batch_id, "candidate_id": candidate_id, "arm": arm}
            )
            predictions.append(
                FrozenPrediction(
                    prediction_id=prediction_id,
                    batch_id=batch_id,
                    candidate_id=candidate_id,
                    symbol=symbol,
                    sector_benchmark=item["sector_benchmark"],
                    arm=arm,
                    action=("hold" if arm == "do_nothing" else "buy" if selected else "reject"),
                    selected=selected,
                    score=float(item[score_names[arm]]) if arm != "do_nothing" else 0.0,
                    position_weight=weight if selected else 0.0,
                    expected_turnover=weight if selected else 0.0,
                    decision_date=decision_day,
                    decision_at=decision_at,
                    frozen_at=frozen_at,
                    data_cutoff_at=data_cutoff_at,
                    model_id=model_id if arm != "factor_only" else "deterministic-factor-v1",
                    model_hash=(
                        model_hash
                        if arm != "factor_only"
                        else _digest({"model_id": "deterministic-factor-v1"})
                    ),
                    prompt_hash=prompt_hash,
                    feature_hash=feature_hash,
                    data_snapshot_hash=data_snapshot_hash,
                    entry_price=item["snapshot"].last_price,
                    entry_spy_price=float(benchmark_prices["SPY"]),
                    entry_sector_price=float(benchmark_prices[item["sector_benchmark"]]),
                )
            )
    candidate_ids = tuple(
        f"{decision_day.isoformat()}:{symbol}" for symbol in sorted(candidate_data)
    )
    return PredictionBatch(
        batch_id=batch_id,
        decision_date=decision_day,
        decision_at=decision_at,
        frozen_at=frozen_at,
        expected_candidate_ids=candidate_ids,
        predictions=tuple(predictions),
    )


class PostgresLearningStore:
    """Append-only store whose database triggers enforce point-in-time identity."""

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("A Postgres database URL is required")
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> PostgresLearningStore:
        return cls(os.environ.get(DATABASE_URL_ENV, ""))

    def _connect(self):
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install psycopg[binary] to use learning persistence") from error
        return psycopg.connect(self.database_url)

    def record_batch(self, batch: PredictionBatch) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            cursor.execute(
                """
                INSERT INTO learning_prediction_batches
                    (batch_id, decision_date, decision_at, frozen_at,
                     expected_candidate_count, batch_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO NOTHING
                """,
                (
                    batch.batch_id,
                    batch.decision_date,
                    batch.decision_at,
                    batch.frozen_at,
                    len(batch.expected_candidate_ids),
                    batch.batch_hash,
                ),
            )
            cursor.execute(
                "SELECT batch_hash FROM learning_prediction_batches WHERE batch_id = %s",
                (batch.batch_id,),
            )
            row = cursor.fetchone()
            if row is None or str(row[0]) != batch.batch_hash:
                raise ValueError(f"Learning batch {batch.batch_id} is immutable")
            cursor.executemany(
                """
                INSERT INTO learning_predictions
                    (prediction_id, batch_id, candidate_id, symbol,
                     sector_benchmark, arm, action, selected, score,
                     position_weight, expected_turnover, decision_date,
                     decision_at, frozen_at, data_cutoff_at, model_id,
                     model_hash, prompt_hash, feature_hash, data_snapshot_hash,
                     entry_price, entry_spy_price, entry_sector_price,
                     prediction_hash)
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (prediction_id) DO NOTHING
                """,
                [
                    (
                        item.prediction_id,
                        item.batch_id,
                        item.candidate_id,
                        item.symbol,
                        item.sector_benchmark,
                        item.arm,
                        item.action,
                        item.selected,
                        item.score,
                        item.position_weight,
                        item.expected_turnover,
                        item.decision_date,
                        item.decision_at,
                        item.frozen_at,
                        item.data_cutoff_at,
                        item.model_id,
                        item.model_hash,
                        item.prompt_hash,
                        item.feature_hash,
                        item.data_snapshot_hash,
                        item.entry_price,
                        item.entry_spy_price,
                        item.entry_sector_price,
                        item.prediction_hash,
                    )
                    for item in batch.predictions
                ],
            )
            cursor.execute(
                """
                SELECT prediction_id, prediction_hash
                FROM learning_predictions WHERE batch_id = %s
                """,
                (batch.batch_id,),
            )
            stored = {str(item[0]): str(item[1]) for item in cursor.fetchall()}
            expected = {item.prediction_id: item.prediction_hash for item in batch.predictions}
            if stored != expected:
                raise ValueError(f"Learning predictions for {batch.batch_id} are immutable")

    def load_batches(self) -> list[PredictionBatch]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, decision_date, decision_at, frozen_at, batch_hash
                FROM learning_prediction_batches ORDER BY decision_date, batch_id
                """
            )
            batch_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT prediction_id, batch_id, candidate_id, symbol,
                       sector_benchmark, arm, action, selected, score,
                       position_weight, expected_turnover, decision_date,
                       decision_at, frozen_at, data_cutoff_at, model_id,
                       model_hash, prompt_hash, feature_hash, data_snapshot_hash,
                       entry_price, entry_spy_price, entry_sector_price,
                       prediction_hash
                FROM learning_predictions
                ORDER BY batch_id, prediction_id
                """
            )
            predictions_by_batch: dict[str, list[FrozenPrediction]] = {}
            for row in cursor.fetchall():
                item = FrozenPrediction(
                    prediction_id=str(row[0]),
                    batch_id=str(row[1]),
                    candidate_id=str(row[2]),
                    symbol=str(row[3]),
                    sector_benchmark=str(row[4]),
                    arm=str(row[5]),
                    action=str(row[6]),
                    selected=bool(row[7]),
                    score=float(row[8]),
                    position_weight=float(row[9]),
                    expected_turnover=float(row[10]),
                    decision_date=row[11],
                    decision_at=row[12],
                    frozen_at=row[13],
                    data_cutoff_at=row[14],
                    model_id=str(row[15]),
                    model_hash=str(row[16]),
                    prompt_hash=str(row[17]),
                    feature_hash=str(row[18]),
                    data_snapshot_hash=str(row[19]),
                    entry_price=float(row[20]),
                    entry_spy_price=float(row[21]),
                    entry_sector_price=float(row[22]),
                    prediction_hash=str(row[23]),
                )
                predictions_by_batch.setdefault(item.batch_id, []).append(item)
        batches = []
        for batch_id, decision_date, decision_at, frozen_at, batch_hash in batch_rows:
            predictions = tuple(predictions_by_batch.get(str(batch_id), []))
            batches.append(
                PredictionBatch(
                    batch_id=str(batch_id),
                    decision_date=decision_date,
                    decision_at=decision_at,
                    frozen_at=frozen_at,
                    expected_candidate_ids=tuple(
                        sorted({item.candidate_id for item in predictions})
                    ),
                    predictions=predictions,
                    batch_hash=str(batch_hash),
                )
            )
        return batches

    def existing_outcome_keys(self) -> set[tuple[str, int]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT prediction_id, horizon_sessions FROM learning_forward_outcomes")
            return {(str(row[0]), int(row[1])) for row in cursor.fetchall()}

    def record_outcomes(self, outcomes: list[ForwardOutcome]) -> None:
        if not outcomes:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO learning_forward_outcomes
                    (outcome_id, prediction_id, prediction_hash, batch_hash,
                     candidate_id, arm, decision_date, horizon_sessions,
                     mark_session_date, mark_observed_at, mark_available_at,
                     recorded_at, gross_return, spy_relative_return,
                     sector_relative_return, strategy_gross_return, turnover,
                     cost_bps, cost_return, strategy_net_return, outcome_hash)
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (prediction_id, horizon_sessions) DO NOTHING
                """,
                [
                    (
                        item.outcome_id,
                        item.prediction_id,
                        item.prediction_hash,
                        item.batch_hash,
                        item.candidate_id,
                        item.arm,
                        item.decision_date,
                        item.horizon_sessions,
                        item.mark_session_date,
                        item.mark_observed_at,
                        item.mark_available_at,
                        item.recorded_at,
                        item.gross_return,
                        item.spy_relative_return,
                        item.sector_relative_return,
                        item.strategy_gross_return,
                        item.turnover,
                        item.cost_bps,
                        item.cost_return,
                        item.strategy_net_return,
                        item.outcome_hash,
                    )
                    for item in outcomes
                ],
            )
            prediction_ids = sorted({item.prediction_id for item in outcomes})
            cursor.execute(
                """
                SELECT prediction_id, horizon_sessions, outcome_hash
                FROM learning_forward_outcomes
                WHERE prediction_id = ANY(%s)
                """,
                (prediction_ids,),
            )
            stored = {(str(row[0]), int(row[1])): str(row[2]) for row in cursor.fetchall()}
            expected = {
                (item.prediction_id, item.horizon_sessions): item.outcome_hash for item in outcomes
            }
            stored = {key: value for key, value in stored.items() if key in expected}
            if stored != expected:
                raise ValueError("Learning forward outcomes are immutable")

    def load_outcomes(self) -> list[ForwardOutcome]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT outcome_id, prediction_id, prediction_hash, batch_hash,
                       candidate_id, arm, decision_date, horizon_sessions,
                       mark_session_date, mark_observed_at, mark_available_at,
                       recorded_at, gross_return, spy_relative_return,
                       sector_relative_return, strategy_gross_return, turnover,
                       cost_bps, cost_return, strategy_net_return, outcome_hash
                FROM learning_forward_outcomes
                ORDER BY decision_date, prediction_id, horizon_sessions
                """
            )
            return [ForwardOutcome(*row) for row in cursor.fetchall()]

    def record_report(self, report: PromotionReport) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO learning_evaluation_reports
                    (report_id, generated_at, as_of, horizon_sessions,
                     policy_hash, report_hash, passed, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (report_hash) DO NOTHING
                """,
                (
                    report.report_hash,
                    report.generated_at,
                    report.generated_at,
                    report.horizon_sessions,
                    hashlib.sha256(
                        json.dumps(
                            dict(report.policy), sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    report.report_hash,
                    report.passed,
                    Jsonb(report.to_dict()),
                ),
            )

    def current_state(self, system_key: str = "agentic-trader") -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT resulting_state FROM learning_current_state
                WHERE system_key = %s
                """,
                (system_key,),
            )
            row = cursor.fetchone()
            return "shadow" if row is None else str(row[0])
