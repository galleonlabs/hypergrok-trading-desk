"""Versioned sentiment evidence without browser automation or raw post storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
from typing import Iterable, Mapping
from urllib.parse import urlparse

from .canonical import canonical_decimal, canonical_json
from .errors import ValidationError
from .policy import exact_decimal


_ZERO = Decimal("0")
_ONE = Decimal("1")
_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN, Emin=-192, Emax=192)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a non-empty, trimmed string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    parsed = _text(value, field, maximum=64)
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return parsed


def _bounded_decimal(
    value: Decimal | str | int,
    field: str,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{field} must be between {minimum} and {maximum}")
    return parsed


class CollectionMethod(str, Enum):
    MANUAL_BROWSER = "manual_browser"
    X_API = "x_api"
    COMPLIANT_PROVIDER = "compliant_provider"


class SentimentLabel(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SentimentEvidence:
    evidence_id: str
    post_id: str
    source_url: str
    author_hash: str
    content_hash: str
    cluster_hash: str
    published_at: datetime
    observed_at: datetime
    polarity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "post_id", _text(self.post_id, "post_id"))
        url = _text(self.source_url, "source_url", maximum=2048)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError("source_url must be an absolute HTTPS URL")
        object.__setattr__(self, "source_url", url)
        for field in ("author_hash", "content_hash", "cluster_hash"):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        published = _utc(self.published_at, "published_at")
        observed = _utc(self.observed_at, "observed_at")
        if observed < published:
            raise ValidationError("observed_at cannot predate published_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(
            self,
            "polarity",
            _bounded_decimal(
                self.polarity,
                "polarity",
                minimum=Decimal("-1"),
                maximum=_ONE,
            ),
        )

    def canonical_record(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "post_id": self.post_id,
            "source_url": self.source_url,
            "author_hash": self.author_hash,
            "content_hash": self.content_hash,
            "cluster_hash": self.cluster_hash,
            "published_at": self.published_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "observed_at": self.observed_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "polarity": canonical_decimal(self.polarity),
        }


@dataclass(frozen=True, slots=True)
class SentimentPolicy:
    version: str = "sentiment-quality-v1"
    minimum_posts: int = 30
    minimum_authors: int = 20
    trim_fraction: Decimal = Decimal("0.1")
    bullish_threshold: Decimal = Decimal("0.15")
    bearish_threshold: Decimal = Decimal("-0.15")
    max_cluster_share: Decimal = Decimal("0.2")
    ttl_seconds: int = 900

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version", maximum=64))
        for field in ("minimum_posts", "minimum_authors", "ttl_seconds"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValidationError(f"{field} must be a positive integer")
        if self.minimum_authors > self.minimum_posts:
            raise ValidationError("minimum_authors cannot exceed minimum_posts")
        trim = _bounded_decimal(
            self.trim_fraction,
            "trim_fraction",
            minimum=_ZERO,
            maximum=Decimal("0.49"),
        )
        bullish = _bounded_decimal(
            self.bullish_threshold,
            "bullish_threshold",
            minimum=_ZERO,
            maximum=_ONE,
        )
        bearish = _bounded_decimal(
            self.bearish_threshold,
            "bearish_threshold",
            minimum=Decimal("-1"),
            maximum=_ZERO,
        )
        cluster = _bounded_decimal(
            self.max_cluster_share,
            "max_cluster_share",
            minimum=_ZERO,
            maximum=_ONE,
        )
        if bearish >= _ZERO or bullish <= _ZERO:
            raise ValidationError("sentiment thresholds must straddle zero")
        object.__setattr__(self, "trim_fraction", trim)
        object.__setattr__(self, "bullish_threshold", bullish)
        object.__setattr__(self, "bearish_threshold", bearish)
        object.__setattr__(self, "max_cluster_share", cluster)

    def canonical_record(self) -> dict[str, object]:
        return {
            "version": self.version,
            "minimum_posts": self.minimum_posts,
            "minimum_authors": self.minimum_authors,
            "trim_fraction": canonical_decimal(self.trim_fraction),
            "bullish_threshold": canonical_decimal(self.bullish_threshold),
            "bearish_threshold": canonical_decimal(self.bearish_threshold),
            "max_cluster_share": canonical_decimal(self.max_cluster_share),
            "ttl_seconds": self.ttl_seconds,
        }


@dataclass(frozen=True, slots=True)
class SentimentSnapshot:
    asset_id: str
    query: str
    query_version: str
    classifier_version: str
    policy_version: str
    policy_hash: str
    method: CollectionMethod
    window_start: datetime
    window_end: datetime
    collected_at: datetime
    expires_at: datetime
    evidence: tuple[SentimentEvidence, ...]
    excluded_count: int
    collection_complete: bool
    score: Decimal | None
    label: SentimentLabel
    quality_reasons: tuple[str, ...]
    cluster_share: Decimal
    artifact_hash: str

    def __post_init__(self) -> None:
        for field, maximum in (
            ("asset_id", 256),
            ("query", 1024),
            ("query_version", 64),
            ("classifier_version", 128),
            ("policy_version", 64),
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field, maximum=maximum))
        object.__setattr__(self, "policy_hash", _hash(self.policy_hash, "policy_hash"))
        object.__setattr__(self, "artifact_hash", _hash(self.artifact_hash, "artifact_hash"))
        if not isinstance(self.method, CollectionMethod):
            try:
                object.__setattr__(self, "method", CollectionMethod(self.method))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid collection method") from error
        if not isinstance(self.label, SentimentLabel):
            try:
                object.__setattr__(self, "label", SentimentLabel(self.label))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid sentiment label") from error
        start = _utc(self.window_start, "window_start")
        end = _utc(self.window_end, "window_end")
        collected = _utc(self.collected_at, "collected_at")
        expires = _utc(self.expires_at, "expires_at")
        if not start < end <= collected < expires:
            raise ValidationError("sentiment snapshot times are inconsistent")
        for field, value in (
            ("window_start", start),
            ("window_end", end),
            ("collected_at", collected),
            ("expires_at", expires),
        ):
            object.__setattr__(self, field, value)
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, SentimentEvidence) for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of SentimentEvidence")
        if tuple(sorted(self.evidence, key=lambda item: (item.published_at, item.post_id))) != self.evidence:
            raise ValidationError("sentiment evidence must be canonically ordered")
        if type(self.excluded_count) is not int or self.excluded_count < 0:
            raise ValidationError("excluded_count must be a non-negative integer")
        if type(self.collection_complete) is not bool:
            raise TypeError("collection_complete must be bool")
        if self.score is not None:
            object.__setattr__(
                self,
                "score",
                _bounded_decimal(
                    self.score,
                    "score",
                    minimum=Decimal("-1"),
                    maximum=_ONE,
                ),
            )
        if not isinstance(self.quality_reasons, tuple) or any(
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 128
            for reason in self.quality_reasons
        ):
            raise ValidationError("quality_reasons must contain stable reason codes")
        object.__setattr__(
            self,
            "cluster_share",
            _bounded_decimal(
                self.cluster_share,
                "cluster_share",
                minimum=_ZERO,
                maximum=_ONE,
            ),
        )
        if self.quality_reasons and self.label is not SentimentLabel.UNKNOWN:
            raise ValidationError("unavailable sentiment must use the unknown label")
        if not self.quality_reasons and (
            self.label is SentimentLabel.UNKNOWN or self.score is None
        ):
            raise ValidationError("available sentiment requires a score and known label")
        expected = _artifact_hash(_snapshot_hash_payload(self))
        if self.artifact_hash != expected:
            raise ValidationError("artifact_hash does not match sentiment snapshot")

    @property
    def available(self) -> bool:
        return not self.quality_reasons and self.label is not SentimentLabel.UNKNOWN

    @property
    def eligible_for_unattended_use(self) -> bool:
        return self.available and self.method is not CollectionMethod.MANUAL_BROWSER

    def is_fresh(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return self.collected_at <= checked < self.expires_at

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "sentiment_snapshot.v1",
            "asset_id": self.asset_id,
            "query": self.query,
            "query_version": self.query_version,
            "classifier_version": self.classifier_version,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "method": self.method.value,
            "window_start": self.window_start.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "window_end": self.window_end.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "collected_at": self.collected_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "expires_at": self.expires_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "post_count": len(self.evidence),
            "author_count": len({item.author_hash for item in self.evidence}),
            "excluded_count": self.excluded_count,
            "collection_complete": self.collection_complete,
            "score": None if self.score is None else canonical_decimal(self.score),
            "label": self.label.value,
            "quality_reasons": list(self.quality_reasons),
            "cluster_share": canonical_decimal(self.cluster_share),
            "artifact_hash": self.artifact_hash,
            "available": self.available,
            "eligible_for_unattended_use": self.eligible_for_unattended_use,
            "evidence": [item.canonical_record() for item in self.evidence],
            "raw_post_text_stored": False,
        }


def _artifact_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _snapshot_hash_payload(snapshot: SentimentSnapshot) -> dict[str, object]:
    return {
        "asset_id": snapshot.asset_id,
        "query": snapshot.query,
        "query_version": snapshot.query_version,
        "classifier_version": snapshot.classifier_version,
        "method": snapshot.method.value,
        "window_start": snapshot.window_start.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "window_end": snapshot.window_end.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "collected_at": snapshot.collected_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "expires_at": snapshot.expires_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "evidence": [item.canonical_record() for item in snapshot.evidence],
        "excluded_count": snapshot.excluded_count,
        "collection_complete": snapshot.collection_complete,
        "score": None if snapshot.score is None else canonical_decimal(snapshot.score),
        "label": snapshot.label.value,
        "quality_reasons": list(snapshot.quality_reasons),
        "cluster_share": canonical_decimal(snapshot.cluster_share),
        "policy_version": snapshot.policy_version,
        "policy_hash": snapshot.policy_hash,
    }


def build_sentiment_snapshot(
    *,
    asset_id: str,
    query: str,
    query_version: str,
    classifier_version: str,
    method: CollectionMethod,
    window_start: datetime,
    window_end: datetime,
    collected_at: datetime,
    evidence: Iterable[SentimentEvidence],
    excluded_count: int,
    collection_complete: bool,
    policy: SentimentPolicy = SentimentPolicy(),
) -> SentimentSnapshot:
    if not isinstance(policy, SentimentPolicy):
        raise TypeError("policy must be SentimentPolicy")
    if not isinstance(method, CollectionMethod):
        try:
            method = CollectionMethod(method)
        except (TypeError, ValueError) as error:
            raise ValidationError("invalid collection method") from error
    checked_asset = _text(asset_id, "asset_id")
    checked_query = _text(query, "query", maximum=1024)
    checked_query_version = _text(query_version, "query_version", maximum=64)
    checked_classifier = _text(classifier_version, "classifier_version", maximum=128)
    start = _utc(window_start, "window_start")
    end = _utc(window_end, "window_end")
    collected = _utc(collected_at, "collected_at")
    if not start < end <= collected:
        raise ValidationError("sentiment window must end no later than collection")
    if type(excluded_count) is not int or excluded_count < 0:
        raise ValidationError("excluded_count must be a non-negative integer")
    if type(collection_complete) is not bool:
        raise TypeError("collection_complete must be bool")
    items = tuple(sorted(evidence, key=lambda item: (item.published_at, item.post_id)))
    if any(not isinstance(item, SentimentEvidence) for item in items):
        raise TypeError("evidence must contain SentimentEvidence records")
    if any(not start <= item.published_at <= end for item in items):
        raise ValidationError("sentiment evidence falls outside the registered window")
    if any(item.observed_at > collected for item in items):
        raise ValidationError("sentiment evidence was observed after collection")
    for field, values in (
        ("post_id", [item.post_id for item in items]),
        ("source_url", [item.source_url for item in items]),
        ("content_hash", [item.content_hash for item in items]),
        ("author_hash", [item.author_hash for item in items]),
    ):
        if len(values) != len(set(values)):
            raise ValidationError(f"sentiment evidence repeats {field}")

    cluster_counts: dict[str, int] = {}
    for item in items:
        cluster_counts[item.cluster_hash] = cluster_counts.get(item.cluster_hash, 0) + 1
    with localcontext(_CONTEXT) as context:
        cluster_share = (
            _ZERO
            if not items
            else context.divide(Decimal(max(cluster_counts.values())), Decimal(len(items)))
        )
    quality_reasons: list[str] = []
    if not collection_complete:
        quality_reasons.append("collection_incomplete")
    if len(items) < policy.minimum_posts:
        quality_reasons.append("insufficient_posts")
    if len({item.author_hash for item in items}) < policy.minimum_authors:
        quality_reasons.append("insufficient_authors")
    if cluster_share > policy.max_cluster_share:
        quality_reasons.append("duplicate_cluster_concentration")
    if collected - end > timedelta(seconds=policy.ttl_seconds):
        quality_reasons.append("collection_stale")

    score: Decimal | None = None
    label = SentimentLabel.UNKNOWN
    if not quality_reasons:
        ordered = sorted(item.polarity for item in items)
        with localcontext(_CONTEXT) as context:
            trim_count = int(
                context.multiply(Decimal(len(ordered)), policy.trim_fraction).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            retained = ordered[trim_count : len(ordered) - trim_count or None]
            if not retained:
                quality_reasons.append("trim_removed_all_evidence")
            else:
                score = context.divide(sum(retained, _ZERO), Decimal(len(retained)))
                if score >= policy.bullish_threshold:
                    label = SentimentLabel.BULLISH
                elif score <= policy.bearish_threshold:
                    label = SentimentLabel.BEARISH
                else:
                    label = SentimentLabel.NEUTRAL

    expires = collected + timedelta(seconds=policy.ttl_seconds)
    policy_hash = _artifact_hash(policy.canonical_record())
    provisional = {
        "asset_id": checked_asset,
        "query": checked_query,
        "query_version": checked_query_version,
        "classifier_version": checked_classifier,
        "policy_version": policy.version,
        "policy_hash": policy_hash,
        "method": method,
        "window_start": start,
        "window_end": end,
        "collected_at": collected,
        "expires_at": expires,
        "evidence": items,
        "excluded_count": excluded_count,
        "collection_complete": collection_complete,
        "score": score,
        "label": label,
        "quality_reasons": tuple(quality_reasons),
        "cluster_share": cluster_share,
    }
    payload = {
        "asset_id": checked_asset,
        "query": checked_query,
        "query_version": checked_query_version,
        "classifier_version": checked_classifier,
        "method": method.value,
        "window_start": start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "window_end": end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "collected_at": collected.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "evidence": [item.canonical_record() for item in items],
        "excluded_count": excluded_count,
        "collection_complete": collection_complete,
        "score": None if score is None else canonical_decimal(score),
        "label": label.value,
        "quality_reasons": quality_reasons,
        "cluster_share": canonical_decimal(cluster_share),
        "policy_version": policy.version,
        "policy_hash": policy_hash,
    }
    return SentimentSnapshot(
        **provisional,
        artifact_hash=_artifact_hash(payload),
    )


def _parse_instant(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 string") from error
    return _utc(parsed, field)


def sentiment_snapshot_from_dict(value: Mapping[str, object]) -> SentimentSnapshot:
    """Reconstruct and integrity-check one persisted public snapshot document."""

    if not isinstance(value, Mapping):
        raise TypeError("sentiment snapshot document must be a mapping")
    document = dict(value)
    expected = {
        "schema_version",
        "asset_id",
        "query",
        "query_version",
        "classifier_version",
        "policy_version",
        "policy_hash",
        "method",
        "window_start",
        "window_end",
        "collected_at",
        "expires_at",
        "post_count",
        "author_count",
        "excluded_count",
        "collection_complete",
        "score",
        "label",
        "quality_reasons",
        "cluster_share",
        "artifact_hash",
        "available",
        "eligible_for_unattended_use",
        "evidence",
        "raw_post_text_stored",
    }
    if set(document) != expected or document["schema_version"] != "sentiment_snapshot.v1":
        raise ValidationError("sentiment snapshot document fields are unsupported")
    raw_evidence = document["evidence"]
    if not isinstance(raw_evidence, list):
        raise ValidationError("sentiment evidence must be an array")
    evidence: list[SentimentEvidence] = []
    evidence_fields = {
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
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, dict) or set(raw) != evidence_fields:
            raise ValidationError(f"sentiment evidence[{index}] fields are unsupported")
        evidence.append(
            SentimentEvidence(
                evidence_id=raw["evidence_id"],  # type: ignore[arg-type]
                post_id=raw["post_id"],  # type: ignore[arg-type]
                source_url=raw["source_url"],  # type: ignore[arg-type]
                author_hash=raw["author_hash"],  # type: ignore[arg-type]
                content_hash=raw["content_hash"],  # type: ignore[arg-type]
                cluster_hash=raw["cluster_hash"],  # type: ignore[arg-type]
                published_at=_parse_instant(
                    raw["published_at"], f"evidence[{index}].published_at"
                ),
                observed_at=_parse_instant(
                    raw["observed_at"], f"evidence[{index}].observed_at"
                ),
                polarity=raw["polarity"],  # type: ignore[arg-type]
            )
        )
    quality = document["quality_reasons"]
    if not isinstance(quality, list):
        raise ValidationError("quality_reasons must be an array")
    score = document["score"]
    if score is not None and not isinstance(score, str):
        raise ValidationError("score must be an exact decimal string or null")
    if isinstance(document["excluded_count"], bool) or not isinstance(
        document["excluded_count"], int
    ):
        raise ValidationError("excluded_count must be an integer")
    if type(document["collection_complete"]) is not bool:
        raise ValidationError("collection_complete must be boolean")
    snapshot = SentimentSnapshot(
        asset_id=document["asset_id"],  # type: ignore[arg-type]
        query=document["query"],  # type: ignore[arg-type]
        query_version=document["query_version"],  # type: ignore[arg-type]
        classifier_version=document["classifier_version"],  # type: ignore[arg-type]
        policy_version=document["policy_version"],  # type: ignore[arg-type]
        policy_hash=document["policy_hash"],  # type: ignore[arg-type]
        method=document["method"],  # type: ignore[arg-type]
        window_start=_parse_instant(document["window_start"], "window_start"),
        window_end=_parse_instant(document["window_end"], "window_end"),
        collected_at=_parse_instant(document["collected_at"], "collected_at"),
        expires_at=_parse_instant(document["expires_at"], "expires_at"),
        evidence=tuple(evidence),
        excluded_count=document["excluded_count"],
        collection_complete=document["collection_complete"],
        score=score,
        label=document["label"],  # type: ignore[arg-type]
        quality_reasons=tuple(quality),
        cluster_share=document["cluster_share"],  # type: ignore[arg-type]
        artifact_hash=document["artifact_hash"],  # type: ignore[arg-type]
    )
    if canonical_json(snapshot.as_dict()) != canonical_json(document):
        raise ValidationError("sentiment snapshot derived fields do not match")
    return snapshot


__all__ = (
    "CollectionMethod",
    "SentimentEvidence",
    "SentimentLabel",
    "SentimentPolicy",
    "SentimentSnapshot",
    "build_sentiment_snapshot",
    "sentiment_snapshot_from_dict",
)
