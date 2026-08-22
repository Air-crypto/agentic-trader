from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
TEST_QUOTE_SYMBOLS = (
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "VNQ",
    "DBC",
    "IEF",
    "TLT",
    "BIL",
    "AAPL",
    "OLD",
    "NEW",
)
TEST_SECTORS = {
    "SPY": "Broad Market",
    "QQQ": "Broad Market",
    "IWM": "Broad Market",
    "EFA": "International Equity",
    "EEM": "Emerging Markets",
    "VNQ": "Real Estate",
    "DBC": "Commodities",
    "IEF": "Fixed Income",
    "TLT": "Fixed Income",
    "BIL": "Cash Equivalent",
    "AAPL": "Technology",
    "OLD": "Legacy Sector",
    "NEW": "New Sector",
}


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
        "sector_by_symbol": TEST_SECTORS,
        "high_water_mark": 750.0,
        "prior_close_equity": 750.0,
        "orders_today": 0,
        "notional_today": 0.0,
        "pending_deposits": 0.0,
        "orders_source": "broker",
        "session_is_regular": True,
        "quote_timestamps": {symbol: datetime.now(UTC) for symbol in TEST_QUOTE_SYMBOLS},
    }
    defaults.update(overrides)
    return AccountSnapshot(**defaults)


def make_order(**overrides) -> ProposedOrder:
    defaults = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 25.0,
        "order_type": "market",
        "reference_price": 500.0,
        "rationale": "trend model target",
        "quote_timestamp": datetime.now(UTC),
    }
    defaults.update(overrides)
    return ProposedOrder(**defaults)


def test_default_limits_preserve_exit_capacity():
    limits = ExecutionLimits()
    assert limits.max_order_notional == 150.0
    assert limits.max_position_weight == 0.035
    assert limits.max_broad_market_weight == 0.035
    assert limits.max_held_names == 3
    assert limits.max_global_position_weight == 0.035
    assert limits.max_sector_weight == 0.07
    assert limits.min_cash_reserve_weight == 0.895
    assert limits.max_orders_per_day == 8
    assert limits.max_daily_notional == 800.0
    assert limits.max_entry_orders_per_day == 2
    assert limits.max_entry_daily_notional == 300.0
    assert limits.max_daily_loss_weight == 0.005
    assert limits.max_drawdown_weight == 0.03
    assert limits.max_loss_from_deposits_weight == 0.03
    assert limits.require_fresh_quotes
    assert limits.max_quote_age_seconds == 15
    assert limits.max_extended_spread_bps == 10.0


def test_daily_limits_cannot_be_relaxed_through_cli_style_overrides():
    with pytest.raises(ValueError, match="8-order"):
        ExecutionLimits(max_orders_per_day=9)
    with pytest.raises(ValueError, match="800"):
        ExecutionLimits(max_daily_notional=801)
    with pytest.raises(ValueError, match="2"):
        ExecutionLimits(max_entry_orders_per_day=3)
    with pytest.raises(ValueError, match="300"):
        ExecutionLimits(max_entry_daily_notional=301)
    with pytest.raises(ValueError, match="3-name"):
        ExecutionLimits(max_held_names=4)
    with pytest.raises(ValueError, match="3.5%"):
        ExecutionLimits(max_global_position_weight=0.036)
    with pytest.raises(ValueError, match="7%"):
        ExecutionLimits(max_sector_weight=0.071)


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


def test_paired_broker_identity_can_replace_copied_account_secret(monkeypatch):
    monkeypatch.delenv(ACCOUNT_ENV_VAR, raising=False)
    decision = evaluate_order(make_order(), make_account(broker_identity_verified=True))
    assert decision.approved


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
    order = make_order(order_type="limit", limit_price=25.0, quantity=1.0, notional=25.0)
    assert evaluate_order(order, make_account()).approved


def test_limit_order_notional_must_equal_emitted_quantity_times_limit():
    order = make_order(
        order_type="limit",
        limit_price=100.0,
        quantity=100.0,
        notional=25.0,
    )
    decision = evaluate_order(order, make_account(equity=20_000.0, cash=20_000.0))
    assert "order_notional_does_not_match_broker_parameters" in decision.reasons
    assert order.broker_notional() == 10_000.0


