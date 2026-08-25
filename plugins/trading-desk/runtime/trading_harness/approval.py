"""Local testnet-only approval tokens bound to an exact protected risk ticket.

This is not an MCP tool and never accepts approval from chat.  A trusted local
UI may hold the HMAC key and call this module after an explicit terminal/UI
confirmation.  Mainnet approval is deliberately absent; it requires a later
hardware-backed/asymmetric authority and independent review.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
from typing import TYPE_CHECKING

from .canonical import canonical_json, domain_hash
from .domain import Environment
from .errors import StateConflict, ValidationError
from .hyperliquid_recovery import (
    CancelByCloidAction,
    NoopFenceAction,
    RecoveryAction,
    RecoveryKind,
    ReduceOnlyCloseAction,
    recovery_action_material,
)
from .planning import RiskTicket, RiskTicketStatus

if TYPE_CHECKING:
    from .execution_store import IncidentRecord, RecoveryPermit, TrustedApproval


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be non-empty trimmed text")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PlanApproval:
    approval_id: str
    ticket_id: str
    ticket_hash: str
    plan_hash: str
    account_id: str
    environment: Environment
    audience: str
    approver_id: str
    issued_at: datetime
    expires_at: datetime
    key_id: str
    mac: str

    def __post_init__(self) -> None:
        for field in (
            "approval_id",
            "ticket_id",
            "account_id",
            "audience",
            "approver_id",
            "key_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in ("ticket_hash", "plan_hash", "mac"):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid approval environment") from error
        if self.environment is not Environment.TESTNET:
            raise ValidationError("local HMAC approvals are testnet-only")
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValidationError("approval must expire after issuance")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def payload(self) -> dict[str, object]:
        return {
            "domain": "trading-harness/testnet-plan-approval/v1",
            "approval_id": self.approval_id,
            "ticket_id": self.ticket_id,
            "ticket_hash": self.ticket_hash,
            "plan_hash": self.plan_hash,
            "account_id": self.account_id,
            "environment": self.environment.value,
            "audience": self.audience,
            "approver_id": self.approver_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }

    @property
    def token_hash(self) -> str:
        return domain_hash(
            "trading-harness/approval-token-record/v1",
            {"payload": self.payload(), "mac": self.mac},
        )

    def redacted_dict(self) -> dict[str, object]:
        return {
            **self.payload(),
            "token_hash": self.token_hash,
            "mac_redacted": True,
        }


class TestnetApprovalAuthority:
    """Issue/verify bounded tokens; the secret is injected by a trusted process."""

    def __init__(self, secret: bytes, *, key_id: str, audience: str) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValidationError("approval secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.key_id = _text(key_id, "key_id", 128)
        self.audience = _text(audience, "audience", 128)

    def _mac(self, payload: dict[str, object]) -> str:
        return hmac.new(
            self._secret,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        ticket: RiskTicket,
        *,
        approval_id: str,
        approver_id: str,
        confirmation: str,
        at: datetime,
        ttl_seconds: int = 60,
    ) -> PlanApproval:
        if not isinstance(ticket, RiskTicket):
            raise TypeError("ticket must be RiskTicket")
        if ticket.status is not RiskTicketStatus.AWAITING_APPROVAL or ticket.plan is None:
            raise StateConflict("only an awaiting protected ticket may be approved")
        if ticket.plan.entry.environment is not Environment.TESTNET:
            raise StateConflict("local approval authority is testnet-only")
        now = _utc(at, "at")
        if not ticket.created_at <= now < ticket.expires_at:
            raise StateConflict("risk ticket is not active")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValidationError("approval ttl_seconds must be from 1 to 300")
        expected_confirmation = f"approve {ticket.ticket_id} {ticket.ticket_hash[:16]}"
        if confirmation != expected_confirmation:
            raise ValidationError("trusted UI confirmation does not match the exact ticket")
        expires = min(ticket.expires_at, now + timedelta(seconds=ttl_seconds))
        provisional = PlanApproval(
            approval_id=_text(approval_id, "approval_id"),
            ticket_id=ticket.ticket_id,
            ticket_hash=ticket.ticket_hash,
            plan_hash=ticket.plan.plan_hash,
            account_id=ticket.plan.entry.account_id,
            environment=Environment.TESTNET,
            audience=self.audience,
            approver_id=_text(approver_id, "approver_id"),
            issued_at=now,
            expires_at=expires,
            key_id=self.key_id,
            mac="0" * 64,
        )
        return replace(provisional, mac=self._mac(provisional.payload()))

    def verify(
        self,
        approval: PlanApproval,
        ticket: RiskTicket,
        *,
        at: datetime,
    ) -> str:
        if not isinstance(approval, PlanApproval):
            raise TypeError("approval must be PlanApproval")
        if not isinstance(ticket, RiskTicket):
            raise TypeError("ticket must be RiskTicket")
        now = _utc(at, "at")
        if approval.key_id != self.key_id or approval.audience != self.audience:
            raise StateConflict("approval targets another authority or audience")
        expected = self._mac(approval.payload())
        if not hmac.compare_digest(approval.mac, expected):
            raise StateConflict("approval MAC is invalid")
        if not approval.issued_at <= now < approval.expires_at:
            raise StateConflict("approval is not active")
        if ticket.plan is None or (
            approval.ticket_id != ticket.ticket_id
            or approval.ticket_hash != ticket.ticket_hash
            or approval.plan_hash != ticket.plan.plan_hash
            or approval.account_id != ticket.plan.entry.account_id
            or approval.environment is not ticket.plan.entry.environment
        ):
            raise StateConflict("approval does not bind the exact risk ticket")
        return approval.token_hash


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    """Authenticated, short-lived authority for one exact safety action."""

    permit_id: str
    parent_command_id: str
    incident_id: str
    incident_code: str
    expected_incident_revision: int
    kind: RecoveryKind
    environment: Environment
    account_id: str
    source_hash: str
    preflight_hash: str | None
    recovery_hash: str
    safety_policy_hash: str
    original_attempt_id: str | None
    original_nonce: int | None
    issuer_id: str
    audience: str
    issued_at: datetime
    expires_at: datetime
    key_id: str
    mac: str

    def __post_init__(self) -> None:
        for field in (
            "permit_id",
            "parent_command_id",
            "incident_id",
            "incident_code",
            "account_id",
            "issuer_id",
            "audience",
            "key_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in ("source_hash", "recovery_hash", "safety_policy_hash", "mac"):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        if self.preflight_hash is not None:
            object.__setattr__(
                self,
                "preflight_hash",
                _hash(self.preflight_hash, "preflight_hash"),
            )
        if not isinstance(self.kind, RecoveryKind):
            try:
                object.__setattr__(self, "kind", RecoveryKind(self.kind))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid recovery authorization kind") from error
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid recovery authorization environment") from error
        if self.environment is not Environment.TESTNET:
            raise ValidationError("local recovery authorization is testnet-only")
        if type(self.expected_incident_revision) is not int or self.expected_incident_revision <= 0:
            raise ValidationError("expected incident revision must be positive")
        if self.original_attempt_id is not None:
            object.__setattr__(
                self,
                "original_attempt_id",
                _text(self.original_attempt_id, "original_attempt_id"),
            )
        if self.original_nonce is not None and (
            type(self.original_nonce) is not int or self.original_nonce < 0
        ):
            raise ValidationError("original nonce must be a non-negative integer")
        if self.kind is RecoveryKind.NOOP_FENCE:
            if (
                self.preflight_hash is None
                or self.original_attempt_id is None
                or self.original_nonce is None
            ):
                raise ValidationError("noop recovery authorization lacks original attempt")
        elif (
            self.preflight_hash is not None
            or self.original_attempt_id is not None
            or self.original_nonce is not None
        ):
            raise ValidationError("only noop recovery authorization binds an original attempt")
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if not issued < expires <= issued + timedelta(seconds=15):
            raise ValidationError("recovery authorization expiry exceeds 15 seconds")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def payload(self) -> dict[str, object]:
        return {
            "domain": "trading-harness/testnet-recovery-authorization/v1",
            "permit_id": self.permit_id,
            "parent_command_id": self.parent_command_id,
            "incident_id": self.incident_id,
            "incident_code": self.incident_code,
            "expected_incident_revision": self.expected_incident_revision,
            "kind": self.kind.value,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "source_hash": self.source_hash,
            "preflight_hash": self.preflight_hash,
            "recovery_hash": self.recovery_hash,
            "safety_policy_hash": self.safety_policy_hash,
            "original_attempt_id": self.original_attempt_id,
            "original_nonce": self.original_nonce,
            "issuer_id": self.issuer_id,
            "audience": self.audience,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }

    @property
    def token_hash(self) -> str:
        return domain_hash(
            "trading-harness/recovery-authorization-record/v1",
            {"payload": self.payload(), "mac": self.mac},
        )

    def redacted_dict(self) -> dict[str, object]:
        return {
            **self.payload(),
            "token_hash": self.token_hash,
            "mac_redacted": True,
        }


def _recovery_source(recovery: RecoveryAction) -> str:
    if isinstance(recovery, ReduceOnlyCloseAction):
        return recovery.position_snapshot_hash
    if isinstance(recovery, CancelByCloidAction):
        return recovery.account_snapshot_hash
    if isinstance(recovery, NoopFenceAction):
        return recovery.ambiguous_attempt_hash
    raise TypeError("recovery must be a typed RecoveryAction")


class TestnetRecoveryAuthority:
    """Authenticate automated safety actions under a separate local trust root."""

    def __init__(
        self,
        secret: bytes,
        *,
        key_id: str,
        issuer_id: str,
        audience: str,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValidationError("recovery secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.key_id = _text(key_id, "key_id", 128)
        self.issuer_id = _text(issuer_id, "issuer_id", 128)
        self.audience = _text(audience, "audience", 128)

    def _mac(self, payload: dict[str, object]) -> str:
        return hmac.new(
            self._secret,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        recovery: RecoveryAction,
        incident: "IncidentRecord",
        *,
        permit_id: str,
        safety_policy_hash: str,
        at: datetime,
        ttl_seconds: int = 10,
    ) -> RecoveryAuthorization:
        from .execution_store import IncidentRecord

        if not isinstance(
            recovery,
            (ReduceOnlyCloseAction, CancelByCloidAction, NoopFenceAction),
        ):
            raise TypeError("recovery must be a typed RecoveryAction")
        if not isinstance(incident, IncidentRecord):
            raise TypeError("incident must be a persisted IncidentRecord")
        if (
            incident.command_id is None
            or incident.incident_id != recovery.incident_id
            or incident.state != "open"
            or incident.severity != "critical"
        ):
            raise StateConflict("recovery authorization requires its open critical incident")
        if recovery.network.environment is not Environment.TESTNET:
            raise StateConflict("local recovery authority is testnet-only")
        now = _utc(at, "at")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 15:
            raise ValidationError("recovery ttl_seconds must be from 1 to 15")
        action_expiry = datetime.fromtimestamp(
            recovery.expires_at_ms / 1_000,
            tz=timezone.utc,
        )
        if now >= action_expiry:
            raise StateConflict("recovery action is already expired")
        expires = min(action_expiry, now + timedelta(seconds=ttl_seconds))
        original_attempt_id = (
            recovery.attempt_id if isinstance(recovery, NoopFenceAction) else None
        )
        original_nonce = (
            recovery.original_nonce if isinstance(recovery, NoopFenceAction) else None
        )
        preflight_hash = (
            recovery.preflight_hash if isinstance(recovery, NoopFenceAction) else None
        )
        provisional = RecoveryAuthorization(
            permit_id=_text(permit_id, "permit_id"),
            parent_command_id=incident.command_id,
            incident_id=incident.incident_id,
            incident_code=incident.code,
            expected_incident_revision=incident.revision,
            kind=recovery.kind,
            environment=Environment.TESTNET,
            account_id=recovery.account_id,
            source_hash=_recovery_source(recovery),
            preflight_hash=preflight_hash,
            recovery_hash=recovery.recovery_hash,
            safety_policy_hash=_hash(safety_policy_hash, "safety_policy_hash"),
            original_attempt_id=original_attempt_id,
            original_nonce=original_nonce,
            issuer_id=self.issuer_id,
            audience=self.audience,
            issued_at=now,
            expires_at=expires,
            key_id=self.key_id,
            mac="0" * 64,
        )
        # Reconstructing the material here rejects a mutated frozen dataclass.
        recovery_action_material(recovery)
        return replace(provisional, mac=self._mac(provisional.payload()))

    def verify(
        self,
        authorization: RecoveryAuthorization,
        recovery: RecoveryAction,
        incident: "IncidentRecord",
        *,
        safety_policy_hash: str,
        at: datetime,
    ) -> str:
        from .execution_store import IncidentRecord

        if not isinstance(authorization, RecoveryAuthorization):
            raise TypeError("authorization must be RecoveryAuthorization")
        if not isinstance(incident, IncidentRecord):
            raise TypeError("incident must be a persisted IncidentRecord")
        if not isinstance(
            recovery,
            (ReduceOnlyCloseAction, CancelByCloidAction, NoopFenceAction),
        ):
            raise TypeError("recovery must be a typed RecoveryAction")
        now = _utc(at, "at")
        if (
            authorization.key_id != self.key_id
            or authorization.issuer_id != self.issuer_id
            or authorization.audience != self.audience
        ):
            raise StateConflict("recovery authorization targets another authority")
        if not hmac.compare_digest(
            authorization.mac,
            self._mac(authorization.payload()),
        ):
            raise StateConflict("recovery authorization MAC is invalid")
        if not authorization.issued_at <= now < authorization.expires_at:
            raise StateConflict("recovery authorization is not active")
        material = recovery_action_material(recovery)
        del material
        expected_source = _recovery_source(recovery)
        expected_preflight = (
            recovery.preflight_hash if isinstance(recovery, NoopFenceAction) else None
        )
        expected_attempt = (
            recovery.attempt_id if isinstance(recovery, NoopFenceAction) else None
        )
        expected_nonce = (
            recovery.original_nonce if isinstance(recovery, NoopFenceAction) else None
        )
        if (
            incident.incident_id != authorization.incident_id
            or incident.command_id != authorization.parent_command_id
            or incident.code != authorization.incident_code
            or incident.revision != authorization.expected_incident_revision
            or incident.state != "open"
            or incident.severity != "critical"
            or recovery.incident_id != authorization.incident_id
            or recovery.account_id != authorization.account_id
            or recovery.kind is not authorization.kind
            or recovery.network.environment is not authorization.environment
            or recovery.recovery_hash != authorization.recovery_hash
            or expected_source != authorization.source_hash
            or expected_preflight != authorization.preflight_hash
            or expected_attempt != authorization.original_attempt_id
            or expected_nonce != authorization.original_nonce
            or _hash(safety_policy_hash, "safety_policy_hash")
            != authorization.safety_policy_hash
        ):
            raise StateConflict("recovery authorization does not bind exact current evidence")
        return authorization.token_hash


def verified_execution_approval(
    authority: TestnetApprovalAuthority,
    approval: PlanApproval,
    ticket: RiskTicket,
    *,
    at: datetime,
) -> "TrustedApproval":
    """Verify cryptographic authority before creating the store's opaque record."""

    if not isinstance(authority, TestnetApprovalAuthority):
        raise TypeError("authority must be TestnetApprovalAuthority")
    token_hash = authority.verify(approval, ticket, at=at)
    # Local import keeps the durable store independent from approval-key code.
    from .execution_store import TrustedApproval

    return TrustedApproval(
        approval_id=approval.approval_id,
        ticket_hash=approval.ticket_hash,
        token_hash=token_hash,
        approver_id=approval.approver_id,
        audience=approval.audience,
        environment=approval.environment,
        account_id=approval.account_id,
        issued_at=approval.issued_at,
        expires_at=approval.expires_at,
    )


