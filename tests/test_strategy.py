from __future__ import annotations

import numpy as np
import pandas as pd

from agentic_trader.config import CASH_ASSET, LARGE_CAPS, StrategyConfig
from agentic_trader.strategy import build_daily_targets, target_for_date


def synthetic_prices(config: StrategyConfig, periods: int = 700) -> pd.DataFrame:
    index = pd.bdate_range("2020-01-01", periods=periods)
    rng = np.random.default_rng(7)
    data: dict[str, np.ndarray] = {}
    for position, symbol in enumerate(config.all_assets):
        drift = 0.00015 + position * 0.00001
        shocks = rng.normal(drift, 0.006 + position * 0.0001, periods)
        data[symbol] = 100 * np.exp(np.cumsum(shocks))
    return pd.DataFrame(data, index=index)


def test_target_is_fully_invested_and_respects_stock_caps() -> None:
    config = StrategyConfig(include_stocks=True)
    prices = synthetic_prices(config)
    target, _ = target_for_date(prices, prices.index[-1], config)

    assert np.isclose(target.sum(), 1.0)
    assert (target >= 0).all()
    assert (target.drop(CASH_ASSET) <= config.max_asset_weight + 1e-12).all()
    assert (target.reindex(LARGE_CAPS).fillna(0) <= config.max_stock_weight + 1e-12).all()
    assert target.reindex(LARGE_CAPS).fillna(0).sum() <= config.max_stock_sleeve + 1e-12


def test_close_signal_does_not_earn_unavailable_next_day_return() -> None:
    config = StrategyConfig()
    prices = synthetic_prices(config)
    daily, _ = build_daily_targets(prices, config)
    month_end = prices.groupby(prices.index.to_period("M")).tail(1).index[-2]
    next_day = prices.index[prices.index.get_loc(month_end) + 1]
    execution_close = prices.index[prices.index.get_loc(month_end) + 2]
    expected, _ = target_for_date(prices, month_end, config)

    pd.testing.assert_series_equal(
        daily.loc[next_day], daily.loc[month_end], check_names=False, atol=1e-12
    )
    pd.testing.assert_series_equal(
        daily.loc[execution_close], expected, check_names=False, atol=1e-12
    )


def test_future_prices_do_not_change_historical_signal() -> None:
    config = StrategyConfig()
    prices = synthetic_prices(config)
    as_of = prices.index[-40]
    original, _ = target_for_date(prices, as_of, config)
    altered = prices.copy()
    altered.loc[altered.index > as_of] *= 10
    after_future_change, _ = target_for_date(altered, as_of, config)

    pd.testing.assert_series_equal(original, after_future_change)
