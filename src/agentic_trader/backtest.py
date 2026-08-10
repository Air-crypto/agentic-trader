from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd

from .config import CASH_ASSET, StrategyConfig
from .strategy import build_daily_targets


@dataclass
class BacktestResult:
    daily: pd.DataFrame
    weights: pd.DataFrame
    decisions: pd.DataFrame
    metrics: dict[str, float]
    benchmark_daily: pd.DataFrame
    benchmark_metrics: dict[str, float]


def performance_metrics(
    daily: pd.DataFrame, cash_returns: pd.Series | None = None
) -> dict[str, float]:
    returns = daily["net_return"].dropna()
    if returns.empty:
        raise ValueError("No returns available for metrics")
    years = len(returns) / 252
    ending_equity = float((1.0 + returns).prod())
    cagr = ending_equity ** (1.0 / years) - 1.0
    volatility = float(returns.std(ddof=1) * sqrt(252))
    excess = returns
    if cash_returns is not None:
        excess = returns - cash_returns.reindex(returns.index).fillna(0.0)
    excess_volatility = float(excess.std(ddof=1) * sqrt(252))
    sharpe = (
        float(excess.mean() * 252 / excess_volatility) if excess_volatility > 0 else float("nan")
    )
    downside = returns.clip(upper=0)
    downside_volatility = float(sqrt((downside.pow(2).mean()) * 252))
    sortino = (
        float(returns.mean() * 252 / downside_volatility)
        if downside_volatility > 0
        else float("nan")
    )
    max_drawdown = float(daily["drawdown"].min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else float("nan")
    monthly = (1.0 + returns).resample("ME").prod() - 1.0
    return {
        "total_return": ending_equity - 1.0,
        "cagr": cagr,
        "annual_volatility": volatility,
        "sharpe_vs_cash": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "positive_month_rate": float(monthly.gt(0).mean()),
        "annual_turnover": float(daily["turnover"].sum() / years),
    }


def simulate(
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    config: StrategyConfig,
    start: str | None = None,
    end: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    index = prices.index
    if start is not None:
        index = index[index >= pd.Timestamp(start)]
    if end is not None:
        index = index[index <= pd.Timestamp(end)]
    asset_returns = prices.pct_change(fill_method=None).fillna(0.0).reindex(index)
    requested_targets = targets.reindex(index).ffill()

    current = pd.Series(0.0, index=config.all_assets)
    current.loc[CASH_ASSET] = 1.0
    last_requested: pd.Series | None = None
    equity = 1.0
    global_peak = 1.0
    risk_peak = 1.0
    cooldown = 0
    records: list[dict[str, object]] = []
    weight_records: list[pd.Series] = []
    cost_rate = config.one_way_cost_bps / 10_000

    for timestamp in index:
        base = requested_targets.loc[timestamp].copy()
        prior_drawdown = equity / risk_peak - 1.0
        state = "normal"
        if cooldown > 0:
            desired = pd.Series(0.0, index=config.all_assets)
            desired.loc[CASH_ASSET] = 1.0
            state = "hard-stop"
        elif prior_drawdown <= -config.soft_drawdown:
            desired = base.copy()
            non_cash = desired.index != CASH_ASSET
            desired.loc[non_cash] *= 0.5
            desired.loc[CASH_ASSET] = 1.0 - float(desired.loc[non_cash].sum())
            state = "de-risked"
        else:
            desired = base

        should_trade = last_requested is None or not np.allclose(
            desired.to_numpy(), last_requested.to_numpy(), atol=1e-10
        )
        turnover = float((desired - current).abs().sum()) if should_trade else 0.0
        if should_trade:
            current = desired.copy()
            last_requested = desired.copy()

        day_returns = asset_returns.loc[timestamp].reindex(config.all_assets).fillna(0.0)
        starting_weights = current.copy()
        gross_return = float(starting_weights @ day_returns)
        trading_cost = turnover * cost_rate
        net_return = gross_return - trading_cost
        equity *= 1.0 + net_return
        global_peak = max(global_peak, equity)
        risk_peak = max(risk_peak, equity)
        drawdown = equity / global_peak - 1.0
        control_drawdown = equity / risk_peak - 1.0

        ending_values = starting_weights * (1.0 + day_returns)
        denominator = float(ending_values.sum())
        current = ending_values / denominator if denominator > 0 else starting_weights

        if cooldown > 0:
            cooldown -= 1
            if cooldown == 0:
                risk_peak = equity
        elif control_drawdown <= -config.hard_drawdown:
            cooldown = config.cooldown_days

        records.append(
            {
                "date": timestamp,
                "gross_return": gross_return,
                "trading_cost": trading_cost,
                "net_return": net_return,
                "equity": equity,
                "drawdown": drawdown,
                "control_drawdown": control_drawdown,
                "turnover": turnover,
                "risk_state": state,
            }
        )
        starting_weights.name = timestamp
        weight_records.append(starting_weights)

    daily = pd.DataFrame(records).set_index("date")
    weights = pd.DataFrame(weight_records).reindex(columns=config.all_assets)
    return daily, weights


def _buy_and_hold(
    prices: pd.DataFrame, symbol: str, start: str
) -> tuple[pd.DataFrame, dict[str, float]]:
    returns = prices[symbol].pct_change(fill_method=None).fillna(0.0)
    returns = returns.loc[pd.Timestamp(start) :]
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    daily = pd.DataFrame(
        {
            "net_return": returns,
            "equity": equity,
            "drawdown": drawdown,
            "turnover": 0.0,
        }
    )
    return daily, performance_metrics(daily)


def run_backtest(
    prices: pd.DataFrame,
    config: StrategyConfig,
    evaluation_start: str | None = None,
) -> BacktestResult:
    targets, decisions = build_daily_targets(prices, config)
    start = evaluation_start or config.out_of_sample_start
    daily, weights = simulate(prices, targets, config, start=start)
    cash_returns = prices[CASH_ASSET].pct_change(fill_method=None).fillna(0.0)
    metrics = performance_metrics(daily, cash_returns)
    benchmark_daily, benchmark_metrics = _buy_and_hold(prices, "SPY", start)
    return BacktestResult(
        daily=daily,
        weights=weights,
        decisions=decisions,
        metrics=metrics,
        benchmark_daily=benchmark_daily,
        benchmark_metrics=benchmark_metrics,
    )
