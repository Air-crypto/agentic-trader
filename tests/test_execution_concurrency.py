from __future__ import annotations

import time

import pytest

from agentic_trader.execution import (
    SessionLockedError,
    daily_consumption,
    daily_entry_consumption,
    merge_broker_and_local_consumption,
    record_plan_consumption,
    session_lock,
)


def test_second_session_is_refused_while_first_holds_lock(tmp_path):
    with session_lock(tmp_path):
        with pytest.raises(SessionLockedError):
            with session_lock(tmp_path):
                pass


def test_lock_is_released_after_the_session(tmp_path):
    with session_lock(tmp_path):
        pass
    with session_lock(tmp_path):
        pass


def test_lock_is_released_even_if_the_session_raises(tmp_path):
    with pytest.raises(ValueError):
        with session_lock(tmp_path):
            raise ValueError("session blew up")
    with session_lock(tmp_path):
        pass


def test_stale_lock_from_a_crashed_run_is_reclaimed(tmp_path):
    lock = tmp_path / "artifacts/live/session.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid=99999 crashed")
    old = time.time() - 7_200
    import os

    os.utime(lock, (old, old))
    with session_lock(tmp_path, ttl_seconds=1_800):
        pass


def test_daily_consumption_accumulates_across_processes(tmp_path):
    assert daily_consumption(tmp_path) == (0, 0.0)
    record_plan_consumption(2, 300.0, root=tmp_path)
    record_plan_consumption(1, 100.0, root=tmp_path)
    assert daily_consumption(tmp_path) == (3, 400.0)
    assert daily_entry_consumption(tmp_path) == (3, 400.0)


def test_entry_and_exit_counters_accumulate_independently_across_runs(tmp_path):
    record_plan_consumption(
        4,
        400.0,
        root=tmp_path,
        entry_orders=4,
        entry_notional=400.0,
    )
    record_plan_consumption(
        2,
        200.0,
        root=tmp_path,
        entry_orders=0,
        entry_notional=0.0,
    )
    record_plan_consumption(
        2,
        200.0,
        root=tmp_path,
        entry_orders=2,
        entry_notional=200.0,
    )
    assert daily_consumption(tmp_path) == (8, 800.0)
    assert daily_entry_consumption(tmp_path) == (6, 600.0)


def test_legacy_counters_are_treated_as_entry_usage(tmp_path):
    state = tmp_path / "artifacts/live/state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        '{"daily":{"2026-08-10":{"orders":3,"notional":250.0}}}\n'
    )
    from datetime import date

    assert daily_entry_consumption(tmp_path, day=date(2026, 8, 10)) == (3, 250.0)


def test_record_plan_consumption_rejects_inconsistent_entry_counters(tmp_path):
    with pytest.raises(ValueError, match="entry_orders"):
        record_plan_consumption(
            1,
            100.0,
            root=tmp_path,
            entry_orders=2,
            entry_notional=100.0,
        )


def test_broker_usage_missing_locally_is_conservatively_counted_as_entry():
    assert merge_broker_and_local_consumption(
        broker=(5, 500.0),
        persisted=(4, 400.0),
        persisted_entry=(3, 300.0),
    ) == (5, 500.0, 4, 400.0)


def test_local_counters_cover_orders_not_yet_visible_at_broker():
    assert merge_broker_and_local_consumption(
        broker=(3, 300.0),
        persisted=(6, 600.0),
        persisted_entry=(4, 400.0),
    ) == (6, 600.0, 4, 400.0)


def test_consumption_is_scoped_to_the_day(tmp_path):
    from datetime import date

    record_plan_consumption(2, 300.0, root=tmp_path, day=date(2026, 8, 10))
    assert daily_consumption(tmp_path, day=date(2026, 8, 11)) == (0, 0.0)
    assert daily_consumption(tmp_path, day=date(2026, 8, 10)) == (2, 300.0)
