"""Durable local execution-attempt journal.

The journal closes the same-machine check-then-send race. A record is never
deleted automatically: once a confirmed plan reaches the send boundary, that
plan cannot be submitted again from the same state directory.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JournalError(RuntimeError):
    """The execution journal cannot prove this plan is fresh."""


@dataclass
class Attempt:
    path: Path
    document: dict[str, Any]

    @classmethod
    def acquire(
        cls,
        state_dir: Path,
        *,
        digest: str,
        cloid: str,
        network: str,
        account: str,
    ) -> Attempt:
        _secure_state_dir(state_dir)
        path = state_dir / f"{digest}.json"
        now = datetime.now(UTC).isoformat()
        document: dict[str, Any] = {
            "schema_version": 1,
            "plan_sha256": digest,
            "cloid": cloid,
            "network": network,
            "account": account,
            "stage": "reserved",
            "created_at": now,
            "updated_at": now,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise JournalError(
                "This plan already has an execution-attempt record. "
                "Do not retry it; reconcile the cloid and create a new plan."
            ) from exc
        except OSError as exc:
            raise JournalError(f"Could not reserve execution journal record in {state_dir}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise JournalError(f"Could not persist execution reservation at {path}") from exc
        return cls(path=path, document=document)

    def transition(self, stage: str, **fields: Any) -> None:
        allowed = {
            "reserved": {"sending"},
            "sending": {"accepted", "rejected", "unknown"},
        }
        current = str(self.document["stage"])
        if stage not in allowed.get(current, set()):
            raise JournalError(f"Invalid execution journal transition: {current} -> {stage}")
        self.document.update(fields)
        self.document["stage"] = stage
        self.document["updated_at"] = datetime.now(UTC).isoformat()
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise JournalError(f"Could not persist execution state at {self.path}") from exc


def _secure_state_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise JournalError(f"Could not create execution state directory: {path}") from exc
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        raise JournalError(f"Execution state path is not a real directory: {path}")
    if details.st_mode & 0o077:
        raise JournalError(f"Execution state directory must have mode 700: {path}")
