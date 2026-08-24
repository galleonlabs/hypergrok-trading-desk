#!/usr/bin/env python3
"""Mirror the canonical ChatGPT plugin skills into ``.agents/skills``.

The plugin copy is canonical because it is the packaged ChatGPT/Codex
interface.  OpenCode and repository-native Codex discovery consume the mirror.
Only the reviewed skill directories below may be written.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


SKILL_NAMES = (
    "assess-asset",
    "operate-trading-desk",
    "brief-market",
    "validate-thesis",
    "scan-signals",
    "test-strategy",
)


def _files(root: Path) -> dict[Path, bytes]:
    result: dict[Path, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"skill resources may not be symlinks: {path}")
        if path.is_file():
            result[path.relative_to(root)] = path.read_bytes()
    return result


def sync(repo_root: Path, *, check: bool) -> list[str]:
    source_root = repo_root / "plugins" / "trading-desk" / "skills"
    target_root = repo_root / ".agents" / "skills"
    differences: list[str] = []

    for name in SKILL_NAMES:
        source = source_root / name
        target = target_root / name
        if not (source / "SKILL.md").is_file():
            raise ValueError(f"canonical skill is incomplete: {source}")

        source_files = _files(source)
        target_files = _files(target) if target.exists() else {}
        if source_files != target_files:
            differences.append(name)
        if check:
            continue

        target.mkdir(parents=True, exist_ok=True)
        for relative, content in source_files.items():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != content:
                destination.write_bytes(content)

        # Remove stale files only inside this explicit, reviewed skill target.
        for relative in sorted(set(target_files) - set(source_files), reverse=True):
            stale = target / relative
            stale.unlink()
        for directory in sorted(
            (path for path in target.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            if not any(directory.iterdir()):
                directory.rmdir()

    return differences


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    differences = sync(repo_root, check=arguments.check)
    if arguments.check and differences:
        print("skill mirror is stale: " + ", ".join(differences), file=sys.stderr)
        return 1
    if differences and not arguments.check:
        print("updated: " + ", ".join(differences))
    else:
        print("skill mirror is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
