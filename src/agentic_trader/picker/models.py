from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

PICK_ACTIONS = {"long", "reject", "close"}
RESEARCH_MODEL_ID = "gpt-5.6-sol"
RESEARCH_REVIEW_MODE = "single_model_direct"
PACKET_ACTIONS = {"buy", "close"}
THESIS_STATUSES = {"pending_entry", "active", "expired", "invalidated", "closed", "cancelled"}
EVIDENCE_SOURCE_ALIASES = {
    "issuer_release": "issuer_primary",
    "reputable_news": "reputable_reporting",
}
EVIDENCE_AUTHORITIES = {
    "issuer_primary": "issuer",
    "exchange_notice": "exchange",
    "government_record": "government",
    "reputable_reporting": "reporting",
    "social_unverified": "social",
    # Kept parseable for historical records, but no longer live-authoritative.
    "sec_filing": "sec",
}


def parse_timestamp(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def unit_interval(name: str, value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return parsed


def positive(name: str, value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def finite_number(name: str, value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def nonnegative(name: str, value: Any) -> float:
    parsed = finite_number(name, value)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def strict_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_research_model_id(value: Any) -> str:
    """Return the sole supported research model ID or fail closed."""
    if value != RESEARCH_MODEL_ID:
        raise ValueError(f"model_id must be exactly {RESEARCH_MODEL_ID}")
    return RESEARCH_MODEL_ID


@dataclass(frozen=True)
class EvidenceVersion:
    evidence_id: str
    source_type: str
    title: str
    publisher: str
    url: str
    published_at: datetime
    first_seen_at: datetime
    retrieved_at: datetime
    quote: str
    document_hash: str
    primary: bool
    independence_group: str
    quote_verified: bool
    valid_at: datetime | None = None
    symbol: str = ""
    cik: str = ""
    authority: str = ""
    issuer_verified: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceVersion:
        published_at = parse_timestamp(raw["published_at"])
        first_seen_at = parse_timestamp(raw["first_seen_at"])
        retrieved_at = parse_timestamp(raw["retrieved_at"])
        valid_at = parse_timestamp(raw["valid_at"]) if raw.get("valid_at") else None
        url = str(raw["url"])
        quote = str(raw["quote"]).strip()
        if not url.startswith("https://"):
            raise ValueError("Evidence URLs must use HTTPS")
        if len(quote) < 20:
            raise ValueError("Evidence quote must contain at least 20 characters")
        if first_seen_at < published_at:
            raise ValueError("first_seen_at cannot precede published_at")
        if retrieved_at < first_seen_at:
            raise ValueError("retrieved_at cannot precede first_seen_at")
        document_hash = str(raw["document_hash"])
        if len(document_hash) != 64:
            raise ValueError("document_hash must be a SHA-256 hex digest")
        primary = raw["primary"]
        quote_verified = raw["quote_verified"]
        issuer_verified = raw.get("issuer_verified", False)
        if (
            not isinstance(primary, bool)
            or not isinstance(quote_verified, bool)
            or not isinstance(issuer_verified, bool)
        ):
            raise ValueError("Evidence boolean fields must be JSON booleans")
        cik = str(raw.get("cik", "")).strip()
        if cik and (not cik.isdigit() or len(cik) > 10):
            raise ValueError("Evidence CIK must contain at most 10 digits")
        source_type = EVIDENCE_SOURCE_ALIASES.get(
            str(raw["source_type"]).strip().lower(),
            str(raw["source_type"]).strip().lower(),
        )
        if source_type not in EVIDENCE_AUTHORITIES:
            raise ValueError(f"Unsupported evidence source type: {source_type}")
        host = urlparse(url).hostname or ""
        host = host.casefold()
        derived_authority = EVIDENCE_AUTHORITIES[source_type]
        if source_type == "sec_filing" and not (host == "sec.gov" or host.endswith(".sec.gov")):
            raise ValueError("SEC evidence must use an sec.gov URL")
        if source_type == "government_record" and (
            not host.endswith(".gov") or host == "sec.gov" or host.endswith(".sec.gov")
        ):
            raise ValueError("Government evidence must use a non-SEC .gov URL")
        if source_type in {"reputable_reporting", "social_unverified"} and primary:
            raise ValueError(f"{source_type} cannot be declared a primary source")
        if source_type in {"reputable_reporting", "social_unverified"} and issuer_verified:
            raise ValueError(f"{source_type} cannot be declared issuer-verified")
        declared_authority = str(raw.get("authority", derived_authority)).lower()
        if declared_authority and declared_authority != derived_authority:
            raise ValueError("Evidence authority does not match source URL/type")
        return cls(
            evidence_id=str(raw["evidence_id"]),
            source_type=source_type,
            title=str(raw["title"]),
            publisher=str(raw["publisher"]),
            url=url,
            published_at=published_at,
            first_seen_at=first_seen_at,
            retrieved_at=retrieved_at,
            quote=quote,
            document_hash=document_hash,
            primary=primary,
            independence_group=str(raw["independence_group"]),
            quote_verified=quote_verified,
            valid_at=valid_at,
            symbol=str(raw.get("symbol", "")).upper(),
            cik=cik.zfill(10) if cik else "",
            authority=derived_authority,
            issuer_verified=issuer_verified,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("published_at", "first_seen_at", "retrieved_at", "valid_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        return payload


@dataclass(frozen=True)
class QuantSnapshot:
    symbol: str
    as_of: datetime
    last_price: float
    market_cap: float
    average_dollar_volume: float
    spread_bps: float
    sector: str
    fractional_tradable: bool
    sufficient_history: bool
    momentum_rank: float
    quality_rank: float
    revisions_rank: float
    volatility_63d: float
    beta_252d: float
    atr_pct: float
    data_snapshot_hash: str = ""
    feature_version: str = ""
    calculated_by: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QuantSnapshot:
        return cls(
            symbol=str(raw["symbol"]).upper(),
            as_of=parse_timestamp(raw["as_of"]),
            last_price=positive("last_price", raw["last_price"]),
            market_cap=positive("market_cap", raw["market_cap"]),
            average_dollar_volume=positive("average_dollar_volume", raw["average_dollar_volume"]),
            spread_bps=nonnegative("spread_bps", raw["spread_bps"]),
            sector=str(raw["sector"]).strip() or "Unknown",
            fractional_tradable=strict_bool("fractional_tradable", raw["fractional_tradable"]),
            sufficient_history=strict_bool("sufficient_history", raw["sufficient_history"]),
            momentum_rank=unit_interval("momentum_rank", raw["momentum_rank"]),
            quality_rank=unit_interval("quality_rank", raw["quality_rank"]),
            revisions_rank=unit_interval("revisions_rank", raw["revisions_rank"]),
            volatility_63d=nonnegative("volatility_63d", raw["volatility_63d"]),
            beta_252d=finite_number("beta_252d", raw["beta_252d"]),
            atr_pct=positive("atr_pct", raw["atr_pct"]),
            data_snapshot_hash=str(raw.get("data_snapshot_hash", "")),
            feature_version=str(raw.get("feature_version", "")),
            calculated_by=str(raw.get("calculated_by", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "as_of": self.as_of.isoformat()}


@dataclass(frozen=True)
class PickerDraft:
    draft_id: str
    run_id: str
    created_at: datetime
    symbol: str
    action: str
    horizon_trading_days: int
    thesis: str
    catalyst: str
    materiality_basis: str
    novelty_basis: str
    priced_in_analysis: str
    counter_thesis: str
    invalidation: str
    evidence_ids: tuple[str, ...]
    event_quality: float
    materiality: float
    novelty: float
    timing: float
    speculation: float

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PickerDraft:
        action = str(raw["action"]).lower()
        if action not in PICK_ACTIONS:
            raise ValueError(f"Unsupported picker action: {action}")
        horizon = int(raw["horizon_trading_days"])
        if not 1 <= horizon <= 60:
            raise ValueError("Picker horizon must be between 1 and 60 trading days")
        evidence_ids = tuple(str(item) for item in raw.get("evidence_ids", ()))
        if action != "reject" and not evidence_ids:
            raise ValueError("Actionable drafts require evidence")
        thesis = str(raw["thesis"]).strip()
        if len(thesis) < 20:
            raise ValueError("Picker thesis is too short to be falsifiable")
        return cls(
            draft_id=str(raw["draft_id"]),
            run_id=str(raw["run_id"]),
            created_at=parse_timestamp(raw["created_at"]),
            symbol=str(raw["symbol"]).upper(),
            action=action,
            horizon_trading_days=horizon,
            thesis=thesis,
            catalyst=str(raw["catalyst"]),
            materiality_basis=str(raw["materiality_basis"]),
            novelty_basis=str(raw["novelty_basis"]),
            priced_in_analysis=str(raw["priced_in_analysis"]),
            counter_thesis=str(raw["counter_thesis"]),
            invalidation=str(raw["invalidation"]),
            evidence_ids=evidence_ids,
            event_quality=unit_interval("event_quality", raw["event_quality"]),
            materiality=unit_interval("materiality", raw["materiality"]),
            novelty=unit_interval("novelty", raw["novelty"]),
            timing=unit_interval("timing", raw["timing"]),
            speculation=unit_interval("speculation", raw["speculation"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "created_at": self.created_at.isoformat(),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class DecisionPacket:
    packet_id: str
    run_id: str
    draft_id: str
    created_at: datetime
    valid_for_date: date
    expires_at: datetime
    symbol: str
    action: str
    horizon_trading_days: int
    target_weight: float
    stop_loss_pct: float
    sector_relative_stop_pct: float
    sector: str
    rank_score: float
    thesis_hash: str
    evidence_ids: tuple[str, ...]
    prompt_hash: str
    model_id: str
    packet_hash: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["valid_for_date"] = self.valid_for_date.isoformat()
        payload["expires_at"] = self.expires_at.isoformat()
        payload["evidence_ids"] = list(self.evidence_ids)
        payload.pop("packet_hash", None)
        return payload

    def with_hash(self) -> DecisionPacket:
        return replace(self, packet_hash=content_hash(canonical_json(self.unsigned_dict())))

    def verify_hash(self) -> bool:
        return self.packet_hash == content_hash(canonical_json(self.unsigned_dict()))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], verify_hash: bool = True) -> DecisionPacket:
        action = str(raw["action"]).lower()
        if action not in PACKET_ACTIONS:
            raise ValueError(f"Unsupported packet action: {action}")
        target_weight = unit_interval("target_weight", raw["target_weight"])
        packet = cls(
            packet_id=str(raw["packet_id"]),
            run_id=str(raw["run_id"]),
            draft_id=str(raw["draft_id"]),
            created_at=parse_timestamp(raw["created_at"]),
            valid_for_date=parse_date(raw["valid_for_date"]),
            expires_at=parse_timestamp(raw["expires_at"]),
            symbol=str(raw["symbol"]).upper(),
            action=action,
            horizon_trading_days=int(raw["horizon_trading_days"]),
            target_weight=target_weight,
            stop_loss_pct=unit_interval("stop_loss_pct", raw["stop_loss_pct"]),
            sector_relative_stop_pct=unit_interval(
                "sector_relative_stop_pct", raw["sector_relative_stop_pct"]
            ),
            sector=str(raw["sector"]).strip() or "Unknown",
            rank_score=unit_interval("rank_score", raw["rank_score"]),
            thesis_hash=str(raw["thesis_hash"]),
            evidence_ids=tuple(str(item) for item in raw["evidence_ids"]),
            prompt_hash=str(raw["prompt_hash"]),
            model_id=str(raw["model_id"]),
            packet_hash=str(raw.get("packet_hash", "")),
        )
        if not 1 <= packet.horizon_trading_days <= 60:
            raise ValueError("Packet horizon must be between 1 and 60 trading days")
        if packet.expires_at <= packet.created_at:
            raise ValueError("Packet expires_at must follow created_at")
        if verify_hash and not packet.verify_hash():
            raise ValueError("Decision packet hash mismatch")
        return packet

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "packet_hash": self.packet_hash}


@dataclass(frozen=True)
class ActiveThesis:
    pick_id: str
    packet_id: str
    symbol: str
    sector: str
    status: str
    entry_date: date
    expiry_date: date
    entry_price: float
    entry_spy_price: float
    target_weight: float
    stop_loss_pct: float
    sector_relative_stop_pct: float

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ActiveThesis:
        status = str(raw["status"])
        if status not in THESIS_STATUSES:
            raise ValueError(f"Unsupported thesis status: {status}")
        return cls(
            pick_id=str(raw["pick_id"]),
            packet_id=str(raw["packet_id"]),
            symbol=str(raw["symbol"]).upper(),
            sector=str(raw["sector"]),
            status=status,
            entry_date=parse_date(raw["entry_date"]),
            expiry_date=parse_date(raw["expiry_date"]),
            entry_price=positive("entry_price", raw["entry_price"]),
            entry_spy_price=positive("entry_spy_price", raw["entry_spy_price"]),
            target_weight=unit_interval("target_weight", raw["target_weight"]),
            stop_loss_pct=unit_interval("stop_loss_pct", raw["stop_loss_pct"]),
            sector_relative_stop_pct=unit_interval(
                "sector_relative_stop_pct", raw["sector_relative_stop_pct"]
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "entry_date": self.entry_date.isoformat(),
            "expiry_date": self.expiry_date.isoformat(),
        }
