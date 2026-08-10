from __future__ import annotations

import uuid

import pytest

from agentic_trader.execution import (
    ACCOUNT_ENV_VAR,
    NET_DEPOSITS_ENV_VAR,
    AccountSnapshot,
    ExecutionLimits,
    ProposedOrder,
    broker_position_values,
    deterministic_ref_id,
    evaluate_batch,
    evaluate_order,
    marketable_limit_price,
    plan_orders_from_targets,
    record_live_state,
    summarize_broker_orders,
)

TEST_ACCOUNT = "111111111"


@pytest.fixture(autouse=True)
def configured_account(monkeypatch):
    monkeypatch.setenv(ACCOUNT_ENV_VAR, TEST_ACCOUNT)
    # Cleared so a value in the developer's shell cannot change test outcomes.
    monkeypatch.delenv(NET_DEPOSITS_ENV_VAR, raising=False)


def make_account(**overrides) -> AccountSnapshot:
    defaults = {
        "account_number": TEST_ACCOUNT,
        "equity": 750.0,
        "cash": 750.0,
        "positions": {},
        "high_water_mark": 750.0,
        "prior_close_equity": 750.0,
        "orders_today": 0,
        "notional_today": 0.0,
        "pending_deposits": 0.0,
        "orders_source": "broker",
        "session_is_regular": True,
    }
    defaults.update(overrides)
    return AccountSnapshot(**defaults)


def make_order(**overrides) -> ProposedOrder:
    defaults = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 100.0,
        "order_type": "market",
        "reference_price": 500.0,
        "rationale": "trend model target",
    }
    defaults.update(overrides)
    return ProposedOrder(**defaults)


def test_clean_order_is_approved():
    decision = evaluate_order(make_order(), make_account())
    assert decision.approved
    assert decision.reasons == ()


def test_rejects_non_agentic_account():
    decision = evaluate_order(make_order(), make_account(account_number="999999999"))
    assert "account_is_not_the_agentic_account" in decision.reasons


def test_rejects_everything_when_no_account_is_configured(monkeypatch):
    monkeypatch.delenv(ACCOUNT_ENV_VAR, raising=False)
    decision = evaluate_order(make_order(), make_account())
    assert "agentic_account_not_configured" in decision.reasons


def test_blank_account_env_var_is_treated_as_unconfigured(monkeypatch):
    monkeypatch.setenv(ACCOUNT_ENV_VAR, "   ")
    decision = evaluate_order(make_order(), make_account())
    assert "agentic_account_not_configured" in decision.reasons


def test_rejects_symbol_off_allowlist():
    decision = evaluate_order(make_order(symbol="GME"), make_account())
    assert "symbol_not_on_allowlist" in decision.reasons


def test_dynamic_picker_allowlist_is_side_specific():
    limits = ExecutionLimits(
        symbol_allowlist=(),
        buy_symbol_allowlist=("NEW",),
        sell_symbol_allowlist=("OLD",),
    )
    assert evaluate_order(make_order(symbol="NEW"), make_account(), limits).approved
    denied_buy = evaluate_order(make_order(symbol="OLD"), make_account(), limits)
    assert "symbol_not_on_allowlist" in denied_buy.reasons
    account = make_account(positions={"OLD": 100.0})
    allowed_sell = evaluate_order(
        make_order(symbol="OLD", side="sell", notional=50.0),
        account,
        limits,
    )
    assert allowed_sell.approved


def test_rejects_oversized_order():
    decision = evaluate_order(make_order(notional=400.0), make_account())
    assert "order_notional_exceeds_cap" in decision.reasons


def test_rejects_dust_order():
    decision = evaluate_order(make_order(notional=5.0), make_account())
    assert "order_notional_below_minimum" in decision.reasons


def test_rejects_short_sale():
    decision = evaluate_order(make_order(side="sell_short"), make_account())
    assert "unsupported_side" in decision.reasons


def test_rejects_sell_without_position():
    decision = evaluate_order(make_order(side="sell"), make_account())
    assert "sell_without_existing_long_position" in decision.reasons


def test_rejects_sell_larger_than_position():
    account = make_account(positions={"SPY": 40.0})
    decision = evaluate_order(make_order(side="sell", notional=100.0), account)
    assert "sell_exceeds_position_value" in decision.reasons


def test_rejects_limit_order_without_price():
    order = make_order(order_type="limit", limit_price=None, quantity=1.0)
    assert (
        "limit_order_requires_positive_limit_price" in evaluate_order(order, make_account()).reasons
    )


