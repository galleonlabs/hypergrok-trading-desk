from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trading_harness.admission import AdmissionRequest, AdmissionService
from trading_harness.canonical import semantic_intent_hash
from trading_harness.domain import (
    Authorization,
    AuthorizationModel,
    DeploymentGrant,
    Environment,
    GrantState,
    GrantType,
    OrderType,
    SemanticIntent,
    Side,
)
from trading_harness.errors import (
    AdmissionDenied,
    PolicyViolation,
    RecordNotFound,
    StateConflict,
    ValidationError,
)
from trading_harness.policy import ExposureQuote, RiskPolicy, derive_exposure_quote
from trading_harness.store import SQLiteStore


NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)


def intent(sequence: int = 1, **changes: object) -> SemanticIntent:
    values: dict[str, object] = {
        "intent_id": f"intent-{sequence}",
        "thesis_id": "sma-cross",
        "thesis_version": "1",
        "strategy_version": "compiler-1",
        "code_hash": "a" * 64,
        "venue": "hyperliquid",
        "account_id": "testnet-canary",
        "environment": Environment.TESTNET,
        "instrument": "ETH-PERP",
        "action": "simulate_order",
        "side": Side.BUY,
        "quantity": Decimal("1"),
        "order_type": OrderType.LIMIT,
        "expires_at": NOW + timedelta(minutes=5),
        "client_order_id": f"client-{sequence}",
        "limit_price": Decimal("3000"),
        "price_bound": Decimal("3020"),
        "stop_price": Decimal("2900"),
        "leverage": Decimal("2"),
        "max_slippage_bps": Decimal("50"),
        "fee_bps": Decimal("4"),
        "signal_instance_hash": f"{sequence:064x}",
    }
    values.update(changes)
    return SemanticIntent(**values)  # type: ignore[arg-type]


def grant(**changes: object) -> DeploymentGrant:
    values: dict[str, object] = {
        "grant_id": "grant-1",
        "thesis_id": "sma-cross",
        "thesis_version": "1",
        "strategy_version": "compiler-1",
        "code_hash": "a" * 64,
        "venue": "hyperliquid",
        "account_id": "testnet-canary",
        "environment": Environment.TESTNET,
        "grant_type": GrantType.INFRASTRUCTURE_TESTNET,
        "issued_at": NOW - timedelta(minutes=10),
        "starts_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(days=1),
        "authorization_model": AuthorizationModel.INFRASTRUCTURE,
        "state": GrantState.ACTIVE,
        "allowed_instruments": ("ETH-PERP",),
        "allowed_actions": ("simulate_order",),
        "max_notional": Decimal("10000"),
        "max_loss": Decimal("5000"),
    }
    values.update(changes)
    return DeploymentGrant(**values)  # type: ignore[arg-type]


def policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "testnet-policy",
        "version": "1",
        "max_order_quantity": Decimal("10"),
        "max_order_notional": Decimal("10000"),
        "max_order_worst_case_loss": Decimal("5000"),
        "max_account_gross_notional": Decimal("15000"),
        "max_account_worst_case_loss": Decimal("7500"),
        "max_leverage": Decimal("3"),
        "max_slippage_bps": Decimal("100"),
        "max_fee_bps": Decimal("20"),
        "allowed_instruments": ("ETH-PERP",),
        "allowed_actions": ("simulate_order",),
        "allowed_order_types": ("limit",),
    }
    values.update(changes)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def authorization(
    target: SemanticIntent,
    sequence: int = 1,
    **changes: object,
) -> Authorization:
    values: dict[str, object] = {
        "authorization_id": f"auth-{sequence}",
        "intent_hash": semantic_intent_hash(target),
        "grant_id": "grant-1",
        "account_id": target.account_id,
        "environment": target.environment,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=2),
        "audience": "admission.test",
    }
    values.update(changes)
    return Authorization(**values)  # type: ignore[arg-type]


