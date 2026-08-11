from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import timedelta

import pytest

import agentic_trader.cli as cli
from agentic_trader.picker.ledger import InMemoryLedger


def _critics_file(tmp_path, as_of, critics):
    pending_path = tmp_path / "pending.json"
    assert (
        cli.command_picker_export_pending(
            Namespace(as_of=as_of.isoformat(), output=str(pending_path))
        )
        == 0
    )
    binding = json.loads(pending_path.read_text())["_critic_binding"]
    critics_path = tmp_path / "critics.json"
    critics_path.write_text(
        json.dumps(
            {
                "_critic_binding": binding,
                "critics": [item.to_dict() for item in critics],
            }
        )
    )
    return critics_path


def test_pending_sonnet_batch_is_finalized_only_by_grok(
    monkeypatch,
    tmp_path,
    now,
    evidence,
    draft,
    critic,
):
    ledger = InMemoryLedger()
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    bundle = {
        "batch_id": "pending-batch-1",
        "cycle_id": "cycle-1",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": "claude-sonnet-5",
        "run_id": draft.run_id,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle))
    ledger.start_research_cycle("cycle-1", now.date(), now)
    assert (
        cli.command_picker_stage_pending(Namespace(bundle=str(bundle_path))) == 0
    )
    assert ledger.latest_staged_batch(now.date()) is None

    critics_path = _critics_file(tmp_path, now.date(), [critic])
    args = Namespace(
        critics=str(critics_path),
        as_of=now.date().isoformat(),
    )
    assert cli.command_picker_finalize_pending(args) == 0
    staged = ledger.latest_staged_batch(now.date())
    assert staged["payload"]["critics"][0]["model_id"] == critic.model_id
    assert ledger.latest_pending_batch(now.date()) is None


def test_pending_batch_rejects_same_model_critic(
    monkeypatch,
    tmp_path,
    now,
    evidence,
    draft,
    critic,
):
    ledger = InMemoryLedger()
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    bundle = {
        "batch_id": "pending-batch-2",
        "cycle_id": "cycle-2",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": "claude-sonnet-5",
        "run_id": draft.run_id,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle))
    ledger.start_research_cycle("cycle-2", now.date(), now)
    cli.command_picker_stage_pending(Namespace(bundle=str(bundle_path))
    )
    self_critic = replace(critic, model_id="claude-sonnet-5")
    critics_path = _critics_file(tmp_path, now.date(), [self_critic])
    with pytest.raises(ValueError, match="independent Grok"):
        cli.command_picker_finalize_pending(
            Namespace(
                critics=str(critics_path),
                as_of=now.date().isoformat(),
            )
        )


def test_pending_batch_requires_all_structured_soft_checks(
    monkeypatch,
    tmp_path,
    now,
    evidence,
    draft,
    critic,
):
    ledger = InMemoryLedger()
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    bundle = {
        "batch_id": "pending-batch-soft-checks",
        "cycle_id": "cycle-soft-checks",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": "claude-sonnet-5",
        "run_id": draft.run_id,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle))
    ledger.start_research_cycle("cycle-soft-checks", now.date(), now)
    cli.command_picker_stage_pending(Namespace(bundle=str(bundle_path)))
    critics_path = _critics_file(
        tmp_path,
        now.date(),
        [replace(critic, soft_checks=())],
    )
    with pytest.raises(ValueError, match="five structured soft checks"):
        cli.command_picker_finalize_pending(
            Namespace(
                critics=str(critics_path),
                as_of=now.date().isoformat(),
            )
        )


def test_newer_pending_batch_blocks_fallback_to_older_finalized_batch(
    monkeypatch,
    now,
):
    ledger = InMemoryLedger()
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    ledger.stage_batch(
        "older-finalized",
        now.date(),
        now - timedelta(hours=1),
        "a" * 64,
        "claude-sonnet-5",
        {"drafts": [], "option_drafts": [], "critics": []},
    )
    ledger.stage_pending_batch(
        "newer-pending",
        now.date(),
        now,
        "b" * 64,
        "claude-sonnet-5",
        {"drafts": [], "option_drafts": []},
    )

    picker_result = cli.command_picker_authorize_batch(
        Namespace(
            as_of=now.date().isoformat(),
            quant="not-read.json",
            output="not-written.json",
        )
    )
    option_result = cli.command_option_authorize_batch(
        Namespace(
            as_of=now.date().isoformat(),
            snapshot="not-read.json",
            output="not-written.json",
        )
    )
    assert picker_result == 2
    assert option_result == 2


def test_pending_batch_rejects_duplicate_draft_ids(
    tmp_path,
    now,
    evidence,
    draft,
):
    bundle = {
        "batch_id": "duplicate-drafts",
        "cycle_id": "duplicate-cycle",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": "claude-sonnet-5",
        "run_id": draft.run_id,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict(), draft.to_dict()],
        "option_drafts": [],
    }
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match="globally unique"):
        cli.command_picker_stage_pending(Namespace(bundle=str(path)))


def test_critic_binding_finalizes_exact_batch_not_newer_pending(
    monkeypatch,
    tmp_path,
    now,
    evidence,
    draft,
    critic,
):
    ledger = InMemoryLedger()
    monkeypatch.setattr(
        cli.PostgresLedger,
        "from_env",
        classmethod(lambda cls: ledger),
    )
    first = {
        "batch_id": "bound-first",
        "cycle_id": "cycle-bound-first",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": "claude-sonnet-5",
        "run_id": draft.run_id,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    first_path = tmp_path / "first.json"
    first_path.write_text(json.dumps(first))
    ledger.start_research_cycle("cycle-bound-first", now.date(), now)
    cli.command_picker_stage_pending(Namespace(bundle=str(first_path)))
    critics_path = _critics_file(tmp_path, now.date(), [critic])

    newer_draft = replace(
        draft,
        draft_id="draft-newer",
        run_id="run-newer",
        created_at=now + timedelta(minutes=1),
    )
    second = {
        **first,
        "batch_id": "bound-second",
        "cycle_id": "cycle-bound-second",
        "created_at": (now + timedelta(minutes=1)).isoformat(),
        "run_id": newer_draft.run_id,
        "drafts": [newer_draft.to_dict()],
    }
    second_path = tmp_path / "second.json"
    second_path.write_text(json.dumps(second))
    ledger.start_research_cycle(
        "cycle-bound-second",
        now.date(),
        now + timedelta(minutes=1),
    )
    cli.command_picker_stage_pending(Namespace(bundle=str(second_path)))

    assert (
        cli.command_picker_finalize_pending(
            Namespace(
                critics=str(critics_path),
                as_of=now.date().isoformat(),
            )
        )
        == 0
    )
    assert ledger.pending_batch("bound-first")["status"] == "finalized"
    assert ledger.latest_pending_batch(now.date())["batch_id"] == "bound-second"
