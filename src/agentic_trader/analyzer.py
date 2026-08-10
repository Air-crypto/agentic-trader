from __future__ import annotations

import json
from math import sqrt
from pathlib import Path

import pandas as pd

LOOKBACKS = {
    "return_1m": 21,
    "return_3m": 63,
    "return_6m": 126,
    "return_12m": 252,
}


def _trailing_return(series: pd.Series, days: int) -> float:
    clean = series.dropna()
    if len(clean) <= days:
        return float("nan")
    return float(clean.iloc[-1] / clean.iloc[-(days + 1)] - 1.0)


def _maximum_drawdown(series: pd.Series) -> float:
    clean = series.dropna()
    wealth = clean / clean.iloc[0]
    return float((wealth / wealth.cummax() - 1.0).min())


def analyze_universe(prices: pd.DataFrame, benchmark: str = "SPY") -> pd.DataFrame:
    """Calculate point-in-time features for any symbols with sufficient history."""
    if benchmark not in prices:
        raise ValueError(f"Benchmark {benchmark} is missing")
    benchmark_returns = prices[benchmark].pct_change(fill_method=None)
    rows: list[dict[str, float | str | bool]] = []
    for symbol in prices.columns:
        if symbol == benchmark:
            continue
        series = prices[symbol].dropna()
        if len(series) < 253:
            rows.append(
                {
                    "symbol": symbol,
                    "sufficient_history": False,
                    "observations": len(series),
                }
            )
            continue

        returns = series.pct_change(fill_method=None)
        aligned = pd.concat(
            [returns.rename("asset"), benchmark_returns.rename("benchmark")], axis=1
        ).dropna()
        trailing = aligned.tail(252)
        benchmark_variance = float(trailing["benchmark"].var(ddof=1))
        beta = (
            float(trailing.cov().loc["asset", "benchmark"] / benchmark_variance)
            if benchmark_variance > 0
            else float("nan")
        )
        correlation = float(trailing["asset"].corr(trailing["benchmark"]))
        volatility_20 = float(returns.tail(20).std(ddof=1) * sqrt(252))
        volatility_63 = float(returns.tail(63).std(ddof=1) * sqrt(252))
        downside = returns.tail(63).clip(upper=0)
        downside_volatility = float(sqrt(downside.pow(2).mean() * 252))
        recent_year = series.tail(253)
        high_52w = float(recent_year.max())
        rows.append(
            {
                "symbol": symbol,
                "sufficient_history": True,
                "observations": len(series),
                "last_date": series.index[-1].date().isoformat(),
                "last_adjusted_close": float(series.iloc[-1]),
                **{name: _trailing_return(series, days) for name, days in LOOKBACKS.items()},
                "distance_from_sma_50": float(series.iloc[-1] / series.tail(50).mean() - 1.0),
                "distance_from_sma_200": float(series.iloc[-1] / series.tail(200).mean() - 1.0),
                "distance_from_52w_high": float(series.iloc[-1] / high_52w - 1.0),
                "annual_volatility_20d": volatility_20,
                "annual_volatility_63d": volatility_63,
                "annual_downside_volatility_63d": downside_volatility,
                "beta_252d": beta,
                "correlation_to_benchmark_252d": correlation,
                "max_drawdown_252d": _maximum_drawdown(recent_year),
                "worst_day_252d": float(returns.tail(252).min()),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("No non-benchmark symbols were supplied")
    valid = frame["sufficient_history"].fillna(False)
    if valid.any():
        momentum = (
            0.35 * frame.loc[valid, "return_12m"]
            + 0.25 * frame.loc[valid, "return_6m"]
            + 0.15 * frame.loc[valid, "return_3m"]
            + 0.10 * frame.loc[valid, "distance_from_sma_200"]
            - 0.15 * frame.loc[valid, "annual_volatility_63d"]
        )
        frame.loc[valid, "descriptive_momentum_quality_score"] = momentum
    return frame


def write_analysis(analysis: pd.DataFrame, output_dir: str | Path) -> dict[str, object]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    analysis.to_csv(destination / "universe-analysis.csv", index=False)
    payload: dict[str, object] = {
        "as_of": str(analysis.loc[analysis["sufficient_history"], "last_date"].max()),
        "instruments": analysis.to_dict(orient="records"),
        "note": (
            "The descriptive score is a feature summary, not a validated forecast "
            "or trade instruction."
        ),
    }
    (destination / "universe-analysis.json").write_text(json.dumps(payload, indent=2, default=str))
    return payload
