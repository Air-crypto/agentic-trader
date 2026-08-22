"""Deterministic pre-trade guard for real-money orders.

Nothing here touches the network or places an order. The guard turns account
state plus a proposed order into an approve/reject decision so that the limits
are enforced by code rather than by an instruction an agent may ignore.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from math import isfinite
from pathlib import Path
from typing import Any

# The permitted account is read from the environment rather than committed,
# because this repository is public and an account number is a useful handle for
# social engineering. Unset means no account is tradable, so the guard fails
# closed instead of falling back to a default.
ACCOUNT_ENV_VAR = "AGENTIC_TRADER_ACCOUNT"

# Total capital contributed to the account. Configuring this gives a loss limit
# that needs no persistence, so the guard stays protected in a fresh checkout.
NET_DEPOSITS_ENV_VAR = "AGENTIC_TRADER_NET_DEPOSITS"


def agentic_account_number() -> str | None:
    value = os.environ.get(ACCOUNT_ENV_VAR, "").strip()
    return value or None


def configured_net_deposits() -> float | None:
    value = os.environ.get(NET_DEPOSITS_ENV_VAR, "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def _finite_float(value: Any) -> float | None:
    """Return a finite float, never a NaN/Inf value that bypasses comparisons."""
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _utc_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


# Restricted to the instruments the tournament actually measured. Anything the
# backtest never priced cannot be sized by a backtest-derived rule.
DEFAULT_SYMBOL_ALLOWLIST = (
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "VNQ",
    "DBC",
    "IEF",
    "TLT",
    "GLD",
    "BIL",
)

# Concentration risk differs by instrument. A broad index fund at 50% is a
# normal allocation; a single name at 50% is a bet. Cash equivalents are not a
# risk position at all, so parking uninvested money in BIL stays uncapped.
CASH_EQUIVALENTS = ("BIL",)
BROAD_MARKET_FUNDS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT", "GLD")

KILL_SWITCH_FILENAME = "KILL_SWITCH"
LIVE_STATE_FILENAME = "artifacts/live/state.json"
SESSION_LOCK_FILENAME = "artifacts/live/session.lock"

# A duplicate automation trigger would otherwise let two runs each observe zero
# orders placed today and each submit the full plan, doubling the position.
SESSION_LOCK_TTL_SECONDS = 1_800


class SessionLockedError(RuntimeError):
    """Raised when another live session already holds the lock."""


@contextmanager
def session_lock(
    root: str | Path = ".", ttl_seconds: int = SESSION_LOCK_TTL_SECONDS
) -> Iterator[Path]:
    """Serialize live sessions so a duplicate trigger cannot double-trade."""
    path = Path(root) / SESSION_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age < ttl_seconds:
            raise SessionLockedError(
                f"Another live session has held {path} for {age:.0f}s. "
                "Refusing to run concurrently."
            ) from None
        # A crashed run would otherwise block trading forever.
        path.unlink(missing_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(
            descriptor, f"pid={os.getpid()} acquired={datetime.now(UTC).isoformat()}\n".encode()
        )
        os.close(descriptor)
        yield path
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ExecutionLimits:
    """Hard caps expressed in account terms rather than percentages of a guess."""

    max_order_notional: float = 150.0
    max_position_weight: float = 0.035
    max_broad_market_weight: float = 0.035
    # These whole-portfolio caps apply before any new exposure is allowed. They
    # deliberately include legacy/manual holdings so an oversized broker
    # portfolio cannot keep adding risk one otherwise-small order at a time.
    max_held_names: int = 3
    max_global_position_weight: float = 0.035
    max_sector_weight: float = 0.07
    min_cash_reserve_weight: float = 0.895
    max_orders_per_day: int = 8
    max_daily_notional: float = 800.0
    max_entry_orders_per_day: int = 2
    max_entry_daily_notional: float = 300.0
    max_daily_loss_weight: float = 0.005
    max_drawdown_weight: float = 0.03
    max_loss_from_deposits_weight: float = 0.03
    min_order_notional: float = 25.0
    allow_fractional: bool = True
    require_broker_order_counts: bool = True
    require_fresh_quotes: bool = True
    max_quote_age_seconds: int = 15
    max_extended_spread_bps: float = 10.0
    allow_extended_hours: bool = False
    symbol_allowlist: tuple[str, ...] = DEFAULT_SYMBOL_ALLOWLIST
    buy_symbol_allowlist: tuple[str, ...] | None = None
    sell_symbol_allowlist: tuple[str, ...] | None = None

    def position_cap_for(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol in CASH_EQUIVALENTS:
            return 1.0
        if symbol in BROAD_MARKET_FUNDS:
            return self.max_broad_market_weight
        return self.max_position_weight

    def symbol_allowed(self, symbol: str, side: str) -> bool:
        allowed = (
            self.buy_symbol_allowlist
            if side == "buy"
            else self.sell_symbol_allowlist
            if side == "sell"
            else None
        )
        return symbol in (allowed if allowed is not None else self.symbol_allowlist)

    def __post_init__(self) -> None:
        finite_fields = {
            "max_order_notional": self.max_order_notional,
            "max_position_weight": self.max_position_weight,
            "max_broad_market_weight": self.max_broad_market_weight,
            "max_held_names": self.max_held_names,
            "max_global_position_weight": self.max_global_position_weight,
            "max_sector_weight": self.max_sector_weight,
            "min_cash_reserve_weight": self.min_cash_reserve_weight,
            "max_orders_per_day": self.max_orders_per_day,
            "max_daily_notional": self.max_daily_notional,
            "max_entry_orders_per_day": self.max_entry_orders_per_day,
            "max_entry_daily_notional": self.max_entry_daily_notional,
            "max_daily_loss_weight": self.max_daily_loss_weight,
            "max_drawdown_weight": self.max_drawdown_weight,
            "max_loss_from_deposits_weight": self.max_loss_from_deposits_weight,
            "min_order_notional": self.min_order_notional,
            "max_extended_spread_bps": self.max_extended_spread_bps,
        }
        invalid = [name for name, value in finite_fields.items() if _finite_float(value) is None]
        if invalid:
            raise ValueError(f"Execution limits must be finite: {', '.join(invalid)}")
        if (
            not isfinite(self.max_order_notional)
            or self.max_order_notional <= 0
            or self.max_order_notional > 150
        ):
            raise ValueError("max_order_notional cannot relax the $150 hard cap")
        if self.min_order_notional > self.max_order_notional:
            raise ValueError("min_order_notional cannot exceed max_order_notional")
        if not 0 < self.max_position_weight <= 1:
            raise ValueError("max_position_weight must be within (0, 1]")
        if not 0 < self.max_broad_market_weight <= 1:
            raise ValueError("max_broad_market_weight must be within (0, 1]")
        if (
            isinstance(self.max_held_names, bool)
            or not isinstance(self.max_held_names, int)
            or not 0 < self.max_held_names <= 3
        ):
            raise ValueError("max_held_names cannot relax the 3-name hard cap")
        if not 0 < self.max_global_position_weight <= 0.035:
            raise ValueError("max_global_position_weight cannot relax the 3.5% hard cap")
        if not 0 < self.max_sector_weight <= 0.07:
            raise ValueError("max_sector_weight cannot relax the 7% hard cap")
        if not 0 <= self.min_cash_reserve_weight < 1:
            raise ValueError("min_cash_reserve_weight must be within [0, 1)")
        if not 0 < self.max_orders_per_day <= 8:
            raise ValueError("max_orders_per_day cannot relax the 8-order hard cap")
        if not 0 < self.max_daily_notional <= 800:
            raise ValueError("max_daily_notional cannot relax the $800 hard cap")
        if not 0 < self.max_entry_orders_per_day <= min(self.max_orders_per_day, 2):
            raise ValueError("entry orders cannot exceed 2 or the total cap")
        if not 0 < self.max_entry_daily_notional <= min(self.max_daily_notional, 300):
            raise ValueError("entry notional cannot exceed $300 or the total cap")
        if not 0 < self.max_daily_loss_weight <= 1:
            raise ValueError("max_daily_loss_weight must be within (0, 1]")
        if not 0 < self.max_drawdown_weight <= 1:
            raise ValueError("max_drawdown_weight must be within (0, 1]")
        if not 0 < self.max_loss_from_deposits_weight <= 1:
            raise ValueError("max_loss_from_deposits_weight must be within (0, 1]")
        if self.min_order_notional <= 0:
            raise ValueError("min_order_notional must be positive")
        if not 0 < self.max_quote_age_seconds <= 60:
            raise ValueError("max_quote_age_seconds cannot exceed 60")
        if not 0 < self.max_extended_spread_bps <= 10:
            raise ValueError("max_extended_spread_bps cannot relax the 10 bps hard cap")


@dataclass(frozen=True)
class AccountSnapshot:
    """Point-in-time broker state. Supplied by the caller, never fetched here."""

    account_number: str
    equity: float
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
    # Point-in-time sector metadata for both current holdings and proposed
    # entries. A missing, blank, or explicitly unknown label fails new exposure
    # closed because the aggregate sector cap otherwise cannot be calculated.
    sector_by_symbol: dict[str, str] = field(default_factory=dict)
    high_water_mark: float | None = None
    prior_close_equity: float | None = None
    orders_today: int = 0
    notional_today: float = 0.0
    pending_deposits: float = 0.0
    net_deposits: float | None = None
    # Where orders_today came from. Only "broker" is trustworthy under a
    # duplicate trigger, because a concurrent run's orders exist at the broker
    # before either run has written anything locally.
    orders_source: str = "unknown"
    # Fractional and dollar-based orders are rejected by the broker outside
    # 9:30-16:00 ET, so planning them then produces orders that cannot be filled.
    session_is_regular: bool = False
    market_hours: str = "closed"
    session_tradable_symbols: tuple[str, ...] = ()
    quote_timestamps: dict[str, datetime | str] = field(default_factory=dict)
    quote_spreads_bps: dict[str, float] = field(default_factory=dict)
    # The paired Robinhood MCP can attest account eligibility without copying
    # the account number into another plaintext environment variable.
    broker_identity_verified: bool = False
    external_halt_reasons: tuple[str, ...] = ()
    # Entry/rebalance usage is tracked separately so mandatory exits can use
    # reserved capacity. None means the historical intent is unknown; treating
    # all prior usage as entry activity fails closed without weakening the
    # broker-verified hard total.
    entry_orders_today: int | None = None
    entry_notional_today: float | None = None

    @property
    def settled_equity(self) -> float:
        """Equity excluding deposits that have not cleared yet."""
        return self.equity - self.pending_deposits

    def position_value(self, symbol: str) -> float:
        return float(self.positions.get(symbol.upper(), 0.0))

    def sector_for(self, symbol: str) -> str | None:
        normalized_symbol = symbol.strip().upper()
        raw_sector = next(
            (
                value
                for raw_symbol, value in self.sector_by_symbol.items()
                if str(raw_symbol).strip().upper() == normalized_symbol
            ),
            None,
        )
        if not isinstance(raw_sector, str):
            return None
        sector = " ".join(raw_sector.split())
        if sector.casefold() in {"", "unknown", "unclassified", "n/a", "na", "none", "null"}:
            return None
        # Sector aggregation must not be bypassed by harmless differences in
        # capitalization or repeated whitespace.
        return sector.casefold()

    @property
    def effective_entry_orders_today(self) -> int:
        if self.entry_orders_today is None:
            return self.orders_today
        return self.entry_orders_today

    @property
    def effective_entry_notional_today(self) -> float:
        if self.entry_notional_today is None:
            return self.notional_today
        return self.entry_notional_today


def _broker_number(value: Any) -> float | None:
    """Parse a numeric broker field without guessing missing values."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    return _finite_float(value)


