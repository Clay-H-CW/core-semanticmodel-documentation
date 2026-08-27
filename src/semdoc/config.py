"""Environment loading.

A tiny .env reader rather than a python-dotenv dependency: the format we need is
`KEY=value` with comments, and keeping the dependency list short is a stated goal.
"""

from __future__ import annotations

import os
import pathlib


def load_env(path: str | pathlib.Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load `KEY=value` pairs from a .env file into os.environ.

    Existing environment variables win unless `override` is set, so a shell export or CI
    secret is never silently replaced by a stale local file.
    """
    env_path = pathlib.Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value

    return loaded


def require(name: str, *, hint: str = "") -> str:
    value = os.environ.get(name)
    if not value:
        suffix = f" {hint}" if hint else ""
        raise SystemExit(f"{name} is not set. Add it to .env or your environment.{suffix}")
    return value
