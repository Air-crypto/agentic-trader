from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentic_trader.cloud_runtime import (
    REQUIRED_PRIVATE_PUBLIC_OBJECTS,
    ExecutionPlan,
    InMemoryCloudRuntimeStore,
    PostgresCloudRuntimeStore,
    _nyse_execution_session,
    canonical_hash,
)
from agentic_trader.confirmation import (
    CONFIRMATION_PUBLIC_KEY_ENV,
    confirmation_message,
    public_key_text,
)


@pytest.fixture
def confirmation_signer(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        CONFIRMATION_PUBLIC_KEY_ENV,
        public_key_text(private_key.public_key()),
    )

    def sign(plan_id: str, review_hash: str) -> str:
        signature = private_key.sign(confirmation_message(plan_id, review_hash).encode())
        return base64.b64encode(signature).decode()

    return sign


def _lease(store: InMemoryCloudRuntimeStore, now: datetime):
    lease = store.acquire_run_lease(
        task_name="morning-live",
        scheduled_for=now,
        git_sha="abc123",
        now=now,
    )
    assert lease is not None
    return lease


def _plan(store: InMemoryCloudRuntimeStore, now: datetime):
    lease = _lease(store, now)
    broker_parameters = {
        "symbol": "SPY",
        "side": "buy",
        "type": "limit",
        "quantity": "1",
        "limit_price": "500.00",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    payload = {
        "planned_at": (now + timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "trade_date": _nyse_execution_session(now + timedelta(seconds=1)).isoformat(),
        "research_batch_id": "batch-1",
        "approved_orders": [
            {
                "ref_id": "ref-1",
                "symbol": "SPY",
                "side": "buy",
                "notional": 500.0,
                "broker_parameters": broker_parameters,
            }
        ],
        "rejected_orders": [],
        "broker_authority": {"version": "test-v1", "account_key": "account-hash"},
        "account_last_four": "1234",
    }
    snapshot = {
        "account_key": "account-hash",
        "positions": [],
        "orders": [],
        "quote": {"SPY": 500.0},
    }
    plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key="account-hash",
        snapshot=snapshot,
        payload=payload,
    )
    store.persist_plan(plan, lease.lease_token)
    review_payload = {
        "reviews": [
            {
                "ref_id": "ref-1",
                "broker_parameters": broker_parameters,
                "broker_response": {
                    "broker_parameters": broker_parameters,
                    "order_checks": {},
                    "quote_data": {
                        "symbol": "SPY",
                        "bid_price": "499.90",
                        "ask_price": "500.10",
                    },
                    "market_data_disclosure": "Test disclosure",
                    "native_response": {
                        "symbol": "SPY",
                        "side": "buy",
                        "type": "limit",
                        "trigger": "immediate",
                        "quantity": "1",
                        "price": "500.00",
                        "order_checks": {},
                        "quote_data": {
                            "symbol": "SPY",
                            "bid_price": "499.90",
                            "ask_price": "500.10",
                        },
                        "market_data_disclosure": "Test disclosure",
                    },
                },
                "broker_provenance": {
                    "broker": "Robinhood",
                    "tool": "review_equity_order",
                },
            }
        ]
    }
    return lease, plan, review_payload, broker_parameters


def _confirmed_attempt(store, now, confirmation_signer):
    _, plan, review_payload, broker_parameters = _plan(store, now)
    review = store.record_plan_review(
        plan.plan_id,
        plan.draft_hash,
        review_payload,
        reviewed_at=now + timedelta(seconds=2),
    )
    confirmation = store.record_confirmation(
        plan.plan_id,
        review.review_hash,
        confirmation_signer(plan.plan_id, review.review_hash),
        confirmed_at=now + timedelta(seconds=3),
    )
    attempt, _ = store.create_order_attempt(
        plan_id=plan.plan_id,
        confirmation_id=confirmation.confirmation_id,
        review_hash=review.review_hash,
        ref_id="ref-1",
        broker_request=broker_parameters,
        now=now + timedelta(seconds=4),
    )
    snapshot_hash = "b" * 64
    store.execution_reservations["ref-1"] = {
        "ref_id": "ref-1",
        "account_key": plan.account_key,
        "trade_date": plan.trade_date,
        "notional": 500.0,
        "is_entry": True,
        "is_option_open": False,
        "plan_id": plan.plan_id,
        "confirmation_id": confirmation.confirmation_id,
        "attempt_id": attempt.attempt_id,
        "validated_at": now + timedelta(seconds=4),
        "validation_snapshot_hash": snapshot_hash,
        "authority_fingerprint_hash": canonical_hash(plan.payload["broker_authority"]),
    }
    return plan, review, confirmation, attempt, snapshot_hash


class _ScriptedCursor:
    def __init__(self, responses: list[list[tuple[Any, ...]]]) -> None:
        self._responses = iter(responses)
        self._current: list[tuple[Any, ...]] = []
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        return False

    def execute(self, query: str, parameters: object = None) -> None:
        self.queries.append(query)
        self._current = next(self._responses)

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


class _ScriptedConnection:
    def __init__(self, cursor: _ScriptedCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        return False

    def cursor(self):
        return self._cursor


class _TransactionalMigrationDatabase:
    def __init__(self, applied: dict[str, str] | None = None) -> None:
        self.applied = dict(applied or {})
        self.effects: list[str] = []
        self.connection_count = 0

    def connect(self):
        self.connection_count += 1
        return _TransactionalMigrationConnection(self)


class _TransactionalMigrationConnection:
    def __init__(self, database: _TransactionalMigrationDatabase) -> None:
        self.database = database
        self.pending_applied = dict(database.applied)
        self.pending_effects = list(database.effects)

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if error_type is None:
            self.database.applied = self.pending_applied
            self.database.effects = self.pending_effects
        return False

    def cursor(self):
        return _TransactionalMigrationCursor(self)


class _TransactionalMigrationCursor:
    def __init__(self, connection: _TransactionalMigrationConnection) -> None:
        self.connection = connection
        self._current: list[tuple[Any, ...]] = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        return False

    def execute(self, query: str, parameters: object = None) -> None:
        self._current = []
        self.rowcount = -1
        normalized = " ".join(query.split())
        if "MIGRATION_FAIL" in query:
            raise RuntimeError("simulated migration failure")
        if "MIGRATION_" in query:
            marker = query.split("MIGRATION_", 1)[1].split()[0]
            self.connection.pending_effects.append(marker)
            return
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            self._current = [(None,)]
            return
        if normalized.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            return
        if normalized == "SELECT to_regclass('public.schema_migrations')":
            self._current = [("schema_migrations",)]
            return
        if normalized == "SELECT version, checksum FROM schema_migrations":
            self._current = list(self.connection.pending_applied.items())
            return
        if normalized.startswith("INSERT INTO schema_migrations"):
            version, checksum = parameters
            if version in self.connection.pending_applied:
                self.rowcount = 0
            else:
                self.connection.pending_applied[str(version)] = str(checksum)
                self.rowcount = 1
            return
        if normalized.startswith("SELECT checksum FROM schema_migrations"):
            version = str(parameters[0])
            self._current = [(self.connection.pending_applied[version],)]
            return
        raise AssertionError(f"Unexpected SQL in transaction test: {normalized}")

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


def test_schema_status_detects_missing_and_checksum_drift(tmp_path):
    first = tmp_path / "001.sql"
    second = tmp_path / "002.sql"
    first.write_text("SELECT 1;\n")
    second.write_text("SELECT 2;\n")
    store = InMemoryCloudRuntimeStore()

    status = store.schema_status([first, second])
    assert status.missing == ("001.sql", "002.sql")
    store.record_migrations([first, second])
    assert store.assert_schema_current([first, second]).current

    third = tmp_path / "003.sql"
    third.write_text("SELECT 3;\n")
    store.record_migrations([third])
    unexpected = store.schema_status([first, second])
    assert not unexpected.current
    assert unexpected.unexpected == ("003.sql",)
    with pytest.raises(RuntimeError, match="unexpected=.*003.sql"):
        store.assert_schema_current([first, second])

    first.write_text("SELECT 3;\n")
    drifted = store.schema_status([first, second])
    assert drifted.drifted == ("001.sql",)
    with pytest.raises(RuntimeError, match="not current"):
        store.assert_schema_current([first, second])


def test_postgres_security_attestation_covers_all_private_object_classes():
    inventory = [
        (object_class, identity, object_kind, "postgres", None)
        for object_class, identity, object_kind in sorted(REQUIRED_PRIVATE_PUBLIC_OBJECTS)
    ]
    inventory.append(("relation", "public.schema_migrations", "r", "postgres", True))
    cursor = _ScriptedCursor(
        [
            [("postgres",)],
            [("anon",), ("authenticated",), ("service_role",)],
            inventory,
            [],
            [],
        ]
    )

    assert PostgresCloudRuntimeStore._security_posture_violations(cursor) == ()
    query_text = "\n".join(cursor.queries)
    assert "relrowsecurity" in query_text
    assert "has_table_privilege" in query_text
    assert "has_any_column_privilege" in query_text
    assert "has_sequence_privilege" in query_text
    assert "has_function_privilege" in query_text
    assert "has_schema_privilege" in query_text


def test_postgres_security_attestation_reports_each_fail_closed_boundary():
    inventory = [
        (object_class, identity, object_kind, "postgres", None)
        for object_class, identity, object_kind in sorted(REQUIRED_PRIVATE_PUBLIC_OBJECTS)
    ]
    view_index = next(
        index for index, row in enumerate(inventory) if row[1] == "public.learning_current_state"
    )
    inventory[view_index] = (
        "relation",
        "public.learning_current_state",
        "v",
        "supabase_admin",
        None,
    )
    inventory.append(("relation", "public.schema_migrations", "r", "postgres", False))
    cursor = _ScriptedCursor(
        [
            [("postgres",)],
            [("anon",), ("authenticated",)],
            inventory,
            [("relation", "public.learning_current_state", "anon", "SELECT")],
            [("anon",)],
        ]
    )

    violations = PostgresCloudRuntimeStore._security_posture_violations(cursor)

    assert "missing_api_role:service_role" in violations
    assert (
        "object_owner_mismatch:relation:public.learning_current_state:supabase_admin" in violations
    )
    assert "rls_disabled:public.schema_migrations" in violations
    assert "effective_privilege:relation:public.learning_current_state:anon:SELECT" in violations
    assert "schema_create_privilege:anon" in violations


def test_postgres_schema_status_is_not_current_when_security_attestation_fails(
    tmp_path,
    monkeypatch,
):
    migration = tmp_path / "001_first.sql"
    migration.write_text("SELECT 1;\n")
    store = PostgresCloudRuntimeStore("postgresql://example.invalid/runtime")
    checksum = canonical_hash("placeholder")
    monkeypatch.setattr(
        store,
        "_read_applied_migrations",
        lambda cursor: {migration.name: checksum},
    )
    monkeypatch.setattr(
        store,
        "_security_posture_violations",
        lambda cursor: ("effective_privilege:relation:public.private:anon:SELECT",),
    )
    monkeypatch.setattr(
        "agentic_trader.cloud_runtime._migration_checksum",
        lambda path: checksum,
    )

    status = store._schema_status_from_cursor([migration], object())

    assert not status.current
    assert not status.missing
    assert not status.drifted
    assert status.security_violations == (
        "effective_privilege:relation:public.private:anon:SELECT",
    )


def test_postgres_migrations_commit_all_files_and_checksums_in_one_transaction(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "001_first.sql"
    second = tmp_path / "002_second.sql"
    first.write_text("-- MIGRATION_FIRST\nSELECT 1;\n")
    second.write_text("-- MIGRATION_SECOND\nSELECT 2;\n")
    database = _TransactionalMigrationDatabase()
    store = PostgresCloudRuntimeStore("postgresql://example.invalid/runtime")
    monkeypatch.setattr(store, "_connect", database.connect)
    monkeypatch.setattr(store, "_security_posture_violations", lambda cursor: ())

    status = store.apply_migrations([first, second])

    assert status.current
    assert database.connection_count == 1
    assert database.effects == ["FIRST", "SECOND"]
    assert set(database.applied) == {"001_first.sql", "002_second.sql"}
    assert database.applied == status.expected


def test_postgres_migration_failure_rolls_back_every_file_and_ledger_insert(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "001_first.sql"
    second = tmp_path / "002_second.sql"
    first.write_text("-- MIGRATION_FIRST\nSELECT 1;\n")
    second.write_text("-- MIGRATION_FAIL\nSELECT 2;\n")
    database = _TransactionalMigrationDatabase()
    store = PostgresCloudRuntimeStore("postgresql://example.invalid/runtime")
    monkeypatch.setattr(store, "_connect", database.connect)
    monkeypatch.setattr(store, "_security_posture_violations", lambda cursor: ())

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        store.apply_migrations([first, second])

    assert database.connection_count == 1
    assert database.effects == []
    assert database.applied == {}


def test_postgres_migrations_reject_unexpected_ledger_rows_before_ddl(
    tmp_path,
    monkeypatch,
):
    migration = tmp_path / "001_first.sql"
    migration.write_text("-- MIGRATION_FIRST\nSELECT 1;\n")
    database = _TransactionalMigrationDatabase({"999_unknown.sql": "0" * 64})
    store = PostgresCloudRuntimeStore("postgresql://example.invalid/runtime")
    monkeypatch.setattr(store, "_connect", database.connect)
    monkeypatch.setattr(store, "_security_posture_violations", lambda cursor: ())

    with pytest.raises(RuntimeError, match="Unexpected applied migrations"):
        store.apply_migrations([migration])

    assert database.effects == []
    assert database.applied == {"999_unknown.sql": "0" * 64}


def test_postgres_migrations_reject_checksum_drift_before_ddl(tmp_path, monkeypatch):
    migration = tmp_path / "001_first.sql"
    migration.write_text("-- MIGRATION_FIRST\nSELECT 1;\n")
    database = _TransactionalMigrationDatabase({migration.name: "0" * 64})
    store = PostgresCloudRuntimeStore("postgresql://example.invalid/runtime")
    monkeypatch.setattr(store, "_connect", database.connect)
    monkeypatch.setattr(store, "_security_posture_violations", lambda cursor: ())

    with pytest.raises(RuntimeError, match="Migration checksum drift"):
        store.apply_migrations([migration])

    assert database.effects == []
    assert database.applied == {migration.name: "0" * 64}


def test_run_lease_blocks_overlap_reclaims_expiry_and_seals_completion():
    store = InMemoryCloudRuntimeStore()
    now = datetime(2026, 8, 21, 13, 35, tzinfo=UTC)
    first = _lease(store, now)

    assert (
        store.acquire_run_lease(
            task_name="morning-live",
            scheduled_for=now,
            git_sha="abc123",
            now=now + timedelta(seconds=30),
        )
        is None
    )
    reclaimed = store.acquire_run_lease(
        task_name="morning-live",
        scheduled_for=now,
        git_sha="def456",
        lease_seconds=60,
        now=first.lease_expires_at,
    )
    assert reclaimed is not None
    assert reclaimed.run_id == first.run_id
    assert reclaimed.lease_token != first.lease_token

    store.release_run_lease(
        reclaimed.run_id,
        reclaimed.lease_token,
        status="completed",
        now=reclaimed.heartbeat_at + timedelta(seconds=1),
    )
    assert (
        store.acquire_run_lease(
            task_name="morning-live",
            scheduled_for=now,
            git_sha="ghi789",
            now=reclaimed.lease_expires_at,
        )
        is None
    )


def test_persist_plan_rejects_a_lease_expired_at_real_write_time():
    store = InMemoryCloudRuntimeStore()
    started_at = datetime.now(UTC) - timedelta(hours=3)
    lease = _lease(store, started_at)
    plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key="account-hash",
        snapshot={"account_key": "account-hash"},
        payload={
            "planned_at": (started_at + timedelta(seconds=1)).isoformat(),
            "expires_at": (started_at + timedelta(minutes=5)).isoformat(),
            "approved_orders": [],
        },
    )

    with pytest.raises(RuntimeError, match="active durable run lease"):
        store.persist_plan(plan, lease.lease_token)


def test_persist_plan_rejects_multiple_orders_before_review():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    lease, persisted, _, _ = _plan(store, now)
    payload = json.loads(json.dumps(persisted.payload))
    second = json.loads(json.dumps(payload["approved_orders"][0]))
    second["ref_id"] = "ref-2"
    second["broker_parameters"]["symbol"] = "QQQ"
    second["symbol"] = "QQQ"
    payload["approved_orders"].append(second)
    plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key=persisted.account_key,
        snapshot={"account_key": persisted.account_key, "version": 2},
        payload=payload,
    )

    with pytest.raises(ValueError, match="at most one order"):
        store.persist_plan(plan, lease.lease_token)


def test_interactive_review_inherits_morning_regular_hours_contract():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, parent, _, _ = _plan(store, now)
    interactive = store.acquire_run_lease(
        task_name=f"interactive-review:{parent.plan_id}",
        scheduled_for=now + timedelta(seconds=2),
        git_sha="abc123",
        now=now + timedelta(seconds=2),
    )
    assert interactive is not None
    payload = json.loads(json.dumps(parent.payload))
    payload["approved_orders"][0]["ref_id"] = "interactive-ref"
    payload["approved_orders"][0]["broker_parameters"]["market_hours"] = "all_day_hours"
    child = ExecutionPlan.build(
        run_id=interactive.run_id,
        account_key=parent.account_key,
        snapshot={"account_key": parent.account_key, "interactive": True},
        payload=payload,
    )

    with pytest.raises(ValueError, match="regular-hours"):
        store.persist_plan(child, interactive.lease_token)


def test_evening_task_and_holiday_session_are_lease_enforced():
    store = InMemoryCloudRuntimeStore()
    # Sunday evening before Labor Day maps to Tuesday's NYSE session, despite
    # the plan being Monday in UTC.
    planned_at = datetime(2026, 9, 7, 1, 15, tzinfo=UTC)
    lease = store.acquire_run_lease(
        task_name="evening-live",
        scheduled_for=planned_at,
        git_sha="abc123",
        now=planned_at,
    )
    assert lease is not None
    parameters = {
        "symbol": "SPY",
        "side": "buy",
        "type": "limit",
        "quantity": "1",
        "limit_price": "50.00",
        "time_in_force": "gfd",
        "market_hours": "all_day_hours",
    }
    payload = {
        "planned_at": planned_at.isoformat(),
        "expires_at": (planned_at + timedelta(minutes=5)).isoformat(),
        "trade_date": "2026-09-08",
        "approved_orders": [
            {
                "ref_id": "holiday-ref",
                "symbol": "SPY",
                "side": "buy",
                "notional": 50.0,
                "broker_parameters": parameters,
            }
        ],
        "execution_limits": {
            "max_entry_orders_per_day": 1,
            "max_entry_daily_notional": 100.0,
            "max_order_notional": 100.0,
        },
    }
    plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key="account-hash",
        snapshot={"account_key": "account-hash"},
        payload=payload,
    )
    assert plan.trade_date.isoformat() == "2026-09-08"
    assert store.persist_plan(plan, lease.lease_token) == plan

    wrong_session = {**payload, "trade_date": "2026-09-07"}
    wrong_plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key="account-hash",
        snapshot={"account_key": "account-hash", "wrong": True},
        payload=wrong_session,
    )
    with pytest.raises(ValueError, match="actual NYSE session"):
        store.persist_plan(wrong_plan, lease.lease_token)

    oversized = json.loads(json.dumps(payload))
    oversized["approved_orders"][0]["notional"] = 150.0
    oversized_plan = ExecutionPlan.build(
        run_id=lease.run_id,
        account_key="account-hash",
        snapshot={"account_key": "account-hash", "oversized": True},
        payload=oversized,
    )
    with pytest.raises(ValueError, match=r"<=\$100"):
        store.persist_plan(oversized_plan, lease.lease_token)


