from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def resolve_rule(rules: dict[str, str], command: str) -> str | None:
    """Model OpenCode's documented last-matching-rule behavior."""

    result: str | None = None
    for pattern, action in rules.items():
        if fnmatchcase(command, pattern):
            result = action
    return result


class OpenCodeCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))

    def test_config_is_model_neutral_and_uses_only_local_read_tools(self) -> None:
        self.assertEqual("https://opencode.ai/config.json", self.config["$schema"])
        self.assertNotIn("model", self.config)
        self.assertNotIn("provider", self.config)
        self.assertEqual(
            {
                "type": "local",
                "command": ["python3", "plugins/trading-desk/server.py"],
                "cwd": ".",
                "enabled": True,
                "timeout": 30000,
            },
            self.config["mcp"]["trading_desk"],
        )

    def test_global_permissions_fail_closed(self) -> None:
        permissions = self.config["permission"]
        self.assertEqual("ask", permissions["*"])
        self.assertEqual("deny", permissions["external_directory"])
        self.assertEqual("deny", permissions["bash"]["*"])
        self.assertEqual("deny", permissions["bash"]["git diff*"])
        self.assertEqual("deny", permissions["bash"]["git log*"])
        self.assertEqual("deny", permissions["bash"]["git push*"])
        self.assertEqual("deny", permissions["bash"]["rm *"])
        self.assertEqual("deny", permissions["read"]["*.env"])
        self.assertEqual("deny", permissions["read"]["*.key"])
        self.assertEqual("deny", permissions["read"]["*.sqlite*"])
        self.assertEqual("deny", permissions["edit"]["*.env"])

    def test_bash_cannot_bypass_secret_read_denials_through_git(self) -> None:
        bash = self.config["permission"]["bash"]
        self.assertEqual("allow", resolve_rule(bash, "git status --short --branch"))
        self.assertEqual("deny", resolve_rule(bash, "git status --ignored .env"))
        self.assertEqual(
            "deny",
            resolve_rule(bash, "git diff --no-index .env README.md"),
        )
        self.assertEqual(
            "deny",
            resolve_rule(bash, "git diff --no-index wallet.key README.md"),
        )
        self.assertEqual("deny", resolve_rule(bash, "git log -p -- .env"))

    def test_only_reviewed_repo_skills_and_mcp_tools_are_allowed(self) -> None:
        skills = self.config["permission"]["skill"]
        self.assertEqual(
            {
                "*": "deny",
                "operate-trading-desk": "allow",
                "brief-market": "allow",
                "validate-thesis": "allow",
                "scan-signals": "allow",
                "test-strategy": "allow",
            },
            skills,
        )
        mcp_permissions = {
            key: value
            for key, value in self.config["permission"].items()
            if key.startswith("trading_desk_")
        }
        self.assertEqual(
            {
                "trading_desk_*": "deny",
                "trading_desk_get_harness_status": "allow",
                "trading_desk_get_market_brief": "allow",
                "trading_desk_validate_trade_intent": "allow",
            },
            mcp_permissions,
        )
        permission_order = list(self.config["permission"])
        for tool_name in set(mcp_permissions) - {"trading_desk_*"}:
            self.assertLess(
                permission_order.index("trading_desk_*"),
                permission_order.index(tool_name),
            )
        plan = self.config["agent"]["plan"]["permission"]
        self.assertEqual("deny", plan["edit"])
        self.assertEqual("deny", plan["bash"])
        self.assertEqual(skills, plan["skill"])
        self.assertEqual(
            mcp_permissions,
            {
                key: value
                for key, value in plan.items()
                if key.startswith("trading_desk_")
            },
        )
        plan_order = list(plan)
        for tool_name in set(mcp_permissions) - {"trading_desk_*"}:
            self.assertLess(
                plan_order.index("trading_desk_*"),
                plan_order.index(tool_name),
            )


if __name__ == "__main__":
    unittest.main()
