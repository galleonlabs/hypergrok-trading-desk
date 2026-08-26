"""Operator CLI for the isolated, attended TESTNET executor deployment."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import threading
from typing import TextIO
import uuid

from .approval import TestnetApprovalAuthority
from .canonical import canonical_data
from .credential_provider import (
    KeychainCredentialConfig,
    MacOSKeychainCredentialProvider,
)
from .errors import StateConflict, ValidationError
from .execution_grant import (
    TestnetInfrastructureGrantAuthority,
    infrastructure_grant_confirmation,
)
from .executor_config import ExecutorConfig, load_executor_config
from .execution_store import ExecutionStore
from .domain import Environment
from .executor_service import (
    _validate_state_database_layout,
    _verify_state_database_binding,
    build_active_testnet_executor_service,
    initialize_testnet_executor_state,
    open_testnet_executor_state,
)
from .executor_status import RedactedExecutorConfigStatus
from .executor_runtime_store import ExecutorRuntimeStore, ManualHaltReason
from .grant_artifact import load_signed_infrastructure_grant
from .keychain_secret import (
    KeychainSecretConfig,
    MacOSKeychainHexSecretProvider,
)
from .learning_bridge import LearningRecorder
from .learning_ledger import LearningLedger
from .planning import RiskSizingPolicy, risk_ticket_from_dict
from .staging_inbox import StagingState, TradeStagingInbox, TrustedQuoteDecision
from .testnet_control import AttendedTestnetControlPlane


Clock = Callable[[], datetime]
Prompt = Callable[[str], str]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise argparse.ArgumentTypeError("path must be normalized and absolute")
    return path


def _json(value: object, *, stream: TextIO | None = None) -> None:
    converted = canonical_data(value)
    print(
        json.dumps(converted, indent=2, sort_keys=True),
        file=sys.stdout if stream is None else stream,
    )


def _failure(command: str, error: Exception) -> int:
    detail = str(error).strip()
    suffix = "" if not detail else f": {detail}"
    print(
        f"{command} failed: {type(error).__name__}{suffix}",
        file=sys.stderr,
    )
    return 2


def _load(path: Path) -> ExecutorConfig:
    return load_executor_config(path)


def _secret_provider(config, purpose: str) -> MacOSKeychainHexSecretProvider:
    return MacOSKeychainHexSecretProvider(
        KeychainSecretConfig(
            service=config.service,
            account=config.account,
            purpose=purpose,
            timeout_seconds=config.timeout_seconds,
            keychain_path=config.keychain_path,
        )
    )


def _inbox(config: ExecutorConfig, *, clock: Clock = _clock) -> TradeStagingInbox:
    return TradeStagingInbox(
        config.paths.staging_database,
        quote_callback=lambda _request: TrustedQuoteDecision.blocked(
            block_code="trusted_quote_profile_not_loaded"
        ),
        clock=clock,
        must_exist=True,
    )


def _require_state_file(
    config: ExecutorConfig,
    path: Path,
    *,
    label: str,
) -> None:
    try:
        _validate_state_database_layout(config, path, existing=True)
        _verify_state_database_binding(config, path)
    except (OSError, ValidationError) as error:
        raise StateConflict(f"{label} state must be initialized") from error


def _ticket_view(config: ExecutorConfig, document_id: str, *, clock: Clock = _clock):
    view = _inbox(config, clock=clock).get(document_id)
    if view.state is not StagingState.STAGED:
        raise StateConflict("staging document is not active")
    payload = view.document.ticket_payload
    if not isinstance(payload, dict) or not isinstance(payload.get("risk_ticket"), dict):
        raise ValidationError("staging document lacks a risk ticket")
    ticket = risk_ticket_from_dict(payload["risk_ticket"])
    return view, payload, ticket


def _terminal_prompt(message: str) -> str:
    """Read direct operator input from the controlling terminal, never stdin."""

    try:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write(message)
            terminal.flush()
            value = terminal.readline(513)
    except OSError as error:
        raise ValidationError(
            "direct controlling-terminal confirmation is required"
        ) from error
    if not value.endswith("\n") or len(value) > 512:
        raise ValidationError("terminal confirmation is invalid")
    return value[:-1]


def _validate(config_path: Path) -> int:
    try:
        config = _load(config_path)
        policy = RiskSizingPolicy()
        _json(
            {
                "schema_version": "testnet_executor_validation.v1",
                "config": RedactedExecutorConfigStatus.from_config(config).as_dict(),
                "environment": "testnet",
                "installed_risk_policy_hash": policy.policy_hash,
                "risk_policy_matches": config.risk_policy_hash == policy.policy_hash,
                "mainnet_authorized": False,
                "credential_loaded": False,
                "venue_write_attempted": False,
                "valid": config.risk_policy_hash == policy.policy_hash,
            }
        )
        return 0 if config.risk_policy_hash == policy.policy_hash else 2
    except Exception as error:
        return _failure("validate", error)


def _init(config_path: Path) -> int:
    try:
        config = _load(config_path)
        state = initialize_testnet_executor_state(config)
        _json(
            {
                "schema_version": "testnet_executor_initialization.v1",
                "initialized": True,
                "config": RedactedExecutorConfigStatus.from_config(config).as_dict(),
                "runtime": state.runtime_store.read().as_dict(),
                "credential_loaded": False,
                "venue_write_attempted": False,
                "mainnet_authorized": False,
            }
        )
        return 0
    except Exception as error:
        return _failure("init", error)


def _status(config_path: Path) -> int:
    try:
        config = _load(config_path)
        state = open_testnet_executor_state(config)
        report = state.observer.status().as_dict()
        report["shared_learning_available"] = state.learning is not None
        report["entry_blocked_by_shared_learning"] = state.learning is None
        _json(report)
        return 0
    except Exception as error:
        return _failure("status", error)


def _dry_run(config_path: Path) -> int:
    try:
        config = _load(config_path)
        state = open_testnet_executor_state(config)
        report = state.observer.dry_run().as_dict()
        report["shared_learning_available"] = state.learning is not None
        report["entry_blocked_by_shared_learning"] = state.learning is None
        _json(report)
        return 0
    except Exception as error:
        return _failure("dry-run", error)


def _acknowledge_halt(
    config_path: Path,
    expected_revision: int,
    expected_reason: str,
    *,
    prompt: Prompt = _terminal_prompt,
) -> int:
    try:
        config = _load(config_path)
        _require_state_file(
            config,
            config.paths.execution_database,
            label="executor",
        )
        runtime_store = ExecutorRuntimeStore(config, must_exist=True)
        current = runtime_store.read()
        reason = ManualHaltReason(expected_reason)
        if (
            current.revision != expected_revision
            or not current.manual_halt
            or current.manual_halt_reason is not reason
        ):
            raise StateConflict("current halt does not match the exact acknowledgement")
        phrase = (
            f"ACKNOWLEDGE HALT {config.config_hash[:16]} "
            f"REVISION {expected_revision} REASON {reason.value}"
        )
        _json(
            {
                "schema_version": "attended_halt_acknowledgement.v1",
                "current_runtime": current.as_dict(),
                "required_confirmation": phrase,
                "credentials_loaded": False,
                "venue_write_attempted": False,
                "risk_gate_after_acknowledgement": "halted",
                "startup_reconciliation_required": True,
            }
        )
        if prompt(f'Type exactly: "{phrase}"\n> ') != phrase:
            raise ValidationError("halt acknowledgement confirmation differs")
        _require_state_file(
            config,
            config.paths.execution_database,
            label="executor",
        )
        updated = runtime_store.acknowledge_stale_manual_halt(
            expected_revision=expected_revision,
            expected_reason=reason,
        )
        _json(
            {
                "schema_version": "attended_halt_acknowledgement_result.v1",
                "acknowledged": True,
                "runtime": updated.as_dict(),
                "credentials_loaded": False,
                "venue_write_attempted": False,
                "risk_gate": "halted",
                "startup_reconciliation_required": True,
            }
        )
        return 0
    except Exception as error:
        return _failure("acknowledge-halt", error)


def _show_stage(config_path: Path, document_id: str) -> int:
    try:
        config = _load(config_path)
        _require_state_file(
            config,
            config.paths.staging_database,
            label="staging",
        )
        view, payload, ticket = _ticket_view(config, document_id)
        _require_state_file(
            config,
            config.paths.staging_database,
            label="staging",
        )
        _json(
            {
                "schema_version": "attended_stage_review.v1",
                "document_id": view.document.document_id,
                "document_hash": view.document.document_hash,
                "expires_at": view.document.expires_at,
                "ticket": ticket.as_dict(),
                "purpose": payload["purpose"],
                "profitability_qualified": False,
                "mainnet_authorized": False,
                "stop_mandatory": True,
                "required_confirmation": (
                    AttendedTestnetControlPlane.confirmation_for(ticket)
                ),
                "authorization_created": False,
                "order_submitted": False,
            }
        )
        return 0
    except Exception as error:
        return _failure("show-stage", error)


def _authorize_stage(
    config_path: Path,
    grant_path: Path,
    document_id: str,
    approver_id: str,
    *,
    prompt: Prompt = _terminal_prompt,
    clock: Clock = _clock,
) -> int:
    try:
        config = _load(config_path)
        for path, label in (
            (config.paths.staging_database, "staging"),
            (config.paths.learning_database, "learning"),
            (config.paths.execution_database, "executor"),
        ):
            _require_state_file(
                config,
                path,
                label=label,
            )
        view, _, ticket = _ticket_view(config, document_id, clock=clock)
        expected = AttendedTestnetControlPlane.confirmation_for(ticket)
        _json(
            {
                "schema_version": "attended_authorization_prompt.v1",
                "document_id": view.document.document_id,
                "ticket_id": ticket.ticket_id,
                "ticket_hash": ticket.ticket_hash,
                "instrument": ticket.plan.entry.instrument if ticket.plan else None,
                "side": ticket.plan.entry.side.value if ticket.plan else None,
                "quantity": ticket.quantity,
                "stressed_loss": ticket.stressed_loss,
                "stop_price": (
                    ticket.plan.protective_stop.stop_price if ticket.plan else None
                ),
                "required_confirmation": expected,
                "order_submitted": False,
            }
        )
        supplied = prompt(f"Type exactly [{expected}]: ")
        if supplied != expected:
            raise ValidationError("direct confirmation does not match the ticket")

        signed_grant = load_signed_infrastructure_grant(grant_path)
        grant_secret = _secret_provider(
            config.grant_credential, "grant_hmac"
        ).load_secret()
        expected_issuer = f"{config.node_id}-grant-authority"
        expected_key_id = config.grant_credential.account
        expected_audience = f"{config.node_id}-learning-profile"
        if (
            signed_grant.issuer_id != expected_issuer
            or signed_grant.key_id != expected_key_id
            or signed_grant.audience != expected_audience
        ):
            raise StateConflict("signed grant targets another configured authority")
        grant_authority = TestnetInfrastructureGrantAuthority(
            grant_secret,
            issuer_id=expected_issuer,
            key_id=expected_key_id,
            audience=expected_audience,
        )
        trusted_grant = grant_authority.verify(signed_grant, at=clock())
        approval_secret = _secret_provider(
            config.approval_credential, "approval_hmac"
        ).load_secret()
        approval_authority = TestnetApprovalAuthority(
            approval_secret,
            key_id=config.approval_credential.account,
            audience=f"{config.node_id}-entry-admission",
        )
        execution_store = ExecutionStore(
            config.paths.execution_database,
            environment=Environment.TESTNET,
            account_id=config.account_id,
            max_reserved_loss=config.max_reserved_loss,
            max_reserved_notional=config.max_reserved_notional,
            must_exist=True,
        )
        control = AttendedTestnetControlPlane(
            _inbox(config, clock=clock),
            execution_store,
            config=config,
            grant=trusted_grant,
            approval_authority=approval_authority,
            learning_recorder=LearningRecorder(
                LearningLedger(
                    config.paths.learning_database,
                    clock=clock,
                    must_exist=True,
                )
            ),
            clock=clock,
        )
        for path, label in (
            (config.paths.staging_database, "staging"),
            (config.paths.learning_database, "learning"),
            (config.paths.execution_database, "executor"),
        ):
            _require_state_file(config, path, label=label)
        result = control.authorize_stage(
            document_id,
            confirmation=supplied,
            approver_id=approver_id,
        )
        _json(result.as_dict())
        return 0
    except Exception as error:
        return _failure("authorize-stage", error)


def _secure_new_artifact(path: Path, document: dict[str, object]) -> None:
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValidationError("grant output path must be a non-root absolute path")
    parent = path.parent
    metadata = parent.stat()
    if not parent.is_dir() or parent.is_symlink() or metadata.st_mode & 0o077:
        raise ValidationError("grant output directory must be a real mode-0700 directory")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValidationError("grant output directory must be process-owned")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _issue_grant(
    config_path: Path,
    output_path: Path,
    grant_id: str,
    generation: int,
    ttl_seconds: int,
    *,
    prompt: Prompt = _terminal_prompt,
    clock: Clock = _clock,
) -> int:
    try:
        config = _load(config_path)
        policy = RiskSizingPolicy()
        if config.risk_policy_hash != policy.policy_hash:
            raise ValidationError("config does not bind the installed risk policy")
        now = clock()
        expected = infrastructure_grant_confirmation(
            grant_id=grant_id,
            generation=generation,
            account_id=config.account_id,
            allowed_instruments=config.allowed_instruments,
            risk_policy_hash=policy.policy_hash,
            max_loss=config.max_reserved_loss,
            max_notional=config.max_reserved_notional,
            max_leverage=config.max_leverage,
            ttl_seconds=ttl_seconds,
        )
        _json(
            {
                "schema_version": "testnet_learning_grant_prompt.v1",
                "grant_id": grant_id,
                "generation": generation,
                "account_fingerprint": RedactedExecutorConfigStatus.from_config(
                    config
                ).account_fingerprint,
                "allowed_instruments": list(config.allowed_instruments),
                "max_loss": config.max_reserved_loss,
                "max_notional": config.max_reserved_notional,
                "max_leverage": config.max_leverage,
                "ttl_seconds": ttl_seconds,
                "profitability_qualified": False,
                "mainnet_authorized": False,
                "required_confirmation": expected,
            }
        )
        supplied = prompt(f"Type exactly [{expected}]: ")
        if supplied != expected:
            raise ValidationError("direct confirmation does not match the grant")
        secret = _secret_provider(config.grant_credential, "grant_hmac").load_secret()
        authority = TestnetInfrastructureGrantAuthority(
            secret,
            issuer_id=f"{config.node_id}-grant-authority",
            key_id=config.grant_credential.account,
            audience=f"{config.node_id}-learning-profile",
        )
        grant = authority.issue(
            grant_id=grant_id,
            generation=generation,
            account_id=config.account_id,
            allowed_instruments=config.allowed_instruments,
            risk_policy_hash=policy.policy_hash,
            max_loss=config.max_reserved_loss,
            max_notional=config.max_reserved_notional,
            max_leverage=config.max_leverage,
            confirmation=supplied,
            at=now,
            ttl_seconds=ttl_seconds,
        )
        _secure_new_artifact(output_path, grant.as_dict())
        _json(
            {
                "schema_version": "issued_testnet_learning_grant.v1",
                "grant_hash": grant.grant_hash,
                "grant_id": grant.grant_id,
                "expires_at": grant.expires_at,
                "purpose": "infrastructure_learning",
                "profitability_qualified": False,
                "mainnet_authorized": False,
                "artifact_created": True,
            }
        )
        return 0
    except Exception as error:
        return _failure("issue-grant", error)


def _run(
    config_path: Path,
    instance_id: str | None,
    worker_id: str | None,
    max_drain_steps: int,
) -> int:
    try:
        config = _load(config_path)
        state = open_testnet_executor_state(config)
        wallet = MacOSKeychainCredentialProvider(
            KeychainCredentialConfig(
                service=config.credential.service,
                account=config.credential.account,
                expected_signer_address=config.api_wallet_address,
                timeout_seconds=config.credential.timeout_seconds,
                keychain_path=config.credential.keychain_path,
            )
        ).load_wallet()
        recovery_secret = _secret_provider(
            config.recovery_credential, "recovery_hmac"
        ).load_secret()
        selected_instance = instance_id or f"service-{uuid.uuid4().hex}"
        selected_worker = worker_id or f"worker-{config.node_id}"
        service = build_active_testnet_executor_service(
            state=state,
            wallet=wallet,
            recovery_secret=recovery_secret,
            instance_id=selected_instance,
            worker_id=selected_worker,
        )
        stop_event = threading.Event()

        def stop(_signum: int, _frame: object) -> None:
            service.runtime.request_shutdown()
            stop_event.set()

        previous = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            service.start()
            while not stop_event.is_set():
                _json(service.tick().as_dict())
                stop_event.wait(config.poll_interval_ms / 1_000)
            _json(service.runtime.shutdown(max_drain_steps=max_drain_steps).as_dict())
        finally:
            if service.runtime.started:
                try:
                    _json(
                        service.runtime.shutdown(
                            max_drain_steps=max_drain_steps
                        ).as_dict()
                    )
                except Exception:
                    # The outer failure remains the primary error.  A sticky
                    # runtime halt/expired lease will be visible to status and
                    # the supervisor; never attempt a force-release here.
                    pass
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    except Exception as error:
        return _failure("run", error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-harness-executor",
        description=(
            "Isolated attended TESTNET executor controls. Mainnet is hard-disabled."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("validate", "validate and fingerprint a strict TESTNET config"),
        ("init", "initialize config-bound local state without credentials/network"),
        ("status", "read verified redacted executor status"),
        (
            "dry-run",
            "inspect the next lane without credentials, network, or runtime transition",
        ),
    ):
        selected = commands.add_parser(name, help=help_text)
        selected.add_argument("--config", type=_absolute_path, required=True)

    show = commands.add_parser("show-stage", help="review an exact staged ticket")
    show.add_argument("--config", type=_absolute_path, required=True)
    show.add_argument("--document-id", required=True)

    authorize = commands.add_parser(
        "authorize-stage",
        help="directly confirm and queue one staged TESTNET bracket",
    )
    authorize.add_argument("--config", type=_absolute_path, required=True)
    authorize.add_argument("--grant", type=_absolute_path, required=True)
    authorize.add_argument("--document-id", required=True)
    authorize.add_argument("--approver-id", required=True)

    acknowledge = commands.add_parser(
        "acknowledge-halt",
        help="attend and clear one exact stale manual halt without enabling entry",
    )
    acknowledge.add_argument("--config", type=_absolute_path, required=True)
    acknowledge.add_argument("--expected-revision", type=int, required=True)
    acknowledge.add_argument(
        "--expected-reason",
        choices=tuple(item.value for item in ManualHaltReason),
        required=True,
    )

    issue = commands.add_parser(
        "issue-grant",
        help="directly confirm and create one non-overwriting signed TESTNET grant",
    )
    issue.add_argument("--config", type=_absolute_path, required=True)
    issue.add_argument("--output", type=_absolute_path, required=True)
    issue.add_argument("--grant-id", required=True)
    issue.add_argument("--generation", type=int, default=1)
    issue.add_argument("--ttl-seconds", type=int, default=3600)

    run = commands.add_parser("run", help="run the isolated TESTNET worker")
    run.add_argument("--config", type=_absolute_path, required=True)
    run.add_argument("--instance-id")
    run.add_argument("--worker-id")
    run.add_argument("--max-drain-steps", type=int, default=20)
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "validate":
        return _validate(arguments.config)
    if arguments.command == "init":
        return _init(arguments.config)
    if arguments.command == "status":
        return _status(arguments.config)
    if arguments.command == "dry-run":
        return _dry_run(arguments.config)
    if arguments.command == "show-stage":
        return _show_stage(arguments.config, arguments.document_id)
    if arguments.command == "acknowledge-halt":
        return _acknowledge_halt(
            arguments.config,
            arguments.expected_revision,
            arguments.expected_reason,
        )
    if arguments.command == "authorize-stage":
        return _authorize_stage(
            arguments.config,
            arguments.grant,
            arguments.document_id,
            arguments.approver_id,
        )
    if arguments.command == "issue-grant":
        return _issue_grant(
            arguments.config,
            arguments.output,
            arguments.grant_id,
            arguments.generation,
            arguments.ttl_seconds,
        )
    return _run(
        arguments.config,
        arguments.instance_id,
        arguments.worker_id,
        arguments.max_drain_steps,
    )


def _require_command_identity(arguments: argparse.Namespace) -> None:
    if arguments.command == "validate":
        return
    config = _load(arguments.config)
    control_commands = frozenset(
        {"show-stage", "authorize-stage", "acknowledge-halt", "issue-grant"}
    )
    if arguments.command in control_commands:
        expected_uid = config.control_uid
        role = "control"
    else:
        expected_uid = config.executor_uid
        role = "executor"
    if not hasattr(os, "geteuid") or os.geteuid() != expected_uid:
        raise ValidationError(f"command requires the configured {role} UID")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        try:
            _require_command_identity(arguments)
        except Exception as error:
            return _failure(arguments.command, error)
        return _dispatch(arguments)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
