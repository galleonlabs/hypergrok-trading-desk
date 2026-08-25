"""Deployable composition root for the isolated TESTNET executor process.

The observer/initializer paths never load credentials or call a venue.  The
active builder requires an already-loaded API-wallet object and an independent
recovery HMAC secret, then composes the reviewed stores, synchronizer,
reconcilers, safety controller, signers, one-shot dispatchers, and serialized
runtime.  Mainnet is not representable by ``ExecutorConfig`` or this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
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
from .executor_handlers import (
    TestnetExecutorHandlerSet,
    build_testnet_executor_handlers,
)
from .execution_learning_sync import (
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
from .staging_inbox import TradeStagingInbox, TrustedQuoteDecision


Clock = Callable[[], datetime]
AccountReader = Callable[[str, str], HyperliquidAccountSnapshot]
MarketReader = Callable[[str, str], Mapping[str, Any]]


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


def _state_artifacts(config: ExecutorConfig) -> tuple[Path, ...]:
    result: list[Path] = []
    for database in _state_files(config):
        result.append(database)
        result.extend(
            Path(str(database) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        )
    return tuple(result)


def _validate_state_layout(config: ExecutorConfig, *, existing: bool) -> None:
    directories = {path.parent for path in _state_files(config)} | {
        config.paths.control_socket.parent
    }
    for directory in directories:
        try:
            metadata = directory.stat()
        except OSError as error:
            raise ValidationError("executor state directory is unavailable") from error
        if not directory.is_dir() or directory.is_symlink():
            raise ValidationError("executor state parent must be a real directory")
        if metadata.st_mode & 0o077:
            raise ValidationError("executor state directory must have mode 0700")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValidationError("executor state directory must be process-owned")
    for path in _state_artifacts(config):
        if not path.exists():
            if existing and path in _state_files(config):
                raise ValidationError("executor state is not initialized")
            continue
        if path.is_symlink() or not path.is_file():
            raise ValidationError("executor state file must be a regular non-symlink")
        metadata = path.stat()
        if existing and metadata.st_mode & 0o077:
            raise ValidationError("executor state file must have mode 0600")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValidationError("executor state file must be process-owned")


@dataclass(frozen=True, slots=True)
class ExecutorLocalState:
    config: ExecutorConfig
    execution_store: ExecutionStore
    runtime_store: ExecutorRuntimeStore
    daily_loss: DailyLossLedger
    scanner: ExecutionWorkScanner
    nonce_allocator: PersistentNonceAllocator
    learning: LearningLedger
    observer: ExecutorRuntime


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
    previous_umask = os.umask(0o077)
    try:
        store = ExecutionStore(
            config.paths.execution_database,
            environment=Environment.TESTNET,
            account_id=config.account_id,
            max_reserved_loss=config.max_reserved_loss,
            max_reserved_notional=config.max_reserved_notional,
        )
        runtime_store = ExecutorRuntimeStore(config, clock=clock)
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
        )
        nonce = PersistentNonceAllocator(
            config.paths.nonce_database,
            signer_address=config.api_wallet_address,
            network=HyperliquidNetwork.TESTNET,
            clock=clock,
        )
        learning = LearningLedger(config.paths.learning_database, clock=clock)
        TradeStagingInbox(
            config.paths.staging_database,
            quote_callback=lambda _request: TrustedQuoteDecision.blocked(
                block_code="trusted_quote_profile_not_loaded"
            ),
            clock=clock,
        )
    finally:
        os.umask(previous_umask)
    for path in _state_artifacts(config):
        if not path.exists():
            continue
        try:
            path.chmod(0o600)
        except OSError as error:
            raise ValidationError("executor state permissions could not be set") from error
    _validate_state_layout(config, existing=True)
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


def open_testnet_executor_state(
    config: ExecutorConfig,
    *,
    clock: Clock = _clock,
) -> ExecutorLocalState:
    """Open already-initialized state; never create a missing deployment."""

    _validate_state_layout(config, existing=True)
    return initialize_testnet_executor_state(config, clock=clock)


@dataclass(frozen=True, slots=True)
class ActiveExecutorCycle:
    loss_sync: HyperliquidDailyLossSync | None
    loss_sync_failed: bool
    loss_sync_skipped_for_priority: bool
    learning_sync: ExecutionLearningSyncReport | None
    learning_sync_failed: bool
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
            "runtime_step": self.runtime_step.as_dict(),
            "environment": "testnet",
            "mainnet_authorized": False,
        }


@dataclass(slots=True)
class ActiveTestnetExecutorService:
    state: ExecutorLocalState
    handlers: TestnetExecutorHandlerSet
    loss_synchronizer: HyperliquidDailyLossSynchronizer
    learning_projector: ExecutionLearningProjector
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
        try:
            learning_report = self.learning_projector.synchronize()
        except Exception:
            learning_failed = True
        runtime_step = self.runtime.tick(
            entry_refresh_permitted=(
                report is not None and report.complete and not failed
            )
        )
        try:
            learning_report = self.learning_projector.synchronize()
        except Exception:
            learning_failed = True
        return ActiveExecutorCycle(
            loss_sync=report,
            loss_sync_failed=failed,
            loss_sync_skipped_for_priority=skipped,
            learning_sync=learning_report,
            learning_sync_failed=learning_failed,
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
