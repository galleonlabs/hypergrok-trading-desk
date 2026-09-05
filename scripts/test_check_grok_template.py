#!/usr/bin/env python3
"""Negative fixtures for scripts/check_grok_template.py."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(ROOT, "scripts", "check_grok_template.py")
DRIFT_PATH = "skills/hypergrok-bootstrap/SKILL.md"


def run(root, env=None):
    result = subprocess.run(
        [sys.executable, VALIDATOR, root],
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


def git(root, args, check=True):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "check",
            "GIT_AUTHOR_EMAIL": "check@test",
            "GIT_COMMITTER_NAME": "check",
            "GIT_COMMITTER_EMAIL": "check@test",
        }
    )
    return subprocess.run(
        ["git", "-C", root, "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def init_repo(root, tag=None):
    git(root, ["init"])
    git(root, ["add", "-A"])
    git(root, ["commit", "-m", "fixture"])
    if tag:
        git(root, ["tag", "-a", tag, "-m", tag])


def release_tag(root):
    with open(os.path.join(root, "plugin.json"), encoding="utf-8") as handle:
        return f"v{json.load(handle)['version']}"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def expect_skip(name, code, output, failures):
    if code != 0:
        failures.append(f"{name} should skip but failed:\n{output}")
    elif "skip:" not in output:
        failures.append(f"{name}: expected an explicit skip, got:\n{output}")


def test_tag_absent(temp, baseline, failures):
    case = os.path.join(temp, "tag_absent")
    shutil.copytree(baseline, case)
    init_repo(case)
    code, output = run(case)
    expect_skip("tag_absent", code, output, failures)
    if code == 0 and "skip:" in output and release_tag(case) not in output:
        failures.append(f"tag_absent: skip should name {release_tag(case)}, got:\n{output}")


def test_git_unavailable(temp, baseline, failures):
    case = os.path.join(temp, "git_unavailable")
    shutil.copytree(baseline, case)
    env = os.environ.copy()
    env["PATH"] = "/var/empty"
    code, output = run(case, env=env)
    expect_skip("git_unavailable", code, output, failures)
    if code == 0 and "skip:" in output and "git is unavailable" not in output:
        failures.append(f"git_unavailable: expected git-unavailable skip, got:\n{output}")


def test_drifted_bytes(temp, baseline, failures):
    case = os.path.join(temp, "drifted_bytes")
    shutil.copytree(baseline, case)
    tag = release_tag(case)
    init_repo(case, tag=tag)
    path = os.path.join(case, DRIFT_PATH)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n# drift\n")
    digest = sha256(path)

    def retarget(data):
        for entry in data["skills"]:
            if entry.get("name") == "hypergrok-bootstrap":
                entry["sha256"] = digest

    edit(case, retarget)
    code, output = run(case)
    if code == 0:
        failures.append("drifted_bytes: validator passed but should fail")
    elif DRIFT_PATH not in output:
        failures.append(f"drifted_bytes: expected drifted path '{DRIFT_PATH}', got:\n{output}")
    elif tag not in output:
        failures.append(f"drifted_bytes: expected tag {tag} in the failure, got:\n{output}")


def main():
    failures = []
    with tempfile.TemporaryDirectory() as temp:
        baseline = os.path.join(temp, "baseline")
        shutil.copytree(ROOT, baseline, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        code, output = run(baseline)
        if code != 0:
            failures.append(f"baseline should pass but failed:\n{output}")
        elif "skip:" not in output:
            failures.append(f"baseline: expected an explicit skip without git, got:\n{output}")
        for fixture in FIXTURES:
            case = os.path.join(temp, fixture.__name__)
            shutil.copytree(baseline, case)
            needle = fixture(case)
            code, output = run(case)
            if code == 0:
                failures.append(f"{fixture.__name__}: validator passed but should fail")
            elif needle not in output:
                failures.append(f"{fixture.__name__}: expected '{needle}', got:\n{output}")
        test_tag_absent(temp, baseline, failures)
        test_git_unavailable(temp, baseline, failures)
        test_drifted_bytes(temp, baseline, failures)
    if failures:
        print("\n".join(failures))
        print(f"{len(failures)} problem(s)")
        return 1
    print(
        f"ok: {len(FIXTURES)} Grok Bot template fixtures fail as expected; "
        "tag-absent and git-unavailable skip; drifted tagged bytes fail by path"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