def verified_recovery_permit(
    authority: TestnetRecoveryAuthority,
    authorization: RecoveryAuthorization,
    recovery: RecoveryAction,
    incident: "IncidentRecord",
    *,
    safety_policy_hash: str,
    at: datetime,
) -> "RecoveryPermit":
    """Verify the local MAC before creating a durable recovery permit."""

    if not isinstance(authority, TestnetRecoveryAuthority):
        raise TypeError("authority must be TestnetRecoveryAuthority")
    token_hash = authority.verify(
        authorization,
        recovery,
        incident,
        safety_policy_hash=safety_policy_hash,
        at=at,
    )
    from .execution_store import RecoveryPermit

    return RecoveryPermit(
        permit_id=authorization.permit_id,
        token_hash=token_hash,
        parent_command_id=authorization.parent_command_id,
        incident_id=authorization.incident_id,
        kind=authorization.kind.value,
        environment=authorization.environment,
        account_id=authorization.account_id,
        source_hash=authorization.source_hash,
        preflight_hash=authorization.preflight_hash,
        recovery_hash=authorization.recovery_hash,
        recovery_material=recovery_action_material(recovery),
        safety_policy_hash=authorization.safety_policy_hash,
        original_attempt_id=authorization.original_attempt_id,
        original_nonce=authorization.original_nonce,
        issuer_id=authorization.issuer_id,
        audience=authorization.audience,
        issued_at=authorization.issued_at,
        expires_at=authorization.expires_at,
    )


__all__ = (
    "PlanApproval",
    "RecoveryAuthorization",
    "TestnetApprovalAuthority",
    "TestnetRecoveryAuthority",
    "verified_execution_approval",
    "verified_recovery_permit",
)
