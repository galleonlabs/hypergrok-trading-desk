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
EXPECTED_SKILLS = 17
EXPECTED_AGENTS = 7
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

    skills_dir = os.path.join(root, "skills")
    agents_dir = os.path.join(root, "agents")
    skills = [name for name in os.listdir(skills_dir) if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))] if os.path.isdir(skills_dir) else []
    agents = [name for name in os.listdir(agents_dir) if name.endswith(".md") and os.path.isfile(os.path.join(agents_dir, name))] if os.path.isdir(agents_dir) else []
    checks.append(result(len(skills) == EXPECTED_SKILLS, "skills", f"{len(skills)} skills present", f"expected {EXPECTED_SKILLS}, found {len(skills)}"))
    checks.append(result(len(agents) == EXPECTED_AGENTS, "agents", f"{len(agents)} agent profiles present", f"expected {EXPECTED_AGENTS}, found {len(agents)}"))

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
