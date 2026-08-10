from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from .evaluation import OutcomeMark
from .models import (
    ActiveThesis,
    CriticVerdict,
    DecisionPacket,
    EvidenceVersion,
    PickerDraft,
)

DATABASE_URL_ENV = "DATABASE_URL"


def account_key(account_number: str) -> str:
    """Irreversible account identifier suitable for a shared decision ledger."""
    return hashlib.sha256(account_number.encode()).hexdigest()


class PickerLedger(Protocol):
    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def put_evidence(self, evidence: EvidenceVersion) -> None: ...

    def put_draft(self, draft: PickerDraft) -> None: ...

    def put_critic(self, verdict: CriticVerdict) -> None: ...

    def authorize_packet(self, packet: DecisionPacket) -> None: ...

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]: ...

    def upsert_thesis(self, thesis: ActiveThesis) -> None: ...

    def active_theses(self) -> list[ActiveThesis]: ...

    def control_state(self, account_hash: str) -> dict[str, Any]: ...

    def record_equity_peak(self, account_hash: str, equity: float) -> float: ...

    def halt(self, account_hash: str, reason: str) -> None: ...

    def put_outcome(self, outcome: OutcomeMark) -> None: ...

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None: ...

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None: ...

    def set_batch_status(self, batch_id: str, status: str) -> None: ...


