from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

from .analyzer import analyze_universe, write_analysis
from .cloud_runtime import (
    NONTERMINAL_ATTEMPT_STATES,
    ExecutionPlan,
    OrderAttempt,
    PostgresCloudRuntimeStore,
    canonical_hash,
    native_order_from_response,
)
from .config import StrategyConfig
from .confirmation import (
    confirmation_literal,
    confirmation_message,
    generate_confirmation_key,
    sign_confirmation,
)
from .data import download_adjusted_close
from .execution import (
    CASH_EQUIVALENTS,
    DEFAULT_SYMBOL_ALLOWLIST,
    AccountSnapshot,
    ExecutionLimits,
    ProposedOrder,
    SessionLockedError,
    append_audit_record,
    broker_position_values,
    check_account_halts,
    daily_consumption,
    daily_entry_consumption,
    deterministic_ref_id,
    evaluate_batch,
    load_live_state,
    merge_broker_and_local_consumption,
    plan_orders_from_targets,
    record_live_state,
    record_reservation_consumption,
    session_lock,
    summarize_broker_orders,
)
from .option_chain import (
    download_option_chain_snapshot,
    write_option_chain_snapshot,
)
from .option_execution import (
    OptionAccountSnapshot,
    ProposedOptionOrder,
    evaluate_option_batch,
    summarize_broker_option_orders,
)
from .option_reconcile import reconcile_option_orders
from .options import OptionStructure, analyze_option_structure
from .picker.critic_policy import ALLOWED_CRITIC_MODELS
from .picker.features import liquid_universe, rank_candidates, snapshots_from_ranked
from .picker.invalidation import trading_day_expiry, trading_days_until
from .picker.learning import (
    PromotionPolicy,
    build_promotion_report,
    mark_available_outcomes,
)
from .picker.learning_store import (
    PostgresLearningStore,
    build_shadow_batch,
    market_close_from_dict,
    prediction_batch_from_dict,
)
from .picker.ledger import OFFICIAL_CLOSE_SOURCE, PostgresLedger, account_key
from .picker.models import (
    CRITIC_SOFT_DIMENSIONS,
    ActiveThesis,
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
    PickerDraft,
    QuantSnapshot,
    canonical_json,
    content_hash,
)
from .picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
    OptionDraft,
)
from .picker.option_validation import validate_option_draft
from .picker.portfolio import build_picker_portfolio
from .picker.validation import validate_picker_draft
from .proposal import ResearchProposal, validate_proposal
from .reconcile import reconcile
from .research.event_study import run_event_study, write_event_study
from .research.models import ResearchBundle
from .research.scoring import score_bundle
from .runtime_env import load_runtime_env
from .sources.registry import SourceRegistry, quote_is_grounded
from .strategy import target_for_date
from .tournament import STRATEGIES, run_tournament
from .validation import validate_strategy


def _paired_broker_account(raw_account: dict[str, object]) -> str:
    """Resolve the paired broker account without requiring a copied account secret."""
    account_number = str(raw_account.get("account_number", "")).strip()
    if not account_number:
        raise ValueError("Broker snapshot is missing account_number")
    configured = os.environ.get("AGENTIC_TRADER_ACCOUNT", "").strip()
    if configured and configured != account_number:
        raise ValueError("Broker snapshot account does not match configured account")
    if not bool(raw_account.get("agentic_allowed", False)):
        raise ValueError("Broker account is not agentic_allowed")
    return account_number


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        start=args.start,
        out_of_sample_start=args.oos_start,
        end=args.end,
        initial_capital=args.capital,
        include_stocks=args.include_stocks,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--oos-start", default="2015-01-01")
    parser.add_argument("--end")
    parser.add_argument("--capital", type=float, default=5_000.0)
    parser.add_argument("--include-stocks", action="store_true")
    parser.add_argument("--refresh", action="store_true")


def _load_prices(config: StrategyConfig, refresh: bool):
    return download_adjusted_close(
        config.all_assets,
        start=config.start,
        end=config.resolved_end,
        refresh=refresh,
    )


