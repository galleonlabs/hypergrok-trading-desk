#!/usr/bin/env python3
"""Negative fixtures for scripts/check_grok_template.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(ROOT, "scripts", "check_grok_template.py")


def run(root):
    result = subprocess.run(
        [sys.executable, VALIDATOR, root], capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout + result.stderr


def edit(root, mutate):
    path = os.path.join(root, "template", "grok-bot.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    mutate(data)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def wrong_release(root):
    edit(root, lambda data: data["source"].update(release="v0.0.0"))
    return "source.release"


def missing_skill(root):
    edit(root, lambda data: data["skills"].pop())
    return "exactly the 17 repository skills"


def stale_hash(root):
    edit(root, lambda data: data["skills"][0].update(sha256="0" * 64))
    return "sha256 does not match"


def template_has_plugin(root):
    edit(root, lambda data: data.update(plugins=["exchange-connector"]))
    return "plugins must be empty"


def invalid_public_url(root):
    edit(root, lambda data: data.update(status="published", publicShareUrl="https://example.com/bot"))
    return "published template needs"


FIXTURES = [wrong_release, missing_skill, stale_hash, template_has_plugin, invalid_public_url]


def main():
    failures = []
    with tempfile.TemporaryDirectory() as temp:
        baseline = os.path.join(temp, "baseline")
        shutil.copytree(ROOT, baseline, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        code, output = run(baseline)
        if code != 0:
            failures.append(f"baseline should pass but failed:\n{output}")
        for fixture in FIXTURES:
            case = os.path.join(temp, fixture.__name__)
            shutil.copytree(baseline, case)
            needle = fixture(case)
            code, output = run(case)
            if code == 0:
                failures.append(f"{fixture.__name__}: validator passed but should fail")
            elif needle not in output:
                failures.append(f"{fixture.__name__}: expected '{needle}', got:\n{output}")
    if failures:
        print("\n".join(failures))
        print(f"{len(failures)} problem(s)")
        return 1
    print(f"ok: {len(FIXTURES)} Grok Bot template fixtures fail as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
