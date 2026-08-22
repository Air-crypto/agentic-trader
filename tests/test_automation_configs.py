from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_live_automation_configs_reference_canonical_prompts():
    for name in ("morning-live", "evening-live"):
        prompt = (ROOT / "automations" / f"{name}-prompt.txt").read_text()
        config = json.loads((ROOT / "automations" / f"{name}.json").read_text())
        assert config["prompt_file"] == f"automations/{name}-prompt.txt"
        assert config["project"] == "."
        assert config["mode"].startswith("LIVE_")
        assert config["broker_mutations_allowed"] is True
        assert config["requires_per_order_confirmation"] is True
        assert config["new_option_openings_allowed"] is False
        assert "Do not reserve or place" in prompt
        assert "explicit confirmation" in prompt
        assert "New option" in prompt


def test_root_automation_manifest_enables_live_review_not_unconfirmed_orders():
    manifest = json.loads((ROOT / "automation.json").read_text())
    assert manifest == {
        "status": "live_research_and_order_review",
        "broker_mutations_allowed": True,
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
