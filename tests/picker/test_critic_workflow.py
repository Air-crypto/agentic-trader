from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import timedelta

import pytest

import agentic_trader.cli as cli
from agentic_trader.picker.ledger import InMemoryLedger


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
    assert (
        cli.command_picker_stage_pending(Namespace(bundle=str(bundle_path))) == 0
    )
    assert ledger.latest_staged_batch(now.date()) is None

    critics_path = tmp_path / "critics.json"
    critics_path.write_text(json.dumps({"critics": [critic.to_dict()]}))
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
    cli.command_picker_stage_pending(Namespace(bundle=str(bundle_path))
    )
    critics_path = tmp_path / "critics.json"
    self_critic = replace(critic, model_id="claude-sonnet-5")
    critics_path.write_text(json.dumps({"critics": [self_critic.to_dict()]}))
    with pytest.raises(ValueError, match="independent Grok"):
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
