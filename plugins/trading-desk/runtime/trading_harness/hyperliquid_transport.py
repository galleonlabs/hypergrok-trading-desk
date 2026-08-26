"""Single-attempt transport for immutable signed Hyperliquid envelopes.

The sender posts the exact bytes frozen by :mod:`hyperliquid_signer` to the
network URL encoded in that artifact.  It never follows redirects and never
retries.  Any timeout, connection error, non-200 response, redirect, oversized
body, or undecodable response is recorded as an *unknown submission outcome*
because the action may already have reached Hyperliquid and must be reconciled
by CLOID/account state before another send is considered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from typing import TypeAlias
from urllib import error as urlerror
from urllib import request as urlrequest

from .canonical import canonical_json, domain_hash
from .errors import HarnessError, ValidationError
from .execution_store import (
    EntrySubmissionAuthority,
    ExecutionStore,
    NoopFenceResponseEvidence,
    RecoverySubmissionAuthority,
    TransportOutcomeEvidence,
)
from .hyperliquid_signer import (
    SignedActionEnvelope,
    SignedRecoveryEnvelope,
    SignerOutputError,
)
from .hyperliquid_recovery import RecoveryKind
from .hyperliquid_wire import HyperliquidNetwork


Clock: TypeAlias = Callable[[], datetime]
ExchangeSender: TypeAlias = Callable[[str, bytes, float], "HttpExchangeResponse"]
SignedEnvelope: TypeAlias = SignedActionEnvelope | SignedRecoveryEnvelope

SUBMISSION_ATTEMPT_HASH_DOMAIN = "trading-harness/hyperliquid-submission-attempt/v1"
SUBMISSION_RESPONSE_HASH_DOMAIN = "trading-harness/hyperliquid-submission-response/v1"
RECOVERY_SUBMISSION_ENABLED = True
NOOP_FENCE_SUBMISSION_ENABLED = True

_NOOP_DEFAULT_RESPONSE: dict[str, object] = {
    "status": "ok",
    "response": {"type": "default"},
}
_NOOP_DEFAULT_RESPONSE_JSON = canonical_json(_NOOP_DEFAULT_RESPONSE)

_ALLOWED_EXCHANGE_URLS = frozenset(
    {
        HyperliquidNetwork.MAINNET.exchange_url,
        HyperliquidNetwork.TESTNET.exchange_url,
    }
)
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_REQUEST_BYTES = 512 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class HyperliquidSubmissionError(HarnessError):
    """A pre-send transport invariant failed; no submission was attempted."""


class SubmissionOutcome(str, Enum):
    RESPONSE_RECEIVED = "response_received"
    UNKNOWN = "unknown"


class _RedirectRefused(Exception):
    pass


class _ResponseTooLarge(Exception):
    pass


class _RejectRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urlrequest.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        raise _RedirectRefused("redirect refused")


@dataclass(frozen=True, slots=True)
class HttpExchangeResponse:
    status: int
    final_url: str
    body: bytes


@dataclass(frozen=True, slots=True)
class SubmissionAttempt:
    """One network send result; unknown outcomes always require reconciliation."""

    network: HyperliquidNetwork
    endpoint: str
    artifact_kind: str
    incident_id: str | None
    account_id: str
    nonce: int
    wire_hash: str
    signed_envelope_hash: str
    signer_binding_hash: str
    recovery_kind: str | None
    recovery_command_id: str | None
    recovery_attempt_id: str | None
    recovery_signed_evidence_hash: str | None
    submission_authority_hash: str | None
    attempted_at_ms: int
    outcome: SubmissionOutcome
    http_status: int | None
    response_json: str | None
    response_hash: str | None
    detail_code: str
    attempt_hash: str
    send_count: int = 1
    retry_performed: bool = False
    requires_reconciliation: bool = True

    @property
    def outcome_unknown(self) -> bool:
        return self.outcome is SubmissionOutcome.UNKNOWN

    def response(self) -> object | None:
        return None if self.response_json is None else json.loads(self.response_json)

    def verify_integrity(self) -> None:
        if not isinstance(self.network, HyperliquidNetwork):
            raise HyperliquidSubmissionError("submission attempt network is invalid")
        if self.network is not HyperliquidNetwork.TESTNET:
            raise HyperliquidSubmissionError("mainnet submission attempt is hard-disabled")
        if self.endpoint != self.network.exchange_url:
            raise HyperliquidSubmissionError("submission endpoint binding is invalid")
        if self.artifact_kind not in {"protected_order", "recovery"}:
            raise HyperliquidSubmissionError("submission artifact kind is invalid")
        if self.artifact_kind == "protected_order" and self.incident_id is not None:
            raise HyperliquidSubmissionError("protected submission cannot bind an incident")
        if self.artifact_kind == "recovery" and (
            not isinstance(self.incident_id, str) or not self.incident_id
        ):
            raise HyperliquidSubmissionError("recovery submission requires an incident")
        recovery_bindings = (
            self.recovery_kind,
            self.recovery_command_id,
            self.recovery_attempt_id,
            self.recovery_signed_evidence_hash,
            self.submission_authority_hash,
        )
        if self.artifact_kind == "protected_order" and any(
            value is not None for value in recovery_bindings
        ):
            raise HyperliquidSubmissionError(
                "protected submission cannot bind recovery authority"
            )
        if self.artifact_kind == "recovery":
            if any(not isinstance(value, str) or not value for value in recovery_bindings):
                raise HyperliquidSubmissionError(
                    "recovery submission authority binding is incomplete"
                )
            for field, value in (
                ("recovery_signed_evidence_hash", self.recovery_signed_evidence_hash),
                ("submission_authority_hash", self.submission_authority_hash),
            ):
                if not re.fullmatch(r"[0-9a-f]{64}", value or ""):
                    raise HyperliquidSubmissionError(f"submission {field} is invalid")
            if self.recovery_kind not in {value.value for value in RecoveryKind}:
                raise HyperliquidSubmissionError(
                    "submission recovery kind is invalid"
                )
            for field, value in (
                ("recovery_command_id", self.recovery_command_id),
                ("recovery_attempt_id", self.recovery_attempt_id),
            ):
                if (
                    not isinstance(value, str)
                    or value != value.strip()
                    or len(value) > 128
                    or any(ord(character) < 32 for character in value)
                ):
                    raise HyperliquidSubmissionError(f"submission {field} is invalid")
        if (
            not isinstance(self.account_id, str)
            or not self.account_id
            or type(self.nonce) is not int
            or self.nonce < 0
            or type(self.attempted_at_ms) is not int
            or self.attempted_at_ms < 0
        ):
            raise HyperliquidSubmissionError("submission identity or time is invalid")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise HyperliquidSubmissionError("submission HTTP status is invalid")
        if (
            not isinstance(self.detail_code, str)
            or not self.detail_code
            or len(self.detail_code) > 128
            or any(ord(character) < 32 for character in self.detail_code)
        ):
            raise HyperliquidSubmissionError("submission detail code is invalid")
        for field, value in (
            ("wire_hash", self.wire_hash),
            ("signed_envelope_hash", self.signed_envelope_hash),
            ("signer_binding_hash", self.signer_binding_hash),
            ("attempt_hash", self.attempt_hash),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise HyperliquidSubmissionError(f"submission {field} is invalid")
        if self.send_count != 1 or self.retry_performed or not self.requires_reconciliation:
            raise HyperliquidSubmissionError("submission retry/reconciliation invariant failed")
        if self.outcome is SubmissionOutcome.RESPONSE_RECEIVED:
            if (
                self.http_status != 200
                or self.response_json is None
                or self.response_hash is None
                or self.detail_code != "response_received"
            ):
                raise HyperliquidSubmissionError("received response attempt is incomplete")
            try:
                decoded = json.loads(
                    self.response_json,
                    object_pairs_hook=_unique_json_object,
                )
            except (TypeError, ValueError, RecursionError) as error:
                raise HyperliquidSubmissionError("stored response JSON is invalid") from error
            if canonical_json(decoded) != self.response_json:
                raise HyperliquidSubmissionError("stored response JSON is not canonical")
            if domain_hash(SUBMISSION_RESPONSE_HASH_DOMAIN, decoded) != self.response_hash:
                raise HyperliquidSubmissionError("stored response hash is invalid")
        elif self.outcome is SubmissionOutcome.UNKNOWN:
            if self.response_json is not None or self.response_hash is not None:
                raise HyperliquidSubmissionError("unknown attempt cannot contain a response")
        else:
            raise HyperliquidSubmissionError("submission outcome is invalid")
        material = _attempt_material(
            network=self.network,
            endpoint=self.endpoint,
            artifact_kind=self.artifact_kind,
            incident_id=self.incident_id,
            account_id=self.account_id,
            nonce=self.nonce,
            wire_hash=self.wire_hash,
            signed_envelope_hash=self.signed_envelope_hash,
            signer_binding_hash=self.signer_binding_hash,
            recovery_kind=self.recovery_kind,
            recovery_command_id=self.recovery_command_id,
            recovery_attempt_id=self.recovery_attempt_id,
            recovery_signed_evidence_hash=self.recovery_signed_evidence_hash,
            submission_authority_hash=self.submission_authority_hash,
            attempted_at_ms=self.attempted_at_ms,
            outcome=self.outcome,
            http_status=self.http_status,
            response_hash=self.response_hash,
            detail_code=self.detail_code,
        )
        if domain_hash(SUBMISSION_ATTEMPT_HASH_DOMAIN, material) != self.attempt_hash:
            raise HyperliquidSubmissionError("submission attempt hash mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "hyperliquid.submission_attempt.v1",
            "network": self.network.value,
            "endpoint": self.endpoint,
            "artifact_kind": self.artifact_kind,
            "incident_id": self.incident_id,
            "account_id": self.account_id,
            "nonce": self.nonce,
            "wire_hash": self.wire_hash,
            "signed_envelope_hash": self.signed_envelope_hash,
            "signer_binding_hash": self.signer_binding_hash,
            "recovery_kind": self.recovery_kind,
            "recovery_command_id": self.recovery_command_id,
            "recovery_attempt_id": self.recovery_attempt_id,
            "recovery_signed_evidence_hash": self.recovery_signed_evidence_hash,
            "submission_authority_hash": self.submission_authority_hash,
            "attempted_at_ms": self.attempted_at_ms,
            "outcome": self.outcome.value,
            "outcome_unknown": self.outcome_unknown,
            "http_status": self.http_status,
            "response": self.response(),
            "response_hash": self.response_hash,
            "detail_code": self.detail_code,
            "attempt_hash": self.attempt_hash,
            "send_count": self.send_count,
            "retry_performed": self.retry_performed,
            "requires_reconciliation": self.requires_reconciliation,
        }

    def execution_store_evidence(
        self,
        *,
        command_id: str,
        attempt_id: str,
        signed_evidence_hash: str,
    ) -> TransportOutcomeEvidence:
        """Produce exact one-send outcome evidence for durable persistence."""

        self.verify_integrity()
        if self.artifact_kind == "recovery" and (
            command_id != self.recovery_command_id
            or attempt_id != self.recovery_attempt_id
            or signed_evidence_hash != self.recovery_signed_evidence_hash
        ):
            raise HyperliquidSubmissionError(
                "recovery transport evidence differs from consumed authority"
            )
        return TransportOutcomeEvidence(
            command_id=command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=signed_evidence_hash,
            endpoint=self.endpoint,
            attempted_at_ms=self.attempted_at_ms,
            outcome=self.outcome.value,
            http_status=self.http_status,
            detail_code=self.detail_code,
            response_hash=self.response_hash,
            transport_attempt_hash=self.attempt_hash,
            send_count=self.send_count,
            retry_performed=self.retry_performed,
            venue_write_attempted=True,
        )

    def noop_fence_response_evidence(
        self,
        command_id: str,
        attempt_id: str,
        signed_evidence_hash: str,
        *,
        parsed_at: datetime,
    ) -> NoopFenceResponseEvidence:
        """Build exact durable proof for an accepted same-nonce noop only."""

        self.verify_integrity()
        if (
            self.artifact_kind != "recovery"
            or self.recovery_kind != RecoveryKind.NOOP_FENCE.value
        ):
            raise HyperliquidSubmissionError(
                "noop response evidence requires a noop-fence recovery attempt"
            )
        if (
            self.outcome is not SubmissionOutcome.RESPONSE_RECEIVED
            or self.response_json != _NOOP_DEFAULT_RESPONSE_JSON
            or self.response_hash
            != domain_hash(
                SUBMISSION_RESPONSE_HASH_DOMAIN,
                _NOOP_DEFAULT_RESPONSE,
            )
        ):
            raise HyperliquidSubmissionError(
                "noop response is not the exact canonical default success"
            )
        parsed_at_ms = _utc_ms(lambda: parsed_at)
        if parsed_at_ms < self.attempted_at_ms:
            raise HyperliquidSubmissionError(
                "noop response parse time predates the network attempt"
            )
        transport = self.execution_store_evidence(
            command_id=command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=signed_evidence_hash,
        )
        return NoopFenceResponseEvidence(
            recovery_command_id=command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=signed_evidence_hash,
            transport_evidence_hash=transport.evidence_hash,
            nonce=self.nonce,
            response_json=self.response_json,
            response_hash=self.response_hash,
            parsed_at=parsed_at,
        )


def _utc_ms(clock: Clock) -> int:
    try:
        value = clock()
    except Exception as error:
        raise ValidationError(f"submission clock failed: {type(error).__name__}") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("submission clock must return a timezone-aware datetime")
    utc = value.astimezone(timezone.utc)
    delta = utc - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError("submission clock predates the Unix epoch")
    return result


def _read_limited(response: object) -> bytes:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if not isinstance(raw, bytes):
        raise TypeError("HTTP response body must be bytes")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _ResponseTooLarge("response exceeds size limit")
    return raw


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _default_sender(endpoint: str, body: bytes, timeout: float) -> HttpExchangeResponse:
    if endpoint not in _ALLOWED_EXCHANGE_URLS:
        raise HyperliquidSubmissionError("refusing a non-allowlisted exchange endpoint")
    if not isinstance(body, bytes) or not body or len(body) > _MAX_REQUEST_BYTES:
        raise HyperliquidSubmissionError("signed request bytes are invalid or oversized")
    request = urlrequest.Request(
        endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "trading-harness-hyperliquid-signer/1",
        },
        method="POST",
    )
    opener = urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        _RejectRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpExchangeResponse(
                status=response.status,
                final_url=response.geturl(),
                body=_read_limited(response),
            )
    except urlerror.HTTPError as error:
        return HttpExchangeResponse(
            status=error.code,
            final_url=error.geturl(),
            body=_read_limited(error),
        )


def _attempt_material(
    *,
    network: HyperliquidNetwork,
    endpoint: str,
    artifact_kind: str,
    incident_id: str | None,
    account_id: str,
    nonce: int,
    wire_hash: str,
    signed_envelope_hash: str,
    signer_binding_hash: str,
    recovery_kind: str | None = None,
    recovery_command_id: str | None = None,
    recovery_attempt_id: str | None = None,
    recovery_signed_evidence_hash: str | None = None,
    submission_authority_hash: str | None = None,
    attempted_at_ms: int,
    outcome: SubmissionOutcome,
    http_status: int | None,
    response_hash: str | None,
    detail_code: str,
) -> dict[str, object]:
    return {
        "network": network.value,
        "endpoint": endpoint,
        "artifact_kind": artifact_kind,
        "incident_id": incident_id,
        "account_id": account_id,
        "nonce": nonce,
        "wire_hash": wire_hash,
        "signed_envelope_hash": signed_envelope_hash,
        "signer_binding_hash": signer_binding_hash,
        "recovery_kind": recovery_kind,
        "recovery_command_id": recovery_command_id,
        "recovery_attempt_id": recovery_attempt_id,
        "recovery_signed_evidence_hash": recovery_signed_evidence_hash,
        "submission_authority_hash": submission_authority_hash,
        "attempted_at_ms": attempted_at_ms,
        "outcome": outcome.value,
        "http_status": http_status,
        "response_hash": response_hash,
        "detail_code": detail_code,
        "send_count": 1,
        "retry_performed": False,
        "requires_reconciliation": True,
    }


def _attempt(
    signed: SignedEnvelope,
    *,
    recovery_kind: str | None = None,
    recovery_command_id: str | None = None,
    recovery_attempt_id: str | None = None,
    recovery_signed_evidence_hash: str | None = None,
    submission_authority_hash: str | None = None,
    attempted_at_ms: int,
    outcome: SubmissionOutcome,
    http_status: int | None,
    response_json: str | None,
    response_hash: str | None,
    detail_code: str,
) -> SubmissionAttempt:
    material = _attempt_material(
        network=signed.network,
        endpoint=signed.exchange_url,
        artifact_kind=signed.artifact_kind,
        incident_id=signed.incident_id,
        account_id=signed.account_id,
        nonce=signed.nonce,
        wire_hash=signed.wire_hash,
        signed_envelope_hash=signed.envelope_hash,
        signer_binding_hash=signed.signer_binding_hash,
        recovery_kind=recovery_kind,
        recovery_command_id=recovery_command_id,
        recovery_attempt_id=recovery_attempt_id,
        recovery_signed_evidence_hash=recovery_signed_evidence_hash,
        submission_authority_hash=submission_authority_hash,
        attempted_at_ms=attempted_at_ms,
        outcome=outcome,
        http_status=http_status,
        response_hash=response_hash,
        detail_code=detail_code,
    )
    result = SubmissionAttempt(
        network=signed.network,
        endpoint=signed.exchange_url,
        artifact_kind=signed.artifact_kind,
        incident_id=signed.incident_id,
        account_id=signed.account_id,
        nonce=signed.nonce,
        wire_hash=signed.wire_hash,
        signed_envelope_hash=signed.envelope_hash,
        signer_binding_hash=signed.signer_binding_hash,
        recovery_kind=recovery_kind,
        recovery_command_id=recovery_command_id,
        recovery_attempt_id=recovery_attempt_id,
        recovery_signed_evidence_hash=recovery_signed_evidence_hash,
        submission_authority_hash=submission_authority_hash,
        attempted_at_ms=attempted_at_ms,
        outcome=outcome,
        http_status=http_status,
        response_json=response_json,
        response_hash=response_hash,
        detail_code=detail_code,
        attempt_hash=domain_hash(SUBMISSION_ATTEMPT_HASH_DOMAIN, material),
    )
    result.verify_integrity()
    return result


def submit_signed_action(
    signed: SignedEnvelope,
    *,
    store: ExecutionStore | None = None,
    command_id: str | None = None,
    attempt_id: str | None = None,
    signed_evidence_hash: str | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> SubmissionAttempt:
    """Send exactly once and preserve every uncertain result as ``unknown``."""

    if not isinstance(signed, (SignedActionEnvelope, SignedRecoveryEnvelope)):
        raise TypeError("signed must be a protected or recovery signed envelope")
    if not callable(clock):
        raise TypeError("clock must be callable")
    if signed.network is HyperliquidNetwork.MAINNET:
        raise HyperliquidSubmissionError("mainnet submission is hard-disabled")
    if signed.exchange_url not in _ALLOWED_EXCHANGE_URLS:
        raise HyperliquidSubmissionError("signed network has no allowlisted endpoint")
    if len(signed.wire_bytes) > _MAX_REQUEST_BYTES:
        raise HyperliquidSubmissionError("signed wire exceeds request size limit")
    try:
        signed.verify_integrity()
    except SignerOutputError as error:
        raise HyperliquidSubmissionError("signed artifact integrity check failed") from error
    attempted_at_ms = _utc_ms(clock)
    if attempted_at_ms >= signed.expires_after_ms:
        raise HyperliquidSubmissionError("signed action expired before local submission")
    if (
        isinstance(signed, SignedActionEnvelope)
        and attempted_at_ms >= signed.preflight_expires_at_ms
    ):
        raise HyperliquidSubmissionError("dispatch preflight expired before submission")

    recovery_binding: dict[str, str | None] = {
        "recovery_kind": None,
        "recovery_command_id": None,
        "recovery_attempt_id": None,
        "recovery_signed_evidence_hash": None,
        "submission_authority_hash": None,
    }
    if isinstance(signed, SignedActionEnvelope):
        if (
            type(store) is not ExecutionStore
            or not isinstance(command_id, str)
            or not command_id
            or not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(signed_evidence_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", signed_evidence_hash)
            or type(fencing_token) is not int
            or fencing_token <= 0
        ):
            raise HyperliquidSubmissionError(
                "protected submission requires exact durable authority arguments"
            )
        if (
            signed.network is not HyperliquidNetwork.TESTNET
            or store.environment is not signed.network.environment
            or store.account_id != signed.account_id
        ):
            raise HyperliquidSubmissionError(
                "protected submission store scope is not exact testnet"
            )
        local_signed_evidence = signed.execution_store_evidence(command_id)
        if local_signed_evidence.evidence_hash != signed_evidence_hash:
            raise HyperliquidSubmissionError(
                "protected signed evidence hash differs before submission"
            )
        authority = ExecutionStore.require_submission_authority(
            store,
            command_id,
            attempt_id,
            signed_evidence_hash,
            worker_id,
            fencing_token,
            at=_EPOCH + timedelta(milliseconds=attempted_at_ms),
        )
        if not isinstance(authority, EntrySubmissionAuthority):
            raise HyperliquidSubmissionError(
                "store returned an invalid entry submission authority"
            )
        authority_lease_ms = _utc_ms(lambda: authority.lease_expires_at)
        if (
            authority.command_id != command_id
            or authority.attempt_id != attempt_id
            or authority.signed_evidence_hash != signed_evidence_hash
            or authority.nonce != signed.nonce
            or authority.action_hash != signed.action_hash
            or authority.wire_hash != signed.wire_hash
            or authority.worker_id != worker_id
            or authority.fencing_token != fencing_token
            or attempted_at_ms >= authority_lease_ms
        ):
            raise HyperliquidSubmissionError(
                "entry submission authority differs from signed attempt"
            )
    else:
        if command_id is not None:
            raise HyperliquidSubmissionError(
                "recovery submission cannot bind an entry command_id"
            )
        if type(store) is not ExecutionStore:
            raise HyperliquidSubmissionError(
                "recovery submission requires an exact ExecutionStore"
            )
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(signed_evidence_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", signed_evidence_hash)
            or type(fencing_token) is not int
            or fencing_token <= 0
        ):
            raise HyperliquidSubmissionError(
                "recovery submission authority arguments are invalid"
            )
        if (
            signed.network is not HyperliquidNetwork.TESTNET
            or store.environment is not signed.network.environment
            or store.account_id != signed.account_id
        ):
            raise HyperliquidSubmissionError(
                "recovery submission store scope is not exact testnet"
            )
        if (
            attempted_at_ms >= signed.permit_expires_at_ms
            or attempted_at_ms >= signed.lease_expires_at_ms
        ):
            raise HyperliquidSubmissionError(
                "recovery permit or claim expired before local submission"
            )
        try:
            local_signed_evidence = signed.execution_store_evidence()
        except (SignerOutputError, ValidationError) as error:
            raise HyperliquidSubmissionError(
                "signed recovery evidence construction failed"
            ) from error
        if local_signed_evidence.evidence_hash != signed_evidence_hash:
            raise HyperliquidSubmissionError(
                "recovery signed evidence hash differs before submission"
            )

        # This is the final pre-send transition.  It atomically changes the
        # durable attempt from prepared to sending; a second call cannot obtain
        # another authority and therefore cannot reach the sender.
        authority = ExecutionStore.require_recovery_submission_authority(
            store,
            signed.recovery_command_id,
            attempt_id,
            signed_evidence_hash,
            worker_id,
            fencing_token,
            at=_EPOCH + timedelta(milliseconds=attempted_at_ms),
        )
        if not isinstance(authority, RecoverySubmissionAuthority):
            raise HyperliquidSubmissionError(
                "store returned an invalid recovery submission authority"
            )
        authority_lease_ms = _utc_ms(lambda: authority.lease_expires_at)
        if (
            authority.recovery_command_id != signed.recovery_command_id
            or authority.attempt_id != attempt_id
            or authority.signed_evidence_hash != signed_evidence_hash
            or authority.nonce != signed.nonce
            or authority.action_hash != signed.action_hash
            or authority.wire_hash != signed.wire_hash
            or authority.worker_id != worker_id
            or authority.fencing_token != fencing_token
            or authority_lease_ms != signed.lease_expires_at_ms
            or attempted_at_ms >= authority_lease_ms
            or not re.fullmatch(r"[0-9a-f]{64}", authority.authority_hash)
        ):
            raise HyperliquidSubmissionError(
                "recovery submission authority differs from signed attempt"
            )
        recovery_binding = {
            "recovery_kind": signed.recovery_kind.value,
            "recovery_command_id": authority.recovery_command_id,
            "recovery_attempt_id": authority.attempt_id,
            "recovery_signed_evidence_hash": authority.signed_evidence_hash,
            "submission_authority_hash": authority.authority_hash,
        }

    # One call, deliberately no loop and no retry adapter.
    try:
        result = _default_sender(
            signed.exchange_url,
            signed.wire_bytes,
            _HTTP_TIMEOUT_SECONDS,
        )
    except HyperliquidSubmissionError:
        raise
    except Exception as error:
        code = "transport_" + type(error).__name__
        if isinstance(error, TimeoutError):
            code = "timeout"
        elif isinstance(error, _RedirectRefused):
            code = "redirect_refused"
        elif isinstance(error, _ResponseTooLarge):
            code = "response_too_large"
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=None,
            response_json=None,
            response_hash=None,
            detail_code=code,
        )

    if not isinstance(result, HttpExchangeResponse):
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=None,
            response_json=None,
            response_hash=None,
            detail_code="invalid_sender_result",
        )
    if (
        type(result.status) is not int
        or not isinstance(result.final_url, str)
        or not isinstance(result.body, bytes)
    ):
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=None,
            response_json=None,
            response_hash=None,
            detail_code="invalid_http_result",
        )
    if result.final_url != signed.exchange_url:
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=result.status,
            response_json=None,
            response_hash=None,
            detail_code="redirect_refused",
        )
    if len(result.body) > _MAX_RESPONSE_BYTES:
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=result.status,
            response_json=None,
            response_hash=None,
            detail_code="response_too_large",
        )
    if result.status != 200:
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=result.status,
            response_json=None,
            response_hash=None,
            detail_code=f"http_status_{result.status}",
        )
    try:
        decoded = json.loads(
            result.body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        response_json = canonical_json(decoded)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=200,
            response_json=None,
            response_hash=None,
            detail_code="invalid_json_response",
        )
    if (
        isinstance(signed, SignedRecoveryEnvelope)
        and signed.recovery_kind is RecoveryKind.NOOP_FENCE
        and decoded != _NOOP_DEFAULT_RESPONSE
    ):
        return _attempt(
            signed,
            **recovery_binding,
            attempted_at_ms=attempted_at_ms,
            outcome=SubmissionOutcome.UNKNOWN,
            http_status=200,
            response_json=None,
            response_hash=None,
            detail_code="noop_response_not_canonical_default",
        )
    response_hash = domain_hash(SUBMISSION_RESPONSE_HASH_DOMAIN, decoded)
    return _attempt(
        signed,
        **recovery_binding,
        attempted_at_ms=attempted_at_ms,
        outcome=SubmissionOutcome.RESPONSE_RECEIVED,
        http_status=200,
        response_json=response_json,
        response_hash=response_hash,
        detail_code="response_received",
    )


__all__ = (
    "SUBMISSION_ATTEMPT_HASH_DOMAIN",
    "SUBMISSION_RESPONSE_HASH_DOMAIN",
    "RECOVERY_SUBMISSION_ENABLED",
    "NOOP_FENCE_SUBMISSION_ENABLED",
    "ExchangeSender",
    "HttpExchangeResponse",
    "HyperliquidSubmissionError",
    "SubmissionAttempt",
    "SubmissionOutcome",
    "SignedEnvelope",
    "submit_signed_action",
)
