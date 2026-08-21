"""Pure domain objects for the deterministic trading harness.

The types in this module describe intent and authority; they do not place an
order, read an account, sign a payload, or make a network request.  Every
dataclass is frozen so state changes must be represented as new, auditable
objects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Any, Mapping, TypeVar

from .canonical import validate_decimal_bounds


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EnumT = TypeVar("_EnumT", bound=Enum)


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Environment(_StringEnum):
    TESTNET = "testnet"
    MAINNET = "mainnet"
    SHADOW = "shadow"


class Side(_StringEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(_StringEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class AuthorizationModel(_StringEnum):
    INFRASTRUCTURE = "infrastructure"
    PER_TICKET_HUMAN = "per_ticket_human"
    SYSTEMATIC_POLICY = "systematic_policy"


class GrantType(_StringEnum):
    INFRASTRUCTURE_TESTNET = "infrastructure_testnet"
    STRATEGY_TESTNET = "strategy_testnet"
    MANUAL_MAINNET_CANARY = "manual_mainnet_canary"
    SYSTEMATIC_TESTNET = "systematic_testnet"
    SYSTEMATIC_SHADOW = "systematic_shadow"
    SYSTEMATIC_MAINNET_CAPPED = "systematic_mainnet_capped"


class GrantState(_StringEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    EXPIRED = "expired"


class AuthorizationState(_StringEnum):
    ISSUED = "issued"
    CONSUMING = "consuming"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    VOIDED = "voided"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty, trimmed string")
    return value


def _enum(value: Any, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as error:
            raise ValueError(f"invalid {field_name}: {value!r}") from error
    raise TypeError(f"{field_name} must be {enum_type.__name__} or str")


def _instant(value: Any, field_name: str) -> datetime:
    if isinstance(value, str):
        encoded = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(encoded)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime or ISO-8601 str")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(
    value: Any,
    field_name: str,
    *,
    optional: bool = False,
) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must not be float; pass Decimal or an exact string"
        )
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be Decimal or an exact string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise ValueError(f"{field_name} must be a non-empty, trimmed decimal")
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"{field_name} is not an exact decimal") from error
    else:
        raise TypeError(f"{field_name} must be Decimal, int, or exact str")
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    validate_decimal_bounds(result, field=field_name)
    return result


def _positive_decimal(value: Any, field_name: str, *, optional: bool) -> Decimal | None:
    result = _decimal(value, field_name, optional=optional)
    if result is not None and result <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result


def _nonnegative_decimal(
    value: Any, field_name: str, *, optional: bool
) -> Decimal | None:
    result = _decimal(value, field_name, optional=optional)
    if result is not None and result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _string_scope(values: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be an iterable of strings, not str")
    try:
        normalized = tuple(sorted({_text(item, field_name) for item in values}))
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of strings") from error
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _string_identities(values: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be an iterable of identities, not str")
    try:
        return tuple(sorted({_text(item, field_name) for item in values}))
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of identities") from error


def _sha256(value: Any, field_name: str) -> str:
    value = _text(value, field_name)
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class SemanticIntent:
    """Immutable economic meaning approved before signer runtime fields exist."""

    intent_id: str
    thesis_id: str
    thesis_version: str
    strategy_version: str
    code_hash: str
    venue: str
    account_id: str
    environment: Environment
    instrument: str
    action: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    expires_at: datetime
    client_order_id: str
    limit_price: Decimal | None = None
    price_bound: Decimal | None = None
    stop_price: Decimal | None = None
    protection_limit_price: Decimal | None = None
    reduce_only: bool = False
    leverage: Decimal | None = None
    max_slippage_bps: Decimal = Decimal("0")
    fee_bps: Decimal = Decimal("0")
    time_in_force: str | None = None
    signal_instance_hash: str | None = None
    allowed_runtime_fields: tuple[str, ...] = (
        "nonce",
        "signature",
        "signing_timestamp",
    )
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "intent_id",
            "thesis_id",
            "thesis_version",
            "strategy_version",
            "code_hash",
            "venue",
            "account_id",
            "instrument",
            "client_order_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))

        action = _text(self.action, "action").lower()
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self, "environment", _enum(self.environment, Environment, "environment")
        )
        object.__setattr__(self, "side", _enum(self.side, Side, "side"))
        object.__setattr__(
            self, "order_type", _enum(self.order_type, OrderType, "order_type")
        )
        object.__setattr__(
            self, "quantity", _positive_decimal(self.quantity, "quantity", optional=False)
        )
        for field_name in (
            "limit_price",
            "price_bound",
            "stop_price",
            "protection_limit_price",
            "leverage",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_decimal(getattr(self, field_name), field_name, optional=True),
            )
        for field_name in ("max_slippage_bps", "fee_bps"):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_decimal(
                    getattr(self, field_name), field_name, optional=False
                ),
            )
        object.__setattr__(self, "expires_at", _instant(self.expires_at, "expires_at"))

        if type(self.reduce_only) is not bool:
            raise TypeError("reduce_only must be bool")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be integer 1")
        if self.time_in_force is not None:
            object.__setattr__(
                self, "time_in_force", _text(self.time_in_force, "time_in_force")
            )
        if self.signal_instance_hash is not None:
            object.__setattr__(
                self,
                "signal_instance_hash",
                _sha256(self.signal_instance_hash, "signal_instance_hash"),
            )

        runtime_fields = _string_scope(
            self.allowed_runtime_fields, "allowed_runtime_fields"
        )
        permitted_runtime_fields = {"nonce", "signature", "signing_timestamp"}
        unexpected = set(runtime_fields) - permitted_runtime_fields
        if unexpected:
            raise ValueError(
                "unsupported allowed_runtime_fields: " + ", ".join(sorted(unexpected))
            )
        object.__setattr__(self, "allowed_runtime_fields", runtime_fields)

        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise ValueError("stop orders require stop_price")
        if self.order_type is OrderType.STOP_LIMIT and (
            self.stop_price is None or self.limit_price is None
        ):
            raise ValueError("stop-limit orders require stop_price and limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders cannot carry limit_price; use price_bound")
        if self.protection_limit_price is not None and self.stop_price is None:
            raise ValueError("protection_limit_price requires stop_price")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SemanticIntent":
        """Parse an intent mapping using the same strict constructor rules."""

        if not isinstance(value, Mapping):
            raise TypeError("SemanticIntent.from_mapping requires a mapping")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class Authorization:
    """A single-use authorization bound to one semantic intent digest."""

    authorization_id: str
    intent_hash: str
    grant_id: str
    account_id: str
    environment: Environment
    issued_at: datetime
    expires_at: datetime
    audience: str
    issuer_id: str = "deployment-authority"
    approver_ids: tuple[str, ...] = ()
    state: AuthorizationState = AuthorizationState.ISSUED

    def __post_init__(self) -> None:
        for field_name in (
            "authorization_id",
            "grant_id",
            "account_id",
            "audience",
            "issuer_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "intent_hash", _sha256(self.intent_hash, "intent_hash"))
        object.__setattr__(
            self, "environment", _enum(self.environment, Environment, "environment")
        )
        object.__setattr__(self, "issued_at", _instant(self.issued_at, "issued_at"))
        object.__setattr__(self, "expires_at", _instant(self.expires_at, "expires_at"))
        object.__setattr__(
            self, "state", _enum(self.state, AuthorizationState, "state")
        )
        object.__setattr__(
            self,
            "approver_ids",
            _string_identities(self.approver_ids, "approver_ids"),
        )
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization expires_at must be after issued_at")

    def is_active(self, at: datetime) -> bool:
        at = _instant(at, "at")
        return (
            self.state is AuthorizationState.ISSUED
            and self.issued_at <= at < self.expires_at
        )

    def with_state(self, state: AuthorizationState) -> "Authorization":
        """Return a new snapshot; persistence owns legal transition checks."""

        return replace(self, state=_enum(state, AuthorizationState, "state"))


_GRANT_ENVIRONMENTS = {
    GrantType.INFRASTRUCTURE_TESTNET: Environment.TESTNET,
    GrantType.STRATEGY_TESTNET: Environment.TESTNET,
    GrantType.MANUAL_MAINNET_CANARY: Environment.MAINNET,
    GrantType.SYSTEMATIC_TESTNET: Environment.TESTNET,
    GrantType.SYSTEMATIC_SHADOW: Environment.SHADOW,
    GrantType.SYSTEMATIC_MAINNET_CAPPED: Environment.MAINNET,
}

_GRANT_AUTHORIZATION_MODELS = {
    GrantType.INFRASTRUCTURE_TESTNET: AuthorizationModel.INFRASTRUCTURE,
    GrantType.STRATEGY_TESTNET: AuthorizationModel.PER_TICKET_HUMAN,
    GrantType.MANUAL_MAINNET_CANARY: AuthorizationModel.PER_TICKET_HUMAN,
    GrantType.SYSTEMATIC_TESTNET: AuthorizationModel.SYSTEMATIC_POLICY,
    GrantType.SYSTEMATIC_SHADOW: AuthorizationModel.SYSTEMATIC_POLICY,
    GrantType.SYSTEMATIC_MAINNET_CAPPED: AuthorizationModel.SYSTEMATIC_POLICY,
}


@dataclass(frozen=True, slots=True)
class DeploymentGrant:
    """Environment-specific permission, separate from scientific evidence."""

    grant_id: str
    thesis_id: str
    thesis_version: str
    strategy_version: str
    code_hash: str
    venue: str
    account_id: str
    environment: Environment
    grant_type: GrantType
    issued_at: datetime
    expires_at: datetime
    authorization_model: AuthorizationModel | None = None
    starts_at: datetime | None = None
    review_at: datetime | None = None
    revoked_at: datetime | None = None
    state: GrantState = GrantState.ACTIVE
    allowed_instruments: tuple[str, ...] = ("*",)
    allowed_actions: tuple[str, ...] = ("*",)
    max_notional: Decimal | None = None
    max_loss: Decimal | None = None
    approver_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "grant_id",
            "thesis_id",
            "thesis_version",
            "strategy_version",
            "code_hash",
            "venue",
            "account_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(
            self, "environment", _enum(self.environment, Environment, "environment")
        )
        object.__setattr__(
            self, "grant_type", _enum(self.grant_type, GrantType, "grant_type")
        )
        object.__setattr__(self, "state", _enum(self.state, GrantState, "state"))

        expected_model = _GRANT_AUTHORIZATION_MODELS[self.grant_type]
        authorization_model = (
            expected_model
            if self.authorization_model is None
            else _enum(
                self.authorization_model,
                AuthorizationModel,
                "authorization_model",
            )
        )
        if authorization_model is not expected_model:
            raise ValueError(
                f"{self.grant_type.value} requires authorization_model "
                f"{expected_model.value}"
            )
        object.__setattr__(self, "authorization_model", authorization_model)

        issued_at = _instant(self.issued_at, "issued_at")
        expires_at = _instant(self.expires_at, "expires_at")
        starts_at = issued_at if self.starts_at is None else _instant(self.starts_at, "starts_at")
        review_at = None if self.review_at is None else _instant(self.review_at, "review_at")
        revoked_at = None if self.revoked_at is None else _instant(self.revoked_at, "revoked_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "review_at", review_at)
        object.__setattr__(self, "revoked_at", revoked_at)

        if starts_at < issued_at:
            raise ValueError("grant starts_at cannot precede issued_at")
        if expires_at <= starts_at:
            raise ValueError("grant expires_at must be after starts_at")
        if review_at is not None and not (starts_at <= review_at <= expires_at):
            raise ValueError("grant review_at must fall within its active interval")
        if revoked_at is not None and revoked_at < issued_at:
            raise ValueError("grant revoked_at cannot precede issued_at")
        if (self.state is GrantState.REVOKED) != (revoked_at is not None):
            raise ValueError("revoked grants require revoked_at, and only revoked grants set it")

        expected_environment = _GRANT_ENVIRONMENTS[self.grant_type]
        if self.environment is not expected_environment:
            raise ValueError(
                f"{self.grant_type.value} is scoped to {expected_environment.value}"
            )

        object.__setattr__(
            self,
            "allowed_instruments",
            _string_scope(self.allowed_instruments, "allowed_instruments"),
        )
        object.__setattr__(
            self,
            "allowed_actions",
            _string_scope(self.allowed_actions, "allowed_actions"),
        )
        for field_name in ("max_notional", "max_loss"):
            object.__setattr__(
                self,
                field_name,
                _positive_decimal(getattr(self, field_name), field_name, optional=True),
            )
        object.__setattr__(
            self,
            "approver_ids",
            _string_identities(self.approver_ids, "approver_ids"),
        )

    @property
    def status(self) -> GrantState:
        """Alias used by registry and API representations."""

        return self.state

    def is_active(self, at: datetime) -> bool:
        at = _instant(at, "at")
        return (
            self.state is GrantState.ACTIVE
            and self.revoked_at is None
            and self.starts_at <= at < self.expires_at
        )

    def matches_scope(self, intent: SemanticIntent) -> bool:
        """Check exact strategy/account scope without checking time or evidence."""

        if not isinstance(intent, SemanticIntent):
            raise TypeError("matches_scope requires SemanticIntent")
        instrument_allowed = (
            "*" in self.allowed_instruments
            or intent.instrument in self.allowed_instruments
        )
        action_allowed = "*" in self.allowed_actions or intent.action in self.allowed_actions
        return (
            self.thesis_id == intent.thesis_id
            and self.thesis_version == intent.thesis_version
            and self.strategy_version == intent.strategy_version
            and self.code_hash == intent.code_hash
            and self.venue == intent.venue
            and self.account_id == intent.account_id
            and self.environment is intent.environment
            and instrument_allowed
            and action_allowed
        )
