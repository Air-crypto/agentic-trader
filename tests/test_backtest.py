from __future__ import annotations

import numpy as np
import pandas as pd

from agentic_trader.backtest import simulate
from agentic_trader.config import StrategyConfig


def _flat_prices(config: StrategyConfig, index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(100.0, index=index, columns=config.all_assets)


def _spy_targets(config: StrategyConfig, index: pd.DatetimeIndex) -> pd.DataFrame:
    targets = pd.DataFrame(0.0, index=index, columns=config.all_assets)
    targets["SPY"] = 1.0
    return targets


def test_one_way_cost_is_charged_on_each_traded_leg() -> None:
    config = StrategyConfig(one_way_cost_bps=10.0)
    index = pd.bdate_range("2025-01-02", periods=2)
    prices = _flat_prices(config, index)
    targets = _spy_targets(config, index)

    daily, _ = simulate(prices, targets, config)

    assert np.isclose(daily.iloc[0]["turnover"], 2.0)
    assert np.isclose(daily.iloc[0]["trading_cost"], 0.002)
    assert np.isclose(daily.iloc[0]["net_return"], -0.002)


def test_reported_drawdown_does_not_reset_after_cooldown() -> None:
    config = StrategyConfig(
        one_way_cost_bps=0.0,
        soft_drawdown=0.04,
        hard_drawdown=0.05,
        cooldown_days=2,
    )
    index = pd.bdate_range("2025-01-02", periods=7)
    prices = _flat_prices(config, index)
    prices["SPY"] = [100.0, 94.0, 94.0, 94.0, 94.0, 88.36, 88.36]
    targets = _spy_targets(config, index)

    daily, _ = simulate(prices, targets, config)

    assert daily["control_drawdown"].min() > -0.07
    assert daily["drawdown"].min() < -0.11
