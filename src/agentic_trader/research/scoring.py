from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import ResearchBundle, ResearchEvent

SOURCE_QUALITY = {
    "sec_filing": 1.00,
    "government_record": 0.95,
    "regulatory_record": 0.90,
    "issuer_release": 0.85,
    "reputable_news": 0.65,
    "industry_source": 0.50,
}


@dataclass(frozen=True)
class EventScore:
    event_id: str
    ticker: str
    score: float
    eligible_for_event_study: bool
    evidence_quality: float
    directness: float
    relationship_confidence: float
    materiality: float
    magnitude: float
    novelty: float
    timing: float
    speculation_penalty: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def score_event(event: ResearchEvent, bundle: ResearchBundle) -> EventScore:
    evidence_by_id = bundle.evidence_by_id
    edge = bundle.dependencies_by_id[event.dependency_id]
    evidence = [evidence_by_id[item_id] for item_id in event.evidence_ids]
    qualities = [SOURCE_QUALITY[item.source_type] for item in evidence]
    quality = max(qualities) if qualities else 0.0
    independence = len({item.independence_group for item in evidence})
    if independence >= 2:
        quality = min(1.0, quality + 0.10)
    if not any(item.primary for item in evidence):
        quality = min(quality, 0.55)

    raw_score = (
        0.25 * quality
        + 0.10 * edge.directness
        + 0.15 * edge.relationship_confidence
        + 0.20 * edge.materiality_score
        + 0.15 * event.magnitude_score
        + 0.10 * event.novelty_score
        + 0.05 * event.timing_score
        - 0.25 * event.speculation_score
    )
    score = max(0.0, min(100.0, raw_score * 100))

    timely_edge_evidence = [
        evidence_by_id[item_id]
        for item_id in edge.evidence_ids
        if evidence_by_id[item_id].published_at <= event.published_at
    ]
    reasons: list[str] = []
    if not any(item.primary for item in evidence):
        reasons.append("no_primary_event_source")
    if not timely_edge_evidence:
        reasons.append("dependency_not_verified_as_of_event")
    if edge.directness < 0.75:
        reasons.append("dependency_too_indirect")
    if edge.relationship_confidence < 0.75:
        reasons.append("relationship_confidence_below_0_75")
    if edge.materiality_score < 0.20:
        reasons.append("materiality_below_0_20")
    if not event.quantified:
        reasons.append("event_not_quantified")
    if event.speculation_score > 0.40:
        reasons.append("speculation_above_0_40")
    if score < 65.0:
        reasons.append("score_below_65")

    return EventScore(
        event_id=event.id,
        ticker=event.ticker,
        score=score,
        eligible_for_event_study=not reasons,
        evidence_quality=quality,
        directness=edge.directness,
        relationship_confidence=edge.relationship_confidence,
        materiality=edge.materiality_score,
        magnitude=event.magnitude_score,
        novelty=event.novelty_score,
        timing=event.timing_score,
        speculation_penalty=event.speculation_score,
        reasons=tuple(reasons),
    )


def score_bundle(bundle: ResearchBundle) -> list[EventScore]:
    return [score_event(event, bundle) for event in bundle.events]
