from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import BacktestResult, performance_metrics, run_backtest
from .config import CASH_ASSET, StrategyConfig


def _period_metrics(
    daily: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cash_returns: pd.Series,
) -> dict[str, float]:
    period = daily.loc[start:end].copy()
    period["equity"] = (1.0 + period["net_return"]).cumprod()
    period["drawdown"] = period["equity"] / period["equity"].cummax() - 1.0
    return performance_metrics(period, cash_returns.loc[start:end])


def rolling_windows(result: BacktestResult, prices: pd.DataFrame, years: int = 2) -> pd.DataFrame:
    first_year = result.daily.index.min().year
    last_year = result.daily.index.max().year
    cash_returns = prices[CASH_ASSET].pct_change(fill_method=None).fillna(0.0)
    rows: list[dict[str, float | str]] = []
    for start_year in range(first_year, last_year - years + 2):
        start = pd.Timestamp(f"{start_year}-01-01")
        end = pd.Timestamp(f"{start_year + years - 1}-12-31")
        period = result.daily.loc[start:end]
        if len(period) < 252 * years * 0.8:
            continue
        rows.append(
            {
                "window": f"{start_year}-{start_year + years - 1}",
                **_period_metrics(result.daily, start, end, cash_returns),
            }
        )
    return pd.DataFrame(rows)


def _cash_metrics(prices: pd.DataFrame, start: str) -> dict[str, float]:
    returns = prices[CASH_ASSET].pct_change(fill_method=None).fillna(0.0)
    returns = returns.loc[pd.Timestamp(start) :]
    equity = (1.0 + returns).cumprod()
    daily = pd.DataFrame(
        {
            "net_return": returns,
            "equity": equity,
            "drawdown": equity / equity.cummax() - 1.0,
            "turnover": 0.0,
        }
    )
    return performance_metrics(daily)


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def validate_strategy(
    prices: pd.DataFrame,
    config: StrategyConfig,
    output_dir: str | Path = "artifacts/latest",
) -> dict[str, object]:
    """Run the fixed primary test plus a deliberately small robustness grid."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    primary = run_backtest(prices, config)
    windows = rolling_windows(primary, prices, years=2)
    cash = _cash_metrics(prices, config.out_of_sample_start)

    robustness_rows: list[dict[str, float | int]] = []
    for top_n in (3, 4, 5):
        for target_volatility in (0.06, 0.08, 0.10):
            for long_momentum_days in (189, 252, 315):
                candidate = replace(
                    config,
                    top_n=top_n,
                    target_volatility=target_volatility,
                    long_momentum_days=long_momentum_days,
                )
                tested = run_backtest(prices, candidate)
                robustness_rows.append(
                    {
                        "top_n": top_n,
                        "target_volatility": target_volatility,
                        "long_momentum_days": long_momentum_days,
                        **tested.metrics,
                    }
                )
    robustness = pd.DataFrame(robustness_rows)

    positive_window_rate = float(windows["total_return"].gt(0).mean()) if not windows.empty else 0.0
    robustness_positive_rate = float(robustness["cagr"].gt(0).mean())
    robustness_median_sharpe = float(robustness["sharpe_vs_cash"].median())
    gates = {
        "positive_oos_cagr": primary.metrics["cagr"] > 0,
        "beats_cash": primary.metrics["cagr"] > cash["cagr"],
        "sharpe_at_least_0_50": primary.metrics["sharpe_vs_cash"] >= 0.50,
        "drawdown_within_12_percent": primary.metrics["max_drawdown"] >= -0.12,
        "positive_two_year_windows_at_least_70_percent": positive_window_rate >= 0.70,
        "robustness_positive_at_least_80_percent": robustness_positive_rate >= 0.80,
        "robustness_median_sharpe_at_least_0_40": robustness_median_sharpe >= 0.40,
    }
    status = "passes_research_gates" if all(gates.values()) else "fails_research_gates"

    primary.daily.to_parquet(destination / "primary-daily.parquet")
    primary.weights.to_parquet(destination / "primary-weights.parquet")
    primary.decisions.to_parquet(destination / "primary-decisions.parquet")
    primary.benchmark_daily.to_parquet(destination / "spy-daily.parquet")
    windows.to_csv(destination / "rolling-windows.csv", index=False)
    robustness.to_csv(destination / "robustness.csv", index=False)

    summary: dict[str, object] = {
        "status": status,
        "important_limitations": [
            "Historical performance does not establish future profitability.",
            "Yahoo data is suitable for research, not execution-quality pricing.",
            "The large-cap universe has survivorship bias when enabled.",
            "Tax, spread, market-impact, and fractional-share constraints are estimates.",
            "A passing backtest still requires forward paper trading.",
        ],
        "configuration": asdict(config),
        "primary": primary.metrics,
        "spy_buy_and_hold": primary.benchmark_metrics,
        "cash_bil": cash,
        "diagnostics": {
            "positive_two_year_window_rate": positive_window_rate,
            "robustness_positive_cagr_rate": robustness_positive_rate,
            "robustness_median_sharpe_vs_cash": robustness_median_sharpe,
            "robustness_runs": len(robustness),
        },
        "gates": gates,
    }
    with (destination / "summary.json").open("w") as handle:
        json.dump(_json_safe(summary), handle, indent=2, sort_keys=True)
    return summary
