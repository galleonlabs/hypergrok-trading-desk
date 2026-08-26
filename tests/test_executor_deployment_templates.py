from __future__ import annotations

from pathlib import Path
import plistlib
import re
import unittest

from trading_harness.executor_cli import build_parser
from trading_harness.executor_config import parse_executor_config
from trading_harness.planning import RiskSizingPolicy


ROOT = Path(__file__).resolve().parents[1]
LAUNCHD = ROOT / "deploy/launchd/com.jawndiego.trading-desk-executor.plist.example"
CONFIG = ROOT / "deploy/config/testnet-executor.toml.example"
LEARNING_LAUNCHD = ROOT / "deploy/launchd/com.jawndiego.trading-desk-learning-mcp.plist.example"
LEARNING_SYSTEMD = ROOT / "deploy/systemd/trading-desk-learning-mcp.service.example"
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


SERVICE_VALUES = {
    "__REVIEWED_EXECUTOR_USER__": "trading-executor",
    "__REVIEWED_EXECUTOR_GROUP__": "trading-executor",
    "__REVIEWED_REPO_DIR__": "/opt/trading-desk/executor",
    "__REVIEWED_VENV_BIN__": "/opt/trading-desk/executor/.venv/bin",
    "__REVIEWED_CONFIG_FILE__": "/etc/trading-desk/testnet-executor.toml",
    "__REVIEWED_EXECUTOR_STATE_DIR__": "/var/lib/trading-desk/testnet-executor",
    "__REVIEWED_LEARNING_STATE_DIR__": "/var/lib/trading-desk/learning",
    "__REVIEWED_LOG_DIR__": "/var/log/trading-desk/testnet-executor",
}
CONFIG_VALUES = {
    "__REVIEWED_TESTNET_ACCOUNT_ID__": "dedicated-testnet",
    "__REVIEWED_EXECUTOR_UID__": "451",
    "__REVIEWED_RESEARCH_UID__": "450",
    "__REVIEWED_CONTROL_UID__": "452",
    "__REVIEWED_MAIN_ACCOUNT_ADDRESS__": "0x" + "1" * 40,
    "__REVIEWED_API_WALLET_ADDRESS__": "0x" + "2" * 40,
    "__REVIEWED_INSTRUMENT__": "ETH-PERP",
    "__REVIEWED_ASSET_ID__": "1",
    "__REVIEWED_RECOVERY_CLOID__": "0x" + "e" * 32,
    "__REVIEWED_SIGNER_KEYCHAIN_SERVICE__": "testnet-signer",
    "__REVIEWED_SIGNER_KEYCHAIN_ACCOUNT__": "api-wallet",
    "__REVIEWED_APPROVAL_KEYCHAIN_SERVICE__": "testnet-approval",
    "__REVIEWED_APPROVAL_KEYCHAIN_ACCOUNT__": "approval-hmac",
    "__REVIEWED_RECOVERY_KEYCHAIN_SERVICE__": "testnet-recovery",
    "__REVIEWED_RECOVERY_KEYCHAIN_ACCOUNT__": "recovery-hmac",
    "__REVIEWED_GRANT_KEYCHAIN_SERVICE__": "testnet-grant",
    "__REVIEWED_GRANT_KEYCHAIN_ACCOUNT__": "grant-hmac",
    "__REVIEWED_EXECUTION_STATE_DIR__": "/var/lib/trading-desk/testnet-executor/execution",
    "__REVIEWED_NONCE_STATE_DIR__": "/var/lib/trading-desk/testnet-executor/nonce",
    "__REVIEWED_DAILY_LOSS_STATE_DIR__": "/var/lib/trading-desk/testnet-executor/daily-loss",
    "__REVIEWED_CONTROL_SOCKET_STATE_DIR__": "/var/lib/trading-desk/testnet-executor/socket",
    "__REVIEWED_LEARNING_STATE_DIR__": "/var/lib/trading-desk/learning",
}
LEARNING_VALUES = {
    "__REVIEWED_RESEARCH_USER__": "trading-research",
    "__REVIEWED_RESEARCH_GROUP__": "trading-research",
    "__REVIEWED_VENV_BIN__": "/opt/trading-desk/research/.venv/bin",
    "__REVIEWED_MCP_PORT__": "8765",
    "__REVIEWED_CONFIG_FILE__": "/etc/trading-desk/research-testnet.toml",
    "__REVIEWED_RESEARCH_DB__": "/var/lib/trading-desk/research/research.sqlite3",
    "__REVIEWED_GRANT_FILE__": "/var/lib/trading-desk/research/grant.json",
    "__REVIEWED_REPO_DIR__": "/opt/trading-desk/research",
    "__REVIEWED_LOG_DIR__": "/var/log/trading-desk/research",
    "__REVIEWED_RESEARCH_STATE_DIR__": "/var/lib/trading-desk/research",
    "__REVIEWED_LEARNING_STATE_DIR__": "/var/lib/trading-desk/learning",
    "__REVIEWED_EXECUTOR_STATE_DIR__": "/var/lib/trading-desk/testnet-executor",
}


