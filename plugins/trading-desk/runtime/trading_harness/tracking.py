"""Versioned tracked-asset configuration for the always-on research node."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib

from .canonical import canonical_json
from .domain import Environment
from .errors import ValidationError


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a non-empty, trimmed string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


class TrackingStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class MarketDataNetwork(str, Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"


@dataclass(frozen=True, slots=True)
class TrackedAsset:
    asset_id: str
    venue: str
    market_data_network: MarketDataNetwork
    execution_environment: Environment
    symbol: str
    interval: str
    poll_seconds: int
    technical_profile_version: str
    sentiment_policy_version: str
    sentiment_query: str
    sentiment_query_version: str
    status: TrackingStatus
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        limits = {
            "asset_id": 128,
            "venue": 32,
            "symbol": 64,
            "interval": 16,
            "technical_profile_version": 64,
            "sentiment_policy_version": 64,
            "sentiment_query": 1024,
            "sentiment_query_version": 64,
        }
        for field, maximum in limits.items():
            object.__setattr__(
                self,
                field,
                _text(getattr(self, field), field, maximum=maximum),
            )
        if not isinstance(self.market_data_network, MarketDataNetwork):
            try:
                object.__setattr__(
                    self,
                    "market_data_network",
                    MarketDataNetwork(self.market_data_network),
                )
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid market_data_network") from error
        if not isinstance(self.execution_environment, Environment):
            try:
                object.__setattr__(
                    self,
                    "execution_environment",
                    Environment(self.execution_environment),
                )
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid execution_environment") from error
        if not isinstance(self.status, TrackingStatus):
            try:
                object.__setattr__(self, "status", TrackingStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid tracking status") from error
        if type(self.poll_seconds) is not int or not 10 <= self.poll_seconds <= 86_400:
            raise ValidationError("poll_seconds must be from 10 to 86400")
        if type(self.revision) is not int or self.revision < 1:
            raise ValidationError("revision must be a positive integer")
        created = _utc(self.created_at, "created_at")
        updated = _utc(self.updated_at, "updated_at")
        if updated < created:
            raise ValidationError("updated_at cannot predate created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    @property
    def config_hash(self) -> str:
        payload = {
            "domain": "tracked-asset-v1",
            "asset_id": self.asset_id,
            "venue": self.venue,
            "market_data_network": self.market_data_network.value,
            "execution_environment": self.execution_environment.value,
            "symbol": self.symbol,
            "interval": self.interval,
            "poll_seconds": self.poll_seconds,
            "technical_profile_version": self.technical_profile_version,
            "sentiment_policy_version": self.sentiment_policy_version,
            "sentiment_query": self.sentiment_query,
            "sentiment_query_version": self.sentiment_query_version,
            "status": self.status.value,
            "revision": self.revision,
        }
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def revise(
        self,
        *,
        updated_at: datetime,
        status: TrackingStatus | None = None,
        poll_seconds: int | None = None,
        sentiment_query: str | None = None,
        sentiment_query_version: str | None = None,
    ) -> "TrackedAsset":
        return replace(
            self,
            status=self.status if status is None else status,
            poll_seconds=self.poll_seconds if poll_seconds is None else poll_seconds,
            sentiment_query=(
                self.sentiment_query if sentiment_query is None else sentiment_query
            ),
            sentiment_query_version=(
                self.sentiment_query_version
                if sentiment_query_version is None
                else sentiment_query_version
            ),
            revision=self.revision + 1,
            updated_at=updated_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "tracked_asset.v1",
            "asset_id": self.asset_id,
            "venue": self.venue,
            "market_data_network": self.market_data_network.value,
            "execution_environment": self.execution_environment.value,
            "symbol": self.symbol,
            "interval": self.interval,
            "poll_seconds": self.poll_seconds,
            "technical_profile_version": self.technical_profile_version,
            "sentiment_policy_version": self.sentiment_policy_version,
            "sentiment_query": self.sentiment_query,
            "sentiment_query_version": self.sentiment_query_version,
            "status": self.status.value,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "updated_at": self.updated_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "config_hash": self.config_hash,
        }


__all__ = (
    "MarketDataNetwork",
    "TrackedAsset",
    "TrackingStatus",
)
