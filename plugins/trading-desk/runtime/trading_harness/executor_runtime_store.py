"""Durable, fenced control-plane state for the isolated TESTNET executor.

The runtime store owns only ``executor_runtime_*`` SQLite objects and can
therefore share the execution database without inspecting or mutating capital
tables.  It binds that database to one canonical executor configuration,
serializes a singleton worker lease with monotonically increasing fencing
tokens, keeps a hash-checked transition journal, and exposes only a redacted
read model.

No method loads credentials, signs, performs network I/O, or claims execution
work.  A runtime must first reconcile and explicitly transition its persisted
risk gate; acquisition and restart always begin halted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any

from .canonical import canonical_json, domain_hash
from .errors import StateConflict, StorageError, ValidationError
from .executor_config import ExecutorConfig, ExecutorConfigDrift
from .executor_status import ExecutorProcessState, ExecutorRiskGate


EXECUTOR_RUNTIME_SCHEMA_VERSION = 1
MAX_LEASE_SECONDS = 300
MIN_LEASE_SECONDS = 2
_ZERO_HASH = "0" * 64
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_EVENT_TYPES = frozenset(
    {
        "lease_acquired",
        "state_changed",
        "manual_halt_engaged",
        "manual_halt_cleared",
        "stop_requested",
        "stopped",
        "lease_released",
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


_PROCESS_TRANSITIONS: Mapping[ExecutorProcessState, frozenset[ExecutorProcessState]] = {
    ExecutorProcessState.STARTING: frozenset(
        {
            ExecutorProcessState.RUNNING,
            ExecutorProcessState.DEGRADED,
            ExecutorProcessState.STOPPING,
        }
    ),
    ExecutorProcessState.RUNNING: frozenset(
        {ExecutorProcessState.DEGRADED, ExecutorProcessState.STOPPING}
    ),
    ExecutorProcessState.DEGRADED: frozenset(
        {ExecutorProcessState.RUNNING, ExecutorProcessState.STOPPING}
    ),
    ExecutorProcessState.STOPPING: frozenset({ExecutorProcessState.STOPPED}),
    ExecutorProcessState.STOPPED: frozenset(),
}
_RISK_TRANSITIONS: Mapping[ExecutorRiskGate, frozenset[ExecutorRiskGate]] = {
    ExecutorRiskGate.HALTED: frozenset({ExecutorRiskGate.RECONCILING}),
    ExecutorRiskGate.RECONCILING: frozenset(
        {ExecutorRiskGate.HALTED, ExecutorRiskGate.READY}
    ),
    ExecutorRiskGate.READY: frozenset(
        {ExecutorRiskGate.HALTED, ExecutorRiskGate.RECONCILING}
    ),
}


Clock = Callable[[], datetime]


class RuntimeLeaseState(str, Enum):
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    RELEASED = "released"


class ManualHaltReason(str, Enum):
    OPERATOR = "operator"
    DAILY_LOSS = "daily_loss"
    STALE_DATA = "stale_data"
    RECONCILIATION = "reconciliation"
    INTERNAL_ERROR = "internal_error"


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _clock_read(clock: Clock) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise StorageError("executor runtime clock failed") from error
    return _utc(value, field="clock")


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


def _hash(value: object, *, field: str, stored: bool = False) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        exception = StorageError if stored else ValidationError
        raise exception(f"{field} must be a lowercase SHA-256 digest")
    return value


def _instance(value: object) -> str:
    if not isinstance(value, str) or not _INSTANCE_RE.fullmatch(value):
        raise ValidationError("instance_id must be a bounded identifier")
    return value


def _instance_fingerprint(instance_id: object) -> str:
    return domain_hash(
        "trading-harness/executor-runtime-instance/v1", _instance(instance_id)
    )


def _positive_int(value: object, *, field: str, stored: bool = False) -> int:
    if type(value) is not int or value <= 0:
        exception = StorageError if stored else ValidationError
        raise exception(f"{field} must be a positive integer")
    return value


def _lease_seconds(value: object) -> int:
    if type(value) is not int or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise ValidationError(
            f"lease_seconds must be an integer from {MIN_LEASE_SECONDS} through "
            f"{MAX_LEASE_SECONDS}"
        )
    return value


def _bool(value: object, *, field: str, stored: bool = False) -> bool:
    if type(value) is bool:
        return value
    if stored and type(value) is int and value in {0, 1}:
        return bool(value)
    exception = StorageError if stored else ValidationError
    raise exception(f"{field} must be boolean")


def _normalized_sql(value: object) -> str:
    return "" if not isinstance(value, str) else " ".join(value.rstrip(";").split()).lower()


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    config_hash: str
    deployment_fingerprint: str
    instance_fingerprint: str
    fencing_token: int
    lease_expires_at: datetime
    revision: int

    def __post_init__(self) -> None:
        for field in ("config_hash", "deployment_fingerprint", "instance_fingerprint"):
            _hash(getattr(self, field), field=field)
        _positive_int(self.fencing_token, field="fencing_token")
        _positive_int(self.revision, field="revision")
        object.__setattr__(
            self,
            "lease_expires_at",
            _utc(self.lease_expires_at, field="lease_expires_at"),
        )


@dataclass(frozen=True, slots=True)
class ExecutorRuntimeReadModel:
    config_hash: str
    deployment_fingerprint: str
    lease_state: RuntimeLeaseState
    fencing_token: int
    instance_fingerprint: str | None
    process_state: ExecutorProcessState
    declared_risk_gate: ExecutorRiskGate
    effective_risk_gate: ExecutorRiskGate
    manual_halt: bool
    manual_halt_reason: ManualHaltReason | None
    acquired_at: datetime | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    heartbeat_expires_at: datetime | None
    stop_requested_at: datetime | None
    released_at: datetime | None
    observed_at: datetime
    lease_current: bool
    heartbeat_current: bool
    revision: int
    state_hash: str | None
    journal_chain_hash: str
    read_model_hash: str

    def as_dict(self) -> dict[str, object]:
        def instant(value: datetime | None) -> str | None:
            return None if value is None else _time_text(value, field="status time")

        return {
            "config_hash": self.config_hash,
            "deployment_fingerprint": self.deployment_fingerprint,
            "lease_state": self.lease_state.value,
            "fencing_token": self.fencing_token,
            "instance_fingerprint": self.instance_fingerprint,
            "process_state": self.process_state.value,
            "declared_risk_gate": self.declared_risk_gate.value,
            "effective_risk_gate": self.effective_risk_gate.value,
            "manual_halt": self.manual_halt,
            "manual_halt_reason": (
                None if self.manual_halt_reason is None else self.manual_halt_reason.value
            ),
            "acquired_at": instant(self.acquired_at),
            "lease_expires_at": instant(self.lease_expires_at),
            "heartbeat_at": instant(self.heartbeat_at),
            "heartbeat_expires_at": instant(self.heartbeat_expires_at),
            "stop_requested_at": instant(self.stop_requested_at),
            "released_at": instant(self.released_at),
            "observed_at": instant(self.observed_at),
            "lease_current": self.lease_current,
            "heartbeat_current": self.heartbeat_current,
            "revision": self.revision,
            "state_hash": self.state_hash,
            "journal_chain_hash": self.journal_chain_hash,
            "read_model_hash": self.read_model_hash,
        }


@dataclass(frozen=True, slots=True)
class _RuntimeState:
    fencing_token: int
    lease_state: RuntimeLeaseState
    instance_fingerprint: str
    process_state: ExecutorProcessState
    risk_gate: ExecutorRiskGate
    manual_halt: bool
    manual_halt_reason: ManualHaltReason | None
    acquired_at: datetime
    renewed_at: datetime
    lease_expires_at: datetime
    heartbeat_at: datetime | None
    heartbeat_expires_at: datetime | None
    stop_requested_at: datetime | None
    released_at: datetime | None
    revision: int
    last_event_sequence: int
    last_event_hash: str
    updated_at: datetime
    record_hash: str


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS executor_runtime_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE executor_runtime_deployment (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        environment TEXT NOT NULL CHECK (environment = 'testnet'),
        config_hash TEXT NOT NULL,
        deployment_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        record_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE executor_runtime_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
        lease_state TEXT NOT NULL CHECK (lease_state IN ('active', 'released')),
        instance_fingerprint TEXT NOT NULL,
        process_state TEXT NOT NULL CHECK (
            process_state IN ('starting', 'running', 'degraded', 'stopping', 'stopped')
        ),
        risk_gate TEXT NOT NULL CHECK (risk_gate IN ('halted', 'reconciling', 'ready')),
        manual_halt INTEGER NOT NULL CHECK (manual_halt IN (0, 1)),
        manual_halt_reason TEXT CHECK (
            manual_halt_reason IS NULL OR manual_halt_reason IN (
                'operator', 'daily_loss', 'stale_data', 'reconciliation', 'internal_error'
            )
        ),
        acquired_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        heartbeat_at TEXT,
        heartbeat_expires_at TEXT,
        stop_requested_at TEXT,
        released_at TEXT,
        revision INTEGER NOT NULL CHECK (revision > 0),
        last_event_sequence INTEGER NOT NULL CHECK (last_event_sequence > 0),
        last_event_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        record_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE executor_runtime_events (
        event_sequence INTEGER PRIMARY KEY CHECK (event_sequence > 0),
        fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
        event_type TEXT NOT NULL CHECK (event_type IN (
            'lease_acquired', 'state_changed', 'manual_halt_engaged',
            'manual_halt_cleared', 'stop_requested', 'stopped', 'lease_released'
        )),
        recorded_at TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER executor_runtime_deployment_no_update
    BEFORE UPDATE ON executor_runtime_deployment
    BEGIN SELECT RAISE(ABORT, 'executor runtime deployment is immutable'); END
    """,
    """
    CREATE TRIGGER executor_runtime_deployment_no_delete
    BEFORE DELETE ON executor_runtime_deployment
    BEGIN SELECT RAISE(ABORT, 'executor runtime deployment is immutable'); END
    """,
    """
    CREATE TRIGGER executor_runtime_state_no_delete
    BEFORE DELETE ON executor_runtime_state
    BEGIN SELECT RAISE(ABORT, 'executor runtime state may not be deleted'); END
    """,
    """
    CREATE TRIGGER executor_runtime_events_no_update
    BEFORE UPDATE ON executor_runtime_events
    BEGIN SELECT RAISE(ABORT, 'executor runtime events are immutable'); END
    """,
    """
    CREATE TRIGGER executor_runtime_events_no_delete
    BEFORE DELETE ON executor_runtime_events
    BEGIN SELECT RAISE(ABORT, 'executor runtime events are immutable'); END
    """,
)
_SCHEMA_CHECKSUM = hashlib.sha256(
    "\n-- executor-runtime schema statement --\n".join(_SCHEMA_STATEMENTS).encode()
).hexdigest()
_EXPECTED_COLUMNS = {
    "executor_runtime_schema_migrations": (
        "version", "name", "checksum", "applied_at"
    ),
    "executor_runtime_deployment": (
        "singleton", "schema_version", "environment", "config_hash",
        "deployment_fingerprint", "created_at", "record_hash",
    ),
    "executor_runtime_state": (
        "singleton", "fencing_token", "lease_state", "instance_fingerprint",
        "process_state", "risk_gate", "manual_halt", "manual_halt_reason",
        "acquired_at", "renewed_at", "lease_expires_at", "heartbeat_at",
        "heartbeat_expires_at", "stop_requested_at", "released_at", "revision",
        "last_event_sequence", "last_event_hash", "updated_at", "record_hash",
    ),
    "executor_runtime_events": (
        "event_sequence", "fencing_token", "event_type", "recorded_at",
        "previous_hash", "payload_json", "payload_hash", "event_hash",
    ),
}
_EXPECTED_TRIGGERS = {
    "executor_runtime_deployment_no_update": _SCHEMA_STATEMENTS[4],
    "executor_runtime_deployment_no_delete": _SCHEMA_STATEMENTS[5],
    "executor_runtime_state_no_delete": _SCHEMA_STATEMENTS[6],
    "executor_runtime_events_no_update": _SCHEMA_STATEMENTS[7],
    "executor_runtime_events_no_delete": _SCHEMA_STATEMENTS[8],
}


