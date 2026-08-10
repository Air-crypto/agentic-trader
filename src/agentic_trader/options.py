from __future__ import annotations

import json
from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt
from pathlib import Path
from typing import Any


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return exp(-(value**2) / 2.0) / sqrt(2.0 * pi)


@dataclass(frozen=True)
class OptionLeg:
    kind: str
    side: str
    strike: float
    quantity: int
    premium: float
    implied_volatility: float

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OptionLeg:
        kind = str(raw["kind"]).lower()
        side = str(raw["side"]).lower()
        if kind not in {"call", "put"}:
            raise ValueError("Option kind must be call or put")
        if side not in {"long", "short"}:
            raise ValueError("Option side must be long or short")
        strike = float(raw["strike"])
        quantity = int(raw.get("quantity", 1))
        premium = float(raw["premium"])
        implied_volatility = float(raw["implied_volatility"])
        if strike <= 0 or quantity <= 0 or premium < 0 or implied_volatility <= 0:
            raise ValueError("Option strike, quantity, and volatility must be positive")
        return cls(
            kind=kind,
            side=side,
            strike=strike,
            quantity=quantity,
            premium=premium,
            implied_volatility=implied_volatility,
        )

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1


@dataclass(frozen=True)
class OptionStructure:
    underlying: str
    spot: float
    days_to_expiry: int
    risk_free_rate: float
    dividend_yield: float
    legs: tuple[OptionLeg, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OptionStructure:
        structure = cls(
            underlying=str(raw["underlying"]).upper(),
            spot=float(raw["spot"]),
            days_to_expiry=int(raw["days_to_expiry"]),
            risk_free_rate=float(raw.get("risk_free_rate", 0.0)),
            dividend_yield=float(raw.get("dividend_yield", 0.0)),
            legs=tuple(OptionLeg.from_dict(value) for value in raw["legs"]),
        )
        if structure.spot <= 0 or structure.days_to_expiry <= 0:
            raise ValueError("Spot and days to expiry must be positive")
        if not structure.legs:
            raise ValueError("At least one option leg is required")
        return structure

    @classmethod
    def from_path(cls, path: str | Path) -> OptionStructure:
        return cls.from_dict(json.loads(Path(path).read_text()))


def _leg_payoff(leg: OptionLeg, terminal_spot: float) -> float:
    intrinsic = (
        max(terminal_spot - leg.strike, 0.0)
        if leg.kind == "call"
        else max(leg.strike - terminal_spot, 0.0)
    )
    return leg.sign * (intrinsic - leg.premium) * leg.quantity * 100


def payoff_at_expiry(structure: OptionStructure, terminal_spot: float) -> float:
    if terminal_spot < 0:
        raise ValueError("Terminal spot cannot be negative")
    return float(sum(_leg_payoff(leg, terminal_spot) for leg in structure.legs))


def _black_scholes_leg(structure: OptionStructure, leg: OptionLeg) -> dict[str, float]:
    time = structure.days_to_expiry / 365
    volatility = leg.implied_volatility
    spot = structure.spot
    strike = leg.strike
    rate = structure.risk_free_rate
    dividend = structure.dividend_yield
    d1 = (log(spot / strike) + (rate - dividend + 0.5 * volatility**2) * time) / (
        volatility * sqrt(time)
    )
    d2 = d1 - volatility * sqrt(time)
    discounted_spot = spot * exp(-dividend * time)
    discounted_strike = strike * exp(-rate * time)

    if leg.kind == "call":
        theoretical_value = discounted_spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
        delta = exp(-dividend * time) * _normal_cdf(d1)
        theta_year = (
            -discounted_spot * _normal_pdf(d1) * volatility / (2 * sqrt(time))
            - rate * discounted_strike * _normal_cdf(d2)
            + dividend * discounted_spot * _normal_cdf(d1)
        )
    else:
        theoretical_value = discounted_strike * _normal_cdf(-d2) - discounted_spot * _normal_cdf(
            -d1
        )
        delta = exp(-dividend * time) * (_normal_cdf(d1) - 1)
        theta_year = (
            -discounted_spot * _normal_pdf(d1) * volatility / (2 * sqrt(time))
            + rate * discounted_strike * _normal_cdf(-d2)
            - dividend * discounted_spot * _normal_cdf(-d1)
        )

    gamma = exp(-dividend * time) * _normal_pdf(d1) / (spot * volatility * sqrt(time))
    vega = discounted_spot * _normal_pdf(d1) * sqrt(time) / 100
    multiplier = leg.sign * leg.quantity * 100
    return {
        "theoretical_value": theoretical_value * multiplier,
        "delta": delta * multiplier,
        "gamma": gamma * multiplier,
        "vega_per_vol_point": vega * multiplier,
        "theta_per_day": theta_year / 365 * multiplier,
    }


def _breakevens(structure: OptionStructure) -> list[float]:
    upper = max(structure.spot * 3, max(leg.strike for leg in structure.legs) * 2)
    points = [upper * index / 4_000 for index in range(4_001)]
    payoffs = [payoff_at_expiry(structure, point) for point in points]
    roots: list[float] = []
    for left in range(len(points) - 1):
        first = payoffs[left]
        second = payoffs[left + 1]
        if first == 0:
            roots.append(points[left])
        elif first * second < 0:
            fraction = abs(first) / (abs(first) + abs(second))
            roots.append(points[left] + fraction * (points[left + 1] - points[left]))
    return sorted({round(root, 4) for root in roots})


def analyze_option_structure(structure: OptionStructure) -> dict[str, object]:
    critical_spots = sorted({0.0, structure.spot, *(leg.strike for leg in structure.legs)})
    critical_payoffs = [
        payoff_at_expiry(structure, terminal_spot) for terminal_spot in critical_spots
    ]
    upper_slope = sum(leg.sign * leg.quantity * 100 for leg in structure.legs if leg.kind == "call")
    loss_is_unbounded = upper_slope < 0
    profit_is_unbounded = upper_slope > 0
    max_loss = None if loss_is_unbounded else max(0.0, -min(critical_payoffs))
    max_profit = None if profit_is_unbounded else max(critical_payoffs)
    net_premium_paid = sum(leg.sign * leg.premium * leg.quantity * 100 for leg in structure.legs)

    aggregate_greeks = {
        "theoretical_value": 0.0,
        "delta": 0.0,
        "gamma": 0.0,
        "vega_per_vol_point": 0.0,
        "theta_per_day": 0.0,
    }
    for leg in structure.legs:
        leg_greeks = _black_scholes_leg(structure, leg)
        for name, value in leg_greeks.items():
            aggregate_greeks[name] += value

    return {
        "underlying": structure.underlying,
        "spot": structure.spot,
        "days_to_expiry": structure.days_to_expiry,
        "defined_risk": not loss_is_unbounded,
        "net_premium_paid": net_premium_paid,
        "maximum_loss": max_loss,
        "maximum_profit": max_profit,
        "breakevens_at_expiry": _breakevens(structure),
        "aggregate_greeks": aggregate_greeks,
        "critical_payoffs": [
            {"terminal_spot": spot, "profit_loss": payoff}
            for spot, payoff in zip(critical_spots, critical_payoffs, strict=True)
        ],
        "model_limitations": [
            "Black-Scholes is an approximation for American-style equity options.",
            "Volatility, spreads, assignment, dividends, and rates can change.",
            "A current-chain payoff is not a historical options backtest.",
        ],
    }