def exposure(target: SemanticIntent, **changes: object) -> ExposureQuote:
    derived = derive_exposure_quote(target)
    values: dict[str, object] = {
        "intent_hash": derived.intent_hash,
        "quantity": derived.quantity,
        "notional": derived.notional,
        "worst_case_loss": derived.worst_case_loss,
        "slippage_bps": derived.slippage_bps,
        "fee_bps": derived.fee_bps,
    }
    values.update(changes)
    return ExposureQuote(**values)  # type: ignore[arg-type]


class AdmissionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteStore(Path(self.temporary.name) / "harness.sqlite3")
        self.store.register_deployment_grant(grant(), policy())
        self.service = AdmissionService(self.store, audience="admission.test")

    def test_store_uses_wal_and_enforces_foreign_keys(self) -> None:
        connection = sqlite3.connect(Path(self.temporary.name) / "harness.sqlite3")
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            connection.close()

        orphan = authorization(intent(), grant_id="missing-grant")
        with self.assertRaises(StateConflict):
            self.store.register_authorization(orphan)

    def register_request(
        self,
        target: SemanticIntent,
        sequence: int = 1,
        **quote_changes: object,
    ) -> AdmissionRequest:
        auth = authorization(target, sequence)
        self.store.register_authorization(auth)
        return AdmissionRequest(
            intent=target,
            exposure=exposure(target, **quote_changes),
            authorization_id=auth.authorization_id,
            command_id=f"command-{sequence}",
            audience="admission.test",
            authorization_model=AuthorizationModel.INFRASTRUCTURE,
        )

    def test_admission_is_one_atomic_commit(self) -> None:
        request = self.register_request(intent())

        command = self.service.admit(request, now=NOW)

        self.assertEqual(command.state, "queued")
        self.assertEqual(command.reserved_notional, Decimal("3020"))
        self.assertEqual(self.store.authorization_state("auth-1"), "consuming")
        self.assertEqual(self.store.get_outbox("command-1").state, "pending")
        self.assertEqual(
            self.store.get_exposure("testnet-canary", Environment.TESTNET).reserved_loss,
            Decimal("3036.308"),
        )

    def test_single_use_authorization_cannot_admit_twice(self) -> None:
        request = self.register_request(intent())
        self.service.admit(request, now=NOW)

        with self.assertRaisesRegex(AdmissionDenied, "AUTHORIZATION_ALREADY_USED"):
            self.service.admit(replace(request, command_id="command-replay"), now=NOW)

        self.assertEqual(
            self.store.get_exposure("testnet-canary", Environment.TESTNET).reserved_notional,
            Decimal("3020"),
        )

    def test_policy_denial_rolls_back_authorization_and_outbox(self) -> None:
        request = self.register_request(intent(quantity=Decimal("4")))

        with self.assertRaises(PolicyViolation):
            self.service.admit(request, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-1"), "issued")
        self.assertEqual(
            self.store.get_exposure("testnet-canary", Environment.TESTNET).reserved_notional,
            Decimal("0"),
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_outbox("command-1")

    def test_expired_authorization_does_not_create_state(self) -> None:
        target = intent()
        expired = authorization(
            target,
            issued_at=NOW - timedelta(minutes=2),
            expires_at=NOW - timedelta(seconds=1),
        )
        self.store.register_authorization(expired)
        request = AdmissionRequest(
            intent=target,
            exposure=exposure(target),
            authorization_id=expired.authorization_id,
            command_id="command-expired",
            audience="admission.test",
            authorization_model=AuthorizationModel.INFRASTRUCTURE,
        )

        with self.assertRaisesRegex(AdmissionDenied, "AUTHORIZATION_INACTIVE"):
            self.service.admit(request, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-1"), "issued")

    def test_caller_cannot_understate_derived_economics(self) -> None:
        target = intent()
        request = self.register_request(
            target,
            notional=Decimal("1"),
            worst_case_loss=Decimal("0"),
        )

        with self.assertRaisesRegex(
            AdmissionDenied, "RISK_QUOTE_ECONOMICS_MISMATCH"
        ):
            self.service.admit(request, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-1"), "issued")

    def test_stop_trigger_alone_does_not_reduce_worst_case_loss(self) -> None:
        unprotected = derive_exposure_quote(intent(stop_price=Decimal("2900")))
        protected = derive_exposure_quote(
            intent(
                stop_price=Decimal("2900"),
                protection_limit_price=Decimal("2890"),
            )
        )

        self.assertEqual(unprotected.notional, Decimal("3020"))
        self.assertEqual(unprotected.worst_case_loss, Decimal("3036.308"))
        self.assertEqual(protected.worst_case_loss, Decimal("146.308"))

    def test_revoked_grant_denies_without_consuming_authorization(self) -> None:
        request = self.register_request(intent())
        self.store.revoke_grant("grant-1", now=NOW)

        with self.assertRaisesRegex(AdmissionDenied, "GRANT_INACTIVE"):
            self.service.admit(request, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-1"), "issued")

    def test_revocation_atomically_makes_queued_outbox_nondispatchable(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)

        self.store.revoke_grant("grant-1", now=NOW + timedelta(seconds=1))

        outbox = self.store.get_outbox("command-1")
        self.assertEqual(outbox.state, "revoked")
        self.assertFalse(outbox.dispatchable)
        self.assertEqual(
            self.store.get_exposure(
                "testnet-canary", Environment.TESTNET
            ).reserved_notional,
            Decimal("3020"),
        )

    def test_authorization_revocation_blocks_outbox_without_releasing_risk(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)

        self.store.revoke_authorization(
            "auth-1", now=NOW + timedelta(seconds=1)
        )

        self.assertEqual(self.store.authorization_state("auth-1"), "revoked")
        self.assertFalse(self.store.get_outbox("command-1").dispatchable)
        self.assertEqual(self.store.get_outbox("command-1").state, "revoked")
        self.assertEqual(
            self.store.get_exposure(
                "testnet-canary", Environment.TESTNET
            ).reserved_notional,
            Decimal("3020"),
        )

    def test_unknown_outcome_cannot_release_without_reconciliation_evidence(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)
        self.store.mark_unknown("command-1", now=NOW + timedelta(seconds=1))

        with self.assertRaisesRegex(StateConflict, "reconciliation-specific evidence"):
            self.store.mark_terminal(
                "command-1",
                state="canceled",
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual(
            self.store.get_exposure(
                "testnet-canary", Environment.TESTNET
            ).reserved_notional,
            Decimal("3020"),
        )

    def test_duplicate_client_order_id_rolls_back_second_authorization(self) -> None:
        first = self.register_request(intent(1), 1)
        self.service.admit(first, now=NOW)
        second_intent = intent(2, client_order_id="client-1")
        second = self.register_request(second_intent, 2)

        with self.assertRaises(StateConflict):
            self.service.admit(second, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-2"), "issued")

    def test_signal_instance_hash_is_unique_per_account_environment(self) -> None:
        first = self.register_request(intent(1), 1)
        self.service.admit(first, now=NOW)
        second_intent = intent(2, signal_instance_hash=intent(1).signal_instance_hash)
        second = self.register_request(second_intent, 2)

        with self.assertRaises(StateConflict):
            self.service.admit(second, now=NOW)

        self.assertEqual(self.store.authorization_state("auth-2"), "issued")

    def test_missing_bound_or_leverage_denies_before_authorization_changes(self) -> None:
        for sequence, change in (
            (11, {"price_bound": None}),
            (12, {"leverage": None}),
        ):
            with self.subTest(change=change):
                target = intent(sequence, **change)
                auth = authorization(target, sequence)
                self.store.register_authorization(auth)
                request = AdmissionRequest(
                    intent=target,
                    exposure=ExposureQuote(
                        intent_hash=semantic_intent_hash(target),
                        quantity=target.quantity,
                        notional=Decimal("1"),
                        worst_case_loss=Decimal("1"),
                    ),
                    authorization_id=auth.authorization_id,
                    command_id=f"command-{sequence}",
                    audience="admission.test",
                    authorization_model=AuthorizationModel.INFRASTRUCTURE,
                )

                with self.assertRaisesRegex(AdmissionDenied, "RISK_ECONOMICS_INVALID"):
                    self.service.admit(request, now=NOW)
                self.assertEqual(self.store.authorization_state(auth.authorization_id), "issued")

    def test_platform_action_denial_precedes_all_persistent_changes(self) -> None:
        target = intent(20, action="withdraw")
        auth = authorization(target, 20)
        self.store.register_authorization(auth)
        request = AdmissionRequest(
            intent=target,
            exposure=exposure(target),
            authorization_id=auth.authorization_id,
            command_id="command-20",
            audience="admission.test",
            authorization_model=AuthorizationModel.INFRASTRUCTURE,
        )

        with self.assertRaisesRegex(AdmissionDenied, "PLATFORM_ACTION_NOT_ALLOWED"):
            self.service.admit(request, now=NOW)

        self.assertEqual(self.store.authorization_state(auth.authorization_id), "issued")
        with self.assertRaises(RecordNotFound):
            self.store.get_outbox("command-20")

    def test_ambient_decimal_precision_cannot_change_admission_economics(self) -> None:
        target = intent()
        request = self.register_request(target)

        with localcontext() as ambient:
            ambient.prec = 2
            command = self.service.admit(request, now=NOW)

        self.assertEqual(command.original_notional, Decimal("3020"))
        self.assertEqual(command.original_loss, Decimal("3036.308"))


class FoundationGrantBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteStore(Path(self.temporary.name) / "harness.sqlite3")
        self.store.register_deployment_grant(grant(), policy())
        self.service = AdmissionService(self.store, audience="admission.test")

    def register_request(
        self,
        target: SemanticIntent,
        sequence: int = 1,
    ) -> AdmissionRequest:
        auth = authorization(target, sequence)
        self.store.register_authorization(auth)
        return AdmissionRequest(
            intent=target,
            exposure=exposure(target),
            authorization_id=auth.authorization_id,
            command_id=f"command-{sequence}",
            audience="admission.test",
            authorization_model=AuthorizationModel.INFRASTRUCTURE,
        )

    def test_only_infrastructure_testnet_grants_can_be_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "grant-boundary.sqlite3")
            unsupported = (
                (
                    GrantType.STRATEGY_TESTNET,
                    Environment.TESTNET,
                    AuthorizationModel.PER_TICKET_HUMAN,
                ),
                (
                    GrantType.MANUAL_MAINNET_CANARY,
                    Environment.MAINNET,
                    AuthorizationModel.PER_TICKET_HUMAN,
                ),
                (
                    GrantType.SYSTEMATIC_TESTNET,
                    Environment.TESTNET,
                    AuthorizationModel.SYSTEMATIC_POLICY,
                ),
                (
                    GrantType.SYSTEMATIC_SHADOW,
                    Environment.SHADOW,
                    AuthorizationModel.SYSTEMATIC_POLICY,
                ),
                (
                    GrantType.SYSTEMATIC_MAINNET_CAPPED,
                    Environment.MAINNET,
                    AuthorizationModel.SYSTEMATIC_POLICY,
                ),
            )
            for sequence, (grant_type, environment, model) in enumerate(
                unsupported, start=1
            ):
                with self.subTest(grant_type=grant_type):
                    candidate = grant(
                        grant_id=f"unsupported-{sequence}",
                        grant_type=grant_type,
                        environment=environment,
                        authorization_model=model,
                    )
                    with self.assertRaisesRegex(
                        ValidationError, "foundation persists only"
                    ):
                        store.register_deployment_grant(candidate, policy())

            connection = sqlite3.connect(Path(directory) / "grant-boundary.sqlite3")
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM deployment_grants"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)

    def test_unknown_outcome_keeps_full_reservation(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)

        command = self.store.mark_unknown("command-1", now=NOW + timedelta(seconds=1))
        account = self.store.get_exposure("testnet-canary", Environment.TESTNET)

        self.assertEqual(command.state, "submitted_unknown")
        self.assertEqual(command.reserved_notional, Decimal("3020"))
        self.assertEqual(account.reserved_notional, Decimal("3020"))
        self.assertEqual(self.store.authorization_state("auth-1"), "consumed")
        self.assertEqual(self.store.get_outbox("command-1").state, "blocked_unknown")
        self.assertFalse(self.store.get_outbox("command-1").dispatchable)

    def test_partial_fill_books_fraction_and_prohibits_unsafe_cancel_release(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)

        partial = self.store.record_fill(
            "command-1",
            cumulative_filled_quantity=Decimal("0.25"),
            now=NOW + timedelta(seconds=1),
        )
        during = self.store.get_exposure("testnet-canary", Environment.TESTNET)
        with self.assertRaisesRegex(StateConflict, "reconciliation-specific evidence"):
            self.store.mark_terminal(
                "command-1",
                state="canceled",
                now=NOW + timedelta(seconds=2),
            )
        after = self.store.get_exposure("testnet-canary", Environment.TESTNET)

        self.assertEqual(partial.reserved_notional, Decimal("2265"))
        self.assertEqual(partial.booked_notional, Decimal("755"))
        self.assertEqual(during.reserved_loss, Decimal("2277.231"))
        self.assertEqual(during.booked_loss, Decimal("759.077"))
        self.assertEqual(after, during)
        self.assertEqual(self.store.get_outbox("command-1").state, "observed_fill")

    def test_rejected_command_releases_entire_unused_reservation(self) -> None:
        self.service.admit(self.register_request(intent()), now=NOW)

        command = self.store.mark_terminal(
            "command-1", state="rejected", now=NOW + timedelta(seconds=1)
        )
        account = self.store.get_exposure("testnet-canary", Environment.TESTNET)

        self.assertEqual(command.reserved_quantity, Decimal("0"))
        self.assertEqual(command.released_notional, Decimal("3020"))
        self.assertEqual(account.reserved_notional, Decimal("0"))
        self.assertEqual(account.booked_notional, Decimal("0"))


class ConcurrentAdmissionTests(unittest.TestCase):
    def test_begin_immediate_prevents_account_limit_oversubscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "concurrent.sqlite3")
            store.register_deployment_grant(
                grant(max_notional=Decimal("100"), max_loss=Decimal("100")),
                policy(
                    max_order_notional=Decimal("100"),
                    max_order_worst_case_loss=Decimal("100"),
                    max_account_gross_notional=Decimal("100"),
                    max_account_worst_case_loss=Decimal("100"),
                ),
            )
            service = AdmissionService(store, audience="admission.test")
            requests: list[AdmissionRequest] = []
            for sequence in (1, 2):
                target = intent(
                    sequence,
                    limit_price=Decimal("50"),
                    price_bound=Decimal("60"),
                    stop_price=None,
                    max_slippage_bps=Decimal("0"),
                    fee_bps=Decimal("0"),
                )
                auth = authorization(target, sequence)
                store.register_authorization(auth)
                requests.append(
                    AdmissionRequest(
                        intent=target,
                        exposure=exposure(target),
                        authorization_id=auth.authorization_id,
                        command_id=f"command-{sequence}",
                        audience="admission.test",
                        authorization_model=AuthorizationModel.INFRASTRUCTURE,
                    )
                )

            def admit(request: AdmissionRequest) -> str:
                try:
                    return service.admit(request, now=NOW).state
                except PolicyViolation as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(admit, requests))

            self.assertCountEqual(outcomes, ["queued", "ACCOUNT_NOTIONAL_LIMIT"])
            self.assertEqual(
                store.get_exposure(
                    "testnet-canary", Environment.TESTNET
                ).reserved_notional,
                Decimal("60"),
            )
            states = {
                store.authorization_state("auth-1"),
                store.authorization_state("auth-2"),
            }
            self.assertEqual(states, {"issued", "consuming"})


if __name__ == "__main__":
    unittest.main()
