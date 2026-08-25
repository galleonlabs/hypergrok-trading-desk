"""Attended local authorization bridge for staged TESTNET learning trades.

This module is deliberately not an MCP tool.  It converts one immutable,
non-authoritative staging document into a durable execution command only after
an operator supplies the exact ticket confirmation required by
``TestnetApprovalAuthority``.  It has no credential, signer, nonce, transport,
or venue dependency; success means only that a protected command is queued in
``ExecutionStore`` for the isolated executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .approval import (
    TestnetApprovalAuthority,
    verified_execution_approval,
)
from .canonical import domain_hash
from .domain import Environment
from .errors import RecordNotFound, StateConflict, ValidationError
from .execution_grant import TrustedInfrastructureGrant
from .execution_store import CommandRecord, ExecutionStore, TrustedApproval
from .executor_config import ExecutorConfig
from .learning_bridge import LearningRecorder
from .learning_ledger import ApprovalState, ExecutionState
from .planning import RiskTicket, RiskTicketStatus, risk_ticket_from_dict
from .staging_inbox import (
    NON_AUTHORITATIVE_STAGING,
    StagingDecision,
    StagingState,
    StagingView,
    TradeStagingInbox,
)


Clock = Callable[[], datetime]
_HASH_FIELDS = (
    "analysis_hash",
    "analysis_record_hash",
    "infrastructure_grant_hash",
    "daily_loss_snapshot_hash",
)
_PAYLOAD_FIELDS = {
    "schema_version",
    "purpose",
    "profitability_qualified",
    "mainnet_authorized",
    "analysis_hash",
    "analysis_record_hash",
    "infrastructure_grant_hash",
    "grant_authentication_deferred_to_control",
    "daily_loss_snapshot_hash",
    "daily_loss_deferred_to_executor",
    "manual_sentiment_confirmation_required",
    "risk_ticket",
}


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, *, field: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, *, field: str) -> str:
    checked = _text(value, field=field, maximum=64)
    if len(checked) != 64 or any(character not in "0123456789abcdef" for character in checked):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return checked


@dataclass(frozen=True, slots=True)
class TestnetAuthorizationResult:
    document_id: str
    document_hash: str
    analysis_hash: str
    ticket_id: str
    ticket_hash: str
    approval_id: str
    command_id: str
    command_state: str
    learning_cycle_id: str
    authorized_at: datetime
    result_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "testnet_authorization_result.v1",
            "document_id": self.document_id,
            "document_hash": self.document_hash,
            "analysis_hash": self.analysis_hash,
            "ticket_id": self.ticket_id,
            "ticket_hash": self.ticket_hash,
            "approval_id": self.approval_id,
            "command_id": self.command_id,
            "command_state": self.command_state,
            "learning_cycle_id": self.learning_cycle_id,
            "authorized_at": self.authorized_at.isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            "purpose": "infrastructure_learning",
            "profitability_qualified": False,
            "mainnet_authorized": False,
            "stop_mandatory": True,
            "order_submitted": False,
            "venue_write_attempted": False,
            "result_hash": self.result_hash,
        }


class AttendedTestnetControlPlane:
    """Queue an exact staged bracket after direct local confirmation."""

    def __init__(
        self,
        inbox: TradeStagingInbox,
        execution_store: ExecutionStore,
        *,
        config: ExecutorConfig,
        grant: TrustedInfrastructureGrant,
        approval_authority: TestnetApprovalAuthority,
        learning_recorder: LearningRecorder | None = None,
        clock: Clock | None = None,
    ) -> None:
        if type(inbox) is not TradeStagingInbox:
            raise TypeError("inbox must be exact TradeStagingInbox")
        if type(execution_store) is not ExecutionStore:
            raise TypeError("execution_store must be exact ExecutionStore")
        if not isinstance(config, ExecutorConfig):
            raise TypeError("config must be ExecutorConfig")
        if not isinstance(grant, TrustedInfrastructureGrant):
            raise TypeError("grant must be TrustedInfrastructureGrant")
        if not isinstance(approval_authority, TestnetApprovalAuthority):
            raise TypeError("approval_authority must be TestnetApprovalAuthority")
        if learning_recorder is not None and not isinstance(
            learning_recorder, LearningRecorder
        ):
            raise TypeError("learning_recorder must be LearningRecorder or None")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        if (
            config.environment is not Environment.TESTNET
            or execution_store.environment is not Environment.TESTNET
            or grant.environment is not Environment.TESTNET
            or execution_store.account_id != config.account_id
            or grant.account_id != config.account_id
            or grant.risk_policy_hash != config.risk_policy_hash
            or frozenset(grant.allowed_instruments)
            != frozenset(config.allowed_instruments)
            or config.max_reserved_loss > grant.max_loss
            or config.max_reserved_notional > grant.max_notional
            or config.max_leverage > grant.max_leverage
        ):
            raise ValidationError(
                "control-plane configuration differs from TESTNET grant/store scope"
            )
        self.inbox = inbox
        self.execution_store = execution_store
        self.config = config
        self.grant = grant
        self.approval_authority = approval_authority
        self.learning_recorder = learning_recorder
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        try:
            return _utc(self.clock(), field="control-plane clock")
        except ValidationError:
            raise
        except Exception as error:
            raise ValidationError("control-plane clock failed") from error

    def _ticket(self, view: StagingView, *, at: datetime) -> tuple[dict[str, Any], RiskTicket]:
        document = view.document
        if (
            view.state is not StagingState.STAGED
            or document.decision is not StagingDecision.STAGED
            or document.authority != NON_AUTHORITATIVE_STAGING
            or view.authoritative
            or not document.created_at <= at < document.expires_at
        ):
            raise StateConflict("staging document is not an active non-authoritative ticket")
        payload = document.ticket_payload
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
            raise ValidationError("staged learning ticket fields are unsupported")
        if (
            payload.get("schema_version") != "infrastructure_learning_ticket.v1"
            or payload.get("purpose") != "infrastructure_learning"
            or payload.get("profitability_qualified") is not False
            or payload.get("mainnet_authorized") is not False
            or type(payload.get("manual_sentiment_confirmation_required")) is not bool
            or type(payload.get("grant_authentication_deferred_to_control")) is not bool
        ):
            raise ValidationError("staged ticket claims unsupported authority")
        for field in _HASH_FIELDS:
            _hash(payload.get(field), field=field)
        if payload["analysis_hash"] != document.expected_analysis_hash:
            raise StateConflict("staged ticket differs from requested analysis")
        if payload["infrastructure_grant_hash"] != self.grant.grant_hash:
            raise StateConflict("staged ticket differs from installed learning grant")
        raw_ticket = payload.get("risk_ticket")
        if not isinstance(raw_ticket, Mapping):
            raise ValidationError("staged ticket lacks a risk-ticket object")
        try:
            ticket = risk_ticket_from_dict(raw_ticket)
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError("staged risk ticket is invalid") from error
        if (
            ticket.status is not RiskTicketStatus.AWAITING_APPROVAL
            or ticket.plan is None
            or not ticket.created_at <= at < ticket.expires_at
            or ticket.expires_at > self.grant.expires_at
            or ticket.policy_hash != self.config.risk_policy_hash
            or ticket.plan.entry.environment is not Environment.TESTNET
            or ticket.plan.entry.account_id != self.config.account_id
            or ticket.plan.entry.instrument not in self.config.allowed_instruments
            or ticket.plan.entry.venue != "hyperliquid"
        ):
            raise StateConflict("staged risk ticket is inactive or outside configured scope")
        return payload, ticket

    @staticmethod
    def confirmation_for(ticket: RiskTicket) -> str:
        if not isinstance(ticket, RiskTicket):
            raise TypeError("ticket must be RiskTicket")
        return f"approve {ticket.ticket_id} {ticket.ticket_hash[:16]}"

    @staticmethod
    def _identities(document_hash: str, ticket_hash: str) -> tuple[str, str]:
        approval_id = "approval-" + domain_hash(
            "trading-harness/staged-approval-id/v1",
            {"document_hash": document_hash, "ticket_hash": ticket_hash},
        )[:40]
        command_id = "command-" + domain_hash(
            "trading-harness/staged-command-id/v1",
            {"document_hash": document_hash, "ticket_hash": ticket_hash},
        )[:40]
        return approval_id, command_id

    def _existing_command(
        self,
        *,
        command_id: str,
        approval_id: str,
        ticket: RiskTicket,
    ) -> CommandRecord | None:
        try:
            command = self.execution_store.get_command(command_id)
        except RecordNotFound:
            return None
        if (
            ticket.plan is None
            or command.ticket_hash != ticket.ticket_hash
            or command.plan_hash != ticket.plan.plan_hash
            or command.approval_id != approval_id
        ):
            raise StateConflict("deterministic command identity is bound differently")
        return command

    def _approval(
        self,
        *,
        approval_id: str,
        ticket: RiskTicket,
        approver_id: str,
        confirmation: str,
        at: datetime,
    ) -> TrustedApproval:
        expected = self.confirmation_for(ticket)
        if confirmation != expected:
            raise ValidationError("trusted UI confirmation does not match the exact ticket")
        try:
            existing = self.execution_store.get_approval(approval_id)
        except RecordNotFound:
            approval = self.approval_authority.issue(
                ticket,
                approval_id=approval_id,
                approver_id=approver_id,
                confirmation=confirmation,
                at=at,
            )
            trusted = verified_execution_approval(
                self.approval_authority,
                approval,
                ticket,
                at=at,
            )
            try:
                return self.execution_store.register_approval(trusted)
            except StateConflict:
                existing = self.execution_store.get_approval(approval_id)
        if (
            existing.ticket_hash != ticket.ticket_hash
            or existing.approver_id != approver_id
            or existing.audience != self.approval_authority.audience
            or existing.environment is not Environment.TESTNET
            or existing.account_id != self.config.account_id
        ):
            raise StateConflict("deterministic approval identity is bound differently")
        return existing

    def _record_learning(
        self,
        *,
        payload: Mapping[str, Any],
        approval: TrustedApproval,
        command: CommandRecord,
    ) -> str:
        cycle_id = f"trade-{command.ticket_hash[:32]}"
        if self.learning_recorder is None:
            return cycle_id
        cycle, _ = self.learning_recorder.record_staged_ticket(payload)
        if cycle.cycle_id != cycle_id:
            raise StateConflict("learning cycle differs from admitted command")
        self.learning_recorder.record_approval_reference(
            cycle_id,
            approval,
            state=ApprovalState.APPROVED,
            occurred_at=approval.issued_at,
        )
        self.learning_recorder.record_execution_reference(
            cycle_id,
            command,
            self.execution_store.get_legs(command.command_id),
            state=ExecutionState.AUTHORIZED,
            occurred_at=command.created_at,
        )
        return cycle_id

    def authorize_stage(
        self,
        document_id: str,
        *,
        confirmation: str,
        approver_id: str,
    ) -> TestnetAuthorizationResult:
        """Queue one protected command; never sign or submit it."""

        checked_document = _text(document_id, field="document_id", maximum=68)
        checked_approver = _text(approver_id, field="approver_id")
        checked_confirmation = _text(
            confirmation, field="confirmation", maximum=256
        )
        now = self._now()
        view = self.inbox.get(checked_document)
        payload, ticket = self._ticket(view, at=now)
        if checked_confirmation != self.confirmation_for(ticket):
            raise ValidationError("trusted UI confirmation does not match the exact ticket")
        if not self.grant.is_active(now):
            raise StateConflict("infrastructure learning grant is inactive")

        # Learning evidence is committed before capital authority is created.
        if self.learning_recorder is not None:
            self.learning_recorder.record_staged_ticket(payload)
        self.execution_store.register_infrastructure_grant(self.grant, at=now)
        self.execution_store.register_ticket(
            ticket,
            infrastructure_grant_hash=self.grant.grant_hash,
            stored_at=now,
        )
        approval_id, command_id = self._identities(
            view.document.document_hash, ticket.ticket_hash
        )
        existing_command = self._existing_command(
            command_id=command_id,
            approval_id=approval_id,
            ticket=ticket,
        )
        if existing_command is None:
            approval = self._approval(
                approval_id=approval_id,
                ticket=ticket,
                approver_id=checked_approver,
                confirmation=checked_confirmation,
                at=now,
            )
            state = self.execution_store.approval_state(approval_id)
            if state == "issued":
                try:
                    command = self.execution_store.admit(
                        command_id=command_id,
                        approval_id=approval_id,
                        token_hash=approval.token_hash,
                        audience=approval.audience,
                        at=now,
                    )
                except Exception:
                    recovered = self._existing_command(
                        command_id=command_id,
                        approval_id=approval_id,
                        ticket=ticket,
                    )
                    if recovered is None:
                        raise
                    command = recovered
            elif state == "consumed":
                recovered = self._existing_command(
                    command_id=command_id,
                    approval_id=approval_id,
                    ticket=ticket,
                )
                if recovered is None:
                    raise StateConflict("consumed approval has no durable command")
                command = recovered
            else:
                raise StateConflict("approval is not available for command admission")
        else:
            command = existing_command
            approval = self.execution_store.get_approval(approval_id)

        cycle_id = self._record_learning(
            payload=payload,
            approval=approval,
            command=command,
        )
        material = {
            "document_id": view.document.document_id,
            "document_hash": view.document.document_hash,
            "analysis_hash": view.document.expected_analysis_hash,
            "ticket_id": ticket.ticket_id,
            "ticket_hash": ticket.ticket_hash,
            "approval_id": approval_id,
            "command_id": command.command_id,
            "command_state": command.state,
            "learning_cycle_id": cycle_id,
            "authorized_at": now,
            "purpose": "infrastructure_learning",
            "profitability_qualified": False,
            "mainnet_authorized": False,
            "stop_mandatory": True,
            "order_submitted": False,
            "venue_write_attempted": False,
        }
        return TestnetAuthorizationResult(
            document_id=view.document.document_id,
            document_hash=view.document.document_hash,
            analysis_hash=view.document.expected_analysis_hash,
            ticket_id=ticket.ticket_id,
            ticket_hash=ticket.ticket_hash,
            approval_id=approval_id,
            command_id=command.command_id,
            command_state=command.state,
            learning_cycle_id=cycle_id,
            authorized_at=now,
            result_hash=domain_hash(
                "trading-harness/testnet-authorization-result/v1", material
            ),
        )


__all__ = (
    "AttendedTestnetControlPlane",
    "TestnetAuthorizationResult",
)
