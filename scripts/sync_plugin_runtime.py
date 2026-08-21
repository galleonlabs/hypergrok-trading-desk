#!/usr/bin/env python3
"""Synchronize the self-contained Trading Desk runtime from the Python core.

Only direct ``*.py`` children of ``src/trading_harness`` are mirrored.  The
destination is fixed to the plugin runtime, symlinks are rejected, and writes
are atomic so a cached plugin never observes a partially written module.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile


def _reject_symlink_components(path: Path, boundary: Path) -> None:
    candidate = path
    while True:
        if candidate.exists() and candidate.is_symlink():
            raise ValueError(f"runtime path may not traverse a symlink: {candidate}")
        if candidate == boundary:
            return
        if candidate.parent == candidate:
            raise ValueError("runtime paths do not share a safe boundary")
        candidate = candidate.parent


def _python_files(root: Path, *, must_exist: bool) -> dict[str, bytes]:
    if not root.exists():
        if must_exist:
            raise ValueError(f"runtime directory is missing: {root}")
        return {}
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"runtime path must be a real directory: {root}")

    files: dict[str, bytes] = {}
    for path in sorted(root.iterdir()):
        if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise ValueError(f"runtime entries may not be symlinks: {path}")
        if path.is_dir() or path.suffix != ".py":
            raise ValueError(f"unexpected plugin runtime entry: {path}")
        files[path.name] = path.read_bytes()
    if must_exist and "__init__.py" not in files:
        raise ValueError(f"runtime package is incomplete: {root}")
    return files


def synchronize(source: Path, target: Path, *, check: bool) -> list[str]:
    """Return stale filenames and optionally make ``target`` an exact mirror."""

    source = source.absolute()
    target = target.absolute()
    boundary = Path(os.path.commonpath((source, target)))
    _reject_symlink_components(source, boundary)
    _reject_symlink_components(target, boundary)
    source_files = _python_files(source, must_exist=True)
    target_files = _python_files(target, must_exist=check) if target.exists() else {}
    stale = sorted(
        name
        for name in set(source_files) | set(target_files)
        if source_files.get(name) != target_files.get(name)
    )
    if check or not stale:
        return stale

    if target.exists() and target.is_symlink():
        raise ValueError(f"runtime target may not be a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"runtime target may not be a symlink: {target}")

    for name, content in source_files.items():
        destination = target / name
        if destination.exists() and destination.is_symlink():
            raise ValueError(f"runtime module may not be a symlink: {destination}")
        if destination.exists() and destination.read_bytes() == content:
            continue
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target,
                prefix=f".{name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                temporary_name = stream.name
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    # Remove stale Python modules only inside this fixed generated package.
    for name in sorted(set(target_files) - set(source_files)):
        stale_path = target / name
        if stale_path.is_symlink() or not stale_path.is_file():
            raise ValueError(f"refusing to remove unsafe runtime entry: {stale_path}")
        stale_path.unlink()
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "src" / "trading_harness"
    target = (
        repo_root
        / "plugins"
        / "trading-desk"
        / "runtime"
        / "trading_harness"
    )
    try:
        stale = synchronize(source, target, check=arguments.check)
    except (OSError, ValueError) as error:
        print(f"plugin runtime sync failed: {error}", file=sys.stderr)
        return 2
    if arguments.check and stale:
        print("plugin runtime mirror is stale: " + ", ".join(stale), file=sys.stderr)
        return 1
    if stale:
        print("updated plugin runtime: " + ", ".join(stale))
    else:
        print("plugin runtime mirror is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
