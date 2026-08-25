"""Durable persistence for research artifacts and the always-on node.

This store is deliberately separate from :mod:`trading_harness.store`, whose
tables form the capital-admission foundation.  It may share the same SQLite
file, but it owns only ``research_*`` tables and confers no authorization or
venue-write capability.

The important invariants are:

* schema migrations are ordered and checksummed;
* tracked-asset changes use compare-and-swap revisions;
* research artifacts are canonical, content-addressed, and immutable;
* one fenced lease owns a node at a time; and
* stale node instances cannot update runtime or heartbeat state.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from .analysis import Candle, TechnicalSnapshot
from .assessment import OpportunityAssessment
from .canonical import canonical_json, domain_hash
from .errors import RecordNotFound, StateConflict, StorageError, ValidationError
from .history import HistoricalCandle
from .planning import RiskTicket
from .sentiment import SentimentSnapshot
from .tracking import MarketDataNetwork, TrackedAsset, TrackingStatus


_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_DETAILS_BYTES = 64 * 1024
_ARTIFACT_KINDS = frozenset(
    {"candle", "technical", "sentiment", "assessment", "risk_ticket"}
)
_PROCESS_STATES = frozenset(
    {"starting", "running", "degraded", "stopping", "stopped"}
)
_CAPABILITIES = frozenset({"research_only", "paper", "testnet", "mainnet"})
_RISK_GATES = frozenset({"halted", "reconciling", "ready"})
_HEARTBEAT_STATUSES = frozenset({"healthy", "degraded", "failed"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def _sha256(value: object, *, field: str) -> str:
    value = _text(value, field=field, maximum=64)
    if len(value) != 64 or any(character not in _SHA256_CHARACTERS for character in value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _stored_sha256(value: object, *, field: str) -> str:
    try:
        return _sha256(value, field=field)
    except ValidationError as error:
        raise StorageError(f"persisted {field} is invalid") from error


def _positive_int(
    value: object,
    *,
    field: str,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} exceeds {maximum}")
    return value


def _canonical_payload(value: object, *, maximum_bytes: int) -> tuple[str, str]:
    try:
        payload_json = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValidationError("payload is not bounded canonical JSON") from error
    encoded = payload_json.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise ValidationError("canonical payload exceeds its size limit")
    return payload_json, hashlib.sha256(encoded).hexdigest()


def _decode_canonical_payload(
    payload_json: object,
    content_hash: object,
    *,
    field: str,
    maximum_bytes: int,
) -> Any:
    if not isinstance(payload_json, str):
        raise StorageError(f"persisted {field} payload is not text")
    encoded = payload_json.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise StorageError(f"persisted {field} payload exceeds its size limit")
    expected_hash = _stored_sha256(content_hash, field=f"{field} content_hash")
    if hashlib.sha256(encoded).hexdigest() != expected_hash:
        raise StorageError(f"persisted {field} payload hash does not match")
    try:
        decoded = json.loads(payload_json)
        canonical = canonical_json(decoded)
    except (TypeError, ValueError, RecursionError) as error:
        raise StorageError(f"persisted {field} payload is not canonical JSON") from error
    if canonical != payload_json:
        raise StorageError(f"persisted {field} payload is not canonical JSON")
    return decoded


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        material = "\n-- research migration statement --\n".join(self.statements)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


_RESEARCH_SCHEMA_V1 = _Migration(
    version=1,
    name="research_node_foundation",
    statements=(
        """
        CREATE TABLE research_tracked_assets (
            asset_id TEXT PRIMARY KEY,
            venue TEXT NOT NULL,
            market_data_network TEXT NOT NULL,
            execution_environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            config_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (venue, market_data_network, symbol, interval)
        )
        """,
        """
        CREATE TABLE research_artifacts (
            artifact_kind TEXT NOT NULL CHECK (
                artifact_kind IN (
                    'candle', 'technical', 'sentiment',
                    'assessment', 'risk_ticket'
                )
            ),
            artifact_id TEXT NOT NULL,
            asset_id TEXT NOT NULL
                REFERENCES research_tracked_assets(asset_id),
            series_key TEXT NOT NULL,
            semantic_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            artifact_time TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (artifact_kind, artifact_id),
            UNIQUE (artifact_kind, semantic_hash)
        )
        """,
        """
        CREATE INDEX idx_research_artifacts_asset_time
        ON research_artifacts (
            asset_id, artifact_kind, series_key, artifact_time, artifact_id
        )
        """,
        """
        CREATE TABLE research_node_leases (
            node_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
            state TEXT NOT NULL CHECK (state IN ('active', 'released')),
            acquired_at TEXT NOT NULL,
            renewed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            released_at TEXT
        )
        """,
        """
        CREATE TABLE research_node_runtime (
            node_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
            generation INTEGER NOT NULL CHECK (generation > 0),
            revision INTEGER NOT NULL CHECK (revision > 0),
            process_state TEXT NOT NULL CHECK (
                process_state IN (
                    'starting', 'running', 'degraded', 'stopping', 'stopped'
                )
            ),
            capability TEXT NOT NULL CHECK (
                capability IN ('research_only', 'paper', 'testnet', 'mainnet')
            ),
            risk_gate TEXT NOT NULL CHECK (
                risk_gate IN ('halted', 'reconciling', 'ready')
            ),
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            details_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE research_node_heartbeats (
            node_id TEXT NOT NULL
                REFERENCES research_node_runtime(node_id),
            component TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
            status TEXT NOT NULL CHECK (
                status IN ('healthy', 'degraded', 'failed')
            ),
            observed_at TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            details_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY (node_id, component)
        )
        """,
        """
        CREATE INDEX idx_research_heartbeats_node
        ON research_node_heartbeats (node_id, fencing_token, component)
        """,
    ),
)

_RESEARCH_SCHEMA_V2 = _Migration(
    version=2,
    name="immutable_asset_analysis",
    statements=(
        """
        CREATE TABLE research_asset_analyses (
            analysis_hash TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES research_tracked_assets(asset_id),
            tracker_revision INTEGER NOT NULL CHECK (tracker_revision > 0),
            history_hash TEXT NOT NULL,
            signal_hash TEXT NOT NULL,
            sentiment_hash TEXT,
            assessment_hash TEXT,
            observed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_research_asset_analysis_time
        ON research_asset_analyses (asset_id, observed_at, analysis_hash)
        """,
        """
        CREATE TRIGGER trg_research_asset_analysis_no_update
        BEFORE UPDATE ON research_asset_analyses
        BEGIN SELECT RAISE(ABORT, 'research asset analyses are immutable'); END
        """,
        """
        CREATE TRIGGER trg_research_asset_analysis_no_delete
        BEFORE DELETE ON research_asset_analyses
        BEGIN SELECT RAISE(ABORT, 'research asset analyses are immutable'); END
        """,
    ),
)

_MIGRATIONS = (_RESEARCH_SCHEMA_V1, _RESEARCH_SCHEMA_V2)
RESEARCH_SCHEMA_VERSION = _MIGRATIONS[-1].version

_EXPECTED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "research_schema_migrations": (
        "version",
        "name",
        "checksum",
        "applied_at",
    ),
    "research_tracked_assets": (
        "asset_id",
        "venue",
        "market_data_network",
        "execution_environment",
        "symbol",
        "interval",
        "status",
        "revision",
        "config_hash",
        "payload_json",
        "payload_hash",
        "created_at",
        "updated_at",
    ),
    "research_artifacts": (
        "artifact_kind",
        "artifact_id",
        "asset_id",
        "series_key",
        "semantic_hash",
        "content_hash",
        "record_hash",
        "artifact_time",
        "stored_at",
        "payload_json",
    ),
    "research_node_leases": (
        "node_id",
        "instance_id",
        "fencing_token",
        "state",
        "acquired_at",
        "renewed_at",
        "expires_at",
        "released_at",
    ),
    "research_node_runtime": (
        "node_id",
        "instance_id",
        "fencing_token",
        "generation",
        "revision",
        "process_state",
        "capability",
        "risk_gate",
        "started_at",
        "updated_at",
        "details_json",
        "content_hash",
    ),
    "research_node_heartbeats": (
        "node_id",
        "component",
        "instance_id",
        "fencing_token",
        "status",
        "observed_at",
        "valid_until",
        "details_json",
        "content_hash",
    ),
    "research_asset_analyses": (
        "analysis_hash",
        "asset_id",
        "tracker_revision",
        "history_hash",
        "signal_hash",
        "sentiment_hash",
        "assessment_hash",
        "observed_at",
        "expires_at",
        "stored_at",
        "payload_json",
        "content_hash",
        "record_hash",
    ),
}

_EXPECTED_INDEXES = frozenset(
    {
        "idx_research_artifacts_asset_time",
        "idx_research_heartbeats_node",
        "idx_research_asset_analysis_time",
    }
)


@dataclass(frozen=True, slots=True)
class ResearchArtifactRecord:
    artifact_kind: str
    artifact_id: str
    asset_id: str
    series_key: str
    semantic_hash: str
    content_hash: str
    record_hash: str
    artifact_time: datetime
    stored_at: datetime
    payload_json: str

    @property
    def payload(self) -> Any:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class AssetAnalysisRecord:
    analysis_hash: str
    asset_id: str
    tracker_revision: int
    history_hash: str
    signal_hash: str
    sentiment_hash: str | None
    assessment_hash: str | None
    observed_at: datetime
    expires_at: datetime
    stored_at: datetime
    payload_json: str
    content_hash: str
    record_hash: str

    @property
    def payload(self) -> Mapping[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise StorageError("persisted asset analysis is not an object")
        return value


@dataclass(frozen=True, slots=True)
class NodeLeaseRecord:
    node_id: str
    instance_id: str
    fencing_token: int
    state: str
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    released_at: datetime | None

    def is_active(self, at: datetime) -> bool:
        checked = _utc(at, field="at")
        return self.state == "active" and self.renewed_at <= checked < self.expires_at


@dataclass(frozen=True, slots=True)
class NodeRuntimeRecord:
    node_id: str
    instance_id: str
    fencing_token: int
    generation: int
    revision: int
    process_state: str
    capability: str
    risk_gate: str
    started_at: datetime
    updated_at: datetime
    details: Mapping[str, Any]
    content_hash: str


@dataclass(frozen=True, slots=True)
class HeartbeatRecord:
    node_id: str
    component: str
    instance_id: str
    fencing_token: int
    status: str
    observed_at: datetime
    valid_until: datetime
    details: Mapping[str, Any]
    content_hash: str

    def is_fresh(self, at: datetime) -> bool:
        checked = _utc(at, field="at")
        return self.observed_at <= checked < self.valid_until


class ResearchStore:
    """File-backed SQLite store for non-authoritative research/node state."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValidationError("ResearchStore requires a file-backed database")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValidationError("busy_timeout_ms must be a positive integer")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
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
                CREATE TABLE IF NOT EXISTS research_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            self._verify_table_columns(connection, "research_schema_migrations")
            applied_rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM research_schema_migrations ORDER BY version
                """
            ).fetchall()
            known = {migration.version: migration for migration in _MIGRATIONS}
            seen_versions: list[int] = []
            for row in applied_rows:
                version = int(row["version"])
                migration = known.get(version)
                if migration is None:
                    raise StorageError(
                        f"database has unknown research migration version {version}"
                    )
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise StorageError(
                        f"research migration {version} checksum or name mismatch"
                    )
                seen_versions.append(version)
            if seen_versions != list(range(1, len(seen_versions) + 1)):
                raise StorageError("research migration history is not contiguous")

            applied = set(seen_versions)
            migration_time = _time_text(
                datetime.now(timezone.utc), field="migration time"
            )
            for migration in _MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO research_schema_migrations (
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
            self._verify_schema(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise StorageError(
                f"research schema initialization failed: {type(error).__name__}"
            ) from error
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _verify_table_columns(connection: sqlite3.Connection, table: str) -> None:
        actual = tuple(
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        expected = _EXPECTED_COLUMNS[table]
        if actual != expected:
            raise StorageError(f"research table {table} has an unexpected schema")

    def _verify_schema(self, connection: sqlite3.Connection) -> None:
        for table in _EXPECTED_COLUMNS:
            self._verify_table_columns(connection, table)
        index_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        if not _EXPECTED_INDEXES.issubset(index_names):
            raise StorageError("research schema is missing required indexes")

    # -- tracked assets -------------------------------------------------

    @staticmethod
    def _tracked_payload(asset: TrackedAsset) -> tuple[str, str]:
        payload = asset.as_dict()
        payload["created_at"] = _time_text(asset.created_at, field="created_at")
        payload["updated_at"] = _time_text(asset.updated_at, field="updated_at")
        return _canonical_payload(payload, maximum_bytes=_MAX_DETAILS_BYTES)

    @staticmethod
    def _tracked_from_row(row: Mapping[str, Any]) -> TrackedAsset:
        payload = _decode_canonical_payload(
            row["payload_json"],
            row["payload_hash"],
            field="tracked asset",
            maximum_bytes=_MAX_DETAILS_BYTES,
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted tracked-asset payload is not an object")
        try:
            asset = TrackedAsset(
                asset_id=row["asset_id"],
                venue=row["venue"],
                market_data_network=MarketDataNetwork(row["market_data_network"]),
                execution_environment=row["execution_environment"],
                symbol=row["symbol"],
                interval=row["interval"],
                poll_seconds=payload["poll_seconds"],
                technical_profile_version=payload["technical_profile_version"],
                sentiment_policy_version=payload["sentiment_policy_version"],
                sentiment_query=payload["sentiment_query"],
                sentiment_query_version=payload["sentiment_query_version"],
                status=TrackingStatus(row["status"]),
                revision=row["revision"],
                created_at=_parse_time(row["created_at"], field="tracked created_at"),
                updated_at=_parse_time(row["updated_at"], field="tracked updated_at"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError("persisted tracked-asset record is invalid") from error
        payload_json, payload_hash = ResearchStore._tracked_payload(asset)
        if asset.config_hash != _stored_sha256(row["config_hash"], field="config_hash"):
            raise StorageError("persisted tracked-asset config hash does not match")
        if payload_json != row["payload_json"]:
            raise StorageError("persisted tracked-asset payload does not match columns")
        if payload_hash != _stored_sha256(row["payload_hash"], field="payload_hash"):
            raise StorageError("persisted tracked-asset payload hash does not match")
        return asset

    @staticmethod
    def _immutable_tracking_identity(asset: TrackedAsset) -> tuple[object, ...]:
        return (
            asset.asset_id,
            asset.venue,
            asset.market_data_network,
            asset.execution_environment,
            asset.symbol,
            asset.interval,
            asset.technical_profile_version,
            asset.sentiment_policy_version,
            asset.created_at,
        )

    def upsert_tracked_asset(
        self,
        asset: TrackedAsset,
        *,
        expected_revision: int | None = None,
    ) -> TrackedAsset:
        """Create revision one or CAS-update an existing tracked asset.

        Repeating an identical write is idempotent.  Any material update
        requires ``expected_revision`` and the supplied object must be exactly
        the next revision.
        """

        if not isinstance(asset, TrackedAsset):
            raise TypeError("asset must be TrackedAsset")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValidationError("expected_revision must be a non-negative integer")
        try:
            with self._transaction() as connection:
                return self._upsert_tracked_asset_locked(
                    connection,
                    asset,
                    expected_revision=expected_revision,
                )
        except sqlite3.IntegrityError as error:
            raise StateConflict(
                "tracked asset ID or venue/network/symbol/interval already exists"
            ) from error

    def _upsert_tracked_asset_locked(
        self,
        connection: sqlite3.Connection,
        asset: TrackedAsset,
        *,
        expected_revision: int | None,
    ) -> TrackedAsset:
        payload_json, payload_hash = self._tracked_payload(asset)
        row = connection.execute(
            "SELECT * FROM research_tracked_assets WHERE asset_id = ?",
            (asset.asset_id,),
        ).fetchone()
        if row is None:
            if asset.revision != 1 or expected_revision not in (None, 0):
                raise StateConflict("new tracked assets must begin at revision 1")
            connection.execute(
                """
                INSERT INTO research_tracked_assets (
                    asset_id, venue, market_data_network, execution_environment,
                    symbol, interval, status, revision, config_hash,
                    payload_json, payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    asset.venue,
                    asset.market_data_network.value,
                    asset.execution_environment.value,
                    asset.symbol,
                    asset.interval,
                    asset.status.value,
                    asset.revision,
                    asset.config_hash,
                    payload_json,
                    payload_hash,
                    _time_text(asset.created_at, field="asset.created_at"),
                    _time_text(asset.updated_at, field="asset.updated_at"),
                ),
            )
            return asset

        current = self._tracked_from_row(row)
        if expected_revision is not None and expected_revision != current.revision:
            raise StateConflict("tracked asset compare-and-swap revision is stale")
        if payload_json == row["payload_json"] and asset.config_hash == current.config_hash:
            return current
        if expected_revision is None:
            raise StateConflict("tracked asset updates require expected_revision")
        if asset.revision != current.revision + 1:
            raise StateConflict("tracked asset update must supply the next revision")
        if self._immutable_tracking_identity(asset) != self._immutable_tracking_identity(
            current
        ):
            raise StateConflict("tracked asset immutable identity cannot change")
        if asset.updated_at <= current.updated_at:
            raise StateConflict("tracked asset updated_at must increase")
        changed = connection.execute(
            """
            UPDATE research_tracked_assets SET
                status = ?, revision = ?, config_hash = ?, payload_json = ?,
                payload_hash = ?, updated_at = ?
            WHERE asset_id = ? AND revision = ? AND config_hash = ?
            """,
            (
                asset.status.value,
                asset.revision,
                asset.config_hash,
                payload_json,
                payload_hash,
                _time_text(asset.updated_at, field="asset.updated_at"),
                asset.asset_id,
                current.revision,
                current.config_hash,
            ),
        )
        if changed.rowcount != 1:
            raise StateConflict("tracked asset changed concurrently")
        return asset

    def pause_tracked_asset(
        self,
        asset_id: str,
        *,
        expected_revision: int,
        at: datetime,
    ) -> TrackedAsset:
        checked_id = _text(asset_id, field="asset_id", maximum=128)
        if type(expected_revision) is not int or expected_revision <= 0:
            raise ValidationError("expected_revision must be a positive integer")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM research_tracked_assets WHERE asset_id = ?",
                (checked_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"tracked asset not found: {checked_id}")
            current = self._tracked_from_row(row)
            if current.revision != expected_revision:
                raise StateConflict("tracked asset compare-and-swap revision is stale")
            if current.status is TrackingStatus.PAUSED:
                return current
            revised = current.revise(
                status=TrackingStatus.PAUSED,
                updated_at=checked_at,
            )
            return self._upsert_tracked_asset_locked(
                connection,
                revised,
                expected_revision=current.revision,
            )

    def get_tracked_asset(self, asset_id: str) -> TrackedAsset:
        checked_id = _text(asset_id, field="asset_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM research_tracked_assets WHERE asset_id = ?",
                (checked_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"tracked asset not found: {checked_id}")
        return self._tracked_from_row(row)

    def list_tracked_assets(
        self,
        *,
        status: TrackingStatus | str | None = None,
    ) -> tuple[TrackedAsset, ...]:
        status_value: str | None = None
        if status is not None:
            try:
                status_value = TrackingStatus(status).value
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid tracking status") from error
        connection = self._connect()
        try:
            if status_value is None:
                rows = connection.execute(
                    "SELECT * FROM research_tracked_assets ORDER BY asset_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM research_tracked_assets
                    WHERE status = ? ORDER BY asset_id
                    """,
                    (status_value,),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._tracked_from_row(row) for row in rows)

    # -- immutable research artifacts ---------------------------------

    @staticmethod
    def _analysis_from_row(row: Mapping[str, Any]) -> AssetAnalysisRecord:
        payload_json = str(row["payload_json"])
        content_hash = _stored_sha256(
            row["content_hash"], field="asset analysis content_hash"
        )
        payload = _decode_canonical_payload(
            payload_json,
            content_hash,
            field="asset analysis",
            maximum_bytes=_MAX_ARTIFACT_BYTES,
        )
        if not isinstance(payload, dict):
            raise StorageError("persisted asset analysis is not an object")
        record = AssetAnalysisRecord(
            analysis_hash=_stored_sha256(
                row["analysis_hash"], field="analysis_hash"
            ),
            asset_id=_stored_text(row["asset_id"], field="asset_id", maximum=128),
            tracker_revision=int(row["tracker_revision"]),
            history_hash=_stored_sha256(
                row["history_hash"], field="history_hash"
            ),
            signal_hash=_stored_sha256(row["signal_hash"], field="signal_hash"),
            sentiment_hash=(
                None
                if row["sentiment_hash"] is None
                else _stored_sha256(row["sentiment_hash"], field="sentiment_hash")
            ),
            assessment_hash=(
                None
                if row["assessment_hash"] is None
                else _stored_sha256(
                    row["assessment_hash"], field="assessment_hash"
                )
            ),
            observed_at=_parse_time(
                row["observed_at"], field="analysis observed_at"
            ),
            expires_at=_parse_time(row["expires_at"], field="analysis expires_at"),
            stored_at=_parse_time(row["stored_at"], field="analysis stored_at"),
            payload_json=payload_json,
            content_hash=content_hash,
            record_hash=_stored_sha256(row["record_hash"], field="record_hash"),
        )
        if record.tracker_revision <= 0:
            raise StorageError("persisted analysis tracker revision is invalid")
        core = dict(payload)
        if core.pop("analysis_hash", None) != record.analysis_hash:
            raise StorageError("persisted analysis hash differs from payload")
        if domain_hash("trading-harness/asset-analysis/v1", core) != record.analysis_hash:
            raise StorageError("persisted asset analysis hash does not match")
        material = {
            "analysis_hash": record.analysis_hash,
            "asset_id": record.asset_id,
            "tracker_revision": record.tracker_revision,
            "history_hash": record.history_hash,
            "signal_hash": record.signal_hash,
            "sentiment_hash": record.sentiment_hash,
            "assessment_hash": record.assessment_hash,
            "observed_at": _time_text(record.observed_at, field="observed_at"),
            "expires_at": _time_text(record.expires_at, field="expires_at"),
            "stored_at": _time_text(record.stored_at, field="stored_at"),
            "content_hash": record.content_hash,
            "payload_json": record.payload_json,
        }
        if record.record_hash != domain_hash(
            "trading-harness/asset-analysis-record/v1", material
        ):
            raise StorageError("persisted asset analysis record hash does not match")
        return record

    def put_asset_analysis(
        self,
        asset_id: str,
        analysis: Mapping[str, Any],
        *,
        stored_at: datetime,
    ) -> AssetAnalysisRecord:
        checked_asset = _text(asset_id, field="asset_id", maximum=128)
        if not isinstance(analysis, Mapping):
            raise TypeError("analysis must be a mapping")
        document = dict(analysis)
        analysis_hash = _sha256(
            document.get("analysis_hash"), field="analysis_hash"
        )
        core = dict(document)
        core.pop("analysis_hash")
        if domain_hash("trading-harness/asset-analysis/v1", core) != analysis_hash:
            raise ValidationError("analysis_hash does not match analysis contents")
        asset = core.get("asset")
        history = core.get("history")
        signal = core.get("registered_signal")
        sentiment = core.get("sentiment")
        assessment = core.get("assessment")
        if not all(isinstance(value, Mapping) for value in (asset, history, signal, sentiment, assessment)):
            raise ValidationError("asset analysis dependencies are incomplete")
        if asset.get("asset_id") != checked_asset:
            raise ValidationError("asset analysis targets another tracked asset")
        tracker_revision = _positive_int(
            asset.get("revision"), field="tracker_revision"
        )
        history_hash = _sha256(history.get("data_hash"), field="history_hash")
        signal_hash = _sha256(signal.get("signal_hash"), field="signal_hash")
        snapshot = sentiment.get("snapshot")
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise ValidationError("analysis sentiment snapshot is invalid")
        sentiment_hash = (
            None
            if snapshot is None
            else _sha256(snapshot.get("artifact_hash"), field="sentiment_hash")
        )
        assessment_hash = (
            None
            if assessment.get("artifact_hash") is None
            else _sha256(assessment.get("artifact_hash"), field="assessment_hash")
        )
        observed_at = _parse_time(core.get("observed_at"), field="observed_at")
        expires_at = _parse_time(signal.get("expires_at"), field="signal expires_at")
        checked_stored = _utc(stored_at, field="stored_at")
        if not observed_at <= checked_stored or expires_at < observed_at:
            raise ValidationError("asset analysis storage/expiry time is invalid")
        payload_json, content_hash = _canonical_payload(
            document, maximum_bytes=_MAX_ARTIFACT_BYTES
        )
        material = {
            "analysis_hash": analysis_hash,
            "asset_id": checked_asset,
            "tracker_revision": tracker_revision,
            "history_hash": history_hash,
            "signal_hash": signal_hash,
            "sentiment_hash": sentiment_hash,
            "assessment_hash": assessment_hash,
            "observed_at": _time_text(observed_at, field="observed_at"),
            "expires_at": _time_text(expires_at, field="expires_at"),
            "stored_at": _time_text(checked_stored, field="stored_at"),
            "content_hash": content_hash,
            "payload_json": payload_json,
        }
        record_hash = domain_hash(
            "trading-harness/asset-analysis-record/v1", material
        )
        with self._transaction() as connection:
            tracked = connection.execute(
                "SELECT revision FROM research_tracked_assets WHERE asset_id = ?",
                (checked_asset,),
            ).fetchone()
            if tracked is None:
                raise RecordNotFound("tracked asset is not registered")
            if int(tracked["revision"]) != tracker_revision:
                raise StateConflict("asset analysis tracker revision is stale")
            existing = connection.execute(
                "SELECT * FROM research_asset_analyses WHERE analysis_hash = ?",
                (analysis_hash,),
            ).fetchone()
            if existing is not None:
                current = self._analysis_from_row(existing)
                if current.payload_json == payload_json:
                    return current
                raise StateConflict("analysis hash is already bound differently")
            connection.execute(
                """
                INSERT INTO research_asset_analyses (
                    analysis_hash, asset_id, tracker_revision, history_hash,
                    signal_hash, sentiment_hash, assessment_hash, observed_at,
                    expires_at, stored_at, payload_json, content_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_hash,
                    checked_asset,
                    tracker_revision,
                    history_hash,
                    signal_hash,
                    sentiment_hash,
                    assessment_hash,
                    _time_text(observed_at, field="observed_at"),
                    _time_text(expires_at, field="expires_at"),
                    _time_text(checked_stored, field="stored_at"),
                    payload_json,
                    content_hash,
                    record_hash,
                ),
            )
            return self._analysis_from_row(
                connection.execute(
                    "SELECT * FROM research_asset_analyses WHERE analysis_hash = ?",
                    (analysis_hash,),
                ).fetchone()
            )

    def get_asset_analysis(self, analysis_hash: str) -> AssetAnalysisRecord:
        checked = _sha256(analysis_hash, field="analysis_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM research_asset_analyses WHERE analysis_hash = ?",
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("asset analysis is not registered")
        return self._analysis_from_row(row)

    def latest_asset_analysis(self, asset_id: str) -> AssetAnalysisRecord | None:
        checked = _text(asset_id, field="asset_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM research_asset_analyses
                WHERE asset_id = ?
                ORDER BY observed_at DESC, analysis_hash DESC LIMIT 1
                """,
                (checked,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._analysis_from_row(row)

    @staticmethod
    def _artifact_record_hash(
        *,
        artifact_kind: str,
        artifact_id: str,
        asset_id: str,
        series_key: str,
        semantic_hash: str,
        content_hash: str,
        artifact_time: datetime,
        stored_at: datetime,
        payload_json: str,
    ) -> str:
        return domain_hash(
            "trading-harness/research-artifact-record/v1",
            {
                "artifact_kind": artifact_kind,
                "artifact_id": artifact_id,
                "asset_id": asset_id,
                "series_key": series_key,
                "semantic_hash": semantic_hash,
                "content_hash": content_hash,
                "artifact_time": artifact_time,
                "stored_at": stored_at,
                "payload_json": payload_json,
            },
        )

    @staticmethod
    def _artifact_from_row(row: Mapping[str, Any]) -> ResearchArtifactRecord:
        kind = _stored_text(row["artifact_kind"], field="artifact_kind", maximum=32)
        if kind not in _ARTIFACT_KINDS:
            raise StorageError("persisted artifact kind is unsupported")
        payload_json = row["payload_json"]
        _decode_canonical_payload(
            payload_json,
            row["content_hash"],
            field="artifact",
            maximum_bytes=_MAX_ARTIFACT_BYTES,
        )
        record = ResearchArtifactRecord(
            artifact_kind=kind,
            artifact_id=_stored_text(
                row["artifact_id"], field="artifact_id", maximum=512
            ),
            asset_id=_stored_text(row["asset_id"], field="asset_id", maximum=128),
            series_key=_stored_text(
                row["series_key"], field="series_key", maximum=128
            ),
            semantic_hash=_stored_sha256(
                row["semantic_hash"], field="semantic_hash"
            ),
            content_hash=_stored_sha256(row["content_hash"], field="content_hash"),
            record_hash=_stored_sha256(row["record_hash"], field="record_hash"),
            artifact_time=_parse_time(row["artifact_time"], field="artifact_time"),
            stored_at=_parse_time(row["stored_at"], field="stored_at"),
            payload_json=str(payload_json),
        )
        expected_record_hash = ResearchStore._artifact_record_hash(
            artifact_kind=record.artifact_kind,
            artifact_id=record.artifact_id,
            asset_id=record.asset_id,
            series_key=record.series_key,
            semantic_hash=record.semantic_hash,
            content_hash=record.content_hash,
            artifact_time=record.artifact_time,
            stored_at=record.stored_at,
            payload_json=record.payload_json,
        )
        if record.record_hash != expected_record_hash:
            raise StorageError("persisted research artifact record hash does not match")
        return record

    def _require_asset_locked(
        self,
        connection: sqlite3.Connection,
        asset_id: str,
    ) -> TrackedAsset:
        row = connection.execute(
            "SELECT * FROM research_tracked_assets WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"tracked asset not found: {asset_id}")
        return self._tracked_from_row(row)

    @staticmethod
    def _require_artifact_reference_locked(
        connection: sqlite3.Connection,
        *,
        artifact_kind: str,
        asset_id: str,
        semantic_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM research_artifacts
            WHERE artifact_kind = ? AND asset_id = ? AND semantic_hash = ?
            """,
            (artifact_kind, asset_id, semantic_hash),
        ).fetchone()
        if row is None:
            raise StateConflict(
                f"{artifact_kind} artifact dependency is missing for asset"
            )

    def _put_artifact(
        self,
        *,
        artifact_kind: str,
        artifact_id: str,
        asset_id: str,
        series_key: str,
        semantic_hash: str,
        artifact_time: datetime,
        stored_at: datetime,
        payload: object,
        dependencies: Sequence[tuple[str, str]] = (),
        asset_check: Callable[[TrackedAsset], None] | None = None,
    ) -> ResearchArtifactRecord:
        if artifact_kind not in _ARTIFACT_KINDS:
            raise ValidationError("unsupported artifact kind")
        checked_id = _text(artifact_id, field="artifact_id", maximum=512)
        checked_asset = _text(asset_id, field="asset_id", maximum=128)
        checked_series = _text(series_key, field="series_key", maximum=128)
        checked_semantic = _sha256(semantic_hash, field="semantic_hash")
        checked_time = _utc(artifact_time, field="artifact_time")
        checked_stored = _utc(stored_at, field="stored_at")
        if checked_stored < checked_time:
            raise ValidationError("stored_at cannot predate artifact_time")
        payload_json, content_hash = _canonical_payload(
            payload, maximum_bytes=_MAX_ARTIFACT_BYTES
        )
        record_hash = self._artifact_record_hash(
            artifact_kind=artifact_kind,
            artifact_id=checked_id,
            asset_id=checked_asset,
            series_key=checked_series,
            semantic_hash=checked_semantic,
            content_hash=content_hash,
            artifact_time=checked_time,
            stored_at=checked_stored,
            payload_json=payload_json,
        )
        try:
            with self._transaction() as connection:
                tracked = self._require_asset_locked(connection, checked_asset)
                if asset_check is not None:
                    asset_check(tracked)
                for dependency_kind, dependency_hash in dependencies:
                    self._require_artifact_reference_locked(
                        connection,
                        artifact_kind=dependency_kind,
                        asset_id=checked_asset,
                        semantic_hash=_sha256(
                            dependency_hash, field="dependency semantic_hash"
                        ),
                    )
                row = connection.execute(
                    """
                    SELECT * FROM research_artifacts
                    WHERE artifact_kind = ? AND artifact_id = ?
                    """,
                    (artifact_kind, checked_id),
                ).fetchone()
                if row is not None:
                    existing = self._artifact_from_row(row)
                    if (
                        existing.asset_id == checked_asset
                        and existing.series_key == checked_series
                        and existing.semantic_hash == checked_semantic
                        and existing.content_hash == content_hash
                        and existing.artifact_time == checked_time
                        and existing.payload_json == payload_json
                    ):
                        return existing
                    raise StateConflict(
                        "immutable research artifact ID already has different content"
                    )
                semantic_row = connection.execute(
                    """
                    SELECT * FROM research_artifacts
                    WHERE artifact_kind = ? AND semantic_hash = ?
                    """,
                    (artifact_kind, checked_semantic),
                ).fetchone()
                if semantic_row is not None:
                    existing = self._artifact_from_row(semantic_row)
                    if (
                        existing.artifact_id == checked_id
                        and existing.content_hash == content_hash
                    ):
                        return existing
                    raise StateConflict(
                        "research artifact semantic hash is already bound"
                    )
                connection.execute(
                    """
                    INSERT INTO research_artifacts (
                        artifact_kind, artifact_id, asset_id, series_key,
                        semantic_hash, content_hash, artifact_time, stored_at,
                        record_hash, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_kind,
                        checked_id,
                        checked_asset,
                        checked_series,
                        checked_semantic,
                        content_hash,
                        _time_text(checked_time, field="artifact_time"),
                        _time_text(checked_stored, field="stored_at"),
                        record_hash,
                        payload_json,
                    ),
                )
                inserted = connection.execute(
                    """
                    SELECT * FROM research_artifacts
                    WHERE artifact_kind = ? AND artifact_id = ?
                    """,
                    (artifact_kind, checked_id),
                ).fetchone()
                if inserted is None:
                    raise StorageError("research artifact disappeared before commit")
                return self._artifact_from_row(inserted)
        except sqlite3.IntegrityError as error:
            raise StateConflict("research artifact conflicts with durable state") from error

    def put_candle(
        self,
        asset_id: str,
        candle: Candle | HistoricalCandle,
        *,
        stored_at: datetime,
    ) -> ResearchArtifactRecord:
        if not isinstance(candle, (Candle, HistoricalCandle)):
            raise TypeError("candle must be Candle or HistoricalCandle")
        checked_asset = _text(asset_id, field="asset_id", maximum=128)
        if isinstance(candle, HistoricalCandle):
            payload = {
                "schema_version": "hyperliquid.historical_candle.v1",
                **candle.canonical_record(),
            }
            open_identity: datetime | int = candle.open_time_ms
            artifact_time = _EPOCH + timedelta(milliseconds=candle.close_time_ms)
        else:
            payload = {"schema_version": "candle.v1", **candle.canonical_record()}
            open_identity = candle.open_time
            artifact_time = candle.close_time
        artifact_id = domain_hash(
            "trading-harness/research-candle-key/v1",
            {
                "asset_id": checked_asset,
                "symbol": candle.symbol,
                "interval": candle.interval,
                "open_time": open_identity,
            },
        )
        semantic_hash = domain_hash(
            "trading-harness/research-candle/v1", payload
        )

        def check(tracked: TrackedAsset) -> None:
            if tracked.symbol != candle.symbol or tracked.interval != candle.interval:
                raise StateConflict("candle does not match the tracked asset series")

        return self._put_artifact(
            artifact_kind="candle",
            artifact_id=artifact_id,
            asset_id=checked_asset,
            series_key=candle.interval,
            semantic_hash=semantic_hash,
            artifact_time=artifact_time,
            stored_at=stored_at,
            payload=payload,
            asset_check=check,
        )

    def put_technical(
        self,
        asset_id: str,
        snapshot: TechnicalSnapshot,
        *,
        stored_at: datetime,
    ) -> ResearchArtifactRecord:
        if not isinstance(snapshot, TechnicalSnapshot):
            raise TypeError("snapshot must be TechnicalSnapshot")
        if snapshot.executable:
            raise ValidationError("technical research artifacts cannot be executable")
        checked_asset = _text(asset_id, field="asset_id", maximum=128)
        payload = snapshot.as_dict()
        payload_json, semantic_hash = _canonical_payload(
            payload, maximum_bytes=_MAX_ARTIFACT_BYTES
        )
        del payload_json
        artifact_id = domain_hash(
            "trading-harness/research-technical-key/v1",
            {
                "asset_id": checked_asset,
                "interval": snapshot.interval,
                "config_version": snapshot.config_version,
                "data_hash": snapshot.data_hash,
                "candle_close_time": snapshot.candle_close_time,
            },
        )

        def check(tracked: TrackedAsset) -> None:
            if tracked.symbol != snapshot.symbol or tracked.interval != snapshot.interval:
                raise StateConflict("technical snapshot does not match tracked asset")

        return self._put_artifact(
            artifact_kind="technical",
            artifact_id=artifact_id,
            asset_id=checked_asset,
            series_key=snapshot.config_version,
            semantic_hash=semantic_hash,
            artifact_time=snapshot.as_of,
            stored_at=stored_at,
            payload=payload,
            asset_check=check,
        )

    def put_sentiment(
        self,
        snapshot: SentimentSnapshot,
        *,
        stored_at: datetime,
    ) -> ResearchArtifactRecord:
        if not isinstance(snapshot, SentimentSnapshot):
            raise TypeError("snapshot must be SentimentSnapshot")
        semantic_hash = _sha256(snapshot.artifact_hash, field="sentiment artifact_hash")
        return self._put_artifact(
            artifact_kind="sentiment",
            artifact_id=semantic_hash,
            asset_id=snapshot.asset_id,
            series_key=snapshot.query_version,
            semantic_hash=semantic_hash,
            artifact_time=snapshot.collected_at,
            stored_at=stored_at,
            payload=snapshot.as_dict(),
        )

    def put_assessment(
        self,
        assessment: OpportunityAssessment,
        *,
        stored_at: datetime,
    ) -> ResearchArtifactRecord:
        if not isinstance(assessment, OpportunityAssessment):
            raise TypeError("assessment must be OpportunityAssessment")
        if assessment.eligible_to_trade:
            raise ValidationError("research assessments cannot be trade-authoritative")
        semantic_hash = _sha256(
            assessment.artifact_hash, field="assessment artifact_hash"
        )
        return self._put_artifact(
            artifact_kind="assessment",
            artifact_id=assessment.assessment_id,
            asset_id=assessment.asset_id,
            series_key=assessment.policy_version,
            semantic_hash=semantic_hash,
            artifact_time=assessment.created_at,
            stored_at=stored_at,
            payload=assessment.as_dict(),
            dependencies=(
                ("technical", assessment.technical_hash),
                ("sentiment", assessment.sentiment_hash),
            ),
        )

    def put_risk_ticket(
        self,
        asset_id: str,
        ticket: RiskTicket,
        *,
        stored_at: datetime,
    ) -> ResearchArtifactRecord:
        if not isinstance(ticket, RiskTicket):
            raise TypeError("ticket must be RiskTicket")
        semantic_hash = _sha256(ticket.ticket_hash, field="ticket_hash")
        return self._put_artifact(
            artifact_kind="risk_ticket",
            artifact_id=ticket.ticket_id,
            asset_id=asset_id,
            series_key=ticket.policy_version,
            semantic_hash=semantic_hash,
            artifact_time=ticket.created_at,
            stored_at=stored_at,
            payload=ticket.as_dict(),
            dependencies=(("assessment", ticket.assessment_hash),),
        )

    def get_artifact(
        self,
        artifact_kind: str,
        artifact_id: str,
    ) -> ResearchArtifactRecord:
        if artifact_kind not in _ARTIFACT_KINDS:
            raise ValidationError("unsupported artifact kind")
        checked_id = _text(artifact_id, field="artifact_id", maximum=512)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM research_artifacts
                WHERE artifact_kind = ? AND artifact_id = ?
                """,
                (artifact_kind, checked_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"research artifact not found: {artifact_kind}")
        return self._artifact_from_row(row)

    def get_artifact_by_hash(
        self,
        artifact_kind: str,
        semantic_hash: str,
    ) -> ResearchArtifactRecord:
        if artifact_kind not in _ARTIFACT_KINDS:
            raise ValidationError("unsupported artifact kind")
        checked_hash = _sha256(semantic_hash, field="semantic_hash")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM research_artifacts
                WHERE artifact_kind = ? AND semantic_hash = ?
                """,
                (artifact_kind, checked_hash),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"research artifact not found: {artifact_kind}")
        return self._artifact_from_row(row)

    def list_artifacts(
        self,
        asset_id: str,
        artifact_kind: str,
        *,
        series_key: str | None = None,
        after: datetime | None = None,
        through: datetime | None = None,
        limit: int = 1_000,
        ascending: bool = True,
    ) -> tuple[ResearchArtifactRecord, ...]:
        checked_asset = _text(asset_id, field="asset_id", maximum=128)
        if artifact_kind not in _ARTIFACT_KINDS:
            raise ValidationError("unsupported artifact kind")
        checked_limit = _positive_int(limit, field="limit", maximum=10_000)
        if type(ascending) is not bool:
            raise TypeError("ascending must be bool")
        clauses = ["asset_id = ?", "artifact_kind = ?"]
        parameters: list[object] = [checked_asset, artifact_kind]
        if series_key is not None:
            clauses.append("series_key = ?")
            parameters.append(_text(series_key, field="series_key", maximum=128))
        if after is not None:
            clauses.append("artifact_time > ?")
            parameters.append(_time_text(after, field="after"))
        if through is not None:
            clauses.append("artifact_time <= ?")
            parameters.append(_time_text(through, field="through"))
        parameters.append(checked_limit)
        direction = "ASC" if ascending else "DESC"
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM research_artifacts
                WHERE {' AND '.join(clauses)}
                ORDER BY artifact_time {direction}, artifact_id {direction}
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._artifact_from_row(row) for row in rows)

    # -- fenced node lease ---------------------------------------------

    @staticmethod
    def _lease_from_row(row: Mapping[str, Any]) -> NodeLeaseRecord:
        state = _stored_text(row["state"], field="lease state", maximum=16)
        if state not in {"active", "released"}:
            raise StorageError("persisted lease state is unsupported")
        token = int(row["fencing_token"])
        if token <= 0:
            raise StorageError("persisted fencing token is invalid")
        released = (
            None
            if row["released_at"] is None
            else _parse_time(row["released_at"], field="lease released_at")
        )
        record = NodeLeaseRecord(
            node_id=_stored_text(row["node_id"], field="node_id", maximum=128),
            instance_id=_stored_text(
                row["instance_id"], field="instance_id", maximum=128
            ),
            fencing_token=token,
            state=state,
            acquired_at=_parse_time(row["acquired_at"], field="lease acquired_at"),
            renewed_at=_parse_time(row["renewed_at"], field="lease renewed_at"),
            expires_at=_parse_time(row["expires_at"], field="lease expires_at"),
            released_at=released,
        )
        if not record.acquired_at <= record.renewed_at <= record.expires_at:
            raise StorageError("persisted lease timestamps are inconsistent")
        if (state == "released") != (released is not None):
            raise StorageError("persisted lease release state is inconsistent")
        return record

    @staticmethod
    def _lease_ttl(ttl_seconds: int) -> int:
        return _positive_int(ttl_seconds, field="ttl_seconds", maximum=86_400)

    def acquire_node_lease(
        self,
        node_id: str,
        instance_id: str,
        *,
        at: datetime,
        ttl_seconds: int,
    ) -> NodeLeaseRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        checked_at = _utc(at, field="at")
        ttl = self._lease_ttl(ttl_seconds)
        expires = checked_at + timedelta(seconds=ttl)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM research_node_leases WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO research_node_leases (
                        node_id, instance_id, fencing_token, state, acquired_at,
                        renewed_at, expires_at, released_at
                    ) VALUES (?, ?, 1, 'active', ?, ?, ?, NULL)
                    """,
                    (
                        checked_node,
                        checked_instance,
                        _time_text(checked_at, field="at"),
                        _time_text(checked_at, field="at"),
                        _time_text(expires, field="expires_at"),
                    ),
                )
            else:
                current = self._lease_from_row(row)
                if checked_at < current.renewed_at:
                    raise StateConflict("node lease clock moved backwards")
                if current.is_active(checked_at):
                    if current.instance_id == checked_instance:
                        return current
                    raise StateConflict("node lease is held by another instance")
                connection.execute(
                    """
                    UPDATE research_node_leases SET
                        instance_id = ?, fencing_token = ?, state = 'active',
                        acquired_at = ?, renewed_at = ?, expires_at = ?,
                        released_at = NULL
                    WHERE node_id = ? AND fencing_token = ?
                    """,
                    (
                        checked_instance,
                        current.fencing_token + 1,
                        _time_text(checked_at, field="at"),
                        _time_text(checked_at, field="at"),
                        _time_text(expires, field="expires_at"),
                        checked_node,
                        current.fencing_token,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM research_node_leases WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if updated is None:
                raise StorageError("node lease disappeared before commit")
            return self._lease_from_row(updated)

    def renew_node_lease(
        self,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        *,
        at: datetime,
        ttl_seconds: int,
    ) -> NodeLeaseRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        ttl = self._lease_ttl(ttl_seconds)
        expires = checked_at + timedelta(seconds=ttl)
        with self._transaction() as connection:
            current = self._require_lease_locked(
                connection,
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                at=checked_at,
            )
            if checked_at < current.renewed_at:
                raise StateConflict("node lease clock moved backwards")
            if checked_at == current.renewed_at:
                if expires == current.expires_at:
                    return current
                raise StateConflict("same-time lease renewal cannot change expiry")
            changed = connection.execute(
                """
                UPDATE research_node_leases SET renewed_at = ?, expires_at = ?
                WHERE node_id = ? AND instance_id = ? AND fencing_token = ?
                    AND state = 'active'
                """,
                (
                    _time_text(checked_at, field="at"),
                    _time_text(expires, field="expires_at"),
                    checked_node,
                    checked_instance,
                    token,
                ),
            )
            if changed.rowcount != 1:
                raise StateConflict("node lease changed concurrently")
            row = connection.execute(
                "SELECT * FROM research_node_leases WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if row is None:
                raise StorageError("node lease disappeared before commit")
            return self._lease_from_row(row)

    def release_node_lease(
        self,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        *,
        at: datetime,
    ) -> NodeLeaseRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_at = _utc(at, field="at")
        with self._transaction() as connection:
            self._require_lease_locked(
                connection,
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                at=checked_at,
            )
            runtime_row = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if runtime_row is not None:
                runtime = self._runtime_from_row(runtime_row)
                if (
                    runtime.instance_id == checked_instance
                    and runtime.fencing_token == token
                    and (
                        runtime.process_state != "stopped"
                        or runtime.risk_gate != "halted"
                    )
                ):
                    raise StateConflict(
                        "active runtime must stop and halt risk before lease release"
                    )
            changed = connection.execute(
                """
                UPDATE research_node_leases SET
                    state = 'released', renewed_at = ?, expires_at = ?,
                    released_at = ?
                WHERE node_id = ? AND instance_id = ? AND fencing_token = ?
                    AND state = 'active'
                """,
                (
                    _time_text(checked_at, field="at"),
                    _time_text(checked_at, field="at"),
                    _time_text(checked_at, field="at"),
                    checked_node,
                    checked_instance,
                    token,
                ),
            )
            if changed.rowcount != 1:
                raise StateConflict("node lease changed concurrently")
            row = connection.execute(
                "SELECT * FROM research_node_leases WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if row is None:
                raise StorageError("node lease disappeared before commit")
            return self._lease_from_row(row)

    def _require_lease_locked(
        self,
        connection: sqlite3.Connection,
        *,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        at: datetime,
    ) -> NodeLeaseRecord:
        row = connection.execute(
            "SELECT * FROM research_node_leases WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise StateConflict("node lease is missing")
        current = self._lease_from_row(row)
        if (
            current.instance_id != instance_id
            or current.fencing_token != fencing_token
            or not current.is_active(at)
        ):
            raise StateConflict("node lease is stale or owned by another instance")
        return current

    def get_node_lease(self, node_id: str) -> NodeLeaseRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM research_node_leases WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"node lease not found: {checked_node}")
        return self._lease_from_row(row)

    # -- runtime and heartbeats ----------------------------------------

    @staticmethod
    def _details(value: Mapping[str, Any] | None) -> tuple[str, str, Mapping[str, Any]]:
        supplied: Mapping[str, Any] = {} if value is None else value
        if not isinstance(supplied, Mapping):
            raise TypeError("details must be a mapping or None")
        payload_json, payload_hash = _canonical_payload(
            dict(supplied), maximum_bytes=_MAX_DETAILS_BYTES
        )
        decoded = json.loads(payload_json)
        if not isinstance(decoded, dict):
            raise ValidationError("details must encode a JSON object")
        return payload_json, payload_hash, decoded

    @staticmethod
    def _runtime_payload(
        *,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        generation: int,
        revision: int,
        process_state: str,
        capability: str,
        risk_gate: str,
        started_at: datetime,
        updated_at: datetime,
        details: Mapping[str, Any],
    ) -> tuple[str, str]:
        return _canonical_payload(
            {
                "node_id": node_id,
                "instance_id": instance_id,
                "fencing_token": fencing_token,
                "generation": generation,
                "revision": revision,
                "process_state": process_state,
                "capability": capability,
                "risk_gate": risk_gate,
                "started_at": _time_text(started_at, field="started_at"),
                "updated_at": _time_text(updated_at, field="updated_at"),
                "details": dict(details),
            },
            maximum_bytes=_MAX_DETAILS_BYTES,
        )

    @classmethod
    def _runtime_from_row(cls, row: Mapping[str, Any]) -> NodeRuntimeRecord:
        details = _decode_canonical_payload(
            row["details_json"],
            hashlib.sha256(str(row["details_json"]).encode("utf-8")).hexdigest(),
            field="runtime details",
            maximum_bytes=_MAX_DETAILS_BYTES,
        )
        if not isinstance(details, dict):
            raise StorageError("persisted runtime details are not an object")
        process_state = _stored_text(
            row["process_state"], field="process_state", maximum=16
        )
        capability = _stored_text(row["capability"], field="capability", maximum=16)
        risk_gate = _stored_text(row["risk_gate"], field="risk_gate", maximum=16)
        if process_state not in _PROCESS_STATES or capability not in _CAPABILITIES:
            raise StorageError("persisted runtime state is unsupported")
        if risk_gate not in _RISK_GATES:
            raise StorageError("persisted runtime risk gate is unsupported")
        record = NodeRuntimeRecord(
            node_id=_stored_text(row["node_id"], field="node_id", maximum=128),
            instance_id=_stored_text(
                row["instance_id"], field="instance_id", maximum=128
            ),
            fencing_token=int(row["fencing_token"]),
            generation=int(row["generation"]),
            revision=int(row["revision"]),
            process_state=process_state,
            capability=capability,
            risk_gate=risk_gate,
            started_at=_parse_time(row["started_at"], field="runtime started_at"),
            updated_at=_parse_time(row["updated_at"], field="runtime updated_at"),
            details=details,
            content_hash=_stored_sha256(row["content_hash"], field="content_hash"),
        )
        if min(record.fencing_token, record.generation, record.revision) <= 0:
            raise StorageError("persisted runtime counters are invalid")
        if record.updated_at < record.started_at:
            raise StorageError("persisted runtime timestamps are inconsistent")
        _, expected_hash = cls._runtime_payload(
            node_id=record.node_id,
            instance_id=record.instance_id,
            fencing_token=record.fencing_token,
            generation=record.generation,
            revision=record.revision,
            process_state=record.process_state,
            capability=record.capability,
            risk_gate=record.risk_gate,
            started_at=record.started_at,
            updated_at=record.updated_at,
            details=record.details,
        )
        if record.content_hash != expected_hash:
            raise StorageError("persisted runtime content hash does not match")
        return record

    def start_node_runtime(
        self,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        *,
        capability: str,
        at: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> NodeRuntimeRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_capability = _text(capability, field="capability", maximum=16)
        if checked_capability not in _CAPABILITIES:
            raise ValidationError("unsupported node capability")
        checked_at = _utc(at, field="at")
        details_json, _, checked_details = self._details(details)
        with self._transaction() as connection:
            self._require_lease_locked(
                connection,
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                at=checked_at,
            )
            row = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if row is None:
                generation = 1
                revision = 1
            else:
                current = self._runtime_from_row(row)
                if token <= current.fencing_token:
                    raise StateConflict("node runtime already started for this lease")
                generation = current.generation + 1
                revision = current.revision + 1
            _, content_hash = self._runtime_payload(
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                generation=generation,
                revision=revision,
                process_state="starting",
                capability=checked_capability,
                risk_gate="halted",
                started_at=checked_at,
                updated_at=checked_at,
                details=checked_details,
            )
            connection.execute(
                """
                INSERT INTO research_node_runtime (
                    node_id, instance_id, fencing_token, generation, revision,
                    process_state, capability, risk_gate, started_at, updated_at,
                    details_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, 'halted', ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    fencing_token = excluded.fencing_token,
                    generation = excluded.generation,
                    revision = excluded.revision,
                    process_state = excluded.process_state,
                    capability = excluded.capability,
                    risk_gate = excluded.risk_gate,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    details_json = excluded.details_json,
                    content_hash = excluded.content_hash
                """,
                (
                    checked_node,
                    checked_instance,
                    token,
                    generation,
                    revision,
                    checked_capability,
                    _time_text(checked_at, field="started_at"),
                    _time_text(checked_at, field="updated_at"),
                    details_json,
                    content_hash,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if updated is None:
                raise StorageError("node runtime disappeared before commit")
            return self._runtime_from_row(updated)

    @staticmethod
    def _validate_runtime_state(process_state: str, risk_gate: str) -> None:
        if process_state not in _PROCESS_STATES:
            raise ValidationError("unsupported process_state")
        if risk_gate not in _RISK_GATES:
            raise ValidationError("unsupported risk_gate")
        if process_state in {"degraded", "stopping", "stopped"} and risk_gate != "halted":
            raise ValidationError(f"{process_state} runtime must halt new risk")
        if risk_gate == "ready" and process_state != "running":
            raise ValidationError("risk can be ready only while runtime is running")

    def update_node_runtime(
        self,
        node_id: str,
        instance_id: str,
        fencing_token: int,
        *,
        expected_revision: int,
        process_state: str,
        risk_gate: str,
        at: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> NodeRuntimeRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        expected = _positive_int(expected_revision, field="expected_revision")
        checked_state = _text(process_state, field="process_state", maximum=16)
        checked_gate = _text(risk_gate, field="risk_gate", maximum=16)
        self._validate_runtime_state(checked_state, checked_gate)
        checked_at = _utc(at, field="at")
        details_json, _, checked_details = self._details(details)
        transitions = {
            "starting": {"starting", "running", "degraded", "stopping"},
            "running": {"running", "degraded", "stopping"},
            "degraded": {"degraded", "running", "stopping"},
            "stopping": {"stopping", "stopped"},
            "stopped": set(),
        }
        with self._transaction() as connection:
            self._require_lease_locked(
                connection,
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                at=checked_at,
            )
            row = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"node runtime not found: {checked_node}")
            current = self._runtime_from_row(row)
            if (
                current.instance_id != checked_instance
                or current.fencing_token != token
            ):
                raise StateConflict("node runtime belongs to a stale instance")
            if current.revision != expected:
                raise StateConflict("node runtime compare-and-swap revision is stale")
            if checked_state not in transitions[current.process_state]:
                raise StateConflict(
                    f"illegal runtime transition: {current.process_state} -> {checked_state}"
                )
            if current.capability == "research_only" and checked_gate != "halted":
                raise ValidationError("research-only runtime must halt new risk")
            if checked_at < current.updated_at:
                raise StateConflict("node runtime clock moved backwards")
            revision = current.revision + 1
            _, content_hash = self._runtime_payload(
                node_id=current.node_id,
                instance_id=current.instance_id,
                fencing_token=current.fencing_token,
                generation=current.generation,
                revision=revision,
                process_state=checked_state,
                capability=current.capability,
                risk_gate=checked_gate,
                started_at=current.started_at,
                updated_at=checked_at,
                details=checked_details,
            )
            changed = connection.execute(
                """
                UPDATE research_node_runtime SET
                    revision = ?, process_state = ?, risk_gate = ?, updated_at = ?,
                    details_json = ?, content_hash = ?
                WHERE node_id = ? AND instance_id = ? AND fencing_token = ?
                    AND revision = ?
                """,
                (
                    revision,
                    checked_state,
                    checked_gate,
                    _time_text(checked_at, field="updated_at"),
                    details_json,
                    content_hash,
                    checked_node,
                    checked_instance,
                    token,
                    expected,
                ),
            )
            if changed.rowcount != 1:
                raise StateConflict("node runtime changed concurrently")
            updated = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if updated is None:
                raise StorageError("node runtime disappeared before commit")
            return self._runtime_from_row(updated)

    def get_node_runtime(self, node_id: str) -> NodeRuntimeRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"node runtime not found: {checked_node}")
        return self._runtime_from_row(row)

    @staticmethod
    def _heartbeat_payload(
        *,
        node_id: str,
        component: str,
        instance_id: str,
        fencing_token: int,
        status: str,
        observed_at: datetime,
        valid_until: datetime,
        details: Mapping[str, Any],
    ) -> tuple[str, str]:
        return _canonical_payload(
            {
                "node_id": node_id,
                "component": component,
                "instance_id": instance_id,
                "fencing_token": fencing_token,
                "status": status,
                "observed_at": _time_text(observed_at, field="observed_at"),
                "valid_until": _time_text(valid_until, field="valid_until"),
                "details": dict(details),
            },
            maximum_bytes=_MAX_DETAILS_BYTES,
        )

    @classmethod
    def _heartbeat_from_row(cls, row: Mapping[str, Any]) -> HeartbeatRecord:
        details = _decode_canonical_payload(
            row["details_json"],
            hashlib.sha256(str(row["details_json"]).encode("utf-8")).hexdigest(),
            field="heartbeat details",
            maximum_bytes=_MAX_DETAILS_BYTES,
        )
        if not isinstance(details, dict):
            raise StorageError("persisted heartbeat details are not an object")
        status = _stored_text(row["status"], field="heartbeat status", maximum=16)
        if status not in _HEARTBEAT_STATUSES:
            raise StorageError("persisted heartbeat status is unsupported")
        record = HeartbeatRecord(
            node_id=_stored_text(row["node_id"], field="node_id", maximum=128),
            component=_stored_text(
                row["component"], field="component", maximum=128
            ),
            instance_id=_stored_text(
                row["instance_id"], field="instance_id", maximum=128
            ),
            fencing_token=int(row["fencing_token"]),
            status=status,
            observed_at=_parse_time(row["observed_at"], field="heartbeat observed_at"),
            valid_until=_parse_time(row["valid_until"], field="heartbeat valid_until"),
            details=details,
            content_hash=_stored_sha256(row["content_hash"], field="content_hash"),
        )
        if record.fencing_token <= 0 or record.valid_until <= record.observed_at:
            raise StorageError("persisted heartbeat counters or timestamps are invalid")
        _, expected_hash = cls._heartbeat_payload(
            node_id=record.node_id,
            component=record.component,
            instance_id=record.instance_id,
            fencing_token=record.fencing_token,
            status=record.status,
            observed_at=record.observed_at,
            valid_until=record.valid_until,
            details=record.details,
        )
        if record.content_hash != expected_hash:
            raise StorageError("persisted heartbeat content hash does not match")
        return record

    def heartbeat(
        self,
        node_id: str,
        component: str,
        instance_id: str,
        fencing_token: int,
        *,
        status: str,
        at: datetime,
        ttl_seconds: int,
        details: Mapping[str, Any] | None = None,
    ) -> HeartbeatRecord:
        checked_node = _text(node_id, field="node_id", maximum=128)
        checked_component = _text(component, field="component", maximum=128)
        checked_instance = _text(instance_id, field="instance_id", maximum=128)
        token = _positive_int(fencing_token, field="fencing_token")
        checked_status = _text(status, field="status", maximum=16)
        if checked_status not in _HEARTBEAT_STATUSES:
            raise ValidationError("unsupported heartbeat status")
        checked_at = _utc(at, field="at")
        ttl = _positive_int(ttl_seconds, field="ttl_seconds", maximum=86_400)
        details_json, _, checked_details = self._details(details)
        with self._transaction() as connection:
            lease = self._require_lease_locked(
                connection,
                node_id=checked_node,
                instance_id=checked_instance,
                fencing_token=token,
                at=checked_at,
            )
            # A component cannot advertise health beyond the fencing lease
            # that gives its process authority to publish that heartbeat.
            valid_until = min(
                checked_at + timedelta(seconds=ttl),
                lease.expires_at,
            )
            _, content_hash = self._heartbeat_payload(
                node_id=checked_node,
                component=checked_component,
                instance_id=checked_instance,
                fencing_token=token,
                status=checked_status,
                observed_at=checked_at,
                valid_until=valid_until,
                details=checked_details,
            )
            runtime_row = connection.execute(
                "SELECT * FROM research_node_runtime WHERE node_id = ?",
                (checked_node,),
            ).fetchone()
            if runtime_row is None:
                raise StateConflict("node runtime must start before heartbeats")
            runtime = self._runtime_from_row(runtime_row)
            if runtime.instance_id != checked_instance or runtime.fencing_token != token:
                raise StateConflict("node heartbeat comes from a stale runtime")
            if runtime.process_state == "stopped" or checked_at < runtime.started_at:
                raise StateConflict("node runtime cannot accept this heartbeat")
            row = connection.execute(
                """
                SELECT * FROM research_node_heartbeats
                WHERE node_id = ? AND component = ?
                """,
                (checked_node, checked_component),
            ).fetchone()
            if row is not None:
                previous = self._heartbeat_from_row(row)
                if previous.fencing_token > token:
                    raise StateConflict("heartbeat fencing token is stale")
                if previous.fencing_token == token:
                    if checked_at < previous.observed_at:
                        raise StateConflict("heartbeat clock moved backwards")
                    if checked_at == previous.observed_at:
                        if previous.content_hash == content_hash:
                            return previous
                        raise StateConflict("same-time heartbeat content differs")
            connection.execute(
                """
                INSERT INTO research_node_heartbeats (
                    node_id, component, instance_id, fencing_token, status,
                    observed_at, valid_until, details_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, component) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    fencing_token = excluded.fencing_token,
                    status = excluded.status,
                    observed_at = excluded.observed_at,
                    valid_until = excluded.valid_until,
                    details_json = excluded.details_json,
                    content_hash = excluded.content_hash
                """,
                (
                    checked_node,
                    checked_component,
                    checked_instance,
                    token,
                    checked_status,
                    _time_text(checked_at, field="at"),
                    _time_text(valid_until, field="valid_until"),
                    details_json,
                    content_hash,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM research_node_heartbeats
                WHERE node_id = ? AND component = ?
                """,
                (checked_node, checked_component),
            ).fetchone()
            if updated is None:
                raise StorageError("node heartbeat disappeared before commit")
            return self._heartbeat_from_row(updated)

    def list_heartbeats(
        self,
        node_id: str,
        *,
        current_runtime_only: bool = True,
    ) -> tuple[HeartbeatRecord, ...]:
        checked_node = _text(node_id, field="node_id", maximum=128)
        if type(current_runtime_only) is not bool:
            raise TypeError("current_runtime_only must be bool")
        connection = self._connect()
        try:
            if current_runtime_only:
                rows = connection.execute(
                    """
                    SELECT heartbeat.* FROM research_node_heartbeats AS heartbeat
                    JOIN research_node_runtime AS runtime
                      ON runtime.node_id = heartbeat.node_id
                     AND runtime.fencing_token = heartbeat.fencing_token
                    WHERE heartbeat.node_id = ?
                    ORDER BY heartbeat.component
                    """,
                    (checked_node,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM research_node_heartbeats
                    WHERE node_id = ? ORDER BY component
                    """,
                    (checked_node,),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._heartbeat_from_row(row) for row in rows)


__all__ = (
    "AssetAnalysisRecord",
    "HeartbeatRecord",
    "NodeLeaseRecord",
    "NodeRuntimeRecord",
    "RESEARCH_SCHEMA_VERSION",
    "ResearchArtifactRecord",
    "ResearchStore",
)
