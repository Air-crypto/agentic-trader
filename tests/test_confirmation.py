from __future__ import annotations

from pathlib import Path

import pytest

from agentic_trader.confirmation import (
    confirmation_literal,
    confirmation_message,
    generate_confirmation_key,
    sign_confirmation,
    verify_confirmation_signature,
)


def test_human_held_key_signs_one_exact_plan(tmp_path: Path) -> None:
    private_key = tmp_path / "confirmation.pem"
    public_key = generate_confirmation_key(private_key)
    plan_id = "plan-1"
    review_hash = "a" * 64
    signature = sign_confirmation(private_key, plan_id, review_hash)

    actor = verify_confirmation_signature(
        plan_id,
        review_hash,
        signature,
        encoded_public_key=public_key,
    )

    assert actor.startswith("ed25519:")
    assert private_key.stat().st_mode & 0o777 == 0o600
    assert confirmation_message(plan_id, review_hash) == f"CONFIRM {plan_id} {review_hash}"
    assert confirmation_literal(plan_id, review_hash, signature).endswith(signature)
    with pytest.raises(ValueError, match="invalid"):
        verify_confirmation_signature(
            plan_id,
            "b" * 64,
            signature,
            encoded_public_key=public_key,
        )


def test_key_generation_refuses_to_overwrite_private_key(tmp_path: Path) -> None:
    private_key = tmp_path / "confirmation.pem"
    generate_confirmation_key(private_key)

    with pytest.raises(FileExistsError):
        generate_confirmation_key(private_key)
