"""Exact per-database deployment bindings for executor-composed SQLite state."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from .canonical import domain_hash
from .errors import StorageError, ValidationError
from .executor_config import ExecutorConfig
from .sqlite_snapshot import sqlite_verification_snapshot


STATE_BINDING_SCHEMA_VERSION = 1
STATE_BINDING_TABLE = "executor_deployment_binding"
MAX_PRIVATE_STATE_FILE_BYTES = 1024 * 1024 * 1024
MAX_SHARED_STATE_FILE_BYTES = 64 * 1024 * 1024
STATE_BINDING_TABLE_SQL = """
    CREATE TABLE executor_deployment_binding (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        state_role TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        binding_hash TEXT NOT NULL
    ) STRICT
"""


def normalized_schema_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.rstrip("; ").split()).lower()


STATE_BINDING_TABLE_SQL_NORMALIZED = normalized_schema_sql(STATE_BINDING_TABLE_SQL)


def configured_state_roles(config: ExecutorConfig) -> dict[Path, str]:
    return {
        config.paths.execution_database: "execution",
        config.paths.nonce_database: "nonce",
        config.paths.daily_loss_database: "daily_loss",
        config.paths.learning_database: "learning",
        config.paths.staging_database: "staging",
    }


def state_file_size_limit(config: ExecutorConfig, database: Path) -> int:
    try:
        role = configured_state_roles(config)[database]
    except KeyError as error:
        raise ValidationError("database is not a configured executor state path") from error
    if role in {"learning", "staging"}:
        return MAX_SHARED_STATE_FILE_BYTES
    return MAX_PRIVATE_STATE_FILE_BYTES


def _binding_hash(*, role: str, config_hash: str) -> str:
    return domain_hash(
        "trading-harness/executor-state-binding/v1",
        {
            "schema_version": STATE_BINDING_SCHEMA_VERSION,
            "state_role": role,
            "config_hash": config_hash,
        },
    )


def write_state_database_binding(config: ExecutorConfig, database: Path) -> None:
    try:
        role = configured_state_roles(config)[database]
    except KeyError as error:
        raise ValidationError("database is not a configured executor state path") from error
    uri = f"{database.absolute().as_uri()}?mode=rw"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, isolation_level=None, uri=True)
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            f"SELECT 1 FROM sqlite_master WHERE name = '{STATE_BINDING_TABLE}'"
        ).fetchone()
        if existing is not None:
            raise ValidationError("executor state binding already exists")
        connection.execute(STATE_BINDING_TABLE_SQL)
        connection.execute(
            """
            INSERT INTO executor_deployment_binding (
                singleton, schema_version, state_role, config_hash, binding_hash
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (
                STATE_BINDING_SCHEMA_VERSION,
                role,
                config.config_hash,
                _binding_hash(role=role, config_hash=config.config_hash),
            ),
        )
        connection.execute("COMMIT")
    except ValidationError:
        if connection is not None and connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    except (sqlite3.Error, StorageError) as error:
        if connection is not None and connection.in_transaction:
            connection.execute("ROLLBACK")
        raise ValidationError("executor state binding could not be created") from error
    finally:
        if connection is not None:
            connection.close()


def verify_state_database_binding(config: ExecutorConfig, database: Path) -> None:
    try:
        role = configured_state_roles(config)[database]
    except KeyError as error:
        raise ValidationError("database is not a configured executor state path") from error
    try:
        with sqlite_verification_snapshot(
            database,
            label=f"{role} state database",
            max_bytes=state_file_size_limit(config, database),
        ) as snapshot:
            uri = f"{snapshot.database.absolute().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, isolation_level=None, uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                schema = connection.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name = ?",
                    (STATE_BINDING_TABLE,),
                ).fetchall()
                if len(schema) != 1 or schema[0]["type"] != "table":
                    raise ValidationError("executor state binding table is missing")
                if normalized_schema_sql(schema[0]["sql"]) != (
                    STATE_BINDING_TABLE_SQL_NORMALIZED
                ):
                    raise ValidationError("executor state binding schema does not match")
                rows = connection.execute(
                    "SELECT * FROM executor_deployment_binding ORDER BY singleton"
                ).fetchall()
                if len(rows) != 1:
                    raise ValidationError("executor state binding row is missing")
                row = rows[0]
                if (
                    row["singleton"] != 1
                    or row["schema_version"] != STATE_BINDING_SCHEMA_VERSION
                    or row["state_role"] != role
                    or row["config_hash"] != config.config_hash
                    or row["binding_hash"]
                    != _binding_hash(role=role, config_hash=config.config_hash)
                ):
                    raise ValidationError("executor state binding does not match config")
            finally:
                connection.close()
    except ValidationError:
        raise
    except (OSError, sqlite3.Error, StorageError) as error:
        raise ValidationError("executor state binding cannot be verified") from error


def write_state_bindings(config: ExecutorConfig) -> None:
    previous_umask = os.umask(0o077)
    try:
        for database in configured_state_roles(config):
            write_state_database_binding(config, database)
    finally:
        os.umask(previous_umask)


def verify_state_bindings(config: ExecutorConfig) -> None:
    for database in configured_state_roles(config):
        verify_state_database_binding(config, database)


__all__ = (
    "STATE_BINDING_TABLE",
    "STATE_BINDING_TABLE_SQL",
    "STATE_BINDING_TABLE_SQL_NORMALIZED",
    "MAX_PRIVATE_STATE_FILE_BYTES",
    "MAX_SHARED_STATE_FILE_BYTES",
    "configured_state_roles",
    "normalized_schema_sql",
    "state_file_size_limit",
    "verify_state_database_binding",
    "verify_state_bindings",
    "write_state_database_binding",
    "write_state_bindings",
)
