from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import agentic_trader.cli as cli
from agentic_trader.cli import (
    _option_account_snapshot,
    _option_equity_constraints,
    _option_premium_stop_ids,
    build_parser,
)
from agentic_trader.option_execution import ProposedOptionOrder
from agentic_trader.option_reconcile import reconcile_option_orders
from agentic_trader.picker.invalidation import trading_days_until
from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
    OptionDecisionPacket,
)


def _packet(now: datetime) -> OptionDecisionPacket:
    contract = OptionContractSnapshot(
        option_id="option-1",
        contract_symbol="EXM260911C00100000",
        underlying="EXM",
        option_type="call",
        expiration_date=now.date() + timedelta(days=30),
        strike=100.0,
        bid=0.48,
        ask=0.52,
        quote_at=now,
        underlying_price=100.0,
    )
    return OptionDecisionPacket(
        packet_id="packet-1",
        run_id="run-1",
        draft_id="draft-1",
        created_at=now - timedelta(minutes=2),
        valid_for_date=now.date(),
        expires_at=now - timedelta(seconds=1),
        underlying="EXM",
        action="long_call",
        contract=contract,
        quantity=1,
        side="buy",
        position_effect="open",
        limit_price=0.50,
        max_risk=50.0,
        collateral_required=0.0,
        shares_encumbered=0,
        evidence_ids=("evidence-1",),
        prompt_hash="a" * 64,
        model_id="model",
        draft_hash="b" * 64,
        horizon_trading_days=20,
        invalidation="Close if the catalyst is disproven.",
    )


def _position() -> ActiveOptionPosition:
    return ActiveOptionPosition(
        position_id="position-1",
        packet_id="packet-1",
        underlying="EXM",
        strategy="long_call",
        option_id="option-1",
        contract_symbol="EXM260911C00100000",
        option_type="call",
        expiration_date=date(2026, 9, 11),
        strike=100.0,
        quantity=1,
        side="long",
        opened_at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
        average_open_price=0.50,
        premium_at_risk=50.0,
        collateral_reserved=0.0,
        shares_encumbered=0,
        status="open",
        structure_fingerprint="f" * 64,
    )


def _account(**overrides):
    raw = {
        "account_number": "111111111",
        "equity": 2_000.0,
        "cash": 1_500.0,
        "option_level": "option_level_2",
        "agentic_allowed": True,
        "session_is_regular": True,
        "broker_equity_orders": [],
        "broker_option_orders": [],
        "broker_option_positions": [],
        "broker_equity_positions": [],
        "underlying_prices": {},
    }
    raw.update(overrides)
    return raw


def test_option_snapshot_counts_planned_equity_orders_in_shared_daily_limit():
    snapshot = _option_account_snapshot(
        _account(),
        [],
        planned_equity_orders=3,
        persisted_orders_today=2,
    )
    assert snapshot.orders_today == 3
    assert snapshot.orders_source == "broker"
    assert snapshot.external_halt_reasons == ()
    assert _option_account_snapshot(
        _account(),
        [],
        persisted_orders_today=4,
    ).orders_today == 4


def test_option_snapshot_halts_on_broker_ledger_position_mismatch():
    snapshot = _option_account_snapshot(_account(), [_position()])
    assert "option_position_ledger_broker_mismatch" in snapshot.external_halt_reasons


def test_option_snapshot_accepts_matching_native_broker_position():
    snapshot = _option_account_snapshot(
        _account(
            broker_option_positions=[
                {
                    "option": "https://api.robinhood.com/options/instruments/option-1/",
                    "quantity": "1",
                    "type": "long",
                }
            ]
        ),
        [_position()],
    )
    assert "option_position_ledger_broker_mismatch" not in snapshot.external_halt_reasons


def test_equity_coverage_is_derived_from_native_broker_positions():
    snapshot = _option_account_snapshot(
        _account(
            broker_equity_positions=[{"symbol": "EXM", "quantity": "100"}],
            underlying_prices={"EXM": 10.0},
        ),
        [],
    )
    assert snapshot.underlying_shares == {"EXM": 100.0}
    assert snapshot.underlying_values == {"EXM": 1_000.0}


def test_business_day_expiry_count_skips_weekend():
    assert trading_days_until(date(2026, 8, 7), date(2026, 8, 10)) == 1
    assert trading_days_until(date(2026, 9, 4), date(2026, 9, 8)) == 1


def test_option_resources_constrain_equity_cash_and_covered_share_sales():
    base = _position()
    csp = replace(
        base,
        position_id="csp",
        strategy="cash_secured_put",
        collateral_reserved=500.0,
        position_hash="",
    )
    covered = replace(
        base,
        position_id="covered",
        strategy="covered_call",
        side="short",
        shares_encumbered=100,
        position_hash="",
    )

    reserved, halts = _option_equity_constraints(
        [csp, covered],
        {"EXM": 10.0},
        {"EXM": 0.0},
        equity=2_000.0,
    )

    assert reserved == 500.0
    assert halts == ["covered_option_share_encumbrance_blocks_equity_sale:EXM"]
    _, retained = _option_equity_constraints(
        [covered],
        {"EXM": 10.0},
        {"EXM": 0.50},
        equity=2_000.0,
    )
    assert retained == []


