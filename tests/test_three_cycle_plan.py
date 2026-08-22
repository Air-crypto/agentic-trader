from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime

from agentic_trader.cli import command_live_plan
from agentic_trader.execution import (
    daily_consumption,
    daily_entry_consumption,
    record_live_state,
)


def test_repeated_plan_only_cycles_do_not_spend_daily_entry_budget(monkeypatch, tmp_path):
    account_number = "111111111"
    monkeypatch.setenv("AGENTIC_TRADER_ACCOUNT", account_number)
    monkeypatch.setenv("AGENTIC_TRADER_NET_DEPOSITS", "10000")
    record_live_state(10_000.0, tmp_path, record_prior_close=True)
    request = {
        "account": {
            "account_number": account_number,
            "type": "cash",
            "equity": 10_000.0,
            "cash": 10_000.0,
            "buying_power": {
                "buying_power": 10_000.0,
                "unleveraged_buying_power": 10_000.0,
                "intraday_buying_power": 10_000.0,
                "off_intraday_buying_power": 10_000.0,
            },
            "pending_deposits": 0.0,
            "broker_positions": [],
            "broker_orders": [],
            "broker_option_orders": [],
            "broker_option_positions": [],
            "broker_orders_complete_for_session": True,
            "broker_option_orders_complete_for_session": True,
            "broker_advanced_orders_complete_for_session": True,
            "agentic_allowed": True,
            "session_is_regular": True,
            "market_hours": "regular_hours",
            "session_tradable_symbols": ["SPY"],
            "quote_timestamps": {"SPY": datetime.now(UTC).isoformat()},
            "quote_spreads_bps": {"SPY": 1.0},
        },
        "prices": {"SPY": 500.0},
        "targets": {"SPY": 0.15},
        "sector_by_symbol": {"SPY": "broad_market"},
        "instrument_metadata": {"SPY": {"source": "robinhood_scanner", "asset_type": "etf"}},
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
        max_entry_orders_per_day=2,
        max_entry_daily_notional=300.0,
        rebalance_threshold=0.05,
        record_equity=False,
        output=str(output_path),
    )

    assert [command_live_plan(args) for _ in range(3)] == [0, 0, 0]
    assert daily_consumption(tmp_path) == (0, 0.0)
    assert daily_entry_consumption(tmp_path) == (0, 0.0)
    plan = json.loads(output_path.read_text())
    assert len(plan["approved_orders"]) == 1
    assert plan["entry_orders_already_used_today"] == 0
    assert plan["planned_at"] < plan["expires_at"]


def test_equity_plan_counts_prior_option_orders_in_shared_budget(
    monkeypatch,
    tmp_path,
):
    account_number = "111111111"
    monkeypatch.setenv("AGENTIC_TRADER_ACCOUNT", account_number)
    monkeypatch.setenv("AGENTIC_TRADER_NET_DEPOSITS", "10000")
    record_live_state(10_000.0, tmp_path, record_prior_close=True)
    request = {
        "account": {
            "account_number": account_number,
            "type": "cash",
            "equity": 10_000.0,
            "cash": 10_000.0,
            "buying_power": {
                "buying_power": 10_000.0,
                "unleveraged_buying_power": 10_000.0,
                "intraday_buying_power": 10_000.0,
                "off_intraday_buying_power": 10_000.0,
            },
            "pending_deposits": 0.0,
            "broker_positions": [],
            "broker_orders": [],
            "broker_option_orders": [
                {
                    "quantity": "1",
                    "price": "0.75",
                    "state": "filled",
                    "legs": [
                        {
                            "option_id": "option-1",
                            "side": "buy",
                            "position_effect": "open",
                        }
                    ],
                }
            ],
            "broker_option_positions": [],
            "broker_orders_complete_for_session": True,
            "broker_option_orders_complete_for_session": True,
            "broker_advanced_orders_complete_for_session": True,
            "agentic_allowed": True,
            "session_is_regular": True,
            "market_hours": "regular_hours",
            "session_tradable_symbols": ["SPY"],
            "quote_timestamps": {"SPY": datetime.now(UTC).isoformat()},
            "quote_spreads_bps": {"SPY": 1.0},
        },
        "prices": {"SPY": 500.0},
        "targets": {"SPY": 0.15},
        "sector_by_symbol": {"SPY": "broad_market"},
        "instrument_metadata": {"SPY": {"source": "robinhood_scanner", "asset_type": "etf"}},
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
        max_entry_orders_per_day=2,
        max_entry_daily_notional=300.0,
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
