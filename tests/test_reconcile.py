from __future__ import annotations

from agentic_trader.execution import KILL_SWITCH_FILENAME
from agentic_trader.reconcile import reconcile


def approval(symbol="SPY", side="buy", notional=150.0, limit_price=774.75):
    return {"symbol": symbol, "side": side, "notional": notional, "limit_price": limit_price}


def fill(symbol="SPY", side="buy", notional=150.0, average_price=773.50, order_id="a1", **kwargs):
    return {
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "average_price": average_price,
        "order_id": order_id,
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


def test_fill_with_no_plan_at_all_is_unauthorized(tmp_path):
    result = reconcile([], [fill()], root=tmp_path)
    assert not result["clean"]
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()
