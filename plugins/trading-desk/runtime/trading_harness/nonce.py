"""Crash-safe, per-signer Hyperliquid nonce allocation.

Hyperliquid tracks nonces per API-wallet signer, even when that signer acts for
multiple subaccounts.  This allocator therefore keys state by signer and
network only, serializes allocation with ``BEGIN IMMEDIATE``, and commits the
chosen nonce before any caller can transmit a signed action.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3

from .errors import StorageError, ValidationError
from .executor_state_binding import (
    MAX_PRIVATE_STATE_FILE_BYTES,
    STATE_BINDING_TABLE,
    STATE_BINDING_TABLE_SQL_NORMALIZED,
    normalized_schema_sql,
)
from .hyperliquid_wire import HyperliquidNetwork
from .sqlite_snapshot import sqlite_verification_snapshot


Clock = Callable[[], datetime]
_SIGNER_RE = re.compile(r"^0x[0-9a-f]{40}$")
_MAX_FUTURE_DRIFT_MS = 86_400_000
NONCE_SCHEMA_VERSION = 1
_SCHEMA_NAME = "bound_hyperliquid_signer_nonces"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS nonce_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS hyperliquid_nonce_bindings (
        signer_address TEXT NOT NULL,
        network TEXT NOT NULL CHECK(network IN ('mainnet', 'testnet')),
        schema_version INTEGER NOT NULL CHECK(schema_version = 1),
        binding_hash TEXT NOT NULL,
        PRIMARY KEY (signer_address, network)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS hyperliquid_signer_nonces (
        signer_address TEXT NOT NULL,
        network TEXT NOT NULL CHECK(network IN ('mainnet', 'testnet')),
        last_nonce INTEGER NOT NULL CHECK(last_nonce >= 0),
        PRIMARY KEY (signer_address, network)
    ) STRICT
    """,
)
_SCHEMA_CHECKSUM = hashlib.sha256(
    "\n-- nonce schema statement --\n".join(_SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()
_EXPECTED_COLUMNS = {
    "nonce_schema_migrations": ("version", "name", "checksum"),
    "hyperliquid_nonce_bindings": (
        "signer_address",
        "network",
        "schema_version",
        "binding_hash",
    ),
    "hyperliquid_signer_nonces": (
        "signer_address",
        "network",
        "last_nonce",
    ),
}


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc_ms(clock: Clock) -> int:
    try:
        value = clock()
    except Exception as error:
        raise ValidationError(f"nonce clock failed: {type(error).__name__}") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("nonce clock must return a timezone-aware datetime")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _binding_hash(signer_address: str, network: HyperliquidNetwork) -> str:
    material = "\0".join(
        (
            "trading-harness/hyperliquid-nonce-binding/v1",
            str(NONCE_SCHEMA_VERSION),
            _SCHEMA_CHECKSUM,
            signer_address,
            network.value,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalized_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.rstrip(";").split()).lower()
    return normalized.replace("create table if not exists ", "create table ", 1)


class PersistentNonceAllocator:
    """Allocate strictly increasing nonces from one durable SQLite database."""

    def __init__(
        self,
        database: str | Path,
        *,
        signer_address: str,
        network: HyperliquidNetwork,
        clock: Clock = _default_clock,
        must_exist: bool = False,
    ) -> None:
        if isinstance(database, Path):
            database_text = str(database)
        elif isinstance(database, str):
            database_text = database
        else:
            raise TypeError("database must be a filesystem path")
        if not database_text or database_text == ":memory:" or "\x00" in database_text:
            raise ValidationError("database path is invalid")
        if not isinstance(signer_address, str) or not _SIGNER_RE.fullmatch(
            signer_address
        ):
            raise ValidationError("signer_address must be a lowercase Ethereum address")
        if not isinstance(network, HyperliquidNetwork):
            try:
                network = HyperliquidNetwork(network)
            except (TypeError, ValueError) as error:
                raise ValidationError("network must be explicit mainnet or testnet") from error
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(must_exist) is not bool:
            raise TypeError("must_exist must be a boolean")
        self._database = database_text
        self._must_exist = must_exist
        self._signer_address = signer_address
        self._network = network
        self._clock = clock
        self._initialize()

    def _connect(
        self,
        *,
        verification_only: bool = False,
        verification_path: Path | None = None,
    ) -> sqlite3.Connection:
        selected = Path(self._database) if verification_path is None else verification_path
        database: str | Path = selected
        if verification_only:
            database = f"{selected.absolute().as_uri()}?mode=ro"
        elif self._must_exist:
            database = f"{Path(self._database).absolute().as_uri()}?mode=rw"
        connection = sqlite3.connect(
            database,
            timeout=30,
            isolation_level=None,
            uri=verification_only or self._must_exist,
        )
        try:
            connection.row_factory = sqlite3.Row
            if verification_only:
                connection.execute("PRAGMA query_only = ON")
                query_only = connection.execute("PRAGMA query_only").fetchone()
                if query_only is None or query_only[0] != 1:
                    raise StorageError(
                        "nonce verification connection is not query-only"
                    )
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA synchronous = FULL")
            if verification_only:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
                if journal_mode is None or journal_mode[0].lower() != "wal":
                    raise StorageError(
                        "existing nonce store is not configured for WAL"
                    )
            elif self._must_exist:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
                if journal_mode is None or journal_mode[0].lower() != "wal":
                    raise StorageError(
                        "existing nonce store is not configured for WAL"
                    )
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            if self._must_exist:
                with sqlite_verification_snapshot(
                    Path(self._database),
                    label="nonce database",
                    max_bytes=MAX_PRIVATE_STATE_FILE_BYTES,
                ) as snapshot:
                    connection = self._connect(
                        verification_only=True,
                        verification_path=snapshot.database,
                    )
                    try:
                        self._verify_integrity(connection)
                        self._verify_schema(connection)
                        self._bind_or_verify(connection, allow_create=False)
                    finally:
                        connection.close()
                        connection = None
                return

            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            row = connection.execute(
                "SELECT name, checksum FROM nonce_schema_migrations WHERE version = ?",
                (NONCE_SCHEMA_VERSION,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO nonce_schema_migrations (version, name, checksum)
                    VALUES (?, ?, ?)
                    """,
                    (NONCE_SCHEMA_VERSION, _SCHEMA_NAME, _SCHEMA_CHECKSUM),
                )
            elif row["name"] != _SCHEMA_NAME or row["checksum"] != _SCHEMA_CHECKSUM:
                raise StorageError("nonce migration checksum does not match")
            self._verify_schema(connection)
            self._bind_or_verify(connection, allow_create=True)
            connection.commit()
        except (sqlite3.Error, StorageError) as error:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            if isinstance(error, StorageError):
                raise
            raise StorageError(
                f"nonce store initialization failed: {type(error).__name__}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            raise StorageError("nonce store integrity check failed")

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
        expected_names = set(_EXPECTED_COLUMNS)
        allowed_names = expected_names | {STATE_BINDING_TABLE}
        if object_names not in (expected_names, allowed_names):
            raise StorageError("nonce database has unexpected schema objects")
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
                raise StorageError("nonce deployment binding schema does not match")
        for table, expected in _EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple(row["name"] for row in rows)
            if actual != expected:
                raise StorageError(f"nonce table schema does not match: {table}")
        placeholders = ",".join("?" for _ in _EXPECTED_COLUMNS)
        table_rows = connection.execute(
            f"""
            SELECT name, sql FROM sqlite_master
            WHERE type = 'table' AND name IN ({placeholders})
            """,
            tuple(_EXPECTED_COLUMNS),
        ).fetchall()
        actual_tables = {
            row["name"]: _normalized_sql(row["sql"]) for row in table_rows
        }
        expected_tables = {
            table: _normalized_sql(statement)
            for table, statement in zip(
                _EXPECTED_COLUMNS, _SCHEMA_STATEMENTS, strict=True
            )
        }
        if actual_tables != expected_tables:
            raise StorageError("nonce table definitions do not match")
        migrations = connection.execute(
            "SELECT version, name, checksum FROM nonce_schema_migrations ORDER BY version"
        ).fetchall()
        if len(migrations) != 1:
            raise StorageError("nonce migration history is invalid")
        row = migrations[0]
        if (
            row["version"] != NONCE_SCHEMA_VERSION
            or row["name"] != _SCHEMA_NAME
            or row["checksum"] != _SCHEMA_CHECKSUM
        ):
            raise StorageError("nonce migration history does not match")

    def _bind_or_verify(
        self, connection: sqlite3.Connection, *, allow_create: bool
    ) -> None:
        rows = connection.execute(
            """
            SELECT signer_address, network, schema_version, binding_hash
            FROM hyperliquid_nonce_bindings
            ORDER BY signer_address, network
            """
        ).fetchall()
        expected_identity = (self._signer_address, self._network.value)
        found_expected = False
        for row in rows:
            signer_address = row["signer_address"]
            network_value = row["network"]
            schema_version = row["schema_version"]
            binding_hash = row["binding_hash"]
            if (
                not isinstance(signer_address, str)
                or not _SIGNER_RE.fullmatch(signer_address)
                or network_value not in {
                    HyperliquidNetwork.MAINNET.value,
                    HyperliquidNetwork.TESTNET.value,
                }
                or type(schema_version) is not int
                or schema_version != NONCE_SCHEMA_VERSION
            ):
                raise StorageError("persisted nonce binding is invalid")
            stored_network = HyperliquidNetwork(network_value)
            if binding_hash != _binding_hash(signer_address, stored_network):
                raise StorageError("persisted nonce binding hash does not match")
            if (signer_address, network_value) == expected_identity:
                found_expected = True
        if found_expected:
            return
        if not allow_create:
            raise StorageError("nonce store is not bound to the requested signer and network")
        connection.execute(
            """
            INSERT INTO hyperliquid_nonce_bindings (
                signer_address, network, schema_version, binding_hash
            ) VALUES (?, ?, ?, ?)
            """,
            (
                self._signer_address,
                self._network.value,
                NONCE_SCHEMA_VERSION,
                _binding_hash(self._signer_address, self._network),
            ),
        )

    def allocate(self) -> int:
        """Persist and return ``max(last + 1, current Unix milliseconds)``."""

        now_ms = _utc_ms(self._clock)
        if now_ms < 0:
            raise ValidationError("nonce clock predates the Unix epoch")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT last_nonce
                FROM hyperliquid_signer_nonces
                WHERE signer_address = ? AND network = ?
                """,
                (self._signer_address, self._network.value),
            ).fetchone()
            previous = None if row is None else row["last_nonce"]
            if previous is not None and (
                type(previous) is not int or previous < 0
            ):
                raise StorageError("persisted nonce is invalid")
            if previous is not None and previous > now_ms + _MAX_FUTURE_DRIFT_MS:
                raise StorageError("persisted nonce is implausibly far ahead of the clock")
            nonce = now_ms if previous is None else max(previous + 1, now_ms)
            connection.execute(
                """
                INSERT INTO hyperliquid_signer_nonces (
                    signer_address, network, last_nonce
                ) VALUES (?, ?, ?)
                ON CONFLICT(signer_address, network) DO UPDATE SET
                    last_nonce = excluded.last_nonce
                """,
                (self._signer_address, self._network.value, nonce),
            )
            connection.commit()
            return nonce
        except (sqlite3.Error, StorageError) as error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if isinstance(error, StorageError):
                raise
            raise StorageError(
                f"nonce allocation failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

    def last_allocated(self) -> int | None:
        """Read the last committed nonce without advancing it."""

        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT last_nonce
                FROM hyperliquid_signer_nonces
                WHERE signer_address = ? AND network = ?
                """,
                (self._signer_address, self._network.value),
            ).fetchone()
        except sqlite3.Error as error:
            raise StorageError(f"nonce read failed: {type(error).__name__}") from error
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            return None
        value = row["last_nonce"]
        if type(value) is not int or value < 0:
            raise StorageError("persisted nonce is invalid")
        return value


__all__ = ("PersistentNonceAllocator",)
