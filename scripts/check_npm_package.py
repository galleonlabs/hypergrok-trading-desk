#!/usr/bin/env python3
"""Checks the npm resource package contract. Stdlib Python only; no network.

This package distributes instructions and read-only Python tools. It has no
JavaScript entry point, install hooks or dependencies. The exact allowlist
keeps local desk state and generated files out of a release.

Usage: python3 scripts/check_npm_package.py [repo-root] [--tarball package.tgz]
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tarfile

NAME = "hypergrok-trading-desk"
REPOSITORY = "https://github.com/galleonlabs/hypergrok-trading-desk"
ROOT_FILES = (
    "AGENTS.md", "ATTRIBUTION.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE",
    "README.md", "SECURITY.md", "SETUP.md", "plugin.json", "skills/README.md",
)
MANIFESTS = (
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    ".cursor-plugin/plugin.json", ".grok-plugin/plugin.json",
    ".grok-plugin/marketplace.json",
)
RESOURCES = (
    "assets/mascot.jpg", "assets/mascot-320.jpg", "rules/hypergrok-team.mdc",
    "template/grok-bot.json", "docs/ARCHITECTURE.md", "docs/FAQ.md",
    "docs/FORK_REVIEW.md", "docs/GROK_BOT_TEMPLATE.md", "docs/PROVENANCE.md",
    "docs/RESEARCH_DATASETS.md",
)
SCRIPTS = (
    "check.sh", "check_grok_template.py", "check_manifests.py",
    "check_npm_package.py", "check_public_template.py", "cli_contract.py", "desk_doctor.py",
    "opening_bell.py", "research_dataset.py", "test_research_dataset.py", "test_check_grok_template.py", "test_check_manifests.py",
    "test_check_npm_package.py", "test_check_public_template.py",
    "test_desk_doctor.py", "test_opening_bell.py", "test_order_rounding.py",
)
SAFE_SCRIPTS = {"test": "bash scripts/check.sh", "prepack": "bash scripts/check.sh"}
NO_RUNTIME_FIELDS = (
    "bin", "main", "module", "exports", "browser", "dependencies",
    "optionalDependencies", "peerDependencies", "devDependencies",
    "bundledDependencies", "bundleDependencies", "gypfile",
)


def load(root, relative, errors):
    try:
        value = json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative}: cannot read valid JSON: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{relative}: must be a JSON object")
        return {}
    return value


def required_files(root, errors):
    skills = sorted(root.glob("skills/*/SKILL.md"))
    agents = sorted(root.glob("agents/*.md"))
    if len(skills) != 17:
        errors.append(f"package.json: expected 17 skills, found {len(skills)}")
    if len(agents) != 7:
        errors.append(f"package.json: expected 7 agents, found {len(agents)}")
    paths = set(ROOT_FILES + MANIFESTS + RESOURCES)
    paths.update(f"scripts/{name}" for name in SCRIPTS)
    for skill in skills:
        for name in ("SKILL.md", "LICENSE", "ATTRIBUTION.md"):
            paths.add((skill.parent / name).relative_to(root).as_posix())
    paths.update(path.relative_to(root).as_posix() for path in agents)
    return paths


def check(root):
    errors = []
    package = load(root, "package.json", errors)
    plugin = load(root, "plugin.json", errors)
    if package.get("name") != NAME:
        errors.append(f"package.json: name must be '{NAME}'")
    version = package.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        errors.append("package.json: version must be a stable semantic version")
    if version != plugin.get("version"):
        errors.append("package.json: version must match plugin.json")
    if package.get("license") != "MIT" or package.get("license") != plugin.get("license"):
        errors.append("package.json: license must be MIT and match plugin.json")
    if package.get("repository") != {"type": "git", "url": f"git+{REPOSITORY}.git"}:
        errors.append("package.json: repository must name the canonical GitHub repository")
    if plugin.get("repository") != REPOSITORY:
        errors.append("plugin.json: repository must match the npm package repository")
    if package.get("publishConfig") != {"access": "public", "registry": "https://registry.npmjs.org/"}:
        errors.append("package.json: publishConfig must target the public npm registry")
    if package.get("private") not in (None, False):
        errors.append("package.json: private must not prevent publication")
    if package.get("scripts") != SAFE_SCRIPTS:
        errors.append("package.json: scripts may only run native test and prepack checks; install lifecycle hooks are forbidden")
    for field in NO_RUNTIME_FIELDS:
        if field in package:
            errors.append(f"package.json: '{field}' is not part of this resource-only distribution")
    expected = required_files(root, errors)
    files = package.get("files")
    if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
        errors.append("package.json: files must be an explicit list of file paths")
        files = []
    if len(files) != len(set(files)):
        errors.append("package.json: files contains duplicate paths")
    missing = expected - set(files)
    extra = set(files) - expected
    if missing:
        errors.append("package.json: allowlist is missing required files: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("package.json: allowlist contains unapproved files or patterns: " + ", ".join(sorted(extra)))
    for relative in sorted(expected):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            errors.append(f"package.json: required file '{relative}' is missing or is a symlink")
        elif not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"package.json: required file '{relative}' resolves outside the repository")
    return errors, expected | {"package.json"}


def check_tarball(path, expected, root=None):
    """Inspect an actual npm artifact without extracting or running its content."""
    errors, found = [], set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = member.name.removeprefix("package/")
                if member.name != f"package/{relative}" or not member.isfile():
                    errors.append(f"npm tarball: unexpected path or file type '{member.name}'")
                elif relative not in expected:
                    errors.append(f"npm tarball: unapproved file '{relative}'")
                elif relative in found:
                    errors.append(f"npm tarball: duplicate file '{relative}'")
                elif root is not None:
                    content = archive.extractfile(member)
                    if content.read() != (root / relative).read_bytes():
                        errors.append(f"npm tarball: '{relative}' differs from the reviewed source")
                found.add(relative)
    except (OSError, tarfile.TarError) as exc:
        errors.append(f"npm tarball: cannot read archive: {exc}")
    missing = expected - found
    if missing:
        errors.append("npm tarball: missing required files: " + ", ".join(sorted(missing)))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--tarball", type=Path)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    errors, expected = check(root)
    if args.tarball:
        errors.extend(check_tarball(args.tarball, expected, root))
    if errors:
        print("\n".join(errors))
        print(f"{len(errors)} problem(s)")
        return 1
    suffix = "; npm tarball contains the exact allowlist" if args.tarball else ""
    print(f"ok: npm resource package, {len(expected)} files, no runtime dependencies or install hooks{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
