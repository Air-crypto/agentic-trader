from __future__ import annotations

from math import sqrt

import numpy as np
import pandas as pd

from .config import CASH_ASSET, DEFENSIVE_ASSETS, LARGE_CAPS, StrategyConfig


def _lagged_return(series: pd.Series, lookback: int, skip: int) -> float:
    clean = series.dropna()
    if len(clean) <= lookback:
        return float("nan")
    return float(clean.iloc[-(skip + 1)] / clean.iloc[-(lookback + 1)] - 1.0)


def score_assets(prices: pd.DataFrame, as_of: pd.Timestamp, config: StrategyConfig) -> pd.DataFrame:
    """Create a point-in-time signal table using only data available at ``as_of``."""
    history = prices.loc[:as_of]
    cash_momentum = _lagged_return(history[CASH_ASSET], config.long_momentum_days, config.skip_days)
    spy = history["SPY"].dropna()
    spy_trend = (
        float(spy.iloc[-1] / spy.tail(config.trend_days).mean() - 1.0)
        if len(spy) >= config.trend_days
        else float("nan")
    )

    rows: list[dict[str, object]] = []
    candidates = config.risk_assets + DEFENSIVE_ASSETS
    for symbol in candidates:
        series = history[symbol].dropna()
        enough_history = len(series) > config.long_momentum_days
        if not enough_history:
            rows.append(
                {
                    "symbol": symbol,
                    "kind": "stock" if symbol in LARGE_CAPS else "fund",
                    "score": np.nan,
                    "long_momentum": np.nan,
                    "trend": np.nan,
                    "volatility": np.nan,
                    "eligible": False,
                }
            )
            continue

        short_momentum = _lagged_return(series, config.short_momentum_days, config.skip_days)
        long_momentum = _lagged_return(series, config.long_momentum_days, config.skip_days)
        trend = float(series.iloc[-1] / series.tail(config.trend_days).mean() - 1.0)
        volatility = float(
            series.pct_change(fill_method=None).tail(config.volatility_days).std(ddof=1) * sqrt(252)
        )
        kind = "stock" if symbol in LARGE_CAPS else "fund"
        is_defensive = symbol in DEFENSIVE_ASSETS
        eligible = bool(
            np.isfinite(cash_momentum)
            and np.isfinite(volatility)
            and volatility > 0
            and long_momentum > cash_momentum
            and (is_defensive or trend > 0)
            and (kind != "stock" or spy_trend > 0)
        )
        rows.append(
            {
                "symbol": symbol,
                "kind": kind,
                "score": 0.5 * short_momentum + 0.5 * long_momentum,
                "long_momentum": long_momentum,
                "trend": trend,
                "volatility": volatility,
                "eligible": eligible,
            }
        )

    return pd.DataFrame(rows).set_index("symbol")


def _capped_weights(
    selected: pd.DataFrame, returns: pd.DataFrame, config: StrategyConfig
) -> pd.Series:
    inverse_volatility = 1.0 / selected["volatility"].astype(float)
    weights = inverse_volatility / inverse_volatility.sum()

    caps = pd.Series(config.max_asset_weight, index=weights.index)
    stock_mask = selected["kind"].eq("stock")
    caps.loc[stock_mask] = np.minimum(caps.loc[stock_mask], config.max_stock_weight)
    weights = weights.clip(upper=caps)

    stock_total = float(weights.loc[stock_mask].sum())
    if stock_total > config.max_stock_sleeve:
        weights.loc[stock_mask] *= config.max_stock_sleeve / stock_total

    covariance = returns.reindex(columns=weights.index).tail(config.volatility_days)
    covariance = covariance.cov() * 252
    if covariance.notna().all().all():
        portfolio_variance = float(weights @ covariance @ weights)
        if portfolio_variance > 0:
            scale = min(1.0, config.target_volatility / sqrt(portfolio_variance))
            weights *= scale
    return weights


def target_for_date(
    prices: pd.DataFrame, as_of: pd.Timestamp, config: StrategyConfig
) -> tuple[pd.Series, pd.DataFrame]:
    scores = score_assets(prices, as_of, config)
    eligible = scores.loc[scores["eligible"]].sort_values("score", ascending=False)
    risk_symbols = [symbol for symbol in eligible.index if symbol in config.risk_assets]
    selected_symbols = risk_symbols[: config.top_n]

    if len(selected_symbols) < config.top_n:
        defensive = [
            symbol
            for symbol in eligible.index
            if symbol in DEFENSIVE_ASSETS and symbol not in selected_symbols
        ]
        selected_symbols.extend(defensive[: config.top_n - len(selected_symbols)])

    target = pd.Series(0.0, index=config.all_assets, dtype=float)
    if selected_symbols:
        selected = scores.loc[selected_symbols]
        returns = prices.loc[:as_of].pct_change(fill_method=None)
        target.loc[selected_symbols] = _capped_weights(selected, returns, config)
    target.loc[CASH_ASSET] = max(0.0, 1.0 - float(target.sum()))

    scores = scores.assign(selected=scores.index.isin(selected_symbols))
    return target, scores


def build_daily_targets(
    prices: pd.DataFrame, config: StrategyConfig
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build month-end targets with a conservative close-to-close execution lag."""
    month_ends = prices.groupby(prices.index.to_period("M")).tail(1).index
    targets: dict[pd.Timestamp, pd.Series] = {}
    decisions: list[pd.DataFrame] = []
    for rebalance_date in month_ends:
        if prices.loc[:rebalance_date].shape[0] <= config.long_momentum_days:
            continue
        target, scores = target_for_date(prices, rebalance_date, config)
        targets[rebalance_date] = target
        decisions.append(scores.reset_index().assign(rebalance_date=rebalance_date))

    if not targets:
        raise ValueError("Price history is too short to produce a signal")

    monthly = pd.DataFrame(targets).T.reindex(columns=config.all_assets)
    daily = monthly.reindex(prices.index).ffill().shift(config.signal_lag_trading_days).fillna(0.0)
    no_position = daily.sum(axis=1).eq(0.0)
    daily.loc[no_position, CASH_ASSET] = 1.0
    decision_frame = pd.concat(decisions, ignore_index=True)
    return daily, decision_frame
