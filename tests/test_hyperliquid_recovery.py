from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import unittest
from unittest import mock

from trading_harness.canonical import canonical_decimal, domain_hash
from trading_harness.errors import ValidationError
from trading_harness.execution_store import AttemptRecord, IncidentRecord
from trading_harness.hyperliquid_recovery import (
    RECOVERY_ACTION_HASH_DOMAIN,
    CancelRequest,
    RecoveryKind,
    ambiguous_attempt_hash,
    build_cancel_by_cloid,
    build_noop_fence,
    build_reduce_only_close,
    recovery_action_from_material,
    recovery_action_material,
)
from trading_harness.hyperliquid_signer import (
    OFFICIAL_SDK_VERSION,
    RECOVERY_SIGNING_ENABLED,
    SignerOutputError,
    SignerPolicy,
    SignerPolicyError,
    SigningAccount,
    _sign_recovery_action_for_test as sign_recovery_action,
    official_sdk_available,
    sign_recovery_action as public_sign_recovery_action,
)
from trading_harness.hyperliquid_transport import (
    HttpExchangeResponse,
    HyperliquidSubmissionError,
    NOOP_FENCE_SUBMISSION_ENABLED,
    RECOVERY_SUBMISSION_ENABLED,
    submit_signed_action,
)
from trading_harness import hyperliquid_transport
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from tests.test_hyperliquid_account import (
    ACCOUNT,
    RECEIVED_TIME_MS,
    STOP_CLOID,
    TARGET_CLOID,
    FixtureTransport,
    fetch as fetch_account,
    raw_position,
    valid_clearing,
)
from tests.test_hyperliquid_signer import (
    SIGNER,
    FakeNonceAllocator,
    FakeSigner,
    FakeWallet,
)


RECOVERY_NOW_MS = RECEIVED_TIME_MS
RECOVERY_NOW = datetime.fromtimestamp(RECOVERY_NOW_MS / 1000, tz=timezone.utc)
CLOSE_CLOID = "0x" + "4" * 32
COMMAND_ID = "command-unknown-1"
INCIDENT_ID = "incident-account-safety-1"
ACTION_HASH = hashlib.sha256(b"original-action").hexdigest()
WIRE_HASH = hashlib.sha256(b"original-wire").hexdigest()
PREFLIGHT_HASH = hashlib.sha256(b"dispatch-preflight").hexdigest()
SIGNED_EVIDENCE_HASH = hashlib.sha256(b"signed-evidence").hexdigest()
TRANSPORT_EVIDENCE_HASH = hashlib.sha256(b"transport-evidence").hexdigest()


def snapshot(*, short: bool = False, positions: bool = True, network: str = "testnet"):
    if not positions:
        clearing = valid_clearing(positions=[])
    elif short:
        clearing = valid_clearing(positions=[raw_position(signed_size="-0.5000")])
    else:
        clearing = valid_clearing()
    result, _ = fetch_account(
        FixtureTransport(clearing=clearing),
        received_at_ms=RECOVERY_NOW_MS,
        network=network,
    )
    return result


def incident(
    *,
    command_id: str | None = COMMAND_ID,
    state: str = "open",
    incident_id: str = INCIDENT_ID,
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=incident_id,
        command_id=command_id,
        code="account_safety_recovery",
        severity="critical",
        state=state,
        opened_at=RECOVERY_NOW - timedelta(seconds=1),
        updated_at=RECOVERY_NOW,
        revision=1,
        details={},
    )


def attempt(
    *,
    state: str = "unknown",
    response_hash: str | None = None,
    nonce: int = RECOVERY_NOW_MS - 100,
    command_id: str = COMMAND_ID,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id="attempt-unknown-1",
        command_id=command_id,
        worker_id="worker-1",
        fencing_token=1,
        preflight_hash=PREFLIGHT_HASH,
        signed_evidence_hash=SIGNED_EVIDENCE_HASH,
        transport_evidence_hash=TRANSPORT_EVIDENCE_HASH,
        nonce=nonce,
        action_hash=ACTION_HASH,
        wire_hash=WIRE_HASH,
        state=state,
        response_hash=response_hash,
        prepared_at=RECOVERY_NOW - timedelta(seconds=2),
        updated_at=RECOVERY_NOW - timedelta(seconds=1),
    )


