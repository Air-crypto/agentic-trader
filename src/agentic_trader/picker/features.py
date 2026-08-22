from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .models import QuantSnapshot, canonical_json, content_hash

FEATURE_VERSION = "picker_features_v1"


@dataclass(frozen=True)
class FeaturePolicy:
    min_price: float = 5.0
    min_market_cap: float = 2_000_000_000.0
    min_average_dollar_volume: float = 50_000_000.0
    max_spread_bps: float = 25.0


REQUIRED_MARKET_COLUMNS = {
    "symbol",
    "sector",
    "last_price",
    "market_cap",
    "average_dollar_volume",
    "spread_bps",
    "fractional_tradable",
    "sufficient_history",
    "volatility_63d",
    "beta_252d",
    "atr_pct",
}


def _series(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(float("nan"), index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def _rank_within_sector(frame: pd.DataFrame, values: pd.Series) -> pd.Series:
    ranked = values.groupby(frame["sector"].fillna("Unknown")).rank(pct=True, method="average")
    overall = values.rank(pct=True, method="average")
    return (0.5 * ranked + 0.5 * overall).fillna(0.0).clip(0.0, 1.0)


def liquid_universe(frame: pd.DataFrame, policy: FeaturePolicy | None = None) -> pd.DataFrame:
    """Apply frozen current-liquidity gates before any LLM sees a candidate."""
    policy = policy or FeaturePolicy()
    missing = REQUIRED_MARKET_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Candidate frame is missing required columns: {sorted(missing)}")
    result = frame.copy()
    result["symbol"] = result["symbol"].astype(str).str.upper()
    result["sector"] = result["sector"].fillna("Unknown").astype(str)
    numeric = ("last_price", "market_cap", "average_dollar_volume", "spread_bps")
    for column in numeric:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    accepted = (
        result["last_price"].ge(policy.min_price)
        & result["market_cap"].ge(policy.min_market_cap)
        & result["average_dollar_volume"].ge(policy.min_average_dollar_volume)
        & result["spread_bps"].le(policy.max_spread_bps)
        & result["fractional_tradable"].eq(True)  # noqa: E712 - pandas vector comparison
        & result["sufficient_history"].eq(True)  # noqa: E712 - pandas vector comparison
    )
    return result.loc[accepted].reset_index(drop=True)


def rank_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    """Create simple, frozen cross-sectional ranks from orthogonal feature families."""
    if frame.empty:
        return frame.assign(
            momentum_rank=pd.Series(dtype=float),
            quality_rank=pd.Series(dtype=float),
            revisions_rank=pd.Series(dtype=float),
            baseline_rank_score=pd.Series(dtype=float),
        )
    result = frame.copy()
    momentum_raw = (
        0.35 * _series(result, "return_12m")
        + 0.25 * _series(result, "return_6m")
        + 0.15 * _series(result, "return_3m")
        + 0.10 * _series(result, "distance_from_sma_200")
        - 0.15 * _series(result, "volatility_63d")
    )
    quality_raw = pd.concat(
        [
            _series(result, "revenue_growth"),
            _series(result, "gross_margin_change"),
            _series(result, "net_income_growth"),
            _series(result, "return_on_equity"),
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    revisions_raw = pd.concat(
        [
            _series(result, "earnings_surprise"),
            _series(result, "guidance_change"),
            _series(result, "cash_flow_revision"),
        ],
        axis=1,
    ).mean(axis=1, skipna=True)

    result["momentum_rank"] = _rank_within_sector(result, momentum_raw)
    result["quality_rank"] = _rank_within_sector(result, quality_raw)
    result["revisions_rank"] = _rank_within_sector(result, revisions_raw)
    # Equal weighting is intentionally difficult to overfit and provides a
    # no-LLM baseline against which catalyst extraction can be measured.
    result["baseline_rank_score"] = result[
        ["momentum_rank", "quality_rank", "revisions_rank"]
    ].mean(axis=1)
    return result.sort_values(
        ["baseline_rank_score", "average_dollar_volume", "symbol"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def snapshots_from_ranked(
    ranked: pd.DataFrame,
    as_of: datetime,
) -> dict[str, QuantSnapshot]:
    snapshots: dict[str, QuantSnapshot] = {}
    for raw in ranked.to_dict(orient="records"):
        frozen_input = {
            key: raw.get(key)
            for key in sorted(
                REQUIRED_MARKET_COLUMNS
                | {
                    "return_12m",
                    "return_6m",
                    "return_3m",
                    "distance_from_sma_200",
                    "revenue_growth",
                    "gross_margin_change",
                    "net_income_growth",
                    "return_on_equity",
                    "earnings_surprise",
                    "guidance_change",
                    "cash_flow_revision",
                }
            )
        }
        snapshot = QuantSnapshot.from_dict(
            {
                "symbol": raw["symbol"],
                "as_of": as_of.isoformat(),
                "last_price": raw["last_price"],
                "market_cap": raw["market_cap"],
                "average_dollar_volume": raw["average_dollar_volume"],
                "spread_bps": raw["spread_bps"],
                "sector": raw["sector"],
                "fractional_tradable": raw["fractional_tradable"],
                "sufficient_history": raw["sufficient_history"],
                "momentum_rank": raw["momentum_rank"],
                "quality_rank": raw["quality_rank"],
                "revisions_rank": raw["revisions_rank"],
                "volatility_63d": raw["volatility_63d"],
                "beta_252d": raw["beta_252d"],
                "atr_pct": raw["atr_pct"],
                "data_snapshot_hash": content_hash(canonical_json(frozen_input)),
                "feature_version": FEATURE_VERSION,
                "calculated_by": "agentic_trader.picker.features",
            }
        )
        snapshots[snapshot.symbol] = snapshot
    return snapshots
