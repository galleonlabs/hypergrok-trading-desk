"""Isolated, fail-closed Hyperliquid signing boundary.

The boundary accepts a reviewed three-leg protected order or one narrowly
typed account-safety recovery backed by a consumed durable store authority.
It independently revalidates compact wire shape and field insertion order,
binds explicit network/account/asset policy, commits fresh nonces through an
injected durable allocator, and only then calls a signing function.

No private-key, environment-variable, or file loader exists here.  The wallet
object is injected by the isolated process.  The official SDK integration is
lazy and accepts exactly ``hyperliquid-python-sdk==0.24.0``; tests and offline
development may inject a signature-compatible function without installing the
SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, localcontext
import hashlib
from importlib import metadata as importlib_metadata
import json
import re
from typing import Any, Protocol, TypeAlias

from .canonical import canonical_decimal, canonical_json, domain_hash, validate_decimal_bounds
from .errors import HarnessError, ValidationError
from .domain import Environment
from .execution_store import (
    AttemptRecord,
    DispatchPreflight,
    ExecutionStore,
    IncidentRecord,
    RecoveryCommand,
    RecoverySigningAuthority,
    SignedEnvelopeEvidence,
    SignedRecoveryEvidence,
)
from .hyperliquid_account import HyperliquidAccountSnapshot
from .hyperliquid_recovery import (
    RECOVERY_ACTION_HASH_DOMAIN,
    CancelByCloidAction,
    NoopFenceAction,
    RecoveryAction,
    RecoveryKind,
    ReduceOnlyCloseAction,
    ambiguous_attempt_hash,
    derive_recovery_close_cloid,
    recovery_action_material,
)
from .hyperliquid_wire import (
    HyperliquidNetwork,
    PerpInstrumentMetadata,
    ProtectedOrderAction,
    build_protected_order_action,
)
from .planning import ProtectedTradePlan, protected_trade_plan_from_dict


OFFICIAL_SDK_DISTRIBUTION = "hyperliquid-python-sdk"
OFFICIAL_SDK_VERSION = "0.24.0"
SIGNED_ENVELOPE_HASH_DOMAIN = "trading-harness/hyperliquid-signed-envelope/v1"
SIGNATURE_HASH_DOMAIN = "trading-harness/hyperliquid-signature/v1"
SIGNER_BINDING_HASH_DOMAIN = "trading-harness/hyperliquid-signer-binding/v1"

_ACTION_HASH_DOMAIN = "trading-harness/hyperliquid-action/v1"
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_WALLET_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# The official SDK encodes r/s with ``hex(int)`` and therefore omits leading
# zero nibbles.  Accept one to 64 lowercase hex digits, but reject zero and
# non-canonical leading zeroes.
_SIGNATURE_COMPONENT_RE = re.compile(r"^0x[1-9a-f][0-9a-f]{0,63}$")
_MAX_EXPIRY_HORIZON_MS = 15_000
_NONCE_PAST_WINDOW_MS = 2 * 86_400_000
_NONCE_FUTURE_WINDOW_MS = 86_400_000
_ZERO = Decimal("0")
_SIGNER_CONTEXT = Context(prec=256)
MAX_PROTECTED_QUANTITY = Decimal("1000")
MAX_PROTECTED_NOTIONAL = Decimal("100000")
RECOVERY_SIGNING_ENABLED = True
RECOVERY_SAFETY_POLICY_HASH_DOMAIN = (
    "trading-harness/hyperliquid-recovery-safety-policy/v1"
)
RECOVERY_WIRE_ACTION_HASH_DOMAIN = (
    "trading-harness/hyperliquid-recovery-wire-action/v1"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


SignL1Action: TypeAlias = Callable[
    [object, dict[str, object], str | None, int, int | None, bool],
    object,
]
Clock: TypeAlias = Callable[[], datetime]


class NonceAllocator(Protocol):
    """The narrow interface supplied by ``PersistentNonceAllocator``."""

    def allocate(self) -> int:
        """Commit and return one nonce before signing starts."""


class HyperliquidSignerError(HarnessError):
    """Base class for isolated signing-boundary failures."""


class SignerPolicyError(HyperliquidSignerError, ValueError):
    """The requested action is outside the explicit signer policy."""


class SignerDependencyError(HyperliquidSignerError):
    """The pinned official SDK signing function is unavailable."""


class SignerOutputError(HyperliquidSignerError, ValueError):
    """A signing implementation returned a malformed signature."""


def _reject_json_float(value: str) -> object:
    del value
    raise ValueError("JSON floats are unsupported")


def _reject_json_constant(value: str) -> object:
    del value
    raise ValueError("non-finite JSON values are unsupported")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _frozen_json_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        raise SignerOutputError(f"{field} is not JSON text")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise SignerOutputError(f"{field} is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise SignerOutputError(f"{field} is not an object")
    return parsed


def _text(value: object, field: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise SignerPolicyError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise SignerPolicyError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _address(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise SignerPolicyError(f"{field} must be a lowercase 20-byte address")
    return value


def _wallet_address(wallet: object) -> str:
    try:
        value = getattr(wallet, "address")
    except Exception as error:
        raise SignerPolicyError(
            f"wallet address lookup failed: {type(error).__name__}"
        ) from error
    if not isinstance(value, str) or not _WALLET_ADDRESS_RE.fullmatch(value):
        raise SignerPolicyError("wallet must expose a valid public address")
    return value.lower()


def _utc_ms(clock: Clock) -> int:
    try:
        value = clock()
    except Exception as error:
        raise ValidationError(f"signer clock failed: {type(error).__name__}") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("signer clock must return a timezone-aware datetime")
    utc = value.astimezone(timezone.utc)
    delta = utc - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError("signer clock predates the Unix epoch")
    return result


@dataclass(frozen=True, slots=True)
class SigningAccount:
    """One reviewed logical account, API-wallet signer, and optional vault."""

    account_id: str
    main_account_address: str
    signer_address: str
    vault_address: str | None = None
    owned_cloids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(
            self,
            "main_account_address",
            _address(self.main_account_address, "main_account_address"),
        )
        object.__setattr__(
            self,
            "signer_address",
            _address(self.signer_address, "signer_address"),
        )
        if self.main_account_address == self.signer_address:
            raise SignerPolicyError("isolated API-wallet signer must differ from main account")
        if self.vault_address is not None:
            object.__setattr__(
                self,
                "vault_address",
                _address(self.vault_address, "vault_address"),
            )
            if self.vault_address == self.main_account_address:
                raise SignerPolicyError("vault_address must differ from main account")
            if self.vault_address == self.signer_address:
                raise SignerPolicyError("vault_address must differ from API-wallet signer")
        owned = frozenset(self.owned_cloids)
        if any(not isinstance(value, str) or not _CLOID_RE.fullmatch(value) for value in owned):
            raise SignerPolicyError("owned_cloids contains an invalid CLOID")
        object.__setattr__(self, "owned_cloids", owned)


@dataclass(frozen=True, slots=True)
class SignerPolicy:
    """Closed signer allowlists with mainnet compiled off."""

    accounts: tuple[SigningAccount, ...]
    allowed_asset_ids: frozenset[int]
    allowed_networks: frozenset[HyperliquidNetwork] = frozenset(
        {HyperliquidNetwork.TESTNET}
    )
    allow_mainnet: bool = False
    minimum_expiry_remaining_ms: int = 1_000
    maximum_expiry_horizon_ms: int = _MAX_EXPIRY_HORIZON_MS
    allowed_recovery_kinds: frozenset[RecoveryKind] = frozenset()

    def __post_init__(self) -> None:
        accounts = tuple(self.accounts)
        if not accounts or any(not isinstance(item, SigningAccount) for item in accounts):
            raise SignerPolicyError("accounts must contain reviewed SigningAccount values")
        if len({item.account_id for item in accounts}) != len(accounts):
            raise SignerPolicyError("account allowlist contains duplicate account IDs")
        if len({item.signer_address for item in accounts}) != len(accounts):
            raise SignerPolicyError(
                "each allowlisted account requires a dedicated API-wallet signer"
            )
        object.__setattr__(self, "accounts", accounts)

        assets = frozenset(self.allowed_asset_ids)
        if not assets:
            raise SignerPolicyError("asset allowlist must not be empty")
        if any(type(asset) is not int or not 0 <= asset <= 1_000_000 for asset in assets):
            raise SignerPolicyError("asset allowlist contains an invalid asset ID")
        object.__setattr__(self, "allowed_asset_ids", assets)

        try:
            networks = frozenset(
                value
                if isinstance(value, HyperliquidNetwork)
                else HyperliquidNetwork(value)
                for value in self.allowed_networks
            )
        except (TypeError, ValueError) as error:
            raise SignerPolicyError("network allowlist is invalid") from error
        if not networks:
            raise SignerPolicyError("network allowlist must not be empty")
        if type(self.allow_mainnet) is not bool:
            raise SignerPolicyError("allow_mainnet must be boolean")
        if HyperliquidNetwork.MAINNET in networks or self.allow_mainnet:
            raise SignerPolicyError("mainnet signing is hard-disabled in this build")
        object.__setattr__(self, "allowed_networks", networks)

        try:
            recovery_kinds = frozenset(
                value if isinstance(value, RecoveryKind) else RecoveryKind(value)
                for value in self.allowed_recovery_kinds
            )
        except (TypeError, ValueError) as error:
            raise SignerPolicyError("recovery action allowlist is invalid") from error
        object.__setattr__(self, "allowed_recovery_kinds", recovery_kinds)

        for field, value in (
            ("minimum_expiry_remaining_ms", self.minimum_expiry_remaining_ms),
            ("maximum_expiry_horizon_ms", self.maximum_expiry_horizon_ms),
        ):
            if type(value) is not int or value <= 0:
                raise SignerPolicyError(f"{field} must be a positive integer")
        if not (
            self.minimum_expiry_remaining_ms
            <= self.maximum_expiry_horizon_ms
            <= _MAX_EXPIRY_HORIZON_MS
        ):
            raise SignerPolicyError("expiry policy exceeds the compiled 15-second bound")

    def account(self, account_id: str) -> SigningAccount:
        matches = [item for item in self.accounts if item.account_id == account_id]
        if len(matches) != 1:
            raise SignerPolicyError("protected action account is not allowlisted")
        return matches[0]

    @property
    def safety_policy_hash(self) -> str:
        accounts = [
            {
                "account_id": item.account_id,
                "main_account_address": item.main_account_address,
                "signer_address": item.signer_address,
                "vault_address": item.vault_address,
                "owned_cloids": sorted(item.owned_cloids),
            }
            for item in sorted(self.accounts, key=lambda value: value.account_id)
        ]
        return domain_hash(
            RECOVERY_SAFETY_POLICY_HASH_DOMAIN,
            {
                "schema_version": "hyperliquid.recovery_safety_policy.v1",
                "accounts": accounts,
                "allowed_asset_ids": sorted(self.allowed_asset_ids),
                "allowed_networks": sorted(
                    value.value for value in self.allowed_networks
                ),
                "allowed_recovery_kinds": sorted(
                    value.value for value in self.allowed_recovery_kinds
                ),
                "minimum_expiry_remaining_ms": self.minimum_expiry_remaining_ms,
                "maximum_expiry_horizon_ms": self.maximum_expiry_horizon_ms,
                "mainnet_enabled": False,
            },
        )


@dataclass(frozen=True, slots=True)
class Signature:
    r: str
    s: str
    v: int

    def as_dict(self) -> dict[str, object]:
        return {"r": self.r, "s": self.s, "v": self.v}


@dataclass(frozen=True, slots=True)
class SignedActionEnvelope:
    """Immutable signed wire bytes and their complete audit binding."""

    network: HyperliquidNetwork
    account_id: str
    main_account_address: str
    signer_address: str
    vault_address: str | None
    plan_hash: str
    metadata_hash: str
    action_hash: str
    preflight_hash: str
    preflight_expires_at_ms: int
    nonce: int
    authorization_expires_at_ms: int
    expires_after_ms: int
    signed_at_ms: int
    signature: Signature
    signature_hash: str
    envelope_hash: str
    signer_binding_hash: str
    wire_json: str
    wire_hash: str
    signing_implementation: str

    @property
    def artifact_kind(self) -> str:
        return "protected_order"

    @property
    def incident_id(self) -> None:
        return None

    @property
    def exchange_url(self) -> str:
        return self.network.exchange_url

    @property
    def wire_bytes(self) -> bytes:
        return self.wire_json.encode("utf-8")

    def envelope(self) -> dict[str, object]:
        return _frozen_json_object(self.wire_json, "signed wire")

    def verify_integrity(self) -> None:
        if not isinstance(self.network, HyperliquidNetwork):
            raise SignerOutputError("signed wire network is invalid")
        if self.network is HyperliquidNetwork.MAINNET:
            raise SignerOutputError("mainnet signed wire is hard-disabled")
        for field, value in (
            ("plan_hash", self.plan_hash),
            ("metadata_hash", self.metadata_hash),
            ("action_hash", self.action_hash),
            ("preflight_hash", self.preflight_hash),
        ):
            if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                raise SignerOutputError(f"signed wire {field} is invalid")
        if not (
            type(self.signed_at_ms) is int
            and type(self.expires_after_ms) is int
            and type(self.authorization_expires_at_ms) is int
            and type(self.preflight_expires_at_ms) is int
            and self.signed_at_ms
            < self.expires_after_ms
            <= self.authorization_expires_at_ms
            and self.expires_after_ms <= self.preflight_expires_at_ms
        ):
            raise SignerOutputError("signed wire expiry ordering is invalid")
        if hashlib.sha256(self.wire_bytes).hexdigest() != self.wire_hash:
            raise SignerOutputError("signed wire hash mismatch")
        envelope = self.envelope()
        if tuple(envelope) != (
            "action",
            "nonce",
            "signature",
            "vaultAddress",
            "expiresAfter",
        ):
            raise SignerOutputError("signed wire field order is unsupported")
        if domain_hash(SIGNED_ENVELOPE_HASH_DOMAIN, envelope) != self.envelope_hash:
            raise SignerOutputError("signed envelope hash mismatch")
        if domain_hash(SIGNATURE_HASH_DOMAIN, self.signature.as_dict()) != self.signature_hash:
            raise SignerOutputError("signature hash mismatch")
        if envelope.get("nonce") != self.nonce:
            raise SignerOutputError("signed wire nonce mismatch")
        if envelope.get("expiresAfter") != self.expires_after_ms:
            raise SignerOutputError("signed wire expiry mismatch")
        if self.expires_after_ms > self.preflight_expires_at_ms:
            raise SignerOutputError("signed wire outlives its dispatch preflight")
        if envelope.get("vaultAddress") != self.vault_address:
            raise SignerOutputError("signed wire vault binding mismatch")
        if envelope.get("signature") != self.signature.as_dict():
            raise SignerOutputError("signed wire signature mismatch")
        raw_signature = envelope.get("signature")
        if not isinstance(raw_signature, dict) or tuple(raw_signature) != ("r", "s", "v"):
            raise SignerOutputError("signed wire signature field order is unsupported")
        action = envelope.get("action")
        if not isinstance(action, dict):
            raise SignerOutputError("signed wire action is invalid")
        try:
            _validated_action(
                ProtectedOrderAction(
                    network=self.network,
                    account_id=self.account_id,
                    plan_hash=self.plan_hash,
                    metadata_hash=self.metadata_hash,
                    expires_at_ms=self.authorization_expires_at_ms,
                    action=action,
                    action_hash=self.action_hash,
                )
            )
        except (TypeError, SignerPolicyError) as error:
            raise SignerOutputError("signed wire action binding is invalid") from error
        binding = {
            "artifact_kind": self.artifact_kind,
            "network": self.network.value,
            "account_id": self.account_id,
            "main_account_address": self.main_account_address,
            "signer_address": self.signer_address,
            "vault_address": self.vault_address,
            "action_hash": self.action_hash,
            "preflight_hash": self.preflight_hash,
            "preflight_expires_at_ms": self.preflight_expires_at_ms,
        }
        if domain_hash(SIGNER_BINDING_HASH_DOMAIN, binding) != self.signer_binding_hash:
            raise SignerOutputError("signed wire signer policy binding mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "hyperliquid.signed_action_envelope.v1",
            "network": self.network.value,
            "exchange_url": self.exchange_url,
            "account_id": self.account_id,
            "main_account_address": self.main_account_address,
            "signer_address": self.signer_address,
            "vault_address": self.vault_address,
            "plan_hash": self.plan_hash,
            "metadata_hash": self.metadata_hash,
            "action_hash": self.action_hash,
            "preflight_hash": self.preflight_hash,
            "preflight_expires_at_ms": self.preflight_expires_at_ms,
            "nonce": self.nonce,
            "authorization_expires_at_ms": self.authorization_expires_at_ms,
            "expires_after_ms": self.expires_after_ms,
            "signed_at_ms": self.signed_at_ms,
            "signature": self.signature.as_dict(),
            "signature_hash": self.signature_hash,
            "envelope_hash": self.envelope_hash,
            "signer_binding_hash": self.signer_binding_hash,
            "wire_hash": self.wire_hash,
            "signing_implementation": self.signing_implementation,
            "envelope": self.envelope(),
            "submitted": False,
        }

    def execution_store_evidence(self, command_id: str) -> SignedEnvelopeEvidence:
        """Produce the exact immutable signed evidence persisted before send."""

        self.verify_integrity()
        return SignedEnvelopeEvidence(
            command_id=command_id,
            preflight_hash=self.preflight_hash,
            environment=self.network.environment,
            endpoint=self.exchange_url,
            account_id=self.account_id,
            plan_hash=self.plan_hash,
            action_hash=self.action_hash,
            nonce=self.nonce,
            wire_hash=self.wire_hash,
            signature_hash=self.signature_hash,
            envelope_hash=self.envelope_hash,
            signer_binding_hash=self.signer_binding_hash,
            authorization_expires_at_ms=self.authorization_expires_at_ms,
            expires_after_ms=self.expires_after_ms,
            signed_at_ms=self.signed_at_ms,
        )


@dataclass(frozen=True, slots=True)
class SignedRecoveryEnvelope:
    """Immutable wire for one independently validated account-safety action."""

    network: HyperliquidNetwork
    account_id: str
    main_account_address: str
    signer_address: str
    vault_address: str | None
    recovery_command_id: str
    permit_id: str
    parent_command_id: str
    preflight_hash: str | None
    original_attempt_id: str | None
    original_nonce: int | None
    worker_id: str
    fencing_token: int
    recovery_kind: RecoveryKind
    incident_id: str
    source_hash: str
    recovery_hash: str
    action_hash: str
    safety_policy_hash: str
    signing_authority_hash: str
    permit_expires_at_ms: int
    lease_expires_at_ms: int
    recovery_material_json: str
    nonce: int
    authorization_expires_at_ms: int
    expires_after_ms: int
    signed_at_ms: int
    signature: Signature
    signature_hash: str
    envelope_hash: str
    signer_binding_hash: str
    wire_json: str
    wire_hash: str
    signing_implementation: str

    @property
    def artifact_kind(self) -> str:
        return "recovery"

    @property
    def exchange_url(self) -> str:
        return self.network.exchange_url

    @property
    def wire_bytes(self) -> bytes:
        return self.wire_json.encode("utf-8")

    def envelope(self) -> dict[str, object]:
        return _frozen_json_object(self.wire_json, "signed recovery wire")

    def recovery_material(self) -> dict[str, object]:
        return _frozen_json_object(
            self.recovery_material_json, "signed recovery material"
        )

    def verify_integrity(self) -> None:
        if not isinstance(self.network, HyperliquidNetwork) or not isinstance(
            self.recovery_kind, RecoveryKind
        ):
            raise SignerOutputError("signed recovery network or kind is invalid")
        if self.network is not HyperliquidNetwork.TESTNET:
            raise SignerOutputError("mainnet recovery wire is hard-disabled")
        try:
            _text(self.account_id, "account_id")
            _text(self.incident_id, "incident_id")
            _address(self.main_account_address, "main_account_address")
            _address(self.signer_address, "signer_address")
            if self.vault_address is not None:
                _address(self.vault_address, "vault_address")
        except SignerPolicyError as error:
            raise SignerOutputError("signed recovery identity is invalid") from error
        if not isinstance(self.signature, Signature):
            raise SignerOutputError("signed recovery signature object is invalid")
        try:
            if _parse_signature(self.signature.as_dict()) != self.signature:
                raise SignerOutputError("signed recovery signature is non-canonical")
        except SignerPolicyError as error:
            raise SignerOutputError("signed recovery signature is invalid") from error
        if type(self.nonce) is not int or self.nonce < 0:
            raise SignerOutputError("signed recovery nonce is invalid")
        if not (
            type(self.signed_at_ms) is int
            and type(self.authorization_expires_at_ms) is int
            and type(self.expires_after_ms) is int
            and type(self.permit_expires_at_ms) is int
            and type(self.lease_expires_at_ms) is int
            and self.signed_at_ms
            < self.expires_after_ms
            <= self.authorization_expires_at_ms
            and self.expires_after_ms <= self.permit_expires_at_ms
            and self.expires_after_ms <= self.lease_expires_at_ms
        ):
            raise SignerOutputError("signed recovery expiry ordering is invalid")
        if hashlib.sha256(self.wire_bytes).hexdigest() != self.wire_hash:
            raise SignerOutputError("signed recovery wire hash mismatch")
        envelope = self.envelope()
        if json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=False,
        ) != self.wire_json:
            raise SignerOutputError("signed recovery wire JSON is not exact")
        if tuple(envelope) != (
            "action",
            "nonce",
            "signature",
            "vaultAddress",
            "expiresAfter",
        ):
            raise SignerOutputError("signed recovery wire field order is unsupported")
        if domain_hash(SIGNED_ENVELOPE_HASH_DOMAIN, envelope) != self.envelope_hash:
            raise SignerOutputError("signed recovery envelope hash mismatch")
        if envelope.get("nonce") != self.nonce:
            raise SignerOutputError("signed recovery nonce mismatch")
        if envelope.get("expiresAfter") != self.expires_after_ms:
            raise SignerOutputError("signed recovery expiry mismatch")
        if envelope.get("vaultAddress") != self.vault_address:
            raise SignerOutputError("signed recovery vault mismatch")
        raw_signature = envelope.get("signature")
        if raw_signature != self.signature.as_dict():
            raise SignerOutputError("signed recovery signature mismatch")
        if not isinstance(raw_signature, dict) or tuple(raw_signature) != ("r", "s", "v"):
            raise SignerOutputError("signed recovery signature order is unsupported")
        if domain_hash(SIGNATURE_HASH_DOMAIN, self.signature.as_dict()) != self.signature_hash:
            raise SignerOutputError("signed recovery signature hash mismatch")
        material = self.recovery_material()
        if canonical_json(material) != self.recovery_material_json:
            raise SignerOutputError("signed recovery material is not canonical")
        if domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material) != self.recovery_hash:
            raise SignerOutputError("signed recovery binding hash mismatch")
        action = envelope.get("action")
        if not isinstance(action, dict) or material.get("action") != action:
            raise SignerOutputError("signed recovery action differs from its binding")
        if domain_hash(RECOVERY_WIRE_ACTION_HASH_DOMAIN, action) != self.action_hash:
            raise SignerOutputError("signed recovery action hash mismatch")
        try:
            _validate_recovery_material(self.recovery_kind, material, action)
        except SignerPolicyError as error:
            raise SignerOutputError("signed recovery action binding is invalid") from error
        if material.get("incident_id") != self.incident_id:
            raise SignerOutputError("signed recovery incident binding mismatch")
        if material.get("expires_at_ms") != self.authorization_expires_at_ms:
            raise SignerOutputError("signed recovery authorization expiry mismatch")
        if material.get("network") != self.network.value:
            raise SignerOutputError("signed recovery network binding mismatch")
        if material.get("account_id") != self.account_id:
            raise SignerOutputError("signed recovery account binding mismatch")
        if material.get("main_account_address") != self.main_account_address:
            raise SignerOutputError("signed recovery main-account binding mismatch")
        expected_source = {
            RecoveryKind.REDUCE_ONLY_CLOSE: material.get("position_snapshot_hash"),
            RecoveryKind.CANCEL_BY_CLOID: material.get("account_snapshot_hash"),
            RecoveryKind.NOOP_FENCE: material.get("ambiguous_attempt_hash"),
        }[self.recovery_kind]
        if expected_source != self.source_hash:
            raise SignerOutputError("signed recovery source binding mismatch")
        for field, value in (
            ("source_hash", self.source_hash),
            ("recovery_hash", self.recovery_hash),
            ("action_hash", self.action_hash),
            ("safety_policy_hash", self.safety_policy_hash),
            ("signing_authority_hash", self.signing_authority_hash),
        ):
            if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                raise SignerOutputError(f"signed recovery {field} is invalid")
        for field, value in (
            ("recovery_command_id", self.recovery_command_id),
            ("permit_id", self.permit_id),
            ("parent_command_id", self.parent_command_id),
            ("worker_id", self.worker_id),
        ):
            try:
                _text(value, field)
            except SignerPolicyError as error:
                raise SignerOutputError(f"signed recovery {field} is invalid") from error
        if self.preflight_hash is not None:
            try:
                _hash(self.preflight_hash, "preflight_hash")
            except SignerPolicyError as error:
                raise SignerOutputError("signed recovery preflight hash is invalid") from error
        if self.original_attempt_id is not None:
            try:
                _text(self.original_attempt_id, "original_attempt_id")
            except SignerPolicyError as error:
                raise SignerOutputError(
                    "signed recovery original attempt ID is invalid"
                ) from error
        if self.original_nonce is not None and (
            type(self.original_nonce) is not int or self.original_nonce < 0
        ):
            raise SignerOutputError("signed recovery original nonce is invalid")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise SignerOutputError("signed recovery fencing token is invalid")
        if self.recovery_kind is RecoveryKind.NOOP_FENCE:
            if (
                self.original_attempt_id is None
                or self.original_nonce is None
                or self.preflight_hash is None
                or self.nonce != self.original_nonce
                or material.get("attempt_id") != self.original_attempt_id
                or material.get("command_id") != self.parent_command_id
                or material.get("preflight_hash") != self.preflight_hash
                or material.get("original_nonce") != self.original_nonce
            ):
                raise SignerOutputError("signed noop recovery lost its original attempt")
        elif self.original_attempt_id is not None or self.original_nonce is not None:
            raise SignerOutputError("non-noop recovery binds an original attempt")
        binding = {
            "artifact_kind": self.artifact_kind,
            "network": self.network.value,
            "account_id": self.account_id,
            "main_account_address": self.main_account_address,
            "signer_address": self.signer_address,
            "vault_address": self.vault_address,
            "recovery_command_id": self.recovery_command_id,
            "permit_id": self.permit_id,
            "parent_command_id": self.parent_command_id,
            "preflight_hash": self.preflight_hash,
            "original_attempt_id": self.original_attempt_id,
            "original_nonce": self.original_nonce,
            "worker_id": self.worker_id,
            "fencing_token": self.fencing_token,
            "incident_id": self.incident_id,
            "source_hash": self.source_hash,
            "recovery_hash": self.recovery_hash,
            "action_hash": self.action_hash,
            "safety_policy_hash": self.safety_policy_hash,
            "signing_authority_hash": self.signing_authority_hash,
            "permit_expires_at_ms": self.permit_expires_at_ms,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "authorization_expires_at_ms": self.authorization_expires_at_ms,
        }
        if domain_hash(SIGNER_BINDING_HASH_DOMAIN, binding) != self.signer_binding_hash:
            raise SignerOutputError("signed recovery signer policy binding mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "hyperliquid.signed_recovery_envelope.v1",
            "artifact_kind": self.artifact_kind,
            "network": self.network.value,
            "exchange_url": self.exchange_url,
            "account_id": self.account_id,
            "main_account_address": self.main_account_address,
            "signer_address": self.signer_address,
            "vault_address": self.vault_address,
            "recovery_command_id": self.recovery_command_id,
            "permit_id": self.permit_id,
            "parent_command_id": self.parent_command_id,
            "preflight_hash": self.preflight_hash,
            "original_attempt_id": self.original_attempt_id,
            "original_nonce": self.original_nonce,
            "worker_id": self.worker_id,
            "fencing_token": self.fencing_token,
            "recovery_kind": self.recovery_kind.value,
            "incident_id": self.incident_id,
            "source_hash": self.source_hash,
            "recovery_hash": self.recovery_hash,
            "action_hash": self.action_hash,
            "safety_policy_hash": self.safety_policy_hash,
            "signing_authority_hash": self.signing_authority_hash,
            "permit_expires_at_ms": self.permit_expires_at_ms,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "nonce": self.nonce,
            "authorization_expires_at_ms": self.authorization_expires_at_ms,
            "expires_after_ms": self.expires_after_ms,
            "signed_at_ms": self.signed_at_ms,
            "signature": self.signature.as_dict(),
            "signature_hash": self.signature_hash,
            "envelope_hash": self.envelope_hash,
            "signer_binding_hash": self.signer_binding_hash,
            "wire_hash": self.wire_hash,
            "signing_implementation": self.signing_implementation,
            "recovery_material": self.recovery_material(),
            "envelope": self.envelope(),
            "submitted": False,
        }

    def execution_store_evidence(self) -> SignedRecoveryEvidence:
        """Return the exact durable evidence required before recovery send."""

        self.verify_integrity()
        return SignedRecoveryEvidence(
            recovery_command_id=self.recovery_command_id,
            incident_id=self.incident_id,
            kind=self.recovery_kind.value,
            source_hash=self.source_hash,
            recovery_hash=self.recovery_hash,
            signing_authority_hash=self.signing_authority_hash,
            safety_policy_hash=self.safety_policy_hash,
            nonce=self.nonce,
            wire_hash=self.wire_hash,
            action_hash=self.action_hash,
            signature_hash=self.signature_hash,
            envelope_hash=self.envelope_hash,
            signer_binding_hash=self.signer_binding_hash,
            expires_after_ms=self.expires_after_ms,
            signed_at_ms=self.signed_at_ms,
        )


def official_sdk_available() -> bool:
    """Return whether the exact reviewed official SDK can be lazily loaded."""

    try:
        version = importlib_metadata.version(OFFICIAL_SDK_DISTRIBUTION)
    except importlib_metadata.PackageNotFoundError:
        return False
    if version != OFFICIAL_SDK_VERSION:
        return False
    try:
        from hyperliquid.utils.signing import sign_l1_action
    except (ImportError, ModuleNotFoundError):
        return False
    return callable(sign_l1_action)


def load_official_sign_l1_action() -> SignL1Action:
    """Load only ``sign_l1_action`` from official SDK version 0.24.0."""

    try:
        version = importlib_metadata.version(OFFICIAL_SDK_DISTRIBUTION)
    except importlib_metadata.PackageNotFoundError as error:
        raise SignerDependencyError(
            f"{OFFICIAL_SDK_DISTRIBUTION}=={OFFICIAL_SDK_VERSION} is not installed"
        ) from error
    if version != OFFICIAL_SDK_VERSION:
        raise SignerDependencyError(
            f"refusing {OFFICIAL_SDK_DISTRIBUTION} version {version!r}; "
            f"exactly {OFFICIAL_SDK_VERSION} is required"
        )
    try:
        from hyperliquid.utils.signing import sign_l1_action
    except (ImportError, ModuleNotFoundError) as error:
        raise SignerDependencyError("official sign_l1_action could not be imported") from error
    if not callable(sign_l1_action):
        raise SignerDependencyError("official sign_l1_action is not callable")
    return sign_l1_action


def _wire_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SignerPolicyError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
        validate_decimal_bounds(parsed, field=field)
    except (DecimalException, ValueError) as error:
        raise SignerPolicyError(f"{field} must be a bounded finite decimal") from error
    if parsed <= _ZERO or canonical_decimal(parsed) != value:
        raise SignerPolicyError(f"{field} is not a positive canonical decimal")
    return parsed


def _keys(value: dict[str, object], expected: tuple[str, ...], field: str) -> None:
    if tuple(value) != expected:
        raise SignerPolicyError(f"{field} fields or field order are unsupported")


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SignerPolicyError(f"{field} must be a JSON object")
    return value


def _validate_order(
    value: object,
    index: int,
) -> tuple[int, bool, Decimal, Decimal, str, Decimal | None]:
    order = _object(value, f"orders[{index}]")
    _keys(order, ("a", "b", "p", "s", "r", "t", "c"), f"orders[{index}]")
    asset = order["a"]
    if type(asset) is not int or not 0 <= asset <= 1_000_000:
        raise SignerPolicyError("order asset ID is invalid")
    is_buy = order["b"]
    reduce_only = order["r"]
    if type(is_buy) is not bool or type(reduce_only) is not bool:
        raise SignerPolicyError("order side and reduce-only fields must be boolean")
    price = _wire_decimal(order["p"], f"orders[{index}].p")
    size = _wire_decimal(order["s"], f"orders[{index}].s")
    cloid = order["c"]
    if not isinstance(cloid, str) or not _CLOID_RE.fullmatch(cloid):
        raise SignerPolicyError("every order requires a lowercase 128-bit CLOID")

    order_type = _object(order["t"], f"orders[{index}].t")
    trigger_price: Decimal | None = None
    if index == 0:
        if reduce_only:
            raise SignerPolicyError("entry leg must increase risk")
        _keys(order_type, ("limit",), "entry order type")
        limit = _object(order_type["limit"], "entry limit")
        _keys(limit, ("tif",), "entry limit")
        if limit["tif"] != "Ioc":
            raise SignerPolicyError("entry leg must use exact Ioc time in force")
    else:
        if not reduce_only:
            raise SignerPolicyError("protective legs must be reduce-only")
        _keys(order_type, ("trigger",), f"orders[{index}] trigger type")
        trigger = _object(order_type["trigger"], f"orders[{index}] trigger")
        _keys(
            trigger,
            ("isMarket", "triggerPx", "tpsl"),
            f"orders[{index}] trigger",
        )
        if trigger["isMarket"] is not True:
            raise SignerPolicyError("protective triggers must be market triggers")
        expected_kind = "sl" if index == 1 else "tp"
        if trigger["tpsl"] != expected_kind:
            raise SignerPolicyError("protective trigger legs are not ordered SL then TP")
        trigger_price = _wire_decimal(
            trigger["triggerPx"], f"orders[{index}].triggerPx"
        )
    return asset, is_buy, size, price, cloid, trigger_price


def _validated_action(protected: ProtectedOrderAction) -> dict[str, object]:
    if not isinstance(protected, ProtectedOrderAction):
        raise TypeError("protected must be ProtectedOrderAction")
    if not isinstance(protected.network, HyperliquidNetwork):
        raise SignerPolicyError("protected action network is invalid")
    account_id = _text(protected.account_id, "account_id")
    plan_hash = _hash(protected.plan_hash, "plan_hash")
    metadata_hash = _hash(protected.metadata_hash, "metadata_hash")
    supplied_hash = _hash(protected.action_hash, "action_hash")
    if type(protected.expires_at_ms) is not int or protected.expires_at_ms < 0:
        raise SignerPolicyError("expires_at_ms must be a non-negative integer")
    action = deepcopy(_object(protected.action, "protected action"))
    _keys(action, ("type", "orders", "grouping"), "protected action")
    if action["type"] != "order" or action["grouping"] != "normalTpsl":
        raise SignerPolicyError("only normalTpsl order actions may be signed")
    orders = action["orders"]
    if not isinstance(orders, list) or len(orders) != 3:
        raise SignerPolicyError("protected action must contain exactly three legs")
    checked = tuple(_validate_order(value, index) for index, value in enumerate(orders))
    assets = {item[0] for item in checked}
    sizes = {item[2] for item in checked}
    cloids = {item[4] for item in checked}
    if len(assets) != 1 or len(sizes) != 1 or len(cloids) != 3:
        raise SignerPolicyError("protected legs must share asset/size and use unique CLOIDs")
    entry_buy = checked[0][1]
    if checked[1][1] is entry_buy or checked[2][1] is entry_buy:
        raise SignerPolicyError("protective legs must oppose the entry side")
    stop_trigger = checked[1][5]
    target_trigger = checked[2][5]
    if stop_trigger is None or target_trigger is None:
        raise SignerPolicyError("protected triggers are missing")
    if entry_buy and not stop_trigger < target_trigger:
        raise SignerPolicyError("long stop must be below its take-profit trigger")
    if not entry_buy and not stop_trigger > target_trigger:
        raise SignerPolicyError("short stop must be above its take-profit trigger")

    binding = {
        "network": protected.network.value,
        "account_id": account_id,
        "plan_hash": plan_hash,
        "metadata_hash": metadata_hash,
        "expires_at_ms": protected.expires_at_ms,
        "action": action,
    }
    if domain_hash(_ACTION_HASH_DOMAIN, binding) != supplied_hash:
        raise SignerPolicyError("protected action hash does not match its contents")
    return action


def _signed_wire_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SignerPolicyError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
        validate_decimal_bounds(parsed, field=field)
    except (DecimalException, ValueError) as error:
        raise SignerPolicyError(f"{field} must be a bounded finite decimal") from error
    if canonical_decimal(parsed) != value:
        raise SignerPolicyError(f"{field} is not canonical")
    return parsed


def _validate_close_material(
    material: dict[str, object], action: dict[str, object]
) -> None:
    expected = {
        "kind",
        "network",
        "account_id",
        "main_account_address",
        "incident_id",
        "position_snapshot_hash",
        "symbol",
        "asset_id",
        "original_signed_position",
        "close_size",
        "price_bound",
        "cloid",
        "expires_at_ms",
        "action",
    }
    if "position_snapshot_time_ms" in material:
        expected.add("position_snapshot_time_ms")
    if set(material) != expected:
        raise SignerPolicyError("close recovery binding fields are unsupported")
    snapshot_time = material.get("position_snapshot_time_ms")
    if snapshot_time is not None and (
        type(snapshot_time) is not int or snapshot_time < 0
    ):
        raise SignerPolicyError("close recovery snapshot time is invalid")
    original = _signed_wire_decimal(
        material["original_signed_position"], "original signed position"
    )
    close_size = _wire_decimal(material["close_size"], "close size")
    price_bound = _wire_decimal(material["price_bound"], "close price bound")
    if original == _ZERO or close_size > abs(original):
        raise SignerPolicyError("close recovery could exceed or flip the position")
    asset = material["asset_id"]
    if type(asset) is not int or not 0 <= asset <= 1_000_000:
        raise SignerPolicyError("close recovery asset is invalid")
    cloid = material["cloid"]
    if not isinstance(cloid, str) or not _CLOID_RE.fullmatch(cloid):
        raise SignerPolicyError("close recovery CLOID is invalid")
    _keys(action, ("type", "orders", "grouping"), "close recovery action")
    if action["type"] != "order" or action["grouping"] != "na":
        raise SignerPolicyError("close recovery must be an ungrouped order")
    orders = action["orders"]
    if not isinstance(orders, list) or len(orders) != 1:
        raise SignerPolicyError("close recovery must contain exactly one order")
    order = _object(orders[0], "close recovery order")
    _keys(
        order,
        ("a", "b", "p", "s", "r", "t", "c"),
        "close recovery order",
    )
    if (
        order["a"] != asset
        or order["b"] is not (original < _ZERO)
        or order["r"] is not True
        or order["c"] != cloid
        or order["p"] != canonical_decimal(price_bound)
        or order["s"] != canonical_decimal(close_size)
    ):
        raise SignerPolicyError("close recovery order differs from its binding")
    order_type = _object(order["t"], "close recovery order type")
    _keys(order_type, ("limit",), "close recovery order type")
    limit = _object(order_type["limit"], "close recovery limit")
    _keys(limit, ("tif",), "close recovery limit")
    if limit["tif"] != "Ioc":
        raise SignerPolicyError("close recovery must use Ioc")


def _validate_cancel_material(
    material: dict[str, object], action: dict[str, object]
) -> None:
    expected = {
        "kind",
        "network",
        "account_id",
        "main_account_address",
        "incident_id",
        "account_snapshot_hash",
        "requests",
        "expires_at_ms",
        "action",
    }
    if "account_snapshot_time_ms" in material:
        expected.add("account_snapshot_time_ms")
    if set(material) != expected:
        raise SignerPolicyError("cancel recovery binding fields are unsupported")
    snapshot_time = material.get("account_snapshot_time_ms")
    if snapshot_time is not None and (
        type(snapshot_time) is not int or snapshot_time < 0
    ):
        raise SignerPolicyError("cancel recovery snapshot time is invalid")
    requests = material["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= 20:
        raise SignerPolicyError("cancel recovery requests are invalid")
    _keys(action, ("type", "cancels"), "cancel recovery action")
    if action["type"] != "cancelByCloid":
        raise SignerPolicyError("recovery cancellation must use cancelByCloid")
    cancels = action["cancels"]
    if not isinstance(cancels, list) or len(cancels) != len(requests):
        raise SignerPolicyError("cancel recovery action count differs from its binding")
    seen: set[str] = set()
    for index, (request, raw_cancel) in enumerate(zip(requests, cancels)):
        request_item = _object(request, f"cancel binding requests[{index}]")
        if set(request_item) != {"symbol", "asset_id", "cloid"}:
            raise SignerPolicyError("cancel binding request fields are unsupported")
        asset = request_item["asset_id"]
        cloid = request_item["cloid"]
        if type(asset) is not int or not 0 <= asset <= 1_000_000:
            raise SignerPolicyError("cancel recovery asset is invalid")
        if not isinstance(cloid, str) or not _CLOID_RE.fullmatch(cloid):
            raise SignerPolicyError("cancel recovery CLOID is invalid")
        if cloid in seen:
            raise SignerPolicyError("cancel recovery contains duplicate CLOIDs")
        seen.add(cloid)
        cancel = _object(raw_cancel, f"cancel action cancels[{index}]")
        _keys(cancel, ("asset", "cloid"), f"cancel action cancels[{index}]")
        if cancel != {"asset": asset, "cloid": cloid}:
            raise SignerPolicyError("cancel action differs from its binding")


def _validate_noop_material(
    material: dict[str, object], action: dict[str, object]
) -> None:
    expected = {
        "kind",
        "network",
        "account_id",
        "main_account_address",
        "incident_id",
        "attempt_id",
        "command_id",
        "preflight_hash",
        "signed_evidence_hash",
        "transport_evidence_hash",
        "original_nonce",
        "original_action_hash",
        "original_wire_hash",
        "ambiguous_attempt_hash",
        "expires_at_ms",
        "action",
    }
    if set(material) != expected:
        raise SignerPolicyError("noop recovery binding fields are unsupported")
    nonce = material["original_nonce"]
    if type(nonce) is not int or nonce < 0:
        raise SignerPolicyError("noop original nonce is invalid")
    _hash(material["original_action_hash"], "noop original action hash")
    _hash(material["original_wire_hash"], "noop original wire hash")
    _hash(material["ambiguous_attempt_hash"], "ambiguous attempt hash")
    if material["preflight_hash"] is not None:
        _hash(material["preflight_hash"], "noop preflight hash")
    _hash(material["signed_evidence_hash"], "noop signed evidence hash")
    _hash(material["transport_evidence_hash"], "noop transport evidence hash")
    _keys(action, ("type",), "noop recovery action")
    if action["type"] != "noop":
        raise SignerPolicyError("noop recovery action is invalid")


def _validate_recovery_material(
    kind: RecoveryKind,
    material: dict[str, object],
    action: dict[str, object],
) -> None:
    if material.get("kind") != kind.value or material.get("action") != action:
        raise SignerPolicyError("recovery kind/action binding is inconsistent")
    if kind is RecoveryKind.REDUCE_ONLY_CLOSE:
        _validate_close_material(material, action)
    elif kind is RecoveryKind.CANCEL_BY_CLOID:
        _validate_cancel_material(material, action)
    elif kind is RecoveryKind.NOOP_FENCE:
        _validate_noop_material(material, action)
    else:  # pragma: no cover - enum exhaustiveness guard
        raise SignerPolicyError("unsupported recovery kind")


def _validated_recovery_action(
    recovery: RecoveryAction,
    *,
    evidence: HyperliquidAccountSnapshot | AttemptRecord,
    incident_id: str,
    parent_command_id: str | None,
    now_ms: int,
) -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    tuple[int, ...],
    tuple[str, ...],
    int | None,
]:
    checked_incident_id = _text(incident_id, "incident_id")
    if recovery.incident_id != checked_incident_id:
        raise SignerPolicyError("recovery incident binding does not match evidence")
    action = deepcopy(_object(recovery.action, "recovery action"))
    common = {
        "kind": recovery.kind.value,
        "network": recovery.network.value,
        "account_id": recovery.account_id,
        "main_account_address": recovery.main_account_address,
        "incident_id": recovery.incident_id,
    }
    asset_ids: tuple[int, ...]
    cloids: tuple[str, ...]
    original_nonce: int | None = None
    if isinstance(recovery, ReduceOnlyCloseAction):
        if not isinstance(evidence, HyperliquidAccountSnapshot):
            raise SignerPolicyError("close recovery requires fresh account evidence")
        if evidence.snapshot_hash != recovery.position_snapshot_hash:
            raise SignerPolicyError("close recovery snapshot hash does not match evidence")
        if (
            recovery.position_snapshot_time_ms is not None
            and evidence.server_time_ms != recovery.position_snapshot_time_ms
        ):
            raise SignerPolicyError(
                "close recovery snapshot time does not match evidence"
            )
        if evidence.network != recovery.network.value:
            raise SignerPolicyError("close recovery snapshot network differs")
        if evidence.main_account_address != recovery.main_account_address:
            raise SignerPolicyError("close recovery snapshot account differs")
        age = now_ms - evidence.server_time_ms
        if age > 5_000 or age < -5_000:
            raise SignerPolicyError("close recovery account evidence is stale")
        position = evidence.position(recovery.symbol)
        if (
            position is None
            or position.asset_id != recovery.asset_id
            or position.signed_size != recovery.original_signed_position
        ):
            raise SignerPolicyError("close recovery position differs from fresh evidence")
        material = {
            **common,
            "position_snapshot_hash": recovery.position_snapshot_hash,
            "symbol": recovery.symbol,
            "asset_id": recovery.asset_id,
            "original_signed_position": canonical_decimal(
                recovery.original_signed_position
            ),
            "close_size": canonical_decimal(recovery.close_size),
            "price_bound": canonical_decimal(recovery.price_bound),
            "cloid": recovery.cloid,
            "expires_at_ms": recovery.expires_at_ms,
            "action": action,
        }
        if recovery.position_snapshot_time_ms is not None:
            material["position_snapshot_time_ms"] = (
                recovery.position_snapshot_time_ms
            )
        source_hash = recovery.position_snapshot_hash
        asset_ids = (recovery.asset_id,)
        cloids = (recovery.cloid,)
    elif isinstance(recovery, CancelByCloidAction):
        if not isinstance(evidence, HyperliquidAccountSnapshot):
            raise SignerPolicyError("cancel recovery requires fresh account evidence")
        if evidence.snapshot_hash != recovery.account_snapshot_hash:
            raise SignerPolicyError("cancel recovery snapshot hash does not match evidence")
        if (
            recovery.account_snapshot_time_ms is not None
            and evidence.server_time_ms != recovery.account_snapshot_time_ms
        ):
            raise SignerPolicyError(
                "cancel recovery snapshot time does not match evidence"
            )
        if evidence.network != recovery.network.value:
            raise SignerPolicyError("cancel recovery snapshot network differs")
        if evidence.main_account_address != recovery.main_account_address:
            raise SignerPolicyError("cancel recovery snapshot account differs")
        age = now_ms - evidence.server_time_ms
        if age > 5_000 or age < -5_000:
            raise SignerPolicyError("cancel recovery account evidence is stale")
        try:
            metadata_matches = len(recovery.requests) == len(recovery.asset_ids) and all(
                evidence.metadata.instrument(request.symbol).asset_id == asset_id
                for request, asset_id in zip(recovery.requests, recovery.asset_ids)
            )
        except ValidationError as error:
            raise SignerPolicyError(
                "cancel recovery references unknown fresh metadata"
            ) from error
        if not metadata_matches:
            raise SignerPolicyError("cancel recovery assets differ from fresh metadata")
        open_orders = evidence.all_open_orders()
        for request in recovery.requests:
            matches = tuple(
                order for order in open_orders if order.cloid == request.cloid
            )
            if len(matches) != 1:
                raise SignerPolicyError(
                    "cancel recovery CLOID is not unique in fresh account evidence"
                )
            order = matches[0]
            if order.symbol != request.symbol:
                raise SignerPolicyError(
                    "cancel recovery symbol differs from fresh account evidence"
                )
            if evidence.position(request.symbol) is not None and (
                order.is_protective_stop or not order.reduce_only
            ):
                raise SignerPolicyError(
                    "cancel recovery cannot remove live protective or "
                    "exposure-increasing orders"
                )
        material = {
            **common,
            "account_snapshot_hash": recovery.account_snapshot_hash,
            "requests": [
                {
                    "symbol": request.symbol,
                    "asset_id": asset_id,
                    "cloid": request.cloid,
                }
                for request, asset_id in zip(recovery.requests, recovery.asset_ids)
            ],
            "expires_at_ms": recovery.expires_at_ms,
            "action": action,
        }
        if recovery.account_snapshot_time_ms is not None:
            material["account_snapshot_time_ms"] = (
                recovery.account_snapshot_time_ms
            )
        source_hash = recovery.account_snapshot_hash
        asset_ids = recovery.asset_ids
        cloids = tuple(request.cloid for request in recovery.requests)
    elif isinstance(recovery, NoopFenceAction):
        if not isinstance(evidence, AttemptRecord):
            raise SignerPolicyError("noop fence requires persisted attempt evidence")
        if evidence.state != "unknown" or evidence.response_hash is not None:
            raise SignerPolicyError("noop fence evidence is not an unknown attempt")
        if parent_command_id != evidence.command_id:
            raise SignerPolicyError("noop incident command differs from attempt")
        if ambiguous_attempt_hash(evidence) != recovery.ambiguous_attempt_hash:
            raise SignerPolicyError("noop ambiguous attempt hash differs from evidence")
        if (
            evidence.attempt_id != recovery.attempt_id
            or evidence.command_id != recovery.command_id
            or evidence.preflight_hash != recovery.preflight_hash
            or evidence.signed_evidence_hash != recovery.signed_evidence_hash
            or evidence.transport_evidence_hash != recovery.transport_evidence_hash
            or evidence.nonce != recovery.original_nonce
            or evidence.action_hash != recovery.original_action_hash
            or evidence.wire_hash != recovery.original_wire_hash
        ):
            raise SignerPolicyError("noop recovery differs from persisted attempt evidence")
        material = {
            **common,
            "attempt_id": recovery.attempt_id,
            "command_id": recovery.command_id,
            "preflight_hash": recovery.preflight_hash,
            "signed_evidence_hash": recovery.signed_evidence_hash,
            "transport_evidence_hash": recovery.transport_evidence_hash,
            "original_nonce": recovery.original_nonce,
            "original_action_hash": recovery.original_action_hash,
            "original_wire_hash": recovery.original_wire_hash,
            "ambiguous_attempt_hash": recovery.ambiguous_attempt_hash,
            "expires_at_ms": recovery.expires_at_ms,
            "action": action,
        }
        source_hash = recovery.ambiguous_attempt_hash
        asset_ids = ()
        cloids = ()
        original_nonce = recovery.original_nonce
    else:
        raise TypeError("recovery must be a typed RecoveryAction")
    if domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material) != recovery.recovery_hash:
        raise SignerPolicyError("recovery hash does not match its bound contents")
    try:
        canonical_material = recovery_action_material(recovery)
    except (TypeError, ValidationError) as error:
        raise SignerPolicyError("recovery failed exact material verification") from error
    if canonical_material != material:
        raise SignerPolicyError("recovery canonical material differs")
    _validate_recovery_material(recovery.kind, material, action)
    return action, material, source_hash, asset_ids, cloids, original_nonce


def _parse_signature(value: object) -> Signature:
    root = _object(value, "signature")
    if tuple(root) != ("r", "s", "v"):
        raise SignerOutputError("signature fields or field order are unsupported")
    r = root["r"]
    s = root["s"]
    v = root["v"]
    if not isinstance(r, str) or not _SIGNATURE_COMPONENT_RE.fullmatch(r):
        raise SignerOutputError("signature.r must be a canonical lowercase value")
    if not isinstance(s, str) or not _SIGNATURE_COMPONENT_RE.fullmatch(s):
        raise SignerOutputError("signature.s must be a canonical lowercase value")
    if type(v) is not int or v not in {27, 28}:
        raise SignerOutputError("signature.v must be 27 or 28")
    return Signature(r=r, s=s, v=v)


def _datetime_ms(value: datetime, field: str) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SignerPolicyError(f"{field} must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    delta = utc - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise SignerPolicyError(f"{field} predates the Unix epoch")
    return result


def _validate_protected_sources(
    protected: ProtectedOrderAction,
    *,
    plan: ProtectedTradePlan,
    metadata: PerpInstrumentMetadata,
    preflight: DispatchPreflight,
    now_ms: int,
) -> tuple[dict[str, object], int]:
    if not isinstance(plan, ProtectedTradePlan):
        raise TypeError("plan must be ProtectedTradePlan")
    if not isinstance(metadata, PerpInstrumentMetadata):
        raise TypeError("metadata must be PerpInstrumentMetadata")
    if not isinstance(preflight, DispatchPreflight):
        raise TypeError("preflight must be DispatchPreflight")
    try:
        verified_metadata = PerpInstrumentMetadata(
            symbol=metadata.symbol,
            asset_id=metadata.asset_id,
            sz_decimals=metadata.sz_decimals,
            max_leverage=metadata.max_leverage,
            margin_mode=metadata.margin_mode,
            is_delisted=metadata.is_delisted,
            source_hash=metadata.source_hash,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise SignerPolicyError("metadata failed independent verification") from error
    if verified_metadata != metadata:
        raise SignerPolicyError("metadata differs from its verified encoding")
    try:
        verified_plan = protected_trade_plan_from_dict(plan.as_dict())
    except (TypeError, ValueError, ValidationError) as error:
        raise SignerPolicyError("protected plan failed independent verification") from error
    if verified_plan != plan:
        raise SignerPolicyError("protected plan differs from its verified encoding")
    try:
        verified_preflight = DispatchPreflight(
            command_id=preflight.command_id,
            ticket_hash=preflight.ticket_hash,
            plan_hash=preflight.plan_hash,
            environment=preflight.environment,
            account_id=preflight.account_id,
            account_snapshot_hash=preflight.account_snapshot_hash,
            account_server_time_ms=preflight.account_server_time_ms,
            metadata_hash=preflight.metadata_hash,
            market_snapshot_hash=preflight.market_snapshot_hash,
            risk_policy_hash=preflight.risk_policy_hash,
            observed_at=preflight.observed_at,
            expires_at=preflight.expires_at,
            passed=preflight.passed,
            preflight_hash=preflight.preflight_hash,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise SignerPolicyError("dispatch preflight failed independent verification") from error
    if verified_preflight != preflight:
        raise SignerPolicyError("dispatch preflight differs from its verified encoding")
    if protected.network is HyperliquidNetwork.MAINNET:
        raise SignerPolicyError("mainnet signing is hard-disabled in this build")
    if plan.entry.environment is not Environment.TESTNET:
        raise SignerPolicyError("only testnet protected plans may be signed")
    if not preflight.passed:
        raise SignerPolicyError("dispatch preflight did not pass")
    observed_ms = _datetime_ms(preflight.observed_at, "preflight.observed_at")
    preflight_expiry_ms = _datetime_ms(preflight.expires_at, "preflight.expires_at")
    if not observed_ms <= now_ms < preflight_expiry_ms:
        raise SignerPolicyError("dispatch preflight is not active")
    if (
        preflight.environment is not Environment.TESTNET
        or preflight.environment.value != protected.network.value
        or preflight.account_id != plan.entry.account_id
        or preflight.account_id != protected.account_id
        or preflight.plan_hash != plan.plan_hash
        or preflight.plan_hash != protected.plan_hash
        or preflight.metadata_hash != metadata.source_hash
        or preflight.metadata_hash != protected.metadata_hash
    ):
        raise SignerPolicyError("plan, metadata, action, and preflight bindings differ")
    now = _EPOCH + timedelta(milliseconds=now_ms)
    expected = build_protected_order_action(
        plan,
        metadata,
        network=protected.network,
        at=now,
    )
    if (
        expected.account_id != protected.account_id
        or expected.plan_hash != protected.plan_hash
        or expected.metadata_hash != protected.metadata_hash
        or expected.expires_at_ms != protected.expires_at_ms
        or expected.action_hash != protected.action_hash
        or expected.action != protected.action
    ):
        raise SignerPolicyError(
            "protected action was not exactly rebuilt from the verified plan"
        )
    quantity = plan.entry.quantity
    price_bound = plan.entry.price_bound
    if quantity > MAX_PROTECTED_QUANTITY:
        raise SignerPolicyError("protected quantity exceeds the compiled ceiling")
    if price_bound is None:
        raise SignerPolicyError("protected plan lacks an entry price bound")
    with localcontext(_SIGNER_CONTEXT) as context:
        notional = context.multiply(quantity, price_bound)
    validate_decimal_bounds(notional, field="protected notional")
    if notional > MAX_PROTECTED_NOTIONAL:
        raise SignerPolicyError("protected notional exceeds the compiled ceiling")
    return _validated_action(protected), preflight_expiry_ms


def sign_protected_action(
    protected: ProtectedOrderAction,
    *,
    plan: ProtectedTradePlan,
    metadata: PerpInstrumentMetadata,
    preflight: DispatchPreflight,
    policy: SignerPolicy,
    wallet: object,
    nonce_allocator: NonceAllocator,
    clock: Clock = lambda: datetime.now(timezone.utc),
    sign_l1_action: SignL1Action | None = None,
) -> SignedActionEnvelope:
    """Validate, durably allocate, sign once, and freeze exact wire bytes."""

    if not isinstance(policy, SignerPolicy):
        raise TypeError("policy must be SignerPolicy")
    if not callable(getattr(nonce_allocator, "allocate", None)):
        raise TypeError("nonce_allocator must provide allocate()")
    if not callable(clock):
        raise TypeError("clock must be callable")
    now_ms = _utc_ms(clock)
    action, preflight_expiry_ms = _validate_protected_sources(
        protected,
        plan=plan,
        metadata=metadata,
        preflight=preflight,
        now_ms=now_ms,
    )
    if protected.network not in policy.allowed_networks:
        raise SignerPolicyError("protected action network is not allowlisted")
    if protected.network is HyperliquidNetwork.MAINNET:
        raise SignerPolicyError("mainnet signing is hard-disabled in this build")
    account = policy.account(protected.account_id)
    signer_address = _wallet_address(wallet)
    if signer_address != account.signer_address:
        raise SignerPolicyError("injected wallet does not match the account signer allowlist")
    asset = action["orders"][0]["a"]  # type: ignore[index]
    if asset not in policy.allowed_asset_ids:
        raise SignerPolicyError("protected action asset is not allowlisted")

    remaining = protected.expires_at_ms - now_ms
    if remaining < policy.minimum_expiry_remaining_ms:
        raise SignerPolicyError("protected action expiry is stale or too close")
    # The reviewed intent expiry is an upper authorization bound.  The actual
    # L1 action receives a new, shorter transaction-delay deadline so a normal
    # 60-second approval can never become a 60-second delayed venue action.
    expires_after_ms = min(
        protected.expires_at_ms,
        now_ms + policy.maximum_expiry_horizon_ms,
        preflight_expiry_ms,
    )
    if expires_after_ms - now_ms < policy.minimum_expiry_remaining_ms:
        raise SignerPolicyError("dispatch preflight expires too soon for signing")

    # PersistentNonceAllocator commits inside allocate().  This must remain
    # before the signing call: a signing exception burns a nonce safely rather
    # than risking reuse after a crash.
    nonce = nonce_allocator.allocate()
    if type(nonce) is not int or nonce < 0:
        raise SignerPolicyError("nonce allocator returned an invalid nonce")
    if not now_ms - _NONCE_PAST_WINDOW_MS < nonce < now_ms + _NONCE_FUTURE_WINDOW_MS:
        raise SignerPolicyError("allocated nonce is outside Hyperliquid's time window")

    implementation = "injected"
    signing_function = sign_l1_action
    if signing_function is None:
        signing_function = load_official_sign_l1_action()
        implementation = f"{OFFICIAL_SDK_DISTRIBUTION}=={OFFICIAL_SDK_VERSION}"
    if not callable(signing_function):
        raise TypeError("sign_l1_action must be callable")
    signing_action = deepcopy(action)
    signing_action_before = json.dumps(
        signing_action,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    try:
        raw_signature = signing_function(
            wallet,
            signing_action,
            account.vault_address,
            nonce,
            expires_after_ms,
            protected.network is HyperliquidNetwork.MAINNET,
        )
    except HyperliquidSignerError:
        raise
    except Exception as error:
        raise SignerOutputError(
            f"sign_l1_action failed: {type(error).__name__}"
        ) from error
    signing_action_after = json.dumps(
        signing_action,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    if signing_action_after != signing_action_before:
        raise SignerOutputError("sign_l1_action mutated the reviewed action")
    signature = _parse_signature(raw_signature)
    envelope: dict[str, object] = {
        "action": action,
        "nonce": nonce,
        "signature": signature.as_dict(),
        "vaultAddress": account.vault_address,
        "expiresAfter": expires_after_ms,
    }
    # Hyperliquid L1 signing uses msgpack and field order is significant.  The
    # JSON sent to the API must therefore preserve the exact reviewed action
    # insertion order used by sign_l1_action; key-sorted canonical JSON would
    # recover a different signer at the venue.
    wire_json = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    wire_hash = hashlib.sha256(wire_json.encode("utf-8")).hexdigest()
    signature_hash = domain_hash(SIGNATURE_HASH_DOMAIN, signature.as_dict())
    envelope_hash = domain_hash(SIGNED_ENVELOPE_HASH_DOMAIN, envelope)
    signer_binding_hash = domain_hash(
        SIGNER_BINDING_HASH_DOMAIN,
        {
            "artifact_kind": "protected_order",
            "network": protected.network.value,
            "account_id": protected.account_id,
            "main_account_address": account.main_account_address,
            "signer_address": signer_address,
            "vault_address": account.vault_address,
            "action_hash": protected.action_hash,
            "preflight_hash": preflight.preflight_hash,
            "preflight_expires_at_ms": preflight_expiry_ms,
        },
    )
    result = SignedActionEnvelope(
        network=protected.network,
        account_id=protected.account_id,
        main_account_address=account.main_account_address,
        signer_address=signer_address,
        vault_address=account.vault_address,
        plan_hash=protected.plan_hash,
        metadata_hash=protected.metadata_hash,
        action_hash=protected.action_hash,
        preflight_hash=preflight.preflight_hash,
        preflight_expires_at_ms=preflight_expiry_ms,
        nonce=nonce,
        authorization_expires_at_ms=protected.expires_at_ms,
        expires_after_ms=expires_after_ms,
        signed_at_ms=now_ms,
        signature=signature,
        signature_hash=signature_hash,
        envelope_hash=envelope_hash,
        signer_binding_hash=signer_binding_hash,
        wire_json=wire_json,
        wire_hash=wire_hash,
        signing_implementation=implementation,
    )
    result.verify_integrity()
    return result


def _verified_recovery_policy(policy: SignerPolicy) -> SignerPolicy:
    if not isinstance(policy, SignerPolicy):
        raise TypeError("policy must be SignerPolicy")
    try:
        verified = SignerPolicy(
            accounts=policy.accounts,
            allowed_asset_ids=policy.allowed_asset_ids,
            allowed_networks=policy.allowed_networks,
            allow_mainnet=policy.allow_mainnet,
            minimum_expiry_remaining_ms=policy.minimum_expiry_remaining_ms,
            maximum_expiry_horizon_ms=policy.maximum_expiry_horizon_ms,
            allowed_recovery_kinds=policy.allowed_recovery_kinds,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise SignerPolicyError("recovery signer policy failed verification") from error
    if verified != policy:
        raise SignerPolicyError("recovery signer policy differs from verified encoding")
    return verified


def _recovery_signer_binding(
    *,
    recovery: RecoveryAction,
    account: SigningAccount,
    signer_address: str,
    recovery_command_id: str,
    permit_id: str,
    parent_command_id: str,
    preflight_hash: str | None,
    original_attempt_id: str | None,
    original_nonce: int | None,
    worker_id: str,
    fencing_token: int,
    action_hash: str,
    safety_policy_hash: str,
    signing_authority_hash: str,
    permit_expires_at_ms: int,
    lease_expires_at_ms: int,
    authorization_expires_at_ms: int,
) -> dict[str, object]:
    return {
        "artifact_kind": "recovery",
        "network": recovery.network.value,
        "account_id": recovery.account_id,
        "main_account_address": recovery.main_account_address,
        "signer_address": signer_address,
        "vault_address": account.vault_address,
        "recovery_command_id": recovery_command_id,
        "permit_id": permit_id,
        "parent_command_id": parent_command_id,
        "preflight_hash": preflight_hash,
        "original_attempt_id": original_attempt_id,
        "original_nonce": original_nonce,
        "worker_id": worker_id,
        "fencing_token": fencing_token,
        "incident_id": recovery.incident_id,
        "source_hash": (
            recovery.position_snapshot_hash
            if isinstance(recovery, ReduceOnlyCloseAction)
            else recovery.account_snapshot_hash
            if isinstance(recovery, CancelByCloidAction)
            else recovery.ambiguous_attempt_hash
        ),
        "recovery_hash": recovery.recovery_hash,
        "action_hash": action_hash,
        "safety_policy_hash": safety_policy_hash,
        "signing_authority_hash": signing_authority_hash,
        "permit_expires_at_ms": permit_expires_at_ms,
        "lease_expires_at_ms": lease_expires_at_ms,
        "authorization_expires_at_ms": authorization_expires_at_ms,
    }


def _freeze_signed_recovery(
    recovery: RecoveryAction,
    *,
    action: dict[str, object],
    material: dict[str, object],
    source_hash: str,
    account: SigningAccount,
    signer_address: str,
    nonce: int,
    expires_after_ms: int,
    now_ms: int,
    recovery_command_id: str,
    permit_id: str,
    parent_command_id: str,
    preflight_hash: str | None,
    original_attempt_id: str | None,
    original_nonce: int | None,
    worker_id: str,
    fencing_token: int,
    safety_policy_hash: str,
    signing_authority_hash: str,
    permit_expires_at_ms: int,
    lease_expires_at_ms: int,
    wallet: object,
    sign_l1_action: SignL1Action | None,
) -> SignedRecoveryEnvelope:
    implementation = "injected"
    signing_function = sign_l1_action
    if signing_function is None:
        signing_function = load_official_sign_l1_action()
        implementation = f"{OFFICIAL_SDK_DISTRIBUTION}=={OFFICIAL_SDK_VERSION}"
    if not callable(signing_function):
        raise TypeError("sign_l1_action must be callable")
    signing_action = deepcopy(action)
    signing_action_before = json.dumps(
        signing_action,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    try:
        raw_signature = signing_function(
            wallet,
            signing_action,
            account.vault_address,
            nonce,
            expires_after_ms,
            False,
        )
    except HyperliquidSignerError:
        raise
    except Exception as error:
        raise SignerOutputError(
            f"recovery sign_l1_action failed: {type(error).__name__}"
        ) from error
    signing_action_after = json.dumps(
        signing_action,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    if signing_action_after != signing_action_before:
        raise SignerOutputError("sign_l1_action mutated the reviewed recovery action")
    signature = _parse_signature(raw_signature)
    envelope: dict[str, object] = {
        "action": action,
        "nonce": nonce,
        "signature": signature.as_dict(),
        "vaultAddress": account.vault_address,
        "expiresAfter": expires_after_ms,
    }
    wire_json = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    action_hash = domain_hash(RECOVERY_WIRE_ACTION_HASH_DOMAIN, action)
    binding = _recovery_signer_binding(
        recovery=recovery,
        account=account,
        signer_address=signer_address,
        recovery_command_id=recovery_command_id,
        permit_id=permit_id,
        parent_command_id=parent_command_id,
        preflight_hash=preflight_hash,
        original_attempt_id=original_attempt_id,
        original_nonce=original_nonce,
        worker_id=worker_id,
        fencing_token=fencing_token,
        action_hash=action_hash,
        safety_policy_hash=safety_policy_hash,
        signing_authority_hash=signing_authority_hash,
        permit_expires_at_ms=permit_expires_at_ms,
        lease_expires_at_ms=lease_expires_at_ms,
        authorization_expires_at_ms=recovery.expires_at_ms,
    )
    result = SignedRecoveryEnvelope(
        network=recovery.network,
        account_id=recovery.account_id,
        main_account_address=recovery.main_account_address,
        signer_address=signer_address,
        vault_address=account.vault_address,
        recovery_command_id=recovery_command_id,
        permit_id=permit_id,
        parent_command_id=parent_command_id,
        preflight_hash=preflight_hash,
        original_attempt_id=original_attempt_id,
        original_nonce=original_nonce,
        worker_id=worker_id,
        fencing_token=fencing_token,
        recovery_kind=recovery.kind,
        incident_id=recovery.incident_id,
        source_hash=source_hash,
        recovery_hash=recovery.recovery_hash,
        action_hash=action_hash,
        safety_policy_hash=safety_policy_hash,
        signing_authority_hash=signing_authority_hash,
        permit_expires_at_ms=permit_expires_at_ms,
        lease_expires_at_ms=lease_expires_at_ms,
        recovery_material_json=canonical_json(material),
        nonce=nonce,
        authorization_expires_at_ms=recovery.expires_at_ms,
        expires_after_ms=expires_after_ms,
        signed_at_ms=now_ms,
        signature=signature,
        signature_hash=domain_hash(SIGNATURE_HASH_DOMAIN, signature.as_dict()),
        envelope_hash=domain_hash(SIGNED_ENVELOPE_HASH_DOMAIN, envelope),
        signer_binding_hash=domain_hash(SIGNER_BINDING_HASH_DOMAIN, binding),
        wire_json=wire_json,
        wire_hash=hashlib.sha256(wire_json.encode("utf-8")).hexdigest(),
        signing_implementation=implementation,
    )
    result.verify_integrity()
    return result


def _require_derived_live_close_cloid(
    recovery: ReduceOnlyCloseAction,
    command: RecoveryCommand,
) -> str:
    if (
        command.incident_id != recovery.incident_id
        or command.source_hash != recovery.position_snapshot_hash
    ):
        raise SignerPolicyError(
            "durable close recovery source binding is inconsistent"
        )
    expected = derive_recovery_close_cloid(
        account_id=recovery.account_id,
        incident_id=recovery.incident_id,
        position_snapshot_hash=recovery.position_snapshot_hash,
    )
    if recovery.cloid != expected:
        raise SignerPolicyError(
            "live close recovery requires its exact derived CLOID"
        )
    return expected


def _validate_recovery_allowlists(
    recovery: RecoveryAction,
    *,
    policy: SignerPolicy,
    store: ExecutionStore | None = None,
    command: RecoveryCommand | None = None,
    asset_ids: tuple[int, ...],
    cloids: tuple[str, ...],
) -> SigningAccount:
    if recovery.kind not in policy.allowed_recovery_kinds:
        raise SignerPolicyError("recovery kind is not explicitly allowlisted")
    if recovery.network not in policy.allowed_networks:
        raise SignerPolicyError("recovery network is not allowlisted")
    if recovery.network is not HyperliquidNetwork.TESTNET:
        raise SignerPolicyError("mainnet recovery signing is hard-disabled")
    account = policy.account(recovery.account_id)
    if account.main_account_address != recovery.main_account_address:
        raise SignerPolicyError("recovery main account differs from signer policy")
    if not set(asset_ids).issubset(policy.allowed_asset_ids):
        raise SignerPolicyError("recovery asset is not allowlisted")
    if (store is None) != (command is None):
        raise SignerPolicyError("durable recovery allowlist binding is incomplete")
    selected_cloids = set(cloids)
    if (
        isinstance(recovery, ReduceOnlyCloseAction)
        and store is not None
        and command is not None
    ):
        if recovery.position_snapshot_time_ms is None:
            raise SignerPolicyError(
                "live close recovery lacks venue-server source watermark"
            )
        expected_close_cloid = _require_derived_live_close_cloid(recovery, command)
        if selected_cloids != {expected_close_cloid}:
            raise SignerPolicyError(
                "live close recovery requires its exact derived CLOID"
            )
        allowed_cloids = {expected_close_cloid}
    elif isinstance(recovery, ReduceOnlyCloseAction):
        # Store-less golden vectors retain the legacy explicit static
        # allowlist.  They cannot claim live durable signing authority.
        allowed_cloids = set(account.owned_cloids)
    elif (
        isinstance(recovery, CancelByCloidAction)
        and store is not None
        and command is not None
    ):
        if recovery.account_snapshot_time_ms is None:
            raise SignerPolicyError(
                "live cancel recovery lacks venue-server source watermark"
            )
        legs = ExecutionStore.get_legs(store, command.parent_command_id)
        if (
            len(legs) != 3
            or {item.role for item in legs}
            != {"entry", "protective_stop", "take_profit"}
        ):
            raise SignerPolicyError(
                "cancel recovery parent lacks its durable protected legs"
            )
        allowed_cloids = {item.cloid for item in legs}
    elif isinstance(recovery, NoopFenceAction):
        allowed_cloids = set()
    else:
        # Golden-vector tests have no live store capability.  Their fixed
        # policy retains the legacy explicit CLOID allowlist, while the live
        # signer must use the exact durable parent legs above.
        allowed_cloids = set(account.owned_cloids)
    if not selected_cloids.issubset(allowed_cloids):
        raise SignerPolicyError("recovery references a foreign CLOID")
    return account


def _validate_durable_recovery_binding(
    recovery: RecoveryAction,
    *,
    store: ExecutionStore,
    command: RecoveryCommand,
    authority: RecoverySigningAuthority,
    material: dict[str, object],
    source_hash: str,
    policy: SignerPolicy,
    worker_id: str,
    fencing_token: int,
) -> tuple[int, int]:
    if command.state != "signing":
        raise SignerPolicyError("durable recovery command is not in signing state")
    expected = (
        (authority.recovery_command_id, command.recovery_command_id),
        (authority.permit_id, command.permit_id),
        (authority.parent_command_id, command.parent_command_id),
        (authority.incident_id, command.incident_id),
        (authority.kind, command.kind),
        (authority.source_hash, command.source_hash),
        (authority.preflight_hash, command.preflight_hash),
        (authority.recovery_hash, command.recovery_hash),
        (authority.safety_policy_hash, command.safety_policy_hash),
        (authority.original_attempt_id, command.original_attempt_id),
        (authority.original_nonce, command.original_nonce),
    )
    if any(left != right for left, right in expected):
        raise SignerPolicyError("recovery signing authority differs from durable command")
    if authority.worker_id != worker_id or authority.fencing_token != fencing_token:
        raise SignerPolicyError("recovery signing authority claim binding differs")
    if (
        command.recovery_command_id != authority.recovery_command_id
        or command.incident_id != recovery.incident_id
        or command.kind != recovery.kind.value
        or command.source_hash != source_hash
        or command.recovery_hash != recovery.recovery_hash
    ):
        raise SignerPolicyError("typed recovery differs from durable command")
    material_json = canonical_json(material)
    if (
        material_json != command.recovery_material_json
        or hashlib.sha256(material_json.encode("utf-8")).hexdigest()
        != command.recovery_material_hash
    ):
        raise SignerPolicyError("typed recovery differs from durable canonical material")
    if policy.safety_policy_hash != authority.safety_policy_hash:
        raise SignerPolicyError("signer safety policy differs from recovery authority")
    if (
        type(store) is not ExecutionStore
        or store.environment is not Environment.TESTNET
        or store.account_id != recovery.account_id
        or recovery.network is not HyperliquidNetwork.TESTNET
    ):
        raise SignerPolicyError("recovery execution-store scope is not exact testnet")
    permit_expires_at_ms = _datetime_ms(
        authority.permit_expires_at, "authority.permit_expires_at"
    )
    lease_expires_at_ms = _datetime_ms(
        authority.lease_expires_at, "authority.lease_expires_at"
    )
    return permit_expires_at_ms, lease_expires_at_ms


def _sign_recovery_action_for_test(
    recovery: RecoveryAction,
    *,
    evidence: HyperliquidAccountSnapshot | AttemptRecord,
    incident: IncidentRecord,
    policy: SignerPolicy,
    wallet: object,
    nonce_allocator: NonceAllocator | None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    sign_l1_action: SignL1Action | None = None,
) -> SignedRecoveryEnvelope:
    """Internal golden-vector helper; it cannot consume a live store permit."""

    policy = _verified_recovery_policy(policy)
    if not isinstance(incident, IncidentRecord) or incident.state != "open":
        raise SignerPolicyError("recovery requires the bound open persisted incident")
    if not isinstance(
        recovery,
        (ReduceOnlyCloseAction, CancelByCloidAction, NoopFenceAction),
    ):
        raise TypeError("recovery must be a typed RecoveryAction")
    if not callable(clock):
        raise TypeError("clock must be callable")
    now_ms = _utc_ms(clock)
    parent_command_id = incident.command_id or "internal-test-parent"
    (
        action,
        material,
        source_hash,
        asset_ids,
        cloids,
        original_nonce,
    ) = _validated_recovery_action(
        recovery,
        evidence=evidence,
        incident_id=incident.incident_id,
        parent_command_id=parent_command_id,
        now_ms=now_ms,
    )
    account = _validate_recovery_allowlists(
        recovery,
        policy=policy,
        asset_ids=asset_ids,
        cloids=cloids,
    )
    remaining = recovery.expires_at_ms - now_ms
    if not policy.minimum_expiry_remaining_ms <= remaining <= min(
        policy.maximum_expiry_horizon_ms, _MAX_EXPIRY_HORIZON_MS
    ):
        raise SignerPolicyError("recovery expiry is not within the short signer bound")
    signer_address = _wallet_address(wallet)
    if signer_address != account.signer_address:
        raise SignerPolicyError("injected wallet does not match recovery signer policy")
    if recovery.kind is RecoveryKind.NOOP_FENCE:
        if nonce_allocator is not None:
            raise SignerPolicyError("noop fence must not allocate or replace its original nonce")
        if original_nonce is None:
            raise SignerPolicyError("noop fence lacks its original nonce")
        nonce = original_nonce
        original_attempt_id = recovery.attempt_id
        preflight_hash = recovery.preflight_hash
    else:
        if not callable(getattr(nonce_allocator, "allocate", None)):
            raise SignerPolicyError("close and cancel recovery require a nonce allocator")
        nonce = nonce_allocator.allocate()  # type: ignore[union-attr]
        original_attempt_id = None
        preflight_hash = None
    if type(nonce) is not int or not (
        now_ms - _NONCE_PAST_WINDOW_MS < nonce < now_ms + _NONCE_FUTURE_WINDOW_MS
    ):
        raise SignerPolicyError("recovery nonce is outside Hyperliquid's time window")
    synthetic = {
        "recovery_command_id": "internal-test-" + recovery.recovery_hash[:32],
        "permit_id": "internal-test-permit-" + recovery.recovery_hash[:24],
        "parent_command_id": parent_command_id,
        "incident_id": recovery.incident_id,
        "recovery_hash": recovery.recovery_hash,
        "worker_id": "internal-golden-vector",
        "fencing_token": 1,
    }
    signing_authority_hash = domain_hash(
        "trading-harness/internal-recovery-signing-authority/v1", synthetic
    )
    return _freeze_signed_recovery(
        recovery,
        action=action,
        material=material,
        source_hash=source_hash,
        account=account,
        signer_address=signer_address,
        nonce=nonce,
        expires_after_ms=recovery.expires_at_ms,
        now_ms=now_ms,
        recovery_command_id=synthetic["recovery_command_id"],  # type: ignore[arg-type]
        permit_id=synthetic["permit_id"],  # type: ignore[arg-type]
        parent_command_id=parent_command_id,
        preflight_hash=preflight_hash,
        original_attempt_id=original_attempt_id,
        original_nonce=original_nonce,
        worker_id="internal-golden-vector",
        fencing_token=1,
        safety_policy_hash=policy.safety_policy_hash,
        signing_authority_hash=signing_authority_hash,
        permit_expires_at_ms=recovery.expires_at_ms,
        lease_expires_at_ms=recovery.expires_at_ms,
        wallet=wallet,
        sign_l1_action=sign_l1_action,
    )


def sign_recovery_action(
    recovery: RecoveryAction,
    *,
    store: ExecutionStore,
    recovery_command_id: str,
    worker_id: str,
    fencing_token: int,
    evidence: HyperliquidAccountSnapshot | AttemptRecord,
    policy: SignerPolicy,
    wallet: object,
    nonce_allocator: NonceAllocator | None,
    clock: Clock = lambda: datetime.now(timezone.utc),
    sign_l1_action: SignL1Action | None = None,
) -> SignedRecoveryEnvelope:
    """Consume one durable TESTNET authority, sign once, and freeze the wire."""

    if type(store) is not ExecutionStore:
        raise TypeError("store must be an exact ExecutionStore")
    if not isinstance(
        recovery,
        (ReduceOnlyCloseAction, CancelByCloidAction, NoopFenceAction),
    ):
        raise TypeError("recovery must be a typed RecoveryAction")
    if not isinstance(policy, SignerPolicy):
        raise TypeError("policy must be SignerPolicy")
    checked_command_id = _text(recovery_command_id, "recovery_command_id")
    checked_worker_id = _text(worker_id, "worker_id")
    if type(fencing_token) is not int or fencing_token <= 0:
        raise TypeError("fencing_token must be a positive integer")
    if not callable(clock):
        raise TypeError("clock must be callable")
    try:
        now = clock()
    except Exception as error:
        raise ValidationError(f"signer clock failed: {type(error).__name__}") from error
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("signer clock must return a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    now_ms = _datetime_ms(now, "signer clock")

    command = ExecutionStore.get_recovery_command(store, checked_command_id)
    if not isinstance(command, RecoveryCommand):
        raise SignerPolicyError("store returned an invalid recovery command")
    # Reject a static/foreign close identifier before transitioning the
    # durable claim into the conservative `signing` state.  This preserves
    # liveness without moving any wallet, nonce, or SDK operation ahead of the
    # store's sole signing capability transition.
    if isinstance(recovery, ReduceOnlyCloseAction):
        _require_derived_live_close_cloid(recovery, command)

    # This store transition is the sole public recovery-signing capability.
    # It happens before any wallet lookup, nonce allocation, or SDK access.
    authority = ExecutionStore.require_recovery_signing_authority(
        store,
        checked_command_id,
        checked_worker_id,
        fencing_token,
        at=now,
    )
    if not isinstance(authority, RecoverySigningAuthority):
        raise SignerPolicyError("store returned an invalid recovery signing authority")
    command = ExecutionStore.get_recovery_command(store, checked_command_id)
    if not isinstance(command, RecoveryCommand):
        raise SignerPolicyError("store returned an invalid recovery command")

    selected_evidence = evidence
    if isinstance(recovery, NoopFenceAction):
        if not isinstance(evidence, AttemptRecord):
            raise SignerPolicyError("noop fence requires persisted attempt evidence")
        persisted_attempt = ExecutionStore.get_attempt(store, command.parent_command_id)
        if evidence != persisted_attempt:
            raise SignerPolicyError("noop evidence is not the exact durable parent attempt")
        selected_evidence = persisted_attempt
    (
        action,
        material,
        source_hash,
        asset_ids,
        cloids,
        original_nonce,
    ) = _validated_recovery_action(
        recovery,
        evidence=selected_evidence,
        incident_id=authority.incident_id,
        parent_command_id=authority.parent_command_id,
        now_ms=now_ms,
    )
    policy = _verified_recovery_policy(policy)
    account = _validate_recovery_allowlists(
        recovery,
        policy=policy,
        store=store,
        command=command,
        asset_ids=asset_ids,
        cloids=cloids,
    )
    permit_expires_at_ms, lease_expires_at_ms = _validate_durable_recovery_binding(
        recovery,
        store=store,
        command=command,
        authority=authority,
        material=material,
        source_hash=source_hash,
        policy=policy,
        worker_id=checked_worker_id,
        fencing_token=fencing_token,
    )
    expires_after_ms = min(
        recovery.expires_at_ms,
        permit_expires_at_ms,
        lease_expires_at_ms,
        now_ms + policy.maximum_expiry_horizon_ms,
        now_ms + _MAX_EXPIRY_HORIZON_MS,
    )
    if expires_after_ms - now_ms < policy.minimum_expiry_remaining_ms:
        raise SignerPolicyError("durable recovery authority expires too soon")

    signer_address = _wallet_address(wallet)
    if signer_address != account.signer_address:
        raise SignerPolicyError("injected wallet does not match recovery signer policy")
    if recovery.kind is RecoveryKind.NOOP_FENCE:
        if nonce_allocator is not None:
            raise SignerPolicyError("noop fence must not allocate or replace its original nonce")
        if (
            original_nonce is None
            or authority.original_nonce is None
            or original_nonce != authority.original_nonce
        ):
            raise SignerPolicyError("noop fence lost its durable original nonce")
        nonce = original_nonce
    else:
        if authority.original_attempt_id is not None or authority.original_nonce is not None:
            raise SignerPolicyError("non-noop authority unexpectedly binds an original attempt")
        if not callable(getattr(nonce_allocator, "allocate", None)):
            raise SignerPolicyError("close and cancel recovery require a nonce allocator")
        nonce = nonce_allocator.allocate()  # type: ignore[union-attr]
    if type(nonce) is not int or nonce < 0:
        raise SignerPolicyError("recovery nonce is invalid")
    if not now_ms - _NONCE_PAST_WINDOW_MS < nonce < now_ms + _NONCE_FUTURE_WINDOW_MS:
        raise SignerPolicyError("recovery nonce is outside Hyperliquid's time window")
    return _freeze_signed_recovery(
        recovery,
        action=action,
        material=material,
        source_hash=source_hash,
        account=account,
        signer_address=signer_address,
        nonce=nonce,
        expires_after_ms=expires_after_ms,
        now_ms=now_ms,
        recovery_command_id=command.recovery_command_id,
        permit_id=command.permit_id,
        parent_command_id=command.parent_command_id,
        preflight_hash=command.preflight_hash,
        original_attempt_id=command.original_attempt_id,
        original_nonce=command.original_nonce,
        worker_id=checked_worker_id,
        fencing_token=fencing_token,
        safety_policy_hash=policy.safety_policy_hash,
        signing_authority_hash=authority.authority_hash,
        permit_expires_at_ms=permit_expires_at_ms,
        lease_expires_at_ms=lease_expires_at_ms,
        wallet=wallet,
        sign_l1_action=sign_l1_action,
    )


__all__ = (
    "OFFICIAL_SDK_DISTRIBUTION",
    "OFFICIAL_SDK_VERSION",
    "MAX_PROTECTED_NOTIONAL",
    "MAX_PROTECTED_QUANTITY",
    "RECOVERY_SAFETY_POLICY_HASH_DOMAIN",
    "RECOVERY_SIGNING_ENABLED",
    "RECOVERY_WIRE_ACTION_HASH_DOMAIN",
    "SIGNED_ENVELOPE_HASH_DOMAIN",
    "SIGNATURE_HASH_DOMAIN",
    "SIGNER_BINDING_HASH_DOMAIN",
    "HyperliquidSignerError",
    "NonceAllocator",
    "Signature",
    "SignedActionEnvelope",
    "SignedRecoveryEnvelope",
    "SignerDependencyError",
    "SignerOutputError",
    "SignerPolicy",
    "SignerPolicyError",
    "SigningAccount",
    "load_official_sign_l1_action",
    "official_sdk_available",
    "sign_protected_action",
    "sign_recovery_action",
)