def render(path: Path, values: dict[str, str]) -> str:
    result = path.read_text(encoding="utf-8")
    self_placeholders = set(PLACEHOLDER.findall(result))
    if not self_placeholders.issubset(values):
        raise ValueError("replacement set omits a template value")
    for key in self_placeholders:
        value = values[key]
        result = result.replace(key, value)
    if PLACEHOLDER.search(result):
        raise ValueError("unresolved placeholder")
    return result


class ExecutorDeploymentTemplateTests(unittest.TestCase):
    def test_templates_are_dedicated_testnet_services_without_shell_or_environment(self) -> None:
        launch = plistlib.loads(LAUNCHD.read_bytes())
        self.assertEqual(
            "com.jawndiego.trading-desk-testnet-executor", launch["Label"]
        )
        self.assertTrue(launch["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, launch["KeepAlive"])
        self.assertEqual(0o77, launch["Umask"])
        self.assertEqual(180, launch["ExitTimeOut"])
        self.assertNotIn("EnvironmentVariables", launch)
        self.assertNotIn("/bin/sh", " ".join(launch["ProgramArguments"]))
        self.assertIn("run", launch["ProgramArguments"])
        self.assertIn("--config", launch["ProgramArguments"])

        self.assertFalse(
            (ROOT / "deploy/systemd/trading-desk-testnet-executor.service.example").exists()
        )

    def test_rendered_service_commands_match_separate_executor_cli(self) -> None:
        parser = build_parser()
        launch = plistlib.loads(render(LAUNCHD, SERVICE_VALUES).encode())
        parsed = parser.parse_args(launch["ProgramArguments"][1:])
        self.assertEqual("run", parsed.command)
        self.assertEqual(
            Path("/etc/trading-desk/testnet-executor.toml"), parsed.config
        )
        self.assertEqual("isolated-testnet-worker", parsed.worker_id)


    def test_config_template_renders_to_strict_mainnet_impossible_profile(self) -> None:
        config = parse_executor_config(render(CONFIG, CONFIG_VALUES), environ={})
        self.assertEqual("testnet", config.environment.value)
        self.assertEqual(
            (451, 450, 452),
            (
                config.executor_uid,
                config.research_uid,
                config.control_uid,
            ),
        )
        self.assertEqual(RiskSizingPolicy().policy_hash, config.risk_policy_hash)
        self.assertEqual(("ETH-PERP",), config.allowed_instruments)
        self.assertEqual((1,), config.allowed_asset_ids)
        self.assertEqual(("0x" + "e" * 32,), config.recovery_cloids)
        private_parents = {
            config.paths.execution_database.parent,
            config.paths.nonce_database.parent,
            config.paths.daily_loss_database.parent,
            config.paths.control_socket.parent,
        }
        self.assertEqual(4, len(private_parents))
        self.assertEqual(
            config.paths.learning_database.parent,
            config.paths.staging_database.parent,
        )
        self.assertNotIn(
            "__REVIEWED_EXECUTOR_STATE_DIR__",
            CONFIG.read_text(encoding="utf-8"),
        )
        self.assertTrue(
            all(
                item.keychain_path == "/Library/Keychains/System.keychain"
                for item in (
                    config.credential,
                    config.approval_credential,
                    config.recovery_credential,
                    config.grant_credential,
                )
            )
        )
        self.assertEqual(
            4,
            len(
                {
                    (item.service, item.account)
                    for item in (
                        config.credential,
                        config.approval_credential,
                        config.recovery_credential,
                        config.grant_credential,
                    )
                }
            ),
        )

    def test_templates_contain_no_secret_values_or_mainnet_switch(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                LAUNCHD,
                LEARNING_LAUNCHD,
                LEARNING_SYSTEMD,
                CONFIG,
            )
        )
        for forbidden in (
            "private_key",
            "wallet_secret",
            "bearer_token",
            "--mainnet",
            "environment = \"mainnet\"",
            "/tmp/",
            "/Users/",
            "$HOME",
        ):
            self.assertNotIn(forbidden, combined.lower())

    def test_configured_learning_mcp_templates_are_loopback_non_execution_services(self) -> None:
        launch = plistlib.loads(render(LEARNING_LAUNCHD, LEARNING_VALUES).encode())
        arguments = launch["ProgramArguments"]
        self.assertEqual("127.0.0.1", arguments[arguments.index("--host") + 1])
        self.assertEqual("8765", arguments[arguments.index("--port") + 1])
        self.assertIn("--learning-grant", arguments)
        self.assertNotIn("trading-harness-executor", " ".join(arguments))
        self.assertNotIn("EnvironmentVariables", launch)

        systemd = render(LEARNING_SYSTEMD, LEARNING_VALUES)
        self.assertIn("--host 127.0.0.1 --port 8765", systemd)
        self.assertIn(
            "ReadOnlyPaths=/etc/trading-desk/research-testnet.toml "
            "/var/lib/trading-desk/research/grant.json",
            systemd,
        )
        self.assertNotIn("Environment=", systemd)
        self.assertNotIn("/bin/sh", systemd)
        self.assertIn(
            "InaccessiblePaths=/var/lib/trading-desk/testnet-executor",
            systemd,
        )


if __name__ == "__main__":
    unittest.main()
