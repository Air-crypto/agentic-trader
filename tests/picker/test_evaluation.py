from __future__ import annotations

import pandas as pd

from agentic_trader.picker.evaluation import evaluation_summary, measure_outcome


def test_outcome_is_measured_relative_to_market_and_sector(now):
    outcome = measure_outcome(
        "packet-1",
        5,
        now,
        entry_price=100.0,
        current_price=110.0,
        entry_spy_price=500.0,
        current_spy_price=525.0,
        entry_sector_price=200.0,
        current_sector_price=216.0,
    )
    assert abs(outcome.raw_return - 0.10) < 1e-12
    assert abs(outcome.spy_abnormal_return - 0.05) < 1e-12
    assert abs(outcome.sector_abnormal_return - 0.02) < 1e-12


def test_summary_uses_all_candidates_for_rank_ic():
    frame = pd.DataFrame(
        {
            "rank_score": [0.9, 0.7, 0.3, 0.1],
            "sector_abnormal_return": [0.08, 0.03, -0.01, -0.05],
            "selected": [True, True, False, False],
        }
    )
    summary = evaluation_summary(frame)
    assert summary["observations"] == 4
    assert summary["rank_ic"] == 1.0
    assert summary["selected_hit_rate"] == 1.0
