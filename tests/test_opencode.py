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

    def test_config_is_model_neutral_and_uses_loopback_remote_mcp(self) -> None:
        self.assertEqual("https://opencode.ai/config.json", self.config["$schema"])
        self.assertNotIn("model", self.config)
        self.assertNotIn("provider", self.config)
        self.assertEqual(
            {
                "type": "remote",
                "url": "http://127.0.0.1:8765/mcp",
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

    def test_allowed_commands_cannot_append_shell_control_operators(self) -> None:
        bash = self.config["permission"]["bash"]
        for command in (
            "python3 -m unittest; env",
            "python3 -m unittest && env",
            "python3 -m compileall src | sh",
            "trading-harness doctor > /tmp/output",
            "python3 -m unittest $(env)",
            "python3 -m unittest `env`",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", resolve_rule(bash, command))

    def test_only_reviewed_repo_skills_and_mcp_tools_are_allowed(self) -> None:
        skills = self.config["permission"]["skill"]
        self.assertEqual(
            {
                "*": "deny",
                "assess-asset": "allow",
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
                "trading_desk_analyze_asset": "ask",
                "trading_desk_get_latest_sentiment": "allow",
                "trading_desk_get_learning_review": "allow",
                "trading_desk_get_learning_summary": "allow",
                "trading_desk_get_node_status": "allow",
                "trading_desk_get_harness_status": "allow",
                "trading_desk_get_market_brief": "allow",
                "trading_desk_get_trade_stage": "allow",
                "trading_desk_list_tracked_assets": "allow",
                "trading_desk_pause_tracked_asset": "ask",
                "trading_desk_record_manual_sentiment": "ask",
                "trading_desk_stage_trade_candidate": "ask",
                "trading_desk_track_asset": "ask",
                "trading_desk_validate_candidate_profitability": "allow",
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
        plan_mcp = {
            key: value
            for key, value in plan.items()
            if key.startswith("trading_desk_")
        }
        self.assertEqual(
            plan_mcp,
            {
                key: value
                for key, value in mcp_permissions.items()
                if value == "allow" or key == "trading_desk_*"
            },
        )
        plan_order = list(plan)
        for tool_name in set(plan_mcp) - {"trading_desk_*"}:
            self.assertLess(
                plan_order.index("trading_desk_*"),
                plan_order.index(tool_name),
            )


if __name__ == "__main__":
    unittest.main()