def command_validate(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    prices = _load_prices(config, args.refresh)
    summary = validate_strategy(prices, config, output_dir=args.output)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["status"] == "passes_research_gates" else 2


def command_signal(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    prices = _load_prices(config, args.refresh)
    as_of = prices.dropna(how="all").index[-1]
    target, scores = target_for_date(prices, as_of, config)
    selected = scores.loc[scores["selected"]].sort_values("score", ascending=False)
    last_prices = prices.loc[as_of]
    holdings = []
    for symbol, weight in target[target.gt(0)].items():
        dollars = float(weight * config.initial_capital)
        holdings.append(
            {
                "symbol": symbol,
                "target_weight": float(weight),
                "target_dollars": dollars,
                "reference_price": float(last_prices[symbol]),
                "fractional_shares": float(dollars / last_prices[symbol]),
            }
        )

    payload = {
        "mode": "PAPER_ONLY_DO_NOT_EXECUTE",
        "track": ("SURVIVORSHIP_BIASED_SHADOW" if config.include_stocks else "ETF_CORE"),
        "as_of": as_of.date().isoformat(),
        "capital": config.initial_capital,
        "holdings": holdings,
        "selected_signal_rows": selected.reset_index().to_dict(orient="records"),
        "note": "Targets use adjusted research prices; obtain executable quotes separately.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_event_score(args: argparse.Namespace) -> int:
    bundle = ResearchBundle.from_path(args.bundle)
    scores = [score.to_dict() for score in score_bundle(bundle)]
    payload = {
        "events": len(scores),
        "eligible_for_event_study": sum(
            bool(score["eligible_for_event_study"]) for score in scores
        ),
        "scores": scores,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0


def command_event_study(args: argparse.Namespace) -> int:
    bundle = ResearchBundle.from_path(args.bundle)
    # The data layer requires at least 253 observations. Fetch enough pre-event
    # history even when every event in a bundle is recent.
    earliest = min(event.published_at for event in bundle.events) - timedelta(days=400)
    tickers = tuple(dict.fromkeys([event.ticker for event in bundle.events] + ["SPY"]))
    prices = download_adjusted_close(
        tickers,
        start=earliest.date().isoformat(),
        end=args.end,
        refresh=args.refresh,
    )
    result = run_event_study(
        bundle,
        prices,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    report = write_event_study(result, args.output)
    print(json.dumps(report, indent=2, default=str))
    return 0 if result.status == "passes_event_study_gates" else 2


def command_tournament(args: argparse.Namespace) -> int:
    config = StrategyConfig(
        start=args.start,
        out_of_sample_start=args.holdout_start,
        end=args.end,
        initial_capital=args.capital,
        include_stocks=False,
    )
    prices = _load_prices(config, args.refresh)
    report = run_tournament(
        prices,
        config,
        development_start=args.development_start,
        holdout_start=args.holdout_start,
        output_dir=args.output,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["status"] == "pure_algo_candidate_passes" else 2


def command_tournament_signal(args: argparse.Namespace) -> int:
    summary = json.loads(Path(args.summary).read_text())
    selected = str(summary["selected_strategy"])
    if selected not in STRATEGIES or args.challenger not in STRATEGIES:
        raise ValueError("Tournament summary or challenger has an unknown strategy")
    config = StrategyConfig(
        start=args.start,
        end=args.end,
        initial_capital=args.capital,
        include_stocks=False,
    )
    prices = _load_prices(config, args.refresh)
    as_of = prices.dropna(how="all").index[-1]

    def book(strategy: str) -> dict[str, object]:
        target = STRATEGIES[strategy](prices, config).loc[as_of]
        holdings = [
            {
                "symbol": symbol,
                "target_weight": float(weight),
                "target_dollars": float(weight * args.capital),
            }
            for symbol, weight in target[target.gt(0)].items()
        ]
        return {"strategy": strategy, "holdings": holdings}

    challenger = book(args.challenger)
    payload = {
        "mode": "PAPER_ONLY_DO_NOT_EXECUTE",
        "as_of": as_of.date().isoformat(),
        "capital_per_shadow_book": args.capital,
        "tournament_status": summary["status"],
        "books": {
            "development_selected": book(selected),
            "forward_challenger": challenger,
            "hybrid": {
                **challenger,
                "event_sleeve_weight": 0.0,
                "reason": "Alternative-data event-study gates have not passed.",
            },
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    requested = [value.strip().upper() for value in args.tickers.split(",")]
    tickers = tuple(dict.fromkeys([*requested, args.benchmark.upper()]))
    prices = download_adjusted_close(
        tickers,
        start=args.start,
        end=args.end,
        refresh=args.refresh,
    )
    analysis = analyze_universe(prices, benchmark=args.benchmark.upper())
    payload = write_analysis(analysis, args.output)
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_option_analyze(args: argparse.Namespace) -> int:
    structure = OptionStructure.from_path(args.spec)
    payload = analyze_option_structure(structure)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0


def command_option_chain(args: argparse.Namespace) -> int:
    snapshot = download_option_chain_snapshot(args.symbol, args.expiration)
    payload = write_option_chain_snapshot(snapshot, args.output)
    print(json.dumps(payload, indent=2))
    return 0


def command_proposal_validate(args: argparse.Namespace) -> int:
    proposal = ResearchProposal.from_path(args.proposal)
    evidence_bundle = ResearchBundle.from_path(args.evidence_bundle)
    symbols = tuple(dict.fromkeys([*(leg.symbol for leg in proposal.legs), args.benchmark.upper()]))
    prices = download_adjusted_close(
        symbols,
        start=args.start,
        end=args.end,
        refresh=args.refresh,
    )
    analysis = analyze_universe(prices, benchmark=args.benchmark.upper())
    payload = validate_proposal(
        proposal,
        analysis,
        args.capital,
        known_evidence_ids={item.id for item in evidence_bundle.evidence},
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0 if payload["accepted_for_shadow_research"] else 2


def _migration_paths(path: str | None = None) -> list[Path]:
    return [Path(path)] if path else sorted(Path("db/migrations").glob("*.sql"))


_DURABLE_SECRET_KEYS = {
    "access_token",
    "account_id",
    "account_number",
    "account_url",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_DURABLE_ACCOUNT_KEYS = {"account_id", "account_number", "account_url"}
_DURABLE_ACCOUNT_KEY_SUFFIXES = ("_account_id", "_account_number", "_account_url")


def _redact_durable_payload(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            is_account_key = normalized in _DURABLE_ACCOUNT_KEYS or normalized.endswith(
                _DURABLE_ACCOUNT_KEY_SUFFIXES
            )
            if (
                normalized in _DURABLE_SECRET_KEYS
                or normalized.endswith(("_token", "_secret"))
                or is_account_key
            ):
                if is_account_key:
                    text = "" if item is None else str(item).strip()
                    redacted[str(key)] = f"••••{text[-4:]}" if text else "<redacted>"
                else:
                    redacted[str(key)] = "<redacted>"
            else:
                redacted[str(key)] = _redact_durable_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_durable_payload(item) for item in value]
    return value


def _parse_aware_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include an explicit timezone")
    return parsed.astimezone(UTC)


def _validate_cloud_run_window(
    task_name: str,
    scheduled_for: datetime,
    *,
    now: datetime | None = None,
) -> None:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    pacific = scheduled_for.astimezone(ZoneInfo("America/Los_Angeles"))
    exact_minute = pacific.second == 0 and pacific.microsecond == 0
    if task_name == "morning-live":
        valid = pacific.weekday() <= 4 and (pacific.hour, pacific.minute) == (6, 35)
        if not exact_minute or not valid:
            raise ValueError("morning-live must bind the 06:35 Pacific weekday window")
    elif task_name == "evening-live":
        valid = pacific.weekday() in {6, 0, 1, 2, 3} and (
            pacific.hour,
            pacific.minute,
        ) == (18, 15)
        if not exact_minute or not valid:
            raise ValueError("evening-live must bind the 18:15 Pacific Sunday-Thursday window")
    elif task_name.startswith("interactive-review:"):
        if not task_name.removeprefix("interactive-review:").strip():
            raise ValueError("Interactive-review leases require the prior plan ID")
        age = abs((now - scheduled_for).total_seconds())
        if age > 300:
            raise ValueError("Interactive-review lease timestamps must be within five minutes")
    else:
        raise ValueError("Unknown cloud automation task")
    if task_name in {"morning-live", "evening-live"}:
        age = (now - scheduled_for).total_seconds()
        if age < -60 or age > 6 * 60 * 60:
            raise ValueError("Scheduled production lease is outside its six-hour run window")


def _cloud_plan_document(plan: ExecutionPlan) -> dict[str, object]:
    visible_payload = {
        key: value for key, value in plan.payload.items() if key != "_cloud_snapshot"
    }
    return {
        **visible_payload,
        "plan_id": plan.plan_id,
        "draft_hash": plan.draft_hash,
        "snapshot_hash": plan.snapshot_hash,
        "cloud_status": plan.status,
        "cloud_persisted": True,
    }


def command_cloud_schema_check(args: argparse.Namespace) -> int:
    store = PostgresCloudRuntimeStore.from_env()
    status = store.schema_status(_migration_paths(getattr(args, "path", None)))
    print(json.dumps(status.to_dict(), indent=2))
    return 0 if status.current else 2


def command_cloud_run_acquire(args: argparse.Namespace) -> int:
    store = PostgresCloudRuntimeStore.from_env()
    store.assert_schema_current(_migration_paths())
    scheduled_for = _parse_aware_timestamp(args.scheduled_for)
    _validate_cloud_run_window(args.task, scheduled_for)
    lease = store.acquire_run_lease(
        task_name=args.task,
        scheduled_for=scheduled_for,
        git_sha=args.git_sha,
        lease_seconds=args.lease_seconds,
    )
    if lease is None:
        print(
            json.dumps(
                {
                    "acquired": False,
                    "reason": "schedule_window_already_active_or_completed",
                }
            )
        )
        return 3
    print(json.dumps({"acquired": True, **lease.to_dict()}, default=str))
    return 0


def command_cloud_run_heartbeat(args: argparse.Namespace) -> int:
    lease = PostgresCloudRuntimeStore.from_env().heartbeat_run(
        args.run_id,
        args.lease_token,
        lease_seconds=args.lease_seconds,
    )
    print(json.dumps({"heartbeat": True, **lease.to_dict()}, default=str))
    return 0


def command_cloud_run_finish(args: argparse.Namespace) -> int:
    lease = PostgresCloudRuntimeStore.from_env().release_run_lease(
        args.run_id,
        args.lease_token,
        status=args.status,
        reason=args.reason,
    )
    print(
        json.dumps(
            {
                "finished": True,
                "run_id": lease.run_id,
                "status": lease.status,
            }
        )
    )
    return 0


def command_cloud_artifact_record(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.input).read_text())
    unredacted = raw if isinstance(raw, dict) else {"items": raw}
    payload = _redact_durable_payload(unredacted)
    if not isinstance(payload, dict):  # pragma: no cover - kept true by the wrapper above
        raise TypeError("Durable artifact payload must be an object")
    record = PostgresCloudRuntimeStore.from_env().record_artifact(
        args.run_id,
        args.artifact_type,
        payload,
        source_uri=args.source_uri,
    )
    print(json.dumps(record, indent=2, default=str))
    return 0


def command_cloud_kg_record(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.input).read_text())
    if not isinstance(raw, dict):
        raise ValueError("Runtime KG input must be an object")
    store = PostgresCloudRuntimeStore.from_env()
    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])
    observations = raw.get("observations", [])
    if not all(isinstance(items, list) for items in (nodes, edges, observations)):
        raise ValueError("Runtime KG nodes, edges, and observations must be lists")
    for node in nodes:
        store.upsert_knowledge_node(node)
    for edge in edges:
        store.upsert_knowledge_edge(edge)
    for observation in observations:
        store.append_knowledge_observation(observation)
    payload = {
        "persisted": True,
        "nodes": len(nodes),
        "edges": len(edges),
        "observations": len(observations),
    }
    print(json.dumps(payload))
    return 0


def command_live_review_record(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.reviews).read_text())
    unredacted = raw if isinstance(raw, dict) else {"reviews": raw}
    review_payload = _redact_durable_payload(unredacted)
    if not isinstance(review_payload, dict):  # pragma: no cover - wrapper guarantees this
        raise TypeError("Durable review payload must be an object")
    review = PostgresCloudRuntimeStore.from_env().record_plan_review(
        args.plan_id,
        args.draft_hash,
        review_payload,
    )
    payload = {
        "recorded": True,
        "plan_id": review.plan_id,
        "draft_hash": review.draft_hash,
        "review_hash": review.review_hash,
        "reviewed_at": review.reviewed_at,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_confirmation_keygen(args: argparse.Namespace) -> int:
    public_key = generate_confirmation_key(args.private_key)
    print(
        json.dumps(
            {
                "created": True,
                "private_key": str(Path(args.private_key).expanduser()),
                "runtime_secret_name": "AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY",
                "runtime_secret_value": public_key,
            },
            indent=2,
        )
    )
    return 0


def command_confirmation_sign(args: argparse.Namespace) -> int:
    store = PostgresCloudRuntimeStore.from_env()
    plan, review = store.get_plan_review(args.plan_id, args.plan_hash)
    now = datetime.now(UTC)
    if plan.status != "awaiting_confirmation" or plan.expires_at <= now:
        raise RuntimeError("Only a current reviewed plan can be signed")
    orders = plan.payload.get("approved_orders")
    reviews = review.review_payload.get("reviews")
    if (
        not isinstance(orders, list)
        or len(orders) != 1
        or not isinstance(reviews, list)
        or len(reviews) != 1
    ):
        raise ValueError("Trusted confirmation requires exactly one reviewed order")
    order = orders[0]
    broker_review = reviews[0]
    if not isinstance(order, dict) or not isinstance(broker_review, dict):
        raise ValueError("Trusted confirmation contains malformed durable authority")
    broker_response = broker_review.get("broker_response")
    if not isinstance(broker_response, dict):
        raise ValueError("Trusted confirmation is missing the native broker response")
    account_last_four = str(plan.payload.get("account_last_four") or "").strip()
    if len(account_last_four) != 4 or not account_last_four.isdigit():
        raise ValueError("Durable plan is missing its masked broker account identity")
    rendered = {
        "action": "REVIEW_BEFORE_LOCAL_SIGNATURE",
        "account": f"••••{account_last_four}",
        "plan_id": plan.plan_id,
        "draft_hash": plan.draft_hash,
        "review_hash": review.review_hash,
        "expires_at": plan.expires_at.isoformat(),
        "ref_id": order.get("ref_id"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "notional": order.get("notional"),
        "exact_broker_parameters": order.get("broker_parameters"),
        "native_order_checks": broker_response.get("order_checks"),
        "native_quote_data": broker_response.get("quote_data"),
        "native_alerts": broker_response.get("alerts"),
        "native_fees": broker_response.get("fees"),
        "market_data_disclosure": broker_response.get("market_data_disclosure"),
        "broker_response_hash": broker_review.get("broker_response_hash"),
    }
    print(json.dumps(rendered, indent=2, default=str))
    typed_phrase = f"SIGN {plan.plan_id} {review.review_hash}"
    if not sys.stdin.isatty():
        raise RuntimeError("Trusted confirmation signing requires an interactive local terminal")
    supplied = input(f"Type exactly `{typed_phrase}` to sign this one order: ").strip()
    if supplied != typed_phrase:
        raise ValueError("Local confirmation phrase did not match the exact reviewed order")
    signature = sign_confirmation(args.private_key, plan.plan_id, review.review_hash)
    print(confirmation_literal(plan.plan_id, review.review_hash, signature))
    return 0


def command_live_confirm(args: argparse.Namespace) -> int:
    message = confirmation_message(args.plan_id, args.plan_hash)
    prefix = f"{message} SIGNATURE "
    supplied = args.confirmation_text.strip()
    if not supplied.startswith(prefix):
        raise ValueError(f"Exact signed confirmation must start with: {prefix}<signature>")
    signature = supplied.removeprefix(prefix).strip()
    if not signature or supplied != confirmation_literal(args.plan_id, args.plan_hash, signature):
        raise ValueError("Exact signed confirmation is malformed")
    confirmation = PostgresCloudRuntimeStore.from_env().record_confirmation(
        args.plan_id,
        args.plan_hash,
        signature,
        payload={"literal": supplied},
    )
    payload = {
        "confirmed": True,
        "confirmation_id": confirmation.confirmation_id,
        "plan_id": confirmation.plan_id,
        "review_hash": confirmation.review_hash,
        "expires_at": confirmation.expires_at,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def command_live_plan_export(args: argparse.Namespace) -> int:
    plan = PostgresCloudRuntimeStore.from_env().get_plan(args.plan_id, args.plan_hash)
    payload = _cloud_plan_document(plan)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "exported": True,
                "plan_id": plan.plan_id,
                "draft_hash": plan.draft_hash,
                "output": str(output),
            }
        )
    )
    return 0


def command_live_startup_check(args: argparse.Namespace) -> int:
    snapshot = json.loads(Path(args.snapshot).read_text())
    raw_account = snapshot.get("account", snapshot) if isinstance(snapshot, dict) else {}
    if not isinstance(raw_account, dict):
        raise ValueError("Startup snapshot must contain a broker account object")
    account_number = _paired_broker_account(raw_account)
    attempts = PostgresCloudRuntimeStore.from_env().nonterminal_attempts(
        account_key(account_number)
    )
    raw_orders = raw_account.get("broker_orders", [])
    broker_refs = {
        str(item.get("ref_id") or item.get("client_order_id") or "")
        for item in raw_orders
        if isinstance(item, dict)
    }
    broker_ids = {
        str(item.get("order_id") or item.get("id") or "")
        for item in raw_orders
        if isinstance(item, dict)
    }
    unresolved = [
        {
            "attempt_id": attempt.attempt_id,
            "plan_id": attempt.plan_id,
            "ref_id": attempt.ref_id,
            "state": attempt.state,
            "broker_order_observed": (
                bool(attempt.broker_order_id) and attempt.broker_order_id in broker_ids
            )
            or attempt.ref_id in broker_refs,
            "action": "reconcile_broker_truth_before_any_new_plan",
        }
        for attempt in attempts
    ]
    payload = {
        "account": f"••••{account_number[-4:]}",
        "clear": not unresolved,
        "unresolved_attempts": unresolved,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0 if not unresolved else 2


def command_live_attempt_claim(args: argparse.Namespace) -> int:
    attempt = PostgresCloudRuntimeStore.from_env().claim_order_attempt_for_submission(
        args.attempt_id,
        plan_id=args.plan_id,
        review_hash=args.plan_hash,
        confirmation_id=args.confirmation_id,
        ref_id=args.ref_id,
        validation_snapshot_hash=args.validation_snapshot_hash,
    )
    print(
        json.dumps(
            {
                "attempt_id": attempt.attempt_id,
                "plan_id": attempt.plan_id,
                "confirmation_id": attempt.confirmation_id,
                "ref_id": attempt.ref_id,
                "state": attempt.state,
                "broker_parameters": attempt.broker_request,
            }
        )
    )
    return 0


def _expire_abandoned_pre_submission_attempts(
    cloud_store: PostgresCloudRuntimeStore,
    attempts: list[OrderAttempt],
    *,
    now: datetime,
) -> list[OrderAttempt]:
    remaining: list[OrderAttempt] = []
    for attempt in attempts:
        if (
            attempt.state in {"prepared", "reserved"}
            and not attempt.broker_order_id
            and attempt.latest_response is None
            and cloud_store.get_plan(attempt.plan_id).expires_at <= now
        ):
            cloud_store.transition_order_attempt(
                attempt.attempt_id,
                "expired",
                error="exact_plan_expired_before_submission_claim",
                occurred_at=now,
            )
            continue
        remaining.append(attempt)
    return remaining


def command_live_attempt_transition(args: argparse.Namespace) -> int:
    if args.state in {"reserved", "submitting"}:
        raise ValueError("reserved/submitting require live-attempt-claim")
    response = json.loads(Path(args.response).read_text()) if args.response else None
    if response is not None and not isinstance(response, dict):
        raise ValueError("Broker attempt response must be an object")
    if args.state == "submitted" and (response is None or not args.broker_order_id):
        raise ValueError("Submitted attempts require the broker response and order ID")
    if response is not None and args.broker_order_id:
        response_order = native_order_from_response(response)
        response_order_id = str(response_order.get("order_id") or response_order.get("id") or "")
        if not response_order_id or response_order_id != args.broker_order_id:
            raise ValueError("Broker response order ID differs from the durable transition")
    if args.state == "unknown" and not args.error:
        raise ValueError("Unknown attempts require the ambiguity or timeout error")
    if args.state in {"partially_filled", "filled", "cancelled"} and (
        response is None or not args.broker_order_id
    ):
        raise ValueError(f"{args.state} attempts require broker evidence and order ID")
    if args.state == "rejected" and response is None:
        raise ValueError("Rejected attempts require definitive broker evidence")
    attempt = PostgresCloudRuntimeStore.from_env().transition_order_attempt(
        args.attempt_id,
        args.state,
        response=response,
        broker_order_id=args.broker_order_id,
        error=args.error,
    )
    print(
        json.dumps(
            {
                "attempt_id": attempt.attempt_id,
                "ref_id": attempt.ref_id,
                "state": attempt.state,
                "broker_order_id": attempt.broker_order_id,
            }
        )
    )
    return 0


def _broker_account_from_snapshot(path: str) -> tuple[str, dict[str, object]]:
    snapshot = json.loads(Path(path).read_text())
    raw_account = snapshot.get("account", snapshot) if isinstance(snapshot, dict) else {}
    if not isinstance(raw_account, dict):
        raise ValueError("Broker snapshot must contain an account object")
    return _paired_broker_account(raw_account), raw_account


def command_live_control_status(args: argparse.Namespace) -> int:
    account_number, _ = _broker_account_from_snapshot(args.snapshot)
    account_hash = account_key(account_number)
    ledger = PostgresLedger.from_env()
    control = ledger.control_state(account_hash)
    usage = ledger.execution_budget_usage(account_hash, datetime.now(UTC).date())
    attempts = PostgresCloudRuntimeStore.from_env().nonterminal_attempts(account_hash)
    payload = {
        "account": f"••••{account_number[-4:]}",
        "control": control,
        "execution_usage": usage,
        "nonterminal_attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "plan_id": attempt.plan_id,
                "ref_id": attempt.ref_id,
                "state": attempt.state,
            }
            for attempt in attempts
        ],
    }
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return 0


def command_live_halt(args: argparse.Namespace) -> int:
    account_number, _ = _broker_account_from_snapshot(args.snapshot)
    account_hash = account_key(account_number)
    PostgresLedger.from_env().halt(account_hash, args.reason, scope=args.scope)
    PostgresCloudRuntimeStore.from_env().append_audit_event(
        "operator_halt_engaged",
        {"account_key": account_hash, "scope": args.scope, "reason": args.reason},
    )
    print(
        json.dumps(
            {
                "halted": True,
                "account": f"••••{account_number[-4:]}",
                "scope": args.scope,
                "reason": args.reason,
            },
            ensure_ascii=False,
        )
    )
    return 0


def command_live_plan(args: argparse.Namespace) -> int:
    """Produce a guarded real-money order plan. Never places an order itself."""
    try:
        with session_lock(args.root):
            return _live_plan(args)
    except SessionLockedError as error:
        print(json.dumps({"mode": "REFUSED", "reason": str(error)}, indent=2))
        return 3


def _single_order_confirmation_boundary(
    approved: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Keep one deterministic order per review, preferring reducing exits."""

    if len(approved) <= 1:
        return approved, []

    def priority(item: tuple[int, dict[str, object]]) -> tuple[int, int]:
        index, order = item
        side = str(order.get("side") or "").strip().lower()
        intent = str(order.get("intent_class") or "").strip().lower()
        return (0 if side == "sell" and intent in {"mandatory_exit", "close"} else 1, index)

    selected_index, selected = min(enumerate(approved), key=priority)
    deferred = []
    for index, order in enumerate(approved):
        if index == selected_index:
            continue
        rejected = {**order, "approved": False}
        rejected.pop("broker_parameters", None)
        rejected["reasons"] = [
            *list(order.get("reasons") or []),
            "deferred_separate_plan_review_confirmation_required",
        ]
        deferred.append(rejected)
    return [selected], deferred


def _most_recent_completed_nyse_session(now: datetime | None = None) -> date:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=(now.date() - timedelta(days=21)).isoformat(),
        end_date=now.date().isoformat(),
    )
    if schedule.empty or "market_close" not in schedule:
        raise RuntimeError("NYSE calendar has no recent session data")
    completed = schedule[schedule["market_close"] <= pd.Timestamp(now)]
    if completed.empty:
        raise RuntimeError("NYSE calendar has no completed recent session")
    return completed.index[-1].date()


def _nyse_session_date(now: datetime | None = None) -> date:
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York"))
    candidate = local.date() + (timedelta(days=1) if local.hour >= 20 else timedelta())
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=candidate,
        end_date=candidate + timedelta(days=10),
    )
    if schedule.empty:
        raise RuntimeError("NYSE calendar has no upcoming trading session")
    return schedule.index[0].date()


def _prior_close_anchor_halt(control: dict[str, object], now: datetime | None = None) -> str | None:
    try:
        expected = _most_recent_completed_nyse_session(now)
    except Exception:
        return "picker_prior_close_anchor_calendar_unavailable"
    observed = control.get("prior_close_date")
    if observed is None:
        return f"picker_prior_close_anchor_missing:{expected.isoformat()}"
    try:
        observed_date = (
            observed if isinstance(observed, date) else date.fromisoformat(str(observed))
        )
    except ValueError:
        return f"picker_prior_close_anchor_invalid:{expected.isoformat()}"
    if observed_date != expected:
        return (
            "picker_prior_close_anchor_stale:"
            f"observed={observed_date.isoformat()}:expected={expected.isoformat()}"
        )
    metric_at = control.get("prior_close_metric_at")
    observed_at = control.get("prior_close_observed_at")
    source = str(control.get("prior_close_source") or "")
    artifact_hash = str(control.get("prior_close_artifact_hash") or "")
    if (
        not isinstance(metric_at, datetime)
        or metric_at.tzinfo is None
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or source != OFFICIAL_CLOSE_SOURCE
        or len(artifact_hash) != 64
    ):
        return f"picker_prior_close_provenance_missing:{expected.isoformat()}"
    schedule = mcal.get_calendar("NYSE").schedule(start_date=expected, end_date=expected)
    if schedule.empty:
        return "picker_prior_close_anchor_calendar_unavailable"
    market_close = schedule.iloc[0]["market_close"].to_pydatetime().astimezone(UTC)
    if (
        abs((metric_at.astimezone(UTC) - market_close).total_seconds()) > 60
        or abs((observed_at.astimezone(UTC) - market_close).total_seconds()) > 60
    ):
        return f"picker_prior_close_provenance_shifted:{expected.isoformat()}"
    return None


CANONICAL_SECTOR_IDS = frozenset(
    {
        "communication_services",
        "consumer_discretionary",
        "consumer_staples",
        "energy",
        "financials",
        "health_care",
        "industrials",
        "information_technology",
        "materials",
        "real_estate",
        "utilities",
        "broad_market",
        "commodities",
        "cash_equivalent",
    }
)
SECTOR_TAXONOMY_VERSION = "agentic-gics-v1"
CODE_OWNED_SECTOR_BY_SYMBOL = {
    "AMZN": "consumer_discretionary",
    "BIL": "cash_equivalent",
    "DBC": "commodities",
    "EEM": "broad_market",
    "EFA": "broad_market",
    "FCX": "materials",
    "GLD": "commodities",
    "IEF": "broad_market",
    "IWM": "broad_market",
    "KO": "consumer_staples",
    "LLY": "health_care",
    "META": "communication_services",
    "QQQ": "broad_market",
    "SPY": "broad_market",
    "TLT": "broad_market",
    "VNQ": "real_estate",
    "XLE": "energy",
    "XLV": "health_care",
}
TERMINAL_BROKER_ORDER_STATES = frozenset(
    {
        "filled",
        "cancelled",
        "canceled",
        "rejected",
        "failed",
        "expired",
        "voided",
        "partially_filled_rest_cancelled",
        "locate_failed",
    }
)
NONTERMINAL_BROKER_ORDER_STATES = frozenset(
    {
        "queued",
        "confirmed",
        "unconfirmed",
        "pending",
        "partially_filled",
        "submitted",
        "open",
        "new",
        "placed",
        "pending_cancelled",
        "locating",
    }
)


def _broker_order_state(order: dict[str, object]) -> str:
    state = str(order.get("state") or order.get("status") or "").strip().lower()
    if state not in TERMINAL_BROKER_ORDER_STATES | NONTERMINAL_BROKER_ORDER_STATES:
        raise ValueError(f"Broker order has unknown or missing state: {state or '<missing>'}")
    return state


def _pending_equity_buy_exposure(
    orders: list[dict[str, object]],
) -> tuple[dict[str, float], float]:
    exposure: dict[str, float] = {}
    cash_hold = 0.0
    for order in orders:
        state = _broker_order_state(order)
        if state in TERMINAL_BROKER_ORDER_STATES:
            continue
        side = str(order.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("Nonterminal broker equity order has an unknown side")
        _, notional = summarize_broker_orders([order])
        if side == "buy":
            symbol = str(order.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError("Nonterminal broker equity buy is missing its symbol")
            exposure[symbol] = exposure.get(symbol, 0.0) + float(notional)
            cash_hold += float(notional)
    return exposure, cash_hold


def _pending_option_cash_hold(orders: list[dict[str, object]]) -> float:
    hold = 0.0
    for order in orders:
        if _broker_order_state(order) in TERMINAL_BROKER_ORDER_STATES:
            continue
        _, notional = summarize_broker_option_orders([order])
        hold += float(notional)
    return hold


def _normalized_broker_authority(raw_account: dict[str, object]) -> dict[str, object]:
    """Create the immutable non-quote broker state signed by the human review."""

    account_number = _paired_broker_account(raw_account)
    equity_positions = raw_account.get("broker_positions")
    equity_orders = raw_account.get("broker_orders")
    option_orders = raw_account.get("broker_option_orders")
    option_positions = raw_account.get("broker_option_positions")
    if not all(
        isinstance(items, list) and all(isinstance(item, dict) for item in items)
        for items in (equity_positions, equity_orders, option_orders, option_positions)
    ):
        raise ValueError("Broker authority requires complete native position and order lists")
    normalized_positions: list[dict[str, object]] = []
    for item in equity_positions:
        symbol = str(item.get("symbol") or "").strip().upper()
        raw_quantity = item.get("quantity")
        if isinstance(raw_quantity, dict):
            raw_quantity = raw_quantity.get("amount")
        try:
            quantity = float(raw_quantity)
        except (TypeError, ValueError) as error:
            raise ValueError("Broker authority equity position quantity is invalid") from error
        if not symbol or not isfinite(quantity) or quantity < 0:
            raise ValueError("Broker authority equity position is malformed")
        normalized = _redact_durable_payload(item)
        if not isinstance(normalized, dict):  # pragma: no cover - item is a dict
            raise TypeError("Normalized broker position must be an object")
        normalized_positions.append({**normalized, "symbol": symbol, "quantity": quantity})

    def open_orders(orders: list[dict[str, object]], *, option: bool) -> list[dict[str, object]]:
        normalized_orders: list[dict[str, object]] = []
        for item in orders:
            state = _broker_order_state(item)
            if state in TERMINAL_BROKER_ORDER_STATES:
                continue
            if option:
                _, notional = summarize_broker_option_orders([item])
            else:
                _, notional = summarize_broker_orders([item])
            normalized = _redact_durable_payload(item)
            if not isinstance(normalized, dict):  # pragma: no cover - item is a dict
                raise TypeError("Normalized broker order must be an object")
            normalized_orders.append(
                {**normalized, "state": state, "submitted_notional": float(notional)}
            )
        return sorted(normalized_orders, key=canonical_hash)

    normalized_options: list[dict[str, object]] = []
    for item in option_positions:
        option_id, side, quantity = _broker_option_position_key(item)
        normalized = _redact_durable_payload(item)
        if not isinstance(normalized, dict):  # pragma: no cover - item is a dict
            raise TypeError("Normalized broker option position must be an object")
        normalized_options.append(
            {**normalized, "option_id": option_id, "side": side, "quantity": quantity}
        )
    settled_cash, cash_reasons = _native_settled_cash(raw_account)
    raw_tradable = raw_account.get("session_tradable_symbols")
    if not isinstance(raw_tradable, list):
        raise ValueError("Broker authority requires complete session eligibility")
    account_type = str(raw_account.get("type") or raw_account.get("account_type") or "").lower()
    return {
        "version": "robinhood_broker_authority_v1",
        "account_key": account_key(account_number),
        "account_type": account_type,
        "agentic_allowed": raw_account.get("agentic_allowed") is True,
        "margin_not_used": account_type in {"cash", "limited_margin"}
        and not any(
            reason
            in {
                "native_cash_missing_or_invalid",
                "native_buying_power_missing_or_invalid",
                "native_unleveraged_buying_power_missing_or_invalid",
                "broker_leverage_fields_missing_or_invalid",
                "broker_leveraged_buying_power_detected",
            }
            for reason in cash_reasons
        ),
        "native_cash": _redact_durable_payload(raw_account.get("cash")),
        "native_buying_power": _redact_durable_payload(raw_account.get("buying_power")),
        "pending_deposits": raw_account.get("pending_deposits"),
        "cash_without_margin_after_holds": settled_cash,
        "cash_guard_reasons": cash_reasons,
        "equity_positions": sorted(normalized_positions, key=canonical_hash),
        "open_equity_orders": open_orders(equity_orders, option=False),
        "open_option_orders": open_orders(option_orders, option=True),
        "nonzero_option_positions": sorted(normalized_options, key=canonical_hash),
        "broker_orders_complete_for_session": (
            raw_account.get("broker_orders_complete_for_session") is True
        ),
        "broker_option_orders_complete_for_session": (
            raw_account.get("broker_option_orders_complete_for_session") is True
        ),
        "broker_advanced_orders_complete_for_session": (
            raw_account.get("broker_advanced_orders_complete_for_session") is True
        ),
        "session_is_regular": raw_account.get("session_is_regular") is True,
        "market_hours": str(raw_account.get("market_hours") or "").lower(),
        "session_tradable_symbols": sorted(str(item).upper() for item in raw_tradable),
    }


def _validated_sector_taxonomy(
    raw: object,
    required_symbols: set[str],
) -> tuple[dict[str, str], dict[str, object]]:
    if not isinstance(raw, dict):
        raise ValueError("Durable live snapshots require a versioned sector_taxonomy object")
    source = str(raw.get("source") or "").strip()
    version = str(raw.get("version") or "").strip()
    mapping = raw.get("mapping")
    if (
        source != "agentic_trader_code_owned"
        or version != SECTOR_TAXONOMY_VERSION
        or not isinstance(mapping, dict)
    ):
        raise ValueError("Sector taxonomy must use the code-owned versioned mapping")
    normalized = {
        str(symbol).strip().upper(): str(sector).strip() for symbol, sector in mapping.items()
    }
    invalid = {
        symbol: sector
        for symbol, sector in normalized.items()
        if not symbol or sector not in CANONICAL_SECTOR_IDS
    }
    if invalid:
        raise ValueError(f"Sector taxonomy contains noncanonical labels: {sorted(invalid)}")
    missing = sorted(symbol for symbol in required_symbols if symbol not in normalized)
    if missing:
        raise ValueError(f"Sector taxonomy is incomplete for: {missing}")
    mismatches = sorted(
        symbol
        for symbol in required_symbols
        if CODE_OWNED_SECTOR_BY_SYMBOL.get(symbol) != normalized.get(symbol)
    )
    if mismatches:
        raise ValueError(f"Sector taxonomy differs from code-owned truth for: {mismatches}")
    durable = {"source": source, "version": version, "mapping": normalized}
    return normalized, durable


def _native_settled_cash(raw_account: dict[str, object]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    buying_power = raw_account.get("buying_power")
    cash_candidates: list[float] = []

    def native_cash_value(raw_cash: object, reason: str) -> float | None:
        if isinstance(raw_cash, dict):
            raw_cash = raw_cash.get("amount")
        try:
            candidate = float(raw_cash)
        except (TypeError, ValueError):
            reasons.append(reason)
            return None
        if not isfinite(candidate) or candidate < 0:
            reasons.append(reason)
            return None
        return candidate

    native_cash = native_cash_value(
        raw_account.get("cash"),
        "native_cash_missing_or_invalid",
    )
    if native_cash is not None:
        cash_candidates.append(native_cash)
    if not isinstance(buying_power, dict):
        reasons.append("native_buying_power_missing_or_invalid")
        unleveraged_value = None
    else:
        unleveraged_value = native_cash_value(
            buying_power.get("unleveraged_buying_power"),
            "native_unleveraged_buying_power_missing_or_invalid",
        )
        if unleveraged_value is not None:
            cash_candidates.append(unleveraged_value)
    if not cash_candidates:
        cash = 0.0
    else:
        cash = min(cash_candidates)
        if max(cash_candidates) - cash > 0.01:
            reasons.append("cash_without_margin_sources_contradict")
    if "pending_deposits" not in raw_account:
        pending_deposits = 0.0
        reasons.append("pending_deposits_missing_or_invalid")
    else:
        try:
            pending_deposits = float(raw_account["pending_deposits"])
        except (TypeError, ValueError):
            pending_deposits = 0.0
            reasons.append("pending_deposits_missing_or_invalid")
        if not isfinite(pending_deposits) or pending_deposits < 0:
            pending_deposits = 0.0
            reasons.append("pending_deposits_missing_or_invalid")
    cash = max(cash - pending_deposits, 0.0)
    equity_orders = raw_account.get("broker_orders")
    option_orders = raw_account.get("broker_option_orders")
    if not isinstance(equity_orders, list) or not all(
        isinstance(item, dict) for item in equity_orders
    ):
        reasons.append("native_equity_orders_missing_or_invalid")
        equity_orders = []
    if not isinstance(option_orders, list) or not all(
        isinstance(item, dict) for item in option_orders
    ):
        reasons.append("native_option_orders_missing_or_invalid")
        option_orders = []
    try:
        _, equity_hold = _pending_equity_buy_exposure(equity_orders)
        option_hold = _pending_option_cash_hold(option_orders)
    except ValueError as error:
        reasons.append(f"broker_order_hold_unknown:{error}")
        equity_hold = cash
        option_hold = cash
    cash = max(cash - equity_hold - option_hold, 0.0)
    if raw_account.get("broker_advanced_orders_complete_for_session") is not True:
        reasons.append("broker_advanced_order_truth_unavailable")
    account_type = str(raw_account.get("type") or raw_account.get("account_type") or "").lower()
    if account_type not in {"cash", "limited_margin"}:
        reasons.append("broker_account_type_uses_or_may_use_margin")
    if isinstance(buying_power, dict) and unleveraged_value is not None:
        generic = buying_power.get("buying_power")
        intraday = buying_power.get("intraday_buying_power")
        off_intraday = buying_power.get("off_intraday_buying_power")
        try:
            leverage_values = [
                float(value)
                for value in (generic, intraday, off_intraday)
                if value not in (None, "")
            ]
        except (TypeError, ValueError):
            reasons.append("broker_leverage_fields_missing_or_invalid")
        else:
            if any(value > unleveraged_value + 1e-6 for value in leverage_values):
                reasons.append("broker_leveraged_buying_power_detected")
    return cash, list(dict.fromkeys(reasons))


def _holding_quote_entry_halts(
    positions: list[dict[str, object]],
    prices: dict[str, float],
    quote_timestamps: dict[str, object],
    *,
    now: datetime | None = None,
) -> tuple[list[str], set[str]]:
    """Require fresh valuation inputs for every risky broker holding before a buy."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    reasons: list[str] = []
    risky_symbols: set[str] = set()
    for position in positions:
        symbol = str(position.get("symbol") or "").strip().upper()
        raw_quantity = position.get("quantity")
        if isinstance(raw_quantity, dict):
            raw_quantity = raw_quantity.get("amount")
        try:
            quantity = float(raw_quantity)
        except (TypeError, ValueError):
            reasons.append(f"holding_quantity_missing_or_invalid:{symbol or '<missing>'}")
            continue
        if not isfinite(quantity) or quantity < 0 or not symbol:
            reasons.append(f"holding_quantity_missing_or_invalid:{symbol or '<missing>'}")
            continue
        if quantity == 0 or symbol in CASH_EQUIVALENTS:
            continue
        risky_symbols.add(symbol)
        price = prices.get(symbol)
        if price is None or not isfinite(price) or price <= 0:
            reasons.append(f"holding_price_missing_or_invalid:{symbol}")
        raw_timestamp = quote_timestamps.get(symbol)
        try:
            quote_at = _parse_aware_timestamp(str(raw_timestamp))
        except (TypeError, ValueError):
            reasons.append(f"holding_quote_timestamp_missing_or_invalid:{symbol}")
            continue
        age = (now - quote_at).total_seconds()
        if age < 0 or age > 15:
            reasons.append(f"holding_quote_stale:{symbol}")
    return list(dict.fromkeys(reasons)), risky_symbols


def _broker_session_sell_symbols(
    raw_account: dict[str, object],
    orders: list[dict[str, object]],
    session_date: date,
) -> tuple[set[str], list[str]]:
    completeness = {
        "broker_session_order_history_incomplete": (
            raw_account.get("broker_orders_complete_for_session") is True
        ),
        "broker_option_order_history_incomplete": (
            raw_account.get("broker_option_orders_complete_for_session") is True
        ),
        "broker_advanced_order_truth_unavailable": (
            raw_account.get("broker_advanced_orders_complete_for_session") is True
        ),
    }
    incomplete = [reason for reason, complete in completeness.items() if not complete]
    if incomplete:
        return set(), incomplete
    symbols: set[str] = set()
    reasons: list[str] = []
    for order in orders:
        state = str(order.get("state") or order.get("status") or "").strip().lower()
        side = str(order.get("side") or "").strip().lower()
        if state != "filled" or side != "sell":
            continue
        symbol = str(order.get("symbol") or "").strip().upper()
        raw_timestamp = (
            order.get("last_transaction_at")
            or order.get("updated_at")
            or order.get("executed_at")
            or order.get("created_at")
        )
        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            reasons.append("filled_sell_timestamp_missing_or_invalid")
            continue
        if timestamp.tzinfo is None or not symbol:
            reasons.append("filled_sell_timestamp_or_symbol_missing")
            continue
        if _nyse_session_date(timestamp) == session_date:
            symbols.add(symbol)
    return symbols, list(dict.fromkeys(reasons))


def _entry_broker_guard_reasons(
    symbol: str,
    positions: dict[str, float],
    instrument_metadata: object,
    cash_reasons: list[str],
    order_history_reasons: list[str],
) -> list[str]:
    reasons = [*cash_reasons, *order_history_reasons]
    symbol = symbol.upper()
    if float(positions.get(symbol, 0.0)) > 0:
        reasons.append("no_averaging_down_existing_position")
    metadata = instrument_metadata.get(symbol) if isinstance(instrument_metadata, dict) else None
    asset_type = str(metadata.get("asset_type") or "").lower() if isinstance(metadata, dict) else ""
    source = str(metadata.get("source") or "") if isinstance(metadata, dict) else ""
    if source != "robinhood_scanner" or asset_type not in {"stock", "etf"}:
        reasons.append("broker_asset_type_classification_missing")
    if symbol not in frozenset(DEFAULT_SYMBOL_ALLOWLIST):
        reasons.append("buy_symbol_outside_code_owned_measured_universe")
    return list(dict.fromkeys(reasons))


def _union_broker_and_reservation_usage(
    broker_equity_orders: list[dict[str, object]],
    broker_option_orders: list[dict[str, object]],
    reservations: list[dict[str, object]],
) -> tuple[int, float, int, float]:
    charged: dict[str, tuple[float, bool]] = {}
    ref_aliases: dict[tuple[str, str], str] = {}
    broker_id_aliases: dict[tuple[str, str], str] = {}
    anonymous_index = 0

    def charge(key: str, value: tuple[float, bool]) -> None:
        existing = charged.get(key)
        charged[key] = (
            (max(existing[0], value[0]), existing[1] or value[1]) if existing is not None else value
        )

    for order in broker_equity_orders:
        count, notional = summarize_broker_orders([order])
        if count == 0:
            continue
        ref_id = str(order.get("ref_id") or order.get("client_order_id") or "").strip()
        broker_id = str(order.get("order_id") or order.get("id") or "").strip()
        key = (
            f"equity-broker:{broker_id}"
            if broker_id
            else f"equity-ref:{ref_id}"
            if ref_id
            else f"manual-equity:{anonymous_index}"
        )
        anonymous_index += not bool(ref_id or broker_id)
        charge(key, (float(notional), str(order.get("side") or "").lower() != "sell"))
        if ref_id:
            ref_aliases[("equity", ref_id)] = key
        if broker_id:
            broker_id_aliases[("equity", broker_id)] = key
    for order in broker_option_orders:
        count, notional = summarize_broker_option_orders([order])
        if count == 0:
            continue
        ref_id = str(order.get("ref_id") or order.get("client_order_id") or "").strip()
        broker_id = str(order.get("order_id") or order.get("id") or "").strip()
        key = (
            f"option-broker:{broker_id}"
            if broker_id
            else f"option-ref:{ref_id}"
            if ref_id
            else f"manual-option:{anonymous_index}"
        )
        anonymous_index += not bool(ref_id or broker_id)
        is_entry = str(order.get("position_effect") or "open").lower() != "close"
        charge(key, (float(notional), is_entry))
        if ref_id:
            ref_aliases[("option", ref_id)] = key
        if broker_id:
            broker_id_aliases[("option", broker_id)] = key
    for reservation in reservations:
        ref_id = str(reservation.get("ref_id") or "").strip()
        if not ref_id:
            raise ValueError("Durable execution reservation is missing ref_id")
        kind = "option" if bool(reservation.get("is_option_open")) else "equity"
        broker_id = str(reservation.get("broker_order_id") or "").strip()
        key = (
            (broker_id_aliases.get((kind, broker_id)) if broker_id else None)
            or ref_aliases.get((kind, ref_id))
            or f"{kind}-ref:{ref_id}"
        )
        durable = (float(reservation["notional"]), bool(reservation["is_entry"]))
        charge(key, durable)
    return (
        len(charged),
        sum(item[0] for item in charged.values()),
        sum(item[1] for item in charged.values()),
        sum(item[0] for item in charged.values() if item[1]),
    )


def _live_plan(args: argparse.Namespace) -> int:
    request = json.loads(Path(args.request).read_text())
    if bool(request.get("picker_mode", False)) and not bool(getattr(args, "persist", False)):
        raise ValueError("Picker live plans must use durable cloud persistence")
    raw_account = request["account"]
    prices = {str(k).upper(): float(v) for k, v in request["prices"].items()}

    # The drawdown and daily-loss halts read from persisted state rather than
    # the request so a caller cannot clear a halt by rewriting its own input.
    state = load_live_state(args.root)
    persisted_orders, persisted_notional = daily_consumption(args.root)
    persisted_entry_orders, persisted_entry_notional = daily_entry_consumption(args.root)
    raw_positions = raw_account.get("broker_positions")
    if raw_positions is not None:
        positions = broker_position_values(raw_positions, prices)
    else:
        legacy_positions = raw_account.get("positions", {})
        if not isinstance(legacy_positions, dict):
            raise ValueError(
                "positions must be a symbol-to-value mapping; use broker_positions "
                "for the native Robinhood response"
            )
        positions = {str(k).upper(): float(v) for k, v in legacy_positions.items()}

    raw_orders = raw_account.get("broker_orders")
    native_orders = (
        [item for item in raw_orders if isinstance(item, dict)]
        if isinstance(raw_orders, list)
        else []
    )
    if isinstance(raw_orders, list) and len(native_orders) == len(raw_orders):
        broker_orders, broker_notional = summarize_broker_orders(raw_orders)
        orders_source = "broker"
    else:
        broker_orders = int(raw_account.get("orders_today", 0))
        broker_notional = float(raw_account.get("notional_today", 0.0))
        # A caller-provided label is not proof that the count came from the
        # broker. Only the raw response parsed above earns broker verification.
        orders_source = "unknown"
    raw_option_orders = raw_account.get("broker_option_orders")
    option_orders_verified = isinstance(raw_option_orders, list)
    if option_orders_verified:
        _, option_notional = summarize_broker_option_orders(raw_option_orders)
        broker_orders += len(raw_option_orders)
        broker_notional += option_notional

    equity = float(raw_account["equity"])
    settled_cash, cash_entry_halts = _native_settled_cash(raw_account)
    try:
        pending_buy_exposure, _ = _pending_equity_buy_exposure(native_orders)
    except ValueError as error:
        pending_buy_exposure = {}
        cash_entry_halts.append(f"pending_equity_exposure_unknown:{error}")
    for symbol, notional in pending_buy_exposure.items():
        positions[symbol] = positions.get(symbol, 0.0) + notional
    session_date = _nyse_session_date()
    native_sell_symbols, order_history_entry_halts = _broker_session_sell_symbols(
        raw_account,
        native_orders,
        session_date,
    )
    instrument_metadata = request.get("instrument_metadata")
    broker_authority = (
        _normalized_broker_authority(raw_account) if bool(getattr(args, "persist", False)) else {}
    )
    picker_halts: list[str] = []
    packet_trade_dates: dict[str, str] = {}
    raw_broker_option_positions = raw_account.get("broker_option_positions")
    if not isinstance(raw_broker_option_positions, list):
        picker_halts.append("broker_option_positions_missing")
    database_high_water_mark: float | None = None
    option_reserved_cash = 0.0
    database_prior_close: float | None = None
    durable_usage: dict[str, float | int] = {
        "total_orders": 0,
        "total_notional": 0.0,
        "entry_orders": 0,
        "entry_notional": 0.0,
        "option_openings": 0,
    }
    durable_reservations: list[dict[str, object]] = []
    same_session_exits: set[str] = set(native_sell_symbols)
    durable_ledger: PostgresLedger | None = None
    if bool(getattr(args, "persist", False)):
        if not option_orders_verified:
            picker_halts.append("broker_option_orders_missing")
        configured_account = _paired_broker_account(raw_account)
        durable_ledger = PostgresLedger.from_env()
        account_hash = account_key(configured_account)
        same_session_exits.update(
            durable_ledger.same_session_exit_symbols(account_hash, session_date)
        )
        control = durable_ledger.control_state(account_hash)
        anchor_halt = _prior_close_anchor_halt(control)
        if anchor_halt:
            picker_halts.append(anchor_halt)
        durable_usage = durable_ledger.execution_budget_usage(account_hash, session_date)
        durable_reservations = durable_ledger.execution_reservations_for_session(
            account_hash,
            session_date,
        )
        database_prior_close = control.get("prior_close_equity")
        if bool(control.get("halted")):
            halt_prefix = (
                "picker_database_all_halt"
                if control.get("halt_scope") == "all"
                else "picker_database_halt"
            )
            picker_halts.append(f"{halt_prefix}:{control.get('halt_reason') or 'unspecified'}")
        database_high_water_mark = durable_ledger.record_equity_peak(account_hash, equity)
        active_option_positions = [
            position
            for position in durable_ledger.option_positions()
            if position.status in {"pending_open", "open", "closing"}
        ]
        if isinstance(raw_broker_option_positions, list):
            picker_halts.extend(
                _equity_option_position_halts(
                    raw_broker_option_positions,
                    active_option_positions,
                )
            )
        targets = {
            str(symbol).upper(): float(weight)
            for symbol, weight in request.get("targets", {}).items()
        }
        option_reserved_cash, option_halts = _option_equity_constraints(
            active_option_positions,
            prices,
            targets,
            equity,
        )
        picker_halts.extend(option_halts)

    if bool(request.get("picker_mode", False)):
        if durable_ledger is None:  # guarded above; retained for type narrowing
            raise RuntimeError("Picker live plans require the durable ledger")
        valid_packets = {
            packet.packet_id: packet for packet in durable_ledger.authorized_packets(session_date)
        }
        packet_trade_dates = {
            packet_id: packet.valid_for_date.isoformat()
            for packet_id, packet in valid_packets.items()
        }
        requested_ids = set(str(item) for item in request.get("authorization_packet_ids", []))
        if not requested_ids.issubset(valid_packets):
            picker_halts.append("picker_authorization_packet_missing_or_expired")
        active_theses = durable_ledger.active_theses()
        permitted_buys = {
            packet.symbol
            for packet_id, packet in valid_packets.items()
            if packet_id in requested_ids and packet.action == "buy"
        } | {
            thesis.symbol
            for thesis in active_theses
            if thesis.status in {"pending_entry", "active"}
        }
        requested_buys = {str(item).upper() for item in request.get("buy_symbol_allowlist", [])}
        if not requested_buys.issubset(permitted_buys):
            picker_halts.append("picker_buy_symbol_not_authorized_by_database")
        targets = {
            str(symbol).upper(): float(weight)
            for symbol, weight in request.get("targets", {}).items()
        }
        for packet_id in requested_ids:
            packet = valid_packets.get(packet_id)
            if (
                packet is not None
                and packet.action == "buy"
                and targets.get(packet.symbol, 0.0) > packet.target_weight + 1e-9
            ):
                picker_halts.append(f"picker_target_exceeds_packet_weight:{packet.symbol}")

    if bool(getattr(args, "persist", False)):
        (
            merged_orders,
            merged_notional,
            merged_entry_orders,
            merged_entry_notional,
        ) = _union_broker_and_reservation_usage(
            native_orders,
            [item for item in raw_option_orders if isinstance(item, dict)]
            if isinstance(raw_option_orders, list)
            else [],
            durable_reservations,
        )
    else:
        persisted_orders = max(persisted_orders, int(durable_usage["total_orders"]))
        persisted_notional = max(persisted_notional, float(durable_usage["total_notional"]))
        persisted_entry_orders = max(
            persisted_entry_orders,
            int(durable_usage["entry_orders"]),
        )
        persisted_entry_notional = max(
            persisted_entry_notional,
            float(durable_usage["entry_notional"]),
        )
        (
            merged_orders,
            merged_notional,
            merged_entry_orders,
            merged_entry_notional,
        ) = merge_broker_and_local_consumption(
            broker=(broker_orders, broker_notional),
            persisted=(persisted_orders, persisted_notional),
            persisted_entry=(persisted_entry_orders, persisted_entry_notional),
        )
    session_is_regular = bool(raw_account.get("session_is_regular", False))
    market_hours = str(
        raw_account.get(
            "market_hours",
            "regular_hours" if session_is_regular else "closed",
        )
    ).lower()
    raw_tradable_symbols = raw_account.get("session_tradable_symbols", [])
    raw_quote_timestamps = raw_account.get("quote_timestamps", {})
    raw_quote_spreads = raw_account.get("quote_spreads_bps", {})
    if (
        not isinstance(raw_tradable_symbols, list)
        or not isinstance(raw_quote_timestamps, dict)
        or not isinstance(raw_quote_spreads, dict)
    ):
        raise ValueError(
            "session_tradable_symbols must be a list and quote/spread metadata mappings"
        )
    if isinstance(raw_positions, list) and all(isinstance(item, dict) for item in raw_positions):
        holding_quote_halts, _ = _holding_quote_entry_halts(
            raw_positions,
            prices,
            raw_quote_timestamps,
        )
        cash_entry_halts.extend(holding_quote_halts)
    elif bool(getattr(args, "persist", False)):
        cash_entry_halts.append("broker_positions_missing_or_invalid")
    required_sector_symbols = {
        str(symbol).upper() for symbol in set(positions) | set(request.get("targets", {}))
    }
    if bool(getattr(args, "persist", False)):
        sector_by_symbol, sector_taxonomy = _validated_sector_taxonomy(
            request.get("sector_taxonomy"),
            required_sector_symbols,
        )
    else:
        raw_sectors = request.get("sector_by_symbol", {})
        sector_by_symbol = (
            {str(symbol).upper(): str(sector) for symbol, sector in raw_sectors.items()}
            if isinstance(raw_sectors, dict)
            else {}
        )
        sector_taxonomy = {
            "source": "legacy_unverified",
            "version": "legacy",
            "mapping": sector_by_symbol,
        }
    account = AccountSnapshot(
        account_number=str(raw_account["account_number"]),
        equity=equity,
        cash=max(settled_cash - option_reserved_cash, 0.0),
        positions=positions,
        sector_by_symbol=sector_by_symbol,
        high_water_mark=database_high_water_mark or state.get("high_water_mark"),
        prior_close_equity=(
            database_prior_close
            if bool(getattr(args, "persist", False))
            else state.get("prior_close_equity")
        ),
        # Take the larger of the broker's count and what this repo approved
        # today, so a duplicate run cannot re-spend the daily budget whether or
        # not the other run's orders have reached the broker yet.
        orders_today=merged_orders,
        notional_today=merged_notional,
        entry_orders_today=merged_entry_orders,
        entry_notional_today=merged_entry_notional,
        pending_deposits=float(raw_account.get("pending_deposits", 0.0)),
        net_deposits=(
            float(raw_account["net_deposits"])
            if raw_account.get("net_deposits") is not None
            else None
        ),
        orders_source=orders_source,
        session_is_regular=session_is_regular,
        market_hours=market_hours,
        session_tradable_symbols=tuple(str(symbol).upper() for symbol in raw_tradable_symbols),
        quote_timestamps={
            str(symbol).upper(): timestamp for symbol, timestamp in raw_quote_timestamps.items()
        },
        quote_spreads_bps={
            str(symbol).upper(): float(spread) for symbol, spread in raw_quote_spreads.items()
        },
        broker_identity_verified=bool(raw_account.get("agentic_allowed", False)),
        external_halt_reasons=tuple(picker_halts),
    )
    requested_buy_allowlist = {
        str(item).upper() for item in request.get("buy_symbol_allowlist", [])
    }
    code_owned_buy_allowlist = frozenset(DEFAULT_SYMBOL_ALLOWLIST)
    if requested_buy_allowlist and not requested_buy_allowlist.issubset(code_owned_buy_allowlist):
        raise ValueError("buy_symbol_allowlist cannot expand the code-owned measured universe")
    limits = ExecutionLimits(
        max_order_notional=args.max_order_notional,
        max_position_weight=min(
            args.max_position_weight,
            float(request.get("max_position_weight", args.max_position_weight)),
        ),
        max_orders_per_day=args.max_orders_per_day,
        max_daily_notional=args.max_daily_notional,
        max_entry_orders_per_day=args.max_entry_orders_per_day,
        max_entry_daily_notional=args.max_entry_daily_notional,
        require_fresh_quotes=True,
        max_quote_age_seconds=min(int(request.get("max_quote_age_seconds", 15)), 15),
        allow_extended_hours=bool(request.get("allow_extended_hours", False)),
        buy_symbol_allowlist=(tuple(sorted(requested_buy_allowlist or code_owned_buy_allowlist))),
        sell_symbol_allowlist=(
            tuple(
                sorted(
                    set(str(item).upper() for item in request["sell_symbol_allowlist"])
                    | set(positions)
                )
            )
            if "sell_symbol_allowlist" in request
            else None
        ),
    )
    decisions = plan_orders_from_targets(
        {str(k).upper(): float(v) for k, v in request["targets"].items()},
        account,
        prices=prices,
        limits=limits,
        rebalance_threshold=args.rebalance_threshold,
        metadata_by_symbol=request.get("metadata_by_symbol"),
        root=args.root,
    )
    approved: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for decision in decisions:
        item = decision.to_dict()
        added_reasons: list[str] = []
        if decision.approved and decision.order.side == "buy":
            if decision.order.symbol in same_session_exits:
                added_reasons.append("same_nyse_session_reentry_after_exit")
            added_reasons.extend(
                _entry_broker_guard_reasons(
                    decision.order.symbol,
                    positions,
                    instrument_metadata,
                    cash_entry_halts,
                    order_history_entry_halts,
                )
            )
        if decision.approved and not added_reasons:
            approved.append(item)
        else:
            item["approved"] = False
            item["reasons"] = [*list(decision.reasons), *added_reasons]
            rejected.append(item)
    for item in rejected:
        if not item.get("approved"):
            item.pop("broker_parameters", None)
    if bool(getattr(args, "persist", False)):
        approved, deferred = _single_order_confirmation_boundary(approved)
        rejected.extend(deferred)
    # Identity does not depend on the observed order count, so duplicate runs
    # derive the same key even when they queried at different session stages.
    for order in approved:
        order["ref_id"] = deterministic_ref_id(
            account.account_number,
            order["symbol"],
            order["side"],
            day=session_date,
            pick_id=str(order.get("pick_id") or ""),
            intent=str(order.get("intent_class") or "rebalance"),
        )
    planned_at = datetime.now(UTC)
    payload = {
        "mode": "PLAN_ONLY_REQUIRES_HUMAN_APPROVAL",
        "planned_at": planned_at.isoformat(),
        "expires_at": (planned_at + timedelta(minutes=5)).isoformat(),
        "trade_date": session_date.isoformat(),
        "account_number": account.account_number,
        "equity": equity,
        "prices": prices,
        "picker_mode": bool(request.get("picker_mode", False)),
        "orders_already_used_today": account.orders_today,
        "notional_already_used_today": account.notional_today,
        "entry_orders_already_used_today": account.effective_entry_orders_today,
        "entry_notional_already_used_today": account.effective_entry_notional_today,
        "execution_limits": {
            "max_order_notional": limits.max_order_notional,
            "max_position_weight": limits.max_position_weight,
            "max_broad_market_weight": limits.max_broad_market_weight,
            "max_held_names": limits.max_held_names,
            "max_global_position_weight": limits.max_global_position_weight,
            "max_sector_weight": limits.max_sector_weight,
            "min_cash_reserve_weight": limits.min_cash_reserve_weight,
            "max_orders_per_day": limits.max_orders_per_day,
            "max_daily_notional": limits.max_daily_notional,
            "max_entry_orders_per_day": limits.max_entry_orders_per_day,
            "max_entry_daily_notional": limits.max_entry_daily_notional,
            "max_quote_age_seconds": limits.max_quote_age_seconds,
            "max_extended_spread_bps": limits.max_extended_spread_bps,
            "allow_extended_hours": limits.allow_extended_hours,
        },
        "authorization_packet_ids": list(request.get("authorization_packet_ids", [])),
        "packet_trade_dates": packet_trade_dates,
        "legacy_position_closes": sorted(
            str(symbol).upper() for symbol in request.get("legacy_position_closes", [])
        ),
        "research_batch_id": str(request.get("research_batch_id", "")),
        "sector_by_symbol": sector_by_symbol,
        "sector_taxonomy": sector_taxonomy,
        "instrument_metadata": instrument_metadata if isinstance(instrument_metadata, dict) else {},
        "broker_authority": broker_authority,
        "halts": list(check_account_halts(account, limits, args.root)),
        "approved_orders": approved,
        "rejected_orders": rejected,
        "note": "Approval means the order is within risk limits, not that it is profitable.",
    }
    if bool(getattr(args, "persist", False)):
        run_id = str(getattr(args, "run_id", "") or "").strip()
        lease_token = str(getattr(args, "lease_token", "") or "").strip()
        if not run_id or not lease_token:
            raise ValueError("Durable live planning requires --run-id and --lease-token")
        store = PostgresCloudRuntimeStore.from_env()
        store.assert_schema_current(_migration_paths())
        cloud_plan = ExecutionPlan.build(
            run_id=run_id,
            account_key=account_key(account.account_number),
            snapshot=request,
            payload={
                **payload,
                "account_number": "",
                "account_last_four": account.account_number[-4:],
                "account_key": account_key(account.account_number),
            },
        )
        store.persist_plan(cloud_plan, lease_token)
        payload = _cloud_plan_document(cloud_plan)
    if args.record_equity:
        record_live_state(equity, root=args.root)
    append_audit_record({"event": "live_plan", **payload}, root=args.root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    report_payload = {
        **payload,
        "account_number": f"••••{account.account_number[-4:]}",
    }
    print(json.dumps(report_payload, indent=2, ensure_ascii=False))
    return 0 if approved else 2


def command_live_reconcile(args: argparse.Namespace) -> int:
    """Compare executed fills to the approved plan; halt on anything unaccounted."""
    plan_id = str(getattr(args, "plan_id", "") or "").strip()
    cloud_store: PostgresCloudRuntimeStore | None = None
    cloud_plan: ExecutionPlan | None = None
    if plan_id:
        cloud_store = PostgresCloudRuntimeStore.from_env()
        cloud_plan = cloud_store.get_plan(plan_id)
        plan = _cloud_plan_document(cloud_plan)
    else:
        plan = json.loads(Path(args.plan).read_text())
        if bool(plan.get("picker_mode", False)):
            raise ValueError("Picker live reservations require a durable plan ID and hash")
    executed = json.loads(Path(args.executed).read_text())
    if isinstance(executed, dict):
        executed = executed.get("orders", [])
    if not isinstance(executed, list) or not all(isinstance(item, dict) for item in executed):
        raise ValueError("Executed broker orders must be a list of objects")
    attempts = []
    if cloud_store is not None and cloud_plan is not None:
        attempts = [
            attempt
            for attempt in cloud_store.nonterminal_attempts(cloud_plan.account_key)
            if attempt.plan_id == plan_id
        ]
        broker_id_bindings: dict[str, str] = {}
        for attempt in attempts:
            if not attempt.broker_order_id:
                continue
            existing_ref = broker_id_bindings.get(attempt.broker_order_id)
            if existing_ref is not None and existing_ref != attempt.ref_id:
                raise RuntimeError("Durable attempts bind one broker order ID to multiple refs")
            broker_id_bindings[attempt.broker_order_id] = attempt.ref_id
        normalized_executed = []
        for item in executed:
            broker_order_id = str(item.get("order_id") or item.get("id") or "")
            supplied_ref = str(item.get("ref_id") or item.get("client_order_id") or "")
            bound_ref = broker_id_bindings.get(broker_order_id)
            if supplied_ref and bound_ref and supplied_ref != bound_ref:
                raise ValueError("Broker row ref differs from its durable broker-order binding")
            normalized_executed.append({**item, "ref_id": supplied_ref or bound_ref or ""})
        executed = normalized_executed
    native_by_ref = {
        str(item.get("ref_id") or item.get("client_order_id") or ""): item
        for item in executed
        if str(item.get("ref_id") or item.get("client_order_id") or "")
    }
    result = reconcile(plan.get("approved_orders", []), executed, root=args.root)
    halt_account_key = (
        cloud_plan.account_key
        if cloud_plan is not None
        else account_key(str(plan["account_number"]))
        if plan.get("account_number")
        else ""
    )
    if not result["clean"] and os.environ.get("DATABASE_URL") and halt_account_key:
        ledger = PostgresLedger.from_env()
        ledger.halt(
            halt_account_key,
            ";".join(str(item) for item in result["breaches"]),
            scope="all",
        )
        result["database_halt_engaged"] = True
    if cloud_store is not None and cloud_plan is not None:
        cloud_store.record_reconciliation(plan_id, result)
        if result["clean"]:
            matched = {str(item.get("ref_id") or ""): item for item in result.get("matched", [])}
            terminal = {
                str(item.get("ref_id") or ""): item for item in result.get("terminal_unfilled", [])
            }
            for attempt in attempts:
                if attempt.ref_id in matched:
                    broker_row = native_by_ref[attempt.ref_id]
                    broker_order_id = str(broker_row.get("order_id") or broker_row.get("id") or "")
                    if attempt.state == "filled":
                        if (
                            not attempt.broker_order_id
                            or broker_order_id != attempt.broker_order_id
                        ):
                            raise ValueError(
                                "Filled retry broker order ID differs from durable evidence"
                            )
                        # reconcile() already revalidated the current row's exact
                        # signed request parameters. Preserve the original fill
                        # evidence instead of requiring mutable broker metadata
                        # to remain byte-identical on a crash retry.
                        continue
                    if attempt.state in {"prepared", "reserved"}:
                        attempt = cloud_store.transition_order_attempt(
                            attempt.attempt_id,
                            "unknown",
                            error="broker_fill_recovered_before_submission_state",
                        )
                    cloud_store.transition_order_attempt(
                        attempt.attempt_id,
                        "filled",
                        response=broker_row,
                        broker_order_id=broker_order_id,
                    )
                elif attempt.ref_id in terminal:
                    broker_row = native_by_ref[attempt.ref_id]
                    broker_order_id = str(broker_row.get("order_id") or broker_row.get("id") or "")
                    if attempt.state in {"prepared", "reserved"}:
                        attempt = cloud_store.transition_order_attempt(
                            attempt.attempt_id,
                            "unknown",
                            error="terminal_broker_order_recovered",
                        )
                    terminal_state = (
                        "rejected"
                        if str(terminal[attempt.ref_id].get("state") or "").lower() == "rejected"
                        else "cancelled"
                    )
                    attempt = cloud_store.transition_order_attempt(
                        attempt.attempt_id,
                        terminal_state,
                        response=broker_row,
                        broker_order_id=broker_order_id or None,
                    )
                    cloud_store.transition_order_attempt(attempt.attempt_id, "reconciled")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["clean"] else 2


def _cloud_execution_limits(plan: dict[str, object]) -> ExecutionLimits:
    raw = plan.get("execution_limits")
    if not isinstance(raw, dict):
        raise ValueError("Durable plan is missing its immutable execution limits")
    return ExecutionLimits(
        max_order_notional=float(raw["max_order_notional"]),
        max_position_weight=float(raw["max_position_weight"]),
        max_broad_market_weight=float(raw["max_broad_market_weight"]),
        max_held_names=int(raw["max_held_names"]),
        max_global_position_weight=float(raw["max_global_position_weight"]),
        max_sector_weight=float(raw["max_sector_weight"]),
        min_cash_reserve_weight=float(raw["min_cash_reserve_weight"]),
        max_orders_per_day=int(raw["max_orders_per_day"]),
        max_daily_notional=float(raw["max_daily_notional"]),
        max_entry_orders_per_day=int(raw["max_entry_orders_per_day"]),
        max_entry_daily_notional=float(raw["max_entry_daily_notional"]),
        require_fresh_quotes=True,
        max_quote_age_seconds=min(int(raw["max_quote_age_seconds"]), 15),
        max_extended_spread_bps=float(raw["max_extended_spread_bps"]),
        allow_extended_hours=bool(raw["allow_extended_hours"]),
    )


def _revalidate_cloud_orders(
    plan: dict[str, object],
    cloud_plan: ExecutionPlan,
    fresh_snapshot: dict[str, object],
    ledger: PostgresLedger,
    *,
    root: str,
) -> tuple[str, datetime]:
    raw_account = fresh_snapshot.get("account")
    raw_prices = fresh_snapshot.get("prices")
    if not isinstance(raw_account, dict) or not isinstance(raw_prices, dict):
        raise ValueError("Fresh snapshot requires account and prices objects")
    account_number = _paired_broker_account(raw_account)
    if account_key(account_number) != cloud_plan.account_key:
        raise ValueError("Fresh snapshot account differs from the reviewed plan")
    prices = {str(symbol).upper(): float(price) for symbol, price in raw_prices.items()}
    raw_positions = raw_account.get("broker_positions")
    raw_orders = raw_account.get("broker_orders")
    raw_option_orders = raw_account.get("broker_option_orders")
    raw_option_positions = raw_account.get("broker_option_positions")
    if not isinstance(raw_positions, list) or not isinstance(raw_orders, list):
        raise ValueError("Fresh snapshot requires native broker positions and equity orders")
    if not isinstance(raw_option_orders, list):
        raise ValueError("Fresh snapshot requires native broker option orders")
    if not isinstance(raw_option_positions, list):
        raise ValueError("Fresh snapshot requires native broker option positions")
    positions = broker_position_values(raw_positions, prices)
    planned_order_symbols = {
        str(item.get("symbol") or "").upper()
        for item in plan.get("approved_orders", [])
        if isinstance(item, dict)
    }
    sector_by_symbol, sector_taxonomy = _validated_sector_taxonomy(
        fresh_snapshot.get("sector_taxonomy"),
        set(positions) | planned_order_symbols,
    )
    if sector_taxonomy != plan.get("sector_taxonomy"):
        raise ValueError("Fresh sector taxonomy differs from the reviewed plan")
    durable_reservations = ledger.execution_reservations_for_session(
        cloud_plan.account_key,
        cloud_plan.trade_date,
    )
    current_plan_refs = {
        str(item.get("ref_id") or "")
        for item in plan.get("approved_orders", [])
        if isinstance(item, dict)
    }
    reservations_before_this_plan = [
        reservation
        for reservation in durable_reservations
        if not (
            str(reservation.get("plan_id") or "") == cloud_plan.plan_id
            and str(reservation.get("ref_id") or "") in current_plan_refs
        )
    ]
    control = ledger.control_state(cloud_plan.account_key)
    equity = float(raw_account["equity"])
    settled_cash, cash_entry_halts = _native_settled_cash(raw_account)
    try:
        pending_buy_exposure, _ = _pending_equity_buy_exposure(
            [item for item in raw_orders if isinstance(item, dict)]
        )
    except ValueError as error:
        pending_buy_exposure = {}
        cash_entry_halts.append(f"pending_equity_exposure_unknown:{error}")
    for symbol, notional in pending_buy_exposure.items():
        positions[symbol] = positions.get(symbol, 0.0) + notional
    session_date = _nyse_session_date()
    native_sell_symbols, order_history_entry_halts = _broker_session_sell_symbols(
        raw_account,
        [item for item in raw_orders if isinstance(item, dict)],
        session_date,
    )
    instrument_metadata = fresh_snapshot.get("instrument_metadata")
    if not isinstance(instrument_metadata, dict) or instrument_metadata != plan.get(
        "instrument_metadata"
    ):
        raise ValueError("Fresh instrument classification differs from the reviewed plan")
    fresh_broker_authority = _normalized_broker_authority(raw_account)
    if fresh_broker_authority != plan.get("broker_authority"):
        raise ValueError("Fresh authority-critical broker state differs from the signed review")
    active_options = [
        position
        for position in ledger.option_positions()
        if position.status in {"pending_open", "open", "closing"}
    ]
    option_reserved_cash, option_halts = _option_equity_constraints(
        active_options,
        prices,
        {},
        equity,
    )
    external_halts = list(option_halts)
    external_halts.extend(_equity_option_position_halts(raw_option_positions, active_options))
    anchor_halt = _prior_close_anchor_halt(control)
    if anchor_halt:
        external_halts.append(anchor_halt)
    if bool(control.get("halted")):
        prefix = (
            "picker_database_all_halt"
            if control.get("halt_scope") == "all"
            else "picker_database_halt"
        )
        external_halts.append(f"{prefix}:{control.get('halt_reason') or 'unspecified'}")
    raw_quote_timestamps = raw_account.get("quote_timestamps")
    raw_spreads = raw_account.get("quote_spreads_bps")
    raw_tradable = raw_account.get("session_tradable_symbols")
    if (
        not isinstance(raw_quote_timestamps, dict)
        or not isinstance(raw_spreads, dict)
        or not isinstance(raw_tradable, list)
    ):
        raise ValueError("Fresh snapshot is missing quote/session metadata")
    holding_quote_halts, risky_holding_symbols = _holding_quote_entry_halts(
        raw_positions,
        prices,
        raw_quote_timestamps,
    )
    cash_entry_halts.extend(holding_quote_halts)
    (
        total_broker_orders,
        total_broker_notional,
        total_entry_orders,
        total_entry_notional,
    ) = _union_broker_and_reservation_usage(
        [item for item in raw_orders if isinstance(item, dict)],
        [item for item in raw_option_orders if isinstance(item, dict)],
        reservations_before_this_plan,
    )
    account = AccountSnapshot(
        account_number=account_number,
        equity=equity,
        cash=max(settled_cash - option_reserved_cash, 0.0),
        positions=positions,
        sector_by_symbol=sector_by_symbol,
        high_water_mark=control.get("high_water_mark"),
        prior_close_equity=control.get("prior_close_equity"),
        orders_today=total_broker_orders,
        notional_today=total_broker_notional,
        entry_orders_today=total_entry_orders,
        entry_notional_today=total_entry_notional,
        pending_deposits=float(raw_account.get("pending_deposits", 0.0)),
        net_deposits=(
            float(raw_account["net_deposits"])
            if raw_account.get("net_deposits") is not None
            else None
        ),
        orders_source="broker",
        session_is_regular=bool(raw_account.get("session_is_regular", False)),
        market_hours=str(raw_account.get("market_hours", "closed")).lower(),
        session_tradable_symbols=tuple(str(item).upper() for item in raw_tradable),
        quote_timestamps={str(key).upper(): value for key, value in raw_quote_timestamps.items()},
        quote_spreads_bps={str(key).upper(): float(value) for key, value in raw_spreads.items()},
        broker_identity_verified=True,
        external_halt_reasons=tuple(external_halts),
    )
    limits = _cloud_execution_limits(plan)
    proposed: list[ProposedOrder] = []
    for raw_order in plan.get("approved_orders", []):
        if not isinstance(raw_order, dict):
            raise ValueError("Durable plan contains a malformed order")
        symbol = str(raw_order["symbol"]).upper()
        reviewed_reference = float(raw_order["reference_price"])
        fresh_reference = prices.get(symbol)
        if fresh_reference is None or fresh_reference <= 0:
            raise ValueError(f"Fresh snapshot is missing a positive {symbol} price")
        if raw_order["order_type"] == "market":
            drift_bps = abs(fresh_reference / reviewed_reference - 1.0) * 10_000
            if drift_bps > 20:
                raise ValueError(f"Fresh {symbol} market price moved more than 20 bps")
        order = ProposedOrder(
            symbol=symbol,
            side=str(raw_order["side"]),
            notional=float(raw_order["notional"]),
            order_type=str(raw_order["order_type"]),
            limit_price=(
                float(raw_order["limit_price"])
                if raw_order.get("limit_price") is not None
                else None
            ),
            quantity=(
                float(raw_order["quantity"]) if raw_order.get("quantity") is not None else None
            ),
            reference_price=fresh_reference,
            rationale="Fresh revalidation of the exact reviewed order",
            pick_id=str(raw_order.get("pick_id") or ""),
            intent_class=str(raw_order.get("intent_class") or "rebalance"),
            exit_reason=(str(raw_order["exit_reason"]) if raw_order.get("exit_reason") else None),
            market_hours=str(raw_order["broker_parameters"]["market_hours"]),
            quote_timestamp=raw_quote_timestamps.get(symbol),
        )
        if order.broker_parameters() != raw_order.get("broker_parameters"):
            raise ValueError(f"Reviewed broker parameters changed for {symbol}")
        proposed.append(order)
    decisions = evaluate_batch(proposed, account, limits, root=root)
    rejected = {
        decision.order.symbol: list(decision.reasons)
        for decision in decisions
        if not decision.approved
    }
    same_session_exits = ledger.same_session_exit_symbols(
        cloud_plan.account_key,
        session_date,
    )
    same_session_exits.update(native_sell_symbols)
    for decision in decisions:
        if decision.approved and decision.order.side == "buy":
            added_reasons = _entry_broker_guard_reasons(
                decision.order.symbol,
                positions,
                instrument_metadata,
                cash_entry_halts,
                order_history_entry_halts,
            )
            if decision.order.symbol in same_session_exits:
                added_reasons.append("same_nyse_session_reentry_after_exit")
            if added_reasons:
                rejected[decision.order.symbol] = list(dict.fromkeys(added_reasons))
    if rejected:
        raise ValueError(f"Fresh broker state no longer authorizes the reviewed orders: {rejected}")
    entry_order_present = any(order.side == "buy" for order in proposed)
    freshness_symbols = planned_order_symbols | (
        risky_holding_symbols if entry_order_present else set()
    )
    quote_times = [
        _parse_aware_timestamp(str(raw_quote_timestamps[symbol]))
        for symbol in freshness_symbols
        if symbol
    ]
    if len(quote_times) != len(freshness_symbols) or not quote_times:
        raise ValueError("Fresh snapshot must timestamp every authority-critical quote")
    return account_number, min(quote_times)


def command_live_reserve(args: argparse.Namespace) -> int:
    """Atomically reserve the shared cloud budget immediately before placement."""
    plan_id = str(getattr(args, "plan_id", "") or "").strip()
    plan_hash = str(getattr(args, "plan_hash", "") or "").strip()
    confirmation_id = str(getattr(args, "confirmation_id", "") or "").strip()
    cloud_store: PostgresCloudRuntimeStore | None = None
    cloud_plan: ExecutionPlan | None = None
    fresh_snapshot: dict[str, object] | None = None
    validated_at: datetime | None = None
    validation_snapshot_hash = ""
    authority_fingerprint_hash = ""
    ledger = PostgresLedger.from_env()
    if plan_id:
        if not plan_hash or not confirmation_id:
            raise ValueError("Cloud reservation requires plan hash and confirmation ID")
        snapshot_path = str(getattr(args, "snapshot", "") or "").strip()
        if not snapshot_path:
            raise ValueError("Cloud reservation requires a fresh broker snapshot")
        fresh_snapshot = json.loads(Path(snapshot_path).read_text())
        if not isinstance(fresh_snapshot, dict):
            raise ValueError("Fresh broker snapshot must be an object")
        cloud_store = PostgresCloudRuntimeStore.from_env()
        cloud_plan = cloud_store.get_plan(plan_id, plan_hash)
        cloud_store.validate_confirmation(plan_id, plan_hash, confirmation_id)
        plan = _cloud_plan_document(cloud_plan)
        unresolved = cloud_store.nonterminal_attempts(cloud_plan.account_key)
        unresolved = _expire_abandoned_pre_submission_attempts(
            cloud_store,
            unresolved,
            now=datetime.now(UTC),
        )
        exact_orders = {
            str(order.get("ref_id")): order.get("broker_parameters")
            for order in plan.get("approved_orders", [])
            if isinstance(order, dict)
        }
        blocking_attempts = [
            attempt
            for attempt in unresolved
            if not (
                attempt.plan_id == plan_id
                and attempt.confirmation_id == confirmation_id
                and attempt.state == "prepared"
                and exact_orders.get(attempt.ref_id) == attempt.broker_request
            )
        ]
        if blocking_attempts:
            payload = {
                "reserved_ref_ids": [],
                "blocked_ref_ids": sorted(attempt.ref_id for attempt in blocking_attempts),
                "attempts": [],
                "reason": "account_attempt_requires_broker_reconciliation",
            }
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, indent=2) + "\n")
            print(json.dumps(payload))
            return 2
    else:
        plan = json.loads(Path(args.plan).read_text())
    planned_at = datetime.fromisoformat(str(plan["planned_at"]).replace("Z", "+00:00"))
    expires_at = datetime.fromisoformat(str(plan["expires_at"]).replace("Z", "+00:00"))
    if planned_at.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("Live plan timestamps must be timezone-aware")
    now = datetime.now(UTC)
    planned_at = planned_at.astimezone(UTC)
    expires_at = expires_at.astimezone(UTC)
    if planned_at > now or expires_at <= now or expires_at - planned_at > timedelta(minutes=5):
        raise ValueError("Live plan is expired or has an invalid validity window")
    if cloud_plan is not None and fresh_snapshot is not None:
        configured_account, validated_at = _revalidate_cloud_orders(
            plan,
            cloud_plan,
            fresh_snapshot,
            ledger,
            root=args.root,
        )
        validation_snapshot_hash = canonical_hash(fresh_snapshot)
        authority_fingerprint_hash = canonical_hash(plan["broker_authority"])
    else:
        configured_account = str(plan.get("account_number", "")).strip()
        if not configured_account:
            raise ValueError("Live plan is missing its broker account binding")
    env_account = os.environ.get("AGENTIC_TRADER_ACCOUNT", "").strip()
    if env_account and env_account != configured_account:
        raise ValueError("Live plan account does not match configured account")
    trade_date = cloud_plan.trade_date if cloud_plan is not None else _nyse_session_date()
    research_batch_id = str(plan.get("research_batch_id", ""))
    reservations = []
    for order in plan.get("approved_orders", []):
        is_exit = str(order.get("side", "")).lower() == "sell" and str(
            order.get("intent_class", "")
        ).lower() in {"mandatory_exit", "close"}
        reservations.append(
            (
                str(order["ref_id"]),
                float(order["notional"]),
                not is_exit,
                False,
            )
        )
    if not reservations:
        print(json.dumps({"reserved": [], "reason": "no_approved_orders"}))
        return 0
    control = ledger.control_state(account_key(configured_account))
    if bool(control.get("halted")) and control.get("halt_scope") == "all":
        raise RuntimeError(
            f"Durable all-order halt is engaged: {control.get('halt_reason') or 'unspecified'}"
        )
    observed = (
        int(plan["orders_already_used_today"]),
        float(plan["notional_already_used_today"]),
        int(plan["entry_orders_already_used_today"]),
        float(plan["entry_notional_already_used_today"]),
    )
    reservation_limits = (
        _cloud_execution_limits(plan)
        if cloud_plan is not None
        else ExecutionLimits(
            max_order_notional=float(
                plan.get("execution_limits", {}).get("max_order_notional", 150.0)
            ),
            max_orders_per_day=int(plan.get("execution_limits", {}).get("max_orders_per_day", 8)),
            max_daily_notional=float(
                plan.get("execution_limits", {}).get("max_daily_notional", 800.0)
            ),
            max_entry_orders_per_day=int(
                plan.get("execution_limits", {}).get("max_entry_orders_per_day", 2)
            ),
            max_entry_daily_notional=float(
                plan.get("execution_limits", {}).get("max_entry_daily_notional", 300.0)
            ),
        )
    )
    exit_reservations = [item for item in reservations if not item[2]]
    entry_reservations = [item for item in reservations if item[2]]
    reserved: list[str] = []
    already_reserved: set[str] = set()
    blocked: list[str] = []
    usage = None
    attempts_by_ref: dict[str, object] = {}
    newly_prepared: set[str] = set()
    if bool(control.get("halted")):
        halted_entries = [item[0] for item in entry_reservations]
        blocked.extend(halted_entries)
        entry_reservations = []
    if cloud_store is not None and (
        validated_at is None or not validation_snapshot_hash or not authority_fingerprint_hash
    ):
        raise RuntimeError("Cloud reservation requires a completed fresh-snapshot validation")
    if cloud_store is not None:
        active_ref_ids = {item[0] for item in [*exit_reservations, *entry_reservations]}
        for order in plan.get("approved_orders", []):
            if not isinstance(order, dict):
                raise ValueError("Durable plan contains a malformed order")
            ref_id = str(order["ref_id"])
            if ref_id not in active_ref_ids:
                continue
            attempt, created = cloud_store.create_order_attempt(
                plan_id=plan_id,
                confirmation_id=confirmation_id,
                review_hash=plan_hash,
                ref_id=ref_id,
                broker_request=order["broker_parameters"],
            )
            if attempt.state != "prepared":
                raise RuntimeError("Execution attempt is no longer awaiting durable reservation")
            attempts_by_ref[ref_id] = attempt
            if created:
                newly_prepared.add(ref_id)
    reservation_bindings = {
        ref_id: (
            plan_id,
            confirmation_id,
            attempts_by_ref[ref_id].attempt_id,
            validated_at,
            validation_snapshot_hash,
            authority_fingerprint_hash,
        )
        for ref_id, _, _, _ in reservations
        if cloud_store is not None
    }
    if exit_reservations:
        usage = ledger.reserve_execution_budget(
            account_key(configured_account),
            trade_date,
            exit_reservations,
            max_orders=reservation_limits.max_orders_per_day,
            max_notional=reservation_limits.max_daily_notional,
            max_entry_orders=reservation_limits.max_entry_orders_per_day,
            max_entry_notional=reservation_limits.max_entry_daily_notional,
            observed_usage=observed,
            reservation_bindings=(
                {item[0]: reservation_bindings[item[0]] for item in exit_reservations}
                if cloud_store is not None
                else None
            ),
        )
        if cloud_store is not None:
            reserved.extend(str(item) for item in usage["newly_reserved_ref_ids"])
            reserved.extend(str(item) for item in usage["already_reserved_ref_ids"])
            already_reserved.update(str(item) for item in usage["already_reserved_ref_ids"])
        else:
            reserved.extend(item[0] for item in exit_reservations)
    if entry_reservations:
        try:
            usage = ledger.reserve_execution_budget(
                account_key(configured_account),
                trade_date,
                entry_reservations,
                max_orders=reservation_limits.max_orders_per_day,
                max_notional=reservation_limits.max_daily_notional,
                max_entry_orders=reservation_limits.max_entry_orders_per_day,
                max_entry_notional=reservation_limits.max_entry_daily_notional,
                observed_usage=observed,
                research_batch_id=research_batch_id,
                reservation_bindings=(
                    {item[0]: reservation_bindings[item[0]] for item in entry_reservations}
                    if cloud_store is not None
                    else None
                ),
            )
            if cloud_store is not None:
                reserved.extend(str(item) for item in usage["newly_reserved_ref_ids"])
                reserved.extend(str(item) for item in usage["already_reserved_ref_ids"])
                already_reserved.update(str(item) for item in usage["already_reserved_ref_ids"])
            else:
                reserved.extend(item[0] for item in entry_reservations)
        except RuntimeError:
            blocked.extend(item[0] for item in entry_reservations)
    if cloud_store is not None:
        for ref_id in sorted(already_reserved):
            attempt = attempts_by_ref[ref_id]
            binding = reservation_bindings[ref_id]
            cloud_store.refresh_execution_reservation(
                attempt.attempt_id,
                plan_id=plan_id,
                review_hash=plan_hash,
                confirmation_id=confirmation_id,
                ref_id=ref_id,
                validated_at=binding[3],
                validation_snapshot_hash=binding[4],
                authority_fingerprint_hash=binding[5],
            )
    payload = {
        "reserved_ref_ids": reserved,
        "blocked_ref_ids": blocked,
        "usage": usage,
    }
    if cloud_store is not None:
        payload["validated_at"] = validated_at.isoformat() if validated_at else None
        payload["validation_snapshot_hash"] = validation_snapshot_hash
        payload["authority_fingerprint_hash"] = authority_fingerprint_hash
        attempt_payloads = []
        for ref_id in reserved:
            attempt = attempts_by_ref[ref_id]
            attempt_payloads.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "ref_id": attempt.ref_id,
                    "broker_parameters": attempt.broker_request,
                    "newly_prepared": ref_id in newly_prepared,
                }
            )
        for ref_id in blocked:
            attempt = attempts_by_ref.get(ref_id)
            if attempt is not None and attempt.state == "prepared":
                cloud_store.transition_order_attempt(
                    attempt.attempt_id,
                    "failed",
                    error="durable_execution_reservation_blocked",
                )
        payload["attempts"] = attempt_payloads
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    reserved_set = set(reserved)
    if cloud_store is None:
        record_reservation_consumption(
            [
                (ref_id, notional, is_entry)
                for ref_id, notional, is_entry, _ in reservations
                if ref_id in reserved_set
            ],
            root=getattr(args, "root", "."),
            day=trade_date,
        )
    print(json.dumps(payload))
    return 0 if reserved else 2


def _json_items(path: str, key: str) -> list[dict[str, object]]:
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        return raw
    values = raw.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{path} must be a list or contain a '{key}' list")
    return values


def _verify_registered_sources(evidence: list[EvidenceVersion]) -> None:
    registry = SourceRegistry.default()
    failures: list[str] = []
    for item in evidence:
        check = registry.check(item)
        if not check.accepted:
            failures.append(f"{item.evidence_id}:{check.reason}")
        elif item.issuer_verified != check.issuer_verified:
            failures.append(f"{item.evidence_id}:stored_source_verification_mismatch")
    if failures:
        raise ValueError(f"Evidence failed registered-source verification: {failures}")


# Backward-compatible private name used by older callers and tests. It now
# performs offline issuer-domain/exchange verification and never contacts SEC.
_verify_official_issuer_mappings = _verify_registered_sources


def command_picker_validate(args: argparse.Namespace) -> int:
    """Validate an AI draft and emit an immutable live DecisionPacket."""
    draft = PickerDraft.from_dict(json.loads(Path(args.draft).read_text()))
    critic = CriticVerdict.from_dict(json.loads(Path(args.critic).read_text()))
    evidence = [EvidenceVersion.from_dict(item) for item in _json_items(args.evidence, "evidence")]
    _verify_official_issuer_mappings(evidence)
    evidence_by_id = {item.evidence_id: item for item in evidence}
    quant_raw = json.loads(Path(args.quant).read_text())
    if isinstance(quant_raw, dict) and "snapshots" in quant_raw:
        quant_values = quant_raw["snapshots"]
    elif isinstance(quant_raw, list):
        quant_values = quant_raw
    else:
        quant_values = [quant_raw]
    snapshots = {
        snapshot.symbol: snapshot
        for snapshot in (QuantSnapshot.from_dict(item) for item in quant_values)
    }
    prompt_hash = (
        content_hash(Path(args.prompt_file).read_text())
        if args.prompt_file
        else str(args.prompt_hash)
    )
    if not prompt_hash or prompt_hash == "None":
        raise ValueError("Provide --prompt-file or --prompt-hash")
    result = validate_picker_draft(
        draft,
        evidence_by_id,
        snapshots.get(draft.symbol),
        critic,
        prompt_hash=prompt_hash,
        model_id=args.model_id,
    )
    payload = result.to_dict()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")

    if result.accepted and args.persist:
        account_number = os.environ.get("AGENTIC_TRADER_ACCOUNT", "")
        if not account_number:
            raise ValueError("AGENTIC_TRADER_ACCOUNT is required to persist a live packet")
        ledger = PostgresLedger.from_env()
        ledger.put_run(
            draft.run_id,
            account_key(account_number),
            draft.created_at,
            draft.created_at.date(),
            args.model_id,
            prompt_hash,
            metadata={"schema": "ai_picker_v1_unvalidated"},
        )
        for item in evidence:
            ledger.put_evidence(item)
        ledger.put_draft(draft)
        ledger.put_critic(critic)
        assert result.packet is not None
        ledger.authorize_packet(result.packet)

    print(json.dumps(payload, indent=2))
    return 0 if result.accepted else 2


def command_picker_stage(args: argparse.Namespace) -> int:
    """Validate research schemas and stage an immutable batch without broker access."""
    payload = json.loads(Path(args.bundle).read_text())
    created_at = datetime.fromisoformat(
        str(payload["created_at"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    as_of = date.fromisoformat(str(payload["as_of"]))
    evidence = [EvidenceVersion.from_dict(item) for item in payload["evidence"]]
    drafts = [PickerDraft.from_dict(item) for item in payload["drafts"]]
    option_drafts = [OptionDraft.from_dict(item) for item in payload.get("option_drafts", [])]
    all_draft_ids = [item.draft_id for item in [*drafts, *option_drafts]]
    if len(set(all_draft_ids)) != len(all_draft_ids):
        raise ValueError("Every staged draft_id must be globally unique")
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise ValueError("Every staged evidence_id must be unique")
    critics = [CriticVerdict.from_dict(item) for item in payload["critics"]]
    critic_ids = {item.draft_id for item in critics}
    required_critic_ids = {item.source_draft_id or item.draft_id for item in option_drafts} | {
        item.draft_id for item in drafts
    }
    if required_critic_ids != critic_ids:
        raise ValueError("Every staged draft requires exactly one critic verdict")
    if any(item.created_at.date() != as_of for item in drafts):
        raise ValueError("Every draft must be created on the batch as_of date")
    if any(item.created_at.date() != as_of for item in option_drafts):
        raise ValueError("Every option draft must be created on the batch as_of date")
    prompt_hash = str(payload["prompt_hash"])
    if len(prompt_hash) != 64:
        raise ValueError("prompt_hash must be a SHA-256 digest")
    model_id = str(payload["model_id"])
    batch_id = str(payload["batch_id"])
    ledger = PostgresLedger.from_env()
    run_ids = {item.run_id for item in [*drafts, *option_drafts]}
    if len(run_ids) != 1:
        raise ValueError("A research batch must contain exactly one run_id")
    run_id = next(iter(run_ids))
    ledger.put_run(
        run_id,
        content_hash("ai-picker-research"),
        created_at,
        as_of,
        model_id,
        prompt_hash,
        metadata={"batch_id": batch_id, "schema": "ai_picker_v1_unvalidated"},
    )
    for item in evidence:
        ledger.put_evidence(item)
    for item in drafts:
        ledger.put_draft(item)
    stock_draft_ids = {item.draft_id for item in drafts}
    for item in critics:
        # The normalized critic table currently references stock drafts. Option
        # critics remain immutable in the staged payload and inherit the stock
        # critic when source_draft_id is set.
        if item.draft_id in stock_draft_ids:
            ledger.put_critic(item)
    ledger.stage_batch(batch_id, as_of, created_at, prompt_hash, model_id, payload)
    print(
        json.dumps(
            {
                "staged": True,
                "batch_id": batch_id,
                "drafts": len(drafts),
                "option_drafts": len(option_drafts),
            }
        )
    )
    return 0


def _validate_pending_research_payload(
    payload: dict[str, object],
) -> tuple[datetime, date, list[EvidenceVersion], list[PickerDraft], list[OptionDraft]]:
    created_at = datetime.fromisoformat(
        str(payload["created_at"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    as_of = date.fromisoformat(str(payload["as_of"]))
    evidence = [EvidenceVersion.from_dict(item) for item in payload["evidence"]]
    drafts = [PickerDraft.from_dict(item) for item in payload["drafts"]]
    option_drafts = [OptionDraft.from_dict(item) for item in payload.get("option_drafts", [])]
    all_drafts = [*drafts, *option_drafts]
    if not all_drafts or len({item.run_id for item in all_drafts}) != 1:
        raise ValueError("A pending research batch requires exactly one run_id")
    draft_ids = [item.draft_id for item in all_drafts]
    if len(set(draft_ids)) != len(draft_ids):
        raise ValueError("Pending research draft IDs must be globally unique")
    evidence_ids = [item.evidence_id for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("Pending evidence IDs must be unique")
    if any(item.created_at.date() != as_of for item in all_drafts):
        raise ValueError("Every pending draft must be created on the as_of date")
    if len(str(payload["prompt_hash"])) != 64:
        raise ValueError("prompt_hash must be a SHA-256 digest")
    if not str(payload.get("cycle_id", "")).strip():
        raise ValueError("Pending research bundle requires cycle_id")
    return created_at, as_of, evidence, drafts, option_drafts


def _pending_draft_hashes(
    drafts: list[PickerDraft],
    option_drafts: list[OptionDraft],
) -> dict[str, str]:
    return {
        item.draft_id: content_hash(canonical_json(item.to_dict()))
        for item in [*drafts, *option_drafts]
    }


def command_picker_stage_pending(args: argparse.Namespace) -> int:
    """Stage analyst output for a separate independent critic automation."""
    payload = json.loads(Path(args.bundle).read_text())
    created_at, as_of, _, drafts, option_drafts = _validate_pending_research_payload(payload)
    batch_id = str(payload["batch_id"])
    ledger = PostgresLedger.from_env()
    ledger.stage_pending_batch(
        batch_id,
        as_of,
        created_at,
        str(payload["prompt_hash"]),
        str(payload["model_id"]),
        payload,
    )
    ledger.bind_research_cycle(str(payload["cycle_id"]), batch_id)
    result = {
        "pending": True,
        "batch_id": batch_id,
        "drafts": len(drafts),
        "option_drafts": len(option_drafts),
    }
    print(json.dumps(result))
    return 0


def command_picker_export_pending(args: argparse.Namespace) -> int:
    """Export today's latest pending batch for independent criticism."""
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC).date()
    batch = PostgresLedger.from_env().latest_pending_batch(as_of)
    if batch is None:
        print(json.dumps({"exported": False, "reason": "no_pending_batch"}))
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(batch["payload"])
    _, _, _, drafts, option_drafts = _validate_pending_research_payload(payload)
    payload["_critic_binding"] = {
        "batch_id": batch["batch_id"],
        "prompt_hash": batch["prompt_hash"],
        "analyst_model_id": batch["analyst_model_id"],
        "draft_hashes": _pending_draft_hashes(drafts, option_drafts),
        "payload_hash": content_hash(canonical_json(batch["payload"])),
    }
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"exported": True, "batch_id": batch["batch_id"]}))
    return 0


def command_picker_cycle_start(args: argparse.Namespace) -> int:
    """Create a durable marker before an analyst cycle begins."""
    now = datetime.now(UTC)
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    PostgresLedger.from_env().start_research_cycle(args.cycle_id, as_of, now)
    print(json.dumps({"started": True, "cycle_id": args.cycle_id, "as_of": str(as_of)}))
    return 0


def command_picker_cycle_fail(args: argparse.Namespace) -> int:
    """Explicitly release a failed analyst/critic cycle marker."""
    PostgresLedger.from_env().finish_research_cycle(args.cycle_id, "failed")
    print(json.dumps({"failed": True, "cycle_id": args.cycle_id}))
    return 0


def command_picker_finalize_pending(args: argparse.Namespace) -> int:
    """Attach independent critic verdicts and promote a pending batch to staged."""
    now = datetime.now(UTC)
    ledger = PostgresLedger.from_env()
    critic_payload = json.loads(Path(args.critics).read_text())
    binding = critic_payload.get("_critic_binding")
    if not isinstance(binding, dict) or not binding.get("batch_id"):
        raise ValueError("Critic output must bind an exact pending batch")
    pending = ledger.pending_batch(str(binding["batch_id"]))
    if pending is None:
        print(json.dumps({"finalized": False, "reason": "no_pending_batch"}))
        return 2
    if pending["status"] != "pending":
        raise ValueError("Critic batch is no longer pending")
    payload = dict(pending["payload"])
    created_at, as_of, evidence, drafts, option_drafts = _validate_pending_research_payload(payload)
    if args.as_of and date.fromisoformat(args.as_of) != as_of:
        raise ValueError("Critic as_of does not match the bound batch")
    expected_binding = {
        "batch_id": pending["batch_id"],
        "prompt_hash": pending["prompt_hash"],
        "analyst_model_id": pending["analyst_model_id"],
        "draft_hashes": _pending_draft_hashes(drafts, option_drafts),
        "payload_hash": content_hash(canonical_json(pending["payload"])),
    }
    if binding != expected_binding:
        raise ValueError("Critic output binding does not match pending batch content")
    critics = [CriticVerdict.from_dict(item) for item in critic_payload.get("critics", [])]
    critic_ids = {item.draft_id for item in critics}
    required_ids = {item.draft_id for item in drafts} | {
        item.source_draft_id or item.draft_id for item in option_drafts
    }
    if critic_ids != required_ids:
        raise ValueError("Independent critics must cover every required draft exactly")
    analyst_model_id = str(pending["analyst_model_id"]).strip()
    allowed_critic_models = {item.casefold() for item in ALLOWED_CRITIC_MODELS}
    if any(
        item.model_id.strip().casefold() not in allowed_critic_models
        or item.model_id.strip().casefold() == analyst_model_id.casefold()
        for item in critics
    ):
        raise ValueError("Every critic must record an approved independent critic model ID")
    if any(set(dict(item.soft_checks)) != CRITIC_SOFT_DIMENSIONS for item in critics):
        raise ValueError("Every critic must provide all five structured soft checks")
    payload["critics"] = [item.to_dict() for item in critics]
    run_id = next(iter({item.run_id for item in [*drafts, *option_drafts]}))
    ledger.put_run(
        run_id,
        content_hash("ai-picker-research"),
        created_at,
        as_of,
        analyst_model_id,
        str(pending["prompt_hash"]),
        metadata={
            "batch_id": pending["batch_id"],
            "schema": "ai_picker_v1_unvalidated",
            "critic": "independent_model",
        },
    )
    for item in evidence:
        ledger.put_evidence(item)
    for item in drafts:
        ledger.put_draft(item)
    stock_draft_ids = {item.draft_id for item in drafts}
    for item in critics:
        if item.draft_id in stock_draft_ids:
            ledger.put_critic(item)
    ledger.stage_batch(
        str(pending["batch_id"]),
        as_of,
        created_at,
        str(pending["prompt_hash"]),
        analyst_model_id,
        payload,
    )
    ledger.finalize_pending_batch(str(pending["batch_id"]), "finalized", now)
    ledger.finish_research_cycle(str(payload["cycle_id"]), "finalized")
    result = {
        "finalized": True,
        "batch_id": pending["batch_id"],
        "critics": len(critics),
    }
    print(json.dumps(result))
    return 0


def command_picker_verify_evidence(args: argparse.Namespace) -> int:
    """Ground every evidence quote in a saved source document and hash it."""
    raw_items = _json_items(args.evidence, "evidence")
    documents = Path(args.documents)
    registry = SourceRegistry.default()
    verified = []
    failures = []
    for raw in raw_items:
        evidence_id = str(raw["evidence_id"])
        path = documents / f"{evidence_id}.txt"
        if not path.exists():
            failures.append({"evidence_id": evidence_id, "reason": "document_missing"})
            continue
        document = path.read_text(errors="replace")
        grounded = quote_is_grounded(document, str(raw["quote"]))
        if not grounded:
            failures.append({"evidence_id": evidence_id, "reason": "quote_not_grounded"})
            continue
        candidate = EvidenceVersion.from_dict(
            {
                **raw,
                "document_hash": content_hash(document),
                "quote_verified": True,
                "issuer_verified": False,
            }
        )
        check = registry.check(candidate)
        if not check.accepted:
            failures.append(
                {
                    "evidence_id": evidence_id,
                    "reason": check.reason,
                }
            )
            continue
        verified.append(
            EvidenceVersion.from_dict(
                {**candidate.to_dict(), "issuer_verified": check.issuer_verified}
            ).to_dict()
        )
    payload = {"evidence": verified, "failures": failures}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0 if not failures else 2


def command_picker_build_quant(args: argparse.Namespace) -> int:
    """Compute live ranks in code from a frozen raw market/fundamental snapshot."""
    raw = json.loads(Path(args.input).read_text())
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError("Quant input must be a non-empty list or contain a rows list")
    as_of_raw = args.as_of or (raw.get("as_of") if isinstance(raw, dict) else None)
    if not as_of_raw:
        raise ValueError("Quant input requires an as_of timestamp")
    as_of = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ValueError("Quant as_of must include a timezone")
    as_of = as_of.astimezone(UTC)
    ranked = rank_candidates(liquid_universe(pd.DataFrame(rows)))
    snapshots = snapshots_from_ranked(ranked, as_of)
    payload = {
        "as_of": as_of.isoformat(),
        "raw_snapshot_hash": content_hash(canonical_json({"as_of": as_of, "rows": rows})),
        "snapshots": [snapshots[symbol].to_dict() for symbol in sorted(snapshots)],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    print(json.dumps(payload, indent=2, allow_nan=False))
    return 0


def command_picker_record_close(args: argparse.Namespace) -> int:
    """Persist one official-close equity anchor per NYSE session."""
    now = datetime.now(UTC)
    session_date = (
        date.fromisoformat(args.session_date)
        if args.session_date
        else now.astimezone(ZoneInfo("America/New_York")).date()
    )
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=session_date,
        end_date=session_date,
    )
    if schedule.empty:
        print(json.dumps({"recorded": False, "reason": "not_a_market_session"}))
        return 2
    official_close = schedule.iloc[0]["market_close"].to_pydatetime().astimezone(UTC)
    if now < official_close:
        print(json.dumps({"recorded": False, "reason": "official_close_not_reached"}))
        return 2
    payload = {
        "recorded": False,
        "reason": "official_close_equity_source_unavailable",
        "session_date": session_date.isoformat(),
        "required_next_step": "implement_a_broker_verified_close_time_collector",
    }
    print(json.dumps(payload))
    return 2


def command_picker_authorize_batch(args: argparse.Namespace) -> int:
    """Authorize today's latest staged batch using fresh execution-side quant data."""
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC).date()
    ledger = PostgresLedger.from_env()
    batch = ledger.latest_staged_batch(as_of)
    pending = ledger.latest_pending_batch(as_of)
    if pending is not None and (batch is None or pending["created_at"] >= batch["created_at"]):
        print(
            json.dumps(
                {
                    "authorized": [],
                    "reason": "newer_pending_batch_not_finalized",
                    "pending_batch_id": pending["batch_id"],
                }
            )
        )
        return 2
    if batch is None:
        print(json.dumps({"authorized": [], "reason": "no_staged_batch"}))
        return 2
    payload = batch["payload"]
    evidence_items = [EvidenceVersion.from_dict(raw) for raw in payload["evidence"]]
    _verify_official_issuer_mappings(evidence_items)
    evidence = {item.evidence_id: item for item in evidence_items}
    critics = {
        item.draft_id: item for item in (CriticVerdict.from_dict(raw) for raw in payload["critics"])
    }
    quant_raw = json.loads(Path(args.quant).read_text())
    if isinstance(quant_raw, dict):
        quant_values = quant_raw.get("snapshots", quant_raw)
    else:
        quant_values = quant_raw
    if isinstance(quant_values, dict):
        quant_values = [quant_values]
    snapshots = {
        item.symbol: item for item in (QuantSnapshot.from_dict(raw) for raw in quant_values)
    }
    results = []
    accepted_packets: list[DecisionPacket] = []
    for raw in payload["drafts"]:
        draft = PickerDraft.from_dict(raw)
        critic = critics.get(draft.draft_id)
        if critic is None:
            raise ValueError(f"Missing critic verdict for {draft.draft_id}")
        result = validate_picker_draft(
            draft,
            evidence,
            snapshots.get(draft.symbol),
            critic,
            prompt_hash=str(batch["prompt_hash"]),
            model_id=str(batch["model_id"]),
        )
        results.append({"draft_id": draft.draft_id, **result.to_dict()})
        if result.packet is not None:
            ledger.authorize_packet(result.packet)
            accepted_packets.append(result.packet)
    ledger.set_batch_status(
        str(batch["batch_id"]), "authorized" if accepted_packets else "rejected"
    )
    output_payload = {
        "batch_id": batch["batch_id"],
        "authorized": [packet.to_dict() for packet in accepted_packets],
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_payload, indent=2) + "\n")
    print(json.dumps(output_payload, indent=2))
    return 0 if accepted_packets else 2


def command_picker_plan(args: argparse.Namespace) -> int:
    """Build a stock-picker target request from authorized packets and broker state."""
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC).date()
    now = datetime.now(UTC)
    if args.packets:
        packets = [DecisionPacket.from_dict(item) for item in _json_items(args.packets, "packets")]
    else:
        packets = PostgresLedger.from_env().authorized_packets(as_of, now)
    if args.theses:
        theses = [ActiveThesis.from_dict(item) for item in _json_items(args.theses, "theses")]
    else:
        theses = PostgresLedger.from_env().active_theses()

    snapshot = json.loads(Path(args.snapshot).read_text())
    account = snapshot["account"]
    prices = {str(k).upper(): float(v) for k, v in snapshot["prices"].items()}
    if "SPY" not in prices:
        raise ValueError("Picker planning requires a current SPY price for relative stops")
    plan = build_picker_portfolio(
        packets,
        theses,
        prices,
        spy_price=prices["SPY"],
        as_of=as_of,
        now=now,
    )

    packet_by_symbol = {
        packet.symbol: packet for packet in packets if packet.packet_id in plan.accepted_packet_ids
    }
    close_packet_by_symbol = {
        packet.symbol: packet
        for packet in packets
        if packet.action == "close"
        and packet.verify_hash()
        and packet.valid_for_date == as_of
        and packet.expires_at > now
    }
    thesis_by_symbol = {thesis.symbol: thesis for thesis in theses}
    metadata: dict[str, dict[str, str | None]] = {}
    for symbol in plan.authorized_buy_symbols:
        packet = packet_by_symbol.get(symbol)
        thesis = thesis_by_symbol.get(symbol)
        existing_active = thesis is not None and thesis.status in {
            "pending_entry",
            "active",
        }
        metadata[symbol] = {
            "pick_id": (
                thesis.pick_id
                if existing_active
                else packet.packet_id
                if packet is not None
                else thesis.pick_id
                if thesis
                else ""
            ),
            "intent_class": "rebalance" if existing_active else "entry",
            "exit_reason": None,
        }
    for exit_intent in plan.exits:
        metadata[exit_intent.symbol] = {
            "pick_id": exit_intent.pick_id,
            "intent_class": "mandatory_exit",
            "exit_reason": exit_intent.reason,
        }

    broker_positions = account.get("broker_positions", [])
    if not isinstance(broker_positions, list):
        raise ValueError("Picker snapshot requires native broker_positions")
    held_symbols = {str(item["symbol"]).upper() for item in broker_positions}
    legacy_closes = sorted(held_symbols & set(close_packet_by_symbol))
    for symbol in legacy_closes:
        packet = close_packet_by_symbol[symbol]
        metadata[symbol] = {
            "pick_id": packet.packet_id,
            "intent_class": "mandatory_exit",
            "exit_reason": "authorized_close_packet_for_legacy_position",
        }
    managed_symbols = (
        set(plan.authorized_buy_symbols) | set(plan.authorized_sell_symbols) | set(legacy_closes)
    )
    unmanaged_symbols = sorted(held_symbols - managed_symbols)
    targets = dict(plan.targets)
    if unmanaged_symbols:
        equity = float(account["equity"])
        if not isfinite(equity) or equity <= 0:
            raise ValueError("Picker snapshot equity must be finite and positive")
        position_values = broker_position_values(broker_positions, prices)
        for symbol in unmanaged_symbols:
            # Legacy/manual holdings are outside the picker's lifecycle. Keep
            # their current exposure unchanged until the operator explicitly
            # imports or closes them; omission must never imply liquidation.
            targets[symbol] = position_values[symbol] / equity
    for symbol in legacy_closes:
        targets[symbol] = 0.0
    authorization_packet_ids = sorted(
        set(plan.accepted_packet_ids)
        | {close_packet_by_symbol[symbol].packet_id for symbol in legacy_closes}
    )
    request = {
        **snapshot,
        "picker_mode": True,
        "targets": targets,
        "buy_symbol_allowlist": list(plan.authorized_buy_symbols),
        "sell_symbol_allowlist": sorted(set(plan.authorized_sell_symbols) | held_symbols),
        "metadata_by_symbol": metadata,
        "authorization_packet_ids": authorization_packet_ids,
        "max_position_weight": 0.035,
        "picker_plan": plan.to_dict(),
        "unmanaged_positions": unmanaged_symbols,
        "legacy_position_closes": legacy_closes,
        "research_batch_id": str(snapshot.get("research_batch_id", "")),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(request, indent=2) + "\n")
    print(json.dumps({"output": str(output), **plan.to_dict()}, indent=2))
    return 0


def _picker_fill_fingerprint(fill: dict[str, object]) -> dict[str, object]:
    def number(name: str) -> float:
        value = fill.get(name)
        if isinstance(value, dict):
            value = value.get("amount")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Filled broker order has invalid {name}") from error
        if not isfinite(parsed) or parsed <= 0:
            raise ValueError(f"Filled broker order has invalid {name}")
        return parsed

    raw_timestamp = (
        fill.get("last_transaction_at") or fill.get("executed_at") or fill.get("updated_at")
    )
    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("Filled broker order has an invalid execution timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError("Filled broker order has an invalid execution timestamp")
    broker_order_id = str(fill.get("order_id") or fill.get("id") or "").strip()
    symbol = str(fill.get("symbol") or "").strip().upper()
    side = str(fill.get("side") or "").strip().lower()
    if not broker_order_id or not symbol or side not in {"buy", "sell"}:
        raise ValueError("Filled broker order identity is incomplete")
    quantity_key = (
        "cumulative_quantity" if fill.get("cumulative_quantity") is not None else "quantity"
    )
    return {
        "broker_order_id": broker_order_id,
        "symbol": symbol,
        "side": side,
        "cumulative_quantity": number(quantity_key),
        "average_price": number("average_price"),
        "executed_at": timestamp.astimezone(UTC).isoformat(),
    }


def command_picker_sync(args: argparse.Namespace) -> int:
    """Persist pick lifecycle transitions only after broker fills reconcile cleanly."""
    plan_id = str(getattr(args, "plan_id", "") or "").strip()
    if plan_id:
        cloud_store = PostgresCloudRuntimeStore.from_env()
        plan = _cloud_plan_document(cloud_store.get_plan(plan_id))
        durable_reconciliation = cloud_store.latest_reconciliation(plan_id)
        if durable_reconciliation is None:
            raise ValueError("No durable reconciliation exists for the execution plan")
        reconciliation = durable_reconciliation.payload
    else:
        plan = json.loads(Path(args.plan).read_text())
        reconciliation = json.loads(Path(args.reconciliation).read_text())
    if not bool(reconciliation.get("clean")):
        raise ValueError("Cannot sync picker state from a non-clean reconciliation")
    executed_raw = json.loads(Path(args.executed).read_text())
    executed = executed_raw.get("orders", []) if isinstance(executed_raw, dict) else executed_raw
    matched_ref_ids = {str(item.get("ref_id") or "") for item in reconciliation.get("matched", [])}
    if "" in matched_ref_ids:
        raise ValueError("Clean reconciliation contains a matched fill without ref_id")
    filled: dict[str, dict[str, object]] = {}
    matched_by_broker_id = {
        str(item.get("order_id") or ""): str(item.get("ref_id") or "")
        for item in reconciliation.get("matched", [])
        if str(item.get("order_id") or "") and str(item.get("ref_id") or "")
    }
    for item in executed:
        if str(item.get("state", "")).lower() != "filled":
            continue
        broker_order_id = str(item.get("order_id") or item.get("id") or "")
        supplied_ref = str(item.get("ref_id") or item.get("client_order_id") or "")
        bound_ref = matched_by_broker_id.get(broker_order_id, "")
        if supplied_ref and bound_ref and supplied_ref != bound_ref:
            raise ValueError("Filled broker row ref differs from durable reconciliation")
        ref_id = supplied_ref or bound_ref
        if not ref_id:
            raise ValueError("Filled equity order lacks a durable broker-order binding")
        if ref_id in filled:
            raise ValueError(f"Duplicate terminal fill for ref_id {ref_id}")
        filled[ref_id] = {**item, "ref_id": ref_id}
    if not matched_ref_ids.issubset(filled):
        raise ValueError("Reconciliation references a terminal fill that is unavailable")
    ledger = PostgresLedger.from_env()
    packet_dates = plan.get("packet_trade_dates")
    if not isinstance(packet_dates, dict):
        raise ValueError("Picker plan is missing immutable packet trade dates")
    entry_packet_ids = {
        str(order.get("pick_id") or "")
        for order in plan.get("approved_orders", [])
        if isinstance(order, dict) and str(order.get("intent_class") or "") == "entry"
    }
    packets: dict[str, DecisionPacket] = {}
    for packet_id in entry_packet_ids:
        packet = ledger.packet(packet_id)
        if packet is None or packet.valid_for_date.isoformat() != str(packet_dates.get(packet_id)):
            raise ValueError(f"Entry fill references unavailable durable packet {packet_id}")
        packets[packet_id] = packet
    active = {thesis.pick_id: thesis for thesis in ledger.active_theses()}
    spy_price = float(plan["prices"]["SPY"])
    sync_account_key = str(plan.get("account_key") or "")
    if not sync_account_key and plan.get("account_number"):
        sync_account_key = account_key(str(plan["account_number"]))
    if not sync_account_key:
        raise ValueError("Picker sync requires the durable account identity")
    legacy_closes = {str(symbol).upper() for symbol in plan.get("legacy_position_closes", [])}
    transitions: list[dict[str, str]] = []
    finalized_events: dict[str, tuple[str, date]] = {}

    for order in plan.get("approved_orders", []):
        pick_id = str(order.get("pick_id") or "")
        intent = str(order.get("intent_class") or "")
        ref_id = str(order.get("ref_id") or "")
        fill = filled.get(ref_id) if ref_id in matched_ref_ids else None
        if fill is None:
            continue
        if (
            str(fill.get("symbol", "")).upper() != str(order["symbol"]).upper()
            or str(fill.get("side", "")).lower() != str(order["side"]).lower()
        ):
            raise ValueError(f"Fill fingerprint differs from plan for ref_id {ref_id}")
        try:
            fill_fingerprint = _picker_fill_fingerprint(fill)
        except ValueError as error:
            raise ValueError(f"Fill fingerprint is invalid for ref_id {ref_id}") from error
        average_price = float(fill_fingerprint["average_price"])
        broker_order_id = str(fill_fingerprint["broker_order_id"])
        fill_timestamp = datetime.fromisoformat(str(fill_fingerprint["executed_at"]))
        fill_session_date = _nyse_session_date(fill_timestamp)
        fill_fingerprint_hash = canonical_hash(fill_fingerprint)
        event_payload = {
            "plan_order": order,
            "fill_fingerprint": fill_fingerprint,
            "fill_fingerprint_hash": fill_fingerprint_hash,
            "broker_fill": fill,
        }
        expected_event_type = (
            "entry_filled"
            if intent == "entry"
            else "exit_filled"
            if intent == "mandatory_exit"
            else ""
        )
        existing_event = (
            ledger.equity_order_event(ref_id, expected_event_type) if expected_event_type else None
        )
        if existing_event is not None:
            stored_payload = existing_event.get("payload")
            if (
                existing_event.get("broker_order_id") != broker_order_id
                or existing_event.get("account_key") != sync_account_key
                or str(existing_event.get("symbol") or "").upper() != str(order["symbol"]).upper()
                or existing_event.get("session_date") != fill_session_date
                or not isinstance(stored_payload, dict)
                or stored_payload.get("plan_order") != order
                or stored_payload.get("fill_fingerprint_hash") != fill_fingerprint_hash
            ):
                raise ValueError(f"Existing picker event differs for ref_id {ref_id}")
            finalized_events[ref_id] = (expected_event_type, fill_session_date)
            transitions.append({"pick_id": pick_id, "status": "already_synced"})
            continue
        if intent == "entry":
            if not pick_id:
                raise ValueError(f"Entry fill is missing a durable pick ID for ref_id {ref_id}")
            packet = packets.get(pick_id)
            if packet is None:
                raise ValueError(f"Entry fill references unavailable packet {pick_id}")
            thesis = ActiveThesis(
                pick_id=packet.packet_id,
                packet_id=packet.packet_id,
                symbol=packet.symbol,
                sector=packet.sector,
                status="active",
                entry_date=packet.valid_for_date,
                expiry_date=trading_day_expiry(packet.valid_for_date, packet.horizon_trading_days),
                entry_price=average_price,
                entry_spy_price=spy_price,
                target_weight=packet.target_weight,
                stop_loss_pct=packet.stop_loss_pct,
                sector_relative_stop_pct=packet.sector_relative_stop_pct,
            )
            ledger.upsert_thesis(thesis)
            ledger.append_equity_order_event(
                "entry_filled",
                fill_timestamp.astimezone(UTC),
                event_payload,
                pick_id=pick_id,
                packet_id=packet.packet_id,
                ref_id=ref_id,
                broker_order_id=broker_order_id,
                account_hash=sync_account_key,
                symbol=str(order["symbol"]),
                session_date=fill_session_date,
            )
            transitions.append({"pick_id": pick_id, "status": "active"})
            finalized_events[ref_id] = ("entry_filled", fill_session_date)
        elif intent == "mandatory_exit":
            thesis = active.get(pick_id)
            if thesis is None:
                symbol = str(order["symbol"]).upper()
                stored_thesis = ledger.thesis(pick_id) if pick_id else None
                recoverable_closed_thesis = (
                    bool(plan_id)
                    and bool(ref_id)
                    and bool(broker_order_id)
                    and stored_thesis is not None
                    and stored_thesis.status == "closed"
                    and stored_thesis.pick_id == pick_id
                    and stored_thesis.packet_id == pick_id
                    and stored_thesis.symbol == symbol
                )
                if recoverable_closed_thesis:
                    # The durable plan/ref and exact broker-fill fingerprint above
                    # prove this is recovery from a crash after the thesis close
                    # committed but before its same-session exit event did.
                    event_pick_id = pick_id
                    event_packet_id = stored_thesis.packet_id
                    transition = {"pick_id": pick_id, "status": "closed_event_recovered"}
                elif symbol in legacy_closes:
                    event_pick_id = None
                    event_packet_id = pick_id or None
                    transition = {"pick_id": pick_id, "status": "legacy_closed"}
                else:
                    raise ValueError(f"Exit fill references unknown active thesis {pick_id}")
            else:
                ledger.upsert_thesis(replace(thesis, status="closed"))
                event_pick_id = pick_id
                event_packet_id = thesis.packet_id
                transition = {"pick_id": pick_id, "status": "closed"}
            ledger.append_equity_order_event(
                "exit_filled",
                fill_timestamp.astimezone(UTC),
                event_payload,
                pick_id=event_pick_id,
                packet_id=event_packet_id,
                ref_id=ref_id,
                broker_order_id=broker_order_id,
                account_hash=sync_account_key,
                symbol=str(order["symbol"]),
                session_date=fill_session_date,
            )
            transitions.append(transition)
            finalized_events[ref_id] = ("exit_filled", fill_session_date)

    if plan_id:
        pending_attempts = [
            attempt
            for attempt in cloud_store.nonterminal_attempts(sync_account_key)
            if attempt.plan_id == plan_id and attempt.state == "filled"
        ]
        expected_filled_refs = set(matched_ref_ids)
        if not {attempt.ref_id for attempt in pending_attempts}.issubset(expected_filled_refs):
            raise RuntimeError("Filled attempts remain inconsistent with durable picker events")
        for attempt in pending_attempts:
            event = finalized_events.get(attempt.ref_id)
            if event is None:
                raise RuntimeError("Filled attempt has no synchronized picker lifecycle event")
            cloud_store.finalize_filled_attempt_after_picker_sync(
                attempt.attempt_id,
                event_type=event[0],
                session_date=event[1],
            )

    payload = {"synced": True, "transitions": transitions}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


def command_learning_freeze(args: argparse.Namespace) -> int:
    """Validate and atomically persist one complete four-arm prediction batch."""
    raw = json.loads(Path(args.batch).read_text())
    batch_raw = raw.get("batch", raw) if isinstance(raw, dict) else raw
    if not isinstance(batch_raw, dict):
        raise ValueError("Learning batch must be a JSON object")
    batch = prediction_batch_from_dict(batch_raw)
    PostgresLearningStore.from_env().record_batch(batch)
    payload = {
        "frozen": True,
        "batch_id": batch.batch_id,
        "batch_hash": batch.batch_hash,
        "candidate_count": len(batch.expected_candidate_ids),
        "prediction_count": len(batch.predictions),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


def command_learning_build_batch(args: argparse.Namespace) -> int:
    """Construct complete factor/LLM/hybrid/control arms deterministically."""
    quant_raw = json.loads(Path(args.quant).read_text())
    quant_rows = quant_raw.get("snapshots", quant_raw)
    if not isinstance(quant_rows, list):
        raise ValueError("Learning quant file must contain a snapshots list")
    research = json.loads(Path(args.research).read_text())
    if not isinstance(research, dict):
        raise ValueError("Learning research file must be a JSON object")
    batch = build_shadow_batch(
        [QuantSnapshot.from_dict(item) for item in quant_rows],
        research,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(batch.to_dict(), indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "batch_id": batch.batch_id,
                "candidate_count": len(batch.expected_candidate_ids),
                "prediction_count": len(batch.predictions),
            },
            indent=2,
        )
    )
    return 0


def command_learning_mark(args: argparse.Namespace) -> int:
    """Append only newly matured, availability-aware forward outcomes."""
    raw = json.loads(Path(args.closes).read_text())
    close_rows = raw.get("closes", raw) if isinstance(raw, dict) else raw
    if not isinstance(close_rows, list):
        raise ValueError("Learning closes must be a JSON list or a closes object")
    closes = tuple(market_close_from_dict(item) for item in close_rows)
    as_of = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of
        else datetime.now(UTC)
    )
    store = PostgresLearningStore.from_env()
    batches = store.load_batches()
    if batches and closes:
        first_decision = min(batch.decision_date for batch in batches)
        last_close = max(item.session_date for item in closes)
        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=first_decision,
            end_date=last_close,
        )
        expected_sessions = {item.date() for item in schedule.index if item.date() > first_decision}
        provided_sessions = {item.session_date for item in closes}
        missing_sessions = sorted(expected_sessions - provided_sessions)
        if missing_sessions:
            raise ValueError(
                "Learning closes omit exchange sessions and would shift forward "
                f"horizons: {[item.isoformat() for item in missing_sessions]}"
            )
    existing = store.existing_outcome_keys()
    outcomes = [
        outcome
        for batch in batches
        for outcome in mark_available_outcomes(
            batch,
            closes,
            as_of,
            cost_bps=args.cost_bps,
        )
        if (outcome.prediction_id, outcome.horizon_sessions) not in existing
    ]
    store.record_outcomes(outcomes)
    payload = {
        "recorded": len(outcomes),
        "as_of": as_of.astimezone(UTC).isoformat(),
        "outcomes": [item.to_dict() for item in outcomes],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({**payload, "outcomes": f"{len(outcomes)} written"}, indent=2))
    return 0


def command_learning_report(args: argparse.Namespace) -> int:
    """Evaluate all experiment arms without changing the deployment state."""
    store = PostgresLearningStore.from_env()
    batches = store.load_batches()
    if not batches:
        payload = {
            "passed": False,
            "current_state": store.current_state(),
            "reason": "no_frozen_learning_batches",
        }
        print(json.dumps(payload, indent=2))
        return 2
    as_of = datetime.now(UTC)
    policy = PromotionPolicy(horizon_sessions=args.horizon_sessions)
    report = build_promotion_report(batches, store.load_outcomes(), as_of, policy)
    store.record_report(report)
    payload = {"current_state": store.current_state(), "report": report.to_dict()}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        json.dumps(
            {
                "current_state": payload["current_state"],
                "passed": report.passed,
                "report_hash": report.report_hash,
                "failed_gates": [name for name, passed in report.gates.items() if not passed],
            },
            indent=2,
        )
    )
    return 0 if report.passed else 2


