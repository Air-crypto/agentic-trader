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
    return parsed if parsed > 0 else None


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
    max_position_weight: float = 0.25
    max_broad_market_weight: float = 0.60
    min_cash_reserve_weight: float = 0.10
    max_orders_per_day: int = 4
    max_daily_notional: float = 400.0
    max_daily_loss_weight: float = 0.03
    max_drawdown_weight: float = 0.10
    max_loss_from_deposits_weight: float = 0.10
    min_order_notional: float = 25.0
    allow_fractional: bool = True
    require_broker_order_counts: bool = True
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
        if self.max_order_notional <= 0:
            raise ValueError("max_order_notional must be positive")
        if self.min_order_notional > self.max_order_notional:
            raise ValueError("min_order_notional cannot exceed max_order_notional")
        if not 0 < self.max_position_weight <= 1:
            raise ValueError("max_position_weight must be within (0, 1]")
        if not 0 <= self.min_cash_reserve_weight < 1:
            raise ValueError("min_cash_reserve_weight must be within [0, 1)")
        if self.max_orders_per_day <= 0:
            raise ValueError("max_orders_per_day must be positive")


@dataclass(frozen=True)
class AccountSnapshot:
    """Point-in-time broker state. Supplied by the caller, never fetched here."""

    account_number: str
    equity: float
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
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
    external_halt_reasons: tuple[str, ...] = ()

    @property
    def settled_equity(self) -> float:
        """Equity excluding deposits that have not cleared yet."""
        return self.equity - self.pending_deposits

    def position_value(self, symbol: str) -> float:
        return float(self.positions.get(symbol.upper(), 0.0))


