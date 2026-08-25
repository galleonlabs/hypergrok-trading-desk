from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from trading_harness.canonical import canonical_data, canonical_json, domain_hash
from trading_harness.domain import Environment
from trading_harness.errors import RecordNotFound, StateConflict
from trading_harness.execution_store import DispatchPreflight, RecoveryPermit
from trading_harness.hyperliquid_recovery import (
    CancelRequest,
    RecoveryKind,
    build_cancel_by_cloid,
    build_noop_fence,
    build_reduce_only_close,
    derive_recovery_close_cloid,
    recovery_action_material,
)
from trading_harness.hyperliquid_signer import (
    OFFICIAL_SDK_VERSION,
    MAX_PROTECTED_NOTIONAL,
    MAX_PROTECTED_QUANTITY,
    RECOVERY_SIGNING_ENABLED,
    SignerDependencyError,
    SignerOutputError,
    SignerPolicy,
    SignerPolicyError,
    SigningAccount,
    load_official_sign_l1_action,
    official_sdk_available,
    sign_recovery_action,
    sign_protected_action as _sign_protected_action,
)
from trading_harness.hyperliquid_wire import (
    HyperliquidNetwork,
    ProtectedOrderAction,
    build_protected_order_action,
)
from trading_harness.nonce import PersistentNonceAllocator
from trading_harness.planning import ProtectedTradePlan
from tests.test_execution_store import (
    NOW as STORE_NOW,
    ExecutionStoreTestCase,
    digest,
)
from tests.test_hyperliquid_account import (
    ACCOUNT as RECOVERY_MAIN_ACCOUNT,
    FixtureTransport as RecoveryFixtureTransport,
    TARGET_CLOID as RECOVERY_TARGET_CLOID,
    fetch as fetch_recovery_account,
    raw_order as raw_recovery_order,
    valid_clearing as valid_recovery_clearing,
)
from tests.test_hyperliquid_wire import metadata as metadata_fixture, protected_plan


NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
NOW_MS = 1_787_587_200_000
MAIN_ACCOUNT = "0x" + "a" * 40
SIGNER = "0x" + "b" * 40
OTHER_SIGNER = "0x" + "c" * 40
VAULT = "0x" + "d" * 40
SOURCE_PLAN = protected_plan()
SOURCE_METADATA = metadata_fixture()
PLAN_HASH = SOURCE_PLAN.plan_hash
METADATA_HASH = SOURCE_METADATA.source_hash
R = "0x" + "1" * 64
S = "0x" + "2" * 64
RECOVERY_CLOSE_CLOID = "0x" + "4" * 32


def action() -> dict[str, object]:
    return deepcopy(
        build_protected_order_action(
            SOURCE_PLAN,
            SOURCE_METADATA,
            network=HyperliquidNetwork.TESTNET,
            at=NOW,
        ).action
    )


def protected(
    *,
    wire_action: dict[str, object] | None = None,
    network: HyperliquidNetwork = HyperliquidNetwork.TESTNET,
    account_id: str = "testnet-account",
    expires_at_ms: int | None = None,
) -> ProtectedOrderAction:
    expected = build_protected_order_action(
        SOURCE_PLAN,
        SOURCE_METADATA,
        network=HyperliquidNetwork.TESTNET,
        at=NOW,
    )
    selected = action() if wire_action is None else wire_action
    selected_expiry = expected.expires_at_ms if expires_at_ms is None else expires_at_ms
    binding = {
        "network": network.value,
        "account_id": account_id,
        "plan_hash": PLAN_HASH,
        "metadata_hash": METADATA_HASH,
        "expires_at_ms": selected_expiry,
        "action": selected,
    }
    return ProtectedOrderAction(
        network=network,
        account_id=account_id,
        plan_hash=PLAN_HASH,
        metadata_hash=METADATA_HASH,
        expires_at_ms=selected_expiry,
        action=selected,
        action_hash=domain_hash("trading-harness/hyperliquid-action/v1", binding),
    )


def dispatch_preflight(
    *,
    plan_hash: str = PLAN_HASH,
    metadata_hash: str = METADATA_HASH,
    account_id: str = "testnet-account",
    environment: Environment = Environment.TESTNET,
    observed_at: datetime = NOW - timedelta(milliseconds=1),
    expires_at: datetime = NOW + timedelta(seconds=5),
    passed: bool = True,
) -> DispatchPreflight:
    return DispatchPreflight(
        command_id="command-1",
        ticket_hash=digest("ticket"),
        plan_hash=plan_hash,
        environment=environment,
        account_id=account_id,
        account_snapshot_hash=digest("account"),
        account_server_time_ms=int(
            (observed_at - timedelta(milliseconds=500)).timestamp() * 1_000
        ),
        metadata_hash=metadata_hash,
        market_snapshot_hash=digest("market"),
        risk_policy_hash=digest("risk"),
        observed_at=observed_at,
        expires_at=expires_at,
        passed=passed,
    )


def resized_plan(quantity: Decimal) -> ProtectedTradePlan:
    entry = replace(SOURCE_PLAN.entry, quantity=quantity)
    stop = replace(SOURCE_PLAN.protective_stop, quantity=quantity)
    target = replace(SOURCE_PLAN.take_profit, quantity=quantity)
    payload = {
        "domain": "protected-trade-plan-v1",
        "assessment_hash": SOURCE_PLAN.assessment_hash,
        "grouping": SOURCE_PLAN.grouping.value,
        "legs": [
            canonical_data(entry),
            canonical_data(stop),
            canonical_data(target),
        ],
    }
    plan_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return ProtectedTradePlan(
        assessment_hash=SOURCE_PLAN.assessment_hash,
        entry=entry,
        protective_stop=stop,
        take_profit=target,
        grouping=SOURCE_PLAN.grouping,
        plan_hash=plan_hash,
    )


