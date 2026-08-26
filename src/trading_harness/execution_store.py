"""Durable, network-free persistence for protected-plan execution.

This module implements the transactional side of the execution boundary.  It
does not import a venue SDK, load credentials, sign data, or make a network
request.  Its job is to make every future side effect recoverable:

* bind one trusted approval to one exact risk ticket;
* consume that approval once while reserving risk and creating a three-leg
  command plus durable outbox row in the same transaction;
* fence dispatch/reconciliation workers;
* persist nonce/action/wire hashes before any caller may send;
* turn an expired claim with a prepared attempt into ``submitted_unknown``
  instead of retrying; and
* release risk only from complete venue/account reconciliation evidence.

An execution database has one immutable environment and account identity.
Separate testnet and mainnet files remain the recommended deployment model.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Iterator, Mapping, Sequence

from .canonical import canonical_json, domain_hash, semantic_intent_hash
from .domain import Environment, SemanticIntent, Side
from .errors import (
    AdmissionDenied,
    PolicyViolation,
    RecordNotFound,
    StateConflict,
    StorageError,
    ValidationError,
)
from .execution_grant import TrustedInfrastructureGrant
from .hyperliquid_response import BatchSubmissionResult, LegSubmissionState
from .planning import ProtectedTradePlan, RiskTicket, RiskTicketStatus
from .policy import (
    decimal_add,
    decimal_multiply,
    decimal_subtract,
    exact_decimal,
)


ZERO = Decimal("0")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HASH_CHARS = frozenset("0123456789abcdef")
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_DETAILS_BYTES = 64 * 1024
_ROLES = ("entry", "protective_stop", "take_profit")
_COMMAND_STATES = frozenset(
    {"queued", "claimed", "submitted_unknown", "reconciling", "terminal"}
)
_OUTBOX_STATES = _COMMAND_STATES
_ATTEMPT_STATES = frozenset({"prepared", "response_received", "unknown"})
_RECOVERY_ATTEMPT_STATES = frozenset(
    {"prepared", "sending", "response_received", "unknown"}
)
_RECOVERY_COMMAND_STATES = frozenset(
    {
        "queued",
        "claimed",
        "signing",
        "submitted_unknown",
        "reconciling",
        "terminal",
    }
)


def _file_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IMODE(file_stat.st_mode),
        file_stat.st_uid,
        file_stat.st_gid,
        file_stat.st_nlink,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _snapshot_regular_file(
    path: Path, *, label: str, required_mode: int | None = None
) -> tuple[tuple[int, ...], str, bytes]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise StorageError(f"{label} must be a regular single-link file")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise StorageError(f"{label} mode must be {required_mode:04o}")
        digest = hashlib.sha256()
        header = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if len(header) < 20:
                header += chunk[: 20 - len(header)]
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _file_signature(before) != _file_signature(after):
            raise StorageError(f"{label} changed while it was read")
        return _file_signature(after), digest.hexdigest(), header
    except OSError as error:
        raise StorageError(f"{label} is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_verification_file(
    source: Path,
    destination: Path,
    *,
    label: str,
    expected: tuple[tuple[int, ...], str, bytes],
) -> None:
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        source_stat = os.fstat(source_descriptor)
        if _file_signature(source_stat) != expected[0]:
            raise StorageError(f"{label} changed before verification snapshot")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(destination_descriptor, 0o600)
        digest = hashlib.sha256()
        header = b""
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            if len(header) < 20:
                header += chunk[: 20 - len(header)]
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        os.fsync(destination_descriptor)
        if (
            _file_signature(os.fstat(source_descriptor)) != expected[0]
            or digest.hexdigest() != expected[1]
            or header != expected[2]
        ):
            raise StorageError(f"{label} changed during verification snapshot")
    except OSError as error:
        raise StorageError(f"{label} could not be snapshotted") from error
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
    copied = _snapshot_regular_file(
        destination, label=f"temporary {label}", required_mode=0o600
    )
    if copied[1:] != expected[1:] or copied[0][6] != expected[0][6]:
        raise StorageError(f"temporary {label} does not match its source")


_LEG_STATES = frozenset(
    {
        "queued",
        "submitted_unknown",
        "resting",
        "partially_filled",
        "filled",
        "canceled",
        "rejected",
        "expired",
        "triggered",
        "absent",
    }
)
_TERMINAL_LEG_STATES = frozenset(
    {"filled", "canceled", "rejected", "expired", "absent"}
)
_PROTECTION_STATES = frozenset(
    {"flat", "protected", "under_protected", "over_protected", "failed"}
)
_INCIDENT_STATES = frozenset({"open", "contained", "closed"})
_INCIDENT_SEVERITIES = frozenset({"warning", "high", "critical"})
_MAX_PREFLIGHT_LIFETIME_SECONDS = 30
_RECOVERY_KINDS = frozenset(
    {"reduce_only_close", "cancel_by_cloid", "noop_fence"}
)
_RECOVERY_PRIORITY = {
    "noop_fence": 0,
    "cancel_by_cloid": 1,
    "reduce_only_close": 2,
}
_MAX_RECOVERY_PERMIT_SECONDS = 15
_SUBMISSION_RESPONSE_HASH_DOMAIN = (
    "trading-harness/hyperliquid-submission-response/v1"
)
_NOOP_DEFAULT_RESPONSE = {"status": "ok", "response": {"type": "default"}}
_NOOP_DEFAULT_RESPONSE_JSON = canonical_json(_NOOP_DEFAULT_RESPONSE)


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _time_text(value: datetime, *, field: str) -> str:
    return _utc(value, field=field).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError(f"persisted {field} is not text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError(f"persisted {field} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError(f"persisted {field} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_time(value: object, *, field: str) -> datetime | None:
    return None if value is None else _parse_time(value, field=field)


def _text(value: object, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a non-empty, trimmed string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _stored_text(value: object, *, field: str, maximum: int = 256) -> str:
    try:
        return _text(value, field=field, maximum=maximum)
    except ValidationError as error:
        raise StorageError(f"persisted {field} is invalid") from error


def _hash(value: object, *, field: str) -> str:
    parsed = _text(value, field=field, maximum=64)
    if len(parsed) != 64 or any(character not in _HASH_CHARS for character in parsed):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return parsed


def _stored_hash(value: object, *, field: str) -> str:
    try:
        return _hash(value, field=field)
    except ValidationError as error:
        raise StorageError(f"persisted {field} is invalid") from error


def _positive_int(value: object, *, field: str, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} exceeds {maximum}")
    return value


def _nonnegative_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return value


def _decimal(value: object, *, field: str, nonnegative: bool = False) -> Decimal:
    try:
        result = exact_decimal(value, field=field)  # type: ignore[arg-type]
    except (TypeError, ValidationError) as error:
        if isinstance(error, ValidationError):
            raise
        raise ValidationError(f"{field} is not an exact decimal") from error
    if nonnegative and result < ZERO:
        raise ValidationError(f"{field} must be non-negative")
    return result


def _decimal_text(value: Decimal, *, field: str) -> str:
    from .canonical import canonical_decimal

    return canonical_decimal(_decimal(value, field=field))


def _canonical_payload(value: object, *, maximum: int = _MAX_JSON_BYTES) -> tuple[str, str]:
    try:
        payload_json = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValidationError("payload is not canonical JSON") from error
    encoded = payload_json.encode("utf-8")
    if len(encoded) > maximum:
        raise ValidationError("canonical payload exceeds its size limit")
    return payload_json, hashlib.sha256(encoded).hexdigest()


def _decode_payload(
    payload_json: object,
    content_hash: object,
    *,
    field: str,
    maximum: int = _MAX_JSON_BYTES,
) -> Any:
    if not isinstance(payload_json, str):
        raise StorageError(f"persisted {field} payload is not text")
    encoded = payload_json.encode("utf-8")
    if len(encoded) > maximum:
        raise StorageError(f"persisted {field} payload exceeds its size limit")
    if hashlib.sha256(encoded).hexdigest() != _stored_hash(
        content_hash, field=f"{field} content_hash"
    ):
        raise StorageError(f"persisted {field} content hash does not match")
    try:
        decoded = json.loads(payload_json)
        if canonical_json(decoded) != payload_json:
            raise StorageError(f"persisted {field} payload is not canonical")
    except StorageError:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise StorageError(f"persisted {field} payload is not canonical") from error
    return decoded


def _record_hash(domain: str, value: object) -> str:
    return domain_hash(f"trading-harness/execution-store/{domain}/v1", value)


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        joined = "\n-- execution migration statement --\n".join(self.statements)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


_SCHEMA_V1 = _Migration(
    1,
    "protected_plan_execution_foundation",
    (
        """
        CREATE TABLE execution_store_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            environment TEXT NOT NULL CHECK (environment IN ('testnet', 'mainnet')),
            account_id TEXT NOT NULL,
            max_reserved_loss TEXT NOT NULL,
            max_reserved_notional TEXT NOT NULL,
            created_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_plans (
            plan_hash TEXT PRIMARY KEY,
            assessment_hash TEXT NOT NULL,
            environment TEXT NOT NULL,
            account_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            instrument TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_plan_legs (
            plan_hash TEXT NOT NULL REFERENCES execution_plans(plan_hash),
            role TEXT NOT NULL CHECK (
                role IN ('entry', 'protective_stop', 'take_profit')
            ),
            cloid TEXT NOT NULL UNIQUE,
            intent_hash TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            reduce_only INTEGER NOT NULL CHECK (reduce_only IN (0, 1)),
            quantity TEXT NOT NULL,
            price_bound TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY (plan_hash, role)
        )
        """,
        """
        CREATE TABLE execution_tickets (
            ticket_hash TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL UNIQUE,
            plan_hash TEXT NOT NULL UNIQUE REFERENCES execution_plans(plan_hash),
            state TEXT NOT NULL CHECK (state IN ('awaiting_approval', 'consumed', 'terminal')),
            stressed_loss TEXT NOT NULL,
            reserved_notional TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_approvals (
            approval_id TEXT PRIMARY KEY,
            ticket_hash TEXT NOT NULL UNIQUE REFERENCES execution_tickets(ticket_hash),
            token_hash TEXT NOT NULL UNIQUE,
            approver_id TEXT NOT NULL,
            audience TEXT NOT NULL,
            environment TEXT NOT NULL,
            account_id TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('issued', 'consumed', 'revoked')),
            command_id TEXT UNIQUE,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_exposure (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            reserved_loss TEXT NOT NULL,
            reserved_notional TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_commands (
            command_id TEXT PRIMARY KEY,
            ticket_hash TEXT NOT NULL UNIQUE REFERENCES execution_tickets(ticket_hash),
            plan_hash TEXT NOT NULL UNIQUE REFERENCES execution_plans(plan_hash),
            approval_id TEXT NOT NULL UNIQUE REFERENCES execution_approvals(approval_id),
            state TEXT NOT NULL CHECK (
                state IN ('queued', 'claimed', 'submitted_unknown', 'reconciling', 'terminal')
            ),
            reserved_loss TEXT NOT NULL,
            reserved_notional TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT,
            revision INTEGER NOT NULL CHECK (revision > 0),
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_command_legs (
            command_id TEXT NOT NULL REFERENCES execution_commands(command_id),
            role TEXT NOT NULL CHECK (
                role IN ('entry', 'protective_stop', 'take_profit')
            ),
            cloid TEXT NOT NULL UNIQUE,
            intent_hash TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            reduce_only INTEGER NOT NULL CHECK (reduce_only IN (0, 1)),
            requested_quantity TEXT NOT NULL,
            cumulative_filled TEXT NOT NULL,
            venue_oid INTEGER,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            PRIMARY KEY (command_id, role)
        )
        """,
        """
        CREATE TABLE execution_outbox (
            command_id TEXT PRIMARY KEY REFERENCES execution_commands(command_id),
            state TEXT NOT NULL CHECK (
                state IN ('queued', 'claimed', 'submitted_unknown', 'reconciling', 'terminal')
            ),
            worker_id TEXT,
            fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
            claimed_at TEXT,
            lease_expires_at TEXT,
            current_attempt_id TEXT,
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_attempts (
            attempt_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE REFERENCES execution_commands(command_id),
            worker_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
            nonce INTEGER NOT NULL CHECK (nonce >= 0),
            action_hash TEXT NOT NULL,
            wire_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('prepared', 'response_received', 'unknown')),
            response_hash TEXT,
            prepared_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL REFERENCES execution_commands(command_id),
            account_snapshot_hash TEXT NOT NULL,
            complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_fills (
            fill_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL REFERENCES execution_commands(command_id),
            role TEXT NOT NULL,
            cloid TEXT NOT NULL,
            quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            fee TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_positions (
            instrument TEXT PRIMARY KEY,
            signed_quantity TEXT NOT NULL,
            account_snapshot_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_protection (
            command_id TEXT PRIMARY KEY REFERENCES execution_commands(command_id),
            instrument TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('flat', 'protected', 'under_protected', 'over_protected', 'failed')
            ),
            signed_position_quantity TEXT NOT NULL,
            protected_quantity TEXT NOT NULL,
            stop_cloid TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_incidents (
            incident_id TEXT PRIMARY KEY,
            command_id TEXT REFERENCES execution_commands(command_id),
            code TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'high', 'critical')),
            state TEXT NOT NULL CHECK (state IN ('open', 'contained', 'closed')),
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            details_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_events (
            event_sequence INTEGER PRIMARY KEY,
            event_hash TEXT NOT NULL UNIQUE,
            previous_hash TEXT,
            command_id TEXT REFERENCES execution_commands(command_id),
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_execution_outbox_state
        ON execution_outbox (state, created_at, command_id)
        """,
        """
        CREATE INDEX idx_execution_events_command
        ON execution_events (command_id, event_sequence)
        """,
        """
        CREATE INDEX idx_execution_fills_command
        ON execution_fills (command_id, occurred_at, fill_id)
        """,
    ),
)

_SCHEMA_V2 = _Migration(
    2,
    "fresh_dispatch_preflight",
    (
        """
        CREATE TABLE execution_dispatch_preflights (
            preflight_hash TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE REFERENCES execution_commands(command_id),
            ticket_hash TEXT NOT NULL UNIQUE REFERENCES execution_tickets(ticket_hash),
            plan_hash TEXT NOT NULL UNIQUE REFERENCES execution_plans(plan_hash),
            environment TEXT NOT NULL CHECK (environment IN ('testnet', 'mainnet')),
            account_id TEXT NOT NULL,
            account_snapshot_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            market_snapshot_hash TEXT NOT NULL,
            risk_policy_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            passed INTEGER NOT NULL CHECK (passed = 1),
            registered_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        ALTER TABLE execution_attempts
        ADD COLUMN preflight_hash TEXT
            REFERENCES execution_dispatch_preflights(preflight_hash)
        """,
        """
        CREATE UNIQUE INDEX idx_execution_attempt_preflight
        ON execution_attempts (preflight_hash)
        WHERE preflight_hash IS NOT NULL
        """,
    ),
)

_SCHEMA_V3 = _Migration(
    3,
    "signed_and_transport_evidence",
    (
        """
        CREATE TABLE execution_signed_envelopes (
            evidence_hash TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE REFERENCES execution_commands(command_id),
            preflight_hash TEXT NOT NULL UNIQUE
                REFERENCES execution_dispatch_preflights(preflight_hash),
            environment TEXT NOT NULL CHECK (environment = 'testnet'),
            endpoint TEXT NOT NULL,
            account_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            action_hash TEXT NOT NULL,
            nonce INTEGER NOT NULL CHECK (nonce >= 0),
            wire_hash TEXT NOT NULL,
            signature_hash TEXT NOT NULL,
            envelope_hash TEXT NOT NULL,
            signer_binding_hash TEXT NOT NULL,
            authorization_expires_at_ms INTEGER NOT NULL,
            expires_after_ms INTEGER NOT NULL,
            signed_at_ms INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_transport_outcomes (
            evidence_hash TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE REFERENCES execution_commands(command_id),
            attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_attempts(attempt_id),
            signed_evidence_hash TEXT NOT NULL
                REFERENCES execution_signed_envelopes(evidence_hash),
            endpoint TEXT NOT NULL,
            attempted_at_ms INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('response_received', 'unknown')),
            http_status INTEGER,
            detail_code TEXT NOT NULL,
            response_hash TEXT,
            transport_attempt_hash TEXT,
            send_count INTEGER,
            retry_performed INTEGER NOT NULL CHECK (retry_performed = 0),
            venue_write_attempted INTEGER,
            evidence_basis TEXT NOT NULL CHECK (
                evidence_basis IN ('transport_result', 'claim_expiry')
            ),
            recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        ALTER TABLE execution_attempts
        ADD COLUMN signed_evidence_hash TEXT
            REFERENCES execution_signed_envelopes(evidence_hash)
        """,
        """
        ALTER TABLE execution_attempts
        ADD COLUMN transport_evidence_hash TEXT
            REFERENCES execution_transport_outcomes(evidence_hash)
        """,
    ),
)

_SCHEMA_V4 = _Migration(
    4,
    "durable_recovery_commands",
    (
        """
        CREATE TABLE execution_recovery_permits (
            permit_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            parent_command_id TEXT NOT NULL REFERENCES execution_commands(command_id),
            incident_id TEXT NOT NULL REFERENCES execution_incidents(incident_id),
            kind TEXT NOT NULL CHECK (
                kind IN ('reduce_only_close', 'cancel_by_cloid', 'noop_fence')
            ),
            environment TEXT NOT NULL CHECK (environment = 'testnet'),
            account_id TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            preflight_hash TEXT,
            recovery_hash TEXT NOT NULL,
            recovery_material_json TEXT NOT NULL,
            recovery_material_hash TEXT NOT NULL,
            safety_policy_hash TEXT NOT NULL,
            original_attempt_id TEXT,
            original_nonce INTEGER,
            issuer_id TEXT NOT NULL,
            audience TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('issued', 'consumed', 'revoked')),
            recovery_command_id TEXT UNIQUE,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_commands (
            recovery_command_id TEXT PRIMARY KEY,
            permit_id TEXT NOT NULL UNIQUE REFERENCES execution_recovery_permits(permit_id),
            parent_command_id TEXT NOT NULL REFERENCES execution_commands(command_id),
            incident_id TEXT NOT NULL REFERENCES execution_incidents(incident_id),
            kind TEXT NOT NULL,
            priority INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            preflight_hash TEXT,
            recovery_hash TEXT NOT NULL,
            recovery_material_json TEXT NOT NULL,
            recovery_material_hash TEXT NOT NULL,
            safety_policy_hash TEXT NOT NULL,
            original_attempt_id TEXT,
            original_nonce INTEGER,
            state TEXT NOT NULL CHECK (
                state IN (
                    'queued', 'claimed', 'signing', 'submitted_unknown',
                    'reconciling', 'terminal'
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT,
            revision INTEGER NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_outbox (
            recovery_command_id TEXT PRIMARY KEY
                REFERENCES execution_recovery_commands(recovery_command_id),
            state TEXT NOT NULL CHECK (
                state IN (
                    'queued', 'claimed', 'signing', 'submitted_unknown',
                    'reconciling', 'terminal'
                )
            ),
            worker_id TEXT,
            fencing_token INTEGER NOT NULL,
            claimed_at TEXT,
            lease_expires_at TEXT,
            current_attempt_id TEXT,
            attempt_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_signed_recovery_evidence (
            evidence_hash TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_commands(recovery_command_id),
            incident_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            recovery_hash TEXT NOT NULL,
            signing_authority_hash TEXT NOT NULL,
            safety_policy_hash TEXT NOT NULL,
            nonce INTEGER NOT NULL,
            wire_hash TEXT NOT NULL,
            action_hash TEXT NOT NULL,
            signature_hash TEXT NOT NULL,
            envelope_hash TEXT NOT NULL,
            signer_binding_hash TEXT NOT NULL,
            expires_after_ms INTEGER NOT NULL,
            signed_at_ms INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_signing_authorities (
            authority_hash TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_commands(recovery_command_id),
            worker_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            issued_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_attempts (
            attempt_id TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_commands(recovery_command_id),
            worker_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            signed_evidence_hash TEXT NOT NULL
                REFERENCES execution_signed_recovery_evidence(evidence_hash),
            transport_evidence_hash TEXT,
            nonce INTEGER NOT NULL,
            action_hash TEXT NOT NULL,
            wire_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('prepared', 'sending', 'response_received', 'unknown')
            ),
            prepared_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_transport_evidence (
            evidence_hash TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_commands(recovery_command_id),
            attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_recovery_attempts(attempt_id),
            signed_evidence_hash TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            attempted_at_ms INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('response_received', 'unknown')),
            http_status INTEGER,
            detail_code TEXT NOT NULL,
            response_hash TEXT,
            transport_attempt_hash TEXT,
            send_count INTEGER,
            retry_performed INTEGER NOT NULL CHECK (retry_performed = 0),
            venue_write_attempted INTEGER,
            evidence_basis TEXT NOT NULL CHECK (
                evidence_basis IN ('transport_result', 'claim_expiry')
            ),
            recorded_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE execution_recovery_reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL
                REFERENCES execution_recovery_commands(recovery_command_id),
            account_snapshot_hash TEXT NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
            incident_resolution TEXT CHECK (
                incident_resolution IN ('contained', 'closed')
            ),
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_execution_recovery_priority
        ON execution_recovery_outbox (state, recovery_command_id)
        """,
    ),
)

_SCHEMA_V5 = _Migration(
    5,
    "durable_noop_fence_response",
    (
        """
        CREATE TABLE execution_noop_fence_responses (
            evidence_hash TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_commands(recovery_command_id),
            attempt_id TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_attempts(attempt_id),
            signed_evidence_hash TEXT NOT NULL,
            transport_evidence_hash TEXT NOT NULL UNIQUE
                REFERENCES execution_recovery_transport_evidence(evidence_hash),
            nonce INTEGER NOT NULL CHECK (nonce >= 0),
            response_json TEXT NOT NULL,
            response_hash TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
    ),
)

_SCHEMA_V6 = _Migration(
    6,
    "infrastructure_learning_grants",
    (
        """
        CREATE TABLE execution_infrastructure_grants (
            grant_hash TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation > 0),
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL CHECK (environment = 'testnet'),
            allowed_instruments_json TEXT NOT NULL,
            risk_policy_hash TEXT NOT NULL,
            max_loss TEXT NOT NULL,
            max_notional TEXT NOT NULL,
            max_leverage TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            audience TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            not_before TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            UNIQUE(grant_id, generation)
        )
        """,
        """
        ALTER TABLE execution_tickets
        ADD COLUMN infrastructure_grant_hash TEXT
            REFERENCES execution_infrastructure_grants(grant_hash)
        """,
    ),
)

_SCHEMA_V7 = _Migration(
    7,
    "complete_fill_economics_for_learning",
    (
        """
        ALTER TABLE execution_fills ADD COLUMN venue_oid INTEGER
        """,
        """
        ALTER TABLE execution_fills ADD COLUMN venue_trade_id INTEGER
        """,
        """
        ALTER TABLE execution_fills ADD COLUMN transaction_hash TEXT
        """,
        """
        ALTER TABLE execution_fills ADD COLUMN closed_pnl TEXT
        """,
        """
        ALTER TABLE execution_fills ADD COLUMN fee_token TEXT
        """,
        """
        ALTER TABLE execution_fills ADD COLUMN observed_at TEXT
        """,
    ),
)

_SCHEMA_V8 = _Migration(
    8,
    "single_use_entry_submission_authority",
    (
        """
        CREATE TABLE execution_submission_authorities (
            authority_hash TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE
                REFERENCES execution_commands(command_id),
            attempt_id TEXT NOT NULL UNIQUE
                REFERENCES execution_attempts(attempt_id),
            signed_evidence_hash TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
            issued_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
    ),
)

_SCHEMA_V9 = _Migration(
    9,
    "durable_recovery_fill_economics",
    (
        """
        CREATE TABLE execution_recovery_fills (
            fill_id TEXT PRIMARY KEY,
            recovery_command_id TEXT NOT NULL
                REFERENCES execution_recovery_commands(recovery_command_id),
            parent_command_id TEXT NOT NULL
                REFERENCES execution_commands(command_id),
            cloid TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            quantity TEXT NOT NULL,
            signed_quantity TEXT NOT NULL,
            start_position TEXT NOT NULL,
            end_position TEXT NOT NULL,
            price TEXT NOT NULL,
            fee TEXT NOT NULL,
            closed_pnl TEXT NOT NULL,
            fee_token TEXT NOT NULL,
            crossed INTEGER NOT NULL CHECK (crossed IN (0, 1)),
            builder_fee TEXT,
            venue_oid INTEGER NOT NULL,
            venue_trade_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            account_snapshot_hash TEXT NOT NULL,
            venue_evidence_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_execution_recovery_fills_parent
        ON execution_recovery_fills (
            parent_command_id, occurred_at, fill_id
        )
        """,
    ),
)

_SCHEMA_V10 = _Migration(
    10,
    "venue_server_preflight_watermark",
    (
        """
        ALTER TABLE execution_dispatch_preflights
        ADD COLUMN account_server_time_ms INTEGER
        """,
        """
        CREATE UNIQUE INDEX idx_execution_parent_fill_venue_identity
        ON execution_fills (
            venue_oid, venue_trade_id, transaction_hash, occurred_at
        )
        WHERE venue_oid IS NOT NULL
          AND venue_trade_id IS NOT NULL
          AND transaction_hash IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_execution_recovery_fill_venue_identity
        ON execution_recovery_fills (
            venue_oid, venue_trade_id, transaction_hash, occurred_at
        )
        """,
    ),
)

_MIGRATIONS = (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _SCHEMA_V5,
    _SCHEMA_V6,
    _SCHEMA_V7,
    _SCHEMA_V8,
    _SCHEMA_V9,
    _SCHEMA_V10,
)
EXECUTION_SCHEMA_VERSION = 10


def _execution_schema_objects(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE sql IS NOT NULL AND (
            name = 'execution_schema_migrations'
            OR name LIKE 'execution_%'
            OR name LIKE 'idx_execution_%'
            OR tbl_name = 'execution_schema_migrations'
            OR tbl_name LIKE 'execution_%'
        )
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        (str(row["type"]), str(row["name"]), " ".join(str(row["sql"]).split()))
        for row in rows
    )


@lru_cache(maxsize=1)
def _expected_execution_schema_objects() -> tuple[tuple[str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE execution_schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        for migration in _MIGRATIONS:
            for statement in migration.statements:
                connection.execute(statement)
        return _execution_schema_objects(connection)
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class DispatchPreflight:
    """Fresh deterministic send-time attestation from the trusted control plane."""

    command_id: str
    ticket_hash: str
    plan_hash: str
    environment: Environment
    account_id: str
    account_snapshot_hash: str
    metadata_hash: str
    market_snapshot_hash: str
    risk_policy_hash: str
    observed_at: datetime
    expires_at: datetime
    passed: bool
    account_server_time_ms: int | None = None
    preflight_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "command_id",
            _text(self.command_id, field="command_id", maximum=128),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, field="account_id", maximum=256),
        )
        for field in (
            "ticket_hash",
            "plan_hash",
            "account_snapshot_hash",
            "metadata_hash",
            "market_snapshot_hash",
            "risk_policy_hash",
        ):
            object.__setattr__(
                self,
                field,
                _hash(getattr(self, field), field=field),
            )
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("preflight environment is invalid") from error
        if self.environment not in {Environment.TESTNET, Environment.MAINNET}:
            raise ValidationError("preflight must target testnet or mainnet")
        observed = _utc(self.observed_at, field="observed_at")
        expires = _utc(self.expires_at, field="expires_at")
        if not observed < expires:
            raise ValidationError("preflight must expire after observation")
        if expires - observed > timedelta(seconds=_MAX_PREFLIGHT_LIFETIME_SECONDS):
            raise ValidationError("preflight lifetime exceeds compiled freshness bound")
        if type(self.passed) is not bool:
            raise TypeError("preflight passed must be bool")
        if self.account_server_time_ms is not None:
            if (
                type(self.account_server_time_ms) is not int
                or self.account_server_time_ms < 0
                or self.account_server_time_ms
                > int(observed.timestamp() * 1_000) + 5_000
            ):
                raise ValidationError("preflight account server time is invalid")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        expected = _record_hash("dispatch-preflight-attestation", self.payload())
        if self.preflight_hash:
            supplied = _hash(self.preflight_hash, field="preflight_hash")
            if supplied != expected:
                raise ValidationError("preflight_hash does not match attestation")
        object.__setattr__(self, "preflight_hash", expected)

    def payload(self) -> dict[str, object]:
        payload = {
            "schema_version": (
                "dispatch_preflight.v1"
                if self.account_server_time_ms is None
                else "dispatch_preflight.v2"
            ),
            "command_id": self.command_id,
            "ticket_hash": self.ticket_hash,
            "plan_hash": self.plan_hash,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "account_snapshot_hash": self.account_snapshot_hash,
            "metadata_hash": self.metadata_hash,
            "market_snapshot_hash": self.market_snapshot_hash,
            "risk_policy_hash": self.risk_policy_hash,
            "observed_at": _time_text(self.observed_at, field="observed_at"),
            "expires_at": _time_text(self.expires_at, field="expires_at"),
            "passed": self.passed,
        }
        if self.account_server_time_ms is not None:
            payload["account_server_time_ms"] = self.account_server_time_ms
        return payload

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "preflight_hash": self.preflight_hash}


@dataclass(frozen=True, slots=True)
class SignedEnvelopeEvidence:
    """Immutable non-secret evidence for the exact signed wire."""

    command_id: str
    preflight_hash: str
    environment: Environment
    endpoint: str
    account_id: str
    plan_hash: str
    action_hash: str
    nonce: int
    wire_hash: str
    signature_hash: str
    envelope_hash: str
    signer_binding_hash: str
    authorization_expires_at_ms: int
    expires_after_ms: int
    signed_at_ms: int
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        for field, maximum in (
            ("command_id", 128),
            ("endpoint", 256),
            ("account_id", 256),
        ):
            object.__setattr__(
                self, field, _text(getattr(self, field), field=field, maximum=maximum)
            )
        for field in (
            "preflight_hash",
            "plan_hash",
            "action_hash",
            "wire_hash",
            "signature_hash",
            "envelope_hash",
            "signer_binding_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field=field))
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("signed evidence environment is invalid") from error
        if self.environment is not Environment.TESTNET:
            raise ValidationError("signed evidence is testnet-only")
        if self.endpoint != "https://api.hyperliquid-testnet.xyz/exchange":
            raise ValidationError("signed evidence endpoint is not testnet exchange")
        for field in (
            "nonce",
            "authorization_expires_at_ms",
            "expires_after_ms",
            "signed_at_ms",
        ):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValidationError(f"{field} must be a non-negative integer")
        if not self.signed_at_ms < self.expires_after_ms <= self.authorization_expires_at_ms:
            raise ValidationError("signed evidence expiry ordering is invalid")
        expected = _record_hash("signed-envelope-evidence", self.payload())
        if self.evidence_hash:
            supplied = _hash(self.evidence_hash, field="evidence_hash")
            if supplied != expected:
                raise ValidationError("signed evidence hash does not match")
        object.__setattr__(self, "evidence_hash", expected)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "signed_envelope_evidence.v1",
            "command_id": self.command_id,
            "preflight_hash": self.preflight_hash,
            "environment": self.environment.value,
            "endpoint": self.endpoint,
            "account_id": self.account_id,
            "plan_hash": self.plan_hash,
            "action_hash": self.action_hash,
            "nonce": self.nonce,
            "wire_hash": self.wire_hash,
            "signature_hash": self.signature_hash,
            "envelope_hash": self.envelope_hash,
            "signer_binding_hash": self.signer_binding_hash,
            "authorization_expires_at_ms": self.authorization_expires_at_ms,
            "expires_after_ms": self.expires_after_ms,
            "signed_at_ms": self.signed_at_ms,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class TransportOutcomeEvidence:
    """Immutable result or explicit crash-boundary evidence for one attempt."""

    command_id: str
    attempt_id: str
    signed_evidence_hash: str
    endpoint: str
    attempted_at_ms: int
    outcome: str
    http_status: int | None
    detail_code: str
    response_hash: str | None
    transport_attempt_hash: str | None
    send_count: int | None
    retry_performed: bool
    venue_write_attempted: bool | None
    evidence_basis: str = "transport_result"
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        for field, maximum in (
            ("command_id", 128),
            ("attempt_id", 128),
            ("endpoint", 256),
            ("detail_code", 128),
        ):
            object.__setattr__(
                self, field, _text(getattr(self, field), field=field, maximum=maximum)
            )
        object.__setattr__(
            self,
            "signed_evidence_hash",
            _hash(self.signed_evidence_hash, field="signed_evidence_hash"),
        )
        for field in ("response_hash", "transport_attempt_hash"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _hash(value, field=field))
        if self.endpoint != "https://api.hyperliquid-testnet.xyz/exchange":
            raise ValidationError("transport evidence endpoint is not testnet exchange")
        if type(self.attempted_at_ms) is not int or self.attempted_at_ms < 0:
            raise ValidationError("attempted_at_ms must be non-negative")
        if self.outcome not in {"response_received", "unknown"}:
            raise ValidationError("transport evidence outcome is invalid")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValidationError("http_status is invalid")
        if self.send_count is not None and self.send_count != 1:
            raise ValidationError("transport evidence send_count must be one or unknown")
        if self.retry_performed is not False:
            raise ValidationError("transport evidence cannot report a retry")
        if self.venue_write_attempted not in {True, False, None}:
            raise ValidationError("venue_write_attempted must be boolean or unknown")
        if self.evidence_basis not in {"transport_result", "claim_expiry"}:
            raise ValidationError("transport evidence basis is invalid")
        if self.evidence_basis == "transport_result" and (
            self.transport_attempt_hash is None
            or self.send_count != 1
            or self.venue_write_attempted is not True
        ):
            raise ValidationError("transport result lacks one-send evidence")
        if self.evidence_basis == "claim_expiry" and (
            self.transport_attempt_hash is not None
            or self.send_count is not None
            or self.venue_write_attempted is not None
        ):
            raise ValidationError("claim-expiry evidence must preserve uncertainty")
        if self.outcome == "response_received" and self.response_hash is None:
            raise ValidationError("response outcome requires response_hash")
        expected = _record_hash("transport-outcome-evidence", self.payload())
        if self.evidence_hash:
            supplied = _hash(self.evidence_hash, field="evidence_hash")
            if supplied != expected:
                raise ValidationError("transport evidence hash does not match")
        object.__setattr__(self, "evidence_hash", expected)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "transport_outcome_evidence.v1",
            "command_id": self.command_id,
            "attempt_id": self.attempt_id,
            "signed_evidence_hash": self.signed_evidence_hash,
            "endpoint": self.endpoint,
            "attempted_at_ms": self.attempted_at_ms,
            "outcome": self.outcome,
            "http_status": self.http_status,
            "detail_code": self.detail_code,
            "response_hash": self.response_hash,
            "transport_attempt_hash": self.transport_attempt_hash,
            "send_count": self.send_count,
            "retry_performed": self.retry_performed,
            "venue_write_attempted": self.venue_write_attempted,
            "evidence_basis": self.evidence_basis,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class NoopFenceResponseEvidence:
    """Durable proof that Hyperliquid accepted the exact same-nonce noop."""

    recovery_command_id: str
    attempt_id: str
    signed_evidence_hash: str
    transport_evidence_hash: str
    nonce: int
    response_json: str
    response_hash: str
    parsed_at: datetime
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        for field in ("recovery_command_id", "attempt_id"):
            object.__setattr__(
                self,
                field,
                _text(getattr(self, field), field=field, maximum=128),
            )
        for field in (
            "signed_evidence_hash",
            "transport_evidence_hash",
            "response_hash",
        ):
            object.__setattr__(
                self,
                field,
                _hash(getattr(self, field), field=field),
            )
        _nonnegative_int(self.nonce, field="nonce")
        if self.response_json != _NOOP_DEFAULT_RESPONSE_JSON:
            raise ValidationError(
                "noop fence response must be the exact canonical default success"
            )
        expected_response_hash = domain_hash(
            _SUBMISSION_RESPONSE_HASH_DOMAIN,
            _NOOP_DEFAULT_RESPONSE,
        )
        if self.response_hash != expected_response_hash:
            raise ValidationError("noop fence response hash differs from canonical body")
        object.__setattr__(
            self,
            "parsed_at",
            _utc(self.parsed_at, field="parsed_at"),
        )
        expected = _record_hash("noop-fence-response-evidence", self.payload())
        if self.evidence_hash:
            if _hash(self.evidence_hash, field="evidence_hash") != expected:
                raise ValidationError("noop fence evidence hash differs")
        object.__setattr__(self, "evidence_hash", expected)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "noop_fence_response_evidence.v1",
            "recovery_command_id": self.recovery_command_id,
            "attempt_id": self.attempt_id,
            "signed_evidence_hash": self.signed_evidence_hash,
            "transport_evidence_hash": self.transport_evidence_hash,
            "nonce": self.nonce,
            "response_json": self.response_json,
            "response_hash": self.response_hash,
            "parsed_at": _time_text(self.parsed_at, field="parsed_at"),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class NoopFenceResolution:
    parent_command_id: str
    recovery_command_id: str
    incident_id: str
    original_attempt_id: str
    original_nonce: int
    response_evidence_hash: str
    proof_hash: str
    account_snapshot_hash: str
    observed_at: datetime
    resolution_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "noop_fence_resolution.v1",
            "parent_command_id": self.parent_command_id,
            "recovery_command_id": self.recovery_command_id,
            "incident_id": self.incident_id,
            "original_attempt_id": self.original_attempt_id,
            "original_nonce": self.original_nonce,
            "response_evidence_hash": self.response_evidence_hash,
            "proof_hash": self.proof_hash,
            "account_snapshot_hash": self.account_snapshot_hash,
            "observed_at": _time_text(self.observed_at, field="observed_at"),
            "resolution_hash": self.resolution_hash,
        }


@dataclass(frozen=True, slots=True)
class RecoveryPermit:
    permit_id: str
    token_hash: str
    parent_command_id: str
    incident_id: str
    kind: str
    environment: Environment
    account_id: str
    source_hash: str
    preflight_hash: str | None
    recovery_hash: str
    recovery_material: Mapping[str, Any]
    safety_policy_hash: str
    original_attempt_id: str | None
    original_nonce: int | None
    issuer_id: str
    audience: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for field, maximum in (
            ("permit_id", 128),
            ("parent_command_id", 128),
            ("incident_id", 128),
            ("account_id", 256),
            ("issuer_id", 256),
            ("audience", 256),
        ):
            object.__setattr__(
                self, field, _text(getattr(self, field), field=field, maximum=maximum)
            )
        if self.kind not in _RECOVERY_KINDS:
            raise ValidationError("recovery permit kind is invalid")
        for field in (
            "token_hash",
            "source_hash",
            "recovery_hash",
            "safety_policy_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field=field))
        if self.preflight_hash is not None:
            object.__setattr__(
                self,
                "preflight_hash",
                _hash(self.preflight_hash, field="preflight_hash"),
            )
        if not isinstance(self.recovery_material, Mapping):
            raise TypeError("recovery_material must be a mapping")
        material_json, _ = _canonical_payload(dict(self.recovery_material))
        material = json.loads(material_json)
        if not isinstance(material, dict):
            raise ValidationError("recovery_material must encode an object")
        if material.get("kind") != self.kind:
            raise ValidationError("recovery material kind differs from permit")
        if domain_hash(
            "trading-harness/hyperliquid-recovery-action/v1", material
        ) != self.recovery_hash:
            raise ValidationError("recovery material hash differs from recovery_hash")
        object.__setattr__(self, "recovery_material", material)
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("recovery permit environment is invalid") from error
        if self.environment is not Environment.TESTNET:
            raise ValidationError("recovery permits are testnet-only")
        issued = _utc(self.issued_at, field="issued_at")
        expires = _utc(self.expires_at, field="expires_at")
        if not issued < expires <= issued + timedelta(
            seconds=_MAX_RECOVERY_PERMIT_SECONDS
        ):
            raise ValidationError("recovery permit expiry is outside short bound")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if self.kind == "noop_fence":
            if (
                self.original_attempt_id is None
                or self.original_nonce is None
                or self.preflight_hash is None
            ):
                raise ValidationError(
                    "noop permit requires original attempt, nonce, and preflight"
                )
            object.__setattr__(
                self,
                "original_attempt_id",
                _text(
                    self.original_attempt_id,
                    field="original_attempt_id",
                    maximum=128,
                ),
            )
            _nonnegative_int(self.original_nonce, field="original_nonce")
        elif self.original_attempt_id is not None or self.original_nonce is not None:
            raise ValidationError("only noop permits bind an original attempt nonce")


@dataclass(frozen=True, slots=True)
class RecoveryCommand:
    recovery_command_id: str
    permit_id: str
    parent_command_id: str
    incident_id: str
    kind: str
    priority: int
    source_hash: str
    preflight_hash: str | None
    recovery_hash: str
    recovery_material_json: str
    recovery_material_hash: str
    safety_policy_hash: str
    original_attempt_id: str | None
    original_nonce: int | None
    state: str
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None
    revision: int


@dataclass(frozen=True, slots=True)
class RecoveryOutbox:
    recovery_command_id: str
    state: str
    worker_id: str | None
    fencing_token: int
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    current_attempt_id: str | None
    attempt_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    attempt_id: str
    recovery_command_id: str
    worker_id: str
    fencing_token: int
    signed_evidence_hash: str
    transport_evidence_hash: str | None
    nonce: int
    action_hash: str
    wire_hash: str
    state: str
    prepared_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoverySigningAuthority:
    recovery_command_id: str
    permit_id: str
    parent_command_id: str
    incident_id: str
    kind: str
    source_hash: str
    preflight_hash: str | None
    recovery_hash: str
    safety_policy_hash: str
    original_attempt_id: str | None
    original_nonce: int | None
    worker_id: str
    fencing_token: int
    permit_expires_at: datetime
    lease_expires_at: datetime
    authority_hash: str


@dataclass(frozen=True, slots=True)
class RecoverySubmissionAuthority:
    recovery_command_id: str
    attempt_id: str
    signed_evidence_hash: str
    nonce: int
    action_hash: str
    wire_hash: str
    worker_id: str
    fencing_token: int
    lease_expires_at: datetime
    authority_hash: str


@dataclass(frozen=True, slots=True)
class EntrySubmissionAuthority:
    command_id: str
    attempt_id: str
    signed_evidence_hash: str
    nonce: int
    action_hash: str
    wire_hash: str
    worker_id: str
    fencing_token: int
    lease_expires_at: datetime
    authority_hash: str


@dataclass(frozen=True, slots=True)
class RecoveryReconciliationProof:
    recovery_command_id: str
    kind: str
    account_snapshot_hash: str
    observed_at: datetime
    signed_position_quantity: Decimal
    protected_quantity: Decimal
    open_order_cloids: tuple[str, ...]
    affected_cloids: tuple[str, ...]
    resolved_original_nonce: int | None
    resolved_original_outcome: str | None
    complete: bool
    success: bool
    proof_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recovery_command_id",
            _text(
                self.recovery_command_id,
                field="recovery_command_id",
                maximum=128,
            ),
        )
        if self.kind not in _RECOVERY_KINDS:
            raise ValidationError("recovery proof kind is invalid")
        object.__setattr__(
            self,
            "account_snapshot_hash",
            _hash(self.account_snapshot_hash, field="account_snapshot_hash"),
        )
        object.__setattr__(self, "observed_at", _utc(self.observed_at, field="observed_at"))
        object.__setattr__(
            self,
            "signed_position_quantity",
            _decimal(self.signed_position_quantity, field="signed_position_quantity"),
        )
        object.__setattr__(
            self,
            "protected_quantity",
            _decimal(
                self.protected_quantity,
                field="protected_quantity",
                nonnegative=True,
            ),
        )
        for field in ("open_order_cloids", "affected_cloids"):
            values = tuple(getattr(self, field))
            if len(values) != len(set(values)):
                raise ValidationError(f"{field} contains duplicates")
            for value in values:
                if (
                    not isinstance(value, str)
                    or len(value) != 34
                    or not value.startswith("0x")
                    or any(character not in _HASH_CHARS for character in value[2:])
                ):
                    raise ValidationError(f"{field} contains invalid CLOID")
            object.__setattr__(self, field, tuple(sorted(values)))
        if self.resolved_original_nonce is not None:
            _nonnegative_int(
                self.resolved_original_nonce, field="resolved_original_nonce"
            )
        if self.resolved_original_outcome not in {
            None,
            "accepted",
            "rejected",
            "absent",
            "fenced",
        }:
            raise ValidationError("resolved_original_outcome is invalid")
        if type(self.complete) is not bool or type(self.success) is not bool:
            raise TypeError("complete and success must be bool")
        if self.kind == "noop_fence" and self.success and (
            self.resolved_original_nonce is None
            or self.resolved_original_outcome is None
        ):
            raise ValidationError("successful noop proof must resolve original nonce")
        if self.kind != "noop_fence" and (
            self.resolved_original_nonce is not None
            or self.resolved_original_outcome is not None
        ):
            raise ValidationError("only noop proof may resolve an original nonce")
        if self.kind == "cancel_by_cloid" and self.success:
            if not self.affected_cloids or set(self.affected_cloids) & set(
                self.open_order_cloids
            ):
                raise ValidationError("cancel proof does not show affected CLOIDs absent")
        expected = _record_hash("recovery-reconciliation-proof", self.payload())
        if self.proof_hash:
            if _hash(self.proof_hash, field="proof_hash") != expected:
                raise ValidationError("recovery proof hash differs")
        object.__setattr__(self, "proof_hash", expected)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "recovery_reconciliation_proof.v1",
            "recovery_command_id": self.recovery_command_id,
            "kind": self.kind,
            "account_snapshot_hash": self.account_snapshot_hash,
            "observed_at": _time_text(self.observed_at, field="observed_at"),
            "signed_position_quantity": _decimal_text(
                self.signed_position_quantity, field="signed_position_quantity"
            ),
            "protected_quantity": _decimal_text(
                self.protected_quantity, field="protected_quantity"
            ),
            "open_order_cloids": list(self.open_order_cloids),
            "affected_cloids": list(self.affected_cloids),
            "resolved_original_nonce": self.resolved_original_nonce,
            "resolved_original_outcome": self.resolved_original_outcome,
            "complete": self.complete,
            "success": self.success,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "proof_hash": self.proof_hash}


@dataclass(frozen=True, slots=True)
class SignedRecoveryEvidence:
    recovery_command_id: str
    incident_id: str
    kind: str
    source_hash: str
    recovery_hash: str
    signing_authority_hash: str
    safety_policy_hash: str
    nonce: int
    wire_hash: str
    action_hash: str
    signature_hash: str
    envelope_hash: str
    signer_binding_hash: str
    expires_after_ms: int
    signed_at_ms: int
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        for field in ("recovery_command_id", "incident_id"):
            object.__setattr__(
                self,
                field,
                _text(getattr(self, field), field=field, maximum=128),
            )
        if self.kind not in _RECOVERY_KINDS:
            raise ValidationError("signed recovery kind is invalid")
        for field in (
            "source_hash",
            "recovery_hash",
            "signing_authority_hash",
            "safety_policy_hash",
            "wire_hash",
            "action_hash",
            "signature_hash",
            "envelope_hash",
            "signer_binding_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field=field))
        for field in ("nonce", "expires_after_ms", "signed_at_ms"):
            _nonnegative_int(getattr(self, field), field=field)
        if self.signed_at_ms >= self.expires_after_ms:
            raise ValidationError("signed recovery evidence is already expired")
        expected = _record_hash("signed-recovery-evidence", self.payload())
        if self.evidence_hash:
            if _hash(self.evidence_hash, field="evidence_hash") != expected:
                raise ValidationError("signed recovery evidence hash differs")
        object.__setattr__(self, "evidence_hash", expected)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "signed_recovery_evidence.v1",
            "recovery_command_id": self.recovery_command_id,
            "incident_id": self.incident_id,
            "kind": self.kind,
            "source_hash": self.source_hash,
            "recovery_hash": self.recovery_hash,
            "signing_authority_hash": self.signing_authority_hash,
            "safety_policy_hash": self.safety_policy_hash,
            "nonce": self.nonce,
            "wire_hash": self.wire_hash,
            "action_hash": self.action_hash,
            "signature_hash": self.signature_hash,
            "envelope_hash": self.envelope_hash,
            "signer_binding_hash": self.signer_binding_hash,
            "expires_after_ms": self.expires_after_ms,
            "signed_at_ms": self.signed_at_ms,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class TrustedApproval:
    approval_id: str
    ticket_hash: str
    token_hash: str
    approver_id: str
    audience: str
    environment: Environment
    account_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for field, maximum in (
            ("approval_id", 128),
            ("approver_id", 256),
            ("audience", 256),
            ("account_id", 256),
        ):
            object.__setattr__(
                self, field, _text(getattr(self, field), field=field, maximum=maximum)
            )
        object.__setattr__(self, "ticket_hash", _hash(self.ticket_hash, field="ticket_hash"))
        object.__setattr__(self, "token_hash", _hash(self.token_hash, field="token_hash"))
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("approval environment is invalid") from error
        if self.environment not in {Environment.TESTNET, Environment.MAINNET}:
            raise ValidationError("approval must target testnet or mainnet")
        issued = _utc(self.issued_at, field="issued_at")
        expires = _utc(self.expires_at, field="expires_at")
        if expires <= issued:
            raise ValidationError("approval must expire after issuance")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command_id: str
    ticket_hash: str
    plan_hash: str
    approval_id: str
    state: str
    reserved_loss: Decimal
    reserved_notional: Decimal
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None
    revision: int


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    command_id: str
    state: str
    worker_id: str | None
    fencing_token: int
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    current_attempt_id: str | None
    attempt_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: str
    command_id: str
    worker_id: str
    fencing_token: int
    preflight_hash: str | None
    signed_evidence_hash: str | None
    transport_evidence_hash: str | None
    nonce: int
    action_hash: str
    wire_hash: str
    state: str
    response_hash: str | None
    prepared_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LegRecord:
    command_id: str
    role: str
    cloid: str
    intent_hash: str
    side: str
    reduce_only: bool
    requested_quantity: Decimal
    cumulative_filled: Decimal
    venue_oid: int | None
    status: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PositionRecord:
    instrument: str
    signed_quantity: Decimal
    account_snapshot_hash: str
    observed_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class ProtectionRecord:
    command_id: str
    instrument: str
    state: str
    signed_position_quantity: Decimal
    protected_quantity: Decimal
    stop_cloid: str
    observed_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    incident_id: str
    command_id: str | None
    code: str
    severity: str
    state: str
    opened_at: datetime
    updated_at: datetime
    revision: int
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_sequence: int
    event_hash: str
    previous_hash: str | None
    command_id: str | None
    event_type: str
    occurred_at: datetime
    payload_json: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class LegReconciliation:
    role: str
    cloid: str
    status: str
    cumulative_filled: Decimal
    venue_oid: int | None = None

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValidationError("reconciliation role is invalid")
        object.__setattr__(self, "cloid", _text(self.cloid, field="cloid", maximum=128))
        if self.status not in _LEG_STATES - {"queued", "submitted_unknown"}:
            raise ValidationError("reconciliation leg status is invalid")
        quantity = _decimal(
            self.cumulative_filled,
            field="cumulative_filled",
            nonnegative=True,
        )
        object.__setattr__(self, "cumulative_filled", quantity)
        if self.venue_oid is not None and (
            type(self.venue_oid) is not int or self.venue_oid < 0
        ):
            raise ValidationError("venue_oid must be a non-negative integer or None")

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "cloid": self.cloid,
            "status": self.status,
            "cumulative_filled": _decimal_text(
                self.cumulative_filled, field="cumulative_filled"
            ),
            "venue_oid": self.venue_oid,
        }


@dataclass(frozen=True, slots=True)
class VenueFill:
    fill_id: str
    role: str
    cloid: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    occurred_at: datetime
    venue_oid: int | None = None
    venue_trade_id: int | None = None
    transaction_hash: str | None = None
    closed_pnl: Decimal | None = None
    fee_token: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _text(self.fill_id, field="fill_id", maximum=256))
        if self.role not in _ROLES:
            raise ValidationError("fill role is invalid")
        object.__setattr__(self, "cloid", _text(self.cloid, field="cloid", maximum=128))
        for field in ("quantity", "price"):
            value = _decimal(getattr(self, field), field=field)
            if value <= ZERO:
                raise ValidationError(f"{field} must be positive")
            object.__setattr__(self, field, value)
        object.__setattr__(
            self,
            "fee",
            _decimal(self.fee, field="fee", nonnegative=True),
        )
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, field="occurred_at"))
        for field in ("venue_oid", "venue_trade_id"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValidationError(f"{field} must be a non-negative integer or None")
        venue_identity = (
            self.venue_oid,
            self.venue_trade_id,
            self.transaction_hash,
        )
        if any(value is not None for value in venue_identity) and not all(
            value is not None for value in venue_identity
        ):
            raise ValidationError("venue fill identity must be complete or absent")
        if self.transaction_hash is not None and (
            not isinstance(self.transaction_hash, str)
            or not re.fullmatch(r"0x[0-9a-f]{64}", self.transaction_hash)
        ):
            raise ValidationError("transaction_hash is invalid")
        if self.closed_pnl is not None:
            object.__setattr__(
                self,
                "closed_pnl",
                _decimal(self.closed_pnl, field="closed_pnl"),
            )
        if self.fee_token is not None and (
            not isinstance(self.fee_token, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", self.fee_token)
        ):
            raise ValidationError("fee_token is invalid")
        if self.observed_at is not None:
            observed = _utc(self.observed_at, field="observed_at")
            if observed < self.occurred_at:
                raise ValidationError("fill observed_at precedes occurred_at")
            object.__setattr__(self, "observed_at", observed)

    def as_dict(self) -> dict[str, object]:
        return {
            "fill_id": self.fill_id,
            "role": self.role,
            "cloid": self.cloid,
            "quantity": _decimal_text(self.quantity, field="quantity"),
            "price": _decimal_text(self.price, field="price"),
            "fee": _decimal_text(self.fee, field="fee"),
            "occurred_at": _time_text(self.occurred_at, field="occurred_at"),
            "venue_oid": self.venue_oid,
            "venue_trade_id": self.venue_trade_id,
            "transaction_hash": self.transaction_hash,
            "closed_pnl": (
                None
                if self.closed_pnl is None
                else _decimal_text(self.closed_pnl, field="closed_pnl")
            ),
            "fee_token": self.fee_token,
            "observed_at": (
                None
                if self.observed_at is None
                else _time_text(self.observed_at, field="observed_at")
            ),
        }


@dataclass(frozen=True, slots=True)
class RecoveryVenueFill:
    """Exact economics for one durably owned reduce-only recovery fill."""

    fill_id: str
    recovery_command_id: str
    parent_command_id: str
    cloid: str
    symbol: str
    side: str
    quantity: Decimal
    signed_quantity: Decimal
    start_position: Decimal
    end_position: Decimal
    price: Decimal
    fee: Decimal
    closed_pnl: Decimal
    fee_token: str
    crossed: bool
    builder_fee: Decimal | None
    venue_oid: int
    venue_trade_id: int
    transaction_hash: str
    occurred_at: datetime
    observed_at: datetime
    account_snapshot_hash: str
    venue_evidence_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fill_id", _text(self.fill_id, field="fill_id", maximum=256)
        )
        object.__setattr__(
            self,
            "recovery_command_id",
            _text(
                self.recovery_command_id,
                field="recovery_command_id",
                maximum=128,
            ),
        )
        object.__setattr__(
            self,
            "parent_command_id",
            _text(self.parent_command_id, field="parent_command_id", maximum=128),
        )
        object.__setattr__(
            self, "cloid", _text(self.cloid, field="cloid", maximum=128)
        )
        object.__setattr__(
            self, "symbol", _text(self.symbol, field="symbol", maximum=64)
        )
        if self.side not in {"buy", "sell"}:
            raise ValidationError("recovery fill side is invalid")
        for field in (
            "quantity",
            "signed_quantity",
            "start_position",
            "end_position",
            "price",
            "fee",
            "closed_pnl",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), field=field))
        if self.quantity <= ZERO or self.price <= ZERO or self.fee < ZERO:
            raise ValidationError("recovery fill economics are invalid")
        if abs(self.signed_quantity) != self.quantity or (
            (self.side == "buy") != (self.signed_quantity > ZERO)
        ):
            raise ValidationError("recovery fill side and signed quantity differ")
        if self.end_position != self.start_position + self.signed_quantity:
            raise ValidationError("recovery fill position transition is invalid")
        if (
            not isinstance(self.fee_token, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", self.fee_token)
        ):
            raise ValidationError("recovery fill fee token is invalid")
        if type(self.crossed) is not bool:
            raise TypeError("recovery fill crossed must be boolean")
        if self.builder_fee is not None:
            object.__setattr__(
                self,
                "builder_fee",
                _decimal(self.builder_fee, field="builder_fee", nonnegative=True),
            )
        for field in ("venue_oid", "venue_trade_id"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValidationError(f"{field} must be a non-negative integer")
        if not isinstance(self.transaction_hash, str) or not re.fullmatch(
            r"0x[0-9a-f]{64}", self.transaction_hash
        ):
            raise ValidationError("recovery fill transaction hash is invalid")
        occurred = _utc(self.occurred_at, field="occurred_at")
        observed = _utc(self.observed_at, field="observed_at")
        if observed < occurred:
            raise ValidationError("recovery fill observation precedes occurrence")
        object.__setattr__(self, "occurred_at", occurred)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(
            self,
            "account_snapshot_hash",
            _hash(self.account_snapshot_hash, field="account_snapshot_hash"),
        )
        object.__setattr__(
            self,
            "venue_evidence_hash",
            _hash(self.venue_evidence_hash, field="venue_evidence_hash"),
        )
        delta = occurred - _EPOCH
        occurred_ms = (
            delta.days * 86_400_000
            + delta.seconds * 1_000
            + delta.microseconds // 1_000
        )
        expected_fill_id = (
            f"hyperliquid:{self.symbol}:{occurred_ms}:"
            f"{self.venue_trade_id}:{self.venue_oid}"
        )
        if self.fill_id != expected_fill_id:
            raise ValidationError("recovery fill identity is not canonical")

    def as_dict(self) -> dict[str, object]:
        return {
            "fill_id": self.fill_id,
            "recovery_command_id": self.recovery_command_id,
            "parent_command_id": self.parent_command_id,
            "cloid": self.cloid,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": _decimal_text(self.quantity, field="quantity"),
            "signed_quantity": _decimal_text(
                self.signed_quantity, field="signed_quantity"
            ),
            "start_position": _decimal_text(
                self.start_position, field="start_position"
            ),
            "end_position": _decimal_text(self.end_position, field="end_position"),
            "price": _decimal_text(self.price, field="price"),
            "fee": _decimal_text(self.fee, field="fee"),
            "closed_pnl": _decimal_text(self.closed_pnl, field="closed_pnl"),
            "fee_token": self.fee_token,
            "crossed": self.crossed,
            "builder_fee": (
                None
                if self.builder_fee is None
                else _decimal_text(self.builder_fee, field="builder_fee")
            ),
            "venue_oid": self.venue_oid,
            "venue_trade_id": self.venue_trade_id,
            "transaction_hash": self.transaction_hash,
            "occurred_at": _time_text(self.occurred_at, field="occurred_at"),
            "observed_at": _time_text(self.observed_at, field="observed_at"),
            "account_snapshot_hash": self.account_snapshot_hash,
            "venue_evidence_hash": self.venue_evidence_hash,
        }


_REQUIRED_TABLES = frozenset(
    {
        "execution_schema_migrations",
        "execution_store_identity",
        "execution_plans",
        "execution_plan_legs",
        "execution_tickets",
        "execution_approvals",
        "execution_exposure",
        "execution_commands",
        "execution_command_legs",
        "execution_outbox",
        "execution_attempts",
        "execution_dispatch_preflights",
        "execution_signed_envelopes",
        "execution_transport_outcomes",
        "execution_submission_authorities",
        "execution_infrastructure_grants",
        "execution_recovery_permits",
        "execution_recovery_commands",
        "execution_recovery_outbox",
        "execution_signed_recovery_evidence",
        "execution_recovery_signing_authorities",
        "execution_recovery_attempts",
        "execution_recovery_transport_evidence",
        "execution_noop_fence_responses",
        "execution_recovery_reconciliations",
        "execution_recovery_fills",
        "execution_reconciliations",
        "execution_fills",
        "execution_positions",
        "execution_protection",
        "execution_incidents",
        "execution_events",
    }
)


class ExecutionStore:
    """One-account, one-environment protected execution state machine."""

    def __init__(
        self,
        path: str | Path,
        *,
        environment: Environment,
        account_id: str,
        max_reserved_loss: Decimal | str | int,
        max_reserved_notional: Decimal | str | int,
        busy_timeout_ms: int = 5_000,
        must_exist: bool = False,
    ) -> None:
        if str(path) == ":memory:":
            raise ValidationError("ExecutionStore requires a file-backed database")
        if not isinstance(environment, Environment):
            try:
                environment = Environment(environment)
            except (TypeError, ValueError) as error:
                raise ValidationError("environment must be explicit testnet or mainnet") from error
        if environment is not Environment.TESTNET:
            raise ValidationError(
                "execution store is testnet-only until cryptographic mainnet "
                "authority is implemented"
            )
        self.environment = environment
        self.account_id = _text(account_id, field="account_id", maximum=256)
        self.max_reserved_loss = _decimal(
            max_reserved_loss, field="max_reserved_loss"
        )
        self.max_reserved_notional = _decimal(
            max_reserved_notional, field="max_reserved_notional"
        )
        if self.max_reserved_loss <= ZERO or self.max_reserved_notional <= ZERO:
            raise ValidationError("execution-store reservation caps must be positive")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValidationError("busy_timeout_ms must be a positive integer")
        if type(must_exist) is not bool:
            raise TypeError("must_exist must be a boolean")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._must_exist = must_exist
        if not must_exist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(
        self,
        *,
        read_only: bool = False,
        verification_path: Path | None = None,
    ) -> sqlite3.Connection:
        database_path = self.path if verification_path is None else verification_path
        database: str | Path = database_path
        if read_only:
            immutable = "&immutable=1" if verification_path is None else ""
            database = f"{database_path.absolute().as_uri()}?mode=ro{immutable}"
        elif self._must_exist:
            database = f"{self.path.absolute().as_uri()}?mode=rw"
        connection = sqlite3.connect(
            database,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            uri=read_only or self._must_exist,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA synchronous = FULL")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        if self._must_exist:
            self._verify_existing()
            return
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StorageError(f"SQLite refused WAL mode: {mode}")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM execution_schema_migrations ORDER BY version
                """
            ).fetchall()
            known = {migration.version: migration for migration in _MIGRATIONS}
            seen: list[int] = []
            for row in rows:
                version = int(row["version"])
                migration = known.get(version)
                if migration is None:
                    raise StorageError(
                        f"unknown execution migration version {version}"
                    )
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise StorageError(
                        f"execution migration {version} checksum or name mismatch"
                    )
                seen.append(version)
            if seen != list(range(1, len(seen) + 1)):
                raise StorageError("execution migration history is not contiguous")
            migration_time = _time_text(
                datetime.now(timezone.utc), field="migration_time"
            )
            for migration in _MIGRATIONS:
                if migration.version in seen:
                    continue
                if migration.version == 8:
                    legacy_prepared = connection.execute(
                        """
                        SELECT 1 FROM execution_attempts AS attempt
                        JOIN execution_commands AS command
                          ON command.command_id = attempt.command_id
                        WHERE attempt.state = 'prepared'
                          AND command.state != 'terminal'
                        LIMIT 1
                        """
                    ).fetchone()
                    if legacy_prepared is not None:
                        raise StorageError(
                            "cannot migrate a legacy prepared attempt across "
                            "the submission-authority boundary"
                        )
                if migration.version == 10:
                    active_legacy_preflight = connection.execute(
                        """
                        SELECT 1 FROM execution_dispatch_preflights AS preflight
                        JOIN execution_commands AS command
                          ON command.command_id = preflight.command_id
                        WHERE command.state != 'terminal'
                        LIMIT 1
                        """
                    ).fetchone()
                    if active_legacy_preflight is not None:
                        raise StorageError(
                            "cannot migrate an active preflight without its exact "
                            "venue-server watermark"
                        )
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO execution_schema_migrations (
                        version, name, checksum, applied_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        migration_time,
                    ),
                )
            self._verify_tables_locked(connection)
            identity = connection.execute(
                "SELECT * FROM execution_store_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                created = datetime.now(timezone.utc)
                identity_payload = self._identity_payload(created)
                identity_hash = _record_hash("identity", identity_payload)
                connection.execute(
                    """
                    INSERT INTO execution_store_identity (
                        singleton, environment, account_id, max_reserved_loss,
                        max_reserved_notional, created_at, record_hash
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.environment.value,
                        self.account_id,
                        _decimal_text(
                            self.max_reserved_loss, field="max_reserved_loss"
                        ),
                        _decimal_text(
                            self.max_reserved_notional,
                            field="max_reserved_notional",
                        ),
                        _time_text(created, field="created_at"),
                        identity_hash,
                    ),
                )
                exposure_payload = self._exposure_payload(
                    ZERO, ZERO, 1, created
                )
                connection.execute(
                    """
                    INSERT INTO execution_exposure (
                        singleton, reserved_loss, reserved_notional, revision,
                        updated_at, record_hash
                    ) VALUES (1, '0', '0', 1, ?, ?)
                    """,
                    (
                        _time_text(created, field="updated_at"),
                        _record_hash("exposure", exposure_payload),
                    ),
                )
            else:
                self._verify_identity_row(identity)
                self._read_exposure_locked(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise StorageError(
                f"execution schema initialization failed: {type(error).__name__}"
            ) from error
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_existing(self) -> None:
        database_snapshot = _snapshot_regular_file(
            self.path, label="execution database"
        )
        header = database_snapshot[2]
        if (
            len(header) != 20
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x02\x02"
        ):
            raise StorageError("execution database is not a WAL-mode SQLite file")
        verification_directory: tempfile.TemporaryDirectory[str] | None = None
        verification_path: Path | None = None
        wal_path = Path(f"{self.path}-wal")
        wal_snapshot = (
            _snapshot_regular_file(wal_path, label="execution WAL")
            if os.path.lexists(wal_path)
            else None
        )
        connection: sqlite3.Connection | None = None
        try:
            if wal_snapshot is not None and wal_snapshot[0][6] > 0:
                verification_directory = tempfile.TemporaryDirectory(
                    prefix=".execution-store-verify-",
                    dir=self.path.parent,
                )
                verification_path = (
                    Path(verification_directory.name) / self.path.name
                )
                _copy_verification_file(
                    self.path,
                    verification_path,
                    label="execution database",
                    expected=database_snapshot,
                )
                _copy_verification_file(
                    wal_path,
                    Path(f"{verification_path}-wal"),
                    label="execution WAL",
                    expected=wal_snapshot,
                )
                if (
                    _snapshot_regular_file(
                        self.path, label="execution database"
                    )
                    != database_snapshot
                    or _snapshot_regular_file(
                        wal_path, label="execution WAL"
                    )
                    != wal_snapshot
                ):
                    raise StorageError(
                        "execution database changed during verification snapshot"
                    )
            connection = self._connect(
                read_only=True, verification_path=verification_path
            )
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or int(query_only[0]) != 1:
                raise StorageError("execution database verification is not query-only")
            integrity = connection.execute("PRAGMA quick_check").fetchall()
            if not integrity or any(str(row[0]).lower() != "ok" for row in integrity):
                raise StorageError("execution database integrity check failed")
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_violations:
                raise StorageError("execution database foreign keys are invalid")
            rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM execution_schema_migrations ORDER BY version
                """
            ).fetchall()
            if len(rows) != len(_MIGRATIONS):
                raise StorageError("execution migration history is not current")
            for row, migration in zip(rows, _MIGRATIONS, strict=True):
                if (
                    row["version"] != migration.version
                    or row["name"] != migration.name
                    or row["checksum"] != migration.checksum
                ):
                    raise StorageError(
                        "execution migration history does not match current schema"
                    )
            self._verify_tables_locked(connection)
            if _execution_schema_objects(
                connection
            ) != _expected_execution_schema_objects():
                raise StorageError("execution database schema does not match")
            identity = connection.execute(
                "SELECT * FROM execution_store_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                raise StorageError("execution database identity is missing")
            self._verify_identity_row(identity)
            self._read_exposure_locked(connection)
            current_wal_snapshot = (
                _snapshot_regular_file(wal_path, label="execution WAL")
                if os.path.lexists(wal_path)
                else None
            )
            if (
                _snapshot_regular_file(
                    self.path, label="execution database"
                )
                != database_snapshot
                or current_wal_snapshot != wal_snapshot
            ):
                raise StorageError("execution database changed during verification")
        except sqlite3.Error as error:
            raise StorageError(
                f"execution database verification failed: {type(error).__name__}"
            ) from error
        except OSError as error:
            raise StorageError("execution database snapshot is unavailable") from error
        finally:
            if connection is not None:
                connection.close()
            if verification_directory is not None:
                verification_directory.cleanup()

    @staticmethod
    def _verify_tables_locked(connection: sqlite3.Connection) -> None:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not _REQUIRED_TABLES.issubset(tables):
            raise StorageError("execution schema is missing required tables")

    def _identity_payload(self, created_at: datetime) -> dict[str, object]:
        return {
            "environment": self.environment.value,
            "account_id": self.account_id,
            "max_reserved_loss": _decimal_text(
                self.max_reserved_loss, field="max_reserved_loss"
            ),
            "max_reserved_notional": _decimal_text(
                self.max_reserved_notional, field="max_reserved_notional"
            ),
            "created_at": _time_text(created_at, field="created_at"),
        }

    def _verify_identity_row(self, row: Mapping[str, Any]) -> None:
        try:
            environment = Environment(row["environment"])
            account_id = _stored_text(
                row["account_id"], field="account_id", maximum=256
            )
            loss = _decimal(row["max_reserved_loss"], field="max_reserved_loss")
            notional = _decimal(
                row["max_reserved_notional"], field="max_reserved_notional"
            )
            created = _parse_time(row["created_at"], field="identity created_at")
        except (TypeError, ValueError) as error:
            raise StorageError("persisted execution identity is invalid") from error
        expected = _record_hash(
            "identity",
            {
                "environment": environment.value,
                "account_id": account_id,
                "max_reserved_loss": _decimal_text(loss, field="max_reserved_loss"),
                "max_reserved_notional": _decimal_text(
                    notional, field="max_reserved_notional"
                ),
                "created_at": _time_text(created, field="created_at"),
            },
        )
        if _stored_hash(row["record_hash"], field="identity record_hash") != expected:
            raise StorageError("persisted execution identity hash does not match")
        if (
            environment is not self.environment
            or account_id != self.account_id
            or loss != self.max_reserved_loss
            or notional != self.max_reserved_notional
        ):
            raise StorageError(
                "execution database identity does not match environment/account/caps"
            )

    @staticmethod
    def _exposure_payload(
        reserved_loss: Decimal,
        reserved_notional: Decimal,
        revision: int,
        updated_at: datetime,
    ) -> dict[str, object]:
        return {
            "reserved_loss": _decimal_text(
                reserved_loss, field="reserved_loss"
            ),
            "reserved_notional": _decimal_text(
                reserved_notional, field="reserved_notional"
            ),
            "revision": revision,
            "updated_at": _time_text(updated_at, field="updated_at"),
        }

    def _read_exposure_locked(
        self, connection: sqlite3.Connection
    ) -> tuple[Decimal, Decimal, int, datetime]:
        row = connection.execute(
            "SELECT * FROM execution_exposure WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StorageError("execution exposure singleton is missing")
        loss = _decimal(row["reserved_loss"], field="reserved_loss", nonnegative=True)
        notional = _decimal(
            row["reserved_notional"],
            field="reserved_notional",
            nonnegative=True,
        )
        revision = int(row["revision"])
        updated = _parse_time(row["updated_at"], field="exposure updated_at")
        expected = _record_hash(
            "exposure", self._exposure_payload(loss, notional, revision, updated)
        )
        if _stored_hash(row["record_hash"], field="exposure record_hash") != expected:
            raise StorageError("persisted exposure hash does not match")
        return loss, notional, revision, updated

    def _write_exposure_locked(
        self,
        connection: sqlite3.Connection,
        *,
        loss: Decimal,
        notional: Decimal,
        previous_revision: int,
        at: datetime,
    ) -> None:
        if loss < ZERO or notional < ZERO:
            raise StorageError("exposure update would become negative")
        revision = previous_revision + 1
        payload = self._exposure_payload(loss, notional, revision, at)
        changed = connection.execute(
            """
            UPDATE execution_exposure SET
                reserved_loss = ?, reserved_notional = ?, revision = ?,
                updated_at = ?, record_hash = ?
            WHERE singleton = 1 AND revision = ?
            """,
            (
                _decimal_text(loss, field="reserved_loss"),
                _decimal_text(notional, field="reserved_notional"),
                revision,
                _time_text(at, field="updated_at"),
                _record_hash("exposure", payload),
                previous_revision,
            ),
        )
        if changed.rowcount != 1:
            raise StateConflict("execution exposure changed concurrently")

    def get_reserved_exposure(self) -> tuple[Decimal, Decimal]:
        connection = self._connect()
        try:
            loss, notional, _, _ = self._read_exposure_locked(connection)
            return loss, notional
        finally:
            connection.close()

    # -- immutable events ---------------------------------------------

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: str | None,
        event_type: str,
        occurred_at: datetime,
        payload: object,
    ) -> EventRecord:
        checked_type = _text(event_type, field="event_type", maximum=128)
        payload_json, content_hash = _canonical_payload(
            payload, maximum=_MAX_DETAILS_BYTES
        )
        last = connection.execute(
            """
            SELECT event_sequence, event_hash
            FROM execution_events ORDER BY event_sequence DESC LIMIT 1
            """
        ).fetchone()
        sequence = 1 if last is None else int(last["event_sequence"]) + 1
        previous_hash = None if last is None else str(last["event_hash"])
        material = {
            "event_sequence": sequence,
            "previous_hash": previous_hash,
            "command_id": command_id,
            "event_type": checked_type,
            "occurred_at": _time_text(occurred_at, field="occurred_at"),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }
        event_hash = _record_hash("event", material)
        connection.execute(
            """
            INSERT INTO execution_events (
                event_sequence, event_hash, previous_hash, command_id,
                event_type, occurred_at, payload_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_hash,
                previous_hash,
                command_id,
                checked_type,
                _time_text(occurred_at, field="occurred_at"),
                payload_json,
                content_hash,
            ),
        )
        return EventRecord(
            sequence,
            event_hash,
            previous_hash,
            command_id,
            checked_type,
            _utc(occurred_at, field="occurred_at"),
            payload_json,
            content_hash,
        )

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> EventRecord:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(row["content_hash"], field="event content_hash")
        _decode_payload(
            payload_json,
            content_hash,
            field="event",
            maximum=_MAX_DETAILS_BYTES,
        )
        record = EventRecord(
            event_sequence=int(row["event_sequence"]),
            event_hash=_stored_hash(row["event_hash"], field="event_hash"),
            previous_hash=(
                None
                if row["previous_hash"] is None
                else _stored_hash(row["previous_hash"], field="previous_hash")
            ),
            command_id=(
                None
                if row["command_id"] is None
                else _stored_text(row["command_id"], field="command_id", maximum=128)
            ),
            event_type=_stored_text(
                row["event_type"], field="event_type", maximum=128
            ),
            occurred_at=_parse_time(row["occurred_at"], field="event occurred_at"),
            payload_json=payload_json,
            content_hash=content_hash,
        )
        expected = _record_hash(
            "event",
            {
                "event_sequence": record.event_sequence,
                "previous_hash": record.previous_hash,
                "command_id": record.command_id,
                "event_type": record.event_type,
                "occurred_at": _time_text(
                    record.occurred_at, field="occurred_at"
                ),
                "content_hash": record.content_hash,
                "payload_json": record.payload_json,
            },
        )
        if record.event_hash != expected:
            raise StorageError("execution event hash does not match")
        return record

    def list_events(self, command_id: str | None = None) -> tuple[EventRecord, ...]:
        connection = self._connect()
        try:
            if command_id is None:
                rows = connection.execute(
                    "SELECT * FROM execution_events ORDER BY event_sequence"
                ).fetchall()
            else:
                checked = _text(command_id, field="command_id", maximum=128)
                rows = connection.execute(
                    """
                    SELECT * FROM execution_events
                    WHERE command_id = ? ORDER BY event_sequence
                    """,
                    (checked,),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def verify_event_chain(self) -> bool:
        previous: str | None = None
        for event in self.list_events():
            if event.previous_hash != previous:
                raise StorageError("execution event chain is discontinuous")
            previous = event.event_hash
        return True

    # -- exact plans, tickets, and approvals --------------------------

    @staticmethod
    def _infrastructure_grant_payload(
        grant: TrustedInfrastructureGrant,
    ) -> dict[str, object]:
        return {
            "schema_version": "trusted_infrastructure_learning_grant.v1",
            "grant_hash": grant.grant_hash,
            "grant_id": grant.grant_id,
            "generation": grant.generation,
            "purpose": "infrastructure_learning",
            "account_id": grant.account_id,
            "environment": grant.environment.value,
            "allowed_instruments": list(grant.allowed_instruments),
            "risk_policy_hash": grant.risk_policy_hash,
            "max_loss": _decimal_text(grant.max_loss, field="max_loss"),
            "max_notional": _decimal_text(
                grant.max_notional, field="max_notional"
            ),
            "max_leverage": _decimal_text(
                grant.max_leverage, field="max_leverage"
            ),
            "profitability_qualified": False,
            "mainnet_authorized": False,
            "issuer_id": grant.issuer_id,
            "audience": grant.audience,
            "issued_at": _time_text(grant.issued_at, field="issued_at"),
            "not_before": _time_text(grant.not_before, field="not_before"),
            "expires_at": _time_text(grant.expires_at, field="expires_at"),
        }

    @classmethod
    def _infrastructure_grant_from_row(
        cls,
        row: Mapping[str, Any],
    ) -> TrustedInfrastructureGrant:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="infrastructure grant content_hash"
        )
        payload = _decode_payload(
            payload_json,
            content_hash,
            field="infrastructure grant",
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted infrastructure grant is not an object")
        try:
            instruments = json.loads(str(row["allowed_instruments_json"]))
            if not isinstance(instruments, list):
                raise TypeError("allowed instruments are not a list")
            grant = TrustedInfrastructureGrant(
                grant_hash=str(row["grant_hash"]),
                grant_id=str(row["grant_id"]),
                generation=int(row["generation"]),
                account_id=str(row["account_id"]),
                environment=Environment(str(row["environment"])),
                allowed_instruments=tuple(instruments),
                risk_policy_hash=str(row["risk_policy_hash"]),
                max_loss=_decimal(row["max_loss"], field="grant max_loss"),
                max_notional=_decimal(
                    row["max_notional"], field="grant max_notional"
                ),
                max_leverage=_decimal(
                    row["max_leverage"], field="grant max_leverage"
                ),
                issuer_id=str(row["issuer_id"]),
                audience=str(row["audience"]),
                issued_at=_parse_time(row["issued_at"], field="grant issued_at"),
                not_before=_parse_time(
                    row["not_before"], field="grant not_before"
                ),
                expires_at=_parse_time(
                    row["expires_at"], field="grant expires_at"
                ),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted infrastructure grant is invalid") from error
        if canonical_json(cls._infrastructure_grant_payload(grant)) != payload_json:
            raise StorageError("persisted infrastructure grant differs from columns")
        registered_at = _parse_time(
            row["registered_at"], field="grant registered_at"
        )
        material = {
            "grant_hash": grant.grant_hash,
            "registered_at": _time_text(registered_at, field="registered_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }
        if _stored_hash(
            row["record_hash"], field="infrastructure grant record_hash"
        ) != _record_hash("infrastructure-grant", material):
            raise StorageError("persisted infrastructure grant hash differs")
        return grant

    def register_infrastructure_grant(
        self,
        grant: TrustedInfrastructureGrant,
        *,
        at: datetime,
    ) -> TrustedInfrastructureGrant:
        if not isinstance(grant, TrustedInfrastructureGrant):
            raise TypeError("grant must be TrustedInfrastructureGrant")
        checked_at = _utc(at, field="at")
        if (
            grant.environment is not self.environment
            or grant.account_id != self.account_id
            or not grant.is_active(checked_at)
        ):
            raise ValidationError("grant is inactive or outside execution-store scope")
        payload_json, content_hash = _canonical_payload(
            self._infrastructure_grant_payload(grant)
        )
        instruments_json = canonical_json(list(grant.allowed_instruments))
        material = {
            "grant_hash": grant.grant_hash,
            "registered_at": _time_text(checked_at, field="registered_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM execution_infrastructure_grants
                WHERE grant_hash = ?
                """,
                (grant.grant_hash,),
            ).fetchone()
            if existing is not None:
                current = self._infrastructure_grant_from_row(existing)
                if current == grant:
                    return current
                raise StateConflict("infrastructure grant hash is already bound")
            connection.execute(
                """
                INSERT INTO execution_infrastructure_grants (
                    grant_hash, grant_id, generation, account_id, environment,
                    allowed_instruments_json, risk_policy_hash, max_loss,
                    max_notional, max_leverage, issuer_id, audience, issued_at,
                    not_before, expires_at, registered_at, payload_json,
                    content_hash, record_hash
                ) VALUES (?, ?, ?, ?, 'testnet', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant.grant_hash,
                    grant.grant_id,
                    grant.generation,
                    grant.account_id,
                    instruments_json,
                    grant.risk_policy_hash,
                    _decimal_text(grant.max_loss, field="max_loss"),
                    _decimal_text(grant.max_notional, field="max_notional"),
                    _decimal_text(grant.max_leverage, field="max_leverage"),
                    grant.issuer_id,
                    grant.audience,
                    _time_text(grant.issued_at, field="issued_at"),
                    _time_text(grant.not_before, field="not_before"),
                    _time_text(grant.expires_at, field="expires_at"),
                    _time_text(checked_at, field="registered_at"),
                    payload_json,
                    content_hash,
                    _record_hash("infrastructure-grant", material),
                ),
            )
            self._append_event_locked(
                connection,
                command_id=None,
                event_type="infrastructure_learning_grant_registered",
                occurred_at=checked_at,
                payload={
                    "grant_hash": grant.grant_hash,
                    "grant_id": grant.grant_id,
                    "generation": grant.generation,
                    "profitability_qualified": False,
                    "mainnet_authorized": False,
                },
            )
            return grant

    def get_infrastructure_grant(
        self,
        grant_hash: str,
    ) -> TrustedInfrastructureGrant:
        checked = _hash(grant_hash, field="grant_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_infrastructure_grants
                WHERE grant_hash = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("infrastructure learning grant is not registered")
        return self._infrastructure_grant_from_row(row)

    def register_ticket(
        self,
        ticket: RiskTicket,
        *,
        infrastructure_grant_hash: str,
        stored_at: datetime,
    ) -> str:
        if not isinstance(ticket, RiskTicket):
            raise TypeError("ticket must be RiskTicket")
        if ticket.status is not RiskTicketStatus.AWAITING_APPROVAL:
            raise ValidationError("only awaiting-approval tickets can be registered")
        if not isinstance(ticket.plan, ProtectedTradePlan):
            raise ValidationError("execution ticket requires a protected plan")
        plan = ticket.plan
        entry = plan.entry
        if (
            entry.environment is not self.environment
            or entry.account_id != self.account_id
        ):
            raise ValidationError("ticket environment/account does not match store")
        if entry.venue != "hyperliquid":
            raise ValidationError("v1 execution store supports Hyperliquid only")
        checked_at = _utc(stored_at, field="stored_at")
        checked_grant_hash = _hash(
            infrastructure_grant_hash,
            field="infrastructure_grant_hash",
        )
        if checked_at < ticket.created_at or checked_at >= ticket.expires_at:
            raise ValidationError("ticket must be stored during its active interval")
        if ticket.assessment_hash != plan.assessment_hash:
            raise ValidationError("ticket and plan assessment hashes differ")
        if ticket.quantity != entry.quantity:
            raise ValidationError("ticket and entry quantities differ")
        if ticket.stressed_loss <= ZERO:
            raise ValidationError("ticket must reserve positive stressed loss")
        if entry.price_bound is None:
            raise ValidationError("protected entry requires price_bound")
        reserved_notional = decimal_multiply(
            entry.quantity,
            entry.price_bound,
            field="ticket reserved notional",
        )
        grant = self.get_infrastructure_grant(checked_grant_hash)
        if (
            not grant.is_active(checked_at)
            or ticket.expires_at > grant.expires_at
            or entry.instrument not in grant.allowed_instruments
            or ticket.policy_hash != grant.risk_policy_hash
            or ticket.stressed_loss > grant.max_loss
            or reserved_notional > grant.max_notional
            or entry.leverage is None
            or entry.leverage > grant.max_leverage
        ):
            raise PolicyViolation(
                "INFRASTRUCTURE_GRANT_SCOPE",
                "ticket exceeds or differs from its testnet learning grant",
            )
        if ticket.stressed_loss > self.max_reserved_loss:
            raise PolicyViolation(
                "EXECUTION_TICKET_LOSS_CAP",
                "ticket stressed loss exceeds immutable store cap",
            )
        if reserved_notional > self.max_reserved_notional:
            raise PolicyViolation(
                "EXECUTION_TICKET_NOTIONAL_CAP",
                "ticket notional exceeds immutable store cap",
            )
        plan_payload_json, plan_content_hash = _canonical_payload(plan.as_dict())
        ticket_payload_json, ticket_content_hash = _canonical_payload(ticket.as_dict())
        plan_record_material = {
            "plan_hash": plan.plan_hash,
            "assessment_hash": plan.assessment_hash,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "venue": entry.venue,
            "instrument": entry.instrument,
            "registered_at": _time_text(checked_at, field="registered_at"),
            "content_hash": plan_content_hash,
            "payload_json": plan_payload_json,
        }
        ticket_record_material = {
            "ticket_hash": ticket.ticket_hash,
            "ticket_id": ticket.ticket_id,
            "plan_hash": plan.plan_hash,
            "infrastructure_grant_hash": checked_grant_hash,
            "state": "awaiting_approval",
            "stressed_loss": _decimal_text(
                ticket.stressed_loss, field="stressed_loss"
            ),
            "reserved_notional": _decimal_text(
                reserved_notional, field="reserved_notional"
            ),
            "created_at": _time_text(ticket.created_at, field="created_at"),
            "expires_at": _time_text(ticket.expires_at, field="expires_at"),
            "registered_at": _time_text(checked_at, field="registered_at"),
            "content_hash": ticket_content_hash,
            "payload_json": ticket_payload_json,
        }
        roles = (
            ("entry", plan.entry),
            ("protective_stop", plan.protective_stop),
            ("take_profit", plan.take_profit),
        )
        try:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                    (ticket.ticket_hash,),
                ).fetchone()
                if existing is not None:
                    self._verify_ticket_row(existing)
                    if (
                        existing["ticket_id"] == ticket.ticket_id
                        and existing["plan_hash"] == plan.plan_hash
                        and existing["infrastructure_grant_hash"]
                        == checked_grant_hash
                        and existing["content_hash"] == ticket_content_hash
                    ):
                        return ticket.ticket_hash
                    raise StateConflict("ticket hash is already bound differently")
                connection.execute(
                    """
                    INSERT INTO execution_plans (
                        plan_hash, assessment_hash, environment, account_id,
                        venue, instrument, registered_at, payload_json,
                        content_hash, record_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.plan_hash,
                        plan.assessment_hash,
                        self.environment.value,
                        self.account_id,
                        entry.venue,
                        entry.instrument,
                        _time_text(checked_at, field="registered_at"),
                        plan_payload_json,
                        plan_content_hash,
                        _record_hash("plan", plan_record_material),
                    ),
                )
                for role, intent in roles:
                    payload_json, content_hash = _canonical_payload(intent)
                    if intent.price_bound is None:
                        raise ValidationError(f"{role} requires price_bound")
                    connection.execute(
                        """
                        INSERT INTO execution_plan_legs (
                            plan_hash, role, cloid, intent_hash, side,
                            reduce_only, quantity, price_bound, payload_json,
                            content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plan.plan_hash,
                            role,
                            intent.client_order_id,
                            semantic_intent_hash(intent),
                            intent.side.value,
                            int(intent.reduce_only),
                            _decimal_text(intent.quantity, field="quantity"),
                            _decimal_text(
                                intent.price_bound, field="price_bound"
                            ),
                            payload_json,
                            content_hash,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO execution_tickets (
                        ticket_hash, ticket_id, plan_hash,
                        infrastructure_grant_hash, state,
                        stressed_loss, reserved_notional, created_at,
                        expires_at, registered_at, payload_json, content_hash,
                        record_hash
                    ) VALUES (?, ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket.ticket_hash,
                        ticket.ticket_id,
                        plan.plan_hash,
                        checked_grant_hash,
                        _decimal_text(ticket.stressed_loss, field="stressed_loss"),
                        _decimal_text(
                            reserved_notional, field="reserved_notional"
                        ),
                        _time_text(ticket.created_at, field="created_at"),
                        _time_text(ticket.expires_at, field="expires_at"),
                        _time_text(checked_at, field="registered_at"),
                        ticket_payload_json,
                        ticket_content_hash,
                        _record_hash("ticket", ticket_record_material),
                    ),
                )
                self._append_event_locked(
                    connection,
                    command_id=None,
                    event_type="risk_ticket_registered",
                    occurred_at=checked_at,
                    payload={
                        "ticket_hash": ticket.ticket_hash,
                        "plan_hash": plan.plan_hash,
                        "infrastructure_grant_hash": checked_grant_hash,
                        "purpose": "infrastructure_learning",
                        "profitability_qualified": False,
                        "environment": self.environment.value,
                        "account_id": self.account_id,
                    },
                )
                return ticket.ticket_hash
        except sqlite3.IntegrityError as error:
            raise StateConflict("plan, ticket, or leg identity already exists") from error

    @staticmethod
    def _ticket_material(row: Mapping[str, Any], *, state: str | None = None) -> dict[str, object]:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(row["content_hash"], field="ticket content_hash")
        _decode_payload(payload_json, content_hash, field="ticket")
        return {
            "ticket_hash": _stored_hash(row["ticket_hash"], field="ticket_hash"),
            "ticket_id": _stored_text(
                row["ticket_id"], field="ticket_id", maximum=128
            ),
            "plan_hash": _stored_hash(row["plan_hash"], field="plan_hash"),
            "infrastructure_grant_hash": _stored_hash(
                row["infrastructure_grant_hash"],
                field="infrastructure_grant_hash",
            ),
            "state": (
                _stored_text(row["state"], field="ticket state", maximum=32)
                if state is None
                else state
            ),
            "stressed_loss": _decimal_text(
                _decimal(row["stressed_loss"], field="stressed_loss"),
                field="stressed_loss",
            ),
            "reserved_notional": _decimal_text(
                _decimal(row["reserved_notional"], field="reserved_notional"),
                field="reserved_notional",
            ),
            "created_at": _time_text(
                _parse_time(row["created_at"], field="ticket created_at"),
                field="created_at",
            ),
            "expires_at": _time_text(
                _parse_time(row["expires_at"], field="ticket expires_at"),
                field="expires_at",
            ),
            "registered_at": _time_text(
                _parse_time(row["registered_at"], field="ticket registered_at"),
                field="registered_at",
            ),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }

    @staticmethod
    def _verify_ticket_row(row: Mapping[str, Any]) -> None:
        material = ExecutionStore._ticket_material(row)
        if _stored_hash(row["record_hash"], field="ticket record_hash") != _record_hash(
            "ticket", material
        ):
            raise StorageError("persisted ticket record hash does not match")

    @staticmethod
    def _verify_plan_row(row: Mapping[str, Any]) -> None:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(row["content_hash"], field="plan content_hash")
        _decode_payload(payload_json, content_hash, field="plan")
        material = {
            "plan_hash": _stored_hash(row["plan_hash"], field="plan_hash"),
            "assessment_hash": _stored_hash(
                row["assessment_hash"], field="assessment_hash"
            ),
            "environment": str(row["environment"]),
            "account_id": str(row["account_id"]),
            "venue": str(row["venue"]),
            "instrument": str(row["instrument"]),
            "registered_at": str(row["registered_at"]),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }
        if _stored_hash(row["record_hash"], field="plan record_hash") != _record_hash(
            "plan", material
        ):
            raise StorageError("persisted plan record hash does not match")

    @staticmethod
    def _verify_plan_leg_row(row: Mapping[str, Any]) -> None:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="plan leg content_hash"
        )
        payload = _decode_payload(payload_json, content_hash, field="plan leg")
        if not isinstance(payload, dict):
            raise StorageError("persisted plan leg payload is not an object")
        try:
            intent = SemanticIntent.from_mapping(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError("persisted plan leg intent is invalid") from error
        comparisons = {
            "cloid": intent.client_order_id,
            "intent_hash": semantic_intent_hash(intent),
            "side": intent.side.value,
            "reduce_only": int(intent.reduce_only),
            "quantity": _decimal_text(intent.quantity, field="quantity"),
            "price_bound": (
                None
                if intent.price_bound is None
                else _decimal_text(intent.price_bound, field="price_bound")
            ),
        }
        if comparisons["price_bound"] is None:
            raise StorageError("persisted plan leg has no price bound")
        for column, expected in comparisons.items():
            if row[column] != expected:
                raise StorageError(
                    f"persisted plan leg {column} differs from immutable payload"
                )

    def get_ticket_payload(self, ticket_hash: str) -> Mapping[str, Any]:
        checked = _hash(ticket_hash, field="ticket_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_tickets WHERE ticket_hash = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("execution ticket is not registered")
        self._verify_ticket_row(row)
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise StorageError("persisted ticket payload is not an object")
        return payload

    def get_plan_payload(self, plan_hash: str) -> Mapping[str, Any]:
        checked = _hash(plan_hash, field="plan_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_plans WHERE plan_hash = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("execution plan is not registered")
        self._verify_plan_row(row)
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise StorageError("persisted plan payload is not an object")
        return payload

    def register_approval(self, approval: TrustedApproval) -> TrustedApproval:
        if not isinstance(approval, TrustedApproval):
            raise TypeError("approval must be TrustedApproval")
        if (
            approval.environment is not self.environment
            or approval.account_id != self.account_id
        ):
            raise ValidationError("approval environment/account does not match store")
        material = {
            "approval_id": approval.approval_id,
            "ticket_hash": approval.ticket_hash,
            "token_hash": approval.token_hash,
            "approver_id": approval.approver_id,
            "audience": approval.audience,
            "environment": approval.environment.value,
            "account_id": approval.account_id,
            "issued_at": _time_text(approval.issued_at, field="issued_at"),
            "expires_at": _time_text(approval.expires_at, field="expires_at"),
            "state": "issued",
            "command_id": None,
            "updated_at": _time_text(approval.issued_at, field="updated_at"),
        }
        try:
            with self._transaction() as connection:
                ticket = connection.execute(
                    "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                    (approval.ticket_hash,),
                ).fetchone()
                if ticket is None:
                    raise RecordNotFound("approval ticket is not registered")
                self._verify_ticket_row(ticket)
                if ticket["state"] != "awaiting_approval":
                    raise StateConflict("ticket is not awaiting approval")
                if approval.issued_at < _parse_time(
                    ticket["created_at"], field="ticket created_at"
                ):
                    raise ValidationError("approval cannot predate its ticket")
                if approval.expires_at > _parse_time(
                    ticket["expires_at"], field="ticket expires_at"
                ):
                    raise ValidationError("approval cannot outlive its ticket")
                existing = connection.execute(
                    "SELECT * FROM execution_approvals WHERE approval_id = ?",
                    (approval.approval_id,),
                ).fetchone()
                record_hash = _record_hash("approval", material)
                if existing is not None:
                    if (
                        existing["ticket_hash"] == approval.ticket_hash
                        and existing["token_hash"] == approval.token_hash
                        and existing["record_hash"] == record_hash
                    ):
                        return approval
                    raise StateConflict("approval ID is already bound differently")
                connection.execute(
                    """
                    INSERT INTO execution_approvals (
                        approval_id, ticket_hash, token_hash, approver_id,
                        audience, environment, account_id, issued_at,
                        expires_at, state, command_id, updated_at, record_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', NULL, ?, ?)
                    """,
                    (
                        approval.approval_id,
                        approval.ticket_hash,
                        approval.token_hash,
                        approval.approver_id,
                        approval.audience,
                        approval.environment.value,
                        approval.account_id,
                        _time_text(approval.issued_at, field="issued_at"),
                        _time_text(approval.expires_at, field="expires_at"),
                        _time_text(approval.issued_at, field="updated_at"),
                        record_hash,
                    ),
                )
                self._append_event_locked(
                    connection,
                    command_id=None,
                    event_type="trusted_approval_registered",
                    occurred_at=approval.issued_at,
                    payload={
                        "approval_id": approval.approval_id,
                        "ticket_hash": approval.ticket_hash,
                        "approver_id": approval.approver_id,
                        "audience": approval.audience,
                    },
                )
                return approval
        except sqlite3.IntegrityError as error:
            raise StateConflict("approval token or ticket is already authorized") from error

    @staticmethod
    def _approval_material(row: Mapping[str, Any]) -> dict[str, object]:
        return {
            "approval_id": str(row["approval_id"]),
            "ticket_hash": str(row["ticket_hash"]),
            "token_hash": str(row["token_hash"]),
            "approver_id": str(row["approver_id"]),
            "audience": str(row["audience"]),
            "environment": str(row["environment"]),
            "account_id": str(row["account_id"]),
            "issued_at": str(row["issued_at"]),
            "expires_at": str(row["expires_at"]),
            "state": str(row["state"]),
            "command_id": row["command_id"],
            "updated_at": str(row["updated_at"]),
        }

    def _verify_approval_row(self, row: Mapping[str, Any]) -> None:
        material = self._approval_material(row)
        if _stored_hash(
            row["record_hash"], field="approval record_hash"
        ) != _record_hash("approval", material):
            raise StorageError("persisted approval record hash does not match")

    def approval_state(self, approval_id: str) -> str:
        checked = _text(approval_id, field="approval_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_approvals WHERE approval_id = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("approval is not registered")
        self._verify_approval_row(row)
        return str(row["state"])

    def get_approval(self, approval_id: str) -> TrustedApproval:
        """Return one verified opaque approval without exposing its MAC.

        This read model exists so an attended local control plane can recover
        safely from a crash between durable approval registration and atomic
        command admission.  The returned token hash is already the verified,
        non-secret capability committed by the approval authority; no raw key
        or approval MAC is stored in this database.
        """

        checked = _text(approval_id, field="approval_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_approvals WHERE approval_id = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("approval is not registered")
        self._verify_approval_row(row)
        try:
            environment = Environment(str(row["environment"]))
            return TrustedApproval(
                approval_id=str(row["approval_id"]),
                ticket_hash=str(row["ticket_hash"]),
                token_hash=str(row["token_hash"]),
                approver_id=str(row["approver_id"]),
                audience=str(row["audience"]),
                environment=environment,
                account_id=str(row["account_id"]),
                issued_at=_parse_time(
                    row["issued_at"], field="approval issued_at"
                ),
                expires_at=_parse_time(
                    row["expires_at"], field="approval expires_at"
                ),
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise StorageError("persisted approval is invalid") from error

    def revoke_approval(self, approval_id: str, *, at: datetime) -> None:
        checked = _text(approval_id, field="approval_id", maximum=128)
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_approvals WHERE approval_id = ?", (checked,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("approval is not registered")
            self._verify_approval_row(row)
            if row["state"] != "issued":
                raise StateConflict("only an unused approval can be revoked")
            if checked_at < _parse_time(row["issued_at"], field="approval issued_at"):
                raise ValidationError("approval revocation cannot predate issuance")
            material = self._approval_material(row)
            material["state"] = "revoked"
            material["updated_at"] = _time_text(checked_at, field="updated_at")
            connection.execute(
                """
                UPDATE execution_approvals SET
                    state = 'revoked', updated_at = ?, record_hash = ?
                WHERE approval_id = ? AND state = 'issued'
                """,
                (
                    _time_text(checked_at, field="updated_at"),
                    _record_hash("approval", material),
                    checked,
                ),
            )
            self._append_event_locked(
                connection,
                command_id=None,
                event_type="trusted_approval_revoked",
                occurred_at=checked_at,
                payload={"approval_id": checked, "ticket_hash": row["ticket_hash"]},
            )

    def admit(
        self,
        *,
        command_id: str,
        approval_id: str,
        token_hash: str,
        audience: str,
        at: datetime,
    ) -> CommandRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_approval = _text(approval_id, field="approval_id", maximum=128)
        checked_token = _hash(token_hash, field="token_hash")
        checked_audience = _text(audience, field="audience", maximum=256)
        checked_at = _utc(at, field="at")
        try:
            with self._transaction() as connection:
                approval = connection.execute(
                    "SELECT * FROM execution_approvals WHERE approval_id = ?",
                    (checked_approval,),
                ).fetchone()
                if approval is None:
                    raise AdmissionDenied("APPROVAL_NOT_FOUND", "approval is not registered")
                self._verify_approval_row(approval)
                if approval["state"] != "issued":
                    raise AdmissionDenied("APPROVAL_ALREADY_USED", "approval is not issued")
                if approval["token_hash"] != checked_token:
                    raise AdmissionDenied("APPROVAL_TOKEN_MISMATCH", "opaque token hash differs")
                if approval["audience"] != checked_audience:
                    raise AdmissionDenied("APPROVAL_AUDIENCE_MISMATCH", "audience differs")
                if (
                    approval["environment"] != self.environment.value
                    or approval["account_id"] != self.account_id
                ):
                    raise AdmissionDenied("APPROVAL_SCOPE_MISMATCH", "store scope differs")
                if not (
                    _parse_time(approval["issued_at"], field="approval issued_at")
                    <= checked_at
                    < _parse_time(approval["expires_at"], field="approval expires_at")
                ):
                    raise AdmissionDenied("APPROVAL_INACTIVE", "approval is expired or not active")
                ticket = connection.execute(
                    "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                    (approval["ticket_hash"],),
                ).fetchone()
                if ticket is None:
                    raise StorageError("approval references missing ticket")
                self._verify_ticket_row(ticket)
                if ticket["state"] != "awaiting_approval":
                    raise AdmissionDenied("TICKET_ALREADY_USED", "ticket is not available")
                if checked_at >= _parse_time(ticket["expires_at"], field="ticket expires_at"):
                    raise AdmissionDenied("TICKET_EXPIRED", "risk ticket has expired")
                plan_row = connection.execute(
                    "SELECT * FROM execution_plans WHERE plan_hash = ?",
                    (ticket["plan_hash"],),
                ).fetchone()
                if plan_row is None:
                    raise StorageError("ticket references missing protected plan")
                self._verify_plan_row(plan_row)
                active_critical = connection.execute(
                    """
                    SELECT incident_id FROM execution_incidents
                    WHERE severity = 'critical' AND state != 'closed'
                    LIMIT 1
                    """
                ).fetchone()
                if active_critical is not None:
                    raise AdmissionDenied(
                        "ACCOUNT_CRITICAL_INCIDENT_ACTIVE",
                        "critical account incident blocks new risk",
                    )
                if connection.execute(
                    """
                    SELECT 1 FROM execution_recovery_commands
                    WHERE state != 'terminal' LIMIT 1
                    """
                ).fetchone() is not None:
                    raise AdmissionDenied(
                        "ACCOUNT_RECOVERY_ACTIVE",
                        "active recovery command blocks new risk",
                    )
                active_command = connection.execute(
                    """
                    SELECT command_id FROM execution_commands
                    WHERE state != 'terminal'
                    LIMIT 1
                    """
                ).fetchone()
                if active_command is not None:
                    raise AdmissionDenied(
                        "ACCOUNT_COMMAND_ALREADY_ACTIVE",
                        "flat-account v1 permits one nonterminal risk command",
                    )
                reserved_loss = _decimal(
                    ticket["stressed_loss"], field="stressed_loss", nonnegative=True
                )
                reserved_notional = _decimal(
                    ticket["reserved_notional"],
                    field="reserved_notional",
                    nonnegative=True,
                )
                current_loss, current_notional, exposure_revision, _ = (
                    self._read_exposure_locked(connection)
                )
                next_loss = decimal_add(
                    current_loss, reserved_loss, field="aggregate reserved loss"
                )
                next_notional = decimal_add(
                    current_notional,
                    reserved_notional,
                    field="aggregate reserved notional",
                )
                if next_loss > self.max_reserved_loss:
                    raise PolicyViolation(
                        "EXECUTION_ACCOUNT_LOSS_CAP",
                        "aggregate reservation exceeds immutable loss cap",
                    )
                if next_notional > self.max_reserved_notional:
                    raise PolicyViolation(
                        "EXECUTION_ACCOUNT_NOTIONAL_CAP",
                        "aggregate reservation exceeds immutable notional cap",
                    )

                approval_material = self._approval_material(approval)
                approval_material.update(
                    {
                        "state": "consumed",
                        "command_id": checked_command,
                        "updated_at": _time_text(checked_at, field="updated_at"),
                    }
                )
                changed = connection.execute(
                    """
                    UPDATE execution_approvals SET
                        state = 'consumed', command_id = ?, updated_at = ?,
                        record_hash = ?
                    WHERE approval_id = ? AND state = 'issued'
                    """,
                    (
                        checked_command,
                        _time_text(checked_at, field="updated_at"),
                        _record_hash("approval", approval_material),
                        checked_approval,
                    ),
                )
                if changed.rowcount != 1:
                    raise StateConflict("approval was consumed concurrently")
                consumed_ticket_material = self._ticket_material(
                    ticket, state="consumed"
                )
                connection.execute(
                    """
                    UPDATE execution_tickets SET state = 'consumed', record_hash = ?
                    WHERE ticket_hash = ? AND state = 'awaiting_approval'
                    """,
                    (
                        _record_hash("ticket", consumed_ticket_material),
                        ticket["ticket_hash"],
                    ),
                )
                command_material = self._command_material_values(
                    checked_command,
                    str(ticket["ticket_hash"]),
                    str(ticket["plan_hash"]),
                    checked_approval,
                    "queued",
                    reserved_loss,
                    reserved_notional,
                    checked_at,
                    checked_at,
                    None,
                    1,
                )
                connection.execute(
                    """
                    INSERT INTO execution_commands (
                        command_id, ticket_hash, plan_hash, approval_id, state,
                        reserved_loss, reserved_notional, created_at, updated_at,
                        terminal_at, revision, record_hash
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, NULL, 1, ?)
                    """,
                    (
                        checked_command,
                        ticket["ticket_hash"],
                        ticket["plan_hash"],
                        checked_approval,
                        _decimal_text(reserved_loss, field="reserved_loss"),
                        _decimal_text(
                            reserved_notional, field="reserved_notional"
                        ),
                        _time_text(checked_at, field="created_at"),
                        _time_text(checked_at, field="updated_at"),
                        _record_hash("command", command_material),
                    ),
                )
                plan_legs = connection.execute(
                    """
                    SELECT * FROM execution_plan_legs
                    WHERE plan_hash = ? ORDER BY role
                    """,
                    (ticket["plan_hash"],),
                ).fetchall()
                if {str(row["role"]) for row in plan_legs} != set(_ROLES):
                    raise StorageError("protected plan does not contain exactly three legs")
                for leg in plan_legs:
                    self._verify_plan_leg_row(leg)
                    leg_material = self._leg_material_values(
                        checked_command,
                        str(leg["role"]),
                        str(leg["cloid"]),
                        str(leg["intent_hash"]),
                        str(leg["side"]),
                        bool(leg["reduce_only"]),
                        _decimal(leg["quantity"], field="quantity"),
                        ZERO,
                        None,
                        "queued",
                        checked_at,
                    )
                    connection.execute(
                        """
                        INSERT INTO execution_command_legs (
                            command_id, role, cloid, intent_hash, side,
                            reduce_only, requested_quantity, cumulative_filled,
                            venue_oid, status, updated_at, record_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '0', NULL, 'queued', ?, ?)
                        """,
                        (
                            checked_command,
                            leg["role"],
                            leg["cloid"],
                            leg["intent_hash"],
                            leg["side"],
                            leg["reduce_only"],
                            leg["quantity"],
                            _time_text(checked_at, field="updated_at"),
                            _record_hash("leg", leg_material),
                        ),
                    )
                outbox_material = self._outbox_material_values(
                    checked_command,
                    "queued",
                    None,
                    0,
                    None,
                    None,
                    None,
                    0,
                    checked_at,
                    checked_at,
                )
                connection.execute(
                    """
                    INSERT INTO execution_outbox (
                        command_id, state, worker_id, fencing_token, claimed_at,
                        lease_expires_at, current_attempt_id, attempt_count,
                        created_at, updated_at, record_hash
                    ) VALUES (?, 'queued', NULL, 0, NULL, NULL, NULL, 0, ?, ?, ?)
                    """,
                    (
                        checked_command,
                        _time_text(checked_at, field="created_at"),
                        _time_text(checked_at, field="updated_at"),
                        _record_hash("outbox", outbox_material),
                    ),
                )
                self._write_exposure_locked(
                    connection,
                    loss=next_loss,
                    notional=next_notional,
                    previous_revision=exposure_revision,
                    at=checked_at,
                )
                self._append_event_locked(
                    connection,
                    command_id=checked_command,
                    event_type="command_admitted",
                    occurred_at=checked_at,
                    payload={
                        "ticket_hash": ticket["ticket_hash"],
                        "plan_hash": ticket["plan_hash"],
                        "approval_id": checked_approval,
                        "reserved_loss": _decimal_text(
                            reserved_loss, field="reserved_loss"
                        ),
                        "reserved_notional": _decimal_text(
                            reserved_notional, field="reserved_notional"
                        ),
                        "leg_count": 3,
                    },
                )
                return self._command_from_row(
                    connection.execute(
                        "SELECT * FROM execution_commands WHERE command_id = ?",
                        (checked_command,),
                    ).fetchone()
                )
        except sqlite3.IntegrityError as error:
            raise StateConflict("command, plan, or leg identity is already consumed") from error

    # -- mutable record integrity helpers -----------------------------

    @staticmethod
    def _command_material_values(
        command_id: str,
        ticket_hash: str,
        plan_hash: str,
        approval_id: str,
        state: str,
        reserved_loss: Decimal,
        reserved_notional: Decimal,
        created_at: datetime,
        updated_at: datetime,
        terminal_at: datetime | None,
        revision: int,
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "ticket_hash": ticket_hash,
            "plan_hash": plan_hash,
            "approval_id": approval_id,
            "state": state,
            "reserved_loss": _decimal_text(reserved_loss, field="reserved_loss"),
            "reserved_notional": _decimal_text(
                reserved_notional, field="reserved_notional"
            ),
            "created_at": _time_text(created_at, field="created_at"),
            "updated_at": _time_text(updated_at, field="updated_at"),
            "terminal_at": (
                None
                if terminal_at is None
                else _time_text(terminal_at, field="terminal_at")
            ),
            "revision": revision,
        }

    @classmethod
    def _command_from_row(cls, row: Mapping[str, Any] | None) -> CommandRecord:
        if row is None:
            raise StorageError("command row is missing")
        state = _stored_text(row["state"], field="command state", maximum=32)
        if state not in _COMMAND_STATES:
            raise StorageError("persisted command state is unsupported")
        record = CommandRecord(
            command_id=_stored_text(
                row["command_id"], field="command_id", maximum=128
            ),
            ticket_hash=_stored_hash(row["ticket_hash"], field="ticket_hash"),
            plan_hash=_stored_hash(row["plan_hash"], field="plan_hash"),
            approval_id=_stored_text(
                row["approval_id"], field="approval_id", maximum=128
            ),
            state=state,
            reserved_loss=_decimal(
                row["reserved_loss"], field="reserved_loss", nonnegative=True
            ),
            reserved_notional=_decimal(
                row["reserved_notional"],
                field="reserved_notional",
                nonnegative=True,
            ),
            created_at=_parse_time(row["created_at"], field="command created_at"),
            updated_at=_parse_time(row["updated_at"], field="command updated_at"),
            terminal_at=_optional_time(
                row["terminal_at"], field="command terminal_at"
            ),
            revision=int(row["revision"]),
        )
        expected = _record_hash(
            "command",
            cls._command_material_values(
                record.command_id,
                record.ticket_hash,
                record.plan_hash,
                record.approval_id,
                record.state,
                record.reserved_loss,
                record.reserved_notional,
                record.created_at,
                record.updated_at,
                record.terminal_at,
                record.revision,
            ),
        )
        if _stored_hash(row["record_hash"], field="command record_hash") != expected:
            raise StorageError("persisted command record hash does not match")
        return record

    @staticmethod
    def _outbox_material_values(
        command_id: str,
        state: str,
        worker_id: str | None,
        fencing_token: int,
        claimed_at: datetime | None,
        lease_expires_at: datetime | None,
        current_attempt_id: str | None,
        attempt_count: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "state": state,
            "worker_id": worker_id,
            "fencing_token": fencing_token,
            "claimed_at": (
                None
                if claimed_at is None
                else _time_text(claimed_at, field="claimed_at")
            ),
            "lease_expires_at": (
                None
                if lease_expires_at is None
                else _time_text(lease_expires_at, field="lease_expires_at")
            ),
            "current_attempt_id": current_attempt_id,
            "attempt_count": attempt_count,
            "created_at": _time_text(created_at, field="created_at"),
            "updated_at": _time_text(updated_at, field="updated_at"),
        }

    @classmethod
    def _outbox_from_row(cls, row: Mapping[str, Any] | None) -> OutboxRecord:
        if row is None:
            raise StorageError("outbox row is missing")
        state = _stored_text(row["state"], field="outbox state", maximum=32)
        if state not in _OUTBOX_STATES:
            raise StorageError("persisted outbox state is unsupported")
        record = OutboxRecord(
            command_id=_stored_text(
                row["command_id"], field="command_id", maximum=128
            ),
            state=state,
            worker_id=(
                None
                if row["worker_id"] is None
                else _stored_text(row["worker_id"], field="worker_id", maximum=128)
            ),
            fencing_token=int(row["fencing_token"]),
            claimed_at=_optional_time(row["claimed_at"], field="claimed_at"),
            lease_expires_at=_optional_time(
                row["lease_expires_at"], field="lease_expires_at"
            ),
            current_attempt_id=(
                None
                if row["current_attempt_id"] is None
                else _stored_text(
                    row["current_attempt_id"],
                    field="current_attempt_id",
                    maximum=128,
                )
            ),
            attempt_count=int(row["attempt_count"]),
            created_at=_parse_time(row["created_at"], field="outbox created_at"),
            updated_at=_parse_time(row["updated_at"], field="outbox updated_at"),
        )
        expected = _record_hash(
            "outbox",
            cls._outbox_material_values(
                record.command_id,
                record.state,
                record.worker_id,
                record.fencing_token,
                record.claimed_at,
                record.lease_expires_at,
                record.current_attempt_id,
                record.attempt_count,
                record.created_at,
                record.updated_at,
            ),
        )
        if _stored_hash(row["record_hash"], field="outbox record_hash") != expected:
            raise StorageError("persisted outbox record hash does not match")
        return record

    @staticmethod
    def _leg_material_values(
        command_id: str,
        role: str,
        cloid: str,
        intent_hash: str,
        side: str,
        reduce_only: bool,
        requested_quantity: Decimal,
        cumulative_filled: Decimal,
        venue_oid: int | None,
        status: str,
        updated_at: datetime,
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "role": role,
            "cloid": cloid,
            "intent_hash": intent_hash,
            "side": side,
            "reduce_only": reduce_only,
            "requested_quantity": _decimal_text(
                requested_quantity, field="requested_quantity"
            ),
            "cumulative_filled": _decimal_text(
                cumulative_filled, field="cumulative_filled"
            ),
            "venue_oid": venue_oid,
            "status": status,
            "updated_at": _time_text(updated_at, field="updated_at"),
        }

    @classmethod
    def _leg_from_row(cls, row: Mapping[str, Any]) -> LegRecord:
        role = _stored_text(row["role"], field="role", maximum=32)
        status = _stored_text(row["status"], field="leg status", maximum=32)
        if role not in _ROLES or status not in _LEG_STATES:
            raise StorageError("persisted execution leg is unsupported")
        record = LegRecord(
            command_id=_stored_text(
                row["command_id"], field="command_id", maximum=128
            ),
            role=role,
            cloid=_stored_text(row["cloid"], field="cloid", maximum=128),
            intent_hash=_stored_hash(row["intent_hash"], field="intent_hash"),
            side=_stored_text(row["side"], field="side", maximum=8),
            reduce_only=bool(row["reduce_only"]),
            requested_quantity=_decimal(
                row["requested_quantity"], field="requested_quantity"
            ),
            cumulative_filled=_decimal(
                row["cumulative_filled"],
                field="cumulative_filled",
                nonnegative=True,
            ),
            venue_oid=(None if row["venue_oid"] is None else int(row["venue_oid"])),
            status=status,
            updated_at=_parse_time(row["updated_at"], field="leg updated_at"),
        )
        if record.cumulative_filled > record.requested_quantity:
            raise StorageError("persisted leg fill exceeds requested quantity")
        expected = _record_hash(
            "leg",
            cls._leg_material_values(
                record.command_id,
                record.role,
                record.cloid,
                record.intent_hash,
                record.side,
                record.reduce_only,
                record.requested_quantity,
                record.cumulative_filled,
                record.venue_oid,
                record.status,
                record.updated_at,
            ),
        )
        if _stored_hash(row["record_hash"], field="leg record_hash") != expected:
            raise StorageError("persisted leg record hash does not match")
        return record

    def get_command(self, command_id: str) -> CommandRecord:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"execution command not found: {checked}")
        return self._command_from_row(row)

    def list_commands(self, *, active_only: bool = False) -> tuple[CommandRecord, ...]:
        if type(active_only) is not bool:
            raise TypeError("active_only must be bool")
        connection = self._connect()
        try:
            rows = connection.execute(
                (
                    "SELECT * FROM execution_commands WHERE state != 'terminal' "
                    "ORDER BY created_at, command_id"
                    if active_only
                    else "SELECT * FROM execution_commands ORDER BY created_at, command_id"
                )
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._command_from_row(row) for row in rows)

    def get_outbox(self, command_id: str) -> OutboxRecord:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_outbox WHERE command_id = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"execution outbox not found: {checked}")
        return self._outbox_from_row(row)

    def list_outboxes(self, *, active_only: bool = False) -> tuple[OutboxRecord, ...]:
        if type(active_only) is not bool:
            raise TypeError("active_only must be bool")
        connection = self._connect()
        try:
            rows = connection.execute(
                (
                    "SELECT * FROM execution_outbox WHERE state != 'terminal' "
                    "ORDER BY created_at, command_id"
                    if active_only
                    else "SELECT * FROM execution_outbox ORDER BY created_at, command_id"
                )
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._outbox_from_row(row) for row in rows)

    def get_legs(self, command_id: str) -> tuple[LegRecord, ...]:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM execution_command_legs
                WHERE command_id = ? ORDER BY
                    CASE role
                        WHEN 'entry' THEN 0
                        WHEN 'protective_stop' THEN 1
                        ELSE 2
                    END
                """,
                (checked,),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise RecordNotFound(f"execution legs not found: {checked}")
        result = tuple(self._leg_from_row(row) for row in rows)
        if tuple(leg.role for leg in result) != _ROLES:
            raise StorageError("command does not contain exactly three ordered legs")
        return result

    def void_unsent_command(
        self,
        command_id: str,
        *,
        reason: str,
        at: datetime,
        worker_id: str | None = None,
        fencing_token: int | None = None,
        prepared_attempt_id: str | None = None,
    ) -> CommandRecord:
        """Permanently void a proven-unsent command and consume its authority.

        This path is available while no attempt exists, or for one exact
        ``prepared`` attempt before submission authority was consumed. The
        ticket and approval are never revived; another send requires a new
        ticket and a new trusted approval.
        """

        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_reason = _text(reason, field="reason", maximum=256)
        checked_at = _utc(at, field="at")
        checked_prepared_attempt = (
            None
            if prepared_attempt_id is None
            else _text(
                prepared_attempt_id,
                field="prepared_attempt_id",
                maximum=128,
            )
        )
        with self._transaction() as connection:
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if command_row is None:
                raise RecordNotFound("execution command is not registered")
            command = self._command_from_row(command_row)
            if command.state not in {"queued", "claimed"}:
                raise StateConflict("only an unsent queued/claimed command may be voided")
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            prepared_attempt = None
            if attempt_row is not None:
                prepared_attempt = self._attempt_from_row(attempt_row)
                if (
                    checked_prepared_attempt is None
                    or prepared_attempt.attempt_id != checked_prepared_attempt
                    or prepared_attempt.state != "prepared"
                    or prepared_attempt.transport_evidence_hash is not None
                    or connection.execute(
                        """
                        SELECT 1 FROM execution_submission_authorities
                        WHERE command_id = ? OR attempt_id = ?
                        """,
                        (checked_command, prepared_attempt.attempt_id),
                    ).fetchone()
                    is not None
                ):
                    raise StateConflict("command has an attempt and must be reconciled")
            elif checked_prepared_attempt is not None:
                raise StateConflict("prepared attempt to void is missing")
            outbox_row = connection.execute(
                "SELECT * FROM execution_outbox WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if outbox_row is None:
                raise StorageError("execution command has no outbox")
            outbox = self._outbox_from_row(outbox_row)
            if command.state == "claimed":
                if worker_id is None or fencing_token is None:
                    raise StateConflict(
                        "claimed command void requires its exact active worker fence"
                    )
                self._require_claim_locked(
                    connection,
                    command_id=checked_command,
                    worker_id=_text(worker_id, field="worker_id", maximum=128),
                    fencing_token=_positive_int(
                        fencing_token, field="fencing_token"
                    ),
                    at=checked_at,
                    allowed_states=frozenset({"claimed"}),
                )
            elif worker_id is not None or fencing_token is not None:
                raise StateConflict("queued command void cannot consume a worker fence")
            if prepared_attempt is None:
                if outbox.current_attempt_id is not None or outbox.attempt_count != 0:
                    raise StateConflict("outbox records an attempt and cannot be voided")
            elif (
                outbox.current_attempt_id != prepared_attempt.attempt_id
                or outbox.attempt_count != 1
            ):
                raise StateConflict("outbox prepared attempt binding differs")
            leg_rows = connection.execute(
                "SELECT * FROM execution_command_legs WHERE command_id = ?",
                (checked_command,),
            ).fetchall()
            if len(leg_rows) != 3:
                raise StorageError("unsent command is missing protected legs")
            for row in leg_rows:
                leg = self._leg_from_row(row)
                if leg.cumulative_filled != ZERO or leg.venue_oid is not None:
                    raise StateConflict("leg has venue evidence and cannot be voided")
                self._update_leg_locked(
                    connection,
                    leg,
                    status="expired",
                    cumulative_filled=ZERO,
                    venue_oid=None,
                    at=checked_at,
                )
            current_loss, current_notional, exposure_revision, _ = (
                self._read_exposure_locked(connection)
            )
            self._write_exposure_locked(
                connection,
                loss=decimal_subtract(
                    current_loss,
                    command.reserved_loss,
                    field="voided reserved loss",
                ),
                notional=decimal_subtract(
                    current_notional,
                    command.reserved_notional,
                    field="voided reserved notional",
                ),
                previous_revision=exposure_revision,
                at=checked_at,
            )
            terminal_command = self._set_command_state_locked(
                connection,
                command_row,
                state="terminal",
                at=checked_at,
                terminal=True,
            )
            self._set_outbox_locked(
                connection,
                outbox_row,
                state="terminal",
                at=checked_at,
                worker_id=None,
                fencing_token=outbox.fencing_token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=(
                    None
                    if prepared_attempt is None
                    else prepared_attempt.attempt_id
                ),
                attempt_count=0 if prepared_attempt is None else 1,
            )
            ticket = connection.execute(
                "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                (command.ticket_hash,),
            ).fetchone()
            if ticket is None:
                raise StorageError("voided command ticket is missing")
            terminal_ticket = self._ticket_material(ticket, state="terminal")
            connection.execute(
                """
                UPDATE execution_tickets SET state = 'terminal', record_hash = ?
                WHERE ticket_hash = ? AND state = 'consumed'
                """,
                (_record_hash("ticket", terminal_ticket), command.ticket_hash),
            )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="unsent_command_voided",
                occurred_at=checked_at,
                payload={
                    "reason": checked_reason,
                    "authority_reusable": False,
                    "attempt_existed": prepared_attempt is not None,
                    "submission_authority_consumed": False,
                    "venue_write_attempted": False,
                },
            )
            return terminal_command

    # -- fresh send-time preflight ------------------------------------

    @staticmethod
    def _preflight_material(
        preflight: DispatchPreflight,
        *,
        registered_at: datetime,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        material = {
            "preflight_hash": preflight.preflight_hash,
            "command_id": preflight.command_id,
            "ticket_hash": preflight.ticket_hash,
            "plan_hash": preflight.plan_hash,
            "environment": preflight.environment.value,
            "account_id": preflight.account_id,
            "account_snapshot_hash": preflight.account_snapshot_hash,
            "metadata_hash": preflight.metadata_hash,
            "market_snapshot_hash": preflight.market_snapshot_hash,
            "risk_policy_hash": preflight.risk_policy_hash,
            "observed_at": _time_text(
                preflight.observed_at, field="observed_at"
            ),
            "expires_at": _time_text(preflight.expires_at, field="expires_at"),
            "passed": preflight.passed,
            "registered_at": _time_text(registered_at, field="registered_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }
        if preflight.account_server_time_ms is not None:
            material["account_server_time_ms"] = preflight.account_server_time_ms
        return material

    @classmethod
    def _preflight_from_row(cls, row: Mapping[str, Any]) -> DispatchPreflight:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="preflight content_hash"
        )
        payload = _decode_payload(payload_json, content_hash, field="preflight")
        if not isinstance(payload, dict):
            raise StorageError("persisted preflight payload is not an object")
        try:
            preflight = DispatchPreflight(
                command_id=str(row["command_id"]),
                ticket_hash=str(row["ticket_hash"]),
                plan_hash=str(row["plan_hash"]),
                environment=Environment(str(row["environment"])),
                account_id=str(row["account_id"]),
                account_snapshot_hash=str(row["account_snapshot_hash"]),
                metadata_hash=str(row["metadata_hash"]),
                market_snapshot_hash=str(row["market_snapshot_hash"]),
                risk_policy_hash=str(row["risk_policy_hash"]),
                observed_at=_parse_time(
                    row["observed_at"], field="preflight observed_at"
                ),
                expires_at=_parse_time(
                    row["expires_at"], field="preflight expires_at"
                ),
                passed=bool(row["passed"]),
                account_server_time_ms=(
                    None
                    if row["account_server_time_ms"] is None
                    else int(row["account_server_time_ms"])
                ),
                preflight_hash=str(row["preflight_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted preflight is invalid") from error
        if canonical_json(preflight.as_dict()) != payload_json:
            raise StorageError("persisted preflight payload differs from columns")
        registered_at = _parse_time(
            row["registered_at"], field="preflight registered_at"
        )
        expected_record_hash = _record_hash(
            "preflight",
            cls._preflight_material(
                preflight,
                registered_at=registered_at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        if _stored_hash(
            row["record_hash"], field="preflight record_hash"
        ) != expected_record_hash:
            raise StorageError("persisted preflight record hash does not match")
        return preflight

    def register_preflight(
        self,
        preflight: DispatchPreflight,
        *,
        at: datetime,
    ) -> DispatchPreflight:
        if not isinstance(preflight, DispatchPreflight):
            raise TypeError("preflight must be DispatchPreflight")
        checked_at = _utc(at, field="at")
        if not preflight.passed:
            raise AdmissionDenied(
                "DISPATCH_PREFLIGHT_FAILED",
                "failed preflight cannot authorize attempt preparation",
            )
        if preflight.account_server_time_ms is None:
            raise AdmissionDenied(
                "DISPATCH_PREFLIGHT_SERVER_WATERMARK_MISSING",
                "preflight lacks the exact venue-server account watermark",
            )
        if (
            preflight.environment is not self.environment
            or preflight.account_id != self.account_id
        ):
            raise AdmissionDenied(
                "DISPATCH_PREFLIGHT_SCOPE_MISMATCH",
                "preflight environment/account differs from store",
            )
        if not preflight.observed_at <= checked_at < preflight.expires_at:
            raise AdmissionDenied(
                "DISPATCH_PREFLIGHT_STALE",
                "preflight is not active at registration",
            )
        payload_json, content_hash = _canonical_payload(preflight.as_dict())
        record_hash = _record_hash(
            "preflight",
            self._preflight_material(
                preflight,
                registered_at=checked_at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        try:
            with self._transaction() as connection:
                command_row = connection.execute(
                    "SELECT * FROM execution_commands WHERE command_id = ?",
                    (preflight.command_id,),
                ).fetchone()
                if command_row is None:
                    raise RecordNotFound("preflight command is not registered")
                command = self._command_from_row(command_row)
                if command.state != "claimed":
                    raise StateConflict("preflight requires a claimed command")
                if (
                    command.ticket_hash != preflight.ticket_hash
                    or command.plan_hash != preflight.plan_hash
                ):
                    raise AdmissionDenied(
                        "DISPATCH_PREFLIGHT_BINDING_MISMATCH",
                        "preflight ticket/plan differs from command",
                    )
                ticket_row = connection.execute(
                    "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                    (command.ticket_hash,),
                ).fetchone()
                if ticket_row is None:
                    raise StorageError("preflight command ticket is missing")
                self._verify_ticket_row(ticket_row)
                ticket_payload = json.loads(str(ticket_row["payload_json"]))
                if not isinstance(ticket_payload, dict):
                    raise StorageError("preflight ticket payload is not an object")
                policy_hash = ticket_payload.get("policy_hash")
                if not isinstance(policy_hash, str):
                    raise AdmissionDenied(
                        "DISPATCH_PREFLIGHT_LEGACY_POLICY_UNBOUND",
                        "ticket lacks an exact risk-policy hash",
                    )
                if _hash(policy_hash, field="ticket policy_hash") != preflight.risk_policy_hash:
                    raise AdmissionDenied(
                        "DISPATCH_PREFLIGHT_POLICY_MISMATCH",
                        "preflight risk policy differs from approved ticket",
                    )
                outbox_row = connection.execute(
                    "SELECT * FROM execution_outbox WHERE command_id = ?",
                    (preflight.command_id,),
                ).fetchone()
                if outbox_row is None:
                    raise StorageError("preflight command has no outbox")
                outbox = self._outbox_from_row(outbox_row)
                if (
                    outbox.state != "claimed"
                    or outbox.lease_expires_at is None
                    or not outbox.claimed_at
                    or not outbox.claimed_at <= checked_at < outbox.lease_expires_at
                ):
                    raise StateConflict("preflight requires an active dispatch claim")
                if preflight.observed_at < outbox.claimed_at:
                    raise AdmissionDenied(
                        "DISPATCH_PREFLIGHT_PREDATES_CLAIM",
                        "send-time preflight must be observed after dispatch claim",
                    )
                if connection.execute(
                    "SELECT 1 FROM execution_attempts WHERE command_id = ?",
                    (preflight.command_id,),
                ).fetchone() is not None:
                    raise StateConflict("attempt already exists for preflight command")
                existing = connection.execute(
                    """
                    SELECT * FROM execution_dispatch_preflights
                    WHERE command_id = ?
                    """,
                    (preflight.command_id,),
                ).fetchone()
                if existing is not None:
                    current = self._preflight_from_row(existing)
                    if current.preflight_hash == preflight.preflight_hash:
                        return current
                    raise StateConflict("command cannot swap dispatch preflights")
                connection.execute(
                    """
                    INSERT INTO execution_dispatch_preflights (
                        preflight_hash, command_id, ticket_hash, plan_hash,
                        environment, account_id, account_snapshot_hash,
                        account_server_time_ms,
                        metadata_hash, market_snapshot_hash, risk_policy_hash,
                        observed_at, expires_at, passed, registered_at,
                        payload_json, content_hash, record_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        preflight.preflight_hash,
                        preflight.command_id,
                        preflight.ticket_hash,
                        preflight.plan_hash,
                        preflight.environment.value,
                        preflight.account_id,
                        preflight.account_snapshot_hash,
                        preflight.account_server_time_ms,
                        preflight.metadata_hash,
                        preflight.market_snapshot_hash,
                        preflight.risk_policy_hash,
                        _time_text(preflight.observed_at, field="observed_at"),
                        _time_text(preflight.expires_at, field="expires_at"),
                        _time_text(checked_at, field="registered_at"),
                        payload_json,
                        content_hash,
                        record_hash,
                    ),
                )
                self._append_event_locked(
                    connection,
                    command_id=preflight.command_id,
                    event_type="dispatch_preflight_registered",
                    occurred_at=checked_at,
                    payload={
                        "preflight_hash": preflight.preflight_hash,
                        "account_snapshot_hash": preflight.account_snapshot_hash,
                        "account_server_time_ms": preflight.account_server_time_ms,
                        "metadata_hash": preflight.metadata_hash,
                        "market_snapshot_hash": preflight.market_snapshot_hash,
                        "risk_policy_hash": preflight.risk_policy_hash,
                        "expires_at": _time_text(
                            preflight.expires_at, field="expires_at"
                        ),
                        "model_authority": False,
                    },
                )
                return preflight
        except sqlite3.IntegrityError as error:
            raise StateConflict(
                "preflight command/ticket/plan is already uniquely bound"
            ) from error

    def get_preflight(self, command_id: str) -> DispatchPreflight:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_dispatch_preflights
                WHERE command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("dispatch preflight is not registered")
        return self._preflight_from_row(row)

    @staticmethod
    def _signed_evidence_material(
        evidence: SignedEnvelopeEvidence,
        *,
        recorded_at: datetime,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            **evidence.as_dict(),
            "recorded_at": _time_text(recorded_at, field="recorded_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }

    @classmethod
    def _signed_evidence_from_row(
        cls, row: Mapping[str, Any]
    ) -> SignedEnvelopeEvidence:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="signed evidence content_hash"
        )
        payload = _decode_payload(payload_json, content_hash, field="signed evidence")
        if not isinstance(payload, dict):
            raise StorageError("persisted signed evidence payload is not an object")
        try:
            evidence = SignedEnvelopeEvidence(
                command_id=str(row["command_id"]),
                preflight_hash=str(row["preflight_hash"]),
                environment=Environment(str(row["environment"])),
                endpoint=str(row["endpoint"]),
                account_id=str(row["account_id"]),
                plan_hash=str(row["plan_hash"]),
                action_hash=str(row["action_hash"]),
                nonce=int(row["nonce"]),
                wire_hash=str(row["wire_hash"]),
                signature_hash=str(row["signature_hash"]),
                envelope_hash=str(row["envelope_hash"]),
                signer_binding_hash=str(row["signer_binding_hash"]),
                authorization_expires_at_ms=int(
                    row["authorization_expires_at_ms"]
                ),
                expires_after_ms=int(row["expires_after_ms"]),
                signed_at_ms=int(row["signed_at_ms"]),
                evidence_hash=str(row["evidence_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted signed evidence is invalid") from error
        if canonical_json(evidence.as_dict()) != payload_json:
            raise StorageError("persisted signed evidence differs from columns")
        recorded_at = _parse_time(
            row["recorded_at"], field="signed evidence recorded_at"
        )
        expected = _record_hash(
            "signed-evidence-record",
            cls._signed_evidence_material(
                evidence,
                recorded_at=recorded_at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        if _stored_hash(
            row["record_hash"], field="signed evidence record_hash"
        ) != expected:
            raise StorageError("persisted signed evidence record hash does not match")
        return evidence

    def _put_signed_evidence_locked(
        self,
        connection: sqlite3.Connection,
        evidence: SignedEnvelopeEvidence,
        *,
        at: datetime,
    ) -> SignedEnvelopeEvidence:
        payload_json, content_hash = _canonical_payload(evidence.as_dict())
        record_hash = _record_hash(
            "signed-evidence-record",
            self._signed_evidence_material(
                evidence,
                recorded_at=at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        existing = connection.execute(
            "SELECT * FROM execution_signed_envelopes WHERE command_id = ?",
            (evidence.command_id,),
        ).fetchone()
        if existing is not None:
            current = self._signed_evidence_from_row(existing)
            if current.evidence_hash == evidence.evidence_hash:
                return current
            raise StateConflict("command cannot swap signed-envelope evidence")
        connection.execute(
            """
            INSERT INTO execution_signed_envelopes (
                evidence_hash, command_id, preflight_hash, environment,
                endpoint, account_id, plan_hash, action_hash, nonce, wire_hash,
                signature_hash, envelope_hash, signer_binding_hash,
                authorization_expires_at_ms, expires_after_ms, signed_at_ms,
                recorded_at, payload_json, content_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_hash,
                evidence.command_id,
                evidence.preflight_hash,
                evidence.environment.value,
                evidence.endpoint,
                evidence.account_id,
                evidence.plan_hash,
                evidence.action_hash,
                evidence.nonce,
                evidence.wire_hash,
                evidence.signature_hash,
                evidence.envelope_hash,
                evidence.signer_binding_hash,
                evidence.authorization_expires_at_ms,
                evidence.expires_after_ms,
                evidence.signed_at_ms,
                _time_text(at, field="recorded_at"),
                payload_json,
                content_hash,
                record_hash,
            ),
        )
        return evidence

    @staticmethod
    def _transport_evidence_material(
        evidence: TransportOutcomeEvidence,
        *,
        recorded_at: datetime,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            **evidence.as_dict(),
            "recorded_at": _time_text(recorded_at, field="recorded_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }

    @classmethod
    def _transport_evidence_from_row(
        cls, row: Mapping[str, Any]
    ) -> TransportOutcomeEvidence:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="transport evidence content_hash"
        )
        payload = _decode_payload(
            payload_json, content_hash, field="transport evidence"
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted transport evidence payload is not an object")
        try:
            evidence = TransportOutcomeEvidence(
                command_id=str(row["command_id"]),
                attempt_id=str(row["attempt_id"]),
                signed_evidence_hash=str(row["signed_evidence_hash"]),
                endpoint=str(row["endpoint"]),
                attempted_at_ms=int(row["attempted_at_ms"]),
                outcome=str(row["outcome"]),
                http_status=(
                    None if row["http_status"] is None else int(row["http_status"])
                ),
                detail_code=str(row["detail_code"]),
                response_hash=(
                    None
                    if row["response_hash"] is None
                    else str(row["response_hash"])
                ),
                transport_attempt_hash=(
                    None
                    if row["transport_attempt_hash"] is None
                    else str(row["transport_attempt_hash"])
                ),
                send_count=(
                    None if row["send_count"] is None else int(row["send_count"])
                ),
                retry_performed=bool(row["retry_performed"]),
                venue_write_attempted=(
                    None
                    if row["venue_write_attempted"] is None
                    else bool(row["venue_write_attempted"])
                ),
                evidence_basis=str(row["evidence_basis"]),
                evidence_hash=str(row["evidence_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted transport evidence is invalid") from error
        if canonical_json(evidence.as_dict()) != payload_json:
            raise StorageError("persisted transport evidence differs from columns")
        recorded_at = _parse_time(
            row["recorded_at"], field="transport evidence recorded_at"
        )
        expected = _record_hash(
            "transport-evidence-record",
            cls._transport_evidence_material(
                evidence,
                recorded_at=recorded_at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        if _stored_hash(
            row["record_hash"], field="transport evidence record_hash"
        ) != expected:
            raise StorageError("persisted transport evidence record hash does not match")
        return evidence

    def _put_transport_evidence_locked(
        self,
        connection: sqlite3.Connection,
        evidence: TransportOutcomeEvidence,
        *,
        at: datetime,
    ) -> TransportOutcomeEvidence:
        payload_json, content_hash = _canonical_payload(evidence.as_dict())
        record_hash = _record_hash(
            "transport-evidence-record",
            self._transport_evidence_material(
                evidence,
                recorded_at=at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        existing = connection.execute(
            "SELECT * FROM execution_transport_outcomes WHERE command_id = ?",
            (evidence.command_id,),
        ).fetchone()
        if existing is not None:
            current = self._transport_evidence_from_row(existing)
            if current.evidence_hash == evidence.evidence_hash:
                return current
            raise StateConflict("command cannot swap transport evidence")
        connection.execute(
            """
            INSERT INTO execution_transport_outcomes (
                evidence_hash, command_id, attempt_id, signed_evidence_hash,
                endpoint, attempted_at_ms, outcome, http_status, detail_code,
                response_hash, transport_attempt_hash, send_count,
                retry_performed, venue_write_attempted, evidence_basis,
                recorded_at, payload_json, content_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_hash,
                evidence.command_id,
                evidence.attempt_id,
                evidence.signed_evidence_hash,
                evidence.endpoint,
                evidence.attempted_at_ms,
                evidence.outcome,
                evidence.http_status,
                evidence.detail_code,
                evidence.response_hash,
                evidence.transport_attempt_hash,
                evidence.send_count,
                (
                    None
                    if evidence.venue_write_attempted is None
                    else int(evidence.venue_write_attempted)
                ),
                evidence.evidence_basis,
                _time_text(at, field="recorded_at"),
                payload_json,
                content_hash,
                record_hash,
            ),
        )
        return evidence

    def get_signed_evidence(self, command_id: str) -> SignedEnvelopeEvidence:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_signed_envelopes WHERE command_id = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("signed-envelope evidence is not registered")
        return self._signed_evidence_from_row(row)

    def get_transport_evidence(self, command_id: str) -> TransportOutcomeEvidence:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_transport_outcomes WHERE command_id = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("transport evidence is not registered")
        return self._transport_evidence_from_row(row)

    # -- fenced outbox and one prepared send attempt ------------------

    def _set_command_state_locked(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        state: str,
        at: datetime,
        terminal: bool = False,
    ) -> CommandRecord:
        current = self._command_from_row(row)
        if state not in _COMMAND_STATES:
            raise ValidationError("unsupported command state")
        revision = current.revision + 1
        terminal_at = _utc(at, field="at") if terminal else current.terminal_at
        material = self._command_material_values(
            current.command_id,
            current.ticket_hash,
            current.plan_hash,
            current.approval_id,
            state,
            current.reserved_loss,
            current.reserved_notional,
            current.created_at,
            at,
            terminal_at,
            revision,
        )
        changed = connection.execute(
            """
            UPDATE execution_commands SET
                state = ?, updated_at = ?, terminal_at = ?, revision = ?,
                record_hash = ?
            WHERE command_id = ? AND revision = ?
            """,
            (
                state,
                _time_text(at, field="updated_at"),
                (
                    None
                    if terminal_at is None
                    else _time_text(terminal_at, field="terminal_at")
                ),
                revision,
                _record_hash("command", material),
                current.command_id,
                current.revision,
            ),
        )
        if changed.rowcount != 1:
            raise StateConflict("execution command changed concurrently")
        return self._command_from_row(
            connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (current.command_id,),
            ).fetchone()
        )

    def _set_outbox_locked(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        state: str,
        at: datetime,
        worker_id: str | None,
        fencing_token: int,
        claimed_at: datetime | None,
        lease_expires_at: datetime | None,
        current_attempt_id: str | None,
        attempt_count: int,
    ) -> OutboxRecord:
        current = self._outbox_from_row(row)
        if state not in _OUTBOX_STATES:
            raise ValidationError("unsupported outbox state")
        material = self._outbox_material_values(
            current.command_id,
            state,
            worker_id,
            fencing_token,
            claimed_at,
            lease_expires_at,
            current_attempt_id,
            attempt_count,
            current.created_at,
            at,
        )
        connection.execute(
            """
            UPDATE execution_outbox SET
                state = ?, worker_id = ?, fencing_token = ?, claimed_at = ?,
                lease_expires_at = ?, current_attempt_id = ?,
                attempt_count = ?, updated_at = ?, record_hash = ?
            WHERE command_id = ?
            """,
            (
                state,
                worker_id,
                fencing_token,
                None if claimed_at is None else _time_text(claimed_at, field="claimed_at"),
                (
                    None
                    if lease_expires_at is None
                    else _time_text(lease_expires_at, field="lease_expires_at")
                ),
                current_attempt_id,
                attempt_count,
                _time_text(at, field="updated_at"),
                _record_hash("outbox", material),
                current.command_id,
            ),
        )
        return self._outbox_from_row(
            connection.execute(
                "SELECT * FROM execution_outbox WHERE command_id = ?",
                (current.command_id,),
            ).fetchone()
        )

    def _normalize_expired_claims_locked(
        self, connection: sqlite3.Connection, *, at: datetime
    ) -> None:
        now_text = _time_text(at, field="at")
        rows = connection.execute(
            """
            SELECT * FROM execution_outbox
            WHERE state = 'claimed' AND lease_expires_at <= ?
            ORDER BY created_at, command_id
            """,
            (now_text,),
        ).fetchall()
        for row in rows:
            current = self._outbox_from_row(row)
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (current.command_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("expired claim references missing command")
            if current.current_attempt_id is None:
                self._set_outbox_locked(
                    connection,
                    row,
                    state="queued",
                    at=at,
                    worker_id=None,
                    fencing_token=current.fencing_token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=None,
                    attempt_count=current.attempt_count,
                )
                self._set_command_state_locked(
                    connection, command_row, state="queued", at=at
                )
                event_type = "unsent_claim_expired_requeued"
            else:
                attempt = connection.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                    (current.current_attempt_id,),
                ).fetchone()
                if attempt is None:
                    raise StorageError("claimed outbox references missing attempt")
                attempt_record = self._attempt_from_row(attempt)
                submission_authority = connection.execute(
                    """
                    SELECT 1 FROM execution_submission_authorities
                    WHERE command_id = ? AND attempt_id = ?
                      AND signed_evidence_hash = ?
                    """,
                    (
                        current.command_id,
                        attempt_record.attempt_id,
                        attempt_record.signed_evidence_hash,
                    ),
                ).fetchone()
                if submission_authority is None:
                    if attempt_record.state != "prepared":
                        raise StorageError(
                            "entry attempt without submission authority is not prepared"
                        )
                    leg_rows = connection.execute(
                        """
                        SELECT * FROM execution_command_legs
                        WHERE command_id = ?
                        """,
                        (current.command_id,),
                    ).fetchall()
                    if len(leg_rows) != 3:
                        raise StorageError(
                            "proven-unsent entry is missing protected legs"
                        )
                    for leg_row in leg_rows:
                        leg = self._leg_from_row(leg_row)
                        if leg.cumulative_filled != ZERO or leg.venue_oid is not None:
                            raise StateConflict(
                                "proven-unsent entry unexpectedly has venue evidence"
                            )
                        self._update_leg_locked(
                            connection,
                            leg,
                            status="expired",
                            cumulative_filled=ZERO,
                            venue_oid=None,
                            at=at,
                        )
                    command = self._command_from_row(command_row)
                    current_loss, current_notional, exposure_revision, _ = (
                        self._read_exposure_locked(connection)
                    )
                    self._write_exposure_locked(
                        connection,
                        loss=decimal_subtract(
                            current_loss,
                            command.reserved_loss,
                            field="proven-unsent reserved loss",
                        ),
                        notional=decimal_subtract(
                            current_notional,
                            command.reserved_notional,
                            field="proven-unsent reserved notional",
                        ),
                        previous_revision=exposure_revision,
                        at=at,
                    )
                    self._set_command_state_locked(
                        connection,
                        command_row,
                        state="terminal",
                        at=at,
                        terminal=True,
                    )
                    self._set_outbox_locked(
                        connection,
                        row,
                        state="terminal",
                        at=at,
                        worker_id=None,
                        fencing_token=current.fencing_token,
                        claimed_at=None,
                        lease_expires_at=None,
                        current_attempt_id=attempt_record.attempt_id,
                        attempt_count=current.attempt_count,
                    )
                    ticket = connection.execute(
                        "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                        (command.ticket_hash,),
                    ).fetchone()
                    if ticket is None:
                        raise StorageError(
                            "proven-unsent entry ticket is missing"
                        )
                    terminal_ticket = self._ticket_material(
                        ticket, state="terminal"
                    )
                    connection.execute(
                        """
                        UPDATE execution_tickets SET state = 'terminal',
                            record_hash = ?
                        WHERE ticket_hash = ? AND state = 'consumed'
                        """,
                        (
                            _record_hash("ticket", terminal_ticket),
                            command.ticket_hash,
                        ),
                    )
                    self._append_event_locked(
                        connection,
                        command_id=current.command_id,
                        event_type="prepared_entry_expired_proven_unsent",
                        occurred_at=at,
                        payload={
                            "fencing_token": current.fencing_token,
                            "attempt_id": attempt_record.attempt_id,
                            "submission_authority_consumed": False,
                            "venue_write_attempted": False,
                            "authority_reusable": False,
                        },
                    )
                    continue
                transport_evidence: TransportOutcomeEvidence | None = None
                if attempt_record.signed_evidence_hash is not None:
                    transport_evidence = TransportOutcomeEvidence(
                        command_id=attempt_record.command_id,
                        attempt_id=attempt_record.attempt_id,
                        signed_evidence_hash=attempt_record.signed_evidence_hash,
                        endpoint="https://api.hyperliquid-testnet.xyz/exchange",
                        attempted_at_ms=int(at.timestamp() * 1_000),
                        outcome="unknown",
                        http_status=None,
                        detail_code="worker_lease_expired_after_prepare",
                        response_hash=None,
                        transport_attempt_hash=None,
                        send_count=None,
                        retry_performed=False,
                        venue_write_attempted=None,
                        evidence_basis="claim_expiry",
                    )
                    self._put_transport_evidence_locked(
                        connection, transport_evidence, at=at
                    )
                connection.execute(
                    """
                    UPDATE execution_attempts SET
                        state = 'unknown', transport_evidence_hash = ?,
                        updated_at = ?, record_hash = ?
                    WHERE attempt_id = ? AND state = 'prepared'
                    """,
                    (
                        (
                            None
                            if transport_evidence is None
                            else transport_evidence.evidence_hash
                        ),
                        now_text,
                        self._attempt_hash_from_values(
                            attempt_record.attempt_id,
                            attempt_record.command_id,
                            attempt_record.worker_id,
                            attempt_record.fencing_token,
                            attempt_record.preflight_hash,
                            attempt_record.signed_evidence_hash,
                            (
                                None
                                if transport_evidence is None
                                else transport_evidence.evidence_hash
                            ),
                            attempt_record.nonce,
                            attempt_record.action_hash,
                            attempt_record.wire_hash,
                            "unknown",
                            attempt_record.response_hash,
                            attempt_record.prepared_at,
                            at,
                        ),
                        current.current_attempt_id,
                    ),
                )
                self._set_outbox_locked(
                    connection,
                    row,
                    state="submitted_unknown",
                    at=at,
                    worker_id=None,
                    fencing_token=current.fencing_token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=current.current_attempt_id,
                    attempt_count=current.attempt_count,
                )
                self._set_command_state_locked(
                    connection, command_row, state="submitted_unknown", at=at
                )
                self._mark_legs_unknown_locked(connection, current.command_id, at=at)
                event_type = "prepared_attempt_became_unknown"
            self._append_event_locked(
                connection,
                command_id=current.command_id,
                event_type=event_type,
                occurred_at=at,
                payload={
                    "fencing_token": current.fencing_token,
                    "attempt_id": current.current_attempt_id,
                    "transport_evidence_hash": (
                        None
                        if current.current_attempt_id is None
                        else connection.execute(
                            """
                            SELECT transport_evidence_hash
                            FROM execution_attempts WHERE attempt_id = ?
                            """,
                            (current.current_attempt_id,),
                        ).fetchone()["transport_evidence_hash"]
                    ),
                },
            )

    def claim_next(
        self,
        worker_id: str,
        *,
        at: datetime,
        lease_seconds: int,
    ) -> OutboxRecord | None:
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        checked_at = _utc(at, field="at")
        lease = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
        expires = checked_at + timedelta(seconds=lease)
        with self._transaction() as connection:
            self._normalize_expired_claims_locked(connection, at=checked_at)
            if connection.execute(
                """
                SELECT 1 FROM execution_incidents
                WHERE severity = 'critical' AND state != 'closed'
                LIMIT 1
                """
            ).fetchone() is not None:
                raise StateConflict("critical account incident blocks new dispatch")
            if connection.execute(
                """
                SELECT 1 FROM execution_recovery_commands
                WHERE state != 'terminal' LIMIT 1
                """
            ).fetchone() is not None:
                return None
            if connection.execute(
                """
                SELECT 1 FROM execution_commands
                WHERE state IN ('submitted_unknown', 'reconciling')
                LIMIT 1
                """
            ).fetchone() is not None:
                # Returning without raising commits any claim-expiry
                # normalization performed above while still blocking every
                # new risk-increasing dispatch.
                return None
            row = connection.execute(
                """
                SELECT * FROM execution_outbox
                WHERE state = 'queued'
                ORDER BY created_at, command_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            current = self._outbox_from_row(row)
            claimed = self._set_outbox_locked(
                connection,
                row,
                state="claimed",
                at=checked_at,
                worker_id=checked_worker,
                fencing_token=current.fencing_token + 1,
                claimed_at=checked_at,
                lease_expires_at=expires,
                current_attempt_id=None,
                attempt_count=current.attempt_count,
            )
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (current.command_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("outbox references missing command")
            self._set_command_state_locked(
                connection, command_row, state="claimed", at=checked_at
            )
            self._append_event_locked(
                connection,
                command_id=current.command_id,
                event_type="outbox_claimed",
                occurred_at=checked_at,
                payload={
                    "worker_id": checked_worker,
                    "fencing_token": claimed.fencing_token,
                    "lease_expires_at": _time_text(
                        expires, field="lease_expires_at"
                    ),
                },
            )
            return claimed

    def _require_claim_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        at: datetime,
        allowed_states: frozenset[str],
    ) -> tuple[OutboxRecord, sqlite3.Row]:
        row = connection.execute(
            "SELECT * FROM execution_outbox WHERE command_id = ?", (command_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFound("outbox command is not registered")
        current = self._outbox_from_row(row)
        if (
            current.state not in allowed_states
            or current.worker_id != worker_id
            or current.fencing_token != fencing_token
            or current.lease_expires_at is None
            or not current.claimed_at
            or not current.claimed_at <= at < current.lease_expires_at
        ):
            raise StateConflict("outbox claim is stale, expired, or wrong state")
        return current, row

    def renew_claim(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
        lease_seconds: int,
    ) -> OutboxRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        lease = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
        with self._transaction() as connection:
            current, row = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                allowed_states=frozenset({"claimed", "reconciling"}),
            )
            return self._set_outbox_locked(
                connection,
                row,
                state=current.state,
                at=checked_at,
                worker_id=checked_worker,
                fencing_token=token,
                claimed_at=current.claimed_at,
                lease_expires_at=checked_at + timedelta(seconds=lease),
                current_attempt_id=current.current_attempt_id,
                attempt_count=current.attempt_count,
            )

    @staticmethod
    def _attempt_hash_from_values(
        attempt_id: str,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        preflight_hash: str | None,
        signed_evidence_hash: str | None,
        transport_evidence_hash: str | None,
        nonce: int,
        action_hash: str,
        wire_hash: str,
        state: str,
        response_hash: str | None,
        prepared_at: datetime,
        updated_at: datetime,
    ) -> str:
        material: dict[str, object] = {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "worker_id": worker_id,
            "fencing_token": fencing_token,
            "nonce": nonce,
            "action_hash": action_hash,
            "wire_hash": wire_hash,
            "state": state,
            "response_hash": response_hash,
            "prepared_at": _time_text(prepared_at, field="prepared_at"),
            "updated_at": _time_text(updated_at, field="updated_at"),
        }
        # Migration v2 leaves old attempts nullable.  Their v1 record hashes
        # remain verifiable, but every newly prepared attempt must bind a
        # non-null preflight below.
        if preflight_hash is not None:
            material["preflight_hash"] = preflight_hash
        if signed_evidence_hash is not None:
            material["signed_evidence_hash"] = signed_evidence_hash
        if transport_evidence_hash is not None:
            material["transport_evidence_hash"] = transport_evidence_hash
        return _record_hash("attempt", material)

    @classmethod
    def _attempt_from_row(cls, row: Mapping[str, Any] | None) -> AttemptRecord:
        if row is None:
            raise StorageError("attempt row is missing")
        state = _stored_text(row["state"], field="attempt state", maximum=32)
        if state not in _ATTEMPT_STATES:
            raise StorageError("persisted attempt state is unsupported")
        record = AttemptRecord(
            attempt_id=_stored_text(
                row["attempt_id"], field="attempt_id", maximum=128
            ),
            command_id=_stored_text(
                row["command_id"], field="command_id", maximum=128
            ),
            worker_id=_stored_text(
                row["worker_id"], field="worker_id", maximum=128
            ),
            fencing_token=int(row["fencing_token"]),
            preflight_hash=(
                None
                if row["preflight_hash"] is None
                else _stored_hash(row["preflight_hash"], field="preflight_hash")
            ),
            signed_evidence_hash=(
                None
                if row["signed_evidence_hash"] is None
                else _stored_hash(
                    row["signed_evidence_hash"], field="signed_evidence_hash"
                )
            ),
            transport_evidence_hash=(
                None
                if row["transport_evidence_hash"] is None
                else _stored_hash(
                    row["transport_evidence_hash"], field="transport_evidence_hash"
                )
            ),
            nonce=int(row["nonce"]),
            action_hash=_stored_hash(row["action_hash"], field="action_hash"),
            wire_hash=_stored_hash(row["wire_hash"], field="wire_hash"),
            state=state,
            response_hash=(
                None
                if row["response_hash"] is None
                else _stored_hash(row["response_hash"], field="response_hash")
            ),
            prepared_at=_parse_time(row["prepared_at"], field="attempt prepared_at"),
            updated_at=_parse_time(row["updated_at"], field="attempt updated_at"),
        )
        expected = cls._attempt_hash_from_values(
            record.attempt_id,
            record.command_id,
            record.worker_id,
            record.fencing_token,
            record.preflight_hash,
            record.signed_evidence_hash,
            record.transport_evidence_hash,
            record.nonce,
            record.action_hash,
            record.wire_hash,
            record.state,
            record.response_hash,
            record.prepared_at,
            record.updated_at,
        )
        if _stored_hash(row["record_hash"], field="attempt record_hash") != expected:
            raise StorageError("persisted attempt record hash does not match")
        return record

    def prepare_attempt(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        attempt_id: str,
        preflight_hash: str,
        signed_evidence: SignedEnvelopeEvidence,
        nonce: int,
        action_hash: str,
        wire_hash: str,
        at: datetime,
    ) -> AttemptRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_attempt = _text(attempt_id, field="attempt_id", maximum=128)
        checked_preflight = _hash(preflight_hash, field="preflight_hash")
        if not isinstance(signed_evidence, SignedEnvelopeEvidence):
            raise TypeError("signed_evidence must be SignedEnvelopeEvidence")
        checked_nonce = _nonnegative_int(nonce, field="nonce")
        checked_action = _hash(action_hash, field="action_hash")
        checked_wire = _hash(wire_hash, field="wire_hash")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            current, outbox_row = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                allowed_states=frozenset({"claimed"}),
            )
            preflight_row = connection.execute(
                """
                SELECT * FROM execution_dispatch_preflights
                WHERE preflight_hash = ? AND command_id = ?
                """,
                (checked_preflight, checked_command),
            ).fetchone()
            if preflight_row is None:
                raise AdmissionDenied(
                    "DISPATCH_PREFLIGHT_NOT_FOUND",
                    "attempt requires the exact command-bound preflight",
                )
            preflight = self._preflight_from_row(preflight_row)
            if not preflight.passed:
                raise AdmissionDenied(
                    "DISPATCH_PREFLIGHT_FAILED",
                    "attempt preflight did not pass",
                )
            if not preflight.observed_at <= checked_at < preflight.expires_at:
                raise AdmissionDenied(
                    "DISPATCH_PREFLIGHT_STALE",
                    "attempt preflight is stale",
                )
            preflight_expiry_ms = int(preflight.expires_at.timestamp() * 1_000)
            if (
                signed_evidence.command_id != checked_command
                or signed_evidence.preflight_hash != checked_preflight
                or signed_evidence.environment is not self.environment
                or signed_evidence.account_id != self.account_id
                or signed_evidence.plan_hash != preflight.plan_hash
                or signed_evidence.nonce != checked_nonce
                or signed_evidence.action_hash != checked_action
                or signed_evidence.wire_hash != checked_wire
            ):
                raise AdmissionDenied(
                    "SIGNED_EVIDENCE_BINDING_MISMATCH",
                    "signed envelope differs from command/preflight/attempt",
                )
            if signed_evidence.expires_after_ms > preflight_expiry_ms:
                raise AdmissionDenied(
                    "SIGNED_EVIDENCE_OUTLIVES_PREFLIGHT",
                    "signed venue action remains valid beyond preflight",
                )
            if checked_at.timestamp() * 1_000 >= signed_evidence.expires_after_ms:
                raise AdmissionDenied(
                    "SIGNED_EVIDENCE_STALE",
                    "signed venue action is already expired",
                )
            existing = connection.execute(
                "SELECT * FROM execution_attempts WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if existing is not None:
                record = self._attempt_from_row(existing)
                if (
                    record.attempt_id == checked_attempt
                    and record.preflight_hash == checked_preflight
                    and record.signed_evidence_hash
                    == signed_evidence.evidence_hash
                    and record.nonce == checked_nonce
                    and record.action_hash == checked_action
                    and record.wire_hash == checked_wire
                ):
                    return record
                raise StateConflict("command already has a prepared attempt; retry forbidden")
            record_hash = self._attempt_hash_from_values(
                checked_attempt,
                checked_command,
                checked_worker,
                token,
                checked_preflight,
                signed_evidence.evidence_hash,
                None,
                checked_nonce,
                checked_action,
                checked_wire,
                "prepared",
                None,
                checked_at,
                checked_at,
            )
            self._put_signed_evidence_locked(
                connection, signed_evidence, at=checked_at
            )
            connection.execute(
                """
                INSERT INTO execution_attempts (
                    attempt_id, command_id, worker_id, fencing_token,
                    preflight_hash, signed_evidence_hash,
                    transport_evidence_hash, nonce,
                    action_hash, wire_hash, state, response_hash, prepared_at,
                    updated_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'prepared', NULL, ?, ?, ?)
                """,
                (
                    checked_attempt,
                    checked_command,
                    checked_worker,
                    token,
                    checked_preflight,
                    signed_evidence.evidence_hash,
                    checked_nonce,
                    checked_action,
                    checked_wire,
                    _time_text(checked_at, field="prepared_at"),
                    _time_text(checked_at, field="updated_at"),
                    record_hash,
                ),
            )
            self._set_outbox_locked(
                connection,
                outbox_row,
                state="claimed",
                at=checked_at,
                worker_id=checked_worker,
                fencing_token=token,
                claimed_at=current.claimed_at,
                lease_expires_at=current.lease_expires_at,
                current_attempt_id=checked_attempt,
                attempt_count=current.attempt_count + 1,
            )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="send_attempt_prepared",
                occurred_at=checked_at,
                payload={
                    "attempt_id": checked_attempt,
                    "nonce": checked_nonce,
                    "action_hash": checked_action,
                    "wire_hash": checked_wire,
                    "fencing_token": token,
                    "preflight_hash": checked_preflight,
                    "signed_evidence_hash": signed_evidence.evidence_hash,
                },
            )
            return self._attempt_from_row(
                connection.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                    (checked_attempt,),
                ).fetchone()
            )

    def require_submission_authority(
        self,
        command_id: str,
        attempt_id: str,
        signed_evidence_hash: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
    ) -> EntrySubmissionAuthority:
        """Consume the sole durable pre-send authority for a protected entry."""

        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_attempt = _text(attempt_id, field="attempt_id", maximum=128)
        checked_signed = _hash(
            signed_evidence_hash, field="signed_evidence_hash"
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            outbox, _ = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                allowed_states=frozenset({"claimed"}),
            )
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (checked_attempt,),
            ).fetchone()
            if attempt_row is None:
                raise RecordNotFound("entry submission attempt is missing")
            attempt = self._attempt_from_row(attempt_row)
            if (
                attempt.command_id != checked_command
                or attempt.worker_id != checked_worker
                or attempt.fencing_token != token
                or attempt.state != "prepared"
                or attempt.signed_evidence_hash != checked_signed
                or outbox.current_attempt_id != checked_attempt
                or outbox.lease_expires_at is None
            ):
                raise StateConflict(
                    "entry submission authority differs from durable attempt"
                )
            signed_row = connection.execute(
                """
                SELECT * FROM execution_signed_envelopes
                WHERE evidence_hash = ? AND command_id = ?
                """,
                (checked_signed, checked_command),
            ).fetchone()
            if signed_row is None:
                raise StorageError("entry submission signed evidence is missing")
            signed = self._signed_evidence_from_row(signed_row)
            if (
                signed.nonce != attempt.nonce
                or signed.action_hash != attempt.action_hash
                or signed.wire_hash != attempt.wire_hash
                or int(checked_at.timestamp() * 1_000) >= signed.expires_after_ms
            ):
                raise StateConflict("entry signed evidence is stale or mismatched")
            if connection.execute(
                """
                SELECT 1 FROM execution_submission_authorities
                WHERE command_id = ? OR attempt_id = ?
                """,
                (checked_command, checked_attempt),
            ).fetchone() is not None:
                raise StateConflict("entry submission authority is already consumed")
            material = {
                "command_id": checked_command,
                "attempt_id": checked_attempt,
                "signed_evidence_hash": checked_signed,
                "nonce": attempt.nonce,
                "action_hash": attempt.action_hash,
                "wire_hash": attempt.wire_hash,
                "worker_id": checked_worker,
                "fencing_token": token,
                "issued_at": checked_at,
                "lease_expires_at": outbox.lease_expires_at,
            }
            authority_hash = _record_hash("entry-submission-authority", material)
            payload = {**material, "authority_hash": authority_hash}
            payload_json, content_hash = _canonical_payload(payload)
            connection.execute(
                """
                INSERT INTO execution_submission_authorities (
                    authority_hash, command_id, attempt_id,
                    signed_evidence_hash, worker_id, fencing_token,
                    issued_at, lease_expires_at, payload_json,
                    content_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authority_hash,
                    checked_command,
                    checked_attempt,
                    checked_signed,
                    checked_worker,
                    token,
                    _time_text(checked_at, field="issued_at"),
                    _time_text(outbox.lease_expires_at, field="lease_expires_at"),
                    payload_json,
                    content_hash,
                    _record_hash(
                        "entry-submission-authority-record",
                        {
                            "authority_hash": authority_hash,
                            "payload_json": payload_json,
                            "content_hash": content_hash,
                        },
                    ),
                ),
            )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="entry_submission_authority_consumed",
                occurred_at=checked_at,
                payload={
                    "attempt_id": checked_attempt,
                    "signed_evidence_hash": checked_signed,
                    "authority_hash": authority_hash,
                    "retry_allowed": False,
                },
            )
            return EntrySubmissionAuthority(
                command_id=checked_command,
                attempt_id=checked_attempt,
                signed_evidence_hash=checked_signed,
                nonce=attempt.nonce,
                action_hash=attempt.action_hash,
                wire_hash=attempt.wire_hash,
                worker_id=checked_worker,
                fencing_token=token,
                lease_expires_at=outbox.lease_expires_at,
                authority_hash=authority_hash,
            )

    def get_attempt(self, command_id: str) -> AttemptRecord:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_attempts WHERE command_id = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"execution attempt not found: {checked}")
        return self._attempt_from_row(row)

    def _mark_legs_unknown_locked(
        self, connection: sqlite3.Connection, command_id: str, *, at: datetime
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM execution_command_legs WHERE command_id = ?",
            (command_id,),
        ).fetchall()
        for row in rows:
            leg = self._leg_from_row(row)
            if leg.status == "queued":
                self._update_leg_locked(
                    connection,
                    leg,
                    status="submitted_unknown",
                    cumulative_filled=leg.cumulative_filled,
                    venue_oid=leg.venue_oid,
                    at=at,
                )

    def mark_submitted_unknown(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        transport_evidence: TransportOutcomeEvidence,
        at: datetime,
    ) -> CommandRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        if not isinstance(transport_evidence, TransportOutcomeEvidence):
            raise TypeError("transport_evidence must be TransportOutcomeEvidence")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            current, row = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                allowed_states=frozenset({"claimed"}),
            )
            if current.current_attempt_id is None:
                raise StateConflict("unknown outcome requires a prepared attempt")
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (current.current_attempt_id,),
            ).fetchone()
            attempt = self._attempt_from_row(attempt_row)
            if (
                transport_evidence.command_id != checked_command
                or transport_evidence.attempt_id != attempt.attempt_id
                or transport_evidence.signed_evidence_hash
                != attempt.signed_evidence_hash
                or transport_evidence.outcome != "unknown"
                or transport_evidence.evidence_basis != "transport_result"
            ):
                raise StateConflict(
                    "unknown transport evidence differs from prepared attempt"
                )
            self._put_transport_evidence_locked(
                connection, transport_evidence, at=checked_at
            )
            connection.execute(
                """
                UPDATE execution_attempts SET
                    state = 'unknown', transport_evidence_hash = ?,
                    updated_at = ?, record_hash = ? WHERE attempt_id = ?
                """,
                (
                    transport_evidence.evidence_hash,
                    _time_text(checked_at, field="updated_at"),
                    self._attempt_hash_from_values(
                        attempt.attempt_id,
                        attempt.command_id,
                        attempt.worker_id,
                        attempt.fencing_token,
                        attempt.preflight_hash,
                        attempt.signed_evidence_hash,
                        transport_evidence.evidence_hash,
                        attempt.nonce,
                        attempt.action_hash,
                        attempt.wire_hash,
                        "unknown",
                        attempt.response_hash,
                        attempt.prepared_at,
                        checked_at,
                    ),
                    attempt.attempt_id,
                ),
            )
            self._set_outbox_locked(
                connection,
                row,
                state="submitted_unknown",
                at=checked_at,
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=attempt.attempt_id,
                attempt_count=current.attempt_count,
            )
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            command = self._set_command_state_locked(
                connection,
                command_row,
                state="submitted_unknown",
                at=checked_at,
            )
            self._mark_legs_unknown_locked(connection, checked_command, at=checked_at)
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="submission_outcome_unknown",
                occurred_at=checked_at,
                payload={
                    "attempt_id": attempt.attempt_id,
                    "transport_evidence_hash": transport_evidence.evidence_hash,
                    "transport_attempt_hash": (
                        transport_evidence.transport_attempt_hash
                    ),
                    "detail_code": transport_evidence.detail_code,
                    "retry_allowed": False,
                },
            )
            return command

    def _update_leg_locked(
        self,
        connection: sqlite3.Connection,
        leg: LegRecord,
        *,
        status: str,
        cumulative_filled: Decimal,
        venue_oid: int | None,
        at: datetime,
    ) -> LegRecord:
        if status not in _LEG_STATES:
            raise ValidationError("unsupported leg state")
        cumulative = _decimal(
            cumulative_filled, field="cumulative_filled", nonnegative=True
        )
        if cumulative < leg.cumulative_filled:
            raise StateConflict("cumulative venue fill cannot decrease")
        if cumulative > leg.requested_quantity:
            raise StateConflict("cumulative venue fill exceeds requested quantity")
        if leg.venue_oid is not None and venue_oid not in (None, leg.venue_oid):
            raise StateConflict("venue OID changed for an existing leg")
        resolved_oid = leg.venue_oid if venue_oid is None else venue_oid
        if resolved_oid is not None and (type(resolved_oid) is not int or resolved_oid < 0):
            raise ValidationError("venue_oid must be non-negative")
        if leg.status in _TERMINAL_LEG_STATES and status != leg.status:
            raise StateConflict("terminal leg status cannot change without an incident")
        if status == "filled" and cumulative != leg.requested_quantity:
            raise StateConflict("filled leg must equal requested quantity")
        if status == "partially_filled" and not ZERO < cumulative < leg.requested_quantity:
            raise StateConflict("partial leg fill must be between zero and requested")
        material = self._leg_material_values(
            leg.command_id,
            leg.role,
            leg.cloid,
            leg.intent_hash,
            leg.side,
            leg.reduce_only,
            leg.requested_quantity,
            cumulative,
            resolved_oid,
            status,
            at,
        )
        connection.execute(
            """
            UPDATE execution_command_legs SET
                cumulative_filled = ?, venue_oid = ?, status = ?,
                updated_at = ?, record_hash = ?
            WHERE command_id = ? AND role = ?
            """,
            (
                _decimal_text(cumulative, field="cumulative_filled"),
                resolved_oid,
                status,
                _time_text(at, field="updated_at"),
                _record_hash("leg", material),
                leg.command_id,
                leg.role,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM execution_command_legs
            WHERE command_id = ? AND role = ?
            """,
            (leg.command_id, leg.role),
        ).fetchone()
        if row is None:
            raise StorageError("command leg disappeared during update")
        return self._leg_from_row(row)

    def record_submission_response(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        result: BatchSubmissionResult,
        *,
        transport_evidence: TransportOutcomeEvidence,
        at: datetime,
    ) -> CommandRecord:
        if not isinstance(result, BatchSubmissionResult):
            raise TypeError("result must be BatchSubmissionResult")
        if not isinstance(transport_evidence, TransportOutcomeEvidence):
            raise TypeError("transport_evidence must be TransportOutcomeEvidence")
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        response_hash = _hash(result.response_hash, field="response_hash")
        with self._transaction() as connection:
            current, outbox_row = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                allowed_states=frozenset({"claimed"}),
            )
            if current.current_attempt_id is None:
                raise StateConflict("submission response requires prepared attempt")
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (current.current_attempt_id,),
            ).fetchone()
            attempt = self._attempt_from_row(attempt_row)
            if attempt.state != "prepared":
                raise StateConflict("attempt already has an outcome")
            if (
                transport_evidence.command_id != checked_command
                or transport_evidence.attempt_id != attempt.attempt_id
                or transport_evidence.signed_evidence_hash
                != attempt.signed_evidence_hash
                or transport_evidence.outcome != "response_received"
                or transport_evidence.evidence_basis != "transport_result"
            ):
                raise StateConflict(
                    "response transport evidence differs from prepared attempt"
                )
            self._put_transport_evidence_locked(
                connection, transport_evidence, at=checked_at
            )
            if len(result.legs) not in (0, 3):
                raise ValidationError("protected batch response must cover zero or three legs")
            provisional_protection: ProtectionRecord | None = None
            if result.legs:
                legs = {
                    leg.role: leg
                    for leg in (
                        self._leg_from_row(row)
                        for row in connection.execute(
                            """
                            SELECT * FROM execution_command_legs
                            WHERE command_id = ?
                            """,
                            (checked_command,),
                        ).fetchall()
                    )
                }
                for index, role in enumerate(_ROLES):
                    venue = result.legs[index]
                    leg = legs[role]
                    if venue.requested_size != leg.requested_quantity:
                        raise StateConflict("response requested size differs from command")
                    if venue.state is LegSubmissionState.ERROR:
                        status = "rejected"
                    elif venue.state is LegSubmissionState.RESTING:
                        status = "resting"
                    elif venue.fully_filled:
                        status = "filled"
                    else:
                        status = "partially_filled"
                    legs[role] = self._update_leg_locked(
                        connection,
                        leg,
                        status=status,
                        cumulative_filled=venue.filled_size,
                        venue_oid=venue.oid,
                        at=checked_at,
                    )
                entry_result = result.legs[0]
                stop_result = result.legs[1]
                entry_has_fill = entry_result.filled_size > ZERO
                entry_partial = entry_result.partially_filled
                stop_resting = stop_result.state is LegSubmissionState.RESTING
                if entry_partial or (entry_has_fill and not stop_resting):
                    plan_row = connection.execute(
                        """
                        SELECT instrument FROM execution_plans
                        WHERE plan_hash = (
                            SELECT plan_hash FROM execution_commands
                            WHERE command_id = ?
                        )
                        """,
                        (checked_command,),
                    ).fetchone()
                    if plan_row is None:
                        raise StorageError("hazardous response command plan is missing")
                    entry_leg = legs["entry"]
                    signed_position = (
                        entry_result.filled_size
                        if entry_leg.side == Side.BUY.value
                        else -entry_result.filled_size
                    )
                    provisional_protection = self._upsert_protection_locked(
                        connection,
                        command_id=checked_command,
                        instrument=str(plan_row["instrument"]),
                        signed_position=signed_position,
                        protected_quantity=ZERO,
                        stop_cloid=legs["protective_stop"].cloid,
                        observed_at=checked_at,
                        failed=entry_has_fill and not stop_resting,
                    )
                    incident_code = (
                        "PROTECTION_SUBMISSION_FAILED"
                        if entry_has_fill and not stop_resting
                        else "ENTRY_PARTIAL_FILL"
                    )
                    self._open_incident_locked(
                        connection,
                        incident_id=_record_hash(
                            "incident-id",
                            {
                                "command_id": checked_command,
                                "response_hash": response_hash,
                                "code": incident_code,
                            },
                        ),
                        command_id=checked_command,
                        code=incident_code,
                        severity="critical",
                        at=checked_at,
                        details={
                            "provisional": True,
                            "entry_filled_quantity": _decimal_text(
                                entry_result.filled_size,
                                field="entry_filled_quantity",
                            ),
                            "entry_partial": entry_partial,
                            "stop_response_state": stop_result.state.value,
                            "protection_state": provisional_protection.state,
                            "new_risk_halted": True,
                        },
                    )
            connection.execute(
                """
                UPDATE execution_attempts SET
                    state = 'response_received', response_hash = ?,
                    transport_evidence_hash = ?, updated_at = ?, record_hash = ?
                WHERE attempt_id = ? AND state = 'prepared'
                """,
                (
                    response_hash,
                    transport_evidence.evidence_hash,
                    _time_text(checked_at, field="updated_at"),
                    self._attempt_hash_from_values(
                        attempt.attempt_id,
                        attempt.command_id,
                        attempt.worker_id,
                        attempt.fencing_token,
                        attempt.preflight_hash,
                        attempt.signed_evidence_hash,
                        transport_evidence.evidence_hash,
                        attempt.nonce,
                        attempt.action_hash,
                        attempt.wire_hash,
                        "response_received",
                        response_hash,
                        attempt.prepared_at,
                        checked_at,
                    ),
                    attempt.attempt_id,
                ),
            )
            self._set_outbox_locked(
                connection,
                outbox_row,
                state="reconciling",
                at=checked_at,
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=attempt.attempt_id,
                attempt_count=current.attempt_count,
            )
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            command = self._set_command_state_locked(
                connection, command_row, state="reconciling", at=checked_at
            )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="submission_response_recorded",
                occurred_at=checked_at,
                payload={
                    "batch": result.as_dict(),
                    "transport_evidence_hash": transport_evidence.evidence_hash,
                    "transport_attempt_hash": (
                        transport_evidence.transport_attempt_hash
                    ),
                    "detail_code": transport_evidence.detail_code,
                },
            )
            if provisional_protection is not None:
                self._append_event_locked(
                    connection,
                    command_id=checked_command,
                    event_type="provisional_protection_failure",
                    occurred_at=checked_at,
                    payload={
                        "state": provisional_protection.state,
                        "signed_position_quantity": _decimal_text(
                            provisional_protection.signed_position_quantity,
                            field="signed_position_quantity",
                        ),
                        "protected_quantity": "0",
                        "requires_immediate_reconciliation": True,
                    },
                )
            return command

    def claim_reconciliation(
        self,
        command_id: str,
        worker_id: str,
        *,
        at: datetime,
        lease_seconds: int,
    ) -> OutboxRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        checked_at = _utc(at, field="at")
        lease = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
        expires = checked_at + timedelta(seconds=lease)
        with self._transaction() as connection:
            self._normalize_expired_claims_locked(connection, at=checked_at)
            row = connection.execute(
                "SELECT * FROM execution_outbox WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("reconciliation command is not registered")
            current = self._outbox_from_row(row)
            if current.state not in {"submitted_unknown", "reconciling"}:
                raise StateConflict("command is not eligible for reconciliation")
            if (
                current.worker_id is not None
                and current.lease_expires_at is not None
                and checked_at < current.lease_expires_at
            ):
                if current.worker_id == checked_worker:
                    return current
                raise StateConflict("reconciliation is claimed by another worker")
            claimed = self._set_outbox_locked(
                connection,
                row,
                state="reconciling",
                at=checked_at,
                worker_id=checked_worker,
                fencing_token=current.fencing_token + 1,
                claimed_at=checked_at,
                lease_expires_at=expires,
                current_attempt_id=current.current_attempt_id,
                attempt_count=current.attempt_count,
            )
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if command_row is None:
                raise StorageError("outbox references missing command")
            command = self._command_from_row(command_row)
            if command.state != "reconciling":
                self._set_command_state_locked(
                    connection, command_row, state="reconciling", at=checked_at
                )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="reconciliation_claimed",
                occurred_at=checked_at,
                payload={
                    "worker_id": checked_worker,
                    "fencing_token": claimed.fencing_token,
                    "lease_expires_at": _time_text(
                        expires, field="lease_expires_at"
                    ),
                },
            )
            return claimed

    # -- venue/account reconciliation ---------------------------------

    def _put_fill_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: str,
        fill: VenueFill,
        observed_at: datetime,
        legs: Mapping[str, LegRecord],
    ) -> None:
        if fill.role not in legs or legs[fill.role].cloid != fill.cloid:
            raise StateConflict("fill does not match a command leg")
        if fill.occurred_at > observed_at:
            raise ValidationError("fill cannot postdate reconciliation observation")
        if connection.execute(
            "SELECT 1 FROM execution_recovery_fills WHERE fill_id = ?",
            (fill.fill_id,),
        ).fetchone() is not None:
            raise StateConflict("venue fill cannot belong to recovery and parent")
        if fill.venue_oid is not None:
            recovery_owner = connection.execute(
                """
                SELECT 1 FROM execution_recovery_fills
                WHERE venue_oid = ? AND venue_trade_id = ?
                    AND transaction_hash = ? AND occurred_at = ?
                """,
                (
                    fill.venue_oid,
                    fill.venue_trade_id,
                    fill.transaction_hash,
                    _time_text(fill.occurred_at, field="occurred_at"),
                ),
            ).fetchone()
            if recovery_owner is not None:
                raise StateConflict(
                    "venue fill cannot belong to recovery and parent"
                )
        existing = connection.execute(
            "SELECT * FROM execution_fills WHERE fill_id = ?", (fill.fill_id,)
        ).fetchone()
        if existing is not None:
            existing_payload_json = str(existing["payload_json"])
            existing_content_hash = _stored_hash(
                existing["content_hash"], field="fill content_hash"
            )
            _decode_payload(
                existing_payload_json,
                existing_content_hash,
                field="fill",
            )
            existing_material = {
                "fill_id": str(existing["fill_id"]),
                "command_id": str(existing["command_id"]),
                "role": str(existing["role"]),
                "cloid": str(existing["cloid"]),
                "quantity": str(existing["quantity"]),
                "price": str(existing["price"]),
                "fee": str(existing["fee"]),
                "occurred_at": str(existing["occurred_at"]),
            }
            if existing["observed_at"] is not None:
                existing_material.update(
                    {
                        "venue_oid": existing["venue_oid"],
                        "venue_trade_id": existing["venue_trade_id"],
                        "transaction_hash": existing["transaction_hash"],
                        "closed_pnl": existing["closed_pnl"],
                        "fee_token": existing["fee_token"],
                        "observed_at": str(existing["observed_at"]),
                    }
                )
            existing_material.update(
                {
                    "content_hash": existing_content_hash,
                    "payload_json": existing_payload_json,
                }
            )
            if _stored_hash(
                existing["record_hash"], field="fill record_hash"
            ) != _record_hash("fill", existing_material):
                raise StorageError("persisted fill record hash does not match")
            comparisons = {
                "command_id": command_id,
                "role": fill.role,
                "cloid": fill.cloid,
                "quantity": _decimal_text(fill.quantity, field="quantity"),
                "price": _decimal_text(fill.price, field="price"),
                "fee": _decimal_text(fill.fee, field="fee"),
                "occurred_at": _time_text(fill.occurred_at, field="occurred_at"),
                "venue_oid": fill.venue_oid,
                "venue_trade_id": fill.venue_trade_id,
                "transaction_hash": fill.transaction_hash,
                "closed_pnl": (
                    None
                    if fill.closed_pnl is None
                    else _decimal_text(fill.closed_pnl, field="closed_pnl")
                ),
                "fee_token": fill.fee_token,
            }
            if all(existing[field] == expected for field, expected in comparisons.items()):
                return
            raise StateConflict("fill ID is already bound to different content")
        payload = {
            "command_id": command_id,
            **fill.as_dict(),
            "observed_at": _time_text(observed_at, field="observed_at"),
        }
        payload_json, content_hash = _canonical_payload(payload)
        material = {
            "fill_id": fill.fill_id,
            "command_id": command_id,
            "role": fill.role,
            "cloid": fill.cloid,
            "quantity": _decimal_text(fill.quantity, field="quantity"),
            "price": _decimal_text(fill.price, field="price"),
            "fee": _decimal_text(fill.fee, field="fee"),
            "occurred_at": _time_text(fill.occurred_at, field="occurred_at"),
            "venue_oid": fill.venue_oid,
            "venue_trade_id": fill.venue_trade_id,
            "transaction_hash": fill.transaction_hash,
            "closed_pnl": (
                None
                if fill.closed_pnl is None
                else _decimal_text(fill.closed_pnl, field="closed_pnl")
            ),
            "fee_token": fill.fee_token,
            "observed_at": _time_text(observed_at, field="observed_at"),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }
        record_hash = _record_hash("fill", material)
        connection.execute(
            """
            INSERT INTO execution_fills (
                fill_id, command_id, role, cloid, quantity, price, fee,
                occurred_at, payload_json, content_hash, record_hash,
                venue_oid, venue_trade_id, transaction_hash, closed_pnl,
                fee_token, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.fill_id,
                command_id,
                fill.role,
                fill.cloid,
                _decimal_text(fill.quantity, field="quantity"),
                _decimal_text(fill.price, field="price"),
                _decimal_text(fill.fee, field="fee"),
                _time_text(fill.occurred_at, field="occurred_at"),
                payload_json,
                content_hash,
                record_hash,
                fill.venue_oid,
                fill.venue_trade_id,
                fill.transaction_hash,
                (
                    None
                    if fill.closed_pnl is None
                    else _decimal_text(fill.closed_pnl, field="closed_pnl")
                ),
                fill.fee_token,
                _time_text(observed_at, field="observed_at"),
            ),
        )

    @staticmethod
    def _position_material(
        instrument: str,
        quantity: Decimal,
        snapshot_hash: str,
        observed_at: datetime,
        revision: int,
    ) -> dict[str, object]:
        return {
            "instrument": instrument,
            "signed_quantity": _decimal_text(
                quantity, field="signed_position_quantity"
            ),
            "account_snapshot_hash": snapshot_hash,
            "observed_at": _time_text(observed_at, field="observed_at"),
            "revision": revision,
        }

    @classmethod
    def _position_from_row(cls, row: Mapping[str, Any]) -> PositionRecord:
        record = PositionRecord(
            instrument=_stored_text(
                row["instrument"], field="instrument", maximum=64
            ),
            signed_quantity=_decimal(
                row["signed_quantity"], field="signed_quantity"
            ),
            account_snapshot_hash=_stored_hash(
                row["account_snapshot_hash"], field="account_snapshot_hash"
            ),
            observed_at=_parse_time(row["observed_at"], field="position observed_at"),
            revision=int(row["revision"]),
        )
        expected = _record_hash(
            "position",
            cls._position_material(
                record.instrument,
                record.signed_quantity,
                record.account_snapshot_hash,
                record.observed_at,
                record.revision,
            ),
        )
        if _stored_hash(row["record_hash"], field="position record_hash") != expected:
            raise StorageError("persisted position record hash does not match")
        return record

    def _upsert_position_locked(
        self,
        connection: sqlite3.Connection,
        *,
        instrument: str,
        quantity: Decimal,
        snapshot_hash: str,
        observed_at: datetime,
    ) -> PositionRecord:
        row = connection.execute(
            "SELECT * FROM execution_positions WHERE instrument = ?", (instrument,)
        ).fetchone()
        revision = 1
        if row is not None:
            current = self._position_from_row(row)
            if observed_at < current.observed_at:
                raise StateConflict("position snapshot time cannot move backwards")
            if observed_at == current.observed_at:
                if (
                    current.signed_quantity == quantity
                    and current.account_snapshot_hash == snapshot_hash
                ):
                    return current
                raise StateConflict("same-time position snapshot is contradictory")
            revision = current.revision + 1
        material = self._position_material(
            instrument, quantity, snapshot_hash, observed_at, revision
        )
        connection.execute(
            """
            INSERT INTO execution_positions (
                instrument, signed_quantity, account_snapshot_hash, observed_at,
                revision, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instrument) DO UPDATE SET
                signed_quantity = excluded.signed_quantity,
                account_snapshot_hash = excluded.account_snapshot_hash,
                observed_at = excluded.observed_at,
                revision = excluded.revision,
                record_hash = excluded.record_hash
            """,
            (
                instrument,
                _decimal_text(quantity, field="signed_quantity"),
                snapshot_hash,
                _time_text(observed_at, field="observed_at"),
                revision,
                _record_hash("position", material),
            ),
        )
        return self._position_from_row(
            connection.execute(
                "SELECT * FROM execution_positions WHERE instrument = ?",
                (instrument,),
            ).fetchone()
        )

    @staticmethod
    def _protection_material(
        command_id: str,
        instrument: str,
        state: str,
        signed_position: Decimal,
        protected_quantity: Decimal,
        stop_cloid: str,
        observed_at: datetime,
        revision: int,
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "instrument": instrument,
            "state": state,
            "signed_position_quantity": _decimal_text(
                signed_position, field="signed_position_quantity"
            ),
            "protected_quantity": _decimal_text(
                protected_quantity, field="protected_quantity"
            ),
            "stop_cloid": stop_cloid,
            "observed_at": _time_text(observed_at, field="observed_at"),
            "revision": revision,
        }

    @classmethod
    def _protection_from_row(cls, row: Mapping[str, Any]) -> ProtectionRecord:
        state = _stored_text(row["state"], field="protection state", maximum=32)
        if state not in _PROTECTION_STATES:
            raise StorageError("persisted protection state is unsupported")
        record = ProtectionRecord(
            command_id=_stored_text(
                row["command_id"], field="command_id", maximum=128
            ),
            instrument=_stored_text(
                row["instrument"], field="instrument", maximum=64
            ),
            state=state,
            signed_position_quantity=_decimal(
                row["signed_position_quantity"], field="signed_position_quantity"
            ),
            protected_quantity=_decimal(
                row["protected_quantity"],
                field="protected_quantity",
                nonnegative=True,
            ),
            stop_cloid=_stored_text(
                row["stop_cloid"], field="stop_cloid", maximum=128
            ),
            observed_at=_parse_time(
                row["observed_at"], field="protection observed_at"
            ),
            revision=int(row["revision"]),
        )
        expected = _record_hash(
            "protection",
            cls._protection_material(
                record.command_id,
                record.instrument,
                record.state,
                record.signed_position_quantity,
                record.protected_quantity,
                record.stop_cloid,
                record.observed_at,
                record.revision,
            ),
        )
        if _stored_hash(row["record_hash"], field="protection record_hash") != expected:
            raise StorageError("persisted protection hash does not match")
        return record

    def _upsert_protection_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: str,
        instrument: str,
        signed_position: Decimal,
        protected_quantity: Decimal,
        stop_cloid: str,
        observed_at: datetime,
        failed: bool,
    ) -> ProtectionRecord:
        absolute_position = abs(signed_position)
        if failed and absolute_position > ZERO:
            state = "failed"
        elif absolute_position == ZERO and protected_quantity == ZERO:
            state = "flat"
        elif protected_quantity == absolute_position:
            state = "protected"
        elif protected_quantity < absolute_position:
            state = "under_protected"
        else:
            state = "over_protected"
        row = connection.execute(
            "SELECT * FROM execution_protection WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        revision = 1
        if row is not None:
            current = self._protection_from_row(row)
            if observed_at < current.observed_at:
                raise StateConflict("protection observation cannot move backwards")
            revision = current.revision + 1
        material = self._protection_material(
            command_id,
            instrument,
            state,
            signed_position,
            protected_quantity,
            stop_cloid,
            observed_at,
            revision,
        )
        connection.execute(
            """
            INSERT INTO execution_protection (
                command_id, instrument, state, signed_position_quantity,
                protected_quantity, stop_cloid, observed_at, revision,
                record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                state = excluded.state,
                signed_position_quantity = excluded.signed_position_quantity,
                protected_quantity = excluded.protected_quantity,
                stop_cloid = excluded.stop_cloid,
                observed_at = excluded.observed_at,
                revision = excluded.revision,
                record_hash = excluded.record_hash
            """,
            (
                command_id,
                instrument,
                state,
                _decimal_text(signed_position, field="signed_position_quantity"),
                _decimal_text(protected_quantity, field="protected_quantity"),
                stop_cloid,
                _time_text(observed_at, field="observed_at"),
                revision,
                _record_hash("protection", material),
            ),
        )
        return self._protection_from_row(
            connection.execute(
                "SELECT * FROM execution_protection WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        )

    def reconcile(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        reconciliation_id: str,
        account_snapshot_hash: str,
        observed_at: datetime,
        complete: bool,
        legs: Sequence[LegReconciliation],
        signed_position_quantity: Decimal | str | int,
        protected_quantity: Decimal | str | int,
        fills: Sequence[VenueFill] = (),
        mutation_at: datetime | None = None,
    ) -> CommandRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_id = _text(
            reconciliation_id, field="reconciliation_id", maximum=128
        )
        snapshot_hash = _hash(
            account_snapshot_hash, field="account_snapshot_hash"
        )
        checked_at = _utc(observed_at, field="observed_at")
        checked_mutation_at = (
            checked_at
            if mutation_at is None
            else _utc(mutation_at, field="mutation_at")
        )
        if type(complete) is not bool:
            raise TypeError("complete must be bool")
        leg_values = tuple(legs)
        if not leg_values or any(
            not isinstance(value, LegReconciliation) for value in leg_values
        ):
            raise TypeError("legs must contain LegReconciliation records")
        if len({value.role for value in leg_values}) != len(leg_values):
            raise ValidationError("reconciliation repeats a leg role")
        if complete and {value.role for value in leg_values} != set(_ROLES):
            raise ValidationError("complete reconciliation requires all three legs")
        signed_position = _decimal(
            signed_position_quantity, field="signed_position_quantity"
        )
        protected = _decimal(
            protected_quantity, field="protected_quantity", nonnegative=True
        )
        fill_values = tuple(fills)
        if any(not isinstance(value, VenueFill) for value in fill_values):
            raise TypeError("fills must contain VenueFill records")
        payload = {
            "command_id": checked_command,
            "reconciliation_id": checked_id,
            "account_snapshot_hash": snapshot_hash,
            "observed_at": _time_text(checked_at, field="observed_at"),
            "complete": complete,
            "legs": [
                value.as_dict()
                for value in sorted(
                    leg_values, key=lambda item: _ROLES.index(item.role)
                )
            ],
            "signed_position_quantity": _decimal_text(
                signed_position, field="signed_position_quantity"
            ),
            "protected_quantity": _decimal_text(
                protected, field="protected_quantity"
            ),
            "fills": [value.as_dict() for value in sorted(fill_values, key=lambda x: x.fill_id)],
        }
        payload_json, content_hash = _canonical_payload(payload)
        reconciliation_material = {
            "reconciliation_id": checked_id,
            "command_id": checked_command,
            "account_snapshot_hash": snapshot_hash,
            "complete": complete,
            "observed_at": _time_text(checked_at, field="observed_at"),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }
        reconciliation_hash = _record_hash(
            "reconciliation", reconciliation_material
        )
        with self._transaction() as connection:
            current_outbox, outbox_row = self._require_claim_locked(
                connection,
                command_id=checked_command,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_mutation_at,
                allowed_states=frozenset({"reconciling"}),
            )
            existing = connection.execute(
                """
                SELECT * FROM execution_reconciliations
                WHERE reconciliation_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if existing is not None:
                if existing["record_hash"] == reconciliation_hash:
                    self._set_outbox_locked(
                        connection,
                        outbox_row,
                        state="reconciling",
                        at=checked_mutation_at,
                        worker_id=None,
                        fencing_token=token,
                        claimed_at=None,
                        lease_expires_at=None,
                        current_attempt_id=current_outbox.current_attempt_id,
                        attempt_count=current_outbox.attempt_count,
                    )
                    self._append_event_locked(
                        connection,
                        command_id=checked_command,
                        event_type="reconciliation_replay_released",
                        occurred_at=checked_mutation_at,
                        payload={
                            "reconciliation_id": checked_id,
                            "account_snapshot_hash": snapshot_hash,
                        },
                    )
                    return self._command_from_row(
                        connection.execute(
                            "SELECT * FROM execution_commands WHERE command_id = ?",
                            (checked_command,),
                        ).fetchone()
                    )
                raise StateConflict("reconciliation ID is already bound differently")
            command_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            command = self._command_from_row(command_row)
            if command.state != "reconciling":
                raise StateConflict("command is not in reconciliation state")
            current_legs = {
                leg.role: leg
                for leg in (
                    self._leg_from_row(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM execution_command_legs
                        WHERE command_id = ?
                        """,
                        (checked_command,),
                    ).fetchall()
                )
            }
            if set(current_legs) != set(_ROLES):
                raise StorageError("command is missing protected legs")
            for update in leg_values:
                current_leg = current_legs[update.role]
                if current_leg.cloid != update.cloid:
                    raise StateConflict("reconciliation CLOID differs from command")
                current_legs[update.role] = self._update_leg_locked(
                    connection,
                    current_leg,
                    status=update.status,
                    cumulative_filled=update.cumulative_filled,
                    venue_oid=update.venue_oid,
                    at=checked_at,
                )
            for fill in fill_values:
                self._put_fill_locked(
                    connection,
                    command_id=checked_command,
                    fill=fill,
                    observed_at=checked_at,
                    legs=current_legs,
                )
            plan = connection.execute(
                "SELECT instrument FROM execution_plans WHERE plan_hash = ?",
                (command.plan_hash,),
            ).fetchone()
            if plan is None:
                raise StorageError("command plan is missing")
            instrument = str(plan["instrument"])
            self._upsert_position_locked(
                connection,
                instrument=instrument,
                quantity=signed_position,
                snapshot_hash=snapshot_hash,
                observed_at=checked_at,
            )
            stop_leg = current_legs["protective_stop"]
            protection_failed = (
                stop_leg.status in {"rejected", "expired", "absent"}
                and signed_position != ZERO
            )
            protection = self._upsert_protection_locked(
                connection,
                command_id=checked_command,
                instrument=instrument,
                signed_position=signed_position,
                protected_quantity=protected,
                stop_cloid=stop_leg.cloid,
                observed_at=checked_at,
                failed=protection_failed,
            )
            entry_side = current_legs["entry"].side
            direction_conflict = (
                signed_position > ZERO and entry_side != Side.BUY.value
            ) or (signed_position < ZERO and entry_side != Side.SELL.value)
            if protection.state in {
                "failed",
                "under_protected",
                "over_protected",
            }:
                code = {
                    "failed": "PROTECTION_FAILED",
                    "under_protected": "POSITION_UNDER_PROTECTED",
                    "over_protected": "POSITION_OVER_PROTECTED",
                }[protection.state]
                self._open_incident_locked(
                    connection,
                    incident_id=_record_hash(
                        "incident-id",
                        {
                            "command_id": checked_command,
                            "reconciliation_id": checked_id,
                            "code": code,
                        },
                    ),
                    command_id=checked_command,
                    code=code,
                    severity="critical",
                    at=checked_at,
                    details={
                        "signed_position_quantity": _decimal_text(
                            signed_position, field="signed_position_quantity"
                        ),
                        "protected_quantity": _decimal_text(
                            protected, field="protected_quantity"
                        ),
                    },
                )
            if direction_conflict:
                self._open_incident_locked(
                    connection,
                    incident_id=_record_hash(
                        "incident-id",
                        {
                            "command_id": checked_command,
                            "reconciliation_id": checked_id,
                            "code": "POSITION_DIRECTION_CONTRADICTION",
                        },
                    ),
                    command_id=checked_command,
                    code="POSITION_DIRECTION_CONTRADICTION",
                    severity="critical",
                    at=checked_at,
                    details={"entry_side": entry_side},
                )
            connection.execute(
                """
                INSERT INTO execution_reconciliations (
                    reconciliation_id, command_id, account_snapshot_hash,
                    complete, observed_at, payload_json, content_hash,
                    record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_id,
                    checked_command,
                    snapshot_hash,
                    int(complete),
                    _time_text(checked_at, field="observed_at"),
                    payload_json,
                    content_hash,
                    reconciliation_hash,
                ),
            )
            terminal = (
                complete
                and signed_position == ZERO
                and protected == ZERO
                and all(
                    leg.status in _TERMINAL_LEG_STATES
                    for leg in current_legs.values()
                )
            )
            if terminal:
                current_loss, current_notional, exposure_revision, _ = (
                    self._read_exposure_locked(connection)
                )
                next_loss = decimal_subtract(
                    current_loss,
                    command.reserved_loss,
                    field="reconciled reserved loss",
                )
                next_notional = decimal_subtract(
                    current_notional,
                    command.reserved_notional,
                    field="reconciled reserved notional",
                )
                self._write_exposure_locked(
                    connection,
                    loss=next_loss,
                    notional=next_notional,
                    previous_revision=exposure_revision,
                    at=checked_mutation_at,
                )
                command = self._set_command_state_locked(
                    connection,
                    command_row,
                    state="terminal",
                    at=checked_mutation_at,
                    terminal=True,
                )
                self._set_outbox_locked(
                    connection,
                    outbox_row,
                    state="terminal",
                    at=checked_mutation_at,
                    worker_id=None,
                    fencing_token=token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=self._outbox_from_row(
                        outbox_row
                    ).current_attempt_id,
                    attempt_count=self._outbox_from_row(outbox_row).attempt_count,
                )
                ticket = connection.execute(
                    "SELECT * FROM execution_tickets WHERE ticket_hash = ?",
                    (command.ticket_hash,),
                ).fetchone()
                if ticket is None:
                    raise StorageError("terminal command ticket is missing")
                terminal_ticket = self._ticket_material(ticket, state="terminal")
                connection.execute(
                    """
                    UPDATE execution_tickets SET state = 'terminal', record_hash = ?
                    WHERE ticket_hash = ? AND state = 'consumed'
                    """,
                    (_record_hash("ticket", terminal_ticket), command.ticket_hash),
                )
                terminal_flat_incidents = connection.execute(
                    """
                    SELECT * FROM execution_incidents
                    WHERE command_id = ? AND severity = 'critical'
                      AND (
                          state = 'contained'
                          OR (
                              state = 'open'
                              AND code =
                                  'UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING'
                          )
                      )
                    ORDER BY opened_at, incident_id
                    """,
                    (checked_command,),
                ).fetchall()
                for incident_row in terminal_flat_incidents:
                    incident = self._incident_from_row(incident_row)
                    details_json, details_hash = _canonical_payload(
                        dict(incident.details), maximum=_MAX_DETAILS_BYTES
                    )
                    closed_material = self._incident_material(
                        incident.incident_id,
                        incident.command_id,
                        incident.code,
                        incident.severity,
                        "closed",
                        incident.opened_at,
                        checked_mutation_at,
                        incident.revision + 1,
                        details_json,
                        details_hash,
                    )
                    connection.execute(
                        """
                        UPDATE execution_incidents SET state = 'closed',
                            updated_at = ?, revision = ?, record_hash = ?
                        WHERE incident_id = ? AND state = ?
                        """,
                        (
                            _time_text(checked_mutation_at, field="updated_at"),
                            incident.revision + 1,
                            _record_hash("incident", closed_material),
                            incident.incident_id,
                            incident.state,
                        ),
                    )
                    self._append_event_locked(
                        connection,
                        command_id=checked_command,
                        event_type=(
                            "contained_incident_closed_terminal_flat"
                            if incident.state == "contained"
                            else "ambiguity_incident_closed_terminal_flat"
                        ),
                        occurred_at=checked_mutation_at,
                        payload={
                            "incident_id": incident.incident_id,
                            "incident_code": incident.code,
                            "previous_state": incident.state,
                            "account_snapshot_hash": snapshot_hash,
                        },
                    )
            else:
                current_outbox = self._outbox_from_row(outbox_row)
                self._set_outbox_locked(
                    connection,
                    outbox_row,
                    state="reconciling",
                    at=checked_mutation_at,
                    worker_id=None,
                    fencing_token=token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=current_outbox.current_attempt_id,
                    attempt_count=current_outbox.attempt_count,
                )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type=(
                    "reconciliation_terminal" if terminal else "reconciliation_recorded"
                ),
                occurred_at=checked_mutation_at,
                payload={
                    "reconciliation_id": checked_id,
                    "account_snapshot_hash": snapshot_hash,
                    "complete": complete,
                    "terminal": terminal,
                    "position": _decimal_text(
                        signed_position, field="signed_position_quantity"
                    ),
                    "protection_state": protection.state,
                },
            )
            return command

    def release_reconciliation_claim(
        self,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
        reason: str,
    ) -> OutboxRecord:
        checked_command = _text(command_id, field="command_id", maximum=128)
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        checked_reason = _text(reason, field="reason", maximum=128)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_outbox WHERE command_id = ?",
                (checked_command,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("execution outbox is missing")
            current = self._outbox_from_row(row)
            if (
                current.state != "reconciling"
                or current.worker_id != checked_worker
                or current.fencing_token != token
                or current.claimed_at is None
                or current.lease_expires_at is None
                or checked_at < current.claimed_at
            ):
                raise StateConflict(
                    "reconciliation claim release is stale or mismatched"
                )
            released = self._set_outbox_locked(
                connection,
                row,
                state="reconciling",
                at=checked_at,
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=current.current_attempt_id,
                attempt_count=current.attempt_count,
            )
            self._append_event_locked(
                connection,
                command_id=checked_command,
                event_type="reconciliation_claim_released",
                occurred_at=checked_at,
                payload={
                    "reason": checked_reason,
                    "fencing_token": token,
                    "venue_write_attempted": False,
                },
            )
            return released

    # -- incidents and reconciled reads -------------------------------

    @staticmethod
    def _incident_material(
        incident_id: str,
        command_id: str | None,
        code: str,
        severity: str,
        state: str,
        opened_at: datetime,
        updated_at: datetime,
        revision: int,
        details_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            "incident_id": incident_id,
            "command_id": command_id,
            "code": code,
            "severity": severity,
            "state": state,
            "opened_at": _time_text(opened_at, field="opened_at"),
            "updated_at": _time_text(updated_at, field="updated_at"),
            "revision": revision,
            "details_json": details_json,
            "content_hash": content_hash,
        }

    @classmethod
    def _incident_from_row(cls, row: Mapping[str, Any]) -> IncidentRecord:
        details_json = str(row["details_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="incident content_hash"
        )
        details = _decode_payload(
            details_json,
            content_hash,
            field="incident",
            maximum=_MAX_DETAILS_BYTES,
        )
        if not isinstance(details, dict):
            raise StorageError("persisted incident details are not an object")
        state = _stored_text(row["state"], field="incident state", maximum=16)
        severity = _stored_text(
            row["severity"], field="incident severity", maximum=16
        )
        if state not in _INCIDENT_STATES or severity not in _INCIDENT_SEVERITIES:
            raise StorageError("persisted incident state is unsupported")
        record = IncidentRecord(
            incident_id=_stored_text(
                row["incident_id"], field="incident_id", maximum=128
            ),
            command_id=(
                None
                if row["command_id"] is None
                else _stored_text(
                    row["command_id"], field="command_id", maximum=128
                )
            ),
            code=_stored_text(row["code"], field="incident code", maximum=128),
            severity=severity,
            state=state,
            opened_at=_parse_time(row["opened_at"], field="incident opened_at"),
            updated_at=_parse_time(row["updated_at"], field="incident updated_at"),
            revision=int(row["revision"]),
            details=details,
        )
        expected = _record_hash(
            "incident",
            cls._incident_material(
                record.incident_id,
                record.command_id,
                record.code,
                record.severity,
                record.state,
                record.opened_at,
                record.updated_at,
                record.revision,
                details_json,
                content_hash,
            ),
        )
        if _stored_hash(row["record_hash"], field="incident record_hash") != expected:
            raise StorageError("persisted incident record hash does not match")
        return record

    def _open_incident_locked(
        self,
        connection: sqlite3.Connection,
        *,
        incident_id: str,
        command_id: str | None,
        code: str,
        severity: str,
        at: datetime,
        details: Mapping[str, Any],
    ) -> IncidentRecord:
        checked_id = _text(incident_id, field="incident_id", maximum=128)
        checked_code = _text(code, field="incident code", maximum=128)
        if severity not in _INCIDENT_SEVERITIES:
            raise ValidationError("incident severity is invalid")
        details_json, content_hash = _canonical_payload(
            dict(details), maximum=_MAX_DETAILS_BYTES
        )
        existing = connection.execute(
            "SELECT * FROM execution_incidents WHERE incident_id = ?", (checked_id,)
        ).fetchone()
        if existing is not None:
            return self._incident_from_row(existing)
        material = self._incident_material(
            checked_id,
            command_id,
            checked_code,
            severity,
            "open",
            at,
            at,
            1,
            details_json,
            content_hash,
        )
        connection.execute(
            """
            INSERT INTO execution_incidents (
                incident_id, command_id, code, severity, state, opened_at,
                updated_at, revision, details_json, content_hash, record_hash
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, 1, ?, ?, ?)
            """,
            (
                checked_id,
                command_id,
                checked_code,
                severity,
                _time_text(at, field="opened_at"),
                _time_text(at, field="updated_at"),
                details_json,
                content_hash,
                _record_hash("incident", material),
            ),
        )
        record = self._incident_from_row(
            connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (checked_id,),
            ).fetchone()
        )
        self._append_event_locked(
            connection,
            command_id=command_id,
            event_type="incident_opened",
            occurred_at=at,
            payload={
                "incident_id": checked_id,
                "code": checked_code,
                "severity": severity,
            },
        )
        return record

    def record_incident(
        self,
        *,
        incident_id: str,
        command_id: str | None,
        code: str,
        severity: str,
        at: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> IncidentRecord:
        checked_command = (
            None
            if command_id is None
            else _text(command_id, field="command_id", maximum=128)
        )
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            if checked_command is not None and connection.execute(
                "SELECT 1 FROM execution_commands WHERE command_id = ?",
                (checked_command,),
            ).fetchone() is None:
                raise RecordNotFound("incident command is not registered")
            return self._open_incident_locked(
                connection,
                incident_id=incident_id,
                command_id=checked_command,
                code=code,
                severity=severity,
                at=checked_at,
                details={} if details is None else details,
            )

    def update_incident_state(
        self,
        incident_id: str,
        *,
        expected_revision: int,
        state: str,
        at: datetime,
    ) -> IncidentRecord:
        checked_id = _text(incident_id, field="incident_id", maximum=128)
        expected_revision = _positive_int(
            expected_revision, field="expected_revision"
        )
        if state not in _INCIDENT_STATES - {"open"}:
            raise ValidationError("incident update state must be contained or closed")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (checked_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("incident is not registered")
            current = self._incident_from_row(row)
            if current.severity == "critical":
                raise StateConflict(
                    "critical incidents may transition only through complete "
                    "recovery reconciliation"
                )
            if current.revision != expected_revision:
                raise StateConflict("incident compare-and-swap revision is stale")
            if current.state == "closed":
                raise StateConflict("closed incident cannot transition")
            if current.state == "contained" and state != "closed":
                raise StateConflict("contained incident can transition only to closed")
            if checked_at < current.updated_at:
                raise StateConflict("incident time cannot move backwards")
            details_json, content_hash = _canonical_payload(
                dict(current.details), maximum=_MAX_DETAILS_BYTES
            )
            revision = current.revision + 1
            material = self._incident_material(
                current.incident_id,
                current.command_id,
                current.code,
                current.severity,
                state,
                current.opened_at,
                checked_at,
                revision,
                details_json,
                content_hash,
            )
            connection.execute(
                """
                UPDATE execution_incidents SET
                    state = ?, updated_at = ?, revision = ?, record_hash = ?
                WHERE incident_id = ? AND revision = ?
                """,
                (
                    state,
                    _time_text(checked_at, field="updated_at"),
                    revision,
                    _record_hash("incident", material),
                    checked_id,
                    current.revision,
                ),
            )
            record = self._incident_from_row(
                connection.execute(
                    "SELECT * FROM execution_incidents WHERE incident_id = ?",
                    (checked_id,),
                ).fetchone()
            )
            self._append_event_locked(
                connection,
                command_id=record.command_id,
                event_type=f"incident_{state}",
                occurred_at=checked_at,
                payload={"incident_id": checked_id, "code": record.code},
            )
            return record

    def list_incidents(
        self, command_id: str | None = None
    ) -> tuple[IncidentRecord, ...]:
        connection = self._connect()
        try:
            if command_id is None:
                rows = connection.execute(
                    "SELECT * FROM execution_incidents ORDER BY opened_at, incident_id"
                ).fetchall()
            else:
                checked = _text(command_id, field="command_id", maximum=128)
                rows = connection.execute(
                    """
                    SELECT * FROM execution_incidents
                    WHERE command_id = ? ORDER BY opened_at, incident_id
                    """,
                    (checked,),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._incident_from_row(row) for row in rows)

    def get_position(self, instrument: str) -> PositionRecord:
        checked = _text(instrument, field="instrument", maximum=64)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_positions WHERE instrument = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("position has not been reconciled")
        return self._position_from_row(row)

    def list_positions(self) -> tuple[PositionRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM execution_positions ORDER BY instrument"
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._position_from_row(row) for row in rows)

    def get_protection(self, command_id: str) -> ProtectionRecord:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_protection WHERE command_id = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("protection has not been reconciled")
        return self._protection_from_row(row)

    def list_protections(self) -> tuple[ProtectionRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM execution_protection ORDER BY command_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._protection_from_row(row) for row in rows)

    def list_fills(self, command_id: str) -> tuple[VenueFill, ...]:
        checked = _text(command_id, field="command_id", maximum=128)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM execution_fills
                WHERE command_id = ? ORDER BY occurred_at, fill_id
                """,
                (checked,),
            ).fetchall()
        finally:
            connection.close()
        result: list[VenueFill] = []
        for row in rows:
            payload_json = str(row["payload_json"])
            content_hash = _stored_hash(
                row["content_hash"], field="fill content_hash"
            )
            payload = _decode_payload(payload_json, content_hash, field="fill")
            material = {
                "fill_id": str(row["fill_id"]),
                "command_id": str(row["command_id"]),
                "role": str(row["role"]),
                "cloid": str(row["cloid"]),
                "quantity": str(row["quantity"]),
                "price": str(row["price"]),
                "fee": str(row["fee"]),
                "occurred_at": str(row["occurred_at"]),
            }
            if row["observed_at"] is not None:
                material.update(
                    {
                        "venue_oid": row["venue_oid"],
                        "venue_trade_id": row["venue_trade_id"],
                        "transaction_hash": row["transaction_hash"],
                        "closed_pnl": row["closed_pnl"],
                        "fee_token": row["fee_token"],
                        "observed_at": str(row["observed_at"]),
                    }
                )
                if not isinstance(payload, dict) or payload.get(
                    "observed_at"
                ) != row["observed_at"]:
                    raise StorageError(
                        "persisted fill observation differs from payload"
                    )
            material.update(
                {"content_hash": content_hash, "payload_json": payload_json}
            )
            if _stored_hash(row["record_hash"], field="fill record_hash") != _record_hash(
                "fill", material
            ):
                raise StorageError("persisted fill record hash does not match")
            result.append(
                VenueFill(
                    fill_id=str(row["fill_id"]),
                    role=str(row["role"]),
                    cloid=str(row["cloid"]),
                    quantity=Decimal(str(row["quantity"])),
                    price=Decimal(str(row["price"])),
                    fee=Decimal(str(row["fee"])),
                    occurred_at=_parse_time(
                        row["occurred_at"], field="fill occurred_at"
                    ),
                    venue_oid=(
                        None if row["venue_oid"] is None else int(row["venue_oid"])
                    ),
                    venue_trade_id=(
                        None
                        if row["venue_trade_id"] is None
                        else int(row["venue_trade_id"])
                    ),
                    transaction_hash=(
                        None
                        if row["transaction_hash"] is None
                        else str(row["transaction_hash"])
                    ),
                    closed_pnl=(
                        None
                        if row["closed_pnl"] is None
                        else Decimal(str(row["closed_pnl"]))
                    ),
                    fee_token=(
                        None if row["fee_token"] is None else str(row["fee_token"])
                    ),
                    observed_at=(
                        None
                        if row["observed_at"] is None
                        else _parse_time(row["observed_at"], field="fill observed_at")
                    ),
                )
            )
        return tuple(result)

    # -- durable account-safety recovery commands --------------------

    @staticmethod
    def _recovery_permit_material(
        permit: RecoveryPermit,
        *,
        state: str,
        recovery_command_id: str | None,
        updated_at: datetime,
    ) -> dict[str, object]:
        recovery_material_json, recovery_material_hash = _canonical_payload(
            dict(permit.recovery_material)
        )
        return {
            "permit_id": permit.permit_id,
            "token_hash": permit.token_hash,
            "parent_command_id": permit.parent_command_id,
            "incident_id": permit.incident_id,
            "kind": permit.kind,
            "environment": permit.environment.value,
            "account_id": permit.account_id,
            "source_hash": permit.source_hash,
            "preflight_hash": permit.preflight_hash,
            "recovery_hash": permit.recovery_hash,
            "recovery_material_json": recovery_material_json,
            "recovery_material_hash": recovery_material_hash,
            "safety_policy_hash": permit.safety_policy_hash,
            "original_attempt_id": permit.original_attempt_id,
            "original_nonce": permit.original_nonce,
            "issuer_id": permit.issuer_id,
            "audience": permit.audience,
            "issued_at": _time_text(permit.issued_at, field="issued_at"),
            "expires_at": _time_text(permit.expires_at, field="expires_at"),
            "state": state,
            "recovery_command_id": recovery_command_id,
            "updated_at": _time_text(updated_at, field="updated_at"),
        }

    @classmethod
    def _recovery_permit_from_row(cls, row: Mapping[str, Any]) -> RecoveryPermit:
        permit = RecoveryPermit(
            permit_id=str(row["permit_id"]),
            token_hash=str(row["token_hash"]),
            parent_command_id=str(row["parent_command_id"]),
            incident_id=str(row["incident_id"]),
            kind=str(row["kind"]),
            environment=Environment(str(row["environment"])),
            account_id=str(row["account_id"]),
            source_hash=str(row["source_hash"]),
            preflight_hash=(
                None if row["preflight_hash"] is None else str(row["preflight_hash"])
            ),
            recovery_hash=str(row["recovery_hash"]),
            recovery_material=json.loads(str(row["recovery_material_json"])),
            safety_policy_hash=str(row["safety_policy_hash"]),
            original_attempt_id=(
                None
                if row["original_attempt_id"] is None
                else str(row["original_attempt_id"])
            ),
            original_nonce=(
                None if row["original_nonce"] is None else int(row["original_nonce"])
            ),
            issuer_id=str(row["issuer_id"]),
            audience=str(row["audience"]),
            issued_at=_parse_time(row["issued_at"], field="permit issued_at"),
            expires_at=_parse_time(row["expires_at"], field="permit expires_at"),
        )
        expected = _record_hash(
            "recovery-permit",
            cls._recovery_permit_material(
                permit,
                state=str(row["state"]),
                recovery_command_id=(
                    None
                    if row["recovery_command_id"] is None
                    else str(row["recovery_command_id"])
                ),
                updated_at=_parse_time(
                    row["updated_at"], field="permit updated_at"
                ),
            ),
        )
        if _stored_hash(row["record_hash"], field="permit record_hash") != expected:
            raise StorageError("persisted recovery permit hash does not match")
        return permit

    def register_recovery_permit(self, permit: RecoveryPermit) -> RecoveryPermit:
        if not isinstance(permit, RecoveryPermit):
            raise TypeError("permit must be RecoveryPermit")
        if permit.environment is not self.environment or permit.account_id != self.account_id:
            raise ValidationError("recovery permit scope differs from execution store")
        with self._transaction() as connection:
            incident_row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (permit.incident_id,),
            ).fetchone()
            if incident_row is None:
                raise RecordNotFound("recovery permit incident is not persisted")
            incident = self._incident_from_row(incident_row)
            if (
                incident.command_id != permit.parent_command_id
                or incident.severity != "critical"
                or incident.state != "open"
            ):
                raise StateConflict("recovery requires its bound open critical incident")
            parent = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (permit.parent_command_id,),
            ).fetchone()
            if parent is None:
                raise RecordNotFound("recovery parent command is missing")
            self._command_from_row(parent)
            parent_attempt = connection.execute(
                "SELECT * FROM execution_attempts WHERE command_id = ?",
                (permit.parent_command_id,),
            ).fetchone()
            unknown_attempt = False
            if parent_attempt is not None:
                unknown_attempt = (
                    self._attempt_from_row(parent_attempt).state == "unknown"
                )
            if unknown_attempt and permit.kind != "noop_fence":
                fenced = connection.execute(
                    """
                    SELECT 1
                    FROM execution_recovery_commands AS command
                    JOIN execution_recovery_reconciliations AS reconciliation
                      ON reconciliation.recovery_command_id = command.recovery_command_id
                    WHERE command.parent_command_id = ?
                      AND command.kind = 'noop_fence'
                      AND command.state = 'terminal'
                      AND reconciliation.complete = 1
                      AND reconciliation.success = 1
                    LIMIT 1
                    """,
                    (permit.parent_command_id,),
                ).fetchone()
                if fenced is None:
                    raise StateConflict(
                        "unknown parent attempt requires terminal noop-fence "
                        "reconciliation before cancel or close"
                    )
            if permit.kind == "noop_fence":
                if parent_attempt is None:
                    raise StateConflict("noop recovery requires persisted unknown attempt")
                attempt = self._attempt_from_row(parent_attempt)
                if (
                    attempt.state != "unknown"
                    or attempt.attempt_id != permit.original_attempt_id
                    or attempt.nonce != permit.original_nonce
                    or attempt.preflight_hash != permit.preflight_hash
                ):
                    raise StateConflict("noop permit differs from original unknown attempt")
            elif permit.preflight_hash is not None and parent_attempt is not None:
                attempt = self._attempt_from_row(parent_attempt)
                if attempt.preflight_hash != permit.preflight_hash:
                    raise StateConflict("recovery permit preflight differs from parent")
            existing = connection.execute(
                "SELECT * FROM execution_recovery_permits WHERE permit_id = ?",
                (permit.permit_id,),
            ).fetchone()
            record_hash = _record_hash(
                "recovery-permit",
                self._recovery_permit_material(
                    permit,
                    state="issued",
                    recovery_command_id=None,
                    updated_at=permit.issued_at,
                ),
            )
            if existing is not None:
                current = self._recovery_permit_from_row(existing)
                if current == permit and existing["record_hash"] == record_hash:
                    return current
                raise StateConflict("recovery permit ID is already bound differently")
            connection.execute(
                """
                INSERT INTO execution_recovery_permits (
                    permit_id, token_hash, parent_command_id, incident_id, kind,
                    environment, account_id, source_hash, preflight_hash,
                    recovery_hash, safety_policy_hash, original_attempt_id,
                    recovery_material_json, recovery_material_hash,
                    original_nonce, issuer_id, audience, issued_at, expires_at,
                    state, recovery_command_id, updated_at, record_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, 'testnet',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'issued', NULL, ?, ?
                )
                """,
                (
                    permit.permit_id,
                    permit.token_hash,
                    permit.parent_command_id,
                    permit.incident_id,
                    permit.kind,
                    permit.account_id,
                    permit.source_hash,
                    permit.preflight_hash,
                    permit.recovery_hash,
                    permit.safety_policy_hash,
                    permit.original_attempt_id,
                    canonical_json(dict(permit.recovery_material)),
                    hashlib.sha256(
                        canonical_json(dict(permit.recovery_material)).encode("utf-8")
                    ).hexdigest(),
                    permit.original_nonce,
                    permit.issuer_id,
                    permit.audience,
                    _time_text(permit.issued_at, field="issued_at"),
                    _time_text(permit.expires_at, field="expires_at"),
                    _time_text(permit.issued_at, field="updated_at"),
                    record_hash,
                ),
            )
            self._append_event_locked(
                connection,
                command_id=permit.parent_command_id,
                event_type="recovery_permit_registered",
                occurred_at=permit.issued_at,
                payload={
                    "permit_id": permit.permit_id,
                    "incident_id": permit.incident_id,
                    "kind": permit.kind,
                    "recovery_hash": permit.recovery_hash,
                },
            )
            return permit

    @staticmethod
    def _recovery_command_material(record: RecoveryCommand) -> dict[str, object]:
        return {
            "recovery_command_id": record.recovery_command_id,
            "permit_id": record.permit_id,
            "parent_command_id": record.parent_command_id,
            "incident_id": record.incident_id,
            "kind": record.kind,
            "priority": record.priority,
            "source_hash": record.source_hash,
            "preflight_hash": record.preflight_hash,
            "recovery_hash": record.recovery_hash,
            "recovery_material_json": record.recovery_material_json,
            "recovery_material_hash": record.recovery_material_hash,
            "safety_policy_hash": record.safety_policy_hash,
            "original_attempt_id": record.original_attempt_id,
            "original_nonce": record.original_nonce,
            "state": record.state,
            "created_at": _time_text(record.created_at, field="created_at"),
            "updated_at": _time_text(record.updated_at, field="updated_at"),
            "terminal_at": (
                None
                if record.terminal_at is None
                else _time_text(record.terminal_at, field="terminal_at")
            ),
            "revision": record.revision,
        }

    @classmethod
    def _recovery_command_from_row(cls, row: Mapping[str, Any]) -> RecoveryCommand:
        record = RecoveryCommand(
            recovery_command_id=str(row["recovery_command_id"]),
            permit_id=str(row["permit_id"]),
            parent_command_id=str(row["parent_command_id"]),
            incident_id=str(row["incident_id"]),
            kind=str(row["kind"]),
            priority=int(row["priority"]),
            source_hash=str(row["source_hash"]),
            preflight_hash=(
                None if row["preflight_hash"] is None else str(row["preflight_hash"])
            ),
            recovery_hash=str(row["recovery_hash"]),
            recovery_material_json=str(row["recovery_material_json"]),
            recovery_material_hash=str(row["recovery_material_hash"]),
            safety_policy_hash=str(row["safety_policy_hash"]),
            original_attempt_id=(
                None
                if row["original_attempt_id"] is None
                else str(row["original_attempt_id"])
            ),
            original_nonce=(
                None if row["original_nonce"] is None else int(row["original_nonce"])
            ),
            state=str(row["state"]),
            created_at=_parse_time(row["created_at"], field="recovery created_at"),
            updated_at=_parse_time(row["updated_at"], field="recovery updated_at"),
            terminal_at=_optional_time(
                row["terminal_at"], field="recovery terminal_at"
            ),
            revision=int(row["revision"]),
        )
        if (
            record.kind not in _RECOVERY_KINDS
            or record.state not in _RECOVERY_COMMAND_STATES
            or record.priority != _RECOVERY_PRIORITY[record.kind]
        ):
            raise StorageError("persisted recovery command state is invalid")
        calculated_material_hash = hashlib.sha256(
            record.recovery_material_json.encode("utf-8")
        ).hexdigest()
        if calculated_material_hash != _stored_hash(
            record.recovery_material_hash,
            field="recovery_material_hash",
        ):
            raise StorageError("persisted recovery material content hash differs")
        try:
            recovery_material = json.loads(record.recovery_material_json)
        except ValueError as error:
            raise StorageError("persisted recovery material is invalid JSON") from error
        if canonical_json(recovery_material) != record.recovery_material_json:
            raise StorageError("persisted recovery material is not canonical")
        if domain_hash(
            "trading-harness/hyperliquid-recovery-action/v1", recovery_material
        ) != record.recovery_hash:
            raise StorageError("persisted recovery material hash differs")
        if _stored_hash(row["record_hash"], field="recovery command hash") != _record_hash(
            "recovery-command", cls._recovery_command_material(record)
        ):
            raise StorageError("persisted recovery command hash does not match")
        return record

    def queue_recovery(
        self,
        *,
        recovery_command_id: str,
        permit_id: str,
        token_hash: str,
        audience: str,
        at: datetime,
    ) -> RecoveryCommand:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_permit = _text(permit_id, field="permit_id", maximum=128)
        checked_token = _hash(token_hash, field="token_hash")
        checked_audience = _text(audience, field="audience", maximum=256)
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_recovery_permits WHERE permit_id = ?",
                (checked_permit,),
            ).fetchone()
            if row is None:
                raise AdmissionDenied("RECOVERY_PERMIT_NOT_FOUND", "permit missing")
            permit = self._recovery_permit_from_row(row)
            if row["state"] != "issued":
                raise AdmissionDenied("RECOVERY_PERMIT_USED", "permit is not issued")
            if row["token_hash"] != checked_token or row["audience"] != checked_audience:
                raise AdmissionDenied("RECOVERY_PERMIT_TOKEN_MISMATCH", "permit differs")
            if not permit.issued_at <= checked_at < permit.expires_at:
                raise AdmissionDenied("RECOVERY_PERMIT_EXPIRED", "permit is inactive")
            incident_row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (permit.incident_id,),
            ).fetchone()
            if incident_row is None:
                raise StorageError("recovery permit incident disappeared")
            incident = self._incident_from_row(incident_row)
            if incident.state != "open" or incident.severity != "critical":
                raise AdmissionDenied(
                    "RECOVERY_INCIDENT_INACTIVE", "incident is not open critical"
                )
            if connection.execute(
                """
                SELECT 1 FROM execution_recovery_commands
                WHERE state != 'terminal' LIMIT 1
                """,
            ).fetchone() is not None:
                raise StateConflict("account already has an active recovery command")
            permit_material = self._recovery_permit_material(
                permit,
                state="consumed",
                recovery_command_id=checked_id,
                updated_at=checked_at,
            )
            connection.execute(
                """
                UPDATE execution_recovery_permits SET
                    state = 'consumed', recovery_command_id = ?, updated_at = ?,
                    record_hash = ? WHERE permit_id = ? AND state = 'issued'
                """,
                (
                    checked_id,
                    _time_text(checked_at, field="updated_at"),
                    _record_hash("recovery-permit", permit_material),
                    permit.permit_id,
                ),
            )
            command = RecoveryCommand(
                recovery_command_id=checked_id,
                permit_id=permit.permit_id,
                parent_command_id=permit.parent_command_id,
                incident_id=permit.incident_id,
                kind=permit.kind,
                priority=_RECOVERY_PRIORITY[permit.kind],
                source_hash=permit.source_hash,
                preflight_hash=permit.preflight_hash,
                recovery_hash=permit.recovery_hash,
                recovery_material_json=canonical_json(dict(permit.recovery_material)),
                recovery_material_hash=hashlib.sha256(
                    canonical_json(dict(permit.recovery_material)).encode("utf-8")
                ).hexdigest(),
                safety_policy_hash=permit.safety_policy_hash,
                original_attempt_id=permit.original_attempt_id,
                original_nonce=permit.original_nonce,
                state="queued",
                created_at=checked_at,
                updated_at=checked_at,
                terminal_at=None,
                revision=1,
            )
            connection.execute(
                """
                INSERT INTO execution_recovery_commands (
                    recovery_command_id, permit_id, parent_command_id,
                    incident_id, kind, priority, source_hash, preflight_hash,
                    recovery_hash, recovery_material_json,
                    recovery_material_hash, safety_policy_hash, original_attempt_id,
                    original_nonce, state, created_at, updated_at, terminal_at,
                    revision, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'queued', ?, ?, NULL, 1, ?)
                """,
                (
                    command.recovery_command_id,
                    command.permit_id,
                    command.parent_command_id,
                    command.incident_id,
                    command.kind,
                    command.priority,
                    command.source_hash,
                    command.preflight_hash,
                    command.recovery_hash,
                    command.recovery_material_json,
                    command.recovery_material_hash,
                    command.safety_policy_hash,
                    command.original_attempt_id,
                    command.original_nonce,
                    _time_text(checked_at, field="created_at"),
                    _time_text(checked_at, field="updated_at"),
                    _record_hash(
                        "recovery-command", self._recovery_command_material(command)
                    ),
                ),
            )
            outbox_material = {
                "recovery_command_id": checked_id,
                "state": "queued",
                "worker_id": None,
                "fencing_token": 0,
                "claimed_at": None,
                "lease_expires_at": None,
                "current_attempt_id": None,
                "attempt_count": 0,
                "created_at": _time_text(checked_at, field="created_at"),
                "updated_at": _time_text(checked_at, field="updated_at"),
            }
            connection.execute(
                """
                INSERT INTO execution_recovery_outbox (
                    recovery_command_id, state, worker_id, fencing_token,
                    claimed_at, lease_expires_at, current_attempt_id,
                    attempt_count, created_at, updated_at, record_hash
                ) VALUES (?, 'queued', NULL, 0, NULL, NULL, NULL, 0, ?, ?, ?)
                """,
                (
                    checked_id,
                    _time_text(checked_at, field="created_at"),
                    _time_text(checked_at, field="updated_at"),
                    _record_hash("recovery-outbox", outbox_material),
                ),
            )
            self._append_event_locked(
                connection,
                command_id=permit.parent_command_id,
                event_type="recovery_command_queued",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": checked_id,
                    "kind": permit.kind,
                    "priority": command.priority,
                    "incident_id": permit.incident_id,
                },
            )
            return command

    @staticmethod
    def _recovery_outbox_material(record: RecoveryOutbox) -> dict[str, object]:
        return {
            "recovery_command_id": record.recovery_command_id,
            "state": record.state,
            "worker_id": record.worker_id,
            "fencing_token": record.fencing_token,
            "claimed_at": (
                None
                if record.claimed_at is None
                else _time_text(record.claimed_at, field="claimed_at")
            ),
            "lease_expires_at": (
                None
                if record.lease_expires_at is None
                else _time_text(record.lease_expires_at, field="lease_expires_at")
            ),
            "current_attempt_id": record.current_attempt_id,
            "attempt_count": record.attempt_count,
            "created_at": _time_text(record.created_at, field="created_at"),
            "updated_at": _time_text(record.updated_at, field="updated_at"),
        }

    @classmethod
    def _recovery_outbox_from_row(cls, row: Mapping[str, Any]) -> RecoveryOutbox:
        record = RecoveryOutbox(
            recovery_command_id=str(row["recovery_command_id"]),
            state=str(row["state"]),
            worker_id=(None if row["worker_id"] is None else str(row["worker_id"])),
            fencing_token=int(row["fencing_token"]),
            claimed_at=_optional_time(row["claimed_at"], field="claimed_at"),
            lease_expires_at=_optional_time(
                row["lease_expires_at"], field="lease_expires_at"
            ),
            current_attempt_id=(
                None
                if row["current_attempt_id"] is None
                else str(row["current_attempt_id"])
            ),
            attempt_count=int(row["attempt_count"]),
            created_at=_parse_time(row["created_at"], field="recovery outbox created_at"),
            updated_at=_parse_time(row["updated_at"], field="recovery outbox updated_at"),
        )
        if record.state not in _RECOVERY_COMMAND_STATES:
            raise StorageError("persisted recovery outbox state is invalid")
        if _stored_hash(row["record_hash"], field="recovery outbox hash") != _record_hash(
            "recovery-outbox", cls._recovery_outbox_material(record)
        ):
            raise StorageError("persisted recovery outbox hash does not match")
        return record

    def _set_recovery_command_state_locked(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        state: str,
        at: datetime,
        terminal: bool = False,
    ) -> RecoveryCommand:
        current = self._recovery_command_from_row(row)
        updated = RecoveryCommand(
            recovery_command_id=current.recovery_command_id,
            permit_id=current.permit_id,
            parent_command_id=current.parent_command_id,
            incident_id=current.incident_id,
            kind=current.kind,
            priority=current.priority,
            source_hash=current.source_hash,
            preflight_hash=current.preflight_hash,
            recovery_hash=current.recovery_hash,
            recovery_material_json=current.recovery_material_json,
            recovery_material_hash=current.recovery_material_hash,
            safety_policy_hash=current.safety_policy_hash,
            original_attempt_id=current.original_attempt_id,
            original_nonce=current.original_nonce,
            state=state,
            created_at=current.created_at,
            updated_at=at,
            terminal_at=at if terminal else current.terminal_at,
            revision=current.revision + 1,
        )
        connection.execute(
            """
            UPDATE execution_recovery_commands SET
                state = ?, updated_at = ?, terminal_at = ?, revision = ?,
                record_hash = ? WHERE recovery_command_id = ? AND revision = ?
            """,
            (
                state,
                _time_text(at, field="updated_at"),
                None
                if updated.terminal_at is None
                else _time_text(updated.terminal_at, field="terminal_at"),
                updated.revision,
                _record_hash(
                    "recovery-command", self._recovery_command_material(updated)
                ),
                current.recovery_command_id,
                current.revision,
            ),
        )
        return updated

    def _set_recovery_outbox_locked(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        state: str,
        worker_id: str | None,
        fencing_token: int,
        claimed_at: datetime | None,
        lease_expires_at: datetime | None,
        current_attempt_id: str | None,
        attempt_count: int,
        at: datetime,
    ) -> RecoveryOutbox:
        current = self._recovery_outbox_from_row(row)
        updated = RecoveryOutbox(
            recovery_command_id=current.recovery_command_id,
            state=state,
            worker_id=worker_id,
            fencing_token=fencing_token,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            current_attempt_id=current_attempt_id,
            attempt_count=attempt_count,
            created_at=current.created_at,
            updated_at=at,
        )
        connection.execute(
            """
            UPDATE execution_recovery_outbox SET
                state = ?, worker_id = ?, fencing_token = ?, claimed_at = ?,
                lease_expires_at = ?, current_attempt_id = ?, attempt_count = ?,
                updated_at = ?, record_hash = ? WHERE recovery_command_id = ?
            """,
            (
                state,
                worker_id,
                fencing_token,
                None if claimed_at is None else _time_text(claimed_at, field="claimed_at"),
                None
                if lease_expires_at is None
                else _time_text(lease_expires_at, field="lease_expires_at"),
                current_attempt_id,
                attempt_count,
                _time_text(at, field="updated_at"),
                _record_hash("recovery-outbox", self._recovery_outbox_material(updated)),
                current.recovery_command_id,
            ),
        )
        return updated

    def get_recovery_command(self, recovery_command_id: str) -> RecoveryCommand:
        checked = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("recovery command is not registered")
        return self._recovery_command_from_row(row)

    def list_recovery_commands(
        self,
        *,
        active_only: bool = False,
    ) -> tuple[RecoveryCommand, ...]:
        if type(active_only) is not bool:
            raise TypeError("active_only must be bool")
        connection = self._connect()
        try:
            rows = connection.execute(
                (
                    "SELECT * FROM execution_recovery_commands "
                    "WHERE state != 'terminal' ORDER BY priority, created_at, recovery_command_id"
                    if active_only
                    else "SELECT * FROM execution_recovery_commands ORDER BY created_at, recovery_command_id"
                )
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._recovery_command_from_row(row) for row in rows)

    def get_recovery_outbox(self, recovery_command_id: str) -> RecoveryOutbox:
        checked = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_outbox
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("recovery outbox is not registered")
        return self._recovery_outbox_from_row(row)

    def list_recovery_outboxes(
        self,
        *,
        active_only: bool = False,
    ) -> tuple[RecoveryOutbox, ...]:
        if type(active_only) is not bool:
            raise TypeError("active_only must be bool")
        connection = self._connect()
        try:
            rows = connection.execute(
                (
                    "SELECT * FROM execution_recovery_outbox WHERE state != 'terminal' "
                    "ORDER BY created_at, recovery_command_id"
                    if active_only
                    else "SELECT * FROM execution_recovery_outbox "
                    "ORDER BY created_at, recovery_command_id"
                )
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._recovery_outbox_from_row(row) for row in rows)

    def _normalize_expired_recovery_locked(
        self, connection: sqlite3.Connection, *, at: datetime
    ) -> None:
        rows = connection.execute(
            """
            SELECT * FROM execution_recovery_outbox
            WHERE state IN ('claimed', 'signing') AND lease_expires_at <= ?
            """,
            (_time_text(at, field="at"),),
        ).fetchall()
        for row in rows:
            outbox = self._recovery_outbox_from_row(row)
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (outbox.recovery_command_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("expired recovery claim has no command")
            if outbox.current_attempt_id is None:
                if outbox.state == "claimed":
                    self._set_recovery_outbox_locked(
                        connection,
                        row,
                        state="queued",
                        worker_id=None,
                        fencing_token=outbox.fencing_token,
                        claimed_at=None,
                        lease_expires_at=None,
                        current_attempt_id=None,
                        attempt_count=outbox.attempt_count,
                        at=at,
                    )
                    self._set_recovery_command_state_locked(
                        connection, command_row, state="queued", at=at
                    )
                else:
                    # Transport is ordered strictly after durable attempt
                    # creation.  No attempt therefore proves no venue write,
                    # even if signing burned a nonce.  Terminalize this exact
                    # authority and let safety issue a fresh short-lived one.
                    command = self._set_recovery_command_state_locked(
                        connection,
                        command_row,
                        state="terminal",
                        at=at,
                        terminal=True,
                    )
                    self._set_recovery_outbox_locked(
                        connection,
                        row,
                        state="terminal",
                        worker_id=None,
                        fencing_token=outbox.fencing_token,
                        claimed_at=None,
                        lease_expires_at=None,
                        current_attempt_id=None,
                        attempt_count=outbox.attempt_count,
                        at=at,
                    )
                    self._append_event_locked(
                        connection,
                        command_id=command.parent_command_id,
                        event_type="recovery_signing_expired_unsent",
                        occurred_at=at,
                        payload={
                            "recovery_command_id": command.recovery_command_id,
                            "venue_write_attempted": False,
                            "nonce_may_have_been_burned": True,
                            "replacement_permit_required": True,
                        },
                    )
            else:
                attempt_row = connection.execute(
                    """
                    SELECT * FROM execution_recovery_attempts WHERE attempt_id = ?
                    """,
                    (outbox.current_attempt_id,),
                ).fetchone()
                if attempt_row is None:
                    raise StorageError("recovery outbox attempt is missing")
                attempt = self._recovery_attempt_from_row(attempt_row)
                if attempt.state == "prepared":
                    command = self._set_recovery_command_state_locked(
                        connection,
                        command_row,
                        state="terminal",
                        at=at,
                        terminal=True,
                    )
                    self._set_recovery_outbox_locked(
                        connection,
                        row,
                        state="terminal",
                        worker_id=None,
                        fencing_token=outbox.fencing_token,
                        claimed_at=None,
                        lease_expires_at=None,
                        current_attempt_id=attempt.attempt_id,
                        attempt_count=outbox.attempt_count,
                        at=at,
                    )
                    self._append_event_locked(
                        connection,
                        command_id=command.parent_command_id,
                        event_type="recovery_attempt_expired_proven_unsent",
                        occurred_at=at,
                        payload={
                            "recovery_command_id": command.recovery_command_id,
                            "attempt_id": attempt.attempt_id,
                            "submission_authority_consumed": False,
                            "venue_write_attempted": False,
                            "replacement_permit_required": True,
                        },
                    )
                    continue
                if attempt.state != "sending":
                    raise StorageError(
                        "expired recovery attempt is not prepared or sending"
                    )
                evidence = TransportOutcomeEvidence(
                    command_id=attempt.recovery_command_id,
                    attempt_id=attempt.attempt_id,
                    signed_evidence_hash=attempt.signed_evidence_hash,
                    endpoint="https://api.hyperliquid-testnet.xyz/exchange",
                    attempted_at_ms=int(at.timestamp() * 1_000),
                    outcome="unknown",
                    http_status=None,
                    detail_code="recovery_worker_lease_expired_after_prepare",
                    response_hash=None,
                    transport_attempt_hash=None,
                    send_count=None,
                    retry_performed=False,
                    venue_write_attempted=None,
                    evidence_basis="claim_expiry",
                )
                self._put_recovery_transport_locked(connection, evidence, at=at)
                self._update_recovery_attempt_locked(
                    connection,
                    attempt,
                    state="unknown",
                    transport_evidence_hash=evidence.evidence_hash,
                    at=at,
                )
                self._set_recovery_outbox_locked(
                    connection,
                    row,
                    state="submitted_unknown",
                    worker_id=None,
                    fencing_token=outbox.fencing_token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=attempt.attempt_id,
                    attempt_count=outbox.attempt_count,
                    at=at,
                )
                self._set_recovery_command_state_locked(
                    connection, command_row, state="submitted_unknown", at=at
                )
                command = self._recovery_command_from_row(command_row)
                self._append_event_locked(
                    connection,
                    command_id=command.parent_command_id,
                    event_type="recovery_prepared_attempt_became_unknown",
                    occurred_at=at,
                    payload={
                        "recovery_command_id": command.recovery_command_id,
                        "attempt_id": attempt.attempt_id,
                        "transport_evidence_hash": evidence.evidence_hash,
                        "evidence_basis": "claim_expiry",
                        "retry_allowed": False,
                    },
                )

    def _terminalize_expired_queued_recovery_locked(
        self,
        connection: sqlite3.Connection,
        *,
        at: datetime,
    ) -> None:
        rows = connection.execute(
            """
            SELECT outbox.* FROM execution_recovery_outbox AS outbox
            JOIN execution_recovery_commands AS command
              ON command.recovery_command_id = outbox.recovery_command_id
            JOIN execution_recovery_permits AS permit
              ON permit.permit_id = command.permit_id
            WHERE outbox.state = 'queued' AND permit.expires_at <= ?
            """,
            (_time_text(at, field="at"),),
        ).fetchall()
        for row in rows:
            outbox = self._recovery_outbox_from_row(row)
            if outbox.current_attempt_id is not None or outbox.attempt_count != 0:
                raise StorageError("queued expired recovery unexpectedly has an attempt")
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (outbox.recovery_command_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("expired recovery command is missing")
            command = self._set_recovery_command_state_locked(
                connection,
                command_row,
                state="terminal",
                at=at,
                terminal=True,
            )
            self._set_recovery_outbox_locked(
                connection,
                row,
                state="terminal",
                worker_id=None,
                fencing_token=outbox.fencing_token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=None,
                attempt_count=0,
                at=at,
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_expired_unsent",
                occurred_at=at,
                payload={
                    "recovery_command_id": command.recovery_command_id,
                    "incident_id": command.incident_id,
                    "venue_write_attempted": False,
                    "replacement_permit_required": True,
                },
            )

    def normalize_expired_claims(self, *, at: datetime) -> bool:
        """Durably classify expired parent/recovery claims before routing.

        A worker crash after attempt persistence has an unknown venue outcome,
        never a retryable send.  This public state-machine operation lets the
        runtime normalize that fact before a reconciliation adapter tries to
        load transport evidence.  It performs no network, signing, or claim.
        """

        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            before = int(
                connection.execute(
                    "SELECT count(*) FROM execution_events"
                ).fetchone()[0]
            )
            self._normalize_expired_claims_locked(connection, at=checked_at)
            self._normalize_expired_recovery_locked(connection, at=checked_at)
            self._terminalize_expired_queued_recovery_locked(
                connection, at=checked_at
            )
            after = int(
                connection.execute(
                    "SELECT count(*) FROM execution_events"
                ).fetchone()[0]
            )
            return after != before

    def claim_next_recovery(
        self,
        worker_id: str,
        *,
        at: datetime,
        lease_seconds: int,
    ) -> RecoveryOutbox | None:
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        checked_at = _utc(at, field="at")
        lease = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
        with self._transaction() as connection:
            self._normalize_expired_recovery_locked(connection, at=checked_at)
            self._terminalize_expired_queued_recovery_locked(
                connection,
                at=checked_at,
            )
            row = connection.execute(
                """
                SELECT outbox.* FROM execution_recovery_outbox AS outbox
                JOIN execution_recovery_commands AS command
                  ON command.recovery_command_id = outbox.recovery_command_id
                WHERE outbox.state = 'queued'
                ORDER BY command.priority, command.created_at,
                         command.recovery_command_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            current = self._recovery_outbox_from_row(row)
            expires = checked_at + timedelta(seconds=lease)
            claimed = self._set_recovery_outbox_locked(
                connection,
                row,
                state="claimed",
                worker_id=checked_worker,
                fencing_token=current.fencing_token + 1,
                claimed_at=checked_at,
                lease_expires_at=expires,
                current_attempt_id=None,
                attempt_count=current.attempt_count,
                at=checked_at,
            )
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (current.recovery_command_id,),
            ).fetchone()
            self._set_recovery_command_state_locked(
                connection, command_row, state="claimed", at=checked_at
            )
            self._append_event_locked(
                connection,
                command_id=self._recovery_command_from_row(command_row).parent_command_id,
                event_type="recovery_claimed",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": current.recovery_command_id,
                    "worker_id": checked_worker,
                    "fencing_token": claimed.fencing_token,
                },
            )
            return claimed

    @staticmethod
    def _signed_recovery_material(
        evidence: SignedRecoveryEvidence,
        *,
        recorded_at: datetime,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            **evidence.as_dict(),
            "recorded_at": _time_text(recorded_at, field="recorded_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }

    def _put_signed_recovery_locked(
        self,
        connection: sqlite3.Connection,
        evidence: SignedRecoveryEvidence,
        *,
        at: datetime,
    ) -> None:
        payload_json, content_hash = _canonical_payload(evidence.as_dict())
        record_hash = _record_hash(
            "signed-recovery-record",
            self._signed_recovery_material(
                evidence,
                recorded_at=at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        existing = connection.execute(
            """
            SELECT * FROM execution_signed_recovery_evidence
            WHERE recovery_command_id = ?
            """,
            (evidence.recovery_command_id,),
        ).fetchone()
        if existing is not None:
            if existing["evidence_hash"] == evidence.evidence_hash:
                return
            raise StateConflict("recovery command cannot swap signed evidence")
        connection.execute(
            """
            INSERT INTO execution_signed_recovery_evidence (
                evidence_hash, recovery_command_id, incident_id, kind,
                source_hash, recovery_hash, safety_policy_hash, nonce,
                signing_authority_hash, wire_hash, action_hash, signature_hash, envelope_hash,
                signer_binding_hash, expires_after_ms, signed_at_ms,
                recorded_at, payload_json, content_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_hash,
                evidence.recovery_command_id,
                evidence.incident_id,
                evidence.kind,
                evidence.source_hash,
                evidence.recovery_hash,
                evidence.safety_policy_hash,
                evidence.nonce,
                evidence.signing_authority_hash,
                evidence.wire_hash,
                evidence.action_hash,
                evidence.signature_hash,
                evidence.envelope_hash,
                evidence.signer_binding_hash,
                evidence.expires_after_ms,
                evidence.signed_at_ms,
                _time_text(at, field="recorded_at"),
                payload_json,
                content_hash,
                record_hash,
            ),
        )

    @staticmethod
    def _recovery_attempt_material(record: RecoveryAttempt) -> dict[str, object]:
        return {
            "attempt_id": record.attempt_id,
            "recovery_command_id": record.recovery_command_id,
            "worker_id": record.worker_id,
            "fencing_token": record.fencing_token,
            "signed_evidence_hash": record.signed_evidence_hash,
            "transport_evidence_hash": record.transport_evidence_hash,
            "nonce": record.nonce,
            "action_hash": record.action_hash,
            "wire_hash": record.wire_hash,
            "state": record.state,
            "prepared_at": _time_text(record.prepared_at, field="prepared_at"),
            "updated_at": _time_text(record.updated_at, field="updated_at"),
        }

    @classmethod
    def _recovery_attempt_from_row(cls, row: Mapping[str, Any]) -> RecoveryAttempt:
        record = RecoveryAttempt(
            attempt_id=str(row["attempt_id"]),
            recovery_command_id=str(row["recovery_command_id"]),
            worker_id=str(row["worker_id"]),
            fencing_token=int(row["fencing_token"]),
            signed_evidence_hash=str(row["signed_evidence_hash"]),
            transport_evidence_hash=(
                None
                if row["transport_evidence_hash"] is None
                else str(row["transport_evidence_hash"])
            ),
            nonce=int(row["nonce"]),
            action_hash=str(row["action_hash"]),
            wire_hash=str(row["wire_hash"]),
            state=str(row["state"]),
            prepared_at=_parse_time(row["prepared_at"], field="recovery prepared_at"),
            updated_at=_parse_time(row["updated_at"], field="recovery updated_at"),
        )
        if record.state not in _RECOVERY_ATTEMPT_STATES:
            raise StorageError("persisted recovery attempt state is invalid")
        for value, field in (
            (record.signed_evidence_hash, "signed_evidence_hash"),
            (record.action_hash, "action_hash"),
            (record.wire_hash, "wire_hash"),
        ):
            _stored_hash(value, field=field)
        if _stored_hash(row["record_hash"], field="recovery attempt hash") != _record_hash(
            "recovery-attempt", cls._recovery_attempt_material(record)
        ):
            raise StorageError("persisted recovery attempt hash does not match")
        return record

    def _update_recovery_attempt_locked(
        self,
        connection: sqlite3.Connection,
        attempt: RecoveryAttempt,
        *,
        state: str,
        transport_evidence_hash: str | None,
        at: datetime,
    ) -> RecoveryAttempt:
        updated = RecoveryAttempt(
            attempt_id=attempt.attempt_id,
            recovery_command_id=attempt.recovery_command_id,
            worker_id=attempt.worker_id,
            fencing_token=attempt.fencing_token,
            signed_evidence_hash=attempt.signed_evidence_hash,
            transport_evidence_hash=transport_evidence_hash,
            nonce=attempt.nonce,
            action_hash=attempt.action_hash,
            wire_hash=attempt.wire_hash,
            state=state,
            prepared_at=attempt.prepared_at,
            updated_at=at,
        )
        connection.execute(
            """
            UPDATE execution_recovery_attempts SET
                state = ?, transport_evidence_hash = ?, updated_at = ?,
                record_hash = ? WHERE attempt_id = ?
            """,
            (
                state,
                transport_evidence_hash,
                _time_text(at, field="updated_at"),
                _record_hash(
                    "recovery-attempt", self._recovery_attempt_material(updated)
                ),
                attempt.attempt_id,
            ),
        )
        return updated

    def require_recovery_signing_authority(
        self,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
    ) -> RecoverySigningAuthority:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            outbox, outbox_row = self._require_recovery_claim_locked(
                connection,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                states={"claimed"},
            )
            if outbox.current_attempt_id is not None or outbox.attempt_count != 0:
                raise StateConflict("recovery signing authority is already consumed")
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            command = self._recovery_command_from_row(command_row)
            permit_row = connection.execute(
                "SELECT * FROM execution_recovery_permits WHERE permit_id = ?",
                (command.permit_id,),
            ).fetchone()
            permit = self._recovery_permit_from_row(permit_row)
            if permit_row["state"] != "consumed" or checked_at >= permit.expires_at:
                raise StateConflict("consumed recovery permit is missing or expired")
            incident_row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (command.incident_id,),
            ).fetchone()
            incident = self._incident_from_row(incident_row)
            if incident.state != "open" or incident.severity != "critical":
                raise StateConflict("recovery signing requires open critical incident")
            if command.kind == "noop_fence":
                attempt_row = connection.execute(
                    "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                    (command.original_attempt_id,),
                ).fetchone()
                attempt = self._attempt_from_row(attempt_row)
                if (
                    attempt.state != "unknown"
                    or attempt.nonce != command.original_nonce
                    or attempt.preflight_hash != command.preflight_hash
                ):
                    raise StateConflict("noop signing authority lost original attempt")
            material = {
                "recovery_command_id": command.recovery_command_id,
                "permit_id": command.permit_id,
                "parent_command_id": command.parent_command_id,
                "incident_id": command.incident_id,
                "kind": command.kind,
                "source_hash": command.source_hash,
                "preflight_hash": command.preflight_hash,
                "recovery_hash": command.recovery_hash,
                "safety_policy_hash": command.safety_policy_hash,
                "original_attempt_id": command.original_attempt_id,
                "original_nonce": command.original_nonce,
                "worker_id": checked_worker,
                "fencing_token": token,
                "permit_expires_at": _time_text(
                    permit.expires_at, field="permit_expires_at"
                ),
                "lease_expires_at": _time_text(
                    outbox.lease_expires_at, field="lease_expires_at"
                ),
            }
            authority_hash = _record_hash("recovery-signing-authority", material)
            authority = RecoverySigningAuthority(
                recovery_command_id=command.recovery_command_id,
                permit_id=command.permit_id,
                parent_command_id=command.parent_command_id,
                incident_id=command.incident_id,
                kind=command.kind,
                source_hash=command.source_hash,
                preflight_hash=command.preflight_hash,
                recovery_hash=command.recovery_hash,
                safety_policy_hash=command.safety_policy_hash,
                original_attempt_id=command.original_attempt_id,
                original_nonce=command.original_nonce,
                worker_id=checked_worker,
                fencing_token=token,
                permit_expires_at=permit.expires_at,
                lease_expires_at=outbox.lease_expires_at,
                authority_hash=authority_hash,
            )
            payload_json, content_hash = _canonical_payload(
                {**material, "authority_hash": authority_hash}
            )
            connection.execute(
                """
                INSERT INTO execution_recovery_signing_authorities (
                    authority_hash, recovery_command_id, worker_id,
                    fencing_token, issued_at, payload_json, content_hash,
                    record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authority_hash,
                    checked_id,
                    checked_worker,
                    token,
                    _time_text(checked_at, field="issued_at"),
                    payload_json,
                    content_hash,
                    _record_hash(
                        "recovery-signing-authority-record",
                        {
                            "authority_hash": authority_hash,
                            "payload_json": payload_json,
                            "content_hash": content_hash,
                        },
                    ),
                ),
            )
            self._set_recovery_outbox_locked(
                connection,
                outbox_row,
                state="signing",
                worker_id=checked_worker,
                fencing_token=token,
                claimed_at=outbox.claimed_at,
                lease_expires_at=outbox.lease_expires_at,
                current_attempt_id=None,
                attempt_count=0,
                at=checked_at,
            )
            self._set_recovery_command_state_locked(
                connection, command_row, state="signing", at=checked_at
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_signing_authority_issued",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": checked_id,
                    "authority_hash": authority_hash,
                    "single_use": True,
                },
            )
            return authority

    def require_recovery_submission_authority(
        self,
        recovery_command_id: str,
        attempt_id: str,
        signed_evidence_hash: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
    ) -> RecoverySubmissionAuthority:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_attempt = _text(attempt_id, field="attempt_id", maximum=128)
        checked_signed = _hash(
            signed_evidence_hash, field="signed_evidence_hash"
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            outbox, _ = self._require_recovery_claim_locked(
                connection,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                states={"signing"},
            )
            if outbox.current_attempt_id != checked_attempt:
                raise StateConflict("recovery submission attempt differs from outbox")
            attempt_row = connection.execute(
                "SELECT * FROM execution_recovery_attempts WHERE attempt_id = ?",
                (checked_attempt,),
            ).fetchone()
            attempt = self._recovery_attempt_from_row(attempt_row)
            if (
                attempt.state != "prepared"
                or attempt.signed_evidence_hash != checked_signed
                or attempt.transport_evidence_hash is not None
            ):
                raise StateConflict("recovery attempt is not submission-ready")
            if connection.execute(
                """
                SELECT 1 FROM execution_recovery_transport_evidence
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone() is not None:
                raise StateConflict("recovery already has transport evidence")
            self._update_recovery_attempt_locked(
                connection,
                attempt,
                state="sending",
                transport_evidence_hash=None,
                at=checked_at,
            )
            material = {
                "recovery_command_id": checked_id,
                "attempt_id": checked_attempt,
                "signed_evidence_hash": checked_signed,
                "nonce": attempt.nonce,
                "action_hash": attempt.action_hash,
                "wire_hash": attempt.wire_hash,
                "worker_id": checked_worker,
                "fencing_token": token,
                "lease_expires_at": _time_text(
                    outbox.lease_expires_at, field="lease_expires_at"
                ),
            }
            return RecoverySubmissionAuthority(
                recovery_command_id=checked_id,
                attempt_id=checked_attempt,
                signed_evidence_hash=checked_signed,
                nonce=attempt.nonce,
                action_hash=attempt.action_hash,
                wire_hash=attempt.wire_hash,
                worker_id=checked_worker,
                fencing_token=token,
                lease_expires_at=outbox.lease_expires_at,
                authority_hash=_record_hash(
                    "recovery-submission-authority", material
                ),
            )

    def _require_recovery_claim_locked(
        self,
        connection: sqlite3.Connection,
        *,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        at: datetime,
        states: set[str],
    ) -> tuple[RecoveryOutbox, sqlite3.Row]:
        row = connection.execute(
            """
            SELECT * FROM execution_recovery_outbox
            WHERE recovery_command_id = ?
            """,
            (recovery_command_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFound("recovery outbox is missing")
        outbox = self._recovery_outbox_from_row(row)
        if (
            outbox.state not in states
            or outbox.worker_id != worker_id
            or outbox.fencing_token != fencing_token
            or outbox.claimed_at is None
            or outbox.lease_expires_at is None
            or not outbox.claimed_at <= at < outbox.lease_expires_at
        ):
            raise StateConflict("recovery claim is stale, expired, or wrong state")
        return outbox, row

    def prepare_recovery_attempt(
        self,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        attempt_id: str,
        signed_evidence: SignedRecoveryEvidence,
        at: datetime,
    ) -> RecoveryAttempt:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        checked_attempt = _text(attempt_id, field="attempt_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        if not isinstance(signed_evidence, SignedRecoveryEvidence):
            raise TypeError("signed_evidence must be SignedRecoveryEvidence")
        with self._transaction() as connection:
            outbox, outbox_row = self._require_recovery_claim_locked(
                connection,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                states={"signing"},
            )
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("recovery command disappeared")
            command = self._recovery_command_from_row(command_row)
            permit_row = connection.execute(
                "SELECT * FROM execution_recovery_permits WHERE permit_id = ?",
                (command.permit_id,),
            ).fetchone()
            if permit_row is None:
                raise StorageError("recovery permit disappeared")
            permit = self._recovery_permit_from_row(permit_row)
            if checked_at >= permit.expires_at:
                raise AdmissionDenied("RECOVERY_PERMIT_EXPIRED", "permit expired before signing")
            if (
                signed_evidence.recovery_command_id != checked_id
                or signed_evidence.incident_id != command.incident_id
                or signed_evidence.kind != command.kind
                or signed_evidence.source_hash != command.source_hash
                or signed_evidence.recovery_hash != command.recovery_hash
                or signed_evidence.safety_policy_hash != command.safety_policy_hash
            ):
                raise StateConflict("signed recovery evidence differs from durable command")
            authority_row = connection.execute(
                """
                SELECT * FROM execution_recovery_signing_authorities
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if (
                authority_row is None
                or authority_row["authority_hash"]
                != signed_evidence.signing_authority_hash
            ):
                raise StateConflict("signed recovery lacks consumed signing authority")
            if signed_evidence.expires_after_ms > int(permit.expires_at.timestamp() * 1_000):
                raise AdmissionDenied(
                    "SIGNED_RECOVERY_OUTLIVES_PERMIT",
                    "signed recovery expiry exceeds permit",
                )
            if command.kind == "noop_fence" and signed_evidence.nonce != command.original_nonce:
                raise StateConflict("noop recovery changed the original unknown nonce")
            if connection.execute(
                """
                SELECT 1 FROM execution_recovery_attempts
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone() is not None:
                raise StateConflict("recovery command already has an attempt; retry forbidden")
            self._put_signed_recovery_locked(
                connection, signed_evidence, at=checked_at
            )
            attempt = RecoveryAttempt(
                attempt_id=checked_attempt,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                signed_evidence_hash=signed_evidence.evidence_hash,
                transport_evidence_hash=None,
                nonce=signed_evidence.nonce,
                action_hash=signed_evidence.action_hash,
                wire_hash=signed_evidence.wire_hash,
                state="prepared",
                prepared_at=checked_at,
                updated_at=checked_at,
            )
            connection.execute(
                """
                INSERT INTO execution_recovery_attempts (
                    attempt_id, recovery_command_id, worker_id, fencing_token,
                    signed_evidence_hash, transport_evidence_hash, nonce,
                    action_hash, wire_hash, state, prepared_at, updated_at,
                    record_hash
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'prepared', ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    attempt.recovery_command_id,
                    attempt.worker_id,
                    attempt.fencing_token,
                    attempt.signed_evidence_hash,
                    attempt.nonce,
                    attempt.action_hash,
                    attempt.wire_hash,
                    _time_text(checked_at, field="prepared_at"),
                    _time_text(checked_at, field="updated_at"),
                    _record_hash(
                        "recovery-attempt", self._recovery_attempt_material(attempt)
                    ),
                ),
            )
            self._set_recovery_outbox_locked(
                connection,
                outbox_row,
                state="signing",
                worker_id=checked_worker,
                fencing_token=token,
                claimed_at=outbox.claimed_at,
                lease_expires_at=outbox.lease_expires_at,
                current_attempt_id=attempt.attempt_id,
                attempt_count=outbox.attempt_count + 1,
                at=checked_at,
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_attempt_prepared",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": checked_id,
                    "attempt_id": attempt.attempt_id,
                    "signed_evidence_hash": signed_evidence.evidence_hash,
                    "nonce": attempt.nonce,
                },
            )
            return attempt

    def _put_recovery_transport_locked(
        self,
        connection: sqlite3.Connection,
        evidence: TransportOutcomeEvidence,
        *,
        at: datetime,
    ) -> None:
        payload_json, content_hash = _canonical_payload(evidence.as_dict())
        material = {
            **evidence.as_dict(),
            "recorded_at": _time_text(at, field="recorded_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }
        existing = connection.execute(
            """
            SELECT * FROM execution_recovery_transport_evidence
            WHERE recovery_command_id = ?
            """,
            (evidence.command_id,),
        ).fetchone()
        if existing is not None:
            current = self._recovery_transport_from_row(existing)
            if current.evidence_hash == evidence.evidence_hash:
                return
            raise StateConflict("recovery cannot swap transport evidence")
        connection.execute(
            """
            INSERT INTO execution_recovery_transport_evidence (
                evidence_hash, recovery_command_id, attempt_id,
                signed_evidence_hash, endpoint, attempted_at_ms, outcome,
                http_status, detail_code, response_hash,
                transport_attempt_hash, send_count, retry_performed,
                venue_write_attempted, evidence_basis, recorded_at,
                payload_json, content_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_hash,
                evidence.command_id,
                evidence.attempt_id,
                evidence.signed_evidence_hash,
                evidence.endpoint,
                evidence.attempted_at_ms,
                evidence.outcome,
                evidence.http_status,
                evidence.detail_code,
                evidence.response_hash,
                evidence.transport_attempt_hash,
                evidence.send_count,
                None
                if evidence.venue_write_attempted is None
                else int(evidence.venue_write_attempted),
                evidence.evidence_basis,
                _time_text(at, field="recorded_at"),
                payload_json,
                content_hash,
                _record_hash("recovery-transport", material),
            ),
        )

    @classmethod
    def _recovery_transport_from_row(
        cls,
        row: Mapping[str, Any],
    ) -> TransportOutcomeEvidence:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="recovery transport content_hash"
        )
        payload = _decode_payload(
            payload_json,
            content_hash,
            field="recovery transport evidence",
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted recovery transport payload is not an object")
        try:
            evidence = TransportOutcomeEvidence(
                command_id=str(row["recovery_command_id"]),
                attempt_id=str(row["attempt_id"]),
                signed_evidence_hash=str(row["signed_evidence_hash"]),
                endpoint=str(row["endpoint"]),
                attempted_at_ms=int(row["attempted_at_ms"]),
                outcome=str(row["outcome"]),
                http_status=(
                    None if row["http_status"] is None else int(row["http_status"])
                ),
                detail_code=str(row["detail_code"]),
                response_hash=(
                    None if row["response_hash"] is None else str(row["response_hash"])
                ),
                transport_attempt_hash=(
                    None
                    if row["transport_attempt_hash"] is None
                    else str(row["transport_attempt_hash"])
                ),
                send_count=(
                    None if row["send_count"] is None else int(row["send_count"])
                ),
                retry_performed=bool(row["retry_performed"]),
                venue_write_attempted=(
                    None
                    if row["venue_write_attempted"] is None
                    else bool(row["venue_write_attempted"])
                ),
                evidence_basis=str(row["evidence_basis"]),
                evidence_hash=str(row["evidence_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted recovery transport evidence is invalid") from error
        if canonical_json(evidence.as_dict()) != payload_json:
            raise StorageError("persisted recovery transport differs from columns")
        recorded_at = _parse_time(
            row["recorded_at"], field="recovery transport recorded_at"
        )
        material = {
            **evidence.as_dict(),
            "recorded_at": _time_text(recorded_at, field="recorded_at"),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }
        if _stored_hash(
            row["record_hash"], field="recovery transport record_hash"
        ) != _record_hash("recovery-transport", material):
            raise StorageError("persisted recovery transport record hash differs")
        return evidence

    def get_recovery_transport_evidence(
        self,
        recovery_command_id: str,
    ) -> TransportOutcomeEvidence:
        checked = _text(
            recovery_command_id,
            field="recovery_command_id",
            maximum=128,
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_transport_evidence
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("recovery transport evidence is not registered")
        return self._recovery_transport_from_row(row)

    @staticmethod
    def _noop_fence_response_record_material(
        evidence: NoopFenceResponseEvidence,
        *,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            **evidence.as_dict(),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }

    @classmethod
    def _noop_fence_response_from_row(
        cls,
        row: Mapping[str, Any],
    ) -> NoopFenceResponseEvidence:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="noop fence response content_hash"
        )
        payload = _decode_payload(
            payload_json,
            content_hash,
            field="noop fence response evidence",
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted noop fence response is not an object")
        try:
            evidence = NoopFenceResponseEvidence(
                recovery_command_id=str(row["recovery_command_id"]),
                attempt_id=str(row["attempt_id"]),
                signed_evidence_hash=str(row["signed_evidence_hash"]),
                transport_evidence_hash=str(row["transport_evidence_hash"]),
                nonce=int(row["nonce"]),
                response_json=str(row["response_json"]),
                response_hash=str(row["response_hash"]),
                parsed_at=_parse_time(row["parsed_at"], field="noop parsed_at"),
                evidence_hash=str(row["evidence_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted noop fence response is invalid") from error
        if canonical_json(evidence.as_dict()) != payload_json:
            raise StorageError("persisted noop fence response differs from columns")
        if _stored_hash(
            row["record_hash"], field="noop fence response record_hash"
        ) != _record_hash(
            "noop-fence-response-record",
            cls._noop_fence_response_record_material(
                evidence,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        ):
            raise StorageError("persisted noop fence response record hash differs")
        return evidence

    def _put_noop_fence_response_locked(
        self,
        connection: sqlite3.Connection,
        evidence: NoopFenceResponseEvidence,
    ) -> NoopFenceResponseEvidence:
        payload_json, content_hash = _canonical_payload(evidence.as_dict())
        record_hash = _record_hash(
            "noop-fence-response-record",
            self._noop_fence_response_record_material(
                evidence,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        )
        existing = connection.execute(
            """
            SELECT * FROM execution_noop_fence_responses
            WHERE recovery_command_id = ?
            """,
            (evidence.recovery_command_id,),
        ).fetchone()
        if existing is not None:
            current = self._noop_fence_response_from_row(existing)
            if current.evidence_hash == evidence.evidence_hash:
                return current
            raise StateConflict("noop recovery cannot swap accepted response evidence")
        connection.execute(
            """
            INSERT INTO execution_noop_fence_responses (
                evidence_hash, recovery_command_id, attempt_id,
                signed_evidence_hash, transport_evidence_hash, nonce,
                response_json, response_hash, parsed_at, payload_json,
                content_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_hash,
                evidence.recovery_command_id,
                evidence.attempt_id,
                evidence.signed_evidence_hash,
                evidence.transport_evidence_hash,
                evidence.nonce,
                evidence.response_json,
                evidence.response_hash,
                _time_text(evidence.parsed_at, field="parsed_at"),
                payload_json,
                content_hash,
                record_hash,
            ),
        )
        return evidence

    def get_noop_fence_response(
        self,
        recovery_command_id: str,
    ) -> NoopFenceResponseEvidence:
        checked = _text(
            recovery_command_id,
            field="recovery_command_id",
            maximum=128,
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_noop_fence_responses
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("noop fence response evidence is not registered")
        return self._noop_fence_response_from_row(row)

    def require_terminal_noop_fence(
        self,
        parent_command_id: str,
    ) -> NoopFenceResolution:
        """Return a fully verified same-nonce fence for one parent command."""

        checked_parent = _text(
            parent_command_id,
            field="parent_command_id",
            maximum=128,
        )
        connection = self._connect()
        try:
            command_rows = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE parent_command_id = ? AND kind = 'noop_fence'
                  AND state = 'terminal'
                  AND EXISTS (
                      SELECT 1 FROM execution_recovery_reconciliations AS r
                      WHERE r.recovery_command_id =
                            execution_recovery_commands.recovery_command_id
                        AND r.complete = 1 AND r.success = 1
                  )
                """,
                (checked_parent,),
            ).fetchall()
            if len(command_rows) != 1:
                raise RecordNotFound(
                    "parent command lacks one terminal noop fence"
                )
            command = self._recovery_command_from_row(command_rows[0])
            if command.original_attempt_id is None or command.original_nonce is None:
                raise StorageError("terminal noop command lacks original attempt binding")

            original_row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (command.original_attempt_id,),
            ).fetchone()
            if original_row is None:
                raise StorageError("terminal noop original attempt is missing")
            original = self._attempt_from_row(original_row)
            if (
                original.command_id != checked_parent
                or original.state != "unknown"
                or original.nonce != command.original_nonce
                or original.preflight_hash != command.preflight_hash
            ):
                raise StateConflict("terminal noop no longer binds the unknown parent")

            attempt_row = connection.execute(
                """
                SELECT * FROM execution_recovery_attempts
                WHERE recovery_command_id = ?
                """,
                (command.recovery_command_id,),
            ).fetchone()
            if attempt_row is None:
                raise StorageError("terminal noop recovery attempt is missing")
            attempt = self._recovery_attempt_from_row(attempt_row)
            if (
                attempt.state != "response_received"
                or attempt.nonce != command.original_nonce
                or attempt.transport_evidence_hash is None
            ):
                raise StateConflict("terminal noop attempt is not accepted")

            transport_row = connection.execute(
                """
                SELECT * FROM execution_recovery_transport_evidence
                WHERE recovery_command_id = ?
                """,
                (command.recovery_command_id,),
            ).fetchone()
            if transport_row is None:
                raise StorageError("terminal noop transport evidence is missing")
            transport = self._recovery_transport_from_row(transport_row)
            if (
                transport.attempt_id != attempt.attempt_id
                or transport.signed_evidence_hash != attempt.signed_evidence_hash
                or transport.evidence_hash != attempt.transport_evidence_hash
            ):
                raise StateConflict("terminal noop transport binding differs")
            if (
                transport.outcome != "response_received"
                or transport.evidence_basis != "transport_result"
                or transport.detail_code != "response_received"
            ):
                raise StateConflict("terminal noop transport is not accepted")

            response_row = connection.execute(
                """
                SELECT * FROM execution_noop_fence_responses
                WHERE recovery_command_id = ?
                """,
                (command.recovery_command_id,),
            ).fetchone()
            if response_row is None:
                raise StorageError("terminal noop response evidence is missing")
            response = self._noop_fence_response_from_row(response_row)
            if (
                response.attempt_id != attempt.attempt_id
                or response.transport_evidence_hash
                != attempt.transport_evidence_hash
                or response.nonce != command.original_nonce
            ):
                raise StateConflict("terminal noop response binding differs")

            reconciliation_rows = connection.execute(
                """
                SELECT * FROM execution_recovery_reconciliations
                WHERE recovery_command_id = ? AND complete = 1 AND success = 1
                """,
                (command.recovery_command_id,),
            ).fetchall()
            if len(reconciliation_rows) != 1:
                raise StateConflict("terminal noop lacks one successful reconciliation")
            row = reconciliation_rows[0]
            payload_json = str(row["payload_json"])
            content_hash = _stored_hash(
                row["content_hash"], field="noop reconciliation content_hash"
            )
            payload = _decode_payload(
                payload_json,
                content_hash,
                field="noop recovery reconciliation",
            )
            if not isinstance(payload, dict) or set(payload) != {
                "reconciliation_id",
                "proof",
                "incident_resolution",
                "fills",
            }:
                raise StorageError("terminal noop reconciliation payload is invalid")
            if payload["fills"] != []:
                raise StateConflict("terminal noop reconciliation cannot own fills")
            proof_document = payload["proof"]
            if not isinstance(proof_document, dict):
                raise StorageError("terminal noop proof payload is invalid")
            try:
                proof = RecoveryReconciliationProof(
                    recovery_command_id=proof_document["recovery_command_id"],
                    kind=proof_document["kind"],
                    account_snapshot_hash=proof_document["account_snapshot_hash"],
                    observed_at=_parse_time(
                        proof_document["observed_at"],
                        field="noop proof observed_at",
                    ),
                    signed_position_quantity=proof_document[
                        "signed_position_quantity"
                    ],
                    protected_quantity=proof_document["protected_quantity"],
                    open_order_cloids=tuple(proof_document["open_order_cloids"]),
                    affected_cloids=tuple(proof_document["affected_cloids"]),
                    resolved_original_nonce=proof_document[
                        "resolved_original_nonce"
                    ],
                    resolved_original_outcome=proof_document[
                        "resolved_original_outcome"
                    ],
                    complete=proof_document["complete"],
                    success=proof_document["success"],
                    proof_hash=proof_document["proof_hash"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise StorageError("terminal noop proof cannot be reconstructed") from error
            if (
                canonical_json(proof.as_dict())
                != canonical_json(proof_document)
                or proof.kind != "noop_fence"
                or proof.recovery_command_id != command.recovery_command_id
                or proof.resolved_original_nonce != command.original_nonce
                or proof.resolved_original_outcome != "fenced"
                or not proof.complete
                or not proof.success
                or proof.signed_position_quantity != ZERO
            ):
                raise StateConflict("terminal noop proof does not establish a flat fence")
            reconciliation_id = str(payload["reconciliation_id"])
            incident_resolution = payload["incident_resolution"]
            if incident_resolution not in {"contained", "closed"}:
                raise StateConflict("terminal noop incident was not contained")
            record_material = {
                "reconciliation_id": reconciliation_id,
                "proof": proof.as_dict(),
                "incident_resolution": incident_resolution,
                "fills": [],
                "payload_json": payload_json,
                "content_hash": content_hash,
            }
            if _stored_hash(
                row["record_hash"], field="noop reconciliation record_hash"
            ) != _record_hash("recovery-reconciliation", record_material):
                raise StorageError("terminal noop reconciliation hash differs")
            if (
                str(row["account_snapshot_hash"]) != proof.account_snapshot_hash
                or int(row["complete"]) != 1
                or int(row["success"]) != 1
                or _parse_time(row["observed_at"], field="noop observed_at")
                != proof.observed_at
            ):
                raise StorageError("terminal noop reconciliation columns differ")

            incident_row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (command.incident_id,),
            ).fetchone()
            if incident_row is None:
                raise StorageError("terminal noop incident is missing")
            incident = self._incident_from_row(incident_row)
            if incident.state not in {"contained", "closed"}:
                raise StateConflict("terminal noop incident is not contained")
            material = {
                "parent_command_id": checked_parent,
                "recovery_command_id": command.recovery_command_id,
                "incident_id": command.incident_id,
                "original_attempt_id": command.original_attempt_id,
                "original_nonce": command.original_nonce,
                "response_evidence_hash": response.evidence_hash,
                "proof_hash": proof.proof_hash,
                "account_snapshot_hash": proof.account_snapshot_hash,
                "observed_at": _time_text(proof.observed_at, field="observed_at"),
            }
            return NoopFenceResolution(
                parent_command_id=checked_parent,
                recovery_command_id=command.recovery_command_id,
                incident_id=command.incident_id,
                original_attempt_id=command.original_attempt_id,
                original_nonce=command.original_nonce,
                response_evidence_hash=response.evidence_hash,
                proof_hash=proof.proof_hash,
                account_snapshot_hash=proof.account_snapshot_hash,
                observed_at=proof.observed_at,
                resolution_hash=_record_hash("noop-fence-resolution", material),
            )
        finally:
            connection.close()

    def close_noop_fenced_incident(
        self,
        parent_command_id: str,
        resolution_hash: str,
        *,
        at: datetime,
    ) -> IncidentRecord:
        """Close a contained noop incident only after parent terminal-flat proof."""

        checked_parent = _text(
            parent_command_id,
            field="parent_command_id",
            maximum=128,
        )
        checked_resolution = _hash(resolution_hash, field="resolution_hash")
        checked_at = _utc(at, field="at")
        resolution = self.require_terminal_noop_fence(checked_parent)
        if resolution.resolution_hash != checked_resolution:
            raise StateConflict("noop fence resolution hash differs")
        with self._transaction() as connection:
            parent_row = connection.execute(
                "SELECT * FROM execution_commands WHERE command_id = ?",
                (checked_parent,),
            ).fetchone()
            if parent_row is None:
                raise RecordNotFound("noop parent command is missing")
            parent = self._command_from_row(parent_row)
            if parent.state != "terminal":
                raise StateConflict("noop parent must be terminal before incident closure")
            plan_row = connection.execute(
                "SELECT instrument FROM execution_plans WHERE plan_hash = ?",
                (parent.plan_hash,),
            ).fetchone()
            if plan_row is None:
                raise StorageError("noop parent plan is missing")
            position_row = connection.execute(
                "SELECT * FROM execution_positions WHERE instrument = ?",
                (str(plan_row["instrument"]),),
            ).fetchone()
            protection_row = connection.execute(
                "SELECT * FROM execution_protection WHERE command_id = ?",
                (checked_parent,),
            ).fetchone()
            if (
                position_row is None
                or self._position_from_row(position_row).signed_quantity != ZERO
                or protection_row is None
                or self._protection_from_row(protection_row).state != "flat"
            ):
                raise StateConflict("noop parent is not durably terminal and flat")
            if connection.execute(
                """
                SELECT 1 FROM execution_recovery_commands
                WHERE state != 'terminal' LIMIT 1
                """
            ).fetchone() is not None:
                raise StateConflict("another recovery command remains active")
            incident_row = connection.execute(
                "SELECT * FROM execution_incidents WHERE incident_id = ?",
                (resolution.incident_id,),
            ).fetchone()
            if incident_row is None:
                raise StorageError("noop recovery incident is missing")
            incident = self._incident_from_row(incident_row)
            if incident.state == "closed":
                return incident
            if incident.state != "contained" or checked_at < incident.updated_at:
                raise StateConflict("noop recovery incident is not closable")
            details_json, details_hash = _canonical_payload(
                dict(incident.details), maximum=_MAX_DETAILS_BYTES
            )
            revision = incident.revision + 1
            material = self._incident_material(
                incident.incident_id,
                incident.command_id,
                incident.code,
                incident.severity,
                "closed",
                incident.opened_at,
                checked_at,
                revision,
                details_json,
                details_hash,
            )
            connection.execute(
                """
                UPDATE execution_incidents SET state = 'closed', updated_at = ?,
                    revision = ?, record_hash = ?
                WHERE incident_id = ? AND revision = ? AND state = 'contained'
                """,
                (
                    _time_text(checked_at, field="updated_at"),
                    revision,
                    _record_hash("incident", material),
                    incident.incident_id,
                    incident.revision,
                ),
            )
            closed = self._incident_from_row(
                connection.execute(
                    "SELECT * FROM execution_incidents WHERE incident_id = ?",
                    (incident.incident_id,),
                ).fetchone()
            )
            self._append_event_locked(
                connection,
                command_id=checked_parent,
                event_type="noop_fenced_incident_closed",
                occurred_at=checked_at,
                payload={
                    "incident_id": closed.incident_id,
                    "noop_fence_resolution_hash": checked_resolution,
                    "parent_terminal": True,
                    "parent_flat": True,
                },
            )
            return closed

    def record_recovery_outcome(
        self,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        transport_evidence: TransportOutcomeEvidence,
        noop_response: NoopFenceResponseEvidence | None = None,
        at: datetime,
    ) -> RecoveryCommand:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        if not isinstance(transport_evidence, TransportOutcomeEvidence):
            raise TypeError("transport_evidence must be TransportOutcomeEvidence")
        if noop_response is not None and not isinstance(
            noop_response, NoopFenceResponseEvidence
        ):
            raise TypeError("noop_response must be NoopFenceResponseEvidence or None")
        with self._transaction() as connection:
            outbox, outbox_row = self._require_recovery_claim_locked(
                connection,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_at,
                states={"signing"},
            )
            if outbox.current_attempt_id is None:
                raise StateConflict("recovery outcome requires prepared attempt")
            attempt_row = connection.execute(
                "SELECT * FROM execution_recovery_attempts WHERE attempt_id = ?",
                (outbox.current_attempt_id,),
            ).fetchone()
            if attempt_row is None:
                raise StorageError("recovery attempt disappeared")
            attempt = self._recovery_attempt_from_row(attempt_row)
            if attempt.state != "sending":
                raise StateConflict(
                    "recovery transport requires consumed submission authority"
                )
            if (
                transport_evidence.command_id != checked_id
                or transport_evidence.attempt_id != attempt.attempt_id
                or transport_evidence.signed_evidence_hash
                != attempt.signed_evidence_hash
                or transport_evidence.evidence_basis != "transport_result"
            ):
                raise StateConflict("recovery transport differs from attempt")
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("recovery command disappeared")
            command = self._recovery_command_from_row(command_row)
            if command.kind == "noop_fence":
                if transport_evidence.outcome == "response_received":
                    if noop_response is None:
                        raise StateConflict(
                            "accepted noop transport requires canonical response evidence"
                        )
                    if (
                        noop_response.recovery_command_id != checked_id
                        or noop_response.attempt_id != attempt.attempt_id
                        or noop_response.signed_evidence_hash
                        != attempt.signed_evidence_hash
                        or noop_response.transport_evidence_hash
                        != transport_evidence.evidence_hash
                        or noop_response.nonce != attempt.nonce
                        or noop_response.response_hash
                        != transport_evidence.response_hash
                        or int(noop_response.parsed_at.timestamp() * 1_000)
                        < transport_evidence.attempted_at_ms
                        or noop_response.parsed_at > checked_at
                    ):
                        raise StateConflict(
                            "noop response evidence differs from durable attempt"
                        )
                elif noop_response is not None:
                    raise StateConflict("unknown noop transport cannot claim acceptance")
            elif noop_response is not None:
                raise StateConflict("only noop recovery accepts default response evidence")
            self._put_recovery_transport_locked(
                connection, transport_evidence, at=checked_at
            )
            if noop_response is not None:
                self._put_noop_fence_response_locked(connection, noop_response)
            attempt_state = (
                "unknown"
                if transport_evidence.outcome == "unknown"
                else "response_received"
            )
            self._update_recovery_attempt_locked(
                connection,
                attempt,
                state=attempt_state,
                transport_evidence_hash=transport_evidence.evidence_hash,
                at=checked_at,
            )
            state = (
                "submitted_unknown"
                if transport_evidence.outcome == "unknown"
                else "reconciling"
            )
            self._set_recovery_outbox_locked(
                connection,
                outbox_row,
                state=state,
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=attempt.attempt_id,
                attempt_count=outbox.attempt_count,
                at=checked_at,
            )
            command = self._set_recovery_command_state_locked(
                connection, command_row, state=state, at=checked_at
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_transport_outcome_recorded",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": checked_id,
                    "outcome": transport_evidence.outcome,
                    "transport_evidence_hash": transport_evidence.evidence_hash,
                    "retry_allowed": False,
                },
            )
            return command

    def claim_recovery_reconciliation(
        self,
        recovery_command_id: str,
        worker_id: str,
        *,
        at: datetime,
        lease_seconds: int,
    ) -> RecoveryOutbox:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        checked_at = _utc(at, field="at")
        lease = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
        with self._transaction() as connection:
            self._normalize_expired_recovery_locked(connection, at=checked_at)
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_outbox
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("recovery outbox is missing")
            current = self._recovery_outbox_from_row(row)
            if current.state not in {"submitted_unknown", "reconciling"}:
                raise StateConflict("recovery is not ready for reconciliation")
            if (
                current.worker_id is not None
                and current.lease_expires_at is not None
                and checked_at < current.lease_expires_at
            ):
                raise StateConflict("recovery reconciliation is already claimed")
            expires = checked_at + timedelta(seconds=lease)
            claimed = self._set_recovery_outbox_locked(
                connection,
                row,
                state="reconciling",
                worker_id=checked_worker,
                fencing_token=current.fencing_token + 1,
                claimed_at=checked_at,
                lease_expires_at=expires,
                current_attempt_id=current.current_attempt_id,
                attempt_count=current.attempt_count,
                at=checked_at,
            )
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            command = self._recovery_command_from_row(command_row)
            if command.state != "reconciling":
                self._set_recovery_command_state_locked(
                    connection, command_row, state="reconciling", at=checked_at
                )
            return claimed

    @staticmethod
    def _recovery_fill_material(
        fill: RecoveryVenueFill,
        *,
        recovery_command_id: str,
        parent_command_id: str,
        payload_json: str,
        content_hash: str,
    ) -> dict[str, object]:
        return {
            "recovery_command_id": recovery_command_id,
            "parent_command_id": parent_command_id,
            **fill.as_dict(),
            "payload_json": payload_json,
            "content_hash": content_hash,
        }

    @classmethod
    def _recovery_fill_from_row(cls, row: Mapping[str, Any]) -> RecoveryVenueFill:
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="recovery fill content_hash"
        )
        payload = _decode_payload(
            payload_json, content_hash, field="recovery fill"
        )
        fill = RecoveryVenueFill(
            fill_id=str(row["fill_id"]),
            recovery_command_id=str(row["recovery_command_id"]),
            parent_command_id=str(row["parent_command_id"]),
            cloid=str(row["cloid"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            quantity=Decimal(str(row["quantity"])),
            signed_quantity=Decimal(str(row["signed_quantity"])),
            start_position=Decimal(str(row["start_position"])),
            end_position=Decimal(str(row["end_position"])),
            price=Decimal(str(row["price"])),
            fee=Decimal(str(row["fee"])),
            closed_pnl=Decimal(str(row["closed_pnl"])),
            fee_token=str(row["fee_token"]),
            crossed=bool(int(row["crossed"])),
            builder_fee=(
                None
                if row["builder_fee"] is None
                else Decimal(str(row["builder_fee"]))
            ),
            venue_oid=int(row["venue_oid"]),
            venue_trade_id=int(row["venue_trade_id"]),
            transaction_hash=str(row["transaction_hash"]),
            occurred_at=_parse_time(
                row["occurred_at"], field="recovery fill occurred_at"
            ),
            observed_at=_parse_time(
                row["observed_at"], field="recovery fill observed_at"
            ),
            account_snapshot_hash=str(row["account_snapshot_hash"]),
            venue_evidence_hash=str(row["venue_evidence_hash"]),
        )
        expected_payload = fill.as_dict()
        if payload != expected_payload:
            raise StorageError("persisted recovery fill payload differs")
        material = cls._recovery_fill_material(
            fill,
            recovery_command_id=str(row["recovery_command_id"]),
            parent_command_id=str(row["parent_command_id"]),
            payload_json=payload_json,
            content_hash=content_hash,
        )
        if _stored_hash(
            row["record_hash"], field="recovery fill record_hash"
        ) != _record_hash("recovery-fill", material):
            raise StorageError("persisted recovery fill record hash differs")
        return fill

    def _put_recovery_fill_locked(
        self,
        connection: sqlite3.Connection,
        *,
        command: RecoveryCommand,
        fill: RecoveryVenueFill,
    ) -> None:
        if command.kind != "reduce_only_close":
            raise StateConflict("only a reduce-only close may own recovery fills")
        if (
            fill.recovery_command_id != command.recovery_command_id
            or fill.parent_command_id != command.parent_command_id
        ):
            raise StateConflict("recovery fill owner differs from durable command")
        try:
            material = json.loads(command.recovery_material_json)
        except ValueError as error:
            raise StorageError("recovery material is invalid JSON") from error
        original = _decimal(
            material.get("original_signed_position"),
            field="original_signed_position",
        )
        if (
            material.get("cloid") != fill.cloid
            or material.get("symbol") != fill.symbol
            or fill.side != ("sell" if original > ZERO else "buy")
        ):
            raise StateConflict("recovery fill differs from durable close action")
        if fill.observed_at < fill.occurred_at:
            raise StateConflict("recovery fill observation ordering differs")
        parent_fill = connection.execute(
            """
            SELECT 1 FROM execution_fills
            WHERE fill_id = ? OR (
                venue_oid = ? AND venue_trade_id = ?
                AND transaction_hash = ? AND occurred_at = ?
            )
            """,
            (
                fill.fill_id,
                fill.venue_oid,
                fill.venue_trade_id,
                fill.transaction_hash,
                _time_text(fill.occurred_at, field="occurred_at"),
            ),
        ).fetchone()
        if parent_fill is not None:
            raise StateConflict("venue fill cannot belong to parent and recovery")
        payload = fill.as_dict()
        payload_json, content_hash = _canonical_payload(payload)
        record_material = self._recovery_fill_material(
            fill,
            recovery_command_id=command.recovery_command_id,
            parent_command_id=command.parent_command_id,
            payload_json=payload_json,
            content_hash=content_hash,
        )
        record_hash = _record_hash("recovery-fill", record_material)
        existing = connection.execute(
            "SELECT * FROM execution_recovery_fills WHERE fill_id = ?",
            (fill.fill_id,),
        ).fetchone()
        if existing is not None:
            current = self._recovery_fill_from_row(existing)
            current_core = current.as_dict()
            incoming_core = fill.as_dict()
            for field in (
                "observed_at",
                "account_snapshot_hash",
                "venue_evidence_hash",
            ):
                current_core.pop(field)
                incoming_core.pop(field)
            if current_core == incoming_core:
                return
            raise StateConflict("recovery fill identity is bound differently")
        connection.execute(
            """
            INSERT INTO execution_recovery_fills (
                fill_id, recovery_command_id, parent_command_id, cloid,
                symbol, side, quantity, signed_quantity, start_position,
                end_position, price, fee, closed_pnl, fee_token, crossed,
                builder_fee, venue_oid, venue_trade_id, transaction_hash,
                occurred_at, observed_at, account_snapshot_hash,
                venue_evidence_hash, payload_json, content_hash, record_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                fill.fill_id,
                command.recovery_command_id,
                command.parent_command_id,
                fill.cloid,
                fill.symbol,
                fill.side,
                _decimal_text(fill.quantity, field="quantity"),
                _decimal_text(fill.signed_quantity, field="signed_quantity"),
                _decimal_text(fill.start_position, field="start_position"),
                _decimal_text(fill.end_position, field="end_position"),
                _decimal_text(fill.price, field="price"),
                _decimal_text(fill.fee, field="fee"),
                _decimal_text(fill.closed_pnl, field="closed_pnl"),
                fill.fee_token,
                int(fill.crossed),
                (
                    None
                    if fill.builder_fee is None
                    else _decimal_text(fill.builder_fee, field="builder_fee")
                ),
                fill.venue_oid,
                fill.venue_trade_id,
                fill.transaction_hash,
                _time_text(fill.occurred_at, field="occurred_at"),
                _time_text(fill.observed_at, field="observed_at"),
                fill.account_snapshot_hash,
                fill.venue_evidence_hash,
                payload_json,
                content_hash,
                record_hash,
            ),
        )

    def reconcile_recovery(
        self,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        reconciliation_id: str,
        proof: RecoveryReconciliationProof,
        incident_resolution: str | None,
        fills: Sequence[RecoveryVenueFill] = (),
        mutation_at: datetime | None = None,
    ) -> RecoveryCommand:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_reconciliation = _text(
            reconciliation_id, field="reconciliation_id", maximum=128
        )
        if not isinstance(proof, RecoveryReconciliationProof):
            raise TypeError("proof must be RecoveryReconciliationProof")
        if proof.recovery_command_id != checked_id:
            raise StateConflict("recovery proof targets another command")
        checked_at = proof.observed_at
        checked_mutation_at = (
            checked_at
            if mutation_at is None
            else _utc(mutation_at, field="mutation_at")
        )
        if incident_resolution not in {None, "contained", "closed"}:
            raise ValidationError("incident_resolution is invalid")
        if incident_resolution is not None and (not proof.complete or not proof.success):
            raise ValidationError("incident resolution requires complete success")
        fill_values = tuple(fills)
        if any(not isinstance(item, RecoveryVenueFill) for item in fill_values):
            raise TypeError("fills must contain RecoveryVenueFill records")
        if len({item.fill_id for item in fill_values}) != len(fill_values):
            raise ValidationError("recovery fills repeat identity")
        with self._transaction() as connection:
            outbox, outbox_row = self._require_recovery_claim_locked(
                connection,
                recovery_command_id=checked_id,
                worker_id=checked_worker,
                fencing_token=token,
                at=checked_mutation_at,
                states={"reconciling"},
            )
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("recovery command disappeared")
            command = self._recovery_command_from_row(command_row)
            if proof.kind != command.kind:
                raise StateConflict("recovery proof kind differs from command")
            recovery_material = json.loads(command.recovery_material_json)
            if not isinstance(recovery_material, dict):
                raise StorageError("recovery material is not an object")
            if proof.success:
                if command.kind == "reduce_only_close":
                    original = _decimal(
                        recovery_material.get("original_signed_position"),
                        field="original_signed_position",
                    )
                    close_size = _decimal(
                        recovery_material.get("close_size"),
                        field="close_size",
                        nonnegative=True,
                    )
                    maximum_remaining = max(abs(original) - close_size, ZERO)
                    flipped = (
                        original > ZERO and proof.signed_position_quantity < ZERO
                    ) or (
                        original < ZERO and proof.signed_position_quantity > ZERO
                    )
                    if flipped or abs(proof.signed_position_quantity) > maximum_remaining:
                        raise StateConflict(
                            "close proof does not show bounded non-flipping reduction"
                        )
                elif command.kind == "cancel_by_cloid":
                    requests = recovery_material.get("requests")
                    if not isinstance(requests, list):
                        raise StorageError("cancel recovery material lacks requests")
                    expected_cloids = tuple(
                        sorted(
                            str(item.get("cloid"))
                            for item in requests
                            if isinstance(item, dict)
                        )
                    )
                    if proof.affected_cloids != expected_cloids or set(
                        expected_cloids
                    ) & set(proof.open_order_cloids):
                        raise StateConflict(
                            "cancel proof does not match persisted requested CLOIDs"
                        )
                else:
                    original_nonce = recovery_material.get("original_nonce")
                    if (
                        type(original_nonce) is not int
                        or proof.resolved_original_nonce != original_nonce
                        or proof.resolved_original_outcome is None
                    ):
                        raise StateConflict(
                            "noop proof does not resolve persisted original nonce"
                        )
            if fill_values and command.kind != "reduce_only_close":
                raise StateConflict("non-close recovery cannot persist fills")
            if any(
                fill.account_snapshot_hash != proof.account_snapshot_hash
                or fill.observed_at != proof.observed_at
                for fill in fill_values
            ):
                raise StateConflict("recovery fills differ from reconciliation proof")
            for fill in fill_values:
                self._put_recovery_fill_locked(
                    connection,
                    command=command,
                    fill=fill,
                )
            payload = {
                "reconciliation_id": checked_reconciliation,
                "proof": proof.as_dict(),
                "incident_resolution": incident_resolution,
                "fills": [item.as_dict() for item in fill_values],
            }
            payload_json, content_hash = _canonical_payload(payload)
            record_hash = _record_hash(
                "recovery-reconciliation",
                {
                    **payload,
                    "payload_json": payload_json,
                    "content_hash": content_hash,
                },
            )
            existing = connection.execute(
                """
                SELECT * FROM execution_recovery_reconciliations
                WHERE reconciliation_id = ?
                """,
                (checked_reconciliation,),
            ).fetchone()
            if existing is not None:
                if existing["record_hash"] == record_hash:
                    if int(existing["complete"]) == 0:
                        self._set_recovery_outbox_locked(
                            connection,
                            outbox_row,
                            state="reconciling",
                            worker_id=None,
                            fencing_token=token,
                            claimed_at=None,
                            lease_expires_at=None,
                            current_attempt_id=outbox.current_attempt_id,
                            attempt_count=outbox.attempt_count,
                            at=checked_mutation_at,
                        )
                        self._append_event_locked(
                            connection,
                            command_id=command.parent_command_id,
                            event_type="recovery_reconciliation_replay_released",
                            occurred_at=checked_mutation_at,
                            payload={
                                "recovery_command_id": checked_id,
                                "reconciliation_id": checked_reconciliation,
                                "account_snapshot_hash": proof.account_snapshot_hash,
                            },
                        )
                    return command
                raise StateConflict("recovery reconciliation ID conflicts")
            connection.execute(
                """
                INSERT INTO execution_recovery_reconciliations (
                    reconciliation_id, recovery_command_id,
                    account_snapshot_hash, success, complete,
                    incident_resolution, observed_at, payload_json,
                    content_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_reconciliation,
                    checked_id,
                    proof.account_snapshot_hash,
                    int(proof.success),
                    int(proof.complete),
                    incident_resolution,
                    _time_text(checked_at, field="observed_at"),
                    payload_json,
                    content_hash,
                    record_hash,
                ),
            )
            if not proof.complete:
                self._set_recovery_outbox_locked(
                    connection,
                    outbox_row,
                    state="reconciling",
                    worker_id=None,
                    fencing_token=token,
                    claimed_at=None,
                    lease_expires_at=None,
                    current_attempt_id=outbox.current_attempt_id,
                    attempt_count=outbox.attempt_count,
                    at=checked_mutation_at,
                )
                self._append_event_locked(
                    connection,
                    command_id=command.parent_command_id,
                    event_type="recovery_reconciliation_incomplete",
                    occurred_at=checked_mutation_at,
                    payload={
                        "recovery_command_id": checked_id,
                        "reconciliation_id": checked_reconciliation,
                        "proof_hash": proof.proof_hash,
                        "retry_allowed": False,
                        "fresh_venue_read_required": True,
                    },
                )
                return command
            plan_row = connection.execute(
                """
                SELECT plan.instrument FROM execution_commands AS parent
                JOIN execution_plans AS plan ON plan.plan_hash = parent.plan_hash
                WHERE parent.command_id = ?
                """,
                (command.parent_command_id,),
            ).fetchone()
            if plan_row is None:
                raise StorageError("recovery parent plan is missing")
            instrument = str(plan_row["instrument"])
            self._upsert_position_locked(
                connection,
                instrument=instrument,
                quantity=proof.signed_position_quantity,
                snapshot_hash=proof.account_snapshot_hash,
                observed_at=checked_at,
            )
            stop_row = connection.execute(
                """
                SELECT cloid FROM execution_command_legs
                WHERE command_id = ? AND role = 'protective_stop'
                """,
                (command.parent_command_id,),
            ).fetchone()
            if stop_row is None:
                raise StorageError("recovery parent protective stop is missing")
            protection = self._upsert_protection_locked(
                connection,
                command_id=command.parent_command_id,
                instrument=instrument,
                signed_position=proof.signed_position_quantity,
                protected_quantity=proof.protected_quantity,
                stop_cloid=str(stop_row["cloid"]),
                observed_at=checked_at,
                failed=False,
            )
            if incident_resolution == "contained" and not (
                proof.signed_position_quantity == ZERO
                or protection.state == "protected"
            ):
                raise StateConflict(
                    "incident cannot be contained without flat or protected position"
                )
            if incident_resolution == "closed":
                parent_row = connection.execute(
                    "SELECT * FROM execution_commands WHERE command_id = ?",
                    (command.parent_command_id,),
                ).fetchone()
                parent = self._command_from_row(parent_row)
                protection_row = connection.execute(
                    "SELECT * FROM execution_protection WHERE command_id = ?",
                    (command.parent_command_id,),
                ).fetchone()
                if (
                    parent.state != "terminal"
                    or protection_row is None
                    or self._protection_from_row(protection_row).state != "flat"
                ):
                    raise StateConflict(
                        "incident cannot close before parent entry is terminal and flat"
                    )
            if incident_resolution is not None:
                incident_row = connection.execute(
                    "SELECT * FROM execution_incidents WHERE incident_id = ?",
                    (command.incident_id,),
                ).fetchone()
                incident = self._incident_from_row(incident_row)
                details_json, details_hash = _canonical_payload(
                    dict(incident.details), maximum=_MAX_DETAILS_BYTES
                )
                updated_incident = self._incident_material(
                    incident.incident_id,
                    incident.command_id,
                    incident.code,
                    incident.severity,
                    incident_resolution,
                    incident.opened_at,
                    checked_mutation_at,
                    incident.revision + 1,
                    details_json,
                    details_hash,
                )
                connection.execute(
                    """
                    UPDATE execution_incidents SET state = ?, updated_at = ?,
                        revision = ?, record_hash = ? WHERE incident_id = ?
                    """,
                    (
                        incident_resolution,
                        _time_text(checked_mutation_at, field="updated_at"),
                        incident.revision + 1,
                        _record_hash("incident", updated_incident),
                        incident.incident_id,
                    ),
                )
            terminal = self._set_recovery_command_state_locked(
                connection,
                command_row,
                state="terminal",
                at=checked_mutation_at,
                terminal=True,
            )
            self._set_recovery_outbox_locked(
                connection,
                outbox_row,
                state="terminal",
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=outbox.current_attempt_id,
                attempt_count=outbox.attempt_count,
                at=checked_mutation_at,
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_reconciliation_terminal",
                occurred_at=checked_mutation_at,
                payload={
                    "recovery_command_id": checked_id,
                    "success": proof.success,
                    "incident_resolution": incident_resolution,
                    "parent_risk_released": False,
                },
            )
            return terminal

    def release_recovery_reconciliation_claim(
        self,
        recovery_command_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        at: datetime,
        reason: str,
    ) -> RecoveryOutbox:
        checked_id = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        checked_worker = _text(worker_id, field="worker_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        checked_reason = _text(reason, field="reason", maximum=128)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_outbox
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("recovery outbox is missing")
            current = self._recovery_outbox_from_row(row)
            if (
                current.state != "reconciling"
                or current.worker_id != checked_worker
                or current.fencing_token != token
                or current.claimed_at is None
                or current.lease_expires_at is None
                or checked_at < current.claimed_at
            ):
                raise StateConflict(
                    "recovery claim release is stale or mismatched"
                )
            command_row = connection.execute(
                """
                SELECT * FROM execution_recovery_commands
                WHERE recovery_command_id = ?
                """,
                (checked_id,),
            ).fetchone()
            if command_row is None:
                raise StorageError("recovery command disappeared")
            command = self._recovery_command_from_row(command_row)
            released = self._set_recovery_outbox_locked(
                connection,
                row,
                state="reconciling",
                worker_id=None,
                fencing_token=token,
                claimed_at=None,
                lease_expires_at=None,
                current_attempt_id=current.current_attempt_id,
                attempt_count=current.attempt_count,
                at=checked_at,
            )
            self._append_event_locked(
                connection,
                command_id=command.parent_command_id,
                event_type="recovery_reconciliation_claim_released",
                occurred_at=checked_at,
                payload={
                    "recovery_command_id": checked_id,
                    "reason": checked_reason,
                    "fencing_token": token,
                    "venue_write_attempted": False,
                },
            )
            return released

    def get_recovery_attempt(self, recovery_command_id: str) -> RecoveryAttempt:
        checked = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_recovery_attempts
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("recovery attempt is not registered")
        return self._recovery_attempt_from_row(row)

    def list_recovery_fills(
        self,
        *,
        parent_command_id: str | None = None,
        recovery_command_id: str | None = None,
    ) -> tuple[RecoveryVenueFill, ...]:
        if (parent_command_id is None) == (recovery_command_id is None):
            raise ValidationError(
                "select exactly one parent_command_id or recovery_command_id"
            )
        if parent_command_id is not None:
            field = "parent_command_id"
            selected = _text(
                parent_command_id, field=field, maximum=128
            )
        else:
            field = "recovery_command_id"
            selected = _text(
                recovery_command_id, field=field, maximum=128
            )
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM execution_recovery_fills
                WHERE {field} = ? ORDER BY occurred_at, fill_id
                """,
                (selected,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._recovery_fill_from_row(row) for row in rows)

    def get_recovery_permit_state(self, permit_id: str) -> str:
        checked = _text(permit_id, field="permit_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM execution_recovery_permits WHERE permit_id = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("recovery permit is not registered")
        self._recovery_permit_from_row(row)
        return str(row["state"])

    def get_signed_recovery_evidence(
        self, recovery_command_id: str
    ) -> SignedRecoveryEvidence:
        checked = _text(
            recovery_command_id, field="recovery_command_id", maximum=128
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM execution_signed_recovery_evidence
                WHERE recovery_command_id = ?
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("signed recovery evidence is not registered")
        payload_json = str(row["payload_json"])
        content_hash = _stored_hash(
            row["content_hash"], field="signed recovery content_hash"
        )
        payload = _decode_payload(
            payload_json, content_hash, field="signed recovery evidence"
        )
        if not isinstance(payload, dict):
            raise StorageError("signed recovery payload is not an object")
        try:
            evidence = SignedRecoveryEvidence(
                recovery_command_id=str(row["recovery_command_id"]),
                incident_id=str(row["incident_id"]),
                kind=str(row["kind"]),
                source_hash=str(row["source_hash"]),
                recovery_hash=str(row["recovery_hash"]),
                signing_authority_hash=str(row["signing_authority_hash"]),
                safety_policy_hash=str(row["safety_policy_hash"]),
                nonce=int(row["nonce"]),
                wire_hash=str(row["wire_hash"]),
                action_hash=str(row["action_hash"]),
                signature_hash=str(row["signature_hash"]),
                envelope_hash=str(row["envelope_hash"]),
                signer_binding_hash=str(row["signer_binding_hash"]),
                expires_after_ms=int(row["expires_after_ms"]),
                signed_at_ms=int(row["signed_at_ms"]),
                evidence_hash=str(row["evidence_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted signed recovery evidence is invalid") from error
        if canonical_json(evidence.as_dict()) != payload_json:
            raise StorageError("persisted signed recovery payload differs")
        recorded_at = _parse_time(
            row["recorded_at"], field="signed recovery recorded_at"
        )
        if _stored_hash(
            row["record_hash"], field="signed recovery record_hash"
        ) != _record_hash(
            "signed-recovery-record",
            self._signed_recovery_material(
                evidence,
                recorded_at=recorded_at,
                payload_json=payload_json,
                content_hash=content_hash,
            ),
        ):
            raise StorageError("persisted signed recovery record hash differs")
        return evidence


__all__ = (
    "AttemptRecord",
    "CommandRecord",
    "DispatchPreflight",
    "EntrySubmissionAuthority",
    "EXECUTION_SCHEMA_VERSION",
    "EventRecord",
    "ExecutionStore",
    "IncidentRecord",
    "LegRecord",
    "LegReconciliation",
    "NoopFenceResponseEvidence",
    "NoopFenceResolution",
    "OutboxRecord",
    "PositionRecord",
    "ProtectionRecord",
    "RecoveryAttempt",
    "RecoveryCommand",
    "RecoveryOutbox",
    "RecoveryPermit",
    "RecoveryVenueFill",
    "SignedEnvelopeEvidence",
    "SignedRecoveryEvidence",
    "TransportOutcomeEvidence",
    "TrustedApproval",
    "VenueFill",
)
