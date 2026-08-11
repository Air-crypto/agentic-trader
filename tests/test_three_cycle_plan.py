from __future__ import annotations

import json
from argparse import Namespace

from agentic_trader.cli import command_live_plan
from agentic_trader.execution import daily_consumption, daily_entry_consumption


def test_three_plan_only_cycles_share_daily_entry_budget(monkeypatch, tmp_path):
    account_number = "111111111"
    monkeypatch.setenv("AGENTIC_TRADER_ACCOUNT", account_number)
    monkeypatch.setenv("AGENTIC_TRADER_NET_DEPOSITS", "5750")
    request = {
        "account": {
            "account_number": account_number,
            "equity": 5_750.0,
            "cash": 5_750.0,
            "pending_deposits": 0.0,
            "broker_positions": [],
            "broker_orders": [],
            "broker_option_orders": [],
            "session_is_regular": True,
        },
        "prices": {"SPY": 500.0},
        "targets": {"SPY": 0.15},
    }
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps(request))
    args = Namespace(
        request=str(request_path),
        root=str(tmp_path),
        max_order_notional=150.0,
        max_position_weight=0.25,
        max_orders_per_day=8,
        max_daily_notional=800.0,
        max_entry_orders_per_day=6,
        max_entry_daily_notional=600.0,
        rebalance_threshold=0.05,
        record_equity=False,
        output=str(output_path),
    )

    assert [command_live_plan(args) for _ in range(3)] == [0, 0, 0]
    assert daily_consumption(tmp_path) == (3, 450.0)
    assert daily_entry_consumption(tmp_path) == (3, 450.0)
    plan = json.loads(output_path.read_text())
    assert len(plan["approved_orders"]) == 1
    assert plan["entry_orders_already_used_today"] == 2


def test_equity_plan_counts_prior_option_orders_in_shared_budget(
    monkeypatch,
    tmp_path,
):
    account_number = "111111111"
    monkeypatch.setenv("AGENTIC_TRADER_ACCOUNT", account_number)
    monkeypatch.setenv("AGENTIC_TRADER_NET_DEPOSITS", "5750")
    request = {
        "account": {
            "account_number": account_number,
            "equity": 5_750.0,
            "cash": 5_750.0,
            "pending_deposits": 0.0,
            "broker_positions": [],
            "broker_orders": [],
            "broker_option_orders": [
                {
                    "quantity": "1",
                    "price": "0.75",
                    "legs": [
                        {
                            "option_id": "option-1",
                            "side": "buy",
                            "position_effect": "open",
                        }
                    ],
                }
            ],
            "session_is_regular": True,
        },
        "prices": {"SPY": 500.0},
        "targets": {"SPY": 0.15},
    }
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps(request))
    args = Namespace(
        request=str(request_path),
        root=str(tmp_path),
        max_order_notional=150.0,
        max_position_weight=0.25,
        max_orders_per_day=8,
        max_daily_notional=800.0,
        max_entry_orders_per_day=6,
        max_entry_daily_notional=600.0,
        rebalance_threshold=0.05,
        record_equity=False,
        output=str(output_path),
    )
    assert command_live_plan(args) == 0
    plan = json.loads(output_path.read_text())
    assert plan["orders_already_used_today"] == 1
    assert plan["notional_already_used_today"] == 75.0
    assert plan["entry_orders_already_used_today"] == 1
    assert plan["entry_notional_already_used_today"] == 75.0
