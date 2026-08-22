from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_live_automation_configs_reference_canonical_prompts():
    for name in ("morning-live", "evening-live"):
        prompt = (ROOT / "automations" / f"{name}-prompt.txt").read_text()
        config = json.loads((ROOT / "automations" / f"{name}.json").read_text())
        assert config["prompt_file"] == f"automations/{name}-prompt.txt"
        assert config["cursor_setup"] == "automations/CURSOR_SETUP.md"
        assert config["project"] == "."
        assert config["model"] == "gpt-5.6-sol"
        assert config["independent_critic_required"] is False
        assert config["durable_cloud_runtime_required"] is True
        assert config["local_state_authoritative"] is False
        assert config["mode"].startswith("LIVE_")
        assert config["scheduled_broker_mutations_allowed"] is False
        assert config["signed_resume_broker_mutations_allowed"] is True
        assert config["requires_per_order_confirmation"] is True
        assert config["new_option_openings_allowed"] is False
        assert config["scheduled_option_authorization_allowed"] is False
        assert config["scheduled_option_planning_allowed"] is False
        assert config["option_positions_and_orders_read_only"] is True
        assert "cloud-schema-check" in prompt
        assert "cloud-run-acquire" in prompt
        assert "live-startup-check" in prompt
        assert "--persist" in prompt
        assert "live-review-record" in prompt
        assert "CONFIRM <plan_id> <review_hash>" in prompt
        assert "must not reserve, place" in prompt or "may not reserve, place" in prompt
        assert "New options" in prompt or "No new options" in prompt
        assert "Never edit code" in prompt


def test_cloud_prompts_never_migrate_or_use_local_state_as_authority():
    for name in ("morning-live", "evening-live"):
        prompt = (ROOT / "automations" / f"{name}-prompt.txt").read_text()
        assert "Never run option-migrate/cloud-migrate" in prompt or (
            "Do not run `option-migrate` or `cloud-migrate`" in prompt
        )
        assert "Supabase/Postgres is authoritative" in prompt
        assert "same-run scratch" in prompt
        assert "Cursor Memory" in prompt
        assert "Runtime Secrets" in prompt
    evening = (ROOT / "automations" / "evening-live-prompt.txt").read_text()
    assert "cloud-kg-record" in evening
    assert "never edit repository KG Markdown" in evening


def test_root_automation_manifest_enables_live_review_not_unconfirmed_orders():
    manifest = json.loads((ROOT / "automation.json").read_text())
    assert manifest == {
        "status": "live_research_and_order_review",
        "model": "gpt-5.6-sol",
        "independent_critic_required": False,
        "scheduled_broker_mutations_allowed": False,
        "signed_resume_broker_mutations_allowed": True,
        "requires_per_order_confirmation": True,
        "new_option_openings_allowed": False,
        "scheduled_option_authorization_allowed": False,
        "scheduled_option_planning_allowed": False,
        "option_positions_and_orders_read_only": True,
        "tasks": [
            "automations/morning-live.json",
            "automations/evening-live.json",
        ],
    }


def test_live_schedules_use_local_wall_clock_and_two_runs():
    morning = json.loads((ROOT / "automations" / "morning-live.json").read_text())
    evening = json.loads((ROOT / "automations" / "evening-live.json").read_text())
    assert morning["timezone"] == evening["timezone"] == "America/Los_Angeles"
    assert morning["schedule"] == "35 6 * * 1-5"
    assert evening["schedule"] == "15 18 * * 0-4"


def test_cursor_automations_use_one_model_without_a_critic():
    assert not (ROOT / ".cursor" / "agents" / "market-critic.md").exists()

    setup = (ROOT / "automations" / "CURSOR_SETUP.md").read_text()
    normalized_setup = " ".join(setup.split())
    assert "`gpt-5.6-sol`, the only model" in normalized_setup
    assert "do not configure a critic, subagent, or self-critique step" in normalized_setup
    assert (
        "do not detect semantic evidence conflicts or judge catalyst freshness" in normalized_setup
    )

    for name in ("morning-live", "evening-live"):
        prompt = (ROOT / "automations" / f"{name}-prompt.txt").read_text()
        normalized_prompt = " ".join(prompt.split())
        assert "`gpt-5.6-sol` as the only model" in normalized_prompt
        assert "Never create, request, or fabricate a critic verdict" in normalized_prompt
        assert "directly to the deterministic source-binding" in normalized_prompt
        assert "do not detect conflicting evidence or judge catalyst freshness" in normalized_prompt
        assert "gpt-5.5" not in prompt
        assert ".cursor/agents/market-critic.md" not in prompt

    assert "gpt-5.5" not in setup
    assert ".cursor/agents/market-critic.md" not in setup


def test_live_prompts_use_exact_single_model_batch_ids():
    required_commands = (
        "picker-cycle-start --cycle-id <cycle_id> --as-of YYYY-MM-DD",
        "picker-stage --bundle <bundle.json>",
        "picker-authorize-batch --batch-id <batch_id> --quant <quant.json> "
        "--as-of YYYY-MM-DD --output <authorized-batch.json>",
        "picker-plan --batch-id <batch_id> --packets <authorized-batch.json> "
        "--snapshot <broker-snapshot.json> --as-of YYYY-MM-DD --output <request.json>",
    )
    retired_commands = (
        "picker-stage-pending",
        "picker-export-pending",
        "picker-finalize-pending",
    )

    for path in (
        ROOT / "automations" / "morning-live-prompt.txt",
        ROOT / "automations" / "evening-live-prompt.txt",
        ROOT / "automations" / "CURSOR_SETUP.md",
    ):
        contents = path.read_text()
        normalized = " ".join(contents.split())
        assert '`model_id: "gpt-5.6-sol"`' in normalized
        assert '`review_mode: "single_model_direct"`' in normalized
        assert "exact new `batch_id`" in normalized or "newly staged exact batch ID" in normalized
        assert "no critic fields" in normalized
        assert "Never run" in normalized
        assert "Never omit `--batch-id` or `--packets` from `picker-plan`" in normalized
        assert "intentionally has no same-day database packet fallback" in normalized
        assert "option positions and orders are read-only broker-truth inputs" in normalized.lower()
        assert "exact-batch close-only CLI primitives" in normalized
        assert "no option-close workflow is activated" in normalized
        assert "option-authorize-batch" not in contents
        assert "option-plan" not in contents
        for command in required_commands:
            assert command in normalized
        for command in retired_commands:
            assert command in contents
