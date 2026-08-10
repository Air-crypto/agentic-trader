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


@dataclass(frozen=True)
class ExecutedOrder:
    symbol: str
    side: str
    notional: float
    average_price: float
    order_id: str
    state: str = "filled"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutedOrder:
        return cls(
            symbol=str(raw["symbol"]).upper(),
            side=str(raw["side"]).lower(),
            notional=float(raw["notional"]),
            average_price=float(raw.get("average_price") or 0.0),
            order_id=str(raw.get("order_id", "")),
            state=str(raw.get("state", "filled")).lower(),
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
        limit_price = approval.get("limit_price")
        deviation = None
        if limit_price and order.average_price > 0:
            deviation = (order.average_price - float(limit_price)) / float(limit_price)
            # Only fills worse than the limit are a problem; buying below the
            # limit or selling above it is price improvement.
            adverse = deviation if order.side == "buy" else -deviation
            if adverse > PRICE_DEVIATION_LIMIT:
                price_breaches.append(
                    {
                        "symbol": order.symbol,
                        "order_id": order.order_id,
                        "limit_price": float(limit_price),
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
