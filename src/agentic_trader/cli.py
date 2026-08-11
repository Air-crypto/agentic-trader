from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .analyzer import analyze_universe, write_analysis
from .config import StrategyConfig
from .data import download_adjusted_close
from .execution import (
    AccountSnapshot,
    ExecutionLimits,
    SessionLockedError,
    append_audit_record,
    broker_position_values,
    check_account_halts,
    daily_consumption,
    deterministic_ref_id,
    load_live_state,
    plan_orders_from_targets,
    record_live_state,
    record_plan_consumption,
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
from .picker.invalidation import trading_day_expiry, trading_days_until
from .picker.ledger import PostgresLedger, account_key
from .picker.models import (
    ActiveThesis,
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
    PickerDraft,
    QuantSnapshot,
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
from .sources.sec import SECClient
from .strategy import target_for_date
from .tournament import STRATEGIES, run_tournament
from .validation import validate_strategy


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


def command_live_plan(args: argparse.Namespace) -> int:
    """Produce a guarded real-money order plan. Never places an order itself."""
    try:
        with session_lock(args.root):
            return _live_plan(args)
    except SessionLockedError as error:
        print(json.dumps({"mode": "REFUSED", "reason": str(error)}, indent=2))
        return 3


def _live_plan(args: argparse.Namespace) -> int:
    request = json.loads(Path(args.request).read_text())
    raw_account = request["account"]
    prices = {str(k).upper(): float(v) for k, v in request["prices"].items()}

    # The drawdown and daily-loss halts read from persisted state rather than
    # the request so a caller cannot clear a halt by rewriting its own input.
    state = load_live_state(args.root)
    persisted_orders, persisted_notional = daily_consumption(args.root)
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
    if raw_orders is not None:
        broker_orders, broker_notional = summarize_broker_orders(raw_orders)
        orders_source = "broker"
    else:
        broker_orders = int(raw_account.get("orders_today", 0))
        broker_notional = float(raw_account.get("notional_today", 0.0))
        # A caller-provided label is not proof that the count came from the
        # broker. Only the raw response parsed above earns broker verification.
        orders_source = "unknown"

    equity = float(raw_account["equity"])
    picker_halts: list[str] = []
    database_high_water_mark: float | None = None
    option_reserved_cash = 0.0
    if bool(request.get("picker_mode", False)):
        configured_account = os.environ.get("AGENTIC_TRADER_ACCOUNT", "")
        if not configured_account or configured_account != str(raw_account["account_number"]):
            picker_halts.append("picker_account_configuration_mismatch")
        else:
            ledger = PostgresLedger.from_env()
            account_hash = account_key(configured_account)
            control = ledger.control_state(account_hash)
            if bool(control.get("halted")):
                picker_halts.append(
                    f"picker_database_halt:{control.get('halt_reason') or 'unspecified'}"
                )
            database_high_water_mark = ledger.record_equity_peak(account_hash, equity)
            valid_packets = {
                packet.packet_id: packet
                for packet in ledger.authorized_packets(datetime.now(UTC).date())
            }
            requested_ids = set(str(item) for item in request.get("authorization_packet_ids", []))
            if not requested_ids.issubset(valid_packets):
                picker_halts.append("picker_authorization_packet_missing_or_expired")
            active_theses = ledger.active_theses()
            active_option_positions = [
                position
                for position in ledger.option_positions()
                if position.status in {"pending_open", "open", "closing"}
            ]
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
            option_reserved_cash, option_halts = _option_equity_constraints(
                active_option_positions,
                prices,
                targets,
                equity,
            )
            picker_halts.extend(option_halts)
            for packet_id in requested_ids:
                packet = valid_packets.get(packet_id)
                if (
                    packet is not None
                    and packet.action == "buy"
                    and targets.get(packet.symbol, 0.0) > packet.target_weight + 1e-9
                ):
                    picker_halts.append(f"picker_target_exceeds_packet_weight:{packet.symbol}")

    account = AccountSnapshot(
        account_number=str(raw_account["account_number"]),
        equity=equity,
        cash=max(float(raw_account["cash"]) - option_reserved_cash, 0.0),
        positions=positions,
        high_water_mark=database_high_water_mark or state.get("high_water_mark"),
        prior_close_equity=state.get("prior_close_equity"),
        # Take the larger of the broker's count and what this repo approved
        # today, so a duplicate run cannot re-spend the daily budget whether or
        # not the other run's orders have reached the broker yet.
        orders_today=max(broker_orders, persisted_orders),
        notional_today=max(broker_notional, persisted_notional),
        pending_deposits=float(raw_account.get("pending_deposits", 0.0)),
        net_deposits=(
            float(raw_account["net_deposits"])
            if raw_account.get("net_deposits") is not None
            else None
        ),
        orders_source=orders_source,
        session_is_regular=bool(raw_account.get("session_is_regular", False)),
        external_halt_reasons=tuple(picker_halts),
    )
    limits = ExecutionLimits(
        max_order_notional=args.max_order_notional,
        max_position_weight=min(
            args.max_position_weight,
            float(request.get("max_position_weight", args.max_position_weight)),
        ),
        max_orders_per_day=args.max_orders_per_day,
        max_daily_notional=args.max_daily_notional,
        buy_symbol_allowlist=(
            tuple(str(item).upper() for item in request["buy_symbol_allowlist"])
            if "buy_symbol_allowlist" in request
            else None
        ),
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
    approved = [decision.to_dict() for decision in decisions if decision.approved]
    # Identity does not depend on the observed order count, so duplicate runs
    # derive the same key even when they queried at different session stages.
    for order in approved:
        order["ref_id"] = deterministic_ref_id(
            account.account_number,
            order["symbol"],
            order["side"],
            pick_id=str(order.get("pick_id") or ""),
            intent=str(order.get("intent_class") or "rebalance"),
        )
    payload = {
        "mode": "PLAN_ONLY_REQUIRES_HUMAN_APPROVAL",
        "account_number": account.account_number,
        "equity": equity,
        "prices": prices,
        "picker_mode": bool(request.get("picker_mode", False)),
        "orders_already_used_today": account.orders_today,
        "notional_already_used_today": account.notional_today,
        "authorization_packet_ids": list(request.get("authorization_packet_ids", [])),
        "halts": list(check_account_halts(account, limits, args.root)),
        "approved_orders": approved,
        "rejected_orders": [d.to_dict() for d in decisions if not d.approved],
        "note": "Approval means the order is within risk limits, not that it is profitable.",
    }
    if approved:
        record_plan_consumption(
            len(approved),
            sum(order["notional"] for order in approved),
            root=args.root,
        )
    if args.record_equity:
        record_live_state(equity, root=args.root)
    append_audit_record({"event": "live_plan", **payload}, root=args.root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0 if approved else 2


def command_live_reconcile(args: argparse.Namespace) -> int:
    """Compare executed fills to the approved plan; halt on anything unaccounted."""
    plan = json.loads(Path(args.plan).read_text())
    executed = json.loads(Path(args.executed).read_text())
    if isinstance(executed, dict):
        executed = executed.get("orders", [])
    result = reconcile(plan.get("approved_orders", []), executed, root=args.root)
    if (
        not result["clean"]
        and plan.get("authorization_packet_ids")
        and os.environ.get("DATABASE_URL")
        and os.environ.get("AGENTIC_TRADER_ACCOUNT")
    ):
        ledger = PostgresLedger.from_env()
        ledger.halt(
            account_key(os.environ["AGENTIC_TRADER_ACCOUNT"]),
            ";".join(str(item) for item in result["breaches"]),
        )
        result["database_halt_engaged"] = True
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["clean"] else 2


def _json_items(path: str, key: str) -> list[dict[str, object]]:
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        return raw
    values = raw.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{path} must be a list or contain a '{key}' list")
    return values


def command_picker_validate(args: argparse.Namespace) -> int:
    """Validate an AI draft and emit an immutable live DecisionPacket."""
    draft = PickerDraft.from_dict(json.loads(Path(args.draft).read_text()))
    critic = CriticVerdict.from_dict(json.loads(Path(args.critic).read_text()))
    evidence = [EvidenceVersion.from_dict(item) for item in _json_items(args.evidence, "evidence")]
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
    option_drafts = [
        OptionDraft.from_dict(item) for item in payload.get("option_drafts", [])
    ]
    critics = [CriticVerdict.from_dict(item) for item in payload["critics"]]
    critic_ids = {item.draft_id for item in critics}
    required_critic_ids = {
        item.source_draft_id or item.draft_id for item in option_drafts
    } | {item.draft_id for item in drafts}
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
    option_drafts = [
        OptionDraft.from_dict(item) for item in payload.get("option_drafts", [])
    ]
    all_drafts = [*drafts, *option_drafts]
    if not all_drafts or len({item.run_id for item in all_drafts}) != 1:
        raise ValueError("A pending research batch requires exactly one run_id")
    if any(item.created_at.date() != as_of for item in all_drafts):
        raise ValueError("Every pending draft must be created on the as_of date")
    if len(str(payload["prompt_hash"])) != 64:
        raise ValueError("prompt_hash must be a SHA-256 digest")
    return created_at, as_of, evidence, drafts, option_drafts


def command_picker_stage_pending(args: argparse.Namespace) -> int:
    """Stage analyst output for a separate Grok critic automation."""
    payload = json.loads(Path(args.bundle).read_text())
    created_at, as_of, _, drafts, option_drafts = _validate_pending_research_payload(
        payload
    )
    batch_id = str(payload["batch_id"])
    PostgresLedger.from_env().stage_pending_batch(
        batch_id,
        as_of,
        created_at,
        str(payload["prompt_hash"]),
        str(payload["model_id"]),
        payload,
    )
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
    output.write_text(json.dumps(batch["payload"], indent=2) + "\n")
    print(json.dumps({"exported": True, "batch_id": batch["batch_id"]}))
    return 0


def command_picker_finalize_pending(args: argparse.Namespace) -> int:
    """Attach real Grok verdicts and promote a pending batch to staged."""
    now = datetime.now(UTC)
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    ledger = PostgresLedger.from_env()
    pending = ledger.latest_pending_batch(as_of)
    if pending is None:
        print(json.dumps({"finalized": False, "reason": "no_pending_batch"}))
        return 2
    payload = dict(pending["payload"])
    created_at, _, evidence, drafts, option_drafts = (
        _validate_pending_research_payload(payload)
    )
    critics = [
        CriticVerdict.from_dict(item) for item in _json_items(args.critics, "critics")
    ]
    critic_ids = {item.draft_id for item in critics}
    required_ids = {item.draft_id for item in drafts} | {
        item.source_draft_id or item.draft_id for item in option_drafts
    }
    if critic_ids != required_ids:
        raise ValueError("Independent critics must cover every required draft exactly")
    analyst_model_id = str(pending["analyst_model_id"])
    if any(
        "grok" not in item.model_id.lower() or item.model_id == analyst_model_id
        for item in critics
    ):
        raise ValueError("Every critic must record an independent Grok model ID")
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
            "critic": "independent_grok",
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
    verified = []
    failures = []
    for raw in raw_items:
        evidence_id = str(raw["evidence_id"])
        path = documents / f"{evidence_id}.txt"
        if not path.exists():
            failures.append({"evidence_id": evidence_id, "reason": "document_missing"})
            continue
        document = path.read_text(errors="replace")
        grounded = SECClient.quote_is_grounded(document, str(raw["quote"]))
        if not grounded:
            failures.append({"evidence_id": evidence_id, "reason": "quote_not_grounded"})
            continue
        candidate = {
            **raw,
            "document_hash": content_hash(document),
            "quote_verified": True,
        }
        verified.append(EvidenceVersion.from_dict(candidate).to_dict())
    payload = {"evidence": verified, "failures": failures}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0 if not failures else 2


def command_picker_authorize_batch(args: argparse.Namespace) -> int:
    """Authorize today's latest staged batch using fresh execution-side quant data."""
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC).date()
    ledger = PostgresLedger.from_env()
    batch = ledger.latest_staged_batch(as_of)
    pending = ledger.latest_pending_batch(as_of)
    if pending is not None and (
        batch is None or pending["created_at"] >= batch["created_at"]
    ):
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
    evidence = {
        item.evidence_id: item
        for item in (EvidenceVersion.from_dict(raw) for raw in payload["evidence"])
    }
    critics = {
        item.draft_id: item
        for item in (CriticVerdict.from_dict(raw) for raw in payload["critics"])
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
    thesis_by_symbol = {thesis.symbol: thesis for thesis in theses}
    metadata: dict[str, dict[str, str | None]] = {}
    for symbol in plan.authorized_buy_symbols:
        packet = packet_by_symbol.get(symbol)
        thesis = thesis_by_symbol.get(symbol)
        metadata[symbol] = {
            "pick_id": packet.packet_id if packet is not None else thesis.pick_id if thesis else "",
            "intent_class": "entry" if packet is not None else "rebalance",
            "exit_reason": None,
        }
    for exit_intent in plan.exits:
        metadata[exit_intent.symbol] = {
            "pick_id": exit_intent.pick_id,
            "intent_class": "mandatory_exit",
            "exit_reason": exit_intent.reason,
        }

    broker_positions = account.get("broker_positions", [])
    held_symbols = {str(item["symbol"]).upper() for item in broker_positions}
    request = {
        **snapshot,
        "picker_mode": True,
        "targets": plan.targets,
        "buy_symbol_allowlist": list(plan.authorized_buy_symbols),
        "sell_symbol_allowlist": sorted(set(plan.authorized_sell_symbols) | held_symbols),
        "metadata_by_symbol": metadata,
        "authorization_packet_ids": list(plan.accepted_packet_ids),
        "max_position_weight": 0.15,
        "picker_plan": plan.to_dict(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(request, indent=2) + "\n")
    print(json.dumps({"output": str(output), **plan.to_dict()}, indent=2))
    return 0


def command_picker_sync(args: argparse.Namespace) -> int:
    """Persist pick lifecycle transitions only after broker fills reconcile cleanly."""
    plan = json.loads(Path(args.plan).read_text())
    reconciliation = json.loads(Path(args.reconciliation).read_text())
    if not bool(reconciliation.get("clean")):
        raise ValueError("Cannot sync picker state from a non-clean reconciliation")
    executed_raw = json.loads(Path(args.executed).read_text())
    executed = executed_raw.get("orders", []) if isinstance(executed_raw, dict) else executed_raw
    filled = {
        (str(item["symbol"]).upper(), str(item["side"]).lower()): item
        for item in executed
        if str(item.get("state", "")).lower() in {"filled", "partially_filled"}
    }
    ledger = PostgresLedger.from_env()
    packets = {
        packet.packet_id: packet for packet in ledger.authorized_packets(datetime.now(UTC).date())
    }
    active = {thesis.pick_id: thesis for thesis in ledger.active_theses()}
    spy_price = float(plan["prices"]["SPY"])
    transitions: list[dict[str, str]] = []

    for order in plan.get("approved_orders", []):
        pick_id = str(order.get("pick_id") or "")
        intent = str(order.get("intent_class") or "")
        fill = filled.get((str(order["symbol"]).upper(), str(order["side"]).lower()))
        if not pick_id or fill is None:
            continue
        if intent == "entry":
            packet = packets.get(pick_id)
            if packet is None:
                raise ValueError(f"Entry fill references unavailable packet {pick_id}")
            average_price = float(fill.get("average_price") or fill.get("price"))
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
            transitions.append({"pick_id": pick_id, "status": "active"})
        elif intent == "mandatory_exit":
            thesis = active.get(pick_id)
            if thesis is None:
                raise ValueError(f"Exit fill references unknown active thesis {pick_id}")
            ledger.upsert_thesis(replace(thesis, status="closed"))
            transitions.append({"pick_id": pick_id, "status": "closed"})

    payload = {"synced": True, "transitions": transitions}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
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
            position.shares_encumbered
            * prices.get(position.underlying, 0.0)
            / equity
            if equity > 0
            else 1.0
        )
        if targets.get(position.underlying, 0.0) + 1e-9 < encumbered_weight:
            halts.append(
                "covered_option_share_encumbrance_blocks_equity_sale:"
                f"{position.underlying}"
            )
    return reserved_cash, halts


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
    side = str(
        raw.get("type")
        or raw.get("position_type")
        or raw.get("side")
        or ""
    ).lower()
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
    if pending is not None and (
        batch is None or pending["created_at"] >= batch["created_at"]
    ):
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
    option_drafts = [
        OptionDraft.from_dict(item) for item in staged.get("option_drafts", [])
    ]
    if not option_drafts:
        payload = {"authorized": [], "results": [], "reason": "no_option_drafts"}
        print(json.dumps(payload))
        return 2

    snapshot, account = _option_snapshot(args.snapshot)
    configured_account = os.environ.get("AGENTIC_TRADER_ACCOUNT", "")
    if not configured_account or configured_account != str(account["account_number"]):
        raise ValueError("Option snapshot account does not match AGENTIC_TRADER_ACCOUNT")
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

    evidence = {
        item.evidence_id: item
        for item in (EvidenceVersion.from_dict(raw) for raw in staged["evidence"])
    }
    source_drafts = {
        item.draft_id: item
        for item in (PickerDraft.from_dict(raw) for raw in staged.get("drafts", []))
    }
    critics = {
        item.draft_id: item
        for item in (CriticVerdict.from_dict(raw) for raw in staged["critics"])
    }
    contracts = [
        OptionContractSnapshot.from_dict(raw)
        for raw in snapshot.get("contracts", [])
    ]
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
                encumbered.get(position.underlying, 0)
                + position.shares_encumbered
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
    """Apply all idempotent picker/options migrations in order."""
    paths = (
        [Path(args.path)]
        if args.path
        else sorted(Path("db/migrations").glob("*.sql"))
    )
    ledger = PostgresLedger.from_env()
    for path in paths:
        ledger.apply_migration(path)
    payload = {"applied": True, "migrations": [str(path) for path in paths]}
    print(json.dumps(payload))
    return 0


def _option_account_snapshot(
    raw: dict[str, object],
    positions: list[ActiveOptionPosition],
    planned_equity_orders: int = 0,
    persisted_orders_today: int = 0,
) -> OptionAccountSnapshot:
    broker_option_orders = raw.get("broker_option_orders")
    broker_equity_orders = raw.get("broker_equity_orders")
    if isinstance(broker_option_orders, list) and isinstance(
        broker_equity_orders, list
    ):
        openings, _ = summarize_broker_option_orders(broker_option_orders)
        equity_order_count, _ = summarize_broker_orders(broker_equity_orders)
        orders_today = max(
            equity_order_count + len(broker_option_orders) + planned_equity_orders,
            persisted_orders_today,
        )
        orders_source = "broker"
    else:
        openings = int(raw.get("option_openings_today", 0))
        orders_today = int(raw.get("orders_today", 0))
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
            (item.option_id, item.side, float(item.quantity))
            for item in open_positions
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
    configured_account = os.environ.get("AGENTIC_TRADER_ACCOUNT", "")
    if not configured_account or configured_account != str(raw_account["account_number"]):
        raise ValueError("Option snapshot account does not match AGENTIC_TRADER_ACCOUNT")

    ledger = PostgresLedger.from_env()
    control = ledger.control_state(account_key(configured_account))
    positions = ledger.option_positions()
    account_payload = dict(raw_account)
    halt_reasons = list(account_payload.get("halt_reasons", []))
    if bool(control.get("halted")):
        halt_reasons.append(
            f"picker_database_halt:{control.get('halt_reason') or 'unspecified'}"
        )
    account_payload["halt_reasons"] = halt_reasons
    planned_equity_orders = 0
    equity_plan_path = Path(args.equity_plan)
    if equity_plan_path.exists():
        equity_plan = json.loads(equity_plan_path.read_text())
        planned_equity_orders = len(equity_plan.get("approved_orders", []))
    persisted_orders_today, _ = daily_consumption(args.root)
    account = _option_account_snapshot(
        account_payload,
        positions,
        planned_equity_orders=planned_equity_orders,
        persisted_orders_today=persisted_orders_today,
    )
    packets = ledger.valid_option_packets(as_of, now)
    contracts = {
        item.option_id: item
        for item in (
            OptionContractSnapshot.from_dict(raw)
            for raw in snapshot.get("contracts", [])
        )
    }
    premium_stop_ids = _option_premium_stop_ids(positions, contracts)
    if premium_stop_ids:
        account = replace(
            account,
            mandatory_close_option_ids=tuple(
                sorted(set(account.mandatory_close_option_ids) | premium_stop_ids)
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
    payload = {
        "mode": "PLAN_ONLY_REQUIRES_HUMAN_APPROVAL",
        "account_number": configured_account,
        "authorization_packet_ids": sorted(
            {str(item["packet_id"]) for item in approved if item["packet_id"]}
        ),
        "approved_orders": approved,
        "rejected_orders": rejected,
        "halts": list(account.external_halt_reasons),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    if approved:
        record_plan_consumption(len(approved), 0.0, root=args.root)
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
        and os.environ.get("AGENTIC_TRADER_ACCOUNT")
    ):
        PostgresLedger.from_env().halt(
            account_key(os.environ["AGENTIC_TRADER_ACCOUNT"]),
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
    _, account = _option_snapshot(args.snapshot)
    configured_account = os.environ.get("AGENTIC_TRADER_ACCOUNT", "")
    if not configured_account or configured_account != str(account["account_number"]):
        raise ValueError("Option snapshot account does not match AGENTIC_TRADER_ACCOUNT")
    ledger = PostgresLedger.from_env()
    packets = {
        packet.packet_id: packet
        for packet in ledger.valid_option_packets(datetime.now(UTC).date())
    }
    available_cash = float(account["cash"]) - float(
        account.get("pending_deposits", 0.0)
    )
    broker_equity_positions = account.get("broker_equity_positions")
    if not isinstance(broker_equity_positions, list):
        raise ValueError("Option snapshot requires native broker_equity_positions")
    available_shares = {
        symbol: int(quantity)
        for symbol, quantity in _broker_equity_shares(
            broker_equity_positions
        ).items()
    }
    reserved: list[str] = []
    for order in plan.get("approved_orders", []):
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
            (
                {packet.underlying: packet.shares_encumbered}
                if packet.shares_encumbered
                else {}
            ),
            available_cash=available_cash,
            available_shares=available_shares,
        )
        reserved.append(packet.packet_id)
    payload = {"reserved": reserved}
    print(json.dumps(payload))
    return 0


def command_option_sync(args: argparse.Namespace) -> int:
    """Persist option lifecycle changes only after clean option reconciliation."""
    plan = json.loads(Path(args.plan).read_text())
    executed_raw = json.loads(Path(args.executed).read_text())
    executed_orders = (
        executed_raw.get("orders", [])
        if isinstance(executed_raw, dict)
        else executed_raw
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
    equity_reconciliation = json.loads(
        Path(args.equity_reconciliation).read_text()
    )
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
    if any(
        stored_reconciliation.get(field) != reconciliation.get(field)
        for field in bound_fields
    ):
        raise ValueError("Stored option reconciliation is stale or does not match fills")
    if not bool(reconciliation.get("clean")):
        raise ValueError("Cannot sync option state from incomplete reconciliation")
    filled_ref_ids = {
        str(item.get("ref_id") or item.get("client_order_id") or "")
        for item in executed_orders
        if str(item.get("state", "")).lower() == "filled"
    }
    matched = {
        str(item["ref_id"]): item for item in reconciliation.get("matched", [])
    }
    if not set(matched).issubset(filled_ref_ids):
        raise ValueError("Option reconciliation does not match the executed-order file")
    ledger = PostgresLedger.from_env()
    packet_ids = {
        str(item.get("packet_id") or "")
        for item in plan.get("approved_orders", [])
    }
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
                transitions.append(
                    {"position_id": packet.packet_id, "status": "cancelled"}
                )
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
                    if close_packet is not None
                    and close_packet.position_effect == "close"
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

    live_plan = subparsers.add_parser(
        "live-plan", help="Produce a risk-checked real-money order plan (does not place orders)"
    )
    live_plan.add_argument("--request", required=True, help="JSON with account, targets, prices")
    live_plan.add_argument("--root", default=".")
    live_plan.add_argument("--max-order-notional", type=float, default=150.0)
    live_plan.add_argument("--max-position-weight", type=float, default=0.25)
    live_plan.add_argument("--max-orders-per-day", type=int, default=4)
    live_plan.add_argument("--max-daily-notional", type=float, default=400.0)
    live_plan.add_argument("--rebalance-threshold", type=float, default=0.05)
    live_plan.add_argument("--record-equity", action="store_true")
    live_plan.add_argument("--output", default="artifacts/live/plan.json")
    live_plan.set_defaults(func=command_live_plan)

    live_reconcile = subparsers.add_parser(
        "live-reconcile", help="Verify fills against the approved plan and halt on breaches"
    )
    live_reconcile.add_argument("--plan", default="artifacts/live/plan.json")
    live_reconcile.add_argument("--executed", required=True, help="JSON list of executed orders")
    live_reconcile.add_argument("--root", default=".")
    live_reconcile.add_argument("--output", default="artifacts/live/reconciliation.json")
    live_reconcile.set_defaults(func=command_live_reconcile)

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

    picker_export_pending = subparsers.add_parser(
        "picker-export-pending",
        help="Export today's latest pending research batch for criticism",
    )
    picker_export_pending.add_argument("--as-of")
    picker_export_pending.add_argument(
        "--output", default="artifacts/picker/pending-research.json"
    )
    picker_export_pending.set_defaults(func=command_picker_export_pending)

    picker_finalize_pending = subparsers.add_parser(
        "picker-finalize-pending",
        help="Attach independent Grok critics and promote a pending batch",
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
    picker_verify.add_argument(
        "--output", default="artifacts/picker/verified-evidence.json"
    )
    picker_verify.set_defaults(func=command_picker_verify_evidence)

    picker_authorize = subparsers.add_parser(
        "picker-authorize-batch",
        help="Authorize today's staged batch using fresh broker-side quant snapshots",
    )
    picker_authorize.add_argument("--quant", required=True)
    picker_authorize.add_argument("--as-of")
    picker_authorize.add_argument(
        "--output", default="artifacts/picker/authorized-batch.json"
    )
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
    picker_sync.add_argument("--executed", required=True)
    picker_sync.add_argument("--reconciliation", default="artifacts/live/reconciliation.json")
    picker_sync.add_argument("--output", default="artifacts/live/picker-sync.json")
    picker_sync.set_defaults(func=command_picker_sync)

    option_authorize = subparsers.add_parser(
        "option-authorize-batch",
        help="Authorize staged Level 2 option drafts using live broker quotes",
    )
    option_authorize.add_argument("--snapshot", required=True)
    option_authorize.add_argument("--as-of")
    option_authorize.add_argument(
        "--output", default="artifacts/options/authorized.json"
    )
    option_authorize.set_defaults(func=command_option_authorize_batch)

    option_migrate = subparsers.add_parser(
        "option-migrate",
        help="Apply the idempotent Postgres migration for Level 2 options",
    )
    option_migrate.add_argument(
        "--path"
    )
    option_migrate.set_defaults(func=command_option_migrate)

    option_plan = subparsers.add_parser(
        "option-plan",
        help="Build a broker-ready plan from authorized Level 2 option packets",
    )
    option_plan.add_argument("--snapshot", required=True)
    option_plan.add_argument("--as-of")
    option_plan.add_argument("--root", default=".")
    option_plan.add_argument(
        "--equity-plan", default="artifacts/live/plan.json"
    )
    option_plan.add_argument("--output", default="artifacts/live/options-plan.json")
    option_plan.set_defaults(func=command_option_plan)

    option_reconcile = subparsers.add_parser(
        "option-reconcile",
        help="Verify option fills against the approved plan and halt on breaches",
    )
    option_reconcile.add_argument("--plan", default="artifacts/live/options-plan.json")
    option_reconcile.add_argument("--executed", required=True)
    option_reconcile.add_argument("--root", default=".")
    option_reconcile.add_argument(
        "--output", default="artifacts/live/options-reconciliation.json"
    )
    option_reconcile.set_defaults(func=command_option_reconcile)

    option_reserve = subparsers.add_parser(
        "option-reserve",
        help="Reserve covered shares or CSP cash immediately before placement",
    )
    option_reserve.add_argument("--plan", default="artifacts/live/options-plan.json")
    option_reserve.add_argument("--snapshot", required=True)
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
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))
