from __future__ import annotations

import pytest

from agentic_trader.execution import (
    ACCOUNT_ENV_VAR,
    AccountSnapshot,
    ExecutionLimits,
    ProposedOrder,
    evaluate_batch,
    evaluate_order,
    marketable_limit_price,
    plan_orders_from_targets,
    record_live_state,
)

TEST_ACCOUNT = "111111111"


@pytest.fixture(autouse=True)
def configured_account(monkeypatch):
    monkeypatch.setenv(ACCOUNT_ENV_VAR, TEST_ACCOUNT)


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
    }
    defaults.update(overrides)
    return AccountSnapshot(**defaults)


def make_order(**overrides) -> ProposedOrder:
    defaults = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 100.0,
        "order_type": "limit",
        "limit_price": 500.0,
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
    decision = evaluate_order(make_order(limit_price=None), make_account())
    assert "limit_order_requires_positive_limit_price" in decision.reasons


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


def test_planner_rejects_symbol_with_no_quote():
    account = make_account(equity=750.0, cash=750.0)
    decisions = plan_orders_from_targets({"SPY": 0.2}, account, prices={})
    assert "limit_order_requires_positive_limit_price" in decisions[0].reasons


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
