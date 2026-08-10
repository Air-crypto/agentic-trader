from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf


def _cache_path(tickers: tuple[str, ...], start: str, end: str, cache_dir: Path) -> Path:
    key = "|".join((*tickers, start, end))
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return cache_dir / f"adjusted-close-{digest}.parquet"


def download_adjusted_close(
    tickers: Iterable[str],
    start: str,
    end: str | None = None,
    cache_dir: str | Path = ".cache/market-data",
    refresh: bool = False,
) -> pd.DataFrame:
    """Download split- and dividend-adjusted daily closes with a local cache."""
    symbols = tuple(dict.fromkeys(tickers))
    if not symbols:
        raise ValueError("At least one ticker is required")

    final_end = end or date.today().isoformat()
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(symbols, start, final_end, cache_root)
    if cached.exists() and not refresh:
        return pd.read_parquet(cached)

    # yfinance treats end as exclusive, so include the requested final date.
    exclusive_end = (date.fromisoformat(final_end) + timedelta(days=1)).isoformat()
    raw = yf.download(
        list(symbols),
        start=start,
        end=exclusive_end,
        auto_adjust=True,
        actions=False,
        progress=False,
        threads=False,
        timeout=30,
    )
    if raw.empty:
        raise RuntimeError("The market-data download returned no rows")

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            prices = raw["Close"]
        elif "Close" in raw.columns.get_level_values(1):
            prices = raw.xs("Close", axis=1, level=1)
        else:
            raise RuntimeError("Could not find adjusted close data in the download")
    else:
        if "Close" not in raw:
            raise RuntimeError("Could not find adjusted close data in the download")
        prices = raw[["Close"]].rename(columns={"Close": symbols[0]})

    prices = prices.reindex(columns=symbols)
    prices.index = pd.DatetimeIndex(prices.index).tz_localize(None)
    prices = prices.sort_index().astype(float).ffill(limit=3).dropna(how="all")
    missing = [symbol for symbol in symbols if prices[symbol].count() < 253]
    if missing:
        raise RuntimeError(f"Insufficient history for: {', '.join(missing)}")

    prices.to_parquet(cached)
    return prices