def command_learning_status(args: argparse.Namespace) -> int:
    """Describe the shadow universe needed to collect forward close marks."""
    store = PostgresLearningStore.from_env()
    batches = store.load_batches()
    outcomes = store.load_outcomes()
    predictions = [item for batch in batches for item in batch.predictions]
    payload = {
        "current_state": store.current_state(),
        "batch_count": len(batches),
        "oldest_decision_date": (
            min(batch.decision_date for batch in batches).isoformat() if batches else None
        ),
        "latest_decision_date": (
            max(batch.decision_date for batch in batches).isoformat() if batches else None
        ),
        "symbols": sorted({item.symbol for item in predictions}),
        "sector_benchmarks": sorted({item.sector_benchmark for item in predictions}),
        "outcome_count": len(outcomes),
        "outcomes_by_horizon": dict(
            sorted(Counter(item.horizon_sessions for item in outcomes).items())
        ),
    }
    print(json.dumps(payload, indent=2))
    return 0


def _option_snapshot(path: str) -> tuple[dict[str, object], dict[str, object]]:
    snapshot = json.loads(Path(path).read_text())
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("account"), dict):
        raise ValueError("Option snapshot must contain an account object")
    return snapshot, snapshot["account"]


def _option_equity_constraints(
    option_positions: list[ActiveOptionPosition],
    prices: dict[str, float],
    targets: dict[str, float],
    equity: float,
) -> tuple[float, list[str]]:
    active = [
        position
        for position in option_positions
        if position.status in {"pending_open", "open", "closing"}
    ]
    reserved_cash = sum(
        position.collateral_reserved
        for position in active
        if position.strategy == "cash_secured_put"
    )
    halts: list[str] = []
    for position in active:
        if position.strategy != "covered_call":
            continue
        encumbered_weight = (
            position.shares_encumbered * prices.get(position.underlying, 0.0) / equity
            if equity > 0
            else 1.0
        )
        if targets.get(position.underlying, 0.0) + 1e-9 < encumbered_weight:
            halts.append(
                f"covered_option_share_encumbrance_blocks_equity_sale:{position.underlying}"
            )
    return reserved_cash, halts


