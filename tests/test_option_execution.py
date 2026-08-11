from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from agentic_trader.option_execution import (
    OptionAccountSnapshot,
    OptionExecutionLimits,
    ProposedOptionOrder,
    deterministic_option_ref_id,
    evaluate_option_batch,
    evaluate_option_order,
    summarize_broker_option_orders,
)

NOW = datetime(2026, 8, 10, 19, 0, tzinfo=UTC)
ACCOUNT = "111111111"
OPTION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def account(**overrides) -> OptionAccountSnapshot:
    defaults = {
        "account_number": ACCOUNT,
        "equity": 2_000.0,
        "cash": 2_000.0,
        "option_level": "option_level_2",
        "orders_source": "broker",
        "session_is_regular": True,
        "agentic_allowed": True,
    }
    defaults.update(overrides)
    return OptionAccountSnapshot(**defaults)


def order(**overrides) -> ProposedOptionOrder:
    defaults = {
        "account_number": ACCOUNT,
        "option_id": OPTION_ID,
        "chain_symbol": "SPY",
        "strategy": "long_call",
        "option_type": "call",
        "side": "buy",
        "position_effect": "open",
        "quantity": 1,
        "limit_price": 0.60,
        "bid_price": 0.58,
        "ask_price": 0.62,
        "quote_timestamp": NOW,
        "expiration_date": date(2026, 9, 11),
        "strike_price": 650.0,
        "rationale": "bounded event exposure",
        "order_date": NOW.date(),
    }
    defaults.update(overrides)
    return ProposedOptionOrder(**defaults)


def test_valid_long_call_is_approved():
    decision = evaluate_option_order(order(), account(), now=NOW)
    assert decision.approved
    assert decision.reasons == ()


def test_requires_level_2_and_agentic_account():
    low_level = evaluate_option_order(order(), account(option_level="option_level_1"), now=NOW)
    assert "option_level_2_required" in low_level.reasons
    denied = evaluate_option_order(order(), account(agentic_allowed=False), now=NOW)
    assert "account_not_agentic_allowed" in denied.reasons


