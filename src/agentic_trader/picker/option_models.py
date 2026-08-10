from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from .models import canonical_json, content_hash, parse_date, parse_timestamp

OPTION_ACTIONS = {
    "long_call",
    "long_put",
    "covered_call",
    "cash_secured_put",
    "close",
    "reject",
}
OPTION_TYPES = {"call", "put"}
OPTION_POSITION_STATUSES = {
    "pending_open",
    "open",
    "closing",
    "closed",
    "assigned",
    "exercised",
    "expired",
    "halted",
}


def _positive(name: str, value: Any, *, allow_zero: bool = False) -> float:
    parsed = float(value)
    if parsed < 0 if allow_zero else parsed <= 0:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return parsed


def _verify_supplied_hash(name: str, supplied: str | None, actual: str) -> None:
    if supplied and supplied != actual:
        raise ValueError(f"{name} hash mismatch")


@dataclass(frozen=True)
class OptionDraft:
    """Untrusted option intent; contract choice deliberately remains absent."""

    draft_id: str
    run_id: str
    created_at: datetime
    underlying: str
    action: str
    thesis: str
    evidence_ids: tuple[str, ...]
    source_draft_id: str | None = None
    position_id: str | None = None
    contract_id: str | None = None
    horizon_trading_days: int = 20
    catalyst: str = ""
    counter_thesis: str = ""
    invalidation: str = ""
    draft_hash: str = ""

    def __post_init__(self) -> None:
        created_at = parse_timestamp(self.created_at)
        action = str(self.action).lower()
        underlying = str(self.underlying).upper()
        evidence_ids = tuple(str(item) for item in self.evidence_ids)
        if action not in OPTION_ACTIONS:
            raise ValueError(f"Unsupported option action: {action}")
        thesis = str(self.thesis).strip()
        if action != "reject" and len(thesis) < 20:
            raise ValueError("Option thesis is too short to be falsifiable")
        if action not in {"close", "reject"} and not evidence_ids:
            raise ValueError("Opening option drafts require evidence")
        if action != "close" and (self.position_id or self.contract_id):
            raise ValueError("Only close drafts may name an existing position or contract")
        horizon = int(self.horizon_trading_days)
        if not 1 <= horizon <= 60:
            raise ValueError("Option horizon must be between 1 and 60 trading days")
        if action not in {"close", "reject"} and self.source_draft_id is None:
            if not all(
                str(value).strip()
                for value in (self.catalyst, self.counter_thesis, self.invalidation)
            ):
                raise ValueError(
                    "Standalone option drafts require catalyst, counter-thesis, "
                    "and invalidation"
                )
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "thesis", thesis)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "horizon_trading_days", horizon)
        object.__setattr__(self, "catalyst", str(self.catalyst).strip())
        object.__setattr__(self, "counter_thesis", str(self.counter_thesis).strip())
        object.__setattr__(self, "invalidation", str(self.invalidation).strip())
        if not self.draft_hash:
            object.__setattr__(
                self,
                "draft_hash",
                content_hash(canonical_json(self.unsigned_dict())),
            )

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.astimezone(UTC).isoformat()
        payload["evidence_ids"] = list(self.evidence_ids)
        payload.pop("draft_hash", None)
        return payload

    def with_hash(self) -> OptionDraft:
        return replace(self, draft_hash=content_hash(canonical_json(self.unsigned_dict())))

    def verify_hash(self) -> bool:
        return self.draft_hash == content_hash(canonical_json(self.unsigned_dict()))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], verify_hash: bool = True) -> OptionDraft:
        supplied_hash = str(raw.get("draft_hash", ""))
        draft = cls(
            draft_id=str(raw["draft_id"]),
            run_id=str(raw["run_id"]),
            created_at=parse_timestamp(raw["created_at"]),
            underlying=str(raw.get("underlying", raw.get("symbol", ""))),
            action=str(raw["action"]),
            thesis=str(raw["thesis"]),
            evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
            source_draft_id=(
                str(raw["source_draft_id"]) if raw.get("source_draft_id") is not None else None
            ),
            position_id=str(raw["position_id"]) if raw.get("position_id") is not None else None,
            contract_id=str(raw["contract_id"]) if raw.get("contract_id") is not None else None,
            horizon_trading_days=int(raw.get("horizon_trading_days", 20)),
            catalyst=str(raw.get("catalyst", "")),
            counter_thesis=str(raw.get("counter_thesis", "")),
            invalidation=str(raw.get("invalidation", "")),
            draft_hash=supplied_hash,
        )
        if verify_hash:
            _verify_supplied_hash("Option draft", supplied_hash, draft.with_hash().draft_hash)
        return draft.with_hash() if not supplied_hash else draft

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "draft_hash": self.draft_hash}