def test_exact_review_and_confirmation_survive_a_fresh_process_boundary(confirmation_signer):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    review = store.record_plan_review(
        plan.plan_id,
        plan.draft_hash,
        review_payload,
        reviewed_at=now + timedelta(seconds=2),
    )
    assert (
        store.record_plan_review(
            plan.plan_id,
            plan.draft_hash,
            review_payload,
            reviewed_at=now + timedelta(seconds=3),
        )
        == review
    )

    exported = json.loads(json.dumps(store.get_plan(plan.plan_id).payload))
    assert exported == plan.payload
    with pytest.raises(ValueError, match="signature"):
        store.record_confirmation(
            plan.plan_id,
            "0" * 64,
            confirmation_signer(plan.plan_id, review.review_hash),
            confirmed_at=now + timedelta(seconds=3),
        )

    signature = confirmation_signer(plan.plan_id, review.review_hash)
    confirmation = store.record_confirmation(
        plan.plan_id,
        review.review_hash,
        signature,
        confirmed_at=now + timedelta(seconds=3),
    )
    assert confirmation.actor_ref.startswith("ed25519:")
    assert confirmation.payload["literal"] == confirmation_message(plan.plan_id, review.review_hash)
    assert confirmation.payload["signature"] == signature
    assert confirmation.payload["authority_fingerprint"] == confirmation.actor_ref.removeprefix(
        "ed25519:"
    )
    assert (
        store.record_confirmation(
            plan.plan_id,
            review.review_hash,
            signature,
            confirmed_at=now + timedelta(seconds=4),
        )
        == confirmation
    )
    assert (
        store.validate_confirmation(
            plan.plan_id,
            review.review_hash,
            confirmation.confirmation_id,
            now=now + timedelta(seconds=4),
        )
        == confirmation
    )
    with pytest.raises(RuntimeError, match="current exact"):
        store.validate_confirmation(
            plan.plan_id,
            review.review_hash,
            confirmation.confirmation_id,
            now=plan.expires_at,
        )


