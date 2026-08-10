from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from .analyzer import analyze_universe, write_analysis
from .config import StrategyConfig
from .data import download_adjusted_close
from .option_chain import (
    download_option_chain_snapshot,
    write_option_chain_snapshot,
)
from .options import OptionStructure, analyze_option_structure
from .proposal import ResearchProposal, validate_proposal
from .research.event_study import run_event_study, write_event_study
from .research.models import ResearchBundle
from .research.scoring import score_bundle
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))
