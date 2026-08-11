from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from agentic_trader.picker.invalidation import trading_day_expiry
from agentic_trader.picker.models import ActiveThesis
from agentic_trader.picker.portfolio import build_picker_portfolio
from agentic_trader.picker.validation import validate_picker_draft


def packet(draft, evidence, quant, critic, now):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        critic,
        prompt_hash="a" * 64,
        model_id="analyst-model",
        now=now,
    )
    assert result.packet is not None
    return result.packet


def test_no_valid_picks_means_cash(draft, evidence, quant, critic, now):
    plan = build_picker_portfolio([], [], {"SPY": 500.0}, 500.0, now.date(), now)
    assert plan.targets == {}
    assert plan.authorized_buy_symbols == ()


def test_valid_packet_becomes_exact_buy_authorization(draft, evidence, quant, critic, now):
    authorized = packet(draft, evidence, quant, critic, now)
    plan = build_picker_portfolio(
        [authorized],
        [],
        {"EXM": 100.0, "SPY": 500.0},
        500.0,
        now.date(),
        now,
    )
    assert plan.authorized_buy_symbols == ("EXM",)
    assert plan.targets["EXM"] == authorized.target_weight
    assert plan.accepted_packet_ids == (authorized.packet_id,)


def test_sector_cap_scales_multiple_names(draft, evidence, quant, critic, now):
    packets = []
    prices = {"SPY": 500.0}
    for index, symbol in enumerate(("AAA", "BBB", "CCC")):
        item_draft = replace(draft, draft_id=f"draft-{index}", symbol=symbol)
        item_critic = replace(critic, draft_id=item_draft.draft_id)
        item_quant = replace(quant, symbol=symbol, sector="Technology")
        item_evidence = [replace(item, symbol=symbol) for item in evidence]
        packets.append(
            packet(item_draft, item_evidence, item_quant, item_critic, now)
        )
        prices[symbol] = 100.0
    plan = build_picker_portfolio(packets, [], prices, 500.0, now.date(), now)
    assert abs(sum(plan.targets.values()) - 0.30) < 1e-12
    assert all(weight <= 0.10 + 1e-12 for weight in plan.targets.values())


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


def test_trading_day_horizon_skips_weekend():
    # Friday + one business day = Monday.
    from datetime import date

    assert trading_day_expiry(date(2026, 8, 7), 1) == date(2026, 8, 10)
