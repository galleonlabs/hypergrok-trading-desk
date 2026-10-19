#!/usr/bin/env python3
"""End-to-end fixtures for scripts/release_bump.py. Stdlib Python only.

Each case copies the working tree to a temporary directory and runs the real
script against it with --root. The copy's check.sh is replaced by a stub, so
the suite proves the script calls it and returns its exit code without
running the whole check (which runs this suite) recursively.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "release_bump.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from release_bump import TAG_RE, PIN_BEFORE, doc_files
from check_manifests import MANIFESTS, PINNED_SOURCE_MANIFESTS  # noqa: E402

NEW = "9.9.9"
STUB_MARK = "stub check.sh ran"
STUB = f"""#!/usr/bin/env bash
echo "{STUB_MARK}"
exit "${{STUB_CHECK_RC:-0}}"
"""
PROSE_CLAIM = "has no native installation proof yet"
EDITED_SKILL = "skills/desk-operating-model/SKILL.md"


def run(args, env=None):
    result = subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, check=False, env=env
    )
    return result.returncode, result.stdout + result.stderr


def bump(root, *args, env=None):
    return run([SCRIPT, *args, "--root", root], env=env)


def read(root, rel):
    with open(os.path.join(root, rel), "rb") as handle:
        return handle.read()


def load(root, rel):
    return json.loads(read(root, rel))


def tree_digest(root):
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()


def tree_files(root):
    return sorted(
        os.path.relpath(os.path.join(dirpath, name), root)
        for dirpath, _, filenames in os.walk(root)
        for name in filenames
        if name.endswith((".md", ".json", ".mdc"))
    )


def review_block(output):
    lines = output.splitlines()
    if "review by hand:" not in lines:
        return []
    block = lines[lines.index("review by hand:") + 1:]
    return [line.strip() for line in block if line.startswith("  ")]


def declared_versions(data):
    found = [data.get("version"), (data.get("metadata") or {}).get("version")]
    for plugin in data.get("plugins") or []:
        found.append(plugin.get("version"))
    return [v for v in found if v is not None]


class ReleaseBumpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.baseline = os.path.join(cls.temp.name, "baseline")
        shutil.copytree(
            ROOT, cls.baseline, symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"),
        )
        with open(os.path.join(cls.baseline, "scripts", "check.sh"), "w") as handle:
            handle.write(STUB)
        cls.old = load(cls.baseline, "plugin.json")["version"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def case(self):
        return self.case_named("")

    def case_named(self, suffix):
        path = os.path.join(self.temp.name, self.id().rsplit(".", 1)[-1] + suffix)
        shutil.copytree(self.baseline, path, symlinks=True)
        return path

    def safe_case(self):
        # Synthetic, non-evidentiary fixture: remove prose release references so
        # the safe automated pin path can be tested independently of real history.
        root = self.case()
        for rel in doc_files(root):
            path = os.path.join(root, rel)
            text = read(root, rel).decode()
            text = TAG_RE.sub(lambda m: m[0] if PIN_BEFORE.search(text[:m.start()])
                              else "the recorded release", text)
            text = re.sub(r"`\d+\.\d+\.\d+`", "the recorded release", text)
            with open(path, "w") as handle:
                handle.write(text)
        return root

    def append(self, root, *lines, rel="README.md"):
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as handle:
            start = len(handle.read().splitlines()) + 2
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n" + "\n\n".join(lines) + "\n")
        return [start + 2 * i for i in range(len(lines))]

    def test_real_dated_claims_block_writes_and_dry_run_preserves_them(self):
        root = self.case()
        claim = f"On 2026-10-13 v{self.old} was published; checked `{self.old}`."
        self.append(root, claim)
        before = tree_digest(root)
        code, output = bump(root, NEW)
        self.assertEqual(code, 2, output)
        self.assertIn("manual reconciliation", output)
        self.assertNotIn(STUB_MARK, output)
        self.assertEqual(tree_digest(root), before)
        code, output = bump(root, NEW, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("manual reconciliation", output)
        self.assertEqual(tree_digest(root), before)
        self.assertIn(claim, read(root, "README.md").decode())

    def test_bare_at_rule_is_anchored_to_the_package_name(self):
        root = self.safe_case()
        anchored, other = f"hypergrok-trading-desk@{self.old} pin", f"other-pkg@{self.old} pin"
        self.append(root, anchored, other)
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        readme = read(root, "README.md").decode().splitlines()
        self.assertIn(f"hypergrok-trading-desk@{NEW} pin", readme)
        self.assertIn(other, readme)

    def test_rewrite_keeps_formatting_and_regex_bounds(self):
        root = self.safe_case()
        bounded = f"v1{self.old} {self.old}0 v{self.old}.2"
        (bounded_no,) = self.append(root, bounded)
        before = {rel: read(root, rel).decode() for rel in tree_files(root)}
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        self.assertNotIn(f"README.md:{bounded_no}:", output)
        changed = 0
        for rel, text in before.items():
            after = read(root, rel).decode()
            old_lines, new_lines = text.splitlines(keepends=True), after.splitlines(keepends=True)
            self.assertEqual(len(old_lines), len(new_lines), rel)
            for number, (was, now) in enumerate(zip(old_lines, new_lines), 1):
                if was != now and "sha256" not in was:
                    changed += 1
                    self.assertEqual(now.replace(NEW, self.old), was, f"{rel}:{number}")
        self.assertGreater(changed, 0)
        self.assertIn(bounded, read(root, "README.md").decode().splitlines())

    def test_malformed_plugin_version_exits_2(self):
        for label, rewrite in (
            ("missing", lambda data: data.pop("version")),
            ("short", lambda data: data.__setitem__("version", "1.6")),
            ("number", lambda data: data.__setitem__("version", 161)),
        ):
            with self.subTest(label):
                root = self.case_named(label)
                data = load(root, "plugin.json")
                rewrite(data)
                with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                before = tree_digest(root)
                code, output = bump(root, NEW)
                self.assertEqual(code, 2, output)
                self.assertNotIn("Traceback", output)
                self.assertIn("plugin.json", output)
                self.assertEqual(tree_digest(root), before)

    def test_same_version_prints_a_notice(self):
        root = self.case()
        code, output = bump(root, self.old)
        self.assertEqual(code, 0, output)
        self.assertIn("already", output)
        self.assertIn("0 changed line(s)", output)

    def test_tagged_effective_target_refused_in_every_mode(self):
        root = self.case()
        git = ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run([*git, *args], check=True, capture_output=True)
        for version in (self.old, NEW):
            subprocess.run([*git, "tag", f"v{version}"], check=True, capture_output=True)
        with open(os.path.join(root, EDITED_SKILL), "a") as handle:
            handle.write("\nchanged skill\n")
        before = tree_digest(root)
        for args in ((), (self.old,), (NEW,), (NEW, "--dry-run")):
            code, output = bump(root, *args)
            self.assertEqual(code, 2, output)
            self.assertIn("already tagged", output)
            self.assertNotIn(STUB_MARK, output)
            self.assertEqual(tree_digest(root), before)

    def test_broken_git_metadata_fails_closed(self):
        root = self.case()
        with open(os.path.join(root, ".git"), "w") as handle:
            handle.write("gitdir: /definitely-missing-release-fixture\n")
        before = tree_digest(root)
        code, output = bump(root)
        self.assertEqual(code, 2, output)
        self.assertNotIn(STUB_MARK, output)
        self.assertEqual(tree_digest(root), before)

    def test_missing_git_fails_closed_in_checkout(self):
        root = self.case()
        os.mkdir(os.path.join(root, ".git"))
        before = tree_digest(root)
        env = dict(os.environ, PATH="/no-release-tools")
        code, output = bump(root, env=env)
        self.assertEqual(code, 2, output)
        self.assertNotIn(STUB_MARK, output)
        self.assertEqual(tree_digest(root), before)

    def test_direct_dry_run_does_not_create_bytecode(self):
        root = self.case()
        before = tree_digest(root)
        code, output = run([os.path.join(root, "scripts/release_bump.py"), NEW, "--dry-run"])
        self.assertEqual(code, 0, output)
        self.assertEqual(tree_digest(root), before)
        self.assertFalse(os.path.exists(os.path.join(root, "scripts/__pycache__")))

    def test_paths_refuse_before_any_mutation(self):
        for variant in ("manifest-symlink", "markdown-symlink", "directory-symlink",
                        "check-symlink", "manifest-hardlink", "absolute-hash", "traversal-hash"):
            with self.subTest(variant):
                root = self.case_named(variant)
                outside = os.path.join(self.temp.name, variant + "-outside")
                with open(outside, "wb") as handle:
                    handle.write(read(root, "plugin.json"))
                outside_before = read(self.temp.name, variant + "-outside")
                if variant in ("absolute-hash", "traversal-hash"):
                    path = os.path.join(root, "template/grok-bot.json")
                    data = load(root, "template/grok-bot.json")
                    data["skills"][0]["path"] = outside if variant == "absolute-hash" else "../" + variant + "-outside"
                    with open(path, "w") as handle:
                        json.dump(data, handle)
                elif variant == "directory-symlink":
                    os.symlink(self.baseline + "/skills", root + "/linked-skills")
                else:
                    rel = {"markdown-symlink": "README.md", "check-symlink": "scripts/check.sh"}.get(variant, "plugin.json")
                    os.unlink(os.path.join(root, rel))
                    (os.link if variant.endswith("hardlink") else os.symlink)(outside, os.path.join(root, rel))
                before = tree_digest(root)
                for args in ((), (NEW,), (NEW, "--dry-run")):
                    code, output = bump(root, *args)
                    self.assertEqual(code, 2, output)
                    self.assertNotIn(STUB_MARK, output)
                    self.assertEqual(tree_digest(root), before)
                    self.assertEqual(read(self.temp.name, variant + "-outside"), outside_before)

    def test_git_checkout_with_contained_symlink(self):
        root = self.safe_case()
        link = os.path.join(root, "CLAUDE.md")
        if not os.path.islink(link):
            os.symlink("AGENTS.md", link)
        git = ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run([*git, *args], check=True, capture_output=True)
        tracked = subprocess.run([*git, "ls-files", "-s", "CLAUDE.md"], check=True,
                                 capture_output=True, text=True).stdout
        self.assertTrue(tracked.startswith("120000 "), tracked)
        before = tree_digest(root)
        code, output = bump(root, NEW, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(tree_digest(root), before)
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        self.assertIn(STUB_MARK, output)
        self.assertEqual(os.readlink(link), "AGENTS.md")
        self.assertEqual(load(root, "plugin.json")["version"], NEW)

    def test_bump_rewrites_pins_and_syncs_hashes(self):
        root = self.safe_case()
        # Real prose drifts after each release, so the fixture lines are injected.
        with open(os.path.join(root, "README.md"), "a", encoding="utf-8") as handle:
            handle.write(f"\nFixture: {self.old} {PROSE_CLAIM}.\n")
            handle.write(f"\nFixture code: hypergrok-trading-desk@{self.old}.\n")
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        self.assertIn(STUB_MARK, output)

        # The Codex marketplace declares no version; it pins only source.ref.
        versions = {rel: declared_versions(load(root, rel)) for rel in [*MANIFESTS, "package.json"]}
        self.assertGreaterEqual(sum(map(len, versions.values())), len(MANIFESTS))
        for rel, found in versions.items():
            self.assertLessEqual(set(found), {NEW}, rel)
        for rel in PINNED_SOURCE_MANIFESTS:
            for plugin in load(root, rel)["plugins"]:
                self.assertEqual(plugin["source"]["ref"], f"v{NEW}", rel)

        code, template_out = run([os.path.join(root, "scripts", "check_grok_template.py"), root])
        self.assertEqual(code, 0, template_out)
        code, manifest_out = run([os.path.join(root, "scripts", "check_manifests.py"), root])
        for needle in ("git clone --branch", "grok plugin install", "source.ref"):
            self.assertNotIn(needle, manifest_out)
        self.assertEqual(code, 0, manifest_out)

        for rel in ("CHANGELOG.md", "CONTRIBUTING.md"):
            self.assertEqual(read(root, rel), read(self.baseline, rel), rel)

        readme = read(root, "README.md").decode().splitlines()
        claim = f"Fixture: {self.old} {PROSE_CLAIM}."
        self.assertIn(claim, readme)
        self.assertIn("review by hand", output)
        self.assertIn(f"README.md:{readme.index(claim) + 1}: {claim}", output)
        self.assertIn(f"Fixture code: hypergrok-trading-desk@{NEW}.", readme)

    def test_dry_run_writes_nothing(self):
        root = self.case()
        before = tree_digest(root)
        code, output = bump(root, NEW, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(tree_digest(root), before)
        self.assertIn("dry run, nothing written", output)
        self.assertNotIn(STUB_MARK, output)
        self.assertIn("plugin.json:", output)
        self.assertIn(f"v{NEW}", output)

    def test_hash_only_after_skill_edit(self):
        root = self.case()
        with open(os.path.join(root, EDITED_SKILL), "a", encoding="utf-8") as handle:
            handle.write("\nAn edited line.\n")
        checker = os.path.join(root, "scripts", "check_grok_template.py")
        code, output = run([checker, root])
        self.assertNotEqual(code, 0, output)
        self.assertIn("sha256 does not match", output)

        code, output = bump(root)
        self.assertEqual(code, 0, output)
        self.assertIn(STUB_MARK, output)
        code, output = run([checker, root])
        self.assertEqual(code, 0, output)
        for rel in [*MANIFESTS, "package.json"]:
            self.assertLessEqual(set(declared_versions(load(root, rel))), {self.old}, rel)

    def test_same_bump_twice_changes_nothing(self):
        root = self.safe_case()
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        after_first = tree_digest(root)
        code, output = bump(root, NEW)
        self.assertEqual(code, 0, output)
        self.assertEqual(tree_digest(root), after_first)
        self.assertIn("0 changed line(s)", output)

    def test_returns_check_exit_code(self):
        root = self.case()
        env = dict(os.environ, STUB_CHECK_RC="3")
        code, output = bump(root, env=env)
        self.assertEqual(code, 3, output)
        self.assertIn(STUB_MARK, output)

    def test_invalid_version_exits_2(self):
        root = self.case()
        before = tree_digest(root)
        code, output = bump(root, "1.6")
        self.assertEqual(code, 2, output)
        self.assertEqual(tree_digest(root), before)
        self.assertNotIn(STUB_MARK, output)


if __name__ == "__main__":
    unittest.main()