def _broker_number(value: Any) -> float | None:
    """Parse a numeric broker field without guessing missing values."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def broker_position_values(
    positions: list[dict[str, Any]],
    prices: dict[str, float],
) -> dict[str, float]:
    """Convert Robinhood position quantities to current market values.

    The broker returns quantities while the planner reasons in dollars. Keeping
    this conversion in deterministic code avoids asking the automation to invent
    a request shape after the first live orders create non-empty positions.
    """
    normalized_prices = {symbol.upper(): float(price) for symbol, price in prices.items()}
    values: dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        quantity = _broker_number(position.get("quantity"))
        price = normalized_prices.get(symbol)
        if not symbol or quantity is None or quantity < 0:
            raise ValueError("Broker position is missing a valid symbol or quantity")
        if price is None or price <= 0:
            raise ValueError(f"Missing a positive current price for position {symbol}")
        values[symbol] = values.get(symbol, 0.0) + quantity * price
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", self.side.lower())
        object.__setattr__(self, "order_type", self.order_type.lower())

    @property
    def is_fractional(self) -> bool:
        return self.order_type == "market"

    def broker_parameters(self) -> dict[str, Any]:
        """Exactly the arguments place_equity_order should receive."""
        common = {
            "symbol": self.symbol,
            "side": self.side,
            "market_hours": "regular_hours",
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

    if kill_switch_engaged(root):
        reasons.append("kill_switch_file_present")
    permitted = agentic_account_number()
    if permitted is None:
        reasons.append("agentic_account_not_configured")
    elif account.account_number != permitted:
        reasons.append("account_is_not_the_agentic_account")
    if account.equity <= 0:
        reasons.append("non_positive_equity")
    if account.orders_today >= limits.max_orders_per_day:
        reasons.append("daily_order_count_limit_reached")
    if account.notional_today >= limits.max_daily_notional:
        reasons.append("daily_notional_limit_reached")
    if limits.require_broker_order_counts and account.orders_source != "broker":
        reasons.append("daily_order_count_not_broker_verified")
    if not account.session_is_regular:
        reasons.append("outside_regular_trading_session")

    # Stateless and therefore the one drawdown protection that still works in an
    # environment with no persistence, such as a fresh cloud checkout.
    net_deposits = account.net_deposits
    if net_deposits is None:
        net_deposits = configured_net_deposits()
    if net_deposits is not None and net_deposits > 0:
        floor = net_deposits * (1.0 - limits.max_loss_from_deposits_weight)
        if account.equity < floor:
            reasons.append("capital_floor_breached")

    high_water_mark = account.high_water_mark
    if high_water_mark is not None and high_water_mark > 0:
        drawdown = 1.0 - (account.equity / high_water_mark)
        if drawdown >= limits.max_drawdown_weight:
            reasons.append("max_drawdown_halt")

    # With neither a persisted peak nor a known deposit base there is no loss
    # limit of any kind, which is the exact situation a fresh cloud checkout
    # produces. Refuse rather than trade unprotected.
    if high_water_mark is None and not net_deposits:
        reasons.append("no_drawdown_protection_available")

    prior_close = account.prior_close_equity
    if prior_close is not None and prior_close > 0:
        daily_loss = 1.0 - (account.equity / prior_close)
        if daily_loss >= limits.max_daily_loss_weight:
            reasons.append("daily_loss_halt")

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

    if order.side not in {"buy", "sell"}:
        reasons.append("unsupported_side")
    if order.order_type not in {"limit", "market"}:
        reasons.append("unsupported_order_type")
    if order.order_type == "limit":
        if order.limit_price is None or order.limit_price <= 0:
            reasons.append("limit_order_requires_positive_limit_price")
        # The broker rejects fractional limit orders, so a limit order must be a
        # whole number of shares.
        if order.quantity is None or order.quantity < 1 or order.quantity % 1 != 0:
            reasons.append("limit_order_requires_whole_share_quantity")
    if order.reference_price is None or order.reference_price <= 0:
        # Without it, reconciliation has nothing to measure the fill against.
        reasons.append("missing_reference_price")
    if not limits.symbol_allowed(order.symbol, order.side):
        reasons.append("symbol_not_on_allowlist")
    if not order.rationale.strip():
        reasons.append("missing_rationale")

    notional = float(order.notional)
    if notional <= 0:
        reasons.append("non_positive_notional")
    if notional > limits.max_order_notional:
        reasons.append("order_notional_exceeds_cap")
    if 0 < notional < limits.min_order_notional:
        reasons.append("order_notional_below_minimum")
    if account.notional_today + notional > limits.max_daily_notional:
        reasons.append("order_would_breach_daily_notional")

    current_value = account.position_value(order.symbol)
    if order.side == "buy":
        # Deposits that have not settled are excluded so the guard cannot spend
        # money the broker has not actually made available.
        spendable = account.cash - account.pending_deposits
        reserve = account.equity * limits.min_cash_reserve_weight
        if notional > max(spendable - reserve, 0.0):
            reasons.append("insufficient_settled_cash_after_reserve")
        projected_weight = (
            (current_value + notional) / account.equity if account.equity > 0 else 1.0
        )
        if projected_weight > limits.position_cap_for(order.symbol) + 1e-9:
            reasons.append("projected_position_weight_exceeds_cap")
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

    The notional and observed order count are deliberately excluded: concurrent
    runs may price snapshots cents apart or observe different stages of the same
    session. The planner emits at most one order per symbol and side each day, so
    this remains unique for every logical order it can create.
    """
    stamp = (day or datetime.now(UTC).date()).isoformat()
    key = f"{account_number}|{stamp}|{symbol.upper()}|{side.lower()}|{pick_id}|{intent.lower()}"
    return str(uuid.uuid5(REF_ID_NAMESPACE, key))