def test_account_defaults_fail_closed_and_nonfinite_values_are_rejected():
    defaults = OptionAccountSnapshot(
        account_number=ACCOUNT,
        equity=2_000.0,
        cash=2_000.0,
        option_level="option_level_2",
    )
    reasons = evaluate_option_order(order(), defaults, now=NOW).reasons
    assert "account_not_agentic_allowed" in reasons
    assert "option_order_count_not_broker_verified" in reasons
    assert "outside_regular_trading_session" in reasons
    with pytest.raises(ValueError, match="finite"):
        account(equity=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        order(limit_price=float("inf"))


def test_strategy_allowlist_cannot_be_configured_to_allow_naked_options():
    with pytest.raises(ValueError, match="hard allowlist"):
        OptionExecutionLimits(allowed_strategies=("naked_call",))
    with pytest.raises(ValueError, match="60"):
        OptionExecutionLimits(max_quote_age_seconds=61)
    with pytest.raises(ValueError, match="75"):
        OptionExecutionLimits(max_long_debit=76)
    with pytest.raises(ValueError, match="hard caps"):
        OptionExecutionLimits(max_openings_per_day=4)
    with pytest.raises(ValueError, match="hard caps"):
        OptionExecutionLimits(max_open_option_positions=4)
    with pytest.raises(ValueError, match="hard caps"):
        OptionExecutionLimits(max_orders_per_day=9)
    with pytest.raises(ValueError, match="hard caps"):
        OptionExecutionLimits(max_entry_orders_per_day=7)
    with pytest.raises(ValueError, match="800"):
        OptionExecutionLimits(max_daily_notional=801)
    with pytest.raises(ValueError, match="600"):
        OptionExecutionLimits(max_entry_daily_notional=601)


def test_default_frequency_limits_reserve_shared_exit_capacity():
    limits = OptionExecutionLimits()
    assert limits.max_openings_per_day == 3
    assert limits.max_open_option_positions == 3
    assert limits.max_orders_per_day == 8
    assert limits.max_entry_orders_per_day == 6
    assert limits.max_daily_notional == 800.0
    assert limits.max_entry_daily_notional == 600.0


def test_option_premium_shares_total_and_reserved_entry_notional():
    entry = evaluate_option_order(
        order(limit_price=0.60),
        account(
            notional_today=550.0,
            entry_notional_today=550.0,
        ),
        now=NOW,
    )
    assert "option_order_would_breach_entry_daily_notional" in entry.reasons

    close = order(
        strategy="close",
        side="sell",
        position_effect="close",
        limit_price=0.60,
    )
    assert evaluate_option_order(
        close,
        account(
            notional_today=550.0,
            entry_notional_today=550.0,
        ),
        now=NOW,
    ).approved
    total_breach = evaluate_option_order(
        close,
        account(
            notional_today=750.0,
            entry_notional_today=550.0,
        ),
        now=NOW,
    )
    assert "option_order_would_breach_daily_notional" in total_breach.reasons


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"quantity": 2}, "option_order_must_be_one_contract"),
        ({"order_type": "market"}, "option_orders_must_be_limit"),
        ({"time_in_force": "gtc"}, "option_orders_must_be_gfd"),
        ({"market_hours": "regular_curb_hours"}, "option_orders_must_use_regular_hours"),
        ({"bid_price": 0.0}, "option_bid_must_be_positive"),
        ({"bid_price": 0.50, "ask_price": 0.70}, "option_spread_exceeds_limit"),
        ({"days_to_expiration": 20}, "entry_dte_outside_allowed_range"),
        ({"days_to_expiration": 61}, "entry_dte_outside_allowed_range"),
        ({"quote_timestamp": NOW - timedelta(seconds=61)}, "option_quote_stale"),
    ],
)
def test_hard_order_constraints(overrides, reason):
    assert reason in evaluate_option_order(order(**overrides), account(), now=NOW).reasons


def test_quote_at_sixty_seconds_and_dte_endpoints_are_allowed():
    assert evaluate_option_order(
        order(quote_timestamp=NOW - timedelta(seconds=60), days_to_expiration=21),
        account(),
        now=NOW,
    ).approved
    assert evaluate_option_order(
        order(days_to_expiration=60),
        account(),
        now=NOW,
    ).approved


def test_strategy_shape_must_match_level_2_strategy():
    bad = order(strategy="covered_call", side="buy")
    assert "strategy_leg_mismatch" in evaluate_option_order(bad, account(), now=NOW).reasons
    unsupported = order(strategy="naked_call", side="sell")
    assert "unsupported_option_strategy" in evaluate_option_order(
        unsupported, account(), now=NOW
    ).reasons


def test_long_debit_uses_lower_of_dollar_and_equity_caps():
    too_large = evaluate_option_order(order(limit_price=0.76), account(), now=NOW)
    assert "long_option_debit_exceeds_cap" in too_large.reasons
    small_account = account(equity=1_000.0, cash=1_000.0)
    equity_limited = evaluate_option_order(order(limit_price=0.60), small_account, now=NOW)
    assert "long_option_debit_exceeds_cap" in equity_limited.reasons


def test_aggregate_debit_and_cash_reserve_are_enforced():
    aggregate = evaluate_option_order(
        order(),
        account(aggregate_long_debit=150.0),
        now=NOW,
    )
    assert "aggregate_long_option_debit_exceeds_cap" in aggregate.reasons
    reserve = evaluate_option_order(order(), account(cash=230.0), now=NOW)
    assert "insufficient_settled_cash_after_reserve" in reserve.reasons


def test_covered_call_needs_unencumbered_hundred_shares():
    covered = order(
        strategy="covered_call",
        option_type="call",
        side="sell",
        strike_price=700.0,
    )
    assert evaluate_option_order(
        covered, account(underlying_shares={"SPY": 100}), now=NOW
    ).approved
    denied = evaluate_option_order(
        covered,
        account(
            underlying_shares={"SPY": 100},
            covered_call_contracts={"SPY": 1},
        ),
        now=NOW,
    )
    assert "insufficient_shares_for_covered_call" in denied.reasons


