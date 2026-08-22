from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import agentic_trader.cli as cli
from agentic_trader.picker.ledger import InMemoryLedger
from agentic_trader.picker.models import RESEARCH_MODEL_ID, RESEARCH_REVIEW_MODE
from agentic_trader.picker.option_models import (
    OptionContractSnapshot,
    OptionDecisionPacket,
    OptionDraft,
)


def _finalized_batch(ledger, now, payload, as_of=None):
    as_of = as_of or now.date()
    ledger.stage_batch(
        "batch-1",
        as_of,
        now,
        "a" * 64,
        RESEARCH_MODEL_ID,
        payload,
    )
    ledger.start_research_cycle("cycle-1", as_of, now)
    ledger.complete_research_cycle("cycle-1", "batch-1", as_of)
    ledger.set_batch_status("batch-1", "authorized")


def _contract(now, option_id, strike=100.0):
    return OptionContractSnapshot(
        option_id=option_id,
        contract_symbol=f"EXM-{strike}-C",
        underlying="EXM",
        option_type="call",
        expiration_date=now.date() + timedelta(days=30),
        strike=strike,
        bid=0.48,
        ask=0.52,
        quote_at=now,
        underlying_price=100.0,
    )


def _close_packet(now, draft, contract, packet_id):
    return OptionDecisionPacket(
        packet_id=packet_id,
        run_id=draft.run_id,
        draft_id=draft.draft_id,
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(minutes=5),
        underlying=draft.underlying,
        action="close",
        contract=contract,
        quantity=1,
        side="sell",
        position_effect="close",
        limit_price=contract.bid,
        max_risk=0.0,
        collateral_required=0.0,
        shares_encumbered=0,
        evidence_ids=(),
        prompt_hash="a" * 64,
        model_id=RESEARCH_MODEL_ID,
        draft_hash=draft.draft_hash,
        horizon_trading_days=20,
        invalidation="Close the existing position under current lifecycle authority.",
    )


def test_option_authorizer_refuses_new_canary_openings(
    monkeypatch,
    tmp_path,
    now,
    evidence,
    draft,
):
    option_draft = OptionDraft(
        draft_id="option-open",
        run_id=draft.run_id,
        created_at=now,
        underlying=draft.symbol,
        action="long_call",
        thesis="Open a bounded long call against the current equity research thesis.",
        evidence_ids=draft.evidence_ids,
        source_draft_id=draft.draft_id,
    )
    payload = {
        "batch_id": "batch-1",
        "model_id": RESEARCH_MODEL_ID,
        "review_mode": RESEARCH_REVIEW_MODE,
        "evidence": [item.to_dict() for item in evidence],
        "drafts": [draft.to_dict()],
        "option_drafts": [option_draft.to_dict()],
    }
    ledger = InMemoryLedger()
    _finalized_batch(ledger, now, payload)
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))
    monkeypatch.setattr(cli, "_verify_official_issuer_mappings", lambda items: None)
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "authorized.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "account": {
                    "account_number": "111111111",
                    "equity": 2_000.0,
                    "cash": 1_500.0,
                    "option_level": "option_level_2",
                    "agentic_allowed": True,
                    "broker_equity_positions": [],
                    "underlying_prices": {"EXM": 100.0},
                },
                "contracts": [_contract(now, "option-1").to_dict()],
            }
        )
    )

    result = cli.command_option_authorize_batch(
        Namespace(
            batch_id="batch-1",
            snapshot=str(snapshot_path),
            as_of=now.date().isoformat(),
            output=str(output_path),
        )
    )

    assert result == 2
    written = json.loads(output_path.read_text())
    assert written["new_option_openings_allowed"] is False
    assert written["authorized"] == []
    assert written["results"][0]["reasons"] == ["new_option_openings_disabled"]


