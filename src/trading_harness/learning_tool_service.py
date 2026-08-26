"""Trusted composition for the agent-facing TESTNET learning tool profile.

The resulting :class:`ToolService` still exposes no approval, execution-store,
credential, signer, nonce, dispatcher, or venue-write method.  This factory
only installs the exact risk policy, signed (but non-authoritative) grant scope,
complete daily-loss ledger, public account reader, immutable staging inbox,
and learning ledger required for ``stage_trade_candidate`` to return a real
non-authoritative TESTNET ticket instead of a configuration blocker.
The agent process never receives the symmetric grant key; MAC authentication
is deferred to the attended control plane before admission.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path

from .errors import ValidationError
from .execution_grant import SignedInfrastructureGrant
from .executor_config import ExecutorConfig
from .executor_service import (
    _validate_state_database_layout,
    _verify_state_database_binding,
)
from .hyperliquid_account import HyperliquidAccountSnapshot
from .learning_bridge import LearningRecorder
from .learning_ledger import LearningLedger
from .learning_quote_service import InfrastructureLearningQuoteService
from .planning import RiskSizingPolicy
from .research_api import ResearchService
from .research_store import ResearchStore
from .staging_inbox import TradeStagingInbox
from .tool_api import ToolService


Clock = Callable[[], datetime]
AccountReader = Callable[[str, str], HyperliquidAccountSnapshot]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _research_path(value: str | Path, config: ExecutorConfig) -> Path:
    selected = Path(value)
    if not selected.is_absolute():
        raise ValidationError("research database path must be absolute")
    if selected.exists() and selected.is_symlink():
        raise ValidationError("research database may not be a symlink")
    if not selected.parent.is_dir() or selected.parent.is_symlink():
        raise ValidationError("research database parent must be a real directory")
    parent_metadata = selected.parent.stat()
    if parent_metadata.st_mode & 0o077:
        raise ValidationError("research database parent must have mode 0700")
    if hasattr(os, "geteuid") and parent_metadata.st_uid != os.geteuid():
        raise ValidationError("research database parent must be process-owned")
    if selected.exists():
        metadata = selected.stat()
        if metadata.st_mode & 0o077:
            raise ValidationError("research database must have mode 0600")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValidationError("research database must be process-owned")
    resolved = selected.resolve(strict=False)
    managed = (
        config.paths.execution_database,
        config.paths.nonce_database,
        config.paths.daily_loss_database,
        config.paths.learning_database,
        config.paths.staging_database,
        config.paths.control_socket,
    )
    for path in managed:
        other = path.resolve(strict=False)
        try:
            aliases = selected.exists() and path.exists() and selected.samefile(path)
        except OSError as error:
            raise ValidationError("research database aliases cannot be verified") from error
        if resolved == other or aliases:
            raise ValidationError("research database must be separate from executor paths")
    return selected


def _shared_state_path(
    path: Path,
    *,
    label: str,
    config: ExecutorConfig,
) -> None:
    try:
        _validate_state_database_layout(config, path, existing=True)
        _verify_state_database_binding(config, path)
    except ValidationError as error:
        raise ValidationError(f"{label} state layout is invalid") from error


def build_testnet_learning_tool_service(
    *,
    config: ExecutorConfig,
    research_database: str | Path,
    signed_grant: SignedInfrastructureGrant,
    clock: Clock = _clock,
    account_reader: AccountReader | None = None,
    policy: RiskSizingPolicy = RiskSizingPolicy(),
) -> ToolService:
    """Build the configured non-authoritative Codex/OpenCode tool surface."""

    if not isinstance(config, ExecutorConfig):
        raise TypeError("config must be ExecutorConfig")
    if not isinstance(signed_grant, SignedInfrastructureGrant):
        raise TypeError("signed_grant must be SignedInfrastructureGrant")
    if not isinstance(policy, RiskSizingPolicy):
        raise TypeError("policy must be RiskSizingPolicy")
    if not callable(clock):
        raise TypeError("clock must be callable")
    if account_reader is not None and not callable(account_reader):
        raise TypeError("account_reader must be callable or None")
    try:
        now = clock()
    except Exception as error:
        raise ValidationError("learning tool profile clock failed") from error
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("learning tool profile clock must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if config.risk_policy_hash != policy.policy_hash:
        raise ValidationError("installed risk policy differs from executor configuration")
    selected_research = _research_path(research_database, config)
    _shared_state_path(
        config.paths.learning_database,
        label="learning",
        config=config,
    )
    _shared_state_path(
        config.paths.staging_database,
        label="staging",
        config=config,
    )
    learning = LearningLedger(
        config.paths.learning_database,
        clock=clock,
        must_exist=True,
    )
    recorder = LearningRecorder(learning)
    research_store = ResearchStore(selected_research)
    research = ResearchService(
        research_store,
        clock=clock,
        learning_recorder=recorder,
    )
    quote = InfrastructureLearningQuoteService(
        research_store,
        config=config,
        policy=policy,
        grant=signed_grant,
        account_reader=account_reader,
        clock=clock,
    )
    staging = TradeStagingInbox(
        config.paths.staging_database,
        quote_callback=quote,
        clock=clock,
        must_exist=True,
    )
    _shared_state_path(
        config.paths.learning_database,
        label="learning",
        config=config,
    )
    _shared_state_path(
        config.paths.staging_database,
        label="staging",
        config=config,
    )
    return ToolService(
        research_service=research,
        research_store_path=selected_research,
        staging_inbox=staging,
        learning_ledger_path=config.paths.learning_database,
        learning_ledger=learning,
        learning_quote_configured=True,
    )


__all__ = ("build_testnet_learning_tool_service",)
