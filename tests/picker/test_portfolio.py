from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from agentic_trader.picker.invalidation import trading_day_expiry
from agentic_trader.picker.models import ActiveThesis
from agentic_trader.picker.portfolio import PickerPortfolioPolicy, build_picker_portfolio
from agentic_trader.picker.validation import validate_picker_draft


def packet(draft, evidence, quant, now):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    return result.packet


def test_live_canary_portfolio_defaults_bound_names_sectors_and_cash():
    policy = PickerPortfolioPolicy()
    assert policy.max_active_names == 3
    assert policy.max_stock_weight == 0.035
    assert policy.max_sector_weight == 0.07
    assert policy.min_cash_weight == 0.895


def test_no_valid_picks_means_cash(draft, evidence, quant, now):
    plan = build_picker_portfolio([], [], {"SPY": 500.0}, 500.0, now.date(), now)
    assert plan.targets == {}
    assert plan.authorized_buy_symbols == ()


def test_valid_packet_becomes_exact_buy_authorization(draft, evidence, quant, now):
    authorized = packet(draft, evidence, quant, now)
    plan = build_picker_portfolio(
        [authorized],
        [],
        {"EXM": 100.0, "SPY": 500.0},
        500.0,
        now.date(),
        now,
    )
    assert plan.authorized_buy_symbols == ("EXM",)
    assert plan.authorized_sell_symbols == ()
    assert plan.targets["EXM"] == authorized.target_weight
    assert plan.accepted_packet_ids == (authorized.packet_id,)


def test_sector_cap_scales_multiple_names(draft, evidence, quant, now):
    packets = []
    prices = {"SPY": 500.0}
    for index, symbol in enumerate(("AAA", "BBB", "CCC")):
        item_draft = replace(draft, draft_id=f"draft-{index}", symbol=symbol)
        item_quant = replace(quant, symbol=symbol, sector="Technology")
        item_evidence = [replace(item, symbol=symbol) for item in evidence]
        packets.append(packet(item_draft, item_evidence, item_quant, now))
        prices[symbol] = 100.0
    plan = build_picker_portfolio(packets, [], prices, 500.0, now.date(), now)
    assert abs(sum(plan.targets.values()) - 0.07) < 1e-12
    assert all(weight <= 0.035 + 1e-12 for weight in plan.targets.values())


def test_expired_thesis_generates_zero_target_and_mandatory_exit(now):
    thesis = ActiveThesis(
        pick_id="pick-1",
        packet_id="packet-1",
        symbol="EXM",
        sector="Industrials",
        status="active",
        entry_date=now.date() - timedelta(days=30),
        expiry_date=now.date(),
        entry_price=100.0,
        entry_spy_price=500.0,
        target_weight=0.10,
        stop_loss_pct=0.08,
        sector_relative_stop_pct=0.05,
    )
    plan = build_picker_portfolio(
        [],
        [thesis],
        {"EXM": 105.0, "SPY": 510.0},
        510.0,
        now.date(),
        now,
    )
    assert plan.targets["EXM"] == 0.0
    assert plan.exits[0].reason == "horizon_expired"


def test_active_thesis_without_current_packet_cannot_authorize_a_top_up(now):
    thesis = ActiveThesis(
        pick_id="pick-1",
        packet_id="old-packet",
        symbol="EXM",
        sector="Industrials",
        status="active",
        entry_date=now.date(),
        expiry_date=now.date() + timedelta(days=10),
        entry_price=100.0,
        entry_spy_price=500.0,
        target_weight=0.035,
        stop_loss_pct=0.08,
        sector_relative_stop_pct=0.05,
    )

    plan = build_picker_portfolio(
        [],
        [thesis],
        {"EXM": 101.0, "SPY": 500.0},
        500.0,
        now.date(),
        now,
    )

    assert plan.targets["EXM"] == thesis.target_weight
    assert plan.authorized_buy_symbols == ()
    assert plan.authorized_sell_symbols == ("EXM",)


def test_trading_day_horizon_skips_weekend():
    # Friday + one business day = Monday.
    from datetime import date

    assert trading_day_expiry(date(2026, 8, 7), 1) == date(2026, 8, 10)
