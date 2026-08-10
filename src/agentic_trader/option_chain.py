from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


@dataclass
class OptionChainSnapshot:
    symbol: str
    spot: float
    expiration: str
    retrieved_at: str
    contracts: pd.DataFrame


def normalize_option_chain(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    symbol: str,
    spot: float,
    expiration: str,
    retrieved_at: datetime,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for kind, source in (("call", calls), ("put", puts)):
        frame = source.copy()
        frame["kind"] = kind
        frames.append(frame)
    contracts = pd.concat(frames, ignore_index=True)
    required = {
        "contractSymbol",
        "strike",
        "bid",
        "ask",
        "lastPrice",
        "impliedVolatility",
    }
    missing = required - set(contracts.columns)
    if missing:
        raise RuntimeError(f"Option chain is missing fields: {sorted(missing)}")

    numeric_columns = [
        "strike",
        "bid",
        "ask",
        "lastPrice",
        "impliedVolatility",
        "volume",
        "openInterest",
    ]
    for column in numeric_columns:
        if column in contracts:
            contracts[column] = pd.to_numeric(contracts[column], errors="coerce")

    contracts["underlying"] = symbol.upper()
    contracts["underlying_spot"] = float(spot)
    contracts["expiration"] = expiration
    contracts["retrieved_at"] = retrieved_at.isoformat()
    contracts["mid"] = (contracts["bid"] + contracts["ask"]) / 2
    contracts["spread"] = contracts["ask"] - contracts["bid"]
    contracts["spread_pct_mid"] = contracts["spread"] / contracts["mid"].where(
        contracts["mid"].gt(0)
    )
    call_intrinsic = (spot - contracts["strike"]).clip(lower=0)
    put_intrinsic = (contracts["strike"] - spot).clip(lower=0)
    contracts["intrinsic_value"] = call_intrinsic.where(contracts["kind"].eq("call"), put_intrinsic)
    contracts["extrinsic_value_mid"] = contracts["mid"] - contracts["intrinsic_value"]
    expiration_date = pd.Timestamp(expiration)
    contracts["days_to_expiry"] = max(0, (expiration_date.date() - retrieved_at.date()).days)
    if "lastTradeDate" in contracts:
        last_trade = pd.to_datetime(contracts["lastTradeDate"], utc=True, errors="coerce")
        contracts["last_trade_age_hours"] = (retrieved_at - last_trade).dt.total_seconds() / 3_600
    return contracts.sort_values(["kind", "strike"]).reset_index(drop=True)


def download_option_chain_snapshot(
    symbol: str, expiration: str | None = None
) -> OptionChainSnapshot:
    ticker = yf.Ticker(symbol)
    expirations = tuple(ticker.options)
    if not expirations:
        raise RuntimeError(f"No listed option expirations returned for {symbol}")
    selected = expiration or expirations[0]
    if selected not in expirations:
        raise ValueError(
            f"Expiration {selected} is unavailable; choose one of: {', '.join(expirations)}"
        )

    history = ticker.history(period="5d", auto_adjust=False)
    if history.empty or history["Close"].dropna().empty:
        raise RuntimeError(f"No current underlying price returned for {symbol}")
    spot = float(history["Close"].dropna().iloc[-1])
    retrieved_at = datetime.now(UTC)
    chain = ticker.option_chain(selected)
    contracts = normalize_option_chain(
        chain.calls,
        chain.puts,
        symbol,
        spot,
        selected,
        retrieved_at,
    )
    return OptionChainSnapshot(
        symbol=symbol.upper(),
        spot=spot,
        expiration=selected,
        retrieved_at=retrieved_at.isoformat(),
        contracts=contracts,
    )


def write_option_chain_snapshot(
    snapshot: OptionChainSnapshot, output_dir: str | Path
) -> dict[str, object]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot.contracts.to_csv(destination / "option-chain.csv", index=False)
    snapshot.contracts.to_parquet(destination / "option-chain.parquet", index=False)
    liquid = snapshot.contracts.loc[
        snapshot.contracts["bid"].gt(0)
        & snapshot.contracts["ask"].ge(snapshot.contracts["bid"])
        & snapshot.contracts.get("openInterest", pd.Series(index=snapshot.contracts.index))
        .fillna(0)
        .gt(0)
    ]
    report: dict[str, object] = {
        "symbol": snapshot.symbol,
        "spot": snapshot.spot,
        "expiration": snapshot.expiration,
        "retrieved_at": snapshot.retrieved_at,
        "contracts": len(snapshot.contracts),
        "contracts_with_positive_bid_and_open_interest": len(liquid),
        "median_spread_pct_mid": (
            float(liquid["spread_pct_mid"].median()) if not liquid.empty else None
        ),
        "warning": (
            "This is a current, delayed snapshot from Yahoo, not execution-quality "
            "NBBO data or a historical options backtest."
        ),
    }
    (destination / "option-chain-summary.json").write_text(json.dumps(report, indent=2))
    return report
