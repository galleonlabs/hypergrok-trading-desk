from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.executor_cli import (
    _acknowledge_halt,
    _issue_grant,
    _require_state_file,
    build_parser,
    main,
)
from trading_harness.executor_config import load_executor_config
from trading_harness.errors import StateConflict
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

    def _state_file(self) -> tuple[object, Path]:
        config = load_executor_config(self.config, environ={})
        path = config.paths.execution_database
        path.write_bytes(b"state")
        path.chmod(0o600)
        return config, path

    @staticmethod
    def _current_umask() -> int:
        current = os.umask(0o777)
        os.umask(current)
        return current

    def test_main_forces_private_umask_during_dispatch_and_restores_it(self) -> None:
        original = os.umask(0o027)
        self.addCleanup(os.umask, original)
        observed: list[int] = []

        def dispatch(_arguments) -> int:
            observed.append(self._current_umask())
            return 23

        with patch("trading_harness.executor_cli._dispatch", side_effect=dispatch):
            result = main(["validate", "--config", str(self.config)])

        self.assertEqual(23, result)
        self.assertEqual([0o077], observed)
        self.assertEqual(0o027, self._current_umask())

    def test_main_restores_umask_when_dispatch_raises(self) -> None:
        original = os.umask(0o022)
        self.addCleanup(os.umask, original)

        with (
            patch(
                "trading_harness.executor_cli._dispatch",
                side_effect=RuntimeError("dispatch failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "dispatch failed"),
        ):
            main(["validate", "--config", str(self.config)])

        self.assertEqual(0o022, self._current_umask())

    def test_control_command_requires_configured_control_uid_before_dispatch(self) -> None:
        output = self.root / "must-not-exist.json"
        with patch(
            "trading_harness.executor_cli._dispatch",
            side_effect=AssertionError("wrong identity must not dispatch"),
        ):
            result, _stdout, stderr = run_cli(
                [
                    "issue-grant",
                    "--config",
                    str(self.config),
                    "--output",
                    str(output),
                    "--grant-id",
                    "identity-test",
                ]
            )

        self.assertEqual(2, result)
        self.assertIn("configured control UID", stderr)
        self.assertFalse(output.exists())

    def test_executor_command_requires_configured_executor_uid_before_dispatch(self) -> None:
        mismatched = self.root / "mismatched-executor.toml"
        mismatched.write_text(
            config_text(self.root, self.policy.policy_hash).replace(
                f"executor_uid = {os.geteuid()}",
                f"executor_uid = {os.geteuid() + 3}",
            ),
            encoding="utf-8",
        )
        mismatched.chmod(0o600)
        with patch(
            "trading_harness.executor_cli._dispatch",
            side_effect=AssertionError("wrong identity must not dispatch"),
        ):
            result, _stdout, stderr = run_cli(
                ["init", "--config", str(mismatched)]
            )

        self.assertEqual(2, result)
        self.assertIn("configured executor UID", stderr)

    def test_state_file_precheck_rejects_symlink_and_nonregular_path(self) -> None:
        config, path = self._state_file()
        target = path.with_name("target.sqlite3")
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(StateConflict, "state must be initialized"):
            _require_state_file(config, path, label="test")
        path.unlink()
        path.mkdir(mode=0o700)
        with self.assertRaisesRegex(StateConflict, "state must be initialized"):
            _require_state_file(config, path, label="test")

    def test_state_file_precheck_rejects_hardlink(self) -> None:
        config, path = self._state_file()
        hardlink = self.root / "state-hardlink.sqlite3"
        os.link(path, hardlink)

        with self.assertRaisesRegex(StateConflict, "state must be initialized"):
            _require_state_file(config, path, label="test")

    def test_state_file_precheck_requires_exact_mode_0600(self) -> None:
        config, path = self._state_file()

        for mode in (0o400, 0o700, 0o640):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with self.assertRaisesRegex(
                    StateConflict, "state must be initialized"
                ):
                    _require_state_file(config, path, label="test")

    def test_state_file_precheck_rejects_insecure_parent_and_sidecar(self) -> None:
        config, path = self._state_file()
        path.parent.chmod(0o777)
        try:
            with self.assertRaisesRegex(StateConflict, "state must be initialized"):
                _require_state_file(config, path, label="test")
        finally:
            path.parent.chmod(0o700)

        sidecar = Path(str(path) + "-wal")
        sidecar.write_bytes(b"wal")
        sidecar.chmod(0o644)
        with self.assertRaisesRegex(StateConflict, "state must be initialized"):
            _require_state_file(config, path, label="test")

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
        status_report = json.loads(status[1])
        dry_report = json.loads(dry[1])
        self.assertTrue(status_report["shared_learning_available"])
        self.assertFalse(status_report["entry_blocked_by_shared_learning"])
        self.assertEqual("startup_reconcile", dry_report["step"])
        self.assertTrue(dry_report["shared_learning_available"])
        self.assertFalse(dry_report["entry_blocked_by_shared_learning"])

    def test_status_surfaces_recovery_capable_shared_learning_degradation(self) -> None:
        initialized = run_cli(["init", "--config", str(self.config)])
        self.assertEqual(0, initialized[0], initialized[2])
        config = load_executor_config(self.config, environ={})
        with closing(
            sqlite3.connect(config.paths.learning_database)
        ) as connection, connection:
            connection.execute("DROP TRIGGER learning_ledger_no_delete")

        status = run_cli(["status", "--config", str(self.config)])
        dry = run_cli(["dry-run", "--config", str(self.config)])

        self.assertEqual(0, status[0], status[2])
        self.assertEqual(0, dry[0], dry[2])
        for report in (json.loads(status[1]), json.loads(dry[1])):
            self.assertFalse(report["shared_learning_available"])
            self.assertTrue(report["entry_blocked_by_shared_learning"])

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
