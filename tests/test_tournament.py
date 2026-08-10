from __future__ import annotations

import numpy as np
import pandas as pd

from agentic_trader.config import StrategyConfig
from agentic_trader.tournament import STRATEGIES, run_tournament


def _prices(config: StrategyConfig, periods: int = 1_100) -> pd.DataFrame:
    index = pd.bdate_range("2017-01-02", periods=periods)
    rng = np.random.default_rng(11)
    market = rng.normal(0.0003, 0.006, periods)
    values: dict[str, np.ndarray] = {}
    for offset, symbol in enumerate(config.all_assets):
        idiosyncratic = rng.normal(0, 0.002 + offset * 0.00005, periods)
        values[symbol] = 100 * np.exp(np.cumsum(market + idiosyncratic))
    return pd.DataFrame(values, index=index)


def test_all_tournament_targets_are_long_only_and_fully_invested() -> None:
    config = StrategyConfig(start="2017-01-01")
    prices = _prices(config)

    for builder in STRATEGIES.values():
        targets = builder(prices, config)
        assert np.allclose(targets.sum(axis=1), 1.0)
        assert targets.ge(0).all().all()


def test_tournament_selects_using_development_and_writes_report(tmp_path) -> None:
    config = StrategyConfig(start="2017-01-01")
    prices = _prices(config)

    report = run_tournament(
        prices,
        config,
        development_start="2018-01-01",
        holdout_start="2020-01-01",
        output_dir=tmp_path,
    )

    assert report["selected_strategy"] in STRATEGIES
    assert (tmp_path / "strategy-metrics.csv").exists()
    assert (tmp_path / "summary.json").exists()
