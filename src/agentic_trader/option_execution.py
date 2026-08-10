"""Deterministic, network-free guards for single-leg option orders.

The objects in this module intentionally accept broker-independent account
snapshots.  They produce the exact dictionaries consumed by Robinhood's option
review and placement tools, but never call either tool themselves.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .execution import kill_switch_engaged

OPTION_REF_ID_NAMESPACE = uuid.UUID("c0a6b668-ada9-4ba6-9758-805c532ac631")
ENTRY_STRATEGIES = ("long_call", "long_put", "covered_call", "cash_secured_put")
ALLOWED_STRATEGIES = (*ENTRY_STRATEGIES, "close")


def _number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _option_level(value: int | str | None) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip().lower()
    if text.isdigit():
        return int(text)
    for level in (3, 2, 1, 0):
        if text.endswith(str(level)):
            return level
    return 0


def _as_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class OptionExecutionLimits:
    """Hard limits for the deliberately small Level-2 option program."""

    minimum_option_level: int = 2
    allowed_strategies: tuple[str, ...] = ALLOWED_STRATEGIES
    max_contracts_per_order: int = 1
    max_openings_per_day: int = 1
    max_open_option_positions: int = 2
    max_orders_per_day: int = 4
    min_entry_dte: int = 21
    max_entry_dte: int = 60
    max_quote_age_seconds: int = 60
    max_spread_fraction: float = 0.10
    max_long_debit: float = 75.0
    max_long_debit_equity_weight: float = 0.05
    max_aggregate_long_debit_weight: float = 0.10
    max_csp_collateral_weight: float = 0.30
    max_post_assignment_weight: float = 0.15
    min_cash_reserve_weight: float = 0.10

    def __post_init__(self) -> None:
        if self.minimum_option_level < 2:
            raise ValueError("minimum_option_level cannot be below Level 2")
        if self.max_contracts_per_order != 1:
            raise ValueError("Only one-contract option orders are supported")
        if (
            self.max_openings_per_day <= 0
            or self.max_open_option_positions <= 0
            or self.max_orders_per_day <= 0
        ):
            raise ValueError("Opening and position limits must be positive")
        if not 0 <= self.min_entry_dte <= self.max_entry_dte:
            raise ValueError("Entry DTE range is invalid")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        for name in (
            "max_spread_fraction",
            "max_long_debit_equity_weight",
            "max_aggregate_long_debit_weight",
            "max_csp_collateral_weight",
            "max_post_assignment_weight",
            "min_cash_reserve_weight",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be within [0, 1]")


@dataclass(frozen=True)
class OptionAccountSnapshot:
    """Caller-supplied account state used by the option guard."""

    account_number: str
    equity: float
    cash: float
    option_level: int | str
    open_option_positions: int = 0
    option_openings_today: int = 0
    orders_today: int = 0
    aggregate_long_debit: float = 0.0
    csp_collateral: float = 0.0
    pending_deposits: float = 0.0
    underlying_shares: dict[str, float] = field(default_factory=dict)
    underlying_values: dict[str, float] = field(default_factory=dict)
    covered_call_contracts: dict[str, int] = field(default_factory=dict)
    mandatory_close_option_ids: tuple[str, ...] = ()
    orders_source: str = "broker"
    session_is_regular: bool = True
    agentic_allowed: bool = True
    external_halt_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "equity",
            "cash",
            "aggregate_long_debit",
            "csp_collateral",
            "pending_deposits",
        ):
            object.__setattr__(self, name, float(getattr(self, name)))
        for name in ("open_option_positions", "option_openings_today", "orders_today"):
            object.__setattr__(self, name, int(getattr(self, name)))
        object.__setattr__(
            self,
            "underlying_shares",
            {str(key).upper(): float(value) for key, value in self.underlying_shares.items()},
        )
        object.__setattr__(
            self,
            "underlying_values",
            {str(key).upper(): float(value) for key, value in self.underlying_values.items()},
        )
        object.__setattr__(
            self,
            "covered_call_contracts",
            {str(key).upper(): int(value) for key, value in self.covered_call_contracts.items()},
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OptionAccountSnapshot:
        """Build a snapshot from a normalized dict without broker model coupling."""
        aliases = {
            "open_positions": "open_option_positions",
            "openings_today": "option_openings_today",
            "option_risk": "aggregate_long_debit",
            "mandatory_closes": "mandatory_close_option_ids",
        }
        values = dict(raw)
        for old, new in aliases.items():
            if old in values and new not in values:
                values[new] = values.pop(old)
        return cls(**values)

    @property
    def settled_cash(self) -> float:
        return self.cash - self.pending_deposits

    def shares(self, symbol: str) -> float:
        return float(self.underlying_shares.get(symbol.upper(), 0.0))

    def underlying_value(self, symbol: str) -> float:
        return float(self.underlying_values.get(symbol.upper(), 0.0))

    def covered_contracts(self, symbol: str) -> int:
        return int(self.covered_call_contracts.get(symbol.upper(), 0))


def deterministic_option_ref_id(
    account_number: str,
    option_id: str,
    side: str,
    position_effect: str,
    day: date | None = None,
    strategy: str = "",
    intent: str = "option_order",
    leg_fingerprint: Any | None = None,
) -> str:
    """Return the stable broker idempotency key for one logical option order."""
    stamp = (day or datetime.now(UTC).date()).isoformat()
    account_hash = hashlib.sha256(account_number.encode()).hexdigest()
    if leg_fingerprint is None:
        leg_fingerprint = ((option_id, side.lower(), position_effect.lower(), 1),)
    fingerprint = json.dumps(leg_fingerprint, sort_keys=True, separators=(",", ":"), default=str)
    key = "|".join(
        (
            account_hash,
            stamp,
            strategy.lower(),
            fingerprint,
            position_effect.lower(),
            intent.lower(),
        )
    )
    return str(uuid.uuid5(OPTION_REF_ID_NAMESPACE, key))


@dataclass(frozen=True)
class ProposedOptionOrder:
    """A single-leg, broker-ready Level-2 option order."""

    account_number: str
    option_id: str
    chain_symbol: str
    strategy: str
    option_type: str
    side: str
    position_effect: str
    quantity: int
    limit_price: float
    bid_price: float
    ask_price: float
    quote_timestamp: datetime | str
    expiration_date: date | str | None = None
    strike_price: float | None = None
    days_to_expiration: int | None = None
    rationale: str = ""
    underlying_type: str = "equity"
    order_type: str = "limit"
    time_in_force: str = "gfd"
    market_hours: str = "regular_hours"
    ref_id: str = ""
    order_date: date | None = None
    intent: str = "option_order"

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_symbol", self.chain_symbol.upper())
        object.__setattr__(self, "quantity", int(self.quantity))
        object.__setattr__(self, "limit_price", float(self.limit_price))
        object.__setattr__(self, "bid_price", float(self.bid_price))
        object.__setattr__(self, "ask_price", float(self.ask_price))
        if self.strike_price is not None:
            object.__setattr__(self, "strike_price", float(self.strike_price))
        if self.days_to_expiration is not None:
            object.__setattr__(self, "days_to_expiration", int(self.days_to_expiration))
        for name in (
            "strategy",
            "option_type",
            "side",
            "position_effect",
            "underlying_type",
            "order_type",
            "time_in_force",
            "market_hours",
        ):
            object.__setattr__(self, name, str(getattr(self, name)).lower())
        if not self.ref_id and self.account_number and self.option_id:
            object.__setattr__(
                self,
                "ref_id",
                deterministic_option_ref_id(
                    self.account_number,
                    self.option_id,
                    self.side,
                    self.position_effect,
                    self.order_date,
                    self.strategy,
                    self.intent,
                ),
            )

    @property
    def premium_notional(self) -> float:
        return float(self.limit_price) * int(self.quantity) * 100.0

    def dte(self, as_of: date) -> int | None:
        if self.days_to_expiration is not None:
            return int(self.days_to_expiration)
        expiration = _as_date(self.expiration_date)
        return None if expiration is None else (expiration - as_of).days

    def leg_fingerprint(self) -> tuple[tuple[str, str, str, int], ...]:
        return ((self.option_id, self.side, self.position_effect, 1),)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProposedOptionOrder:
        """Accept a flat order dict or a normalized option decision packet."""
        contract = raw.get("contract")
        contract = contract if isinstance(contract, dict) else {}
        return cls(
            account_number=str(raw.get("account_number", "")),
            option_id=str(raw.get("option_id", contract.get("option_id", ""))),
            chain_symbol=str(
                raw.get(
                    "chain_symbol",
                    raw.get("underlying", contract.get("underlying", "")),
                )
            ),
            strategy=str(raw.get("strategy", raw.get("action", ""))),
            option_type=str(raw.get("option_type", contract.get("option_type", ""))),
            side=str(raw.get("side", "")),
            position_effect=str(raw.get("position_effect", "")),
            quantity=int(raw.get("quantity", 0)),
            limit_price=float(raw.get("limit_price", raw.get("price", 0.0))),
            bid_price=float(raw.get("bid_price", raw.get("bid", contract.get("bid", 0.0)))),
            ask_price=float(raw.get("ask_price", raw.get("ask", contract.get("ask", 0.0)))),
            quote_timestamp=raw.get(
                "quote_timestamp",
                raw.get("quote_at", contract.get("quote_at", "")),
            ),
            expiration_date=raw.get("expiration_date", contract.get("expiration_date")),
            strike_price=raw.get(
                "strike_price",
                raw.get("strike", contract.get("strike")),
            ),
            days_to_expiration=raw.get("days_to_expiration"),
            rationale=str(
                raw.get("rationale")
                or raw.get("thesis")
                or (f"approved option packet {raw['packet_id']}" if raw.get("packet_id") else "")
            ),
            underlying_type=str(raw.get("underlying_type", "equity")),
            order_type=str(raw.get("order_type", raw.get("type", "limit"))),
            time_in_force=str(raw.get("time_in_force", "gfd")),
            market_hours=str(raw.get("market_hours", "regular_hours")),
            ref_id=str(raw.get("ref_id", "")),
            order_date=_as_date(raw.get("order_date", raw.get("valid_for_date"))),
            intent=str(raw.get("intent", raw.get("packet_id", "option_order"))),
        )

    def broker_parameters(
        self,
        action: str = "place",
        *,
        for_review: bool | None = None,
    ) -> dict[str, Any]:
        """Return the exact review_option_order or place_option_order payload."""
        if for_review is not None:
            action = "review" if for_review else "place"
        action = action.lower()
        if action not in {"review", "place"}:
            raise ValueError("action must be 'review' or 'place'")
        common: dict[str, Any] = {
            "account_number": self.account_number,
            "legs": [
                {
                    "option_id": self.option_id,
                    "side": self.side,
                    "position_effect": self.position_effect,
                    "ratio_quantity": 1,
                }
            ],
            "type": self.order_type,
            "quantity": str(self.quantity),
            "price": f"{self.limit_price:.2f}",
            "time_in_force": self.time_in_force,
            "market_hours": self.market_hours,
        }
        if action == "review":
            return {
                **common,
                "chain_symbol": self.chain_symbol,
                "underlying_type": self.underlying_type,
            }
        return {**common, "ref_id": self.ref_id}

    def review_parameters(self) -> dict[str, Any]:
        return self.broker_parameters("review")

    def place_parameters(self) -> dict[str, Any]:
        return self.broker_parameters("place")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_number": self.account_number,
            "option_id": self.option_id,
            "chain_symbol": self.chain_symbol,
            "strategy": self.strategy,
            "option_type": self.option_type,
            "side": self.side,
            "position_effect": self.position_effect,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "quote_timestamp": (
                self.quote_timestamp.isoformat()
                if isinstance(self.quote_timestamp, datetime)
                else self.quote_timestamp
            ),
            "expiration_date": (
                self.expiration_date.isoformat()
                if isinstance(self.expiration_date, date)
                else self.expiration_date
            ),
            "strike_price": self.strike_price,
            "days_to_expiration": self.days_to_expiration,
            "rationale": self.rationale,
            "underlying_type": self.underlying_type,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "market_hours": self.market_hours,
            "ref_id": self.ref_id,
            "intent": self.intent,
        }


@dataclass(frozen=True)
class OptionOrderDecision:
    order: ProposedOptionOrder
    approved: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = {**self.order.to_dict(), "approved": self.approved, "reasons": list(self.reasons)}
        if self.approved:
            payload["review_parameters"] = self.order.review_parameters()
            payload["place_parameters"] = self.order.place_parameters()
        return payload


def _strategy_shape_reasons(order: ProposedOptionOrder) -> list[str]:
    expected = {
        "long_call": ("call", "buy", "open"),
        "long_put": ("put", "buy", "open"),
        "covered_call": ("call", "sell", "open"),
        "cash_secured_put": ("put", "sell", "open"),
    }
    if order.strategy == "close":
        return [] if order.position_effect == "close" else ["close_strategy_requires_close_effect"]
    shape = expected.get(order.strategy)
    if shape is None:
        return []
    actual = (order.option_type, order.side, order.position_effect)
    return [] if actual == shape else ["strategy_leg_mismatch"]


def evaluate_option_order(
    order: ProposedOptionOrder,
    account: OptionAccountSnapshot,
    limits: OptionExecutionLimits | None = None,
    root: str | Path = ".",
    now: datetime | None = None,
) -> OptionOrderDecision:
    """Approve only a fully known, bounded single-leg Level-2 order."""
    limits = limits or OptionExecutionLimits()
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    reasons = list(account.external_halt_reasons)

    if kill_switch_engaged(root):
        reasons.append("kill_switch_file_present")
    if not account.agentic_allowed:
        reasons.append("account_not_agentic_allowed")
    if account.account_number != order.account_number:
        reasons.append("order_account_mismatch")
    if _option_level(account.option_level) < limits.minimum_option_level:
        reasons.append("option_level_2_required")
    if account.equity <= 0:
        reasons.append("non_positive_equity")
    if account.orders_today >= limits.max_orders_per_day:
        reasons.append("daily_order_count_limit_reached")
    if account.orders_source != "broker":
        reasons.append("option_order_count_not_broker_verified")
    if not account.session_is_regular:
        reasons.append("outside_regular_trading_session")

    if order.strategy not in limits.allowed_strategies:
        reasons.append("unsupported_option_strategy")
    reasons.extend(_strategy_shape_reasons(order))
    if order.option_type not in {"call", "put"}:
        reasons.append("unsupported_option_type")
    if order.side not in {"buy", "sell"}:
        reasons.append("unsupported_side")
    if order.position_effect not in {"open", "close"}:
        reasons.append("unsupported_position_effect")
    if order.quantity != limits.max_contracts_per_order:
        reasons.append("option_order_must_be_one_contract")
    if not order.option_id.strip():
        reasons.append("missing_option_id")
    if not order.chain_symbol.strip():
        reasons.append("missing_chain_symbol")
    if not order.rationale.strip():
        reasons.append("missing_rationale")

    if order.order_type != "limit":
        reasons.append("option_orders_must_be_limit")
    if order.time_in_force != "gfd":
        reasons.append("option_orders_must_be_gfd")
    if order.market_hours != "regular_hours":
        reasons.append("option_orders_must_use_regular_hours")
    if order.limit_price <= 0:
        reasons.append("limit_order_requires_positive_price")

    quote_at = _as_datetime(order.quote_timestamp)
    if quote_at is None:
        reasons.append("missing_or_invalid_quote_timestamp")
    else:
        quote_age = (now - quote_at).total_seconds()
        if quote_age < -1:
            reasons.append("quote_timestamp_in_future")
        elif quote_age > limits.max_quote_age_seconds:
            reasons.append("option_quote_stale")
    if order.bid_price <= 0:
        reasons.append("option_bid_must_be_positive")
    if order.ask_price <= 0:
        reasons.append("option_ask_must_be_positive")
    if order.ask_price < order.bid_price:
        reasons.append("option_quote_crossed")
    elif order.bid_price > 0 and order.ask_price > 0:
        midpoint = (order.bid_price + order.ask_price) / 2.0
        spread = (order.ask_price - order.bid_price) / midpoint if midpoint > 0 else float("inf")
        if spread > limits.max_spread_fraction + 1e-12:
            reasons.append("option_spread_exceeds_limit")

    opening = order.position_effect == "open"
    if opening:
        dte = order.dte(now.date())
        if dte is None:
            reasons.append("missing_expiration")
        elif not limits.min_entry_dte <= dte <= limits.max_entry_dte:
            reasons.append("entry_dte_outside_allowed_range")
        if account.option_openings_today >= limits.max_openings_per_day:
            reasons.append("daily_option_opening_limit_reached")
        if account.open_option_positions >= limits.max_open_option_positions:
            reasons.append("max_open_option_positions_reached")
        if account.mandatory_close_option_ids:
            reasons.append("mandatory_option_closes_pending")

    reserve = max(account.equity, 0.0) * limits.min_cash_reserve_weight
    if opening and order.strategy in {"long_call", "long_put"}:
        debit = order.premium_notional
        order_cap = min(
            limits.max_long_debit,
            max(account.equity, 0.0) * limits.max_long_debit_equity_weight,
        )
        if debit > order_cap + 1e-9:
            reasons.append("long_option_debit_exceeds_cap")
        aggregate_cap = max(account.equity, 0.0) * limits.max_aggregate_long_debit_weight
        if account.aggregate_long_debit + debit > aggregate_cap + 1e-9:
            reasons.append("aggregate_long_option_debit_exceeds_cap")
        if debit > max(account.settled_cash - reserve, 0.0) + 1e-9:
            reasons.append("insufficient_settled_cash_after_reserve")

    if opening and order.strategy == "covered_call":
        available_shares = account.shares(order.chain_symbol) - (
            account.covered_contracts(order.chain_symbol) * 100
        )
        if available_shares < order.quantity * 100:
            reasons.append("insufficient_shares_for_covered_call")

    if opening and order.strategy == "cash_secured_put":
        if order.strike_price is None or order.strike_price <= 0:
            reasons.append("cash_secured_put_requires_positive_strike")
        else:
            collateral = order.strike_price * order.quantity * 100
            collateral_cap = max(account.equity, 0.0) * limits.max_csp_collateral_weight
            if account.csp_collateral + collateral > collateral_cap + 1e-9:
                reasons.append("cash_secured_put_collateral_exceeds_cap")
            projected_underlying = account.underlying_value(order.chain_symbol) + collateral
            assignment_cap = max(account.equity, 0.0) * limits.max_post_assignment_weight
            if projected_underlying > assignment_cap + 1e-9:
                reasons.append("post_assignment_underlying_weight_exceeds_cap")
            if collateral > max(account.settled_cash - reserve, 0.0) + 1e-9:
                reasons.append("insufficient_settled_cash_after_reserve")

    if order.position_effect == "close" and account.mandatory_close_option_ids:
        if order.option_id not in account.mandatory_close_option_ids:
            reasons.append("close_does_not_address_mandatory_position")

    return OptionOrderDecision(order=order, approved=not reasons, reasons=tuple(reasons))


def evaluate_option_batch(
    orders: list[ProposedOptionOrder],
    account: OptionAccountSnapshot,
    limits: OptionExecutionLimits | None = None,
    root: str | Path = ".",
    now: datetime | None = None,
) -> list[OptionOrderDecision]:
    """Evaluate sequentially so approvals consume risk, cash, and daily limits."""
    limits = limits or OptionExecutionLimits()
    decisions: list[OptionOrderDecision] = []
    running = account
    for order in orders:
        decision = evaluate_option_order(order, running, limits, root, now)
        decisions.append(decision)
        if not decision.approved:
            continue

        opening = order.position_effect == "open"
        cash = running.cash
        aggregate_debit = running.aggregate_long_debit
        csp_collateral = running.csp_collateral
        covered = dict(running.covered_call_contracts)
        mandatory = list(running.mandatory_close_option_ids)
        open_positions = running.open_option_positions
        openings_today = running.option_openings_today
        if opening:
            open_positions += 1
            openings_today += 1
            if order.strategy in {"long_call", "long_put"}:
                cash -= order.premium_notional
                aggregate_debit += order.premium_notional
            elif order.strategy == "cash_secured_put" and order.strike_price is not None:
                collateral = order.strike_price * order.quantity * 100
                cash -= collateral
                csp_collateral += collateral
            elif order.strategy == "covered_call":
                symbol = order.chain_symbol
                covered[symbol] = covered.get(symbol, 0) + order.quantity
        else:
            open_positions = max(open_positions - order.quantity, 0)
            if order.option_id in mandatory:
                mandatory.remove(order.option_id)

        running = replace(
            running,
            cash=cash,
            open_option_positions=open_positions,
            option_openings_today=openings_today,
            orders_today=running.orders_today + 1,
            aggregate_long_debit=aggregate_debit,
            csp_collateral=csp_collateral,
            covered_call_contracts=covered,
            mandatory_close_option_ids=tuple(mandatory),
        )
    return decisions


def _native_option_legs(order: dict[str, Any]) -> list[dict[str, Any]]:
    legs = order.get("legs")
    if isinstance(legs, list):
        return [leg for leg in legs if isinstance(leg, dict)]
    option_id = order.get("option_id")
    if option_id:
        return [
            {
                "option_id": option_id,
                "side": order.get("side"),
                "position_effect": order.get("position_effect"),
            }
        ]
    return []


def summarize_broker_option_orders(orders: list[dict[str, Any]]) -> tuple[int, float]:
    """Return opening-order count and conservatively submitted premium notional."""
    openings = 0
    premium_notional = 0.0
    for order in orders:
        legs = _native_option_legs(order)
        if not legs:
            raise ValueError("Broker option order is missing legs")
        quantity = _number(order.get("quantity"))
        price = _number(order.get("price"))
        if price is None:
            price = _number(order.get("limit_price"))
        if price is None:
            price = _number(order.get("average_price"))
        if quantity is None or quantity <= 0 or quantity % 1:
            raise ValueError("Broker option order is missing a whole-contract quantity")
        if price is None or price < 0:
            raise ValueError("Cannot determine broker option-order premium")
        if any(str(leg.get("position_effect", "")).lower() == "open" for leg in legs):
            openings += 1
        premium_notional += quantity * price * 100
    return openings, premium_notional
