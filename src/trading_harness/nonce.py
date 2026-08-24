"""Crash-safe, per-signer Hyperliquid nonce allocation.

Hyperliquid tracks nonces per API-wallet signer, even when that signer acts for
multiple subaccounts.  This allocator therefore keys state by signer and
network only, serializes allocation with ``BEGIN IMMEDIATE``, and commits the
chosen nonce before any caller can transmit a signed action.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from .errors import StorageError, ValidationError
from .hyperliquid_wire import HyperliquidNetwork


Clock = Callable[[], datetime]
_SIGNER_RE = re.compile(r"^0x[0-9a-f]{40}$")
_MAX_FUTURE_DRIFT_MS = 86_400_000


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


class PersistentNonceAllocator:
    """Allocate strictly increasing nonces from one durable SQLite database."""

    def __init__(
        self,
        database: str | Path,
        *,
        signer_address: str,
        network: HyperliquidNetwork,
        clock: Clock = _default_clock,
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
        self._database = database_text
        self._signer_address = signer_address
        self._network = network
        self._clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=30,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hyperliquid_signer_nonces (
                    signer_address TEXT NOT NULL,
                    network TEXT NOT NULL CHECK(network IN ('mainnet', 'testnet')),
                    last_nonce INTEGER NOT NULL CHECK(last_nonce >= 0),
                    PRIMARY KEY (signer_address, network)
                ) STRICT
                """
            )
        except sqlite3.Error as error:
            raise StorageError(
                f"nonce store initialization failed: {type(error).__name__}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

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
