from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import agentic_trader.cli as cli
from agentic_trader.cloud_runtime import InMemoryCloudRuntimeStore
from agentic_trader.confirmation import (
    confirmation_literal,
    generate_confirmation_key,
    sign_confirmation,
)
from agentic_trader.picker.ledger import InMemoryLedger


def _runtime_with_lease(monkeypatch):
    runtime = InMemoryCloudRuntimeStore()
    runtime.record_migrations(cli._migration_paths())
    now = datetime.now(UTC)
    lease = runtime.acquire_run_lease(
        task_name="morning-live",
        scheduled_for=now,
        git_sha="abc123",
        now=now,
    )
    assert lease is not None
    monkeypatch.setattr(
        cli.PostgresCloudRuntimeStore,
        "from_env",
        classmethod(lambda cls: runtime),
    )
    return runtime, lease


def test_cloud_cli_persists_review_confirmation_attempt_and_blocks_duplicate_placement(
    monkeypatch,
    tmp_path,
    capsys,
):
    account_number = "111111111"
    monkeypatch.setenv("AGENTIC_TRADER_ACCOUNT", account_number)
    monkeypatch.setenv("AGENTIC_TRADER_NET_DEPOSITS", "10000")
    runtime, lease = _runtime_with_lease(monkeypatch)
    ledger = InMemoryLedger()
    runtime.execution_reservations = ledger.execution_reservations
    runtime.control_states = ledger.controls
    today = cli._nyse_session_date()
    ledger.stage_batch(
        "batch-1",
        today,
        datetime.now(UTC),
        "a" * 64,
        "gpt-5.6-sol",
        {"drafts": [], "option_drafts": []},
    )
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    prior_close_date = cli._most_recent_completed_nyse_session()
    close_schedule = cli.mcal.get_calendar("NYSE").schedule(
        start_date=prior_close_date,
        end_date=prior_close_date,
    )
    close_at = close_schedule.iloc[0]["market_close"].to_pydatetime().astimezone(UTC)
    ledger.controls[cli.account_key(account_number)] = {
        "halted": False,
        "halt_reason": None,
        "halt_scope": "entries",
        "high_water_mark": 10_000.0,
        "prior_close_equity": 10_000.0,
        "prior_close_date": prior_close_date,
        "prior_close_metric_at": close_at,
        "prior_close_observed_at": close_at,
        "prior_close_source": "robinhood_official_regular_close",
        "prior_close_artifact_hash": "d" * 64,
        "cooldown_until": None,
    }
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
            "net_deposits": 10_000.0,
            "broker_positions": [{"symbol": "EFA", "quantity": {"amount": "1"}}],
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
            "quote_timestamps": {
                "SPY": datetime.now(UTC).isoformat(),
                "EFA": datetime.now(UTC).isoformat(),
            },
            "quote_spreads_bps": {"SPY": 1.0},
        },
        "prices": {"SPY": 500.0, "EFA": 100.0},
        "targets": {"SPY": 0.01, "EFA": 0.01},
        "research_batch_id": "batch-1",
        "sector_taxonomy": {
            "source": "agentic_trader_code_owned",
            "version": "agentic-gics-v1",
            "mapping": {"SPY": "broad_market", "EFA": "broad_market"},
        },
        "instrument_metadata": {
            "SPY": {"source": "robinhood_scanner", "asset_type": "etf"},
            "EFA": {"source": "robinhood_scanner", "asset_type": "etf"},
        },
    }
    request_path = tmp_path / "request.json"
    plan_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps(request))
    plan_args = Namespace(
        request=str(request_path),
        root=str(tmp_path),
        max_order_notional=100.0,
        max_position_weight=0.035,
        max_orders_per_day=8,
        max_daily_notional=800.0,
        max_entry_orders_per_day=1,
        max_entry_daily_notional=100.0,
        rebalance_threshold=0.005,
        record_equity=False,
        persist=True,
        run_id=lease.run_id,
        lease_token=lease.lease_token,
        output=str(plan_path),
    )

    stale_plan_request = json.loads(json.dumps(request))
    stale_plan_request["account"]["quote_timestamps"]["EFA"] = (
        datetime.now(UTC) - timedelta(seconds=30)
    ).isoformat()
    stale_plan_request_path = tmp_path / "stale-holding-plan-request.json"
    stale_plan_output_path = tmp_path / "stale-holding-plan.json"
    stale_plan_request_path.write_text(json.dumps(stale_plan_request))
    stale_plan_args = Namespace(
        **{
            **vars(plan_args),
            "request": str(stale_plan_request_path),
            "output": str(stale_plan_output_path),
        }
    )
    assert cli.command_live_plan(stale_plan_args) == 2
    stale_plan = json.loads(stale_plan_output_path.read_text())
    assert "holding_quote_stale:EFA" in {
        reason for rejected in stale_plan["rejected_orders"] for reason in rejected["reasons"]
    }

    incomplete_native = json.loads(json.dumps(request))
    incomplete_native["account"].pop("buying_power")
    incomplete_native["account"].pop("pending_deposits")
    incomplete_path = tmp_path / "incomplete-native.json"
    incomplete_plan_path = tmp_path / "incomplete-plan.json"
    incomplete_path.write_text(json.dumps(incomplete_native))
    incomplete_args = Namespace(
        **{
            **vars(plan_args),
            "request": str(incomplete_path),
            "output": str(incomplete_plan_path),
        }
    )
    assert cli.command_live_plan(incomplete_args) == 2
    incomplete_plan = json.loads(incomplete_plan_path.read_text())
    incomplete_reasons = {
        reason for rejected in incomplete_plan["rejected_orders"] for reason in rejected["reasons"]
    }
    assert "native_buying_power_missing_or_invalid" in incomplete_reasons
    assert "pending_deposits_missing_or_invalid" in incomplete_reasons

    assert cli.command_live_plan(plan_args) == 0
    plan = json.loads(plan_path.read_text())
    stdout = capsys.readouterr().out
    assert account_number not in stdout
    assert f"••••{account_number[-4:]}" in stdout
    assert plan["cloud_persisted"] is True
    assert plan["execution_limits"]["max_order_notional"] == 100.0
    assert plan["execution_limits"]["max_entry_orders_per_day"] == 1
    assert plan["execution_limits"]["max_entry_daily_notional"] == 100.0
    order = plan["approved_orders"][0]

    reviews_path = tmp_path / "reviews.json"
    reviews_path.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "ref_id": order["ref_id"],
                        "broker_parameters": order["broker_parameters"],
                        "broker_response": {
                            "broker_parameters": order["broker_parameters"],
                            "order_checks": {},
                            "quote_data": {"symbol": "SPY", "ask_price": 500.0},
                            "market_data_disclosure": "fixture disclosure",
                            "native_response": {
                                "symbol": "SPY",
                                "side": "buy",
                                "type": "market",
                                "trigger": "immediate",
                                "dollar_based_amount": {"amount": "100.00"},
                                "order_checks": {},
                                "quote_data": {
                                    "symbol": "SPY",
                                    "ask_price": 500.0,
                                },
                                "market_data_disclosure": "fixture disclosure",
                            },
                        },
                        "broker_provenance": {
                            "broker": "Robinhood",
                            "tool": "review_equity_order",
                        },
                    }
                ]
            }
        )
    )
    review_output = tmp_path / "review-record.json"
    assert (
        cli.command_live_review_record(
            Namespace(
                plan_id=plan["plan_id"],
                draft_hash=plan["draft_hash"],
                reviews=str(reviews_path),
                output=str(review_output),
            )
        )
        == 0
    )
    review = json.loads(review_output.read_text())
    private_key = tmp_path / "confirmation.pem"
    public_key = generate_confirmation_key(private_key)
    monkeypatch.setenv("AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY", public_key)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: f"SIGN {plan['plan_id']} {review['review_hash']}",
    )
    assert (
        cli.command_confirmation_sign(
            Namespace(
                private_key=str(private_key),
                plan_id=plan["plan_id"],
                plan_hash=review["review_hash"],
            )
        )
        == 0
    )
    signer_output = capsys.readouterr().out
    assert '"market_data_disclosure": "fixture disclosure"' in signer_output
    assert '"native_order_checks"' in signer_output
    assert '"exact_broker_parameters"' in signer_output
    signature = sign_confirmation(private_key, plan["plan_id"], review["review_hash"])
    confirmation_text = confirmation_literal(plan["plan_id"], review["review_hash"], signature)
    assert (
        cli.command_live_confirm(
            Namespace(
                plan_id=plan["plan_id"],
                plan_hash=review["review_hash"],
                confirmation_text=confirmation_text,
            )
        )
        == 0
    )
    confirmation = runtime.confirmations[plan["plan_id"]]

    reservation_path = tmp_path / "reservation.json"
    reserve_args = Namespace(
        plan=str(plan_path),
        plan_id=plan["plan_id"],
        plan_hash=review["review_hash"],
        confirmation_id=confirmation.confirmation_id,
        snapshot=str(request_path),
        root=str(tmp_path),
        output=str(reservation_path),
    )
    stale_snapshot = json.loads(json.dumps(request))
    stale_snapshot["account"]["quote_timestamps"]["EFA"] = (
        datetime.now(UTC) - timedelta(seconds=30)
    ).isoformat()
    stale_snapshot_path = tmp_path / "stale-holding-snapshot.json"
    stale_snapshot_path.write_text(json.dumps(stale_snapshot))
    with pytest.raises(ValueError, match="holding_quote_stale:EFA"):
        cli.command_live_reserve(
            Namespace(**{**vars(reserve_args), "snapshot": str(stale_snapshot_path)})
        )

    assert cli.command_live_reserve(reserve_args) == 0
    reservation = json.loads(reservation_path.read_text())
    assert reservation["reserved_ref_ids"] == [order["ref_id"]]
    assert reservation["usage"]["entry_orders"] == 1
    assert reservation["usage"]["entry_notional"] == 100.0
    assert reservation["attempts"][0]["broker_parameters"] == order["broker_parameters"]
    attempt_id = reservation["attempts"][0]["attempt_id"]
    assert runtime.attempts[attempt_id].state == "prepared"

    # An exact retry repairs a crash after attempt creation or reservation.
    assert cli.command_live_reserve(reserve_args) == 0
    duplicate = json.loads(reservation_path.read_text())
    assert duplicate["reserved_ref_ids"] == [order["ref_id"]]
    assert duplicate["blocked_ref_ids"] == []
    assert duplicate["attempts"][0]["attempt_id"] == attempt_id
    assert duplicate["attempts"][0]["newly_prepared"] is False

    # A disposable executor can recover after the original freshness proof
    # expires by fully revalidating a new broker snapshot. Budget and attempt
    # identity remain unchanged.
    old_snapshot_hash = duplicate["validation_snapshot_hash"]
    ledger.execution_reservations[order["ref_id"]]["validated_at"] = datetime.now(UTC) - timedelta(
        seconds=20
    )
    request["account"]["quote_timestamps"]["SPY"] = datetime.now(UTC).isoformat()
    request_path.write_text(json.dumps(request))
    assert cli.command_live_reserve(reserve_args) == 0
    recovered = json.loads(reservation_path.read_text())
    assert recovered["validation_snapshot_hash"] != old_snapshot_hash
    assert recovered["usage"]["entry_orders"] == 1
    assert recovered["usage"]["entry_notional"] == 100.0
    assert recovered["attempts"][0]["attempt_id"] == attempt_id
    assert (
        ledger.execution_reservations[order["ref_id"]]["validation_snapshot_hash"]
        == recovered["validation_snapshot_hash"]
    )
    reservation = recovered

    assert (
        cli.command_live_attempt_claim(
            Namespace(
                attempt_id=attempt_id,
                plan_id=plan["plan_id"],
                plan_hash=review["review_hash"],
                confirmation_id=confirmation.confirmation_id,
                ref_id=order["ref_id"],
                validation_snapshot_hash=reservation["validation_snapshot_hash"],
            )
        )
        == 0
    )
    assert runtime.attempts[attempt_id].state == "submitting"

    original_fill = {
        **order["broker_parameters"],
        "id": "broker-order-1",
        "state": "filled",
        "trigger": "immediate",
        "dollar_based_amount": {"amount": "100.00"},
        "cumulative_quantity": {"amount": "0.2"},
        "average_price": {"amount": "500.00"},
        "mutable_metadata": {"revision": 1},
    }
    runtime.transition_order_attempt(
        attempt_id,
        "filled",
        response=original_fill,
        broker_order_id="broker-order-1",
    )
    executed_path = tmp_path / "executed.json"
    executed_path.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        **original_fill,
                        "mutable_metadata": {"revision": 2},
                    }
                ]
            }
        )
    )
    reconcile_output = tmp_path / "reconciliation.json"
    assert (
        cli.command_live_reconcile(
            Namespace(
                plan_id=plan["plan_id"],
                plan="",
                executed=str(executed_path),
                root=str(tmp_path),
                output=str(reconcile_output),
            )
        )
        == 0
    )
    assert json.loads(reconcile_output.read_text())["clean"] is True
    assert runtime.attempts[attempt_id].state == "filled"
    assert runtime.attempts[attempt_id].latest_response == original_fill

    assert cli.command_live_reserve(reserve_args) == 2
    blocked = json.loads(reservation_path.read_text())
    assert blocked["blocked_ref_ids"] == [order["ref_id"]]


