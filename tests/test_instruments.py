from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from agentic_trader.analyzer import analyze_universe
from agentic_trader.option_chain import normalize_option_chain
from agentic_trader.options import OptionStructure, analyze_option_structure
from agentic_trader.proposal import ResearchProposal, validate_proposal


def test_universe_analyzer_accepts_arbitrary_stock_and_leveraged_etf() -> None:
    index = pd.bdate_range("2024-01-02", periods=320)
    prices = pd.DataFrame(
        {
            "AAPL": 100 * np.cumprod(np.full(len(index), 1.0005)),
            "TQQQ": 100 * np.cumprod(np.full(len(index), 1.0010)),
            "SPY": 100 * np.cumprod(np.full(len(index), 1.0004)),
        },
        index=index,
    )

    analysis = analyze_universe(prices)

    assert set(analysis["symbol"]) == {"AAPL", "TQQQ"}
    assert analysis["sufficient_history"].all()
    assert analysis["return_12m"].notna().all()


def test_option_chain_snapshot_calculates_spread_and_intrinsic_value() -> None:
    common = {
        "lastPrice": [6.0],
        "bid": [5.8],
        "ask": [6.2],
        "impliedVolatility": [0.25],
        "volume": [100],
        "openInterest": [500],
        "lastTradeDate": ["2026-08-08T19:00:00Z"],
    }
    calls = pd.DataFrame({"contractSymbol": ["SPY_CALL"], "strike": [95.0], **common})
    puts = pd.DataFrame({"contractSymbol": ["SPY_PUT"], "strike": [105.0], **common})

    result = normalize_option_chain(
        calls,
        puts,
        symbol="SPY",
        spot=100.0,
        expiration="2026-09-18",
        retrieved_at=datetime(2026, 8, 9, 17, 0, tzinfo=UTC),
    )

    assert len(result) == 2
    assert np.allclose(result["spread"], 0.4)
    assert np.allclose(result["intrinsic_value"], 5.0)
    assert result["last_trade_age_hours"].gt(0).all()


def test_call_spread_has_bounded_loss_and_expected_payoff() -> None:
    structure = OptionStructure.from_dict(
        {
            "underlying": "AAPL",
            "spot": 100,
            "days_to_expiry": 30,
            "risk_free_rate": 0.04,
            "legs": [
                {
                    "kind": "call",
                    "side": "long",
                    "strike": 100,
                    "quantity": 1,
                    "premium": 5,
                    "implied_volatility": 0.30,
                },
                {
                    "kind": "call",
                    "side": "short",
                    "strike": 110,
                    "quantity": 1,
                    "premium": 2,
                    "implied_volatility": 0.30,
                },
            ],
        }
    )

    result = analyze_option_structure(structure)

    assert result["defined_risk"]
    assert result["maximum_loss"] == 300
    assert result["maximum_profit"] == 700
    assert result["breakevens_at_expiry"] == [103.0]


def test_naked_short_call_is_rejected_as_undefined_risk() -> None:
    structure = OptionStructure.from_dict(
        {
            "underlying": "AAPL",
            "spot": 100,
            "days_to_expiry": 30,
            "legs": [
                {
                    "kind": "call",
                    "side": "short",
                    "strike": 110,
                    "quantity": 1,
                    "premium": 2,
                    "implied_volatility": 0.30,
                }
            ],
        }
    )

    result = analyze_option_structure(structure)

    assert not result["defined_risk"]
    assert result["maximum_loss"] is None


def test_proposal_gate_rejects_oversized_leveraged_etf() -> None:
    proposal = ResearchProposal.from_dict(
        {
            "proposal_id": "test-proposal",
            "created_at": "2025-01-02T20:00:00Z",
            "thesis": "Leveraged trend continuation.",
            "horizon_days": 20,
            "legs": [
                {
                    "instrument_type": "leveraged_etf",
                    "symbol": "TQQQ",
                    "direction": "long",
                    "target_weight": 0.20,
                    "evidence_ids": ["evidence-1"],
                }
            ],
        }
    )
    analysis = pd.DataFrame([{"symbol": "TQQQ", "sufficient_history": True}])

    result = validate_proposal(proposal, analysis, capital=5_000, known_evidence_ids={"evidence-1"})

    assert not result["accepted_for_shadow_research"]
    assert "leg_0:leveraged_etf_weight_exceeds_10_percent" in result["reasons"]