def broker_position_values(
    positions: list[dict[str, Any]],
    prices: dict[str, float],
) -> dict[str, float]:
    """Convert Robinhood position quantities to current market values.

    The broker returns quantities while the planner reasons in dollars. Keeping
    this conversion in deterministic code avoids asking the automation to invent
    a request shape after the first live orders create non-empty positions.
    """
    normalized_prices = {symbol.upper(): _finite_float(price) for symbol, price in prices.items()}
    values: dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        quantity = _broker_number(position.get("quantity"))
        price = normalized_prices.get(symbol)
        if not symbol or quantity is None or quantity < 0:
            raise ValueError("Broker position is missing a valid symbol or quantity")
        if price is None or price <= 0:
            raise ValueError(f"Missing a positive current price for position {symbol}")
        position_value = quantity * price
        if not isfinite(position_value):
            raise ValueError(f"Position value for {symbol} must be finite")
        values[symbol] = values.get(symbol, 0.0) + position_value
        if not isfinite(values[symbol]):
            raise ValueError(f"Aggregate position value for {symbol} must be finite")
    return values


def summarize_broker_orders(orders: list[dict[str, Any]]) -> tuple[int, float]:
    """Count broker orders and conservatively recover their submitted notional."""
    total = 0.0
    for order in orders:
        notional = _broker_number(order.get("dollar_based_amount"))
        if notional is None:
            quantity = _broker_number(order.get("quantity"))
            if quantity is None:
                quantity = _broker_number(order.get("cumulative_quantity"))
            price = _broker_number(order.get("price"))
            if price is None:
                price = _broker_number(order.get("limit_price"))
            if price is None:
                price = _broker_number(order.get("average_price"))
            if quantity is None or price is None:
                raise ValueError(
                    "Cannot determine broker-order notional; refusing to undercount daily usage"
                )
            notional = quantity * price
        if notional < 0:
            raise ValueError("Broker-order notional cannot be negative")
        total += notional
        if not isfinite(total):
            raise ValueError("Broker-order notional total must be finite")
    return len(orders), total