def test_market_order_notional_must_equal_emitted_dollar_amount():
    decision = evaluate_order(make_order(notional=100.001), make_account())
    assert "order_notional_does_not_match_broker_parameters" in decision.reasons


def test_refuses_to_plan_outside_regular_hours():
    """A fractional order placed outside 9:30-16:00 ET is rejected by the broker."""
    decision = evaluate_order(make_order(), make_account(session_is_regular=False))
    assert "outside_regular_trading_session" in decision.reasons


def test_overnight_equity_requires_fresh_eligible_whole_share_limit():
    quote_at = datetime.now(UTC)
    account = make_account(
        equity=5_750.0,
        cash=5_750.0,
        session_is_regular=False,
        market_hours="all_day_hours",
        session_tradable_symbols=("SPY",),
        quote_timestamps={"SPY": quote_at},
        quote_spreads_bps={"SPY": 9.0},
    )
    limits = ExecutionLimits(
        allow_extended_hours=True,
        require_fresh_quotes=True,
        max_quote_age_seconds=60,
    )
    order = make_order(
        order_type="limit",
        notional=100.0,
        limit_price=100.0,
        quantity=1.0,
        reference_price=100.0,
        market_hours="all_day_hours",
        quote_timestamp=quote_at,
    )

    assert evaluate_order(order, account, limits).approved

    market = make_order(
        market_hours="all_day_hours",
        quote_timestamp=quote_at,
    )
    assert "extended_hours_requires_limit_order" in evaluate_order(market, account, limits).reasons

    stale = make_order(
        order_type="limit",
        notional=100.0,
        limit_price=100.0,
        quantity=1.0,
        reference_price=100.0,
        market_hours="all_day_hours",
        quote_timestamp=quote_at - timedelta(seconds=61),
    )
    assert "equity_quote_stale" in evaluate_order(stale, account, limits).reasons

    ineligible = make_account(
        session_is_regular=False,
        market_hours="all_day_hours",
        session_tradable_symbols=(),
        quote_timestamps={"SPY": quote_at},
        quote_spreads_bps={"SPY": 9.0},
    )
    assert (
        "symbol_not_tradable_in_selected_session"
        in evaluate_order(order, ineligible, limits).reasons
    )

    wide = make_account(
        equity=5_750.0,
        cash=5_750.0,
        session_is_regular=False,
        market_hours="all_day_hours",
        session_tradable_symbols=("SPY",),
        quote_timestamps={"SPY": quote_at},
        quote_spreads_bps={"SPY": 10.01},
    )
    assert "extended_hours_spread_above_cap" in evaluate_order(order, wide, limits).reasons


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


def test_broad_market_fund_uses_the_live_canary_name_cap():
    account = make_account(equity=5_750.0, cash=5_750.0, positions={"SPY": 100.0})
    decision = evaluate_order(make_order(notional=100.0), account)
    assert decision.approved


def test_broad_market_fund_still_has_its_own_cap():
    account = make_account(equity=5_750.0, cash=5_750.0, positions={"SPY": 150.0})
    decision = evaluate_order(make_order(notional=100.0), account)
    assert "projected_position_weight_exceeds_cap" in decision.reasons


def test_single_name_keeps_the_tighter_cap():
    limits = ExecutionLimits(symbol_allowlist=("AAPL",))
    account = make_account(positions={"AAPL": 100.0})
    decision = evaluate_order(make_order(symbol="AAPL", notional=100.0), account, limits)
    assert "projected_position_weight_exceeds_cap" in decision.reasons


def test_cash_equivalent_is_not_concentration_capped():
    account = make_account(
        positions={"BIL": 600.0},
        sector_by_symbol={},
        cash=1_350.0,
        equity=1_350.0,
    )
    decision = evaluate_order(make_order(symbol="BIL", notional=100.0), account)
    assert decision.approved


def test_entry_rejects_a_fourth_held_name_from_complete_broker_portfolio():
    limits = ExecutionLimits(symbol_allowlist=("AAA", "BBB", "CCC", "DDD"))
    account = make_account(
        equity=10_000.0,
        cash=10_000.0,
        positions={"AAA": 100.0, "BBB": 100.0, "CCC": 100.0},
        sector_by_symbol={
            "AAA": "Technology",
            "BBB": "Healthcare",
            "CCC": "Industrials",
            "DDD": "Consumer",
        },
    )
    decision = evaluate_order(make_order(symbol="DDD", notional=100.0), account, limits)
    assert "portfolio_held_name_count_exceeds_cap" in decision.reasons


