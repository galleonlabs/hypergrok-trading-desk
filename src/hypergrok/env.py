"""Load a local .env file so documented configuration actually takes effect.

The repository ships a `.env.example`. Before this module existed nothing read
`.env`, so a user who copied the example and filled it in saw every setting
silently ignored. Real environment variables always win over file values, so
an explicit `HYPERGROK_NETWORK=mainnet hypergrok ...` still overrides the file.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["find_dotenv", "load_dotenv", "parse_dotenv"]


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse `KEY=value` lines, ignoring comments, blanks and `export` prefixes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def find_dotenv(start: Path | None = None) -> Path | None:
    """Return the nearest `.env` at or above `start`, or None."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: Path | None = None) -> list[str]:
    """Apply a `.env` into `os.environ` without overriding existing variables.

    Returns the names that were applied. A missing or unreadable file is not an
    error: configuration by real environment variables must keep working.
    """
    target = path or find_dotenv()
    if target is None:
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    applied = []
    for key, value in parse_dotenv(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
