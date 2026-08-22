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
        assert config["durable_cloud_runtime_required"] is True
        assert config["local_state_authoritative"] is False
        assert config["mode"].startswith("LIVE_")
        assert config["scheduled_broker_mutations_allowed"] is False
        assert config["signed_resume_broker_mutations_allowed"] is True
        assert config["requires_per_order_confirmation"] is True
        assert config["new_option_openings_allowed"] is False
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
        "scheduled_broker_mutations_allowed": False,
        "signed_resume_broker_mutations_allowed": True,
        "requires_per_order_confirmation": True,
        "new_option_openings_allowed": False,
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


def test_cursor_market_critic_is_pinned_and_nonplacing():
    critic = (ROOT / ".cursor" / "agents" / "market-critic.md").read_text()
    assert "name: market-critic" in critic
    assert "model: gpt-5.5[effort=high]" in critic
    assert "Do not use broker tools" in critic
    assert "five JSON booleans" in critic
    for dimension in (
        "source_breadth",
        "freshness",
        "materiality",
        "novelty",
        "not_priced_in",
    ):
        assert dimension in critic
