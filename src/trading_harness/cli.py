"""Local diagnostics and always-on research-node commands."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import signal
import sys
import threading
from typing import Any
import uuid

from .canonical import SEMANTIC_INTENT_HASH_DOMAIN, semantic_intent_hash
from .domain import SemanticIntent
from .executor import disabled_executor
from .node import ResearchNode, default_state_database, node_status
from .research_store import ResearchStore


_MAX_INTENT_CHARACTERS = 1_000_000


class IntentInputError(ValueError):
    """Raised when a CLI input cannot unambiguously represent an intent."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntentInputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_number(value: str) -> None:
    raise IntentInputError(f"non-finite JSON number is forbidden: {value}")


def _read_intent_text(path: str) -> str:
    if path == "-":
        text = sys.stdin.read(_MAX_INTENT_CHARACTERS + 1)
    else:
        with Path(path).open("r", encoding="utf-8") as stream:
            text = stream.read(_MAX_INTENT_CHARACTERS + 1)
    if len(text) > _MAX_INTENT_CHARACTERS:
        raise IntentInputError("intent document exceeds 1,000,000 characters")
    return text


def _parse_intent(path: str) -> SemanticIntent:
    value = json.loads(
        _read_intent_text(path),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_non_json_number,
    )
    if not isinstance(value, dict):
        raise IntentInputError("intent document must be a JSON object")
    return SemanticIntent.from_mapping(value)


def _doctor() -> int:
    execution = disabled_executor().status
    compatible_python = sys.version_info >= (3, 11)
    safe_execution_state = (
        not execution.venue_writes_enabled
        and not execution.credential_loading_enabled
        and execution.adapter == "disabled"
    )
    report = {
        "component": "trading-harness",
        "execution": execution.as_dict(),
        "live_trading": execution.venue_writes_enabled,
        "ok": compatible_python and safe_execution_state,
        "python": {
            "compatible": compatible_python,
            "implementation": platform.python_implementation(),
            "required": ">=3.11",
            "version": platform.python_version(),
        },
        "runtime_dependencies": "stdlib-only",
        "research": {
            "always_on_node_available": True,
            "local_state_writes_enabled": True,
            "venue_writes_enabled": False,
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _hash_intent(path: str) -> int:
    try:
        intent = _parse_intent(path)
        digest = semantic_intent_hash(intent)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        print(f"hash-intent: invalid intent: {error}", file=sys.stderr)
        return 2

    result = {
        "algorithm": "sha256",
        "domain": SEMANTIC_INTENT_HASH_DOMAIN,
        "intent_hash": digest,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


def _node_status(path: Path, node_id: str) -> int:
    if not path.exists():
        report = {
            "node_id": node_id,
            "available": False,
            "process_state": "stopped",
            "capability": "research_only",
            "risk_gate": "halted",
            "venue_writes_enabled": False,
            "credential_loading_enabled": False,
            "heartbeats": [],
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    try:
        store = ResearchStore(path)
        report = node_status(store, node_id, at=datetime.now(timezone.utc))
    except Exception as error:
        print(f"node status failed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _run_node(
    path: Path,
    node_id: str,
    instance_id: str | None,
    poll_seconds: float,
    history_bars: int,
) -> int:
    resolved_instance = (
        f"local-{uuid.uuid4().hex}" if instance_id is None else instance_id
    )
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous: dict[int, object] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        node = ResearchNode(
            ResearchStore(path),
            node_id=node_id,
            instance_id=resolved_instance,
            history_bars=history_bars,
        )
        node.run_forever(stop_event, poll_seconds=poll_seconds)
    except Exception as error:
        print(f"node run failed: {type(error).__name__}", file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally read-only command surface."""

    parser = argparse.ArgumentParser(
        prog="trading-harness",
        description=(
            "Local diagnostics and research-node controls for the fail-closed "
            "trading harness. No command can submit a trade."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor",
        help="report runtime and fail-closed execution status",
    )
    doctor.set_defaults(handler=lambda _arguments: _doctor())

    hash_intent = commands.add_parser(
        "hash-intent",
        help="validate and hash a semantic-intent JSON document",
    )
    hash_intent.add_argument(
        "path",
        nargs="?",
        default="-",
        help="JSON file to read, or '-' (the default) for standard input",
    )
    hash_intent.set_defaults(
        handler=lambda arguments: _hash_intent(arguments.path)
    )

    node = commands.add_parser(
        "node",
        help="run or inspect the credential-free always-on research node",
    )
    node_commands = node.add_subparsers(dest="node_command", required=True)
    node_run = node_commands.add_parser("run", help="run until SIGINT or SIGTERM")
    node_run.add_argument(
        "--state-db",
        type=Path,
        default=default_state_database(),
        help="file-backed research SQLite path",
    )
    node_run.add_argument("--node-id", default="trading-desk-research")
    node_run.add_argument("--instance-id")
    node_run.add_argument("--poll-seconds", type=float, default=1.0)
    node_run.add_argument("--history-bars", type=int, default=1200)
    node_run.set_defaults(
        handler=lambda arguments: _run_node(
            arguments.state_db,
            arguments.node_id,
            arguments.instance_id,
            arguments.poll_seconds,
            arguments.history_bars,
        )
    )
    node_status_parser = node_commands.add_parser(
        "status",
        help="read persisted lease/runtime/heartbeat status",
    )
    node_status_parser.add_argument(
        "--state-db",
        type=Path,
        default=default_state_database(),
    )
    node_status_parser.add_argument("--node-id", default="trading-desk-research")
    node_status_parser.set_defaults(
        handler=lambda arguments: _node_status(
            arguments.state_db,
            arguments.node_id,
        )
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one read-only CLI command and return a process exit status."""

    arguments = build_parser().parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":  # pragma: no cover - exercised by the entry point
    raise SystemExit(main())
