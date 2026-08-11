from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_automation_json_embeds_canonical_prompts_exactly():
    for name in ("research", "critic", "execution"):
        prompt = (ROOT / "automations" / f"{name}-prompt.txt").read_text()
        config = json.loads(
            (ROOT / "automations" / f"{name}.json").read_text()
        )
        assert config["prompts"] == [{"prompt": prompt}]


def test_root_automation_files_mirror_execution_automation():
    execution_prompt = (ROOT / "automations" / "execution-prompt.txt").read_text()
    execution_config = json.loads(
        (ROOT / "automations" / "execution.json").read_text()
    )
    assert (ROOT / "automation-prompt.txt").read_text() == execution_prompt
    assert json.loads((ROOT / "automation.json").read_text()) == execution_config


def test_automation_crons_are_utc_and_market_safe():
    research = json.loads((ROOT / "automations" / "research.json").read_text())
    execution = json.loads((ROOT / "automations" / "execution.json").read_text())
    critic = json.loads((ROOT / "automations" / "critic.json").read_text())
    assert research["triggers"] == [{"cron": {"cron": "0 12 * * 1-5"}}]
    assert critic["triggers"] == [{"cron": {"cron": "0 14 * * 1-5"}}]
    assert "grok" in critic["model"]
    assert execution["triggers"] == [{"cron": {"cron": "0 15 * * 1-5"}}]
