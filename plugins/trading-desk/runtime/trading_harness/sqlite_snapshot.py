"""No-follow private snapshots for mutation-free SQLite state verification."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator
import sqlite3

from .errors import StorageError


@dataclass(frozen=True, slots=True)
class SQLiteVerificationSnapshot:
    database: Path
    header: bytes


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    signature: tuple[int, ...]
    digest: str
    header: bytes


def validate_sqlite_file_sizes(
    database: Path,
    *,
    max_bytes: int,
) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise TypeError("max_bytes must be a positive integer")
    for path in (
        Path(database),
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StorageError("SQLite state size cannot be inspected") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise StorageError("SQLite state must be a regular single-link file")
        if metadata.st_size > max_bytes:
            raise StorageError("SQLite state exceeds its size limit")


def enforce_sqlite_write_limit(
    connection: sqlite3.Connection,
    database: Path,
    *,
    max_bytes: int,
    reserve_bytes: int,
) -> None:
    """Fail before a bounded write can exceed logical or physical state limits."""

    validate_sqlite_file_sizes(database, max_bytes=max_bytes)
    if type(reserve_bytes) is not int or not 0 <= reserve_bytes < max_bytes:
        raise TypeError("reserve_bytes must be a bounded nonnegative integer")
    wal = Path(f"{database}-wal")
    try:
        wal_size = wal.lstat().st_size
    except FileNotFoundError:
        wal_size = 0
    except OSError as error:
        raise StorageError("SQLite WAL size cannot be inspected") from error
    if wal_size + reserve_bytes > max_bytes:
        raise StorageError("SQLite WAL lacks bounded write headroom")
    page_size_row = connection.execute("PRAGMA page_size").fetchone()
    page_count_row = connection.execute("PRAGMA page_count").fetchone()
    if page_size_row is None or page_count_row is None:
        raise StorageError("SQLite size metadata is unavailable")
    page_size = int(page_size_row[0])
    page_count = int(page_count_row[0])
    if page_size <= 0 or page_count < 0:
        raise StorageError("SQLite size metadata is invalid")
    max_pages = max_bytes // page_size
    if max_pages <= 0 or page_count * page_size + reserve_bytes > max_bytes:
        raise StorageError("SQLite write would exceed its size limit")
    applied = connection.execute(f"PRAGMA max_page_count = {max_pages:d}").fetchone()
    if applied is None or int(applied[0]) > max_pages:
        raise StorageError("SQLite page limit could not be enforced")


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_source(
    path: Path,
    *,
    label: str,
    max_bytes: int | None,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        try:
            metadata = path.lstat()
        except OSError:
            metadata = None
        if metadata is not None and (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
        ):
            raise StorageError(
                f"{label} must be a regular file with one link"
            ) from error
        raise StorageError(f"{label} is unavailable") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise StorageError(f"{label} must be a regular file with one link")
    if max_bytes is not None and metadata.st_size > max_bytes:
        os.close(descriptor)
        raise StorageError(f"{label} exceeds the verification size limit")
    return descriptor


def _read_source(
    path: Path,
    *,
    label: str,
    max_bytes: int | None,
) -> _FileSnapshot:
    descriptor = _open_source(path, label=label, max_bytes=max_bytes)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        header = b""
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise StorageError(f"{label} exceeds the verification size limit")
            if len(header) < 20:
                header += chunk[: 20 - len(header)]
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _signature(before) != _signature(after):
            raise StorageError(f"{label} changed while it was read")
        return _FileSnapshot(_signature(after), digest.hexdigest(), header)
    finally:
        os.close(descriptor)


def _copy_source(
    source: Path,
    destination: Path,
    *,
    label: str,
    max_bytes: int | None,
) -> _FileSnapshot:
    source_descriptor = _open_source(
        source,
        label=label,
        max_bytes=max_bytes,
    )
    destination_descriptor = -1
    try:
        before = os.fstat(source_descriptor)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        destination_descriptor = os.open(destination, flags, 0o600)
        os.fchmod(destination_descriptor, 0o600)
        digest = hashlib.sha256()
        header = b""
        total = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise StorageError(f"{label} exceeds the verification size limit")
            if len(header) < 20:
                header += chunk[: 20 - len(header)]
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise StorageError(f"temporary {label} write did not progress")
                remaining = remaining[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if _signature(before) != _signature(after):
            raise StorageError(f"{label} changed while it was snapshotted")
        return _FileSnapshot(_signature(after), digest.hexdigest(), header)
    except OSError as error:
        raise StorageError(f"{label} could not be snapshotted") from error
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _optional_snapshot(
    path: Path,
    *,
    label: str,
    max_bytes: int | None,
) -> _FileSnapshot | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageError(f"{label} is unavailable") from error
    return _read_source(path, label=label, max_bytes=max_bytes)


def _require_unchanged(
    path: Path,
    *,
    label: str,
    expected: _FileSnapshot | None,
    max_bytes: int | None,
) -> None:
    if _optional_snapshot(path, label=label, max_bytes=max_bytes) != expected:
        raise StorageError(f"{label} changed during verification")


@contextmanager
def sqlite_verification_snapshot(
    database: Path,
    *,
    label: str,
    max_bytes: int | None = None,
) -> Iterator[SQLiteVerificationSnapshot]:
    """Yield a private main/WAL copy and prove the sources stay unchanged."""

    database = Path(database)
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    with tempfile.TemporaryDirectory(
        prefix=".trading-sqlite-verify-",
        dir=database.parent,
    ) as directory:
        private_root = Path(directory)
        root_metadata = private_root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise StorageError("SQLite verification directory is not private")
        private_database = private_root / database.name
        main_snapshot = _copy_source(
            database,
            private_database,
            label=label,
            max_bytes=max_bytes,
        )
        wal_snapshot = _optional_snapshot(
            wal,
            label=f"{label} WAL",
            max_bytes=max_bytes,
        )
        shm_snapshot = _optional_snapshot(
            shm,
            label=f"{label} SHM",
            max_bytes=max_bytes,
        )
        if wal_snapshot is not None:
            copied_wal = _copy_source(
                wal,
                Path(f"{private_database}-wal"),
                label=f"{label} WAL",
                max_bytes=max_bytes,
            )
            if copied_wal != wal_snapshot:
                raise StorageError(f"{label} WAL changed before it was copied")
        _require_unchanged(
            database,
            label=label,
            expected=main_snapshot,
            max_bytes=max_bytes,
        )
        _require_unchanged(
            wal,
            label=f"{label} WAL",
            expected=wal_snapshot,
            max_bytes=max_bytes,
        )
        _require_unchanged(
            shm,
            label=f"{label} SHM",
            expected=shm_snapshot,
            max_bytes=max_bytes,
        )
        try:
            yield SQLiteVerificationSnapshot(
                database=private_database,
                header=main_snapshot.header,
            )
        finally:
            _require_unchanged(
                database,
                label=label,
                expected=main_snapshot,
                max_bytes=max_bytes,
            )
            _require_unchanged(
                wal,
                label=f"{label} WAL",
                expected=wal_snapshot,
                max_bytes=max_bytes,
            )
            _require_unchanged(
                shm,
                label=f"{label} SHM",
                expected=shm_snapshot,
                max_bytes=max_bytes,
            )


__all__ = (
    "SQLiteVerificationSnapshot",
    "enforce_sqlite_write_limit",
    "sqlite_verification_snapshot",
    "validate_sqlite_file_sizes",
)