@dataclass(frozen=True)
class OptionContractSnapshot:
    """Execution-quality, broker-native contract and quote snapshot."""

    option_id: str
    contract_symbol: str
    underlying: str
    option_type: str
    expiration_date: date
    strike: float
    bid: float
    ask: float
    quote_at: datetime
    underlying_price: float
    delta: float | None = None
    open_interest: int | None = None
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        option_type = str(self.option_type).lower()
        if option_type not in OPTION_TYPES:
            raise ValueError("Option type must be call or put")
        expiration_date = parse_date(self.expiration_date)
        quote_at = parse_timestamp(self.quote_at)
        strike = _positive("strike", self.strike)
        bid = _positive("bid", self.bid, allow_zero=True)
        ask = _positive("ask", self.ask, allow_zero=True)
        underlying_price = _positive("underlying_price", self.underlying_price)
        if ask < bid:
            raise ValueError("Option ask cannot be below bid")
        delta = float(self.delta) if self.delta is not None else None
        if delta is not None and not -1.0 <= delta <= 1.0:
            raise ValueError("Option delta must be between -1 and 1")
        open_interest = int(self.open_interest) if self.open_interest is not None else None
        if open_interest is not None and open_interest < 0:
            raise ValueError("Option open interest cannot be negative")
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(self, "expiration_date", expiration_date)
        object.__setattr__(self, "quote_at", quote_at)
        object.__setattr__(self, "strike", strike)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "underlying_price", underlying_price)
        object.__setattr__(self, "underlying", str(self.underlying).upper())
        object.__setattr__(self, "delta", delta)
        object.__setattr__(self, "open_interest", open_interest)
        if not self.snapshot_hash:
            object.__setattr__(
                self,
                "snapshot_hash",
                content_hash(canonical_json(self.unsigned_dict())),
            )

    @property
    def contract_id(self) -> str:
        return self.option_id

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct_midpoint(self) -> float:
        midpoint = self.midpoint
        return (self.ask - self.bid) / midpoint if midpoint > 0 else float("inf")

    @property
    def contract_fingerprint(self) -> str:
        identity = {
            "option_id": self.option_id,
            "underlying": self.underlying,
            "option_type": self.option_type,
            "expiration_date": self.expiration_date.isoformat(),
            "strike": self.strike,
        }
        return content_hash(canonical_json(identity))

    def days_to_expiration(self, as_of: date | datetime) -> int:
        current_date = as_of.date() if isinstance(as_of, datetime) else as_of
        return (self.expiration_date - current_date).days

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expiration_date"] = self.expiration_date.isoformat()
        payload["quote_at"] = self.quote_at.astimezone(UTC).isoformat()
        payload.pop("snapshot_hash", None)
        return payload

    def with_hash(self) -> OptionContractSnapshot:
        return replace(self, snapshot_hash=content_hash(canonical_json(self.unsigned_dict())))

    def verify_hash(self) -> bool:
        return self.snapshot_hash == content_hash(canonical_json(self.unsigned_dict()))

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        verify_hash: bool = True,
    ) -> OptionContractSnapshot:
        supplied_hash = str(raw.get("snapshot_hash", ""))
        option_id = raw.get("option_id", raw.get("instrument_id", raw.get("id")))
        contract_symbol = raw.get(
            "contract_symbol",
            raw.get("symbol", raw.get("contractSymbol", option_id)),
        )
        snapshot = cls(
            option_id=str(option_id or ""),
            contract_symbol=str(contract_symbol or ""),
            underlying=str(
                raw.get("underlying", raw.get("chain_symbol", raw.get("chainSymbol", "")))
            ),
            option_type=str(raw.get("option_type", raw.get("type", raw.get("kind", "")))),
            expiration_date=parse_date(
                raw.get("expiration_date", raw.get("expiration", ""))
            ),
            strike=float(raw.get("strike", raw.get("strike_price"))),
            bid=float(raw.get("bid", raw.get("bid_price"))),
            ask=float(raw.get("ask", raw.get("ask_price"))),
            quote_at=parse_timestamp(
                raw.get("quote_at", raw.get("updated_at", raw.get("retrieved_at")))
            ),
            underlying_price=float(
                raw.get(
                    "underlying_price",
                    raw.get("underlying_spot", raw.get("spot")),
                )
            ),
            delta=float(raw["delta"]) if raw.get("delta") is not None else None,
            open_interest=(
                int(raw.get("open_interest", raw.get("openInterest")))
                if raw.get("open_interest", raw.get("openInterest")) is not None
                else None
            ),
            snapshot_hash=supplied_hash,
        )
        if verify_hash:
            _verify_supplied_hash(
                "Option contract snapshot",
                supplied_hash,
                snapshot.with_hash().snapshot_hash,
            )
        return snapshot.with_hash() if not supplied_hash else snapshot

    @classmethod
    def from_broker_dict(cls, raw: dict[str, Any]) -> OptionContractSnapshot:
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "snapshot_hash": self.snapshot_hash}


