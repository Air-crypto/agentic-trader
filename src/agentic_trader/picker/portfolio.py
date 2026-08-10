from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from .invalidation import evaluate_invalidation
from .models import ActiveThesis, DecisionPacket


@dataclass(frozen=True)
class PickerPortfolioPolicy:
    max_active_names: int = 6
    max_stock_weight: float = 0.15
    max_sector_weight: float = 0.30
    min_cash_weight: float = 0.10


@dataclass(frozen=True)
class ExitIntent:
    symbol: str
    pick_id: str
    reason: str


@dataclass(frozen=True)
class PickerPortfolioPlan:
    targets: dict[str, float]
    authorized_buy_symbols: tuple[str, ...]
    authorized_sell_symbols: tuple[str, ...]
    exits: tuple[ExitIntent, ...]
    accepted_packet_ids: tuple[str, ...]
    rejected_packets: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "targets": self.targets,
            "authorized_buy_symbols": list(self.authorized_buy_symbols),
            "authorized_sell_symbols": list(self.authorized_sell_symbols),
            "exits": [
                {"symbol": item.symbol, "pick_id": item.pick_id, "reason": item.reason}
                for item in self.exits
            ],
            "accepted_packet_ids": list(self.accepted_packet_ids),
            "rejected_packets": self.rejected_packets,
        }


def _apply_sector_caps(
    weights: dict[str, float],
    sectors: dict[str, str],
    max_sector_weight: float,
) -> dict[str, float]:
    result = dict(weights)
    sector_symbols: dict[str, list[str]] = {}
    for symbol in result:
        sector_symbols.setdefault(sectors.get(symbol, "Unknown"), []).append(symbol)
    for symbols in sector_symbols.values():
        total = sum(result[symbol] for symbol in symbols)
        if total <= max_sector_weight or total <= 0:
            continue
        scale = max_sector_weight / total
        for symbol in symbols:
            result[symbol] *= scale
    return result


def build_picker_portfolio(
    packets: list[DecisionPacket],
    active_theses: list[ActiveThesis],
    prices: dict[str, float],
    spy_price: float,
    as_of: date,
    now: datetime | None = None,
    policy: PickerPortfolioPolicy | None = None,
) -> PickerPortfolioPlan:
    """Merge active theses and same-day packets into a capped long-only book."""
    policy = policy or PickerPortfolioPolicy()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    prices = {symbol.upper(): float(price) for symbol, price in prices.items()}
    exits: list[ExitIntent] = []
    weights: dict[str, float] = {}
    sectors: dict[str, str] = {}
    rejected: dict[str, str] = {}
    accepted: list[str] = []
    buy_symbols: set[str] = set()
    sell_symbols: set[str] = set()

    close_symbols = {
        packet.symbol for packet in packets if packet.action == "close" and packet.verify_hash()
    }
    surviving: list[ActiveThesis] = []
    for thesis in active_theses:
        if thesis.status not in {"pending_entry", "active"}:
            continue
        sell_symbols.add(thesis.symbol)
        if thesis.symbol in close_symbols:
            exits.append(ExitIntent(thesis.symbol, thesis.pick_id, "authorized_close_packet"))
            continue
        current = prices.get(thesis.symbol)
        if current is None:
            # Missing live prices block new entries later but must not fabricate
            # an exit price or silently discard an existing thesis.
            surviving.append(thesis)
            weights[thesis.symbol] = min(thesis.target_weight, policy.max_stock_weight)
            sectors[thesis.symbol] = thesis.sector
            buy_symbols.add(thesis.symbol)
            continue
        invalidation = evaluate_invalidation(thesis, current, spy_price, as_of)
        if invalidation.invalidated:
            exits.append(
                ExitIntent(thesis.symbol, thesis.pick_id, invalidation.reason or "invalidated")
            )
            continue
        surviving.append(thesis)
        weights[thesis.symbol] = min(thesis.target_weight, policy.max_stock_weight)
        sectors[thesis.symbol] = thesis.sector
        buy_symbols.add(thesis.symbol)

    active_symbols = {thesis.symbol for thesis in surviving}
    available_slots = max(0, policy.max_active_names - len(active_symbols))
    buy_packets = sorted(
        [packet for packet in packets if packet.action == "buy"],
        key=lambda packet: (-packet.rank_score, packet.symbol),
    )
    for packet in buy_packets:
        if not packet.verify_hash():
            rejected[packet.packet_id] = "packet_hash_mismatch"
            continue
        if packet.valid_for_date != as_of or packet.expires_at <= now:
            rejected[packet.packet_id] = "packet_not_current"
            continue
        if packet.symbol not in prices:
            rejected[packet.packet_id] = "missing_live_price"
            continue
        is_new = packet.symbol not in active_symbols
        if is_new and available_slots <= 0:
            rejected[packet.packet_id] = "max_active_names_reached"
            continue
        if is_new:
            available_slots -= 1
            active_symbols.add(packet.symbol)
        weights[packet.symbol] = min(
            policy.max_stock_weight,
            max(weights.get(packet.symbol, 0.0), packet.target_weight),
        )
        sectors[packet.symbol] = packet.sector
        buy_symbols.add(packet.symbol)
        sell_symbols.add(packet.symbol)
        accepted.append(packet.packet_id)

    weights = _apply_sector_caps(weights, sectors, policy.max_sector_weight)
    max_invested = 1.0 - policy.min_cash_weight
    total = sum(weights.values())
    if total > max_invested:
        scale = max_invested / total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}

    # Explicit zeros make exits deterministic when the broker still holds a
    # symbol that has expired or received a close packet.
    for exit_intent in exits:
        weights[exit_intent.symbol] = 0.0
    return PickerPortfolioPlan(
        targets=dict(sorted(weights.items())),
        authorized_buy_symbols=tuple(sorted(buy_symbols)),
        authorized_sell_symbols=tuple(sorted(sell_symbols)),
        exits=tuple(exits),
        accepted_packet_ids=tuple(accepted),
        rejected_packets=rejected,
    )
