from __future__ import annotations

import time

import pytest

from agentic_trader.execution import (
    SessionLockedError,
    daily_consumption,
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


def test_consumption_is_scoped_to_the_day(tmp_path):
    from datetime import date

    record_plan_consumption(2, 300.0, root=tmp_path, day=date(2026, 8, 10))
    assert daily_consumption(tmp_path, day=date(2026, 8, 11)) == (0, 0.0)
    assert daily_consumption(tmp_path, day=date(2026, 8, 10)) == (2, 300.0)