def _equity_option_position_halts(
    broker_positions: object,
    durable_positions: list[ActiveOptionPosition],
) -> list[str]:
    if not isinstance(broker_positions, list):
        return ["broker_option_positions_missing"]
    try:
        broker_counts = Counter(
            _broker_option_position_key(item) for item in broker_positions if isinstance(item, dict)
        )
    except ValueError:
        return ["broker_option_position_shape_invalid"]
    if len(broker_counts) != len([item for item in broker_positions if isinstance(item, dict)]):
        return ["broker_option_position_shape_invalid"]
    durable_counts = Counter(
        (position.option_id, position.side, float(position.quantity))
        for position in durable_positions
        if position.status in {"pending_open", "open", "closing"}
    )
    halts: list[str] = []
    if broker_counts != durable_counts:
        halts.append("option_position_ledger_broker_mismatch")
    if any(
        str(item.get("state", "")).lower() in {"assigned", "exercised"}
        or bool(item.get("pending_assignment"))
        or bool(item.get("assignment_pending"))
        or bool(item.get("pending_exercise"))
        for item in broker_positions
        if isinstance(item, dict)
    ):
        halts.append("option_assignment_or_exercise_detected")
    return halts


def _option_premium_stop_ids(
    positions: list[ActiveOptionPosition],
    contracts: dict[str, OptionContractSnapshot],
) -> set[str]:
    mandatory: set[str] = set()
    for position in positions:
        if position.status not in {"pending_open", "open", "closing"}:
            continue
        contract = contracts.get(position.option_id)
        if contract is None:
            continue
        if (
            position.strategy in {"long_call", "long_put"}
            and contract.midpoint <= position.average_open_price * 0.50
        ):
            mandatory.add(position.option_id)
        if (
            position.strategy in {"covered_call", "cash_secured_put"}
            and contract.ask >= position.average_open_price * 2.0
        ):
            mandatory.add(position.option_id)
    return mandatory