@dataclass(frozen=True)
class ProposedOrder:
    """A broker-ready order.

    Robinhood only accepts a dollar notional as a market order, and only accepts
    fractional quantities as a market order during regular hours. An account too
    small to buy one whole share therefore cannot use a limit order at all, so
    the form is chosen by what the broker will accept rather than by preference.
    """

    symbol: str
    side: str
    notional: float
    order_type: str = "limit"
    limit_price: float | None = None
    quantity: float | None = None
    # For a market order this is not an instruction to the broker; it is the
    # price reconciliation will measure the fill against.
    reference_price: float | None = None
    rationale: str = ""
    pick_id: str = ""
    intent_class: str = "rebalance"
    exit_reason: str | None = None
    market_hours: str = "regular_hours"
    quote_timestamp: datetime | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", self.side.lower())
        object.__setattr__(self, "order_type", self.order_type.lower())
        object.__setattr__(self, "intent_class", self.intent_class.strip().lower())
        object.__setattr__(self, "market_hours", self.market_hours.strip().lower())

    @property
    def is_fractional(self) -> bool:
        return self.order_type == "market"

    @property
    def uses_exit_reserve(self) -> bool:
        """Whether this order may consume capacity reserved for urgent exits."""
        return self.side == "sell" and self.intent_class in {"mandatory_exit", "close"}

    def broker_parameters(self) -> dict[str, Any]:
        """Exactly the arguments place_equity_order should receive."""
        common = {
            "symbol": self.symbol,
            "side": self.side,
            "market_hours": self.market_hours,
            "time_in_force": "gfd",
        }
        if self.order_type == "market":
            return {**common, "type": "market", "dollar_amount": f"{self.notional:.2f}"}
        return {
            **common,
            "type": "limit",
            "quantity": f"{self.quantity:g}",
            "limit_price": f"{self.limit_price:.2f}",
        }

    def broker_notional(self) -> float:
        """Return the exact notional encoded by :meth:`broker_parameters`."""
        parameters = self.broker_parameters()
        if parameters["type"] == "market":
            return float(Decimal(parameters["dollar_amount"]))
        return float(Decimal(parameters["quantity"]) * Decimal(parameters["limit_price"]))


@dataclass(frozen=True)
class Decision:
    order: ProposedOrder
    approved: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "symbol": self.order.symbol,
            "side": self.order.side,
            "notional": round(self.order.notional, 2),
            "order_type": self.order.order_type,
            "limit_price": self.order.limit_price,
            "quantity": self.order.quantity,
            "reference_price": self.order.reference_price,
            "pick_id": self.order.pick_id,
            "intent_class": self.order.intent_class,
            "exit_reason": self.order.exit_reason,
            "approved": self.approved,
            "reasons": list(self.reasons),
        }
        if self.approved:
            payload["broker_parameters"] = self.order.broker_parameters()
        return payload


def kill_switch_engaged(root: str | Path = ".") -> bool:
    return (Path(root) / KILL_SWITCH_FILENAME).exists()


