from __future__ import annotations

import os

import pytest

from agentic_trader.runtime_env import load_runtime_env


def test_load_runtime_env_accepts_unquoted_values_without_shell_expansion(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("DATABASE_URL=postgres://user:p$a$(never)@host/db\nNET=1234\n")
    path.chmod(0o600)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NET", raising=False)

    assert load_runtime_env(path) == ("DATABASE_URL", "NET")
    assert os.environ["DATABASE_URL"] == "postgres://user:p$a$(never)@host/db"
    assert os.environ["NET"] == "1234"


def test_load_runtime_env_does_not_override_existing_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("NET=1234\n")
    path.chmod(0o600)
    monkeypatch.setenv("NET", "already-set")

    assert load_runtime_env(path) == ()
    assert os.environ["NET"] == "already-set"


def test_load_runtime_env_rejects_shell_syntax_and_unsafe_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export SECRET=value\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Unsupported dotenv syntax"):
        load_runtime_env(path)

    path.write_text("SECRET=value\n")
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="mode 600"):
        load_runtime_env(path)
