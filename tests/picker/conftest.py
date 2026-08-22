from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentic_trader.picker.models import (
    CriticVerdict,
    EvidenceVersion,
    PickerDraft,
    QuantSnapshot,
    content_hash,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def evidence(now: datetime) -> list[EvidenceVersion]:
    document = "The company raised annual revenue guidance by ten percent after new orders."
    common = {
        "source_type": "issuer_primary",
        "published_at": now - timedelta(hours=2),
        "first_seen_at": now - timedelta(hours=2),
        "retrieved_at": now - timedelta(hours=1),
        "quote": document,
        "document_hash": content_hash(document),
        "quote_verified": True,
        "symbol": "EXM",
        "cik": "1234567",
        "issuer_verified": True,
    }
    return [
        EvidenceVersion(
            evidence_id="issuer-1",
            title="Issuer results",
            publisher="Example issuer",
            url="https://investors.example.com/results",
            primary=True,
            authority="issuer",
            independence_group="issuer",
            **common,
        ),
        EvidenceVersion(
            evidence_id="gov-1",
            title="Award record",
            publisher="USAspending",
            url="https://www.usaspending.gov/example",
            primary=False,
            authority="government",
            independence_group="government",
            **{
                **common,
                "source_type": "government_record",
                "issuer_verified": False,
            },
        ),
    ]


@pytest.fixture
def quant(now: datetime) -> QuantSnapshot:
    return QuantSnapshot(
        symbol="EXM",
        as_of=now - timedelta(minutes=5),
        last_price=100.0,
        market_cap=10_000_000_000.0,
        average_dollar_volume=250_000_000.0,
        spread_bps=5.0,
        sector="Industrials",
        fractional_tradable=True,
        sufficient_history=True,
        momentum_rank=0.8,
        quality_rank=0.7,
        revisions_rank=0.9,
        volatility_63d=0.3,
        beta_252d=1.1,
        atr_pct=0.03,
        data_snapshot_hash="f" * 64,
        feature_version="picker_features_v1",
        calculated_by="agentic_trader.picker.features",
    )


@pytest.fixture
def draft(now: datetime) -> PickerDraft:
    return PickerDraft(
        draft_id="draft-1",
        run_id="run-1",
        created_at=now - timedelta(minutes=20),
        symbol="EXM",
        action="long",
        horizon_trading_days=20,
        thesis="Raised guidance and independently verified orders should reprice earnings.",
        catalyst="Guidance increase and new funded order",
        materiality_basis="Ten percent revenue guidance increase",
        novelty_basis="First disclosure in the current session",
        priced_in_analysis="Shares moved less than one percent after filing",
        counter_thesis="Orders could pull revenue forward",
        invalidation="Guidance withdrawn or relative stop breached",
        evidence_ids=("issuer-1", "gov-1"),
        event_quality=0.9,
        materiality=0.7,
        novelty=0.8,
        timing=0.9,
        speculation=0.1,
    )


@pytest.fixture
def critic(now: datetime) -> CriticVerdict:
    return CriticVerdict(
        draft_id="draft-1",
        model_id="cursor-grok-4.5-high-fast",
        created_at=now - timedelta(minutes=5),
        verdict="pass",
        reasons=(),
        contradicted_evidence_ids=(),
        hard_vetoes=(),
        soft_checks=(
            ("freshness", True),
            ("materiality", True),
            ("not_priced_in", True),
            ("novelty", True),
            ("source_breadth", True),
        ),
    )