def check_account_halts(
    account: AccountSnapshot,
    limits: ExecutionLimits,
    root: str | Path = ".",
) -> tuple[str, ...]:
    """Account-level conditions that block every order, not just one."""
    reasons: list[str] = []
    reasons.extend(account.external_halt_reasons)

    required_numbers = {
        "equity": account.equity,
        "cash": account.cash,
        "orders_today": account.orders_today,
        "notional_today": account.notional_today,
        "pending_deposits": account.pending_deposits,
    }
    for name, value in required_numbers.items():
        if _finite_float(value) is None:
            reasons.append(f"non_finite_{name}")
    if account.entry_orders_today is not None and _finite_float(account.entry_orders_today) is None:
        reasons.append("non_finite_entry_orders_today")
    if (
        account.entry_notional_today is not None
        and _finite_float(account.entry_notional_today) is None
    ):
        reasons.append("non_finite_entry_notional_today")
    if any(_finite_float(value) is None for value in account.positions.values()):
        reasons.append("non_finite_position_value")

    if kill_switch_engaged(root):
        reasons.append("kill_switch_file_present")
    permitted = agentic_account_number()
    if permitted is None and not account.broker_identity_verified:
        reasons.append("agentic_account_not_configured")
    elif permitted is not None and account.account_number != permitted:
        reasons.append("account_is_not_the_agentic_account")
    equity = _finite_float(account.equity)
    orders_today = _finite_float(account.orders_today)
    notional_today = _finite_float(account.notional_today)
    if equity is not None and equity <= 0:
        reasons.append("non_positive_equity")
    if orders_today is not None and orders_today >= limits.max_orders_per_day:
        reasons.append("daily_order_count_limit_reached")
    if notional_today is not None and notional_today >= limits.max_daily_notional:
        reasons.append("daily_notional_limit_reached")
    if limits.require_broker_order_counts and account.orders_source != "broker":
        reasons.append("daily_order_count_not_broker_verified")
    supported_extended_session = limits.allow_extended_hours and account.market_hours in {
        "extended_hours",
        "all_day_hours",
    }
    if account.session_is_regular is not True and not supported_extended_session:
        reasons.append("outside_regular_trading_session")

    # Stateless and therefore the one drawdown protection that still works in an
    # environment with no persistence, such as a fresh cloud checkout.
    configured_raw = os.environ.get(NET_DEPOSITS_ENV_VAR, "").strip()
    configured_deposits = configured_net_deposits()
    if configured_raw:
        # Configuration is authoritative. A request cannot weaken the capital
        # floor by supplying a different deposit base.
        net_deposits = configured_deposits
        if configured_deposits is None:
            reasons.append("configured_net_deposits_invalid")
        elif account.net_deposits is not None:
            request_deposits = _finite_float(account.net_deposits)
            if request_deposits is None:
                reasons.append("non_finite_net_deposits")
            elif request_deposits != configured_deposits:
                reasons.append("net_deposits_mismatch")
    else:
        net_deposits = _finite_float(account.net_deposits)
        if account.net_deposits is not None and net_deposits is None:
            reasons.append("non_finite_net_deposits")
        elif net_deposits is not None and net_deposits <= 0:
            reasons.append("non_positive_net_deposits")
            net_deposits = None
    if net_deposits is not None and net_deposits > 0:
        floor = net_deposits * (1.0 - limits.max_loss_from_deposits_weight)
        if equity is not None and equity < floor:
            reasons.append("capital_floor_breached")

    high_water_mark = account.high_water_mark
    high_water_value = _finite_float(high_water_mark)
    if high_water_mark is not None and high_water_value is None:
        reasons.append("non_finite_high_water_mark")
    elif high_water_value is not None and high_water_value > 0 and equity is not None:
        drawdown = 1.0 - (equity / high_water_value)
        if drawdown >= limits.max_drawdown_weight:
            reasons.append("max_drawdown_halt")

    # With neither a persisted peak nor a known deposit base there is no loss
    # limit of any kind, which is the exact situation a fresh cloud checkout
    # produces. Refuse rather than trade unprotected.
    if high_water_mark is None and not net_deposits:
        reasons.append("no_drawdown_protection_available")

    prior_close = account.prior_close_equity
    prior_close_value = _finite_float(prior_close)
    if prior_close is None:
        # The daily-loss circuit breaker cannot operate without its official
        # prior-session anchor.  Treat that as an entry halt rather than
        # silently trading with one of the advertised hard limits disabled.
        reasons.append("prior_close_equity_missing")
    elif prior_close_value is None:
        reasons.append("non_finite_prior_close_equity")
    elif prior_close_value <= 0:
        reasons.append("non_positive_prior_close_equity")
    elif equity is not None:
        daily_loss = 1.0 - (equity / prior_close_value)
        if daily_loss >= limits.max_daily_loss_weight:
            reasons.append("daily_loss_halt")

    return tuple(reasons)


def _entry_portfolio_envelope_reasons(
    order: ProposedOrder,
    account: AccountSnapshot,
    limits: ExecutionLimits,
    notional: float,
) -> tuple[str, ...]:
    """Validate the complete projected risky portfolio before a buy.

    The broker snapshot, rather than the proposed target set, defines existing
    exposure. Cash equivalents are excluded consistently with ``position_cap_for``.
    Every other positive position counts, including broad-market and sector ETFs.
    """
    if order.side != "buy" or notional <= 0:
        return ()
    equity = _finite_float(account.equity)
    if equity is None or equity <= 0:
        return ()

    projected: dict[str, float] = {}
    for raw_symbol, raw_value in account.positions.items():
        symbol = str(raw_symbol).strip().upper()
        value = _finite_float(raw_value)
        if not symbol or value is None or value <= 0 or symbol in CASH_EQUIVALENTS:
            continue
        projected[symbol] = projected.get(symbol, 0.0) + value
    if order.symbol not in CASH_EQUIVALENTS:
        projected[order.symbol] = projected.get(order.symbol, 0.0) + notional

    reasons: list[str] = []
    if len(projected) > limits.max_held_names:
        reasons.append("portfolio_held_name_count_exceeds_cap")
    for symbol, value in sorted(projected.items()):
        if value / equity > limits.max_global_position_weight + 1e-9:
            reasons.append(f"portfolio_position_weight_exceeds_global_cap:{symbol}")

    sector_values: dict[str, float] = {}
    for symbol, value in sorted(projected.items()):
        sector = account.sector_for(symbol)
        if sector is None:
            reasons.append(f"portfolio_sector_mapping_missing:{symbol}")
            continue
        sector_values[sector] = sector_values.get(sector, 0.0) + value
    for sector, value in sorted(sector_values.items()):
        if value / equity > limits.max_sector_weight + 1e-9:
            reasons.append(f"portfolio_sector_weight_exceeds_cap:{sector}")
    return tuple(reasons)