def _broker_option_id(raw: dict[str, object]) -> str:
    value = raw.get("option_id") or raw.get("option") or raw.get("instrument")
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _broker_option_position_key(
    raw: dict[str, object],
) -> tuple[str, str, float]:
    option_id = _broker_option_id(raw)
    side = str(raw.get("type") or raw.get("position_type") or raw.get("side") or "").lower()
    value = raw.get("quantity")
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        quantity = -1.0
    if not option_id or side not in {"long", "short"} or quantity <= 0:
        raise ValueError("Broker option position is missing id, side, or quantity")
    return option_id, side, quantity


def _broker_equity_shares(
    positions: list[dict[str, object]],
) -> dict[str, float]:
    shares: dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        value = position.get("quantity")
        if isinstance(value, dict):
            value = value.get("amount")
        try:
            quantity = float(value)
        except (TypeError, ValueError):
            quantity = -1.0
        if not symbol or quantity < 0:
            raise ValueError("Broker position is missing a valid symbol or quantity")
        shares[symbol] = shares.get(symbol, 0.0) + quantity
    return shares


def command_option_authorize_batch(args: argparse.Namespace) -> int:
    """Authorize staged option drafts using fresh broker-native contract quotes."""
    now = datetime.now(UTC)
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    ledger = PostgresLedger.from_env()
    batch = ledger.latest_research_batch(as_of)
    pending = ledger.latest_pending_batch(as_of)
    if pending is not None and (batch is None or pending["created_at"] >= batch["created_at"]):
        payload = {
            "authorized": [],
            "results": [],
            "reason": "newer_pending_batch_not_finalized",
            "pending_batch_id": pending["batch_id"],
        }
        print(json.dumps(payload))
        return 2
    if batch is None:
        payload = {"authorized": [], "results": [], "reason": "no_staged_batch"}
        print(json.dumps(payload))
        return 2

    staged = batch["payload"]
    option_drafts = [OptionDraft.from_dict(item) for item in staged.get("option_drafts", [])]
    if not option_drafts:
        payload = {"authorized": [], "results": [], "reason": "no_option_drafts"}
        print(json.dumps(payload))
        return 2

    snapshot, account = _option_snapshot(args.snapshot)
    configured_account = _paired_broker_account(account)
    if not bool(account.get("agentic_allowed")):
        payload = {
            "authorized": [],
            "results": [],
            "reason": "account_not_agentic_allowed",
        }
        print(json.dumps(payload))
        return 2
    if str(account.get("option_level", "")).lower() not in {
        "option_level_2",
        "option_level_3",
        "2",
        "3",
    }:
        payload = {
            "authorized": [],
            "results": [],
            "reason": "option_level_2_required",
        }
        print(json.dumps(payload))
        return 2
    control = ledger.control_state(account_key(configured_account))
    if bool(control.get("halted")):
        payload = {
            "authorized": [],
            "results": [],
            "reason": f"picker_database_halt:{control.get('halt_reason') or 'unspecified'}",
        }
        print(json.dumps(payload))
        return 2

    evidence_items = [EvidenceVersion.from_dict(raw) for raw in staged["evidence"]]
    _verify_official_issuer_mappings(evidence_items)
    evidence = {item.evidence_id: item for item in evidence_items}
    source_drafts = {
        item.draft_id: item
        for item in (PickerDraft.from_dict(raw) for raw in staged.get("drafts", []))
    }
    critics = {
        item.draft_id: item for item in (CriticVerdict.from_dict(raw) for raw in staged["critics"])
    }
    contracts = [OptionContractSnapshot.from_dict(raw) for raw in snapshot.get("contracts", [])]
    positions = ledger.option_positions()
    equity = float(account["equity"])
    available_cash = float(account["cash"]) - float(account.get("pending_deposits", 0.0))
    broker_equity_positions = account.get("broker_equity_positions")
    underlying_prices = {
        str(symbol).upper(): float(price)
        for symbol, price in account.get("underlying_prices", {}).items()
    }
    if not isinstance(broker_equity_positions, list):
        raise ValueError("Option snapshot requires native broker_equity_positions")
    shares = _broker_equity_shares(broker_equity_positions)
    values = broker_position_values(broker_equity_positions, underlying_prices)
    encumbered: dict[str, int] = {}
    for position in positions:
        if (
            position.status in {"pending_open", "open", "closing"}
            and position.strategy == "covered_call"
        ):
            encumbered[position.underlying] = (
                encumbered.get(position.underlying, 0) + position.shares_encumbered
            )
    results: list[dict[str, object]] = []
    authorized: list[OptionDecisionPacket] = []

    for draft in option_drafts:
        source = source_drafts.get(draft.source_draft_id or "")
        critic = critics.get(draft.source_draft_id or draft.draft_id)
        if critic is None:
            results.append(
                {
                    "draft_id": draft.draft_id,
                    "accepted": False,
                    "reasons": ["missing_critic"],
                    "packet": None,
                }
            )
            continue
        result = validate_option_draft(
            draft,
            evidence,
            contracts,
            critic,
            prompt_hash=str(batch["prompt_hash"]),
            model_id=str(batch["model_id"]),
            account_equity=equity,
            available_cash=available_cash,
            open_positions=positions,
            source_draft=source,
            underlying_shares=int(shares.get(draft.underlying, 0)),
            encumbered_shares=encumbered.get(draft.underlying, 0),
            current_underlying_value=values.get(draft.underlying, 0.0),
            now=now,
        )
        results.append({"draft_id": draft.draft_id, **result.to_dict()})
        if result.packet is None:
            continue
        packet = result.packet
        ledger.authorize_option_packet(packet)
        authorized.append(packet)

    output_payload = {
        "batch_id": batch["batch_id"],
        "authorized": [packet.to_dict() for packet in authorized],
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_payload, indent=2) + "\n")
    print(json.dumps(output_payload, indent=2))
    return 0 if authorized else 2


