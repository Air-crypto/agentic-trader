from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from agentic_trader.cli import (
    _business_days_until,
    _option_account_snapshot,
    _option_equity_constraints,
    _option_premium_stop_ids,
    build_parser,
)
from agentic_trader.picker.option_models import (
    ActiveOptionPosition,
    OptionContractSnapshot,
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
    )
    assert snapshot.orders_today == 3
    assert snapshot.orders_source == "broker"
    assert snapshot.external_halt_reasons == ()


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
    assert _business_days_until(date(2026, 8, 7), date(2026, 8, 10)) == 1


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
