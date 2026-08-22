from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace

import pytest

import agentic_trader.cli as cli
from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.models import RESEARCH_MODEL_ID, RESEARCH_REVIEW_MODE


def _bundle(now, evidence, draft, **overrides):
    payload = {
        "batch_id": "batch-1",
        "cycle_id": "cycle-1",
        "created_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "prompt_hash": "a" * 64,
        "model_id": RESEARCH_MODEL_ID,
        "review_mode": RESEARCH_REVIEW_MODE,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    return {**payload, **overrides}


def _stage(monkeypatch, tmp_path, ledger, payload):
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))
    monkeypatch.setattr(cli, "_verify_official_issuer_mappings", lambda items: None)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload))
    return cli.command_picker_stage(Namespace(bundle=str(path)))


def test_single_model_bundle_stages_directly_and_completes_cycle(
    monkeypatch, tmp_path, now, evidence, draft
):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)

    assert _stage(monkeypatch, tmp_path, ledger, _bundle(now, evidence, draft)) == 0

    staged = ledger.research_batch("batch-1")
    assert staged is not None
    assert staged["model_id"] == RESEARCH_MODEL_ID
    assert staged["payload"]["review_mode"] == RESEARCH_REVIEW_MODE
    assert ledger.research_cycles["cycle-1"]["status"] == "finalized"
    assert ledger.research_cycles["cycle-1"]["batch_id"] == "batch-1"


def test_direct_stage_does_not_leave_authorizable_batch_if_cycle_is_missing(
    monkeypatch, tmp_path, now, evidence, draft
):
    ledger = InMemoryLedger()

    with pytest.raises(ValueError, match="Unknown research cycle"):
        _stage(monkeypatch, tmp_path, ledger, _bundle(now, evidence, draft))

    assert ledger.research_batch("batch-1") is None


@pytest.mark.parametrize("model_id", ("gpt-5.5", "gpt-5.6-sol-preview", " GPT-5.6-sol "))
def test_stage_rejects_every_other_model(monkeypatch, tmp_path, now, evidence, draft, model_id):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    with pytest.raises(ValueError, match="model_id must be exactly"):
        _stage(
            monkeypatch,
            tmp_path,
            ledger,
            _bundle(now, evidence, draft, model_id=model_id),
        )


def test_stage_rejects_legacy_review_fields(monkeypatch, tmp_path, now, evidence, draft):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    with pytest.raises(ValueError, match="Legacy multi-model"):
        _stage(
            monkeypatch,
            tmp_path,
            ledger,
            _bundle(now, evidence, draft, critics=[]),
        )


def test_stage_rejects_duplicate_draft_ids(monkeypatch, tmp_path, now, evidence, draft):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    duplicate = _bundle(now, evidence, draft, drafts=[draft.to_dict(), draft.to_dict()])
    with pytest.raises(ValueError, match="globally unique"):
        _stage(monkeypatch, tmp_path, ledger, duplicate)


def test_direct_stage_completes_an_exact_legacy_pending_cycle(
    monkeypatch, tmp_path, now, evidence, draft
):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    ledger.research_cycles["cycle-1"].update(status="pending", batch_id="batch-1")

    assert _stage(monkeypatch, tmp_path, ledger, _bundle(now, evidence, draft)) == 0
    assert ledger.research_cycles["cycle-1"]["status"] == "finalized"


def test_stage_collision_cannot_complete_cycle_for_different_payload(
    monkeypatch, tmp_path, now, evidence, draft
):
    ledger = InMemoryLedger()
    ledger.start_research_cycle("cycle-1", now.date(), now)
    assert _stage(monkeypatch, tmp_path, ledger, _bundle(now, evidence, draft)) == 0
    changed = replace(draft, thesis="A materially different but still falsifiable research thesis.")
    ledger.start_research_cycle("cycle-2", now.date(), now)
    payload = _bundle(now, evidence, changed, cycle_id="cycle-2")

    with pytest.raises(ValueError, match="immutable"):
        _stage(monkeypatch, tmp_path, ledger, payload)
    assert ledger.research_cycles["cycle-2"]["status"] == "running"


def test_authorization_uses_exact_batch_id_without_fallback(monkeypatch, now):
    ledger = InMemoryLedger()
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))
    ledger.stage_batch(
        "older-batch",
        now.date(),
        now,
        "a" * 64,
        RESEARCH_MODEL_ID,
        {
            "batch_id": "older-batch",
            "model_id": RESEARCH_MODEL_ID,
            "review_mode": RESEARCH_REVIEW_MODE,
            "evidence": [],
            "drafts": [],
            "option_drafts": [],
        },
    )

    result = cli.command_picker_authorize_batch(
        Namespace(
            batch_id="missing-newer-batch",
            as_of=now.date().isoformat(),
            quant="not-read.json",
            output="not-written.json",
        )
    )

    assert result == 2
    assert ledger.research_batch("older-batch")["status"] == "staged"