def test_review_requires_complete_exact_broker_parameters():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    changed = json.loads(json.dumps(review_payload))
    changed["reviews"][0]["broker_parameters"]["limit_price"] = "499.99"

    with pytest.raises(ValueError, match="differ"):
        store.record_plan_review(
            plan.plan_id,
            plan.draft_hash,
            changed,
            reviewed_at=now + timedelta(seconds=2),
        )


def test_review_rejects_mismatched_native_price_even_when_request_copy_matches():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    changed = json.loads(json.dumps(review_payload))
    changed["reviews"][0]["broker_response"]["native_response"]["price"] = "499.99"

    with pytest.raises(ValueError, match="Native broker review order differs"):
        store.record_plan_review(
            plan.plan_id,
            plan.draft_hash,
            changed,
            reviewed_at=now + timedelta(seconds=2),
        )


def test_review_rejects_any_native_order_alert():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    alerted = json.loads(json.dumps(review_payload))
    alert = {"message": "Extended-hours liquidity warning"}
    alerted["reviews"][0]["broker_response"]["order_checks"] = alert
    alerted["reviews"][0]["broker_response"]["native_response"]["order_checks"] = alert

    with pytest.raises(ValueError, match="raised alerts"):
        store.record_plan_review(
            plan.plan_id,
            plan.draft_hash,
            alerted,
            reviewed_at=now + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    ("missing_field", "nested", "message"),
    [
        ("broker_response", False, "response"),
        ("order_checks", True, "order_checks"),
        ("quote_data", True, "quote_data"),
        ("market_data_disclosure", True, "disclosure"),
        ("broker_provenance", False, "provenance"),
    ],
)
def test_review_requires_complete_native_broker_schema(missing_field, nested, message):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    incomplete = json.loads(json.dumps(review_payload))
    target = incomplete["reviews"][0]["broker_response"] if nested else incomplete["reviews"][0]
    target.pop(missing_field)

    with pytest.raises(ValueError, match=message):
        store.record_plan_review(
            plan.plan_id,
            plan.draft_hash,
            incomplete,
            reviewed_at=now + timedelta(seconds=2),
        )