def _runtime_schema_objects(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE sql IS NOT NULL AND (
            name LIKE 'executor_runtime_%'
            OR tbl_name LIKE 'executor_runtime_%'
        )
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        (str(row["type"]), str(row["name"]), _normalized_sql(row["sql"]))
        for row in rows
    )


@lru_cache(maxsize=1)
def _expected_runtime_schema_objects() -> tuple[tuple[str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        return _runtime_schema_objects(connection)
    finally:
        connection.close()


class ExecutorRuntimeStore:
    """Config-bound singleton lease and fail-closed runtime state machine."""

    def __init__(
        self,
        config: ExecutorConfig,
        *,
        clock: Clock | None = None,
        must_exist: bool = False,
    ) -> None:
        if not isinstance(config, ExecutorConfig):
            raise TypeError("config must be ExecutorConfig")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if type(must_exist) is not bool:
            raise TypeError("must_exist must be a boolean")
        database = config.paths.execution_database
        if database.exists() and database.is_symlink():
            raise ValidationError("executor runtime database may not be a symlink")
        if not database.parent.is_dir() or database.parent.is_symlink():
            raise ValidationError("executor runtime database parent must be a real directory")
        self._config = config
        self._database = database
        self._must_exist = must_exist
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._deployment_fingerprint = domain_hash(
            "trading-harness/executor-runtime-deployment/v1",
            {"schema_version": EXECUTOR_RUNTIME_SCHEMA_VERSION, "config_hash": config.config_hash},
        )
        self._initialize()

    @contextmanager
    def _connection(
        self,
        *,
        read_only: bool = False,
        verification_path: Path | None = None,
    ) -> Iterator[sqlite3.Connection]:
        try:
            database_path = (
                self._database if verification_path is None else verification_path
            )
            database: str | Path = database_path
            if read_only:
                immutable = "&immutable=1" if verification_path is None else ""
                database = f"{database_path.absolute().as_uri()}?mode=ro{immutable}"
            elif self._must_exist:
                database = f"{self._database.absolute().as_uri()}?mode=rw"
            connection = sqlite3.connect(
                database,
                timeout=5,
                isolation_level=None,
                uri=read_only or self._must_exist,
            )
        except sqlite3.Error as error:
            raise StorageError("executor runtime database is unavailable") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        except sqlite3.Error as error:
            raise StorageError("executor runtime database operation failed") from error
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _now(self) -> datetime:
        return _clock_read(self._clock)

    def _initialize(self) -> None:
        if self._must_exist:
            self._verify_existing()
            return
        now = self._now()
        with self._write() as connection:
            connection.execute(_SCHEMA_STATEMENTS[0])
            migration = connection.execute(
                "SELECT name, checksum FROM executor_runtime_schema_migrations WHERE version = 1"
            ).fetchone()
            fresh_schema = migration is None
            if migration is None:
                for statement in _SCHEMA_STATEMENTS[1:]:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO executor_runtime_schema_migrations
                        (version, name, checksum, applied_at)
                    VALUES (1, 'fenced_runtime_v1', ?, ?)
                    """,
                    (_SCHEMA_CHECKSUM, _time_text(now, field="applied_at")),
                )
            elif (
                migration["name"] != "fenced_runtime_v1"
                or migration["checksum"] != _SCHEMA_CHECKSUM
            ):
                raise StorageError("executor runtime migration checksum does not match")
            self._verify_schema_locked(connection)
            deployment = connection.execute(
                "SELECT * FROM executor_runtime_deployment WHERE singleton = 1"
            ).fetchone()
            if deployment is None:
                if not fresh_schema:
                    raise StorageError("executor runtime deployment binding is missing")
                material = {
                    "schema_version": EXECUTOR_RUNTIME_SCHEMA_VERSION,
                    "environment": "testnet",
                    "config_hash": self._config.config_hash,
                    "deployment_fingerprint": self._deployment_fingerprint,
                    "created_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO executor_runtime_deployment (
                        singleton, schema_version, environment, config_hash,
                        deployment_fingerprint, created_at, record_hash
                    ) VALUES (1, 1, 'testnet', ?, ?, ?, ?)
                    """,
                    (
                        self._config.config_hash,
                        self._deployment_fingerprint,
                        _time_text(now, field="created_at"),
                        domain_hash("trading-harness/executor-runtime-binding/v1", material),
                    ),
                )
            else:
                self._verify_deployment_row(deployment)
            tail = self._verify_journal_locked(connection)
            state_row = connection.execute(
                "SELECT * FROM executor_runtime_state WHERE singleton = 1"
            ).fetchone()
            if state_row is not None:
                state = self._state_from_row(state_row)
                if (state.last_event_sequence, state.last_event_hash) != tail:
                    raise StorageError("runtime state does not reference the journal tail")
            elif tail != (0, _ZERO_HASH):
                raise StorageError("runtime journal exists without runtime state")

    def _verify_existing(self) -> None:
        database_snapshot = _snapshot_regular_file(
            self._database, label="executor runtime database"
        )
        header = database_snapshot[2]
        if (
            len(header) != 20
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x02\x02"
        ):
            raise StorageError(
                "executor runtime database is not a WAL-mode SQLite file"
            )
        verification_directory: tempfile.TemporaryDirectory[str] | None = None
        verification_path: Path | None = None
        wal_path = Path(f"{self._database}-wal")
        wal_snapshot = (
            _snapshot_regular_file(wal_path, label="executor runtime WAL")
            if os.path.lexists(wal_path)
            else None
        )
        try:
            if wal_snapshot is not None and wal_snapshot[0][6] > 0:
                verification_directory = tempfile.TemporaryDirectory(
                    prefix=".executor-runtime-verify-",
                    dir=self._database.parent,
                )
                verification_path = (
                    Path(verification_directory.name) / self._database.name
                )
                _copy_verification_file(
                    self._database,
                    verification_path,
                    label="executor runtime database",
                    expected=database_snapshot,
                )
                _copy_verification_file(
                    wal_path,
                    Path(f"{verification_path}-wal"),
                    label="executor runtime WAL",
                    expected=wal_snapshot,
                )
                if (
                    _snapshot_regular_file(
                        self._database, label="executor runtime database"
                    )
                    != database_snapshot
                    or _snapshot_regular_file(
                        wal_path, label="executor runtime WAL"
                    )
                    != wal_snapshot
                ):
                    raise StorageError(
                        "executor runtime database changed during verification snapshot"
                    )
            with self._connection(
                read_only=True, verification_path=verification_path
            ) as connection:
                self._verify_existing_locked(connection)
            current_wal_snapshot = (
                _snapshot_regular_file(wal_path, label="executor runtime WAL")
                if os.path.lexists(wal_path)
                else None
            )
            if (
                _snapshot_regular_file(
                    self._database, label="executor runtime database"
                )
                != database_snapshot
                or current_wal_snapshot != wal_snapshot
            ):
                raise StorageError(
                    "executor runtime database changed during verification"
                )
        except OSError as error:
            raise StorageError(
                "executor runtime database snapshot is unavailable"
            ) from error
        finally:
            if verification_directory is not None:
                verification_directory.cleanup()

    def _verify_existing_locked(self, connection: sqlite3.Connection) -> None:
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise StorageError(
                "executor runtime verification is not query-only"
            )
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if not integrity or any(str(row[0]).lower() != "ok" for row in integrity):
            raise StorageError("executor runtime database integrity check failed")
        self._verify_schema_locked(connection)
        self._verify_binding_locked(connection)
        tail = self._verify_journal_locked(connection)
        state_row = connection.execute(
            "SELECT * FROM executor_runtime_state WHERE singleton = 1"
        ).fetchone()
        if state_row is not None:
            state = self._state_from_row(state_row)
            if (state.last_event_sequence, state.last_event_hash) != tail:
                raise StorageError(
                    "runtime state does not reference the journal tail"
                )
        elif tail != (0, _ZERO_HASH):
            raise StorageError("runtime journal exists without runtime state")

    @staticmethod
    def _verify_schema_locked(connection: sqlite3.Connection) -> None:
        for table, expected in _EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if tuple(row["name"] for row in rows) != expected:
                raise StorageError(f"executor runtime table schema does not match: {table}")
        migrations = connection.execute(
            """
            SELECT version, name, checksum
            FROM executor_runtime_schema_migrations ORDER BY version
            """
        ).fetchall()
        if len(migrations) != 1 or (
            migrations[0]["version"] != EXECUTOR_RUNTIME_SCHEMA_VERSION
            or migrations[0]["name"] != "fenced_runtime_v1"
            or migrations[0]["checksum"] != _SCHEMA_CHECKSUM
        ):
            raise StorageError("executor runtime migration history does not match")
        placeholders = ",".join("?" for _ in _EXPECTED_TRIGGERS)
        rows = connection.execute(
            f"SELECT name, sql FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(_EXPECTED_TRIGGERS),
        ).fetchall()
        actual = {row["name"]: _normalized_sql(row["sql"]) for row in rows}
        expected = {name: _normalized_sql(sql) for name, sql in _EXPECTED_TRIGGERS.items()}
        if actual != expected:
            raise StorageError("executor runtime immutability triggers do not match")
        if _runtime_schema_objects(connection) != _expected_runtime_schema_objects():
            raise StorageError("executor runtime database schema does not match")

    def _verify_deployment_row(self, row: sqlite3.Row) -> None:
        created_at = _parse_time(row["created_at"], field="deployment created_at")
        material = {
            "schema_version": row["schema_version"],
            "environment": row["environment"],
            "config_hash": row["config_hash"],
            "deployment_fingerprint": row["deployment_fingerprint"],
            "created_at": created_at,
        }
        record_hash = _hash(row["record_hash"], field="deployment record_hash", stored=True)
        if record_hash != domain_hash("trading-harness/executor-runtime-binding/v1", material):
            raise StorageError("executor runtime deployment hash does not match")
        if (
            row["schema_version"] != EXECUTOR_RUNTIME_SCHEMA_VERSION
            or row["environment"] != "testnet"
            or row["config_hash"] != self._config.config_hash
            or row["deployment_fingerprint"] != self._deployment_fingerprint
        ):
            raise ExecutorConfigDrift(
                "executor runtime database is bound to a different configuration"
            )

    def _verify_binding_locked(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT * FROM executor_runtime_deployment WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StorageError("executor runtime deployment binding is missing")
        self._verify_deployment_row(row)

    def _verify_journal_locked(self, connection: sqlite3.Connection) -> tuple[int, str]:
        rows = connection.execute(
            "SELECT * FROM executor_runtime_events ORDER BY event_sequence"
        ).fetchall()
        previous = _ZERO_HASH
        expected_sequence = 1
        previous_recorded_at: datetime | None = None
        for row in rows:
            if row["event_sequence"] != expected_sequence:
                raise StorageError("executor runtime journal sequence has a gap")
            if row["event_type"] not in _EVENT_TYPES:
                raise StorageError("executor runtime journal event type is invalid")
            token = _positive_int(
                row["fencing_token"], field="persisted fencing_token", stored=True
            )
            recorded_at = _parse_time(row["recorded_at"], field="event recorded_at")
            if previous_recorded_at is not None and recorded_at < previous_recorded_at:
                raise StorageError("executor runtime journal time moved backwards")
            if _hash(row["previous_hash"], field="previous_hash", stored=True) != previous:
                raise StorageError("executor runtime journal chain is broken")
            payload_json = row["payload_json"]
            if not isinstance(payload_json, str) or len(payload_json.encode()) > 16 * 1024:
                raise StorageError("executor runtime journal payload is invalid")
            payload_hash = _hash(row["payload_hash"], field="payload_hash", stored=True)
            if hashlib.sha256(payload_json.encode()).hexdigest() != payload_hash:
                raise StorageError("executor runtime journal payload hash does not match")
            try:
                payload = json.loads(payload_json)
                if canonical_json(payload) != payload_json or not isinstance(payload, dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise StorageError("executor runtime journal payload is not canonical") from error
            event_hash = _hash(row["event_hash"], field="event_hash", stored=True)
            expected_hash = domain_hash(
                "trading-harness/executor-runtime-event/v1",
                {
                    "deployment_fingerprint": self._deployment_fingerprint,
                    "event_sequence": expected_sequence,
                    "fencing_token": token,
                    "event_type": row["event_type"],
                    "recorded_at": recorded_at,
                    "previous_hash": previous,
                    "payload_hash": payload_hash,
                },
            )
            if event_hash != expected_hash:
                raise StorageError("executor runtime journal event hash does not match")
            previous = event_hash
            previous_recorded_at = recorded_at
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _state_material(self, state: _RuntimeState) -> dict[str, object]:
        return {
            "deployment_fingerprint": self._deployment_fingerprint,
            "fencing_token": state.fencing_token,
            "lease_state": state.lease_state.value,
            "instance_fingerprint": state.instance_fingerprint,
            "process_state": state.process_state.value,
            "risk_gate": state.risk_gate.value,
            "manual_halt": state.manual_halt,
            "manual_halt_reason": (
                None if state.manual_halt_reason is None else state.manual_halt_reason.value
            ),
            "acquired_at": state.acquired_at,
            "renewed_at": state.renewed_at,
            "lease_expires_at": state.lease_expires_at,
            "heartbeat_at": state.heartbeat_at,
            "heartbeat_expires_at": state.heartbeat_expires_at,
            "stop_requested_at": state.stop_requested_at,
            "released_at": state.released_at,
            "revision": state.revision,
            "last_event_sequence": state.last_event_sequence,
            "last_event_hash": state.last_event_hash,
            "updated_at": state.updated_at,
        }

    def _state_from_row(self, row: sqlite3.Row) -> _RuntimeState:
        try:
            lease_state = RuntimeLeaseState(row["lease_state"])
            process_state = ExecutorProcessState(row["process_state"])
            risk_gate = ExecutorRiskGate(row["risk_gate"])
            reason = (
                None
                if row["manual_halt_reason"] is None
                else ManualHaltReason(row["manual_halt_reason"])
            )
        except (TypeError, ValueError) as error:
            raise StorageError("persisted executor runtime enum is invalid") from error
        if lease_state is RuntimeLeaseState.NOT_STARTED:
            raise StorageError("persisted lease state may not be not_started")
        state = _RuntimeState(
            fencing_token=_positive_int(
                row["fencing_token"], field="fencing_token", stored=True
            ),
            lease_state=lease_state,
            instance_fingerprint=_hash(
                row["instance_fingerprint"], field="instance_fingerprint", stored=True
            ),
            process_state=process_state,
            risk_gate=risk_gate,
            manual_halt=_bool(row["manual_halt"], field="manual_halt", stored=True),
            manual_halt_reason=reason,
            acquired_at=_parse_time(row["acquired_at"], field="acquired_at"),
            renewed_at=_parse_time(row["renewed_at"], field="renewed_at"),
            lease_expires_at=_parse_time(row["lease_expires_at"], field="lease_expires_at"),
            heartbeat_at=_optional_time(row["heartbeat_at"], field="heartbeat_at"),
            heartbeat_expires_at=_optional_time(
                row["heartbeat_expires_at"], field="heartbeat_expires_at"
            ),
            stop_requested_at=_optional_time(
                row["stop_requested_at"], field="stop_requested_at"
            ),
            released_at=_optional_time(row["released_at"], field="released_at"),
            revision=_positive_int(row["revision"], field="revision", stored=True),
            last_event_sequence=_positive_int(
                row["last_event_sequence"], field="last_event_sequence", stored=True
            ),
            last_event_hash=_hash(
                row["last_event_hash"], field="last_event_hash", stored=True
            ),
            updated_at=_parse_time(row["updated_at"], field="updated_at"),
            record_hash=_hash(row["record_hash"], field="record_hash", stored=True),
        )
        if not state.acquired_at <= state.renewed_at <= state.lease_expires_at:
            raise StorageError("persisted executor runtime lease interval is invalid")
        if (state.heartbeat_at is None) != (state.heartbeat_expires_at is None):
            raise StorageError("persisted executor runtime heartbeat is incomplete")
        if state.heartbeat_at is not None and not (
            state.acquired_at
            <= state.heartbeat_at
            <= state.heartbeat_expires_at  # type: ignore[operator]
            <= state.lease_expires_at
        ):
            raise StorageError("persisted executor runtime heartbeat interval is invalid")
        if state.manual_halt != (state.manual_halt_reason is not None):
            raise StorageError("persisted executor manual halt is inconsistent")
        if state.manual_halt and state.risk_gate is not ExecutorRiskGate.HALTED:
            raise StorageError("persisted manual halt does not halt the risk gate")
        if state.risk_gate is ExecutorRiskGate.READY and (
            state.process_state is not ExecutorProcessState.RUNNING
            or state.heartbeat_expires_at is None
        ):
            raise StorageError("persisted ready risk gate lacks a running heartbeat")
        if state.lease_state is RuntimeLeaseState.RELEASED:
            if state.released_at is None or state.process_state is not ExecutorProcessState.STOPPED:
                raise StorageError("released executor runtime state is inconsistent")
        elif state.released_at is not None:
            raise StorageError("active executor runtime has a release timestamp")
        if state.process_state in {ExecutorProcessState.STOPPING, ExecutorProcessState.STOPPED}:
            if state.stop_requested_at is None or state.risk_gate is not ExecutorRiskGate.HALTED:
                raise StorageError("stopping executor runtime state is inconsistent")
        elif state.stop_requested_at is not None:
            raise StorageError("running executor runtime has a stop timestamp")
        if state.updated_at < state.acquired_at:
            raise StorageError("persisted executor runtime update precedes acquisition")
        if state.stop_requested_at is not None and not (
            state.acquired_at <= state.stop_requested_at <= state.updated_at
        ):
            raise StorageError("persisted executor stop timestamp is invalid")
        if state.released_at is not None and not (
            state.stop_requested_at is not None
            and state.stop_requested_at <= state.released_at <= state.updated_at
        ):
            raise StorageError("persisted executor release timestamp is invalid")
        expected_hash = domain_hash(
            "trading-harness/executor-runtime-state/v1", self._state_material(state)
        )
        if state.record_hash != expected_hash:
            raise StorageError("executor runtime state hash does not match")
        return state

    def _current_locked(
        self, connection: sqlite3.Connection
    ) -> tuple[_RuntimeState | None, tuple[int, str]]:
        self._verify_schema_locked(connection)
        self._verify_binding_locked(connection)
        tail = self._verify_journal_locked(connection)
        row = connection.execute(
            "SELECT * FROM executor_runtime_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            if tail != (0, _ZERO_HASH):
                raise StorageError("executor runtime state is missing")
            return None, tail
        state = self._state_from_row(row)
        if (state.last_event_sequence, state.last_event_hash) != tail:
            raise StorageError("executor runtime state journal tail does not match")
        return state, tail

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        tail: tuple[int, str],
        fencing_token: int,
        event_type: str,
        at: datetime,
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        if event_type not in _EVENT_TYPES:
            raise ValidationError("executor runtime event type is invalid")
        payload_json = canonical_json(payload)
        if len(payload_json.encode()) > 16 * 1024:
            raise ValidationError("executor runtime event payload is too large")
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        sequence = tail[0] + 1
        event_hash = domain_hash(
            "trading-harness/executor-runtime-event/v1",
            {
                "deployment_fingerprint": self._deployment_fingerprint,
                "event_sequence": sequence,
                "fencing_token": fencing_token,
                "event_type": event_type,
                "recorded_at": at,
                "previous_hash": tail[1],
                "payload_hash": payload_hash,
            },
        )
        connection.execute(
            """
            INSERT INTO executor_runtime_events (
                event_sequence, fencing_token, event_type, recorded_at,
                previous_hash, payload_json, payload_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                fencing_token,
                event_type,
                _time_text(at, field="event recorded_at"),
                tail[1],
                payload_json,
                payload_hash,
                event_hash,
            ),
        )
        return sequence, event_hash

    def _persist_state_locked(
        self,
        connection: sqlite3.Connection,
        *,
        prior: _RuntimeState | None,
        state: _RuntimeState,
    ) -> _RuntimeState:
        completed = replace(
            state,
            record_hash=domain_hash(
                "trading-harness/executor-runtime-state/v1", self._state_material(state)
            ),
        )
        values = (
            completed.fencing_token,
            completed.lease_state.value,
            completed.instance_fingerprint,
            completed.process_state.value,
            completed.risk_gate.value,
            int(completed.manual_halt),
            None if completed.manual_halt_reason is None else completed.manual_halt_reason.value,
            _time_text(completed.acquired_at, field="acquired_at"),
            _time_text(completed.renewed_at, field="renewed_at"),
            _time_text(completed.lease_expires_at, field="lease_expires_at"),
            (
                None
                if completed.heartbeat_at is None
                else _time_text(completed.heartbeat_at, field="heartbeat_at")
            ),
            (
                None
                if completed.heartbeat_expires_at is None
                else _time_text(
                    completed.heartbeat_expires_at,
                    field="heartbeat_expires_at",
                )
            ),
            (
                None
                if completed.stop_requested_at is None
                else _time_text(
                    completed.stop_requested_at,
                    field="stop_requested_at",
                )
            ),
            (
                None
                if completed.released_at is None
                else _time_text(completed.released_at, field="released_at")
            ),
            completed.revision,
            completed.last_event_sequence,
            completed.last_event_hash,
            _time_text(completed.updated_at, field="updated_at"),
            completed.record_hash,
        )
        if prior is None:
            connection.execute(
                """
                INSERT INTO executor_runtime_state (
                    singleton, fencing_token, lease_state, instance_fingerprint,
                    process_state, risk_gate, manual_halt, manual_halt_reason,
                    acquired_at, renewed_at, lease_expires_at, heartbeat_at,
                    heartbeat_expires_at, stop_requested_at, released_at, revision,
                    last_event_sequence, last_event_hash, updated_at, record_hash
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            cursor = connection.execute(
                """
                UPDATE executor_runtime_state SET
                    fencing_token = ?, lease_state = ?, instance_fingerprint = ?,
                    process_state = ?, risk_gate = ?, manual_halt = ?,
                    manual_halt_reason = ?, acquired_at = ?, renewed_at = ?,
                    lease_expires_at = ?, heartbeat_at = ?, heartbeat_expires_at = ?,
                    stop_requested_at = ?, released_at = ?, revision = ?,
                    last_event_sequence = ?, last_event_hash = ?, updated_at = ?,
                    record_hash = ?
                WHERE singleton = 1 AND revision = ? AND record_hash = ?
                """,
                values + (prior.revision, prior.record_hash),
            )
            if cursor.rowcount != 1:
                raise StateConflict("executor runtime state changed concurrently")
        row = connection.execute(
            "SELECT * FROM executor_runtime_state WHERE singleton = 1"
        ).fetchone()
        if row is None:  # pragma: no cover - the write above is in this transaction
            raise StorageError("persisted executor runtime state is missing")
        verified = self._state_from_row(row)
        if verified != completed:
            raise StorageError("persisted executor runtime state differs from transition")
        return verified

    def _transition_locked(
        self,
        connection: sqlite3.Connection,
        *,
        prior: _RuntimeState | None,
        tail: tuple[int, str],
        at: datetime,
        event_type: str,
        fencing_token: int,
        changes: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> _RuntimeState:
        if prior is None:
            raise StorageError("runtime transition requires an initial state template")
        if at < prior.updated_at:
            raise StateConflict("executor runtime clock moved backwards")
        next_tail = self._append_event_locked(
            connection,
            tail=tail,
            fencing_token=fencing_token,
            event_type=event_type,
            at=at,
            payload=payload,
        )
        target = replace(
            prior,
            **changes,
            revision=prior.revision + 1,
            last_event_sequence=next_tail[0],
            last_event_hash=next_tail[1],
            updated_at=at,
            record_hash=_ZERO_HASH,
        )
        return self._persist_state_locked(connection, prior=prior, state=target)

    @staticmethod
    def _lease_authority(
        state: _RuntimeState,
        *,
        instance_fingerprint: str,
        fencing_token: object,
        now: datetime,
    ) -> None:
        token = _positive_int(fencing_token, field="fencing_token")
        if (
            state.lease_state is not RuntimeLeaseState.ACTIVE
            or state.fencing_token != token
            or state.instance_fingerprint != instance_fingerprint
            or state.lease_expires_at <= now
        ):
            raise StateConflict("executor runtime lease authority is stale or absent")

    def _lease(self, state: _RuntimeState) -> RuntimeLease:
        return RuntimeLease(
            config_hash=self._config.config_hash,
            deployment_fingerprint=self._deployment_fingerprint,
            instance_fingerprint=state.instance_fingerprint,
            fencing_token=state.fencing_token,
            lease_expires_at=state.lease_expires_at,
            revision=state.revision,
        )

    def acquire(self, *, instance_id: object, lease_seconds: object) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        duration = _lease_seconds(lease_seconds)
        now = self._now()
        expires = now + timedelta(seconds=duration)
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is not None and now < prior.updated_at:
                raise StateConflict("executor runtime clock moved backwards")
            if (
                prior is not None
                and prior.lease_state is RuntimeLeaseState.ACTIVE
                and prior.lease_expires_at > now
            ):
                raise StateConflict("another executor runtime lease is active")
            token = 1 if prior is None else prior.fencing_token + 1
            manual_halt = False if prior is None else prior.manual_halt
            reason = None if prior is None else prior.manual_halt_reason
            takeover = (
                "first_start"
                if prior is None
                else (
                    "released_restart"
                    if prior.lease_state is RuntimeLeaseState.RELEASED
                    else "expired_takeover"
                )
            )
            next_tail = self._append_event_locked(
                connection,
                tail=tail,
                fencing_token=token,
                event_type="lease_acquired",
                at=now,
                payload={
                    "instance_fingerprint": fingerprint,
                    "takeover": takeover,
                    "previous_fencing_token": None if prior is None else prior.fencing_token,
                    "lease_expires_at": expires,
                    "manual_halt": manual_halt,
                },
            )
            target = _RuntimeState(
                fencing_token=token,
                lease_state=RuntimeLeaseState.ACTIVE,
                instance_fingerprint=fingerprint,
                process_state=ExecutorProcessState.STARTING,
                risk_gate=ExecutorRiskGate.HALTED,
                manual_halt=manual_halt,
                manual_halt_reason=reason,
                acquired_at=now,
                renewed_at=now,
                lease_expires_at=expires,
                heartbeat_at=None,
                heartbeat_expires_at=None,
                stop_requested_at=None,
                released_at=None,
                revision=1 if prior is None else prior.revision + 1,
                last_event_sequence=next_tail[0],
                last_event_hash=next_tail[1],
                updated_at=now,
                record_hash=_ZERO_HASH,
            )
            persisted = self._persist_state_locked(
                connection, prior=prior, state=target
            )
            return self._lease(persisted)

    def heartbeat(
        self,
        *,
        instance_id: object,
        fencing_token: object,
        lease_seconds: object,
    ) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        duration = _lease_seconds(lease_seconds)
        now = self._now()
        expires = now + timedelta(seconds=duration)
        with self._write() as connection:
            prior, _tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=fencing_token,
                now=now,
            )
            if now < prior.updated_at:
                raise StateConflict("executor runtime clock moved backwards")
            target = replace(
                prior,
                renewed_at=now,
                lease_expires_at=expires,
                heartbeat_at=now,
                heartbeat_expires_at=expires,
                revision=prior.revision + 1,
                updated_at=now,
                record_hash=_ZERO_HASH,
            )
            persisted = self._persist_state_locked(
                connection, prior=prior, state=target
            )
            return self._lease(persisted)

    def transition(
        self,
        *,
        instance_id: object,
        fencing_token: object,
        process_state: ExecutorProcessState | str | None = None,
        risk_gate: ExecutorRiskGate | str | None = None,
    ) -> RuntimeLease:
        if process_state is None and risk_gate is None:
            raise ValidationError("runtime transition must change process or risk state")
        fingerprint = _instance_fingerprint(instance_id)
        try:
            requested_process = (
                None
                if process_state is None
                else (
                    process_state
                    if isinstance(process_state, ExecutorProcessState)
                    else ExecutorProcessState(process_state)
                )
            )
            requested_gate = (
                None
                if risk_gate is None
                else (
                    risk_gate
                    if isinstance(risk_gate, ExecutorRiskGate)
                    else ExecutorRiskGate(risk_gate)
                )
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("runtime transition state is invalid") from error
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=fencing_token,
                now=now,
            )
            target_process = prior.process_state if requested_process is None else requested_process
            target_gate = prior.risk_gate if requested_gate is None else requested_gate
            if (
                target_process != prior.process_state
                and target_process not in _PROCESS_TRANSITIONS[prior.process_state]
            ):
                raise StateConflict("executor process-state transition is invalid")
            if (
                target_gate != prior.risk_gate
                and target_gate not in _RISK_TRANSITIONS[prior.risk_gate]
            ):
                raise StateConflict("executor risk-gate transition is invalid")
            if prior.manual_halt and target_gate is not ExecutorRiskGate.HALTED:
                raise StateConflict("manual halt blocks a non-halted risk gate")
            if target_gate is ExecutorRiskGate.READY:
                if (
                    target_process is not ExecutorProcessState.RUNNING
                    or prior.heartbeat_expires_at is None
                    or prior.heartbeat_expires_at <= now
                ):
                    raise StateConflict("ready risk gate requires a current running heartbeat")
            if target_process in {
                ExecutorProcessState.STOPPING,
                ExecutorProcessState.STOPPED,
            }:
                raise StateConflict("use graceful stop methods for stopping states")
            if (
                target_process is ExecutorProcessState.DEGRADED
                and target_gate is ExecutorRiskGate.READY
            ):
                raise StateConflict("degraded process may not retain a ready risk gate")
            if target_process == prior.process_state and target_gate == prior.risk_gate:
                return self._lease(prior)
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="state_changed",
                fencing_token=prior.fencing_token,
                changes={"process_state": target_process, "risk_gate": target_gate},
                payload={
                    "from_process_state": prior.process_state.value,
                    "to_process_state": target_process.value,
                    "from_risk_gate": prior.risk_gate.value,
                    "to_risk_gate": target_gate.value,
                },
            )
            return self._lease(persisted)

    def engage_manual_halt(self, *, reason: ManualHaltReason | str) -> ExecutorRuntimeReadModel:
        try:
            selected = reason if isinstance(reason, ManualHaltReason) else ManualHaltReason(reason)
        except (TypeError, ValueError) as error:
            raise ValidationError("manual halt reason is invalid") from error
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("runtime is not started and is already fail-closed")
            if prior.manual_halt:
                return self._read_model(prior, now, tail[1])
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="manual_halt_engaged",
                fencing_token=prior.fencing_token,
                changes={
                    "manual_halt": True,
                    "manual_halt_reason": selected,
                    "risk_gate": ExecutorRiskGate.HALTED,
                },
                payload={"reason": selected.value},
            )
            return self._read_model(persisted, now, persisted.last_event_hash)

    def clear_manual_halt(
        self,
        *,
        instance_id: object,
        fencing_token: object,
        expected_revision: object,
    ) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        revision = _positive_int(expected_revision, field="expected_revision")
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=fencing_token,
                now=now,
            )
            if prior.revision != revision:
                raise StateConflict("executor runtime revision changed")
            if not prior.manual_halt:
                return self._lease(prior)
            if prior.risk_gate is not ExecutorRiskGate.HALTED:
                raise StorageError("manual halt projection is not halted")
            prior_reason = prior.manual_halt_reason
            if prior_reason is None:  # Defensive; row validation also enforces this.
                raise StorageError("manual halt reason is missing")
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="manual_halt_cleared",
                fencing_token=prior.fencing_token,
                changes={"manual_halt": False, "manual_halt_reason": None},
                payload={"prior_reason": prior_reason.value},
            )
            return self._lease(persisted)

    def acknowledge_stale_manual_halt(
        self,
        *,
        expected_revision: object,
        expected_reason: ManualHaltReason | str,
    ) -> ExecutorRuntimeReadModel:
        """Attended recovery after the owning runtime lease is no longer live.

        This deliberately leaves the declared risk gate HALTED.  A new
        executor instance must still acquire the lease and complete startup
        reconciliation before it can transition through RECONCILING to READY.
        """

        revision = _positive_int(expected_revision, field="expected_revision")
        try:
            reason = (
                expected_reason
                if isinstance(expected_reason, ManualHaltReason)
                else ManualHaltReason(expected_reason)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("expected_reason is invalid") from error
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no state")
            if prior.revision != revision:
                raise StateConflict("executor runtime revision changed")
            if not prior.manual_halt or prior.manual_halt_reason is not reason:
                raise StateConflict("manual halt reason differs from acknowledgement")
            if (
                prior.process_state is not ExecutorProcessState.STOPPED
                and prior.lease_expires_at is not None
                and now < prior.lease_expires_at
            ):
                raise StateConflict("live executor lease must clear its own halt")
            if prior.risk_gate is not ExecutorRiskGate.HALTED:
                raise StorageError("manual halt projection is not halted")
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="manual_halt_cleared",
                fencing_token=prior.fencing_token,
                changes={"manual_halt": False, "manual_halt_reason": None},
                payload={
                    "prior_reason": reason.value,
                    "expected_revision": revision,
                    "risk_gate_remains": ExecutorRiskGate.HALTED.value,
                    "startup_reconciliation_required": True,
                },
            )
            return self._read_model(persisted, now, persisted.last_event_hash)

    def request_stop(
        self, *, instance_id: object, fencing_token: object
    ) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=fencing_token,
                now=now,
            )
            if prior.process_state is ExecutorProcessState.STOPPING:
                return self._lease(prior)
            if prior.process_state is ExecutorProcessState.STOPPED:
                return self._lease(prior)
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="stop_requested",
                fencing_token=prior.fencing_token,
                changes={
                    "process_state": ExecutorProcessState.STOPPING,
                    "risk_gate": ExecutorRiskGate.HALTED,
                    "stop_requested_at": now,
                },
                payload={"from_process_state": prior.process_state.value},
            )
            return self._lease(persisted)

    def mark_stopped(
        self, *, instance_id: object, fencing_token: object
    ) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=fencing_token,
                now=now,
            )
            if prior.process_state is ExecutorProcessState.STOPPED:
                return self._lease(prior)
            if prior.process_state is not ExecutorProcessState.STOPPING:
                raise StateConflict("runtime must enter stopping before stopped")
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="stopped",
                fencing_token=prior.fencing_token,
                changes={"process_state": ExecutorProcessState.STOPPED},
                payload={"stop_requested_at": prior.stop_requested_at},
            )
            return self._lease(persisted)

    def release(self, *, instance_id: object, fencing_token: object) -> RuntimeLease:
        fingerprint = _instance_fingerprint(instance_id)
        checked_token = _positive_int(fencing_token, field="fencing_token")
        now = self._now()
        with self._write() as connection:
            prior, tail = self._current_locked(connection)
            if prior is None:
                raise StateConflict("executor runtime has no lease")
            if (
                prior.lease_state is RuntimeLeaseState.RELEASED
                and prior.instance_fingerprint == fingerprint
                and prior.fencing_token == checked_token
            ):
                return self._lease(prior)
            self._lease_authority(
                prior,
                instance_fingerprint=fingerprint,
                fencing_token=checked_token,
                now=now,
            )
            if prior.process_state is not ExecutorProcessState.STOPPED:
                raise StateConflict("runtime must be stopped before releasing its lease")
            persisted = self._transition_locked(
                connection,
                prior=prior,
                tail=tail,
                at=now,
                event_type="lease_released",
                fencing_token=prior.fencing_token,
                changes={
                    "lease_state": RuntimeLeaseState.RELEASED,
                    "risk_gate": ExecutorRiskGate.HALTED,
                    "released_at": now,
                },
                payload={"fencing_token": prior.fencing_token},
            )
            return self._lease(persisted)

    def _read_model(
        self, state: _RuntimeState | None, observed: datetime, journal_hash: str
    ) -> ExecutorRuntimeReadModel:
        if state is None:
            material = {
                "config_hash": self._config.config_hash,
                "deployment_fingerprint": self._deployment_fingerprint,
                "lease_state": RuntimeLeaseState.NOT_STARTED.value,
                "observed_at": observed,
                "journal_chain_hash": journal_hash,
            }
            return ExecutorRuntimeReadModel(
                config_hash=self._config.config_hash,
                deployment_fingerprint=self._deployment_fingerprint,
                lease_state=RuntimeLeaseState.NOT_STARTED,
                fencing_token=0,
                instance_fingerprint=None,
                process_state=ExecutorProcessState.STOPPED,
                declared_risk_gate=ExecutorRiskGate.HALTED,
                effective_risk_gate=ExecutorRiskGate.HALTED,
                manual_halt=False,
                manual_halt_reason=None,
                acquired_at=None,
                lease_expires_at=None,
                heartbeat_at=None,
                heartbeat_expires_at=None,
                stop_requested_at=None,
                released_at=None,
                observed_at=observed,
                lease_current=False,
                heartbeat_current=False,
                revision=0,
                state_hash=None,
                journal_chain_hash=journal_hash,
                read_model_hash=domain_hash(
                    "trading-harness/executor-runtime-read/v1", material
                ),
            )
        clock_current = observed >= state.updated_at
        lease_current = (
            clock_current
            and state.lease_state is RuntimeLeaseState.ACTIVE
            and state.lease_expires_at > observed
        )
        heartbeat_current = (
            lease_current
            and state.heartbeat_expires_at is not None
            and state.heartbeat_expires_at > observed
        )
        effective = state.risk_gate
        if (
            not lease_current
            or not heartbeat_current
            or state.manual_halt
            or state.process_state is not ExecutorProcessState.RUNNING
        ):
            effective = ExecutorRiskGate.HALTED
        material = {
            "config_hash": self._config.config_hash,
            "deployment_fingerprint": self._deployment_fingerprint,
            "lease_state": state.lease_state.value,
            "fencing_token": state.fencing_token,
            "instance_fingerprint": state.instance_fingerprint,
            "process_state": state.process_state.value,
            "declared_risk_gate": state.risk_gate.value,
            "effective_risk_gate": effective.value,
            "manual_halt": state.manual_halt,
            "manual_halt_reason": (
                None if state.manual_halt_reason is None else state.manual_halt_reason.value
            ),
            "acquired_at": state.acquired_at,
            "lease_expires_at": state.lease_expires_at,
            "heartbeat_at": state.heartbeat_at,
            "heartbeat_expires_at": state.heartbeat_expires_at,
            "stop_requested_at": state.stop_requested_at,
            "released_at": state.released_at,
            "observed_at": observed,
            "lease_current": lease_current,
            "heartbeat_current": heartbeat_current,
            "revision": state.revision,
            "state_hash": state.record_hash,
            "journal_chain_hash": journal_hash,
        }
        return ExecutorRuntimeReadModel(
            config_hash=self._config.config_hash,
            deployment_fingerprint=self._deployment_fingerprint,
            lease_state=state.lease_state,
            fencing_token=state.fencing_token,
            instance_fingerprint=state.instance_fingerprint,
            process_state=state.process_state,
            declared_risk_gate=state.risk_gate,
            effective_risk_gate=effective,
            manual_halt=state.manual_halt,
            manual_halt_reason=state.manual_halt_reason,
            acquired_at=state.acquired_at,
            lease_expires_at=state.lease_expires_at,
            heartbeat_at=state.heartbeat_at,
            heartbeat_expires_at=state.heartbeat_expires_at,
            stop_requested_at=state.stop_requested_at,
            released_at=state.released_at,
            observed_at=observed,
            lease_current=lease_current,
            heartbeat_current=heartbeat_current,
            revision=state.revision,
            state_hash=state.record_hash,
            journal_chain_hash=journal_hash,
            read_model_hash=domain_hash(
                "trading-harness/executor-runtime-read/v1", material
            ),
        )

    def read(self) -> ExecutorRuntimeReadModel:
        """Verify binding, schema, journal and projection before returning status."""

        observed = self._now()
        with self._connection() as connection:
            state, tail = self._current_locked(connection)
        return self._read_model(state, observed, tail[1])

    def verify_journal(self) -> bool:
        with self._connection() as connection:
            state, tail = self._current_locked(connection)
        return state is None or (
            state.last_event_sequence == tail[0] and state.last_event_hash == tail[1]
        )


__all__ = (
    "EXECUTOR_RUNTIME_SCHEMA_VERSION",
    "MAX_LEASE_SECONDS",
    "MIN_LEASE_SECONDS",
    "ExecutorRuntimeReadModel",
    "ExecutorRuntimeStore",
    "ManualHaltReason",
    "RuntimeLease",
    "RuntimeLeaseState",
)
