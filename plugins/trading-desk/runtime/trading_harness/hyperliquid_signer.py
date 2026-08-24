"""Isolated, fail-closed Hyperliquid signing boundary.

The boundary accepts exactly one reviewed unsigned artifact type: a three-leg
IOC entry plus reduce-only market SL and TP grouped as ``normalTpsl``.  It
independently revalidates the compact wire shape and field insertion order,
binds it to explicit network/account/asset policy, commits a nonce through an
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
    IncidentRecord,
    SignedEnvelopeEvidence,
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
RECOVERY_SIGNING_ENABLED = False
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
    """Closed signer allowlists with mainnet disabled unless doubly enabled."""

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
        parsed = json.loads(self.wire_json)
        if not isinstance(parsed, dict):
            raise SignerOutputError("signed wire no longer decodes to an object")
        return parsed

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
    recovery_kind: RecoveryKind
    incident_id: str
    source_hash: str
    recovery_hash: str
    recovery_material_json: str
    nonce: int
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
        parsed = json.loads(self.wire_json)
        if not isinstance(parsed, dict):
            raise SignerOutputError("signed recovery wire is not an object")
        return parsed

    def recovery_material(self) -> dict[str, object]:
        parsed = json.loads(self.recovery_material_json)
        if not isinstance(parsed, dict):
            raise SignerOutputError("recovery binding is not an object")
        return parsed

    def verify_integrity(self) -> None:
        if not isinstance(self.network, HyperliquidNetwork) or not isinstance(
            self.recovery_kind, RecoveryKind
        ):
            raise SignerOutputError("signed recovery network or kind is invalid")
        if self.network is HyperliquidNetwork.MAINNET:
            raise SignerOutputError("mainnet recovery wire is hard-disabled")
        if not (
            type(self.signed_at_ms) is int
            and type(self.expires_after_ms) is int
            and self.signed_at_ms < self.expires_after_ms
        ):
            raise SignerOutputError("signed recovery expiry ordering is invalid")
        if hashlib.sha256(self.wire_bytes).hexdigest() != self.wire_hash:
            raise SignerOutputError("signed recovery wire hash mismatch")
        envelope = self.envelope()
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
        if domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material) != self.recovery_hash:
            raise SignerOutputError("signed recovery binding hash mismatch")
        action = envelope.get("action")
        if not isinstance(action, dict) or material.get("action") != action:
            raise SignerOutputError("signed recovery action differs from its binding")
        try:
            _validate_recovery_material(self.recovery_kind, material, action)
        except SignerPolicyError as error:
            raise SignerOutputError("signed recovery action binding is invalid") from error
        if material.get("incident_id") != self.incident_id:
            raise SignerOutputError("signed recovery incident binding mismatch")
        if material.get("expires_at_ms") != self.expires_after_ms:
            raise SignerOutputError("signed recovery material expiry mismatch")
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
        binding = {
            "artifact_kind": self.artifact_kind,
            "network": self.network.value,
            "account_id": self.account_id,
            "main_account_address": self.main_account_address,
            "signer_address": self.signer_address,
            "vault_address": self.vault_address,
            "incident_id": self.incident_id,
            "recovery_hash": self.recovery_hash,
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
            "recovery_kind": self.recovery_kind.value,
            "incident_id": self.incident_id,
            "source_hash": self.source_hash,
            "recovery_hash": self.recovery_hash,
            "nonce": self.nonce,
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
    if set(material) != expected:
        raise SignerPolicyError("close recovery binding fields are unsupported")
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
    if set(material) != expected:
        raise SignerPolicyError("cancel recovery binding fields are unsupported")
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
    incident: IncidentRecord,
    now_ms: int,
) -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    tuple[int, ...],
    tuple[str, ...],
    int | None,
]:
    if not isinstance(incident, IncidentRecord) or incident.state != "open":
        raise SignerPolicyError("recovery requires the bound open persisted incident")
    if recovery.incident_id != incident.incident_id:
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
        source_hash = recovery.position_snapshot_hash
        asset_ids = (recovery.asset_id,)
        cloids = (recovery.cloid,)
    elif isinstance(recovery, CancelByCloidAction):
        if not isinstance(evidence, HyperliquidAccountSnapshot):
            raise SignerPolicyError("cancel recovery requires fresh account evidence")
        if evidence.snapshot_hash != recovery.account_snapshot_hash:
            raise SignerPolicyError("cancel recovery snapshot hash does not match evidence")
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
        source_hash = recovery.account_snapshot_hash
        asset_ids = recovery.asset_ids
        cloids = tuple(request.cloid for request in recovery.requests)
    elif isinstance(recovery, NoopFenceAction):
        if not isinstance(evidence, AttemptRecord):
            raise SignerPolicyError("noop fence requires persisted attempt evidence")
        if evidence.state != "unknown" or evidence.response_hash is not None:
            raise SignerPolicyError("noop fence evidence is not an unknown attempt")
        if incident.command_id != evidence.command_id:
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
    """Sign one incident-bound close, owned cancel, or same-nonce noop."""

    if not isinstance(policy, SignerPolicy):
        raise TypeError("policy must be SignerPolicy")
    if not isinstance(
        recovery,
        (ReduceOnlyCloseAction, CancelByCloidAction, NoopFenceAction),
    ):
        raise TypeError("recovery must be a typed RecoveryAction")
    if not isinstance(recovery.network, HyperliquidNetwork):
        raise SignerPolicyError("recovery network is invalid")
    if not callable(clock):
        raise TypeError("clock must be callable")
    now_ms = _utc_ms(clock)
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
        incident=incident,
        now_ms=now_ms,
    )
    if recovery.kind not in policy.allowed_recovery_kinds:
        raise SignerPolicyError("recovery kind is not explicitly allowlisted")
    if recovery.network not in policy.allowed_networks:
        raise SignerPolicyError("recovery network is not allowlisted")
    if recovery.network is HyperliquidNetwork.MAINNET:
        raise SignerPolicyError("mainnet recovery signing is hard-disabled")
    account = policy.account(recovery.account_id)
    if account.main_account_address != recovery.main_account_address:
        raise SignerPolicyError("recovery main account differs from signer policy")
    signer_address = _wallet_address(wallet)
    if signer_address != account.signer_address:
        raise SignerPolicyError("injected wallet does not match recovery signer policy")
    if not set(asset_ids).issubset(policy.allowed_asset_ids):
        raise SignerPolicyError("recovery asset is not allowlisted")
    if not set(cloids).issubset(account.owned_cloids):
        raise SignerPolicyError("recovery references a foreign CLOID")
    remaining = recovery.expires_at_ms - now_ms
    if not policy.minimum_expiry_remaining_ms <= remaining <= min(
        policy.maximum_expiry_horizon_ms,
        _MAX_EXPIRY_HORIZON_MS,
    ):
        raise SignerPolicyError("recovery expiry is not within the short signer bound")

    if recovery.kind is RecoveryKind.NOOP_FENCE:
        if nonce_allocator is not None:
            raise SignerPolicyError("noop fence must not allocate or replace its original nonce")
        if original_nonce is None:
            raise SignerPolicyError("noop fence lacks its original nonce")
        nonce = original_nonce
    else:
        if not callable(getattr(nonce_allocator, "allocate", None)):
            raise SignerPolicyError("close and cancel recovery require a nonce allocator")
        nonce = nonce_allocator.allocate()  # type: ignore[union-attr]
    if type(nonce) is not int or nonce < 0:
        raise SignerPolicyError("recovery nonce is invalid")
    if not now_ms - _NONCE_PAST_WINDOW_MS < nonce < now_ms + _NONCE_FUTURE_WINDOW_MS:
        raise SignerPolicyError("recovery nonce is outside Hyperliquid's time window")

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
            recovery.expires_at_ms,
            recovery.network is HyperliquidNetwork.MAINNET,
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
        "expiresAfter": recovery.expires_at_ms,
    }
    wire_json = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    result = SignedRecoveryEnvelope(
        network=recovery.network,
        account_id=recovery.account_id,
        main_account_address=recovery.main_account_address,
        signer_address=signer_address,
        vault_address=account.vault_address,
        recovery_kind=recovery.kind,
        incident_id=recovery.incident_id,
        source_hash=source_hash,
        recovery_hash=recovery.recovery_hash,
        recovery_material_json=canonical_json(material),
        nonce=nonce,
        expires_after_ms=recovery.expires_at_ms,
        signed_at_ms=now_ms,
        signature=signature,
        signature_hash=domain_hash(SIGNATURE_HASH_DOMAIN, signature.as_dict()),
        envelope_hash=domain_hash(SIGNED_ENVELOPE_HASH_DOMAIN, envelope),
        signer_binding_hash=domain_hash(
            SIGNER_BINDING_HASH_DOMAIN,
            {
                "artifact_kind": "recovery",
                "network": recovery.network.value,
                "account_id": recovery.account_id,
                "main_account_address": recovery.main_account_address,
                "signer_address": signer_address,
                "vault_address": account.vault_address,
                "incident_id": recovery.incident_id,
                "recovery_hash": recovery.recovery_hash,
            },
        ),
        wire_json=wire_json,
        wire_hash=hashlib.sha256(wire_json.encode("utf-8")).hexdigest(),
        signing_implementation=implementation,
    )
    result.verify_integrity()
    return result


def sign_recovery_action(
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
    """Refuse recovery signing until a durable RecoveryPermit exists."""

    del (
        recovery,
        evidence,
        incident,
        policy,
        wallet,
        nonce_allocator,
        clock,
        sign_l1_action,
    )
    raise SignerPolicyError(
        "recovery signing is hard-disabled until durable RecoveryPermit support"
    )


__all__ = (
    "OFFICIAL_SDK_DISTRIBUTION",
    "OFFICIAL_SDK_VERSION",
    "MAX_PROTECTED_NOTIONAL",
    "MAX_PROTECTED_QUANTITY",
    "RECOVERY_SIGNING_ENABLED",
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