def evaluate_order(
    order: ProposedOrder,
    account: AccountSnapshot,
    limits: ExecutionLimits | None = None,
    root: str | Path = ".",
) -> Decision:
    """Approve an order only if every check passes. Unknown states reject."""
    limits = limits or ExecutionLimits()
    reasons = list(check_account_halts(account, limits, root))

    notional_value = _finite_float(order.notional)
    current_value = _finite_float(account.positions.get(order.symbol, 0.0))
    is_reducing_exit = (
        order.uses_exit_reserve
        and notional_value is not None
        and current_value is not None
        and current_value > 0
        and 0 < notional_value <= current_value + 1e-9
    )
    if is_reducing_exit:
        # Risk and research controls stop new exposure. They must not deadlock an
        # authenticated, allowlisted mandatory sell of an existing long position.
        # Account identity, the kill switch, session/quote integrity, and option
        # share encumbrances remain binding.
        entry_only_halts = {
            "capital_floor_breached",
            "daily_loss_halt",
            "prior_close_equity_missing",
            "non_finite_prior_close_equity",
            "non_positive_prior_close_equity",
            "max_drawdown_halt",
            "no_drawdown_protection_available",
            "daily_order_count_limit_reached",
            "daily_notional_limit_reached",
            "broker_option_orders_missing",
            "picker_authorization_packet_missing_or_expired",
            "picker_buy_symbol_not_authorized_by_database",
        }
        entry_only_prefixes = (
            "picker_database_halt:",
            "picker_prior_close_anchor_",
            "picker_target_exceeds_packet_weight:",
        )
        reasons = [
            reason
            for reason in reasons
            if reason not in entry_only_halts and not reason.startswith(entry_only_prefixes)
        ]

    expected_market_hours = "regular_hours" if account.session_is_regular else account.market_hours
    if order.market_hours != expected_market_hours:
        reasons.append("order_market_hours_mismatch")
    if expected_market_hours in {"extended_hours", "all_day_hours"}:
        if order.order_type != "limit":
            reasons.append("extended_hours_requires_limit_order")
        if order.symbol not in account.session_tradable_symbols:
            reasons.append("symbol_not_tradable_in_selected_session")
        spread_bps = _finite_float(account.quote_spreads_bps.get(order.symbol))
        if spread_bps is None or spread_bps < 0:
            reasons.append("missing_or_invalid_extended_hours_spread")
        elif spread_bps > limits.max_extended_spread_bps:
            reasons.append("extended_hours_spread_above_cap")
    if limits.require_fresh_quotes:
        quote_at = _utc_datetime(order.quote_timestamp)
        if quote_at is None:
            reasons.append("missing_or_invalid_equity_quote_timestamp")
        else:
            quote_age = (datetime.now(UTC) - quote_at).total_seconds()
            if quote_age < -1:
                reasons.append("equity_quote_timestamp_in_future")
            elif quote_age > limits.max_quote_age_seconds:
                reasons.append("equity_quote_stale")

    if order.side not in {"buy", "sell"}:
        reasons.append("unsupported_side")
    if order.order_type not in {"limit", "market"}:
        reasons.append("unsupported_order_type")
    if order.order_type == "limit":
        limit_price = _finite_float(order.limit_price)
        quantity = _finite_float(order.quantity)
        if order.limit_price is None:
            reasons.append("limit_order_requires_positive_limit_price")
        elif limit_price is None:
            reasons.append("non_finite_limit_price")
        elif limit_price <= 0:
            reasons.append("limit_order_requires_positive_limit_price")
        # The broker rejects fractional limit orders, so a limit order must be a
        # whole number of shares.
        if order.quantity is None:
            reasons.append("limit_order_requires_whole_share_quantity")
        elif quantity is None:
            reasons.append("non_finite_quantity")
        elif quantity < 1 or quantity % 1 != 0:
            reasons.append("limit_order_requires_whole_share_quantity")
    reference_price = _finite_float(order.reference_price)
    if order.reference_price is None:
        reasons.append("missing_reference_price")
    elif reference_price is None:
        reasons.append("non_finite_reference_price")
    elif reference_price <= 0:
        # Without it, reconciliation has nothing to measure the fill against.
        reasons.append("missing_reference_price")
    if not limits.symbol_allowed(order.symbol, order.side):
        reasons.append("symbol_not_on_allowlist")
    if not order.rationale.strip():
        reasons.append("missing_rationale")

    notional = notional_value or 0.0
    if notional_value is None:
        reasons.append("non_finite_notional")
    elif notional <= 0:
        reasons.append("non_positive_notional")
    full_position_exit = (
        is_reducing_exit
        and current_value is not None
        and abs(notional - current_value) <= max(0.01, current_value * 1e-9)
    )
    if not full_position_exit and notional > limits.max_order_notional:
        reasons.append("order_notional_exceeds_cap")
    if not full_position_exit and 0 < notional < limits.min_order_notional:
        reasons.append("order_notional_below_minimum")
    account_notional_today = _finite_float(account.notional_today)
    if (
        not is_reducing_exit
        and account_notional_today is not None
        and account_notional_today + notional > limits.max_daily_notional
    ):
        reasons.append("order_would_breach_daily_notional")
    if not order.uses_exit_reserve:
        entry_orders_today = _finite_float(account.effective_entry_orders_today)
        entry_notional_today = _finite_float(account.effective_entry_notional_today)
        if entry_orders_today is not None and entry_orders_today >= limits.max_entry_orders_per_day:
            reasons.append("entry_order_count_limit_reached")
        if (
            entry_notional_today is not None
            and entry_notional_today + notional > limits.max_entry_daily_notional
        ):
            reasons.append("order_would_breach_entry_daily_notional")

    broker_shape_valid = order.order_type == "market" or (
        order.order_type == "limit"
        and _finite_float(order.limit_price) is not None
        and _finite_float(order.quantity) is not None
    )
    if notional_value is not None and broker_shape_valid:
        try:
            broker_parameters = order.broker_parameters()
            if broker_parameters["type"] == "market":
                broker_notional = Decimal(broker_parameters["dollar_amount"])
            else:
                broker_notional = Decimal(broker_parameters["quantity"]) * Decimal(
                    broker_parameters["limit_price"]
                )
        except (TypeError, ValueError):
            reasons.append("invalid_broker_parameters")
        else:
            if Decimal(str(notional)) != broker_notional:
                reasons.append("order_notional_does_not_match_broker_parameters")

    current_value = current_value or 0.0
    if order.side == "buy":
        # Deposits that have not settled are excluded so the guard cannot spend
        # money the broker has not actually made available.
        cash = _finite_float(account.cash)
        pending_deposits = _finite_float(account.pending_deposits)
        equity = _finite_float(account.equity)
        if cash is not None and pending_deposits is not None and equity is not None:
            spendable = cash - pending_deposits
            reserve = equity * limits.min_cash_reserve_weight
            if notional > max(spendable - reserve, 0.0):
                reasons.append("insufficient_settled_cash_after_reserve")
            projected_weight = (current_value + notional) / equity if equity > 0 else 1.0
            if projected_weight > limits.position_cap_for(order.symbol) + 1e-9:
                reasons.append("projected_position_weight_exceeds_cap")
            reasons.extend(_entry_portfolio_envelope_reasons(order, account, limits, notional))
    else:
        if current_value <= 0:
            reasons.append("sell_without_existing_long_position")
        elif notional > current_value + 1e-9:
            reasons.append("sell_exceeds_position_value")

    return Decision(order=order, approved=not reasons, reasons=tuple(reasons))


