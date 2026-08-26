"""Immutable, non-authoritative trade-ticket staging inbox.

This module is the narrow hand-off between an agent-facing research surface and
a separately trusted quote compiler.  The untrusted request vocabulary is
deliberately only ``asset_id``, ``expected_analysis_hash``, and
``idempotency_key``.  Prices, sizes, sides, accounts, approvals, credentials,
and every other economic or capital-bearing field can enter a staged document
only as opaque output from the callback supplied when the inbox is built.

Staging is not authorization.  Every persisted document carries an explicit
all-false authority block.  This module has no capital-store, approval, secret,
signer, dispatcher, or venue dependency and exposes no network operation.

The SQLite schema owns only ``staging_*`` objects.  Documents are immutable;
creation and expiry are represented by a globally ordered, hash-chained event
ledger.  ``BEGIN IMMEDIATE`` serializes idempotency lookup, trusted quote
evaluation, document insertion, and event append across processes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .canonical import canonical_json, domain_hash
from .executor_state_binding import (
    MAX_SHARED_STATE_FILE_BYTES,
    STATE_BINDING_TABLE,
    STATE_BINDING_TABLE_SQL,
)
from .errors import StorageError
from .sqlite_snapshot import (
    enforce_sqlite_write_limit,
    sqlite_verification_snapshot,
    validate_sqlite_file_sizes,
)


STAGING_INBOX_SCHEMA_VERSION = 1

_DOCUMENT_SCHEMA = "trade_staging_document.v1"
_GENESIS_CHAIN_HASH = "0" * 64
_MAX_TEXT = 128
_MAX_TICKET_BYTES = 512 * 1024
_MAX_DOCUMENT_BYTES = 768 * 1024
_MAX_EVENT_BYTES = 64 * 1024
_MAX_EXPIRIES_PER_TRANSACTION = 32
_STAGING_WRITE_HEADROOM_BYTES = 8 * 1024 * 1024
_MAX_LIST_LIMIT = 1_000
_MAX_TTL = timedelta(days=1)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOCK_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")

_REQUEST_HASH_DOMAIN = "trading-harness/staging-request/v1"
_IDEMPOTENCY_HASH_DOMAIN = "trading-harness/staging-idempotency/v1"
_DOCUMENT_ID_DOMAIN = "trading-harness/staging-document-id/v1"
_DOCUMENT_HASH_DOMAIN = "trading-harness/staging-document/v1"
_TICKET_HASH_DOMAIN = "trading-harness/staged-ticket/v1"
_EVENT_HASH_DOMAIN = "trading-harness/staging-event/v1"
_CHAIN_HASH_DOMAIN = "trading-harness/staging-event-chain/v1"


class StagingInboxError(Exception):
    """Base class for expected staging-inbox failures."""


class StagingValidationError(StagingInboxError, ValueError):
    """An untrusted request or trusted callback value is malformed."""


class StagingConflict(StagingInboxError):
    """An idempotency key is already bound to another request."""


class StagingNotFound(StagingInboxError):
    """A requested immutable staging document does not exist."""


class StagingStorageError(StagingInboxError):
    """Persisted staging state cannot prove its integrity."""


class StagingDecision(str, Enum):
    STAGED = "staged"
    BLOCKED = "blocked"


class StagingState(str, Enum):
    STAGED = "staged"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class StagingEventType(str, Enum):
    DOCUMENT_CREATED = "document_created"
    DOCUMENT_EXPIRED = "document_expired"


def _utc(value: object, *, field_name: str, stored: bool = False) -> datetime:
    exception = StagingStorageError if stored else StagingValidationError
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise exception(f"{field_name} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise exception(f"{field_name} is outside the supported UTC range") from error


def _time_text(value: datetime, *, field_name: str) -> str:
    return _utc(value, field_name=field_name).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_time(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise StagingStorageError(f"persisted {field_name} is not text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StagingStorageError(
            f"persisted {field_name} is not ISO-8601"
        ) from error
    return _utc(parsed, field_name=f"persisted {field_name}", stored=True)


def _text(value: object, *, field_name: str, maximum: int = _MAX_TEXT) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise StagingValidationError(
            f"{field_name} must be a non-empty, trimmed, bounded text value"
        )
    return value


def _stored_text(
    value: object, *, field_name: str, maximum: int = _MAX_TEXT
) -> str:
    try:
        return _text(value, field_name=field_name, maximum=maximum)
    except StagingValidationError as error:
        raise StagingStorageError(f"persisted {field_name} is invalid") from error


def _sha256(value: object, *, field_name: str, stored: bool = False) -> str:
    exception = StagingStorageError if stored else StagingValidationError
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise exception(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _block_code(value: object, *, stored: bool = False) -> str:
    exception = StagingStorageError if stored else StagingValidationError
    if not isinstance(value, str) or not _BLOCK_CODE_RE.fullmatch(value):
        raise exception("block_code must be lowercase snake_case")
    return value


def _positive_ttl(value: object, *, field_name: str) -> timedelta:
    if (
        not isinstance(value, timedelta)
        or value <= timedelta(0)
        or value > _MAX_TTL
    ):
        raise StagingValidationError(
            f"{field_name} must be a positive timedelta no greater than one day"
        )
    return value


def _bounded_canonical_json(value: object, *, maximum: int, label: str) -> str:
    try:
        rendered = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise StagingValidationError(f"{label} is not canonical JSON") from error
    if len(rendered.encode("utf-8")) > maximum:
        raise StagingValidationError(f"{label} exceeds its size limit")
    return rendered


def _decode_canonical_json(
    value: object,
    expected_hash: object,
    *,
    maximum: int,
    label: str,
) -> Any:
    if not isinstance(value, str):
        raise StagingStorageError(f"persisted {label} is not text")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum:
        raise StagingStorageError(f"persisted {label} exceeds its size limit")
    digest = _sha256(expected_hash, field_name=f"{label} hash", stored=True)
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise StagingStorageError(f"persisted {label} hash does not match")
    try:
        decoded = json.loads(value)
        rendered = canonical_json(decoded)
    except (TypeError, ValueError, RecursionError) as error:
        raise StagingStorageError(f"persisted {label} is not canonical JSON") from error
    if rendered != value:
        raise StagingStorageError(f"persisted {label} is not canonical JSON")
    return decoded


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise StagingStorageError(f"persisted {label} fields do not match its schema")


def _idempotency_hash(idempotency_key: str) -> str:
    return domain_hash(_IDEMPOTENCY_HASH_DOMAIN, idempotency_key)


def _request_hash(
    *, asset_id: str, expected_analysis_hash: str, idempotency_key_hash: str
) -> str:
    return domain_hash(
        _REQUEST_HASH_DOMAIN,
        {
            "asset_id": asset_id,
            "expected_analysis_hash": expected_analysis_hash,
            "idempotency_key_hash": idempotency_key_hash,
        },
    )


def _document_id(request_hash: str) -> str:
    return "stg_" + domain_hash(_DOCUMENT_ID_DOMAIN, request_hash)


@dataclass(frozen=True, slots=True)
class NonAuthoritativeStaging:
    """Machine-readable proof that an inbox record confers no authority."""

    approval_created: bool = field(default=False, init=False)
    eligible_to_trade: bool = field(default=False, init=False)
    order_submitted: bool = field(default=False, init=False)
    capital_authority: bool = field(default=False, init=False)
    approval_authority: bool = field(default=False, init=False)
    risk_reservation_authority: bool = field(default=False, init=False)
    credential_access: bool = field(default=False, init=False)
    signing_authority: bool = field(default=False, init=False)
    venue_write_authority: bool = field(default=False, init=False)
    execution_authority: bool = field(default=False, init=False)

    def as_dict(self) -> dict[str, bool]:
        return {
            "approval_created": False,
            "eligible_to_trade": False,
            "order_submitted": False,
            "capital_authority": False,
            "approval_authority": False,
            "risk_reservation_authority": False,
            "credential_access": False,
            "signing_authority": False,
            "venue_write_authority": False,
            "execution_authority": False,
        }


NON_AUTHORITATIVE_STAGING = NonAuthoritativeStaging()


@dataclass(frozen=True, slots=True)
class StageTradeRequest:
    """The complete untrusted request vocabulary for staging one trade."""

    asset_id: str
    expected_analysis_hash: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "asset_id", _text(self.asset_id, field_name="asset_id")
        )
        object.__setattr__(
            self,
            "expected_analysis_hash",
            _sha256(
                self.expected_analysis_hash,
                field_name="expected_analysis_hash",
            ),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _text(
                self.idempotency_key,
                field_name="idempotency_key",
                maximum=128,
            ),
        )

    @classmethod
    def from_untrusted(cls, value: Mapping[str, object]) -> StageTradeRequest:
        """Parse an exact request and reject every additional field."""

        if not isinstance(value, Mapping):
            raise StagingValidationError("staging request must be an object")
        expected = {"asset_id", "expected_analysis_hash", "idempotency_key"}
        if set(value) != expected:
            raise StagingValidationError(
                "staging request fields must be exactly asset_id, "
                "expected_analysis_hash, and idempotency_key"
            )
        return cls(
            asset_id=value["asset_id"],
            expected_analysis_hash=value["expected_analysis_hash"],
            idempotency_key=value["idempotency_key"],
        )


@dataclass(frozen=True, slots=True)
class TrustedQuoteRequest:
    """Minimal immutable input visible to the trusted quote callback."""

    asset_id: str
    expected_analysis_hash: str


@dataclass(frozen=True, slots=True)
class TrustedQuoteDecision:
    """Typed output returned only by the configured trusted quote callback."""

    decision: StagingDecision
    analysis_hash: str | None
    ticket_payload: Mapping[str, Any] | None
    block_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, StagingDecision):
            raise StagingValidationError("trusted quote decision is unsupported")
        if self.decision is StagingDecision.STAGED:
            analysis_hash = _sha256(
                self.analysis_hash, field_name="trusted quote analysis_hash"
            )
            if not isinstance(self.ticket_payload, Mapping):
                raise StagingValidationError(
                    "staged trusted quote requires an object ticket_payload"
                )
            ticket_json = _bounded_canonical_json(
                self.ticket_payload,
                maximum=_MAX_TICKET_BYTES,
                label="trusted ticket payload",
            )
            decoded = json.loads(ticket_json)
            if not isinstance(decoded, dict) or not decoded:
                raise StagingValidationError(
                    "staged trusted quote requires a non-empty object ticket_payload"
                )
            if self.block_code is not None:
                raise StagingValidationError(
                    "staged trusted quote cannot carry a block_code"
                )
            object.__setattr__(self, "analysis_hash", analysis_hash)
            object.__setattr__(self, "ticket_payload", decoded)
            return

        if self.analysis_hash is not None:
            object.__setattr__(
                self,
                "analysis_hash",
                _sha256(
                    self.analysis_hash,
                    field_name="trusted quote analysis_hash",
                ),
            )
        if self.ticket_payload is not None:
            raise StagingValidationError(
                "blocked trusted quote cannot carry a ticket_payload"
            )
        object.__setattr__(self, "block_code", _block_code(self.block_code))

    @classmethod
    def staged(
        cls,
        *,
        analysis_hash: str,
        ticket_payload: Mapping[str, Any],
    ) -> TrustedQuoteDecision:
        return cls(
            decision=StagingDecision.STAGED,
            analysis_hash=analysis_hash,
            ticket_payload=ticket_payload,
            block_code=None,
        )

    @classmethod
    def blocked(
        cls,
        *,
        block_code: str,
        analysis_hash: str | None = None,
    ) -> TrustedQuoteDecision:
        return cls(
            decision=StagingDecision.BLOCKED,
            analysis_hash=analysis_hash,
            ticket_payload=None,
            block_code=block_code,
        )


TrustedQuoteCallback = Callable[[TrustedQuoteRequest], TrustedQuoteDecision]


@dataclass(frozen=True, slots=True)
class StagingDocument:
    document_id: str
    request_hash: str
    idempotency_key_hash: str
    asset_id: str
    expected_analysis_hash: str
    decision: StagingDecision
    block_code: str | None
    ticket_payload: dict[str, Any] | None
    ticket_payload_hash: str | None
    created_at: datetime
    expires_at: datetime
    authority: NonAuthoritativeStaging
    document_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _DOCUMENT_SCHEMA,
            "document_id": self.document_id,
            "request": {
                "asset_id": self.asset_id,
                "expected_analysis_hash": self.expected_analysis_hash,
                "idempotency_key_hash": self.idempotency_key_hash,
                "request_hash": self.request_hash,
            },
            "decision": self.decision.value,
            "block_code": self.block_code,
            "ticket_payload": self.ticket_payload,
            "ticket_payload_hash": self.ticket_payload_hash,
            "created_at": _time_text(self.created_at, field_name="created_at"),
            "expires_at": _time_text(self.expires_at, field_name="expires_at"),
            "authority": self.authority.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class StagingEvent:
    sequence: int
    event_type: StagingEventType
    document_id: str
    occurred_at: datetime
    previous_chain_hash: str
    event_hash: str
    chain_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StagingView:
    document: StagingDocument
    state: StagingState
    expired_at: datetime | None
    latest_event_sequence: int
    chain_hash: str

    @property
    def authoritative(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        joined = "\n-- staging migration statement --\n".join(self.statements)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


_SCHEMA_V1 = _Migration(
    version=1,
    name="immutable_trade_staging_inbox",
    statements=(
        """
        CREATE TABLE staging_documents (
            document_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            idempotency_key_hash TEXT NOT NULL UNIQUE,
            asset_id TEXT NOT NULL,
            expected_analysis_hash TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('staged', 'blocked')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            document_hash TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_staging_documents_created
        ON staging_documents (created_at, document_id)
        """,
        """
        CREATE INDEX idx_staging_documents_expiry
        ON staging_documents (expires_at, document_id)
        """,
        """
        CREATE TABLE staging_events (
            sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('document_created', 'document_expired')
            ),
            document_id TEXT NOT NULL
                REFERENCES staging_documents(document_id),
            occurred_at TEXT NOT NULL,
            previous_chain_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            chain_hash TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (document_id, event_type)
        )
        """,
        """
        CREATE INDEX idx_staging_events_document
        ON staging_events (document_id, sequence)
        """,
        """
        CREATE TRIGGER staging_documents_no_update
        BEFORE UPDATE ON staging_documents
        BEGIN SELECT RAISE(ABORT, 'staging documents are immutable'); END
        """,
        """
        CREATE TRIGGER staging_documents_no_delete
        BEFORE DELETE ON staging_documents
        BEGIN SELECT RAISE(ABORT, 'staging documents are immutable'); END
        """,
        """
        CREATE TRIGGER staging_events_no_update
        BEFORE UPDATE ON staging_events
        BEGIN SELECT RAISE(ABORT, 'staging events are append-only'); END
        """,
        """
        CREATE TRIGGER staging_events_no_delete
        BEFORE DELETE ON staging_events
        BEGIN SELECT RAISE(ABORT, 'staging events are append-only'); END
        """,
    ),
)

_MIGRATIONS = (_SCHEMA_V1,)

_EXPECTED_COLUMNS = {
    "staging_schema_migrations": (
        "version",
        "name",
        "checksum",
        "applied_at",
    ),
    "staging_documents": (
        "document_id",
        "request_hash",
        "idempotency_key_hash",
        "asset_id",
        "expected_analysis_hash",
        "decision",
        "created_at",
        "expires_at",
        "document_hash",
        "payload_hash",
        "payload_json",
    ),
    "staging_events": (
        "sequence",
        "event_type",
        "document_id",
        "occurred_at",
        "previous_chain_hash",
        "event_hash",
        "chain_hash",
        "payload_hash",
        "payload_json",
    ),
}

_EXPECTED_INDEXES = frozenset(
    {
        "idx_staging_documents_created",
        "idx_staging_documents_expiry",
        "idx_staging_events_document",
    }
)

_EXPECTED_TRIGGERS = frozenset(
    {
        "staging_documents_no_update",
        "staging_documents_no_delete",
        "staging_events_no_update",
        "staging_events_no_delete",
    }
)

_EXPECTED_SCHEMA_SQL = {
    "staging_schema_migrations": """
        CREATE TABLE staging_schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """,
    "staging_documents": _SCHEMA_V1.statements[0],
    "idx_staging_documents_created": _SCHEMA_V1.statements[1],
    "idx_staging_documents_expiry": _SCHEMA_V1.statements[2],
    "staging_events": _SCHEMA_V1.statements[3],
    "idx_staging_events_document": _SCHEMA_V1.statements[4],
    "staging_documents_no_update": _SCHEMA_V1.statements[5],
    "staging_documents_no_delete": _SCHEMA_V1.statements[6],
    "staging_events_no_update": _SCHEMA_V1.statements[7],
    "staging_events_no_delete": _SCHEMA_V1.statements[8],
}


def _normalized_schema_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.rstrip("; ").split()).lower()


class TradeStagingInbox:
    """Durable immutable inbox whose records never confer trade authority.

    ``quote_callback`` is a trusted, deterministic, read-only compiler.  It is
    evaluated while the idempotency transaction is locked and may run again
    after a process crash that rolls that transaction back; it therefore must
    not reserve risk, grant authority, mutate an account, or perform a venue
    write.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        quote_callback: TrustedQuoteCallback,
        clock: Callable[[], datetime] | None = None,
        staged_ttl: timedelta = timedelta(minutes=15),
        blocked_ttl: timedelta = timedelta(minutes=5),
        busy_timeout_ms: int = 5_000,
        must_exist: bool = False,
    ) -> None:
        if str(path) == ":memory:":
            raise StagingValidationError(
                "TradeStagingInbox requires a file-backed database"
            )
        if not callable(quote_callback):
            raise StagingValidationError("quote_callback must be callable")
        if clock is not None and not callable(clock):
            raise StagingValidationError("clock must be callable")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise StagingValidationError("busy_timeout_ms must be a positive integer")
        if type(must_exist) is not bool:
            raise TypeError("must_exist must be a boolean")
        self.path = Path(path)
        self._quote_callback = quote_callback
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._staged_ttl = _positive_ttl(staged_ttl, field_name="staged_ttl")
        self._blocked_ttl = _positive_ttl(blocked_ttl, field_name="blocked_ttl")
        self._busy_timeout_ms = busy_timeout_ms
        self._must_exist = must_exist
        if not must_exist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def authority(self) -> NonAuthoritativeStaging:
        return NON_AUTHORITATIVE_STAGING

    def _now(self) -> datetime:
        return _utc(self._clock(), field_name="clock result")

    def _connect(
        self,
        *,
        read_only: bool = False,
        verification_path: Path | None = None,
    ) -> sqlite3.Connection:
        database_path = self.path if verification_path is None else verification_path
        database: str | Path = database_path
        if read_only:
            database = f"{database_path.absolute().as_uri()}?mode=ro"
        elif self._must_exist:
            database = f"{self.path.absolute().as_uri()}?mode=rw"
        connection = sqlite3.connect(
            database,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
            uri=read_only or self._must_exist,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms:d}")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA synchronous = FULL")
            try:
                validate_sqlite_file_sizes(
                    self.path,
                    max_bytes=MAX_SHARED_STATE_FILE_BYTES,
                )
            except StorageError as error:
                connection.close()
                raise StagingStorageError(str(error)) from error
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                enforce_sqlite_write_limit(
                    connection,
                    self.path,
                    max_bytes=MAX_SHARED_STATE_FILE_BYTES,
                    reserve_bytes=_STAGING_WRITE_HEADROOM_BYTES,
                )
            except StorageError as error:
                raise StagingStorageError(str(error)) from error
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise StagingStorageError(
                f"staging transaction failed: {type(error).__name__}"
            ) from error
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
                raise StagingStorageError(f"SQLite refused WAL mode: {mode}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                enforce_sqlite_write_limit(
                    connection,
                    self.path,
                    max_bytes=MAX_SHARED_STATE_FILE_BYTES,
                    reserve_bytes=_STAGING_WRITE_HEADROOM_BYTES,
                )
            except StorageError as error:
                raise StagingStorageError(str(error)) from error
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS staging_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            self._verify_table_columns(connection, "staging_schema_migrations")
            applied_rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM staging_schema_migrations ORDER BY version
                """
            ).fetchall()
            known = {migration.version: migration for migration in _MIGRATIONS}
            seen: list[int] = []
            for row in applied_rows:
                version = int(row["version"])
                migration = known.get(version)
                if migration is None:
                    raise StagingStorageError(
                        f"database has unknown staging migration version {version}"
                    )
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise StagingStorageError(
                        f"staging migration {version} checksum or name mismatch"
                    )
                seen.append(version)
            if seen != list(range(1, len(seen) + 1)):
                raise StagingStorageError(
                    "staging migration history is not contiguous"
                )

            applied = set(seen)
            applied_at = _time_text(self._now(), field_name="migration time")
            for migration in _MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO staging_schema_migrations (
                        version, name, checksum, applied_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        applied_at,
                    ),
                )
            self._verify_schema(connection)
            self._verify_integrity(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise StagingStorageError(
                f"staging schema initialization failed: {type(error).__name__}"
            ) from error
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_existing(self) -> None:
        try:
            with sqlite_verification_snapshot(
                self.path,
                label="staging database",
                max_bytes=MAX_SHARED_STATE_FILE_BYTES,
            ) as snapshot:
                connection = self._connect(
                    read_only=True,
                    verification_path=snapshot.database,
                )
                try:
                    query_only = connection.execute("PRAGMA query_only").fetchone()
                    if query_only is None or int(query_only[0]) != 1:
                        raise StagingStorageError(
                            "staging database verification is not query-only"
                        )
                    mode = connection.execute("PRAGMA journal_mode").fetchone()
                    if mode is None or str(mode[0]).lower() != "wal":
                        raise StagingStorageError(
                            "staging database is not in WAL mode"
                        )
                    quick_check = connection.execute("PRAGMA quick_check").fetchall()
                    if not quick_check or any(
                        str(row[0]).lower() != "ok" for row in quick_check
                    ):
                        raise StagingStorageError(
                            "staging database integrity check failed"
                        )
                    self._verify_current_migrations(connection)
                    self._verify_current_schema(connection)
                    self._verify_integrity(connection)
                finally:
                    connection.close()
        except StagingStorageError:
            raise
        except StorageError as error:
            raise StagingStorageError(str(error)) from error
        except sqlite3.Error as error:
            raise StagingStorageError(
                f"staging database verification failed: {type(error).__name__}"
            ) from error

    @staticmethod
    def _verify_current_migrations(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT version, name, checksum, applied_at
            FROM staging_schema_migrations ORDER BY version
            """
        ).fetchall()
        if len(rows) != len(_MIGRATIONS):
            raise StagingStorageError(
                "staging migration history is not current"
            )
        for row, migration in zip(rows, _MIGRATIONS, strict=True):
            if (
                row["version"] != migration.version
                or row["name"] != migration.name
                or row["checksum"] != migration.checksum
            ):
                raise StagingStorageError(
                    "staging migration history does not match current schema"
                )
            _parse_time(row["applied_at"], field_name="migration applied_at")

    @staticmethod
    def _verify_table_columns(connection: sqlite3.Connection, table: str) -> None:
        actual = tuple(
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != _EXPECTED_COLUMNS[table]:
            raise StagingStorageError(
                f"staging table {table} has an unexpected schema"
            )

    def _verify_schema(self, connection: sqlite3.Connection) -> None:
        for table in _EXPECTED_COLUMNS:
            self._verify_table_columns(connection, table)
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        if not _EXPECTED_INDEXES.issubset(indexes):
            raise StagingStorageError("staging schema is missing required indexes")
        triggers = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if not _EXPECTED_TRIGGERS.issubset(triggers):
            raise StagingStorageError("staging schema is missing immutability triggers")

    def _verify_current_schema(self, connection: sqlite3.Connection) -> None:
        self._verify_schema(connection)
        rows = connection.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
              AND sql IS NOT NULL
            """
        ).fetchall()
        actual_sql = {
            str(row["name"]): _normalized_schema_sql(row["sql"]) for row in rows
        }
        expected_sql = {
            name: _normalized_schema_sql(sql)
            for name, sql in _EXPECTED_SCHEMA_SQL.items()
        }
        expected_with_binding = {
            **expected_sql,
            STATE_BINDING_TABLE: _normalized_schema_sql(STATE_BINDING_TABLE_SQL),
        }
        if actual_sql not in (expected_sql, expected_with_binding):
            raise StagingStorageError(
                "staging tables, indexes, or immutability triggers do not match"
            )

    @staticmethod
    def _quote_result(
        callback: TrustedQuoteCallback,
        request: StageTradeRequest,
    ) -> TrustedQuoteDecision:
        try:
            result = callback(
                TrustedQuoteRequest(
                    asset_id=request.asset_id,
                    expected_analysis_hash=request.expected_analysis_hash,
                )
            )
        except Exception:
            return TrustedQuoteDecision.blocked(
                block_code="trusted_quote_unavailable"
            )
        if not isinstance(result, TrustedQuoteDecision):
            return TrustedQuoteDecision.blocked(block_code="trusted_quote_invalid")
        if (
            result.decision is StagingDecision.STAGED
            and result.analysis_hash != request.expected_analysis_hash
        ):
            return TrustedQuoteDecision.blocked(
                block_code="analysis_hash_mismatch",
                analysis_hash=result.analysis_hash,
            )
        return result

    @staticmethod
    def _build_document(
        *,
        request: StageTradeRequest,
        quote: TrustedQuoteDecision,
        created_at: datetime,
        expires_at: datetime,
    ) -> StagingDocument:
        idempotency_key_hash = _idempotency_hash(request.idempotency_key)
        request_hash = _request_hash(
            asset_id=request.asset_id,
            expected_analysis_hash=request.expected_analysis_hash,
            idempotency_key_hash=idempotency_key_hash,
        )
        document_id = _document_id(request_hash)
        ticket_payload = (
            None
            if quote.ticket_payload is None
            else json.loads(
                _bounded_canonical_json(
                    quote.ticket_payload,
                    maximum=_MAX_TICKET_BYTES,
                    label="trusted ticket payload",
                )
            )
        )
        ticket_payload_hash = (
            None
            if ticket_payload is None
            else domain_hash(_TICKET_HASH_DOMAIN, ticket_payload)
        )
        provisional = StagingDocument(
            document_id=document_id,
            request_hash=request_hash,
            idempotency_key_hash=idempotency_key_hash,
            asset_id=request.asset_id,
            expected_analysis_hash=request.expected_analysis_hash,
            decision=quote.decision,
            block_code=quote.block_code,
            ticket_payload=ticket_payload,
            ticket_payload_hash=ticket_payload_hash,
            created_at=created_at,
            expires_at=expires_at,
            authority=NON_AUTHORITATIVE_STAGING,
            document_hash="",
        )
        return StagingDocument(
            document_id=provisional.document_id,
            request_hash=provisional.request_hash,
            idempotency_key_hash=provisional.idempotency_key_hash,
            asset_id=provisional.asset_id,
            expected_analysis_hash=provisional.expected_analysis_hash,
            decision=provisional.decision,
            block_code=provisional.block_code,
            ticket_payload=provisional.ticket_payload,
            ticket_payload_hash=provisional.ticket_payload_hash,
            created_at=provisional.created_at,
            expires_at=provisional.expires_at,
            authority=provisional.authority,
            document_hash=domain_hash(_DOCUMENT_HASH_DOMAIN, provisional.as_dict()),
        )

    @staticmethod
    def _insert_document(
        connection: sqlite3.Connection, document: StagingDocument
    ) -> None:
        payload_json = _bounded_canonical_json(
            document.as_dict(), maximum=_MAX_DOCUMENT_BYTES, label="staging document"
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO staging_documents (
                document_id, request_hash, idempotency_key_hash, asset_id,
                expected_analysis_hash, decision, created_at, expires_at,
                document_hash, payload_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.document_id,
                document.request_hash,
                document.idempotency_key_hash,
                document.asset_id,
                document.expected_analysis_hash,
                document.decision.value,
                _time_text(document.created_at, field_name="created_at"),
                _time_text(document.expires_at, field_name="expires_at"),
                document.document_hash,
                payload_hash,
                payload_json,
            ),
        )

    @staticmethod
    def _document_from_row(row: Mapping[str, Any]) -> StagingDocument:
        decoded = _decode_canonical_json(
            row["payload_json"],
            row["payload_hash"],
            maximum=_MAX_DOCUMENT_BYTES,
            label="staging document",
        )
        if not isinstance(decoded, dict):
            raise StagingStorageError("persisted staging document is not an object")
        _exact_keys(
            decoded,
            {
                "schema_version",
                "document_id",
                "request",
                "decision",
                "block_code",
                "ticket_payload",
                "ticket_payload_hash",
                "created_at",
                "expires_at",
                "authority",
            },
            label="staging document",
        )
        if decoded["schema_version"] != _DOCUMENT_SCHEMA:
            raise StagingStorageError("persisted staging document schema is unsupported")
        request_value = decoded["request"]
        authority_value = decoded["authority"]
        if not isinstance(request_value, dict) or not isinstance(authority_value, dict):
            raise StagingStorageError(
                "persisted staging request or authority is not an object"
            )
        _exact_keys(
            request_value,
            {
                "asset_id",
                "expected_analysis_hash",
                "idempotency_key_hash",
                "request_hash",
            },
            label="staging request",
        )
        if authority_value != NON_AUTHORITATIVE_STAGING.as_dict():
            raise StagingStorageError(
                "persisted staging document contains capital authority"
            )

        document_id = _stored_text(
            decoded["document_id"], field_name="document_id", maximum=68
        )
        asset_id = _stored_text(request_value["asset_id"], field_name="asset_id")
        analysis_hash = _sha256(
            request_value["expected_analysis_hash"],
            field_name="expected_analysis_hash",
            stored=True,
        )
        idempotency_key_hash = _sha256(
            request_value["idempotency_key_hash"],
            field_name="idempotency_key_hash",
            stored=True,
        )
        request_hash = _sha256(
            request_value["request_hash"], field_name="request_hash", stored=True
        )
        expected_request_hash = _request_hash(
            asset_id=asset_id,
            expected_analysis_hash=analysis_hash,
            idempotency_key_hash=idempotency_key_hash,
        )
        if request_hash != expected_request_hash or document_id != _document_id(request_hash):
            raise StagingStorageError(
                "persisted staging request identity does not match"
            )
        try:
            decision = StagingDecision(decoded["decision"])
        except (TypeError, ValueError) as error:
            raise StagingStorageError(
                "persisted staging decision is unsupported"
            ) from error

        block_code_value = decoded["block_code"]
        ticket_payload_value = decoded["ticket_payload"]
        ticket_hash_value = decoded["ticket_payload_hash"]
        if decision is StagingDecision.STAGED:
            if block_code_value is not None or not isinstance(ticket_payload_value, dict):
                raise StagingStorageError("persisted staged document is malformed")
            ticket_hash = _sha256(
                ticket_hash_value, field_name="ticket_payload_hash", stored=True
            )
            if domain_hash(_TICKET_HASH_DOMAIN, ticket_payload_value) != ticket_hash:
                raise StagingStorageError(
                    "persisted staged ticket payload hash does not match"
                )
            block_code_parsed = None
            ticket_payload_parsed = ticket_payload_value
        else:
            if ticket_payload_value is not None or ticket_hash_value is not None:
                raise StagingStorageError("persisted blocked document carries a ticket")
            block_code_parsed = _block_code(block_code_value, stored=True)
            ticket_payload_parsed = None
            ticket_hash = None

        created_at = _parse_time(decoded["created_at"], field_name="created_at")
        expires_at = _parse_time(decoded["expires_at"], field_name="expires_at")
        if expires_at <= created_at:
            raise StagingStorageError(
                "persisted staging document does not expire after creation"
            )
        provisional = StagingDocument(
            document_id=document_id,
            request_hash=request_hash,
            idempotency_key_hash=idempotency_key_hash,
            asset_id=asset_id,
            expected_analysis_hash=analysis_hash,
            decision=decision,
            block_code=block_code_parsed,
            ticket_payload=ticket_payload_parsed,
            ticket_payload_hash=ticket_hash,
            created_at=created_at,
            expires_at=expires_at,
            authority=NON_AUTHORITATIVE_STAGING,
            document_hash="",
        )
        document_hash = _sha256(
            row["document_hash"], field_name="document_hash", stored=True
        )
        if domain_hash(_DOCUMENT_HASH_DOMAIN, provisional.as_dict()) != document_hash:
            raise StagingStorageError("persisted staging document hash does not match")
        document = StagingDocument(
            document_id=provisional.document_id,
            request_hash=provisional.request_hash,
            idempotency_key_hash=provisional.idempotency_key_hash,
            asset_id=provisional.asset_id,
            expected_analysis_hash=provisional.expected_analysis_hash,
            decision=provisional.decision,
            block_code=provisional.block_code,
            ticket_payload=provisional.ticket_payload,
            ticket_payload_hash=provisional.ticket_payload_hash,
            created_at=provisional.created_at,
            expires_at=provisional.expires_at,
            authority=provisional.authority,
            document_hash=document_hash,
        )
        column_values = {
            "document_id": document.document_id,
            "request_hash": document.request_hash,
            "idempotency_key_hash": document.idempotency_key_hash,
            "asset_id": document.asset_id,
            "expected_analysis_hash": document.expected_analysis_hash,
            "decision": document.decision.value,
            "created_at": _time_text(document.created_at, field_name="created_at"),
            "expires_at": _time_text(document.expires_at, field_name="expires_at"),
        }
        for name, expected in column_values.items():
            if row[name] != expected:
                raise StagingStorageError(
                    f"persisted staging document {name} column does not match"
                )
        return document

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        event_type: StagingEventType,
        document: StagingDocument,
        occurred_at: datetime,
    ) -> StagingEvent:
        previous_row = connection.execute(
            """
            SELECT sequence, chain_hash FROM staging_events
            ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        sequence = 1 if previous_row is None else int(previous_row["sequence"]) + 1
        previous_chain_hash = (
            _GENESIS_CHAIN_HASH
            if previous_row is None
            else _sha256(
                previous_row["chain_hash"], field_name="chain_hash", stored=True
            )
        )
        occurred_text = _time_text(occurred_at, field_name="event occurred_at")
        payload = {
            "sequence": sequence,
            "event_type": event_type.value,
            "document_id": document.document_id,
            "document_hash": document.document_hash,
            "occurred_at": occurred_text,
        }
        if event_type is StagingEventType.DOCUMENT_CREATED:
            payload["state"] = document.decision.value
        else:
            payload["previous_state"] = document.decision.value
            payload["expires_at"] = _time_text(
                document.expires_at, field_name="expires_at"
            )
            payload["state"] = StagingState.EXPIRED.value
        payload_json = _bounded_canonical_json(
            payload, maximum=_MAX_EVENT_BYTES, label="staging event"
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event_hash = domain_hash(_EVENT_HASH_DOMAIN, payload)
        chain_hash = domain_hash(
            _CHAIN_HASH_DOMAIN,
            {
                "previous_chain_hash": previous_chain_hash,
                "event_hash": event_hash,
            },
        )
        connection.execute(
            """
            INSERT INTO staging_events (
                sequence, event_type, document_id, occurred_at,
                previous_chain_hash, event_hash, chain_hash,
                payload_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_type.value,
                document.document_id,
                occurred_text,
                previous_chain_hash,
                event_hash,
                chain_hash,
                payload_hash,
                payload_json,
            ),
        )
        return StagingEvent(
            sequence=sequence,
            event_type=event_type,
            document_id=document.document_id,
            occurred_at=occurred_at,
            previous_chain_hash=previous_chain_hash,
            event_hash=event_hash,
            chain_hash=chain_hash,
            payload=payload,
        )

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> StagingEvent:
        payload = _decode_canonical_json(
            row["payload_json"],
            row["payload_hash"],
            maximum=_MAX_EVENT_BYTES,
            label="staging event",
        )
        if not isinstance(payload, dict):
            raise StagingStorageError("persisted staging event is not an object")
        try:
            event_type = StagingEventType(row["event_type"])
        except (TypeError, ValueError) as error:
            raise StagingStorageError(
                "persisted staging event type is unsupported"
            ) from error
        common = {
            "sequence",
            "event_type",
            "document_id",
            "document_hash",
            "occurred_at",
        }
        expected = (
            common | {"state"}
            if event_type is StagingEventType.DOCUMENT_CREATED
            else common | {"previous_state", "expires_at", "state"}
        )
        _exact_keys(payload, expected, label="staging event")
        sequence = payload["sequence"]
        if type(sequence) is not int or sequence <= 0 or row["sequence"] != sequence:
            raise StagingStorageError("persisted staging event sequence is invalid")
        document_id = _stored_text(
            payload["document_id"], field_name="event document_id", maximum=68
        )
        _sha256(
            payload["document_hash"], field_name="event document_hash", stored=True
        )
        occurred_at = _parse_time(payload["occurred_at"], field_name="event occurred_at")
        if (
            row["event_type"] != payload["event_type"]
            or row["document_id"] != document_id
            or row["occurred_at"]
            != _time_text(occurred_at, field_name="event occurred_at")
        ):
            raise StagingStorageError("persisted staging event columns do not match")
        previous_chain_hash = _sha256(
            row["previous_chain_hash"],
            field_name="previous_chain_hash",
            stored=True,
        )
        event_hash = _sha256(row["event_hash"], field_name="event_hash", stored=True)
        chain_hash = _sha256(row["chain_hash"], field_name="chain_hash", stored=True)
        if domain_hash(_EVENT_HASH_DOMAIN, payload) != event_hash:
            raise StagingStorageError("persisted staging event hash does not match")
        return StagingEvent(
            sequence=sequence,
            event_type=event_type,
            document_id=document_id,
            occurred_at=occurred_at,
            previous_chain_hash=previous_chain_hash,
            event_hash=event_hash,
            chain_hash=chain_hash,
            payload=payload,
        )

    def _verify_integrity(self, connection: sqlite3.Connection) -> str:
        document_rows = connection.execute(
            "SELECT * FROM staging_documents ORDER BY created_at, document_id"
        ).fetchall()
        documents: dict[str, StagingDocument] = {}
        for row in document_rows:
            document = self._document_from_row(row)
            if document.document_id in documents:
                raise StagingStorageError("duplicate staging document identity")
            documents[document.document_id] = document

        event_rows = connection.execute(
            "SELECT * FROM staging_events ORDER BY sequence"
        ).fetchall()
        previous = _GENESIS_CHAIN_HASH
        created_ids: set[str] = set()
        expired_ids: set[str] = set()
        for expected_sequence, row in enumerate(event_rows, start=1):
            event = self._event_from_row(row)
            if event.sequence != expected_sequence:
                raise StagingStorageError("staging event sequence is not contiguous")
            if event.previous_chain_hash != previous:
                raise StagingStorageError("staging event previous chain hash does not match")
            expected_chain = domain_hash(
                _CHAIN_HASH_DOMAIN,
                {
                    "previous_chain_hash": event.previous_chain_hash,
                    "event_hash": event.event_hash,
                },
            )
            if event.chain_hash != expected_chain:
                raise StagingStorageError("staging event chain hash does not match")
            document = documents.get(event.document_id)
            if document is None:
                raise StagingStorageError("staging event references an unknown document")
            if event.payload["document_hash"] != document.document_hash:
                raise StagingStorageError("staging event document hash does not match")
            if event.event_type is StagingEventType.DOCUMENT_CREATED:
                if (
                    event.document_id in created_ids
                    or event.occurred_at != document.created_at
                    or event.payload["state"] != document.decision.value
                ):
                    raise StagingStorageError("staging creation event is invalid")
                created_ids.add(event.document_id)
            else:
                if (
                    event.document_id not in created_ids
                    or event.document_id in expired_ids
                    or event.occurred_at < document.expires_at
                    or event.payload["previous_state"] != document.decision.value
                    or event.payload["state"] != StagingState.EXPIRED.value
                    or event.payload["expires_at"]
                    != _time_text(document.expires_at, field_name="expires_at")
                ):
                    raise StagingStorageError("staging expiry event is invalid")
                expired_ids.add(event.document_id)
            previous = event.chain_hash
        if created_ids != set(documents):
            raise StagingStorageError("a staging document lacks its creation event")
        return previous

    def _expire_due(self, connection: sqlite3.Connection, *, at: datetime) -> int:
        rows = connection.execute(
            """
            SELECT d.*
            FROM staging_documents AS d
            LEFT JOIN staging_events AS e
              ON e.document_id = d.document_id
             AND e.event_type = 'document_expired'
            WHERE d.expires_at <= ? AND e.sequence IS NULL
            ORDER BY d.expires_at, d.document_id
            LIMIT ?
            """,
            (
                _time_text(at, field_name="expiry check"),
                _MAX_EXPIRIES_PER_TRANSACTION,
            ),
        ).fetchall()
        for row in rows:
            document = self._document_from_row(row)
            self._append_event(
                connection,
                event_type=StagingEventType.DOCUMENT_EXPIRED,
                document=document,
                occurred_at=at,
            )
        return len(rows)

    def _expire_document_if_due(
        self,
        connection: sqlite3.Connection,
        document: StagingDocument,
        *,
        at: datetime,
    ) -> int:
        if document.expires_at > at:
            return 0
        existing = connection.execute(
            """
            SELECT 1 FROM staging_events
            WHERE document_id = ? AND event_type = 'document_expired'
            LIMIT 1
            """,
            (document.document_id,),
        ).fetchone()
        if existing is not None:
            return 0
        self._append_event(
            connection,
            event_type=StagingEventType.DOCUMENT_EXPIRED,
            document=document,
            occurred_at=at,
        )
        return 1

    @staticmethod
    def _view(connection: sqlite3.Connection, document: StagingDocument) -> StagingView:
        expiry_row = connection.execute(
            """
            SELECT sequence, occurred_at, chain_hash
            FROM staging_events
            WHERE document_id = ? AND event_type = 'document_expired'
            """,
            (document.document_id,),
        ).fetchone()
        latest_row = connection.execute(
            """
            SELECT sequence, chain_hash FROM staging_events
            ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        if latest_row is None:
            raise StagingStorageError("staging event ledger is unexpectedly empty")
        if expiry_row is None:
            state = StagingState(document.decision.value)
            expired_at = None
        else:
            state = StagingState.EXPIRED
            expired_at = _parse_time(
                expiry_row["occurred_at"], field_name="expiry occurred_at"
            )
        return StagingView(
            document=document,
            state=state,
            expired_at=expired_at,
            latest_event_sequence=int(latest_row["sequence"]),
            chain_hash=_sha256(
                latest_row["chain_hash"], field_name="chain_hash", stored=True
            ),
        )

    def stage(
        self, request: StageTradeRequest | Mapping[str, object]
    ) -> StagingView:
        """Stage exactly one immutable callback-produced decision.

        Repeating the same request with the same idempotency key returns the
        original document without calling the quote callback again.  Reusing
        the key for a different asset or analysis hash fails closed.
        """

        parsed = (
            request
            if type(request) is StageTradeRequest
            else StageTradeRequest.from_untrusted(request)
        )
        idempotency_key_hash = _idempotency_hash(parsed.idempotency_key)
        expected_request_hash = _request_hash(
            asset_id=parsed.asset_id,
            expected_analysis_hash=parsed.expected_analysis_hash,
            idempotency_key_hash=idempotency_key_hash,
        )
        with self._transaction() as connection:
            self._verify_integrity(connection)
            now = self._now()
            self._expire_due(connection, at=now)
            existing_row = connection.execute(
                """
                SELECT * FROM staging_documents WHERE idempotency_key_hash = ?
                """,
                (idempotency_key_hash,),
            ).fetchone()
            if existing_row is not None:
                existing = self._document_from_row(existing_row)
                if existing.request_hash != expected_request_hash:
                    raise StagingConflict(
                        "idempotency key is already bound to another staging request"
                    )
                self._verify_integrity(connection)
                return self._view(connection, existing)

            quote = self._quote_result(self._quote_callback, parsed)
            ttl = (
                self._staged_ttl
                if quote.decision is StagingDecision.STAGED
                else self._blocked_ttl
            )
            try:
                expires_at = now + ttl
            except OverflowError as error:
                raise StagingValidationError("staging expiry is outside UTC range") from error
            document = self._build_document(
                request=parsed,
                quote=quote,
                created_at=now,
                expires_at=expires_at,
            )
            self._insert_document(connection, document)
            self._append_event(
                connection,
                event_type=StagingEventType.DOCUMENT_CREATED,
                document=document,
                occurred_at=now,
            )
            self._expire_due(connection, at=self._now())
            self._verify_integrity(connection)
            return self._view(connection, document)

    def expire_due(self) -> int:
        """Append one bounded batch of due expiry events."""

        with self._transaction() as connection:
            self._verify_integrity(connection)
            count = self._expire_due(connection, at=self._now())
            self._verify_integrity(connection)
            return count

    def get(self, document_id: str) -> StagingView:
        """Read one document, materializing any clock-due expiry first."""

        identity = _text(document_id, field_name="document_id", maximum=68)
        with self._transaction() as connection:
            self._verify_integrity(connection)
            now = self._now()
            self._expire_due(connection, at=now)
            row = connection.execute(
                "SELECT * FROM staging_documents WHERE document_id = ?",
                (identity,),
            ).fetchone()
            if row is None:
                raise StagingNotFound("staging document does not exist")
            document = self._document_from_row(row)
            self._expire_document_if_due(connection, document, at=now)
            self._verify_integrity(connection)
            return self._view(connection, document)

    def get_by_idempotency_key(self, idempotency_key: str) -> StagingView:
        """Read one document by a key whose plaintext is never persisted."""

        key = _text(
            idempotency_key, field_name="idempotency_key", maximum=128
        )
        digest = _idempotency_hash(key)
        with self._transaction() as connection:
            self._verify_integrity(connection)
            now = self._now()
            self._expire_due(connection, at=now)
            row = connection.execute(
                """
                SELECT * FROM staging_documents WHERE idempotency_key_hash = ?
                """,
                (digest,),
            ).fetchone()
            if row is None:
                raise StagingNotFound("staging document does not exist")
            document = self._document_from_row(row)
            self._expire_document_if_due(connection, document, at=now)
            self._verify_integrity(connection)
            return self._view(connection, document)

    def list_documents(
        self,
        *,
        state: StagingState | str | None = None,
        limit: int = 100,
    ) -> tuple[StagingView, ...]:
        """List newest immutable documents with an optional effective-state filter."""

        if state is None:
            parsed_state = None
        else:
            try:
                parsed_state = state if isinstance(state, StagingState) else StagingState(state)
            except (TypeError, ValueError) as error:
                raise StagingValidationError("state filter is unsupported") from error
        if type(limit) is not int or limit <= 0 or limit > _MAX_LIST_LIMIT:
            raise StagingValidationError(
                f"limit must be an integer from 1 through {_MAX_LIST_LIMIT}"
            )
        with self._transaction() as connection:
            self._verify_integrity(connection)
            self._expire_due(connection, at=self._now())
            rows = connection.execute(
                """
                SELECT * FROM staging_documents
                ORDER BY created_at DESC, document_id DESC
                """
            ).fetchall()
            views: list[StagingView] = []
            for row in rows:
                document = self._document_from_row(row)
                view = self._view(connection, document)
                if parsed_state is None or view.state is parsed_state:
                    views.append(view)
                    if len(views) == limit:
                        break
            self._verify_integrity(connection)
            return tuple(views)

    def list_events(
        self, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[StagingEvent, ...]:
        """Read a bounded suffix of the verified global event chain."""

        if type(after_sequence) is not int or after_sequence < 0:
            raise StagingValidationError("after_sequence must be nonnegative")
        if type(limit) is not int or limit <= 0 or limit > _MAX_LIST_LIMIT:
            raise StagingValidationError(
                f"limit must be an integer from 1 through {_MAX_LIST_LIMIT}"
            )
        with self._transaction() as connection:
            self._verify_integrity(connection)
            self._expire_due(connection, at=self._now())
            rows = connection.execute(
                """
                SELECT * FROM staging_events
                WHERE sequence > ? ORDER BY sequence LIMIT ?
                """,
                (after_sequence, limit),
            ).fetchall()
            events = tuple(self._event_from_row(row) for row in rows)
            self._verify_integrity(connection)
            return events

    def verify_integrity(self) -> str:
        """Verify every document and event and return the current chain head."""

        with self._transaction() as connection:
            self._verify_schema(connection)
            return self._verify_integrity(connection)


__all__ = [
    "NON_AUTHORITATIVE_STAGING",
    "STAGING_INBOX_SCHEMA_VERSION",
    "NonAuthoritativeStaging",
    "StageTradeRequest",
    "StagingConflict",
    "StagingDecision",
    "StagingDocument",
    "StagingEvent",
    "StagingEventType",
    "StagingInboxError",
    "StagingNotFound",
    "StagingState",
    "StagingStorageError",
    "StagingValidationError",
    "StagingView",
    "TradeStagingInbox",
    "TrustedQuoteCallback",
    "TrustedQuoteDecision",
    "TrustedQuoteRequest",
]
