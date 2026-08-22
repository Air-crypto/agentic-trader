from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from cryptography.exceptions import InvalidSignature

from .confirmation import confirmation_message, verify_confirmation_signature

DATABASE_URL_ENV = "DATABASE_URL"
EXPECTED_CLOUD_DATABASE_ROLE = "postgres"
SUPABASE_API_ROLES = ("anon", "authenticated", "service_role")
REQUIRED_PRIVATE_PUBLIC_OBJECTS = frozenset(
    {
        ("function", "public.reject_learning_row_mutation()", "f"),
        ("function", "public.reject_option_packet_content_update()", "f"),
        ("function", "public.reject_pending_research_content_update()", "f"),
        ("function", "public.validate_complete_learning_batch()", "f"),
        ("function", "public.validate_learning_outcome_insert()", "f"),
        ("function", "public.validate_learning_prediction_insert()", "f"),
        ("function", "public.validate_learning_promotion_insert()", "f"),
        ("relation", "public.learning_current_state", "v"),
        ("sequence", "public.picker_order_events_event_id_seq", "S"),
    }
)
NONTERMINAL_ATTEMPT_STATES = frozenset(
    {
        "prepared",
        "reserved",
        "submitting",
        "submitted",
        "unknown",
        "partially_filled",
        "filled",
    }
)
SUBMISSION_AUTHORITY_STATES = frozenset({"reserved", "submitting"})
TERMINAL_ATTEMPT_STATES = frozenset(
    {"cancelled", "rejected", "failed", "expired", "invalidated", "reconciled"}
)
ATTEMPT_TRANSITIONS = {
    "prepared": {"reserved", "unknown", "failed", "expired", "invalidated"},
    "reserved": {"submitting", "unknown", "failed", "expired", "invalidated"},
    "submitting": {
        "submitted",
        "unknown",
        "partially_filled",
        "filled",
        "cancelled",
        "rejected",
    },
    "submitted": {"partially_filled", "filled", "cancelled", "rejected", "unknown"},
    "unknown": {"submitted", "partially_filled", "filled", "cancelled", "rejected"},
    "partially_filled": {"filled", "cancelled", "unknown"},
    "filled": {"reconciled"},
    "cancelled": {"reconciled"},
    "rejected": {"reconciled"},
    "failed": {"reconciled"},
    "expired": {"reconciled"},
    "invalidated": {"reconciled"},
    "reconciled": set(),
}


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def sanitize_source_uri(source_uri: str | None) -> str | None:
    """Return a credential-free durable web origin/path for an artifact source."""

    if source_uri is None:
        return None
    candidate = source_uri.strip()
    if not candidate:
        raise ValueError("Artifact source URI cannot be empty")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Artifact source URI is invalid") from error
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Artifact source URI must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("Artifact source URI must not contain credentials")
    if any(character.isspace() for character in parsed.hostname):
        raise ValueError("Artifact source URI host is invalid")
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    # Query strings and fragments frequently carry tokens. They are not required
    # to establish the durable source document and are therefore never stored.
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _confirmation_authority(plan_id: str, review_hash: str, signature: str) -> str:
    try:
        return verify_confirmation_signature(plan_id, review_hash, signature)
    except InvalidSignature as error:
        raise ValueError("Confirmation signature is invalid") from error


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Cloud runtime timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stable_id(kind: str, *parts: object) -> str:
    material = ":".join([kind, *(str(part) for part in parts)])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def order_attempt_id(plan_id: str, ref_id: str) -> str:
    """Derive the one durable attempt identity for an approved order."""

    return _stable_id("execution-attempt", plan_id, ref_id)


def _nyse_execution_session(value: datetime) -> date:
    local = _utc(value).astimezone(ZoneInfo("America/New_York"))
    candidate = local.date() + (timedelta(days=1) if local.hour >= 20 else timedelta())
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=candidate,
        end_date=candidate + timedelta(days=10),
    )
    if schedule.empty:
        raise ValueError("NYSE calendar has no execution session for this plan")
    return schedule.index[0].date()


def _migration_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class SchemaStatus:
    current: bool
    expected: dict[str, str]
    applied: dict[str, str]
    missing: tuple[str, ...]
    drifted: tuple[str, ...]
    unexpected: tuple[str, ...]
    security_violations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_schema_status(
    expected: dict[str, str],
    applied: dict[str, str],
    *,
    security_violations: tuple[str, ...] = (),
) -> SchemaStatus:
    missing = tuple(sorted(set(expected) - set(applied)))
    drifted = tuple(
        sorted(
            version
            for version in set(expected) & set(applied)
            if expected[version] != applied[version]
        )
    )
    unexpected = tuple(sorted(set(applied) - set(expected)))
    return SchemaStatus(
        current=not missing and not drifted and not unexpected and not security_violations,
        expected=expected,
        applied=applied,
        missing=missing,
        drifted=drifted,
        unexpected=unexpected,
        security_violations=security_violations,
    )


