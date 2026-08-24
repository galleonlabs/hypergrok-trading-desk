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
from .planning import RiskTicket, RiskTicketStatus

if TYPE_CHECKING:
    from .execution_store import TrustedApproval


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
        expected_confirmation = f"approve {ticket.ticket_id} {ticket.ticket_hash[:12]}"
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


__all__ = (
    "PlanApproval",
    "TestnetApprovalAuthority",
    "verified_execution_approval",
)