def test_option_close_packets_are_filtered_to_exact_batch_without_same_day_fallback():
    now = datetime.now(UTC)
    exact_draft = OptionDraft(
        draft_id="close-exact",
        run_id="run-1",
        created_at=now,
        underlying="EXM",
        action="close",
        thesis="Close the exact current position under current research authority.",
        evidence_ids=(),
        position_id="position-1",
        contract_id="option-1",
    )
    foreign_draft = OptionDraft(
        draft_id="close-foreign",
        run_id="run-old",
        created_at=now,
        underlying="EXM",
        action="close",
        thesis="Close an unrelated position from another same-day research batch.",
        evidence_ids=(),
        position_id="position-2",
        contract_id="option-2",
    )
    payload = {
        "batch_id": "batch-1",
        "model_id": RESEARCH_MODEL_ID,
        "review_mode": RESEARCH_REVIEW_MODE,
        "evidence": [],
        "drafts": [],
        "option_drafts": [exact_draft.to_dict()],
    }
    ledger = InMemoryLedger()
    _finalized_batch(ledger, now, payload)
    exact = _close_packet(now, exact_draft, _contract(now, "option-1"), "packet-exact")
    foreign = _close_packet(
        now,
        foreign_draft,
        _contract(now, "option-2", 105.0),
        "packet-foreign",
    )
    ledger.authorize_option_packet(exact)
    ledger.authorize_option_packet(foreign)
    batch = ledger.finalized_research_batch("batch-1")
    assert batch is not None

    packets = cli._exact_batch_option_close_packets(
        ledger,
        batch,
        payload,
        now.date(),
        now,
    )

    assert packets == [exact]


def test_option_plan_requires_exact_batch_id():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["option-plan", "--snapshot", "snapshot.json"])


def test_option_reservation_refuses_tampered_opening_plan(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    plan_path = tmp_path / "plan.json"
    snapshot_path = tmp_path / "snapshot.json"
    plan_path.write_text(
        json.dumps(
            {
                "planned_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "research_batch_id": "batch-1",
                "approved_orders": [
                    {
                        "ref_id": "opening-ref",
                        "limit_price": 0.5,
                        "quantity": 1,
                        "position_effect": "open",
                    }
                ],
            }
        )
    )
    snapshot_path.write_text(
        json.dumps(
            {
                "account": {
                    "account_number": "111111111",
                    "agentic_allowed": True,
                }
            }
        )
    )
    ledger = InMemoryLedger()
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))

    with pytest.raises(ValueError, match="New option openings are disabled"):
        cli.command_option_reserve(
            Namespace(
                plan=str(plan_path),
                snapshot=str(snapshot_path),
                root=str(tmp_path),
                output=str(tmp_path / "reservation.json"),
            )
        )


def test_option_reservation_exact_binds_close_order_to_current_batch(
    monkeypatch,
    tmp_path,
):
    now = datetime.now(UTC)
    trade_date = cli._nyse_session_date(now)
    close_draft = OptionDraft(
        draft_id="close-exact",
        run_id="run-1",
        created_at=now,
        underlying="EXM",
        action="close",
        thesis="Close the exact current option under current research authority.",
        evidence_ids=(),
        position_id="position-1",
        contract_id="option-1",
    )
    payload = {
        "batch_id": "batch-1",
        "model_id": RESEARCH_MODEL_ID,
        "review_mode": RESEARCH_REVIEW_MODE,
        "evidence": [],
        "drafts": [],
        "option_drafts": [close_draft.to_dict()],
    }
    ledger = InMemoryLedger()
    _finalized_batch(ledger, now, payload, trade_date)
    packet = _close_packet(now, close_draft, _contract(now, "option-1"), "packet-exact")
    packet = OptionDecisionPacket.from_dict(
        {
            **packet.to_dict(),
            "valid_for_date": trade_date.isoformat(),
            "packet_hash": "",
        },
        verify_hash=False,
    ).with_hash()
    ledger.authorize_option_packet(packet)
    monkeypatch.setattr(cli.PostgresLedger, "from_env", classmethod(lambda cls: ledger))
    order = cli._order_from_option_packet(packet, "111111111")
    approved = {**order.to_dict(), "packet_id": packet.packet_id}
    base_plan = {
        "planned_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "research_batch_id": "batch-1",
        "orders_already_used_today": 0,
        "notional_already_used_today": 0.0,
        "entry_orders_already_used_today": 0,
        "entry_notional_already_used_today": 0.0,
        "open_option_positions": 1,
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "account": {
                    "account_number": "111111111",
                    "agentic_allowed": True,
                    "cash": 1_500.0,
                    "broker_equity_positions": [],
                }
            }
        )
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                **base_plan,
                "approved_orders": [{**approved, "limit_price": approved["limit_price"] + 0.1}],
            }
        )
    )
    args = Namespace(
        plan=str(plan_path),
        snapshot=str(snapshot_path),
        root=str(tmp_path),
        output=str(tmp_path / "reservation.json"),
    )

    with pytest.raises(ValueError, match="differs from authorized packet"):
        cli.command_option_reserve(args)

    plan_path.write_text(json.dumps({**base_plan, "approved_orders": [approved]}))
    assert cli.command_option_reserve(args) == 0
    assert json.loads((tmp_path / "reservation.json").read_text())["reserved_ref_ids"] == [
        order.ref_id
    ]