@dataclass(frozen=True)
class RunLease:
    run_id: str
    task_name: str
    scheduled_for: datetime
    git_sha: str
    lease_token: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    draft_hash: str
    run_id: str
    account_key: str
    trade_date: date
    research_batch_id: str
    snapshot_hash: str
    planned_at: datetime
    expires_at: datetime
    payload: dict[str, Any]
    status: str = "draft"

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        account_key: str,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
    ) -> ExecutionPlan:
        planned_at = _utc(datetime.fromisoformat(str(payload["planned_at"]).replace("Z", "+00:00")))
        expires_at = _utc(datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")))
        if expires_at <= planned_at or expires_at - planned_at > timedelta(minutes=5):
            raise ValueError(
                "Execution plan validity must be positive and no longer than five minutes"
            )
        snapshot_hash = canonical_hash(snapshot)
        immutable = {
            "run_id": run_id,
            "account_key": account_key,
            "snapshot_hash": snapshot_hash,
            "payload": payload,
        }
        draft_hash = canonical_hash(immutable)
        raw_trade_date = payload.get("trade_date")
        orders = payload.get("approved_orders", [])
        if orders and raw_trade_date is None:
            raise ValueError("Executable durable plans require an explicit NYSE trade_date")
        trade_date = (
            date.fromisoformat(str(raw_trade_date))
            if raw_trade_date is not None
            else planned_at.date()
        )
        return cls(
            plan_id=_stable_id("execution-plan", draft_hash),
            draft_hash=draft_hash,
            run_id=run_id,
            account_key=account_key,
            trade_date=trade_date,
            research_batch_id=str(payload.get("research_batch_id", "")),
            snapshot_hash=snapshot_hash,
            planned_at=planned_at,
            expires_at=expires_at,
            payload=deepcopy(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanReview:
    plan_id: str
    draft_hash: str
    review_hash: str
    review_payload: dict[str, Any]
    reviewed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionConfirmation:
    confirmation_id: str
    plan_id: str
    review_hash: str
    actor_ref: str
    confirmed_at: datetime
    expires_at: datetime
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderAttempt:
    attempt_id: str
    plan_id: str
    confirmation_id: str
    account_key: str
    ref_id: str
    request_hash: str
    broker_request: dict[str, Any]
    state: str
    broker_order_id: str | None
    latest_response: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Reconciliation:
    reconciliation_id: str
    plan_id: str
    result_hash: str
    clean: bool
    payload: dict[str, Any]
    reconciled_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expected_review_refs(plan: ExecutionPlan) -> dict[str, dict[str, Any]]:
    orders = plan.payload.get("approved_orders", [])
    if not isinstance(orders, list):
        raise ValueError("Execution plan approved_orders must be a list")
    if len(orders) > 1:
        raise ValueError(
            "A durable execution plan may authorize at most one order; "
            "each order requires its own review and confirmation"
        )
    expected: dict[str, dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("Every approved plan order must be an object")
        ref_id = str(order.get("ref_id", ""))
        parameters = order.get("broker_parameters")
        if not ref_id or not isinstance(parameters, dict):
            raise ValueError("Every approved plan order needs ref_id and broker_parameters")
        if ref_id in expected:
            raise ValueError("Every approved plan order needs a unique ref_id")
        expected[ref_id] = parameters
    return expected


def _require_single_order_authority(plan: ExecutionPlan) -> dict[str, dict[str, Any]]:
    expected = _expected_review_refs(plan)
    if len(expected) != 1:
        raise ValueError("A broker review or confirmation requires exactly one approved order")
    return expected


def _sole_approved_order(plan: ExecutionPlan) -> dict[str, Any]:
    _require_single_order_authority(plan)
    return plan.payload["approved_orders"][0]


def _order_uses_entry_budget(order: dict[str, Any]) -> bool:
    return not (
        str(order.get("side") or "").strip().lower() == "sell"
        and str(order.get("intent_class") or "").strip().lower() in {"mandatory_exit", "close"}
    )


def _validate_plan_integrity(plan: ExecutionPlan) -> None:
    try:
        payload_planned_at = _utc(
            datetime.fromisoformat(str(plan.payload["planned_at"]).replace("Z", "+00:00"))
        )
        payload_expires_at = _utc(
            datetime.fromisoformat(str(plan.payload["expires_at"]).replace("Z", "+00:00"))
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Execution plan payload timestamps are malformed") from error
    raw_trade_date = plan.payload.get("trade_date")
    if plan.payload.get("approved_orders") and raw_trade_date is None:
        raise ValueError("Executable durable plans require an explicit NYSE trade_date")
    payload_trade_date = (
        date.fromisoformat(str(raw_trade_date))
        if raw_trade_date is not None
        else payload_planned_at.date()
    )
    if raw_trade_date is not None and payload_trade_date != _nyse_execution_session(
        payload_planned_at
    ):
        raise ValueError("Execution plan trade_date is not its actual NYSE session")
    if (
        payload_planned_at != _utc(plan.planned_at)
        or payload_expires_at != _utc(plan.expires_at)
        or payload_expires_at <= payload_planned_at
        or payload_expires_at - payload_planned_at > timedelta(minutes=5)
        or plan.trade_date != payload_trade_date
        or plan.research_batch_id != str(plan.payload.get("research_batch_id", ""))
    ):
        raise ValueError("Execution plan denormalized authority differs from its payload")
    expected_hash = canonical_hash(
        {
            "run_id": plan.run_id,
            "account_key": plan.account_key,
            "snapshot_hash": plan.snapshot_hash,
            "payload": plan.payload,
        }
    )
    if plan.draft_hash != expected_hash or plan.plan_id != _stable_id(
        "execution-plan", expected_hash
    ):
        raise ValueError("Execution plan immutable hash verification failed")


def _validate_review_integrity(plan: ExecutionPlan, review: PlanReview) -> None:
    if review.plan_id != plan.plan_id or review.draft_hash != plan.draft_hash:
        raise ValueError("Broker review is not bound to the immutable plan")
    normalized = normalize_reviews(plan, review.review_payload)
    expected_hash = canonical_hash({"draft_hash": plan.draft_hash, "review_payload": normalized})
    if review.review_payload != normalized or review.review_hash != expected_hash:
        raise ValueError("Broker review immutable hash verification failed")


def _validate_confirmation_integrity(
    plan: ExecutionPlan,
    review: PlanReview,
    confirmation: ExecutionConfirmation,
) -> None:
    if (
        confirmation.plan_id != plan.plan_id
        or confirmation.review_hash != review.review_hash
        or confirmation.expires_at != plan.expires_at
    ):
        raise ValueError("Confirmation is not bound to the immutable review")
    payload = confirmation.payload
    if not isinstance(payload, dict):
        raise ValueError("Confirmation payload is malformed")
    message = confirmation_message(plan.plan_id, review.review_hash)
    signature = str(payload.get("signature") or "").strip()
    if payload.get("literal") != message or payload.get("message") != message or not signature:
        raise ValueError("Confirmation payload differs from the signed authority")
    actor_ref = _confirmation_authority(plan.plan_id, review.review_hash, signature)
    if actor_ref != confirmation.actor_ref:
        raise ValueError("Confirmation signer differs from the stored authority")


def _validate_task_plan_contract(plan: ExecutionPlan, task_name: str) -> None:
    """Enforce schedule authority from the durable lease, never from prompt prose."""

    orders = plan.payload.get("approved_orders", [])
    if not isinstance(orders, list):
        raise ValueError("Execution plan approved_orders must be a list")
    if not orders:
        return
    order = _sole_approved_order(plan)
    parameters = order.get("broker_parameters")
    if not isinstance(parameters, dict):
        raise ValueError("Approved order is missing exact broker parameters")
    market_hours = str(parameters.get("market_hours") or "").strip().lower()
    if task_name == "morning-live":
        if market_hours != "regular_hours":
            raise ValueError("Morning durable plans may authorize regular-hours orders only")
        return
    if task_name != "evening-live":
        raise ValueError(f"Unsupported production execution task: {task_name}")
    if not _order_uses_entry_budget(order):
        return
    limits = plan.payload.get("execution_limits")
    if not isinstance(limits, dict):
        raise ValueError("Evening entry plan is missing immutable execution limits")
    if (
        int(limits.get("max_entry_orders_per_day", 0)) > 1
        or float(limits.get("max_entry_daily_notional", 0.0)) > 100.0
        or float(limits.get("max_order_notional", 0.0)) > 100.0
    ):
        raise ValueError("Evening entry limits exceed the task contract")
    try:
        notional = float(order["notional"])
        quantity = float(parameters["quantity"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Evening entries require whole-share quantity and notional") from error
    if (
        market_hours != "all_day_hours"
        or str(parameters.get("type") or "").lower() != "limit"
        or str(parameters.get("time_in_force") or "").lower() != "gfd"
        or "dollar_amount" in parameters
        or notional > 100.0
        or quantity < 1
        or not quantity.is_integer()
    ):
        raise ValueError(
            "Evening entries require one <=$100 whole-share GFD all-day-hours limit order"
        )


def _resolve_execution_task(
    plan: ExecutionPlan,
    lease_task: str,
    parent_lookup: Any,
) -> str:
    task = lease_task
    seen: set[str] = set()
    while task.startswith("interactive-review:"):
        parent_plan_id = task.removeprefix("interactive-review:").strip()
        if not parent_plan_id or parent_plan_id in seen:
            raise RuntimeError("Interactive-review lease has an invalid plan lineage")
        seen.add(parent_plan_id)
        parent_account_key, task = parent_lookup(parent_plan_id)
        if parent_account_key != plan.account_key:
            raise RuntimeError("Interactive review must retain the original broker account")
    if task not in {"morning-live", "evening-live"}:
        raise RuntimeError("Execution plan lease is not rooted in a production live task")
    return task


def _normalized_equity_broker_parameters(parameters: dict[str, Any]) -> dict[str, str]:
    def numeric(name: str) -> str:
        try:
            value = parameters[name]
            if isinstance(value, dict):
                value = value.get("amount")
            return format(Decimal(str(value)).normalize(), "f")
        except (KeyError, InvalidOperation, ValueError) as error:
            raise ValueError(f"Broker order parameter {name} is invalid") from error

    normalized = {
        "symbol": str(parameters.get("symbol") or "").strip().upper(),
        "side": str(parameters.get("side") or "").strip().lower(),
        "type": str(parameters.get("type") or "").strip().lower(),
        "time_in_force": str(parameters.get("time_in_force") or "").strip().lower(),
        "market_hours": str(parameters.get("market_hours") or "").strip().lower(),
    }
    if not all(normalized.values()):
        raise ValueError("Broker order echo is missing required parameters")
    amount_fields = [
        name for name in ("quantity", "dollar_amount") if parameters.get(name) is not None
    ]
    if len(amount_fields) != 1:
        raise ValueError("Broker order requires exactly one of quantity or dollar_amount")
    normalized[amount_fields[0]] = numeric(amount_fields[0])
    for name in ("limit_price", "stop_price"):
        if parameters.get(name) is not None:
            normalized[name] = numeric(name)
    return normalized


def _native_equity_order_parameters(
    order: dict[str, Any], *, require_execution_fields: bool
) -> dict[str, str]:
    """Normalize Robinhood's native equity-order vocabulary for exact matching."""

    native_type = str(order.get("type") or "").strip().lower()
    trigger = str(order.get("trigger") or "immediate").strip().lower()
    if trigger not in {"immediate", "stop"}:
        raise ValueError("Native broker order trigger is unknown")
    user_type = (
        "stop_limit"
        if trigger == "stop" and native_type == "limit"
        else "stop_loss"
        if trigger == "stop" and native_type == "market"
        else native_type
    )
    request_shape: dict[str, Any] = {
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "type": user_type,
        "time_in_force": order.get("time_in_force"),
        "market_hours": order.get("market_hours"),
    }
    dollar_amount = order.get("dollar_amount")
    if dollar_amount is None:
        dollar_amount = order.get("dollar_based_amount")
    if dollar_amount is not None:
        request_shape["dollar_amount"] = dollar_amount
    elif order.get("quantity") is not None:
        request_shape["quantity"] = order.get("quantity")
    limit_price = order.get("limit_price")
    if limit_price is None and native_type == "limit":
        limit_price = order.get("price")
    if limit_price is not None:
        request_shape["limit_price"] = limit_price
    if order.get("stop_price") is not None:
        request_shape["stop_price"] = order.get("stop_price")
    if not require_execution_fields:
        # review_equity_order does not echo these two request arguments.
        request_shape["time_in_force"] = "review_not_echoed"
        request_shape["market_hours"] = "review_not_echoed"
    return _normalized_equity_broker_parameters(request_shape)


def native_order_from_response(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("order"), dict):
        return data["order"]
    if isinstance(response.get("order"), dict):
        return response["order"]
    return response


def _review_echo_matches_request(native_response: dict[str, Any], expected: dict[str, Any]) -> bool:
    expected_normalized = _normalized_equity_broker_parameters(expected)
    native_normalized = _native_equity_order_parameters(
        native_response,
        require_execution_fields=False,
    )
    expected_echo = {
        key: value
        for key, value in expected_normalized.items()
        if key not in {"time_in_force", "market_hours"}
    }
    native_echo = {
        key: value
        for key, value in native_normalized.items()
        if key not in {"time_in_force", "market_hours"}
    }
    return native_echo == expected_echo


def normalize_reviews(plan: ExecutionPlan, payload: dict[str, Any]) -> dict[str, Any]:
    raw_reviews = payload.get("reviews")
    if not isinstance(raw_reviews, list):
        raise ValueError("Review payload must contain a reviews list")
    expected = _expected_review_refs(plan)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_reviews:
        if not isinstance(item, dict):
            raise ValueError("Every broker review must be an object")
        ref_id = str(item.get("ref_id", ""))
        parameters = item.get("broker_parameters")
        if ref_id not in expected or ref_id in seen:
            raise ValueError("Broker reviews must cover each planned ref_id exactly once")
        if parameters != expected[ref_id]:
            raise ValueError(f"Broker review parameters differ from plan for {ref_id}")
        broker_response = item.get("broker_response")
        if not isinstance(broker_response, dict):
            raise ValueError(f"Native broker review response is missing for {ref_id}")
        reviewed_parameters = broker_response.get("broker_parameters")
        if reviewed_parameters != expected[ref_id]:
            raise ValueError(f"Reviewed request parameters differ from plan for {ref_id}")
        native_response = broker_response.get("native_response")
        if not isinstance(native_response, dict) or not _review_echo_matches_request(
            native_response, expected[ref_id]
        ):
            raise ValueError(f"Native broker review order differs from plan for {ref_id}")
        order_checks = broker_response.get("order_checks")
        if not isinstance(order_checks, dict):
            raise ValueError(f"Native broker order_checks are missing for {ref_id}")
        if order_checks:
            raise ValueError(f"Native broker order_checks raised alerts for {ref_id}")
        quote_data = broker_response.get("quote_data")
        if not isinstance(quote_data, dict) or not quote_data:
            raise ValueError(f"Native broker quote_data are missing for {ref_id}")
        quote_symbol = str(quote_data.get("symbol") or "").strip().upper()
        if quote_symbol and quote_symbol != str(expected[ref_id].get("symbol") or "").upper():
            raise ValueError(f"Native broker quote symbol differs for {ref_id}")
        disclosure = broker_response.get("market_data_disclosure")
        if not isinstance(disclosure, str) or not disclosure.strip():
            raise ValueError(f"Broker market-data disclosure is missing for {ref_id}")
        if (
            native_response.get("order_checks") != order_checks
            or native_response.get("quote_data") != quote_data
            or native_response.get("market_data_disclosure") != disclosure
        ):
            raise ValueError(f"Broker review fields differ from native response for {ref_id}")
        provenance = item.get("broker_provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"Broker review provenance is missing for {ref_id}")
        broker = provenance.get("broker")
        tool = provenance.get("tool")
        if not isinstance(broker, str) or broker.strip().lower() != "robinhood":
            raise ValueError(f"Broker review provenance must identify Robinhood for {ref_id}")
        if not isinstance(tool, str) or tool.strip() != "review_equity_order":
            raise ValueError(f"Broker review provenance tool is invalid for {ref_id}")
        seen.add(ref_id)
        normalized.append(
            {
                "ref_id": ref_id,
                "broker_parameters": deepcopy(expected[ref_id]),
                "broker_response": deepcopy(broker_response),
                "broker_response_hash": canonical_hash(broker_response),
                "broker_provenance": deepcopy(provenance),
            }
        )
    if seen != set(expected):
        raise ValueError("Broker review set is incomplete")
    normalized.sort(key=lambda item: str(item["ref_id"]))
    return {**deepcopy(payload), "reviews": normalized}


def _validate_attempt_transition_evidence(
    attempt: OrderAttempt,
    state: str,
    *,
    response: dict[str, Any] | None,
    broker_order_id: str | None,
    error: str | None,
) -> None:
    if state == "unknown" and not str(error or "").strip():
        raise ValueError("Unknown attempts require a durable ambiguity or timeout error")
    post_submission = {"submitting", "submitted", "unknown", "partially_filled"}
    broker_observed = {"submitted", "partially_filled", "filled", "cancelled", "rejected"}
    if attempt.state not in post_submission or state not in broker_observed:
        return
    if not isinstance(response, dict) or not response:
        raise ValueError(f"Attempt transition {attempt.state} -> {state} requires broker evidence")
    echoed = native_order_from_response(response)
    try:
        response_parameters = _native_equity_order_parameters(
            echoed,
            require_execution_fields=True,
        )
        requested_parameters = _normalized_equity_broker_parameters(attempt.broker_request)
    except ValueError as error:
        raise ValueError(
            f"Attempt transition {attempt.state} -> {state} lacks exact broker parameters"
        ) from error
    if response_parameters != requested_parameters:
        raise ValueError("Broker transition evidence differs from the signed order")
    effective_order_id = str(broker_order_id or attempt.broker_order_id or "").strip()
    echoed_order_id = str(echoed.get("id") or echoed.get("order_id") or "").strip()
    if echoed_order_id and effective_order_id and echoed_order_id != effective_order_id:
        raise ValueError("Broker transition evidence identifies a different order")
    if state != "rejected" and not effective_order_id:
        raise ValueError(
            f"Attempt transition {attempt.state} -> {state} requires a broker order ID"
        )


class InMemoryCloudRuntimeStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.migrations: dict[str, str] = {}
        self.runs: dict[str, RunLease] = {}
        self.run_windows: dict[tuple[str, datetime], str] = {}
        self.plans: dict[str, ExecutionPlan] = {}
        self.reviews: dict[str, PlanReview] = {}
        self.confirmations: dict[str, ExecutionConfirmation] = {}
        self.attempts: dict[str, OrderAttempt] = {}
        self.attempts_by_ref: dict[str, str] = {}
        # These mirror the two authority tables owned by the picker ledger. Tests
        # may share the ledger dictionaries with this store to exercise the same
        # transaction boundary as Postgres.
        self.execution_reservations: dict[str, dict[str, Any]] = {}
        self.control_states: dict[str, dict[str, Any]] = {}
        self.picker_order_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.transitions: dict[str, dict[str, Any]] = {}
        self.reconciliations: dict[str, Reconciliation] = {}
        self.audit_events: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.knowledge_nodes: dict[str, dict[str, Any]] = {}
        self.knowledge_edges: dict[str, dict[str, Any]] = {}
        self.knowledge_observations: dict[str, dict[str, Any]] = {}

    def record_migrations(self, paths: list[str | Path]) -> None:
        for path in paths:
            version = Path(path).name
            checksum = _migration_checksum(path)
            existing = self.migrations.get(version)
            if existing is not None and existing != checksum:
                raise RuntimeError(f"Migration checksum drift: {version}")
            self.migrations[version] = checksum

    def schema_status(self, paths: list[str | Path]) -> SchemaStatus:
        expected = {Path(path).name: _migration_checksum(path) for path in paths}
        return _build_schema_status(expected, dict(self.migrations))

    def assert_schema_current(self, paths: list[str | Path]) -> SchemaStatus:
        status = self.schema_status(paths)
        if not status.current:
            raise RuntimeError(
                "Cloud schema is not current; "
                f"missing={status.missing}, drifted={status.drifted}, "
                f"unexpected={status.unexpected}, "
                f"security_violations={status.security_violations}"
            )
        return status

    def acquire_run_lease(
        self,
        *,
        task_name: str,
        scheduled_for: datetime,
        git_sha: str,
        lease_seconds: int = 7200,
        now: datetime | None = None,
    ) -> RunLease | None:
        if not task_name.strip() or not git_sha.strip() or not 60 <= lease_seconds <= 86400:
            raise ValueError("Run leases require task, git SHA, and 60-86400 seconds")
        scheduled_for = _utc(scheduled_for)
        now = _utc(now or datetime.now(UTC))
        key = (task_name, scheduled_for)
        with self._lock:
            existing_id = self.run_windows.get(key)
            existing = self.runs.get(existing_id) if existing_id else None
            if existing and (existing.status == "completed" or existing.lease_expires_at > now):
                return None
            run_id = existing.run_id if existing else _stable_id("automation-run", *key)
            lease = RunLease(
                run_id=run_id,
                task_name=task_name,
                scheduled_for=scheduled_for,
                git_sha=git_sha,
                lease_token=str(uuid.uuid4()),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )
            self.runs[run_id] = lease
            self.run_windows[key] = run_id
            return lease

    def heartbeat_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 7200,
        now: datetime | None = None,
    ) -> RunLease:
        now = _utc(now or datetime.now(UTC))
        with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.lease_token != lease_token or run.status != "running":
                raise RuntimeError("Run lease is unavailable or owned by another worker")
            if run.lease_expires_at <= now:
                raise RuntimeError("Run lease has expired")
            updated = replace(
                run,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self.runs[run_id] = updated
            return updated

    def release_run_lease(
        self,
        run_id: str,
        lease_token: str,
        *,
        status: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> RunLease:
        if status not in {"completed", "failed"}:
            raise ValueError("A run must finish completed or failed")
        now = _utc(now or datetime.now(UTC))
        with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.lease_token != lease_token or run.status != "running":
                raise RuntimeError("Run lease is unavailable or owned by another worker")
            updated = replace(run, status=status, heartbeat_at=now, lease_expires_at=now)
            self.runs[run_id] = updated
            self.append_audit_event(
                "automation_run_finished",
                {"status": status, "reason": reason},
                run_id=run_id,
                occurred_at=now,
            )
            return updated

    def assert_active_lease(
        self, run_id: str, lease_token: str, now: datetime | None = None
    ) -> RunLease:
        now = _utc(now or datetime.now(UTC))
        run = self.runs.get(run_id)
        if (
            run is None
            or run.lease_token != lease_token
            or run.status != "running"
            or run.lease_expires_at <= now
        ):
            raise RuntimeError("An active durable run lease is required")
        return deepcopy(run)

    def persist_plan(self, plan: ExecutionPlan, lease_token: str) -> ExecutionPlan:
        with self._lock:
            _expected_review_refs(plan)
            _validate_plan_integrity(plan)
            lease = self.assert_active_lease(plan.run_id, lease_token)

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                parent = self.plans.get(parent_plan_id)
                if parent is None:
                    raise RuntimeError("Interactive review references an unknown durable plan")
                parent_run = self.runs.get(parent.run_id)
                if parent_run is None:
                    raise RuntimeError("Interactive review parent run is unavailable")
                if parent.trade_date != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return parent.account_key, parent_run.task_name

            production_task = _resolve_execution_task(plan, lease.task_name, parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            existing = self.plans.get(plan.plan_id)
            if existing is not None and existing != plan:
                raise ValueError(f"Execution plan {plan.plan_id} is immutable")
            self.plans[plan.plan_id] = existing or deepcopy(plan)
            self.append_audit_event(
                "execution_plan_persisted",
                {"draft_hash": plan.draft_hash, "snapshot_hash": plan.snapshot_hash},
                run_id=plan.run_id,
                plan_id=plan.plan_id,
                occurred_at=plan.planned_at,
            )
            return self.plans[plan.plan_id]

    def get_plan(self, plan_id: str, expected_hash: str | None = None) -> ExecutionPlan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise ValueError(f"Unknown execution plan {plan_id}")
        if expected_hash is not None and expected_hash not in {
            plan.draft_hash,
            self.reviews.get(plan_id).review_hash if plan_id in self.reviews else "",
        }:
            raise ValueError("Execution plan hash does not match")
        return deepcopy(plan)

    def get_plan_review(
        self,
        plan_id: str,
        expected_review_hash: str | None = None,
    ) -> tuple[ExecutionPlan, PlanReview]:
        with self._lock:
            plan = self.get_plan(plan_id)
            review = self.reviews.get(plan_id)
            if review is None:
                raise ValueError(f"Execution plan {plan_id} has no durable broker review")
            if expected_review_hash is not None and review.review_hash != expected_review_hash:
                raise ValueError("Execution plan review hash does not match")
            _validate_plan_integrity(plan)
            _validate_review_integrity(plan, review)
            return plan, deepcopy(review)

    def record_plan_review(
        self,
        plan_id: str,
        draft_hash: str,
        review_payload: dict[str, Any],
        *,
        reviewed_at: datetime | None = None,
    ) -> PlanReview:
        reviewed_at = _utc(reviewed_at or datetime.now(UTC))
        with self._lock:
            plan = self.get_plan(plan_id, draft_hash)
            _require_single_order_authority(plan)
            if reviewed_at >= plan.expires_at:
                raise ValueError("Cannot review an expired execution plan")
            normalized = normalize_reviews(plan, review_payload)
            review_hash = canonical_hash({"draft_hash": draft_hash, "review_payload": normalized})
            existing = self.reviews.get(plan_id)
            if existing is not None:
                if (
                    existing.draft_hash != draft_hash
                    or existing.review_hash != review_hash
                    or existing.review_payload != normalized
                ):
                    raise ValueError("Execution plan review is immutable")
                return deepcopy(existing)
            review = PlanReview(plan_id, draft_hash, review_hash, normalized, reviewed_at)
            self.reviews[plan_id] = review
            self.plans[plan_id] = replace(plan, status="awaiting_confirmation")
            self.append_audit_event(
                "execution_plan_reviewed",
                {"review_hash": review_hash},
                run_id=plan.run_id,
                plan_id=plan_id,
                occurred_at=reviewed_at,
            )
            return deepcopy(self.reviews[plan_id])

    def record_confirmation(
        self,
        plan_id: str,
        review_hash: str,
        signature: str,
        *,
        payload: dict[str, Any] | None = None,
        confirmed_at: datetime | None = None,
    ) -> ExecutionConfirmation:
        message = confirmation_message(plan_id, review_hash)
        actor_ref = _confirmation_authority(plan_id, review_hash, signature)
        authority_fingerprint = actor_ref.removeprefix("ed25519:")
        confirmation_payload = {
            **deepcopy(payload or {}),
            "literal": message,
            "message": message,
            "signature": signature.strip(),
            "authority_fingerprint": authority_fingerprint,
        }
        confirmed_at = _utc(confirmed_at or datetime.now(UTC))
        with self._lock:
            plan = self.get_plan(plan_id, review_hash)
            _require_single_order_authority(plan)
            review = self.reviews.get(plan_id)
            if review is None or review.review_hash != review_hash:
                raise ValueError("Confirmation does not match the exact broker review")
            if confirmed_at >= plan.expires_at:
                raise ValueError("Execution plan expired before confirmation")
            confirmation = ExecutionConfirmation(
                confirmation_id=_stable_id("execution-confirmation", plan_id, review_hash),
                plan_id=plan_id,
                review_hash=review_hash,
                actor_ref=actor_ref,
                confirmed_at=confirmed_at,
                expires_at=plan.expires_at,
                payload=confirmation_payload,
            )
            existing = self.confirmations.get(plan_id)
            if existing is not None:
                if (
                    existing.review_hash != review_hash
                    or existing.actor_ref != actor_ref
                    or existing.payload != confirmation.payload
                ):
                    raise ValueError("Execution plan confirmation is immutable")
                return deepcopy(existing)
            self.confirmations[plan_id] = confirmation
            self.plans[plan_id] = replace(plan, status="confirmed")
            self.append_audit_event(
                "execution_plan_confirmed",
                {"review_hash": review_hash, "actor_ref": actor_ref},
                run_id=plan.run_id,
                plan_id=plan_id,
                occurred_at=confirmed_at,
            )
            return deepcopy(self.confirmations[plan_id])

    def validate_confirmation(
        self,
        plan_id: str,
        review_hash: str,
        confirmation_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> ExecutionConfirmation:
        now = _utc(now or datetime.now(UTC))
        confirmation = self.confirmations.get(plan_id)
        if (
            confirmation is None
            or confirmation.review_hash != review_hash
            or (confirmation_id is not None and confirmation.confirmation_id != confirmation_id)
            or confirmation.expires_at <= now
        ):
            raise RuntimeError("A current exact execution confirmation is required")
        return deepcopy(confirmation)

    def create_order_attempt(
        self,
        *,
        plan_id: str,
        confirmation_id: str,
        review_hash: str,
        ref_id: str,
        broker_request: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[OrderAttempt, bool]:
        now = _utc(now or datetime.now(UTC))
        with self._lock:
            confirmation = self.validate_confirmation(
                plan_id, review_hash, confirmation_id, now=now
            )
            plan = self.get_plan(plan_id, review_hash)
            expected = _require_single_order_authority(plan)
            if ref_id not in expected or expected[ref_id] != broker_request:
                raise ValueError("Attempt request differs from reviewed plan")
            request_hash = canonical_hash(broker_request)
            existing_id = self.attempts_by_ref.get(ref_id)
            if existing_id is not None:
                existing = self.attempts[existing_id]
                if (
                    existing.plan_id != plan_id
                    or existing.confirmation_id != confirmation.confirmation_id
                    or existing.request_hash != request_hash
                ):
                    raise ValueError(f"Order attempt ref_id collision: {ref_id}")
                return deepcopy(existing), False
            blocking = [
                attempt
                for attempt in self.attempts.values()
                if attempt.account_key == plan.account_key
                and attempt.state in NONTERMINAL_ATTEMPT_STATES
            ]
            if blocking:
                raise RuntimeError("An unresolved account order attempt blocks new reservation")
            attempt = OrderAttempt(
                attempt_id=order_attempt_id(plan_id, ref_id),
                plan_id=plan_id,
                confirmation_id=confirmation.confirmation_id,
                account_key=plan.account_key,
                ref_id=ref_id,
                request_hash=request_hash,
                broker_request=deepcopy(broker_request),
                state="prepared",
                broker_order_id=None,
                latest_response=None,
                error=None,
                created_at=now,
                updated_at=now,
            )
            self.attempts[attempt.attempt_id] = attempt
            self.attempts_by_ref[ref_id] = attempt.attempt_id
            self._record_transition(attempt, None, "prepared", {}, now)
            return deepcopy(attempt), True

    def _record_transition(
        self,
        attempt: OrderAttempt,
        from_state: str | None,
        to_state: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        payload_hash = canonical_hash(payload)
        transition_id = _stable_id(
            "attempt-transition",
            attempt.attempt_id,
            from_state,
            to_state,
            payload_hash,
        )
        record = {
            "transition_id": transition_id,
            "attempt_id": attempt.attempt_id,
            "from_state": from_state,
            "to_state": to_state,
            "occurred_at": occurred_at,
            "payload": deepcopy(payload),
            "payload_hash": payload_hash,
        }
        existing = self.transitions.get(transition_id)
        if existing is not None and existing != record:
            raise ValueError("Attempt transition is immutable")
        self.transitions[transition_id] = existing or record

    def transition_order_attempt(
        self,
        attempt_id: str,
        state: str,
        *,
        response: dict[str, Any] | None = None,
        broker_order_id: str | None = None,
        error: str | None = None,
        occurred_at: datetime | None = None,
    ) -> OrderAttempt:
        occurred_at = _utc(occurred_at or datetime.now(UTC))
        with self._lock:
            attempt = self.attempts.get(attempt_id)
            if attempt is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            if state in SUBMISSION_AUTHORITY_STATES:
                raise ValueError(f"Attempt state {state} requires the atomic submission-claim path")
            if attempt.state == "filled" and state == "reconciled":
                raise ValueError("Filled attempts require durable picker-event finalization")
            if state == attempt.state:
                if (
                    (response is None or response == attempt.latest_response)
                    and (broker_order_id is None or broker_order_id == attempt.broker_order_id)
                    and (error is None or error == attempt.error)
                ):
                    return deepcopy(attempt)
                raise ValueError("Same-state attempt updates must be exact retries")
            if state not in ATTEMPT_TRANSITIONS.get(attempt.state, set()):
                raise ValueError(f"Invalid attempt transition {attempt.state} -> {state}")
            _validate_attempt_transition_evidence(
                attempt,
                state,
                response=response,
                broker_order_id=broker_order_id,
                error=error,
            )
            payload = {
                "response": response,
                "broker_order_id": broker_order_id,
                "error": error,
            }
            updated = replace(
                attempt,
                state=state,
                broker_order_id=broker_order_id or attempt.broker_order_id,
                latest_response=deepcopy(response)
                if response is not None
                else attempt.latest_response,
                error=error,
                updated_at=occurred_at,
            )
            self.attempts[attempt_id] = updated
            self._record_transition(attempt, attempt.state, state, payload, occurred_at)
            self.append_audit_event(
                "execution_attempt_transition",
                {"from": attempt.state, "to": state, **payload},
                plan_id=attempt.plan_id,
                attempt_id=attempt_id,
                ref_id=attempt.ref_id,
                occurred_at=occurred_at,
            )
            return deepcopy(updated)

    def finalize_filled_attempt_after_picker_sync(
        self,
        attempt_id: str,
        *,
        event_type: str,
        session_date: date,
        occurred_at: datetime | None = None,
    ) -> OrderAttempt:
        occurred_at = _utc(occurred_at or datetime.now(UTC))
        with self._lock:
            attempt = self.attempts.get(attempt_id)
            if attempt is None or attempt.state != "filled":
                raise RuntimeError("Picker finalization requires a filled durable attempt")
            event = self.picker_order_events.get((attempt.ref_id, event_type))
            if (
                event is None
                or event.get("account_key") != attempt.account_key
                or event.get("session_date") != session_date
            ):
                raise RuntimeError("Picker fill event is not durably synchronized")
            payload = {"event_type": event_type, "session_date": session_date.isoformat()}
            updated = replace(attempt, state="reconciled", updated_at=occurred_at)
            self.attempts[attempt_id] = updated
            self._record_transition(attempt, "filled", "reconciled", payload, occurred_at)
            self.append_audit_event(
                "execution_attempt_picker_finalized",
                payload,
                plan_id=attempt.plan_id,
                attempt_id=attempt_id,
                ref_id=attempt.ref_id,
                occurred_at=occurred_at,
            )
            return deepcopy(updated)

    def refresh_execution_reservation(
        self,
        attempt_id: str,
        *,
        plan_id: str,
        review_hash: str,
        confirmation_id: str,
        ref_id: str,
        validated_at: datetime,
        validation_snapshot_hash: str,
        authority_fingerprint_hash: str,
        now: datetime | None = None,
    ) -> bool:
        """Replace only an expired freshness proof for an exact prepared attempt."""

        occurred_at = _utc(now or datetime.now(UTC))
        validated_at = _utc(validated_at)
        hex_characters = frozenset("0123456789abcdef")
        if (
            len(validation_snapshot_hash) != 64
            or any(character not in hex_characters for character in validation_snapshot_hash)
            or len(authority_fingerprint_hash) != 64
            or any(character not in hex_characters for character in authority_fingerprint_hash)
        ):
            raise ValueError("Reservation refresh requires SHA-256 snapshot and authority hashes")
        if validated_at > occurred_at or (occurred_at - validated_at).total_seconds() > 15:
            raise RuntimeError("Reservation refresh requires a newly revalidated broker snapshot")

        with self._lock:
            attempt = self.attempts.get(attempt_id)
            if attempt is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            if (
                attempt.plan_id != plan_id
                or attempt.confirmation_id != confirmation_id
                or attempt.ref_id != ref_id
            ):
                raise ValueError("Reservation refresh identifiers do not match the attempt")
            if attempt.state != "prepared":
                raise RuntimeError("Only a prepared order attempt can refresh its reservation")

            plan = self.get_plan(plan_id, review_hash)
            review = self.reviews.get(plan_id)
            confirmation = self.confirmations.get(plan_id)
            if (
                plan.status != "confirmed"
                or plan.planned_at > occurred_at
                or plan.expires_at <= occurred_at
                or review is None
                or review.review_hash != review_hash
                or confirmation is None
                or confirmation.confirmation_id != confirmation_id
                or confirmation.review_hash != review_hash
                or confirmation.confirmed_at > occurred_at
                or confirmation.expires_at <= occurred_at
            ):
                raise RuntimeError(
                    "Reservation refresh requires an active exact plan and confirmation"
                )
            _validate_plan_integrity(plan)
            _validate_review_integrity(plan, review)
            _validate_confirmation_integrity(plan, review, confirmation)
            plan_run = self.runs.get(plan.run_id)
            if plan_run is None:
                raise RuntimeError("Reservation refresh plan run is unavailable")

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                parent = self.plans.get(parent_plan_id)
                parent_run = self.runs.get(parent.run_id) if parent is not None else None
                if parent is None or parent_run is None:
                    raise RuntimeError("Interactive review plan lineage is unavailable")
                if parent.trade_date != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return parent.account_key, parent_run.task_name

            production_task = _resolve_execution_task(plan, plan_run.task_name, parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            approved_order = _sole_approved_order(plan)
            expected = _require_single_order_authority(plan)
            if expected.get(ref_id) != attempt.broker_request:
                raise ValueError("Reservation refresh request differs from the reviewed order")

            reservation = self.execution_reservations.get(ref_id)
            exact_reservation = {
                "ref_id": ref_id,
                "account_key": attempt.account_key,
                "trade_date": plan.trade_date,
                "plan_id": plan_id,
                "confirmation_id": confirmation_id,
                "attempt_id": attempt_id,
                "is_entry": _order_uses_entry_budget(approved_order),
                "is_option_open": False,
            }
            if reservation is None or any(
                reservation.get(key) != value for key, value in exact_reservation.items()
            ):
                raise RuntimeError(
                    "Reservation refresh requires the exact linked budget reservation"
                )
            try:
                reservation_notional = float(reservation["notional"])
                planned_notional = float(approved_order["notional"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "Reservation refresh requires the exact linked budget reservation"
                ) from error
            if abs(reservation_notional - planned_notional) > 1e-6:
                raise RuntimeError(
                    "Reservation refresh requires the exact linked budget reservation"
                )

            expected_authority_hash = canonical_hash(plan.payload.get("broker_authority"))
            if (
                authority_fingerprint_hash != expected_authority_hash
                or reservation.get("authority_fingerprint_hash") != expected_authority_hash
            ):
                raise RuntimeError("Reservation refresh broker authority differs from the review")
            if bool(self.control_states.get(attempt.account_key, {}).get("halted")):
                raise RuntimeError("A durable trading halt blocks reservation refresh")

            current_validated_at = reservation.get("validated_at")
            current_snapshot_hash = reservation.get("validation_snapshot_hash")
            if (
                current_validated_at == validated_at
                and current_snapshot_hash == validation_snapshot_hash
            ):
                return False
            if current_validated_at is not None and (
                not isinstance(current_validated_at, datetime)
                or current_validated_at.tzinfo is None
                or current_validated_at > occurred_at
                or (occurred_at - current_validated_at.astimezone(UTC)).total_seconds() <= 15
                or validated_at <= current_validated_at.astimezone(UTC)
            ):
                raise RuntimeError(
                    "Reservation freshness proof is active or was concurrently refreshed"
                )

            reservation["validated_at"] = validated_at
            reservation["validation_snapshot_hash"] = validation_snapshot_hash
            self.append_audit_event(
                "execution_reservation_refreshed",
                {
                    "old_validated_at": current_validated_at.isoformat()
                    if isinstance(current_validated_at, datetime)
                    else None,
                    "old_validation_snapshot_hash": current_snapshot_hash,
                    "validated_at": validated_at.isoformat(),
                    "validation_snapshot_hash": validation_snapshot_hash,
                    "authority_fingerprint_hash": authority_fingerprint_hash,
                },
                run_id=plan.run_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                ref_id=ref_id,
                occurred_at=occurred_at,
            )
            return True

    def claim_order_attempt_for_submission(
        self,
        attempt_id: str,
        *,
        plan_id: str,
        review_hash: str,
        confirmation_id: str,
        ref_id: str,
        validation_snapshot_hash: str,
        now: datetime | None = None,
    ) -> OrderAttempt:
        """Consume one exact durable reservation and claim its sole broker call."""

        occurred_at = _utc(now or datetime.now(UTC))
        with self._lock:
            attempt = self.attempts.get(attempt_id)
            if attempt is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            if (
                attempt.plan_id != plan_id
                or attempt.confirmation_id != confirmation_id
                or attempt.ref_id != ref_id
            ):
                raise ValueError("Submission claim identifiers do not match the attempt")
            if attempt.state not in {"prepared", "reserved"}:
                raise RuntimeError("Order attempt was already claimed or is no longer claimable")
            if any(
                other.attempt_id != attempt_id
                and other.account_key == attempt.account_key
                and other.state in NONTERMINAL_ATTEMPT_STATES
                for other in self.attempts.values()
            ):
                raise RuntimeError(
                    "Another unresolved account order attempt blocks submission claim"
                )

            plan = self.get_plan(plan_id, review_hash)
            expected = _require_single_order_authority(plan)
            confirmation = self.confirmations.get(plan_id)
            review = self.reviews.get(plan_id)
            if (
                plan.status != "confirmed"
                or plan.planned_at > occurred_at
                or plan.expires_at <= occurred_at
                or review is None
                or review.review_hash != review_hash
                or confirmation is None
                or confirmation.confirmation_id != confirmation_id
                or confirmation.review_hash != review_hash
                or confirmation.confirmed_at > occurred_at
                or confirmation.expires_at <= occurred_at
            ):
                raise RuntimeError(
                    "Submission claim requires an active exact plan and confirmation"
                )
            _validate_plan_integrity(plan)
            _validate_review_integrity(plan, review)
            _validate_confirmation_integrity(plan, review, confirmation)
            plan_run = self.runs.get(plan.run_id)
            if plan_run is None:
                raise RuntimeError("Submission claim plan run is unavailable")

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                parent = self.plans.get(parent_plan_id)
                parent_run = self.runs.get(parent.run_id) if parent is not None else None
                if parent is None or parent_run is None:
                    raise RuntimeError("Interactive review plan lineage is unavailable")
                if parent.trade_date != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return parent.account_key, parent_run.task_name

            production_task = _resolve_execution_task(plan, plan_run.task_name, parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            if expected.get(ref_id) != attempt.broker_request:
                raise ValueError("Submission claim request differs from the reviewed order")

            reservation = self.execution_reservations.get(ref_id)
            approved_order = _sole_approved_order(plan)
            exact_reservation = {
                "account_key": attempt.account_key,
                "trade_date": plan.trade_date,
                "plan_id": plan_id,
                "confirmation_id": confirmation_id,
                "attempt_id": attempt_id,
                "is_entry": _order_uses_entry_budget(approved_order),
                "is_option_open": False,
            }
            if reservation is None or any(
                reservation.get(key) != value for key, value in exact_reservation.items()
            ):
                raise RuntimeError("Submission claim requires the exact linked budget reservation")
            try:
                reservation_notional = float(reservation["notional"])
                planned_notional = float(approved_order["notional"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "Submission claim requires the exact linked budget reservation"
                ) from error
            if abs(reservation_notional - planned_notional) > 1e-6:
                raise RuntimeError("Submission claim requires the exact linked budget reservation")
            validated_at = reservation.get("validated_at")
            if (
                not isinstance(validated_at, datetime)
                or validated_at.tzinfo is None
                or validated_at > occurred_at
                or (occurred_at - validated_at.astimezone(UTC)).total_seconds() > 15
                or reservation.get("validation_snapshot_hash") != validation_snapshot_hash
                or len(validation_snapshot_hash) != 64
            ):
                raise RuntimeError("Submission claim requires a fresh exact broker snapshot")
            authority_hash = str(reservation.get("authority_fingerprint_hash") or "")
            expected_authority_hash = canonical_hash(plan.payload.get("broker_authority"))
            if authority_hash != expected_authority_hash or len(authority_hash) != 64:
                raise RuntimeError("Submission claim broker authority differs from the review")

            control = self.control_states.get(attempt.account_key, {})
            halted = bool(control.get("halted"))
            halt_scope = str(control.get("halt_scope") or "entries")
            if halted and (halt_scope == "all" or bool(reservation.get("is_entry"))):
                raise RuntimeError("A durable trading halt blocks this submission claim")

            payload = {
                "reservation_ref_id": ref_id,
                "review_hash": review_hash,
                "authority": "exact_plan_confirmation_reservation",
            }
            updated = replace(
                attempt,
                state="submitting",
                error=None,
                updated_at=occurred_at,
            )
            self.attempts[attempt_id] = updated
            self.plans[plan_id] = replace(plan, status="submitting")
            self._record_transition(
                attempt,
                attempt.state,
                "submitting",
                payload,
                occurred_at,
            )
            self.append_audit_event(
                "execution_submission_claimed",
                payload,
                run_id=plan.run_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                ref_id=ref_id,
                occurred_at=occurred_at,
            )
            return deepcopy(updated)

    def nonterminal_attempts(self, account_key: str) -> list[OrderAttempt]:
        return sorted(
            [
                deepcopy(attempt)
                for attempt in self.attempts.values()
                if attempt.account_key == account_key
                and attempt.state in NONTERMINAL_ATTEMPT_STATES
            ],
            key=lambda attempt: (attempt.updated_at, attempt.attempt_id),
        )

    def record_reconciliation(
        self,
        plan_id: str,
        payload: dict[str, Any],
        *,
        reconciled_at: datetime | None = None,
    ) -> Reconciliation:
        reconciled_at = _utc(reconciled_at or datetime.now(UTC))
        plan = self.get_plan(plan_id)
        result_hash = canonical_hash(payload)
        reconciliation = Reconciliation(
            reconciliation_id=_stable_id("execution-reconciliation", plan_id, result_hash),
            plan_id=plan_id,
            result_hash=result_hash,
            clean=bool(payload.get("clean")),
            payload=deepcopy(payload),
            reconciled_at=reconciled_at,
        )
        existing = self.reconciliations.get(reconciliation.reconciliation_id)
        if existing is not None:
            if (
                existing.plan_id != plan_id
                or existing.result_hash != result_hash
                or existing.clean != reconciliation.clean
                or existing.payload != payload
            ):
                raise ValueError("Execution reconciliation is immutable")
            return deepcopy(existing)
        self.reconciliations[reconciliation.reconciliation_id] = reconciliation
        self.plans[plan_id] = replace(
            plan, status="reconciled" if reconciliation.clean else "failed"
        )
        self.append_audit_event(
            "execution_reconciled",
            {"clean": reconciliation.clean, "result_hash": result_hash},
            run_id=plan.run_id,
            plan_id=plan_id,
            occurred_at=reconciled_at,
        )
        return deepcopy(self.reconciliations[reconciliation.reconciliation_id])

    def latest_reconciliation(self, plan_id: str) -> Reconciliation | None:
        rows = [item for item in self.reconciliations.values() if item.plan_id == plan_id]
        return deepcopy(max(rows, key=lambda item: item.reconciled_at)) if rows else None

    def append_audit_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        plan_id: str | None = None,
        attempt_id: str | None = None,
        ref_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        occurred_at = _utc(occurred_at or datetime.now(UTC))
        payload_hash = canonical_hash(payload)
        event_id = _stable_id(
            "execution-audit",
            event_type,
            run_id,
            plan_id,
            attempt_id,
            ref_id,
            occurred_at.isoformat(),
            payload_hash,
        )
        record = {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "run_id": run_id,
            "plan_id": plan_id,
            "attempt_id": attempt_id,
            "ref_id": ref_id,
            "payload": deepcopy(payload),
            "payload_hash": payload_hash,
        }
        existing = self.audit_events.get(event_id)
        if existing is not None and existing != record:
            raise ValueError("Execution audit event is immutable")
        self.audit_events[event_id] = existing or record
        return event_id

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        source_uri: str | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = _utc(observed_at or datetime.now(UTC))
        if run_id not in self.runs or not artifact_type.strip():
            raise ValueError("Runtime artifact requires a known run and type")
        source_uri = sanitize_source_uri(source_uri)
        content_hash = canonical_hash(payload)
        artifact_id = _stable_id("cloud-artifact", run_id, artifact_type, content_hash)
        record = {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "content_hash": content_hash,
            "payload": deepcopy(payload),
            "source_uri": source_uri,
            "observed_at": observed_at,
        }
        existing = self.artifacts.get(artifact_id)
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["artifact_type"] != artifact_type
                or existing["content_hash"] != content_hash
                or existing["payload"] != payload
                or existing["source_uri"] != source_uri
            ):
                raise ValueError("Runtime artifact is immutable")
            return deepcopy(existing)
        self.artifacts[artifact_id] = record
        return deepcopy(self.artifacts[artifact_id])

    def upsert_knowledge_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"node_id", "node_type", "title"}
        if not required.issubset(payload):
            raise ValueError("Knowledge node is incomplete")
        record = deepcopy(payload)
        record["updated_at"] = _utc(
            datetime.fromisoformat(
                str(payload.get("updated_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        self.knowledge_nodes[str(payload["node_id"])] = record
        return deepcopy(record)

    def upsert_knowledge_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"edge_id", "source_id", "target_id", "relation", "sign", "horizon", "causality"}
        if not required.issubset(payload):
            raise ValueError("Knowledge edge is incomplete")
        if payload["causality"] not in {"hypothesis", "non_causal"}:
            raise ValueError("Knowledge causality must be hypothesis or non_causal")
        if (
            payload["source_id"] not in self.knowledge_nodes
            or payload["target_id"] not in self.knowledge_nodes
        ):
            raise ValueError("Knowledge edge references an unknown node")
        record = deepcopy(payload)
        record["updated_at"] = _utc(
            datetime.fromisoformat(
                str(payload.get("updated_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        self.knowledge_edges[str(payload["edge_id"])] = record
        return deepcopy(record)

    def append_knowledge_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"observation_id", "edge_id", "decision_date", "horizon", "polarity"}
        if not required.issubset(payload) or payload["edge_id"] not in self.knowledge_edges:
            raise ValueError("Knowledge observation is incomplete or references an unknown edge")
        if payload["polarity"] not in {"supports", "contradicts", "neutral"}:
            raise ValueError("Knowledge observation polarity is invalid")
        record = deepcopy(payload)
        record["observation_hash"] = canonical_hash(payload)
        record["observed_at"] = _utc(
            datetime.fromisoformat(
                str(payload.get("observed_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        observation_id = str(payload["observation_id"])
        existing = self.knowledge_observations.get(observation_id)
        if existing is not None:
            if existing["observation_hash"] != record["observation_hash"]:
                raise ValueError("Knowledge observation is immutable")
            return deepcopy(existing)
        self.knowledge_observations[observation_id] = record
        return deepcopy(self.knowledge_observations[observation_id])


class PostgresCloudRuntimeStore:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("A Postgres database URL is required")
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> PostgresCloudRuntimeStore:
        return cls(os.environ.get(DATABASE_URL_ENV, ""))

    def _connect(self):
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("Install psycopg[binary] for cloud runtime persistence") from error
        return psycopg.connect(self.database_url)

    @staticmethod
    def _read_applied_migrations(cursor: Any) -> dict[str, str]:
        cursor.execute("SELECT to_regclass('public.schema_migrations')")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return {}
        cursor.execute("SELECT version, checksum FROM schema_migrations")
        return {str(version): str(checksum) for version, checksum in cursor.fetchall()}

    @staticmethod
    def _security_posture_violations(cursor: Any) -> tuple[str, ...]:
        violations: list[str] = []

        cursor.execute("SELECT current_user")
        owner_row = cursor.fetchone()
        if owner_row is None:
            return ("database_owner_role_unavailable",)
        connection_role = str(owner_row[0])
        if connection_role != EXPECTED_CLOUD_DATABASE_ROLE:
            violations.append(f"unexpected_database_role:{connection_role}")

        cursor.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(SUPABASE_API_ROLES),),
        )
        present_api_roles = {str(row[0]) for row in cursor.fetchall()}
        for role_name in sorted(set(SUPABASE_API_ROLES) - present_api_roles):
            violations.append(f"missing_api_role:{role_name}")

        cursor.execute(
            """
            SELECT
                CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'relation' END,
                format('%I.%I', n.nspname, c.relname),
                c.relkind::text,
                owner.rolname,
                CASE
                    WHEN c.relkind = ANY(ARRAY['r', 'p']::"char"[])
                    THEN c.relrowsecurity
                END
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_roles AS owner ON owner.oid = c.relowner
            WHERE n.nspname = 'public'
              AND c.relkind = ANY(ARRAY['r', 'p', 'v', 'm', 'f', 'S']::"char"[])
            UNION ALL
            SELECT
                'function',
                format(
                    '%I.%I(%s)',
                    n.nspname,
                    p.proname,
                    pg_get_function_identity_arguments(p.oid)
                ),
                p.prokind::text,
                owner.rolname,
                NULL::boolean
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            JOIN pg_roles AS owner ON owner.oid = p.proowner
            WHERE n.nspname = 'public'
            ORDER BY 1, 2
            """
        )
        inventory_rows = cursor.fetchall()
        inventory = {
            (str(object_class), str(identity), str(object_kind))
            for object_class, identity, object_kind, _, _ in inventory_rows
        }
        for object_class, identity, object_kind in sorted(
            REQUIRED_PRIVATE_PUBLIC_OBJECTS - inventory
        ):
            violations.append(f"missing_private_object:{object_class}:{identity}:{object_kind}")
        for object_class, identity, object_kind, owner, rls_enabled in inventory_rows:
            if str(owner) != EXPECTED_CLOUD_DATABASE_ROLE or str(owner) != connection_role:
                violations.append(f"object_owner_mismatch:{object_class}:{identity}:{owner}")
            if str(object_kind) in {"r", "p"} and rls_enabled is not True:
                violations.append(f"rls_disabled:{identity}")

        cursor.execute(
            """
            WITH api_roles AS (
                SELECT oid, rolname
                FROM pg_roles
                WHERE rolname = ANY(%s)
            ),
            relation_privileges AS (
                SELECT
                    CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'relation' END
                        AS object_class,
                    format('%%I.%%I', n.nspname, c.relname) AS object_identity,
                    role.rolname AS role_name,
                    requested.privilege,
                    CASE
                        WHEN c.relkind = 'S' THEN has_sequence_privilege(
                            role.oid,
                            c.oid,
                            requested.privilege
                        )
                        WHEN requested.privilege = ANY(
                            ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']::text[]
                        ) THEN
                            has_table_privilege(role.oid, c.oid, requested.privilege)
                            OR has_any_column_privilege(
                                role.oid,
                                c.oid,
                                requested.privilege
                            )
                        ELSE has_table_privilege(
                            role.oid,
                            c.oid,
                            requested.privilege
                        )
                    END AS allowed
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                CROSS JOIN api_roles AS role
                CROSS JOIN LATERAL unnest(
                    CASE
                        WHEN c.relkind = 'S'
                        THEN ARRAY['SELECT', 'UPDATE', 'USAGE']::text[]
                        ELSE ARRAY[
                            'SELECT',
                            'INSERT',
                            'UPDATE',
                            'DELETE',
                            'TRUNCATE',
                            'REFERENCES',
                            'TRIGGER',
                            'MAINTAIN'
                        ]::text[]
                    END
                ) AS requested(privilege)
                WHERE n.nspname = 'public'
                  AND c.relkind = ANY(
                      ARRAY['r', 'p', 'v', 'm', 'f', 'S']::"char"[]
                  )
            ),
            function_privileges AS (
                SELECT
                    'function' AS object_class,
                    format(
                        '%%I.%%I(%%s)',
                        n.nspname,
                        p.proname,
                        pg_get_function_identity_arguments(p.oid)
                    ) AS object_identity,
                    role.rolname AS role_name,
                    'EXECUTE' AS privilege,
                    has_function_privilege(role.oid, p.oid, 'EXECUTE') AS allowed
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                CROSS JOIN api_roles AS role
                WHERE n.nspname = 'public'
            )
            SELECT object_class, object_identity, role_name, privilege
            FROM (
                SELECT * FROM relation_privileges
                UNION ALL
                SELECT * FROM function_privileges
            ) AS effective_privileges
            WHERE allowed
            ORDER BY 1, 2, 3, 4
            """,
            (list(SUPABASE_API_ROLES),),
        )
        for object_class, identity, role_name, privilege in cursor.fetchall():
            violations.append(
                f"effective_privilege:{object_class}:{identity}:{role_name}:{privilege}"
            )

        cursor.execute(
            """
            SELECT role.rolname
            FROM pg_roles AS role
            JOIN pg_namespace AS namespace ON namespace.nspname = 'public'
            WHERE role.rolname = ANY(%s)
              AND has_schema_privilege(role.oid, namespace.oid, 'CREATE')
            ORDER BY role.rolname
            """,
            (list(SUPABASE_API_ROLES),),
        )
        for (role_name,) in cursor.fetchall():
            violations.append(f"schema_create_privilege:{role_name}")

        return tuple(sorted(set(violations)))

    def _schema_status_from_cursor(
        self,
        paths: list[str | Path],
        cursor: Any,
    ) -> SchemaStatus:
        expected = {Path(path).name: _migration_checksum(path) for path in paths}
        applied = self._read_applied_migrations(cursor)
        violations = self._security_posture_violations(cursor)
        return _build_schema_status(
            expected,
            applied,
            security_violations=violations,
        )

    def apply_migrations(self, paths: list[str | Path]) -> SchemaStatus:
        expected = {Path(path).name: _migration_checksum(path) for path in paths}
        if len(expected) != len(paths):
            raise ValueError("Migration paths must have unique filenames")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("agentic-trader-cloud-migrations",),
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = self._read_applied_migrations(cursor)
            before = _build_schema_status(expected, applied)
            if before.drifted:
                raise RuntimeError(f"Migration checksum drift: {before.drifted}")
            if before.unexpected:
                raise RuntimeError(f"Unexpected applied migrations: {before.unexpected}")

            for path in paths:
                version = Path(path).name
                if applied.get(version) == expected[version]:
                    continue
                cursor.execute(Path(path).read_text())
                cursor.execute(
                    """
                    INSERT INTO schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version, expected[version]),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "SELECT checksum FROM schema_migrations WHERE version = %s",
                        (version,),
                    )
                    if str(cursor.fetchone()[0]) != expected[version]:
                        raise RuntimeError(f"Migration checksum drift: {version}")

            status = self._schema_status_from_cursor(paths, cursor)
            if not status.current:
                raise RuntimeError(
                    "Cloud schema is not current after migration; "
                    f"missing={status.missing}, drifted={status.drifted}, "
                    f"unexpected={status.unexpected}, "
                    f"security_violations={status.security_violations}"
                )
        return status

    def record_migrations(self, paths: list[str | Path]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            for path in paths:
                version = Path(path).name
                checksum = _migration_checksum(path)
                cursor.execute(
                    """
                    INSERT INTO schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version, checksum),
                )
                cursor.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s",
                    (version,),
                )
                if str(cursor.fetchone()[0]) != checksum:
                    raise RuntimeError(f"Migration checksum drift: {version}")

    def schema_status(self, paths: list[str | Path]) -> SchemaStatus:
        with self._connect() as connection, connection.cursor() as cursor:
            return self._schema_status_from_cursor(paths, cursor)

    def assert_schema_current(self, paths: list[str | Path]) -> SchemaStatus:
        status = self.schema_status(paths)
        if not status.current:
            raise RuntimeError(
                "Cloud schema is not current; "
                f"missing={status.missing}, drifted={status.drifted}, "
                f"unexpected={status.unexpected}, "
                f"security_violations={status.security_violations}"
            )
        return status

    def acquire_run_lease(
        self,
        *,
        task_name: str,
        scheduled_for: datetime,
        git_sha: str,
        lease_seconds: int = 7200,
        now: datetime | None = None,
    ) -> RunLease | None:
        if not task_name.strip() or not git_sha.strip() or not 60 <= lease_seconds <= 86400:
            raise ValueError("Run leases require task, git SHA, and 60-86400 seconds")
        scheduled_for = _utc(scheduled_for)
        now = _utc(now or datetime.now(UTC))
        run_id = _stable_id("automation-run", task_name, scheduled_for)
        lease_token = str(uuid.uuid4())
        expires = now + timedelta(seconds=lease_seconds)
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"automation-window:{task_name}:{scheduled_for.isoformat()}",),
            )
            cursor.execute(
                """
                SELECT run_id, status, lease_expires_at
                FROM automation_runs
                WHERE task_name = %s AND scheduled_for = %s
                FOR UPDATE
                """,
                (task_name, scheduled_for),
            )
            row = cursor.fetchone()
            if row is not None and (str(row[1]) == "completed" or row[2] > now):
                return None
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO automation_runs
                        (run_id, task_name, scheduled_for, git_sha, status,
                         lease_token, lease_expires_at, heartbeat_at, started_at, metadata)
                    VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        task_name,
                        scheduled_for,
                        git_sha,
                        lease_token,
                        expires,
                        now,
                        now,
                        Jsonb({}),
                    ),
                )
            else:
                run_id = str(row[0])
                cursor.execute(
                    """
                    UPDATE automation_runs
                    SET git_sha = %s, status = 'running', lease_token = %s,
                        lease_expires_at = %s, heartbeat_at = %s,
                        failure_reason = NULL, finished_at = NULL
                    WHERE run_id = %s
                    """,
                    (git_sha, lease_token, expires, now, run_id),
                )
        return RunLease(
            run_id,
            task_name,
            scheduled_for,
            git_sha,
            lease_token,
            expires,
            now,
        )

    def heartbeat_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 7200,
        now: datetime | None = None,
    ) -> RunLease:
        now = _utc(now or datetime.now(UTC))
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_runs
                SET heartbeat_at = %s, lease_expires_at = %s
                WHERE run_id = %s AND lease_token = %s AND status = 'running'
                  AND lease_expires_at > %s
                RETURNING task_name, scheduled_for, git_sha
                """,
                (now, expires, run_id, lease_token, now),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Run lease is unavailable, expired, or owned elsewhere")
        return RunLease(run_id, str(row[0]), row[1], str(row[2]), lease_token, expires, now)

    def release_run_lease(
        self,
        run_id: str,
        lease_token: str,
        *,
        status: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> RunLease:
        if status not in {"completed", "failed"}:
            raise ValueError("A run must finish completed or failed")
        now = _utc(now or datetime.now(UTC))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_runs
                SET status = %s, heartbeat_at = %s, lease_expires_at = %s,
                    finished_at = %s, failure_reason = %s
                WHERE run_id = %s AND lease_token = %s AND status = 'running'
                RETURNING task_name, scheduled_for, git_sha
                """,
                (status, now, now, now, reason, run_id, lease_token),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Run lease is unavailable or owned elsewhere")
        self.append_audit_event(
            "automation_run_finished",
            {"status": status, "reason": reason},
            run_id=run_id,
            occurred_at=now,
        )
        return RunLease(run_id, str(row[0]), row[1], str(row[2]), lease_token, now, now, status)

    def assert_active_lease(
        self, run_id: str, lease_token: str, now: datetime | None = None
    ) -> RunLease:
        now = _utc(now or datetime.now(UTC))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task_name, scheduled_for, git_sha, lease_expires_at,
                       heartbeat_at, status
                FROM automation_runs
                WHERE run_id = %s AND lease_token = %s AND status = 'running'
                  AND lease_expires_at > %s
                """,
                (run_id, lease_token, now),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("An active durable run lease is required")
        return RunLease(
            run_id=run_id,
            task_name=str(row[0]),
            scheduled_for=row[1],
            git_sha=str(row[2]),
            lease_token=lease_token,
            lease_expires_at=row[3],
            heartbeat_at=row[4],
            status=str(row[5]),
        )

    @staticmethod
    def _plan_from_row(row: Any) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id=str(row[0]),
            draft_hash=str(row[1]),
            run_id=str(row[2]),
            account_key=str(row[3]),
            trade_date=row[4],
            research_batch_id=str(row[5]),
            snapshot_hash=str(row[6]),
            planned_at=row[7],
            expires_at=row[8],
            status=str(row[9]),
            payload=row[10],
        )

    def persist_plan(self, plan: ExecutionPlan, lease_token: str) -> ExecutionPlan:
        from psycopg.types.json import Jsonb

        _expected_review_refs(plan)
        _validate_plan_integrity(plan)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task_name
                FROM automation_runs
                WHERE run_id = %s AND lease_token = %s AND status = 'running'
                  AND lease_expires_at > CURRENT_TIMESTAMP
                FOR UPDATE
                """,
                (plan.run_id, lease_token),
            )
            lease_row = cursor.fetchone()
            if lease_row is None:
                raise RuntimeError("An active durable run lease is required")
            task_name = str(lease_row[0])

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                cursor.execute(
                    """
                    SELECT p.account_key, r.task_name, p.trade_date
                    FROM execution_plans AS p
                    JOIN automation_runs AS r ON r.run_id = p.run_id
                    WHERE p.plan_id = %s
                    """,
                    (parent_plan_id,),
                )
                parent_row = cursor.fetchone()
                if parent_row is None:
                    raise RuntimeError("Interactive review references an unknown durable plan")
                if parent_row[2] != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return str(parent_row[0]), str(parent_row[1])

            production_task = _resolve_execution_task(plan, task_name, parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            cursor.execute(
                """
                INSERT INTO execution_plans
                    (plan_id, draft_hash, run_id, account_key, trade_date,
                     research_batch_id, snapshot_hash, planned_at, expires_at,
                     status, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (plan_id) DO NOTHING
                """,
                (
                    plan.plan_id,
                    plan.draft_hash,
                    plan.run_id,
                    plan.account_key,
                    plan.trade_date,
                    plan.research_batch_id,
                    plan.snapshot_hash,
                    plan.planned_at,
                    plan.expires_at,
                    plan.status,
                    Jsonb(plan.payload),
                ),
            )
            cursor.execute(
                """
                SELECT plan_id, draft_hash, run_id, account_key, trade_date,
                       research_batch_id, snapshot_hash, planned_at, expires_at,
                       status, payload
                FROM execution_plans WHERE plan_id = %s
                """,
                (plan.plan_id,),
            )
            stored = self._plan_from_row(cursor.fetchone())
            if stored != plan:
                raise ValueError(f"Execution plan {plan.plan_id} is immutable")
            audit_payload = {
                "draft_hash": plan.draft_hash,
                "snapshot_hash": plan.snapshot_hash,
            }
            payload_hash = canonical_hash(audit_payload)
            event_id = _stable_id(
                "execution-audit",
                "execution_plan_persisted",
                plan.run_id,
                plan.plan_id,
                None,
                None,
                plan.planned_at.isoformat(),
                payload_hash,
            )
            cursor.execute(
                """
                INSERT INTO execution_audit_events
                    (event_id, event_type, occurred_at, run_id, plan_id,
                     attempt_id, ref_id, payload, payload_hash)
                VALUES (%s, 'execution_plan_persisted', %s, %s, %s,
                        NULL, NULL, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    plan.planned_at,
                    plan.run_id,
                    plan.plan_id,
                    Jsonb(audit_payload),
                    payload_hash,
                ),
            )
        return stored

    def get_plan(self, plan_id: str, expected_hash: str | None = None) -> ExecutionPlan:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT plan_id, draft_hash, run_id, account_key, trade_date,
                       research_batch_id, snapshot_hash, planned_at, expires_at,
                       status, payload
                FROM execution_plans WHERE plan_id = %s
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Unknown execution plan {plan_id}")
            plan = self._plan_from_row(row)
            if expected_hash is not None and expected_hash != plan.draft_hash:
                cursor.execute(
                    "SELECT review_hash FROM execution_plan_reviews WHERE plan_id = %s",
                    (plan_id,),
                )
                review = cursor.fetchone()
                if review is None or str(review[0]) != expected_hash:
                    raise ValueError("Execution plan hash does not match")
        return plan

    def get_plan_review(
        self,
        plan_id: str,
        expected_review_hash: str | None = None,
    ) -> tuple[ExecutionPlan, PlanReview]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.plan_id, p.draft_hash, p.run_id, p.account_key, p.trade_date,
                       p.research_batch_id, p.snapshot_hash, p.planned_at, p.expires_at,
                       p.status, p.payload,
                       pr.draft_hash, pr.review_hash, pr.review_payload, pr.reviewed_at
                FROM execution_plans AS p
                JOIN execution_plan_reviews AS pr ON pr.plan_id = p.plan_id
                WHERE p.plan_id = %s
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Execution plan {plan_id} has no durable broker review")
        plan = self._plan_from_row(row[:11])
        review = PlanReview(plan_id, str(row[11]), str(row[12]), row[13], row[14])
        if expected_review_hash is not None and review.review_hash != expected_review_hash:
            raise ValueError("Execution plan review hash does not match")
        _validate_plan_integrity(plan)
        _validate_review_integrity(plan, review)
        return plan, review

    def record_plan_review(
        self,
        plan_id: str,
        draft_hash: str,
        review_payload: dict[str, Any],
        *,
        reviewed_at: datetime | None = None,
    ) -> PlanReview:
        from psycopg.types.json import Jsonb

        reviewed_at = _utc(reviewed_at or datetime.now(UTC))
        plan = self.get_plan(plan_id, draft_hash)
        _require_single_order_authority(plan)
        if reviewed_at >= plan.expires_at:
            raise ValueError("Cannot review an expired execution plan")
        normalized = normalize_reviews(plan, review_payload)
        review_hash = canonical_hash({"draft_hash": draft_hash, "review_payload": normalized})
        review = PlanReview(plan_id, draft_hash, review_hash, normalized, reviewed_at)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO execution_plan_reviews
                    (plan_id, draft_hash, review_hash, review_payload, reviewed_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (plan_id) DO NOTHING
                """,
                (plan_id, draft_hash, review_hash, Jsonb(normalized), reviewed_at),
            )
            cursor.execute(
                """
                SELECT draft_hash, review_hash, review_payload, reviewed_at
                FROM execution_plan_reviews WHERE plan_id = %s
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
            existing = PlanReview(plan_id, str(row[0]), str(row[1]), row[2], row[3])
            if (
                existing.draft_hash != review.draft_hash
                or existing.review_hash != review.review_hash
                or existing.review_payload != review.review_payload
            ):
                raise ValueError("Execution plan review is immutable")
            cursor.execute(
                """
                UPDATE execution_plans SET status = 'awaiting_confirmation'
                WHERE plan_id = %s AND status IN ('draft', 'reviewed', 'awaiting_confirmation')
                """,
                (plan_id,),
            )
        self.append_audit_event(
            "execution_plan_reviewed",
            {"review_hash": review_hash},
            run_id=plan.run_id,
            plan_id=plan_id,
            occurred_at=reviewed_at,
        )
        return existing

    def record_confirmation(
        self,
        plan_id: str,
        review_hash: str,
        signature: str,
        *,
        payload: dict[str, Any] | None = None,
        confirmed_at: datetime | None = None,
    ) -> ExecutionConfirmation:
        from psycopg.types.json import Jsonb

        message = confirmation_message(plan_id, review_hash)
        actor_ref = _confirmation_authority(plan_id, review_hash, signature)
        authority_fingerprint = actor_ref.removeprefix("ed25519:")
        confirmation_payload = {
            **deepcopy(payload or {}),
            "literal": message,
            "message": message,
            "signature": signature.strip(),
            "authority_fingerprint": authority_fingerprint,
        }
        confirmed_at = _utc(confirmed_at or datetime.now(UTC))
        plan = self.get_plan(plan_id, review_hash)
        _require_single_order_authority(plan)
        if confirmed_at >= plan.expires_at:
            raise ValueError("Execution plan expired before confirmation")
        confirmation = ExecutionConfirmation(
            _stable_id("execution-confirmation", plan_id, review_hash),
            plan_id,
            review_hash,
            actor_ref,
            confirmed_at,
            plan.expires_at,
            confirmation_payload,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT review_hash FROM execution_plan_reviews WHERE plan_id = %s",
                (plan_id,),
            )
            review = cursor.fetchone()
            if review is None or str(review[0]) != review_hash:
                raise ValueError("Confirmation does not match the exact broker review")
            cursor.execute(
                """
                INSERT INTO execution_confirmations
                    (confirmation_id, plan_id, review_hash, actor_ref,
                     confirmed_at, expires_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (plan_id) DO NOTHING
                """,
                (
                    confirmation.confirmation_id,
                    plan_id,
                    review_hash,
                    actor_ref,
                    confirmed_at,
                    plan.expires_at,
                    Jsonb(confirmation.payload),
                ),
            )
            created = cursor.rowcount == 1
            cursor.execute(
                """
                SELECT confirmation_id, review_hash, actor_ref,
                       confirmed_at, expires_at, payload
                FROM execution_confirmations WHERE plan_id = %s
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
            existing = ExecutionConfirmation(
                str(row[0]), plan_id, str(row[1]), str(row[2]), row[3], row[4], row[5]
            )
            if (
                existing.review_hash != confirmation.review_hash
                or existing.actor_ref != confirmation.actor_ref
                or existing.expires_at != confirmation.expires_at
                or existing.payload != confirmation.payload
            ):
                raise ValueError("Execution plan confirmation is immutable")
            if created:
                cursor.execute(
                    """
                    UPDATE execution_plans SET status = 'confirmed'
                    WHERE plan_id = %s AND status = 'awaiting_confirmation'
                    """,
                    (plan_id,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Execution plan is not awaiting its first confirmation")
        self.append_audit_event(
            "execution_plan_confirmed",
            {"review_hash": review_hash, "actor_ref": actor_ref},
            run_id=plan.run_id,
            plan_id=plan_id,
            occurred_at=confirmed_at,
        )
        return existing

    def validate_confirmation(
        self,
        plan_id: str,
        review_hash: str,
        confirmation_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> ExecutionConfirmation:
        now = _utc(now or datetime.now(UTC))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT confirmation_id, review_hash, actor_ref,
                       confirmed_at, expires_at, payload
                FROM execution_confirmations WHERE plan_id = %s
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
        if (
            row is None
            or str(row[1]) != review_hash
            or (confirmation_id is not None and str(row[0]) != confirmation_id)
            or row[4] <= now
        ):
            raise RuntimeError("A current exact execution confirmation is required")
        return ExecutionConfirmation(
            str(row[0]), plan_id, str(row[1]), str(row[2]), row[3], row[4], row[5]
        )

    @staticmethod
    def _attempt_from_row(row: Any) -> OrderAttempt:
        return OrderAttempt(
            attempt_id=str(row[0]),
            plan_id=str(row[1]),
            confirmation_id=str(row[2]),
            account_key=str(row[3]),
            ref_id=str(row[4]),
            request_hash=str(row[5]),
            broker_request=row[6],
            state=str(row[7]),
            broker_order_id=row[8],
            latest_response=row[9],
            error=row[10],
            created_at=row[11],
            updated_at=row[12],
        )

    def create_order_attempt(
        self,
        *,
        plan_id: str,
        confirmation_id: str,
        review_hash: str,
        ref_id: str,
        broker_request: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[OrderAttempt, bool]:
        from psycopg.types.json import Jsonb

        now = _utc(now or datetime.now(UTC))
        confirmation = self.validate_confirmation(plan_id, review_hash, confirmation_id, now=now)
        plan = self.get_plan(plan_id, review_hash)
        expected = _require_single_order_authority(plan)
        if ref_id not in expected or expected[ref_id] != broker_request:
            raise ValueError("Attempt request differs from reviewed plan")
        request_hash = canonical_hash(broker_request)
        attempt_id = order_attempt_id(plan_id, ref_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-attempt-account:{plan.account_key}",),
            )
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts
                WHERE ref_id = %s
                FOR UPDATE
                """,
                (ref_id,),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                existing = self._attempt_from_row(existing_row)
                if (
                    existing.plan_id != plan_id
                    or existing.confirmation_id != confirmation.confirmation_id
                    or existing.request_hash != request_hash
                ):
                    raise ValueError(f"Order attempt ref_id collision: {ref_id}")
                return existing, False
            cursor.execute(
                """
                SELECT attempt_id
                FROM execution_order_attempts
                WHERE account_key = %s AND state = ANY(%s)
                FOR UPDATE
                """,
                (plan.account_key, list(NONTERMINAL_ATTEMPT_STATES)),
            )
            if cursor.fetchone() is not None:
                raise RuntimeError("An unresolved account order attempt blocks new reservation")
            cursor.execute(
                """
                INSERT INTO execution_order_attempts
                    (attempt_id, plan_id, confirmation_id, account_key, ref_id,
                     request_hash, broker_request, state, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'prepared', %s, %s)
                ON CONFLICT (ref_id) DO NOTHING
                """,
                (
                    attempt_id,
                    plan_id,
                    confirmation.confirmation_id,
                    plan.account_key,
                    ref_id,
                    request_hash,
                    Jsonb(broker_request),
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts WHERE ref_id = %s
                """,
                (ref_id,),
            )
            attempt = self._attempt_from_row(cursor.fetchone())
            if (
                attempt.plan_id != plan_id
                or attempt.confirmation_id != confirmation.confirmation_id
                or attempt.request_hash != request_hash
            ):
                raise ValueError(f"Order attempt ref_id collision: {ref_id}")
            if created:
                self._insert_transition_sql(cursor, attempt, None, "prepared", {}, now)
        return attempt, created

    @staticmethod
    def _insert_transition_sql(
        cursor: Any,
        attempt: OrderAttempt,
        from_state: str | None,
        to_state: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        from psycopg.types.json import Jsonb

        payload_hash = canonical_hash(payload)
        transition_id = _stable_id(
            "attempt-transition", attempt.attempt_id, from_state, to_state, payload_hash
        )
        cursor.execute(
            """
            INSERT INTO execution_attempt_transitions
                (transition_id, attempt_id, from_state, to_state,
                 occurred_at, payload, payload_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transition_id) DO NOTHING
            """,
            (
                transition_id,
                attempt.attempt_id,
                from_state,
                to_state,
                occurred_at,
                Jsonb(payload),
                payload_hash,
            ),
        )

    def transition_order_attempt(
        self,
        attempt_id: str,
        state: str,
        *,
        response: dict[str, Any] | None = None,
        broker_order_id: str | None = None,
        error: str | None = None,
        occurred_at: datetime | None = None,
    ) -> OrderAttempt:
        from psycopg.types.json import Jsonb

        occurred_at = _utc(occurred_at or datetime.now(UTC))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts WHERE attempt_id = %s FOR UPDATE
                """,
                (attempt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            attempt = self._attempt_from_row(row)
            if state in SUBMISSION_AUTHORITY_STATES:
                raise ValueError(f"Attempt state {state} requires the atomic submission-claim path")
            if state == attempt.state:
                if (
                    (response is None or response == attempt.latest_response)
                    and (broker_order_id is None or broker_order_id == attempt.broker_order_id)
                    and (error is None or error == attempt.error)
                ):
                    return attempt
                raise ValueError("Same-state attempt updates must be exact retries")
            if state not in ATTEMPT_TRANSITIONS.get(attempt.state, set()):
                raise ValueError(f"Invalid attempt transition {attempt.state} -> {state}")
            _validate_attempt_transition_evidence(
                attempt,
                state,
                response=response,
                broker_order_id=broker_order_id,
                error=error,
            )
            payload = {
                "response": response,
                "broker_order_id": broker_order_id,
                "error": error,
            }
            cursor.execute(
                """
                UPDATE execution_order_attempts
                SET state = %s,
                    broker_order_id = COALESCE(%s, broker_order_id),
                    latest_response = COALESCE(%s, latest_response),
                    error = %s, updated_at = %s
                WHERE attempt_id = %s
                RETURNING attempt_id, plan_id, confirmation_id, account_key, ref_id,
                          request_hash, broker_request, state, broker_order_id,
                          latest_response, error, created_at, updated_at
                """,
                (
                    state,
                    broker_order_id,
                    Jsonb(response) if response is not None else None,
                    error,
                    occurred_at,
                    attempt_id,
                ),
            )
            updated = self._attempt_from_row(cursor.fetchone())
            self._insert_transition_sql(cursor, attempt, attempt.state, state, payload, occurred_at)
        self.append_audit_event(
            "execution_attempt_transition",
            {"from": attempt.state, "to": state, **payload},
            plan_id=attempt.plan_id,
            attempt_id=attempt_id,
            ref_id=attempt.ref_id,
            occurred_at=occurred_at,
        )
        return updated

    def finalize_filled_attempt_after_picker_sync(
        self,
        attempt_id: str,
        *,
        event_type: str,
        session_date: date,
        occurred_at: datetime | None = None,
    ) -> OrderAttempt:
        from psycopg.types.json import Jsonb

        occurred_at = _utc(occurred_at or datetime.now(UTC))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts
                WHERE attempt_id = %s
                FOR UPDATE
                """,
                (attempt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            attempt = self._attempt_from_row(row)
            if attempt.state != "filled":
                raise RuntimeError("Picker finalization requires a filled durable attempt")
            cursor.execute(
                """
                SELECT 1
                FROM picker_order_events
                WHERE ref_id = %s
                  AND event_type = %s
                  AND account_key = %s
                  AND session_date = %s
                """,
                (attempt.ref_id, event_type, attempt.account_key, session_date),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("Picker fill event is not durably synchronized")
            cursor.execute(
                """
                UPDATE execution_order_attempts
                SET state = 'reconciled', updated_at = %s
                WHERE attempt_id = %s AND state = 'filled'
                RETURNING attempt_id, plan_id, confirmation_id, account_key, ref_id,
                          request_hash, broker_request, state, broker_order_id,
                          latest_response, error, created_at, updated_at
                """,
                (occurred_at, attempt_id),
            )
            updated_row = cursor.fetchone()
            if updated_row is None:
                raise RuntimeError("Filled attempt was concurrently changed")
            updated = self._attempt_from_row(updated_row)
            payload = {"event_type": event_type, "session_date": session_date.isoformat()}
            self._insert_transition_sql(
                cursor, attempt, "filled", "reconciled", payload, occurred_at
            )
            payload_hash = canonical_hash(payload)
            event_id = _stable_id(
                "execution-audit",
                "execution_attempt_picker_finalized",
                attempt.plan_id,
                attempt_id,
                attempt.ref_id,
                occurred_at.isoformat(),
                payload_hash,
            )
            cursor.execute(
                """
                INSERT INTO execution_audit_events
                    (event_id, event_type, occurred_at, plan_id,
                     attempt_id, ref_id, payload, payload_hash)
                VALUES (%s, 'execution_attempt_picker_finalized', %s, %s,
                        %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    occurred_at,
                    attempt.plan_id,
                    attempt_id,
                    attempt.ref_id,
                    Jsonb(payload),
                    payload_hash,
                ),
            )
        return updated

    def refresh_execution_reservation(
        self,
        attempt_id: str,
        *,
        plan_id: str,
        review_hash: str,
        confirmation_id: str,
        ref_id: str,
        validated_at: datetime,
        validation_snapshot_hash: str,
        authority_fingerprint_hash: str,
        now: datetime | None = None,
    ) -> bool:
        """CAS-refresh an expired reservation proof without changing order authority."""

        from psycopg.types.json import Jsonb

        validated_at = _utc(validated_at)
        hex_characters = frozenset("0123456789abcdef")
        if (
            len(validation_snapshot_hash) != 64
            or any(character not in hex_characters for character in validation_snapshot_hash)
            or len(authority_fingerprint_hash) != 64
            or any(character not in hex_characters for character in authority_fingerprint_hash)
        ):
            raise ValueError("Reservation refresh requires SHA-256 snapshot and authority hashes")

        with self._connect() as connection, connection.cursor() as cursor:
            if now is None:
                cursor.execute("SELECT CURRENT_TIMESTAMP")
                occurred_at = cursor.fetchone()[0]
            else:
                occurred_at = _utc(now)
            if validated_at > occurred_at or (occurred_at - validated_at).total_seconds() > 15:
                raise RuntimeError(
                    "Reservation refresh requires a newly revalidated broker snapshot"
                )

            # Match submission-claim lock order. The table lock makes the
            # absence of a halt row stable until this freshness CAS commits.
            cursor.execute("LOCK TABLE picker_control_state IN SHARE MODE")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-attempt-claim:{attempt_id}",),
            )
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts
                WHERE attempt_id = %s
                FOR UPDATE
                """,
                (attempt_id,),
            )
            attempt_row = cursor.fetchone()
            if attempt_row is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            attempt = self._attempt_from_row(attempt_row)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-attempt-account:{attempt.account_key}",),
            )
            if (
                attempt.plan_id != plan_id
                or attempt.confirmation_id != confirmation_id
                or attempt.ref_id != ref_id
            ):
                raise ValueError("Reservation refresh identifiers do not match the attempt")
            if attempt.state != "prepared":
                raise RuntimeError("Only a prepared order attempt can refresh its reservation")

            cursor.execute(
                """
                SELECT p.plan_id, p.draft_hash, p.run_id, p.account_key, p.trade_date,
                       p.research_batch_id, p.snapshot_hash, p.planned_at, p.expires_at,
                       p.status, p.payload,
                       pr.draft_hash, pr.review_hash, pr.review_payload, pr.reviewed_at,
                       c.confirmation_id, c.review_hash, c.actor_ref,
                       c.confirmed_at, c.expires_at, c.payload,
                       r.account_key, r.trade_date, r.notional, r.is_entry,
                       r.is_option_open, r.created_at, r.validated_at,
                       r.validation_snapshot_hash, r.authority_fingerprint_hash
                FROM execution_plans AS p
                JOIN execution_plan_reviews AS pr
                  ON pr.plan_id = p.plan_id
                JOIN execution_confirmations AS c
                  ON c.plan_id = p.plan_id
                JOIN execution_plan_reservations AS r
                  ON r.plan_id = p.plan_id
                 AND r.confirmation_id = c.confirmation_id
                 AND r.attempt_id = %s
                 AND r.ref_id = %s
                 AND r.account_key = p.account_key
                 AND r.trade_date = p.trade_date
                WHERE p.plan_id = %s
                  AND c.confirmation_id = %s
                FOR UPDATE OF p, pr, c, r
                """,
                (attempt_id, ref_id, plan_id, confirmation_id),
            )
            authority_row = cursor.fetchone()
            if authority_row is None:
                raise RuntimeError(
                    "Reservation refresh requires the exact linked budget reservation"
                )
            plan = self._plan_from_row(authority_row[:11])
            review = PlanReview(
                plan_id=plan_id,
                draft_hash=str(authority_row[11]),
                review_hash=str(authority_row[12]),
                review_payload=authority_row[13],
                reviewed_at=authority_row[14],
            )
            confirmation = ExecutionConfirmation(
                confirmation_id=str(authority_row[15]),
                plan_id=plan_id,
                review_hash=str(authority_row[16]),
                actor_ref=str(authority_row[17]),
                confirmed_at=authority_row[18],
                expires_at=authority_row[19],
                payload=authority_row[20],
            )
            approved_order = _sole_approved_order(plan)
            expected = _require_single_order_authority(plan)
            if (
                plan.account_key != attempt.account_key
                or review.review_hash != review_hash
                or confirmation.review_hash != review_hash
                or expected.get(ref_id) != attempt.broker_request
            ):
                raise ValueError("Reservation refresh differs from the exact reviewed order")
            if (
                plan.status != "confirmed"
                or plan.planned_at > occurred_at
                or plan.expires_at <= occurred_at
                or confirmation.confirmed_at > occurred_at
                or confirmation.expires_at <= occurred_at
            ):
                raise RuntimeError(
                    "Reservation refresh requires an active exact plan and confirmation"
                )
            _validate_plan_integrity(plan)
            _validate_review_integrity(plan, review)
            _validate_confirmation_integrity(plan, review, confirmation)
            cursor.execute(
                "SELECT task_name FROM automation_runs WHERE run_id = %s",
                (plan.run_id,),
            )
            run_row = cursor.fetchone()
            if run_row is None:
                raise RuntimeError("Reservation refresh plan run is unavailable")

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                cursor.execute(
                    """
                    SELECT p.account_key, r.task_name, p.trade_date
                    FROM execution_plans AS p
                    JOIN automation_runs AS r ON r.run_id = p.run_id
                    WHERE p.plan_id = %s
                    """,
                    (parent_plan_id,),
                )
                parent_row = cursor.fetchone()
                if parent_row is None:
                    raise RuntimeError("Interactive review plan lineage is unavailable")
                if parent_row[2] != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return str(parent_row[0]), str(parent_row[1])

            production_task = _resolve_execution_task(plan, str(run_row[0]), parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            if (
                str(authority_row[21]) != attempt.account_key
                or authority_row[22] != plan.trade_date
                or abs(float(authority_row[23]) - float(approved_order["notional"])) > 1e-6
                or bool(authority_row[24]) != _order_uses_entry_budget(approved_order)
                or bool(authority_row[25])
                or authority_row[26] > occurred_at
            ):
                raise RuntimeError(
                    "Reservation refresh requires the exact linked budget reservation"
                )
            expected_authority_hash = canonical_hash(plan.payload.get("broker_authority"))
            current_authority_hash = str(authority_row[29] or "")
            if (
                authority_fingerprint_hash != expected_authority_hash
                or current_authority_hash != expected_authority_hash
            ):
                raise RuntimeError("Reservation refresh broker authority differs from the review")

            cursor.execute(
                """
                SELECT halted
                FROM picker_control_state
                WHERE account_key = %s
                """,
                (attempt.account_key,),
            )
            control = cursor.fetchone()
            if control is not None and bool(control[0]):
                raise RuntimeError("A durable trading halt blocks reservation refresh")

            current_validated_at = authority_row[27]
            current_snapshot_hash = (
                str(authority_row[28]) if authority_row[28] is not None else None
            )
            if (
                current_validated_at == validated_at
                and current_snapshot_hash == validation_snapshot_hash
            ):
                return False
            if current_validated_at is not None and (
                current_validated_at > occurred_at
                or (occurred_at - current_validated_at).total_seconds() <= 15
                or validated_at <= current_validated_at
            ):
                raise RuntimeError(
                    "Reservation freshness proof is active or was concurrently refreshed"
                )

            cursor.execute(
                """
                UPDATE execution_plan_reservations
                SET validated_at = %s,
                    validation_snapshot_hash = %s
                WHERE ref_id = %s
                  AND attempt_id = %s
                  AND validated_at IS NOT DISTINCT FROM %s
                  AND validation_snapshot_hash IS NOT DISTINCT FROM %s
                  AND authority_fingerprint_hash = %s
                RETURNING ref_id
                """,
                (
                    validated_at,
                    validation_snapshot_hash,
                    ref_id,
                    attempt_id,
                    current_validated_at,
                    current_snapshot_hash,
                    authority_fingerprint_hash,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("Reservation freshness proof was concurrently changed")

            payload = {
                "old_validated_at": current_validated_at.isoformat()
                if current_validated_at is not None
                else None,
                "old_validation_snapshot_hash": current_snapshot_hash,
                "validated_at": validated_at.isoformat(),
                "validation_snapshot_hash": validation_snapshot_hash,
                "authority_fingerprint_hash": authority_fingerprint_hash,
            }
            payload_hash = canonical_hash(payload)
            event_id = _stable_id(
                "execution-audit",
                "execution_reservation_refreshed",
                plan.run_id,
                plan_id,
                attempt_id,
                ref_id,
                occurred_at.isoformat(),
                payload_hash,
            )
            cursor.execute(
                """
                INSERT INTO execution_audit_events
                    (event_id, event_type, occurred_at, run_id, plan_id,
                     attempt_id, ref_id, payload, payload_hash)
                VALUES (%s, 'execution_reservation_refreshed', %s, %s, %s,
                        %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    occurred_at,
                    plan.run_id,
                    plan_id,
                    attempt_id,
                    ref_id,
                    Jsonb(payload),
                    payload_hash,
                ),
            )
            return True

    def claim_order_attempt_for_submission(
        self,
        attempt_id: str,
        *,
        plan_id: str,
        review_hash: str,
        confirmation_id: str,
        ref_id: str,
        validation_snapshot_hash: str,
        now: datetime | None = None,
    ) -> OrderAttempt:
        """Atomically consume the exact authority needed for one broker call."""

        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            if now is None:
                cursor.execute("SELECT CURRENT_TIMESTAMP")
                occurred_at = cursor.fetchone()[0]
            else:
                occurred_at = _utc(now)

            # SHARE conflicts with all writers. That makes the absence of a
            # control row meaningful too: a concurrent all-order halt cannot be
            # inserted or updated between this check and the state CAS.
            cursor.execute("LOCK TABLE picker_control_state IN SHARE MODE")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-attempt-claim:{attempt_id}",),
            )
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts
                WHERE attempt_id = %s
                FOR UPDATE
                """,
                (attempt_id,),
            )
            attempt_row = cursor.fetchone()
            if attempt_row is None:
                raise ValueError(f"Unknown execution attempt {attempt_id}")
            attempt = self._attempt_from_row(attempt_row)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"execution-attempt-account:{attempt.account_key}",),
            )
            if (
                attempt.plan_id != plan_id
                or attempt.confirmation_id != confirmation_id
                or attempt.ref_id != ref_id
            ):
                raise ValueError("Submission claim identifiers do not match the attempt")
            if attempt.state not in {"prepared", "reserved"}:
                raise RuntimeError("Order attempt was already claimed or is no longer claimable")
            cursor.execute(
                """
                SELECT attempt_id
                FROM execution_order_attempts
                WHERE account_key = %s
                  AND attempt_id <> %s
                  AND state = ANY(%s)
                FOR UPDATE
                """,
                (
                    attempt.account_key,
                    attempt_id,
                    list(NONTERMINAL_ATTEMPT_STATES),
                ),
            )
            if cursor.fetchone() is not None:
                raise RuntimeError(
                    "Another unresolved account order attempt blocks submission claim"
                )

            cursor.execute(
                """
                SELECT p.plan_id, p.draft_hash, p.run_id, p.account_key, p.trade_date,
                       p.research_batch_id, p.snapshot_hash, p.planned_at, p.expires_at,
                       p.status, p.payload,
                       pr.draft_hash, pr.review_hash, pr.review_payload, pr.reviewed_at,
                       c.confirmation_id, c.review_hash, c.actor_ref,
                       c.confirmed_at, c.expires_at, c.payload,
                       r.account_key, r.trade_date, r.notional, r.is_entry,
                       r.is_option_open, r.created_at, r.validated_at,
                       r.validation_snapshot_hash, r.authority_fingerprint_hash
                FROM execution_plans AS p
                JOIN execution_plan_reviews AS pr
                  ON pr.plan_id = p.plan_id
                JOIN execution_confirmations AS c
                  ON c.plan_id = p.plan_id
                JOIN execution_plan_reservations AS r
                  ON r.plan_id = p.plan_id
                 AND r.confirmation_id = c.confirmation_id
                 AND r.attempt_id = %s
                 AND r.ref_id = %s
                 AND r.account_key = p.account_key
                 AND r.trade_date = p.trade_date
                WHERE p.plan_id = %s
                  AND c.confirmation_id = %s
                FOR UPDATE OF p, pr, c, r
                """,
                (attempt_id, ref_id, plan_id, confirmation_id),
            )
            authority_row = cursor.fetchone()
            if authority_row is None:
                raise RuntimeError("Submission claim requires the exact linked budget reservation")
            plan = self._plan_from_row(authority_row[:11])
            review = PlanReview(
                plan_id=plan_id,
                draft_hash=str(authority_row[11]),
                review_hash=str(authority_row[12]),
                review_payload=authority_row[13],
                reviewed_at=authority_row[14],
            )
            confirmation = ExecutionConfirmation(
                confirmation_id=str(authority_row[15]),
                plan_id=plan_id,
                review_hash=str(authority_row[16]),
                actor_ref=str(authority_row[17]),
                confirmed_at=authority_row[18],
                expires_at=authority_row[19],
                payload=authority_row[20],
            )
            expected = _require_single_order_authority(plan)
            approved_order = _sole_approved_order(plan)
            if (
                plan.account_key != attempt.account_key
                or review.review_hash != review_hash
                or confirmation.review_hash != review_hash
                or expected.get(ref_id) != attempt.broker_request
            ):
                raise ValueError("Submission claim differs from the exact reviewed order")
            if (
                plan.status != "confirmed"
                or plan.planned_at > occurred_at
                or plan.expires_at <= occurred_at
                or confirmation.confirmed_at > occurred_at
                or confirmation.expires_at <= occurred_at
            ):
                raise RuntimeError(
                    "Submission claim requires an active exact plan and confirmation"
                )
            _validate_plan_integrity(plan)
            _validate_review_integrity(plan, review)
            _validate_confirmation_integrity(plan, review, confirmation)
            cursor.execute(
                "SELECT task_name FROM automation_runs WHERE run_id = %s",
                (plan.run_id,),
            )
            run_row = cursor.fetchone()
            if run_row is None:
                raise RuntimeError("Submission claim plan run is unavailable")

            def parent_lookup(parent_plan_id: str) -> tuple[str, str]:
                cursor.execute(
                    """
                    SELECT p.account_key, r.task_name, p.trade_date
                    FROM execution_plans AS p
                    JOIN automation_runs AS r ON r.run_id = p.run_id
                    WHERE p.plan_id = %s
                    """,
                    (parent_plan_id,),
                )
                parent_row = cursor.fetchone()
                if parent_row is None:
                    raise RuntimeError("Interactive review plan lineage is unavailable")
                if parent_row[2] != plan.trade_date:
                    raise ValueError("Interactive review changed the original trade session")
                return str(parent_row[0]), str(parent_row[1])

            production_task = _resolve_execution_task(plan, str(run_row[0]), parent_lookup)
            _validate_task_plan_contract(plan, production_task)
            if (
                str(authority_row[21]) != attempt.account_key
                or authority_row[22] != plan.trade_date
                or abs(float(authority_row[23]) - float(approved_order["notional"])) > 1e-6
                or bool(authority_row[24]) != _order_uses_entry_budget(approved_order)
                or bool(authority_row[25])
                or authority_row[26] > occurred_at
            ):
                raise RuntimeError("Submission claim requires the exact linked budget reservation")
            validated_at = authority_row[27]
            if (
                validated_at is None
                or validated_at > occurred_at
                or (occurred_at - validated_at).total_seconds() > 15
                or str(authority_row[28] or "") != validation_snapshot_hash
                or len(validation_snapshot_hash) != 64
            ):
                raise RuntimeError("Submission claim requires a fresh exact broker snapshot")
            authority_hash = str(authority_row[29] or "")
            if (
                authority_hash != canonical_hash(plan.payload.get("broker_authority"))
                or len(authority_hash) != 64
            ):
                raise RuntimeError("Submission claim broker authority differs from the review")

            cursor.execute(
                """
                SELECT halted, halt_scope, halt_reason
                FROM picker_control_state
                WHERE account_key = %s
                """,
                (attempt.account_key,),
            )
            control = cursor.fetchone()
            halted = control is not None and bool(control[0])
            halt_scope = str(control[1] or "entries") if control is not None else "entries"
            if halted and (halt_scope == "all" or bool(authority_row[24])):
                raise RuntimeError("A durable trading halt blocks this submission claim")

            cursor.execute(
                """
                UPDATE execution_plans
                SET status = 'submitting'
                WHERE plan_id = %s AND status = 'confirmed' AND expires_at > %s
                """,
                (plan_id, occurred_at),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Execution plan submission authority was concurrently consumed")
            cursor.execute(
                """
                UPDATE execution_order_attempts
                SET state = 'submitting', error = NULL, updated_at = %s
                WHERE attempt_id = %s AND state = %s
                RETURNING attempt_id, plan_id, confirmation_id, account_key, ref_id,
                          request_hash, broker_request, state, broker_order_id,
                          latest_response, error, created_at, updated_at
                """,
                (occurred_at, attempt_id, attempt.state),
            )
            updated_row = cursor.fetchone()
            if updated_row is None:
                raise RuntimeError("Order attempt submission authority was concurrently consumed")
            updated = self._attempt_from_row(updated_row)
            payload = {
                "reservation_ref_id": ref_id,
                "review_hash": review_hash,
                "authority": "exact_plan_confirmation_reservation",
            }
            self._insert_transition_sql(
                cursor,
                attempt,
                attempt.state,
                "submitting",
                payload,
                occurred_at,
            )
            payload_hash = canonical_hash(payload)
            event_id = _stable_id(
                "execution-audit",
                "execution_submission_claimed",
                plan.run_id,
                plan_id,
                attempt_id,
                ref_id,
                occurred_at.isoformat(),
                payload_hash,
            )
            cursor.execute(
                """
                INSERT INTO execution_audit_events
                    (event_id, event_type, occurred_at, run_id, plan_id,
                     attempt_id, ref_id, payload, payload_hash)
                VALUES (%s, 'execution_submission_claimed', %s, %s, %s,
                        %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    occurred_at,
                    plan.run_id,
                    plan_id,
                    attempt_id,
                    ref_id,
                    Jsonb(payload),
                    payload_hash,
                ),
            )
        return updated

    def nonterminal_attempts(self, account_key: str) -> list[OrderAttempt]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_id, plan_id, confirmation_id, account_key, ref_id,
                       request_hash, broker_request, state, broker_order_id,
                       latest_response, error, created_at, updated_at
                FROM execution_order_attempts
                WHERE account_key = %s AND state = ANY(%s)
                ORDER BY updated_at, attempt_id
                """,
                (account_key, list(NONTERMINAL_ATTEMPT_STATES)),
            )
            return [self._attempt_from_row(row) for row in cursor.fetchall()]

    def record_reconciliation(
        self,
        plan_id: str,
        payload: dict[str, Any],
        *,
        reconciled_at: datetime | None = None,
    ) -> Reconciliation:
        from psycopg.types.json import Jsonb

        reconciled_at = _utc(reconciled_at or datetime.now(UTC))
        plan = self.get_plan(plan_id)
        result_hash = canonical_hash(payload)
        reconciliation = Reconciliation(
            _stable_id("execution-reconciliation", plan_id, result_hash),
            plan_id,
            result_hash,
            bool(payload.get("clean")),
            deepcopy(payload),
            reconciled_at,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO execution_reconciliations
                    (reconciliation_id, plan_id, result_hash, clean, payload, reconciled_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (reconciliation_id) DO NOTHING
                """,
                (
                    reconciliation.reconciliation_id,
                    plan_id,
                    result_hash,
                    reconciliation.clean,
                    Jsonb(payload),
                    reconciled_at,
                ),
            )
            cursor.execute(
                """
                SELECT result_hash, clean, payload, reconciled_at
                FROM execution_reconciliations WHERE reconciliation_id = %s
                """,
                (reconciliation.reconciliation_id,),
            )
            row = cursor.fetchone()
            existing = Reconciliation(
                reconciliation.reconciliation_id,
                plan_id,
                str(row[0]),
                bool(row[1]),
                row[2],
                row[3],
            )
            if (
                existing.result_hash != result_hash
                or existing.clean != reconciliation.clean
                or existing.payload != payload
            ):
                raise ValueError("Execution reconciliation is immutable")
            cursor.execute(
                """
                UPDATE execution_plans SET status = %s WHERE plan_id = %s
                """,
                ("reconciled" if reconciliation.clean else "failed", plan_id),
            )
        self.append_audit_event(
            "execution_reconciled",
            {"clean": reconciliation.clean, "result_hash": result_hash},
            run_id=plan.run_id,
            plan_id=plan_id,
            occurred_at=reconciled_at,
        )
        return existing

    def latest_reconciliation(self, plan_id: str) -> Reconciliation | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reconciliation_id, result_hash, clean, payload, reconciled_at
                FROM execution_reconciliations
                WHERE plan_id = %s ORDER BY reconciled_at DESC LIMIT 1
                """,
                (plan_id,),
            )
            row = cursor.fetchone()
        return (
            Reconciliation(str(row[0]), plan_id, str(row[1]), bool(row[2]), row[3], row[4])
            if row is not None
            else None
        )

    def append_audit_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        plan_id: str | None = None,
        attempt_id: str | None = None,
        ref_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        from psycopg.types.json import Jsonb

        occurred_at = _utc(occurred_at or datetime.now(UTC))
        payload_hash = canonical_hash(payload)
        event_id = _stable_id(
            "execution-audit",
            event_type,
            run_id,
            plan_id,
            attempt_id,
            ref_id,
            occurred_at.isoformat(),
            payload_hash,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO execution_audit_events
                    (event_id, event_type, occurred_at, run_id, plan_id,
                     attempt_id, ref_id, payload, payload_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    event_type,
                    occurred_at,
                    run_id,
                    plan_id,
                    attempt_id,
                    ref_id,
                    Jsonb(payload),
                    payload_hash,
                ),
            )
        return event_id

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        source_uri: str | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        observed_at = _utc(observed_at or datetime.now(UTC))
        if not artifact_type.strip():
            raise ValueError("Runtime artifact type cannot be empty")
        source_uri = sanitize_source_uri(source_uri)
        content_hash = canonical_hash(payload)
        artifact_id = _stable_id("cloud-artifact", run_id, artifact_type, content_hash)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO cloud_runtime_artifacts
                    (artifact_id, run_id, artifact_type, content_hash,
                     payload, source_uri, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO NOTHING
                """,
                (
                    artifact_id,
                    run_id,
                    artifact_type,
                    content_hash,
                    Jsonb(payload),
                    source_uri,
                    observed_at,
                ),
            )
        return {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "content_hash": content_hash,
            "source_uri": source_uri,
            "observed_at": observed_at,
        }

    def upsert_knowledge_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        required = {"node_id", "node_type", "title"}
        if not required.issubset(payload):
            raise ValueError("Knowledge node is incomplete")
        updated_at = _utc(
            datetime.fromisoformat(
                str(payload.get("updated_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_nodes
                    (node_id, node_type, title, payload, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (node_id) DO UPDATE
                SET node_type = EXCLUDED.node_type,
                    title = EXCLUDED.title,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(payload["node_id"]),
                    str(payload["node_type"]),
                    str(payload["title"]),
                    Jsonb(payload),
                    updated_at,
                ),
            )
        return {**deepcopy(payload), "updated_at": updated_at}

    def upsert_knowledge_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        required = {"edge_id", "source_id", "target_id", "relation", "sign", "horizon", "causality"}
        if not required.issubset(payload):
            raise ValueError("Knowledge edge is incomplete")
        if payload["causality"] not in {"hypothesis", "non_causal"}:
            raise ValueError("Knowledge causality must be hypothesis or non_causal")
        updated_at = _utc(
            datetime.fromisoformat(
                str(payload.get("updated_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_edges
                    (edge_id, source_id, target_id, relation, sign,
                     horizon, causality, payload, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (edge_id) DO UPDATE
                SET source_id = EXCLUDED.source_id,
                    target_id = EXCLUDED.target_id,
                    relation = EXCLUDED.relation,
                    sign = EXCLUDED.sign,
                    horizon = EXCLUDED.horizon,
                    causality = EXCLUDED.causality,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(payload["edge_id"]),
                    str(payload["source_id"]),
                    str(payload["target_id"]),
                    str(payload["relation"]),
                    str(payload["sign"]),
                    str(payload["horizon"]),
                    str(payload["causality"]),
                    Jsonb(payload),
                    updated_at,
                ),
            )
        return {**deepcopy(payload), "updated_at": updated_at}

    def append_knowledge_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        required = {"observation_id", "edge_id", "decision_date", "horizon", "polarity"}
        if not required.issubset(payload):
            raise ValueError("Knowledge observation is incomplete")
        if payload["polarity"] not in {"supports", "contradicts", "neutral"}:
            raise ValueError("Knowledge observation polarity is invalid")
        observed_at = _utc(
            datetime.fromisoformat(
                str(payload.get("observed_at", datetime.now(UTC))).replace("Z", "+00:00")
            )
        )
        observation_hash = canonical_hash(payload)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_observations
                    (observation_id, edge_id, run_id, prediction_id, outcome_id,
                     evidence_id, document_hash, decision_date, horizon, regime,
                     polarity, measured_result, observed_at, payload, observation_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id) DO NOTHING
                """,
                (
                    str(payload["observation_id"]),
                    str(payload["edge_id"]),
                    payload.get("run_id"),
                    payload.get("prediction_id"),
                    payload.get("outcome_id"),
                    payload.get("evidence_id"),
                    payload.get("document_hash"),
                    date.fromisoformat(str(payload["decision_date"])),
                    str(payload["horizon"]),
                    payload.get("regime"),
                    str(payload["polarity"]),
                    payload.get("measured_result"),
                    observed_at,
                    Jsonb(payload),
                    observation_hash,
                ),
            )
            cursor.execute(
                """
                SELECT observation_hash, payload, observed_at
                FROM knowledge_observations WHERE observation_id = %s
                """,
                (str(payload["observation_id"]),),
            )
            row = cursor.fetchone()
            if str(row[0]) != observation_hash or row[1] != payload:
                raise ValueError("Knowledge observation is immutable")
            observed_at = row[2]
        return {
            **deepcopy(payload),
            "observed_at": observed_at,
            "observation_hash": observation_hash,
        }
