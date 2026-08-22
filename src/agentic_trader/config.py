from __future__ import annotations

from dataclasses import dataclass
from datetime import date

RISK_ETFS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC")
LARGE_CAPS = ("AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "JPM", "UNH", "XOM", "WMT")
DEFENSIVE_ASSETS = ("IEF", "TLT", "GLD")
CASH_ASSET = "BIL"


@dataclass(frozen=True)
class StrategyConfig:
    start: str = "2010-01-01"
    out_of_sample_start: str = "2015-01-01"
    end: str | None = None
    initial_capital: float = 5_000.0
    include_stocks: bool = False
    top_n: int = 4
    short_momentum_days: int = 126
    long_momentum_days: int = 252
    skip_days: int = 21
    trend_days: int = 200
    volatility_days: int = 63
    target_volatility: float = 0.08
    max_asset_weight: float = 0.35
    max_stock_weight: float = 0.15
    max_stock_sleeve: float = 0.30
    soft_drawdown: float = 0.08
    hard_drawdown: float = 0.10
    cooldown_days: int = 21
    one_way_cost_bps: float = 10.0
    # Close-only data cannot model a next-open fill. Two close rows ensure the
    # signal does not earn the decision-close to next-close return interval.
    signal_lag_trading_days: int = 2

    @property
    def risk_assets(self) -> tuple[str, ...]:
        return RISK_ETFS + (LARGE_CAPS if self.include_stocks else ())

    @property
    def all_assets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.risk_assets + DEFENSIVE_ASSETS + (CASH_ASSET,)))

    @property
    def resolved_end(self) -> str:
        return self.end or date.today().isoformat()
