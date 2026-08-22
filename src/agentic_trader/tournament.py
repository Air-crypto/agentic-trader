from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from math import sqrt
from pathlib import Path

import pandas as pd

from .backtest import performance_metrics, simulate
from .config import (
    CASH_ASSET,
    DEFENSIVE_ASSETS,
    RISK_ETFS,
    StrategyConfig,
)
from .strategy import build_daily_targets

TargetBuilder = Callable[[pd.DataFrame, StrategyConfig], pd.DataFrame]


def _lagged_return(series: pd.Series, lookback: int = 252, skip: int = 21) -> float:
    clean = series.dropna()
    if len(clean) <= lookback:
        return float("nan")
    return float(clean.iloc[-(skip + 1)] / clean.iloc[-(lookback + 1)] - 1.0)


def _month_ends(prices: pd.DataFrame) -> pd.DatetimeIndex:
    return prices.groupby(prices.index.to_period("M")).tail(1).index


def _to_daily(
    monthly: dict[pd.Timestamp, pd.Series],
    prices: pd.DataFrame,
    config: StrategyConfig,
) -> pd.DataFrame:
    targets = pd.DataFrame(monthly).T.reindex(columns=config.all_assets)
    daily = targets.reindex(prices.index).ffill().shift(config.signal_lag_trading_days).fillna(0.0)
    no_position = daily.sum(axis=1).eq(0.0)
    daily.loc[no_position, CASH_ASSET] = 1.0
    return daily


def _empty_target(config: StrategyConfig) -> pd.Series:
    return pd.Series(0.0, index=config.all_assets, dtype=float)


