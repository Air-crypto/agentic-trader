from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .models import ActiveThesis


@dataclass(frozen=True)
class InvalidationResult:
    invalidated: bool
    reason: str | None
    raw_return: float
    spy_relative_return: float


def trading_day_expiry(entry_date: date, horizon_trading_days: int) -> date:
    if not 1 <= horizon_trading_days <= 60:
        raise ValueError("Horizon must be between 1 and 60 trading days")
    return (pd.Timestamp(entry_date) + pd.offsets.BDay(horizon_trading_days)).date()


def evaluate_invalidation(
    thesis: ActiveThesis,
    current_price: float,
    spy_price: float,
    as_of: date,
) -> InvalidationResult:
    if current_price <= 0 or spy_price <= 0:
        raise ValueError("Current prices must be positive")
    raw_return = current_price / thesis.entry_price - 1.0
    spy_return = spy_price / thesis.entry_spy_price - 1.0
    relative = raw_return - spy_return
    if as_of >= thesis.expiry_date:
        return InvalidationResult(True, "horizon_expired", raw_return, relative)
    if raw_return <= -thesis.stop_loss_pct:
        return InvalidationResult(True, "stop_loss", raw_return, relative)
    if relative <= -thesis.sector_relative_stop_pct:
        return InvalidationResult(True, "spy_relative_stop", raw_return, relative)
    return InvalidationResult(False, None, raw_return, relative)
