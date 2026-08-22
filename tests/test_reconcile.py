from __future__ import annotations

from agentic_trader.execution import KILL_SWITCH_FILENAME
from agentic_trader.reconcile import reconcile


def approval(symbol="SPY", side="buy", notional=150.0, limit_price=774.75):
    return {"symbol": symbol, "side": side, "notional": notional, "limit_price": limit_price}


def fill(
    symbol="SPY",
    side="buy",
    notional=150.0,
    average_price=773.50,
    order_id="a1",
    state="filled",
    **kwargs,
):
    return {
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "average_price": average_price,
        "order_id": order_id,
        "state": state,
        **kwargs,
    }


def test_matching_fill_reconciles_clean(tmp_path):
    result = reconcile([approval()], [fill()], root=tmp_path)
    assert result["clean"]
    assert len(result["matched"]) == 1
    assert not (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_unauthorized_symbol_trips_kill_switch(tmp_path):
    result = reconcile([approval()], [fill(symbol="TQQQ", order_id="rogue")], root=tmp_path)
    assert not result["clean"]
    assert "unauthorized_fill_detected" in result["breaches"]
    assert result["unauthorized"][0]["symbol"] == "TQQQ"
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_duplicate_fill_against_one_approval_is_unauthorized(tmp_path):
    result = reconcile([approval()], [fill(order_id="a1"), fill(order_id="a2")], root=tmp_path)
    assert not result["clean"]
    assert len(result["unauthorized"]) == 1
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_oversized_fill_is_unauthorized(tmp_path):
    result = reconcile([approval(notional=150.0)], [fill(notional=400.0)], root=tmp_path)
    assert "unauthorized_fill_detected" in result["breaches"]


def test_small_rounding_difference_still_matches(tmp_path):
    result = reconcile([approval(notional=150.0)], [fill(notional=149.2)], root=tmp_path)
    assert result["clean"]


def test_adverse_buy_fill_trips_kill_switch(tmp_path):
    result = reconcile([approval(limit_price=774.75)], [fill(average_price=790.0)], root=tmp_path)
    assert "fill_price_outside_tolerance" in result["breaches"]
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_price_improvement_is_not_a_breach(tmp_path):
    result = reconcile([approval(limit_price=774.75)], [fill(average_price=760.0)], root=tmp_path)
    assert result["clean"]


def test_adverse_sell_fill_is_detected(tmp_path):
    approved = approval(side="sell", limit_price=100.0)
    executed = fill(side="sell", average_price=98.0, notional=150.0)
    result = reconcile([approved], [executed], root=tmp_path)
    assert "fill_price_outside_tolerance" in result["breaches"]


def test_cancelled_orders_are_ignored(tmp_path):
    result = reconcile([approval()], [fill(state="cancelled")], root=tmp_path)
    assert result["clean"]
    assert result["approved_but_unfilled"][0]["symbol"] == "SPY"


def test_unfilled_approval_is_reported_without_halting(tmp_path):
    result = reconcile([approval(), approval(symbol="IEF")], [fill()], root=tmp_path)
    assert result["clean"]
    assert result["approved_but_unfilled"] == [{"symbol": "IEF", "side": "buy"}]
    assert not (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_accepts_robinhood_native_order_shape(tmp_path):
    """The broker returns id and dollar_based_amount as strings, not our names."""
    broker_order = {
        "id": "8f1c-native",
        "symbol": "SPY",
        "side": "buy",
        "state": "filled",
        "dollar_based_amount": "150.00",
        "average_price": "773.90",
        "quantity": "0.193953",
    }
    result = reconcile([approval()], [broker_order], root=tmp_path)
    assert result["clean"]
    assert result["matched"][0]["order_id"] == "8f1c-native"


def test_notional_is_derived_when_the_broker_reports_only_shares(tmp_path):
    share_order = {
        "id": "limit-1",
        "symbol": "IEF",
        "side": "buy",
        "state": "filled",
        "cumulative_quantity": "1",
        "average_price": "93.30",
    }
    approved = approval(symbol="IEF", notional=93.17, limit_price=93.36)
    assert reconcile([approved], [share_order], root=tmp_path)["clean"]


def test_ref_id_does_not_authorize_size_drift(tmp_path):
    approved = {**approval(), "ref_id": "abc-123"}
    executed = fill(notional=142.0, order_id="x", ref_id="abc-123")
    result = reconcile([approved], [executed], root=tmp_path)
    assert "unauthorized_fill_detected" in result["breaches"]
    assert result["unauthorized"][0]["reason"] == "ref_id_order_fingerprint_mismatch"


def test_wrong_ref_id_does_not_match_a_different_approval(tmp_path):
    approved = {**approval(), "ref_id": "abc-123"}
    executed = fill(notional=150.0, order_id="x", ref_id="totally-different")
    result = reconcile([approved], [executed], root=tmp_path)
    assert "unauthorized_fill_detected" in result["breaches"]


def test_correct_ref_id_still_requires_symbol_and_side(tmp_path):
    approved = {**approval(), "ref_id": "abc-123"}
    wrong_symbol = reconcile(
        [approved],
        [fill(symbol="TQQQ", ref_id="abc-123")],
        root=tmp_path,
        engage_on_breach=False,
    )
    wrong_side = reconcile(
        [approved],
        [fill(side="sell", ref_id="abc-123")],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert wrong_symbol["unauthorized"][0]["reason"] == "ref_id_order_fingerprint_mismatch"
    assert wrong_side["unauthorized"][0]["reason"] == "ref_id_order_fingerprint_mismatch"


def test_signed_broker_parameters_must_match_native_order_shape(tmp_path):
    parameters = {
        "symbol": "SPY",
        "side": "buy",
        "type": "limit",
        "quantity": "1",
        "limit_price": "500.00",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    approved = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 500.0,
        "limit_price": 500.0,
        "ref_id": "exact-ref",
        "broker_parameters": parameters,
    }
    broker_order = {
        "id": "broker-1",
        "symbol": "SPY",
        "side": "buy",
        "state": "filled",
        "type": "limit",
        "trigger": "immediate",
        "quantity": {"amount": "1"},
        "cumulative_quantity": {"amount": "1"},
        "price": {"amount": "500.00"},
        "average_price": {"amount": "500.00"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": "exact-ref",
    }
    assert reconcile([approved], [broker_order], root=tmp_path)["clean"]

    wrong_session = {**broker_order, "market_hours": "all_day_hours"}
    result = reconcile(
        [approved],
        [wrong_session],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert "unauthorized_fill_detected" in result["breaches"]
    assert result["unauthorized"][0]["reason"] == "ref_id_order_fingerprint_mismatch"


def test_dollar_order_fingerprint_ignores_broker_derived_filled_quantity(tmp_path):
    parameters = {
        "symbol": "SPY",
        "side": "buy",
        "type": "market",
        "dollar_amount": "100.00",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    approved = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 100.0,
        "reference_price": 500.0,
        "ref_id": "dollar-ref",
        "broker_parameters": parameters,
    }
    broker_order = {
        "id": "broker-dollar",
        "symbol": "SPY",
        "side": "buy",
        "state": "filled",
        "type": "market",
        "trigger": "immediate",
        "dollar_based_amount": {"amount": "100.00"},
        "quantity": {"amount": "0.2"},
        "cumulative_quantity": {"amount": "0.2"},
        "average_price": {"amount": "500.00"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": "dollar-ref",
    }
    assert reconcile([approved], [broker_order], root=tmp_path)["clean"]


def test_fill_missing_planned_ref_id_is_rejected(tmp_path):
    approved = {**approval(), "ref_id": "abc-123"}
    result = reconcile([approved], [fill()], root=tmp_path)
    assert "unauthorized_fill_detected" in result["breaches"]
    assert result["unauthorized"][0]["reason"] == "missing_ref_id"


def test_partial_fill_is_a_breach(tmp_path):
    result = reconcile([approval()], [fill(state="partially_filled")], root=tmp_path)
    assert "partial_fill_detected" in result["breaches"]
    assert result["partial"][0]["state"] == "partially_filled"
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_nonterminal_order_is_a_breach(tmp_path):
    result = reconcile([approval()], [fill(state="queued")], root=tmp_path)
    assert "nonterminal_order_detected" in result["breaches"]
    assert result["nonterminal"][0]["state"] == "queued"


def test_missing_state_is_a_breach(tmp_path):
    result = reconcile([approval()], [fill(state=None)], root=tmp_path)
    assert "invalid_fill_detected" in result["breaches"]
    assert "missing_order_state" in result["invalid"][0]["issues"]


def test_filled_order_requires_average_price(tmp_path):
    result = reconcile([approval()], [fill(average_price=None)], root=tmp_path)
    assert "invalid_fill_detected" in result["breaches"]
    assert "missing_or_invalid_average_price" in result["invalid"][0]["issues"]


def test_nonfinite_fill_values_are_rejected(tmp_path):
    result = reconcile(
        [approval()],
        [fill(notional=float("nan"), average_price=float("inf"))],
        root=tmp_path,
    )
    assert "invalid_fill_detected" in result["breaches"]
    assert {
        "missing_or_invalid_notional",
        "missing_or_invalid_average_price",
    }.issubset(result["invalid"][0]["issues"])


def test_fill_with_no_plan_at_all_is_unauthorized(tmp_path):
    result = reconcile([], [fill()], root=tmp_path)
    assert not result["clean"]
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()
