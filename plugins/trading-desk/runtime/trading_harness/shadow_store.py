"""Durable append-only persistence for prospective shadow evidence.

The store owns only ``shadow_*`` SQLite objects.  It has no account, network,
agent-runtime, signer, authorization, or venue interface.  Every append runs
under ``BEGIN IMMEDIATE``, reconstructs and verifies the complete prior
:class:`~trading_harness.shadow.ShadowLedger`, applies the domain append rule,
and persists exactly one sequence/hash-chain link.

Validation artifacts cannot be supplied by callers.  ``evaluate_and_store``
loads the durable protocol and ledger inside the same write transaction,
calls :func:`trading_harness.shadow.evaluate_shadow`, and stores only that
canonical result.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping

from .backtest import (
    GateCheck,
    PerformanceMetrics,
    PromotionDecision,
    PromotionStatus,
)
from .canonical import canonical_json, domain_hash
from .errors import RecordNotFound, StateConflict, StorageError, ValidationError
from .shadow import (
    DriftAssessment,
    DriftStatus,
    IncrementalComparison,
    SentimentAuthority,
    ShadowLedger,
    ShadowLedgerError,
    ShadowOutcomeRecord,
    ShadowProtocol,
    ShadowRecordStatus,
    ShadowSignalRecord,
    ShadowValidationArtifact,
    ShadowVariant,
    evaluate_shadow,
)
from .strategy import SignalDirection


SHADOW_STORE_SCHEMA_VERSION = 1
_MAX_PROTOCOL_BYTES = 128 * 1024
_MAX_EVENT_BYTES = 512 * 1024
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(
    r"^[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime, field: str) -> str:
    return _utc(value, field).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError(f"persisted {field} is not text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError(f"persisted {field} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError(f"persisted {field} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise StorageError(f"persisted {field} is not a lowercase SHA-256 digest")
    return value


def _input_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _payload(value: object, maximum_bytes: int) -> tuple[str, str]:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValidationError("shadow payload is not canonical JSON") from error
    if len(encoded) > maximum_bytes:
        raise ValidationError("shadow payload exceeds its size limit")
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _decode_payload(
    payload_json: object,
    payload_hash: object,
    *,
    field: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    if not isinstance(payload_json, str):
        raise StorageError(f"persisted {field} payload is not text")
    encoded = payload_json.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise StorageError(f"persisted {field} payload exceeds its size limit")
    expected = _sha256(payload_hash, f"{field} payload_hash")
    if hashlib.sha256(encoded).hexdigest() != expected:
        raise StorageError(f"persisted {field} payload hash does not match")
    try:
        decoded = json.loads(payload_json)
        recanonicalized = canonical_json(decoded)
    except (TypeError, ValueError, RecursionError) as error:
        raise StorageError(f"persisted {field} payload is invalid") from error
    if recanonicalized != payload_json or not isinstance(decoded, dict):
        raise StorageError(f"persisted {field} payload is not a canonical object")
    return decoded


def _keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise StorageError(f"persisted {field} fields do not match its schema")


def _integer(value: object, field: str, *, nonnegative: bool = True) -> int:
    if type(value) is not int or (nonnegative and value < 0):
        raise StorageError(f"persisted {field} is not a valid integer")
    return value


def _decimal(value: object, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise StorageError(f"persisted {field} is not an exact decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise StorageError(f"persisted {field} decimal is invalid") from error
    if not parsed.is_finite():
        raise StorageError(f"persisted {field} decimal is not finite")
    return parsed


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        joined = "\n-- shadow migration statement --\n".join(self.statements)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


_SCHEMA_V1 = _Migration(
    version=1,
    name="prospective_shadow_evidence",
    statements=(
        """
        CREATE TABLE shadow_protocols (
            protocol_hash TEXT PRIMARY KEY,
            protocol_id TEXT NOT NULL,
            version TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            UNIQUE (protocol_id, version)
        )
        """,
        """
        CREATE TABLE shadow_events (
            protocol_hash TEXT NOT NULL
                REFERENCES shadow_protocols(protocol_hash),
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            record_type TEXT NOT NULL CHECK (record_type IN ('signal', 'outcome')),
            event_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            signal_hash TEXT NOT NULL,
            comparison_id TEXT,
            variant TEXT CHECK (
                variant IS NULL OR variant IN ('ta_only', 'ta_plus_sentiment')
            ),
            recorded_at TEXT NOT NULL,
            appended_at TEXT NOT NULL,
            previous_chain_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            chain_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            PRIMARY KEY (protocol_hash, sequence),
            UNIQUE (protocol_hash, event_id)
        )
        """,
        """
        CREATE UNIQUE INDEX idx_shadow_signal_id
        ON shadow_events (protocol_hash, signal_id)
        WHERE record_type = 'signal'
        """,
        """
        CREATE UNIQUE INDEX idx_shadow_outcome_signal_id
        ON shadow_events (protocol_hash, signal_id)
        WHERE record_type = 'outcome'
        """,
        """
        CREATE UNIQUE INDEX idx_shadow_signal_hash
        ON shadow_events (protocol_hash, signal_hash)
        WHERE record_type = 'signal'
        """,
        """
        CREATE UNIQUE INDEX idx_shadow_comparison_variant
        ON shadow_events (protocol_hash, comparison_id, variant)
        WHERE record_type = 'signal'
        """,
        """
        CREATE TABLE shadow_validation_artifacts (
            artifact_hash TEXT PRIMARY KEY,
            protocol_hash TEXT NOT NULL
                REFERENCES shadow_protocols(protocol_hash),
            as_of TEXT NOT NULL,
            ledger_chain_hash TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            UNIQUE (protocol_hash, as_of)
        )
        """,
        """
        CREATE TRIGGER shadow_protocols_no_update
        BEFORE UPDATE ON shadow_protocols
        BEGIN SELECT RAISE(ABORT, 'shadow protocols are immutable'); END
        """,
        """
        CREATE TRIGGER shadow_protocols_no_delete
        BEFORE DELETE ON shadow_protocols
        BEGIN SELECT RAISE(ABORT, 'shadow protocols are immutable'); END
        """,
        """
        CREATE TRIGGER shadow_events_no_update
        BEFORE UPDATE ON shadow_events
        BEGIN SELECT RAISE(ABORT, 'shadow events are append-only'); END
        """,
        """
        CREATE TRIGGER shadow_events_no_delete
        BEFORE DELETE ON shadow_events
        BEGIN SELECT RAISE(ABORT, 'shadow events are append-only'); END
        """,
        """
        CREATE TRIGGER shadow_artifacts_no_update
        BEFORE UPDATE ON shadow_validation_artifacts
        BEGIN SELECT RAISE(ABORT, 'shadow artifacts are immutable'); END
        """,
        """
        CREATE TRIGGER shadow_artifacts_no_delete
        BEFORE DELETE ON shadow_validation_artifacts
        BEGIN SELECT RAISE(ABORT, 'shadow artifacts are immutable'); END
        """,
    ),
)

_MIGRATIONS = (_SCHEMA_V1,)

_EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "shadow_schema_migrations": ("version", "name", "checksum", "applied_at"),
    "shadow_protocols": (
        "protocol_hash",
        "protocol_id",
        "version",
        "asset_id",
        "registered_at",
        "started_at",
        "stored_at",
        "payload_json",
        "payload_hash",
    ),
    "shadow_events": (
        "protocol_hash",
        "sequence",
        "record_type",
        "event_id",
        "signal_id",
        "signal_hash",
        "comparison_id",
        "variant",
        "recorded_at",
        "appended_at",
        "previous_chain_hash",
        "event_hash",
        "chain_hash",
        "payload_json",
        "payload_hash",
    ),
    "shadow_validation_artifacts": (
        "artifact_hash",
        "protocol_hash",
        "as_of",
        "ledger_chain_hash",
        "stored_at",
        "payload_json",
        "payload_hash",
    ),
}

_EXPECTED_INDEXES = {
    "idx_shadow_signal_id",
    "idx_shadow_outcome_signal_id",
    "idx_shadow_signal_hash",
    "idx_shadow_comparison_variant",
}
_EXPECTED_TRIGGERS = {
    "shadow_protocols_no_update",
    "shadow_protocols_no_delete",
    "shadow_events_no_update",
    "shadow_events_no_delete",
    "shadow_artifacts_no_update",
    "shadow_artifacts_no_delete",
}


def _protocol_from_payload(value: Mapping[str, Any]) -> ShadowProtocol:
    _keys(
        value,
        {
            "protocol_id",
            "version",
            "asset_id",
            "registered_at",
            "started_at",
            "ta_strategy_hash",
            "sentiment_strategy_hash",
            "cost_model_hash",
            "drift_policy_hash",
            "minimum_elapsed_days",
            "minimum_closed_signals",
            "minimum_incremental_r",
            "schema_version",
        },
        "shadow protocol",
    )
    try:
        return ShadowProtocol(
            protocol_id=value["protocol_id"],
            version=value["version"],
            asset_id=value["asset_id"],
            registered_at=_parse_time(value["registered_at"], "registered_at"),
            started_at=_parse_time(value["started_at"], "started_at"),
            ta_strategy_hash=value["ta_strategy_hash"],
            sentiment_strategy_hash=value["sentiment_strategy_hash"],
            cost_model_hash=value["cost_model_hash"],
            drift_policy_hash=value["drift_policy_hash"],
            minimum_elapsed_days=value["minimum_elapsed_days"],
            minimum_closed_signals=value["minimum_closed_signals"],
            minimum_incremental_r=_decimal(
                value["minimum_incremental_r"], "minimum_incremental_r"
            ),
            schema_version=value["schema_version"],
        )
    except (TypeError, ValueError) as error:
        raise StorageError("persisted shadow protocol failed validation") from error


def _signal_from_payload(value: Mapping[str, Any]) -> ShadowSignalRecord:
    _keys(
        value,
        {
            "event_id",
            "signal_id",
            "comparison_id",
            "asset_id",
            "variant",
            "direction",
            "strategy_hash",
            "signal_hash",
            "data_hash",
            "cost_model_hash",
            "evidence_hash",
            "observed_at",
            "expires_at",
            "recorded_at",
            "eligible",
        },
        "shadow signal",
    )
    try:
        return ShadowSignalRecord(
            event_id=value["event_id"],
            signal_id=value["signal_id"],
            comparison_id=value["comparison_id"],
            asset_id=value["asset_id"],
            variant=ShadowVariant(value["variant"]),
            direction=SignalDirection(value["direction"]),
            strategy_hash=value["strategy_hash"],
            signal_hash=value["signal_hash"],
            data_hash=value["data_hash"],
            cost_model_hash=value["cost_model_hash"],
            evidence_hash=value["evidence_hash"],
            observed_at=_parse_time(value["observed_at"], "observed_at"),
            expires_at=_parse_time(value["expires_at"], "expires_at"),
            recorded_at=_parse_time(value["recorded_at"], "recorded_at"),
            eligible=value["eligible"],
        )
    except (TypeError, ValueError) as error:
        raise StorageError("persisted shadow signal failed validation") from error


def _outcome_from_payload(value: Mapping[str, Any]) -> ShadowOutcomeRecord:
    _keys(
        value,
        {
            "event_id",
            "signal_id",
            "signal_event_hash",
            "strategy_hash",
            "signal_hash",
            "data_hash",
            "cost_model_hash",
            "outcome_evidence_hash",
            "status",
            "closed_at",
            "recorded_at",
            "gross_r",
            "cost_r",
            "net_r",
            "invalid_reason",
        },
        "shadow outcome",
    )
    try:
        return ShadowOutcomeRecord(
            event_id=value["event_id"],
            signal_id=value["signal_id"],
            signal_event_hash=value["signal_event_hash"],
            strategy_hash=value["strategy_hash"],
            signal_hash=value["signal_hash"],
            data_hash=value["data_hash"],
            cost_model_hash=value["cost_model_hash"],
            outcome_evidence_hash=value["outcome_evidence_hash"],
            status=ShadowRecordStatus(value["status"]),
            closed_at=_parse_time(value["closed_at"], "closed_at"),
            recorded_at=_parse_time(value["recorded_at"], "recorded_at"),
            gross_r=_decimal(value["gross_r"], "gross_r", optional=True),
            cost_r=_decimal(value["cost_r"], "cost_r", optional=True),
            net_r=_decimal(value["net_r"], "net_r", optional=True),
            invalid_reason=value["invalid_reason"],
        )
    except (TypeError, ValueError) as error:
        raise StorageError("persisted shadow outcome failed validation") from error


def _metrics_from_payload(value: object, field: str) -> PerformanceMetrics:
    if not isinstance(value, dict):
        raise StorageError(f"persisted {field} is not an object")
    _keys(
        value,
        {
            "trade_count",
            "total_net_r",
            "expectancy_r",
            "gross_profit_r",
            "gross_loss_r",
            "profit_factor",
            "max_drawdown_r",
            "best_trade_contribution",
            "bootstrap_lower_95_r",
            "bootstrap_block_length",
            "bootstrap_samples",
        },
        field,
    )
    return PerformanceMetrics(
        trade_count=_integer(value["trade_count"], f"{field}.trade_count"),
        total_net_r=_decimal(value["total_net_r"], f"{field}.total_net_r"),  # type: ignore[arg-type]
        expectancy_r=_decimal(value["expectancy_r"], f"{field}.expectancy_r"),  # type: ignore[arg-type]
        gross_profit_r=_decimal(value["gross_profit_r"], f"{field}.gross_profit_r"),  # type: ignore[arg-type]
        gross_loss_r=_decimal(value["gross_loss_r"], f"{field}.gross_loss_r"),  # type: ignore[arg-type]
        profit_factor=_decimal(value["profit_factor"], f"{field}.profit_factor", optional=True),
        max_drawdown_r=_decimal(value["max_drawdown_r"], f"{field}.max_drawdown_r"),  # type: ignore[arg-type]
        best_trade_contribution=_decimal(
            value["best_trade_contribution"],
            f"{field}.best_trade_contribution",
            optional=True,
        ),
        bootstrap_lower_95_r=_decimal(
            value["bootstrap_lower_95_r"],
            f"{field}.bootstrap_lower_95_r",
            optional=True,
        ),
        bootstrap_block_length=_integer(
            value["bootstrap_block_length"], f"{field}.bootstrap_block_length"
        ),
        bootstrap_samples=_integer(
            value["bootstrap_samples"], f"{field}.bootstrap_samples"
        ),
    )


def _actual(value: object, field: str) -> Decimal | int | str | None:
    if value is None or type(value) is int:
        return value
    if not isinstance(value, str):
        raise StorageError(f"persisted {field} has an invalid type")
    if _DECIMAL_RE.fullmatch(value):
        parsed = _decimal(value, field)
        assert parsed is not None
        return parsed
    return value


def _promotion_from_payload(value: object, field: str) -> PromotionDecision:
    if not isinstance(value, dict):
        raise StorageError(f"persisted {field} is not an object")
    _keys(value, {"status", "checks", "reasons"}, field)
    checks_raw = value["checks"]
    reasons_raw = value["reasons"]
    if not isinstance(checks_raw, list) or not isinstance(reasons_raw, list):
        raise StorageError(f"persisted {field} arrays are invalid")
    checks: list[GateCheck] = []
    for index, item in enumerate(checks_raw):
        if not isinstance(item, dict):
            raise StorageError(f"persisted {field}.checks[{index}] is invalid")
        _keys(item, {"name", "passed", "actual", "requirement"}, f"{field}.check")
        if type(item["passed"]) is not bool:
            raise StorageError(f"persisted {field}.check passed is invalid")
        checks.append(
            GateCheck(
                name=item["name"],
                passed=item["passed"],
                actual=_actual(item["actual"], f"{field}.check.actual"),
                requirement=item["requirement"],
            )
        )
    if any(not isinstance(reason, str) for reason in reasons_raw):
        raise StorageError(f"persisted {field}.reasons is invalid")
    try:
        return PromotionDecision(
            status=PromotionStatus(value["status"]),
            checks=tuple(checks),
            reasons=tuple(reasons_raw),
        )
    except (TypeError, ValueError) as error:
        raise StorageError(f"persisted {field} failed validation") from error


def _artifact_from_payload(value: Mapping[str, Any]) -> ShadowValidationArtifact:
    _keys(
        value,
        {
            "schema_version",
            "protocol_hash",
            "ledger_chain_hash",
            "as_of",
            "elapsed_seconds",
            "pending_signals",
            "invalid_signals",
            "ta_metrics",
            "sentiment_metrics",
            "drift",
            "promotion",
            "incremental",
            "sentiment_authority",
        },
        "shadow validation artifact",
    )
    drift_raw = value["drift"]
    incremental_raw = value["incremental"]
    if not isinstance(drift_raw, dict) or not isinstance(incremental_raw, dict):
        raise StorageError("persisted shadow artifact nested objects are invalid")
    _keys(
        drift_raw,
        {"policy_hash", "status", "assessed_at", "evidence_hash"},
        "drift",
    )
    _keys(
        incremental_raw,
        {
            "paired_count",
            "ta_expectancy_r",
            "sentiment_expectancy_r",
            "mean_incremental_r",
            "lower_95_incremental_r",
            "promotion",
        },
        "incremental",
    )
    try:
        drift = DriftAssessment(
            policy_hash=drift_raw["policy_hash"],
            status=DriftStatus(drift_raw["status"]),
            assessed_at=_parse_time(drift_raw["assessed_at"], "drift.assessed_at"),
            evidence_hash=drift_raw["evidence_hash"],
        )
        incremental = IncrementalComparison(
            paired_count=_integer(incremental_raw["paired_count"], "paired_count"),
            ta_expectancy_r=_decimal(incremental_raw["ta_expectancy_r"], "ta_expectancy_r"),  # type: ignore[arg-type]
            sentiment_expectancy_r=_decimal(
                incremental_raw["sentiment_expectancy_r"], "sentiment_expectancy_r"
            ),  # type: ignore[arg-type]
            mean_incremental_r=_decimal(
                incremental_raw["mean_incremental_r"], "mean_incremental_r"
            ),  # type: ignore[arg-type]
            lower_95_incremental_r=_decimal(
                incremental_raw["lower_95_incremental_r"],
                "lower_95_incremental_r",
                optional=True,
            ),
            promotion=_promotion_from_payload(
                incremental_raw["promotion"], "incremental.promotion"
            ),
        )
        return ShadowValidationArtifact(
            schema_version=value["schema_version"],
            protocol_hash=value["protocol_hash"],
            ledger_chain_hash=value["ledger_chain_hash"],
            as_of=_parse_time(value["as_of"], "as_of"),
            elapsed_seconds=_integer(value["elapsed_seconds"], "elapsed_seconds"),
            pending_signals=_integer(value["pending_signals"], "pending_signals"),
            invalid_signals=_integer(value["invalid_signals"], "invalid_signals"),
            ta_metrics=_metrics_from_payload(value["ta_metrics"], "ta_metrics"),
            sentiment_metrics=_metrics_from_payload(
                value["sentiment_metrics"], "sentiment_metrics"
            ),
            drift=drift,
            promotion=_promotion_from_payload(value["promotion"], "promotion"),
            incremental=incremental,
            sentiment_authority=SentimentAuthority(value["sentiment_authority"]),
        )
    except (TypeError, ValueError) as error:
        raise StorageError("persisted shadow artifact failed validation") from error


class ShadowStore:
    """File-backed, namespaced append store for prospective shadow evidence."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValidationError("ShadowStore requires a file-backed database")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValidationError("busy_timeout_ms must be a positive integer")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA synchronous = FULL")
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
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StorageError(f"SQLite refused WAL mode: {mode}")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            self._verify_columns(connection, "shadow_schema_migrations")
            rows = connection.execute(
                "SELECT version, name, checksum FROM shadow_schema_migrations ORDER BY version"
            ).fetchall()
            known = {migration.version: migration for migration in _MIGRATIONS}
            seen: list[int] = []
            for row in rows:
                version = int(row["version"])
                migration = known.get(version)
                if migration is None:
                    raise StorageError(f"unknown shadow migration version {version}")
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise StorageError(f"shadow migration {version} checksum or name mismatch")
                seen.append(version)
            if seen != list(range(1, len(seen) + 1)):
                raise StorageError("shadow migration history is not contiguous")
            applied = set(seen)
            now = _time_text(datetime.now(timezone.utc), "migration_time")
            for migration in _MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO shadow_schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (migration.version, migration.name, migration.checksum, now),
                )
            self._verify_schema(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise StorageError(
                f"shadow schema initialization failed: {type(error).__name__}"
            ) from error
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _verify_columns(connection: sqlite3.Connection, table: str) -> None:
        columns = tuple(
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if columns != _EXPECTED_COLUMNS[table]:
            raise StorageError(f"shadow table {table} has an unexpected schema")

    def _verify_schema(self, connection: sqlite3.Connection) -> None:
        for table in _EXPECTED_COLUMNS:
            self._verify_columns(connection, table)
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        triggers = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if not _EXPECTED_INDEXES.issubset(indexes):
            raise StorageError("shadow schema is missing a required index")
        if not _EXPECTED_TRIGGERS.issubset(triggers):
            raise StorageError("shadow schema is missing an immutability trigger")

    def register_protocol(
        self, protocol: ShadowProtocol, *, stored_at: datetime | None = None
    ) -> ShadowProtocol:
        if not isinstance(protocol, ShadowProtocol):
            raise TypeError("protocol must be ShadowProtocol")
        at = datetime.now(timezone.utc) if stored_at is None else _utc(stored_at, "stored_at")
        payload_json, payload_hash = _payload(protocol, _MAX_PROTOCOL_BYTES)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_protocols WHERE protocol_hash = ?",
                (protocol.protocol_hash,),
            ).fetchone()
            if row is not None:
                loaded = self._protocol_from_row(row)
                if loaded != protocol:
                    raise StateConflict("protocol hash is bound to different content")
                return loaded
            if at < protocol.registered_at:
                raise ValidationError("protocol cannot be stored before registration")
            if at > protocol.started_at:
                raise ValidationError(
                    "prospective protocol must be stored before shadow start"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO shadow_protocols(
                        protocol_hash, protocol_id, version, asset_id,
                        registered_at, started_at, stored_at, payload_json, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        protocol.protocol_hash,
                        protocol.protocol_id,
                        protocol.version,
                        protocol.asset_id,
                        _time_text(protocol.registered_at, "registered_at"),
                        _time_text(protocol.started_at, "started_at"),
                        _time_text(at, "stored_at"),
                        payload_json,
                        payload_hash,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("protocol identity is already registered") from error
        return protocol

    @staticmethod
    def _protocol_from_row(row: Mapping[str, Any]) -> ShadowProtocol:
        payload = _decode_payload(
            row["payload_json"],
            row["payload_hash"],
            field="shadow protocol",
            maximum_bytes=_MAX_PROTOCOL_BYTES,
        )
        protocol = _protocol_from_payload(payload)
        comparisons = (
            (row["protocol_hash"], protocol.protocol_hash, "protocol_hash"),
            (row["protocol_id"], protocol.protocol_id, "protocol_id"),
            (row["version"], protocol.version, "version"),
            (row["asset_id"], protocol.asset_id, "asset_id"),
            (
                _parse_time(row["registered_at"], "registered_at"),
                protocol.registered_at,
                "registered_at",
            ),
            (
                _parse_time(row["started_at"], "started_at"),
                protocol.started_at,
                "started_at",
            ),
        )
        if any(stored != expected for stored, expected, _ in comparisons):
            raise StorageError("shadow protocol columns disagree with payload")
        stored_at = _parse_time(row["stored_at"], "stored_at")
        if not protocol.registered_at <= stored_at <= protocol.started_at:
            raise StorageError("shadow protocol stored_at violates prospective ordering")
        return protocol

    def get_protocol(self, protocol_hash: str) -> ShadowProtocol:
        checked = _input_hash(protocol_hash, "protocol_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM shadow_protocols WHERE protocol_hash = ?", (checked,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("shadow protocol not found")
        return self._protocol_from_row(row)

    def _protocol_in_transaction(
        self, connection: sqlite3.Connection, protocol_hash: str
    ) -> ShadowProtocol:
        row = connection.execute(
            "SELECT * FROM shadow_protocols WHERE protocol_hash = ?", (protocol_hash,)
        ).fetchone()
        if row is None:
            raise RecordNotFound("shadow protocol not found")
        return self._protocol_from_row(row)

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> ShadowSignalRecord | ShadowOutcomeRecord:
        record_type = row["record_type"]
        if record_type not in {"signal", "outcome"}:
            raise StorageError("persisted shadow event record_type is invalid")
        payload = _decode_payload(
            row["payload_json"],
            row["payload_hash"],
            field=f"shadow {record_type}",
            maximum_bytes=_MAX_EVENT_BYTES,
        )
        event = (
            _signal_from_payload(payload)
            if record_type == "signal"
            else _outcome_from_payload(payload)
        )
        if row["event_id"] != event.event_id or row["signal_id"] != event.signal_id:
            raise StorageError("shadow event identity columns disagree with payload")
        if row["signal_hash"] != event.signal_hash:
            raise StorageError("shadow event signal_hash column disagrees with payload")
        if _parse_time(row["recorded_at"], "recorded_at") != event.recorded_at:
            raise StorageError("shadow event recorded_at column disagrees with payload")
        if isinstance(event, ShadowSignalRecord):
            if row["comparison_id"] != event.comparison_id or row["variant"] != event.variant.value:
                raise StorageError("shadow signal columns disagree with payload")
        elif row["comparison_id"] is not None or row["variant"] is not None:
            raise StorageError("shadow outcome contains signal-only columns")
        if _sha256(row["event_hash"], "event_hash") != event.event_hash:
            raise StorageError("shadow event hash does not match payload")
        _parse_time(row["appended_at"], "appended_at")
        return event

    def _ledger_in_transaction(
        self,
        connection: sqlite3.Connection,
        protocol: ShadowProtocol,
        *,
        appended_through: datetime | None = None,
    ) -> ShadowLedger:
        ledger = ShadowLedger.create(protocol)
        if appended_through is None:
            rows = connection.execute(
                "SELECT * FROM shadow_events WHERE protocol_hash = ? ORDER BY sequence",
                (protocol.protocol_hash,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM shadow_events
                WHERE protocol_hash = ? AND appended_at <= ?
                ORDER BY sequence
                """,
                (
                    protocol.protocol_hash,
                    _time_text(appended_through, "appended_through"),
                ),
            ).fetchall()
        for expected_sequence, row in enumerate(rows):
            if type(row["sequence"]) is not int or row["sequence"] != expected_sequence:
                raise StorageError("shadow event sequence is not contiguous")
            previous = _sha256(row["previous_chain_hash"], "previous_chain_hash")
            if previous != ledger.chain_hash:
                raise StorageError("shadow event previous chain hash is invalid")
            event = self._event_from_row(row)
            try:
                ledger = (
                    ledger.append_signal(protocol, event)
                    if isinstance(event, ShadowSignalRecord)
                    else ledger.append_outcome(protocol, event)
                )
            except ShadowLedgerError as error:
                raise StorageError("persisted shadow event violates ledger invariants") from error
            if _sha256(row["chain_hash"], "chain_hash") != ledger.chain_hash:
                raise StorageError("shadow event chain hash is invalid")
        ledger.verify_for(protocol)
        return ledger

    def load_ledger(self, protocol_hash: str) -> ShadowLedger:
        checked = _input_hash(protocol_hash, "protocol_hash")
        connection = self._connect()
        try:
            protocol = self._protocol_in_transaction(connection, checked)
            return self._ledger_in_transaction(connection, protocol)
        finally:
            connection.close()

    def _append_event(
        self,
        protocol_hash: str,
        event: ShadowSignalRecord | ShadowOutcomeRecord,
        *,
        appended_at: datetime | None,
    ) -> ShadowLedger:
        checked = _input_hash(protocol_hash, "protocol_hash")
        at = datetime.now(timezone.utc) if appended_at is None else _utc(appended_at, "appended_at")
        if event.recorded_at > at:
            raise ValidationError("shadow event is future evidence at append time")
        if isinstance(event, ShadowSignalRecord) and at > event.expires_at:
            raise ValidationError("shadow signal was appended after its expiry")
        record_type = "signal" if isinstance(event, ShadowSignalRecord) else "outcome"
        payload_json, payload_hash = _payload(event, _MAX_EVENT_BYTES)
        with self._transaction() as connection:
            protocol = self._protocol_in_transaction(connection, checked)
            ledger = self._ledger_in_transaction(connection, protocol)
            last_row = connection.execute(
                """
                SELECT appended_at FROM shadow_events
                WHERE protocol_hash = ? ORDER BY sequence DESC LIMIT 1
                """,
                (checked,),
            ).fetchone()
            if last_row is not None and _parse_time(
                last_row["appended_at"], "appended_at"
            ) > at:
                raise StateConflict("shadow append time cannot move backwards")
            previous = ledger.chain_hash
            try:
                next_ledger = (
                    ledger.append_signal(protocol, event)
                    if isinstance(event, ShadowSignalRecord)
                    else ledger.append_outcome(protocol, event)
                )
            except ShadowLedgerError as error:
                raise StateConflict(str(error)) from error
            sequence = len(ledger.events)
            try:
                connection.execute(
                    """
                    INSERT INTO shadow_events(
                        protocol_hash, sequence, record_type, event_id,
                        signal_id, signal_hash, comparison_id, variant,
                        recorded_at, appended_at, previous_chain_hash,
                        event_hash, chain_hash, payload_json, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checked,
                        sequence,
                        record_type,
                        event.event_id,
                        event.signal_id,
                        event.signal_hash,
                        event.comparison_id
                        if isinstance(event, ShadowSignalRecord)
                        else None,
                        event.variant.value
                        if isinstance(event, ShadowSignalRecord)
                        else None,
                        _time_text(event.recorded_at, "recorded_at"),
                        _time_text(at, "appended_at"),
                        previous,
                        event.event_hash,
                        next_ledger.chain_hash,
                        payload_json,
                        payload_hash,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("shadow event conflicts with durable append state") from error
            return next_ledger

    def append_signal(
        self,
        protocol_hash: str,
        signal: ShadowSignalRecord,
        *,
        appended_at: datetime | None = None,
    ) -> ShadowLedger:
        if not isinstance(signal, ShadowSignalRecord):
            raise TypeError("signal must be ShadowSignalRecord")
        return self._append_event(protocol_hash, signal, appended_at=appended_at)

    def append_outcome(
        self,
        protocol_hash: str,
        outcome: ShadowOutcomeRecord,
        *,
        appended_at: datetime | None = None,
    ) -> ShadowLedger:
        if not isinstance(outcome, ShadowOutcomeRecord):
            raise TypeError("outcome must be ShadowOutcomeRecord")
        return self._append_event(protocol_hash, outcome, appended_at=appended_at)

    def evaluate_and_store(
        self,
        protocol_hash: str,
        drift: DriftAssessment,
        *,
        as_of: datetime,
        stored_at: datetime | None = None,
    ) -> ShadowValidationArtifact:
        checked = _input_hash(protocol_hash, "protocol_hash")
        if not isinstance(drift, DriftAssessment):
            raise TypeError("drift must be DriftAssessment")
        checked_as_of = _utc(as_of, "as_of")
        at = datetime.now(timezone.utc) if stored_at is None else _utc(stored_at, "stored_at")
        if at < checked_as_of:
            raise ValidationError("shadow artifact cannot be stored before as_of")
        with self._transaction() as connection:
            protocol = self._protocol_in_transaction(connection, checked)
            # Verify the complete durable stream even when evaluating a
            # historical prefix.  Corruption after ``as_of`` must not be
            # silently ignored by an otherwise valid earlier artifact.
            self._ledger_in_transaction(connection, protocol)
            ledger = self._ledger_in_transaction(
                connection, protocol, appended_through=checked_as_of
            )
            try:
                artifact = evaluate_shadow(
                    protocol, ledger, drift, as_of=checked_as_of
                )
            except ShadowLedgerError as error:
                raise ValidationError(str(error)) from error
            payload_json, payload_hash = _payload(artifact, _MAX_ARTIFACT_BYTES)
            existing = connection.execute(
                """
                SELECT * FROM shadow_validation_artifacts
                WHERE protocol_hash = ? AND as_of = ?
                """,
                (checked, _time_text(checked_as_of, "as_of")),
            ).fetchone()
            if existing is not None:
                loaded = self._artifact_from_row(existing)
                if loaded.artifact_hash != artifact.artifact_hash:
                    raise StateConflict("shadow as_of is already bound to another artifact")
                return loaded
            try:
                connection.execute(
                    """
                    INSERT INTO shadow_validation_artifacts(
                        artifact_hash, protocol_hash, as_of, ledger_chain_hash,
                        stored_at, payload_json, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_hash,
                        checked,
                        _time_text(checked_as_of, "as_of"),
                        ledger.chain_hash,
                        _time_text(at, "stored_at"),
                        payload_json,
                        payload_hash,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflict("shadow artifact conflicts with immutable state") from error
            return artifact

    @staticmethod
    def _artifact_from_row(row: Mapping[str, Any]) -> ShadowValidationArtifact:
        payload = _decode_payload(
            row["payload_json"],
            row["payload_hash"],
            field="shadow validation artifact",
            maximum_bytes=_MAX_ARTIFACT_BYTES,
        )
        artifact = _artifact_from_payload(payload)
        if _sha256(row["artifact_hash"], "artifact_hash") != artifact.artifact_hash:
            raise StorageError("shadow artifact hash does not match payload")
        if row["protocol_hash"] != artifact.protocol_hash:
            raise StorageError("shadow artifact protocol column disagrees with payload")
        if _parse_time(row["as_of"], "as_of") != artifact.as_of:
            raise StorageError("shadow artifact as_of column disagrees with payload")
        if row["ledger_chain_hash"] != artifact.ledger_chain_hash:
            raise StorageError("shadow artifact ledger chain column disagrees with payload")
        _parse_time(row["stored_at"], "stored_at")
        return artifact

    def get_validation_artifact(self, artifact_hash: str) -> ShadowValidationArtifact:
        checked = _input_hash(artifact_hash, "artifact_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM shadow_validation_artifacts WHERE artifact_hash = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("shadow validation artifact not found")
        return self._artifact_from_row(row)


__all__ = ("SHADOW_STORE_SCHEMA_VERSION", "ShadowStore")
