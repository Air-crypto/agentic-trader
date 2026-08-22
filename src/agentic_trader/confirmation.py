from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

CONFIRMATION_PUBLIC_KEY_ENV = "AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY"


def confirmation_message(plan_id: str, review_hash: str) -> str:
    plan_id = plan_id.strip()
    review_hash = review_hash.strip().lower()
    if not plan_id:
        raise ValueError("Confirmation requires a plan ID")
    if len(review_hash) != 64 or any(
        character not in "0123456789abcdef" for character in review_hash
    ):
        raise ValueError("Confirmation requires a SHA-256 review hash")
    return f"CONFIRM {plan_id} {review_hash}"


def confirmation_literal(plan_id: str, review_hash: str, signature: str) -> str:
    return f"{confirmation_message(plan_id, review_hash)} SIGNATURE {signature.strip()}"


def _public_key(encoded: str) -> Ed25519PublicKey:
    material = encoded.strip()
    if not material:
        raise RuntimeError(f"{CONFIRMATION_PUBLIC_KEY_ENV} is required")
    try:
        if material.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(material.encode())
        else:
            raw = base64.b64decode(material, validate=True)
            key = Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError, binascii.Error) as error:
        raise ValueError("Confirmation public key is not valid Ed25519 material") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Confirmation public key must use Ed25519")
    return key


def public_key_text(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def public_key_fingerprint(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def verify_confirmation_signature(
    plan_id: str,
    review_hash: str,
    signature: str,
    *,
    encoded_public_key: str | None = None,
) -> str:
    key = _public_key(
        encoded_public_key
        if encoded_public_key is not None
        else os.environ.get(CONFIRMATION_PUBLIC_KEY_ENV, "")
    )
    try:
        signature_bytes = base64.b64decode(signature.strip(), validate=True)
        key.verify(signature_bytes, confirmation_message(plan_id, review_hash).encode())
    except (ValueError, binascii.Error, InvalidSignature) as error:
        raise ValueError("Confirmation signature is invalid") from error
    return f"ed25519:{public_key_fingerprint(key)}"


def sign_confirmation(private_key_path: str | Path, plan_id: str, review_hash: str) -> str:
    key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Confirmation private key must use Ed25519")
    signature = key.sign(confirmation_message(plan_id, review_hash).encode())
    return base64.b64encode(signature).decode()


def generate_confirmation_key(private_key_path: str | Path) -> str:
    destination = Path(private_key_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, private_bytes)
    finally:
        os.close(descriptor)
    return public_key_text(key.public_key())
