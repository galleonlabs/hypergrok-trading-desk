#!/usr/bin/env python3
"""Validates the plugin distribution manifests. Stdlib Python only; no network.

Every marketplace and plugin manifest in this repository describes the same
distributable: one plugin named `hypergrok` at one version, pointing at the
skills, agents and rules checked in here, and the install commands the docs
tell a user to type must name that same distributable. This script fails the
build when a manifest stops matching what the repository actually ships.

Usage: python3 scripts/check_manifests.py [repo-root]
"""
import json
import os
import re
import sys

CANONICAL_NAME = "hypergrok"
REPOSITORY = "https://github.com/galleonlabs/hypergrok-trading-desk"

MANIFESTS = [
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".cursor-plugin/plugin.json",
    ".grok-plugin/plugin.json",
    ".grok-plugin/marketplace.json",
]

# Manifest keys whose value is a path into this repository.
PATH_KEYS = ("source", "logo", "skills", "agents", "rules")

# The marketplace manifest Claude Code resolves `/plugin marketplace add` against.
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
MARKETPLACE_ADD_RE = re.compile(r"/plugin marketplace add\s+([^\s`]+)")
PLUGIN_INSTALL_RE = re.compile(r"/plugin install\s+([^\s`]+)")
# skills.sh indexes skill directories, not the plugin id. A trailing /hypergrok
# is therefore a listing that does not exist.
SKILLS_SH_RE = re.compile(
    r"https://(?:www\.)?skills\.sh/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)"
)
SKILLS_ADD_RE = re.compile(
    r"\bskills add\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)"
)
# `SETUP.md` section 1 clones a pinned tag, so a `git clone` of this repository
# in the docs must name the tag of the version the manifests declare.
CLONE_RE = re.compile(r"git clone[^\n]*")
CLONE_BRANCH_RE = re.compile(r"--branch(?:=|\s+)([^\s`]+)")

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}
COUNT_RE = re.compile(r"\b([A-Za-z]+|\d+)[ -](?:specialist |shared )?(roles?|skills?)\b")


def load(root, rel, errors):
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        errors.append(f"{rel}: manifest is missing")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        errors.append(f"{rel}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}")
    except UnicodeDecodeError as exc:
        errors.append(f"{rel}: not valid UTF-8: {exc}")
    return None


def entries(data):
    """The manifest object itself plus any nested marketplace plugin entries."""
    if not isinstance(data, dict):
        return []
    found = [("", data)]
    for i, plugin in enumerate(data.get("plugins") or []):
        if isinstance(plugin, dict):
            found.append((f"plugins[{i}].", plugin))
    return found


def check_paths(root, rel, prefix, entry, errors):
    for key in PATH_KEYS:
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, dict):
            if value.get("type") not in (None, "local"):
                continue  # a remote source is not this repository's tree
            value = value.get("path")
        if not isinstance(value, str) or not value:
            errors.append(f"{rel}: {prefix}{key} is not a path")
            continue
        if value.startswith(("http://", "https://")):
            errors.append(f"{rel}: {prefix}{key} must be a repository path, not a URL")
            continue
        resolved = os.path.normpath(os.path.join(root, value))
        inside = os.path.normpath(root)
        if os.path.relpath(resolved, inside).startswith(".."):
            errors.append(f"{rel}: {prefix}{key} '{value}' resolves outside the repository")
            continue
        if not os.path.exists(resolved):
            errors.append(f"{rel}: {prefix}{key} '{value}' does not exist")


def declared_counts(value, found):
    """Collect counts a manifest claims in prose, e.g. 'sixteen skills'."""
    if isinstance(value, dict):
        for item in value.values():
            declared_counts(item, found)
    elif isinstance(value, list):
        for item in value:
            declared_counts(item, found)
    elif isinstance(value, str):
        for token, noun in COUNT_RE.findall(value.lower()):
            count = int(token) if token.isdigit() else NUMBER_WORDS.get(token)
            if count is not None:
                found.append((count, "skills" if noun.startswith("skill") else "roles"))


def markdown_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for name in sorted(filenames):
            if name.endswith(".md"):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def check_skills_pack_target(rel, kind, target, shorthand, skill_names, errors):
    """A skills.sh path or `skills add` argument must name this pack or a skill it ships.

    The plugin id is not a skill. skills.sh renders `/hypergrok` as a missing
    skill even though the pack page for this repository is live.
    """
    parts = target.split("/")
    if len(parts) not in (2, 3):
        errors.append(f"{rel}: {kind} '{target}' should name '{shorthand}'")
        return
    repo = f"{parts[0]}/{parts[1]}"
    if repo != shorthand:
        errors.append(f"{rel}: {kind} '{target}' should name '{shorthand}'")
        return
    if len(parts) == 3 and parts[2] not in skill_names:
        errors.append(
            f"{rel}: {kind} '{target}' names '{parts[2]}', which is not a skill "
            "this repository ships"
        )


