"""Application service behind the Codex/ChatGPT research tools.

The service owns local research-state writes and public market reads.  It has
no account credential, approval, signer, or venue-write dependency.  Every
directional result remains advisory until separately governed historical and
prospective profitability attestations exist.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Any
from urllib.parse import urlparse

from .analysis import TechnicalConfig, analyze_technical
from .backtest import CostModel, validate_profitability
from .canonical import canonical_data, domain_hash
from .domain import Environment
from .errors import RecordNotFound, StateConflict, ValidationError
from .history import CandleHistory, fetch_candle_history, interval_duration_ms
from .learning_bridge import LearningRecorder
from .node import node_status
from .registered_decision import build_registered_assessment
from .research_store import ResearchStore
from .sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
    sentiment_snapshot_from_dict,
)
from .strategy import SignalDirection, latest_signal
from .strategy_adapters import backtest_candles, live_scan_candles
from .tracking import MarketDataNetwork, TrackedAsset, TrackingStatus


Clock = Callable[[], datetime]
HistoryReader = Callable[[TrackedAsset, int, int, datetime], CandleHistory]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise ValidationError(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from error
    return _utc(parsed, field)


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be non-empty trimmed text")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


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


class ResearchService:
    """Narrow local-state and public-research operations for model adapters."""

    def __init__(
        self,
        store: ResearchStore,
        *,
        clock: Clock = _clock,
        history_reader: HistoryReader = _default_history_reader,
        analysis_bars: int = 1_200,
        validation_bars: int = 4_999,
        learning_recorder: LearningRecorder | None = None,
    ) -> None:
        if not isinstance(store, ResearchStore):
            raise TypeError("store must be ResearchStore")
        if not callable(clock) or not callable(history_reader):
            raise TypeError("clock and history_reader must be callable")
        for field, value in (
            ("analysis_bars", analysis_bars),
            ("validation_bars", validation_bars),
        ):
            if type(value) is not int or not 1_001 <= value <= 5_000:
                raise ValidationError(f"{field} must be an integer from 1001 to 5000")
        if learning_recorder is not None and not isinstance(
            learning_recorder, LearningRecorder
        ):
            raise TypeError("learning_recorder must be LearningRecorder or None")
        self.store = store
        self.clock = clock
        self.history_reader = history_reader
        self.analysis_bars = analysis_bars
        self.validation_bars = validation_bars
        self.technical_config = TechnicalConfig()
        self.learning_recorder = learning_recorder

    def _now(self) -> datetime:
        try:
            return _utc(self.clock(), "research service clock")
        except Exception as error:
            if isinstance(error, ValidationError):
                raise
            raise ValidationError(
                f"research service clock failed: {type(error).__name__}"
            ) from error

    @staticmethod
    def _history_bounds(asset: TrackedAsset, at: datetime, bars: int) -> tuple[int, int]:
        duration = interval_duration_ms(asset.interval)
        now_ms = int(at.timestamp() * 1000)
        current_open = now_ms - now_ms % duration
        last_completed = current_open - duration
        start = last_completed - (bars - 1) * duration
        if start < 0:
            raise ValidationError("requested history predates the Unix epoch")
        return start, current_open

    def _history(self, asset: TrackedAsset, *, at: datetime, bars: int) -> CandleHistory:
        start, end = self._history_bounds(asset, at, bars)
        result = self.history_reader(asset, start, end, at)
        if not isinstance(result, CandleHistory):
            raise TypeError("history_reader must return CandleHistory")
        if (
            result.symbol != asset.symbol
            or result.interval != asset.interval
            or result.network != asset.market_data_network.value
        ):
            raise StateConflict("history result does not match tracked asset")
        if not result.coverage_complete or result.truncated or len(result.candles) != bars:
            raise StateConflict("history result lacks complete requested coverage")
        return result

    def track_asset(
        self,
        *,
        asset_id: object,
        symbol: object,
        network: object,
        sentiment_query: object,
        poll_seconds: object = 60,
    ) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        checked_symbol = _text(symbol, "symbol", maximum=64)
        if not _SYMBOL_RE.fullmatch(checked_symbol):
            raise ValidationError("symbol must be a canonical Hyperliquid symbol")
        checked_query = _text(sentiment_query, "sentiment_query", maximum=1024)
        try:
            checked_network = MarketDataNetwork(network)
        except (TypeError, ValueError) as error:
            raise ValidationError("network must be mainnet or testnet") from error
        if type(poll_seconds) is not int or not 10 <= poll_seconds <= 86_400:
            raise ValidationError("poll_seconds must be an integer from 10 to 86400")
        try:
            existing = self.store.get_tracked_asset(checked_id)
        except RecordNotFound:
            existing = None
        if existing is not None:
            same = (
                existing.venue == "hyperliquid"
                and existing.market_data_network is checked_network
                and existing.execution_environment is Environment.SHADOW
                and existing.symbol == checked_symbol
                and existing.interval == "4h"
                and existing.poll_seconds == poll_seconds
                and existing.technical_profile_version == self.technical_config.version
                and existing.sentiment_policy_version == SentimentPolicy().version
                and existing.sentiment_query == checked_query
                and existing.sentiment_query_version == "manual-query-v1"
            )
            if not same:
                raise StateConflict("tracked asset ID already has different configuration")
            return {
                "tracked_asset": existing.as_dict(),
                "local_state_updated": False,
                "trade_authority_created": False,
                "order_submitted": False,
            }
        now = self._now()
        asset = TrackedAsset(
            asset_id=checked_id,
            venue="hyperliquid",
            market_data_network=checked_network,
            execution_environment=Environment.SHADOW,
            symbol=checked_symbol,
            interval="4h",
            poll_seconds=poll_seconds,
            technical_profile_version=self.technical_config.version,
            sentiment_policy_version=SentimentPolicy().version,
            sentiment_query=checked_query,
            sentiment_query_version="manual-query-v1",
            status=TrackingStatus.ACTIVE,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        stored = self.store.upsert_tracked_asset(asset)
        return {
            "tracked_asset": stored.as_dict(),
            "local_state_updated": True,
            "trade_authority_created": False,
            "order_submitted": False,
        }

    def pause_asset(self, *, asset_id: object, expected_revision: object) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        if type(expected_revision) is not int or expected_revision <= 0:
            raise ValidationError("expected_revision must be a positive integer")
        current = self.store.get_tracked_asset(checked_id)
        at = self._now()
        if at <= current.updated_at:
            at = current.updated_at + timedelta(microseconds=1)
        paused = self.store.pause_tracked_asset(
            checked_id,
            expected_revision=expected_revision,
            at=at,
        )
        return {
            "tracked_asset": paused.as_dict(),
            "local_state_updated": True,
            "trade_authority_created": False,
            "order_submitted": False,
        }

    def list_assets(self) -> dict[str, Any]:
        assets = self.store.list_tracked_assets()
        return {
            "count": len(assets),
            "assets": [asset.as_dict() for asset in assets],
            "venue_writes_enabled": False,
        }

    def record_manual_sentiment(
        self,
        *,
        asset_id: object,
        window_start: object,
        window_end: object,
        evidence: object,
        excluded_count: object,
        collection_complete: object,
    ) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        tracked = self.store.get_tracked_asset(checked_id)
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise ValidationError("evidence must be an array")
        if len(evidence) > 100:
            raise ValidationError("evidence cannot exceed 100 records")
        fields = {
            "evidence_id",
            "post_id",
            "source_url",
            "author_hash",
            "content_hash",
            "cluster_hash",
            "published_at",
            "observed_at",
            "polarity",
        }
        items: list[SentimentEvidence] = []
        for index, raw in enumerate(evidence):
            if not isinstance(raw, Mapping) or set(raw) != fields:
                raise ValidationError(f"evidence[{index}] fields are unsupported")
            source_url = raw["source_url"]
            if not isinstance(source_url, str):
                raise ValidationError(f"evidence[{index}].source_url must be text")
            parsed_url = urlparse(source_url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
            ):
                raise ValidationError(
                    f"evidence[{index}].source_url must be an X post URL"
                )
            polarity = raw["polarity"]
            if polarity not in {"-1", "-0.5", "0", "0.5", "1"}:
                raise ValidationError(
                    f"evidence[{index}].polarity must use the manual rubric"
                )
            items.append(
                SentimentEvidence(
                    evidence_id=raw["evidence_id"],
                    post_id=raw["post_id"],
                    source_url=source_url,
                    author_hash=_hash(raw["author_hash"], f"evidence[{index}].author_hash"),
                    content_hash=_hash(raw["content_hash"], f"evidence[{index}].content_hash"),
                    cluster_hash=_hash(raw["cluster_hash"], f"evidence[{index}].cluster_hash"),
                    published_at=_parse_time(
                        raw["published_at"], f"evidence[{index}].published_at"
                    ),
                    observed_at=_parse_time(
                        raw["observed_at"], f"evidence[{index}].observed_at"
                    ),
                    polarity=polarity,
                )
            )
        if isinstance(excluded_count, bool) or not isinstance(excluded_count, int):
            raise ValidationError("excluded_count must be a non-negative integer")
        if type(collection_complete) is not bool:
            raise ValidationError("collection_complete must be boolean")
        snapshot = build_sentiment_snapshot(
            asset_id=tracked.asset_id,
            query=tracked.sentiment_query,
            query_version=tracked.sentiment_query_version,
            classifier_version="manual-codex-polarity-v1",
            method=CollectionMethod.MANUAL_BROWSER,
            window_start=_parse_time(window_start, "window_start"),
            window_end=_parse_time(window_end, "window_end"),
            collected_at=self._now(),
            evidence=items,
            excluded_count=excluded_count,
            collection_complete=collection_complete,
        )
        record = self.store.put_sentiment(snapshot, stored_at=snapshot.collected_at)
        return {
            "snapshot": snapshot.as_dict(),
            "record_hash": record.record_hash,
            "local_state_updated": True,
            "unattended_eligible": False,
            "order_submitted": False,
        }

    def latest_sentiment(self, asset_id: object) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        self.store.get_tracked_asset(checked_id)
        records = self.store.list_artifacts(
            checked_id,
            "sentiment",
            limit=1,
            ascending=False,
        )
        if not records:
            return {"found": False, "asset_id": checked_id, "snapshot": None}
        snapshot = sentiment_snapshot_from_dict(records[0].payload)
        return {
            "found": True,
            "asset_id": checked_id,
            "snapshot": snapshot.as_dict(),
            "record_hash": records[0].record_hash,
        }

    def _receipt_times(self, asset: TrackedAsset) -> dict[int, datetime]:
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
                raise StateConflict("persisted candle receipt metadata is invalid")
            opened = _parse_time(payload["open_time"], "persisted candle open_time")
            result[int(opened.timestamp() * 1000)] = record.stored_at
        return result

    def analyze_asset(self, asset_id: object) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        asset = self.store.get_tracked_asset(checked_id)
        if asset.status is not TrackingStatus.ACTIVE:
            raise StateConflict("tracked asset is paused")
        at = self._now()
        history = self._history(asset, at=at, bars=self.analysis_bars)
        technical = analyze_technical(
            history.technical_candles(),
            as_of=at,
            config=self.technical_config,
        )
        signal = latest_signal(
            live_scan_candles(
                history,
                receipt_times=self._receipt_times(asset) or None,
            )
        )
        if signal is None:
            raise StateConflict("registered strategy did not produce a warmed-up classification")
        latest = self.latest_sentiment(checked_id)
        if latest["found"]:
            snapshot = sentiment_snapshot_from_dict(latest["snapshot"])
            assessment = build_registered_assessment(
                assessment_id=f"assessment-{signal.signal_hash[:24]}",
                asset_id=asset.asset_id,
                signal=signal,
                sentiment=snapshot,
                profitability=None,
                at=at,
            ).as_dict()
        else:
            no_signal = signal.direction is SignalDirection.NOTHING
            assessment = {
                "schema_version": "registered_opportunity_assessment.v1",
                "assessment_id": None,
                "asset_id": asset.asset_id,
                "verdict": "nothing" if no_signal else "unavailable",
                "reason_codes": (
                    [signal.reason]
                    if no_signal
                    else ["sentiment_snapshot_missing"]
                ),
                "eligible_for_risk_quote": False,
                "eligible_to_trade": False,
                "approval_created": False,
                "order_submitted": False,
            }
        signal_document = canonical_data(signal)
        if not isinstance(signal_document, dict):
            raise TypeError("registered signal did not canonicalize to an object")
        signal_document["signal_hash"] = signal.signal_hash
        analysis: dict[str, Any] = {
            "schema_version": "asset_analysis.v1",
            "asset": asset.as_dict(),
            "observed_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "history": {
                "data_hash": history.data_hash,
                "completed_candles": len(history.candles),
                "coverage_complete": history.coverage_complete,
                "truncated": history.truncated,
            },
            "descriptive_technical": technical.as_dict(),
            "registered_signal": signal_document,
            "sentiment": latest,
            "assessment": assessment,
            "profitability_attested": False,
            "venue_writes_enabled": False,
            "order_submitted": False,
        }
        analysis_hash = domain_hash(
            "trading-harness/asset-analysis/v1",
            analysis,
        )
        analysis["analysis_hash"] = analysis_hash
        stored = self.store.put_asset_analysis(
            checked_id,
            analysis,
            stored_at=at,
        )
        analysis["analysis_record_hash"] = stored.record_hash
        analysis["learning_cycle_id"] = None
        analysis["learning_event_hash"] = None
        if self.learning_recorder is not None:
            cycle, event = self.learning_recorder.record_analysis(stored)
            analysis["learning_cycle_id"] = cycle.cycle_id
            analysis["learning_event_hash"] = event.event_hash
        return analysis

    def validate_candidate(self, asset_id: object) -> dict[str, Any]:
        checked_id = _text(asset_id, "asset_id", maximum=128)
        asset = self.store.get_tracked_asset(checked_id)
        if asset.interval != "4h":
            raise ValidationError("candidate-v0 validation requires a 4h asset")
        at = self._now()
        history = self._history(asset, at=at, bars=self.validation_bars)
        cost_model = CostModel(
            model_id="hyperliquid-conservative-v1",
            version="1",
            fee_bps_per_side=Decimal("5"),
            slippage_bps_per_side=Decimal("10"),
            holding_cost_bps_per_bar=Decimal("1"),
        )
        artifact = validate_profitability(backtest_candles(history), cost_model)
        return {
            "schema_version": "candidate_validation_summary.v1",
            "asset_id": asset.asset_id,
            "strategy_hash": artifact.strategy.registration_hash,
            "data_hash": artifact.data_hash,
            "artifact_hash": artifact.artifact_hash,
            "historical_status": artifact.promotion.status.value,
            "historical_reasons": list(artifact.promotion.reasons),
            "trade_count": artifact.base_run.metrics.trade_count,
            "expectancy_r": canonical_data(artifact.base_run.metrics.expectancy_r),
            "lower_95_r": canonical_data(
                artifact.base_run.metrics.bootstrap_lower_95_r
            ),
            "profit_factor": canonical_data(artifact.base_run.metrics.profit_factor),
            "max_drawdown_r": canonical_data(artifact.base_run.metrics.max_drawdown_r),
            "stress_expectancy_r": canonical_data(
                artifact.stress_run.metrics.expectancy_r
            ),
            "shadow_required": True,
            "deployment_qualified": False,
            "profit_guaranteed": False,
            "order_submitted": False,
        }

    def get_node_status(self, node_id: object = "trading-desk-research") -> dict[str, Any]:
        checked = _text(node_id, "node_id", maximum=128)
        return node_status(self.store, checked, at=self._now())


__all__ = ("ResearchService",)