def command_option_migrate(args: argparse.Namespace) -> int:
    """Apply audited database migrations as a deployment-only action."""
    paths = _migration_paths(args.path)
    status = PostgresCloudRuntimeStore.from_env().apply_migrations(paths)
    payload = {
        "applied": True,
        "migrations": [str(path) for path in paths],
        "schema_current": status.current,
    }
    print(json.dumps(payload))
    return 0


def _option_account_snapshot(
    raw: dict[str, object],
    positions: list[ActiveOptionPosition],
    planned_equity_orders: int = 0,
    planned_equity_entry_orders: int = 0,
    planned_equity_notional: float = 0.0,
    planned_equity_entry_notional: float = 0.0,
    persisted_orders_today: int = 0,
    persisted_entry_orders_today: int = 0,
    persisted_notional_today: float = 0.0,
    persisted_entry_notional_today: float = 0.0,
) -> OptionAccountSnapshot:
    broker_option_orders = raw.get("broker_option_orders")
    broker_equity_orders = raw.get("broker_equity_orders")
    if isinstance(broker_option_orders, list) and isinstance(broker_equity_orders, list):
        openings, option_notional = summarize_broker_option_orders(broker_option_orders)
        equity_order_count, equity_notional = summarize_broker_orders(broker_equity_orders)
        orders_today = max(
            equity_order_count + len(broker_option_orders) + planned_equity_orders,
            persisted_orders_today,
        )
        entry_orders_today = max(
            equity_order_count + len(broker_option_orders) + planned_equity_entry_orders,
            persisted_entry_orders_today,
        )
        notional_today = max(
            equity_notional + option_notional + planned_equity_notional,
            persisted_notional_today,
        )
        entry_notional_today = max(
            equity_notional + option_notional + planned_equity_entry_notional,
            persisted_entry_notional_today,
        )
        orders_source = "broker"
    else:
        openings = int(raw.get("option_openings_today", 0))
        orders_today = int(raw.get("orders_today", 0))
        entry_orders_today = int(raw.get("entry_orders_today", orders_today))
        notional_today = float(raw.get("notional_today", 0.0))
        entry_notional_today = float(raw.get("entry_notional_today", notional_today))
        orders_source = "unknown"
    open_positions = [
        item for item in positions if item.status in {"pending_open", "open", "closing"}
    ]
    halt_reasons = [str(item) for item in raw.get("halt_reasons", [])]
    broker_positions = raw.get("broker_option_positions")
    if not isinstance(broker_positions, list):
        halt_reasons.append("broker_option_positions_missing")
    else:
        try:
            broker_position_counts = Counter(
                _broker_option_position_key(item)
                for item in broker_positions
                if isinstance(item, dict)
            )
        except ValueError:
            broker_position_counts = Counter()
            halt_reasons.append("broker_option_position_shape_invalid")
        ledger_position_counts = Counter(
            (item.option_id, item.side, float(item.quantity)) for item in open_positions
        )
        if broker_position_counts != ledger_position_counts:
            halt_reasons.append("option_position_ledger_broker_mismatch")
        if any(
            str(item.get("state", "")).lower() in {"assigned", "exercised"}
            or bool(item.get("pending_assignment"))
            or bool(item.get("assignment_pending"))
            or bool(item.get("pending_exercise"))
            for item in broker_positions
            if isinstance(item, dict)
        ):
            halt_reasons.append("option_assignment_or_exercise_detected")
    covered: dict[str, int] = {}
    for item in open_positions:
        if item.strategy == "covered_call":
            covered[item.underlying] = covered.get(item.underlying, 0) + item.quantity
    today = datetime.now(UTC).date()
    mandatory = tuple(
        item.option_id
        for item in open_positions
        if trading_days_until(today, item.expiration_date) <= 5
    )
    broker_equity_positions = raw.get("broker_equity_positions")
    underlying_prices = {
        str(symbol).upper(): float(price)
        for symbol, price in raw.get("underlying_prices", {}).items()
    }
    if isinstance(broker_equity_positions, list):
        underlying_shares = _broker_equity_shares(broker_equity_positions)
        underlying_values = broker_position_values(
            broker_equity_positions,
            underlying_prices,
        )
    else:
        underlying_shares = {}
        underlying_values = {}
        halt_reasons.append("broker_equity_positions_missing")
    return OptionAccountSnapshot(
        account_number=str(raw["account_number"]),
        equity=float(raw["equity"]),
        cash=float(raw["cash"]),
        option_level=str(raw.get("option_level", "")),
        open_option_positions=len(open_positions),
        option_openings_today=openings,
        orders_today=orders_today,
        entry_orders_today=entry_orders_today,
        notional_today=notional_today,
        entry_notional_today=entry_notional_today,
        aggregate_long_debit=sum(
            item.premium_at_risk
            for item in open_positions
            if item.strategy in {"long_call", "long_put"}
        ),
        csp_collateral=sum(
            item.collateral_reserved
            for item in open_positions
            if item.strategy == "cash_secured_put"
        ),
        pending_deposits=float(raw.get("pending_deposits", 0.0)),
        underlying_shares=underlying_shares,
        underlying_values=underlying_values,
        covered_call_contracts=covered,
        mandatory_close_option_ids=mandatory,
        orders_source=orders_source,
        session_is_regular=bool(raw.get("session_is_regular", False)),
        agentic_allowed=bool(raw.get("agentic_allowed", False)),
        external_halt_reasons=tuple(dict.fromkeys(halt_reasons)),
    )


