from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import inspect
import json
import unittest
from unittest.mock import patch

from trading_harness.errors import StorageError
from trading_harness.execution_store import (
    CommandRecord,
    ExecutionStore,
    IncidentRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryCommand,
    RecoveryOutbox,
)
from trading_harness.execution_work_scanner import (
    REQUIRED_EXECUTION_STORE_METHODS,
    ExecutionWorkKind,
    ExecutionWorkScanner,
)
from trading_harness import execution_work_scanner


NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def command(state: str = "queued") -> CommandRecord:
    return CommandRecord(
        command_id="raw-command-secret",
        ticket_hash=HASH,
        plan_hash=HASH,
        approval_id="approval-secret",
        state=state,
        reserved_loss=Decimal("1"),
        reserved_notional=Decimal("10"),
        created_at=NOW,
        updated_at=NOW,
        terminal_at=None,
        revision=1,
    )


def outbox(state: str = "queued") -> OutboxRecord:
    return OutboxRecord(
        command_id="raw-command-secret",
        state=state,
        worker_id=None,
        fencing_token=0,
        claimed_at=None,
        lease_expires_at=None,
        current_attempt_id=None,
        attempt_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def recovery(state: str = "submitted_unknown") -> RecoveryCommand:
    return RecoveryCommand(
        recovery_command_id="raw-recovery-secret",
        permit_id="permit-secret",
        parent_command_id="raw-command-secret",
        incident_id="raw-incident-secret",
        kind="noop_fence",
        priority=0,
        source_hash=HASH,
        preflight_hash=HASH,
        recovery_hash=HASH,
        recovery_material_json="{}",
        recovery_material_hash=HASH,
        safety_policy_hash=HASH,
        original_attempt_id="attempt-secret",
        original_nonce=1,
        state=state,
        created_at=NOW,
        updated_at=NOW,
        terminal_at=None,
        revision=1,
    )


def recovery_outbox(state: str = "submitted_unknown") -> RecoveryOutbox:
    return RecoveryOutbox(
        recovery_command_id="raw-recovery-secret",
        state=state,
        worker_id=None,
        fencing_token=0,
        claimed_at=None,
        lease_expires_at=None,
        current_attempt_id=None,
        attempt_count=1,
        created_at=NOW,
        updated_at=NOW,
    )


def records() -> dict[str, tuple[object, ...]]:
    return {
        "list_commands": (command(),),
        "list_outboxes": (outbox(),),
        "list_recovery_commands": (recovery(),),
        "list_recovery_outboxes": (recovery_outbox(),),
        "list_positions": (
            PositionRecord("ETH-PERP-secret", Decimal("0.25"), HASH, NOW, 1),
        ),
        "list_protections": (
            ProtectionRecord(
                "raw-command-secret",
                "ETH-PERP-secret",
                "under_protected",
                Decimal("0.25"),
                Decimal("0.1"),
                "stop-secret",
                NOW,
                1,
            ),
        ),
        "list_incidents": (
            IncidentRecord(
                "raw-incident-secret",
                "raw-command-secret",
                "protection_gap",
                "critical",
                "open",
                NOW,
                NOW,
                1,
                {"raw_detail": "must-not-leak"},
            ),
        ),
    }


class ExecutionWorkScannerTests(unittest.TestCase):
    def test_missing_public_methods_report_exact_contract_without_partial_reads(self) -> None:
        store = object.__new__(ExecutionStore)
        with patch.object(ExecutionStore, "list_outboxes", new=None, create=True):
            expected_missing = {
                item.name
                for item in REQUIRED_EXECUTION_STORE_METHODS
                if not callable(getattr(ExecutionStore, item.name, None))
            }
            with patch.object(
                ExecutionStore,
                "list_incidents",
                side_effect=AssertionError("partial list API must not run"),
            ):
                scan = ExecutionWorkScanner(store, clock=lambda: NOW).scan()
        self.assertFalse(scan.compatible)
        self.assertEqual(scan.items, ())
        self.assertEqual(scan.command_count, 0)
        missing = {item.name for item in scan.missing_methods}
        self.assertEqual(missing, expected_missing)
        self.assertIn("list_outboxes", missing)
        self.assertTrue(
            all(
                item.contract.startswith("() -> tuple[")
                for item in scan.missing_methods
            )
        )

    def test_exact_store_required_not_subclass_or_duck_adapter(self) -> None:
        class StoreSubclass(ExecutionStore):
            pass

        for candidate in (object(), object.__new__(StoreSubclass)):
            with self.subTest(candidate=type(candidate).__name__):
                with self.assertRaisesRegex(TypeError, "exact ExecutionStore"):
                    ExecutionWorkScanner(candidate)  # type: ignore[arg-type]

    def test_complete_public_contract_returns_prioritized_redacted_work(self) -> None:
        store = object.__new__(ExecutionStore)
        supplied = records()
        calls: list[str] = []

        def method(name: str):
            def read(_self):
                calls.append(name)
                return supplied[name]

            return read

        with ExitStack() as stack:
            for requirement in REQUIRED_EXECUTION_STORE_METHODS:
                stack.enter_context(
                    patch.object(
                        ExecutionStore,
                        requirement.name,
                        new=method(requirement.name),
                        create=True,
                    )
                )
            scan = ExecutionWorkScanner(store, clock=lambda: NOW).scan()

        self.assertTrue(scan.compatible)
        self.assertEqual(scan.missing_methods, ())
        self.assertEqual(calls, [item.name for item in REQUIRED_EXECUTION_STORE_METHODS])
        self.assertEqual(scan.command_count, 1)
        self.assertEqual(scan.recovery_count, 1)
        self.assertEqual(scan.position_count, 1)
        self.assertEqual(scan.protection_gap_count, 1)
        self.assertEqual(scan.open_incident_count, 1)
        kinds = {item.kind for item in scan.items}
        self.assertEqual(
            kinds,
            {
                ExecutionWorkKind.COMMAND_DISPATCH,
                ExecutionWorkKind.RECOVERY_RECONCILE,
                ExecutionWorkKind.OPEN_POSITION,
                ExecutionWorkKind.PROTECTION_GAP,
                ExecutionWorkKind.OPEN_INCIDENT,
            },
        )
        self.assertEqual(scan.items[0].priority, 0)
        self.assertRegex(scan.scan_hash, r"^[0-9a-f]{64}$")

        encoded = json.dumps(scan.as_dict(), sort_keys=True)
        represented = repr(scan)
        for secret in (
            "raw-command-secret",
            "raw-recovery-secret",
            "raw-incident-secret",
            "ETH-PERP-secret",
            "approval-secret",
            "stop-secret",
            "must-not-leak",
        ):
            self.assertNotIn(secret, encoded)
            self.assertNotIn(secret, represented)

    def test_mismatched_or_unverified_public_collections_fail_closed(self) -> None:
        store = object.__new__(ExecutionStore)
        supplied = records()
        supplied["list_outboxes"] = (outbox("claimed"),)

        def method(name: str):
            return lambda _self: supplied[name]

        with ExitStack() as stack:
            for requirement in REQUIRED_EXECUTION_STORE_METHODS:
                stack.enter_context(
                    patch.object(
                        ExecutionStore,
                        requirement.name,
                        new=method(requirement.name),
                        create=True,
                    )
                )
            with self.assertRaisesRegex(StorageError, "public state is invalid"):
                ExecutionWorkScanner(store, clock=lambda: NOW).scan()

    def test_expired_claims_are_not_reported_as_active_work(self) -> None:
        store = object.__new__(ExecutionStore)
        supplied = records()
        supplied["list_commands"] = (command("claimed"),)
        supplied["list_outboxes"] = (
            replace(
                outbox("claimed"),
                worker_id="worker",
                fencing_token=1,
                claimed_at=NOW,
                lease_expires_at=NOW,
                current_attempt_id="attempt",
                attempt_count=1,
            ),
        )
        supplied["list_recovery_commands"] = (recovery("signing"),)
        supplied["list_recovery_outboxes"] = (
            replace(
                recovery_outbox("signing"),
                worker_id="worker",
                fencing_token=1,
                claimed_at=NOW,
                lease_expires_at=NOW,
                current_attempt_id=None,
                attempt_count=0,
            ),
        )

        def method(name: str):
            return lambda _self: supplied[name]

        with ExitStack() as stack:
            for requirement in REQUIRED_EXECUTION_STORE_METHODS:
                stack.enter_context(
                    patch.object(
                        ExecutionStore,
                        requirement.name,
                        new=method(requirement.name),
                        create=True,
                    )
                )
            scan = ExecutionWorkScanner(store, clock=lambda: NOW).scan()
        by_kind = {item.kind: item for item in scan.items}
        self.assertIn(ExecutionWorkKind.COMMAND_RECONCILE, by_kind)
        self.assertIn(ExecutionWorkKind.RECOVERY_DISPATCH, by_kind)
        self.assertNotIn(ExecutionWorkKind.COMMAND_IN_FLIGHT, by_kind)
        self.assertNotIn(ExecutionWorkKind.RECOVERY_IN_FLIGHT, by_kind)

    def test_scanner_source_has_no_database_or_mutating_store_access(self) -> None:
        source = inspect.getsource(execution_work_scanner.ExecutionWorkScanner)
        for forbidden in (
            "sqlite3",
            "_connect",
            "_transaction",
            "_database",
            ".path",
            "claim_next(",
            "claim_next_recovery(",
            "execute(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
