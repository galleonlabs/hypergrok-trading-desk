"""Authenticated authority for bounded infrastructure learning on TESTNET.

This grant deliberately says nothing about strategy profitability.  It permits
small, attended TESTNET experiments so the harness can collect execution and
review evidence while strategy/mainnet authority remains unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import re
from typing import Any, Mapping

from .canonical import canonical_decimal, canonical_json, domain_hash
from .domain import Environment
from .errors import StateConflict, ValidationError
from .policy import exact_decimal


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _text(value: object, field: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return _utc(value, "time").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 string") from error
    return _utc(parsed, field)


def _positive(value: Decimal | str | int, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed <= 0:
        raise ValidationError(f"{field} must be positive")
    return parsed


def infrastructure_grant_confirmation(
    *,
    grant_id: str,
    generation: int,
    account_id: str,
    allowed_instruments: tuple[str, ...],
    risk_policy_hash: str,
    max_loss: Decimal | str | int,
    max_notional: Decimal | str | int,
    max_leverage: Decimal | str | int,
    ttl_seconds: int,
) -> str:
    """Return the exact operator phrase bound to every grant scope field."""

    checked_grant = _text(grant_id, "grant_id")
    checked_account = _text(account_id, "account_id")
    if type(generation) is not int or generation <= 0:
        raise ValidationError("generation must be positive")
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 86_400:
        raise ValidationError("ttl_seconds must be from 60 to 86400")
    instruments = tuple(sorted(allowed_instruments))
    if (
        not instruments
        or len(instruments) != len(set(instruments))
        or any(
            not isinstance(item, str) or not _INSTRUMENT_RE.fullmatch(item)
            for item in instruments
        )
    ):
        raise ValidationError("allowed_instruments are invalid")
    policy_hash = _hash(risk_policy_hash, "risk_policy_hash")
    loss = _positive(max_loss, "max_loss")
    notional = _positive(max_notional, "max_notional")
    leverage = _positive(max_leverage, "max_leverage")
    if leverage > Decimal("2"):
        raise ValidationError("infrastructure learning leverage cannot exceed 2x")
    scope_hash = domain_hash(
        "trading-harness/infrastructure-learning-confirmation/v1",
        {
            "grant_id": checked_grant,
            "generation": generation,
            "account_id": checked_account,
            "environment": "testnet",
            "allowed_instruments": instruments,
            "risk_policy_hash": policy_hash,
            "max_loss": loss,
            "max_notional": notional,
            "max_leverage": leverage,
            "ttl_seconds": ttl_seconds,
            "profitability_qualified": False,
            "mainnet_authorized": False,
        },
    )
    return f"enable testnet learning {checked_grant} {checked_account} {scope_hash[:16]}"


@dataclass(frozen=True, slots=True)
class SignedInfrastructureGrant:
    grant_id: str
    generation: int
    account_id: str
    environment: Environment
    allowed_instruments: tuple[str, ...]
    risk_policy_hash: str
    max_loss: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    issuer_id: str
    key_id: str
    audience: str
    mac: str

    def __post_init__(self) -> None:
        for field in ("grant_id", "account_id", "issuer_id", "key_id", "audience"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if type(self.generation) is not int or self.generation <= 0:
            raise ValidationError("generation must be positive")
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("grant environment is invalid") from error
        if self.environment is not Environment.TESTNET:
            raise ValidationError("infrastructure learning grants are testnet-only")
        instruments = tuple(sorted(self.allowed_instruments))
        if (
            not instruments
            or len(instruments) != len(set(instruments))
            or any(
                not isinstance(item, str) or not _INSTRUMENT_RE.fullmatch(item)
                for item in instruments
            )
        ):
            raise ValidationError("allowed_instruments are invalid")
        object.__setattr__(self, "allowed_instruments", instruments)
        object.__setattr__(
            self,
            "risk_policy_hash",
            _hash(self.risk_policy_hash, "risk_policy_hash"),
        )
        for field in ("max_loss", "max_notional", "max_leverage"):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        if self.max_leverage > Decimal("2"):
            raise ValidationError("infrastructure learning leverage cannot exceed 2x")
        issued = _utc(self.issued_at, "issued_at")
        starts = _utc(self.not_before, "not_before")
        expires = _utc(self.expires_at, "expires_at")
        if not issued <= starts < expires <= issued + timedelta(hours=24):
            raise ValidationError("grant lifetime is outside the 24-hour testnet bound")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "not_before", starts)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "mac", _hash(self.mac, "mac"))

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "infrastructure_learning_grant.v1",
            "grant_id": self.grant_id,
            "generation": self.generation,
            "purpose": "infrastructure_learning",
            "account_id": self.account_id,
            "environment": self.environment.value,
            "allowed_instruments": list(self.allowed_instruments),
            "risk_policy_hash": self.risk_policy_hash,
            "max_loss": canonical_decimal(self.max_loss),
            "max_notional": canonical_decimal(self.max_notional),
            "max_leverage": canonical_decimal(self.max_leverage),
            "profitability_qualified": False,
            "mainnet_authorized": False,
            "issued_at": self.issued_at,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "issuer_id": self.issuer_id,
            "key_id": self.key_id,
            "audience": self.audience,
        }

    @property
    def grant_hash(self) -> str:
        return domain_hash(
            "trading-harness/infrastructure-learning-grant/v1",
            {"payload": self.payload(), "mac": self.mac},
        )

    def is_active(self, at: datetime) -> bool:
        """Return time validity only; this does not authenticate the MAC."""

        checked = _utc(at, "at")
        return self.not_before <= checked < self.expires_at

    def as_dict(self) -> dict[str, object]:
        """Return the complete portable signed artifact with exact values."""

        return {
            "schema_version": "signed_infrastructure_learning_grant.v1",
            "grant_id": self.grant_id,
            "generation": self.generation,
            "purpose": "infrastructure_learning",
            "account_id": self.account_id,
            "environment": self.environment.value,
            "allowed_instruments": list(self.allowed_instruments),
            "risk_policy_hash": self.risk_policy_hash,
            "max_loss": canonical_decimal(self.max_loss),
            "max_notional": canonical_decimal(self.max_notional),
            "max_leverage": canonical_decimal(self.max_leverage),
            "profitability_qualified": False,
            "mainnet_authorized": False,
            "issued_at": _time_text(self.issued_at),
            "not_before": _time_text(self.not_before),
            "expires_at": _time_text(self.expires_at),
            "issuer_id": self.issuer_id,
            "key_id": self.key_id,
            "audience": self.audience,
            "mac": self.mac,
            "grant_hash": self.grant_hash,
        }


@dataclass(frozen=True, slots=True)
class TrustedInfrastructureGrant:
    grant_hash: str
    grant_id: str
    generation: int
    account_id: str
    environment: Environment
    allowed_instruments: tuple[str, ...]
    risk_policy_hash: str
    max_loss: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    issuer_id: str
    audience: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_hash", _hash(self.grant_hash, "grant_hash"))
        for field in ("grant_id", "account_id", "issuer_id", "audience"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if type(self.generation) is not int or self.generation <= 0:
            raise ValidationError("generation must be positive")
        if self.environment is not Environment.TESTNET:
            raise ValidationError("trusted infrastructure grant is testnet-only")
        instruments = tuple(sorted(self.allowed_instruments))
        if (
            not instruments
            or len(instruments) != len(set(instruments))
            or any(not _INSTRUMENT_RE.fullmatch(item) for item in instruments)
        ):
            raise ValidationError("trusted allowed_instruments are invalid")
        object.__setattr__(self, "allowed_instruments", instruments)
        object.__setattr__(
            self,
            "risk_policy_hash",
            _hash(self.risk_policy_hash, "risk_policy_hash"),
        )
        for field in ("max_loss", "max_notional", "max_leverage"):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        if self.max_leverage > Decimal("2"):
            raise ValidationError("trusted grant leverage cannot exceed 2x")
        for field in ("issued_at", "not_before", "expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if not self.issued_at <= self.not_before < self.expires_at:
            raise ValidationError("trusted grant timestamps are invalid")

    def is_active(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return self.not_before <= checked < self.expires_at


class TestnetInfrastructureGrantAuthority:
    """Issue and verify one bounded, non-profitability TESTNET capability."""

    def __init__(
        self,
        secret: bytes,
        *,
        issuer_id: str,
        key_id: str,
        audience: str,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValidationError("grant secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.issuer_id = _text(issuer_id, "issuer_id")
        self.key_id = _text(key_id, "key_id")
        self.audience = _text(audience, "audience")

    def _mac(self, payload: dict[str, object]) -> str:
        return hmac.new(
            self._secret,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        *,
        grant_id: str,
        generation: int,
        account_id: str,
        allowed_instruments: tuple[str, ...],
        risk_policy_hash: str,
        max_loss: Decimal | str | int,
        max_notional: Decimal | str | int,
        max_leverage: Decimal | str | int,
        confirmation: str,
        at: datetime,
        ttl_seconds: int = 3_600,
    ) -> SignedInfrastructureGrant:
        now = _utc(at, "at")
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 86_400:
            raise ValidationError("ttl_seconds must be from 60 to 86400")
        checked_grant = _text(grant_id, "grant_id")
        checked_account = _text(account_id, "account_id")
        policy_hash = _hash(risk_policy_hash, "risk_policy_hash")
        expected = infrastructure_grant_confirmation(
            grant_id=checked_grant,
            generation=generation,
            account_id=checked_account,
            allowed_instruments=allowed_instruments,
            risk_policy_hash=policy_hash,
            max_loss=max_loss,
            max_notional=max_notional,
            max_leverage=max_leverage,
            ttl_seconds=ttl_seconds,
        )
        if confirmation != expected:
            raise ValidationError("trusted grant confirmation does not match")
        provisional = SignedInfrastructureGrant(
            grant_id=checked_grant,
            generation=generation,
            account_id=checked_account,
            environment=Environment.TESTNET,
            allowed_instruments=allowed_instruments,
            risk_policy_hash=policy_hash,
            max_loss=max_loss,
            max_notional=max_notional,
            max_leverage=max_leverage,
            issued_at=now,
            not_before=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            issuer_id=self.issuer_id,
            key_id=self.key_id,
            audience=self.audience,
            mac="0" * 64,
        )
        return replace(provisional, mac=self._mac(provisional.payload()))

    def verify(
        self,
        grant: SignedInfrastructureGrant,
        *,
        at: datetime,
    ) -> TrustedInfrastructureGrant:
        if not isinstance(grant, SignedInfrastructureGrant):
            raise TypeError("grant must be SignedInfrastructureGrant")
        if (
            grant.issuer_id != self.issuer_id
            or grant.key_id != self.key_id
            or grant.audience != self.audience
        ):
            raise StateConflict("grant targets another authority")
        if not hmac.compare_digest(grant.mac, self._mac(grant.payload())):
            raise StateConflict("grant MAC is invalid")
        if not grant.not_before <= _utc(at, "at") < grant.expires_at:
            raise StateConflict("grant is not active")
        return TrustedInfrastructureGrant(
            grant_hash=grant.grant_hash,
            grant_id=grant.grant_id,
            generation=grant.generation,
            account_id=grant.account_id,
            environment=grant.environment,
            allowed_instruments=grant.allowed_instruments,
            risk_policy_hash=grant.risk_policy_hash,
            max_loss=grant.max_loss,
            max_notional=grant.max_notional,
            max_leverage=grant.max_leverage,
            issuer_id=grant.issuer_id,
            audience=grant.audience,
            issued_at=grant.issued_at,
            not_before=grant.not_before,
            expires_at=grant.expires_at,
        )


def signed_infrastructure_grant_from_dict(
    value: Mapping[str, Any],
) -> SignedInfrastructureGrant:
    """Reconstruct and verify the internal hash of one portable grant artifact.

    Authentication is completed separately by
    :meth:`TestnetInfrastructureGrantAuthority.verify`; parsing alone never
    converts the artifact into ``TrustedInfrastructureGrant``.
    """

    if not isinstance(value, Mapping):
        raise TypeError("signed infrastructure grant must be a mapping")
    document = dict(value)
    expected = {
        "schema_version",
        "grant_id",
        "generation",
        "purpose",
        "account_id",
        "environment",
        "allowed_instruments",
        "risk_policy_hash",
        "max_loss",
        "max_notional",
        "max_leverage",
        "profitability_qualified",
        "mainnet_authorized",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer_id",
        "key_id",
        "audience",
        "mac",
        "grant_hash",
    }
    if set(document) != expected:
        raise ValidationError("signed infrastructure grant fields are unsupported")
    if (
        document["schema_version"]
        != "signed_infrastructure_learning_grant.v1"
        or document["purpose"] != "infrastructure_learning"
        or document["environment"] != "testnet"
        or document["profitability_qualified"] is not False
        or document["mainnet_authorized"] is not False
        or type(document["generation"]) is not int
    ):
        raise ValidationError("signed infrastructure grant boundary is invalid")
    instruments = document["allowed_instruments"]
    if not isinstance(instruments, list) or any(
        not isinstance(item, str) for item in instruments
    ):
        raise ValidationError("signed grant allowed_instruments are invalid")
    for field in ("max_loss", "max_notional", "max_leverage"):
        if not isinstance(document[field], str):
            raise ValidationError(f"signed grant {field} must be an exact string")
    try:
        grant = SignedInfrastructureGrant(
            grant_id=document["grant_id"],
            generation=document["generation"],
            account_id=document["account_id"],
            environment=Environment.TESTNET,
            allowed_instruments=tuple(instruments),
            risk_policy_hash=document["risk_policy_hash"],
            max_loss=exact_decimal(document["max_loss"], field="max_loss"),
            max_notional=exact_decimal(
                document["max_notional"], field="max_notional"
            ),
            max_leverage=exact_decimal(
                document["max_leverage"], field="max_leverage"
            ),
            issued_at=_parse_time(document["issued_at"], "issued_at"),
            not_before=_parse_time(document["not_before"], "not_before"),
            expires_at=_parse_time(document["expires_at"], "expires_at"),
            issuer_id=document["issuer_id"],
            key_id=document["key_id"],
            audience=document["audience"],
            mac=document["mac"],
        )
    except (TypeError, ValueError, ValidationError) as error:
        if isinstance(error, ValidationError):
            raise
        raise ValidationError("signed infrastructure grant is invalid") from error
    if document["grant_hash"] != grant.grant_hash:
        raise ValidationError("signed infrastructure grant hash does not match")
    return grant


__all__ = (
    "SignedInfrastructureGrant",
    "TestnetInfrastructureGrantAuthority",
    "TrustedInfrastructureGrant",
    "infrastructure_grant_confirmation",
    "signed_infrastructure_grant_from_dict",
)
