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
from trading_harness.execution_store import DispatchPreflight
from trading_harness.hyperliquid_signer import (
    OFFICIAL_SDK_VERSION,
    MAX_PROTECTED_NOTIONAL,
    MAX_PROTECTED_QUANTITY,
    SignerDependencyError,
    SignerOutputError,
    SignerPolicy,
    SignerPolicyError,
    SigningAccount,
    load_official_sign_l1_action,
    official_sdk_available,
    sign_protected_action as _sign_protected_action,
)
from trading_harness.hyperliquid_wire import (
    HyperliquidNetwork,
    ProtectedOrderAction,
    build_protected_order_action,
)
from trading_harness.nonce import PersistentNonceAllocator
from trading_harness.planning import ProtectedTradePlan
from tests.test_execution_store import digest
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
