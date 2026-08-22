from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from agentic_trader.picker.models import (
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
)


def test_evidence_rejects_knowledge_time_before_publication(evidence):
    raw = evidence[0].to_dict()
    raw["first_seen_at"] = (evidence[0].published_at - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="first_seen_at"):
        EvidenceVersion.from_dict(raw)


def test_evidence_requires_strict_booleans_and_valid_issuer_identity(evidence):
    raw = evidence[0].to_dict()
    raw["primary"] = "false"
    with pytest.raises(ValueError, match="JSON booleans"):
        EvidenceVersion.from_dict(raw)
    raw = evidence[0].to_dict()
    raw["cik"] = "not-a-cik"
    with pytest.raises(ValueError, match="CIK"):
        EvidenceVersion.from_dict(raw)
    raw = evidence[0].to_dict()
    raw["authority"] = "reporting"
    with pytest.raises(ValueError, match="authority"):
        EvidenceVersion.from_dict(raw)


def test_quant_snapshot_requires_real_booleans_and_finite_values(quant):
    raw = quant.to_dict()
    raw["fractional_tradable"] = "false"
    with pytest.raises(ValueError, match="JSON boolean"):
        type(quant).from_dict(raw)
    raw = quant.to_dict()
    raw["beta_252d"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        type(quant).from_dict(raw)


def test_critic_soft_checks_require_real_json_booleans(critic):
    raw = critic.to_dict()
    raw["soft_checks"]["freshness"] = "false"
    with pytest.raises(ValueError, match="JSON booleans"):
        CriticVerdict.from_dict(raw)


def test_decision_packet_hash_detects_any_change(now):
    packet = DecisionPacket(
        packet_id="packet-1",
        run_id="run-1",
        draft_id="draft-1",
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(hours=2),
        symbol="EXM",
        action="buy",
        horizon_trading_days=20,
        target_weight=0.10,
        stop_loss_pct=0.06,
        sector_relative_stop_pct=0.05,
        sector="Industrials",
        rank_score=0.8,
        thesis_hash="a" * 64,
        evidence_ids=("issuer-1", "gov-1"),
        prompt_hash="b" * 64,
        model_id="analyst-model",
    ).with_hash()
    assert packet.verify_hash()
    assert not replace(packet, target_weight=0.11).verify_hash()
    assert DecisionPacket.from_dict(packet.to_dict()) == packet


def test_decision_packet_rejects_tampered_json(now):
    packet = DecisionPacket(
        packet_id="packet-1",
        run_id="run-1",
        draft_id="draft-1",
        created_at=now,
        valid_for_date=now.date(),
        expires_at=now + timedelta(hours=2),
        symbol="EXM",
        action="buy",
        horizon_trading_days=20,
        target_weight=0.10,
        stop_loss_pct=0.06,
        sector_relative_stop_pct=0.05,
        sector="Industrials",
        rank_score=0.8,
        thesis_hash="a" * 64,
        evidence_ids=("issuer-1", "gov-1"),
        prompt_hash="b" * 64,
        model_id="analyst-model",
    ).with_hash()
    raw = packet.to_dict()
    raw["symbol"] = "EVIL"
    with pytest.raises(ValueError, match="hash mismatch"):
        DecisionPacket.from_dict(raw)