def test_cloud_cli_rejects_nonliteral_confirmation(monkeypatch):
    runtime, _ = _runtime_with_lease(monkeypatch)
    assert runtime is not None
    args = Namespace(
        plan_id="plan",
        plan_hash="a" * 64,
        confirmation_text="yes",
    )
    try:
        cli.command_live_confirm(args)
    except ValueError as error:
        assert "Exact signed confirmation" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Ambiguous confirmation was accepted")


def test_durable_artifact_redaction_masks_accounts_and_secrets():
    redacted = cli._redact_durable_payload(
        {
            "account": {
                "account_number": "123456789",
                "rhs_account_number": "987654321",
                "nested": {"rhc_account_id": "account-secret-id"},
                "cash": 100.0,
            },
            "access_token": "do-not-store",
            "evidence_url": "https://issuer.example/news",
        }
    )

    assert redacted == {
        "account": {
            "account_number": "••••6789",
            "rhs_account_number": "••••4321",
            "nested": {"rhc_account_id": "••••t-id"},
            "cash": 100.0,
        },
        "access_token": "<redacted>",
        "evidence_url": "https://issuer.example/news",
    }


def test_native_cash_requires_unleveraged_buying_power_and_pending_deposits():
    account = {
        "type": "cash",
        "cash": 125.0,
        "broker_orders": [],
        "broker_option_orders": [],
        "broker_advanced_orders_complete_for_session": True,
    }
    cash, reasons = cli._native_settled_cash(account)
    assert cash == 125.0
    assert "native_buying_power_missing_or_invalid" in reasons
    assert "pending_deposits_missing_or_invalid" in reasons

    account.update(
        {
            "buying_power": {
                "buying_power": 100.0,
                "unleveraged_buying_power": 100.0,
                "intraday_buying_power": 100.0,
                "off_intraday_buying_power": 100.0,
            },
            "pending_deposits": 10.0,
        }
    )
    cash, reasons = cli._native_settled_cash(account)
    assert cash == 90.0
    assert "cash_without_margin_sources_contradict" in reasons
    assert "native_buying_power_missing_or_invalid" not in reasons
    assert "pending_deposits_missing_or_invalid" not in reasons


