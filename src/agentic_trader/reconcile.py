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
from pathlib import Path
from typing import Any

from .execution import KILL_SWITCH_FILENAME, append_audit_record

# Fills drift from the plan for legitimate reasons: fractional rounding and
# movement between planning and execution. These bound "legitimate".
NOTIONAL_TOLERANCE = 0.05
PRICE_DEVIATION_LIMIT = 0.005


def _first_number(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class ExecutedOrder:
    symbol: str
    side: str
    notional: float
    average_price: float
    order_id: str
    state: str = "filled"
    ref_id: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutedOrder:
        """Accept Robinhood's own order shape as well as a normalized one.

        The broker returns `id`, `dollar_based_amount`, and `cumulative_quantity`
        rather than `order_id` and `notional`, and returns numbers as strings.
        Requiring a hand transformation would put a translation step between the
        plan and the check meant to verify it.
        """
        average_price = _first_number(raw, "average_price", "price") or 0.0
        notional = _first_number(raw, "notional", "dollar_based_amount")
        if notional is None:
            filled = _first_number(raw, "cumulative_quantity", "quantity")
            notional = (filled or 0.0) * average_price
        return cls(
            symbol=str(raw["symbol"]).upper(),
            side=str(raw["side"]).lower(),
            notional=notional,
            average_price=average_price,
            order_id=str(raw.get("order_id") or raw.get("id") or ""),
            state=str(raw.get("state", "filled")).lower(),
            ref_id=str(raw.get("ref_id") or ""),
        )


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
    filled = [order for order in executed if order.state in {"filled", "partially_filled"}]

    # Each approval may be consumed once, so a duplicated fill against a single
    # approval surfaces as unauthorized rather than quietly matching twice.
    unconsumed = list(approved_orders)
    matched: list[dict[str, Any]] = []
    unauthorized: list[dict[str, Any]] = []
    price_breaches: list[dict[str, Any]] = []

    for order in filled:
        match_index = None
        # An exact ref_id match is unambiguous, so prefer it over inferring the
        # pairing from symbol, side, and size.
        if order.ref_id:
            match_index = next(
                (
                    index
                    for index, approval in enumerate(unconsumed)
                    if approval.get("ref_id") == order.ref_id
                ),
                None,
            )
        if match_index is None:
            for index, approval in enumerate(unconsumed):
                same_instrument = (
                    str(approval["symbol"]).upper() == order.symbol
                    and str(approval["side"]).lower() == order.side
                )
                if not same_instrument:
                    continue
                approved_notional = float(approval["notional"])
                within_size = abs(order.notional - approved_notional) <= max(
                    approved_notional * NOTIONAL_TOLERANCE, 1.0
                )
                if within_size:
                    match_index = index
                    break

        if match_index is None:
            unauthorized.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "notional": order.notional,
                    "order_id": order.order_id,
                }
            )
            continue

        approval = unconsumed.pop(match_index)
        # A market order has no limit, so its fill is measured against the price
        # the plan was built from. This is the only price check a fractional
        # order gets, since the broker will not accept a limit on one.
        benchmark = approval.get("limit_price") or approval.get("reference_price")
        deviation = None
        if benchmark and order.average_price > 0:
            deviation = (order.average_price - float(benchmark)) / float(benchmark)
            # Only fills worse than the benchmark are a problem; buying below it
            # or selling above it is price improvement.
            adverse = deviation if order.side == "buy" else -deviation
            if adverse > PRICE_DEVIATION_LIMIT:
                price_breaches.append(
                    {
                        "symbol": order.symbol,
                        "order_id": order.order_id,
                        "benchmark_price": float(benchmark),
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
                "price_deviation": None if deviation is None else round(deviation, 5),
            }
        )

    breaches: list[str] = []
    if unauthorized:
        breaches.append("unauthorized_fill_detected")
    if price_breaches:
        breaches.append("fill_price_outside_tolerance")

    result = {
        "reconciled_at": datetime.now(UTC).isoformat(),
        "clean": not breaches,
        "breaches": breaches,
        "matched": matched,
        "unauthorized": unauthorized,
        "price_breaches": price_breaches,
        "approved_but_unfilled": [
            {"symbol": approval["symbol"], "side": approval["side"]} for approval in unconsumed
        ],
    }

    if breaches and engage_on_breach:
        engage_kill_switch("; ".join(breaches), root)
        result["kill_switch_engaged"] = True

    append_audit_record({"event": "reconcile", **result}, root=root)
    return result
