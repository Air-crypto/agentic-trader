"""Strict post-trade reconciliation for native Robinhood option orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .execution import append_audit_record
from .reconcile import engage_kill_switch

KNOWN_OPTION_ORDER_STATES = {
    "queued",
    "confirmed",
    "filled",
    "partially_filled",
    "rejected",
    "cancelled",
    "failed",
    "voided",
    "pending_cancelled",
}
PENDING_OPTION_ORDER_STATES = {
    "queued",
    "confirmed",
    "pending_cancelled",
}
TERMINAL_UNFILLED_OPTION_ORDER_STATES = {
    "rejected",
    "cancelled",
    "failed",
    "voided",
}


def _number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _option_id(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("option_id") or value.get("id") or value.get("url") or value.get("instrument")
        )
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        path = urlparse(text).path.rstrip("/")
        return path.rsplit("/", 1)[-1]
    return text.rstrip("/").rsplit("/", 1)[-1]


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    order = getattr(value, "order", None)
    if order is not None and callable(getattr(order, "to_dict", None)):
        return order.to_dict()
    raise TypeError("Option reconciliation inputs must be dict-like")


def _raw_legs(raw: dict[str, Any]) -> list[dict[str, Any]]:
    legs = raw.get("legs")
    if isinstance(legs, list):
        return [leg for leg in legs if isinstance(leg, dict)]
    if raw.get("option_id") or raw.get("option"):
        return [
            {
                "option_id": raw.get("option_id") or raw.get("option"),
                "side": raw.get("side"),
                "position_effect": raw.get("position_effect"),
                "ratio_quantity": raw.get("ratio_quantity", 1),
                "executions": raw.get("executions"),
            }
        ]
    return []


def _fingerprint(raw: dict[str, Any]) -> tuple[tuple[str, str, str, int], ...]:
    normalized: list[tuple[str, str, str, int]] = []
    for leg in _raw_legs(raw):
        instrument = (
            leg.get("option_id")
            or leg.get("option")
            or leg.get("instrument")
            or leg.get("option_instrument")
        )
        ratio = _number(leg.get("ratio_quantity"))
        if ratio is None:
            ratio = _number(leg.get("ratio"))
        normalized.append(
            (
                _option_id(instrument),
                str(leg.get("side", "")).lower(),
                str(leg.get("position_effect", "")).lower(),
                int(ratio or 1),
            )
        )
    return tuple(sorted(normalized))


def _filled_quantity(raw: dict[str, Any]) -> float | None:
    for key in ("processed_quantity", "cumulative_quantity", "filled_quantity"):
        parsed = _number(raw.get(key))
        if parsed is not None:
            return parsed
    leg_quantities: list[float] = []
    for leg in _raw_legs(raw):
        parsed = _number(leg.get("executed_quantity"))
        if parsed is None:
            executions = leg.get("executions")
            if isinstance(executions, list) and executions:
                quantities = [_number(item.get("quantity")) for item in executions]
                if all(value is not None for value in quantities):
                    parsed = sum(value for value in quantities if value is not None)
        if parsed is not None:
            ratio = _number(leg.get("ratio_quantity")) or 1
            leg_quantities.append(parsed / ratio)
    if leg_quantities:
        return min(leg_quantities)
    return _number(raw.get("quantity"))


def _average_fill_price(raw: dict[str, Any]) -> float | None:
    for key in ("average_net_price", "average_price", "processed_premium"):
        parsed = _number(raw.get(key))
        if parsed is not None:
            return parsed

    # Robinhood may put executions on the leg rather than the parent order.
    totals: list[tuple[float, float]] = []
    for leg in _raw_legs(raw):
        executions = leg.get("executions")
        if not isinstance(executions, list):
            continue
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            price = _number(execution.get("price"))
            quantity = _number(execution.get("quantity"))
            if price is not None and quantity is not None and quantity > 0:
                totals.append((price * quantity, quantity))
    if totals:
        notional = sum(value for value, _ in totals)
        quantity = sum(value for _, value in totals)
        return notional / quantity
    return None


@dataclass(frozen=True)
class ExecutedOptionOrder:
    order_id: str
    ref_id: str
    state: str
    quantity: float | None
    average_fill_price: float | None
    direction: str
    leg_fingerprint: tuple[tuple[str, str, str, int], ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutedOptionOrder:
        fingerprint = _fingerprint(raw)
        direction = str(raw.get("direction", "")).lower()
        if not direction and len(fingerprint) == 1:
            direction = "debit" if fingerprint[0][1] == "buy" else "credit"
        return cls(
            order_id=str(raw.get("order_id") or raw.get("id") or ""),
            ref_id=str(raw.get("ref_id") or raw.get("client_order_id") or ""),
            state=str(raw.get("state") or "").lower(),
            quantity=_filled_quantity(raw),
            average_fill_price=_average_fill_price(raw),
            direction=direction,
            leg_fingerprint=fingerprint,
        )


def _approval_summary(raw: dict[str, Any]) -> dict[str, Any]:
    quantity = _number(raw.get("quantity"))
    limit_price = _number(raw.get("limit_price"))
    if limit_price is None:
        limit_price = _number(raw.get("price"))
    fingerprint = _fingerprint(raw)
    direction = str(raw.get("direction", "")).lower()
    if not direction and len(fingerprint) == 1:
        direction = "debit" if fingerprint[0][1] == "buy" else "credit"
    return {
        "ref_id": str(raw.get("ref_id") or ""),
        "quantity": quantity,
        "limit_price": limit_price,
        "direction": direction,
        "fingerprint": fingerprint,
    }


def _public_order(order: ExecutedOptionOrder) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "ref_id": order.ref_id,
        "state": order.state,
        "quantity": order.quantity,
        "average_fill_price": order.average_fill_price,
        "leg_fingerprint": [list(leg) for leg in order.leg_fingerprint],
    }


def reconcile_option_orders(
    approved_orders: list[Any],
    broker_orders: list[dict[str, Any]],
    root: str | Path = ".",
    engage_on_breach: bool = True,
) -> dict[str, Any]:
    """Match native option fills exactly and halt on any ambiguous execution."""
    approvals = [_approval_summary(_as_mapping(value)) for value in approved_orders]
    executed = [ExecutedOptionOrder.from_dict(raw) for raw in broker_orders]

    breaches: list[str] = []
    matched: list[dict[str, Any]] = []
    unauthorized: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    terminal_unfilled: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    price_breaches: list[dict[str, Any]] = []

    approval_ref_counts: dict[str, int] = {}
    for approval in approvals:
        ref_id = approval["ref_id"]
        if ref_id:
            approval_ref_counts[ref_id] = approval_ref_counts.get(ref_id, 0) + 1
    duplicate_approval_refs = sorted(
        ref_id for ref_id, count in approval_ref_counts.items() if count > 1
    )
    if duplicate_approval_refs:
        breaches.append("duplicate_option_approval_detected")
        duplicates.extend(
            {"kind": "approval_ref_id", "value": ref_id} for ref_id in duplicate_approval_refs
        )

    seen_order_ids: set[str] = set()
    seen_ref_ids: set[str] = set()
    unique_executed: list[ExecutedOptionOrder] = []
    for order in executed:
        duplicate_kind = None
        duplicate_value = ""
        if order.order_id and order.order_id in seen_order_ids:
            duplicate_kind, duplicate_value = "broker_order_id", order.order_id
        elif order.ref_id and order.ref_id in seen_ref_ids:
            duplicate_kind, duplicate_value = "broker_ref_id", order.ref_id
        if duplicate_kind:
            duplicates.append(
                {**_public_order(order), "kind": duplicate_kind, "value": duplicate_value}
            )
            continue
        if order.order_id:
            seen_order_ids.add(order.order_id)
        if order.ref_id:
            seen_ref_ids.add(order.ref_id)
        unique_executed.append(order)
    if len(unique_executed) != len(executed):
        breaches.append("duplicate_option_order_detected")

    unconsumed = list(approvals)
    for order in unique_executed:
        public = _public_order(order)
        if order.state not in KNOWN_OPTION_ORDER_STATES:
            unknown.append(public)
            continue
        if order.state == "partially_filled":
            partial_match_index = next(
                (
                    index
                    for index, approval in enumerate(unconsumed)
                    if approval["ref_id"]
                    and order.ref_id == approval["ref_id"]
                    and order.leg_fingerprint == approval["fingerprint"]
                ),
                None,
            )
            if partial_match_index is None:
                unauthorized.append(public)
            else:
                unconsumed.pop(partial_match_index)
                partial.append(public)
            continue
        match_index = next(
            (
                index
                for index, approval in enumerate(unconsumed)
                if approval["ref_id"]
                and order.ref_id == approval["ref_id"]
                and order.leg_fingerprint == approval["fingerprint"]
                and order.quantity is not None
                and approval["quantity"] is not None
                and abs(order.quantity - approval["quantity"]) <= 1e-9
            ),
            None,
        )
        if match_index is None:
            unauthorized.append(public)
            continue

        approval = unconsumed.pop(match_index)
        if order.state in PENDING_OPTION_ORDER_STATES:
            pending.append(public)
            continue
        if order.state in TERMINAL_UNFILLED_OPTION_ORDER_STATES:
            terminal_unfilled.append(public)
            continue

        limit_price = approval["limit_price"]
        fill_price = order.average_fill_price
        direction = approval["direction"]
        if limit_price is None or fill_price is None or direction not in {"debit", "credit"}:
            unknown.append({**public, "reason": "missing_net_fill_or_limit"})
            continue

        adverse = (direction == "debit" and fill_price > limit_price + 1e-9) or (
            direction == "credit" and fill_price < limit_price - 1e-9
        )
        if adverse:
            price_breaches.append(
                {
                    **public,
                    "direction": direction,
                    "limit_price": limit_price,
                    "average_fill_price": fill_price,
                }
            )
        matched.append(
            {
                **public,
                "direction": direction,
                "limit_price": limit_price,
            }
        )

    if unauthorized:
        breaches.append("unauthorized_option_fill_detected")
    if partial:
        breaches.append("partial_option_fill_detected")
    if unknown:
        breaches.append("unknown_option_order_detected")
    if price_breaches:
        breaches.append("option_fill_price_worse_than_limit")

    approved_missing = [
        {
            "ref_id": approval["ref_id"],
            "quantity": approval["quantity"],
            "leg_fingerprint": [list(leg) for leg in approval["fingerprint"]],
        }
        for approval in unconsumed
    ]
    result: dict[str, Any] = {
        "reconciled_at": datetime.now(UTC).isoformat(),
        "clean": not breaches and not pending and not approved_missing,
        "complete": not pending and not approved_missing,
        "breaches": breaches,
        "matched": matched,
        "unauthorized": unauthorized,
        "partial": partial,
        "pending": pending,
        "terminal_unfilled": terminal_unfilled,
        "unknown": unknown,
        "duplicates": duplicates,
        "price_breaches": price_breaches,
        "approved_but_unfilled": approved_missing,
    }
    if breaches and engage_on_breach:
        engage_kill_switch("; ".join(breaches), root)
        result["kill_switch_engaged"] = True

    append_audit_record({"event": "option_reconcile", **result}, root=root)
    return result


def reconcile_options(
    approved_orders: list[Any],
    broker_orders: list[dict[str, Any]],
    root: str | Path = ".",
    engage_on_breach: bool = True,
) -> dict[str, Any]:
    """Compatibility alias for callers that use the shorter function name."""
    return reconcile_option_orders(approved_orders, broker_orders, root, engage_on_breach)