def test_cash_secured_put_caps_collateral_and_assignment_concentration():
    put = order(
        strategy="cash_secured_put",
        option_type="put",
        side="sell",
        strike_price=20.0,
    )
    rich = account(equity=20_000.0, cash=20_000.0)
    assert evaluate_option_order(put, rich, now=NOW).approved
    concentrated = evaluate_option_order(
        put,
        account(equity=20_000.0, cash=20_000.0, underlying_values={"SPY": 1_500.0}),
        now=NOW,
    )
    assert "post_assignment_underlying_weight_exceeds_cap" in concentrated.reasons
    collateral = evaluate_option_order(
        put,
        rich,
        limits=OptionExecutionLimits(max_csp_collateral_weight=0.05),
        now=NOW,
    )
    assert "cash_secured_put_collateral_exceeds_cap" in collateral.reasons


def test_third_opening_and_position_are_allowed_but_fourth_is_blocked():
    third = evaluate_option_order(
        order(),
        account(
            option_openings_today=2,
            open_option_positions=2,
            orders_today=2,
            entry_orders_today=2,
        ),
        now=NOW,
    )
    assert third.approved

    snapshot = account(
        option_openings_today=3,
        open_option_positions=3,
        orders_today=3,
        entry_orders_today=3,
        mandatory_close_option_ids=("old-option",),
    )
    reasons = evaluate_option_order(order(), snapshot, now=NOW).reasons
    assert "daily_option_opening_limit_reached" in reasons
    assert "max_open_option_positions_reached" in reasons
    assert "mandatory_option_closes_pending" in reasons


def test_option_order_shares_the_account_daily_order_limit():
    decision = evaluate_option_order(order(), account(orders_today=8), now=NOW)
    assert "daily_order_count_limit_reached" in decision.reasons


def test_mandatory_close_can_clear_halt_before_batch_entry():
    close = order(
        option_id="old-option",
        strategy="close",
        side="sell",
        position_effect="close",
        option_type="call",
        days_to_expiration=5,
    )
    snapshot = account(
        open_option_positions=1,
        mandatory_close_option_ids=("old-option",),
    )
    decisions = evaluate_option_batch([close, order()], snapshot, now=NOW)
    assert decisions[0].approved
    assert decisions[1].approved


def test_batch_prioritizes_mandatory_close_before_an_earlier_entry():
    close = order(
        option_id="old-option",
        strategy="close",
        side="sell",
        position_effect="close",
        option_type="call",
        days_to_expiration=5,
    )
    snapshot = account(
        open_option_positions=3,
        option_openings_today=2,
        orders_today=2,
        entry_orders_today=2,
        mandatory_close_option_ids=("old-option",),
    )
    decisions = evaluate_option_batch([order(), close], snapshot, now=NOW)
    assert [decision.order.option_id for decision in decisions] == ["old-option", OPTION_ID]
    assert all(decision.approved for decision in decisions)


def test_batch_allows_three_openings_and_blocks_the_fourth():
    decisions = evaluate_option_batch(
        [
            order(limit_price=0.10),
            order(option_id="second-option", limit_price=0.10),
            order(option_id="third-option", limit_price=0.10),
            order(option_id="fourth-option", limit_price=0.10),
        ],
        account(),
        now=NOW,
    )
    assert all(decision.approved for decision in decisions[:3])
    assert "daily_option_opening_limit_reached" in decisions[3].reasons
    assert "max_open_option_positions_reached" in decisions[3].reasons