def recovery_policy(
    *,
    kinds: frozenset[RecoveryKind] = frozenset(
        {
            RecoveryKind.REDUCE_ONLY_CLOSE,
            RecoveryKind.CANCEL_BY_CLOID,
            RecoveryKind.NOOP_FENCE,
        }
    ),
    owned_cloids: frozenset[str] = frozenset(
        {CLOSE_CLOID, STOP_CLOID, TARGET_CLOID}
    ),
    assets: frozenset[int] = frozenset({1}),
    network: HyperliquidNetwork = HyperliquidNetwork.TESTNET,
    allow_mainnet: bool = False,
    account_id: str = "desk-recovery",
) -> SignerPolicy:
    return SignerPolicy(
        accounts=(
            SigningAccount(
                account_id=account_id,
                main_account_address=ACCOUNT,
                signer_address=SIGNER,
                owned_cloids=owned_cloids,
            ),
        ),
        allowed_asset_ids=assets,
        allowed_networks=frozenset({network}),
        allow_mainnet=allow_mainnet,
        allowed_recovery_kinds=kinds,
    )


def build_close(*, short: bool = False, close_size: Decimal | None = None):
    return build_reduce_only_close(
        snapshot(short=short),
        symbol="ETH",
        price_bound=Decimal("2400") if not short else Decimal("2600"),
        close_size=close_size,
        cloid=CLOSE_CLOID,
        incident=incident(),
        account_id="desk-recovery",
        network=HyperliquidNetwork.TESTNET,
        at=RECOVERY_NOW,
    )


def build_cancel():
    return build_cancel_by_cloid(
        snapshot(),
        (CancelRequest("ETH", TARGET_CLOID),),
        owned_cloids=(STOP_CLOID, TARGET_CLOID),
        incident=incident(),
        account_id="desk-recovery",
        network=HyperliquidNetwork.TESTNET,
        at=RECOVERY_NOW,
    )


def build_noop(*, selected_attempt: AttemptRecord | None = None):
    return build_noop_fence(
        attempt() if selected_attempt is None else selected_attempt,
        incident=incident(),
        account_id="desk-recovery",
        main_account_address=ACCOUNT,
        network=HyperliquidNetwork.TESTNET,
        at=RECOVERY_NOW,
    )


def close_material(value, wire_action: dict[str, object]) -> dict[str, object]:
    return {
        "kind": RecoveryKind.REDUCE_ONLY_CLOSE.value,
        "network": value.network.value,
        "account_id": value.account_id,
        "main_account_address": value.main_account_address,
        "incident_id": value.incident_id,
        "position_snapshot_hash": value.position_snapshot_hash,
        "symbol": value.symbol,
        "asset_id": value.asset_id,
        "original_signed_position": canonical_decimal(value.original_signed_position),
        "close_size": canonical_decimal(value.close_size),
        "price_bound": canonical_decimal(value.price_bound),
        "cloid": value.cloid,
        "expires_at_ms": value.expires_at_ms,
        "action": wire_action,
    }


def rebind_close(value, *, wire_action=None, close_size=None):
    selected_action = deepcopy(value.action) if wire_action is None else wire_action
    selected_size = value.close_size if close_size is None else close_size
    provisional = replace(value, action=selected_action, close_size=selected_size)
    material = close_material(provisional, selected_action)
    return replace(
        provisional,
        recovery_hash=domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material),
    )