def evaluate_batch(
    orders: list[ProposedOrder],
    account: AccountSnapshot,
    limits: ExecutionLimits | None = None,
    root: str | Path = ".",
) -> list[Decision]:
    """Evaluate sequentially so earlier approvals consume the daily budget."""
    limits = limits or ExecutionLimits()
    decisions: list[Decision] = []
    running = account
    for order in orders:
        decision = evaluate_order(order, running, limits, root)
        decisions.append(decision)
        if decision.approved:
            positions = dict(running.positions)
            delta = order.notional if order.side == "buy" else -order.notional
            positions[order.symbol] = positions.get(order.symbol, 0.0) + delta
            running = replace(
                running,
                positions=positions,
                cash=running.cash - delta,
                orders_today=running.orders_today + 1,
                notional_today=running.notional_today + order.notional,
                entry_orders_today=(
                    running.effective_entry_orders_today + (0 if order.uses_exit_reserve else 1)
                ),
                entry_notional_today=(
                    running.effective_entry_notional_today
                    + (0.0 if order.uses_exit_reserve else order.notional)
                ),
            )
    return decisions


# Fixed namespace so the same logical order maps to the same ref_id on any
# machine, in any process, on any run.
REF_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def deterministic_ref_id(
    account_number: str,
    symbol: str,
    side: str,
    day: date | None = None,
    pick_id: str = "",
    intent: str = "rebalance",
) -> str:
    """Derive Robinhood's idempotency key from the order's logical identity.

    Two concurrent runs on separate machines cannot share a lock file, and both
    can read the same broker order count before either places anything. Because
    the broker deduplicates on ref_id, deriving it from identity rather than
    randomness turns that race into a single order instead of two.

    Price, notional, and observed order count are deliberately excluded because
    concurrent runs may observe slightly different quotes or session stages.
    Pick and intent distinguish separate logical orders without turning harmless
    price drift into a second broker order.
    """
    stamp = (day or datetime.now(UTC).date()).isoformat()
    key = f"{account_number}|{stamp}|{symbol.upper()}|{side.lower()}|{pick_id}|{intent.lower()}"
    return str(uuid.uuid5(REF_ID_NAMESPACE, key))


def marketable_limit_price(last_price: float, side: str, slippage_bps: float = 20.0) -> float:
    """Cross the spread by a bounded amount instead of paying an open market fill."""
    if _finite_float(last_price) is None or last_price <= 0:
        raise ValueError("last_price must be finite and positive")
    if _finite_float(slippage_bps) is None or slippage_bps < 0:
        raise ValueError("slippage_bps must be finite and non-negative")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    offset = 1.0 + (slippage_bps / 10_000.0) * (1.0 if side == "buy" else -1.0)
    result = round(last_price * offset, 2)
    if not isfinite(result) or result <= 0:
        raise ValueError("marketable limit price must be finite and positive")
    return result


