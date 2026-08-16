import json
import os
from pathlib import Path

import pytest

from hypergrok.journal import Attempt, JournalError


def acquire(path: Path) -> Attempt:
    return Attempt.acquire(
        path,
        digest="a" * 64,
        cloid="0x" + "b" * 32,
        network="testnet",
        account="0x" + "1" * 40,
    )


def test_journal_is_private_and_blocks_same_plan(tmp_path: Path) -> None:
    state = tmp_path / "state"
    attempt = acquire(state)
    assert state.stat().st_mode & 0o777 == 0o700
    assert attempt.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(JournalError, match="already has"):
        acquire(state)


def test_journal_allows_only_fail_closed_transitions(tmp_path: Path) -> None:
    attempt = acquire(tmp_path / "state")
    attempt.transition("sending")
    attempt.transition("accepted")
    document = json.loads(attempt.path.read_text())
    assert document["stage"] == "accepted"
    with pytest.raises(JournalError, match="Invalid"):
        attempt.transition("sending")


def test_journal_rejects_public_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    with pytest.raises(JournalError, match="mode 700"):
        acquire(state)


def test_reservation_fsyncs_file_and_containing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", calls.append)
    acquire(tmp_path / "state")
    assert len(calls) >= 2
