from __future__ import annotations

import json
from dataclasses import dataclass
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd

from .models import ResearchBundle
from .scoring import score_bundle

STUDY_HORIZONS = (1, 5, 20, 60)


@dataclass
class EventStudyResult:
    observations: pd.DataFrame
    summary: pd.DataFrame
    gates: dict[str, bool]
    status: str
    diagnostics: dict[str, float | int]


def _bootstrap_interval(values: np.ndarray, samples: int = 10_000) -> tuple[float, float]:
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(7)
    means = rng.choice(values, size=(samples, len(values)), replace=True).mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _summarize(observations: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | int]] = []
    for horizon, group in observations.groupby("horizon_days"):
        values = group["net_abnormal_return"].to_numpy(dtype=float)
        standard_deviation = float(np.std(values, ddof=1)) if len(values) > 1 else np.nan
        t_statistic = (
            float(np.mean(values) / (standard_deviation / sqrt(len(values))))
            if len(values) > 1 and standard_deviation > 0
            else np.nan
        )
        low, high = _bootstrap_interval(values)
        rows.append(
            {
                "horizon_days": int(horizon),
                "events": len(values),
                "mean_net_abnormal_return": float(np.mean(values)),
                "median_net_abnormal_return": float(np.median(values)),
                "positive_rate": float(np.mean(values > 0)),
                "t_statistic": t_statistic,
                "bootstrap_95_low": low,
                "bootstrap_95_high": high,
            }
        )
    return pd.DataFrame(rows).sort_values("horizon_days").reset_index(drop=True)


def run_event_study(
    bundle: ResearchBundle,
    prices: pd.DataFrame,
    benchmark: str = "SPY",
    round_trip_cost_bps: float = 20.0,
) -> EventStudyResult:
    scores = {item.event_id: item for item in score_bundle(bundle)}
    observations: list[dict[str, object]] = []

    for event in sorted(bundle.events, key=lambda item: item.published_at):
        score = scores[event.id]
        if not score.eligible_for_event_study:
            continue
        available = prices[[event.ticker, benchmark]].dropna()
        event_day = pd.Timestamp(event.published_at.date())
        future_dates = available.index[available.index > event_day]
        if future_dates.empty:
            continue
        entry_date = future_dates[0]
        entry_position = available.index.get_loc(entry_date)
        for horizon in STUDY_HORIZONS:
            exit_position = entry_position + horizon
            if exit_position >= len(available):
                continue
            exit_date = available.index[exit_position]
            asset_return = float(
                available.loc[exit_date, event.ticker] / available.loc[entry_date, event.ticker]
                - 1.0
            )
            benchmark_return = float(
                available.loc[exit_date, benchmark] / available.loc[entry_date, benchmark] - 1.0
            )
            signed_abnormal = event.direction * (asset_return - benchmark_return)
            observations.append(
                {
                    "event_id": event.id,
                    "ticker": event.ticker,
                    "published_at": event.published_at.isoformat(),
                    "entry_date": entry_date.date().isoformat(),
                    "exit_date": exit_date.date().isoformat(),
                    "horizon_days": horizon,
                    "event_score": score.score,
                    "asset_return": asset_return,
                    "benchmark_return": benchmark_return,
                    "net_abnormal_return": signed_abnormal - round_trip_cost_bps / 10_000,
                }
            )

    columns = [
        "event_id",
        "ticker",
        "published_at",
        "entry_date",
        "exit_date",
        "horizon_days",
        "event_score",
        "asset_return",
        "benchmark_return",
        "net_abnormal_return",
    ]
    observation_frame = pd.DataFrame(observations, columns=columns)
    summary = _summarize(observation_frame) if not observation_frame.empty else pd.DataFrame()

    eligible_events = {row["event_id"] for row in observations}
    tickers = {row["ticker"] for row in observations}
    years = {pd.Timestamp(row["published_at"]).year for row in observations}
    ticker_counts = observation_frame.loc[
        observation_frame["horizon_days"].eq(20), "ticker"
    ].value_counts(normalize=True)
    max_ticker_share = float(ticker_counts.max()) if not ticker_counts.empty else 1.0
    twenty_day = summary.loc[summary["horizon_days"].eq(20)]
    mean_20 = (
        float(twenty_day.iloc[0]["mean_net_abnormal_return"])
        if not twenty_day.empty
        else float("nan")
    )
    t_stat_20 = float(twenty_day.iloc[0]["t_statistic"]) if not twenty_day.empty else float("nan")
    gates = {
        "at_least_30_eligible_events": len(eligible_events) >= 30,
        "at_least_5_tickers": len(tickers) >= 5,
        "at_least_3_calendar_years": len(years) >= 3,
        "no_ticker_above_25_percent": max_ticker_share <= 0.25,
        "mean_20_day_abnormal_above_0_5_percent": mean_20 > 0.005,
        "twenty_day_t_stat_at_least_2": t_stat_20 >= 2.0,
    }
    status = "passes_event_study_gates" if all(gates.values()) else "insufficient_evidence"
    diagnostics: dict[str, float | int] = {
        "eligible_events": len(eligible_events),
        "distinct_tickers": len(tickers),
        "distinct_years": len(years),
        "max_ticker_share": max_ticker_share,
        "mean_20_day_net_abnormal_return": mean_20,
        "twenty_day_t_statistic": t_stat_20,
        "round_trip_cost_bps": round_trip_cost_bps,
    }
    return EventStudyResult(
        observations=observation_frame,
        summary=summary,
        gates=gates,
        status=status,
        diagnostics=diagnostics,
    )


def write_event_study(result: EventStudyResult, output_dir: str | Path) -> dict[str, object]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    result.observations.to_csv(destination / "event-observations.csv", index=False)
    result.summary.to_csv(destination / "event-summary.csv", index=False)
    report: dict[str, object] = {
        "status": result.status,
        "gates": result.gates,
        "diagnostics": result.diagnostics,
        "limitations": [
            "Daily closes force conservative next-trading-day entry timing.",
            "A small or single-theme sample cannot establish an investment edge.",
            "Adjusted closes do not model intraday spread or market impact.",
            "Browser and language-model extraction must be audited for omissions.",
        ],
    }
    (destination / "event-study.json").write_text(json.dumps(report, indent=2, allow_nan=True))
    return report
