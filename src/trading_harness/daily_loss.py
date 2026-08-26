"""Durable, fail-closed UTC daily-loss accounting.

The ledger records economic debits as immutable events and never applies a
credit against already-used daily loss.  Losing realized PnL, fees, and paid
funding increase the used budget; profitable PnL and received funding are
retained as evidence with a zero debit.  This makes the budget monotonic
within each UTC day even when trades later recover.

A number is safe to use for admission only when both the fills stream and the
funding stream have gap-free coverage from UTC midnight through the current
clock reading.  Missing coverage fails closed.  All writes are local SQLite
transactions; this module has no exchange or credential capability.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any

from .canonical import canonical_decimal, domain_hash
from .domain import Environment
from .errors import StateConflict, StorageError, ValidationError
from .executor_config import ExecutorConfigDrift
from .executor_state_binding import (
    MAX_PRIVATE_STATE_FILE_BYTES,
    STATE_BINDING_TABLE,
    STATE_BINDING_TABLE_SQL_NORMALIZED,
    normalized_schema_sql,
)
from .policy import decimal_add, decimal_subtract, exact_decimal
from .sqlite_snapshot import sqlite_verification_snapshot


DAILY_LOSS_SCHEMA_VERSION = 1
REQUIRED_COVERAGE_SOURCES = ("fills", "funding")
_ZERO = Decimal("0")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}$")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")


Clock = Callable[[], datetime]


class LossEventKind(str, Enum):
    REALIZED_PNL = "realized_pnl"
    FEE = "fee"
    FUNDING = "funding"


class LossCoverageSource(str, Enum):
    FILLS = "fills"
    FUNDING = "funding"


class IncompleteDailyLossCoverage(StateConflict):
    """The current UTC loss budget cannot be trusted for admission."""

    def __init__(self, missing_sources: tuple[str, ...]) -> None:
        self.missing_sources = missing_sources
        super().__init__("daily-loss coverage is incomplete; admission is halted")


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
        raise StorageError("daily-loss clock failed") from error
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


def _text(value: object, *, field: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} must be bounded, trimmed text")
    return value


def _identifier(value: object, *, field: str) -> str:
    parsed = _text(value, field=field)
    if not _IDENTIFIER_RE.fullmatch(parsed):
        raise ValidationError(f"{field} is not a valid identifier")
    return parsed


def _sha256(value: object, *, field: str, stored: bool = False) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        exception = StorageError if stored else ValidationError
        raise exception(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonnegative(value: object, *, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)  # type: ignore[arg-type]
    if parsed < _ZERO:
        raise ValidationError(f"{field} must be non-negative")
    return parsed


def _positive(value: object, *, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)  # type: ignore[arg-type]
    if parsed <= _ZERO:
        raise ValidationError(f"{field} must be positive")
    return parsed


def _stored_decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise StorageError(f"persisted {field} is not an exact decimal string")
    try:
        parsed = exact_decimal(value, field=field)
    except ValidationError as error:
        raise StorageError(f"persisted {field} is invalid") from error
    if canonical_decimal(parsed) != value:
        raise StorageError(f"persisted {field} is not canonical")
    return parsed


def _day_start(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class DailyLossBinding:
    account_id: str
    environment: Environment
    config_hash: str
    daily_loss_limit: Decimal
    settlement_currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _identifier(self.account_id, field="account_id"))
        try:
            environment = (
                self.environment
                if isinstance(self.environment, Environment)
                else Environment(self.environment)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("environment is invalid") from error
        if environment is not Environment.TESTNET:
            raise ValidationError("daily-loss foundation supports TESTNET only")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self, "config_hash", _sha256(self.config_hash, field="config_hash")
        )
        object.__setattr__(
            self,
            "daily_loss_limit",
            _positive(self.daily_loss_limit, field="daily_loss_limit"),
        )
        currency = _text(
            self.settlement_currency, field="settlement_currency", maximum=16
        )
        if not _CURRENCY_RE.fullmatch(currency):
            raise ValidationError("settlement_currency is invalid")
        object.__setattr__(self, "settlement_currency", currency)

    @property
    def binding_hash(self) -> str:
        return domain_hash("trading-harness/daily-loss-binding/v1", self)


@dataclass(frozen=True, slots=True)
class DailyLossEvent:
    event_id: str
    kind: LossEventKind
    occurred_at: datetime
    observed_at: datetime
    amount: Decimal
    debit: Decimal
    source_ref_hash: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    source: LossCoverageSource
    required_from: datetime
    required_through: datetime
    covered_from: datetime | None
    covered_through: datetime | None
    complete: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "required_from": _time_text(self.required_from, field="required_from"),
            "required_through": _time_text(
                self.required_through, field="required_through"
            ),
            "covered_from": (
                None
                if self.covered_from is None
                else _time_text(self.covered_from, field="covered_from")
            ),
            "covered_through": (
                None
                if self.covered_through is None
                else _time_text(self.covered_through, field="covered_through")
            ),
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class DailyLossSnapshot:
    utc_day: date
    as_of: datetime
    limit: Decimal
    used: Decimal
    remaining: Decimal
    realized_loss_debit: Decimal
    fee_debit: Decimal
    funding_debit: Decimal
    event_count: int
    coverage: tuple[CoverageStatus, ...]
    coverage_complete: bool
    missing_sources: tuple[str, ...]
    binding_hash: str
    snapshot_hash: str

    def as_dict(self) -> dict[str, object]:
        """Return exact, account-redacted status data."""

        return {
            "utc_day": self.utc_day.isoformat(),
            "as_of": _time_text(self.as_of, field="as_of"),
            "limit": canonical_decimal(self.limit),
            "used": canonical_decimal(self.used),
            "remaining": canonical_decimal(self.remaining),
            "realized_loss_debit": canonical_decimal(self.realized_loss_debit),
            "fee_debit": canonical_decimal(self.fee_debit),
            "funding_debit": canonical_decimal(self.funding_debit),
            "event_count": self.event_count,
            "coverage": [item.as_dict() for item in self.coverage],
            "coverage_complete": self.coverage_complete,
            "missing_sources": list(self.missing_sources),
            "binding_hash": self.binding_hash,
            "snapshot_hash": self.snapshot_hash,
        }


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS daily_loss_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE daily_loss_binding (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        account_id TEXT NOT NULL,
        environment TEXT NOT NULL CHECK (environment = 'testnet'),
        config_hash TEXT NOT NULL,
        binding_hash TEXT NOT NULL,
        daily_loss_limit TEXT NOT NULL,
        settlement_currency TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE daily_loss_events (
        event_id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK (source IN ('fills', 'funding')),
        kind TEXT NOT NULL CHECK (kind IN ('realized_pnl', 'fee', 'funding')),
        occurred_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        amount TEXT NOT NULL,
        debit TEXT NOT NULL,
        source_ref_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL,
        UNIQUE (source, kind, source_ref_hash)
    )
    """,
    """
    CREATE INDEX idx_daily_loss_events_occurred
    ON daily_loss_events (occurred_at, event_id)
    """,
    """
    CREATE TABLE daily_loss_coverage (
        coverage_id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK (source IN ('fills', 'funding')),
        covered_from TEXT NOT NULL,
        covered_through TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        source_cursor_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL,
        UNIQUE (source, source_cursor_hash)
    )
    """,
    """
    CREATE INDEX idx_daily_loss_coverage_window
    ON daily_loss_coverage (source, covered_from, covered_through, coverage_id)
    """,
    """
    CREATE TRIGGER daily_loss_binding_no_update
    BEFORE UPDATE ON daily_loss_binding
    BEGIN SELECT RAISE(ABORT, 'daily-loss binding is immutable'); END
    """,
    """
    CREATE TRIGGER daily_loss_binding_no_delete
    BEFORE DELETE ON daily_loss_binding
    BEGIN SELECT RAISE(ABORT, 'daily-loss binding is immutable'); END
    """,
    """
    CREATE TRIGGER daily_loss_events_no_update
    BEFORE UPDATE ON daily_loss_events
    BEGIN SELECT RAISE(ABORT, 'daily-loss events are immutable'); END
    """,
    """
    CREATE TRIGGER daily_loss_events_no_delete
    BEFORE DELETE ON daily_loss_events
    BEGIN SELECT RAISE(ABORT, 'daily-loss events are immutable'); END
    """,
    """
    CREATE TRIGGER daily_loss_coverage_no_update
    BEFORE UPDATE ON daily_loss_coverage
    BEGIN SELECT RAISE(ABORT, 'daily-loss coverage is immutable'); END
    """,
    """
    CREATE TRIGGER daily_loss_coverage_no_delete
    BEFORE DELETE ON daily_loss_coverage
    BEGIN SELECT RAISE(ABORT, 'daily-loss coverage is immutable'); END
    """,
)
_SCHEMA_CHECKSUM = hashlib.sha256(
    "\n-- daily-loss schema statement --\n".join(_SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

_EXPECTED_COLUMNS = {
    "daily_loss_schema_migrations": (
        "version",
        "name",
        "checksum",
        "applied_at",
    ),
    "daily_loss_binding": (
        "singleton",
        "account_id",
        "environment",
        "config_hash",
        "binding_hash",
        "daily_loss_limit",
        "settlement_currency",
        "created_at",
    ),
    "daily_loss_events": (
        "event_id",
        "source",
        "kind",
        "occurred_at",
        "observed_at",
        "amount",
        "debit",
        "source_ref_hash",
        "record_hash",
    ),
    "daily_loss_coverage": (
        "coverage_id",
        "source",
        "covered_from",
        "covered_through",
        "observed_at",
        "source_cursor_hash",
        "record_hash",
    ),
}
_EXPECTED_TABLE_OBJECTS = {
    "daily_loss_schema_migrations": _SCHEMA_STATEMENTS[0],
    "daily_loss_binding": _SCHEMA_STATEMENTS[1],
    "daily_loss_events": _SCHEMA_STATEMENTS[2],
    "daily_loss_coverage": _SCHEMA_STATEMENTS[4],
}
_EXPECTED_IMMUTABILITY_OBJECTS = {
    "idx_daily_loss_events_occurred": _SCHEMA_STATEMENTS[3],
    "idx_daily_loss_coverage_window": _SCHEMA_STATEMENTS[5],
    "daily_loss_binding_no_update": _SCHEMA_STATEMENTS[6],
    "daily_loss_binding_no_delete": _SCHEMA_STATEMENTS[7],
    "daily_loss_events_no_update": _SCHEMA_STATEMENTS[8],
    "daily_loss_events_no_delete": _SCHEMA_STATEMENTS[9],
    "daily_loss_coverage_no_update": _SCHEMA_STATEMENTS[10],
    "daily_loss_coverage_no_delete": _SCHEMA_STATEMENTS[11],
}


def _normalized_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.rstrip(";").split()).lower()
    return normalized.replace("create table if not exists ", "create table ", 1)


class DailyLossLedger:
    """Append-only daily-loss events plus independently proved source coverage."""

    def __init__(
        self,
        database: str | Path,
        *,
        binding: DailyLossBinding,
        clock: Clock | None = None,
        must_exist: bool = False,
    ) -> None:
        selected = Path(database)
        if not selected.is_absolute():
            raise ValidationError("daily-loss database path must be absolute")
        if selected.exists() and selected.is_symlink():
            raise ValidationError("daily-loss database may not be a symlink")
        if not selected.parent.is_dir() or selected.parent.is_symlink():
            raise ValidationError("daily-loss database parent must be a real directory")
        if not isinstance(binding, DailyLossBinding):
            raise TypeError("binding must be DailyLossBinding")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if type(must_exist) is not bool:
            raise TypeError("must_exist must be a boolean")
        self._database = selected
        self._must_exist = must_exist
        self._binding = binding
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    @property
    def binding(self) -> DailyLossBinding:
        return self._binding

    @contextmanager
    def _connection(
        self,
        *,
        verification_only: bool = False,
        verification_path: Path | None = None,
    ) -> Iterator[sqlite3.Connection]:
        try:
            selected = self._database if verification_path is None else verification_path
            database: str | Path = selected
            if verification_only:
                database = f"{selected.as_uri()}?mode=ro"
            elif self._must_exist:
                database = f"{self._database.as_uri()}?mode=rw"
            connection = sqlite3.connect(
                database,
                timeout=5,
                isolation_level=None,
                uri=verification_only or self._must_exist,
            )
        except sqlite3.Error as error:
            raise StorageError("daily-loss database is unavailable") from error
        connection.row_factory = sqlite3.Row
        try:
            if verification_only:
                connection.execute("PRAGMA query_only = ON")
                query_only = connection.execute("PRAGMA query_only").fetchone()
                if query_only is None or query_only[0] != 1:
                    raise StorageError(
                        "daily-loss verification connection is not query-only"
                    )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            if verification_only:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
                if journal_mode is None or journal_mode[0].lower() != "wal":
                    raise StorageError(
                        "existing daily-loss database is not configured for WAL"
                    )
            elif self._must_exist:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
                if journal_mode is None or journal_mode[0].lower() != "wal":
                    raise StorageError(
                        "existing daily-loss database is not configured for WAL"
                    )
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        except sqlite3.Error as error:
            raise StorageError("daily-loss database operation failed") from error
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
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _initialize(self) -> None:
        if self._must_exist:
            with sqlite_verification_snapshot(
                self._database,
                label="daily-loss database",
                max_bytes=MAX_PRIVATE_STATE_FILE_BYTES,
            ) as snapshot:
                with self._connection(
                    verification_only=True,
                    verification_path=snapshot.database,
                ) as connection:
                    self._verify_integrity(connection)
                    self._verify_schema(connection)
                    self._bind_or_verify(connection, allow_create=False)
            return

        now = _clock_read(self._clock)
        with self._write() as connection:
            connection.execute(_SCHEMA_STATEMENTS[0])
            row = connection.execute(
                "SELECT name, checksum FROM daily_loss_schema_migrations WHERE version = ?",
                (DAILY_LOSS_SCHEMA_VERSION,),
            ).fetchone()
            if row is None:
                for statement in _SCHEMA_STATEMENTS[1:]:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO daily_loss_schema_migrations
                        (version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        DAILY_LOSS_SCHEMA_VERSION,
                        "monotonic_utc_daily_loss",
                        _SCHEMA_CHECKSUM,
                        _time_text(now, field="applied_at"),
                    ),
                )
            elif (
                row["name"] != "monotonic_utc_daily_loss"
                or row["checksum"] != _SCHEMA_CHECKSUM
            ):
                raise StorageError("daily-loss migration checksum does not match")
            self._verify_schema(connection)
            self._bind_or_verify(connection, allow_create=True, now=now)

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            raise StorageError("daily-loss database integrity check failed")

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        object_names = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger')
                  AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        expected_names = set(_EXPECTED_TABLE_OBJECTS) | set(
            _EXPECTED_IMMUTABILITY_OBJECTS
        )
        allowed_names = expected_names | {STATE_BINDING_TABLE}
        if object_names not in (expected_names, allowed_names):
            raise StorageError("daily-loss database has unexpected schema objects")
        if STATE_BINDING_TABLE in object_names:
            binding_rows = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?",
                (STATE_BINDING_TABLE,),
            ).fetchall()
            if len(binding_rows) != 1 or (
                binding_rows[0]["type"] != "table"
                or normalized_schema_sql(binding_rows[0]["sql"])
                != STATE_BINDING_TABLE_SQL_NORMALIZED
            ):
                raise StorageError("daily-loss deployment binding schema does not match")
        for table, expected in _EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple(row["name"] for row in rows)
            if actual != expected:
                raise StorageError(f"daily-loss table schema does not match: {table}")
        placeholders = ",".join("?" for _ in _EXPECTED_TABLE_OBJECTS)
        table_rows = connection.execute(
            f"""
            SELECT name, sql FROM sqlite_master
            WHERE type = 'table' AND name IN ({placeholders})
            """,
            tuple(_EXPECTED_TABLE_OBJECTS),
        ).fetchall()
        actual_tables = {
            row["name"]: _normalized_sql(row["sql"]) for row in table_rows
        }
        expected_tables = {
            name: _normalized_sql(sql)
            for name, sql in _EXPECTED_TABLE_OBJECTS.items()
        }
        if actual_tables != expected_tables:
            raise StorageError("daily-loss table definitions do not match")
        migration_rows = connection.execute(
            "SELECT version, name, checksum FROM daily_loss_schema_migrations ORDER BY version"
        ).fetchall()
        if len(migration_rows) != 1:
            raise StorageError("daily-loss migration history is invalid")
        row = migration_rows[0]
        if (
            row["version"] != DAILY_LOSS_SCHEMA_VERSION
            or row["name"] != "monotonic_utc_daily_loss"
            or row["checksum"] != _SCHEMA_CHECKSUM
        ):
            raise StorageError("daily-loss migration history does not match")
        placeholders = ",".join("?" for _ in _EXPECTED_IMMUTABILITY_OBJECTS)
        object_rows = connection.execute(
            f"SELECT name, sql FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(_EXPECTED_IMMUTABILITY_OBJECTS),
        ).fetchall()
        actual_objects = {row["name"]: _normalized_sql(row["sql"]) for row in object_rows}
        expected_objects = {
            name: _normalized_sql(sql)
            for name, sql in _EXPECTED_IMMUTABILITY_OBJECTS.items()
        }
        if actual_objects != expected_objects:
            raise StorageError("daily-loss indexes or immutability triggers do not match")

    def _bind_or_verify(
        self,
        connection: sqlite3.Connection,
        now: datetime | None = None,
        *,
        allow_create: bool,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM daily_loss_binding WHERE singleton = 1"
        ).fetchone()
        expected = self._binding
        if row is None:
            if not allow_create:
                raise StorageError("daily-loss binding is missing")
            if now is None:
                raise StorageError("daily-loss binding creation requires a timestamp")
            connection.execute(
                """
                INSERT INTO daily_loss_binding (
                    singleton, account_id, environment, config_hash, binding_hash,
                    daily_loss_limit, settlement_currency, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expected.account_id,
                    expected.environment.value,
                    expected.config_hash,
                    expected.binding_hash,
                    canonical_decimal(expected.daily_loss_limit),
                    expected.settlement_currency,
                    _time_text(now, field="created_at"),
                ),
            )
            return
        stored_limit = _stored_decimal(
            row["daily_loss_limit"], field="daily_loss_limit"
        )
        persisted = {
            "account_id": row["account_id"],
            "environment": row["environment"],
            "config_hash": row["config_hash"],
            "binding_hash": row["binding_hash"],
            "daily_loss_limit": stored_limit,
            "settlement_currency": row["settlement_currency"],
        }
        wanted = {
            "account_id": expected.account_id,
            "environment": expected.environment.value,
            "config_hash": expected.config_hash,
            "binding_hash": expected.binding_hash,
            "daily_loss_limit": expected.daily_loss_limit,
            "settlement_currency": expected.settlement_currency,
        }
        if persisted != wanted:
            raise ExecutorConfigDrift(
                "daily-loss ledger is bound to a different executor configuration"
            )

    def _now(self) -> datetime:
        return _clock_read(self._clock)

    def _append_event(
        self,
        *,
        event_id: object,
        kind: LossEventKind,
        source: LossCoverageSource,
        source_ref: object,
        occurred_at: object,
        amount: object,
        debit: Decimal,
    ) -> bool:
        checked_event_id = _identifier(event_id, field="event_id")
        checked_source_ref = _text(source_ref, field="source_ref", maximum=1024)
        source_ref_hash = domain_hash(
            "trading-harness/daily-loss-source-ref/v1", checked_source_ref
        )
        occurred = _utc(occurred_at, field="occurred_at")
        observed = self._now()
        if occurred > observed:
            raise ValidationError("occurred_at may not be in the future")
        checked_amount = exact_decimal(amount, field="amount")  # type: ignore[arg-type]
        checked_debit = _nonnegative(debit, field="debit")
        material = {
            "binding_hash": self._binding.binding_hash,
            "event_id": checked_event_id,
            "source": source.value,
            "kind": kind.value,
            "occurred_at": occurred,
            "amount": checked_amount,
            "debit": checked_debit,
            "source_ref_hash": source_ref_hash,
        }
        record_hash = domain_hash("trading-harness/daily-loss-event/v1", material)
        values = (
            checked_event_id,
            source.value,
            kind.value,
            _time_text(occurred, field="occurred_at"),
            _time_text(observed, field="observed_at"),
            canonical_decimal(checked_amount),
            canonical_decimal(checked_debit),
            source_ref_hash,
            record_hash,
        )
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT event_id, record_hash FROM daily_loss_events
                WHERE event_id = ? OR (source = ? AND kind = ? AND source_ref_hash = ?)
                """,
                (checked_event_id, source.value, kind.value, source_ref_hash),
            ).fetchall()
            if existing:
                if (
                    len(existing) == 1
                    and existing[0]["event_id"] == checked_event_id
                    and existing[0]["record_hash"] == record_hash
                ):
                    return False
                raise StateConflict("daily-loss event id or source reference conflicts")
            connection.execute(
                """
                INSERT INTO daily_loss_events (
                    event_id, source, kind, occurred_at, observed_at, amount,
                    debit, source_ref_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return True

    def record_realized_pnl(
        self,
        *,
        event_id: object,
        source_ref: object,
        occurred_at: object,
        realized_pnl: object,
    ) -> bool:
        """Record signed realized PnL; only a negative amount consumes budget."""

        amount = exact_decimal(realized_pnl, field="realized_pnl")  # type: ignore[arg-type]
        debit = -amount if amount < _ZERO else _ZERO
        return self._append_event(
            event_id=event_id,
            kind=LossEventKind.REALIZED_PNL,
            source=LossCoverageSource.FILLS,
            source_ref=source_ref,
            occurred_at=occurred_at,
            amount=amount,
            debit=debit,
        )

    def record_fee(
        self,
        *,
        event_id: object,
        source_ref: object,
        occurred_at: object,
        fee: object,
    ) -> bool:
        """Record a non-negative fee debit from a fill or settlement event."""

        amount = _nonnegative(fee, field="fee")
        return self._append_event(
            event_id=event_id,
            kind=LossEventKind.FEE,
            source=LossCoverageSource.FILLS,
            source_ref=source_ref,
            occurred_at=occurred_at,
            amount=amount,
            debit=amount,
        )

    def record_funding(
        self,
        *,
        event_id: object,
        source_ref: object,
        occurred_at: object,
        net_funding: object,
    ) -> bool:
        """Record signed funding; paid (negative) funding consumes budget."""

        amount = exact_decimal(net_funding, field="net_funding")  # type: ignore[arg-type]
        debit = -amount if amount < _ZERO else _ZERO
        return self._append_event(
            event_id=event_id,
            kind=LossEventKind.FUNDING,
            source=LossCoverageSource.FUNDING,
            source_ref=source_ref,
            occurred_at=occurred_at,
            amount=amount,
            debit=debit,
        )

    def record_coverage(
        self,
        *,
        coverage_id: object,
        source: LossCoverageSource | str,
        covered_from: object,
        covered_through: object,
        source_cursor_hash: object,
    ) -> bool:
        """Append one immutable venue-query coverage interval."""

        checked_id = _identifier(coverage_id, field="coverage_id")
        try:
            checked_source = (
                source if isinstance(source, LossCoverageSource) else LossCoverageSource(source)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("coverage source is invalid") from error
        start = _utc(covered_from, field="covered_from")
        end = _utc(covered_through, field="covered_through")
        observed = self._now()
        if end < start:
            raise ValidationError("covered_through precedes covered_from")
        if end > observed:
            raise ValidationError("coverage may not extend beyond the current clock")
        cursor_hash = _sha256(source_cursor_hash, field="source_cursor_hash")
        material = {
            "binding_hash": self._binding.binding_hash,
            "coverage_id": checked_id,
            "source": checked_source.value,
            "covered_from": start,
            "covered_through": end,
            "source_cursor_hash": cursor_hash,
        }
        record_hash = domain_hash("trading-harness/daily-loss-coverage/v1", material)
        values = (
            checked_id,
            checked_source.value,
            _time_text(start, field="covered_from"),
            _time_text(end, field="covered_through"),
            _time_text(observed, field="observed_at"),
            cursor_hash,
            record_hash,
        )
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT coverage_id, record_hash FROM daily_loss_coverage
                WHERE coverage_id = ? OR (source = ? AND source_cursor_hash = ?)
                """,
                (checked_id, checked_source.value, cursor_hash),
            ).fetchall()
            if existing:
                if (
                    len(existing) == 1
                    and existing[0]["coverage_id"] == checked_id
                    and existing[0]["record_hash"] == record_hash
                ):
                    return False
                raise StateConflict("daily-loss coverage id or cursor conflicts")
            connection.execute(
                """
                INSERT INTO daily_loss_coverage (
                    coverage_id, source, covered_from, covered_through,
                    observed_at, source_cursor_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return True

    @staticmethod
    def _validate_event_row(row: sqlite3.Row, *, binding_hash: str) -> DailyLossEvent:
        try:
            kind = LossEventKind(row["kind"])
            source = LossCoverageSource(row["source"])
        except (TypeError, ValueError) as error:
            raise StorageError("persisted daily-loss event enum is invalid") from error
        if (
            kind in {LossEventKind.REALIZED_PNL, LossEventKind.FEE}
            and source is not LossCoverageSource.FILLS
        ) or (
            kind is LossEventKind.FUNDING
            and source is not LossCoverageSource.FUNDING
        ):
            raise StorageError("persisted daily-loss event source is invalid")
        event_id = _identifier(row["event_id"], field="persisted event_id")
        occurred = _parse_time(row["occurred_at"], field="occurred_at")
        observed = _parse_time(row["observed_at"], field="observed_at")
        if occurred > observed:
            raise StorageError("persisted daily-loss event occurs after observation")
        amount = _stored_decimal(row["amount"], field="amount")
        debit = _stored_decimal(row["debit"], field="debit")
        if debit < _ZERO:
            raise StorageError("persisted daily-loss debit is negative")
        expected_debit = (
            amount
            if kind is LossEventKind.FEE
            else (-amount if amount < _ZERO else _ZERO)
        )
        if kind is LossEventKind.FEE and amount < _ZERO:
            raise StorageError("persisted fee is negative")
        if debit != expected_debit:
            raise StorageError("persisted daily-loss debit is inconsistent")
        source_ref_hash = _sha256(
            row["source_ref_hash"], field="source_ref_hash", stored=True
        )
        record_hash = _sha256(row["record_hash"], field="record_hash", stored=True)
        expected_hash = domain_hash(
            "trading-harness/daily-loss-event/v1",
            {
                "binding_hash": binding_hash,
                "event_id": event_id,
                "source": source.value,
                "kind": kind.value,
                "occurred_at": occurred,
                "amount": amount,
                "debit": debit,
                "source_ref_hash": source_ref_hash,
            },
        )
        if record_hash != expected_hash:
            raise StorageError("persisted daily-loss event hash does not match")
        return DailyLossEvent(
            event_id=event_id,
            kind=kind,
            occurred_at=occurred,
            observed_at=observed,
            amount=amount,
            debit=debit,
            source_ref_hash=source_ref_hash,
            record_hash=record_hash,
        )

    @staticmethod
    def _coverage_status(
        rows: list[sqlite3.Row],
        *,
        source: LossCoverageSource,
        required_from: datetime,
        required_through: datetime,
        binding_hash: str,
    ) -> CoverageStatus:
        intervals: list[tuple[datetime, datetime]] = []
        for row in rows:
            try:
                stored_source = LossCoverageSource(row["source"])
            except (TypeError, ValueError) as error:
                raise StorageError("persisted coverage source is invalid") from error
            if stored_source is not source:
                raise StorageError("persisted coverage query returned the wrong source")
            coverage_id = _identifier(row["coverage_id"], field="persisted coverage_id")
            start = _parse_time(row["covered_from"], field="covered_from")
            end = _parse_time(row["covered_through"], field="covered_through")
            observed = _parse_time(row["observed_at"], field="observed_at")
            if end < start or end > observed:
                raise StorageError("persisted coverage interval is invalid")
            cursor_hash = _sha256(
                row["source_cursor_hash"], field="source_cursor_hash", stored=True
            )
            record_hash = _sha256(
                row["record_hash"], field="record_hash", stored=True
            )
            expected_hash = domain_hash(
                "trading-harness/daily-loss-coverage/v1",
                {
                    "binding_hash": binding_hash,
                    "coverage_id": coverage_id,
                    "source": source.value,
                    "covered_from": start,
                    "covered_through": end,
                    "source_cursor_hash": cursor_hash,
                },
            )
            if record_hash != expected_hash:
                raise StorageError("persisted coverage hash does not match")
            intervals.append((start, end))

        covered_from: datetime | None = None
        covered_through: datetime | None = None
        complete = False
        for start, end in sorted(intervals):
            if end < required_from or start > required_through:
                continue
            if covered_from is None:
                covered_from = start
                covered_through = end
                if start > required_from:
                    # A later interval cannot fill the initial gap because rows are sorted.
                    break
            elif covered_through is not None and start <= covered_through:
                if end > covered_through:
                    covered_through = end
            else:
                # An uncovered positive-duration interval exists.
                break
            if covered_from <= required_from and covered_through >= required_through:
                complete = True
                break
        return CoverageStatus(
            source=source,
            required_from=required_from,
            required_through=required_through,
            covered_from=covered_from,
            covered_through=covered_through,
            complete=complete,
        )

    def snapshot(
        self,
        *,
        require_complete: bool = True,
        as_of: datetime | None = None,
    ) -> DailyLossSnapshot:
        """Return an exact UTC budget at ``as_of`` or the ledger clock.

        A caller-supplied watermark is useful after a bounded venue-history
        query: coverage can be proved exactly through the query end even
        though the wall clock advanced while the request was in flight.  The
        watermark may never be future-dated or cross the ledger clock's UTC
        day; callers must independently enforce freshness.
        """

        if type(require_complete) is not bool:
            raise TypeError("require_complete must be bool")
        observed = self._now()
        now = observed if as_of is None else _utc(as_of, field="as_of")
        if now > observed:
            raise ValidationError("daily-loss as_of may not follow the ledger clock")
        if now.date() != observed.date():
            raise ValidationError("daily-loss as_of must be in the current UTC day")
        start = _day_start(now)
        with self._connection() as connection:
            self._verify_schema(connection)
            self._bind_or_verify(connection, now, allow_create=False)
            event_rows = connection.execute(
                """
                SELECT * FROM daily_loss_events
                WHERE occurred_at >= ? AND occurred_at <= ?
                ORDER BY occurred_at, event_id
                """,
                (_time_text(start, field="day_start"), _time_text(now, field="as_of")),
            ).fetchall()
            coverage_by_source: list[CoverageStatus] = []
            for source in (LossCoverageSource.FILLS, LossCoverageSource.FUNDING):
                rows = connection.execute(
                    """
                    SELECT * FROM daily_loss_coverage
                    WHERE source = ? AND covered_through >= ? AND covered_from <= ?
                    ORDER BY covered_from, covered_through, coverage_id
                    """,
                    (
                        source.value,
                        _time_text(start, field="day_start"),
                        _time_text(now, field="as_of"),
                    ),
                ).fetchall()
                coverage_by_source.append(
                    self._coverage_status(
                        list(rows),
                        source=source,
                        required_from=start,
                        required_through=now,
                        binding_hash=self._binding.binding_hash,
                    )
                )

        events = tuple(
            self._validate_event_row(row, binding_hash=self._binding.binding_hash)
            for row in event_rows
        )
        realized = _ZERO
        fees = _ZERO
        funding = _ZERO
        for event in events:
            if event.kind is LossEventKind.REALIZED_PNL:
                realized = decimal_add(realized, event.debit, field="realized loss debit")
            elif event.kind is LossEventKind.FEE:
                fees = decimal_add(fees, event.debit, field="fee debit")
            else:
                funding = decimal_add(funding, event.debit, field="funding debit")
        used = decimal_add(realized, fees, funding, field="daily loss used")
        remaining = max(
            decimal_subtract(
                self._binding.daily_loss_limit,
                used,
                field="daily loss remaining",
            ),
            _ZERO,
        )
        coverage = tuple(coverage_by_source)
        missing = tuple(item.source.value for item in coverage if not item.complete)
        material: dict[str, Any] = {
            "binding_hash": self._binding.binding_hash,
            "utc_day": now.date(),
            "as_of": now,
            "limit": self._binding.daily_loss_limit,
            "used": used,
            "remaining": remaining,
            "realized_loss_debit": realized,
            "fee_debit": fees,
            "funding_debit": funding,
            "event_hashes": tuple(event.record_hash for event in events),
            "coverage": tuple(item.as_dict() for item in coverage),
            "coverage_complete": not missing,
            "missing_sources": missing,
        }
        snapshot = DailyLossSnapshot(
            utc_day=now.date(),
            as_of=now,
            limit=self._binding.daily_loss_limit,
            used=used,
            remaining=remaining,
            realized_loss_debit=realized,
            fee_debit=fees,
            funding_debit=funding,
            event_count=len(events),
            coverage=coverage,
            coverage_complete=not missing,
            missing_sources=missing,
            binding_hash=self._binding.binding_hash,
            snapshot_hash=domain_hash(
                "trading-harness/daily-loss-snapshot/v1", material
            ),
        )
        if require_complete and missing:
            raise IncompleteDailyLossCoverage(missing)
        return snapshot

    def latest_complete_snapshot(
        self,
        *,
        maximum_age_seconds: int = 5,
    ) -> DailyLossSnapshot:
        """Return the newest common complete source watermark within a bound.

        This never fills or rounds a coverage gap.  It finds the independently
        verified contiguous prefix for both required streams, selects the
        earlier source watermark, and then recomputes the budget exactly at
        that instant.  If either prefix is missing or the common watermark is
        stale, admission remains unavailable.
        """

        if (
            type(maximum_age_seconds) is not int
            or not 0 <= maximum_age_seconds <= 300
        ):
            raise ValidationError(
                "maximum_age_seconds must be an integer from 0 through 300"
            )
        observed = self._now()
        start = _day_start(observed)
        statuses: list[CoverageStatus] = []
        with self._connection() as connection:
            self._verify_schema(connection)
            self._bind_or_verify(connection, observed, allow_create=False)
            for source in (LossCoverageSource.FILLS, LossCoverageSource.FUNDING):
                rows = connection.execute(
                    """
                    SELECT * FROM daily_loss_coverage
                    WHERE source = ? AND covered_through >= ? AND covered_from <= ?
                    ORDER BY covered_from, covered_through, coverage_id
                    """,
                    (
                        source.value,
                        _time_text(start, field="day_start"),
                        _time_text(observed, field="as_of"),
                    ),
                ).fetchall()
                statuses.append(
                    self._coverage_status(
                        list(rows),
                        source=source,
                        required_from=start,
                        required_through=observed,
                        binding_hash=self._binding.binding_hash,
                    )
                )
        missing = tuple(
            item.source.value
            for item in statuses
            if item.covered_from is None
            or item.covered_from > start
            or item.covered_through is None
        )
        if missing:
            raise IncompleteDailyLossCoverage(missing)
        common = min(
            item.covered_through for item in statuses if item.covered_through is not None
        )
        if common.date() != observed.date() or (
            observed - common > timedelta(seconds=maximum_age_seconds)
        ):
            raise StateConflict("latest complete daily-loss watermark is stale")
        return self.snapshot(require_complete=True, as_of=common)

    def remaining(self) -> Decimal:
        """Return an admission-safe remaining budget, never an estimate."""

        return self.snapshot(require_complete=True).remaining


__all__ = (
    "DAILY_LOSS_SCHEMA_VERSION",
    "REQUIRED_COVERAGE_SOURCES",
    "CoverageStatus",
    "DailyLossBinding",
    "DailyLossEvent",
    "DailyLossLedger",
    "DailyLossSnapshot",
    "IncompleteDailyLossCoverage",
    "LossCoverageSource",
    "LossEventKind",
)