def test_code_owned_health_care_taxonomy_and_buy_universe_are_not_expandable():
    sectors, durable = cli._validated_sector_taxonomy(
        {
            "source": "agentic_trader_code_owned",
            "version": cli.SECTOR_TAXONOMY_VERSION,
            "mapping": {"LLY": "health_care", "XLV": "health_care"},
        },
        {"LLY", "XLV"},
    )
    assert sectors == {"LLY": "health_care", "XLV": "health_care"}
    assert durable["mapping"] == sectors
    with pytest.raises(ValueError, match="noncanonical"):
        cli._validated_sector_taxonomy(
            {
                "source": "agentic_trader_code_owned",
                "version": cli.SECTOR_TAXONOMY_VERSION,
                "mapping": {"LLY": "healthcare"},
            },
            {"LLY"},
        )
    reasons = cli._entry_broker_guard_reasons(
        "LLY",
        {},
        {"LLY": {"source": "robinhood_scanner", "asset_type": "stock"}},
        [],
        [],
    )
    assert "buy_symbol_outside_code_owned_measured_universe" in reasons


def test_picker_fill_fingerprint_ignores_mutable_broker_metadata():
    fill = {
        "id": "broker-1",
        "symbol": "SPY",
        "side": "sell",
        "cumulative_quantity": {"amount": "1"},
        "average_price": {"amount": "500.25"},
        "last_transaction_at": "2026-08-22T00:05:00Z",
        "state": "filled",
        "fees": [],
    }
    changed = {
        **fill,
        "state": "partially_filled_rest_cancelled",
        "fees": [{"amount": "0.01"}],
        "updated_at": "2026-08-22T00:10:00Z",
    }
    assert cli._picker_fill_fingerprint(fill) == cli._picker_fill_fingerprint(changed)
    # 20:05 ET belongs to the following 24-hour trading session.
    assert cli._nyse_session_date(datetime(2026, 8, 22, 0, 5, tzinfo=UTC)).isoformat() == (
        "2026-08-24"
    )


