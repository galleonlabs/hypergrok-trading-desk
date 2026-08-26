from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from trading_harness.executor_config import (
    ExecutorConfigError,
    load_executor_config,
    parse_executor_config,
)


def config_text(*, root_extra: str = "", path_extra: str = "") -> str:
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "executor-alpha"
executor_uid = 451
research_uid = 450
control_uid = 452
account_id = "dedicated-testnet"
main_account_address = "0x1111111111111111111111111111111111111111"
api_wallet_address = "0x2222222222222222222222222222222222222222"
daily_loss_limit = "25.50"
max_reserved_loss = "5"
max_reserved_notional = "100"
max_leverage = "2"
risk_policy_hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
allowed_instruments = ["ETH-PERP"]
allowed_asset_ids = [1]
recovery_cloids = ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]
settlement_currency = "USDC"
poll_interval_ms = 1000
reconcile_interval_ms = 5000
{root_extra}
[credential]
provider = "macos_keychain_generic_password"
service = "com.example.testnet-signer"
account = "hyperliquid-api-wallet"
timeout_seconds = 5

[approval_credential]
provider = "macos_keychain_generic_password"
service = "com.example.testnet-approval"
account = "approval-hmac"
timeout_seconds = 5

[recovery_credential]
provider = "macos_keychain_generic_password"
service = "com.example.testnet-recovery"
account = "recovery-hmac"
timeout_seconds = 5

[grant_credential]
provider = "macos_keychain_generic_password"
service = "com.example.testnet-grant"
account = "grant-hmac"
timeout_seconds = 5