def rebind_cancel(value, *, requests, asset_ids, wire_action):
    provisional = replace(
        value,
        requests=tuple(requests),
        asset_ids=tuple(asset_ids),
        action=wire_action,
    )
    material = {
        "kind": RecoveryKind.CANCEL_BY_CLOID.value,
        "network": provisional.network.value,
        "account_id": provisional.account_id,
        "main_account_address": provisional.main_account_address,
        "incident_id": provisional.incident_id,
        "account_snapshot_hash": provisional.account_snapshot_hash,
        "requests": [
            {
                "symbol": request.symbol,
                "asset_id": asset_id,
                "cloid": request.cloid,
            }
            for request, asset_id in zip(
                provisional.requests,
                provisional.asset_ids,
            )
        ],
        "expires_at_ms": provisional.expires_at_ms,
        "action": wire_action,
    }
    return replace(
        provisional,
        recovery_hash=domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material),
    )


class RecoveryConstructionTests(unittest.TestCase):
    def test_exact_material_round_trips_every_recovery_hash(self) -> None:
        for recovery in (build_close(), build_cancel(), build_noop()):
            with self.subTest(kind=recovery.kind):
                material = recovery_action_material(recovery)
                self.assertEqual(
                    domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material),
                    recovery.recovery_hash,
                )
                self.assertIsNot(material["action"], recovery.action)
                self.assertEqual(
                    recovery,
                    recovery_action_from_material(material),
                )

        malformed = recovery_action_material(build_close())
        malformed["unexpected"] = True
        with self.assertRaisesRegex(ValidationError, "fields"):
            recovery_action_from_material(malformed)
        noncanonical = recovery_action_material(build_close())
        noncanonical["close_size"] = "0.500"
        with self.assertRaisesRegex(ValidationError, "canonical"):
            recovery_action_from_material(noncanonical)

    def test_long_full_and_partial_residual_closes_are_bounded_reduce_only_ioc(self) -> None:
        full = build_close()
        residual = build_close(close_size=Decimal("0.2"))

        self.assertIs(full.kind, RecoveryKind.REDUCE_ONLY_CLOSE)
        self.assertEqual(full.original_signed_position, Decimal("0.5"))
        self.assertEqual(full.close_size, Decimal("0.5"))
        order = full.action["orders"][0]  # type: ignore[index]
        self.assertFalse(order["b"])
        self.assertTrue(order["r"])
        self.assertEqual(order["s"], "0.5")
        self.assertEqual(order["t"], {"limit": {"tif": "Ioc"}})
        self.assertEqual(full.action["grouping"], "na")
        self.assertEqual(residual.close_size, Decimal("0.2"))
        self.assertEqual(residual.action["orders"][0]["s"], "0.2")  # type: ignore[index]
        self.assertRegex(full.recovery_hash, r"^[0-9a-f]{64}$")

    def test_short_and_new_residual_snapshot_derive_buy_side_without_caller_choice(self) -> None:
        short = build_close(short=True)
        residual_snapshot, _ = fetch_account(
            FixtureTransport(
                clearing=valid_clearing(
                    positions=[raw_position(signed_size="-0.075")]
                )
            ),
            received_at_ms=RECOVERY_NOW_MS,
            network="testnet",
        )
        residual = build_reduce_only_close(
            residual_snapshot,
            symbol="ETH",
            price_bound=Decimal("2600"),
            cloid=CLOSE_CLOID,
            incident=incident(),
            account_id="desk-recovery",
            network=HyperliquidNetwork.TESTNET,
            at=RECOVERY_NOW,
        )

        self.assertTrue(short.action["orders"][0]["b"])  # type: ignore[index]
        self.assertEqual(residual.close_size, Decimal("0.075"))
        self.assertEqual(residual.action["orders"][0]["s"], "0.075")  # type: ignore[index]

    def test_absent_stale_zero_float_and_oversize_close_fail_closed(self) -> None:
        cases = (
            {"snapshot": snapshot(positions=False)},
            {"snapshot": snapshot(), "at": RECOVERY_NOW + timedelta(seconds=6)},
            {"snapshot": snapshot(), "close_size": Decimal("0")},
            {"snapshot": snapshot(), "close_size": Decimal("0.5001")},
            {"snapshot": snapshot(), "close_size": 0.2},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                arguments = {
                    "snapshot": snapshot(),
                    "symbol": "ETH",
                    "price_bound": Decimal("2400"),
                    "cloid": CLOSE_CLOID,
                    "incident": incident(),
                    "account_id": "desk-recovery",
                    "network": HyperliquidNetwork.TESTNET,
                    "at": RECOVERY_NOW,
                }
                arguments.update(changes)
                with self.assertRaises((TypeError, ValidationError)):
                    build_reduce_only_close(**arguments)  # type: ignore[arg-type]

    def test_cancel_by_cloid_contains_only_owned_exact_requests(self) -> None:
        recovery = build_cancel()

        self.assertIs(recovery.kind, RecoveryKind.CANCEL_BY_CLOID)
        self.assertEqual(recovery.asset_ids, (1,))
        self.assertEqual(
            recovery.action,
            {
                "type": "cancelByCloid",
                "cancels": [{"asset": 1, "cloid": TARGET_CLOID}],
            },
        )

        with self.assertRaisesRegex(ValidationError, "protective stop"):
            build_cancel_by_cloid(
                snapshot(),
                (CancelRequest("ETH", STOP_CLOID),),
                owned_cloids=(STOP_CLOID,),
                incident=incident(),
                account_id="desk-recovery",
                network=HyperliquidNetwork.TESTNET,
                at=RECOVERY_NOW,
            )

        flat_cleanup = build_cancel_by_cloid(
            snapshot(positions=False),
            (CancelRequest("ETH", STOP_CLOID),),
            owned_cloids=(STOP_CLOID,),
            incident=incident(),
            account_id="desk-recovery",
            network=HyperliquidNetwork.TESTNET,
            at=RECOVERY_NOW,
        )
        self.assertEqual(
            flat_cleanup.action,
            {
                "type": "cancelByCloid",
                "cancels": [{"asset": 1, "cloid": STOP_CLOID}],
            },
        )

        with self.assertRaisesRegex(ValidationError, "fresh snapshot"):
            build_cancel_by_cloid(
                snapshot(),
                (CancelRequest("ETH", CLOSE_CLOID),),
                owned_cloids=(CLOSE_CLOID,),
                incident=incident(),
                account_id="desk-recovery",
                network=HyperliquidNetwork.TESTNET,
                at=RECOVERY_NOW,
            )

        with self.assertRaisesRegex(ValidationError, "owned"):
            build_cancel_by_cloid(
                snapshot(),
                (CancelRequest("ETH", STOP_CLOID),),
                owned_cloids=(TARGET_CLOID,),
                incident=incident(),
                account_id="desk-recovery",
                network=HyperliquidNetwork.TESTNET,
                at=RECOVERY_NOW,
            )
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            build_cancel_by_cloid(
                snapshot(),
                (
                    CancelRequest("ETH", STOP_CLOID),
                    CancelRequest("ETH", STOP_CLOID),
                ),
                owned_cloids=(STOP_CLOID,),
                incident=incident(),
                account_id="desk-recovery",
                network=HyperliquidNetwork.TESTNET,
                at=RECOVERY_NOW,
            )

    def test_noop_binds_exact_persisted_unknown_attempt_and_original_nonce(self) -> None:
        source = attempt()
        recovery = build_noop(selected_attempt=source)

        self.assertIs(recovery.kind, RecoveryKind.NOOP_FENCE)
        self.assertEqual(recovery.action, {"type": "noop"})
        self.assertEqual(recovery.original_nonce, source.nonce)
        self.assertEqual(recovery.preflight_hash, source.preflight_hash)
        self.assertEqual(recovery.signed_evidence_hash, source.signed_evidence_hash)
        self.assertEqual(
            recovery.transport_evidence_hash,
            source.transport_evidence_hash,
        )
        self.assertEqual(recovery.original_action_hash, source.action_hash)
        self.assertEqual(recovery.original_wire_hash, source.wire_hash)
        self.assertEqual(recovery.ambiguous_attempt_hash, ambiguous_attempt_hash(source))
        self.assertRegex(recovery.recovery_hash, r"^[0-9a-f]{64}$")

    def test_noop_rejects_nonunknown_response_and_incident_mismatch(self) -> None:
        cases = (
            (attempt(state="prepared"), incident()),
            (attempt(state="response_received", response_hash=ACTION_HASH), incident()),
            (attempt(state="unknown", response_hash=ACTION_HASH), incident()),
            (attempt(), incident(command_id="different")),
            (attempt(), incident(state="closed")),
        )
        for source, selected_incident in cases:
            with self.subTest(source=source, incident=selected_incident):
                with self.assertRaises(ValidationError):
                    build_noop_fence(
                        source,
                        incident=selected_incident,
                        account_id="desk-recovery",
                        main_account_address=ACCOUNT,
                        network=HyperliquidNetwork.TESTNET,
                        at=RECOVERY_NOW,
                    )
        with self.assertRaises(TypeError):
            build_noop_fence(  # type: ignore[arg-type]
                object(),
                incident=incident(),
                account_id="desk-recovery",
                main_account_address=ACCOUNT,
                network=HyperliquidNetwork.TESTNET,
                at=RECOVERY_NOW,
            )


class RecoverySignerTests(unittest.TestCase):
    def test_public_recovery_signing_requires_durable_store_authority(self) -> None:
        recovery = build_close()
        events: list[str] = []

        self.assertTrue(RECOVERY_SIGNING_ENABLED)
        self.assertTrue(RECOVERY_SUBMISSION_ENABLED)
        self.assertTrue(NOOP_FENCE_SUBMISSION_ENABLED)
        with self.assertRaises(TypeError):
            public_sign_recovery_action(
                recovery,
                evidence=snapshot(),
                policy=recovery_policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
                clock=lambda: RECOVERY_NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_close_and_cancel_allocate_then_sign_once(self) -> None:
        for recovery, evidence in (
            (build_close(), snapshot()),
            (build_cancel(), snapshot()),
        ):
            with self.subTest(kind=recovery.kind):
                events: list[str] = []
                signer = FakeSigner(events)
                signed = sign_recovery_action(
                    recovery,
                    evidence=evidence,
                    incident=incident(),
                    policy=recovery_policy(),
                    wallet=FakeWallet(),
                    nonce_allocator=FakeNonceAllocator(
                        events,
                        RECOVERY_NOW_MS + 1,
                    ),
                    clock=lambda: RECOVERY_NOW,
                    sign_l1_action=signer,
                )

                self.assertEqual(events, ["nonce_committed", "signed"])
                self.assertEqual(len(signer.calls), 1)
                self.assertEqual(signer.calls[0][1], recovery.action)
                self.assertEqual(signed.incident_id, INCIDENT_ID)
                self.assertEqual(signed.recovery_kind, recovery.kind)
                self.assertEqual(signed.recovery_hash, recovery.recovery_hash)
                signed.verify_integrity()
                json.dumps(signed.as_dict(), allow_nan=False, sort_keys=True)

    def test_wrong_side_reduce_flag_and_oversize_are_rejected_with_valid_hash(self) -> None:
        base = build_close()
        mutations = []

        wrong_side = deepcopy(base.action)
        wrong_side["orders"][0]["b"] = True  # type: ignore[index]
        mutations.append(rebind_close(base, wire_action=wrong_side))

        non_reduce = deepcopy(base.action)
        non_reduce["orders"][0]["r"] = False  # type: ignore[index]
        mutations.append(rebind_close(base, wire_action=non_reduce))

        oversize_action = deepcopy(base.action)
        oversize_action["orders"][0]["s"] = "0.6"  # type: ignore[index]
        mutations.append(
            rebind_close(
                base,
                wire_action=oversize_action,
                close_size=Decimal("0.6"),
            )
        )

        for recovery in mutations:
            with self.subTest(recovery=recovery):
                events: list[str] = []
                with self.assertRaises(SignerPolicyError):
                    sign_recovery_action(
                        recovery,
                        evidence=snapshot(),
                        incident=incident(),
                        policy=recovery_policy(),
                        wallet=FakeWallet(),
                        nonce_allocator=FakeNonceAllocator(
                            events,
                            RECOVERY_NOW_MS + 1,
                        ),
                        clock=lambda: RECOVERY_NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])

    def test_cancel_asset_mapping_is_revalidated_against_fresh_metadata(self) -> None:
        base = build_cancel()
        wire_action = deepcopy(base.action)
        for item in wire_action["cancels"]:  # type: ignore[index]
            item["asset"] = 0
        rebound = rebind_cancel(
            base,
            requests=base.requests,
            asset_ids=(0,),
            wire_action=wire_action,
        )
        events: list[str] = []

        with self.assertRaisesRegex(SignerPolicyError, "metadata"):
            sign_recovery_action(
                rebound,
                evidence=snapshot(),
                incident=incident(),
                policy=recovery_policy(assets=frozenset({0})),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
                clock=lambda: RECOVERY_NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_cancel_signer_rejects_rebound_live_protective_stop(self) -> None:
        base = build_cancel()
        wire_action = {
            "type": "cancelByCloid",
            "cancels": [{"asset": 1, "cloid": STOP_CLOID}],
        }
        rebound = rebind_cancel(
            base,
            requests=(CancelRequest("ETH", STOP_CLOID),),
            asset_ids=(1,),
            wire_action=wire_action,
        )
        events: list[str] = []

        with self.assertRaisesRegex(SignerPolicyError, "protective"):
            sign_recovery_action(
                rebound,
                evidence=snapshot(),
                incident=incident(),
                policy=recovery_policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
                clock=lambda: RECOVERY_NOW,
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_foreign_cloid_asset_kind_and_stale_evidence_fail_before_nonce(self) -> None:
        cases = (
            (
                build_close(),
                snapshot(),
                recovery_policy(owned_cloids=frozenset({STOP_CLOID, TARGET_CLOID})),
            ),
            (
                build_close(),
                snapshot(),
                recovery_policy(assets=frozenset({0})),
            ),
            (
                build_close(),
                snapshot(),
                recovery_policy(kinds=frozenset({RecoveryKind.CANCEL_BY_CLOID})),
            ),
        )
        for recovery, evidence, selected_policy in cases:
            with self.subTest(policy=selected_policy):
                events: list[str] = []
                with self.assertRaises(SignerPolicyError):
                    sign_recovery_action(
                        recovery,
                        evidence=evidence,
                        incident=incident(),
                        policy=selected_policy,
                        wallet=FakeWallet(),
                        nonce_allocator=FakeNonceAllocator(
                            events,
                            RECOVERY_NOW_MS + 1,
                        ),
                        clock=lambda: RECOVERY_NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])

        events = []
        with self.assertRaisesRegex(SignerPolicyError, "stale"):
            sign_recovery_action(
                build_close(),
                evidence=snapshot(),
                incident=incident(),
                policy=recovery_policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 6_001),
                clock=lambda: RECOVERY_NOW + timedelta(seconds=6),
                sign_l1_action=FakeSigner(events),
            )
        self.assertEqual(events, [])

    def test_noop_uses_same_original_nonce_and_forbids_allocator_or_wrong_evidence(self) -> None:
        source = attempt()
        recovery = build_noop(selected_attempt=source)
        events: list[str] = []
        signer = FakeSigner(events)
        signed = sign_recovery_action(
            recovery,
            evidence=source,
            incident=incident(),
            policy=recovery_policy(),
            wallet=FakeWallet(),
            nonce_allocator=None,
            clock=lambda: RECOVERY_NOW,
            sign_l1_action=signer,
        )

        self.assertEqual(events, ["signed"])
        self.assertEqual(signed.nonce, source.nonce)
        self.assertEqual(signer.calls[0][3], source.nonce)
        self.assertEqual(signed.envelope()["action"], {"type": "noop"})

        allocation_events: list[str] = []
        with self.assertRaisesRegex(SignerPolicyError, "must not allocate"):
            sign_recovery_action(
                recovery,
                evidence=source,
                incident=incident(),
                policy=recovery_policy(),
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(allocation_events),
                clock=lambda: RECOVERY_NOW,
                sign_l1_action=FakeSigner(allocation_events),
            )
        self.assertEqual(allocation_events, [])

        with self.assertRaisesRegex(SignerPolicyError, "differs"):
            sign_recovery_action(
                recovery,
                evidence=replace(source, wire_hash="0" * 64),
                incident=incident(),
                policy=recovery_policy(),
                wallet=FakeWallet(),
                nonce_allocator=None,
                clock=lambda: RECOVERY_NOW,
                sign_l1_action=FakeSigner([]),
            )

    def test_noop_rejects_original_nonce_outside_venue_window(self) -> None:
        old = attempt(nonce=RECOVERY_NOW_MS - 2 * 86_400_000)
        future = attempt(nonce=RECOVERY_NOW_MS + 86_400_000)

        for source in (old, future):
            with self.subTest(source=source):
                recovery = build_noop(selected_attempt=source)
                events: list[str] = []
                with self.assertRaisesRegex(SignerPolicyError, "time window"):
                    sign_recovery_action(
                        recovery,
                        evidence=source,
                        incident=incident(),
                        policy=recovery_policy(),
                        wallet=FakeWallet(),
                        nonce_allocator=None,
                        clock=lambda: RECOVERY_NOW,
                        sign_l1_action=FakeSigner(events),
                    )
                self.assertEqual(events, [])

    def test_recovery_mainnet_is_hard_disabled(self) -> None:
        with self.assertRaises(SignerPolicyError):
            recovery_policy(network=HyperliquidNetwork.MAINNET)
        with self.assertRaises(SignerPolicyError):
            recovery_policy(
                network=HyperliquidNetwork.MAINNET,
                allow_mainnet=True,
                account_id="desk-mainnet",
            )


class RecoveryTransportTests(unittest.TestCase):
    def test_internal_recovery_envelope_lacks_public_store_authority(self) -> None:
        recovery = build_close()
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            evidence=snapshot(),
            incident=incident(),
            policy=recovery_policy(),
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
            clock=lambda: RECOVERY_NOW,
            sign_l1_action=FakeSigner(events),
        )
        calls: list[tuple[str, bytes, float]] = []

        def sender(endpoint: str, body: bytes, timeout: float):
            calls.append((endpoint, body, timeout))
            return HttpExchangeResponse(200, endpoint, b'{"status":"ok","response":{"type":"default"}}')

        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            with self.assertRaisesRegex(HyperliquidSubmissionError, "ExecutionStore"):
                submit_signed_action(signed, clock=lambda: RECOVERY_NOW)

        self.assertEqual(calls, [])

    def test_missing_noop_store_authority_precedes_timeout_sender(self) -> None:
        source = attempt()
        signed = sign_recovery_action(
            build_noop(selected_attempt=source),
            evidence=source,
            incident=incident(),
            policy=recovery_policy(),
            wallet=FakeWallet(),
            nonce_allocator=None,
            clock=lambda: RECOVERY_NOW,
            sign_l1_action=FakeSigner([]),
        )
        calls = 0

        def timeout(endpoint: str, body: bytes, seconds: float):
            nonlocal calls
            del endpoint, body, seconds
            calls += 1
            raise TimeoutError("unknown")

        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=timeout,
        ):
            with self.assertRaisesRegex(HyperliquidSubmissionError, "ExecutionStore"):
                submit_signed_action(signed, clock=lambda: RECOVERY_NOW)

        self.assertEqual(calls, 0)

    def test_recovery_incident_rebinding_breaks_integrity_before_transport(self) -> None:
        recovery = build_close()
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            evidence=snapshot(),
            incident=incident(),
            policy=recovery_policy(),
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
            clock=lambda: RECOVERY_NOW,
            sign_l1_action=FakeSigner(events),
        )
        rebound = replace(signed, incident_id="different-incident")
        calls = 0

        def sender(endpoint: str, body: bytes, timeout: float):
            nonlocal calls
            del endpoint, body, timeout
            calls += 1
            raise AssertionError("must not send")

        with self.assertRaisesRegex(SignerOutputError, "incident|binding"):
            rebound.verify_integrity()
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            with self.assertRaisesRegex(HyperliquidSubmissionError, "integrity"):
                submit_signed_action(rebound, clock=lambda: RECOVERY_NOW)
        self.assertEqual(calls, 0)

    def test_expired_recovery_is_refused_before_transport(self) -> None:
        recovery = build_close()
        events: list[str] = []
        signed = sign_recovery_action(
            recovery,
            evidence=snapshot(),
            incident=incident(),
            policy=recovery_policy(),
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(events, RECOVERY_NOW_MS + 1),
            clock=lambda: RECOVERY_NOW,
            sign_l1_action=FakeSigner(events),
        )
        calls = 0

        def sender(endpoint: str, body: bytes, timeout: float):
            nonlocal calls
            del endpoint, body, timeout
            calls += 1
            raise AssertionError("must not send")

        expired = replace(signed, signed_at_ms=signed.expires_after_ms)
        with self.assertRaisesRegex(SignerOutputError, "expiry"):
            expired.verify_integrity()
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            with self.assertRaisesRegex(HyperliquidSubmissionError, "integrity"):
                submit_signed_action(
                    expired,
                    clock=lambda: RECOVERY_NOW + timedelta(seconds=10),
                )
        self.assertEqual(calls, 0)