def test_picker_sync_recovers_crash_after_managed_thesis_close(
    monkeypatch,
    tmp_path,
):
    ledger = InMemoryLedger()
    ledger.upsert_thesis(
        cli.ActiveThesis(
            pick_id="pick-1",
            packet_id="pick-1",
            symbol="SPY",
            sector="broad_market",
            status="closed",
            entry_date=date(2026, 8, 20),
            expiry_date=date(2026, 9, 20),
            entry_price=500.0,
            entry_spy_price=500.0,
            target_weight=0.01,
            stop_loss_pct=0.05,
            sector_relative_stop_pct=0.05,
        )
    )
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    fill_at = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
    plan = cli.ExecutionPlan(
        plan_id="plan-1",
        draft_hash="a" * 64,
        run_id="run-1",
        account_key="account-hash",
        trade_date=date(2026, 8, 24),
        research_batch_id="batch-1",
        snapshot_hash="b" * 64,
        planned_at=fill_at - timedelta(minutes=1),
        expires_at=fill_at + timedelta(minutes=4),
        status="submitted",
        payload={
            "approved_orders": [
                {
                    "ref_id": "ref-1",
                    "pick_id": "pick-1",
                    "intent_class": "mandatory_exit",
                    "symbol": "SPY",
                    "side": "sell",
                }
            ],
            "packet_trade_dates": {},
            "prices": {"SPY": 500.0},
            "account_key": "account-hash",
            "legacy_position_closes": [],
        },
    )
    reconciliation = {
        "clean": True,
        "matched": [{"ref_id": "ref-1", "order_id": "broker-1"}],
    }

    class CloudStore:
        def get_plan(self, plan_id):
            assert plan_id == "plan-1"
            return plan

        def latest_reconciliation(self, plan_id):
            assert plan_id == "plan-1"
            return SimpleNamespace(payload=reconciliation)

        def nonterminal_attempts(self, account_hash):
            assert account_hash == "account-hash"
            return []

    monkeypatch.setattr(
        cli.PostgresCloudRuntimeStore,
        "from_env",
        classmethod(lambda cls: CloudStore()),
    )
    executed_path = tmp_path / "executed.json"
    executed_path.write_text(
        json.dumps(
            [
                {
                    "id": "broker-1",
                    "state": "filled",
                    "symbol": "SPY",
                    "side": "sell",
                    "cumulative_quantity": {"amount": "1"},
                    "average_price": {"amount": "501.00"},
                    "last_transaction_at": fill_at.isoformat(),
                }
            ]
        )
    )
    output_path = tmp_path / "picker-sync.json"
    assert (
        cli.command_picker_sync(
            Namespace(
                plan_id="plan-1",
                plan="",
                reconciliation="",
                executed=str(executed_path),
                output=str(output_path),
            )
        )
        == 0
    )
    event = ledger.equity_order_event("ref-1", "exit_filled")
    assert event is not None
    assert event["broker_order_id"] == "broker-1"
    assert json.loads(output_path.read_text())["transitions"] == [
        {"pick_id": "pick-1", "status": "closed_event_recovered"}
    ]


