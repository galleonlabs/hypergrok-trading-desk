"""One-shot TESTNET recovery execution coordinator.

The dispatcher owns no venue sender and exposes no retry surface.  It claims
the durable recovery outbox, asks an injected read-only preparer for one typed
recovery action and its exact source evidence, independently binds that action
to the immutable command material, invokes a narrow durable signer, persists
the signed evidence, and calls the fixed public Hyperliquid transport once.

Every result still requires venue reconciliation.  A crash after signing or
attempt preparation is intentionally recovered by ``ExecutionStore`` as an
unknown outcome; this module never signs or sends the command again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Callable, Protocol, TypeAlias
import uuid

from .canonical import canonical_json
from .domain import Environment
from .errors import HarnessError, StateConflict, ValidationError
from .execution_store import (
    AttemptRecord,
    ExecutionStore,
    RecoveryCommand,
    TransportOutcomeEvidence,
)
from .hyperliquid_account import HyperliquidAccountSnapshot
from .hyperliquid_recovery import (
    CancelByCloidAction,
    NoopFenceAction,
    RecoveryAction,
    RecoveryKind,
    ReduceOnlyCloseAction,
    recovery_action_material,
)
from .hyperliquid_signer import (
    NonceAllocator,
    SignL1Action,
    SignedRecoveryEnvelope,
    SignerPolicy,
    sign_recovery_action,
)
from .hyperliquid_transport import (
    HyperliquidSubmissionError,
    SubmissionOutcome,
    submit_signed_action,
)
from .hyperliquid_wire import HyperliquidNetwork


RecoverySourceEvidence: TypeAlias = HyperliquidAccountSnapshot | AttemptRecord
RecoveryClock: TypeAlias = Callable[[], datetime]
_RECOVERY_ACTION_TYPES = (
    ReduceOnlyCloseAction,
    CancelByCloidAction,
    NoopFenceAction,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RecoveryDispatchError(HarnessError):
    """The recovery worker failed closed before a safe terminal transition."""


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


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


def _milliseconds(value: datetime, field: str) -> int:
    delta = _utc(value, field) - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError(f"{field} predates the Unix epoch")
    return result


@dataclass(frozen=True, slots=True)
class PreparedRecovery:
    """One typed action and the read-only evidence used to construct it."""

    action: RecoveryAction
    evidence: RecoverySourceEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.action, _RECOVERY_ACTION_TYPES):
            raise TypeError("action must be a typed RecoveryAction")
        expected_evidence = (
            AttemptRecord
            if isinstance(self.action, NoopFenceAction)
            else HyperliquidAccountSnapshot
        )
        if not isinstance(self.evidence, expected_evidence):
            raise TypeError("recovery action has the wrong source evidence type")


class ReadOnlyRecoveryPreparer(Protocol):
    """Narrow boundary implemented by an allowlisted read-only venue adapter."""

    def prepare(
        self,
        command: RecoveryCommand,
        *,
        at: datetime,
    ) -> PreparedRecovery: ...


class NarrowRecoverySigner(Protocol):
    """Narrow callable that must use the public durable recovery signer."""

    def sign(
        self,
        action: RecoveryAction,
        *,
        store: ExecutionStore,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        evidence: RecoverySourceEvidence,
        at: datetime,
    ) -> SignedRecoveryEnvelope: ...


class DurableRecoverySigner:
    """Bind runtime signing dependencies behind the narrow dispatcher API."""

    __slots__ = (
        "_nonce_allocator",
        "_policy",
        "_sign_l1_action",
        "_wallet",
    )

    def __init__(
        self,
        *,
        policy: SignerPolicy,
        wallet: object,
        nonce_allocator: NonceAllocator | None,
        sign_l1_action: SignL1Action | None = None,
    ) -> None:
        if not isinstance(policy, SignerPolicy):
            raise TypeError("policy must be SignerPolicy")
        if sign_l1_action is not None and not callable(sign_l1_action):
            raise TypeError("sign_l1_action must be callable or None")
        self._policy = policy
        self._wallet = wallet
        self._nonce_allocator = nonce_allocator
        self._sign_l1_action = sign_l1_action

    def sign(
        self,
        action: RecoveryAction,
        *,
        store: ExecutionStore,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        evidence: RecoverySourceEvidence,
        at: datetime,
    ) -> SignedRecoveryEnvelope:
        allocator = None if isinstance(action, NoopFenceAction) else self._nonce_allocator
        return sign_recovery_action(
            action,
            store=store,
            recovery_command_id=recovery_command_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            evidence=evidence,
            policy=self._policy,
            wallet=self._wallet,
            nonce_allocator=allocator,
            clock=lambda: at,
            sign_l1_action=self._sign_l1_action,
        )


@dataclass(frozen=True, slots=True)
class RecoveryDispatchResult:
    recovery_command_id: str
    attempt_id: str
    kind: RecoveryKind
    state: str
    outcome: SubmissionOutcome
    signed_evidence_hash: str
    transport_evidence_hash: str
    noop_response_evidence_hash: str | None
    attempted_at: datetime
    requires_reconciliation: bool = True
    retry_allowed: bool = False

    def __post_init__(self) -> None:
        _text(self.recovery_command_id, "recovery_command_id")
        _text(self.attempt_id, "attempt_id")
        if not isinstance(self.kind, RecoveryKind):
            raise TypeError("kind must be RecoveryKind")
        if self.state not in {"reconciling", "submitted_unknown"}:
            raise ValidationError("recovery dispatch state is not reconcilable")
        if not isinstance(self.outcome, SubmissionOutcome):
            raise TypeError("outcome must be SubmissionOutcome")
        for field in ("signed_evidence_hash", "transport_evidence_hash"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValidationError(f"{field} is invalid")
        if self.noop_response_evidence_hash is not None and (
            not isinstance(self.noop_response_evidence_hash, str)
            or len(self.noop_response_evidence_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.noop_response_evidence_hash
            )
        ):
            raise ValidationError("noop_response_evidence_hash is invalid")
        object.__setattr__(
            self,
            "attempted_at",
            _utc(self.attempted_at, "attempted_at"),
        )
        if self.requires_reconciliation is not True or self.retry_allowed is not False:
            raise ValidationError("recovery dispatch cannot relax reconciliation/retry")
        expected_state = (
            "reconciling"
            if self.outcome is SubmissionOutcome.RESPONSE_RECEIVED
            else "submitted_unknown"
        )
        if self.state != expected_state:
            raise ValidationError("dispatch state differs from transport outcome")
        if (self.noop_response_evidence_hash is not None) != (
            self.kind is RecoveryKind.NOOP_FENCE
            and self.outcome is SubmissionOutcome.RESPONSE_RECEIVED
        ):
            raise ValidationError("noop response evidence presence is invalid")


class RecoveryExecutionDispatcher:
    """Execute at most one durable recovery command through the fixed transport."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        worker_id: str,
        preparer: ReadOnlyRecoveryPreparer,
        signer: NarrowRecoverySigner,
        clock: RecoveryClock = lambda: datetime.now(timezone.utc),
        lease_seconds: int = 30,
    ) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be an exact ExecutionStore")
        if store.environment is not Environment.TESTNET:
            raise ValidationError("recovery dispatcher is testnet-only")
        self.store = store
        self.worker_id = _text(worker_id, "worker_id")
        if not callable(getattr(preparer, "prepare", None)):
            raise TypeError("preparer must expose a narrow prepare method")
        if not callable(getattr(signer, "sign", None)):
            raise TypeError("signer must expose a narrow sign method")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 60:
            raise ValidationError("lease_seconds must be from 30 to 60")
        self.preparer = preparer
        self.signer = signer
        self.clock = clock
        self.lease_seconds = lease_seconds

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception as error:
            raise RecoveryDispatchError(
                f"dispatcher clock failed: {type(error).__name__}"
            ) from error
        return _utc(value, "dispatcher clock")

    def _verify_prepared(
        self,
        command: RecoveryCommand,
        prepared: object,
        at: datetime,
    ) -> PreparedRecovery:
        if not isinstance(prepared, PreparedRecovery):
            raise RecoveryDispatchError("preparer returned an untyped recovery")
        action = prepared.action
        try:
            material = recovery_action_material(action)
            material_json = canonical_json(material)
            persisted = json.loads(command.recovery_material_json)
        except (TypeError, ValueError, ValidationError) as error:
            raise RecoveryDispatchError("prepared recovery material is invalid") from error
        if canonical_json(persisted) != command.recovery_material_json:
            raise StateConflict("durable recovery material is not canonical")
        if (
            material_json != command.recovery_material_json
            or action.recovery_hash != command.recovery_hash
            or action.kind.value != command.kind
            or action.account_id != self.store.account_id
            or action.network is not HyperliquidNetwork.TESTNET
            or action.incident_id != command.incident_id
        ):
            raise RecoveryDispatchError(
                "prepared recovery differs from durable command material"
            )
        at_ms = _milliseconds(at, "preparation time")
        if at_ms >= action.expires_at_ms:
            raise RecoveryDispatchError("prepared recovery is already expired")
        if isinstance(action, NoopFenceAction):
            persisted_attempt = ExecutionStore.get_attempt(
                self.store,
                command.parent_command_id,
            )
            if (
                prepared.evidence != persisted_attempt
                or action.attempt_id != command.original_attempt_id
                or action.original_nonce != command.original_nonce
            ):
                raise RecoveryDispatchError(
                    "prepared noop differs from durable unknown attempt"
                )
        else:
            snapshot = prepared.evidence
            if not isinstance(snapshot, HyperliquidAccountSnapshot):
                raise RecoveryDispatchError("recovery preparer omitted account evidence")
            expected_hash = (
                action.position_snapshot_hash
                if isinstance(action, ReduceOnlyCloseAction)
                else action.account_snapshot_hash
            )
            if (
                snapshot.network != "testnet"
                or snapshot.main_account_address != action.main_account_address
                or snapshot.snapshot_hash != expected_hash
            ):
                raise RecoveryDispatchError(
                    "prepared account evidence differs from recovery action"
                )
            age_ms = at_ms - snapshot.server_time_ms
            if age_ms > 5_000 or age_ms < -5_000:
                raise RecoveryDispatchError("prepared account evidence is not fresh")
        return prepared

    def _verify_signed(
        self,
        command: RecoveryCommand,
        prepared: PreparedRecovery,
        signed: object,
        fencing_token: int,
    ) -> SignedRecoveryEnvelope:
        if type(signed) is not SignedRecoveryEnvelope:
            raise RecoveryDispatchError("signer returned an untyped recovery envelope")
        try:
            signed.verify_integrity()
            signed_material_json = canonical_json(signed.recovery_material())
        except Exception as error:
            raise RecoveryDispatchError("signer returned invalid recovery evidence") from error
        if (
            signed_material_json != command.recovery_material_json
            or signed.recovery_command_id != command.recovery_command_id
            or signed.permit_id != command.permit_id
            or signed.parent_command_id != command.parent_command_id
            or signed.incident_id != command.incident_id
            or signed.recovery_kind.value != command.kind
            or signed.recovery_hash != command.recovery_hash
            or signed.source_hash != command.source_hash
            or signed.safety_policy_hash != command.safety_policy_hash
            or signed.preflight_hash != command.preflight_hash
            or signed.original_attempt_id != command.original_attempt_id
            or signed.original_nonce != command.original_nonce
            or signed.account_id != self.store.account_id
            or signed.network is not HyperliquidNetwork.TESTNET
            or signed.worker_id != self.worker_id
            or signed.fencing_token != fencing_token
            or signed.recovery_hash != prepared.action.recovery_hash
        ):
            raise RecoveryDispatchError(
                "signed recovery differs from durable command or claim"
            )
        return signed

    def dispatch_next(self) -> RecoveryDispatchResult | None:
        """Claim, sign, persist, and submit one recovery; never retry it."""

        claim_at = self._now()
        claim = ExecutionStore.claim_next_recovery(
            self.store,
            self.worker_id,
            at=claim_at,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None
        command = ExecutionStore.get_recovery_command(
            self.store,
            claim.recovery_command_id,
        )
        prepare_at = self._now()
        prepared = self._verify_prepared(
            command,
            self.preparer.prepare(command, at=prepare_at),
            prepare_at,
        )
        sign_at = self._now()
        signed = self._verify_signed(
            command,
            prepared,
            self.signer.sign(
                prepared.action,
                store=self.store,
                recovery_command_id=command.recovery_command_id,
                worker_id=self.worker_id,
                fencing_token=claim.fencing_token,
                evidence=prepared.evidence,
                at=sign_at,
            ),
            claim.fencing_token,
        )
        signed_evidence = signed.execution_store_evidence()
        attempt_id = f"recovery-attempt-{uuid.uuid4().hex}"
        attempt = ExecutionStore.prepare_recovery_attempt(
            self.store,
            command.recovery_command_id,
            self.worker_id,
            claim.fencing_token,
            attempt_id=attempt_id,
            signed_evidence=signed_evidence,
            at=self._now(),
        )
        submission = submit_signed_action(
            signed,
            store=self.store,
            attempt_id=attempt.attempt_id,
            signed_evidence_hash=signed_evidence.evidence_hash,
            worker_id=self.worker_id,
            fencing_token=claim.fencing_token,
            clock=lambda: self._now(),
        )
        transport_evidence = submission.execution_store_evidence(
            command_id=command.recovery_command_id,
            attempt_id=attempt.attempt_id,
            signed_evidence_hash=signed_evidence.evidence_hash,
        )
        record_at = self._now()
        noop_response = None
        recorded_outcome = submission.outcome
        if (
            command.kind == RecoveryKind.NOOP_FENCE.value
            and submission.outcome is SubmissionOutcome.RESPONSE_RECEIVED
        ):
            try:
                noop_response = submission.noop_fence_response_evidence(
                    command.recovery_command_id,
                    attempt.attempt_id,
                    signed_evidence.evidence_hash,
                    parsed_at=record_at,
                )
            except HyperliquidSubmissionError:
                # A late-winning original action can make the same-nonce noop
                # return a deterministic nonce/error body instead of the exact
                # default success.  Persist that as unknown and reconcile the
                # parent/account; never crash, retry, or claim a fence.
                transport_evidence = TransportOutcomeEvidence(
                    command_id=command.recovery_command_id,
                    attempt_id=attempt.attempt_id,
                    signed_evidence_hash=signed_evidence.evidence_hash,
                    endpoint=submission.endpoint,
                    attempted_at_ms=submission.attempted_at_ms,
                    outcome="unknown",
                    http_status=submission.http_status,
                    detail_code="noop_response_not_canonical_default",
                    response_hash=submission.response_hash,
                    transport_attempt_hash=submission.attempt_hash,
                    send_count=submission.send_count,
                    retry_performed=submission.retry_performed,
                    venue_write_attempted=True,
                )
                recorded_outcome = SubmissionOutcome.UNKNOWN
        updated = ExecutionStore.record_recovery_outcome(
            self.store,
            command.recovery_command_id,
            self.worker_id,
            claim.fencing_token,
            transport_evidence=transport_evidence,
            noop_response=noop_response,
            at=record_at,
        )
        return RecoveryDispatchResult(
            recovery_command_id=command.recovery_command_id,
            attempt_id=attempt.attempt_id,
            kind=RecoveryKind(command.kind),
            state=updated.state,
            outcome=recorded_outcome,
            signed_evidence_hash=signed_evidence.evidence_hash,
            transport_evidence_hash=transport_evidence.evidence_hash,
            noop_response_evidence_hash=(
                None if noop_response is None else noop_response.evidence_hash
            ),
            attempted_at=_EPOCH
            + timedelta(milliseconds=submission.attempted_at_ms),
        )


__all__ = (
    "DurableRecoverySigner",
    "NarrowRecoverySigner",
    "PreparedRecovery",
    "ReadOnlyRecoveryPreparer",
    "RecoveryDispatchError",
    "RecoveryDispatchResult",
    "RecoveryExecutionDispatcher",
    "RecoverySourceEvidence",
)
