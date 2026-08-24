"""Always-on, fenced research node for tracked assets.

The first node capability is deliberately ``research_only``.  It ingests
strict completed candles, persists immutable bars and descriptive TA, and
evaluates the frozen registered signal.  It cannot load credentials, create
an approval, sign, or submit an order; its persisted risk gate is always
``halted``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any

from .analysis import TechnicalConfig, TechnicalSnapshot, analyze_technical
from .errors import RecordNotFound, StateConflict, ValidationError
from .history import CandleHistory, fetch_candle_history, interval_duration_ms
from .research_store import NodeLeaseRecord, NodeRuntimeRecord, ResearchStore
from .strategy import StrategySignal, latest_signal
from .strategy_adapters import live_scan_candles
from .tracking import TrackedAsset, TrackingStatus


Clock = Callable[[], datetime]
HistoryReader = Callable[[TrackedAsset, int, int, datetime], CandleHistory]
def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _default_history_reader(
    asset: TrackedAsset,
    start_time_ms: int,
    end_time_ms: int,
    at: datetime,
) -> CandleHistory:
    return fetch_candle_history(
        asset.symbol,
        asset.interval,
        start_time_ms,
        end_time_ms,
        asset.market_data_network.value,
        clock=lambda: at,
    )


@dataclass(frozen=True, slots=True)
class AssetCycleResult:
    asset_id: str
    observed_at: datetime
    history_hash: str
    technical_hash: str
    signal_hash: str | None
    signal_direction: str | None
    candles_persisted: int
    status: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "observed_at": self.observed_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "history_hash": self.history_hash,
            "technical_hash": self.technical_hash,
            "signal_hash": self.signal_hash,
            "signal_direction": self.signal_direction,
            "candles_persisted": self.candles_persisted,
            "status": self.status,
            "reason": self.reason,
            "trade_authority": False,
        }


@dataclass(frozen=True, slots=True)
class NodeCycleResult:
    node_id: str
    instance_id: str
    observed_at: datetime
    assets: tuple[AssetCycleResult, ...]
    failures: tuple[dict[str, str], ...]
    process_state: str
    risk_gate: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "instance_id": self.instance_id,
            "observed_at": self.observed_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "assets": [asset.as_dict() for asset in self.assets],
            "failures": list(self.failures),
            "process_state": self.process_state,
            "capability": "research_only",
            "risk_gate": self.risk_gate,
            "credential_loading_enabled": False,
            "venue_writes_enabled": False,
        }


class ResearchNode:
    """One restart-safe scheduler process protected by a SQLite fencing lease."""

    def __init__(
        self,
        store: ResearchStore,
        *,
        node_id: str = "trading-desk-research",
        instance_id: str,
        clock: Clock = _default_clock,
        history_reader: HistoryReader = _default_history_reader,
        technical_config: TechnicalConfig = TechnicalConfig(),
        history_bars: int = 1_200,
        lease_ttl_seconds: int = 30,
        heartbeat_ttl_seconds: int = 20,
    ) -> None:
        if not isinstance(store, ResearchStore):
            raise TypeError("store must be ResearchStore")
        for field, value in (("node_id", node_id), ("instance_id", instance_id)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValidationError(f"{field} must be non-empty trimmed text")
        if not callable(clock) or not callable(history_reader):
            raise TypeError("clock and history_reader must be callable")
        if not isinstance(technical_config, TechnicalConfig):
            raise TypeError("technical_config must be TechnicalConfig")
        if type(history_bars) is not int or not 1_001 <= history_bars <= 5_000:
            raise ValidationError("history_bars must be an integer from 1001 to 5000")
        for field, value in (
            ("lease_ttl_seconds", lease_ttl_seconds),
            ("heartbeat_ttl_seconds", heartbeat_ttl_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValidationError(f"{field} must be a positive integer")
        if heartbeat_ttl_seconds > lease_ttl_seconds:
            raise ValidationError("heartbeat TTL cannot exceed the node lease TTL")
        self.store = store
        self.node_id = node_id
        self.instance_id = instance_id
        self.clock = clock
        self.history_reader = history_reader
        self.technical_config = technical_config
        self.history_bars = history_bars
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self._lease: NodeLeaseRecord | None = None
        self._runtime: NodeRuntimeRecord | None = None
        self._next_due: dict[str, datetime] = {}

    @property
    def started(self) -> bool:
        return self._lease is not None and self._runtime is not None

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception as error:
            raise ValidationError(f"node clock failed: {type(error).__name__}") from error
        return _utc(value, "node clock")

    def start(self, *, at: datetime | None = None) -> NodeRuntimeRecord:
        if self.started:
            raise StateConflict("research node is already started")
        now = self._now() if at is None else _utc(at, "at")
        lease = self.store.acquire_node_lease(
            self.node_id,
            self.instance_id,
            at=now,
            ttl_seconds=self.lease_ttl_seconds,
        )
        runtime = self.store.start_node_runtime(
            self.node_id,
            self.instance_id,
            lease.fencing_token,
            capability="research_only",
            at=now,
            details={"recovery": "complete", "execution": "disabled"},
        )
        runtime = self.store.update_node_runtime(
            self.node_id,
            self.instance_id,
            lease.fencing_token,
            expected_revision=runtime.revision,
            process_state="running",
            risk_gate="halted",
            at=now,
            details={"recovery": "complete", "execution": "disabled"},
        )
        self._lease = lease
        self._runtime = runtime
        return runtime

    def _renew(self, at: datetime) -> None:
        if self._lease is None:
            raise StateConflict("research node has no lease")
        self._lease = self.store.renew_node_lease(
            self.node_id,
            self.instance_id,
            self._lease.fencing_token,
            at=at,
            ttl_seconds=self.lease_ttl_seconds,
        )

    def _persist_new_candles(self, asset: TrackedAsset, history: CandleHistory, at: datetime) -> int:
        existing = self.store.list_artifacts(
            asset.asset_id,
            "candle",
            series_key=asset.interval,
            limit=1,
            ascending=False,
        )
        latest_open: str | None = None
        if existing:
            payload = existing[0].payload
            if not isinstance(payload, dict) or not isinstance(payload.get("open_time"), str):
                raise StateConflict("latest candle artifact payload is invalid")
            latest_open = payload["open_time"]
        persisted = 0
        for historical in history.candles:
            candle = historical.to_technical_candle()
            open_text = candle.canonical_record()["open_time"]
            if latest_open is not None and open_text <= latest_open:
                continue
            self.store.put_candle(asset.asset_id, candle, stored_at=at)
            persisted += 1
        return persisted

    def _candle_receipts(self, asset: TrackedAsset) -> dict[int, datetime]:
        records = self.store.list_artifacts(
            asset.asset_id,
            "candle",
            series_key=asset.interval,
            limit=5_000,
            ascending=True,
        )
        result: dict[int, datetime] = {}
        for record in records:
            payload = record.payload
            if not isinstance(payload, dict) or not isinstance(payload.get("open_time"), str):
                raise StateConflict("persisted candle artifact payload is invalid")
            try:
                opened = datetime.fromisoformat(
                    payload["open_time"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError as error:
                raise StateConflict("persisted candle open time is invalid") from error
            result[_time_ms(opened)] = record.stored_at
        return result

    def _persist_technical(
        self,
        asset: TrackedAsset,
        technical: TechnicalSnapshot,
        at: datetime,
    ) -> str:
        existing = self.store.list_artifacts(
            asset.asset_id,
            "technical",
            series_key=technical.config_version,
            limit=1,
            ascending=False,
        )
        if existing:
            payload = existing[0].payload
            if isinstance(payload, dict) and payload.get("data_hash") == technical.data_hash:
                return existing[0].semantic_hash
        return self.store.put_technical(
            asset.asset_id,
            technical,
            stored_at=at,
        ).semantic_hash

    def _run_asset(self, asset: TrackedAsset, at: datetime) -> AssetCycleResult:
        if asset.status is not TrackingStatus.ACTIVE:
            raise StateConflict("paused asset reached the active scheduler")
        if asset.technical_profile_version != self.technical_config.version:
            raise ValidationError("tracked technical profile is not installed")
        duration = interval_duration_ms(asset.interval)
        now_ms = _time_ms(at)
        current_open = now_ms - now_ms % duration
        last_completed = current_open - duration
        query_start = last_completed - (self.history_bars - 1) * duration
        if query_start < 0:
            raise ValidationError("requested history predates the Unix epoch")
        history = self.history_reader(asset, query_start, current_open, at)
        if (
            history.symbol != asset.symbol
            or history.interval != asset.interval
            or history.network != asset.market_data_network.value
        ):
            raise StateConflict("history artifact does not match tracked asset")
        if not history.coverage_complete or history.truncated:
            raise StateConflict("history coverage is incomplete or truncated")
        if len(history.candles) != self.history_bars:
            raise StateConflict("history does not contain the required completed bars")
        persisted = self._persist_new_candles(asset, history, at)
        technical = analyze_technical(
            history.technical_candles(),
            as_of=at,
            config=self.technical_config,
        )
        technical_hash = self._persist_technical(asset, technical, at)
        signal: StrategySignal | None = None
        if asset.interval == "4h":
            signal = latest_signal(
                live_scan_candles(
                    history,
                    receipt_times=self._candle_receipts(asset),
                )
            )
        return AssetCycleResult(
            asset_id=asset.asset_id,
            observed_at=at,
            history_hash=history.data_hash,
            technical_hash=technical_hash,
            signal_hash=None if signal is None else signal.signal_hash,
            signal_direction=None if signal is None else signal.direction.value,
            candles_persisted=persisted,
            status="available" if signal is not None else "descriptive_only",
            reason=(
                "registered_signal_calculated"
                if signal is not None
                else "registered_strategy_requires_4h_interval"
            ),
        )

    def run_cycle(self, *, at: datetime | None = None, force: bool = False) -> NodeCycleResult:
        if not self.started or self._lease is None or self._runtime is None:
            raise StateConflict("research node must start before running a cycle")
        if type(force) is not bool:
            raise TypeError("force must be bool")
        now = self._now() if at is None else _utc(at, "at")
        self._renew(now)
        assets: list[AssetCycleResult] = []
        failures: list[dict[str, str]] = []
        for tracked in self.store.list_tracked_assets(status=TrackingStatus.ACTIVE):
            due = self._next_due.get(tracked.asset_id)
            if not force and due is not None and now < due:
                continue
            try:
                result = self._run_asset(tracked, now)
                assets.append(result)
                self.store.heartbeat(
                    self.node_id,
                    f"asset:{tracked.asset_id}",
                    self.instance_id,
                    self._lease.fencing_token,
                    status="healthy",
                    at=now,
                    ttl_seconds=self.heartbeat_ttl_seconds,
                    details={
                        "history_hash": result.history_hash,
                        "technical_hash": result.technical_hash,
                        "signal_hash": result.signal_hash,
                        "signal_direction": result.signal_direction,
                    },
                )
            except Exception as error:
                failures.append(
                    {
                        "asset_id": tracked.asset_id,
                        "error_type": type(error).__name__,
                    }
                )
                self.store.heartbeat(
                    self.node_id,
                    f"asset:{tracked.asset_id}",
                    self.instance_id,
                    self._lease.fencing_token,
                    status="failed",
                    at=now,
                    ttl_seconds=self.heartbeat_ttl_seconds,
                    details={"error_type": type(error).__name__},
                )
            finally:
                self._next_due[tracked.asset_id] = now + timedelta(
                    seconds=tracked.poll_seconds
                )

        target_state = "degraded" if failures else "running"
        self._runtime = self.store.update_node_runtime(
            self.node_id,
            self.instance_id,
            self._lease.fencing_token,
            expected_revision=self._runtime.revision,
            process_state=target_state,
            risk_gate="halted",
            at=now,
            details={
                "active_assets": len(self.store.list_tracked_assets(status="active")),
                "processed_assets": len(assets),
                "failed_assets": len(failures),
                "execution": "disabled",
            },
        )
        self.store.heartbeat(
            self.node_id,
            "scheduler",
            self.instance_id,
            self._lease.fencing_token,
            status="degraded" if failures else "healthy",
            at=now,
            ttl_seconds=self.heartbeat_ttl_seconds,
            details={"processed": len(assets), "failed": len(failures)},
        )
        return NodeCycleResult(
            node_id=self.node_id,
            instance_id=self.instance_id,
            observed_at=now,
            assets=tuple(assets),
            failures=tuple(failures),
            process_state=self._runtime.process_state,
            risk_gate=self._runtime.risk_gate,
        )

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be threading.Event")
        if not isinstance(poll_seconds, (int, float)) or not 0.05 <= poll_seconds <= 60:
            raise ValidationError("poll_seconds must be from 0.05 to 60")
        if not self.started:
            self.start()
        try:
            while not stop_event.is_set():
                self.run_cycle()
                stop_event.wait(float(poll_seconds))
        finally:
            if self.started:
                self.stop()

    def stop(self, *, at: datetime | None = None) -> NodeRuntimeRecord:
        if not self.started or self._lease is None or self._runtime is None:
            raise StateConflict("research node is not started")
        now = self._now() if at is None else _utc(at, "at")
        self._renew(now)
        runtime = self.store.update_node_runtime(
            self.node_id,
            self.instance_id,
            self._lease.fencing_token,
            expected_revision=self._runtime.revision,
            process_state="stopping",
            risk_gate="halted",
            at=now,
            details={"execution": "disabled"},
        )
        runtime = self.store.update_node_runtime(
            self.node_id,
            self.instance_id,
            self._lease.fencing_token,
            expected_revision=runtime.revision,
            process_state="stopped",
            risk_gate="halted",
            at=now,
            details={"execution": "disabled"},
        )
        self.store.release_node_lease(
            self.node_id,
            self.instance_id,
            self._lease.fencing_token,
            at=now,
        )
        self._runtime = None
        self._lease = None
        self._next_due.clear()
        return runtime


def default_state_database() -> Path:
    """Return a credential-free per-user state path for the local node/plugin."""

    return Path.home() / ".local" / "state" / "trading-harness" / "research.sqlite3"


def node_status(store: ResearchStore, node_id: str, *, at: datetime) -> dict[str, Any]:
    """Read one persisted node status without starting or mutating it."""

    checked_at = _utc(at, "at")
    try:
        runtime = store.get_node_runtime(node_id)
        lease = store.get_node_lease(node_id)
    except RecordNotFound:
        return {
            "node_id": node_id,
            "available": False,
            "process_state": "stopped",
            "capability": "research_only",
            "risk_gate": "halted",
            "venue_writes_enabled": False,
            "credential_loading_enabled": False,
            "heartbeats": [],
        }
    heartbeats = store.list_heartbeats(node_id)
    return {
        "node_id": node_id,
        "available": lease.is_active(checked_at) and runtime.process_state == "running",
        "instance_id": runtime.instance_id,
        "generation": runtime.generation,
        "revision": runtime.revision,
        "process_state": runtime.process_state,
        "capability": runtime.capability,
        "risk_gate": runtime.risk_gate,
        "lease_active": lease.is_active(checked_at),
        "updated_at": runtime.updated_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "venue_writes_enabled": False,
        "credential_loading_enabled": False,
        "heartbeats": [
            {
                "component": heartbeat.component,
                "status": heartbeat.status,
                "fresh": heartbeat.is_fresh(checked_at),
                "observed_at": heartbeat.observed_at.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "valid_until": heartbeat.valid_until.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "details": dict(heartbeat.details),
            }
            for heartbeat in heartbeats
        ],
    }


__all__ = (
    "AssetCycleResult",
    "NodeCycleResult",
    "ResearchNode",
    "default_state_database",
    "node_status",
)