def test_daily_usage_unions_proven_overlap_and_sums_disjoint_manual_orders():
    broker_order = {
        "id": "broker-1",
        "symbol": "SPY",
        "side": "buy",
        "state": "filled",
        "dollar_based_amount": {"amount": "100.00"},
    }
    overlap = {
        "ref_id": "internal-ref",
        "broker_order_id": "broker-1",
        "notional": 100.0,
        "is_entry": True,
        "is_option_open": False,
    }
    assert cli._union_broker_and_reservation_usage([broker_order], [], [overlap]) == (
        1,
        100.0,
        1,
        100.0,
    )
    disjoint = {**overlap, "ref_id": "other-ref", "broker_order_id": "broker-2"}
    assert cli._union_broker_and_reservation_usage([broker_order], [], [disjoint]) == (
        2,
        200.0,
        2,
        200.0,
    )


def test_cloud_run_windows_are_bound_to_pacific_wall_clock():
    morning = datetime(2026, 8, 24, 13, 35, tzinfo=UTC)
    evening = datetime(2026, 8, 24, 1, 15, tzinfo=UTC)
    cli._validate_cloud_run_window(
        "morning-live",
        morning,
        now=morning + timedelta(minutes=1),
    )
    cli._validate_cloud_run_window(
        "evening-live",
        evening,
        now=evening + timedelta(minutes=1),
    )
    cli._validate_cloud_run_window(
        "interactive-review:plan-1",
        datetime.now(UTC) - timedelta(seconds=10),
    )

    try:
        cli._validate_cloud_run_window(
            "morning-live",
            datetime(2026, 8, 24, 6, 35, tzinfo=UTC),
            now=datetime(2026, 8, 24, 6, 36, tzinfo=UTC),
        )
    except ValueError as error:
        assert "06:35 Pacific" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("A UTC-misconfigured morning trigger was accepted")
