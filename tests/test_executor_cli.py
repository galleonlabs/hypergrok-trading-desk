from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.executor_cli import (
    _acknowledge_halt,
    _issue_grant,
    build_parser,
    main,
)
from trading_harness.executor_config import load_executor_config
from trading_harness.executor_runtime_store import ManualHaltReason
from trading_harness.executor_service import (
    initialize_testnet_executor_state,
    open_testnet_executor_state,
)
from trading_harness.execution_grant import infrastructure_grant_confirmation
from trading_harness.grant_artifact import (
    load_signed_infrastructure_grant,
    verify_signed_infrastructure_grant,
)
from trading_harness.planning import RiskSizingPolicy
from tests.test_learning_quote_service import config_text
from tests.test_node import AT


SECRET = b"g" * 32


class FakeSecretProvider:
    def load_secret(self) -> bytes:
        return SECRET


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class ExecutorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.policy = RiskSizingPolicy()
        self.config = self.root / "executor.toml"
        self.config.write_text(
            config_text(self.root, self.policy.policy_hash), encoding="utf-8"
        )
        self.config.chmod(0o600)

    def test_command_surface_is_testnet_only_and_has_no_confirmation_argument(self) -> None:
        parser = build_parser()
        commands = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            {
                "validate",
                "init",
                "status",
                "dry-run",
                "show-stage",
                "authorize-stage",
                "acknowledge-halt",
                "issue-grant",
                "run",
            },
            set(commands.choices),
        )
        self.assertNotIn("--mainnet", parser.format_help().lower())
        authorize = commands.choices["authorize-stage"]
        self.assertNotIn("--confirmation", authorize.format_help())

    def test_validate_init_status_and_dry_run_need_no_credentials_or_network(self) -> None:
        validated = run_cli(["validate", "--config", str(self.config)])
        self.assertEqual(0, validated[0], validated[2])
        report = json.loads(validated[1])
        self.assertTrue(report["valid"])
        self.assertFalse(report["credential_loaded"])
        self.assertFalse(report["venue_write_attempted"])
        self.assertNotIn("0x111111", validated[1])

        initialized = run_cli(["init", "--config", str(self.config)])
        status = run_cli(["status", "--config", str(self.config)])
        dry = run_cli(["dry-run", "--config", str(self.config)])

        self.assertEqual(0, initialized[0], initialized[2])
        self.assertEqual(0, status[0], status[2])
        self.assertEqual(0, dry[0], dry[2])
        self.assertEqual("startup_reconcile", json.loads(dry[1])["step"])

    def test_issue_grant_requires_exact_direct_prompt_and_never_overwrites(self) -> None:
        output = self.root / "learning-grant.json"
        expected = infrastructure_grant_confirmation(
            grant_id="grant-one",
            generation=1,
            account_id="learning-account",
            allowed_instruments=("ETH-PERP",),
            risk_policy_hash=self.policy.policy_hash,
            max_loss="25",
            max_notional="1000",
            max_leverage="2",
            ttl_seconds=3_600,
        )
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch(
                "trading_harness.executor_cli._secret_provider",
                return_value=FakeSecretProvider(),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = _issue_grant(
                self.config,
                output,
                "grant-one",
                1,
                3600,
                prompt=lambda _message: expected,
                clock=lambda: AT,
            )
        self.assertEqual(0, result, stderr.getvalue())
        self.assertEqual(0, output.stat().st_mode & 0o077)
        parsed = load_signed_infrastructure_grant(output)
        trusted = verify_signed_infrastructure_grant(
            parsed,
            secret=SECRET,
            expected_issuer_id="learning-executor-grant-authority",
            expected_key_id="grant-hmac",
            expected_audience="learning-executor-learning-profile",
            at=AT,
        )
        self.assertEqual(parsed.grant_hash, trusted.grant_hash)
        original = output.read_bytes()

        with (
            patch(
                "trading_harness.executor_cli._secret_provider",
                return_value=FakeSecretProvider(),
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            repeated = _issue_grant(
                self.config,
                output,
                "grant-one",
                1,
                3600,
                prompt=lambda _message: expected,
                clock=lambda: AT,
            )
        self.assertEqual(2, repeated)
        self.assertEqual(original, output.read_bytes())

    def test_wrong_grant_prompt_fails_before_keychain_or_file_write(self) -> None:
        output = self.root / "must-not-exist.json"
        with (
            patch(
                "trading_harness.executor_cli._secret_provider",
                side_effect=AssertionError("must not load Keychain"),
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            result = _issue_grant(
                self.config,
                output,
                "grant-two",
                1,
                3600,
                prompt=lambda _message: "wrong",
                clock=lambda: AT,
            )
        self.assertEqual(2, result)
        self.assertFalse(output.exists())

    def test_attended_halt_acknowledgement_keeps_gate_halted(self) -> None:
        config = load_executor_config(self.config, environ={})
        state = initialize_testnet_executor_state(config, clock=lambda: AT)
        state.runtime_store.acquire(instance_id="failed-worker", lease_seconds=2)
        halted = state.runtime_store.engage_manual_halt(
            reason=ManualHaltReason.INTERNAL_ERROR
        )
        phrase = (
            f"ACKNOWLEDGE HALT {config.config_hash[:16]} "
            f"REVISION {halted.revision} REASON internal_error"
        )
        output = StringIO()
        error = StringIO()
        with (
            patch(
                "trading_harness.executor_cli.open_testnet_executor_state",
                side_effect=AssertionError(
                    "halt acknowledgement must not open unrelated executor state"
                ),
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            result = _acknowledge_halt(
                self.config,
                halted.revision,
                "internal_error",
                prompt=lambda _message: phrase,
            )

        self.assertEqual(0, result, error.getvalue())
        updated = open_testnet_executor_state(config).runtime_store.read()
        self.assertFalse(updated.manual_halt)
        self.assertEqual("halted", updated.effective_risk_gate.value)


if __name__ == "__main__":
    unittest.main()
