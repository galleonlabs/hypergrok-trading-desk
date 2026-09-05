#!/usr/bin/env python3
"""Check a HyperGrok install without reading keys or changing desk state."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# What the checkout says it ships, read from the two lists it already publishes
# and `scripts/check.sh` already keeps honest: the skills index links every skill,
# and the runbook the user follows names every profile it tells them to create.
SKILL_INDEX = ("skills/README.md", re.compile(r"\[([a-z0-9-]+)\]\(\1/SKILL\.md\)"))
AGENT_INDEX = ("SETUP.md", re.compile(r"`agents/([a-z0-9-]+)\.md`"))
REQUIRED_DESK_DIRS = (
    "proposals",
    "briefs",
    "research",
    "strategies",
    "data",
    "journal/incidents",
    "watch",
)


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


def result(condition: bool, name: str, pass_detail: str, fail_detail: str) -> Check:
    return Check("PASS" if condition else "FAIL", name, pass_detail if condition else fail_detail)


def present_skills(root: str) -> set[str]:
    directory = os.path.join(root, "skills")
    if not os.path.isdir(directory):
        return set()
    return {name for name in os.listdir(directory) if os.path.isfile(os.path.join(directory, name, "SKILL.md"))}


def present_agents(root: str) -> set[str]:
    directory = os.path.join(root, "agents")
    if not os.path.isdir(directory):
        return set()
    return {name[:-3] for name in os.listdir(directory) if name.endswith(".md") and os.path.isfile(os.path.join(directory, name))}


def inventory(root: str, name: str, index: tuple[str, re.Pattern], present: set[str]) -> Check:
    """Check the install component by component, against the list it publishes.

    Counting was the wrong question twice over. A count has to be bumped at every
    release - the defect the version check no longer has - and it passes a tree
    holding the right number of the wrong things, so an unpacked archive that
    dropped one skill and left a stray directory behind read as a healthy desk.
    Naming what is missing is also what the user needs: they can restore one path,
    rather than diff seventeen directories against the runbook.

    A directory the index does not list only warns. Missing is caught by name now,
    so an extra can no longer mask one, and a user's own skill is not a broken desk.
    """
    source, pattern = index
    try:
        with open(os.path.join(root, source), encoding="utf-8") as handle:
            declared = set(pattern.findall(handle.read()))
    except OSError as exc:
        return Check("FAIL", name, f"cannot read {source}: {exc}")
    if not declared:
        return Check("FAIL", name, f"{source} lists no {name}; this checkout cannot say what it ships")
    missing = sorted(declared - present)
    if missing:
        return Check("FAIL", name, f"{len(declared) - len(missing)} of {len(declared)} present; missing {', '.join(missing)}")
    extra = sorted(present - declared)
    if extra:
        return Check("WARN", name, f"all {len(declared)} present; {source} does not list {', '.join(extra)}")
    return Check("PASS", name, f"all {len(declared)} present")


def check_repository(root: str) -> list[Check]:
    checks = []
    # `plugin.json` is the release this checkout claims to be. Every other
    # release fact is read against it rather than against a constant here: a
    # constant would have to be bumped at every release, and a doctor that
    # lags the tag it ships in tells a correctly installed desk it is broken.
    version = None
    try:
        with open(os.path.join(root, "plugin.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        version = manifest.get("version")
        valid = isinstance(version, str) and bool(SEMVER_RE.match(version))
        checks.append(result(valid, "release", f"manifest version {version}", f"plugin.json version {version!r} is not a release version"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(Check("FAIL", "release", f"cannot read plugin.json: {exc}"))

    checks.append(inventory(root, "skills", SKILL_INDEX, present_skills(root)))
    checks.append(inventory(root, "agents", AGENT_INDEX, present_agents(root)))

    setup_path = os.path.join(root, "SETUP.md")
    expected_tag = f"v{version}" if version else None
    try:
        with open(setup_path, encoding="utf-8") as handle:
            setup = handle.read()
        if expected_tag is None:
            checks.append(Check("FAIL", "setup pin", "no manifest version to check the pin against"))
        else:
            pinned = f"--branch {expected_tag}" in setup
            checks.append(result(pinned, "setup pin", expected_tag, f"SETUP.md does not pin {expected_tag}; this checkout is a half-updated release"))
    except OSError as exc:
        checks.append(Check("FAIL", "setup pin", f"cannot read SETUP.md: {exc}"))

    required = ("scripts/check.sh", "scripts/desk_doctor.py", "scripts/opening_bell.py", "skills/hypergrok-bootstrap/SKILL.md")
    missing = [path for path in required if not os.path.isfile(os.path.join(root, path))]
    checks.append(result(not missing, "release files", "bootstrap, doctor and demo present", f"missing: {', '.join(missing)}"))
    return checks


def check_workspace(root: str) -> list[Check]:
    checks = []
    missing = [path for path in REQUIRED_DESK_DIRS if not os.path.isdir(os.path.join(root, path))]
    checks.append(result(not missing, "desk folders", "working folders present", f"missing: {', '.join(missing)}"))

    desk_path = os.path.join(root, "desk.md")
    if not os.path.isfile(desk_path):
        checks.append(Check("WARN", "desk record", f"{desk_path} is not written yet"))
    else:
        with open(desk_path, encoding="utf-8") as handle:
            text = handle.read()
        safe = not re.search(r"private.?key|secret\s*[:=]\s*\S+", text, re.IGNORECASE)
        checks.append(result(safe, "desk record", "present; no key-like field detected", "contains a key-like field; remove secrets from disk"))
        if "risk limits: not yet written" in text or not os.path.isfile(os.path.join(root, "risk-limits.md")):
            checks.append(Check("WARN", "risk limits", "not written; desk remains research-only"))
        else:
            checks.append(Check("PASS", "risk limits", "risk-limits.md present"))
    return checks


def check_public_api(base_url: str, timeout: float) -> Check:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/info",
        data=b'{"type":"allMids"}',
        headers={"Content-Type": "application/json", "User-Agent": "hypergrok-desk-doctor/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not payload.get("ETH"):
            return Check("FAIL", "public API", "allMids response did not contain ETH")
        return Check("PASS", "public API", "mainnet /info allMids answered without a key")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return Check("FAIL", "public API", f"unavailable: {exc}")


def render(checks: list[Check]) -> str:
    width = max(len(check.name) for check in checks)
    lines = ["HYPERGROK DESK DOCTOR", "READ ONLY · no key access · no writes", ""]
    lines.extend(f"{check.status:<4}  {check.name:<{width}}  {check.detail}" for check in checks)
    failed = sum(check.status == "FAIL" for check in checks)
    warned = sum(check.status == "WARN" for check in checks)
    lines.extend(["", f"Result: {len(checks) - failed - warned} passed, {warned} warnings, {failed} failed."])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--desk-root", help="also validate a prepared trading-desk directory")
    parser.add_argument("--base-url", default="https://api.hyperliquid.xyz")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--offline", action="store_true", help="skip the public connectivity check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    checks = check_repository(os.path.abspath(args.repo_root))
    if args.desk_root:
        checks.extend(check_workspace(os.path.abspath(args.desk_root)))
    checks.append(Check("SKIP", "public API", "offline mode") if args.offline else check_public_api(args.base_url, args.timeout))
    if args.json:
        print(json.dumps({"checks": [asdict(check) for check in checks]}, indent=2))
    else:
        print(render(checks))
    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