def test_rejects_fractional_limit_order():
    """The broker accepts a limit order only at whole-share size."""
    order = make_order(order_type="limit", limit_price=500.0, quantity=0.2)
    assert (
        "limit_order_requires_whole_share_quantity" in evaluate_order(order, make_account()).reasons
    )


def test_rejects_order_without_a_reference_price():
    decision = evaluate_order(make_order(reference_price=None), make_account())
    assert "missing_reference_price" in decision.reasons


def test_whole_share_limit_order_is_approved():
    order = make_order(order_type="limit", limit_price=95.0, quantity=1.0, notional=93.0)
    assert evaluate_order(order, make_account()).approved


def test_refuses_to_plan_outside_regular_hours():
    """A fractional order placed outside 9:30-16:00 ET is rejected by the broker."""
    decision = evaluate_order(make_order(), make_account(session_is_regular=False))
    assert "outside_regular_trading_session" in decision.reasons


def test_market_order_broker_parameters_are_dollar_denominated():
    params = make_order(notional=150.0).broker_parameters()
    assert params["type"] == "market"
    assert params["dollar_amount"] == "150.00"
    assert params["market_hours"] == "regular_hours"
    assert "limit_price" not in params


def test_limit_order_broker_parameters_are_share_denominated():
    order = make_order(order_type="limit", limit_price=93.36, quantity=1.0)
    params = order.broker_parameters()
    assert params["type"] == "limit"
    assert params["quantity"] == "1"
    assert params["limit_price"] == "93.36"
    assert "dollar_amount" not in params


def test_rejects_missing_rationale():
    decision = evaluate_order(make_order(rationale="  "), make_account())
    assert "missing_rationale" in decision.reasons


def test_broad_market_fund_may_exceed_the_single_name_cap():
    account = make_account(positions={"SPY": 300.0})
    decision = evaluate_order(make_order(notional=100.0), account)
    assert decision.approved


def test_broad_market_fund_still_has_its_own_cap():
    account = make_account(positions={"SPY": 400.0})
    decision = evaluate_order(make_order(notional=100.0), account)
    assert "projected_position_weight_exceeds_cap" in decision.reasons


def test_single_name_keeps_the_tighter_cap():
    limits = ExecutionLimits(symbol_allowlist=("AAPL",))
    account = make_account(positions={"AAPL": 100.0})
    decision = evaluate_order(make_order(symbol="AAPL", notional=100.0), account, limits)
    assert "projected_position_weight_exceeds_cap" in decision.reasons


def test_cash_equivalent_is_not_concentration_capped():
    account = make_account(positions={"BIL": 600.0}, cash=750.0, equity=1_350.0)
    decision = evaluate_order(make_order(symbol="BIL", notional=100.0), account)
    assert decision.approved


def test_unsettled_deposits_are_not_spendable():
    account = make_account(cash=750.0, pending_deposits=750.0)
    decision = evaluate_order(make_order(), account)
    assert "insufficient_settled_cash_after_reserve" in decision.reasons


def test_cash_reserve_is_preserved():
    account = make_account(equity=750.0, cash=120.0)
    decision = evaluate_order(make_order(notional=100.0), account)
    assert "insufficient_settled_cash_after_reserve" in decision.reasons


def test_drawdown_halt_blocks_trading():
    account = make_account(equity=670.0, high_water_mark=750.0, prior_close_equity=670.0)
    decision = evaluate_order(make_order(), account)
    assert "max_drawdown_halt" in decision.reasons


def test_daily_loss_halt_blocks_trading():
    account = make_account(equity=720.0, prior_close_equity=750.0, high_water_mark=750.0)
    decision = evaluate_order(make_order(), account)
    assert "daily_loss_halt" in decision.reasons


def test_unverified_order_count_is_rejected():
    """A duplicate run must not be able to claim it has placed nothing today."""
    decision = evaluate_order(make_order(), make_account(orders_source="unknown"))
    assert "daily_order_count_not_broker_verified" in decision.reasons


def test_locally_sourced_order_count_is_rejected():
    decision = evaluate_order(make_order(), make_account(orders_source="local_state"))
    assert "daily_order_count_not_broker_verified" in decision.reasons


def test_capital_floor_halts_without_any_persisted_state():
    account = make_account(
        equity=600.0, net_deposits=750.0, high_water_mark=None, prior_close_equity=None
    )
    decision = evaluate_order(make_order(), account)
    assert "capital_floor_breached" in decision.reasons


def test_refuses_to_trade_with_no_loss_limit_at_all():
    """A fresh cloud checkout has no persisted peak; unprotected trading is worse
    than not trading."""
    account = make_account(high_water_mark=None, prior_close_equity=None, net_deposits=None)
    decision = evaluate_order(make_order(), account)
    assert "no_drawdown_protection_available" in decision.reasons


