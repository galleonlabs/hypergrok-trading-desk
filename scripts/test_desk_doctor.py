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
    def make_repo(self, root):
        with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": "1.3.0"}, handle)
        with open(os.path.join(root, "SETUP.md"), "w", encoding="utf-8") as handle:
            handle.write("git clone --branch v1.3.0 repo\n")
        for index in range(17):
            path = os.path.join(root, "skills", f"skill-{index}")
            os.makedirs(path)
            open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(root, "skills", "hypergrok-bootstrap"), exist_ok=True)
        open(os.path.join(root, "skills", "hypergrok-bootstrap", "SKILL.md"), "a", encoding="utf-8").close()
        # Replace one generic skill so the total remains 17.
        os.remove(os.path.join(root, "skills", "skill-16", "SKILL.md"))
        for index in range(7):
            path = os.path.join(root, "agents")
            os.makedirs(path, exist_ok=True)
            open(os.path.join(path, f"agent-{index}.md"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(root, "scripts"))
        for name in ("check.sh", "desk_doctor.py", "opening_bell.py"):
            open(os.path.join(root, "scripts", name), "w", encoding="utf-8").close()

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