def test_entry_cap_reserves_exit_slots_and_total_cap_still_applies():
    correction = order(
        option_id="correction-option",
        strategy="close",
        side="sell",
        position_effect="close",
    )
    snapshot = account(
        open_option_positions=1,
        orders_today=6,
        entry_orders_today=6,
    )
    decisions = evaluate_option_batch([order(), correction], snapshot, now=NOW)
    assert decisions[0].order.option_id == "correction-option"
    assert decisions[0].approved
    assert "daily_entry_order_count_limit_reached" in decisions[1].reasons

    total_exhausted = evaluate_option_order(
        correction,
        account(
            open_option_positions=1,
            orders_today=8,
            entry_orders_today=6,
        ),
        now=NOW,
    )
    assert "daily_order_count_limit_reached" in total_exhausted.reasons


def test_kill_switch_blocks_option_orders(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt")
    decision = evaluate_option_order(order(), account(), root=tmp_path, now=NOW)
    assert "kill_switch_file_present" in decision.reasons


def test_risk_reducing_close_remains_available_during_entry_halts(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt entries")
    close = order(
        strategy="close",
        side="sell",
        position_effect="close",
    )
    decision = evaluate_option_order(
        close,
        account(
            orders_today=7,
            external_halt_reasons=("picker_database_halt:test",),
        ),
        root=tmp_path,
        now=NOW,
    )
    assert decision.approved


def test_ref_id_is_stable_and_changes_with_logical_identity():
    day = date(2026, 8, 10)
    first = deterministic_option_ref_id(ACCOUNT, OPTION_ID, "buy", "open", day, "long_call")
    assert first == deterministic_option_ref_id(
        ACCOUNT, OPTION_ID, "buy", "open", day, "long_call"
    )
    assert uuid.UUID(first).version == 5
    assert first != deterministic_option_ref_id(
        ACCOUNT, OPTION_ID, "sell", "close", day, "close"
    )


def test_broker_parameters_exactly_match_review_and_place_schemas():
    proposed = order()
    common = {
        "account_number": ACCOUNT,
        "legs": [
            {
                "option_id": OPTION_ID,
                "side": "buy",
                "position_effect": "open",
                "ratio_quantity": 1,
            }
        ],
        "type": "limit",
        "quantity": "1",
        "price": "0.60",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    assert proposed.review_parameters() == {
        **common,
        "chain_symbol": "SPY",
        "underlying_type": "equity",
    }
    assert proposed.place_parameters() == {**common, "ref_id": proposed.ref_id}


def test_order_can_be_built_from_independent_packet_dict():
    proposed = ProposedOptionOrder.from_dict(
        {
            "account_number": ACCOUNT,
            "packet_id": "packet-1",
            "valid_for_date": "2026-08-10",
            "action": "long_call",
            "side": "buy",
            "position_effect": "open",
            "quantity": 1,
            "limit_price": 0.60,
            "contract": {
                "option_id": OPTION_ID,
                "underlying": "SPY",
                "option_type": "call",
                "expiration_date": "2026-09-11",
                "strike": 650.0,
                "bid": 0.58,
                "ask": 0.62,
                "quote_at": NOW.isoformat(),
            },
        }
    )
    assert proposed.option_id == OPTION_ID
    assert proposed.chain_symbol == "SPY"
    assert proposed.intent == "packet-1"
    assert evaluate_option_order(proposed, account(), now=NOW).approved


def test_summarizes_native_option_orders_in_contract_dollars():
    orders = [
        {
            "quantity": "1",
            "price": "0.60",
            "legs": [
                {
                    "option": f"https://api.robinhood.com/options/instruments/{OPTION_ID}/",
                    "side": "buy",
                    "position_effect": "open",
                }
            ],
        },
        {
            "quantity": "1",
            "average_price": "0.25",
            "legs": [
                {
                    "option_id": OPTION_ID,
                    "side": "sell",
                    "position_effect": "close",
                }
            ],
        },
    ]
    assert summarize_broker_option_orders(orders) == (1, 85.0)


def test_option_order_summary_fails_closed_when_premium_unknown():
    with pytest.raises(ValueError, match="premium"):
        summarize_broker_option_orders(
            [
                {
                    "quantity": "1",
                    "legs": [
                        {
                            "option_id": OPTION_ID,
                            "side": "buy",
                            "position_effect": "open",
                        }
                    ],
                }
            ]
        )
