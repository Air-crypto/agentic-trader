from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import CRITIC_SOFT_DIMENSIONS, CriticVerdict

ALLOWED_CRITIC_MODELS = frozenset({"cursor-grok-4.5-high-fast"})


@dataclass(frozen=True)
class CriticPolicyResult:
    """Structured critic authorization without interpreting free-form prose."""

    hard_vetoes: tuple[str, ...]
    soft_majority_passed: bool

    @property
    def accepted(self) -> bool:
        return not self.hard_vetoes and self.soft_majority_passed

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.hard_vetoes


def evaluate_critic_policy(
    critic: CriticVerdict,
    *,
    draft_id: str,
    draft_created_at: datetime,
    analyst_model_id: str,
    now: datetime,
) -> CriticPolicyResult:
    """Apply hard vetoes and treat a structured pass as the soft-majority result.

    CriticVerdict has no per-concern votes, so its free-form ``reasons`` cannot
    safely drive authorization. A pass is the critic's structured declaration
    that the soft majority succeeded; all independently verifiable failures
    remain deterministic hard vetoes here.
    """
    hard_vetoes: list[str] = []
    if critic.draft_id != draft_id:
        hard_vetoes.append("critic_draft_mismatch")
    if critic.created_at < draft_created_at:
        hard_vetoes.append("critic_predates_draft")
    if critic.created_at > now:
        hard_vetoes.append("critic_timestamp_in_future")

    critic_model = critic.model_id.strip()
    if (
        critic_model.casefold()
        not in {item.casefold() for item in ALLOWED_CRITIC_MODELS}
        or critic_model.casefold() == analyst_model_id.strip().casefold()
    ):
        hard_vetoes.append("critic_model_not_independent")

    hard_vetoes.extend(f"critic_hard_veto:{item}" for item in critic.hard_vetoes)
    soft_checks = dict(critic.soft_checks)
    if set(soft_checks) != CRITIC_SOFT_DIMENSIONS:
        hard_vetoes.append("critic_soft_checks_incomplete")
        soft_majority_passed = False
    else:
        soft_majority_passed = sum(soft_checks.values()) >= 3
    if critic.verdict != "pass":
        hard_vetoes.append("critic_veto")
    if critic.contradicted_evidence_ids:
        hard_vetoes.append("critic_found_contradicted_evidence")
    if critic.verdict == "pass" and (hard_vetoes or not soft_majority_passed):
        hard_vetoes.append("critic_verdict_policy_mismatch")

    return CriticPolicyResult(
        hard_vetoes=tuple(dict.fromkeys(hard_vetoes)),
        soft_majority_passed=soft_majority_passed,
    )