def test_configured_net_deposits_restores_protection(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=700.0, cash=700.0, high_water_mark=None, prior_close_equity=None, net_deposits=None
    )
    decision = evaluate_order(make_order(), account)
    assert "no_drawdown_protection_available" not in decision.reasons
    assert decision.approved


def test_configured_net_deposits_also_enforces_the_floor(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=600.0, cash=600.0, high_water_mark=None, prior_close_equity=None, net_deposits=None
    )
    assert "capital_floor_breached" in evaluate_order(make_order(), account).reasons


def test_malformed_net_deposits_does_not_silently_disable_protection(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "not-a-number")
    account = make_account(high_water_mark=None, prior_close_equity=None, net_deposits=None)
    assert "no_drawdown_protection_available" in evaluate_order(make_order(), account).reasons


def test_capital_floor_allows_trading_above_the_floor():
    account = make_account(
        equity=700.0, cash=700.0, net_deposits=750.0, high_water_mark=None, prior_close_equity=None
    )
    assert evaluate_order(make_order(), account).approved


def test_capital_floor_tracks_additional_deposits():
    account = make_account(
        equity=900.0,
        cash=900.0,
        net_deposits=1_500.0,
        high_water_mark=None,
        prior_close_equity=None,
    )
    decision = evaluate_order(make_order(), account)
    assert "capital_floor_breached" in decision.reasons


def test_daily_order_count_limit():
    account = make_account(orders_today=4)
    decision = evaluate_order(make_order(), account)
    assert "daily_order_count_limit_reached" in decision.reasons


def test_order_breaching_daily_notional_is_rejected():
    account = make_account(notional_today=350.0)
    decision = evaluate_order(make_order(notional=100.0), account)
    assert "order_would_breach_daily_notional" in decision.reasons


def test_kill_switch_blocks_everything(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt")
    decision = evaluate_order(make_order(), make_account(), root=tmp_path)
    assert "kill_switch_file_present" in decision.reasons


def test_batch_consumes_daily_budget_sequentially():
    orders = [make_order(notional=150.0) for _ in range(4)]
    decisions = evaluate_batch(orders, make_account(equity=5_000.0, cash=5_000.0))
    approved = [decision for decision in decisions if decision.approved]
    assert len(approved) == 2
    assert "order_would_breach_daily_notional" in decisions[2].reasons


def test_batch_accumulates_position_weight():
    limits = ExecutionLimits(
        max_position_weight=0.25, max_broad_market_weight=0.25, max_daily_notional=10_000.0
    )
    orders = [make_order(notional=100.0) for _ in range(3)]
    decisions = evaluate_batch(orders, make_account(equity=750.0, cash=750.0), limits)
    assert decisions[0].approved
    assert "projected_position_weight_exceeds_cap" in decisions[2].reasons


def test_planner_emits_capped_orders_from_targets():
    account = make_account(equity=750.0, cash=750.0)
    decisions = plan_orders_from_targets(
        {"SPY": 0.2, "IEF": 0.2}, account, prices={"SPY": 500.0, "IEF": 95.0}
    )
    assert [decision.order.symbol for decision in decisions] == ["IEF", "SPY"]
    assert all(decision.approved for decision in decisions)
    assert all(decision.order.notional <= 150.0 for decision in decisions)


def test_planner_uses_a_limit_order_when_a_whole_share_fits():
    account = make_account(equity=750.0, cash=750.0)
    decision = plan_orders_from_targets({"IEF": 0.2}, account, prices={"IEF": 95.0})[0]
    assert decision.order.order_type == "limit"
    assert decision.order.quantity == 1.0
    assert decision.approved


def test_planner_falls_back_to_a_dollar_market_order_below_one_share():
    """A $750 account cannot buy a whole share of a $773 fund."""
    account = make_account(equity=750.0, cash=750.0)
    decision = plan_orders_from_targets({"SPY": 0.2}, account, prices={"SPY": 773.2})[0]
    assert decision.order.order_type == "market"
    assert decision.order.quantity is None
    assert decision.approved
    assert decision.order.broker_parameters()["dollar_amount"] == "150.00"


def test_planner_rejects_symbol_with_no_quote():
    account = make_account(equity=750.0, cash=750.0)
    decisions = plan_orders_from_targets({"SPY": 0.2}, account, prices={})
    assert "limit_order_requires_positive_limit_price" in decisions[0].reasons


def test_duplicate_runs_derive_the_same_ref_id():
    """Two concurrent runs cannot share a lock, so the broker must deduplicate."""
    from datetime import date

    day = date(2026, 8, 10)
    first = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day)
    second = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day)
    assert first == second
    assert uuid.UUID(first).version == 5