def test_entry_rejects_when_any_existing_name_is_above_global_cap():
    limits = ExecutionLimits(symbol_allowlist=("AAA", "BBB"))
    account = make_account(
        equity=10_000.0,
        cash=10_000.0,
        positions={"AAA": 351.0},
        sector_by_symbol={"AAA": "Technology", "BBB": "Healthcare"},
    )
    decision = evaluate_order(make_order(symbol="BBB", notional=100.0), account, limits)
    assert "portfolio_position_weight_exceeds_global_cap:AAA" in decision.reasons


def test_entry_rejects_when_projected_sector_exposure_exceeds_seven_percent():
    limits = ExecutionLimits(symbol_allowlist=("AAA", "BBB", "CCC"))
    account = make_account(
        equity=10_000.0,
        cash=10_000.0,
        positions={"AAA": 350.0, "BBB": 250.0},
        sector_by_symbol={"AAA": "Technology", "BBB": " technology ", "CCC": "TECHNOLOGY"},
    )
    decision = evaluate_order(make_order(symbol="CCC", notional=150.0), account, limits)
    assert "portfolio_sector_weight_exceeds_cap:technology" in decision.reasons


def test_entry_fails_closed_for_missing_or_unknown_sector_mapping():
    limits = ExecutionLimits(symbol_allowlist=("AAA", "BBB"))
    missing_existing = evaluate_order(
        make_order(symbol="BBB", notional=100.0),
        make_account(
            equity=10_000.0,
            cash=10_000.0,
            positions={"AAA": 100.0},
            sector_by_symbol={"BBB": "Healthcare"},
        ),
        limits,
    )
    unknown_candidate = evaluate_order(
        make_order(symbol="BBB", notional=100.0),
        make_account(
            equity=10_000.0,
            cash=10_000.0,
            sector_by_symbol={"BBB": "Unknown"},
        ),
        limits,
    )
    assert "portfolio_sector_mapping_missing:AAA" in missing_existing.reasons
    assert "portfolio_sector_mapping_missing:BBB" in unknown_candidate.reasons


def test_reducing_mandatory_exit_bypasses_breached_portfolio_envelope():
    account = make_account(
        equity=1_000.0,
        cash=300.0,
        positions={"SPY": 400.0, "QQQ": 100.0, "IWM": 100.0, "EFA": 100.0},
        sector_by_symbol={},
    )
    decision = evaluate_order(
        make_order(
            symbol="SPY",
            side="sell",
            notional=400.0,
            intent_class="mandatory_exit",
        ),
        account,
    )
    assert decision.approved


def test_default_quote_gate_requires_a_quote_no_older_than_15_seconds():
    missing = evaluate_order(make_order(quote_timestamp=None), make_account())
    stale = evaluate_order(
        make_order(quote_timestamp=datetime.now(UTC) - timedelta(seconds=16)),
        make_account(),
    )
    assert "missing_or_invalid_equity_quote_timestamp" in missing.reasons
    assert "equity_quote_stale" in stale.reasons


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


def test_mandatory_reducing_sell_bypasses_loss_halts(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=600.0,
        cash=300.0,
        positions={"SPY": 300.0},
        high_water_mark=750.0,
        prior_close_equity=750.0,
        net_deposits=750.0,
    )
    entry = evaluate_order(make_order(), account)
    exit_decision = evaluate_order(
        make_order(side="sell", notional=300.0, intent_class="mandatory_exit"),
        account,
    )
    assert {"capital_floor_breached", "max_drawdown_halt", "daily_loss_halt"}.issubset(
        entry.reasons
    )
    assert exit_decision.approved


def test_mandatory_exit_still_requires_authenticated_account():
    account = make_account(
        account_number="999999999",
        positions={"SPY": 300.0},
    )
    decision = evaluate_order(
        make_order(side="sell", notional=300.0, intent_class="mandatory_exit"),
        account,
    )
    assert "account_is_not_the_agentic_account" in decision.reasons


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_order_notional_is_rejected(bad_value):
    decision = evaluate_order(make_order(notional=bad_value), make_account())
    assert "non_finite_notional" in decision.reasons