class OfficialRecoverySigningTests(unittest.TestCase):
    @unittest.skipUnless(
        official_sdk_available(),
        f"requires optional hyperliquid-python-sdk=={OFFICIAL_SDK_VERSION}",
    )
    def test_official_sdk_recovers_signer_from_close_cancel_and_noop(self) -> None:
        from eth_account import Account
        from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

        wallet = Account.from_key(
            "0x0123456789012345678901234567890123456789012345678901234567890123"
        )
        selected_policy = SignerPolicy(
            accounts=(
                SigningAccount(
                    account_id="desk-recovery",
                    main_account_address=ACCOUNT,
                    signer_address=wallet.address.lower(),
                    owned_cloids=frozenset(
                        {CLOSE_CLOID, STOP_CLOID, TARGET_CLOID}
                    ),
                ),
            ),
            allowed_asset_ids=frozenset({1}),
            allowed_recovery_kinds=frozenset(RecoveryKind),
        )
        source = attempt()
        values = (
            (build_close(), snapshot(), FakeNonceAllocator([], RECOVERY_NOW_MS + 1)),
            (build_cancel(), snapshot(), FakeNonceAllocator([], RECOVERY_NOW_MS + 2)),
            (build_noop(selected_attempt=source), source, None),
        )
        for recovery, evidence, allocator in values:
            with self.subTest(kind=recovery.kind):
                signed = sign_recovery_action(
                    recovery,
                    evidence=evidence,
                    incident=incident(),
                    policy=selected_policy,
                    wallet=wallet,
                    nonce_allocator=allocator,
                    clock=lambda: RECOVERY_NOW,
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


if __name__ == "__main__":
    unittest.main()
