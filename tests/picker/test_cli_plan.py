from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import agentic_trader.cli as cli
from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.models import (
    RESEARCH_MODEL_ID,
    RESEARCH_REVIEW_MODE,
    ActiveThesis,
)
from agentic_trader.picker.validation import validate_picker_draft


def _bind_batch(monkeypatch, draft, packet, as_of, created_at):
    ledger = InMemoryLedger()
    payload = {
        "batch_id": "batch-1",
        "model_id": RESEARCH_MODEL_ID,
        "review_mode": RESEARCH_REVIEW_MODE,
        "evidence": [],
        "drafts": [draft.to_dict()],
        "option_drafts": [],
    }
    ledger.stage_batch("batch-1", as_of, created_at, "a" * 64, RESEARCH_MODEL_ID, payload)
    ledger.start_research_cycle("cycle-1", as_of, created_at)
    ledger.complete_research_cycle("cycle-1", "batch-1", as_of)
    ledger.set_batch_status("batch-1", "authorized")
    ledger.put_run(
        draft.run_id,
        "account-hash",
        draft.created_at,
        as_of,
        RESEARCH_MODEL_ID,
        "a" * 64,
    )
    ledger.put_draft(draft)
    ledger.authorize_packet(packet)
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))
    return ledger


def test_reauthorized_active_symbol_is_planned_as_rebalance(
    tmp_path,
    now,
    draft,
    evidence,
    quant,
    monkeypatch,
):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    live_now = datetime.now(UTC)
    packet = replace(
        result.packet,
        created_at=live_now,
        valid_for_date=live_now.date(),
        expires_at=live_now + timedelta(minutes=30),
    ).with_hash()
    _bind_batch(monkeypatch, draft, packet, live_now.date(), live_now)
    thesis = ActiveThesis(
        pick_id="existing-pick",
        packet_id="existing-packet",
        symbol=packet.symbol,
        sector=packet.sector,
        status="active",
        entry_date=live_now.date(),
        expiry_date=live_now.date() + timedelta(days=30),
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
                        {"symbol": packet.symbol, "quantity": "1"},
                        {"symbol": "LEG", "quantity": "2"},
                    ],
                    "broker_orders": [],
                    "broker_option_orders": [],
                    "session_is_regular": True,
                },
                "prices": {packet.symbol: 100.0, "LEG": 50.0, "SPY": 500.0},
                "research_batch_id": "batch-1",
            }
        )
    )
    assert (
        cli.command_picker_plan(
            Namespace(
                batch_id="batch-1",
                packets=str(packets_path),
                theses=str(theses_path),
                snapshot=str(snapshot_path),
                as_of=live_now.date().isoformat(),
                output=str(output_path),
            )
        )
        == 0
    )
    request = json.loads(output_path.read_text())
    metadata = request["metadata_by_symbol"][packet.symbol]
    assert metadata["pick_id"] == thesis.pick_id
    assert metadata["intent_class"] == "rebalance"
    assert request["targets"]["LEG"] == 0.1
    assert request["unmanaged_positions"] == ["LEG"]
    assert request["sell_symbol_allowlist"] == [packet.symbol]