def check_documented_pin(root, version, errors):
    """A documented `git clone` of this repository must pin the released tag.

    `CONTRIBUTING.md`: the tag must contain a `SETUP.md` that pins to that same
    tag. Bumping the manifests and forgetting the pin ships a release whose
    install instructions build the *previous* one, and nothing about that fails
    loudly - the user reads one set of instructions and installs another. The
    manifest version is the release being cut, so `v<version>` is the only tag
    a clone command in these docs may name.
    """
    if version is None:
        return
    expected = f"v{version}"
    for rel in markdown_files(root):
        if rel == "CHANGELOG.md":
            continue  # a historical record, pinned to the release it describes
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            text = fh.read()
        for command in CLONE_RE.findall(text):
            if REPOSITORY not in command:
                continue  # cloning something else, or prose about `git clone`
            found = CLONE_BRANCH_RE.search(command)
            if not found:
                errors.append(
                    f"{rel}: 'git clone' of this repository is not pinned; "
                    f"it needs --branch {expected}"
                )
            elif found.group(1) != expected:
                errors.append(
                    f"{rel}: 'git clone --branch {found.group(1)}' does not pin "
                    f"the version the manifests declare (expected {expected})"
                )


def check_documented_install(root, marketplace, skill_names, errors):
    """Install commands in the docs must name ids this repository actually declares.

    A wrong marketplace or plugin id is the one defect a reader cannot work
    around: the command simply fails for them. The same is true of a skills.sh
    listing or `skills add` argument that names a slug the pack does not ship.
    """
    if not isinstance(marketplace, dict):
        return
    shorthand = REPOSITORY.removeprefix("https://github.com/")
    market_name = marketplace.get("name")
    install_ids = {
        f"{p.get('name')}@{market_name}"
        for p in (marketplace.get("plugins") or [])
        if isinstance(p, dict)
    }
    for rel in markdown_files(root):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            text = fh.read()
        for arg in MARKETPLACE_ADD_RE.findall(text):
            if arg != shorthand:
                errors.append(
                    f"{rel}: '/plugin marketplace add {arg}' should add '{shorthand}'"
                )
        for arg in PLUGIN_INSTALL_RE.findall(text):
            if arg not in install_ids:
                errors.append(
                    f"{rel}: '/plugin install {arg}' is not declared by "
                    f"{MARKETPLACE_MANIFEST} (expected one of {sorted(install_ids)})"
                )
        for arg in SKILLS_SH_RE.findall(text):
            check_skills_pack_target(
                rel, "skills.sh listing", arg, shorthand, skill_names, errors
            )
        for arg in SKILLS_ADD_RE.findall(text):
            check_skills_pack_target(
                rel, "'skills add'", arg, shorthand, skill_names, errors
            )


def main(root):
    errors = []
    skills = sorted(
        d for d in os.listdir(os.path.join(root, "skills"))
        if os.path.exists(os.path.join(root, "skills", d, "SKILL.md"))
    )
    agents = sorted(f for f in os.listdir(os.path.join(root, "agents")) if f.endswith(".md"))
    inventory = {"skills": len(skills), "roles": len(agents)}
    if not skills:
        errors.append("skills/: no skill directories with a SKILL.md")
    if not agents:
        errors.append("agents/: no agent files")

    root_manifest = load(root, MANIFESTS[0], errors)
    version = (root_manifest or {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"{MANIFESTS[0]}: version '{version}' is not a semantic version")
        version = None

    marketplace = None
    for rel in MANIFESTS:
        data = root_manifest if rel == MANIFESTS[0] else load(root, rel, errors)
        if data is None:
            continue
        if rel == MARKETPLACE_MANIFEST:
            marketplace = data
        for prefix, entry in entries(data):
            name = entry.get("name")
            if name != CANONICAL_NAME:
                errors.append(f"{rel}: {prefix}name '{name}' should be '{CANONICAL_NAME}'")
            declared = entry.get("version", (entry.get("metadata") or {}).get("version"))
            if declared is not None and version is not None and declared != version:
                errors.append(
                    f"{rel}: {prefix}version '{declared}' does not match "
                    f"{MANIFESTS[0]} version '{version}'"
                )
            for key in ("repository", "homepage"):
                if key in entry and entry[key] != REPOSITORY:
                    errors.append(f"{rel}: {prefix}{key} '{entry[key]}' should be '{REPOSITORY}'")
            check_paths(root, rel, prefix, entry, errors)

        counts = []
        declared_counts(data, counts)
        for count, noun in counts:
            if count != inventory[noun]:
                errors.append(
                    f"{rel}: claims {count} {noun} but the repository ships {inventory[noun]}"
                )

    check_documented_install(root, marketplace, set(skills), errors)
    check_documented_pin(root, version, errors)

    if errors:
        print("\n".join(errors))
        print(f"{len(errors)} problem(s)")
        return 1
    print(f"ok: {len(MANIFESTS)} manifests, {len(skills)} skills, {len(agents)} agents")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