def marketable_limit_price(last_price: float, side: str, slippage_bps: float = 20.0) -> float:
    """Cross the spread by a bounded amount instead of paying an open market fill."""
    if last_price <= 0:
        raise ValueError("last_price must be positive")
    offset = 1.0 + (slippage_bps / 10_000.0) * (1.0 if side == "buy" else -1.0)
    return round(last_price * offset, 2)


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
    if account.equity <= 0:
        raise ValueError("Cannot plan orders against non-positive equity")
    total_weight = sum(target_weights.values())
    if total_weight > 1.0 + 1e-9:
        raise ValueError("Target weights sum above 100%")

    prices = {symbol.upper(): float(price) for symbol, price in prices.items()}
    metadata_by_symbol = {
        symbol.upper(): metadata for symbol, metadata in (metadata_by_symbol or {}).items()
    }
    symbols = sorted(set(target_weights) | set(account.positions))
    drifts: list[tuple[float, ProposedOrder]] = []
    for symbol in symbols:
        target_value = account.equity * float(target_weights.get(symbol, 0.0))
        current_value = account.position_value(symbol)
        delta = target_value - current_value
        drift = abs(delta) / account.equity
        if drift < rebalance_threshold:
            continue
        notional = min(abs(delta), limits.max_order_notional)
        side = "buy" if delta > 0 else "sell"
        last_price = prices.get(symbol)
        target_weight = float(target_weights.get(symbol, 0.0))
        rationale = f"rebalance toward target weight {target_weight:.3f}"
        metadata = metadata_by_symbol.get(symbol, {})
        pick_id = str(metadata.get("pick_id") or "")
        intent_class = str(metadata.get("intent_class") or ("entry" if side == "buy" else "sell"))
        exit_reason = metadata.get("exit_reason")
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
                    ),
                )
            )
            continue

        whole_shares = int(notional // last_price)
        if whole_shares >= 1:
            # A limit order is only available at whole-share size, so prefer it
            # there for the price protection it gives.
            drifts.append(
                (
                    drift,
                    ProposedOrder(
                        symbol=symbol,
                        side=side,
                        notional=whole_shares * last_price,
                        order_type="limit",
                        limit_price=marketable_limit_price(last_price, side),
                        quantity=float(whole_shares),
                        reference_price=last_price,
                        rationale=rationale,
                        pick_id=pick_id,
                        intent_class=intent_class,
                        exit_reason=str(exit_reason) if exit_reason else None,
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
                    ),
                )
            )

    priority = {"mandatory_exit": 0, "sell": 1, "rebalance": 2, "entry": 3}
    drifts.sort(
        key=lambda item: (
            priority.get(item[1].intent_class, 4),
            item[1].side != "sell",
            -item[0],
        )
    )
    ordered = [order for _, order in drifts][: limits.max_orders_per_day]
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
) -> dict[str, Any]:
    """Persist the equity high-water mark that the drawdown halt depends on."""
    state = load_live_state(root)
    previous = state.get("high_water_mark")
    state["high_water_mark"] = equity if previous is None else max(float(previous), equity)
    state["prior_close_equity"] = equity
    state.setdefault("history", []).append(
        {"date": (as_of or datetime.now(UTC).date()).isoformat(), "equity": equity}
    )
    path = Path(root) / LIVE_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def daily_consumption(root: str | Path = ".", day: date | None = None) -> tuple[int, float]:
    """Orders and notional already approved today, persisted across processes."""
    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    entry = state.get("daily", {}).get(key, {})
    return int(entry.get("orders", 0)), float(entry.get("notional", 0.0))


def record_plan_consumption(
    orders: int,
    notional: float,
    root: str | Path = ".",
    day: date | None = None,
) -> tuple[int, float]:
    """Persist approved-order usage so a concurrent run sees it immediately."""
    state = load_live_state(root)
    key = (day or datetime.now(UTC).date()).isoformat()
    daily = state.setdefault("daily", {})
    entry = daily.setdefault(key, {"orders": 0, "notional": 0.0})
    entry["orders"] = int(entry["orders"]) + orders
    entry["notional"] = float(entry["notional"]) + notional
    path = Path(root) / LIVE_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    return entry["orders"], entry["notional"]


def append_audit_record(record: dict[str, Any], root: str | Path = ".") -> Path:
    """Append-only JSONL trail so every decision is reconstructable after fact."""
    path = Path(root) / "artifacts/live/audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"recorded_at": datetime.now(UTC).isoformat(), **record}
    with path.open("a") as handle:
        handle.write(json.dumps(payload) + "\n")
    return path