def sign_protected_action(
    unsigned: ProtectedOrderAction,
    *,
    plan=SOURCE_PLAN,
    metadata=SOURCE_METADATA,
    preflight: DispatchPreflight | None = None,
    **kwargs,
):
    return _sign_protected_action(
        unsigned,
        plan=plan,
        metadata=metadata,
        preflight=dispatch_preflight() if preflight is None else preflight,
        **kwargs,
    )


def policy(
    *,
    signer_address: str = SIGNER,
    vault_address: str | None = None,
    networks: frozenset[HyperliquidNetwork] = frozenset(
        {HyperliquidNetwork.TESTNET}
    ),
    allow_mainnet: bool = False,
    assets: frozenset[int] = frozenset({1}),
) -> SignerPolicy:
    return SignerPolicy(
        accounts=(
            SigningAccount(
                account_id="testnet-account",
                main_account_address=MAIN_ACCOUNT,
                signer_address=signer_address,
                vault_address=vault_address,
            ),
        ),
        allowed_asset_ids=assets,
        allowed_networks=networks,
        allow_mainnet=allow_mainnet,
    )


class FakeWallet:
    def __init__(self, address: str = SIGNER) -> None:
        self.address = address


class GuardWallet:
    """Fails a test if an unauthorized path reaches wallet lookup."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def address(self) -> str:
        self.calls += 1
        raise AssertionError("wallet must remain untouched")


class FakeNonceAllocator:
    def __init__(self, events: list[str], nonce: object = NOW_MS + 1) -> None:
        self.events = events
        self.nonce = nonce
        self.calls = 0

    def allocate(self) -> object:
        self.events.append("nonce_committed")
        self.calls += 1
        return self.nonce


class FakeSigner:
    def __init__(self, events: list[str], result: object | None = None) -> None:
        self.events = events
        self.result = {"r": R, "s": S, "v": 28} if result is None else result
        self.calls: list[tuple[object, ...]] = []

    def __call__(
        self,
        wallet: object,
        wire_action: dict[str, object],
        vault_address: str | None,
        nonce: int,
        expires_after: int | None,
        is_mainnet: bool,
    ) -> object:
        self.events.append("signed")
        self.calls.append(
            (
                wallet,
                deepcopy(wire_action),
                vault_address,
                nonce,
                expires_after,
                is_mainnet,
            )
        )
        return deepcopy(self.result)


def make_signed(*, vault_address: str | None = None):
    events: list[str] = []
    signer = FakeSigner(events)
    result = sign_protected_action(
        protected(),
        policy=policy(vault_address=vault_address),
        wallet=FakeWallet(),
        nonce_allocator=FakeNonceAllocator(events),
        clock=lambda: NOW,
        sign_l1_action=signer,
    )
    return result


def durable_recovery_policy(
    *,
    maximum_expiry_horizon_ms: int = 15_000,
    signer_address: str = SIGNER,
    kind: RecoveryKind = RecoveryKind.REDUCE_ONLY_CLOSE,
) -> SignerPolicy:
    return SignerPolicy(
        accounts=(
            SigningAccount(
                account_id="testnet-account",
                main_account_address=RECOVERY_MAIN_ACCOUNT,
                signer_address=signer_address,
                owned_cloids=frozenset(
                    {RECOVERY_CLOSE_CLOID}
                    if kind is RecoveryKind.CANCEL_BY_CLOID
                    else {RECOVERY_CLOSE_CLOID, RECOVERY_TARGET_CLOID}
                ),
            ),
        ),
        allowed_asset_ids=frozenset({1}),
        allowed_recovery_kinds=frozenset({kind}),
        maximum_expiry_horizon_ms=maximum_expiry_horizon_ms,
    )


def prepare_durable_recovery_fixture(
    case: ExecutionStoreTestCase,
    *,
    lease_seconds: int = 7,
    signer_address: str = SIGNER,
    kind: RecoveryKind = RecoveryKind.REDUCE_ONLY_CLOSE,
    close_cloid: str | None = None,
):
    """Create one exact consumed permit and claimed close command for tests."""

    case.admit_one()
    incident = case.store.record_incident(
        incident_id="durable-recovery-incident",
        command_id="command-1",
        code="RECOVERY_REQUIRED",
        severity="critical",
        at=STORE_NOW + timedelta(seconds=5),
    )
    evidence_at = STORE_NOW + timedelta(seconds=6)
    evidence_ms = int(evidence_at.timestamp() * 1_000)
    durable_target_cloid = next(
        item.cloid
        for item in case.store.get_legs("command-1")
        if item.role == "take_profit"
    )
    recovery_orders = (
        [
            raw_recovery_order(
                oid=102,
                cloid=durable_target_cloid,
                order_type="Take Market",
                trigger_price="3000",
                trigger_condition="Triggered above 3000",
            )
        ]
        if kind is RecoveryKind.CANCEL_BY_CLOID
        else None
    )
    snapshot, _ = fetch_recovery_account(
        RecoveryFixtureTransport(
            clearing=valid_recovery_clearing(server_time=evidence_ms),
            orders=recovery_orders,
        ),
        received_at_ms=evidence_ms,
        network="testnet",
    )
    if kind is RecoveryKind.REDUCE_ONLY_CLOSE:
        selected_close_cloid = (
            derive_recovery_close_cloid(
                account_id="testnet-account",
                incident_id=incident.incident_id,
                position_snapshot_hash=snapshot.snapshot_hash,
            )
            if close_cloid is None
            else close_cloid
        )
        recovery = build_reduce_only_close(
            snapshot,
            symbol="ETH",
            price_bound=Decimal("2400"),
            cloid=selected_close_cloid,
            incident=incident,
            account_id="testnet-account",
            network=HyperliquidNetwork.TESTNET,
            at=evidence_at,
        )
        source_hash = recovery.position_snapshot_hash
    elif kind is RecoveryKind.CANCEL_BY_CLOID:
        recovery = build_cancel_by_cloid(
            snapshot,
            (CancelRequest("ETH", durable_target_cloid),),
            owned_cloids=(durable_target_cloid,),
            incident=incident,
            account_id="testnet-account",
            network=HyperliquidNetwork.TESTNET,
            at=evidence_at,
        )
        source_hash = recovery.account_snapshot_hash
    else:
        raise ValueError("close/cancel fixture received unsupported recovery kind")
    selected_policy = durable_recovery_policy(
        signer_address=signer_address,
        kind=kind,
    )
    material = recovery_action_material(recovery)
    permit = RecoveryPermit(
        permit_id="durable-recovery-permit",
        token_hash=digest("durable-recovery-token"),
        parent_command_id="command-1",
        incident_id=incident.incident_id,
        kind=recovery.kind.value,
        environment=Environment.TESTNET,
        account_id="testnet-account",
        source_hash=source_hash,
        preflight_hash=None,
        recovery_hash=recovery.recovery_hash,
        recovery_material=material,
        safety_policy_hash=selected_policy.safety_policy_hash,
        original_attempt_id=None,
        original_nonce=None,
        issuer_id="testnet-recovery-authority",
        audience="recovery-worker",
        issued_at=evidence_at,
        expires_at=STORE_NOW + timedelta(seconds=16),
    )
    case.store.register_recovery_permit(permit)
    command = case.store.queue_recovery(
        recovery_command_id="durable-recovery-command",
        permit_id=permit.permit_id,
        token_hash=permit.token_hash,
        audience=permit.audience,
        at=STORE_NOW + timedelta(seconds=7),
    )
    claim = case.store.claim_next_recovery(
        "recovery-worker",
        at=STORE_NOW + timedelta(seconds=8),
        lease_seconds=lease_seconds,
    )
    assert claim is not None
    return recovery, snapshot, selected_policy, incident, permit, command, claim


def prepare_durable_noop_fixture(
    case: ExecutionStoreTestCase,
    *,
    signer_address: str = SIGNER,
):
    ticket, _ = case.admit_one()
    dispatch_claim = case.store.claim_next(
        "dispatcher",
        at=STORE_NOW + timedelta(seconds=1),
        lease_seconds=10,
    )
    assert dispatch_claim is not None
    preflight = case.register_preflight(ticket)
    original_nonce = int(
        (STORE_NOW + timedelta(seconds=2)).timestamp() * 1_000
    )
    signed_parent = case.make_signed_evidence(
        preflight,
        nonce=original_nonce,
    )
    case.store.prepare_attempt(
        "command-1",
        "dispatcher",
        dispatch_claim.fencing_token,
        attempt_id="durable-parent-attempt",
        preflight_hash=preflight.preflight_hash,
        signed_evidence=signed_parent,
        nonce=original_nonce,
        action_hash=signed_parent.action_hash,
        wire_hash=signed_parent.wire_hash,
        at=STORE_NOW + timedelta(seconds=2),
    )
    case.store.mark_submitted_unknown(
        "command-1",
        "dispatcher",
        dispatch_claim.fencing_token,
        transport_evidence=case.make_transport_evidence(
            "durable-parent-attempt",
            signed_parent,
            outcome="unknown",
        ),
        at=STORE_NOW + timedelta(seconds=3),
    )
    case.store.claim_reconciliation(
        "command-1",
        "reconciler",
        at=STORE_NOW + timedelta(seconds=4),
        lease_seconds=30,
    )
    incident = case.store.record_incident(
        incident_id="durable-noop-incident",
        command_id="command-1",
        code="AMBIGUOUS_SUBMISSION",
        severity="critical",
        at=STORE_NOW + timedelta(seconds=5),
    )
    attempt = case.store.get_attempt("command-1")
    recovery = build_noop_fence(
        attempt,
        incident=incident,
        account_id="testnet-account",
        main_account_address=RECOVERY_MAIN_ACCOUNT,
        network=HyperliquidNetwork.TESTNET,
        at=STORE_NOW + timedelta(seconds=6),
    )
    selected_policy = durable_recovery_policy(
        signer_address=signer_address,
        kind=RecoveryKind.NOOP_FENCE,
    )
    permit = RecoveryPermit(
        permit_id="durable-noop-permit",
        token_hash=digest("durable-noop-token"),
        parent_command_id="command-1",
        incident_id=incident.incident_id,
        kind=recovery.kind.value,
        environment=Environment.TESTNET,
        account_id="testnet-account",
        source_hash=recovery.ambiguous_attempt_hash,
        preflight_hash=attempt.preflight_hash,
        recovery_hash=recovery.recovery_hash,
        recovery_material=recovery_action_material(recovery),
        safety_policy_hash=selected_policy.safety_policy_hash,
        original_attempt_id=attempt.attempt_id,
        original_nonce=attempt.nonce,
        issuer_id="testnet-recovery-authority",
        audience="recovery-worker",
        issued_at=STORE_NOW + timedelta(seconds=6),
        expires_at=STORE_NOW + timedelta(seconds=16),
    )
    case.store.register_recovery_permit(permit)
    command = case.store.queue_recovery(
        recovery_command_id="durable-noop-command",
        permit_id=permit.permit_id,
        token_hash=permit.token_hash,
        audience=permit.audience,
        at=STORE_NOW + timedelta(seconds=7),
    )
    claim = case.store.claim_next_recovery(
        "recovery-worker",
        at=STORE_NOW + timedelta(seconds=8),
        lease_seconds=7,
    )
    assert claim is not None
    return recovery, attempt, selected_policy, permit, command, claim


class IsolatedSigningTests(unittest.TestCase):
    def test_nonce_is_committed_before_exact_single_sign_and_wire_is_frozen(self) -> None:
        events: list[str] = []
        signer = FakeSigner(events)
        unsigned = protected()
        signed = sign_protected_action(
            unsigned,
            policy=policy(),
            wallet=FakeWallet("0x" + "B" * 40),
            nonce_allocator=FakeNonceAllocator(events),
            clock=lambda: NOW,
            sign_l1_action=signer,
        )

        self.assertEqual(events, ["nonce_committed", "signed"])
        self.assertEqual(len(signer.calls), 1)
        _, sent_action, vault, nonce, expiry, mainnet = signer.calls[0]
        self.assertEqual(sent_action, action())
        self.assertIsNone(vault)
        self.assertEqual(nonce, NOW_MS + 1)
        self.assertEqual(expiry, NOW_MS + 5_000)
        self.assertFalse(mainnet)
        self.assertEqual(signed.signer_address, SIGNER)
        self.assertEqual(signed.main_account_address, MAIN_ACCOUNT)
        self.assertEqual(signed.preflight_hash, dispatch_preflight().preflight_hash)
        self.assertEqual(signed.preflight_expires_at_ms, NOW_MS + 5_000)
        self.assertEqual(signed.signing_implementation, "injected")
        self.assertRegex(signed.signature_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(signed.envelope_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(
            signed.wire_hash,
            hashlib.sha256(signed.wire_bytes).hexdigest(),
        )
        self.assertEqual(
            tuple(signed.envelope()),
            ("action", "nonce", "signature", "vaultAddress", "expiresAfter"),
        )
        signed.verify_integrity()
        persisted = signed.execution_store_evidence("command-1")
        self.assertEqual(persisted.preflight_hash, signed.preflight_hash)
        self.assertEqual(persisted.wire_hash, signed.wire_hash)
        self.assertEqual(persisted.signer_binding_hash, signed.signer_binding_hash)
        json.dumps(signed.as_dict(), allow_nan=False, sort_keys=True)

        # The signed artifact owns immutable text, not the caller's mutable dict.
        unsigned.action["orders"][0]["p"] = "9999"  # type: ignore[index]
        self.assertEqual(
            signed.envelope()["action"]["orders"][0]["p"],  # type: ignore[index]
            action()["orders"][0]["p"],  # type: ignore[index]
        )

    def test_optional_vault_is_bound_into_signature_and_envelope(self) -> None:
        signed = make_signed(vault_address=VAULT)

        self.assertEqual(signed.vault_address, VAULT)
        self.assertEqual(signed.envelope()["vaultAddress"], VAULT)

    def test_real_persistent_allocator_commits_the_nonce_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            allocator = PersistentNonceAllocator(
                Path(directory) / "signer-nonce.sqlite3",
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            events: list[str] = []
            signed = sign_protected_action(
                protected(),
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=allocator,
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )

            self.assertEqual(allocator.last_allocated(), signed.nonce)
            self.assertEqual(events, ["signed"])

    def test_signature_failure_burns_committed_nonce_and_sanitizes_error(self) -> None:
        events: list[str] = []
        allocator = FakeNonceAllocator(events)

        def broken(*arguments: object) -> object:
            del arguments
            events.append("signed")
            raise RuntimeError("secret implementation detail")

        with self.assertRaises(SignerOutputError) as caught:
            sign_protected_action(
                protected(),
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=allocator,
                clock=lambda: NOW,
                sign_l1_action=broken,
            )

        self.assertEqual(events, ["nonce_committed", "signed"])
        self.assertEqual(allocator.calls, 1)
        self.assertNotIn("secret implementation detail", str(caught.exception))

    def test_signing_implementation_cannot_mutate_the_reviewed_action(self) -> None:
        events: list[str] = []

        def mutating(
            wallet: object,
            wire_action: dict[str, object],
            vault: str | None,
            nonce: int,
            expiry: int | None,
            mainnet: bool,
        ) -> object:
            del wallet, vault, nonce, expiry, mainnet
            events.append("signed")
            wire_action["type"] = "noop"
            return {"r": R, "s": S, "v": 28}

        with self.assertRaisesRegex(SignerOutputError, "mutated"):
            sign_protected_action(
                protected(),
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=mutating,
            )
        self.assertEqual(events, ["nonce_committed", "signed"])

    def test_bad_signature_shape_is_rejected_after_nonce_commit(self) -> None:
        cases = (
            {"r": R, "s": S, "v": 29},
            {"r": "0xBAD", "s": S, "v": 28},
            {"s": S, "r": R, "v": 28},
            {"r": R, "s": S, "v": True},
        )
        for result in cases:
            with self.subTest(result=result):
                events: list[str] = []
                with self.assertRaises(SignerOutputError):
                    sign_protected_action(
                        protected(),
                        policy=policy(),
                        wallet=FakeWallet(),
                        nonce_allocator=FakeNonceAllocator(events),
                        clock=lambda: NOW,
                        sign_l1_action=FakeSigner(events, result),
                    )
                self.assertEqual(events, ["nonce_committed", "signed"])


class IndependentActionValidationTests(unittest.TestCase):
    def assert_action_denied(self, mutation) -> None:
        selected = action()
        mutation(selected)
        events: list[str] = []
        with self.assertRaises(SignerPolicyError):
            sign_protected_action(
                protected(wire_action=selected),
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_rejects_any_widening_or_malformed_bracket_before_nonce(self) -> None:
        mutations = (
            lambda value: value.__setitem__("builder", {"b": MAIN_ACCOUNT, "f": 1}),
            lambda value: value.__setitem__("type", "sendAsset"),
            lambda value: value["orders"].pop(),
            lambda value: value.__setitem__("grouping", "na"),
            lambda value: value["orders"][0]["t"]["limit"].__setitem__("tif", "Gtc"),
            lambda value: value["orders"][1].__setitem__("r", False),
            lambda value: value["orders"][2]["t"]["trigger"].__setitem__("tpsl", "sl"),
            lambda value: value["orders"][2].__setitem__("a", 2),
            lambda value: value["orders"][2].__setitem__(
                "c", value["orders"][1]["c"]
            ),
            lambda value: value["orders"][2].__setitem__("s", "0.3"),
            lambda value: value["orders"][1].__setitem__("b", True),
            lambda value: value["orders"][0].__setitem__("p", "2500.0"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assert_action_denied(mutation)

    def test_post_hash_float_mutation_is_rejected_before_nonce(self) -> None:
        unsigned = protected()
        unsigned.action["orders"][0]["p"] = 2500.0  # type: ignore[index]
        events: list[str] = []

        with self.assertRaises(SignerPolicyError):
            sign_protected_action(
                unsigned,
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_field_reordering_is_rejected_even_when_canonical_hash_matches(self) -> None:
        selected = action()
        selected["orders"][0] = {
            "b": True,
            "a": 1,
            "p": "2500",
            "s": "0.2",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
            "c": "0x" + "1" * 32,
        }

        self.assert_action_denied(lambda value: value.__setitem__("orders", selected["orders"]))

    def test_mismatched_precomputed_action_hash_is_rejected_before_nonce(self) -> None:
        events: list[str] = []
        unsigned = replace(protected(), action_hash="0" * 64)

        with self.assertRaisesRegex(SignerPolicyError, "rebuilt|hash"):
            sign_protected_action(
                unsigned,
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_copied_plan_hash_with_changed_economics_is_independently_rejected(self) -> None:
        forged = object.__new__(ProtectedTradePlan)
        for field in (
            "assessment_hash",
            "entry",
            "protective_stop",
            "take_profit",
            "grouping",
            "plan_hash",
        ):
            object.__setattr__(forged, field, getattr(SOURCE_PLAN, field))
        changed_quantity = SOURCE_PLAN.entry.quantity + Decimal("0.001")
        object.__setattr__(
            forged,
            "entry",
            replace(SOURCE_PLAN.entry, quantity=changed_quantity),
        )
        object.__setattr__(
            forged,
            "protective_stop",
            replace(SOURCE_PLAN.protective_stop, quantity=changed_quantity),
        )
        object.__setattr__(
            forged,
            "take_profit",
            replace(SOURCE_PLAN.take_profit, quantity=changed_quantity),
        )
        events: list[str] = []

        with self.assertRaisesRegex(SignerPolicyError, "plan"):
            sign_protected_action(
                protected(),
                plan=forged,
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_copied_preflight_hash_with_changed_expiry_is_rejected(self) -> None:
        original = dispatch_preflight()
        forged = object.__new__(DispatchPreflight)
        for field in (
            "command_id",
            "ticket_hash",
            "plan_hash",
            "environment",
            "account_id",
            "account_snapshot_hash",
            "account_server_time_ms",
            "metadata_hash",
            "market_snapshot_hash",
            "risk_policy_hash",
            "observed_at",
            "expires_at",
            "passed",
            "preflight_hash",
        ):
            object.__setattr__(forged, field, getattr(original, field))
        object.__setattr__(forged, "expires_at", NOW + timedelta(seconds=20))
        events: list[str] = []

        with self.assertRaisesRegex(SignerPolicyError, "preflight"):
            sign_protected_action(
                protected(),
                preflight=forged,
                policy=policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_compiled_quantity_and_notional_ceilings_precede_nonce(self) -> None:
        quantity_plan = resized_plan(MAX_PROTECTED_QUANTITY + Decimal("1"))
        notional_plan = resized_plan(
            (MAX_PROTECTED_NOTIONAL / SOURCE_PLAN.entry.price_bound)
            .quantize(Decimal("0.001"))
            + Decimal("0.001")
        )
        for selected_plan, message in (
            (quantity_plan, "quantity"),
            (notional_plan, "notional"),
        ):
            with self.subTest(message=message):
                unsigned = build_protected_order_action(
                    selected_plan,
                    SOURCE_METADATA,
                    network=HyperliquidNetwork.TESTNET,
                    at=NOW,
                )
                selected_preflight = dispatch_preflight(
                    plan_hash=selected_plan.plan_hash,
                )
                events: list[str] = []
                with self.assertRaisesRegex(SignerPolicyError, message):
                    sign_protected_action(
                        unsigned,
                        plan=selected_plan,
                        preflight=selected_preflight,
                        policy=policy(),
                        wallet=FakeWallet(),
                        nonce_allocator=FakeNonceAllocator(events),
                        clock=lambda: NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])


class SignerPolicyTests(unittest.TestCase):
    def test_recovery_safety_policy_hash_is_canonical_and_economically_complete(self) -> None:
        first = durable_recovery_policy()
        second = durable_recovery_policy()
        changed_expiry = durable_recovery_policy(
            maximum_expiry_horizon_ms=14_000
        )
        changed_kind = durable_recovery_policy(
            kind=RecoveryKind.CANCEL_BY_CLOID
        )

        self.assertEqual(first.safety_policy_hash, second.safety_policy_hash)
        self.assertNotEqual(first.safety_policy_hash, changed_expiry.safety_policy_hash)
        self.assertNotEqual(first.safety_policy_hash, changed_kind.safety_policy_hash)

    def test_network_account_asset_and_wallet_are_all_explicitly_allowlisted(self) -> None:
        cases = (
            (
                protected(account_id="other"),
                policy(),
                FakeWallet(),
                "bindings",
            ),
            (
                protected(),
                policy(assets=frozenset({2})),
                FakeWallet(),
                "asset",
            ),
            (
                protected(),
                policy(),
                FakeWallet(OTHER_SIGNER),
                "wallet",
            ),
        )
        for unsigned, selected_policy, wallet, message in cases:
            with self.subTest(message=message):
                events: list[str] = []
                with self.assertRaisesRegex(SignerPolicyError, message):
                    sign_protected_action(
                        unsigned,
                        policy=selected_policy,
                        wallet=wallet,
                        nonce_allocator=FakeNonceAllocator(events),
                        clock=lambda: NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])

    def test_mainnet_is_hard_disabled_even_when_flagged(self) -> None:
        with self.assertRaisesRegex(SignerPolicyError, "mainnet"):
            policy(networks=frozenset({HyperliquidNetwork.MAINNET}))
        with self.assertRaisesRegex(SignerPolicyError, "mainnet"):
            policy(allow_mainnet=True)

    def test_expiry_and_nonce_time_window_fail_closed(self) -> None:
        cases = (
            (
                protected(),
                NOW_MS + 1,
                "preflight|expires",
                dispatch_preflight(expires_at=NOW + timedelta(milliseconds=999)),
            ),
            (
                protected(),
                NOW_MS + 86_400_000,
                "nonce",
                dispatch_preflight(),
            ),
        )
        for unsigned, nonce, message, selected_preflight in cases:
            with self.subTest(message=message):
                events: list[str] = []
                with self.assertRaisesRegex(SignerPolicyError, message):
                    sign_protected_action(
                        unsigned,
                        preflight=selected_preflight,
                        policy=policy(),
                        wallet=FakeWallet(),
                        nonce_allocator=FakeNonceAllocator(events, nonce),
                        clock=lambda: NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                if "preflight" in message:
                    self.assertEqual(events, [])
                else:
                    self.assertEqual(events, ["nonce_committed"])

    def test_longer_authorization_is_clamped_to_15_second_l1_expiry(self) -> None:
        events: list[str] = []
        signer = FakeSigner(events)
        signed = sign_protected_action(
            protected(),
            preflight=dispatch_preflight(expires_at=NOW + timedelta(seconds=20)),
            policy=policy(),
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(events),
            clock=lambda: NOW,
            sign_l1_action=signer,
        )

        self.assertEqual(
            signed.authorization_expires_at_ms,
            int(SOURCE_PLAN.entry.expires_at.timestamp() * 1000),
        )
        self.assertEqual(signed.expires_after_ms, NOW_MS + 15_000)
        self.assertEqual(signer.calls[0][4], NOW_MS + 15_000)
        self.assertEqual(signed.envelope()["expiresAfter"], NOW_MS + 15_000)

    def test_signing_account_rejects_master_key_as_api_wallet(self) -> None:
        with self.assertRaisesRegex(SignerPolicyError, "differ"):
            SigningAccount(
                account_id="unsafe",
                main_account_address=MAIN_ACCOUNT,
                signer_address=MAIN_ACCOUNT,
            )


class DurableRecoverySigningTests(ExecutionStoreTestCase):
    def sign_fixture(self, *, policy_override: SignerPolicy | None = None):
        (
            recovery,
            snapshot,
            selected_policy,
            incident,
            permit,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self)
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=snapshot,
            policy=selected_policy if policy_override is None else policy_override,
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(
                events,
                int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 1,
            ),
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            sign_l1_action=FakeSigner(events),
        )
        return (
            signed,
            events,
            recovery,
            snapshot,
            selected_policy,
            incident,
            permit,
            command,
            claim,
        )

    def test_public_signing_consumes_exact_authority_and_freezes_local_attestation(self) -> None:
        self.assertTrue(RECOVERY_SIGNING_ENABLED)
        (
            signed,
            events,
            recovery,
            _,
            selected_policy,
            _,
            permit,
            command,
            claim,
        ) = self.sign_fixture()

        self.assertEqual(events, ["nonce_committed", "signed"])
        self.assertEqual(signed.recovery_command_id, command.recovery_command_id)
        self.assertEqual(signed.permit_id, permit.permit_id)
        self.assertEqual(signed.parent_command_id, command.parent_command_id)
        self.assertEqual(signed.worker_id, "recovery-worker")
        self.assertEqual(signed.fencing_token, claim.fencing_token)
        self.assertEqual(signed.safety_policy_hash, selected_policy.safety_policy_hash)
        self.assertEqual(signed.recovery_hash, recovery.recovery_hash)
        self.assertEqual(
            signed.expires_after_ms,
            int((STORE_NOW + timedelta(seconds=15)).timestamp() * 1_000),
        )
        self.assertNotIn(permit.permit_id, signed.wire_json)
        self.assertNotIn(command.recovery_command_id, signed.wire_json)
        self.assertNotIn(signed.signing_authority_hash, signed.wire_json)
        signed.verify_integrity()
        evidence = signed.execution_store_evidence()
        prepared = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="durable-recovery-attempt",
            signed_evidence=evidence,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=100),
        )
        self.assertEqual(prepared.signed_evidence_hash, evidence.evidence_hash)
        self.assertEqual(prepared.action_hash, signed.action_hash)
        self.assertEqual(prepared.wire_hash, signed.wire_hash)

    def test_public_cancel_path_requires_the_same_durable_authority(self) -> None:
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(
            self,
            kind=RecoveryKind.CANCEL_BY_CLOID,
        )
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=snapshot,
            policy=selected_policy,
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(
                events,
                int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 2,
            ),
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            sign_l1_action=FakeSigner(events),
        )
        self.assertEqual(events, ["nonce_committed", "signed"])
        self.assertNotIn(
            recovery.requests[0].cloid,
            selected_policy.account("testnet-account").owned_cloids,
        )
        self.assertIs(signed.recovery_kind, RecoveryKind.CANCEL_BY_CLOID)
        self.assertEqual(signed.envelope()["action"], recovery.action)
        signed.verify_integrity()

    def test_public_noop_signing_reuses_exact_durable_unknown_nonce(self) -> None:
        (
            recovery,
            attempt,
            selected_policy,
            _,
            command,
            claim,
        ) = prepare_durable_noop_fixture(self)
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=attempt,
            policy=selected_policy,
            wallet=FakeWallet(),
            nonce_allocator=None,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            sign_l1_action=FakeSigner(events),
        )
        self.assertEqual(events, ["signed"])
        self.assertEqual(signed.nonce, attempt.nonce)
        self.assertEqual(signed.original_attempt_id, attempt.attempt_id)
        self.assertEqual(signed.preflight_hash, attempt.preflight_hash)
        self.assertEqual(signed.envelope()["action"], {"type": "noop"})
        signed.verify_integrity()

    def test_duplicate_signing_and_caller_forged_permit_fail_before_wallet_or_nonce(self) -> None:
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            permit,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self)
        base = {
            "store": self.store,
            "recovery_command_id": command.recovery_command_id,
            "worker_id": "recovery-worker",
            "fencing_token": claim.fencing_token,
            "evidence": snapshot,
            "policy": selected_policy,
            "wallet": FakeWallet(),
            "nonce_allocator": FakeNonceAllocator([], int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 1),
            "clock": lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            "sign_l1_action": FakeSigner([]),
        }
        with self.assertRaisesRegex(TypeError, "permit"):
            sign_recovery_action(recovery, permit=permit, **base)  # type: ignore[call-arg]
        forged_store_arguments = dict(base)
        forged_store_wallet = GuardWallet()
        forged_store_arguments["store"] = object()
        forged_store_arguments["wallet"] = forged_store_wallet
        with self.assertRaisesRegex(TypeError, "exact ExecutionStore"):
            sign_recovery_action(recovery, **forged_store_arguments)
        self.assertEqual(forged_store_wallet.calls, 0)
        self.assertEqual(
            self.store.get_recovery_outbox(command.recovery_command_id).state,
            "claimed",
        )

        first_events: list[str] = []
        first_arguments = dict(base)
        first_arguments["nonce_allocator"] = FakeNonceAllocator(
            first_events,
            int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 1,
        )
        first_arguments["sign_l1_action"] = FakeSigner(first_events)
        sign_recovery_action(recovery, **first_arguments)
        replay_events: list[str] = []
        replay_arguments = dict(base)
        replay_wallet = GuardWallet()
        replay_arguments["wallet"] = replay_wallet
        replay_arguments["nonce_allocator"] = FakeNonceAllocator(replay_events)
        replay_arguments["sign_l1_action"] = FakeSigner(replay_events)
        with self.assertRaisesRegex(StateConflict, "state|consumed|claim"):
            sign_recovery_action(recovery, **replay_arguments)
        self.assertEqual(replay_events, [])
        self.assertEqual(replay_wallet.calls, 0)

    def test_live_store_rejects_static_but_non_derived_close_cloid_before_key_use(self) -> None:
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(
            self,
            close_cloid=RECOVERY_CLOSE_CLOID,
        )
        self.assertIn(
            RECOVERY_CLOSE_CLOID,
            selected_policy.account("testnet-account").owned_cloids,
        )
        events: list[str] = []
        wallet = GuardWallet()
        with self.assertRaisesRegex(SignerPolicyError, "exact derived CLOID"):
            sign_recovery_action(
                recovery,
                store=self.store,
                recovery_command_id=command.recovery_command_id,
                worker_id="recovery-worker",
                fencing_token=claim.fencing_token,
                evidence=snapshot,
                policy=selected_policy,
                wallet=wallet,
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])
        self.assertEqual(wallet.calls, 0)
        self.assertEqual(
            self.store.get_recovery_outbox(command.recovery_command_id).state,
            "claimed",
        )

    def test_valid_but_different_recovery_is_denied_before_key_use(self) -> None:
        (
            recovery,
            snapshot,
            selected_policy,
            incident,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self)
        different = build_reduce_only_close(
            snapshot,
            symbol="ETH",
            price_bound=Decimal("2400"),
            close_size=Decimal("0.2"),
            cloid=recovery.cloid,
            incident=incident,
            account_id="testnet-account",
            network=HyperliquidNetwork.TESTNET,
            at=STORE_NOW + timedelta(seconds=6),
        )
        events: list[str] = []
        wallet = GuardWallet()
        with self.assertRaisesRegex(SignerPolicyError, "durable"):
            sign_recovery_action(
                different,
                store=self.store,
                recovery_command_id=command.recovery_command_id,
                worker_id="recovery-worker",
                fencing_token=claim.fencing_token,
                evidence=snapshot,
                policy=selected_policy,
                wallet=wallet,
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])
        self.assertEqual(wallet.calls, 0)

    def test_wrong_safety_policy_is_denied_before_key_use(self) -> None:
        (
            recovery,
            snapshot,
            _,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self)
        wrong_policy = durable_recovery_policy(maximum_expiry_horizon_ms=14_000)
        events = []
        wallet = GuardWallet()
        with self.assertRaisesRegex(SignerPolicyError, "safety policy"):
            sign_recovery_action(
                recovery,
                store=self.store,
                recovery_command_id=command.recovery_command_id,
                worker_id="recovery-worker",
                fencing_token=claim.fencing_token,
                evidence=snapshot,
                policy=wrong_policy,
                wallet=wallet,
                nonce_allocator=FakeNonceAllocator(events),
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])
        self.assertEqual(wallet.calls, 0)

    def test_stale_lease_wrong_fence_and_unknown_command_fail_before_key_use(self) -> None:
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self, lease_seconds=1)
        cases = (
            (command.recovery_command_id, claim.fencing_token + 1, STORE_NOW + timedelta(seconds=8, milliseconds=1)),
            (command.recovery_command_id, claim.fencing_token, STORE_NOW + timedelta(seconds=9)),
            ("unknown-recovery-command", claim.fencing_token, STORE_NOW + timedelta(seconds=8, milliseconds=1)),
        )
        for command_id, token, at in cases:
            with self.subTest(command_id=command_id, token=token, at=at):
                events: list[str] = []
                wallet = GuardWallet()
                expected_error = (
                    RecordNotFound
                    if command_id == "unknown-recovery-command"
                    else StateConflict
                )
                with self.assertRaises(expected_error):
                    sign_recovery_action(
                        recovery,
                        store=self.store,
                        recovery_command_id=command_id,
                        worker_id="recovery-worker",
                        fencing_token=token,
                        evidence=snapshot,
                        policy=selected_policy,
                        wallet=wallet,
                        nonce_allocator=FakeNonceAllocator(events),
                        clock=lambda at=at: at,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])
                self.assertEqual(wallet.calls, 0)

    @unittest.skipUnless(
        official_sdk_available(),
        f"requires optional hyperliquid-python-sdk=={OFFICIAL_SDK_VERSION}",
    )
    def test_public_durable_path_uses_official_0240_signature_contract(self) -> None:
        from eth_account import Account
        from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

        wallet = Account.from_key(
            "0x0123456789012345678901234567890123456789012345678901234567890123"
        )
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(
            self, signer_address=wallet.address.lower()
        )
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=snapshot,
            policy=selected_policy,
            wallet=wallet,
            nonce_allocator=FakeNonceAllocator(
                [],
                int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 1,
            ),
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
        )
        envelope = signed.envelope()
        recovered = recover_agent_or_user_from_l1_action(
            envelope["action"],
            envelope["signature"],
            envelope["vaultAddress"],
            envelope["nonce"],
            envelope["expiresAfter"],
            False,
        )
        self.assertEqual(recovered.lower(), wallet.address.lower())


class OfficialSdkContractTests(unittest.TestCase):
    def test_nonpinned_sdk_version_is_refused_before_import(self) -> None:
        with mock.patch(
            "trading_harness.hyperliquid_signer.importlib_metadata.version",
            return_value="0.23.0",
        ):
            with self.assertRaisesRegex(SignerDependencyError, "0.23.0"):
                load_official_sign_l1_action()

    def test_missing_optional_sdk_is_an_explicit_dependency_failure(self) -> None:
        if official_sdk_available():
            self.skipTest("official SDK is installed; golden vector covers loading")
        with self.assertRaises(SignerDependencyError):
            load_official_sign_l1_action()

    @unittest.skipUnless(
        official_sdk_available(),
        f"requires optional hyperliquid-python-sdk=={OFFICIAL_SDK_VERSION}",
    )
    def test_official_0240_order_signing_golden_vector(self) -> None:
        # Vector copied from official SDK 0.24.0 tests/signing_test.py.
        from eth_account import Account

        wallet = Account.from_key(
            "0x0123456789012345678901234567890123456789012345678901234567890123"
        )
        official_action = {
            "type": "order",
            "orders": [
                {
                    "a": 1,
                    "b": True,
                    "p": "100",
                    "s": "100",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                }
            ],
            "grouping": "na",
        }
        signature = load_official_sign_l1_action()(
            wallet,
            official_action,
            None,
            0,
            None,
            False,
        )

        self.assertEqual(
            signature,
            {
                "r": "0x82b2ba28e76b3d761093aaded1b1cdad4960b3af30212b343fb2e6cdfa4e3d54",
                "s": "0x6b53878fc99d26047f4d7e8c90eb98955a109f44209163f52d8dc4278cbbd9f5",
                "v": 27,
            },
        )

    @unittest.skipUnless(
        official_sdk_available(),
        f"requires optional hyperliquid-python-sdk=={OFFICIAL_SDK_VERSION}",
    )
    def test_official_sdk_recovers_signer_from_frozen_three_leg_wire(self) -> None:
        from eth_account import Account
        from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

        wallet = Account.from_key(
            "0x0123456789012345678901234567890123456789012345678901234567890123"
        )
        selected_policy = SignerPolicy(
            accounts=(
                SigningAccount(
                    account_id="testnet-account",
                    main_account_address=MAIN_ACCOUNT,
                    signer_address=wallet.address.lower(),
                ),
            ),
            allowed_asset_ids=frozenset({1}),
        )
        events: list[str] = []
        signed = sign_protected_action(
            protected(),
            policy=selected_policy,
            wallet=wallet,
            nonce_allocator=FakeNonceAllocator(events),
            clock=lambda: NOW,
        )
        envelope = signed.envelope()
        recovered = recover_agent_or_user_from_l1_action(
            envelope["action"],
            envelope["signature"],
            envelope["vaultAddress"],
            envelope["nonce"],
            envelope["expiresAfter"],
            False,
        )

        self.assertEqual(recovered.lower(), wallet.address.lower())
        self.assertEqual(
            signed.signing_implementation,
            "hyperliquid-python-sdk==0.24.0",
        )


if __name__ == "__main__":
    unittest.main()