[paths]
execution_database = "/var/lib/trading-desk/execution/execution.sqlite3"
nonce_database = "/var/lib/trading-desk/nonce/nonce.sqlite3"
daily_loss_database = "/var/lib/trading-desk/daily-loss/daily-loss.sqlite3"
learning_database = "/var/lib/trading-desk/learning/learning.sqlite3"
staging_database = "/var/lib/trading-desk/learning/staging.sqlite3"
control_socket = "/var/run/trading-desk/socket/executor.sock"
{path_extra}
'''


class StrictExecutorConfigTests(unittest.TestCase):
    def test_valid_testnet_config_is_exact_and_canonically_hashed(self) -> None:
        first = parse_executor_config(config_text(), environ={})
        reordered = parse_executor_config(
            config_text()
            .replace(
                'node_id = "executor-alpha"\naccount_id = "dedicated-testnet"',
                'account_id = "dedicated-testnet"\nnode_id = "executor-alpha"',
            )
            .replace(
                'poll_interval_ms = 1000\nreconcile_interval_ms = 5000',
                'reconcile_interval_ms = 5000\npoll_interval_ms = 1000',
            ),
            environ={},
        )

        self.assertEqual(first.environment.value, "testnet")
        self.assertEqual(
            (451, 450, 452),
            (
                first.executor_uid,
                first.research_uid,
                first.control_uid,
            ),
        )
        self.assertEqual(str(first.daily_loss_limit), "25.50")
        self.assertEqual(str(first.max_reserved_loss), "5")
        self.assertEqual(first.allowed_instruments, ("ETH-PERP",))
        self.assertEqual(first.allowed_asset_ids, (1,))
        self.assertEqual(
            first.recovery_cloids,
            ("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",),
        )
        self.assertEqual(
            first.approval_credential.service,
            "com.example.testnet-approval",
        )
        self.assertEqual(
            first.recovery_credential.service,
            "com.example.testnet-recovery",
        )
        self.assertEqual(first.grant_credential.service, "com.example.testnet-grant")
        self.assertEqual(first.config_hash, reordered.config_hash)
        self.assertRegex(first.config_hash, r"^[0-9a-f]{64}$")

        changed = parse_executor_config(
            config_text().replace('daily_loss_limit = "25.50"', 'daily_loss_limit = "25.51"'),
            environ={},
        )
        self.assertNotEqual(first.config_hash, changed.config_hash)
        changed_uid = parse_executor_config(
            config_text().replace("control_uid = 452", "control_uid = 453"),
            environ={},
        )
        self.assertNotEqual(first.config_hash, changed_uid.config_hash)

    def test_schema_and_identity_uids_are_strict(self) -> None:
        cases = (
            config_text().replace("schema_version = 2", "schema_version = 1"),
            config_text().replace("schema_version = 2", "schema_version = true"),
            config_text().replace("executor_uid = 451", "executor_uid = 0"),
            config_text().replace("research_uid = 450", "research_uid = true"),
            config_text().replace("control_uid = 452", "control_uid = 451"),
            config_text().replace("control_uid = 452", "control_uid = 2147483648"),
        )
        for text in cases:
            with self.subTest(text=text[:180]):
                with self.assertRaises(ExecutorConfigError):
                    parse_executor_config(text, environ={})

    def test_unknown_root_nested_and_private_key_fields_are_rejected(self) -> None:
        cases = (
            config_text(root_extra="mystery = true"),
            config_text(path_extra='unexpected = "/tmp/nope"'),
            config_text(root_extra='private_key = "must-never-be-configured"'),
            config_text().replace(
                'timeout_seconds = 5',
                'timeout_seconds = 5\ncredential_file = "/tmp/key"',
            ),
        )
        for text in cases:
            with self.subTest(text=text[-100:]):
                with self.assertRaises(ExecutorConfigError):
                    parse_executor_config(text, environ={})

    def test_duplicate_keys_and_tables_are_rejected(self) -> None:
        duplicate_key = config_text(root_extra='environment = "testnet"')
        duplicate_table = config_text() + '\n[paths]\ncontrol_socket = "/tmp/x"\n'
        for text in (duplicate_key, duplicate_table):
            with self.subTest(text=text[-80:]):
                with self.assertRaisesRegex(ExecutorConfigError, "strict TOML"):
                    parse_executor_config(text, environ={})

    def test_floats_are_rejected_anywhere(self) -> None:
        cases = (
            config_text().replace("poll_interval_ms = 1000", "poll_interval_ms = 1000.0"),
            config_text().replace('daily_loss_limit = "25.50"', "daily_loss_limit = 25.50"),
            config_text(path_extra="unexpected_float = 1.5"),
        )
        for text in cases:
            with self.subTest(text=text[-100:]):
                with self.assertRaisesRegex(ExecutorConfigError, "floating-point"):
                    parse_executor_config(text, environ={})

    def test_mainnet_shadow_and_environment_overrides_are_rejected(self) -> None:
        for environment in ("mainnet", "shadow"):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ExecutorConfigError, "TESTNET"):
                    parse_executor_config(
                        config_text().replace(
                            'environment = "testnet"', f'environment = "{environment}"'
                        ),
                        environ={},
                    )

        override_sets = (
            {"TRADING_HARNESS_EXECUTOR_ENVIRONMENT": "testnet"},
            {"TRADING_HARNESS_ENVIRONMENT": "testnet"},
            {"HYPERLIQUID_MAINNET": "false"},
            {"HTTP_PROXY": "http://127.0.0.1:1"},
            {"HTTPS_PROXY": "http://127.0.0.1:1"},
            {"ALL_PROXY": "socks5://127.0.0.1:1"},
            {"NO_PROXY": "api.hyperliquid-testnet.xyz"},
            {"http_proxy": "http://127.0.0.1:1"},
            {"https_proxy": "http://127.0.0.1:1"},
            {"all_proxy": "socks5://127.0.0.1:1"},
            {"no_proxy": "api.hyperliquid-testnet.xyz"},
            {"SSL_CERT_FILE": "/unreviewed/ca.pem"},
            {"SSL_CERT_DIR": "/unreviewed/certs"},
            {"REQUESTS_CA_BUNDLE": "/unreviewed/requests.pem"},
            {"CURL_CA_BUNDLE": "/unreviewed/curl.pem"},
            {"SSLKEYLOGFILE": "/unreviewed/tls.keys"},
        )
        for environ in override_sets:
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(ExecutorConfigError, "overrides are forbidden"):
                    parse_executor_config(config_text(), environ=environ)

    def test_relative_non_normalized_root_and_overlapping_paths_are_rejected(self) -> None:
        cases = (
            config_text().replace(
                'execution_database = "/var/lib/trading-desk/execution/execution.sqlite3"',
                'execution_database = "state/execution.sqlite3"',
            ),
            config_text().replace(
                'execution_database = "/var/lib/trading-desk/execution/execution.sqlite3"',
                'execution_database = "/var/lib/trading-desk/../execution.sqlite3"',
            ),
            config_text().replace(
                'learning_database = "/var/lib/trading-desk/learning/learning.sqlite3"',
                'learning_database = "/var/lib/trading-desk/execution/execution.sqlite3"',
            ),
            config_text().replace(
                'control_socket = "/var/run/trading-desk/socket/executor.sock"',
                'control_socket = "/var/lib/trading-desk/execution/execution.sqlite3/socket"',
            ),
            config_text().replace(
                'control_socket = "/var/run/trading-desk/socket/executor.sock"',
                'control_socket = "/"',
            ),
        )
        for text in cases:
            with self.subTest(text=text[-120:]):
                with self.assertRaises(ExecutorConfigError):
                    parse_executor_config(text, environ={})

    def test_state_directory_classes_are_non_overlapping(self) -> None:
        same_private_parent = config_text().replace(
            'nonce_database = "/var/lib/trading-desk/nonce/nonce.sqlite3"',
            'nonce_database = "/var/lib/trading-desk/execution/nonce.sqlite3"',
        )
        with self.assertRaisesRegex(ExecutorConfigError, "managed paths overlap"):
            parse_executor_config(same_private_parent, environ={})

        split_learning_parent = config_text().replace(
            'staging_database = "/var/lib/trading-desk/learning/staging.sqlite3"',
            'staging_database = "/var/lib/trading-desk/staging/staging.sqlite3"',
        )
        with self.assertRaisesRegex(ExecutorConfigError, "share one learning-state"):
            parse_executor_config(split_learning_parent, environ={})

        learning_overlaps_execution = config_text().replace(
            'learning_database = "/var/lib/trading-desk/learning/learning.sqlite3"',
            'learning_database = "/var/lib/trading-desk/execution/learning.sqlite3"',
        ).replace(
            'staging_database = "/var/lib/trading-desk/learning/staging.sqlite3"',
            'staging_database = "/var/lib/trading-desk/execution/staging.sqlite3"',
        )
        with self.assertRaisesRegex(ExecutorConfigError, "managed paths overlap"):
            parse_executor_config(learning_overlaps_execution, environ={})

    def test_addresses_must_be_lowercase_and_api_wallet_must_be_distinct(self) -> None:
        uppercase = config_text().replace("0x111111", "0xAAAAAA", 1)
        identical = config_text().replace(
            "0x2222222222222222222222222222222222222222",
            "0x1111111111111111111111111111111111111111",
        )
        for text in (uppercase, identical):
            with self.assertRaises(ExecutorConfigError):
                parse_executor_config(text, environ={})

    def test_asset_scope_and_caps_are_strict(self) -> None:
        cases = (
            config_text().replace('allowed_asset_ids = [1]', 'allowed_asset_ids = [1, 2]'),
            config_text().replace('allowed_instruments = ["ETH-PERP"]', 'allowed_instruments = []'),
            config_text().replace('max_leverage = "2"', 'max_leverage = "2.1"'),
            config_text().replace('max_reserved_loss = "5"', 'max_reserved_loss = "26"'),
            config_text().replace(
                'recovery_cloids = ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]',
                'recovery_cloids = []',
            ),
            config_text().replace(
                'recovery_cloids = ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]',
                'recovery_cloids = ["0xEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"]',
            ),
            config_text().replace("a" * 64, "bad-policy-hash", 1),
        )
        for text in cases:
            with self.subTest(text=text[:160]):
                with self.assertRaises(ExecutorConfigError):
                    parse_executor_config(text, environ={})

    def test_every_keychain_authority_requires_a_distinct_item(self) -> None:
        duplicated = config_text().replace(
            'service = "com.example.testnet-approval"\naccount = "approval-hmac"',
            'service = "com.example.testnet-signer"\naccount = "hyperliquid-api-wallet"',
        )
        with self.assertRaisesRegex(ExecutorConfigError, "must be distinct"):
            parse_executor_config(duplicated, environ={})

    def test_loader_requires_absolute_regular_non_symlink_utf8_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "executor.toml"
            config.write_text(config_text(), encoding="utf-8")
            config.chmod(0o600)
            loaded = load_executor_config(config.absolute(), environ={})
            self.assertEqual(loaded.node_id, "executor-alpha")

            with self.assertRaisesRegex(ExecutorConfigError, "must be absolute"):
                load_executor_config(Path("executor.toml"), environ={})

            link = root / "link.toml"
            link.symlink_to(config)
            with self.assertRaisesRegex(ExecutorConfigError, "non-symlink"):
                load_executor_config(link.absolute(), environ={})

            malformed = root / "malformed.toml"
            malformed.write_bytes(b"\xff")
            malformed.chmod(0o600)
            with self.assertRaisesRegex(ExecutorConfigError, "UTF-8"):
                load_executor_config(malformed.absolute(), environ={})

            exposed = root / "exposed.toml"
            exposed.write_text(config_text(), encoding="utf-8")
            exposed.chmod(0o644)
            with self.assertRaisesRegex(ExecutorConfigError, "group/world"):
                load_executor_config(exposed.absolute(), environ={})


if __name__ == "__main__":
    unittest.main()
