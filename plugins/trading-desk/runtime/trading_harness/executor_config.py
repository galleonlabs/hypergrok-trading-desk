"""Strict, file-only configuration for the isolated TESTNET executor.

The executor configuration is deliberately narrower than a general
application configuration system.  It has one schema version, one venue, and
one network.  Values are read from a bounded TOML file; environment variables
cannot override any field.  Monetary values are exact decimal strings and
every managed path is absolute and disjoint from every other managed path.

This module contains no credential lookup, network client, or write adapter.
The public addresses and Keychain labels identify what a later isolated
runtime is expected to load; private key material is not part of the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path, PurePath
import re
import tomllib
from typing import Mapping

from .canonical import domain_hash
from .domain import Environment
from .errors import ValidationError
from .policy import exact_decimal


EXECUTOR_CONFIG_SCHEMA_VERSION = 2
EXECUTOR_CONFIG_HASH_DOMAIN = "trading-harness/executor-config/v2"
MAX_CONFIG_BYTES = 64 * 1024

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_ENVIRONMENT_OVERRIDE_KEYS = frozenset(
    {
        "TRADING_HARNESS_ENVIRONMENT",
        "TRADING_HARNESS_LIVE_TRADING",
        "HYPERLIQUID_NETWORK",
        "HYPERLIQUID_MAINNET",
        "HYPERLIQUID_API_URL",
        "HL_NETWORK",
        "HL_API_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSLKEYLOGFILE",
    }
)
_ENVIRONMENT_OVERRIDE_PREFIX = "TRADING_HARNESS_EXECUTOR_"

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "environment",
        "venue",
        "node_id",
        "executor_uid",
        "research_uid",
        "control_uid",
        "account_id",
        "main_account_address",
        "api_wallet_address",
        "daily_loss_limit",
        "max_reserved_loss",
        "max_reserved_notional",
        "max_leverage",
        "risk_policy_hash",
        "allowed_instruments",
        "allowed_asset_ids",
        "recovery_cloids",
        "settlement_currency",
        "poll_interval_ms",
        "reconcile_interval_ms",
        "credential",
        "approval_credential",
        "recovery_credential",
        "grant_credential",
        "paths",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {"provider", "service", "account", "timeout_seconds"}
)
_CREDENTIAL_OPTIONAL_KEYS = frozenset({"keychain_path"})
_PATH_KEYS = frozenset(
    {
        "execution_database",
        "nonce_database",
        "daily_loss_database",
        "learning_database",
        "staging_database",
        "control_socket",
    }
)


class ExecutorConfigError(ValidationError):
    """The executor configuration is malformed or attempts to widen scope."""


class ExecutorConfigDrift(ExecutorConfigError):
    """Durable state is bound to a different canonical configuration."""


def _keys(value: object, expected: frozenset[str], *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ExecutorConfigError(f"{field} must be a TOML table")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ExecutorConfigError(f"{field} keys are invalid ({'; '.join(details)})")
    return value


def _credential_keys(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ExecutorConfigError(f"{field} must be a TOML table")
    actual = set(value)
    missing = sorted(_CREDENTIAL_KEYS - actual)
    unknown = sorted(
        actual - _CREDENTIAL_KEYS - _CREDENTIAL_OPTIONAL_KEYS
    )
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ExecutorConfigError(f"{field} keys are invalid: " + "; ".join(details))
    return value


def _text(value: object, *, field: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ExecutorConfigError(f"{field} must be bounded, trimmed text")
    return value


def _identifier(value: object, *, field: str) -> str:
    parsed = _text(value, field=field)
    if not _IDENTIFIER_RE.fullmatch(parsed):
        raise ExecutorConfigError(f"{field} is not a valid identifier")
    return parsed


def _address(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ExecutorConfigError(f"{field} must be a lowercase 20-byte address")
    return value


def _integer(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutorConfigError(
            f"{field} must be an integer from {minimum} through {maximum}"
        )
    return value


def _exact_positive(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ExecutorConfigError(f"{field} must be an exact TOML string")
    try:
        parsed = exact_decimal(value, field=field)
    except ValidationError as error:
        raise ExecutorConfigError(str(error)) from error
    if parsed <= 0:
        raise ExecutorConfigError(f"{field} must be greater than zero")
    return parsed


def _absolute_path(value: object, *, field: str) -> Path:
    if isinstance(value, os.PathLike):
        try:
            value = os.fspath(value)
        except TypeError as error:
            raise ExecutorConfigError(f"{field} must be a filesystem path") from error
    text = _text(value, field=field, maximum=4096)
    path = Path(text)
    if not path.is_absolute():
        raise ExecutorConfigError(f"{field} must be absolute")
    normalized = os.path.normpath(text)
    if normalized != text or "\x00" in text:
        raise ExecutorConfigError(f"{field} must be a normalized absolute path")
    if path == Path(path.anchor):
        raise ExecutorConfigError(f"{field} may not be a filesystem root")
    return path


def _is_ancestor(parent: PurePath, child: PurePath) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return parent != child


def _reject_overlapping_paths(paths: Mapping[str, Path]) -> None:
    # Resolve existing symlink components for comparison only.  The original,
    # normalized absolute spelling remains part of the canonical config hash.
    # This prevents two differently-spelled names from aliasing the same
    # managed object while keeping the loaded configuration transparent.
    selected = tuple(
        sorted((name, path, path.resolve(strict=False)) for name, path in paths.items())
    )
    for index, (left_name, left_original, left) in enumerate(selected):
        for right_name, right_original, right in selected[index + 1 :]:
            if left == right or _is_ancestor(left, right) or _is_ancestor(right, left):
                raise ExecutorConfigError(
                    f"managed paths overlap: {left_name} and {right_name}"
                )
            if left_original.exists() and right_original.exists():
                try:
                    aliases = left_original.samefile(right_original)
                except OSError as error:
                    raise ExecutorConfigError("managed path aliases cannot be verified") from error
                if aliases:
                    raise ExecutorConfigError(
                        f"managed paths overlap: {left_name} and {right_name}"
                    )


def _reject_environment_overrides(environ: Mapping[str, str]) -> None:
    if not isinstance(environ, Mapping):
        raise TypeError("environ must be a mapping")
    forbidden = sorted(
        key
        for key in environ
        if isinstance(key, str)
        and (
            key.startswith(_ENVIRONMENT_OVERRIDE_PREFIX)
            or key in _ENVIRONMENT_OVERRIDE_KEYS
        )
    )
    if forbidden:
        # Names are safe to report; values are intentionally never read.
        raise ExecutorConfigError(
            "executor environment overrides are forbidden: " + ", ".join(forbidden)
        )


@dataclass(frozen=True, slots=True)
class ExecutorCredentialConfig:
    provider: str
    service: str
    account: str
    timeout_seconds: int
    keychain_path: str | None = None

    def __post_init__(self) -> None:
        if self.provider != "macos_keychain_generic_password":
            raise ExecutorConfigError(
                "credential.provider must be macos_keychain_generic_password"
            )
        object.__setattr__(self, "service", _identifier(self.service, field="credential.service"))
        object.__setattr__(self, "account", _identifier(self.account, field="credential.account"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _integer(
                self.timeout_seconds,
                field="credential.timeout_seconds",
                minimum=1,
                maximum=10,
            ),
        )
        if self.keychain_path is not None:
            path = Path(self.keychain_path)
            if (
                not path.is_absolute()
                or os.path.normpath(self.keychain_path) != self.keychain_path
                or len(self.keychain_path) > 1024
            ):
                raise ExecutorConfigError(
                    "credential.keychain_path must be normalized and absolute"
                )
            object.__setattr__(self, "keychain_path", str(path))


@dataclass(frozen=True, slots=True)
class ExecutorPaths:
    execution_database: Path
    nonce_database: Path
    daily_loss_database: Path
    learning_database: Path
    staging_database: Path
    control_socket: Path

    def __post_init__(self) -> None:
        normalized = {
            field: _absolute_path(getattr(self, field), field=f"paths.{field}")
            for field in _PATH_KEYS
        }
        _reject_overlapping_paths(normalized)
        learning_parent = normalized["learning_database"].parent
        if (
            learning_parent.resolve(strict=False)
            != normalized["staging_database"].parent.resolve(strict=False)
        ):
            raise ExecutorConfigError(
                "learning and staging databases must share one learning-state parent"
            )
        _reject_overlapping_paths(
            {
                "execution_state_parent": normalized["execution_database"].parent,
                "nonce_state_parent": normalized["nonce_database"].parent,
                "daily_loss_state_parent": normalized["daily_loss_database"].parent,
                "control_socket_state_parent": normalized["control_socket"].parent,
                "learning_state_parent": learning_parent,
            }
        )
        for field, value in normalized.items():
            object.__setattr__(self, field, value)


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    schema_version: int
    environment: Environment
    venue: str
    node_id: str
    executor_uid: int
    research_uid: int
    control_uid: int
    account_id: str
    main_account_address: str
    api_wallet_address: str
    daily_loss_limit: Decimal
    max_reserved_loss: Decimal
    max_reserved_notional: Decimal
    max_leverage: Decimal
    risk_policy_hash: str
    allowed_instruments: tuple[str, ...]
    allowed_asset_ids: tuple[int, ...]
    recovery_cloids: tuple[str, ...]
    settlement_currency: str
    poll_interval_ms: int
    reconcile_interval_ms: int
    credential: ExecutorCredentialConfig
    approval_credential: ExecutorCredentialConfig
    recovery_credential: ExecutorCredentialConfig
    grant_credential: ExecutorCredentialConfig
    paths: ExecutorPaths

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != EXECUTOR_CONFIG_SCHEMA_VERSION
        ):
            raise ExecutorConfigError(
                f"schema_version must be {EXECUTOR_CONFIG_SCHEMA_VERSION}"
            )
        try:
            environment = (
                self.environment
                if isinstance(self.environment, Environment)
                else Environment(self.environment)
            )
        except (TypeError, ValueError) as error:
            raise ExecutorConfigError("environment is invalid") from error
        if environment is not Environment.TESTNET:
            raise ExecutorConfigError("only TESTNET execution is supported")
        object.__setattr__(self, "environment", environment)
        if self.venue != "hyperliquid":
            raise ExecutorConfigError("venue must be hyperliquid")
        object.__setattr__(self, "node_id", _identifier(self.node_id, field="node_id"))
        for field in ("executor_uid", "research_uid", "control_uid"):
            object.__setattr__(
                self,
                field,
                _integer(
                    getattr(self, field),
                    field=field,
                    minimum=1,
                    maximum=2_147_483_647,
                ),
            )
        if len({self.executor_uid, self.research_uid, self.control_uid}) != 3:
            raise ExecutorConfigError(
                "executor_uid, research_uid, and control_uid must be distinct"
            )
        object.__setattr__(self, "account_id", _identifier(self.account_id, field="account_id"))
        object.__setattr__(
            self,
            "main_account_address",
            _address(self.main_account_address, field="main_account_address"),
        )
        object.__setattr__(
            self,
            "api_wallet_address",
            _address(self.api_wallet_address, field="api_wallet_address"),
        )
        if self.main_account_address == self.api_wallet_address:
            raise ExecutorConfigError(
                "api_wallet_address must be distinct from main_account_address"
            )
        object.__setattr__(
            self,
            "daily_loss_limit",
            _exact_positive(self.daily_loss_limit, field="daily_loss_limit"),
        )
        for field in ("max_reserved_loss", "max_reserved_notional", "max_leverage"):
            object.__setattr__(
                self,
                field,
                _exact_positive(getattr(self, field), field=field),
            )
        if self.max_reserved_loss > self.daily_loss_limit:
            raise ExecutorConfigError(
                "max_reserved_loss cannot exceed daily_loss_limit"
            )
        if self.max_leverage > Decimal("2"):
            raise ExecutorConfigError("max_leverage cannot exceed 2x")
        if not isinstance(self.risk_policy_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.risk_policy_hash
        ):
            raise ExecutorConfigError("risk_policy_hash must be a SHA-256 digest")
        instruments = tuple(self.allowed_instruments)
        asset_ids = tuple(self.allowed_asset_ids)
        if (
            not instruments
            or len(instruments) != len(asset_ids)
            or len(instruments) != len(set(instruments))
            or len(asset_ids) != len(set(asset_ids))
            or any(
                not isinstance(value, str)
                or not _IDENTIFIER_RE.fullmatch(value)
                for value in instruments
            )
            or any(type(value) is not int or not 0 <= value <= 1_000_000 for value in asset_ids)
        ):
            raise ExecutorConfigError(
                "allowed_instruments and allowed_asset_ids must be unique aligned lists"
            )
        object.__setattr__(self, "allowed_instruments", instruments)
        object.__setattr__(self, "allowed_asset_ids", asset_ids)
        recovery_cloids = tuple(self.recovery_cloids)
        if (
            not recovery_cloids
            or len(recovery_cloids) > 32
            or len(recovery_cloids) != len(set(recovery_cloids))
            or any(
                not isinstance(value, str) or not _CLOID_RE.fullmatch(value)
                for value in recovery_cloids
            )
        ):
            raise ExecutorConfigError(
                "recovery_cloids must contain one to 32 unique 128-bit lowercase IDs"
            )
        object.__setattr__(self, "recovery_cloids", recovery_cloids)
        currency = _text(
            self.settlement_currency, field="settlement_currency", maximum=16
        )
        if not _CURRENCY_RE.fullmatch(currency):
            raise ExecutorConfigError("settlement_currency is invalid")
        object.__setattr__(self, "settlement_currency", currency)
        object.__setattr__(
            self,
            "poll_interval_ms",
            _integer(
                self.poll_interval_ms,
                field="poll_interval_ms",
                minimum=100,
                maximum=60_000,
            ),
        )
        object.__setattr__(
            self,
            "reconcile_interval_ms",
            _integer(
                self.reconcile_interval_ms,
                field="reconcile_interval_ms",
                minimum=250,
                maximum=300_000,
            ),
        )
        for field in (
            "credential",
            "approval_credential",
            "recovery_credential",
            "grant_credential",
        ):
            if not isinstance(getattr(self, field), ExecutorCredentialConfig):
                raise ExecutorConfigError(
                    f"{field} must be ExecutorCredentialConfig"
                )
        keychain_items = {
            (item.service, item.account)
            for item in (
                self.credential,
                self.approval_credential,
                self.recovery_credential,
                self.grant_credential,
            )
        }
        if len(keychain_items) != 4:
            raise ExecutorConfigError(
                "signer, approval, recovery, and grant credentials must be distinct"
            )
        if not isinstance(self.paths, ExecutorPaths):
            raise ExecutorConfigError("paths must be ExecutorPaths")

    @property
    def config_hash(self) -> str:
        """Canonical hash of effective configuration, excluding file layout/order."""

        # ``Path`` is intentionally not part of the canonical serializer's
        # general vocabulary.  Convert only these schema-reviewed path fields
        # to their normalized strings here.
        return domain_hash(
            EXECUTOR_CONFIG_HASH_DOMAIN,
            {
                "schema_version": self.schema_version,
                "environment": self.environment,
                "venue": self.venue,
                "node_id": self.node_id,
                "executor_uid": self.executor_uid,
                "research_uid": self.research_uid,
                "control_uid": self.control_uid,
                "account_id": self.account_id,
                "main_account_address": self.main_account_address,
                "api_wallet_address": self.api_wallet_address,
                "daily_loss_limit": self.daily_loss_limit,
                "max_reserved_loss": self.max_reserved_loss,
                "max_reserved_notional": self.max_reserved_notional,
                "max_leverage": self.max_leverage,
                "risk_policy_hash": self.risk_policy_hash,
                "allowed_instruments": self.allowed_instruments,
                "allowed_asset_ids": self.allowed_asset_ids,
                "recovery_cloids": self.recovery_cloids,
                "settlement_currency": self.settlement_currency,
                "poll_interval_ms": self.poll_interval_ms,
                "reconcile_interval_ms": self.reconcile_interval_ms,
                "credential": self.credential,
                "approval_credential": self.approval_credential,
                "recovery_credential": self.recovery_credential,
                "grant_credential": self.grant_credential,
                "paths": {
                    "execution_database": str(self.paths.execution_database),
                    "nonce_database": str(self.paths.nonce_database),
                    "daily_loss_database": str(self.paths.daily_loss_database),
                    "learning_database": str(self.paths.learning_database),
                    "staging_database": str(self.paths.staging_database),
                    "control_socket": str(self.paths.control_socket),
                },
            },
        )


def _reject_float(_value: str) -> float:
    raise ExecutorConfigError(
        "TOML floating-point values are forbidden; use an exact string for decimals"
    )


def parse_executor_config(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ExecutorConfig:
    """Parse one complete, strict TESTNET executor TOML document.

    ``environ`` exists for deterministic testing.  Runtime callers should
    omit it, causing the real process environment to be inspected for
    forbidden override attempts.  Environment values are never read.
    """

    if not isinstance(text, str):
        raise TypeError("executor config must be text")
    encoded = text.encode("utf-8")
    if not encoded or len(encoded) > MAX_CONFIG_BYTES or "\x00" in text:
        raise ExecutorConfigError("executor config size is invalid")
    _reject_environment_overrides(os.environ if environ is None else environ)
    try:
        decoded = tomllib.loads(text, parse_float=_reject_float)
    except ExecutorConfigError:
        raise
    except (tomllib.TOMLDecodeError, ValueError, TypeError) as error:
        # TOML's parser rejects duplicate keys and duplicate table definitions.
        raise ExecutorConfigError("executor config is not strict TOML") from error
    root = _keys(decoded, _ROOT_KEYS, field="root")
    credential = _credential_keys(root["credential"], field="credential")
    approval_credential = _credential_keys(
        root["approval_credential"],
        field="approval_credential",
    )
    recovery_credential = _credential_keys(
        root["recovery_credential"],
        field="recovery_credential",
    )
    grant_credential = _credential_keys(
        root["grant_credential"],
        field="grant_credential",
    )
    paths = _keys(root["paths"], _PATH_KEYS, field="paths")
    return ExecutorConfig(
        schema_version=root["schema_version"],  # type: ignore[arg-type]
        environment=root["environment"],  # type: ignore[arg-type]
        venue=root["venue"],  # type: ignore[arg-type]
        node_id=root["node_id"],  # type: ignore[arg-type]
        executor_uid=root["executor_uid"],  # type: ignore[arg-type]
        research_uid=root["research_uid"],  # type: ignore[arg-type]
        control_uid=root["control_uid"],  # type: ignore[arg-type]
        account_id=root["account_id"],  # type: ignore[arg-type]
        main_account_address=root["main_account_address"],  # type: ignore[arg-type]
        api_wallet_address=root["api_wallet_address"],  # type: ignore[arg-type]
        daily_loss_limit=root["daily_loss_limit"],  # type: ignore[arg-type]
        max_reserved_loss=root["max_reserved_loss"],  # type: ignore[arg-type]
        max_reserved_notional=root["max_reserved_notional"],  # type: ignore[arg-type]
        max_leverage=root["max_leverage"],  # type: ignore[arg-type]
        risk_policy_hash=root["risk_policy_hash"],  # type: ignore[arg-type]
        allowed_instruments=tuple(root["allowed_instruments"]),  # type: ignore[arg-type]
        allowed_asset_ids=tuple(root["allowed_asset_ids"]),  # type: ignore[arg-type]
        recovery_cloids=tuple(root["recovery_cloids"]),  # type: ignore[arg-type]
        settlement_currency=root["settlement_currency"],  # type: ignore[arg-type]
        poll_interval_ms=root["poll_interval_ms"],  # type: ignore[arg-type]
        reconcile_interval_ms=root["reconcile_interval_ms"],  # type: ignore[arg-type]
        credential=ExecutorCredentialConfig(
            provider=credential["provider"],  # type: ignore[arg-type]
            service=credential["service"],  # type: ignore[arg-type]
            account=credential["account"],  # type: ignore[arg-type]
            timeout_seconds=credential["timeout_seconds"],  # type: ignore[arg-type]
            keychain_path=credential.get("keychain_path"),  # type: ignore[arg-type]
        ),
        approval_credential=ExecutorCredentialConfig(
            provider=approval_credential["provider"],  # type: ignore[arg-type]
            service=approval_credential["service"],  # type: ignore[arg-type]
            account=approval_credential["account"],  # type: ignore[arg-type]
            timeout_seconds=approval_credential["timeout_seconds"],  # type: ignore[arg-type]
            keychain_path=approval_credential.get("keychain_path"),  # type: ignore[arg-type]
        ),
        recovery_credential=ExecutorCredentialConfig(
            provider=recovery_credential["provider"],  # type: ignore[arg-type]
            service=recovery_credential["service"],  # type: ignore[arg-type]
            account=recovery_credential["account"],  # type: ignore[arg-type]
            timeout_seconds=recovery_credential["timeout_seconds"],  # type: ignore[arg-type]
            keychain_path=recovery_credential.get("keychain_path"),  # type: ignore[arg-type]
        ),
        grant_credential=ExecutorCredentialConfig(
            provider=grant_credential["provider"],  # type: ignore[arg-type]
            service=grant_credential["service"],  # type: ignore[arg-type]
            account=grant_credential["account"],  # type: ignore[arg-type]
            timeout_seconds=grant_credential["timeout_seconds"],  # type: ignore[arg-type]
            keychain_path=grant_credential.get("keychain_path"),  # type: ignore[arg-type]
        ),
        paths=ExecutorPaths(
            execution_database=paths["execution_database"],  # type: ignore[arg-type]
            nonce_database=paths["nonce_database"],  # type: ignore[arg-type]
            daily_loss_database=paths["daily_loss_database"],  # type: ignore[arg-type]
            learning_database=paths["learning_database"],  # type: ignore[arg-type]
            staging_database=paths["staging_database"],  # type: ignore[arg-type]
            control_socket=paths["control_socket"],  # type: ignore[arg-type]
        ),
    )


def load_executor_config(
    path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> ExecutorConfig:
    """Read a bounded regular TOML file and return its canonical configuration."""

    selected = Path(path)
    if not selected.is_absolute():
        raise ExecutorConfigError("executor config path must be absolute")
    try:
        if selected.is_symlink() or not selected.is_file():
            raise ExecutorConfigError("executor config must be a regular non-symlink file")
        metadata = selected.stat()
        if metadata.st_mode & 0o077:
            raise ExecutorConfigError(
                "executor config must not be group/world accessible"
            )
        if hasattr(os, "geteuid") and metadata.st_uid not in {
            os.geteuid(),
            0,
        }:
            raise ExecutorConfigError(
                "executor config must be process-owned or admin-owned"
            )
        raw = selected.read_bytes()
    except ExecutorConfigError:
        raise
    except OSError as error:
        raise ExecutorConfigError("executor config cannot be read") from error
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ExecutorConfigError("executor config size is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExecutorConfigError("executor config must be UTF-8") from error
    return parse_executor_config(text, environ=environ)


__all__ = (
    "EXECUTOR_CONFIG_HASH_DOMAIN",
    "EXECUTOR_CONFIG_SCHEMA_VERSION",
    "ExecutorConfig",
    "ExecutorConfigDrift",
    "ExecutorConfigError",
    "ExecutorCredentialConfig",
    "ExecutorPaths",
    "load_executor_config",
    "parse_executor_config",
)