def test_option_premium_stops_cover_long_and_short_level_2_positions():
    long = _position()
    short = replace(
        long,
        position_id="short",
        option_id="option-2",
        strategy="covered_call",
        side="short",
        average_open_price=0.50,
        shares_encumbered=100,
        position_hash="",
    )
    now = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    contracts = {
        "option-1": OptionContractSnapshot(
            option_id="option-1",
            contract_symbol="EXM1",
            underlying="EXM",
            option_type="call",
            expiration_date=date(2026, 9, 11),
            strike=100,
            bid=0.20,
            ask=0.30,
            quote_at=now,
            underlying_price=100,
        ),
        "option-2": OptionContractSnapshot(
            option_id="option-2",
            contract_symbol="EXM2",
            underlying="EXM",
            option_type="call",
            expiration_date=date(2026, 9, 11),
            strike=105,
            bid=0.95,
            ask=1.00,
            quote_at=now,
            underlying_price=100,
        ),
    }

    assert _option_premium_stop_ids([long, short], contracts) == {
        "option-1",
        "option-2",
    }


def test_option_cli_commands_are_registered():
    choices = build_parser()._subparsers._group_actions[0].choices
    assert {
        "option-migrate",
        "option-authorize-batch",
        "option-plan",
        "option-reserve",
        "option-reconcile",
        "option-sync",
    }.issubset(choices)


def test_option_sync_handles_expired_open_packet_and_atomic_close(
    monkeypatch, tmp_path
):
    now = datetime.now(UTC)
    packet = _packet(now)
    ledger = InMemoryLedger()
    ledger.authorize_option_packet(packet)
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )

    open_order = ProposedOptionOrder.from_dict(
        {
            **packet.to_dict(),
            "account_number": "111111111",
            "rationale": "Expired-packet sync fixture",
            "intent": "option_order",
        }
    )
    open_approval = {
        **open_order.to_dict(),
        "packet_id": packet.packet_id,
    }
    open_fill = {
        "id": "open-fill",
        "ref_id": open_order.ref_id,
        "state": "filled",
        "quantity": "1",
        "processed_quantity": "1",
        "average_price": "0.49",
        "direction": "debit",
        "legs": open_order.place_parameters()["legs"],
    }
    plan_path = tmp_path / "plan.json"
    executed_path = tmp_path / "executed.json"
    reconciliation_path = tmp_path / "reconciliation.json"
    equity_reconciliation_path = tmp_path / "equity-reconciliation.json"
    output_path = tmp_path / "sync.json"
    plan_path.write_text(
        json.dumps(
            {
                "account_number": "111111111",
                "approved_orders": [open_approval],
            }
        )
    )
    executed_path.write_text(json.dumps({"orders": [open_fill]}))
    reconciliation_path.write_text(
        json.dumps(
            reconcile_option_orders(
                [open_approval],
                [open_fill],
                root=tmp_path,
            )
        )
    )
    equity_reconciliation_path.write_text(json.dumps({"clean": True}))
    args = Namespace(
        plan=str(plan_path),
        executed=str(executed_path),
        reconciliation=str(reconciliation_path),
        equity_reconciliation=str(equity_reconciliation_path),
        output=str(output_path),
        root=str(tmp_path),
    )
    assert cli.command_option_sync(args) == 0
    assert ledger.option_positions(status="open")[0].option_id == "option-1"
    assert ledger.option_packet_states[packet.packet_id]["status"] == "consumed"
    assert cli.command_option_sync(args) == 0

    close_order = ProposedOptionOrder(
        account_number="111111111",
        option_id="option-1",
        chain_symbol="EXM",
        strategy="close",
        option_type="call",
        side="sell",
        position_effect="close",
        quantity=1,
        limit_price=0.40,
        bid_price=0.40,
        ask_price=0.42,
        quote_timestamp=now,
        expiration_date=packet.contract.expiration_date,
        strike_price=100.0,
        rationale="Close lifecycle fixture",
        order_date=now.date(),
    )
    close_approval = {
        **close_order.to_dict(),
        "packet_id": packet.packet_id,
    }
    close_fill = {
        "id": "close-fill",
        "ref_id": close_order.ref_id,
        "state": "filled",
        "quantity": "1",
        "processed_quantity": "1",
        "average_price": "0.41",
        "direction": "credit",
        "legs": close_order.place_parameters()["legs"],
    }
    plan_path.write_text(
        json.dumps(
            {
                "account_number": "111111111",
                "approved_orders": [close_approval],
            }
        )
    )
    executed_path.write_text(json.dumps({"orders": [close_fill]}))
    reconciliation_path.write_text(
        json.dumps(
            reconcile_option_orders(
                [close_approval],
                [close_fill],
                root=tmp_path,
            )
        )
    )
    assert cli.command_option_sync(args) == 0
    assert ledger.option_positions(status="closed")[0].option_id == "option-1"
    assert cli.command_option_sync(args) == 0
