"""Deployable composition root for the isolated TESTNET executor process.

The observer/initializer paths never load credentials or call a venue.  The
active builder requires an already-loaded API-wallet object and an independent
recovery HMAC secret, then composes the reviewed stores, synchronizer,
reconcilers, safety controller, signers, one-shot dispatchers, and serialized
runtime.  Mainnet is not representable by ``ExecutorConfig`` or this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import re
from typing import Any

from .account_risk import AccountRiskLimits
from .account_safety_controller import TestnetAccountSafetyController
from .approval import TestnetRecoveryAuthority
from .daily_loss import DailyLossBinding, DailyLossLedger
from .dispatcher import ExecutionDispatcher
from .domain import Environment
from .errors import ValidationError
from .execution_store import ExecutionStore
from .execution_work_scanner import ExecutionWorkScanner
from .executor_config import ExecutorConfig
from .executor_state_binding import (
    state_file_size_limit as _state_file_size_limit,
    verify_state_database_binding as _verify_state_database_binding,
    verify_state_bindings as _verify_state_bindings,
    write_state_database_binding as _write_state_database_binding,
    write_state_bindings as _write_state_bindings,
)
from .executor_handlers import (
    TestnetExecutorHandlerSet,
    build_testnet_executor_handlers,
)
from .execution_learning_sync import (
    LearningProjectionError,
    ExecutionLearningProjector,
    ExecutionLearningSyncReport,
)
from .executor_runtime import ExecutorRuntime, RuntimeStep, RuntimeStepResult
from .executor_runtime_store import ExecutorRuntimeStore
from .hyperliquid_account import HyperliquidAccountSnapshot, fetch_account_snapshot
from .hyperliquid_loss_sync import (
    HyperliquidDailyLossSync,
    HyperliquidDailyLossSynchronizer,
    HyperliquidLossSyncError,
)
from .hyperliquid_reconcile import InfoTransport
from .hyperliquid_recovery import RecoveryKind
from .hyperliquid_recovery_reader import HyperliquidRecoveryVenueReader
from .hyperliquid_signer import (
    SignL1Action,
    SignerPolicy,
    SigningAccount,
    sign_protected_action,
)
from .hyperliquid_wire import HyperliquidNetwork
from .learning_ledger import LearningLedger
from .learning_bridge import LearningRecorder
from .market_data import get_market_brief, post_public_info
from .nonce import PersistentNonceAllocator
from .planning import RiskSizingPolicy
from .production_preparer import TestnetEntryPreparer
from .reconciliation_coordinator import (
    HyperliquidVenueReconciler,
    MainEntryReconciliationCoordinator,
)
from .recovery_dispatcher import (
    DurableRecoverySigner,
    RecoveryExecutionDispatcher,
)
from .recovery_reconciliation import RecoveryReconciliationCoordinator
from .staging_inbox import (
    TradeStagingInbox,
    TrustedQuoteDecision,
)


Clock = Callable[[], datetime]
AccountReader = Callable[[str, str], HyperliquidAccountSnapshot]
MarketReader = Callable[[str, str], Mapping[str, Any]]
_VERIFICATION_DIRECTORY_PREFIXES = (
    ".trading-sqlite-verify-",
    ".execution-store-verify-",
    ".executor-runtime-verify-",
)

def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _wallet_address(wallet: object) -> str:
    try:
        value = getattr(wallet, "address")
    except Exception as error:
        raise ValidationError("wallet address lookup failed") from error
    if not isinstance(value, str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{40}", value
    ):
        raise ValidationError("wallet must expose a valid public address")
    return value.lower()


def _state_files(config: ExecutorConfig) -> tuple[Path, ...]:
    return (
        config.paths.execution_database,
        config.paths.nonce_database,
        config.paths.daily_loss_database,
        config.paths.learning_database,
        config.paths.staging_database,
    )


def _core_state_files(config: ExecutorConfig) -> tuple[Path, ...]:
    return (
        config.paths.execution_database,
        config.paths.nonce_database,
        config.paths.daily_loss_database,
    )


def _shared_state_files(config: ExecutorConfig) -> tuple[Path, ...]:
    return (
        config.paths.learning_database,
        config.paths.staging_database,
    )


def _state_database_policies(
    config: ExecutorConfig,
) -> dict[Path, frozenset[int]]:
    executor_only = frozenset({config.executor_uid})
    return {
        config.paths.execution_database: frozenset(
            {config.executor_uid, config.control_uid}
        ),
        config.paths.nonce_database: executor_only,
        config.paths.daily_loss_database: executor_only,
        config.paths.learning_database: frozenset(
            {config.executor_uid, config.research_uid, config.control_uid}
        ),
        config.paths.staging_database: frozenset(
            {config.executor_uid, config.research_uid, config.control_uid}
        ),
    }


def _validate_state_database_layout(
    config: ExecutorConfig,
    database: Path,
    *,
    existing: bool,
) -> None:
    policies = _state_database_policies(config)
    try:
        sidecar_owners = policies[database]
    except KeyError as error:
        raise ValidationError("database is not a configured executor state path") from error
    expected_parent_mode = 0o700
    try:
        parent_metadata = database.parent.lstat()
    except OSError as error:
        raise ValidationError("executor state directory is unavailable") from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValidationError("executor state parent must be a real directory")
    if stat.S_IMODE(parent_metadata.st_mode) != expected_parent_mode:
        raise ValidationError(
            f"executor state directory must have mode {expected_parent_mode:04o}"
        )
    if parent_metadata.st_uid != config.executor_uid:
        raise ValidationError("executor state directory has an invalid owner")
    try:
        entries = tuple(database.parent.iterdir())
    except OSError as error:
        raise ValidationError("executor state directory cannot be inspected") from error
    if any(
        entry.name.startswith(_VERIFICATION_DIRECTORY_PREFIXES)
        for entry in entries
    ):
        raise ValidationError("stale SQLite verification directory requires review")

    artifacts = (
        (database, frozenset({config.executor_uid}), True),
        *(
            (
                Path(str(database) + suffix),
                sidecar_owners,
                False,
            )
            for suffix in ("-wal", "-shm", "-journal")
        ),
    )
    present: set[Path] = set()
    for path, allowed_owners, is_main_database in artifacts:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValidationError("executor state artifact is unavailable") from error
        present.add(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValidationError("executor state file must be a regular non-symlink")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValidationError("executor state file must have mode 0600")
        if metadata.st_uid not in allowed_owners:
            raise ValidationError("executor state file has an invalid owner")
        if metadata.st_size > _state_file_size_limit(config, database):
            raise ValidationError("executor state file exceeds its size limit")
        if is_main_database and metadata.st_size <= 0:
            raise ValidationError("executor state main database is empty")
    if existing and database not in present:
        raise ValidationError("executor state is not initialized")
    if database not in present and any(path in present for path, _, _ in artifacts[1:]):
        raise ValidationError("executor state sidecar has no main database")
    if not existing and present:
        raise ValidationError("executor state initialization requires empty paths")


def _validate_state_layout(
    config: ExecutorConfig,
    *,
    existing: bool,
    include_shared: bool = True,
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() != config.executor_uid:
        raise ValidationError("executor state requires the configured executor UID")
    selected_files = _core_state_files(config) + (
        _shared_state_files(config) if include_shared else ()
    )
    for database in selected_files:
        _validate_state_database_layout(config, database, existing=existing)

    socket_parent = config.paths.control_socket.parent
    try:
        metadata = socket_parent.lstat()
    except OSError as error:
        raise ValidationError("executor state directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("executor state parent must be a real directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValidationError("executor state directory must have mode 0700")
    if metadata.st_uid != config.executor_uid:
        raise ValidationError("executor state directory has an invalid owner")
    try:
        config.paths.control_socket.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ValidationError("control socket path is unavailable") from error
    else:
        raise ValidationError("control socket runtime is not implemented")

    if not existing:
        directories = {path.parent for path in selected_files} | {socket_parent}
        for directory in directories:
            try:
                has_entries = next(directory.iterdir(), None) is not None
            except OSError as error:
                raise ValidationError("executor state directory cannot be inspected") from error
            if has_entries:
                raise ValidationError("executor state initialization requires empty directories")


@dataclass(frozen=True, slots=True)
class ExecutorLocalState:
    config: ExecutorConfig
    execution_store: ExecutionStore
    runtime_store: ExecutorRuntimeStore
    daily_loss: DailyLossLedger
    scanner: ExecutionWorkScanner
    nonce_allocator: PersistentNonceAllocator
    learning: LearningLedger | None
    observer: ExecutorRuntime


def _reserve_new_state_files(config: ExecutorConfig) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for path in _state_files(config):
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise ValidationError("executor state file reservation failed") from error
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _compose_testnet_executor_state(
    config: ExecutorConfig,
    *,
    clock: Clock,
    must_exist: bool,
    include_shared: bool = True,
) -> ExecutorLocalState:
    previous_umask = os.umask(0o077)
    try:
        store = ExecutionStore(
            config.paths.execution_database,
            environment=Environment.TESTNET,
            account_id=config.account_id,
            max_reserved_loss=config.max_reserved_loss,
            max_reserved_notional=config.max_reserved_notional,
            must_exist=must_exist,
        )
        runtime_store = ExecutorRuntimeStore(
            config,
            clock=clock,
            must_exist=must_exist,
        )
        loss = DailyLossLedger(
            config.paths.daily_loss_database,
            binding=DailyLossBinding(
                account_id=config.account_id,
                environment=Environment.TESTNET,
                config_hash=config.config_hash,
                daily_loss_limit=config.daily_loss_limit,
                settlement_currency=config.settlement_currency,
            ),
            clock=clock,
            must_exist=must_exist,
        )
        nonce = PersistentNonceAllocator(
            config.paths.nonce_database,
            signer_address=config.api_wallet_address,
            network=HyperliquidNetwork.TESTNET,
            clock=clock,
            must_exist=must_exist,
        )
        learning: LearningLedger | None = None
        if include_shared:
            learning = LearningLedger(
                config.paths.learning_database,
                clock=clock,
                must_exist=must_exist,
            )
            TradeStagingInbox(
                config.paths.staging_database,
                quote_callback=lambda _request: TrustedQuoteDecision.blocked(
                    block_code="trusted_quote_profile_not_loaded"
                ),
                clock=clock,
                must_exist=must_exist,
            )
    finally:
        os.umask(previous_umask)
    scanner = ExecutionWorkScanner(store, clock=clock)
    observer = ExecutorRuntime(
        runtime_store=runtime_store,
        work_scanner=scanner,
        daily_loss=loss,
        instance_id=f"observer-{config.node_id}",
        worker_id=f"observer-{config.node_id}",
        clock=clock,
    )
    return ExecutorLocalState(
        config=config,
        execution_store=store,
        runtime_store=runtime_store,
        daily_loss=loss,
        scanner=scanner,
        nonce_allocator=nonce,
        learning=learning,
        observer=observer,
    )


def _open_shared_learning_state(
    config: ExecutorConfig,
    *,
    clock: Clock,
) -> LearningLedger:
    for database in _shared_state_files(config):
        _validate_state_database_layout(config, database, existing=True)
        _verify_state_database_binding(config, database)
    learning = LearningLedger(
        config.paths.learning_database,
        clock=clock,
        must_exist=True,
    )
    TradeStagingInbox(
        config.paths.staging_database,
        quote_callback=lambda _request: TrustedQuoteDecision.blocked(
            block_code="trusted_quote_profile_not_loaded"
        ),
        clock=clock,
        must_exist=True,
    )
    for database in _shared_state_files(config):
        _validate_state_database_layout(config, database, existing=True)
        _verify_state_database_binding(config, database)
    return learning


def initialize_testnet_executor_state(
    config: ExecutorConfig,
    *,
    clock: Clock = _clock,
) -> ExecutorLocalState:
    """Create/bind local TESTNET databases without credentials or network I/O."""

    if not isinstance(config, ExecutorConfig):
        raise TypeError("config must be ExecutorConfig")
    if not callable(clock):
        raise TypeError("clock must be callable")
    _validate_state_layout(config, existing=False)
    _reserve_new_state_files(config)
    state = _compose_testnet_executor_state(
        config,
        clock=clock,
        must_exist=False,
    )
    _write_state_bindings(config)
    _validate_state_layout(config, existing=True)
    _verify_state_bindings(config)
    return state


def open_testnet_executor_state(
    config: ExecutorConfig,
    *,
    clock: Clock = _clock,
) -> ExecutorLocalState:
    """Open already-initialized state; never create a missing deployment."""

    if not isinstance(config, ExecutorConfig):
        raise TypeError("config must be ExecutorConfig")
    if not callable(clock):
        raise TypeError("clock must be callable")
    _validate_state_layout(config, existing=True, include_shared=False)
    for database in _core_state_files(config):
        _verify_state_database_binding(config, database)
    state = _compose_testnet_executor_state(
        config,
        clock=clock,
        must_exist=True,
        include_shared=False,
    )
    try:
        learning = _open_shared_learning_state(config, clock=clock)
    except Exception:
        # Shared research evidence is non-capital-authoritative. Any failure at
        # this trust boundary blocks learning and entry, but must not block
        # independently verified reconciliation or account-safety recovery.
        learning = None
    state = replace(state, learning=learning)
    _validate_state_layout(config, existing=True, include_shared=False)
    for database in _core_state_files(config):
        _verify_state_database_binding(config, database)
    return state


@dataclass(frozen=True, slots=True)
class ActiveExecutorCycle:
    loss_sync: HyperliquidDailyLossSync | None
    loss_sync_failed: bool
    loss_sync_skipped_for_priority: bool
    learning_sync: ExecutionLearningSyncReport | None
    learning_sync_failed: bool
    learning_sync_skipped_for_priority: bool
    runtime_step: RuntimeStepResult

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "active_testnet_executor_cycle.v1",
            "loss_sync": None if self.loss_sync is None else self.loss_sync.as_dict(),
            "loss_sync_failed": self.loss_sync_failed,
            "loss_sync_skipped_for_priority": self.loss_sync_skipped_for_priority,
            "learning_sync": (
                None if self.learning_sync is None else self.learning_sync.as_dict()
            ),
            "learning_sync_failed": self.learning_sync_failed,
            "learning_sync_skipped_for_priority": (
                self.learning_sync_skipped_for_priority
            ),
            "runtime_step": self.runtime_step.as_dict(),
            "environment": "testnet",
            "mainnet_authorized": False,
        }


class _UnavailableLearningProjector:
    def synchronize(self) -> ExecutionLearningSyncReport:
        raise LearningProjectionError("shared learning state is unavailable")

    def require_entry_ready(self, _command_id: str) -> None:
        raise LearningProjectionError(
            "entry is blocked while shared learning state is unavailable"
        )


@dataclass(slots=True)
class ActiveTestnetExecutorService:
    state: ExecutorLocalState
    handlers: TestnetExecutorHandlerSet
    loss_synchronizer: HyperliquidDailyLossSynchronizer
    learning_projector: ExecutionLearningProjector | _UnavailableLearningProjector
    runtime: ExecutorRuntime
    clock: Clock
    _last_loss_sync_at: datetime | None = field(default=None, init=False)

    def start(self):
        return self.runtime.start()

    def tick(self) -> ActiveExecutorCycle:
        report: HyperliquidDailyLossSync | None = None
        failed = False
        preview = self.runtime.dry_run()
        urgent_steps = {
            RuntimeStep.RECOVERY_RECONCILE,
            RuntimeStep.RECOVERY_WAIT,
            RuntimeStep.PARENT_RECONCILE,
            RuntimeStep.PARENT_WAIT,
            RuntimeStep.PROTECTION_CHECK,
            RuntimeStep.SAFETY_ACTION,
            RuntimeStep.RECOVERY_DISPATCH,
            RuntimeStep.STARTUP_RECONCILE,
            RuntimeStep.SHUTDOWN_DRAIN,
        }
        try:
            clock_value = self.clock()
        except Exception as error:
            raise ValidationError("active executor service clock failed") from error
        if (
            not isinstance(clock_value, datetime)
            or clock_value.tzinfo is None
            or clock_value.utcoffset() is None
        ):
            raise ValidationError("active executor service clock must be timezone-aware")
        now = clock_value.astimezone(timezone.utc)
        due = self._last_loss_sync_at is None or (
            now - self._last_loss_sync_at
            >= timedelta(milliseconds=self.state.config.reconcile_interval_ms)
        )
        entry_requires_refresh = preview.step is RuntimeStep.ENTRY_DISPATCH
        loss_block_requires_refresh = (
            preview.step is RuntimeStep.LOSS_BLOCKED
            and (
                self._last_loss_sync_at is None
                or now - self._last_loss_sync_at >= timedelta(seconds=5)
            )
        )
        skipped = preview.step in urgent_steps
        if not skipped and (
            due or entry_requires_refresh or loss_block_requires_refresh
        ):
            try:
                report = self.loss_synchronizer.synchronize()
            except HyperliquidLossSyncError:
                # The runtime still gets a turn so existing exposure can
                # reconcile or recover.  The loss gate cannot dispatch risk.
                failed = True
            if report is not None and report.complete:
                self._last_loss_sync_at = now
            elif entry_requires_refresh:
                failed = True
        learning_report: ExecutionLearningSyncReport | None = None
        learning_failed = False
        learning_skipped = skipped
        if not skipped:
            try:
                learning_report = self.learning_projector.synchronize()
            except Exception:
                learning_failed = True
        runtime_step = self.runtime.tick(
            entry_refresh_permitted=(
                report is not None
                and report.complete
                and not failed
                and learning_report is not None
                and not learning_failed
            )
        )
        post_step_urgent = (
            runtime_step.venue_write_attempted
            or runtime_step.step in urgent_steps
        )
        if not skipped and not post_step_urgent:
            try:
                learning_report = self.learning_projector.synchronize()
            except Exception:
                learning_failed = True
        elif post_step_urgent:
            learning_skipped = True
        return ActiveExecutorCycle(
            loss_sync=report,
            loss_sync_failed=failed,
            loss_sync_skipped_for_priority=skipped,
            learning_sync=learning_report,
            learning_sync_failed=learning_failed,
            learning_sync_skipped_for_priority=learning_skipped,
            runtime_step=runtime_step,
        )


def build_active_testnet_executor_service(
    *,
    state: ExecutorLocalState,
    wallet: object,
    recovery_secret: bytes,
    instance_id: str,
    worker_id: str,
    clock: Clock = _clock,
    policy: RiskSizingPolicy = RiskSizingPolicy(),
    account_reader: AccountReader | None = None,
    market_reader: MarketReader | None = None,
    info_transport: InfoTransport = post_public_info,
    sign_l1_action: SignL1Action | None = None,
) -> ActiveTestnetExecutorService:
    """Compose the real one-shot TESTNET write path behind the local runtime."""

    if not isinstance(state, ExecutorLocalState):
        raise TypeError("state must be ExecutorLocalState")
    config = state.config
    if config.environment is not Environment.TESTNET:
        raise ValidationError("active executor is TESTNET-only")
    if config.risk_policy_hash != policy.policy_hash:
        raise ValidationError("installed risk policy differs from executor config")
    if _wallet_address(wallet) != config.api_wallet_address:
        raise ValidationError("wallet differs from configured API-wallet address")
    if not isinstance(recovery_secret, bytes) or len(recovery_secret) < 32:
        raise ValidationError("recovery secret must contain at least 32 bytes")
    if not callable(clock) or not callable(info_transport):
        raise TypeError("clock and info_transport must be callable")
    selected_account_reader = account_reader or (
        lambda address, network: fetch_account_snapshot(
            address,
            network,
            transport=info_transport,
            clock=clock,
        )
    )
    selected_market_reader = market_reader or (
        lambda symbol, network: get_market_brief(
            symbol,
            network,
            transport=info_transport,
            clock=clock,
        )
    )
    signing_account = SigningAccount(
        account_id=config.account_id,
        main_account_address=config.main_account_address,
        signer_address=config.api_wallet_address,
        owned_cloids=frozenset(config.recovery_cloids),
    )
    signer_policy = SignerPolicy(
        accounts=(signing_account,),
        allowed_asset_ids=frozenset(config.allowed_asset_ids),
        allowed_networks=frozenset({HyperliquidNetwork.TESTNET}),
        allowed_recovery_kinds=frozenset(
            {
                RecoveryKind.REDUCE_ONLY_CLOSE,
                RecoveryKind.CANCEL_BY_CLOID,
                RecoveryKind.NOOP_FENCE,
            }
        ),
    )
    recovery_authority = TestnetRecoveryAuthority(
        recovery_secret,
        key_id=config.recovery_credential.account,
        issuer_id=f"{config.node_id}-account-safety",
        audience=f"{config.node_id}-recovery-worker",
    )
    safety = TestnetAccountSafetyController(
        state.execution_store,
        signer_policy=signer_policy,
        recovery_authority=recovery_authority,
    )
    main_coordinator = MainEntryReconciliationCoordinator(
        state.execution_store,
        network=HyperliquidNetwork.TESTNET,
        clock=clock,
    )
    recovery_coordinator = RecoveryReconciliationCoordinator(
        state.execution_store,
        clock=clock,
    )
    handlers = build_testnet_executor_handlers(
        store=state.execution_store,
        account_reader=selected_account_reader,
        main_coordinator=main_coordinator,
        venue_reconciler=HyperliquidVenueReconciler(
            transport=info_transport,
            clock=clock,
        ),
        recovery_coordinator=recovery_coordinator,
        recovery_venue_reader=HyperliquidRecoveryVenueReader(
            state.execution_store,
            transport=info_transport,
            clock=clock,
        ),
        safety_controller=safety,
        worker_id=worker_id,
        market_brief_reader=selected_market_reader,
        clock=clock,
    )
    limits = AccountRiskLimits(
        account_id=config.account_id,
        main_account_address=config.main_account_address,
        environment=Environment.TESTNET,
        daily_loss_limit=config.daily_loss_limit,
        aggregate_open_risk_limit=config.max_reserved_loss,
        max_notional=config.max_reserved_notional,
        leverage=config.max_leverage,
    )
    entry_preparer = TestnetEntryPreparer(
        state.execution_store,
        main_account_address=config.main_account_address,
        limits=limits,
        policy=policy,
        clock=clock,
        account_reader=selected_account_reader,
        market_reader=selected_market_reader,
        daily_loss_reader=lambda _at: state.daily_loss.latest_complete_snapshot(
            maximum_age_seconds=policy.account_max_age_seconds
        ).used,
    )
    learning_projector: ExecutionLearningProjector | _UnavailableLearningProjector
    if state.learning is None:
        learning_projector = _UnavailableLearningProjector()
    else:
        learning_projector = ExecutionLearningProjector(
            state.execution_store,
            LearningRecorder(state.learning),
            settlement_asset=config.settlement_currency,
        )

    def learning_bound_preparer(command, ticket, plan, requested_at):
        learning_projector.require_entry_ready(command.command_id)
        return entry_preparer(command, ticket, plan, requested_at)

    def entry_signer(protected, plan, metadata, preflight):
        return sign_protected_action(
            protected,
            plan=plan,
            metadata=metadata,
            preflight=preflight,
            policy=signer_policy,
            wallet=wallet,
            nonce_allocator=state.nonce_allocator,
            clock=clock,
            sign_l1_action=sign_l1_action,
        )

    entry_dispatcher = ExecutionDispatcher(
        state.execution_store,
        preparer=learning_bound_preparer,
        signer=entry_signer,
        clock=clock,
        lease_seconds=120,
    )
    recovery_dispatcher = RecoveryExecutionDispatcher(
        state.execution_store,
        worker_id=worker_id,
        preparer=handlers.safety_handler,
        signer=DurableRecoverySigner(
            policy=signer_policy,
            wallet=wallet,
            nonce_allocator=state.nonce_allocator,
            sign_l1_action=sign_l1_action,
        ),
        clock=clock,
    )
    runtime = ExecutorRuntime(
        runtime_store=state.runtime_store,
        work_scanner=state.scanner,
        daily_loss=state.daily_loss,
        instance_id=instance_id,
        worker_id=worker_id,
        recovery_dispatcher=recovery_dispatcher,
        entry_dispatcher=entry_dispatcher,
        clock=clock,
        **handlers.runtime_ports(),
    )
    entry_dispatcher.submission_guard = runtime.entry_submission_guard
    loss_sync = HyperliquidDailyLossSynchronizer(
        environment=Environment.TESTNET,
        account_id=config.account_id,
        main_account_address=config.main_account_address,
        config_hash=config.config_hash,
        settlement_currency=config.settlement_currency,
        ledger=state.daily_loss,
        transport=info_transport,
        clock=clock,
    )
    return ActiveTestnetExecutorService(
        state=state,
        handlers=handlers,
        loss_synchronizer=loss_sync,
        learning_projector=learning_projector,
        runtime=runtime,
        clock=clock,
    )


__all__ = (
    "ActiveExecutorCycle",
    "ActiveTestnetExecutorService",
    "ExecutorLocalState",
    "build_active_testnet_executor_service",
    "initialize_testnet_executor_state",
    "open_testnet_executor_state",
)
