#!/usr/bin/env python3
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("desk_doctor", os.path.join(ROOT, "scripts", "desk_doctor.py"))
desk_doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = desk_doctor
SPEC.loader.exec_module(desk_doctor)


class DeskDoctorTest(unittest.TestCase):
    SKILLS = [f"skill-{index}" for index in range(16)] + ["hypergrok-bootstrap"]
    AGENTS = [f"agent-{index}" for index in range(7)]

    def make_repo(self, root):
        """A checkout that publishes an inventory and then satisfies it."""
        with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": "1.3.0"}, handle)
        with open(os.path.join(root, "SETUP.md"), "w", encoding="utf-8") as handle:
            handle.write("git clone --branch v1.3.0 repo\n")
            handle.writelines(f"| `agents/{name}.md` | role |\n" for name in self.AGENTS)
        os.makedirs(os.path.join(root, "skills"))
        with open(os.path.join(root, "skills", "README.md"), "w", encoding="utf-8") as handle:
            handle.writelines(f"| [{name}]({name}/SKILL.md) | what it teaches |\n" for name in self.SKILLS)
        for name in self.SKILLS:
            os.makedirs(os.path.join(root, "skills", name))
            open(os.path.join(root, "skills", name, "SKILL.md"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(root, "agents"))
        for name in self.AGENTS:
            open(os.path.join(root, "agents", f"{name}.md"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(root, "scripts"))
        for name in ("check.sh", "desk_doctor.py", "opening_bell.py"):
            open(os.path.join(root, "scripts", name), "w", encoding="utf-8").close()

    def named(self, checks, name):
        return next(check for check in checks if check.name == name)

    def test_repository_fixture_passes(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            checks = desk_doctor.check_repository(root)
            self.assertTrue(all(check.status == "PASS" for check in checks), checks)

    def test_this_release_passes_its_own_doctor(self):
        """The doctor must pass the release it ships in.

        It read its expected version from a constant once, and a release that
        bumped the manifests left every correctly installed desk being told
        `expected 1.3.0, found 1.4.0`. Checking the real tree here means that
        can only ever fail in CI, never in a user's first five minutes.
        """
        checks = desk_doctor.check_repository(ROOT)
        self.assertEqual([check for check in checks if check.status != "PASS"], [])

    def test_missing_skill_is_named(self):
        """`expected 17, found 16` left the user diffing the runbook by hand."""
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            os.remove(os.path.join(root, "skills", "skill-3", "SKILL.md"))
            check = self.named(desk_doctor.check_repository(root), "skills")
            self.assertEqual(check.status, "FAIL", check)
            self.assertIn("skill-3", check.detail)

    def test_right_count_of_the_wrong_skills_fails(self):
        """The count check passed this: an unpacked archive that dropped one
        skill and left a stray directory behind reported a healthy desk."""
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            skills = os.path.join(root, "skills")
            os.rename(os.path.join(skills, "skill-3"), os.path.join(skills, "skill-3.bak"))
            check = self.named(desk_doctor.check_repository(root), "skills")
            self.assertEqual(check.status, "FAIL", check)
            self.assertIn("skill-3", check.detail)

    def test_undeclared_skill_warns_without_failing(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            os.makedirs(os.path.join(root, "skills", "my-own-skill"))
            open(os.path.join(root, "skills", "my-own-skill", "SKILL.md"), "w", encoding="utf-8").close()
            checks = desk_doctor.check_repository(root)
            self.assertEqual(self.named(checks, "skills").status, "WARN")
            self.assertIn("my-own-skill", self.named(checks, "skills").detail)
            self.assertFalse([check for check in checks if check.status == "FAIL"])

    def test_missing_agent_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            os.remove(os.path.join(root, "agents", "agent-5.md"))
            check = self.named(desk_doctor.check_repository(root), "agents")
            self.assertEqual(check.status, "FAIL", check)
            self.assertIn("agent-5", check.detail)

    def test_missing_skills_index_fails_rather_than_passing_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            os.remove(os.path.join(root, "skills", "README.md"))
            self.assertEqual(self.named(desk_doctor.check_repository(root), "skills").status, "FAIL")

    def test_setup_pin_lagging_the_manifest_fails(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_repo(root)
            with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as handle:
                json.dump({"version": "1.4.0"}, handle)
            checks = desk_doctor.check_repository(root)
            self.assertTrue(any(check.status == "FAIL" and check.name == "setup pin" for check in checks), checks)

    def test_workspace_warns_without_record_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as root:
            for path in desk_doctor.REQUIRED_DESK_DIRS:
                os.makedirs(os.path.join(root, path))
            checks = desk_doctor.check_workspace(root)
            self.assertEqual([check.status for check in checks], ["PASS", "WARN"])

    def test_workspace_rejects_key_like_material(self):
        with tempfile.TemporaryDirectory() as root:
            for path in desk_doctor.REQUIRED_DESK_DIRS:
                os.makedirs(os.path.join(root, path))
            with open(os.path.join(root, "desk.md"), "w", encoding="utf-8") as handle:
                handle.write("private key: should-never-be-here\n")
            checks = desk_doctor.check_workspace(root)
            self.assertTrue(any(check.status == "FAIL" and check.name == "desk record" for check in checks))


if __name__ == "__main__":
    unittest.main()