class InMemoryLedger:
    """Test ledger with the same immutability and uniqueness rules as Postgres."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.evidence: dict[tuple[str, str], EvidenceVersion] = {}
        self.drafts: dict[str, PickerDraft] = {}
        self.critics: dict[str, CriticVerdict] = {}
        self.packets: dict[str, DecisionPacket] = {}
        self.theses: dict[str, ActiveThesis] = {}
        self.controls: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[tuple[str, int], OutcomeMark] = {}
        self.batches: dict[str, dict[str, Any]] = {}

    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_id": run_id,
            "account_key": account_hash,
            "started_at": started_at,
            "as_of": as_of,
            "model_id": model_id,
            "prompt_hash": prompt_hash,
            "status": status,
            "metadata": metadata or {},
        }
        existing = self.runs.get(run_id)
        if existing is not None and existing != payload:
            raise ValueError(f"Run {run_id} already exists with different data")
        self.runs[run_id] = payload

    def put_evidence(self, evidence: EvidenceVersion) -> None:
        key = (evidence.evidence_id, evidence.document_hash)
        existing = self.evidence.get(key)
        if existing is not None and existing != evidence:
            raise ValueError(f"Evidence version {key} is immutable")
        self.evidence[key] = evidence

    def put_draft(self, draft: PickerDraft) -> None:
        if draft.run_id not in self.runs:
            raise ValueError("Draft references an unknown run")
        existing = self.drafts.get(draft.draft_id)
        if existing is not None and existing != draft:
            raise ValueError(f"Draft {draft.draft_id} is immutable")
        self.drafts[draft.draft_id] = draft

    def put_critic(self, verdict: CriticVerdict) -> None:
        if verdict.draft_id not in self.drafts:
            raise ValueError("Critic verdict references an unknown draft")
        existing = self.critics.get(verdict.draft_id)
        if existing is not None and existing != verdict:
            raise ValueError(f"Critic verdict for {verdict.draft_id} is immutable")
        self.critics[verdict.draft_id] = verdict

    def authorize_packet(self, packet: DecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize a packet with an invalid hash")
        if packet.draft_id not in self.drafts or packet.draft_id not in self.critics:
            raise ValueError("Packet requires a known draft and critic verdict")
        collision = next(
            (
                item
                for item in self.packets.values()
                if item.valid_for_date == packet.valid_for_date
                and item.symbol == packet.symbol
                and item.action == packet.action
                and item.packet_id != packet.packet_id
            ),
            None,
        )
        if collision is not None:
            raise ValueError("An authorized packet already exists for symbol/action/day")
        existing = self.packets.get(packet.packet_id)
        if existing is not None and existing != packet:
            raise ValueError(f"Packet {packet.packet_id} is immutable")
        self.packets[packet.packet_id] = packet

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        return sorted(
            [
                packet
                for packet in self.packets.values()
                if packet.valid_for_date == valid_for
                and packet.expires_at > now
                and packet.verify_hash()
            ],
            key=lambda item: (-item.rank_score, item.symbol),
        )

    def upsert_thesis(self, thesis: ActiveThesis) -> None:
        self.theses[thesis.pick_id] = thesis

    def active_theses(self) -> list[ActiveThesis]:
        return sorted(
            [
                thesis
                for thesis in self.theses.values()
                if thesis.status in {"pending_entry", "active", "expired", "invalidated"}
            ],
            key=lambda item: item.pick_id,
        )

    def control_state(self, account_hash: str) -> dict[str, Any]:
        return dict(
            self.controls.get(
                account_hash,
                {
                    "halted": False,
                    "halt_reason": None,
                    "high_water_mark": None,
                    "cooldown_until": None,
                },
            )
        )

    def record_equity_peak(self, account_hash: str, equity: float) -> float:
        if equity <= 0:
            raise ValueError("Equity must be positive")
        state = self.control_state(account_hash)
        previous = state.get("high_water_mark")
        peak = equity if previous is None else max(float(previous), equity)
        state["high_water_mark"] = peak
        self.controls[account_hash] = state
        return peak

    def halt(self, account_hash: str, reason: str) -> None:
        state = self.control_state(account_hash)
        state["halted"] = True
        state["halt_reason"] = reason
        self.controls[account_hash] = state

    def put_outcome(self, outcome: OutcomeMark) -> None:
        key = (outcome.packet_id, outcome.horizon_days)
        existing = self.outcomes.get(key)
        if existing is not None and existing != outcome:
            raise ValueError(f"Outcome {key} is immutable")
        self.outcomes[key] = outcome

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "batch_id": batch_id,
            "as_of": as_of,
            "created_at": created_at,
            "prompt_hash": prompt_hash,
            "model_id": model_id,
            "status": "staged",
            "payload": payload,
        }
        existing = self.batches.get(batch_id)
        if existing is not None and existing != record:
            raise ValueError(f"Research batch {batch_id} is immutable")
        self.batches[batch_id] = record

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None:
        eligible = [
            record
            for record in self.batches.values()
            if record["as_of"] == as_of and record["status"] == "staged"
        ]
        return max(eligible, key=lambda item: item["created_at"]) if eligible else None

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in {"staged", "authorized", "rejected", "consumed"}:
            raise ValueError(f"Unsupported batch status: {status}")
        if batch_id not in self.batches:
            raise ValueError(f"Unknown research batch {batch_id}")
        self.batches[batch_id]["status"] = status


class PostgresLedger:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("A Postgres database URL is required")
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> PostgresLedger:
        return cls(os.environ.get(DATABASE_URL_ENV, ""))

    def _connect(self):
        try:
            import psycopg
        except (
            ImportError
        ) as error:  # pragma: no cover - exercised only without optional dependency
            raise RuntimeError("Install psycopg[binary] to use PostgresLedger") from error
        try:
            return psycopg.connect(self.database_url)
        except Exception as error:  # pragma: no cover - depends on live network
            message = str(error).lower()
            if "network is unreachable" in message or "no route to host" in message:
                raise RuntimeError(
                    "Postgres connection failed with an unreachable-network error. "
                    "Cursor cloud sandboxes are IPv4-only; use the Supabase Shared "
                    "Pooler URI (host *.pooler.supabase.com, session mode :5432 or "
                    "transaction mode :6543), not the direct db.*.supabase.co host."
                ) from error
            if "password authentication failed" in message:
                raise RuntimeError(
                    "Postgres rejected DATABASE_URL credentials. For the Supabase "
                    "Shared Pooler the username must be postgres.<project-ref>, not "
                    "postgres alone; copy the Session pooler URI from the dashboard "
                    "Connect panel, URL-encode special characters in the password, "
                    "and confirm it is the database password (reset it there if "
                    "needed)."
                ) from error
            raise

    def apply_migration(self, path: str | Path) -> None:
        sql = Path(path).read_text()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql)

    @contextmanager
    def run_lock(self, lock_key: str) -> Iterator[None]:
        """Acquire a transaction-scoped global lock across cloud VMs."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            acquired = bool(cursor.fetchone()[0])
            if not acquired:
                raise RuntimeError("Another picker run holds the Postgres advisory lock")
            yield

    def put_run(
        self,
        run_id: str,
        account_hash: str,
        started_at: datetime,
        as_of: date,
        model_id: str,
        prompt_hash: str,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_runs
                    (run_id, account_key, started_at, as_of, model_id,
                     prompt_hash, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run_id,
                    account_hash,
                    started_at,
                    as_of,
                    model_id,
                    prompt_hash,
                    status,
                    Jsonb(metadata or {}),
                ),
            )

    def put_evidence(self, evidence: EvidenceVersion) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO evidence_versions
                    (evidence_id, document_hash, published_at, first_seen_at, retrieved_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (evidence_id, document_hash) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    evidence.document_hash,
                    evidence.published_at,
                    evidence.first_seen_at,
                    evidence.retrieved_at,
                    Jsonb(evidence.to_dict()),
                ),
            )

    def put_draft(self, draft: PickerDraft) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_drafts (draft_id, run_id, symbol, created_at, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (draft_id) DO NOTHING
                """,
                (
                    draft.draft_id,
                    draft.run_id,
                    draft.symbol,
                    draft.created_at,
                    Jsonb(draft.to_dict()),
                ),
            )

    def put_critic(self, verdict: CriticVerdict) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO critic_verdicts (draft_id, created_at, verdict, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (draft_id) DO NOTHING
                """,
                (
                    verdict.draft_id,
                    verdict.created_at,
                    verdict.verdict,
                    Jsonb(verdict.to_dict()),
                ),
            )

    def authorize_packet(self, packet: DecisionPacket) -> None:
        if not packet.verify_hash():
            raise ValueError("Cannot authorize a packet with an invalid hash")
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO decision_packets
                    (packet_id, run_id, draft_id, symbol, action, valid_for_date,
                     expires_at, packet_hash, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (packet_id) DO NOTHING
                """,
                (
                    packet.packet_id,
                    packet.run_id,
                    packet.draft_id,
                    packet.symbol,
                    packet.action,
                    packet.valid_for_date,
                    packet.expires_at,
                    packet.packet_hash,
                    Jsonb(packet.to_dict()),
                ),
            )

    def authorized_packets(
        self, valid_for: date, now: datetime | None = None
    ) -> list[DecisionPacket]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM decision_packets
                WHERE valid_for_date = %s
                  AND expires_at > %s
                  AND status = 'authorized'
                ORDER BY (payload->>'rank_score')::double precision DESC, symbol
                """,
                (valid_for, now),
            )
            return [DecisionPacket.from_dict(row[0]) for row in cursor.fetchall()]

    def upsert_thesis(self, thesis: ActiveThesis) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO active_theses
                    (pick_id, packet_id, symbol, status, entry_date, expiry_date, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pick_id) DO UPDATE
                SET status = EXCLUDED.status,
                    expiry_date = EXCLUDED.expiry_date,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (
                    thesis.pick_id,
                    thesis.packet_id,
                    thesis.symbol,
                    thesis.status,
                    thesis.entry_date,
                    thesis.expiry_date,
                    Jsonb(thesis.to_dict()),
                ),
            )

    def active_theses(self) -> list[ActiveThesis]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM active_theses
                WHERE status IN ('pending_entry', 'active', 'expired', 'invalidated')
                ORDER BY pick_id
                """
            )
            return [ActiveThesis.from_dict(row[0]) for row in cursor.fetchall()]

    def control_state(self, account_hash: str) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT halted, halt_reason, high_water_mark, cooldown_until
                FROM picker_control_state
                WHERE account_key = %s
                """,
                (account_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return {
                    "halted": False,
                    "halt_reason": None,
                    "high_water_mark": None,
                    "cooldown_until": None,
                }
            return {
                "halted": bool(row[0]),
                "halt_reason": row[1],
                "high_water_mark": row[2],
                "cooldown_until": row[3],
            }

    def record_equity_peak(self, account_hash: str, equity: float) -> float:
        if equity <= 0:
            raise ValueError("Equity must be positive")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_control_state (account_key, high_water_mark)
                VALUES (%s, %s)
                ON CONFLICT (account_key) DO UPDATE
                SET high_water_mark = GREATEST(
                        COALESCE(
                            picker_control_state.high_water_mark,
                            EXCLUDED.high_water_mark
                        ),
                        EXCLUDED.high_water_mark
                    ),
                    updated_at = now()
                RETURNING high_water_mark
                """,
                (account_hash, equity),
            )
            return float(cursor.fetchone()[0])

    def halt(self, account_hash: str, reason: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_control_state (account_key, halted, halt_reason)
                VALUES (%s, TRUE, %s)
                ON CONFLICT (account_key) DO UPDATE
                SET halted = TRUE,
                    halt_reason = EXCLUDED.halt_reason,
                    updated_at = now()
                """,
                (account_hash, reason),
            )

    def put_outcome(self, outcome: OutcomeMark) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_outcomes
                    (packet_id, horizon_days, measured_at, raw_return,
                     spy_abnormal_return, sector_abnormal_return, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (packet_id, horizon_days) DO NOTHING
                """,
                (
                    outcome.packet_id,
                    outcome.horizon_days,
                    outcome.measured_at,
                    outcome.raw_return,
                    outcome.spy_abnormal_return,
                    outcome.sector_abnormal_return,
                    Jsonb(outcome.to_dict()),
                ),
            )

    def stage_batch(
        self,
        batch_id: str,
        as_of: date,
        created_at: datetime,
        prompt_hash: str,
        model_id: str,
        payload: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO picker_research_batches
                    (batch_id, as_of, created_at, prompt_hash, model_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO NOTHING
                """,
                (
                    batch_id,
                    as_of,
                    created_at,
                    prompt_hash,
                    model_id,
                    Jsonb(payload),
                ),
            )

    def latest_staged_batch(self, as_of: date) -> dict[str, Any] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id, as_of, created_at, prompt_hash, model_id, status, payload
                FROM picker_research_batches
                WHERE as_of = %s AND status = 'staged'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (as_of,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "batch_id": row[0],
                "as_of": row[1],
                "created_at": row[2],
                "prompt_hash": row[3],
                "model_id": row[4],
                "status": row[5],
                "payload": row[6],
            }

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in {"staged", "authorized", "rejected", "consumed"}:
            raise ValueError(f"Unsupported batch status: {status}")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE picker_research_batches
                SET status = %s
                WHERE batch_id = %s
                """,
                (status, batch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown research batch {batch_id}")