@dataclass(frozen=True)
class OptionDecisionPacket:
    packet_id: str
    run_id: str
    draft_id: str
    created_at: datetime
    valid_for_date: date
    expires_at: datetime
    underlying: str
    action: str
    contract: OptionContractSnapshot
    quantity: int
    side: str
    position_effect: str
    limit_price: float
    max_risk: float
    collateral_required: float
    shares_encumbered: int
    evidence_ids: tuple[str, ...]
    prompt_hash: str
    model_id: str
    draft_hash: str
    horizon_trading_days: int
    invalidation: str
    structure_fingerprint: str = ""
    packet_hash: str = ""

    def __post_init__(self) -> None:
        action = str(self.action).lower()
        if action not in OPTION_ACTIONS - {"reject"}:
            raise ValueError(f"Unsupported packet action: {action}")
        side = str(self.side).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("Option packet side must be buy or sell")
        position_effect = str(self.position_effect).lower()
        if position_effect not in {"open", "close"}:
            raise ValueError("Position effect must be open or close")
        if (action == "close") != (position_effect == "close"):
            raise ValueError("Close action and position effect must agree")
        if int(self.quantity) != 1:
            raise ValueError("Option packets must authorize exactly one contract")
        if len(self.draft_hash) != 64:
            raise ValueError("Option packet draft_hash must be a SHA-256 digest")
        horizon = int(self.horizon_trading_days)
        if not 1 <= horizon <= 60:
            raise ValueError("Option packet horizon must be between 1 and 60 trading days")
        if not str(self.invalidation).strip():
            raise ValueError("Option packet requires a measurable invalidation")
        created_at = parse_timestamp(self.created_at)
        expires_at = parse_timestamp(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("Option packet expires_at must follow created_at")
        if not self.contract.verify_hash():
            raise ValueError("Option packet contains an invalid contract snapshot hash")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "position_effect", position_effect)
        object.__setattr__(self, "quantity", 1)
        object.__setattr__(self, "horizon_trading_days", horizon)
        object.__setattr__(self, "invalidation", str(self.invalidation).strip())
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "valid_for_date", parse_date(self.valid_for_date))
        object.__setattr__(self, "underlying", str(self.underlying).upper())
        object.__setattr__(self, "limit_price", _positive("limit_price", self.limit_price))
        object.__setattr__(self, "max_risk", _positive("max_risk", self.max_risk, allow_zero=True))
        object.__setattr__(
            self,
            "collateral_required",
            _positive("collateral_required", self.collateral_required, allow_zero=True),
        )
        if int(self.shares_encumbered) < 0:
            raise ValueError("shares_encumbered cannot be negative")
        object.__setattr__(self, "shares_encumbered", int(self.shares_encumbered))
        object.__setattr__(self, "evidence_ids", tuple(str(item) for item in self.evidence_ids))
        if not self.structure_fingerprint:
            object.__setattr__(
                self,
                "structure_fingerprint",
                content_hash(canonical_json(self.structure_dict())),
            )
        if not self.packet_hash:
            object.__setattr__(
                self,
                "packet_hash",
                content_hash(canonical_json(self.unsigned_dict())),
            )

    @property
    def strategy(self) -> str:
        return self.action

    @property
    def option_id(self) -> str:
        return self.contract.option_id

    @property
    def order_type(self) -> str:
        return "limit"

    @property
    def time_in_force(self) -> str:
        return "gfd"

    @property
    def session(self) -> str:
        return "regular"

    def structure_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "contract_fingerprint": self.contract.contract_fingerprint,
            "quantity": self.quantity,
            "side": self.side,
            "position_effect": self.position_effect,
        }

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.astimezone(UTC).isoformat()
        payload["valid_for_date"] = self.valid_for_date.isoformat()
        payload["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        payload["contract"] = self.contract.to_dict()
        payload["evidence_ids"] = list(self.evidence_ids)
        payload["order_type"] = self.order_type
        payload["time_in_force"] = self.time_in_force
        payload["session"] = self.session
        payload.pop("packet_hash", None)
        return payload

    def with_hash(self) -> OptionDecisionPacket:
        return replace(self, packet_hash=content_hash(canonical_json(self.unsigned_dict())))

    def verify_hash(self) -> bool:
        return (
            self.contract.verify_hash()
            and self.structure_fingerprint
            == content_hash(canonical_json(self.structure_dict()))
            and self.packet_hash == content_hash(canonical_json(self.unsigned_dict()))
        )

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        verify_hash: bool = True,
    ) -> OptionDecisionPacket:
        supplied_hash = str(raw.get("packet_hash", ""))
        packet = cls(
            packet_id=str(raw["packet_id"]),
            run_id=str(raw["run_id"]),
            draft_id=str(raw["draft_id"]),
            created_at=parse_timestamp(raw["created_at"]),
            valid_for_date=parse_date(raw["valid_for_date"]),
            expires_at=parse_timestamp(raw["expires_at"]),
            underlying=str(raw.get("underlying", raw.get("symbol", ""))),
            action=str(raw.get("action", raw.get("strategy", ""))),
            contract=(
                raw["contract"]
                if isinstance(raw["contract"], OptionContractSnapshot)
                else OptionContractSnapshot.from_dict(raw["contract"])
            ),
            quantity=int(raw.get("quantity", 1)),
            side=str(raw["side"]),
            position_effect=str(raw["position_effect"]),
            limit_price=float(raw["limit_price"]),
            max_risk=float(raw.get("max_risk", 0.0)),
            collateral_required=float(raw.get("collateral_required", 0.0)),
            shares_encumbered=int(raw.get("shares_encumbered", 0)),
            evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
            prompt_hash=str(raw["prompt_hash"]),
            model_id=str(raw["model_id"]),
            draft_hash=str(raw["draft_hash"]),
            horizon_trading_days=int(raw["horizon_trading_days"]),
            invalidation=str(raw["invalidation"]),
            structure_fingerprint=str(raw.get("structure_fingerprint", "")),
            packet_hash=supplied_hash,
        )
        if verify_hash:
            _verify_supplied_hash(
                "Option decision packet",
                supplied_hash,
                packet.with_hash().packet_hash,
            )
            if not packet.verify_hash():
                raise ValueError("Option decision packet structure fingerprint mismatch")
        return packet.with_hash() if not supplied_hash else packet

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "packet_hash": self.packet_hash}