def _order_from_option_packet(
    packet: OptionDecisionPacket,
    account_number: str,
) -> ProposedOptionOrder:
    contract = packet.contract
    return ProposedOptionOrder(
        account_number=account_number,
        option_id=contract.option_id,
        chain_symbol=packet.underlying,
        strategy=packet.action,
        option_type=contract.option_type,
        side=packet.side,
        position_effect=packet.position_effect,
        quantity=packet.quantity,
        limit_price=packet.limit_price,
        bid_price=contract.bid,
        ask_price=contract.ask,
        quote_timestamp=contract.quote_at,
        expiration_date=contract.expiration_date,
        strike_price=contract.strike,
        rationale=f"Authorized option packet {packet.packet_id}",
        order_date=packet.valid_for_date,
    )


def command_option_plan(args: argparse.Namespace) -> int:
    """Serialize option planning with equity planning on this machine."""
    try:
        with session_lock(args.root):
            return _option_plan(args)
    except SessionLockedError as error:
        print(json.dumps({"mode": "REFUSED", "reason": str(error)}, indent=2))
        return 3


def _option_plan(args: argparse.Namespace) -> int:
    """Build a broker-ready, fail-closed option plan from authorized packets."""
    now = datetime.now(UTC)
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    snapshot, raw_account = _option_snapshot(args.snapshot)
    configured_account = _paired_broker_account(raw_account)

    ledger = PostgresLedger.from_env()
    control = ledger.control_state(account_key(configured_account))
    positions = ledger.option_positions()
    account_payload = dict(raw_account)
    halt_reasons = list(account_payload.get("halt_reasons", []))
    if bool(control.get("halted")):
        halt_reasons.append(f"picker_database_halt:{control.get('halt_reason') or 'unspecified'}")
    account_payload["halt_reasons"] = halt_reasons
    planned_equity_orders = 0
    planned_equity_entry_orders = 0
    planned_equity_notional = 0.0
    planned_equity_entry_notional = 0.0
    research_batch_id = ""
    equity_plan_path = Path(args.equity_plan)
    if equity_plan_path.exists():
        equity_plan = json.loads(equity_plan_path.read_text())
        research_batch_id = str(equity_plan.get("research_batch_id", ""))
        equity_approved_orders = equity_plan.get("approved_orders", [])
        planned_equity_orders = len(equity_approved_orders)
        equity_entry_orders = [
            order
            for order in equity_approved_orders
            if not (
                str(order.get("side")).lower() == "sell"
                and str(order.get("intent_class")).lower() in {"mandatory_exit", "close"}
            )
        ]
        planned_equity_entry_orders = len(equity_entry_orders)
        planned_equity_notional = sum(float(order["notional"]) for order in equity_approved_orders)
        planned_equity_entry_notional = sum(
            float(order["notional"]) for order in equity_entry_orders
        )
    persisted_orders_today, persisted_notional_today = daily_consumption(args.root)
    (
        persisted_entry_orders_today,
        persisted_entry_notional_today,
    ) = daily_entry_consumption(args.root)
    account = _option_account_snapshot(
        account_payload,
        positions,
        planned_equity_orders=planned_equity_orders,
        planned_equity_entry_orders=planned_equity_entry_orders,
        planned_equity_notional=planned_equity_notional,
        planned_equity_entry_notional=planned_equity_entry_notional,
        persisted_orders_today=persisted_orders_today,
        persisted_entry_orders_today=persisted_entry_orders_today,
        persisted_notional_today=persisted_notional_today,
        persisted_entry_notional_today=persisted_entry_notional_today,
    )
    packets = ledger.valid_option_packets(as_of, now)
    contracts = {
        item.option_id: item
        for item in (OptionContractSnapshot.from_dict(raw) for raw in snapshot.get("contracts", []))
    }
    premium_stop_ids = _option_premium_stop_ids(positions, contracts)
    if premium_stop_ids:
        account = replace(
            account,
            mandatory_close_option_ids=tuple(
                sorted(set(account.mandatory_close_option_ids) | premium_stop_ids)
            ),
        )

    missing_mandatory_quote_ids = sorted(
        position.option_id
        for position in positions
        if position.status in {"pending_open", "open", "closing"}
        and position.option_id in account.mandatory_close_option_ids
        and position.option_id not in contracts
    )
    if missing_mandatory_quote_ids:
        account = replace(
            account,
            external_halt_reasons=(
                *account.external_halt_reasons,
                *(
                    f"mandatory_option_quote_missing:{option_id}"
                    for option_id in missing_mandatory_quote_ids
                ),
            ),
        )

    orders: list[ProposedOptionOrder] = []
    order_packet_ids: dict[str, str] = {}
    for position in positions:
        if (
            position.status not in {"pending_open", "open", "closing"}
            or position.option_id not in account.mandatory_close_option_ids
        ):
            continue
        contract = contracts.get(position.option_id)
        if contract is None:
            continue
        side = "sell" if position.side == "long" else "buy"
        limit_price = contract.bid if side == "sell" else contract.ask
        order = ProposedOptionOrder(
            account_number=configured_account,
            option_id=position.option_id,
            chain_symbol=position.underlying,
            strategy="close",
            option_type=position.option_type,
            side=side,
            position_effect="close",
            quantity=1,
            limit_price=limit_price,
            bid_price=contract.bid,
            ask_price=contract.ask,
            quote_timestamp=contract.quote_at,
            expiration_date=contract.expiration_date,
            strike_price=contract.strike,
            rationale="Mandatory close no later than five trading days before expiry",
            order_date=as_of,
        )
        orders.append(order)
        order_packet_ids[order.ref_id] = position.packet_id

    planned_option_ids = {order.option_id for order in orders}
    for packet in packets:
        if packet.option_id in planned_option_ids:
            continue
        order = _order_from_option_packet(packet, configured_account)
        orders.append(order)
        planned_option_ids.add(order.option_id)
        order_packet_ids[order.ref_id] = packet.packet_id

    decisions = evaluate_option_batch(orders, account, root=args.root, now=now)
    approved: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for decision in decisions:
        item = decision.to_dict()
        item["packet_id"] = order_packet_ids.get(decision.order.ref_id, "")
        item["broker_parameters"] = decision.order.place_parameters()
        if decision.approved:
            approved.append(item)
        else:
            rejected.append(item)
    rejected.extend(
        {
            "option_id": option_id,
            "position_effect": "close",
            "approved": False,
            "reasons": ["mandatory_close_quote_missing"],
        }
        for option_id in missing_mandatory_quote_ids
    )
    payload = {
        "mode": "PLAN_ONLY_REQUIRES_HUMAN_APPROVAL",
        "planned_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "trade_date": _nyse_session_date(now).isoformat(),
        "account_number": configured_account,
        "authorization_packet_ids": sorted(
            {str(item["packet_id"]) for item in approved if item["packet_id"]}
        ),
        "approved_orders": approved,
        "rejected_orders": rejected,
        "halts": list(account.external_halt_reasons),
        "orders_already_used_today": account.orders_today,
        "notional_already_used_today": account.notional_today,
        "entry_orders_already_used_today": account.effective_entry_orders_today,
        "entry_notional_already_used_today": account.effective_entry_notional_today,
        "option_openings_already_used_today": account.option_openings_today,
        "open_option_positions": account.open_option_positions,
        "research_batch_id": research_batch_id,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    append_audit_record({"event": "option_plan", **payload}, root=args.root)
    print(json.dumps(payload, indent=2))
    return 0 if approved else 2


def command_option_reconcile(args: argparse.Namespace) -> int:
    """Reconcile native option fills and durably halt on any breach."""
    plan = json.loads(Path(args.plan).read_text())
    executed = json.loads(Path(args.executed).read_text())
    if isinstance(executed, dict):
        executed = executed.get("orders", [])
    result = reconcile_option_orders(
        plan.get("approved_orders", []),
        executed,
        root=args.root,
    )
    if (
        not result["clean"]
        and result["breaches"]
        and os.environ.get("DATABASE_URL")
        and plan.get("account_number")
    ):
        PostgresLedger.from_env().halt(
            account_key(str(plan["account_number"])),
            ";".join(str(item) for item in result["breaches"]),
        )
        result["database_halt_engaged"] = True
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["clean"] else 2


def command_option_reserve(args: argparse.Namespace) -> int:
    """Durably reserve covered shares or CSP cash immediately before placement."""
    plan = json.loads(Path(args.plan).read_text())
    planned_at = datetime.fromisoformat(str(plan["planned_at"]).replace("Z", "+00:00"))
    expires_at = datetime.fromisoformat(str(plan["expires_at"]).replace("Z", "+00:00"))
    if planned_at.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("Option plan timestamps must be timezone-aware")
    now = datetime.now(UTC)
    planned_at = planned_at.astimezone(UTC)
    expires_at = expires_at.astimezone(UTC)
    if planned_at > now or expires_at <= now or expires_at - planned_at > timedelta(minutes=5):
        raise ValueError("Option plan is expired or has an invalid validity window")
    _, account = _option_snapshot(args.snapshot)
    configured_account = _paired_broker_account(account)
    ledger = PostgresLedger.from_env()
    trade_date = _nyse_session_date()
    research_batch_id = str(plan.get("research_batch_id", ""))
    budget_reservations = [
        (
            str(order["ref_id"]),
            float(order["limit_price"]) * int(order["quantity"]) * 100,
            str(order.get("position_effect")) == "open",
            str(order.get("position_effect")) == "open",
        )
        for order in plan.get("approved_orders", [])
    ]
    exit_budget = [item for item in budget_reservations if not item[2]]
    entry_budget = [item for item in budget_reservations if item[2]]
    observed_usage = (
        int(plan["orders_already_used_today"]),
        float(plan["notional_already_used_today"]),
        int(plan["entry_orders_already_used_today"]),
        float(plan["entry_notional_already_used_today"]),
    )
    budget_usage = None
    reserved_ref_ids: list[str] = []
    blocked_ref_ids: list[str] = []
    if exit_budget:
        budget_usage = ledger.reserve_execution_budget(
            account_key(configured_account),
            trade_date,
            exit_budget,
            max_entry_orders=2,
            max_entry_notional=300.0,
            observed_usage=observed_usage,
            observed_open_option_positions=int(plan["open_option_positions"]),
        )
        reserved_ref_ids.extend(item[0] for item in exit_budget)
    if entry_budget:
        try:
            budget_usage = ledger.reserve_execution_budget(
                account_key(configured_account),
                trade_date,
                entry_budget,
                max_entry_orders=2,
                max_entry_notional=300.0,
                observed_usage=observed_usage,
                observed_open_option_positions=int(plan["open_option_positions"]),
                research_batch_id=research_batch_id,
            )
            reserved_ref_ids.extend(item[0] for item in entry_budget)
        except RuntimeError:
            blocked_ref_ids.extend(item[0] for item in entry_budget)
    reserved_set = set(reserved_ref_ids)
    record_reservation_consumption(
        [
            (ref_id, notional, is_entry)
            for ref_id, notional, is_entry, _ in budget_reservations
            if ref_id in reserved_set
        ],
        root=getattr(args, "root", "."),
        day=trade_date,
    )
    packets = {
        packet.packet_id: packet for packet in ledger.valid_option_packets(datetime.now(UTC).date())
    }
    available_cash = float(account["cash"]) - float(account.get("pending_deposits", 0.0))
    broker_equity_positions = account.get("broker_equity_positions")
    if not isinstance(broker_equity_positions, list):
        raise ValueError("Option snapshot requires native broker_equity_positions")
    available_shares = {
        symbol: int(quantity)
        for symbol, quantity in _broker_equity_shares(broker_equity_positions).items()
    }
    reserved: list[str] = []
    for order in plan.get("approved_orders", []):
        if str(order["ref_id"]) not in reserved_ref_ids:
            continue
        if str(order.get("position_effect")) != "open":
            continue
        packet_id = str(order.get("packet_id") or "")
        packet = packets.get(packet_id)
        if packet is None:
            raise ValueError(f"Option plan references unavailable packet {packet_id}")
        if not (packet.collateral_required or packet.shares_encumbered):
            continue
        ledger.reserve_option_collateral(
            packet.packet_id,
            account_key(configured_account),
            packet.collateral_required,
            ({packet.underlying: packet.shares_encumbered} if packet.shares_encumbered else {}),
            available_cash=available_cash,
            available_shares=available_shares,
        )
        reserved.append(packet.packet_id)
    payload = {
        "reserved_ref_ids": reserved_ref_ids,
        "blocked_ref_ids": blocked_ref_ids,
        "resource_packet_ids": reserved,
        "budget_usage": budget_usage,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload))
    return 0 if reserved_ref_ids else 2


