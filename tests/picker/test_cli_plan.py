from __future__ import annotations

import json
from argparse import Namespace
from datetime import timedelta

from agentic_trader.cli import command_picker_plan
from agentic_trader.picker.models import ActiveThesis
from agentic_trader.picker.validation import validate_picker_draft


def test_reauthorized_active_symbol_is_planned_as_rebalance(
    tmp_path,
    now,
    draft,
    evidence,
    quant,
    critic,
):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        critic,
        prompt_hash="a" * 64,
        model_id="claude-sonnet-5-thinking-high",
        now=now,
    )
    packet = result.packet
    assert packet is not None
    thesis = ActiveThesis(
        pick_id="existing-pick",
        packet_id="existing-packet",
        symbol=packet.symbol,
        sector=packet.sector,
        status="active",
        entry_date=now.date(),
        expiry_date=now.date() + timedelta(days=30),
        entry_price=100.0,
        entry_spy_price=500.0,
        target_weight=packet.target_weight,
        stop_loss_pct=packet.stop_loss_pct,
        sector_relative_stop_pct=packet.sector_relative_stop_pct,
    )
    packets_path = tmp_path / "packets.json"
    theses_path = tmp_path / "theses.json"
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "request.json"
    packets_path.write_text(json.dumps({"packets": [packet.to_dict()]}))
    theses_path.write_text(json.dumps({"theses": [thesis.to_dict()]}))
    snapshot_path.write_text(
        json.dumps(
            {
                "account": {
                    "account_number": "111111111",
                    "equity": 1_000.0,
                    "cash": 900.0,
                    "broker_positions": [
                        {"symbol": packet.symbol, "quantity": "1"}
                    ],
                    "broker_orders": [],
                    "broker_option_orders": [],
                    "session_is_regular": True,
                },
                "prices": {packet.symbol: 100.0, "SPY": 500.0},
                "research_batch_id": "batch-1",
            }
        )
    )
    assert (
        command_picker_plan(
            Namespace(
                packets=str(packets_path),
                theses=str(theses_path),
                snapshot=str(snapshot_path),
                as_of=now.date().isoformat(),
                output=str(output_path),
            )
        )
        == 0
    )
    request = json.loads(output_path.read_text())
    metadata = request["metadata_by_symbol"][packet.symbol]
    assert metadata["pick_id"] == thesis.pick_id
    assert metadata["intent_class"] == "rebalance"
