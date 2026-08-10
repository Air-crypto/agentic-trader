from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ALLOWED_SOURCE_TYPES = {
    "sec_filing",
    "government_record",
    "issuer_release",
    "regulatory_record",
    "reputable_news",
    "industry_source",
}


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed


def _unit_interval(name: str, value: float) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


@dataclass(frozen=True)
class Evidence:
    id: str
    source_type: str
    title: str
    publisher: str
    url: str
    published_at: datetime
    retrieved_at: datetime
    quote: str
    primary: bool
    independence_group: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Evidence:
        source_type = str(raw["source_type"])
        if source_type not in ALLOWED_SOURCE_TYPES:
            raise ValueError(f"Unsupported source type: {source_type}")
        url = str(raw["url"])
        if not url.startswith("https://"):
            raise ValueError("Evidence URLs must use HTTPS")
        quote = str(raw["quote"]).strip()
        if len(quote) < 20:
            raise ValueError("Evidence quotes must contain at least 20 characters")
        return cls(
            id=str(raw["id"]),
            source_type=source_type,
            title=str(raw["title"]),
            publisher=str(raw["publisher"]),
            url=url,
            published_at=_timestamp(str(raw["published_at"])),
            retrieved_at=_timestamp(str(raw["retrieved_at"])),
            quote=quote,
            primary=bool(raw["primary"]),
            independence_group=str(raw["independence_group"]),
        )


@dataclass(frozen=True)
class DependencyEdge:
    id: str
    driver_entity: str
    public_company: str
    ticker: str
    relationship: str
    products: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    directness: float
    relationship_confidence: float
    materiality_score: float
    materiality_basis: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DependencyEdge:
        return cls(
            id=str(raw["id"]),
            driver_entity=str(raw["driver_entity"]),
            public_company=str(raw["public_company"]),
            ticker=str(raw["ticker"]).upper(),
            relationship=str(raw["relationship"]),
            products=tuple(str(value) for value in raw["products"]),
            evidence_ids=tuple(str(value) for value in raw["evidence_ids"]),
            directness=_unit_interval("directness", raw["directness"]),
            relationship_confidence=_unit_interval(
                "relationship_confidence", raw["relationship_confidence"]
            ),
            materiality_score=_unit_interval("materiality_score", raw["materiality_score"]),
            materiality_basis=str(raw["materiality_basis"]),
        )


@dataclass(frozen=True)
class ResearchEvent:
    id: str
    dependency_id: str
    ticker: str
    event_type: str
    summary: str
    published_at: datetime
    direction: int
    horizon_days: int
    evidence_ids: tuple[str, ...]
    quantified: bool
    magnitude_score: float
    novelty_score: float
    timing_score: float
    speculation_score: float

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchEvent:
        direction = int(raw["direction"])
        if direction not in {-1, 1}:
            raise ValueError("Event direction must be -1 or 1")
        horizon_days = int(raw["horizon_days"])
        if not 1 <= horizon_days <= 252:
            raise ValueError("Event horizon must be between 1 and 252 trading days")
        return cls(
            id=str(raw["id"]),
            dependency_id=str(raw["dependency_id"]),
            ticker=str(raw["ticker"]).upper(),
            event_type=str(raw["event_type"]),
            summary=str(raw["summary"]),
            published_at=_timestamp(str(raw["published_at"])),
            direction=direction,
            horizon_days=horizon_days,
            evidence_ids=tuple(str(value) for value in raw["evidence_ids"]),
            quantified=bool(raw["quantified"]),
            magnitude_score=_unit_interval("magnitude_score", raw["magnitude_score"]),
            novelty_score=_unit_interval("novelty_score", raw["novelty_score"]),
            timing_score=_unit_interval("timing_score", raw["timing_score"]),
            speculation_score=_unit_interval("speculation_score", raw["speculation_score"]),
        )


@dataclass(frozen=True)
class ResearchBundle:
    evidence: tuple[Evidence, ...]
    dependencies: tuple[DependencyEdge, ...]
    events: tuple[ResearchEvent, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchBundle:
        bundle = cls(
            evidence=tuple(Evidence.from_dict(value) for value in raw["evidence"]),
            dependencies=tuple(DependencyEdge.from_dict(value) for value in raw["dependencies"]),
            events=tuple(ResearchEvent.from_dict(value) for value in raw["events"]),
        )
        bundle.validate_references()
        return bundle

    @classmethod
    def from_path(cls, path: str | Path) -> ResearchBundle:
        return cls.from_dict(json.loads(Path(path).read_text()))

    def validate_references(self) -> None:
        evidence = {item.id: item for item in self.evidence}
        dependencies = {item.id: item for item in self.dependencies}
        if len(evidence) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique")
        if len(dependencies) != len(self.dependencies):
            raise ValueError("Dependency IDs must be unique")
        if len({item.id for item in self.events}) != len(self.events):
            raise ValueError("Event IDs must be unique")

        for edge in self.dependencies:
            unknown = set(edge.evidence_ids) - evidence.keys()
            if unknown:
                raise ValueError(f"Dependency {edge.id} has unknown evidence: {unknown}")

        for event in self.events:
            if event.dependency_id not in dependencies:
                raise ValueError(f"Unknown dependency for event {event.id}")
            edge = dependencies[event.dependency_id]
            if event.ticker != edge.ticker:
                raise ValueError(f"Ticker mismatch for event {event.id}")
            unknown = set(event.evidence_ids) - evidence.keys()
            if unknown:
                raise ValueError(f"Event {event.id} has unknown evidence: {unknown}")
            future_evidence = [
                evidence[item_id].id
                for item_id in event.evidence_ids
                if evidence[item_id].published_at > event.published_at
            ]
            if future_evidence:
                raise ValueError(
                    f"Event {event.id} uses evidence published in the future: {future_evidence}"
                )

    @property
    def evidence_by_id(self) -> dict[str, Evidence]:
        return {item.id: item for item in self.evidence}

    @property
    def dependencies_by_id(self) -> dict[str, DependencyEdge]:
        return {item.id: item for item in self.dependencies}