def test_ref_id_does_not_depend_on_observed_daily_order_count():
    """Runs that query before and after another order must still deduplicate."""
    from datetime import date

    day = date(2026, 8, 10)
    before_other_order = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day)
    after_other_order = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day)
    assert before_other_order == after_other_order


def test_ref_id_differs_by_symbol_side_day_and_account():
    from datetime import date

    day = date(2026, 8, 10)
    base = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day)
    assert base != deterministic_ref_id(TEST_ACCOUNT, "IEF", "buy", day)
    assert base != deterministic_ref_id(TEST_ACCOUNT, "SPY", "sell", day)
    assert base != deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", date(2026, 8, 11))
    assert base != deterministic_ref_id("999999999", "SPY", "buy", day)


def test_pick_entry_and_exit_have_distinct_broker_idempotency_keys():
    from datetime import date

    day = date(2026, 8, 10)
    entry = deterministic_ref_id(TEST_ACCOUNT, "EXM", "buy", day, pick_id="pick-1", intent="entry")
    exit_id = deterministic_ref_id(
        TEST_ACCOUNT, "EXM", "sell", day, pick_id="pick-1", intent="exit"
    )
    assert entry != exit_id
    assert entry == deterministic_ref_id(
        TEST_ACCOUNT, "EXM", "buy", day, pick_id="pick-1", intent="entry"
    )


def test_mandatory_exit_is_planned_before_new_entry():
    account = make_account(cash=600.0, positions={"OLD": 150.0})
    limits = ExecutionLimits(
        symbol_allowlist=(),
        buy_symbol_allowlist=("NEW",),
        sell_symbol_allowlist=("OLD",),
    )
    decisions = plan_orders_from_targets(
        {"NEW": 0.2, "OLD": 0.0},
        account,
        prices={"NEW": 100.0, "OLD": 100.0},
        limits=limits,
        metadata_by_symbol={
            "OLD": {
                "pick_id": "old-pick",
                "intent_class": "mandatory_exit",
                "exit_reason": "horizon_expired",
            },
            "NEW": {
                "pick_id": "new-pick",
                "intent_class": "entry",
                "exit_reason": None,
            },
        },
    )
    assert decisions[0].order.symbol == "OLD"
    assert decisions[0].order.intent_class == "mandatory_exit"
    assert decisions[1].order.symbol == "NEW"


def test_broker_positions_are_valued_from_quantities_and_current_prices():
    positions = [
        {"symbol": "SPY", "quantity": "0.2"},
        {"symbol": "IEF", "quantity": "1"},
    ]
    assert broker_position_values(positions, {"SPY": 500.0, "IEF": 95.0}) == {
        "SPY": 100.0,
        "IEF": 95.0,
    }


def test_broker_position_without_a_quote_fails_closed():
    with pytest.raises(ValueError, match="Missing a positive current price"):
        broker_position_values([{"symbol": "SPY", "quantity": "0.2"}], {})


def test_broker_order_summary_accepts_dollars_and_share_limit_orders():
    orders = [
        {"symbol": "SPY", "dollar_based_amount": "150.00"},
        {"symbol": "IEF", "quantity": "1", "price": "93.36"},
    ]
    assert summarize_broker_orders(orders) == (2, 243.36)


def test_broker_order_summary_fails_closed_when_notional_is_unknown():
    with pytest.raises(ValueError, match="Cannot determine broker-order notional"):
        summarize_broker_orders([{"symbol": "SPY", "state": "filled"}])


def test_marketable_limit_crosses_spread_in_the_right_direction():
    assert marketable_limit_price(100.0, "buy") == 100.20
    assert marketable_limit_price(100.0, "sell") == 99.80


def test_planner_ignores_drift_below_threshold():
    account = make_account(equity=750.0, cash=735.0, positions={"SPY": 15.0})
    assert plan_orders_from_targets({"SPY": 0.02}, account, prices={"SPY": 500.0}) == []


def test_planner_rejects_targets_above_full_investment():
    with pytest.raises(ValueError):
        plan_orders_from_targets({"SPY": 0.8, "IEF": 0.4}, make_account(), prices={})


def test_high_water_mark_only_ratchets_up(tmp_path):
    record_live_state(750.0, root=tmp_path)
    record_live_state(700.0, root=tmp_path)
    state = record_live_state(720.0, root=tmp_path)
    assert state["high_water_mark"] == 750.0
    assert state["prior_close_equity"] == 720.0