def build_balanced_targets(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    monthly: dict[pd.Timestamp, pd.Series] = {}
    for date in _month_ends(prices):
        target = _empty_target(config)
        target.loc[["SPY", "IEF", "GLD", CASH_ASSET]] = [0.50, 0.25, 0.15, 0.10]
        monthly[date] = target
    return _to_daily(monthly, prices, config)


def build_spy_trend_targets(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    monthly: dict[pd.Timestamp, pd.Series] = {}
    for date in _month_ends(prices):
        history = prices.loc[:date]
        target = _empty_target(config)
        target.loc[CASH_ASSET] = 1.0
        if len(history) <= config.long_momentum_days:
            monthly[date] = target
            continue

        spy = history["SPY"].dropna()
        cash = history[CASH_ASSET].dropna()
        spy_momentum = _lagged_return(spy, config.long_momentum_days, config.skip_days)
        cash_momentum = _lagged_return(cash, config.long_momentum_days, config.skip_days)
        trend = float(spy.iloc[-1] / spy.tail(config.trend_days).mean() - 1.0)
        volatility = float(
            spy.pct_change(fill_method=None).tail(config.volatility_days).std(ddof=1) * sqrt(252)
        )
        if trend > 0 and spy_momentum > cash_momentum and volatility > 0:
            spy_weight = min(1.0, config.target_volatility / volatility)
            target.loc["SPY"] = spy_weight
            target.loc[CASH_ASSET] = 1.0 - spy_weight
        monthly[date] = target
    return _to_daily(monthly, prices, config)


def build_diversified_trend_targets(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    monthly: dict[pd.Timestamp, pd.Series] = {}
    candidates = RISK_ETFS + DEFENSIVE_ASSETS
    for date in _month_ends(prices):
        history = prices.loc[:date]
        target = _empty_target(config)
        target.loc[CASH_ASSET] = 1.0
        if len(history) <= config.long_momentum_days:
            monthly[date] = target
            continue

        cash_momentum = _lagged_return(
            history[CASH_ASSET], config.long_momentum_days, config.skip_days
        )
        volatilities: dict[str, float] = {}
        for symbol in candidates:
            series = history[symbol].dropna()
            if len(series) <= config.long_momentum_days:
                continue
            momentum = _lagged_return(series, config.long_momentum_days, config.skip_days)
            trend = float(series.iloc[-1] / series.tail(config.trend_days).mean() - 1.0)
            volatility = float(
                series.pct_change(fill_method=None).tail(config.volatility_days).std(ddof=1)
                * sqrt(252)
            )
            if trend > 0 and momentum > cash_momentum and volatility > 0:
                volatilities[symbol] = volatility

        if volatilities:
            inverse_volatility = pd.Series(
                {symbol: 1.0 / value for symbol, value in volatilities.items()}
            )
            weights = (inverse_volatility / inverse_volatility.sum()).clip(upper=0.25)
            returns = history[list(weights.index)].pct_change(fill_method=None)
            covariance = returns.tail(config.volatility_days).cov() * 252
            portfolio_variance = float(weights @ covariance @ weights)
            if portfolio_variance > 0:
                weights *= min(1.0, config.target_volatility / sqrt(portfolio_variance))
            target.loc[weights.index] = weights
            target.loc[CASH_ASSET] = 1.0 - float(weights.sum())
        monthly[date] = target
    return _to_daily(monthly, prices, config)


def build_relative_momentum_targets(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    targets, _ = build_daily_targets(prices, config)
    return targets


def build_price_ensemble_targets(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    components = [
        build_spy_trend_targets(prices, config),
        build_diversified_trend_targets(prices, config),
        build_relative_momentum_targets(prices, config),
    ]
    combined = sum(components) / len(components)
    return combined.div(combined.sum(axis=1), axis=0)


STRATEGIES: dict[str, TargetBuilder] = {
    "balanced_50_25_15_10": build_balanced_targets,
    "spy_absolute_trend": build_spy_trend_targets,
    "diversified_absolute_trend": build_diversified_trend_targets,
    "relative_momentum": build_relative_momentum_targets,
    "price_ensemble": build_price_ensemble_targets,
}


def _cash_metrics(
    prices: pd.DataFrame, config: StrategyConfig, start: str, end: str | None
) -> dict[str, float]:
    targets = pd.DataFrame(0.0, index=prices.index, columns=config.all_assets)
    targets[CASH_ASSET] = 1.0
    daily, _ = simulate(prices, targets, config, start=start, end=end)
    return performance_metrics(daily)


def _strategy_metrics(
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    config: StrategyConfig,
    start: str,
    end: str | None,
) -> tuple[dict[str, float], pd.DataFrame]:
    daily, _ = simulate(prices, targets, config, start=start, end=end)
    cash_returns = prices[CASH_ASSET].pct_change(fill_method=None).fillna(0.0)
    return performance_metrics(daily, cash_returns), daily


def run_tournament(
    prices: pd.DataFrame,
    config: StrategyConfig,
    development_start: str = "2010-01-01",
    holdout_start: str = "2019-01-01",
    output_dir: str | Path = "artifacts/tournament",
) -> dict[str, object]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    development_end = (pd.Timestamp(holdout_start) - pd.Timedelta(days=1)).date().isoformat()
    targets = {strategy: builder(prices, config) for strategy, builder in STRATEGIES.items()}

    cash_development = _cash_metrics(prices, config, development_start, development_end)
    cash_holdout = _cash_metrics(prices, config, holdout_start, None)
    rows: list[dict[str, object]] = []
    daily_results: dict[tuple[str, str], pd.DataFrame] = {}
    for strategy, strategy_targets in targets.items():
        for period, start, end in (
            ("development", development_start, development_end),
            ("holdout", holdout_start, None),
        ):
            metrics, daily = _strategy_metrics(prices, strategy_targets, config, start, end)
            rows.append({"strategy": strategy, "period": period, **metrics})
            daily_results[(strategy, period)] = daily

    metrics_frame = pd.DataFrame(rows)
    development = metrics_frame.loc[metrics_frame["period"].eq("development")].copy()
    development["qualified"] = (
        development["cagr"].gt(cash_development["cagr"])
        & development["sharpe_vs_cash"].ge(0.40)
        & development["max_drawdown"].ge(-0.15)
    )
    qualified = development.loc[development["qualified"]]
    selection_pool = qualified if not qualified.empty else development
    selected_row = selection_pool.sort_values(["sharpe_vs_cash", "calmar"], ascending=False).iloc[0]
    selected = str(selected_row["strategy"])

    holdout_row = metrics_frame.loc[
        metrics_frame["strategy"].eq(selected) & metrics_frame["period"].eq("holdout")
    ].iloc[0]
    stress_config = replace(config, one_way_cost_bps=25.0)
    stress_metrics, stress_daily = _strategy_metrics(
        prices, targets[selected], stress_config, holdout_start, None
    )
    gates = {
        "selected_from_development_passed": bool(selected_row.get("qualified", False)),
        "holdout_beats_cash": float(holdout_row["cagr"]) > cash_holdout["cagr"],
        "holdout_sharpe_at_least_0_50": float(holdout_row["sharpe_vs_cash"]) >= 0.50,
        "holdout_drawdown_within_12_percent": float(holdout_row["max_drawdown"]) >= -0.12,
        "holdout_positive_month_rate_at_least_55_percent": float(holdout_row["positive_month_rate"])
        >= 0.55,
        "cost_stress_still_beats_cash": stress_metrics["cagr"] > cash_holdout["cagr"],
        "cost_stress_sharpe_at_least_0_35": stress_metrics["sharpe_vs_cash"] >= 0.35,
    }
    status = "pure_algo_candidate_passes" if all(gates.values()) else "no_candidate_passes"

    metrics_frame.to_csv(destination / "strategy-metrics.csv", index=False)
    daily_results[(selected, "holdout")].to_parquet(destination / "selected-holdout-daily.parquet")
    stress_daily.to_parquet(destination / "selected-holdout-cost-stress.parquet")
    report: dict[str, object] = {
        "status": status,
        "selection_protocol": {
            "development_period": f"{development_start} through {development_end}",
            "holdout_period": f"{holdout_start} through latest",
            "rule": (
                "Among development-qualified strategies, select the highest "
                "Sharpe versus BIL, breaking ties by Calmar."
            ),
            "strategies_tested": list(STRATEGIES),
        },
        "selected_strategy": selected,
        "development_qualified": bool(selected_row.get("qualified", False)),
        "development_metrics": {
            key: float(selected_row[key])
            for key in (
                "cagr",
                "annual_volatility",
                "sharpe_vs_cash",
                "max_drawdown",
                "calmar",
                "annual_turnover",
            )
        },
        "holdout_metrics": {
            key: float(holdout_row[key])
            for key in (
                "cagr",
                "annual_volatility",
                "sharpe_vs_cash",
                "max_drawdown",
                "calmar",
                "annual_turnover",
            )
        },
        "holdout_cash_cagr": cash_holdout["cagr"],
        "cost_stress_25bps": stress_metrics,
        "gates": gates,
        "llm_only_status": "not_testable_without_timestamped_historical_corpus",
        "hybrid_status": ("event_satellite_weight_zero_until_event_study_gates_pass"),
    }
    (destination / "summary.json").write_text(json.dumps(report, indent=2))
    return report
