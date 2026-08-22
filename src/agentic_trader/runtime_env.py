from __future__ import annotations

import os
import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _default_env_path() -> Path | None:
    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def load_runtime_env(path: str | Path | None = None, *, override: bool = False) -> tuple[str, ...]:
    """Load a local dotenv file as data without invoking a shell.

    Values are taken literally after the first ``=``. Optional matching single
    or double quotes are removed, but interpolation, command substitution, and
    ``export`` syntax are deliberately unsupported.
    """
    env_path = Path(path) if path is not None else _default_env_path()
    if env_path is None or not env_path.is_file():
        return ()
    if env_path.stat().st_mode & 0o077:
        raise PermissionError(f"Runtime secret file must be mode 600: {env_path}")

    loaded: list[str] = []
    for line_number, raw_line in enumerate(env_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise ValueError(f"Unsupported dotenv syntax on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _KEY.fullmatch(key):
            raise ValueError(f"Invalid environment key on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)