def command_option_sync(args: argparse.Namespace) -> int:
    """Persist option lifecycle changes only after clean option reconciliation."""
    plan = json.loads(Path(args.plan).read_text())
    executed_raw = json.loads(Path(args.executed).read_text())
    executed_orders = (
        executed_raw.get("orders", []) if isinstance(executed_raw, dict) else executed_raw
    )
    stored_reconciliation = json.loads(Path(args.reconciliation).read_text())
    lifecycle_at = datetime.fromisoformat(
        str(stored_reconciliation["reconciled_at"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    reconciliation = reconcile_option_orders(
        plan.get("approved_orders", []),
        executed_orders,
        root=args.root,
        engage_on_breach=False,
    )
    equity_reconciliation = json.loads(Path(args.equity_reconciliation).read_text())
    if not bool(equity_reconciliation.get("clean")):
        raise ValueError("Cannot sync options before clean equity reconciliation")
    bound_fields = (
        "clean",
        "complete",
        "breaches",
        "matched",
        "pending",
        "terminal_unfilled",
        "approved_but_unfilled",
    )
    if any(stored_reconciliation.get(field) != reconciliation.get(field) for field in bound_fields):
        raise ValueError("Stored option reconciliation is stale or does not match fills")
    if not bool(reconciliation.get("clean")):
        raise ValueError("Cannot sync option state from incomplete reconciliation")
    filled_ref_ids = {
        str(item.get("ref_id") or item.get("client_order_id") or "")
        for item in executed_orders
        if str(item.get("state", "")).lower() == "filled"
    }
    matched = {str(item["ref_id"]): item for item in reconciliation.get("matched", [])}
    if not set(matched).issubset(filled_ref_ids):
        raise ValueError("Option reconciliation does not match the executed-order file")
    ledger = PostgresLedger.from_env()
    packet_ids = {str(item.get("packet_id") or "") for item in plan.get("approved_orders", [])}
    packets = {
        packet_id: packet
        for packet_id in packet_ids
        if packet_id and (packet := ledger.option_packet(packet_id)) is not None
    }
    positions: dict[str, ActiveOptionPosition] = {}
    for item in sorted(
        ledger.option_positions(),
        key=lambda value: value.status in {"pending_open", "open", "closing"},
    ):
        positions[item.option_id] = item
    transitions: list[dict[str, str]] = []
    for order in plan.get("approved_orders", []):
        packet_id = str(order.get("packet_id", ""))
        position_effect = str(order.get("position_effect", ""))
        option_id = str(order.get("option_id", ""))
        packet = packets.get(packet_id)
        if packet is not None and packet.position_effect == position_effect:
            expected_order = _order_from_option_packet(
                packet,
                str(plan.get("account_number", "")),
            )
            bound_fields = (
                ("option_id", expected_order.option_id),
                ("side", expected_order.side),
                ("position_effect", expected_order.position_effect),
                ("quantity", expected_order.quantity),
                ("limit_price", expected_order.limit_price),
                ("ref_id", expected_order.ref_id),
            )
            if any(order.get(field) != value for field, value in bound_fields):
                raise ValueError("Option plan order does not match its decision packet")
        elif position_effect == "close":
            active_position = positions.get(option_id)
            if active_position is None or packet_id != active_position.packet_id:
                raise ValueError("Mandatory close does not match an active option position")
        fill = matched.get(str(order.get("ref_id", "")))
        if fill is None:
            if packet is not None and str(order.get("position_effect")) == "open":
                ledger.cancel_option_packet(
                    packet.packet_id,
                    "approved_option_order_not_filled",
                )
                transitions.append({"position_id": packet.packet_id, "status": "cancelled"})
            elif (
                packet is not None
                and packet.position_effect == "close"
                and str(order.get("position_effect")) == "close"
            ):
                ledger.cancel_option_packet(
                    packet.packet_id,
                    "approved_option_close_not_filled",
                )
            continue
        if position_effect == "open":
            if packet is None:
                raise ValueError(f"Option fill references unavailable packet {packet_id}")
            position = ActiveOptionPosition(
                position_id=packet.packet_id,
                packet_id=packet.packet_id,
                underlying=packet.underlying,
                strategy=packet.action,
                option_id=packet.option_id,
                contract_symbol=packet.contract.contract_symbol,
                option_type=packet.contract.option_type,
                expiration_date=packet.contract.expiration_date,
                strike=packet.contract.strike,
                quantity=packet.quantity,
                side=("long" if packet.side == "buy" else "short"),
                opened_at=lifecycle_at,
                average_open_price=float(fill["average_fill_price"]),
                premium_at_risk=packet.max_risk,
                collateral_reserved=packet.collateral_required,
                shares_encumbered=packet.shares_encumbered,
                status="open",
                structure_fingerprint=packet.structure_fingerprint,
            )
            ledger.sync_option_open(
                position,
                content_hash(f"{packet.packet_id}|opened|{order['ref_id']}"),
                lifecycle_at,
                fill,
                ref_id=str(order["ref_id"]),
                broker_order_id=str(fill.get("order_id") or ""),
            )
            transitions.append({"position_id": position.position_id, "status": "open"})
        elif position_effect == "close":
            position = positions.get(option_id)
            if position is None:
                raise ValueError(f"Close fill references unknown option {option_id}")
            closed = replace(position, status="closed", position_hash="")
            close_packet = packets.get(packet_id)
            ledger.sync_option_close(
                closed,
                content_hash(f"{position.position_id}|closed|{order['ref_id']}"),
                lifecycle_at,
                fill,
                ref_id=str(order["ref_id"]),
                broker_order_id=str(fill.get("order_id") or ""),
                close_packet_id=(
                    close_packet.packet_id
                    if close_packet is not None and close_packet.position_effect == "close"
                    else None
                ),
            )
            transitions.append({"position_id": position.position_id, "status": "closed"})

    payload = {"synced": True, "transitions": transitions}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-trader",
        description="Research and paper-trade a guarded momentum strategy.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Run OOS and robustness tests")
    _add_common_arguments(validate)
    validate.add_argument("--output", default="artifacts/latest")
    validate.set_defaults(func=command_validate)

    signal = subparsers.add_parser("signal", help="Write paper target holdings")
    _add_common_arguments(signal)
    signal.add_argument("--output", default="artifacts/latest/paper-signal.json")
    signal.set_defaults(func=command_signal)

    event_score = subparsers.add_parser("event-score", help="Validate and score an evidence bundle")
    event_score.add_argument("--bundle", required=True)
    event_score.add_argument("--output", default="artifacts/alternative-data/event-scores.json")
    event_score.set_defaults(func=command_event_score)

    event_study = subparsers.add_parser("event-study", help="Measure event returns versus SPY")
    event_study.add_argument("--bundle", required=True)
    event_study.add_argument("--end")
    event_study.add_argument("--refresh", action="store_true")
    event_study.add_argument("--round-trip-cost-bps", type=float, default=20.0)
    event_study.add_argument("--output", default="artifacts/alternative-data/event-study")
    event_study.set_defaults(func=command_event_study)

    tournament = subparsers.add_parser(
        "tournament", help="Select an algorithm on development data and test holdout"
    )
    tournament.add_argument("--start", default="2008-01-01")
    tournament.add_argument("--development-start", default="2010-01-01")
    tournament.add_argument("--holdout-start", default="2019-01-01")
    tournament.add_argument("--end")
    tournament.add_argument("--capital", type=float, default=5_000.0)
    tournament.add_argument("--refresh", action="store_true")
    tournament.add_argument("--output", default="artifacts/tournament")
    tournament.set_defaults(func=command_tournament)

    tournament_signal = subparsers.add_parser(
        "tournament-signal", help="Write frozen shadow targets for tournament arms"
    )
    tournament_signal.add_argument("--start", default="2008-01-01")
    tournament_signal.add_argument("--end")
    tournament_signal.add_argument("--capital", type=float, default=5_000.0)
    tournament_signal.add_argument("--refresh", action="store_true")
    tournament_signal.add_argument("--summary", default="artifacts/tournament/summary.json")
    tournament_signal.add_argument("--challenger", default="diversified_absolute_trend")
    tournament_signal.add_argument("--output", default="artifacts/tournament/shadow-targets.json")
    tournament_signal.set_defaults(func=command_tournament_signal)

    analyze = subparsers.add_parser(
        "analyze", help="Calculate quantitative features for arbitrary tickers"
    )
    analyze.add_argument("--tickers", required=True, help="Comma-separated symbols")
    analyze.add_argument("--benchmark", default="SPY")
    analyze.add_argument("--start", default="2021-01-01")
    analyze.add_argument("--end")
    analyze.add_argument("--refresh", action="store_true")
    analyze.add_argument("--output", default="artifacts/research/universe")
    analyze.set_defaults(func=command_analyze)

    option_chain = subparsers.add_parser(
        "option-chain", help="Save a timestamped current option-chain snapshot"
    )
    option_chain.add_argument("--symbol", required=True)
    option_chain.add_argument("--expiration")
    option_chain.add_argument("--output", default="artifacts/research/option-chain")
    option_chain.set_defaults(func=command_option_chain)

    option_analyze = subparsers.add_parser(
        "option-analyze", help="Analyze payoff and Greeks for an option structure"
    )
    option_analyze.add_argument("--spec", required=True)
    option_analyze.add_argument("--output", default="artifacts/research/option-analysis.json")
    option_analyze.set_defaults(func=command_option_analyze)

    proposal_validate = subparsers.add_parser(
        "proposal-validate", help="Validate a structured LLM research proposal"
    )
    proposal_validate.add_argument("--proposal", required=True)
    proposal_validate.add_argument("--evidence-bundle", required=True)
    proposal_validate.add_argument("--benchmark", default="SPY")
    proposal_validate.add_argument("--start", default="2021-01-01")
    proposal_validate.add_argument("--end")
    proposal_validate.add_argument("--capital", type=float, default=5_000.0)
    proposal_validate.add_argument("--refresh", action="store_true")
    proposal_validate.add_argument(
        "--output", default="artifacts/research/proposal-validation.json"
    )
    proposal_validate.set_defaults(func=command_proposal_validate)

    cloud_schema = subparsers.add_parser(
        "cloud-schema-check",
        help="Fail closed unless every database migration checksum is current",
    )
    cloud_schema.add_argument("--path")
    cloud_schema.set_defaults(func=command_cloud_schema_check)

    cloud_run_acquire = subparsers.add_parser(
        "cloud-run-acquire",
        help="Acquire one durable scheduled-window lease",
    )
    cloud_run_acquire.add_argument("--task", required=True)
    cloud_run_acquire.add_argument("--scheduled-for", required=True)
    cloud_run_acquire.add_argument("--git-sha", required=True)
    cloud_run_acquire.add_argument("--lease-seconds", type=int, default=7200)
    cloud_run_acquire.set_defaults(func=command_cloud_run_acquire)

    cloud_run_heartbeat = subparsers.add_parser(
        "cloud-run-heartbeat",
        help="Extend an owned durable run lease",
    )
    cloud_run_heartbeat.add_argument("--run-id", required=True)
    cloud_run_heartbeat.add_argument("--lease-token", required=True)
    cloud_run_heartbeat.add_argument("--lease-seconds", type=int, default=7200)
    cloud_run_heartbeat.set_defaults(func=command_cloud_run_heartbeat)

    cloud_run_finish = subparsers.add_parser(
        "cloud-run-finish",
        help="Seal a durable automation run as completed or failed",
    )
    cloud_run_finish.add_argument("--run-id", required=True)
    cloud_run_finish.add_argument("--lease-token", required=True)
    cloud_run_finish.add_argument("--status", choices=("completed", "failed"), required=True)
    cloud_run_finish.add_argument("--reason")
    cloud_run_finish.set_defaults(func=command_cloud_run_finish)

    cloud_artifact = subparsers.add_parser(
        "cloud-artifact-record",
        help="Persist a content-addressed runtime input or evidence snapshot",
    )
    cloud_artifact.add_argument("--run-id", required=True)
    cloud_artifact.add_argument("--artifact-type", required=True)
    cloud_artifact.add_argument("--input", required=True)
    cloud_artifact.add_argument("--source-uri")
    cloud_artifact.set_defaults(func=command_cloud_artifact_record)

    cloud_kg = subparsers.add_parser(
        "cloud-kg-record",
        help="Persist runtime knowledge nodes, edges, and immutable observations",
    )
    cloud_kg.add_argument("--input", required=True)
    cloud_kg.set_defaults(func=command_cloud_kg_record)

    live_plan = subparsers.add_parser(
        "live-plan", help="Produce a risk-checked real-money order plan (does not place orders)"
    )
    live_plan.add_argument("--request", required=True, help="JSON with account, targets, prices")
    live_plan.add_argument("--root", default=".")
    live_plan.add_argument("--max-order-notional", type=float, default=150.0)
    live_plan.add_argument("--max-position-weight", type=float, default=0.035)
    live_plan.add_argument("--max-orders-per-day", type=int, default=8)
    live_plan.add_argument("--max-daily-notional", type=float, default=800.0)
    live_plan.add_argument("--max-entry-orders-per-day", type=int, default=2)
    live_plan.add_argument("--max-entry-daily-notional", type=float, default=300.0)
    live_plan.add_argument("--rebalance-threshold", type=float, default=0.05)
    live_plan.add_argument("--record-equity", action="store_true")
    live_plan.add_argument("--persist", action="store_true")
    live_plan.add_argument("--run-id")
    live_plan.add_argument("--lease-token")
    live_plan.add_argument("--output", default="artifacts/live/plan.json")
    live_plan.set_defaults(func=command_live_plan)

    live_review = subparsers.add_parser(
        "live-review-record",
        help="Bind complete Robinhood review previews to an immutable cloud plan",
    )
    live_review.add_argument("--plan-id", required=True)
    live_review.add_argument("--draft-hash", required=True)
    live_review.add_argument("--reviews", required=True)
    live_review.add_argument("--output", default="artifacts/live/review-record.json")
    live_review.set_defaults(func=command_live_review_record)

    confirmation_keygen = subparsers.add_parser(
        "confirmation-keygen",
        help="Create a human-held Ed25519 confirmation key and print its public half",
    )
    confirmation_keygen.add_argument("--private-key", required=True)
    confirmation_keygen.set_defaults(func=command_confirmation_keygen)

    confirmation_sign = subparsers.add_parser(
        "confirmation-sign",
        help="Sign one exact reviewed plan using the human-held private key",
    )
    confirmation_sign.add_argument("--private-key", required=True)
    confirmation_sign.add_argument("--plan-id", required=True)
    confirmation_sign.add_argument("--plan-hash", required=True)
    confirmation_sign.set_defaults(func=command_confirmation_sign)

    live_confirm = subparsers.add_parser(
        "live-confirm",
        help="Verify and record the user's signed confirmation of one exact reviewed plan",
    )
    live_confirm.add_argument("--plan-id", required=True)
    live_confirm.add_argument("--plan-hash", required=True)
    live_confirm.add_argument("--confirmation-text", required=True)
    live_confirm.set_defaults(func=command_live_confirm)

    live_export = subparsers.add_parser(
        "live-plan-export",
        help="Recover a hash-verified execution plan from Supabase",
    )
    live_export.add_argument("--plan-id", required=True)
    live_export.add_argument("--plan-hash", required=True)
    live_export.add_argument("--output", default="artifacts/live/plan.json")
    live_export.set_defaults(func=command_live_plan_export)

    live_startup = subparsers.add_parser(
        "live-startup-check",
        help="Block new work while a prior durable order attempt is unresolved",
    )
    live_startup.add_argument("--snapshot", required=True)
    live_startup.add_argument("--output", default="artifacts/live/startup-check.json")
    live_startup.set_defaults(func=command_live_startup_check)

    live_reconcile = subparsers.add_parser(
        "live-reconcile", help="Verify fills against the approved plan and halt on breaches"
    )
    live_reconcile.add_argument("--plan", default="artifacts/live/plan.json")
    live_reconcile.add_argument("--plan-id")
    live_reconcile.add_argument("--executed", required=True, help="JSON list of executed orders")
    live_reconcile.add_argument("--root", default=".")
    live_reconcile.add_argument("--output", default="artifacts/live/reconciliation.json")
    live_reconcile.set_defaults(func=command_live_reconcile)

    live_reserve = subparsers.add_parser(
        "live-reserve",
        help="Atomically reserve the shared cloud budget before equity placement",
    )
    live_reserve.add_argument("--plan", default="artifacts/live/plan.json")
    live_reserve.add_argument("--plan-id")
    live_reserve.add_argument("--plan-hash")
    live_reserve.add_argument("--confirmation-id")
    live_reserve.add_argument("--snapshot")
    live_reserve.add_argument("--root", default=".")
    live_reserve.add_argument("--output", default="artifacts/live/reservation.json")
    live_reserve.set_defaults(func=command_live_reserve)

    live_claim = subparsers.add_parser(
        "live-attempt-claim",
        help="Atomically claim one exact reservation immediately before its broker call",
    )
    live_claim.add_argument("--attempt-id", required=True)
    live_claim.add_argument("--plan-id", required=True)
    live_claim.add_argument("--plan-hash", required=True)
    live_claim.add_argument("--confirmation-id", required=True)
    live_claim.add_argument("--ref-id", required=True)
    live_claim.add_argument("--validation-snapshot-hash", required=True)
    live_claim.set_defaults(func=command_live_attempt_claim)

    live_attempt = subparsers.add_parser(
        "live-attempt-transition",
        help="Durably record broker results after a claimed broker call",
    )
    live_attempt.add_argument("--attempt-id", required=True)
    live_attempt.add_argument(
        "--state",
        required=True,
        choices=tuple(
            sorted(
                (NONTERMINAL_ATTEMPT_STATES - {"reserved", "submitting"})
                | {
                    "filled",
                    "cancelled",
                    "rejected",
                    "failed",
                    "expired",
                    "invalidated",
                    "reconciled",
                }
            )
        ),
    )
    live_attempt.add_argument("--response")
    live_attempt.add_argument("--broker-order-id")
    live_attempt.add_argument("--error")
    live_attempt.set_defaults(func=command_live_attempt_transition)

    live_control = subparsers.add_parser(
        "live-control-status",
        help="Show durable halt, budget, and unresolved-attempt state",
    )
    live_control.add_argument("--snapshot", required=True)
    live_control.set_defaults(func=command_live_control_status)

    live_halt = subparsers.add_parser(
        "live-halt",
        help="Engage a durable operator halt without exposing a resume command",
    )
    live_halt.add_argument("--snapshot", required=True)
    live_halt.add_argument("--scope", choices=("entries", "all"), default="all")
    live_halt.add_argument("--reason", required=True)
    live_halt.set_defaults(func=command_live_halt)

    picker_validate = subparsers.add_parser(
        "picker-validate", help="Validate an AI stock-pick draft for live eligibility"
    )
    picker_validate.add_argument("--draft", required=True)
    picker_validate.add_argument("--evidence", required=True)
    picker_validate.add_argument("--quant", required=True)
    picker_validate.add_argument("--critic", required=True)
    picker_validate.add_argument("--prompt-file")
    picker_validate.add_argument("--prompt-hash")
    picker_validate.add_argument("--model-id", required=True)
    picker_validate.add_argument("--persist", action="store_true")
    picker_validate.add_argument("--output", default="artifacts/picker/validation.json")
    picker_validate.set_defaults(func=command_picker_validate)

    picker_stage = subparsers.add_parser(
        "picker-stage", help="Stage a research/critic batch in the durable decision ledger"
    )
    picker_stage.add_argument("--bundle", required=True)
    picker_stage.set_defaults(func=command_picker_stage)

    picker_stage_pending = subparsers.add_parser(
        "picker-stage-pending",
        help="Stage analyst output for a separate independent critic",
    )
    picker_stage_pending.add_argument("--bundle", required=True)
    picker_stage_pending.set_defaults(func=command_picker_stage_pending)

    picker_cycle_start = subparsers.add_parser(
        "picker-cycle-start",
        help="Create a durable marker before an analyst cycle begins",
    )
    picker_cycle_start.add_argument("--cycle-id", required=True)
    picker_cycle_start.add_argument("--as-of")
    picker_cycle_start.set_defaults(func=command_picker_cycle_start)

    picker_cycle_fail = subparsers.add_parser(
        "picker-cycle-fail",
        help="Release a failed analyst or critic cycle marker",
    )
    picker_cycle_fail.add_argument("--cycle-id", required=True)
    picker_cycle_fail.set_defaults(func=command_picker_cycle_fail)

    picker_export_pending = subparsers.add_parser(
        "picker-export-pending",
        help="Export today's latest pending research batch for criticism",
    )
    picker_export_pending.add_argument("--as-of")
    picker_export_pending.add_argument("--output", default="artifacts/picker/pending-research.json")
    picker_export_pending.set_defaults(func=command_picker_export_pending)

    picker_finalize_pending = subparsers.add_parser(
        "picker-finalize-pending",
        help="Attach independent critics and promote a pending batch",
    )
    picker_finalize_pending.add_argument("--critics", required=True)
    picker_finalize_pending.add_argument("--as-of")
    picker_finalize_pending.set_defaults(func=command_picker_finalize_pending)

    picker_verify = subparsers.add_parser(
        "picker-verify-evidence",
        help="Verify evidence quotes against saved source documents",
    )
    picker_verify.add_argument("--evidence", required=True)
    picker_verify.add_argument("--documents", required=True)
    picker_verify.add_argument("--output", default="artifacts/picker/verified-evidence.json")
    picker_verify.set_defaults(func=command_picker_verify_evidence)

    picker_quant = subparsers.add_parser(
        "picker-build-quant",
        help="Compute deterministic picker ranks from a frozen raw input snapshot",
    )
    picker_quant.add_argument("--input", required=True)
    picker_quant.add_argument("--as-of")
    picker_quant.add_argument("--output", default="artifacts/picker/quant.json")
    picker_quant.set_defaults(func=command_picker_build_quant)

    picker_close = subparsers.add_parser(
        "picker-record-close",
        help="Persist a broker equity anchor after the official NYSE close",
    )
    picker_close.add_argument("--snapshot", required=True)
    picker_close.add_argument("--session-date")
    picker_close.set_defaults(func=command_picker_record_close)

    picker_authorize = subparsers.add_parser(
        "picker-authorize-batch",
        help="Authorize today's staged batch using fresh broker-side quant snapshots",
    )
    picker_authorize.add_argument("--quant", required=True)
    picker_authorize.add_argument("--as-of")
    picker_authorize.add_argument("--output", default="artifacts/picker/authorized-batch.json")
    picker_authorize.set_defaults(func=command_picker_authorize_batch)

    picker_plan = subparsers.add_parser(
        "picker-plan", help="Build a live-plan request from authorized AI stock picks"
    )
    picker_plan.add_argument("--snapshot", required=True)
    picker_plan.add_argument("--packets", help="Authorized packet fixture; DB used when omitted")
    picker_plan.add_argument("--theses", help="Active-thesis fixture; DB used when omitted")
    picker_plan.add_argument("--as-of")
    picker_plan.add_argument("--output", default="artifacts/live/request.json")
    picker_plan.set_defaults(func=command_picker_plan)

    picker_sync = subparsers.add_parser(
        "picker-sync", help="Persist picker lifecycle changes after clean fills"
    )
    picker_sync.add_argument("--plan", default="artifacts/live/plan.json")
    picker_sync.add_argument("--plan-id")
    picker_sync.add_argument("--executed", required=True)
    picker_sync.add_argument("--reconciliation", default="artifacts/live/reconciliation.json")
    picker_sync.add_argument("--output", default="artifacts/live/picker-sync.json")
    picker_sync.set_defaults(func=command_picker_sync)

    learning_build = subparsers.add_parser(
        "learning-build-batch",
        help="Build all four shadow experiment arms from quant and research scores",
    )
    learning_build.add_argument("--quant", required=True)
    learning_build.add_argument("--research", required=True)
    learning_build.add_argument("--output", default="artifacts/learning/prediction-batch.json")
    learning_build.set_defaults(func=command_learning_build_batch)

    learning_freeze = subparsers.add_parser(
        "learning-freeze",
        help="Validate and persist a complete four-arm shadow prediction batch",
    )
    learning_freeze.add_argument("--batch", required=True)
    learning_freeze.add_argument("--output", default="artifacts/learning/latest-freeze.json")
    learning_freeze.set_defaults(func=command_learning_freeze)

    learning_mark = subparsers.add_parser(
        "learning-mark", help="Append newly available forward outcome marks"
    )
    learning_mark.add_argument("--closes", required=True)
    learning_mark.add_argument("--as-of")
    learning_mark.add_argument("--cost-bps", type=float, default=20.0)
    learning_mark.add_argument("--output", default="artifacts/learning/latest-outcomes.json")
    learning_mark.set_defaults(func=command_learning_mark)

    learning_report = subparsers.add_parser(
        "learning-report",
        help="Evaluate shadow arms and report promotion gates without promoting",
    )
    learning_report.add_argument(
        "--horizon-sessions", type=int, choices=(1, 3, 5, 20, 60), default=20
    )
    learning_report.add_argument("--output", default="artifacts/learning/latest-report.json")
    learning_report.set_defaults(func=command_learning_report)

    learning_status = subparsers.add_parser(
        "learning-status", help="Show the tracked shadow universe and outcome coverage"
    )
    learning_status.set_defaults(func=command_learning_status)

    option_authorize = subparsers.add_parser(
        "option-authorize-batch",
        help="Authorize staged Level 2 option drafts using live broker quotes",
    )
    option_authorize.add_argument("--snapshot", required=True)
    option_authorize.add_argument("--as-of")
    option_authorize.add_argument("--output", default="artifacts/options/authorized.json")
    option_authorize.set_defaults(func=command_option_authorize_batch)

    option_migrate = subparsers.add_parser(
        "option-migrate",
        help="Apply the idempotent Postgres migration for Level 2 options",
    )
    option_migrate.add_argument("--path")
    option_migrate.set_defaults(func=command_option_migrate)

    cloud_migrate = subparsers.add_parser(
        "cloud-migrate",
        help="Apply audited database migrations as a deployment-only action",
    )
    cloud_migrate.add_argument("--path")
    cloud_migrate.set_defaults(func=command_option_migrate)

    option_plan = subparsers.add_parser(
        "option-plan",
        help="Build a broker-ready plan from authorized Level 2 option packets",
    )
    option_plan.add_argument("--snapshot", required=True)
    option_plan.add_argument("--as-of")
    option_plan.add_argument("--root", default=".")
    option_plan.add_argument("--equity-plan", default="artifacts/live/plan.json")
    option_plan.add_argument("--output", default="artifacts/live/options-plan.json")
    option_plan.set_defaults(func=command_option_plan)

    option_reconcile = subparsers.add_parser(
        "option-reconcile",
        help="Verify option fills against the approved plan and halt on breaches",
    )
    option_reconcile.add_argument("--plan", default="artifacts/live/options-plan.json")
    option_reconcile.add_argument("--executed", required=True)
    option_reconcile.add_argument("--root", default=".")
    option_reconcile.add_argument("--output", default="artifacts/live/options-reconciliation.json")
    option_reconcile.set_defaults(func=command_option_reconcile)

    option_reserve = subparsers.add_parser(
        "option-reserve",
        help="Reserve covered shares or CSP cash immediately before placement",
    )
    option_reserve.add_argument("--plan", default="artifacts/live/options-plan.json")
    option_reserve.add_argument("--snapshot", required=True)
    option_reserve.add_argument("--root", default=".")
    option_reserve.add_argument("--output", default="artifacts/live/options-reservation.json")
    option_reserve.set_defaults(func=command_option_reserve)

    option_sync = subparsers.add_parser(
        "option-sync",
        help="Persist option lifecycle changes after clean reconciliation",
    )
    option_sync.add_argument("--plan", default="artifacts/live/options-plan.json")
    option_sync.add_argument("--executed", required=True)
    option_sync.add_argument(
        "--reconciliation",
        default="artifacts/live/options-reconciliation.json",
    )
    option_sync.add_argument(
        "--equity-reconciliation",
        default="artifacts/live/reconciliation.json",
    )
    option_sync.add_argument("--root", default=".")
    option_sync.add_argument("--output", default="artifacts/live/options-sync.json")
    option_sync.set_defaults(func=command_option_sync)
    return parser


def main() -> None:
    load_runtime_env()
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))