def test_nonfinite_account_value_is_rejected():
    decision = evaluate_order(make_order(), make_account(cash=float("nan")))
    assert "non_finite_cash" in decision.reasons


def test_nonfinite_limit_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        ExecutionLimits(max_daily_loss_weight=float("nan"))


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


def test_configured_net_deposits_does_not_replace_prior_close_protection(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=730.0, cash=730.0, high_water_mark=None, prior_close_equity=None, net_deposits=None
    )
    decision = evaluate_order(make_order(), account)
    assert "no_drawdown_protection_available" not in decision.reasons
    assert "prior_close_equity_missing" in decision.reasons


def test_configured_net_deposits_also_enforces_the_floor(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=600.0, cash=600.0, high_water_mark=None, prior_close_equity=None, net_deposits=None
    )
    assert "capital_floor_breached" in evaluate_order(make_order(), account).reasons


def test_configured_net_deposits_are_authoritative_over_request(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "750")
    account = make_account(
        equity=700.0,
        cash=700.0,
        high_water_mark=None,
        prior_close_equity=None,
        net_deposits=500.0,
    )
    decision = evaluate_order(make_order(), account)
    assert "net_deposits_mismatch" in decision.reasons
    assert "no_drawdown_protection_available" not in decision.reasons


def test_nonfinite_configured_net_deposits_fail_closed(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "inf")
    decision = evaluate_order(make_order(), make_account())
    assert "configured_net_deposits_invalid" in decision.reasons


def test_malformed_net_deposits_does_not_silently_disable_protection(monkeypatch):
    monkeypatch.setenv(NET_DEPOSITS_ENV_VAR, "not-a-number")
    account = make_account(high_water_mark=None, prior_close_equity=None, net_deposits=None)
    assert "no_drawdown_protection_available" in evaluate_order(make_order(), account).reasons


