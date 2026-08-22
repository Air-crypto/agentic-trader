from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import sqrt

import pandas as pd


def _rankdata(values: list[float]) -> list[float]:
    """Average ranks for ties; 1-based like scipy.stats.rankdata(method='average')."""
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        for position in range(index, end):
            ranks[ordered[position][0]] = average_rank
        index = end
    return ranks


def spearman_correlation(left: pd.Series, right: pd.Series) -> float:
    """Spearman rank correlation without a scipy dependency."""
    paired = pd.concat([left, right], axis=1).dropna()
    if len(paired) < 2:
        return float("nan")
    x_ranks = _rankdata(paired.iloc[:, 0].astype(float).tolist())
    y_ranks = _rankdata(paired.iloc[:, 1].astype(float).tolist())
    return float(pd.Series(x_ranks).corr(pd.Series(y_ranks), method="pearson"))


@dataclass(frozen=True)
class OutcomeMark:
    packet_id: str
    horizon_days: int
    measured_at: datetime
    raw_return: float
    spy_abnormal_return: float
    sector_abnormal_return: float

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "measured_at": self.measured_at.isoformat()}


def measure_outcome(
    packet_id: str,
    horizon_days: int,
    measured_at: datetime,
    entry_price: float,
    current_price: float,
    entry_spy_price: float,
    current_spy_price: float,
    entry_sector_price: float,
    current_sector_price: float,
) -> OutcomeMark:
    if horizon_days not in {1, 3, 5, 20, 60}:
        raise ValueError("Outcome horizon must be one of 1, 3, 5, 20, or 60")
    prices = (
        entry_price,
        current_price,
        entry_spy_price,
        current_spy_price,
        entry_sector_price,
        current_sector_price,
    )
    if any(price <= 0 for price in prices):
        raise ValueError("Outcome prices must be positive")
    raw = current_price / entry_price - 1.0
    spy = current_spy_price / entry_spy_price - 1.0
    sector = current_sector_price / entry_sector_price - 1.0
    return OutcomeMark(
        packet_id=packet_id,
        horizon_days=horizon_days,
        measured_at=measured_at,
        raw_return=raw,
        spy_abnormal_return=raw - spy,
        sector_abnormal_return=raw - sector,
    )


def evaluation_summary(frame: pd.DataFrame) -> dict[str, float | int]:
    """Evaluate all candidates, not only selected trades."""
    required = {"rank_score", "sector_abnormal_return", "selected"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Outcome frame is missing columns: {sorted(missing)}")
    clean = frame.dropna(subset=["rank_score", "sector_abnormal_return"]).copy()
    if clean.empty:
        return {
            "observations": 0,
            "rank_ic": float("nan"),
            "selected_mean_sector_abnormal_return": float("nan"),
            "selected_hit_rate": float("nan"),
            "rank_ic_t_statistic": float("nan"),
        }
    rank_ic = spearman_correlation(clean["rank_score"], clean["sector_abnormal_return"])
    selected = clean.loc[clean["selected"].astype(bool), "sector_abnormal_return"]
    # A rough daily-independent diagnostic only; clustered inference belongs in
    # the longer forward-evaluation report.
    t_statistic = (
        float(rank_ic * sqrt(len(clean) - 2) / sqrt(max(1.0 - rank_ic**2, 1e-12)))
        if len(clean) > 2
        else float("nan")
    )
    return {
        "observations": len(clean),
        "rank_ic": rank_ic,
        "selected_mean_sector_abnormal_return": (
            float(selected.mean()) if not selected.empty else float("nan")
        ),
        "selected_hit_rate": (float(selected.gt(0).mean()) if not selected.empty else float("nan")),
        "rank_ic_t_statistic": t_statistic,
    }
