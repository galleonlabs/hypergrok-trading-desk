from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))

    def test_config_is_model_neutral_and_has_no_mcp(self) -> None:
        self.assertEqual("https://opencode.ai/config.json", self.config["$schema"])
        self.assertNotIn("model", self.config)
        self.assertNotIn("provider", self.config)
        self.assertNotIn("mcp", self.config)

    def test_global_permissions_fail_closed(self) -> None:
        permissions = self.config["permission"]
        self.assertEqual("ask", permissions["*"])
        self.assertEqual("deny", permissions["external_directory"])
        self.assertEqual("deny", permissions["bash"]["*"])
        self.assertEqual("deny", permissions["bash"]["git push*"])
        self.assertEqual("deny", permissions["bash"]["rm *"])
        self.assertEqual("deny", permissions["read"]["*.env"])
        self.assertEqual("deny", permissions["read"]["*.key"])
        self.assertEqual("deny", permissions["read"]["*.sqlite*"])
        self.assertEqual("deny", permissions["edit"]["*.env"])

    def test_only_reviewed_repo_skills_are_allowed(self) -> None:
        skills = self.config["permission"]["skill"]
        self.assertEqual(
            {
                "*": "deny",
                "validate-thesis": "allow",
                "scan-signals": "allow",
            },
            skills,
        )
        plan = self.config["agent"]["plan"]["permission"]
        self.assertEqual("deny", plan["edit"])
        self.assertEqual("deny", plan["bash"])
        self.assertEqual(skills, plan["skill"])


if __name__ == "__main__":
    unittest.main()
