#!/usr/bin/env python3
"""Negative fixtures for scripts/check_manifests.py. Stdlib Python only; no network.

Each fixture copies the repository into a temporary directory, breaks the
distribution contract in exactly one way, and asserts the validator fails with
an error naming that file. A guard nobody has watched fail is not a guard.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(ROOT, "scripts", "check_manifests.py")


def run(root):
    proc = subprocess.run(
        [sys.executable, VALIDATOR, root], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


def edit(root, rel, mutate):
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    mutate(data)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def version_mismatch(root):
    edit(root, ".grok-plugin/plugin.json", lambda d: d.update(version="9.9.9"))
    return ".grok-plugin/plugin.json", "version"


def nested_version_mismatch(root):
    edit(root, ".claude-plugin/marketplace.json",
         lambda d: d["plugins"][0].update(version="0.1.0"))
    return ".claude-plugin/marketplace.json", "plugins[0].version"


def missing_component_path(root):
    os.remove(os.path.join(root, "assets", "mascot-320.jpg"))
    return ".cursor-plugin/plugin.json", "logo"


def missing_component_directory(root):
    shutil.rmtree(os.path.join(root, "rules"))
    return ".cursor-plugin/plugin.json", "rules"


def path_escapes_repository(root):
    edit(root, ".cursor-plugin/plugin.json", lambda d: d.update(skills="../skills/"))
    return ".cursor-plugin/plugin.json", "outside the repository"


def invalid_json(root):
    path = os.path.join(root, ".claude-plugin", "marketplace.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"name": "hypergrok",\n')
    return ".claude-plugin/marketplace.json", "invalid JSON"


def inventory_drift(root):
    added = os.path.join(root, "skills", "desk-new-skill")
    os.makedirs(added)
    with open(os.path.join(added, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: desk-new-skill\n---\n")
    return "plugin.json", "ships 17"


def wrong_name(root):
    edit(root, ".grok-plugin/marketplace.json", lambda d: d["plugins"][0].update(name="hyper-grok"))
    return ".grok-plugin/marketplace.json", "plugins[0].name"


def replace(root, rel, old, new):
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, f"{rel}: fixture anchor '{old}' not found"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


def documented_marketplace_typo(root):
    replace(root, "README.md",
            "/plugin marketplace add galleonlabs/hypergrok-trading-desk",
            "/plugin marketplace add galleonlabs/hypergrok")
    return "README.md", "/plugin marketplace add"


def documented_install_id_typo(root):
    replace(root, "SETUP.md", "/plugin install hypergrok@hypergrok",
            "/plugin install hypergrok@galleonlabs")
    return "SETUP.md", "/plugin install"


def marketplace_drops_documented_plugin(root):
    """Drift the other way: the docs still install a plugin the marketplace stopped declaring."""
    edit(root, ".claude-plugin/marketplace.json", lambda d: d.update(plugins=[]))
    return "README.md", "/plugin install"


def documented_skills_sh_plugin_slug(root):
    """skills.sh indexes skill directories; the plugin id is not one of them."""
    replace(
        root,
        "README.md",
        "https://www.skills.sh/galleonlabs/hypergrok-trading-desk)",
        "https://www.skills.sh/galleonlabs/hypergrok-trading-desk/hypergrok)",
    )
    return "README.md", "is not a skill"


def documented_skills_add_typo(root):
    replace(
        root,
        "README.md",
        "skills add galleonlabs/hypergrok-trading-desk",
        "skills add galleonlabs/hypergrok",
    )
    return "README.md", "'skills add'"


def documented_pin_lags_the_release(root):
    """Bump the manifests, forget SETUP.md: the release installs the previous one."""
    edit(root, "plugin.json", lambda d: d.update(version="9.9.9"))
    for rel in (
        ".claude-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        ".grok-plugin/plugin.json",
    ):
        edit(root, rel, lambda d: d.update(version="9.9.9"))
    edit(root, ".claude-plugin/marketplace.json",
         lambda d: (d["metadata"].update(version="9.9.9"),
                    d["plugins"][0].update(version="9.9.9")))
    edit(root, ".grok-plugin/marketplace.json",
         lambda d: d["plugins"][0].update(version="9.9.9"))
    return "SETUP.md", "expected v9.9.9"


def current_pin(root):
    """The tag `SETUP.md` pins today. Derived, so a release does not break these."""
    with open(os.path.join(root, "plugin.json"), encoding="utf-8") as fh:
        return "v" + json.load(fh)["version"]


def documented_pin_is_a_moving_branch(root):
    replace(root, "SETUP.md", f"--branch {current_pin(root)}", "--branch main")
    return "SETUP.md", "--branch main"


def documented_clone_is_unpinned(root):
    replace(root, "SETUP.md", f"--branch {current_pin(root)} ", "")
    return "SETUP.md", "is not pinned"


FIXTURES = [
    version_mismatch,
    nested_version_mismatch,
    missing_component_path,
    missing_component_directory,
    path_escapes_repository,
    invalid_json,
    inventory_drift,
    wrong_name,
    documented_marketplace_typo,
    documented_install_id_typo,
    marketplace_drops_documented_plugin,
    documented_skills_sh_plugin_slug,
    documented_skills_add_typo,
    documented_pin_lags_the_release,
    documented_pin_is_a_moving_branch,
    documented_clone_is_unpinned,
]


def main():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        baseline = os.path.join(tmp, "baseline")
        shutil.copytree(ROOT, baseline, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        code, output = run(baseline)
        if code != 0:
            failures.append(f"baseline copy should pass but failed:\n{output}")

        for fixture in FIXTURES:
            case = os.path.join(tmp, fixture.__name__)
            shutil.copytree(baseline, case)
            rel, needle = fixture(case)
            code, output = run(case)
            if code == 0:
                failures.append(f"{fixture.__name__}: validator passed but should have failed")
            elif rel not in output or needle not in output:
                failures.append(
                    f"{fixture.__name__}: expected an error naming '{rel}' and '{needle}', got:\n{output}"
                )

    if failures:
        print("\n".join(failures))
        print(f"{len(failures)} problem(s)")
        return 1
    print(f"ok: {len(FIXTURES)} manifest fixtures fail the check as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
