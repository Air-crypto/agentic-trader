from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_trader.research.event_study import run_event_study
from agentic_trader.research.models import ResearchBundle
from agentic_trader.research.scoring import score_bundle


def _bundle(evidence_date: str = "2024-01-05T12:00:00Z") -> dict[str, object]:
    return {
        "evidence": [
            {
                "id": "evidence-1",
                "source_type": "issuer_release",
                "title": "New customer capacity",
                "publisher": "Example Corp",
                "url": "https://example.com/release",
                "published_at": evidence_date,
                "retrieved_at": "2024-01-06T12:00:00Z",
                "quote": "The company doubled production under a signed customer agreement.",
                "primary": True,
                "independence_group": "example-corp",
            }
        ],
        "dependencies": [
            {
                "id": "edge-1",
                "driver_entity": "Private Customer",
                "public_company": "Example Corp",
                "ticker": "EXM",
                "relationship": "Supplies a critical production input.",
                "products": ["critical input"],
                "evidence_ids": ["evidence-1"],
                "directness": 0.9,
                "relationship_confidence": 0.9,
                "materiality_score": 0.4,
                "materiality_basis": "Quantified capacity doubled under contract.",
            }
        ],
        "events": [
            {
                "id": "event-1",
                "dependency_id": "edge-1",
                "ticker": "EXM",
                "event_type": "capacity_expansion",
                "summary": "Production capacity doubled under contract.",
                "published_at": "2024-01-05T12:00:00Z",
                "direction": 1,
                "horizon_days": 60,
                "evidence_ids": ["evidence-1"],
                "quantified": True,
                "magnitude_score": 0.8,
                "novelty_score": 0.8,
                "timing_score": 0.9,
                "speculation_score": 0.0,
            }
        ],
    }


def test_bundle_rejects_evidence_published_after_event() -> None:
    with pytest.raises(ValueError, match="published in the future"):
        ResearchBundle.from_dict(_bundle("2024-01-10T12:00:00Z"))


def test_primary_quantified_event_passes_scoring_gate() -> None:
    bundle = ResearchBundle.from_dict(_bundle())
    score = score_bundle(bundle)[0]

    assert score.eligible_for_event_study
    assert score.score >= 65


def test_event_study_enters_strictly_after_publication_date() -> None:
    bundle = ResearchBundle.from_dict(_bundle())
    index = pd.bdate_range("2024-01-02", periods=100)
    prices = pd.DataFrame(
        {
            "EXM": 100 * np.cumprod(np.full(len(index), 1.002)),
            "SPY": 100 * np.cumprod(np.full(len(index), 1.001)),
        },
        index=index,
    )

    result = run_event_study(bundle, prices)

    assert result.observations.iloc[0]["entry_date"] == "2024-01-08"
    assert result.status == "insufficient_evidence"
    assert not result.gates["at_least_30_eligible_events"]