def plan_orders_from_targets(
    target_weights: dict[str, float],
    account: AccountSnapshot,
    prices: dict[str, float],
    limits: ExecutionLimits | None = None,
    rebalance_threshold: float = 0.05,
    metadata_by_symbol: dict[str, dict[str, str | None]] | None = None,
    root: str | Path = ".",
) -> list[Decision]:
    """Translate strategy target weights into guarded, capped, priced orders.

    Trades are emitted only where the drift from target exceeds the threshold,
    which keeps turnover, and therefore cost drag, low on a small account.
    """
    limits = limits or ExecutionLimits()
    if _finite_float(account.equity) is None or account.equity <= 0:
        raise ValueError("Cannot plan orders against non-positive equity")
    if _finite_float(rebalance_threshold) is None or rebalance_threshold < 0:
        raise ValueError("rebalance_threshold must be finite and non-negative")
    normalized_targets: dict[str, float] = {}
    for symbol, raw_weight in target_weights.items():
        weight = _finite_float(raw_weight)
        if weight is None or weight < 0:
            raise ValueError(f"Target weight for {symbol} must be finite and non-negative")
        normalized_targets[symbol.upper()] = weight
    total_weight = sum(normalized_targets.values())
    if total_weight > 1.0 + 1e-9:
        raise ValueError("Target weights sum above 100%")

    normalized_prices: dict[str, float] = {}
    for symbol, raw_price in prices.items():
        price = _finite_float(raw_price)
        if price is None or price <= 0:
            raise ValueError(f"Price for {symbol} must be finite and positive")
        normalized_prices[symbol.upper()] = price
    prices = normalized_prices
    metadata_by_symbol = {
        symbol.upper(): metadata for symbol, metadata in (metadata_by_symbol or {}).items()
    }
    market_hours = "regular_hours" if account.session_is_regular else account.market_hours
    symbols = sorted(set(normalized_targets) | set(account.positions))
    drifts: list[tuple[float, ProposedOrder]] = []
    for symbol in symbols:
        target_value = account.equity * normalized_targets.get(symbol, 0.0)
        current_value = _finite_float(account.positions.get(symbol, 0.0))
        if current_value is None:
            raise ValueError(f"Position value for {symbol} must be finite")
        if not isfinite(target_value):
            raise ValueError(f"Target value for {symbol} must be finite")
        delta = target_value - current_value
        drift = abs(delta) / account.equity
        if drift < rebalance_threshold:
            continue
        side = "buy" if delta > 0 else "sell"
        last_price = prices.get(symbol)
        target_weight = normalized_targets.get(symbol, 0.0)
        rationale = f"rebalance toward target weight {target_weight:.3f}"
        metadata = metadata_by_symbol.get(symbol, {})
        pick_id = str(metadata.get("pick_id") or "")
        intent_class = str(metadata.get("intent_class") or ("entry" if side == "buy" else "sell"))
        exit_reason = metadata.get("exit_reason")
        is_full_mandatory_exit = (
            side == "sell"
            and intent_class.strip().lower() in {"mandatory_exit", "close"}
            and target_value <= 0.01
            and current_value > 0
        )
        desired_notional = (
            abs(delta) if is_full_mandatory_exit else min(abs(delta), limits.max_order_notional)
        )
        rounding = ROUND_DOWN if side == "sell" else None
        notional_decimal = Decimal(str(desired_notional)).quantize(
            Decimal("0.01"),
            rounding=rounding,
        )
        notional = float(notional_decimal)
        if not last_price:
            # A missing quote produces an order the guard rejects, rather than
            # one priced by guesswork.
            drifts.append(
                (
                    drift,
                    ProposedOrder(
                        symbol,
                        side,
                        notional,
                        "limit",
                        rationale=rationale,
                        pick_id=pick_id,
                        intent_class=intent_class,
                        exit_reason=str(exit_reason) if exit_reason else None,
                        market_hours=market_hours,
                        quote_timestamp=account.quote_timestamps.get(symbol),
                    ),
                )
            )
            continue

        limit_price = marketable_limit_price(last_price, side)
        whole_shares = int(notional // (limit_price if side == "buy" else last_price))
        if whole_shares >= 1 and not is_full_mandatory_exit:
            # A limit order is only available at whole-share size, so prefer it
            # there for the price protection it gives.
            drifts.append(
                (
                    drift,
                    ProposedOrder(
                        symbol=symbol,
                        side=side,
                        notional=round(whole_shares * limit_price, 2),
                        order_type="limit",
                        limit_price=limit_price,
                        quantity=float(whole_shares),
                        reference_price=last_price,
                        rationale=rationale,
                        pick_id=pick_id,
                        intent_class=intent_class,
                        exit_reason=str(exit_reason) if exit_reason else None,
                        market_hours=market_hours,
                        quote_timestamp=account.quote_timestamps.get(symbol),
                    ),
                )
            )
        else:
            # Below one share the broker accepts only a dollar-denominated
            # market order, so price protection moves to reconciliation.
            drifts.append(
                (
                    drift,
                    ProposedOrder(
                        symbol=symbol,
                        side=side,
                        notional=notional,
                        order_type="market",
                        reference_price=last_price,
                        rationale=rationale,
                        pick_id=pick_id,
                        intent_class=intent_class,
                        exit_reason=str(exit_reason) if exit_reason else None,
                        market_hours=market_hours,
                        quote_timestamp=account.quote_timestamps.get(symbol),
                    ),
                )
            )

    priority = {
        "mandatory_exit": 0,
        "close": 0,
        "sell": 1,
        "rebalance": 2,
        "entry": 3,
    }
    drifts.sort(
        key=lambda item: (
            priority.get(item[1].intent_class, 4),
            item[1].side != "sell",
            -item[0],
        )
    )
    # Evaluate the complete set: total caps reject risk-increasing orders, while
    # mandatory reductions must remain possible even after that capacity is gone.
    ordered = [order for _, order in drifts]
    return evaluate_batch(ordered, account, limits, root)


def load_live_state(root: str | Path = ".") -> dict[str, Any]:
    path = Path(root) / LIVE_STATE_FILENAME
    if not path.exists():
        return {"high_water_mark": None, "history": []}
    return json.loads(path.read_text())


def record_live_state(
    equity: float,
    root: str | Path = ".",
    as_of: date | None = None,
    *,
    record_prior_close: bool = False,
) -> dict[str, Any]:
    """Persist equity state without turning an intraday tick into a prior close.

    ``record_prior_close`` must only be set for an authenticated end-of-day
    observation. Ordinary automation cycles ratchet the high-water mark and add
    history while preserving the daily-loss baseline.
    """
    if _finite_float(equity) is None:
        raise ValueError("equity must be finite")
    state = load_live_state(root)
    previous = state.get("high_water_mark")
    if previous is not None and _finite_float(previous) is None:
        raise ValueError("Persisted high_water_mark must be finite")
    state["high_water_mark"] = equity if previous is None else max(float(previous), equity)
    observation_date = (as_of or datetime.now(UTC).date()).isoformat()
    if record_prior_close:
        state["prior_close_equity"] = equity
        state["prior_close_date"] = observation_date
    state.setdefault("history", []).append({"date": observation_date, "equity": equity})
    path = Path(root) / LIVE_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def daily_consumption(root: str | Path = ".", day: date | None = None) -> tuple[int, float]:
    """Orders and notional already approved today, persisted across processes."""
    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    entry = state.get("daily", {}).get(key, {})
    orders = _finite_float(entry.get("orders", 0))
    notional = _finite_float(entry.get("notional", 0.0))
    if orders is None or orders < 0 or orders % 1:
        raise ValueError("Persisted daily orders must be a finite non-negative integer")
    if notional is None or notional < 0:
        raise ValueError("Persisted daily notional must be finite and non-negative")
    return int(orders), notional


def daily_entry_consumption(root: str | Path = ".", day: date | None = None) -> tuple[int, float]:
    """Entry/rebalance usage persisted across runs.

    State written before intent-aware accounting is conservatively interpreted
    as entirely entry usage, preserving the exit reserve during migration.
    """
    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    entry = state.get("daily", {}).get(key, {})
    orders = _finite_float(entry.get("entry_orders", entry.get("orders", 0)))
    notional = _finite_float(entry.get("entry_notional", entry.get("notional", 0.0)))
    if orders is None or orders < 0 or orders % 1:
        raise ValueError("Persisted entry orders must be a finite non-negative integer")
    if notional is None or notional < 0:
        raise ValueError("Persisted entry notional must be finite and non-negative")
    return int(orders), notional


def merge_broker_and_local_consumption(
    broker: tuple[int, float],
    persisted: tuple[int, float],
    persisted_entry: tuple[int, float],
) -> tuple[int, float, int, float]:
    """Merge verified hard totals with intent-aware local counters.

    Broker usage not represented in local state has unknown intent and is
    therefore charged to the entry cap. This preserves both cross-machine
    broker verification and the exit reserve.
    """
    broker_orders, broker_notional = broker
    persisted_orders, persisted_notional = persisted
    persisted_entry_orders, persisted_entry_notional = persisted_entry
    values = (
        broker_orders,
        broker_notional,
        persisted_orders,
        persisted_notional,
        persisted_entry_orders,
        persisted_entry_notional,
    )
    if any(_finite_float(value) is None for value in values):
        raise ValueError("Daily consumption counters must be finite")
    if any(value < 0 for value in values):
        raise ValueError("Daily consumption counters cannot be negative")
    if any(value % 1 for value in (broker_orders, persisted_orders, persisted_entry_orders)):
        raise ValueError("Daily order counters must be integers")
    if persisted_entry_orders > persisted_orders:
        raise ValueError("Persisted entry orders cannot exceed total orders")
    if persisted_entry_notional > persisted_notional:
        raise ValueError("Persisted entry notional cannot exceed total notional")

    total_orders = max(broker_orders, persisted_orders)
    total_notional = max(broker_notional, persisted_notional)
    entry_orders = min(
        total_orders,
        persisted_entry_orders + max(broker_orders - persisted_orders, 0),
    )
    entry_notional = min(
        total_notional,
        persisted_entry_notional + max(broker_notional - persisted_notional, 0.0),
    )
    return total_orders, total_notional, entry_orders, entry_notional


def record_plan_consumption(
    orders: int,
    notional: float,
    root: str | Path = ".",
    day: date | None = None,
    *,
    entry_orders: int | None = None,
    entry_notional: float | None = None,
) -> tuple[int, float]:
    """Persist total and entry usage so another run cannot re-spend either cap.

    Callers that do not provide intent-aware counters are treated
    conservatively: all usage consumes entry capacity.
    """
    numeric_inputs = {
        "orders": orders,
        "notional": notional,
        "entry_orders": orders if entry_orders is None else entry_orders,
        "entry_notional": notional if entry_notional is None else entry_notional,
    }
    if any(_finite_float(value) is None for value in numeric_inputs.values()):
        raise ValueError("Plan consumption values must be finite")
    if orders < 0 or orders % 1:
        raise ValueError("orders must be a non-negative integer")
    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    daily = state.setdefault("daily", {})
    entry = daily.setdefault(key, {"orders": 0, "notional": 0.0})
    previous_orders_value = _finite_float(entry["orders"])
    previous_notional_value = _finite_float(entry["notional"])
    if (
        previous_orders_value is None
        or previous_orders_value < 0
        or previous_orders_value % 1
        or previous_notional_value is None
        or previous_notional_value < 0
    ):
        raise ValueError("Persisted plan consumption values must be finite and non-negative")
    previous_orders = int(previous_orders_value)
    previous_notional = previous_notional_value
    previous_entry_orders = _finite_float(entry.get("entry_orders", previous_orders))
    previous_entry_notional = _finite_float(entry.get("entry_notional", previous_notional))
    if (
        previous_entry_orders is None
        or previous_entry_orders < 0
        or previous_entry_orders % 1
        or previous_entry_notional is None
        or previous_entry_notional < 0
    ):
        raise ValueError("Persisted entry consumption values must be finite and non-negative")
    entry.setdefault("entry_orders", previous_orders)
    entry.setdefault("entry_notional", previous_notional)
    consumed_entry_orders = orders if entry_orders is None else entry_orders
    consumed_entry_notional = notional if entry_notional is None else entry_notional
    if not 0 <= consumed_entry_orders <= orders:
        raise ValueError("entry_orders must be between zero and orders")
    if not 0.0 <= consumed_entry_notional <= notional:
        raise ValueError("entry_notional must be between zero and notional")
    entry["orders"] = int(entry["orders"]) + orders
    entry["notional"] = float(entry["notional"]) + notional
    entry["entry_orders"] = int(entry["entry_orders"]) + consumed_entry_orders
    entry["entry_notional"] = float(entry["entry_notional"]) + consumed_entry_notional
    if any(_finite_float(value) is None for value in (entry["notional"], entry["entry_notional"])):
        raise ValueError("Accumulated plan consumption values must be finite")
    path = Path(root) / LIVE_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    return entry["orders"], entry["notional"]


def record_reservation_consumption(
    reservations: list[tuple[str, float, bool]],
    root: str | Path = ".",
    day: date | None = None,
) -> tuple[int, float]:
    """Persist only successfully reserved orders, idempotently by broker ref.

    Planning is read-only and may be repeated while an operator considers a
    broker preview.  Daily capacity is consumed only at the durable reservation
    boundary.  Replaying an identical reservation is a no-op; reusing a ref for
    different economics is rejected.
    """
    normalized: dict[str, dict[str, float | bool]] = {}
    for ref_id, notional, is_entry in reservations:
        ref_id = str(ref_id).strip()
        parsed_notional = _finite_float(notional)
        if not ref_id:
            raise ValueError("Reservation consumption requires a ref_id")
        if parsed_notional is None or parsed_notional <= 0:
            raise ValueError("Reservation notional must be finite and positive")
        record = {"notional": parsed_notional, "is_entry": bool(is_entry)}
        existing = normalized.get(ref_id)
        if existing is not None and existing != record:
            raise ValueError(f"Reservation {ref_id} has conflicting economics")
        normalized[ref_id] = record

    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    daily = state.setdefault("daily", {})
    entry = daily.setdefault(key, {"orders": 0, "notional": 0.0})
    values = {
        "orders": _finite_float(entry.get("orders")),
        "notional": _finite_float(entry.get("notional")),
        "entry_orders": _finite_float(entry.get("entry_orders", entry.get("orders"))),
        "entry_notional": _finite_float(entry.get("entry_notional", entry.get("notional"))),
    }
    if any(value is None or value < 0 for value in values.values()):
        raise ValueError("Persisted reservation consumption must be finite and non-negative")
    if values["orders"] % 1 or values["entry_orders"] % 1:
        raise ValueError("Persisted reservation order counters must be integers")
    entry.setdefault("entry_orders", int(values["orders"]))
    entry.setdefault("entry_notional", float(values["notional"]))
    recorded = entry.setdefault("reservations", {})
    if not isinstance(recorded, dict):
        raise ValueError("Persisted reservation identities must be a mapping")

    new_records: dict[str, dict[str, float | bool]] = {}
    for ref_id, record in normalized.items():
        previous = recorded.get(ref_id)
        if previous is not None:
            if previous != record:
                raise ValueError(f"Reservation {ref_id} is immutable")
            continue
        new_records[ref_id] = record

    entry["orders"] = int(values["orders"]) + len(new_records)
    entry["notional"] = float(values["notional"]) + sum(
        float(record["notional"]) for record in new_records.values()
    )
    entry["entry_orders"] = int(values["entry_orders"]) + sum(
        bool(record["is_entry"]) for record in new_records.values()
    )
    entry["entry_notional"] = float(values["entry_notional"]) + sum(
        float(record["notional"]) for record in new_records.values() if bool(record["is_entry"])
    )
    recorded.update(new_records)
    path = Path(root) / LIVE_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    return int(entry["orders"]), float(entry["notional"])


def append_audit_record(record: dict[str, Any], root: str | Path = ".") -> Path:
    """Append-only JSONL trail so every decision is reconstructable after fact."""
    path = Path(root) / "artifacts/live/audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"recorded_at": datetime.now(UTC).isoformat(), **record}
    with path.open("a") as handle:
        handle.write(json.dumps(payload) + "\n")
    return path
