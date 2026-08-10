from __future__ import annotations

from agentic_trader.execution import KILL_SWITCH_FILENAME
from agentic_trader.option_reconcile import reconcile_option_orders

OPTION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REF_ID = "11111111-2222-5333-8444-555555555555"


def approval(**overrides):
    values = {
        "option_id": OPTION_ID,
        "side": "buy",
        "position_effect": "open",
        "quantity": 1,
        "limit_price": 0.60,
        "ref_id": REF_ID,
    }
    values.update(overrides)
    return values


def broker_order(**overrides):
    values = {
        "id": "broker-order-1",
        "ref_id": REF_ID,
        "state": "filled",
        "quantity": "1",
        "processed_quantity": "1",
        "average_price": "0.59",
        "direction": "debit",
        "legs": [
            {
                "option": f"https://api.robinhood.com/options/instruments/{OPTION_ID}/",
                "side": "buy",
                "position_effect": "open",
                "ratio_quantity": 1,
            }
        ],
    }
    values.update(overrides)
    return values


def test_native_robinhood_option_fill_reconciles_cleanly(tmp_path):
    result = reconcile_option_orders([approval()], [broker_order()], root=tmp_path)
    assert result["clean"]
    assert result["matched"][0]["order_id"] == "broker-order-1"
    assert not (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_match_requires_ref_id_fingerprint_and_quantity(tmp_path):
    wrong_ref = reconcile_option_orders(
        [approval()],
        [broker_order(ref_id="other")],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert "unauthorized_option_fill_detected" in wrong_ref["breaches"]

    wrong_leg = broker_order(
        legs=[
            {
                "option_id": "different-option",
                "side": "buy",
                "position_effect": "open",
            }
        ]
    )
    assert "unauthorized_option_fill_detected" in reconcile_option_orders(
        [approval()], [wrong_leg], root=tmp_path, engage_on_breach=False
    )["breaches"]

    wrong_quantity = broker_order(quantity="2", processed_quantity="2")
    assert "unauthorized_option_fill_detected" in reconcile_option_orders(
        [approval()], [wrong_quantity], root=tmp_path, engage_on_breach=False
    )["breaches"]


def test_partial_fill_is_a_breach_and_engages_kill_switch(tmp_path):
    partial = broker_order(state="partially_filled", processed_quantity="0.5")
    result = reconcile_option_orders([approval()], [partial], root=tmp_path)
    assert "partial_option_fill_detected" in result["breaches"]
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_unknown_state_is_rejected(tmp_path):
    result = reconcile_option_orders(
        [approval()],
        [broker_order(state="mystery")],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert "unknown_option_order_detected" in result["breaches"]
    assert result["unknown"][0]["state"] == "mystery"


def test_duplicate_broker_ref_is_rejected(tmp_path):
    duplicate = broker_order(id="broker-order-2")
    result = reconcile_option_orders(
        [approval()],
        [broker_order(), duplicate],
        root=tmp_path,
    )
    assert "duplicate_option_order_detected" in result["breaches"]
    assert result["duplicates"][0]["kind"] == "broker_ref_id"
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_duplicate_approval_ref_is_rejected(tmp_path):
    result = reconcile_option_orders(
        [approval(), approval(option_id="another-option")],
        [],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert "duplicate_option_approval_detected" in result["breaches"]


def test_adverse_debit_fill_is_measured_against_limit(tmp_path):
    result = reconcile_option_orders(
        [approval(limit_price=0.60)],
        [broker_order(average_price="0.61")],
        root=tmp_path,
    )
    assert "option_fill_price_worse_than_limit" in result["breaches"]
    assert result["price_breaches"][0]["direction"] == "debit"


def test_debit_price_improvement_is_clean(tmp_path):
    assert reconcile_option_orders(
        [approval(limit_price=0.60)],
        [broker_order(average_price="0.55")],
        root=tmp_path,
    )["clean"]


def test_adverse_credit_fill_is_measured_in_opposite_direction(tmp_path):
    approved = approval(side="sell", limit_price=0.60)
    credit = broker_order(
        direction="credit",
        average_price="0.59",
        legs=[
            {
                "option_id": OPTION_ID,
                "side": "sell",
                "position_effect": "open",
                "ratio_quantity": 1,
            }
        ],
    )
    result = reconcile_option_orders([approved], [credit], root=tmp_path)
    assert "option_fill_price_worse_than_limit" in result["breaches"]


def test_credit_price_improvement_is_clean(tmp_path):
    approved = approval(side="sell", limit_price=0.60)
    credit = broker_order(
        direction="credit",
        average_price="0.61",
        legs=[
            {
                "option_id": OPTION_ID,
                "side": "sell",
                "position_effect": "open",
            }
        ],
    )
    assert reconcile_option_orders([approved], [credit], root=tmp_path)["clean"]


def test_leg_execution_price_is_parsed_when_parent_average_is_absent(tmp_path):
    native = broker_order()
    native.pop("average_price")
    native["legs"][0]["executions"] = [{"price": "0.58", "quantity": "1"}]
    result = reconcile_option_orders([approval()], [native], root=tmp_path)
    assert result["clean"]
    assert result["matched"][0]["average_fill_price"] == 0.58


def test_unknown_fill_price_is_a_breach(tmp_path):
    native = broker_order()
    native.pop("average_price")
    result = reconcile_option_orders(
        [approval()],
        [native],
        root=tmp_path,
        engage_on_breach=False,
    )
    assert "unknown_option_order_detected" in result["breaches"]

    nan_price = broker_order(average_price="NaN")
    assert "unknown_option_order_detected" in reconcile_option_orders(
        [approval()],
        [nan_price],
        root=tmp_path,
        engage_on_breach=False,
    )["breaches"]


def test_cancelled_order_is_terminal_unfilled_and_safe_to_release(tmp_path):
    result = reconcile_option_orders(
        [approval()],
        [broker_order(state="cancelled")],
        root=tmp_path,
    )
    assert result["clean"]
    assert result["complete"]
    assert result["terminal_unfilled"][0]["ref_id"] == REF_ID


def test_queued_order_is_incomplete_and_never_safe_to_release(tmp_path):
    result = reconcile_option_orders(
        [approval()],
        [broker_order(state="queued")],
        root=tmp_path,
    )
    assert not result["clean"]
    assert not result["complete"]
    assert result["breaches"] == []
    assert result["pending"][0]["ref_id"] == REF_ID
    assert not (tmp_path / KILL_SWITCH_FILENAME).exists()


def test_fill_without_approval_is_unauthorized_and_halts(tmp_path):
    result = reconcile_option_orders([], [broker_order()], root=tmp_path)
    assert "unauthorized_option_fill_detected" in result["breaches"]
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()