@dataclass(frozen=True)
class ActiveOptionPosition:
    position_id: str
    packet_id: str
    underlying: str
    strategy: str
    option_id: str
    contract_symbol: str
    option_type: str
    expiration_date: date
    strike: float
    quantity: int
    side: str
    opened_at: datetime
    average_open_price: float
    premium_at_risk: float
    collateral_reserved: float
    shares_encumbered: int
    status: str
    structure_fingerprint: str
    position_hash: str = ""

    def __post_init__(self) -> None:
        strategy = str(self.strategy).lower()
        if strategy not in OPTION_ACTIONS - {"close", "reject"}:
            raise ValueError(f"Unsupported option position strategy: {strategy}")
        option_type = str(self.option_type).lower()
        if option_type not in OPTION_TYPES:
            raise ValueError("Option type must be call or put")
        side = str(self.side).lower()
        if side not in {"long", "short"}:
            raise ValueError("Option position side must be long or short")
        status = str(self.status).lower()
        if status not in OPTION_POSITION_STATUSES:
            raise ValueError(f"Unsupported option position status: {status}")
        if int(self.quantity) != 1:
            raise ValueError("Active option positions must contain exactly one contract")
        if int(self.shares_encumbered) < 0:
            raise ValueError("shares_encumbered cannot be negative")
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "underlying", str(self.underlying).upper())
        object.__setattr__(self, "expiration_date", parse_date(self.expiration_date))
        object.__setattr__(self, "opened_at", parse_timestamp(self.opened_at))
        object.__setattr__(self, "strike", _positive("strike", self.strike))
        object.__setattr__(
            self,
            "average_open_price",
            _positive("average_open_price", self.average_open_price, allow_zero=True),
        )
        object.__setattr__(
            self,
            "premium_at_risk",
            _positive("premium_at_risk", self.premium_at_risk, allow_zero=True),
        )
        object.__setattr__(
            self,
            "collateral_reserved",
            _positive("collateral_reserved", self.collateral_reserved, allow_zero=True),
        )
        object.__setattr__(self, "quantity", 1)
        object.__setattr__(self, "shares_encumbered", int(self.shares_encumbered))
        if not self.position_hash:
            object.__setattr__(
                self,
                "position_hash",
                content_hash(canonical_json(self.unsigned_dict())),
            )

    @property
    def contract_id(self) -> str:
        return self.option_id

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expiration_date"] = self.expiration_date.isoformat()
        payload["opened_at"] = self.opened_at.astimezone(UTC).isoformat()
        payload.pop("position_hash", None)
        return payload

    def with_hash(self) -> ActiveOptionPosition:
        return replace(self, position_hash=content_hash(canonical_json(self.unsigned_dict())))

    def verify_hash(self) -> bool:
        return self.position_hash == content_hash(canonical_json(self.unsigned_dict()))

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        verify_hash: bool = True,
    ) -> ActiveOptionPosition:
        supplied_hash = str(raw.get("position_hash", ""))
        position = cls(
            position_id=str(raw["position_id"]),
            packet_id=str(raw["packet_id"]),
            underlying=str(raw["underlying"]),
            strategy=str(raw["strategy"]),
            option_id=str(raw.get("option_id", raw.get("contract_id", ""))),
            contract_symbol=str(raw.get("contract_symbol", "")),
            option_type=str(raw["option_type"]),
            expiration_date=parse_date(raw["expiration_date"]),
            strike=float(raw["strike"]),
            quantity=int(raw.get("quantity", 1)),
            side=str(raw["side"]),
            opened_at=parse_timestamp(raw["opened_at"]),
            average_open_price=float(raw["average_open_price"]),
            premium_at_risk=float(raw.get("premium_at_risk", 0.0)),
            collateral_reserved=float(raw.get("collateral_reserved", 0.0)),
            shares_encumbered=int(raw.get("shares_encumbered", 0)),
            status=str(raw["status"]),
            structure_fingerprint=str(raw["structure_fingerprint"]),
            position_hash=supplied_hash,
        )
        if verify_hash:
            _verify_supplied_hash(
                "Active option position",
                supplied_hash,
                position.with_hash().position_hash,
            )
        return position.with_hash() if not supplied_hash else position

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "position_hash": self.position_hash}
