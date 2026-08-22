from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from agentic_trader.picker.features import (
    liquid_universe,
    rank_candidates,
    snapshots_from_ranked,
)


def candidates() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "GOOD",
                "sector": "Technology",
                "last_price": 50.0,
                "market_cap": 10_000_000_000,
                "average_dollar_volume": 100_000_000,
                "spread_bps": 5,
                "fractional_tradable": True,
                "sufficient_history": True,
                "volatility_63d": 0.25,
                "beta_252d": 1.0,
                "atr_pct": 0.03,
                "return_3m": 0.10,
                "return_6m": 0.20,
                "return_12m": 0.30,
                "distance_from_sma_200": 0.10,
                "revenue_growth": 0.20,
                "gross_margin_change": 0.02,
                "net_income_growth": 0.25,
                "return_on_equity": 0.30,
                "earnings_surprise": 0.10,
                "guidance_change": 0.10,
                "cash_flow_revision": 0.10,
            },
            {
                "symbol": "ILLIQ",
                "sector": "Technology",
                "last_price": 2.0,
                "market_cap": 100_000_000,
                "average_dollar_volume": 100_000,
                "spread_bps": 200,
                "fractional_tradable": False,
                "sufficient_history": True,
                "volatility_63d": 1.0,
                "beta_252d": 2.0,
                "atr_pct": 0.20,
            },
        ]
    )


def test_liquidity_gate_removes_unexecutable_candidates():
    result = liquid_universe(candidates())
    assert result["symbol"].tolist() == ["GOOD"]


def test_ranker_outputs_bounded_simple_factor_score():
    result = rank_candidates(liquid_universe(candidates()))
    assert result.loc[0, "symbol"] == "GOOD"
    assert 0 <= result.loc[0, "baseline_rank_score"] <= 1


def test_missing_required_market_column_fails_closed():
    with pytest.raises(ValueError, match="required columns"):
        liquid_universe(candidates().drop(columns=["spread_bps"]))


def test_snapshot_records_deterministic_feature_provenance():
    ranked = rank_candidates(liquid_universe(candidates()))
    snapshots = snapshots_from_ranked(ranked, datetime(2026, 8, 21, tzinfo=UTC))
    snapshot = snapshots["GOOD"]
    assert snapshot.calculated_by == "agentic_trader.picker.features"
    assert snapshot.feature_version == "picker_features_v1"
    assert len(snapshot.data_snapshot_hash) == 64
