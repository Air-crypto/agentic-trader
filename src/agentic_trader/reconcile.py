"""Post-trade reconciliation against the guard-approved plan.

The pre-trade guard cannot physically block an order, because order placement
happens over MCP rather than inside this process. Reconciliation is the
compensating control: any fill that no approved plan authorized engages the kill
switch, so an agent that bypasses the guard gets at most one order through
before trading halts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

from .cloud_runtime import (
    _native_equity_order_parameters,
    _normalized_equity_broker_parameters,
)
from .execution import KILL_SWITCH_FILENAME, append_audit_record

# Fills drift from the plan for legitimate reasons: fractional rounding and
# movement between planning and execution. These bound "legitimate".
NOTIONAL_TOLERANCE = 0.05
PRICE_DEVIATION_LIMIT = 0.005
TERMINAL_UNFILLED_STATES = {"cancelled", "canceled", "failed", "rejected", "voided"}


def _first_number(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("amount")
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(parsed):
            return parsed
    return None


@dataclass(frozen=True)
class ExecutedOrder:
    symbol: str
    side: str
    notional: float | None
    average_price: float | None
    order_id: str
    state: str
    ref_id: str = ""
    parameter_fingerprint: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutedOrder:
        """Accept Robinhood's own order shape as well as a normalized one.

        The broker returns `id`, `dollar_based_amount`, and `cumulative_quantity`
        rather than `order_id` and `notional`, and returns numbers as strings.
        Requiring a hand transformation would put a translation step between the
        plan and the check meant to verify it.
        """
        average_price = _first_number(raw, "average_price")
        filled_quantity = _first_number(raw, "cumulative_quantity", "quantity")
        notional = _first_number(raw, "notional", "dollar_based_amount")
        if filled_quantity is not None and average_price is not None:
            computed = filled_quantity * average_price
            notional = computed if isfinite(computed) else None
        try:
            parameter_fingerprint = _native_equity_order_parameters(
                raw,
                require_execution_fields=True,
            )
        except ValueError:
            parameter_fingerprint = None
        return cls(
            symbol=str(raw.get("symbol") or "").upper(),
            side=str(raw.get("side") or "").lower(),
            notional=notional,
            average_price=average_price,
            order_id=str(raw.get("order_id") or raw.get("id") or ""),
            state=str(raw.get("state") or "").lower(),
            ref_id=str(raw.get("ref_id") or raw.get("client_order_id") or ""),
            parameter_fingerprint=parameter_fingerprint,
        )

    def fill_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.symbol:
            issues.append("missing_symbol")
        if self.side not in {"buy", "sell"}:
            issues.append("missing_or_invalid_side")
        if not self.order_id:
            issues.append("missing_order_id")
        if self.notional is None or self.notional <= 0:
            issues.append("missing_or_invalid_notional")
        if self.average_price is None or self.average_price <= 0:
            issues.append("missing_or_invalid_average_price")
        return issues

    def summary(self, **extra: Any) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "notional": self.notional,
            "average_price": self.average_price,
            "order_id": self.order_id,
            "state": self.state,
            "ref_id": self.ref_id,
            **extra,
        }


def engage_kill_switch(reason: str, root: str | Path = ".") -> Path:
    path = Path(root) / KILL_SWITCH_FILENAME
    stamp = datetime.now(UTC).isoformat()
    path.write_text(f"Engaged {stamp}\nReason: {reason}\nRemove only after human review.\n")
    return path


def reconcile(
    approved_orders: list[dict[str, Any]],
    executed_orders: list[dict[str, Any]],
    root: str | Path = ".",
    engage_on_breach: bool = True,
) -> dict[str, Any]:
    """Match fills to approvals and halt trading on anything unaccounted for."""
    executed = [ExecutedOrder.from_dict(raw) for raw in executed_orders]
    filled: list[ExecutedOrder] = []
    invalid: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    nonterminal: list[dict[str, Any]] = []
    terminal_unfilled: list[dict[str, Any]] = []
    for order in executed:
        if not order.state:
            invalid.append(order.summary(issues=["missing_order_state"]))
        elif order.state == "filled":
            issues = order.fill_issues()
            if issues:
                invalid.append(order.summary(issues=issues))
            else:
                filled.append(order)
        elif order.state == "partially_filled":
            partial.append(order.summary())
        elif order.state in TERMINAL_UNFILLED_STATES:
            terminal_unfilled.append(order.summary())
        else:
            nonterminal.append(order.summary())

    # Each approval may be consumed once, so a duplicated fill against a single
    # approval surfaces as unauthorized rather than quietly matching twice.
    unconsumed = list(approved_orders)
    matched: list[dict[str, Any]] = []
    unauthorized: list[dict[str, Any]] = []
    price_breaches: list[dict[str, Any]] = []

    for order in filled:
        match_index = None
        mismatch_reason = "no_matching_approval"
        # A ref_id narrows the candidate set, but does not authorize arbitrary
        # symbol, side, or size changes under that identifier.
        if order.ref_id:
            ref_index = next(
                (
                    index
                    for index, approval in enumerate(unconsumed)
                    if str(approval.get("ref_id") or "") == order.ref_id
                ),
                None,
            )
            if ref_index is None:
                mismatch_reason = "ref_id_not_approved_or_already_consumed"
            else:
                approval = unconsumed[ref_index]
                approved_notional = _first_number(approval, "notional")
                same_instrument = (
                    str(approval.get("symbol") or "").upper() == order.symbol
                    and str(approval.get("side") or "").lower() == order.side
                )
                within_size = (
                    approved_notional is not None
                    and order.notional is not None
                    and abs(order.notional - approved_notional)
                    <= max(approved_notional * NOTIONAL_TOLERANCE, 1.0)
                )
                expected_parameters = approval.get("broker_parameters")
                exact_parameters = True
                if isinstance(expected_parameters, dict):
                    try:
                        exact_parameters = order.parameter_fingerprint == (
                            _normalized_equity_broker_parameters(expected_parameters)
                        )
                    except ValueError:
                        exact_parameters = False
                if same_instrument and within_size and exact_parameters:
                    match_index = ref_index
                else:
                    mismatch_reason = "ref_id_order_fingerprint_mismatch"
        else:
            # Legacy plans without ref_ids may still be reconciled by the full
            # instrument/side/size fingerprint. Never use this fallback when
            # either side claims a ref_id.
            for index, approval in enumerate(unconsumed):
                if approval.get("ref_id"):
                    continue
                same_instrument = (
                    str(approval.get("symbol") or "").upper() == order.symbol
                    and str(approval.get("side") or "").lower() == order.side
                )
                if not same_instrument:
                    continue
                approved_notional = _first_number(approval, "notional")
                within_size = (
                    approved_notional is not None
                    and order.notional is not None
                    and abs(order.notional - approved_notional)
                    <= max(approved_notional * NOTIONAL_TOLERANCE, 1.0)
                )
                expected_parameters = approval.get("broker_parameters")
                exact_parameters = True
                if isinstance(expected_parameters, dict):
                    try:
                        exact_parameters = order.parameter_fingerprint == (
                            _normalized_equity_broker_parameters(expected_parameters)
                        )
                    except ValueError:
                        exact_parameters = False
                if within_size and exact_parameters:
                    match_index = index
                    break
            if match_index is None and any(approval.get("ref_id") for approval in unconsumed):
                mismatch_reason = "missing_ref_id"

        if match_index is None:
            unauthorized.append(order.summary(reason=mismatch_reason))
            continue

        approval = unconsumed.pop(match_index)
        # A market order has no limit, so its fill is measured against the price
        # the plan was built from. This is the only price check a fractional
        # order gets, since the broker will not accept a limit on one.
        benchmark = _first_number(approval, "limit_price", "reference_price")
        deviation = None
        if benchmark is None or benchmark <= 0:
            price_breaches.append(
                {
                    "symbol": order.symbol,
                    "order_id": order.order_id,
                    "reason": "missing_or_invalid_benchmark_price",
                }
            )
        elif order.average_price is not None:
            deviation = (order.average_price - benchmark) / benchmark
            # Only fills worse than the benchmark are a problem; buying below it
            # or selling above it is price improvement.
            adverse = deviation if order.side == "buy" else -deviation
            if adverse > PRICE_DEVIATION_LIMIT:
                price_breaches.append(
                    {
                        "symbol": order.symbol,
                        "order_id": order.order_id,
                        "benchmark_price": benchmark,
                        "average_price": order.average_price,
                        "adverse_deviation": round(adverse, 5),
                    }
                )
        matched.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "notional": order.notional,
                "order_id": order.order_id,
                "ref_id": order.ref_id,
                "price_deviation": None if deviation is None else round(deviation, 5),
            }
        )

    breaches: list[str] = []
    if unauthorized:
        breaches.append("unauthorized_fill_detected")
    if invalid:
        breaches.append("invalid_fill_detected")
    if partial:
        breaches.append("partial_fill_detected")
    if nonterminal:
        breaches.append("nonterminal_order_detected")
    if price_breaches:
        breaches.append("fill_price_outside_tolerance")

    result = {
        "reconciled_at": datetime.now(UTC).isoformat(),
        "clean": not breaches,
        "breaches": breaches,
        "matched": matched,
        "unauthorized": unauthorized,
        "invalid": invalid,
        "partial": partial,
        "nonterminal": nonterminal,
        "terminal_unfilled": terminal_unfilled,
        "price_breaches": price_breaches,
        "approved_but_unfilled": [
            {
                "symbol": str(approval.get("symbol") or "").upper(),
                "side": str(approval.get("side") or "").lower(),
            }
            for approval in unconsumed
        ],
    }

    if breaches and engage_on_breach:
        engage_kill_switch("; ".join(breaches), root)
        result["kill_switch_engaged"] = True

    append_audit_record({"event": "reconcile", **result}, root=root)
    return result