def test_review_persists_full_native_response_hash():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, _ = _plan(store, now)
    review = store.record_plan_review(
        plan.plan_id,
        plan.draft_hash,
        review_payload,
        reviewed_at=now + timedelta(seconds=2),
    )
    item = review.review_payload["reviews"][0]
    assert item["broker_response_hash"] == canonical_hash(item["broker_response"])


def test_attempt_is_durable_before_submission_and_unknown_blocks_new_work(confirmation_signer):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    _, plan, review_payload, broker_parameters = _plan(store, now)
    review = store.record_plan_review(
        plan.plan_id,
        plan.draft_hash,
        review_payload,
        reviewed_at=now + timedelta(seconds=2),
    )
    confirmation = store.record_confirmation(
        plan.plan_id,
        review.review_hash,
        confirmation_signer(plan.plan_id, review.review_hash),
        confirmed_at=now + timedelta(seconds=3),
    )
    attempt, created = store.create_order_attempt(
        plan_id=plan.plan_id,
        confirmation_id=confirmation.confirmation_id,
        review_hash=review.review_hash,
        ref_id="ref-1",
        broker_request=broker_parameters,
        now=now + timedelta(seconds=4),
    )
    assert created and attempt.state == "prepared"
    retry, retry_created = store.create_order_attempt(
        plan_id=plan.plan_id,
        confirmation_id=confirmation.confirmation_id,
        review_hash=review.review_hash,
        ref_id="ref-1",
        broker_request=broker_parameters,
        now=now + timedelta(seconds=4),
    )
    assert not retry_created and retry == attempt
    with pytest.raises(ValueError, match="atomic submission-claim"):
        store.transition_order_attempt(attempt.attempt_id, "reserved")
    with pytest.raises(ValueError, match="atomic submission-claim"):
        store.transition_order_attempt(attempt.attempt_id, "submitting")
    snapshot_hash = "b" * 64
    store.execution_reservations["ref-1"] = {
        "ref_id": "ref-1",
        "account_key": plan.account_key,
        "trade_date": plan.trade_date,
        "notional": 500.0,
        "is_entry": True,
        "is_option_open": False,
        "plan_id": plan.plan_id,
        "confirmation_id": confirmation.confirmation_id,
        "attempt_id": attempt.attempt_id,
        "validated_at": now + timedelta(seconds=4),
        "validation_snapshot_hash": snapshot_hash,
        "authority_fingerprint_hash": canonical_hash(plan.payload["broker_authority"]),
    }
    submitting = store.claim_order_attempt_for_submission(
        attempt.attempt_id,
        plan_id=plan.plan_id,
        review_hash=review.review_hash,
        confirmation_id=confirmation.confirmation_id,
        ref_id="ref-1",
        validation_snapshot_hash=snapshot_hash,
        now=now + timedelta(seconds=5),
    )
    assert submitting.state == "submitting"
    with pytest.raises(ValueError, match="Invalid attempt transition"):
        store.transition_order_attempt(
            submitting.attempt_id,
            "failed",
            error="unsafe timeout classification",
        )
    with pytest.raises(ValueError, match="broker evidence"):
        store.transition_order_attempt(
            submitting.attempt_id,
            "filled",
            broker_order_id="broker-order-1",
        )
    with pytest.raises(ValueError, match="ambiguity or timeout"):
        store.transition_order_attempt(submitting.attempt_id, "unknown")
    unknown = store.transition_order_attempt(
        submitting.attempt_id,
        "unknown",
        error="broker timeout",
    )
    assert store.nonterminal_attempts(plan.account_key) == [unknown]
    with pytest.raises(ValueError, match="atomic submission-claim"):
        store.transition_order_attempt(unknown.attempt_id, "reserved")
    with pytest.raises(ValueError, match="Invalid attempt transition"):
        store.transition_order_attempt(
            unknown.attempt_id,
            "failed",
            error="cannot clear ambiguity as failed",
        )
    with pytest.raises(ValueError, match="broker evidence"):
        store.transition_order_attempt(
            unknown.attempt_id,
            "filled",
            broker_order_id="broker-order-1",
        )
    native_fill = {
        "id": "broker-order-1",
        "state": "filled",
        "symbol": "SPY",
        "side": "buy",
        "type": "limit",
        "trigger": "immediate",
        "quantity": {"amount": "1"},
        "price": {"amount": "500.00"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    with pytest.raises(ValueError, match="differs from the signed order"):
        store.transition_order_attempt(
            unknown.attempt_id,
            "filled",
            response={"data": {"order": {**native_fill, "market_hours": "all_day_hours"}}},
            broker_order_id="broker-order-1",
        )
    filled = store.transition_order_attempt(
        unknown.attempt_id,
        "filled",
        response={"data": {"order": native_fill}},
        broker_order_id="broker-order-1",
    )
    assert filled.state == "filled"
    with pytest.raises(ValueError, match="picker-event finalization"):
        store.transition_order_attempt(filled.attempt_id, "reconciled")
    store.picker_order_events[(filled.ref_id, "entry_filled")] = {
        "account_key": plan.account_key,
        "session_date": plan.trade_date,
    }
    finalized = store.finalize_filled_attempt_after_picker_sync(
        filled.attempt_id,
        event_type="entry_filled",
        session_date=plan.trade_date,
    )
    assert finalized.state == "reconciled"


def test_submission_claim_rejects_stale_snapshot_wrong_authority_and_halt(
    confirmation_signer,
):
    now = datetime.now(UTC)

    stale_store = InMemoryCloudRuntimeStore()
    plan, review, confirmation, attempt, snapshot_hash = _confirmed_attempt(
        stale_store, now, confirmation_signer
    )
    with pytest.raises(RuntimeError, match="fresh exact broker snapshot"):
        stale_store.claim_order_attempt_for_submission(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validation_snapshot_hash=snapshot_hash,
            now=now + timedelta(seconds=20),
        )

    authority_store = InMemoryCloudRuntimeStore()
    plan, review, confirmation, attempt, snapshot_hash = _confirmed_attempt(
        authority_store, now, confirmation_signer
    )
    authority_store.execution_reservations[attempt.ref_id]["authority_fingerprint_hash"] = "0" * 64
    with pytest.raises(RuntimeError, match="broker authority"):
        authority_store.claim_order_attempt_for_submission(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validation_snapshot_hash=snapshot_hash,
            now=now + timedelta(seconds=5),
        )

    halted_store = InMemoryCloudRuntimeStore()
    plan, review, confirmation, attempt, snapshot_hash = _confirmed_attempt(
        halted_store, now, confirmation_signer
    )
    halted_store.control_states[plan.account_key] = {
        "halted": True,
        "halt_scope": "all",
    }
    with pytest.raises(RuntimeError, match="trading halt"):
        halted_store.claim_order_attempt_for_submission(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validation_snapshot_hash=snapshot_hash,
            now=now + timedelta(seconds=5),
        )


def test_stale_prepared_reservation_can_refresh_and_then_claim(confirmation_signer):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    plan, review, confirmation, attempt, old_snapshot_hash = _confirmed_attempt(
        store, now, confirmation_signer
    )
    new_snapshot_hash = "c" * 64
    refreshed = store.refresh_execution_reservation(
        attempt.attempt_id,
        plan_id=plan.plan_id,
        review_hash=review.review_hash,
        confirmation_id=confirmation.confirmation_id,
        ref_id=attempt.ref_id,
        validated_at=now + timedelta(seconds=24),
        validation_snapshot_hash=new_snapshot_hash,
        authority_fingerprint_hash=canonical_hash(plan.payload["broker_authority"]),
        now=now + timedelta(seconds=25),
    )

    assert refreshed is True
    reservation = store.execution_reservations[attempt.ref_id]
    assert reservation["validation_snapshot_hash"] == new_snapshot_hash
    assert reservation["validation_snapshot_hash"] != old_snapshot_hash
    assert (
        sum(
            event["event_type"] == "execution_reservation_refreshed"
            for event in store.audit_events.values()
        )
        == 1
    )
    claimed = store.claim_order_attempt_for_submission(
        attempt.attempt_id,
        plan_id=plan.plan_id,
        review_hash=review.review_hash,
        confirmation_id=confirmation.confirmation_id,
        ref_id=attempt.ref_id,
        validation_snapshot_hash=new_snapshot_hash,
        now=now + timedelta(seconds=25),
    )
    assert claimed.state == "submitting"


def test_concurrent_reservation_refresh_has_one_cas_winner(confirmation_signer):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    plan, review, confirmation, attempt, _ = _confirmed_attempt(store, now, confirmation_signer)
    barrier = threading.Barrier(2)

    def refresh(snapshot_hash: str) -> bool | str:
        barrier.wait()
        try:
            return store.refresh_execution_reservation(
                attempt.attempt_id,
                plan_id=plan.plan_id,
                review_hash=review.review_hash,
                confirmation_id=confirmation.confirmation_id,
                ref_id=attempt.ref_id,
                validated_at=now + timedelta(seconds=24),
                validation_snapshot_hash=snapshot_hash,
                authority_fingerprint_hash=canonical_hash(plan.payload["broker_authority"]),
                now=now + timedelta(seconds=25),
            )
        except RuntimeError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(refresh, ("c" * 64, "d" * 64)))

    assert results.count(True) == 1
    assert sum("concurrently refreshed" in str(result) for result in results) == 1
    assert (
        sum(
            event["event_type"] == "execution_reservation_refreshed"
            for event in store.audit_events.values()
        )
        == 1
    )


def test_postgres_reservation_refresh_uses_old_proof_cas(monkeypatch, confirmation_signer):
    memory_store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    plan, review, confirmation, attempt, old_snapshot_hash = _confirmed_attempt(
        memory_store, now, confirmation_signer
    )
    reservation = memory_store.execution_reservations[attempt.ref_id]
    attempt_row = (
        attempt.attempt_id,
        attempt.plan_id,
        attempt.confirmation_id,
        attempt.account_key,
        attempt.ref_id,
        attempt.request_hash,
        attempt.broker_request,
        attempt.state,
        attempt.broker_order_id,
        attempt.latest_response,
        attempt.error,
        attempt.created_at,
        attempt.updated_at,
    )
    authority_row = (
        plan.plan_id,
        plan.draft_hash,
        plan.run_id,
        plan.account_key,
        plan.trade_date,
        plan.research_batch_id,
        plan.snapshot_hash,
        plan.planned_at,
        plan.expires_at,
        "confirmed",
        plan.payload,
        review.draft_hash,
        review.review_hash,
        review.review_payload,
        review.reviewed_at,
        confirmation.confirmation_id,
        confirmation.review_hash,
        confirmation.actor_ref,
        confirmation.confirmed_at,
        confirmation.expires_at,
        confirmation.payload,
        reservation["account_key"],
        reservation["trade_date"],
        reservation["notional"],
        reservation["is_entry"],
        reservation["is_option_open"],
        attempt.created_at,
        reservation["validated_at"],
        reservation["validation_snapshot_hash"],
        reservation["authority_fingerprint_hash"],
    )
    cursor = _ScriptedCursor(
        [
            [],
            [],
            [attempt_row],
            [],
            [authority_row],
            [("morning-live",)],
            [],
            [(attempt.ref_id,)],
            [],
        ]
    )
    store = PostgresCloudRuntimeStore("postgresql://fixture.invalid/db")
    monkeypatch.setattr(store, "_connect", lambda: _ScriptedConnection(cursor))

    assert store.refresh_execution_reservation(
        attempt.attempt_id,
        plan_id=plan.plan_id,
        review_hash=review.review_hash,
        confirmation_id=confirmation.confirmation_id,
        ref_id=attempt.ref_id,
        validated_at=now + timedelta(seconds=24),
        validation_snapshot_hash="c" * 64,
        authority_fingerprint_hash=canonical_hash(plan.payload["broker_authority"]),
        now=now + timedelta(seconds=25),
    )
    update_query = next(
        query for query in cursor.queries if "UPDATE execution_plan_reservations" in query
    )
    assert "validated_at IS NOT DISTINCT FROM" in update_query
    assert "validation_snapshot_hash IS NOT DISTINCT FROM" in update_query
    assert old_snapshot_hash != "c" * 64


def test_reservation_refresh_rejects_changed_authority_and_expired_plan(confirmation_signer):
    now = datetime.now(UTC)
    authority_store = InMemoryCloudRuntimeStore()
    plan, review, confirmation, attempt, _ = _confirmed_attempt(
        authority_store, now, confirmation_signer
    )
    with pytest.raises(RuntimeError, match="broker authority"):
        authority_store.refresh_execution_reservation(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validated_at=now + timedelta(seconds=24),
            validation_snapshot_hash="c" * 64,
            authority_fingerprint_hash="0" * 64,
            now=now + timedelta(seconds=25),
        )

    expired_store = InMemoryCloudRuntimeStore()
    plan, review, confirmation, attempt, _ = _confirmed_attempt(
        expired_store, now, confirmation_signer
    )
    with pytest.raises(RuntimeError, match="active exact plan and confirmation"):
        expired_store.refresh_execution_reservation(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validated_at=now + timedelta(minutes=6, seconds=-1),
            validation_snapshot_hash="c" * 64,
            authority_fingerprint_hash=canonical_hash(plan.payload["broker_authority"]),
            now=now + timedelta(minutes=6),
        )


@pytest.mark.parametrize("attempt_state", ["submitting", "unknown", "failed"])
def test_reservation_refresh_rejects_claimed_ambiguous_and_terminal_attempts(
    confirmation_signer,
    attempt_state,
):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    plan, review, confirmation, attempt, snapshot_hash = _confirmed_attempt(
        store, now, confirmation_signer
    )
    if attempt_state == "submitting":
        store.claim_order_attempt_for_submission(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validation_snapshot_hash=snapshot_hash,
            now=now + timedelta(seconds=5),
        )
    else:
        store.transition_order_attempt(
            attempt.attempt_id,
            attempt_state,
            error="durable test transition",
            occurred_at=now + timedelta(seconds=5),
        )

    with pytest.raises(RuntimeError, match="Only a prepared order attempt"):
        store.refresh_execution_reservation(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validated_at=now + timedelta(seconds=24),
            validation_snapshot_hash="c" * 64,
            authority_fingerprint_hash=canonical_hash(plan.payload["broker_authority"]),
            now=now + timedelta(seconds=25),
        )


def test_submission_claim_recomputes_denormalized_plan_and_review_hashes(
    confirmation_signer,
):
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    plan, review, confirmation, attempt, snapshot_hash = _confirmed_attempt(
        store, now, confirmation_signer
    )
    store.plans[plan.plan_id] = replace(
        store.plans[plan.plan_id],
        expires_at=plan.expires_at + timedelta(minutes=1),
    )
    store.confirmations[plan.plan_id] = replace(
        store.confirmations[plan.plan_id],
        expires_at=plan.expires_at + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="denormalized authority"):
        store.claim_order_attempt_for_submission(
            attempt.attempt_id,
            plan_id=plan.plan_id,
            review_hash=review.review_hash,
            confirmation_id=confirmation.confirmation_id,
            ref_id=attempt.ref_id,
            validation_snapshot_hash=snapshot_hash,
            now=now + timedelta(seconds=5),
        )


def test_reconciliation_audit_artifacts_and_runtime_kg_are_durable():
    store = InMemoryCloudRuntimeStore()
    now = datetime.now(UTC)
    lease, plan, _, _ = _plan(store, now)
    reconciliation = store.record_reconciliation(
        plan.plan_id,
        {"clean": True, "matched": [], "breaches": []},
        reconciled_at=now + timedelta(seconds=2),
    )
    assert store.latest_reconciliation(plan.plan_id) == reconciliation
    assert (
        store.record_reconciliation(
            plan.plan_id,
            {"clean": True, "matched": [], "breaches": []},
            reconciled_at=now + timedelta(seconds=3),
        )
        == reconciliation
    )

    artifact = store.record_artifact(
        lease.run_id,
        "raw_quant",
        {"rows": [{"symbol": "SPY", "rank": 1}]},
        source_uri="https://Issuer.Example/news?id=secret#private",
        observed_at=now + timedelta(seconds=2),
    )
    assert artifact["content_hash"] == canonical_hash(artifact["payload"])
    assert artifact["source_uri"] == "https://issuer.example/news"
    with pytest.raises(ValueError, match="credentials"):
        store.record_artifact(
            lease.run_id,
            "credentialed_source",
            {"rows": []},
            source_uri="https://reader:secret@issuer.example/news",
            observed_at=now + timedelta(seconds=2),
        )

    store.upsert_knowledge_node({"node_id": "spy", "node_type": "security", "title": "SPY"})
    store.upsert_knowledge_node({"node_id": "rates", "node_type": "macro", "title": "Rates"})
    store.upsert_knowledge_edge(
        {
            "edge_id": "spy-rates",
            "source_id": "spy",
            "target_id": "rates",
            "relation": "affected_by",
            "sign": "mixed",
            "horizon": "20-trading-days",
            "causality": "hypothesis",
        }
    )
    observation = store.append_knowledge_observation(
        {
            "observation_id": "obs-1",
            "edge_id": "spy-rates",
            "run_id": lease.run_id,
            "decision_date": now.date().isoformat(),
            "horizon": "20-trading-days",
            "polarity": "supports",
            "measured_result": 0.01,
            "evidence_id": "evidence-1",
        }
    )
    assert observation["observation_hash"]
    assert {event["event_type"] for event in store.audit_events.values()} >= {
        "execution_plan_persisted",
        "execution_reconciled",
    }