def test_authorized_close_packet_can_exit_a_legacy_holding(
    tmp_path,
    now,
    draft,
    evidence,
    quant,
    monkeypatch,
):
    close_draft = replace(draft, action="close")
    result = validate_picker_draft(
        close_draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    live_now = datetime.now(UTC)
    packet = replace(
        result.packet,
        created_at=live_now,
        valid_for_date=live_now.date(),
        expires_at=live_now + timedelta(minutes=30),
    ).with_hash()
    _bind_batch(monkeypatch, close_draft, packet, live_now.date(), live_now)
    packets_path = tmp_path / "packets.json"
    theses_path = tmp_path / "theses.json"
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "request.json"
    packets_path.write_text(json.dumps({"packets": [packet.to_dict()]}))
    theses_path.write_text(json.dumps({"theses": []}))
    snapshot_path.write_text(
        json.dumps(
            {
                "account": {
                    "account_number": "111111111",
                    "equity": 1_000.0,
                    "cash": 900.0,
                    "broker_positions": [{"symbol": packet.symbol, "quantity": "1"}],
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
        cli.command_picker_plan(
            Namespace(
                batch_id="batch-1",
                packets=str(packets_path),
                theses=str(theses_path),
                snapshot=str(snapshot_path),
                as_of=live_now.date().isoformat(),
                output=str(output_path),
            )
        )
        == 0
    )
    request = json.loads(output_path.read_text())
    assert request["targets"][packet.symbol] == 0.0
    assert request["unmanaged_positions"] == []
    assert request["legacy_position_closes"] == [packet.symbol]
    assert request["authorization_packet_ids"] == [packet.packet_id]
    assert request["metadata_by_symbol"][packet.symbol]["intent_class"] == "mandatory_exit"


def test_picker_plan_fixture_must_exactly_match_durable_packet(
    tmp_path,
    now,
    draft,
    evidence,
    quant,
    monkeypatch,
):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    packet = result.packet
    _bind_batch(monkeypatch, draft, packet, now.date(), now)
    tampered = replace(packet, target_weight=packet.target_weight / 2).with_hash()
    packets_path = tmp_path / "packets.json"
    packets_path.write_text(json.dumps({"packets": [tampered.to_dict()]}))

    with pytest.raises(ValueError, match="exactly match durable"):
        cli.command_picker_plan(
            Namespace(
                batch_id="batch-1",
                packets=str(packets_path),
                theses=None,
                snapshot="not-read.json",
                as_of=now.date().isoformat(),
                output="not-written.json",
            )
        )


def test_exact_batch_packet_filter_rejects_reused_ids_with_changed_draft(
    monkeypatch,
    now,
    draft,
    evidence,
    quant,
):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    packet = result.packet
    ledger = _bind_batch(monkeypatch, draft, packet, now.date(), now)
    batch = ledger.finalized_research_batch("batch-1")
    assert batch is not None
    changed_draft = replace(
        draft,
        thesis="A changed falsifiable thesis must not inherit the older packet authority.",
    )
    changed_payload = {
        **batch["payload"],
        "drafts": [changed_draft.to_dict()],
    }

    assert (
        cli._exact_batch_stock_packets(
            ledger,
            batch,
            changed_payload,
            now.date(),
            now,
        )
        == []
    )


def test_picker_plan_requires_authorizer_packet_output():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "picker-plan",
                "--batch-id",
                "batch-1",
                "--snapshot",
                "snapshot.json",
            ]
        )


def test_sell_authority_rejects_held_symbol_without_lifecycle_or_close_packet(now):
    allowed, failures = cli._picker_sell_authority(
        {},
        set(),
        [],
        {"LEG": 50.0, "SPY": 500.0},
        now.date(),
        {"LEG": 0.0},
        {"LEG"},
        now,
    )

    assert allowed == ()
    assert failures == ["picker_sell_symbol_not_authorized_by_database_or_lifecycle:LEG"]


def test_sell_authority_requires_close_packet_id_to_be_requested(
    now,
    draft,
    evidence,
    quant,
):
    result = validate_picker_draft(
        draft,
        {item.evidence_id: item for item in evidence},
        quant,
        prompt_hash="a" * 64,
        now=now,
    )
    assert result.packet is not None
    close_packet = replace(result.packet, action="close", target_weight=0.0).with_hash()

    allowed, failures = cli._picker_sell_authority(
        {close_packet.packet_id: close_packet},
        set(),
        [],
        {close_packet.symbol: 100.0, "SPY": 500.0},
        now.date(),
        {close_packet.symbol: 0.0},
        {close_packet.symbol},
        now,
    )

    assert allowed == ()
    assert failures == ["picker_sell_symbol_not_authorized_by_database_or_lifecycle:EXM"]


def test_sell_authority_preserves_code_derived_active_thesis_risk_exit(now):
    thesis = ActiveThesis(
        pick_id="active-pick",
        packet_id="historical-packet",
        symbol="EXM",
        sector="Industrials",
        status="active",
        entry_date=now.date() - timedelta(days=20),
        expiry_date=now.date(),
        entry_price=100.0,
        entry_spy_price=500.0,
        target_weight=0.035,
        stop_loss_pct=0.08,
        sector_relative_stop_pct=0.05,
    )

    allowed, failures = cli._picker_sell_authority(
        {},
        set(),
        [thesis],
        {"EXM": 100.0, "SPY": 500.0},
        now.date(),
        {"EXM": 0.0},
        {"EXM"},
        now,
    )

    assert allowed == ("EXM",)
    assert failures == []


def test_empty_picker_buy_authority_cannot_average_up_active_thesis(now):
    buy_allowlist = cli._live_buy_symbol_allowlist(set(), picker_mode=True)
    limits = cli.ExecutionLimits(buy_symbol_allowlist=buy_allowlist)
    account = cli.AccountSnapshot(
        account_number="111111111",
        equity=1_000.0,
        cash=995.0,
        positions={"SPY": 5.0},
        sector_by_symbol={"SPY": "Broad Market"},
        high_water_mark=1_000.0,
        prior_close_equity=1_000.0,
        orders_source="broker",
        session_is_regular=True,
        quote_timestamps={"SPY": now},
        broker_identity_verified=True,
    )

    decisions = cli.plan_orders_from_targets(
        {"SPY": 0.035},
        account,
        {"SPY": 500.0},
        limits=limits,
        rebalance_threshold=0.0,
        metadata_by_symbol={
            "SPY": {
                "pick_id": "active-thesis",
                "intent_class": "rebalance",
                "exit_reason": None,
            }
        },
    )

    assert buy_allowlist == ()
    assert len(decisions) == 1
    assert decisions[0].order.side == "buy"
    assert not decisions[0].approved
    assert "symbol_not_on_allowlist" in decisions[0].reasons
