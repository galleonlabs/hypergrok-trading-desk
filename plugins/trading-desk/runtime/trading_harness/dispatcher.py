"""Serialized entry dispatcher over the durable execution-store contract.

No dependency has a default: a trusted control plane must supply fresh
preflight preparation, an isolated signer call, and the one-shot transport.
The module is not exposed through MCP or the research CLI.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ContextManager

from .errors import (
    AdmissionDenied,
    EntrySubmissionRevoked,
    StateConflict,
    ValidationError,
)
from .execution_store import (
    CommandRecord,
    DispatchPreflight,
    ExecutionStore,
    TransportOutcomeEvidence,
)
from .hyperliquid_response import (
    BatchSubmissionResult,
    SubmissionResponseError,
    parse_order_response,
)
from .hyperliquid_signer import SignedActionEnvelope
from .hyperliquid_transport import (
    SubmissionAttempt,
    SubmissionOutcome,
    submit_signed_action,
)
from .hyperliquid_wire import PerpInstrumentMetadata, ProtectedOrderAction
from .planning import (
    ProtectedTradePlan,
    RiskTicket,
    protected_trade_plan_from_dict,
    risk_ticket_from_dict,
)


Clock = Callable[[], datetime]
Preparer = Callable[
    [CommandRecord, RiskTicket, ProtectedTradePlan, datetime],
    "DispatchPackage",
]
Signer = Callable[
    [
        ProtectedOrderAction,
        ProtectedTradePlan,
        PerpInstrumentMetadata,
        DispatchPreflight,
    ],
    SignedActionEnvelope,
]
SubmissionGuard = Callable[[], ContextManager[None]]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DispatchPackage:
    preflight: DispatchPreflight
    metadata: PerpInstrumentMetadata
    protected_action: ProtectedOrderAction

    def __post_init__(self) -> None:
        if not isinstance(self.preflight, DispatchPreflight):
            raise TypeError("preflight must be DispatchPreflight")
        if not isinstance(self.metadata, PerpInstrumentMetadata):
            raise TypeError("metadata must be PerpInstrumentMetadata")
        if not isinstance(self.protected_action, ProtectedOrderAction):
            raise TypeError("protected_action must be ProtectedOrderAction")
        if not self.preflight.passed:
            raise StateConflict("dispatcher cannot accept a failed preflight")
        if (
            self.preflight.command_id == ""
            or self.preflight.plan_hash != self.protected_action.plan_hash
            or self.preflight.account_id != self.protected_action.account_id
            or self.preflight.environment.value != self.protected_action.network.value
            or self.preflight.metadata_hash != self.protected_action.metadata_hash
        ):
            raise StateConflict("preflight and protected action bindings differ")


@dataclass(frozen=True, slots=True)
class DispatchResult:
    command_id: str
    outcome: str
    command_state: str
    preflight_hash: str
    attempt_id: str
    nonce: int
    action_hash: str
    wire_hash: str
    transport_attempt_hash: str | None
    batch_result: BatchSubmissionResult | None
    detail_code: str
    venue_write_attempted: bool
    retry_performed: bool = False
    reconciliation_required: bool = True
    incident_ids: tuple[str, ...] = ()
    recovery_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "execution_dispatch_result.v1",
            "command_id": self.command_id,
            "outcome": self.outcome,
            "command_state": self.command_state,
            "preflight_hash": self.preflight_hash,
            "attempt_id": self.attempt_id,
            "nonce": self.nonce,
            "action_hash": self.action_hash,
            "wire_hash": self.wire_hash,
            "transport_attempt_hash": self.transport_attempt_hash,
            "batch_result": (
                None if self.batch_result is None else self.batch_result.as_dict()
            ),
            "detail_code": self.detail_code,
            "venue_write_attempted": self.venue_write_attempted,
            "retry_performed": self.retry_performed,
            "reconciliation_required": self.reconciliation_required,
            "incident_ids": list(self.incident_ids),
            "recovery_required": self.recovery_required,
        }


@dataclass(frozen=True, slots=True)
class DispatchDenialResult:
    command_id: str
    outcome: str
    command_state: str
    detail_code: str
    venue_write_attempted: bool = False
    retry_performed: bool = False
    reconciliation_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "execution_dispatch_denial.v1",
            "command_id": self.command_id,
            "outcome": self.outcome,
            "command_state": self.command_state,
            "detail_code": self.detail_code,
            "venue_write_attempted": self.venue_write_attempted,
            "retry_performed": self.retry_performed,
            "reconciliation_required": self.reconciliation_required,
        }


class ExecutionDispatcher:
    """Claim, preflight, sign, persist-before-send, send once, and hand off."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        preparer: Preparer,
        signer: Signer,
        clock: Clock = _clock,
        lease_seconds: int = 15,
        submission_guard: SubmissionGuard | None = None,
    ) -> None:
        if not isinstance(store, ExecutionStore):
            raise TypeError("store must be ExecutionStore")
        for field, value in (
            ("preparer", preparer),
            ("signer", signer),
            ("clock", clock),
        ):
            if not callable(value):
                raise TypeError(f"{field} must be callable")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 180:
            raise ValidationError("lease_seconds must be an integer from 5 to 180")
        self.store = store
        self.preparer = preparer
        self.signer = signer
        self.clock = clock
        self.lease_seconds = lease_seconds
        if submission_guard is not None and not callable(submission_guard):
            raise TypeError("submission_guard must be callable or None")
        self.submission_guard = submission_guard

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception as error:
            raise ValidationError(f"dispatcher clock failed: {type(error).__name__}") from error
        return _utc(value, "dispatcher clock")

    def dispatch_next(
        self, worker_id: str
    ) -> DispatchResult | DispatchDenialResult | None:
        claim_time = self._now()
        outbox = self.store.claim_next(
            worker_id,
            at=claim_time,
            lease_seconds=self.lease_seconds,
        )
        if outbox is None:
            return None
        command = self.store.get_command(outbox.command_id)
        ticket = risk_ticket_from_dict(
            self.store.get_ticket_payload(command.ticket_hash)
        )
        plan = protected_trade_plan_from_dict(
            self.store.get_plan_payload(command.plan_hash)
        )
        if ticket.plan != plan:
            raise StateConflict("persisted ticket and plan payloads disagree")
        try:
            prepared_at = self._now()
            package = self.preparer(command, ticket, plan, prepared_at)
            if not isinstance(package, DispatchPackage):
                raise TypeError("preparer must return DispatchPackage")
            if (
                package.preflight.command_id != command.command_id
                or package.preflight.ticket_hash != command.ticket_hash
                or package.preflight.plan_hash != command.plan_hash
            ):
                raise StateConflict("dispatch package targets another command")
            preflight = self.store.register_preflight(
                package.preflight,
                at=self._now(),
            )
        except (AdmissionDenied, StateConflict, ValidationError) as error:
            terminal = self.store.void_unsent_command(
                command.command_id,
                reason="preflight_permanent_failure:" + type(error).__name__,
                at=self._now(),
                worker_id=worker_id,
                fencing_token=outbox.fencing_token,
            )
            return DispatchDenialResult(
                command_id=command.command_id,
                outcome="preflight_denied",
                command_state=terminal.state,
                detail_code=(
                    error.code
                    if isinstance(error, AdmissionDenied)
                    else type(error).__name__
                ),
            )
        try:
            signed = self.signer(
                package.protected_action,
                plan,
                package.metadata,
                preflight,
            )
            if not isinstance(signed, SignedActionEnvelope):
                raise TypeError("signer must return SignedActionEnvelope")
            signed.verify_integrity()
            if (
                signed.plan_hash != plan.plan_hash
                or signed.action_hash != package.protected_action.action_hash
                or signed.metadata_hash != preflight.metadata_hash
                or signed.account_id != plan.entry.account_id
                or signed.preflight_hash != preflight.preflight_hash
            ):
                raise StateConflict("signed envelope differs from prepared command")
            self.store.renew_claim(
                command.command_id,
                worker_id,
                outbox.fencing_token,
                at=self._now(),
                lease_seconds=self.lease_seconds,
            )
        except Exception as error:
            self.store.void_unsent_command(
                command.command_id,
                reason="signing_or_binding_failure:" + type(error).__name__,
                at=self._now(),
                worker_id=worker_id,
                fencing_token=outbox.fencing_token,
            )
            raise
        attempt_id = f"dispatch-{command.command_id}-{signed.wire_hash[:24]}"
        signed_evidence = signed.execution_store_evidence(command.command_id)
        attempt_time = self._now()
        if attempt_time >= preflight.expires_at:
            terminal = self.store.void_unsent_command(
                command.command_id,
                reason="dispatch_preflight_expired_before_attempt",
                at=attempt_time,
                worker_id=worker_id,
                fencing_token=outbox.fencing_token,
            )
            return DispatchDenialResult(
                command_id=command.command_id,
                outcome="preflight_expired",
                command_state=terminal.state,
                detail_code="dispatch_preflight_expired_before_attempt",
            )
        self.store.prepare_attempt(
            command.command_id,
            worker_id,
            outbox.fencing_token,
            attempt_id=attempt_id,
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed_evidence,
            nonce=signed.nonce,
            action_hash=signed.action_hash,
            wire_hash=signed.wire_hash,
            at=attempt_time,
        )
        guard = (
            nullcontext()
            if self.submission_guard is None
            else self.submission_guard()
        )
        try:
            with guard:
                transport = submit_signed_action(
                    signed,
                    store=self.store,
                    command_id=command.command_id,
                    attempt_id=attempt_id,
                    signed_evidence_hash=signed_evidence.evidence_hash,
                    worker_id=worker_id,
                    fencing_token=outbox.fencing_token,
                    clock=self.clock,
                )
        except EntrySubmissionRevoked:
            terminal = self.store.void_unsent_command(
                command.command_id,
                reason="runtime_submission_capability_revoked",
                at=self._now(),
                worker_id=worker_id,
                fencing_token=outbox.fencing_token,
                prepared_attempt_id=attempt_id,
            )
            return DispatchDenialResult(
                command_id=command.command_id,
                outcome="submission_revoked",
                command_state=terminal.state,
                detail_code="runtime_submission_capability_revoked",
            )
        if not isinstance(transport, SubmissionAttempt):
            raise TypeError("transport must return SubmissionAttempt")
        transport.verify_integrity()
        if (
            transport.account_id != signed.account_id
            or transport.nonce != signed.nonce
            or transport.wire_hash != signed.wire_hash
            or transport.network is not signed.network
            or transport.signed_envelope_hash != signed.envelope_hash
            or transport.signer_binding_hash != signed.signer_binding_hash
            or transport.artifact_kind != "protected_order"
            or transport.incident_id is not None
            or transport.send_count != 1
            or transport.retry_performed
        ):
            raise StateConflict("transport attempt differs from signed envelope")
        transport_evidence = transport.execution_store_evidence(
            command_id=command.command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=signed_evidence.evidence_hash,
        )
        if transport.outcome is SubmissionOutcome.UNKNOWN:
            command = self.store.mark_submitted_unknown(
                command.command_id,
                worker_id,
                outbox.fencing_token,
                transport_evidence=transport_evidence,
                at=self._now(),
            )
            return DispatchResult(
                command_id=command.command_id,
                outcome="unknown",
                command_state=command.state,
                preflight_hash=preflight.preflight_hash,
                attempt_id=attempt_id,
                nonce=signed.nonce,
                action_hash=signed.action_hash,
                wire_hash=signed.wire_hash,
                transport_attempt_hash=transport.attempt_hash,
                batch_result=None,
                detail_code=transport.detail_code,
                venue_write_attempted=True,
            )
        try:
            response = transport.response()
            batch = parse_order_response(
                response,
                requested_sizes=(
                    plan.entry.quantity,
                    plan.protective_stop.quantity,
                    plan.take_profit.quantity,
                ),
            )
            command = self.store.record_submission_response(
                command.command_id,
                worker_id,
                outbox.fencing_token,
                batch,
                transport_evidence=transport_evidence,
                at=self._now(),
            )
        except (SubmissionResponseError, StateConflict, ValidationError, TypeError):
            unknown_evidence = TransportOutcomeEvidence(
                command_id=command.command_id,
                attempt_id=attempt_id,
                signed_evidence_hash=signed_evidence.evidence_hash,
                endpoint=transport.endpoint,
                attempted_at_ms=transport.attempted_at_ms,
                outcome="unknown",
                http_status=transport.http_status,
                detail_code="response_unparseable_or_unrecordable",
                response_hash=transport.response_hash,
                transport_attempt_hash=transport.attempt_hash,
                send_count=transport.send_count,
                retry_performed=transport.retry_performed,
                venue_write_attempted=True,
            )
            command = self.store.mark_submitted_unknown(
                command.command_id,
                worker_id,
                outbox.fencing_token,
                transport_evidence=unknown_evidence,
                at=self._now(),
            )
            return DispatchResult(
                command_id=command.command_id,
                outcome="unknown",
                command_state=command.state,
                preflight_hash=preflight.preflight_hash,
                attempt_id=attempt_id,
                nonce=signed.nonce,
                action_hash=signed.action_hash,
                wire_hash=signed.wire_hash,
                transport_attempt_hash=transport.attempt_hash,
                batch_result=None,
                detail_code="response_unparseable_or_unrecordable",
                venue_write_attempted=True,
            )
        incidents = self.store.list_incidents(command.command_id)
        active_incidents = tuple(
            incident.incident_id
            for incident in incidents
            if incident.state != "closed"
        )
        return DispatchResult(
            command_id=command.command_id,
            outcome="response_received",
            command_state=command.state,
            preflight_hash=preflight.preflight_hash,
            attempt_id=attempt_id,
            nonce=signed.nonce,
            action_hash=signed.action_hash,
            wire_hash=signed.wire_hash,
            transport_attempt_hash=transport.attempt_hash,
            batch_result=batch,
            detail_code=transport.detail_code,
            venue_write_attempted=True,
            incident_ids=active_incidents,
            recovery_required=bool(active_incidents),
        )


__all__ = (
    "DispatchPackage",
    "DispatchDenialResult",
    "DispatchResult",
    "ExecutionDispatcher",
)