def test_capital_floor_allows_trading_above_the_floor():
    account = make_account(
        equity=730.0, cash=730.0, net_deposits=750.0, high_water_mark=None, prior_close_equity=730.0
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
    account = make_account(orders_today=8)
    decision = evaluate_order(make_order(), account)
    assert "daily_order_count_limit_reached" in decision.reasons


def test_order_breaching_daily_notional_is_rejected():
    account = make_account(
        notional_today=750.0,
        entry_orders_today=0,
        entry_notional_today=0.0,
    )
    decision = evaluate_order(make_order(notional=100.0), account)
    assert "order_would_breach_daily_notional" in decision.reasons


def test_kill_switch_blocks_everything(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt")
    decision = evaluate_order(make_order(), make_account(), root=tmp_path)
    assert "kill_switch_file_present" in decision.reasons


def test_batch_consumes_daily_budget_sequentially():
    orders = [
        make_order(symbol=symbol, notional=150.0, intent_class="entry")
        for symbol in ("SPY", "QQQ", "IWM", "EFA", "EEM")
    ]
    decisions = evaluate_batch(orders, make_account(equity=5_000.0, cash=5_000.0))
    assert all(decision.approved for decision in decisions[:2])
    assert "entry_order_count_limit_reached" in decisions[2].reasons
    assert "order_would_breach_entry_daily_notional" in decisions[2].reasons


def test_entry_order_count_cap_preserves_two_slots_for_exits():
    account = make_account(
        equity=5_000.0,
        cash=4_900.0,
        positions={"SPY": 100.0},
        orders_today=6,
        notional_today=300.0,
        entry_orders_today=6,
        entry_notional_today=300.0,
    )
    entry = evaluate_order(make_order(symbol="QQQ", intent_class="entry"), account)
    exit_order = evaluate_order(
        make_order(
            side="sell",
            notional=100.0,
            intent_class="mandatory_exit",
        ),
        account,
    )
    assert "entry_order_count_limit_reached" in entry.reasons
    assert exit_order.approved


def test_mandatory_exits_are_not_deadlocked_by_exhausted_total_caps():
    account = make_account(
        equity=5_000.0,
        cash=4_700.0,
        positions={"SPY": 300.0},
        orders_today=6,
        notional_today=600.0,
        entry_orders_today=6,
        entry_notional_today=600.0,
    )
    exits = [
        make_order(
            side="sell",
            notional=100.0,
            intent_class="mandatory_exit",
        )
        for _ in range(3)
    ]
    decisions = evaluate_batch(exits, account)
    assert all(decision.approved for decision in decisions)


def test_close_intent_can_use_exit_reserve_case_insensitively():
    account = make_account(
        positions={"SPY": 100.0},
        orders_today=6,
        notional_today=600.0,
        entry_orders_today=6,
        entry_notional_today=600.0,
    )
    order = make_order(side="sell", intent_class=" Close ")
    assert order.intent_class == "close"
    assert evaluate_order(order, account).approved


def test_buy_cannot_consume_exit_reserve_by_mislabeled_intent():
    account = make_account(
        orders_today=6,
        notional_today=600.0,
        entry_orders_today=6,
        entry_notional_today=600.0,
    )
    order = make_order(side="buy", intent_class="close")
    decision = evaluate_order(order, account)
    assert "entry_order_count_limit_reached" in decision.reasons
    assert "order_would_breach_entry_daily_notional" in decision.reasons


def test_unknown_historical_intents_conservatively_consume_entry_capacity():
    account = make_account(orders_today=6, notional_today=600.0)
    decision = evaluate_order(make_order(intent_class="entry"), account)
    assert "entry_order_count_limit_reached" in decision.reasons
    assert "order_would_breach_entry_daily_notional" in decision.reasons


def test_batch_allows_every_authenticated_mandatory_exit():
    symbols = ("SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT")
    orders = [
        make_order(
            symbol=symbol,
            side="sell",
            notional=100.0,
            intent_class="mandatory_exit",
        )
        for symbol in symbols
    ]
    account = make_account(
        equity=5_000.0,
        cash=4_100.0,
        positions={symbol: 100.0 for symbol in symbols},
    )
    decisions = evaluate_batch(orders, account)
    assert all(decision.approved for decision in decisions)


def test_mandatory_exit_bypasses_database_halt_but_not_identity_controls():
    account = make_account(
        positions={"SPY": 300.0},
        orders_today=8,
        notional_today=800.0,
        external_halt_reasons=("picker_database_halt:max_drawdown",),
    )
    decision = evaluate_order(
        make_order(side="sell", notional=300.0, intent_class="mandatory_exit"),
        account,
    )
    assert decision.approved


def test_all_scope_database_halt_blocks_mandatory_exit():
    account = make_account(
        positions={"SPY": 300.0},
        external_halt_reasons=("picker_database_all_halt:reconciliation_breach",),
    )
    decision = evaluate_order(
        make_order(side="sell", notional=300.0, intent_class="mandatory_exit"),
        account,
    )
    assert not decision.approved
    assert "picker_database_all_halt:reconciliation_breach" in decision.reasons


def test_batch_accumulates_global_position_weight():
    limits = ExecutionLimits(
        max_position_weight=0.25,
        max_broad_market_weight=0.25,
        min_cash_reserve_weight=0.10,
    )
    orders = [make_order(notional=100.0) for _ in range(3)]
    decisions = evaluate_batch(orders, make_account(equity=10_000.0, cash=10_000.0), limits)
    assert all(decision.approved for decision in decisions[:2])
    assert "entry_order_count_limit_reached" in decisions[2].reasons


def test_planner_emits_capped_orders_from_targets():
    account = make_account(equity=5_750.0, cash=5_750.0)
    decisions = plan_orders_from_targets(
        {"SPY": 0.035, "IEF": 0.035},
        account,
        prices={"SPY": 500.0, "IEF": 95.0},
        rebalance_threshold=0.0,
    )
    assert [decision.order.symbol for decision in decisions] == ["IEF", "SPY"]
    assert all(decision.approved for decision in decisions)
    assert all(decision.order.notional <= 150.0 for decision in decisions)


def test_planner_notional_exactly_matches_emitted_limit_order():
    account = make_account(equity=5_750.0, cash=5_750.0)
    decision = plan_orders_from_targets(
        {"IEF": 0.035},
        account,
        prices={"IEF": 95.0},
        rebalance_threshold=0.0,
    )[0]
    assert decision.approved
    assert decision.order.notional == decision.order.broker_notional()


def test_planner_uses_a_limit_order_when_a_whole_share_fits():
    account = make_account(equity=5_750.0, cash=5_750.0)
    decision = plan_orders_from_targets(
        {"IEF": 0.035},
        account,
        prices={"IEF": 95.0},
        rebalance_threshold=0.0,
    )[0]
    assert decision.order.order_type == "limit"
    assert decision.order.quantity == 1.0
    assert decision.approved


def test_planner_falls_back_to_a_dollar_market_order_below_one_share():
    """The capped order cannot buy a whole share of a $773 fund."""
    account = make_account(equity=5_750.0, cash=5_750.0)
    decision = plan_orders_from_targets(
        {"SPY": 0.035},
        account,
        prices={"SPY": 773.2},
        rebalance_threshold=0.0,
    )[0]
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


def test_ref_id_uses_stable_logical_identity_without_quote_sensitive_fields():
    from datetime import date

    day = date(2026, 8, 10)
    first = deterministic_ref_id(TEST_ACCOUNT, "SPY", "buy", day, "pick-1", "entry")
    assert first == deterministic_ref_id(
        TEST_ACCOUNT,
        "SPY",
        "buy",
        day,
        "pick-1",
        "entry",
    )
    assert first != deterministic_ref_id(
        TEST_ACCOUNT,
        "SPY",
        "buy",
        day,
        "pick-2",
        "entry",
    )
    assert first != deterministic_ref_id(
        TEST_ACCOUNT,
        "SPY",
        "buy",
        day,
        "pick-1",
        "rebalance",
    )


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


def test_full_position_mandatory_exit_is_not_limited_to_entry_cap():
    account = make_account(
        equity=1_000.0,
        cash=700.0,
        positions={"SPY": 300.0},
    )
    decision = plan_orders_from_targets(
        {"SPY": 0.0},
        account,
        prices={"SPY": 100.0},
        metadata_by_symbol={"SPY": {"intent_class": "mandatory_exit"}},
    )[0]
    assert decision.approved
    assert decision.order.notional == 300.0
    assert decision.order.broker_notional() == 300.0
    assert decision.order.order_type == "market"


def test_full_position_exit_rounds_down_to_a_broker_safe_dollar_amount():
    account = make_account(
        equity=1_000.0,
        cash=699.991,
        positions={"SPY": 300.009},
    )
    decision = plan_orders_from_targets(
        {"SPY": 0.0},
        account,
        prices={"SPY": 100.0},
        metadata_by_symbol={"SPY": {"intent_class": "mandatory_exit"}},
    )[0]
    assert decision.approved
    assert decision.order.notional == 300.0


def test_planner_rejects_nonfinite_market_data():
    with pytest.raises(ValueError, match="finite"):
        plan_orders_from_targets({"SPY": float("nan")}, make_account(), prices={})
    with pytest.raises(ValueError, match="finite"):
        plan_orders_from_targets({"SPY": 0.2}, make_account(), prices={"SPY": float("inf")})


def test_exit_priority_gets_last_total_slot_before_entry():
    account = make_account(
        equity=1_000.0,
        cash=900.0,
        positions={"OLD": 100.0},
        orders_today=7,
        notional_today=700.0,
        entry_orders_today=6,
        entry_notional_today=600.0,
    )
    limits = ExecutionLimits(
        symbol_allowlist=(),
        buy_symbol_allowlist=("NEW",),
        sell_symbol_allowlist=("OLD",),
    )
    decisions = plan_orders_from_targets(
        {"NEW": 0.1, "OLD": 0.0},
        account,
        prices={"NEW": 100.0, "OLD": 100.0},
        limits=limits,
        metadata_by_symbol={
            "OLD": {"intent_class": "close"},
            "NEW": {"intent_class": "entry"},
        },
    )
    assert decisions[0].order.symbol == "OLD"
    assert decisions[0].approved
    assert decisions[1].order.symbol == "NEW"
    assert "daily_order_count_limit_reached" in decisions[1].reasons


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
    from datetime import date

    record_live_state(
        750.0,
        root=tmp_path,
        as_of=date(2026, 8, 20),
        record_prior_close=True,
    )
    record_live_state(700.0, root=tmp_path, as_of=date(2026, 8, 21))
    state = record_live_state(720.0, root=tmp_path, as_of=date(2026, 8, 21))
    assert state["high_water_mark"] == 750.0
    assert state["prior_close_equity"] == 750.0
    assert state["prior_close_date"] == "2026-08-20"


def test_intraday_state_does_not_invent_a_prior_close(tmp_path):
    state = record_live_state(720.0, root=tmp_path)
    assert "prior_close_equity" not in state
