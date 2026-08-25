"""Agent-neutral, bounded research/learning tools for every model interface.

This module has no dependency on MCP or a model SDK.  Protocol adapters use
the immutable catalog and :class:`ToolService`; the service repeats all
security-relevant validation instead of trusting client-advertised schemas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from .canonical import (
    SEMANTIC_INTENT_HASH_DOMAIN,
    canonical_data,
    semantic_intent_hash,
)
from .domain import SemanticIntent
from .executor import disabled_executor
from .errors import RecordNotFound
from .learning_bridge import LearningRecorder
from .learning_ledger import LearningLedger
from .post_trade_review import PostTradeReviewer
from .node import default_state_database
from .research_api import ResearchService
from .research_store import ResearchStore
from .staging_inbox import (
    StagingState,
    StagingView,
    TradeStagingInbox,
    TrustedQuoteDecision,
    TrustedQuoteRequest,
)


JsonObject = dict[str, Any]
MarketBriefReader = Callable[..., object]

_MAX_INTENT_BYTES = 1_000_000
_MAX_INTENT_DEPTH = 4
_MAX_INTENT_NODES = 128
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(
    r"^[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
_REQUIRED_INTENT_FIELDS = frozenset(
    {
        "intent_id",
        "thesis_id",
        "thesis_version",
        "strategy_version",
        "code_hash",
        "venue",
        "account_id",
        "environment",
        "instrument",
        "action",
        "side",
        "quantity",
        "order_type",
        "expires_at",
        "client_order_id",
    }
)
_OPTIONAL_INTENT_FIELDS = frozenset(
    {
        "limit_price",
        "price_bound",
        "stop_price",
        "protection_limit_price",
        "reduce_only",
        "leverage",
        "max_slippage_bps",
        "fee_bps",
        "time_in_force",
        "signal_instance_hash",
        "allowed_runtime_fields",
        "schema_version",
    }
)
_INTENT_FIELDS = _REQUIRED_INTENT_FIELDS | _OPTIONAL_INTENT_FIELDS
_TEXT_LIMITS = {
    "intent_id": 128,
    "thesis_id": 128,
    "thesis_version": 64,
    "strategy_version": 64,
    "venue": 32,
    "account_id": 256,
    "instrument": 64,
    "action": 64,
    "expires_at": 64,
    "client_order_id": 128,
    "time_in_force": 32,
}
_DECIMAL_FIELDS = frozenset(
    {
        "quantity",
        "limit_price",
        "price_bound",
        "stop_price",
        "protection_limit_price",
        "leverage",
        "max_slippage_bps",
        "fee_bps",
    }
)
_RUNTIME_FIELDS = frozenset({"nonce", "signature", "signing_timestamp"})


class ToolInputError(ValueError):
    """Raised when a tool call does not match the bounded public contract."""


class _IntentDocumentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Protocol-neutral metadata for one callable tool."""

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False

    def as_dict(self) -> JsonObject:
        """Return an isolated JSON-safe representation of this definition."""

        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": deepcopy(dict(self.input_schema)),
            "output_schema": deepcopy(dict(self.output_schema)),
            "read_only": self.read_only,
            "destructive": self.destructive,
            "idempotent": self.idempotent,
            "open_world": self.open_world,
        }


def _text_schema(maximum: int) -> JsonObject:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


_EXACT_DECIMAL_SCHEMA: JsonObject = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": _DECIMAL_RE.pattern,
    "description": "Exact finite decimal string; JSON numbers are forbidden.",
}

_INTENT_PROPERTIES: JsonObject = {
    **{field: _text_schema(maximum) for field, maximum in _TEXT_LIMITS.items()},
    "code_hash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": "^[0-9a-f]{64}$",
    },
    "environment": {"type": "string", "enum": ["testnet", "mainnet", "shadow"]},
    "side": {"type": "string", "enum": ["buy", "sell"]},
    "order_type": {
        "type": "string",
        "enum": ["market", "limit", "stop", "stop_limit"],
    },
    **{field: deepcopy(_EXACT_DECIMAL_SCHEMA) for field in _DECIMAL_FIELDS},
    "reduce_only": {"type": "boolean"},
    "signal_instance_hash": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": "^[0-9a-f]{64}$",
    },
    "allowed_runtime_fields": {
        "type": "array",
        "items": {"type": "string", "enum": sorted(_RUNTIME_FIELDS)},
        "minItems": 1,
        "maxItems": 3,
        "uniqueItems": True,
    },
    "schema_version": {"type": "integer", "const": 1},
}

_INTENT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "maxProperties": len(_INTENT_FIELDS),
    "required": sorted(_REQUIRED_INTENT_FIELDS),
    "properties": _INTENT_PROPERTIES,
}

_STATUS_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "component",
        "mode",
        "ok",
        "execution",
        "exposed_tools",
        "market_data",
        "research",
        "learning",
        "venue_writes_enabled",
        "credential_loading_enabled",
    ],
    "properties": {
        "component": {"type": "string", "const": "trading-harness"},
        "mode": {
            "type": "string",
            "enum": ["research_only", "research_and_testnet_learning_staging"],
        },
        "ok": {"type": "boolean"},
        "execution": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "adapter",
                "venue_writes_enabled",
                "credential_loading_enabled",
                "reason",
            ],
            "properties": {
                "adapter": {"type": "string", "const": "disabled"},
                "venue_writes_enabled": {"type": "boolean", "const": False},
                "credential_loading_enabled": {"type": "boolean", "const": False},
                "reason": {"type": "string", "minLength": 1, "maxLength": 256},
            },
        },
        "exposed_tools": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "analyze_asset",
                    "get_latest_sentiment",
                    "get_learning_review",
                    "get_learning_summary",
                    "get_node_status",
                    "get_harness_status",
                    "get_market_brief",
                    "get_trade_stage",
                    "list_tracked_assets",
                    "pause_tracked_asset",
                    "record_manual_sentiment",
                    "stage_trade_candidate",
                    "track_asset",
                    "validate_trade_intent",
                    "validate_candidate_profitability",
                ],
            },
            "minItems": 15,
            "maxItems": 15,
            "uniqueItems": True,
        },
        "research": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "asset_tracking",
                "enabled",
                "local_state_writes_enabled",
                "manual_browser_unattended_eligible",
                "registered_strategy",
            ],
            "properties": {
                "asset_tracking": {"type": "boolean", "const": True},
                "enabled": {"type": "boolean", "const": True},
                "local_state_writes_enabled": {"type": "boolean", "const": True},
                "manual_browser_unattended_eligible": {
                    "type": "boolean",
                    "const": False,
                },
                "registered_strategy": {
                    "type": "string",
                    "const": "candidate-v0/1",
                },
            },
        },
        "market_data": {
            "type": "object",
            "additionalProperties": False,
            "required": ["access", "credentials_required", "enabled", "networks"],
            "properties": {
                "access": {"type": "string", "const": "public_read_only"},
                "credentials_required": {"type": "boolean", "const": False},
                "enabled": {"type": "boolean", "const": True},
                "networks": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mainnet", "testnet"]},
                    "minItems": 2,
                    "maxItems": 2,
                    "uniqueItems": True,
                },
            },
        },
        "learning": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "staging_profile_configured",
                "stage_is_authoritative",
                "approval_tool_exposed",
                "execution_tool_exposed",
                "mainnet_authorized",
            ],
            "properties": {
                "staging_profile_configured": {"type": "boolean"},
                "stage_is_authoritative": {"type": "boolean", "const": False},
                "approval_tool_exposed": {"type": "boolean", "const": False},
                "execution_tool_exposed": {"type": "boolean", "const": False},
                "mainnet_authorized": {"type": "boolean", "const": False},
            },
        },
        "venue_writes_enabled": {"type": "boolean", "const": False},
        "credential_loading_enabled": {"type": "boolean", "const": False},
    },
}

_TIMESTAMP_SCHEMA: JsonObject = {
    "type": "string",
    "format": "date-time",
    "minLength": 20,
    "maxLength": 64,
}
_SOURCE_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url", "endpoint", "request_type"],
    "properties": {
        "url": {
            "type": "string",
            "enum": [
                "https://api.hyperliquid.xyz/info",
                "https://api.hyperliquid-testnet.xyz/info",
            ],
        },
        "endpoint": {"type": "string", "const": "/info"},
        "request_type": {
            "type": "string",
            "enum": ["metaAndAssetCtxs", "l2Book"],
        },
    },
}
_DEPTH_BAND_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["bid_size", "ask_size", "bid_complete", "ask_complete"],
    "properties": {
        "bid_size": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "ask_size": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "bid_complete": {"type": "boolean"},
        "ask_complete": {"type": "boolean"},
    },
}
_TIMESTAMPS_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["market_context", "book", "top_level_observed_at_scope"],
    "properties": {
        "market_context": {
            "type": "object",
            "additionalProperties": False,
            "required": ["exchange_observed_at", "received_at", "basis", "reason"],
            "properties": {
                "exchange_observed_at": {"type": "null"},
                "received_at": deepcopy(_TIMESTAMP_SCHEMA),
                "basis": {"type": "string", "const": "local_receipt_only"},
                "reason": {
                    "type": "string",
                    "const": "metaAndAssetCtxs has no exchange timestamp",
                },
            },
        },
        "book": {
            "type": "object",
            "additionalProperties": False,
            "required": ["observed_at", "received_at", "age_ms", "basis"],
            "properties": {
                "observed_at": deepcopy(_TIMESTAMP_SCHEMA),
                "received_at": deepcopy(_TIMESTAMP_SCHEMA),
                "age_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                "basis": {
                    "type": "string",
                    "const": "hyperliquid_l2Book.time",
                },
            },
        },
        "top_level_observed_at_scope": {
            "type": "string",
            "const": "book_only",
        },
    },
}
_MID_CONSISTENCY_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "context_mid",
        "book_mid",
        "absolute_difference",
        "divergence_bps",
        "divergence_bps_exact",
        "divergence_bps_display_precision_digits",
        "max_divergence_bps",
        "comparison",
        "within_limit",
    ],
    "properties": {
        "context_mid": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "book_mid": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "absolute_difference": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "divergence_bps": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "divergence_bps_exact": {
            "type": "object",
            "additionalProperties": False,
            "required": ["numerator", "denominator", "multiplier"],
            "properties": {
                "numerator": deepcopy(_EXACT_DECIMAL_SCHEMA),
                "denominator": deepcopy(_EXACT_DECIMAL_SCHEMA),
                "multiplier": {"type": "string", "const": "10000"},
            },
        },
        "divergence_bps_display_precision_digits": {
            "type": "integer",
            "const": 34,
        },
        "max_divergence_bps": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "comparison": {
            "type": "string",
            "const": "difference*10000 <= book_mid*max_divergence_bps",
        },
        "within_limit": {"type": "boolean", "const": True},
    },
}

_MARKET_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "venue",
        "network",
        "symbol",
        "observed_at",
        "received_at",
        "age_ms",
        "context_received_at",
        "timestamps",
        "sources",
        "mid",
        "mark",
        "oracle",
        "funding_hourly",
        "open_interest",
        "day_notional_volume",
        "mid_consistency",
        "book",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "hyperliquid.market_brief.v1"},
        "venue": {"type": "string", "const": "hyperliquid"},
        "network": {"type": "string", "enum": ["mainnet", "testnet"]},
        "symbol": {
            **_text_schema(64),
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
        },
        "observed_at": deepcopy(_TIMESTAMP_SCHEMA),
        "received_at": deepcopy(_TIMESTAMP_SCHEMA),
        "age_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
        "context_received_at": deepcopy(_TIMESTAMP_SCHEMA),
        "timestamps": _TIMESTAMPS_SCHEMA,
        "sources": {
            "type": "array",
            "items": _SOURCE_SCHEMA,
            "minItems": 2,
            "maxItems": 2,
        },
        "mid": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "mark": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "oracle": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "funding_hourly": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "open_interest": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "day_notional_volume": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "mid_consistency": _MID_CONSISTENCY_SCHEMA,
        "book": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "time_ms",
                "mid",
                "best_bid",
                "best_ask",
                "bid_level_count",
                "ask_level_count",
                "level_cap_per_side",
                "depth",
            ],
            "properties": {
                "time_ms": {"type": "integer", "minimum": 0},
                "mid": deepcopy(_EXACT_DECIMAL_SCHEMA),
                "best_bid": deepcopy(_EXACT_DECIMAL_SCHEMA),
                "best_ask": deepcopy(_EXACT_DECIMAL_SCHEMA),
                "bid_level_count": {"type": "integer", "minimum": 1, "maximum": 20},
                "ask_level_count": {"type": "integer", "minimum": 1, "maximum": 20},
                "level_cap_per_side": {"type": "integer", "const": 20},
                "depth": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["5bps", "10bps", "25bps"],
                    "properties": {
                        "5bps": deepcopy(_DEPTH_BAND_SCHEMA),
                        "10bps": deepcopy(_DEPTH_BAND_SCHEMA),
                        "25bps": deepcopy(_DEPTH_BAND_SCHEMA),
                    },
                },
            },
        },
    },
}

_INTENT_RESULT_BASE: JsonObject = {
    "authorization_created": {"type": "boolean", "const": False},
    "order_submitted": {"type": "boolean", "const": False},
    "validation_scope": {
        "type": "string",
        "const": "schema_and_canonical_hash_only",
    },
}
_INTENT_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "authorization_created",
                "order_submitted",
                "validation_scope",
                "valid",
                "algorithm",
                "domain",
                "intent_hash",
                "intent_id",
                "environment",
                "instrument",
            ],
            "properties": {
                **deepcopy(_INTENT_RESULT_BASE),
                "valid": {"type": "boolean", "const": True},
                "algorithm": {"type": "string", "const": "sha256"},
                "domain": {
                    "type": "string",
                    "const": SEMANTIC_INTENT_HASH_DOMAIN,
                },
                "intent_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "intent_id": _text_schema(128),
                "environment": {
                    "type": "string",
                    "enum": ["testnet", "mainnet", "shadow"],
                },
                "instrument": _text_schema(64),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "authorization_created",
                "order_submitted",
                "validation_scope",
                "valid",
                "error",
            ],
            "properties": {
                **deepcopy(_INTENT_RESULT_BASE),
                "valid": {"type": "boolean", "const": False},
                "error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "pattern": "^[a-z0-9_]+$",
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                        },
                    },
                },
            },
        },
    ]
}

_HASH_SCHEMA: JsonObject = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": "^[0-9a-f]{64}$",
}
_TRACKED_ASSET_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "asset_id",
        "venue",
        "market_data_network",
        "execution_environment",
        "symbol",
        "interval",
        "poll_seconds",
        "technical_profile_version",
        "sentiment_policy_version",
        "sentiment_query",
        "sentiment_query_version",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "config_hash",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "tracked_asset.v1"},
        "asset_id": _text_schema(128),
        "venue": {"type": "string", "const": "hyperliquid"},
        "market_data_network": {"type": "string", "enum": ["mainnet", "testnet"]},
        "execution_environment": {"type": "string", "const": "shadow"},
        "symbol": {
            **_text_schema(64),
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
        },
        "interval": {"type": "string", "const": "4h"},
        "poll_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
        "technical_profile_version": _text_schema(64),
        "sentiment_policy_version": _text_schema(64),
        "sentiment_query": _text_schema(1024),
        "sentiment_query_version": _text_schema(64),
        "status": {"type": "string", "enum": ["active", "paused"]},
        "revision": {"type": "integer", "minimum": 1},
        "created_at": deepcopy(_TIMESTAMP_SCHEMA),
        "updated_at": deepcopy(_TIMESTAMP_SCHEMA),
        "config_hash": deepcopy(_HASH_SCHEMA),
    },
}
_LOCAL_WRITE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "tracked_asset",
        "local_state_updated",
        "trade_authority_created",
        "order_submitted",
    ],
    "properties": {
        "tracked_asset": deepcopy(_TRACKED_ASSET_SCHEMA),
        "local_state_updated": {"type": "boolean"},
        "trade_authority_created": {"type": "boolean", "const": False},
        "order_submitted": {"type": "boolean", "const": False},
    },
}
_EVIDENCE_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "evidence_id",
        "post_id",
        "source_url",
        "author_hash",
        "content_hash",
        "cluster_hash",
        "published_at",
        "observed_at",
        "polarity",
    ],
    "properties": {
        "evidence_id": _text_schema(256),
        "post_id": _text_schema(256),
        "source_url": {
            "type": "string",
            "format": "uri",
            "maxLength": 2048,
            "pattern": "^https://(?:www\\.)?(?:x\\.com|twitter\\.com)/.+$",
        },
        "author_hash": deepcopy(_HASH_SCHEMA),
        "content_hash": deepcopy(_HASH_SCHEMA),
        "cluster_hash": deepcopy(_HASH_SCHEMA),
        "published_at": deepcopy(_TIMESTAMP_SCHEMA),
        "observed_at": deepcopy(_TIMESTAMP_SCHEMA),
        "polarity": {
            "type": "string",
            "enum": ["-1", "-0.5", "0", "0.5", "1"],
            "description": "Manual research polarity from -1 through 1; never unattended authority.",
        },
    },
}
_SENTIMENT_WRITE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "snapshot",
        "record_hash",
        "local_state_updated",
        "unattended_eligible",
        "order_submitted",
    ],
    "properties": {
        "snapshot": {
            "type": "object",
            "required": [
                "method",
                "eligible_for_unattended_use",
                "raw_post_text_stored",
                "artifact_hash",
            ],
            "properties": {
                "method": {"type": "string", "const": "manual_browser"},
                "eligible_for_unattended_use": {
                    "type": "boolean",
                    "const": False,
                },
                "raw_post_text_stored": {"type": "boolean", "const": False},
                "artifact_hash": deepcopy(_HASH_SCHEMA),
            },
        },
        "record_hash": deepcopy(_HASH_SCHEMA),
        "local_state_updated": {"type": "boolean", "const": True},
        "unattended_eligible": {"type": "boolean", "const": False},
        "order_submitted": {"type": "boolean", "const": False},
    },
}
_LATEST_SENTIMENT_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["found", "asset_id", "snapshot"],
    "properties": {
        "found": {"type": "boolean"},
        "asset_id": _text_schema(128),
        "snapshot": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": [
                        "method",
                        "expires_at",
                        "label",
                        "available",
                        "eligible_for_unattended_use",
                        "artifact_hash",
                        "raw_post_text_stored",
                    ],
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": [
                                "manual_browser",
                                "x_api",
                                "compliant_provider",
                            ],
                        },
                        "expires_at": deepcopy(_TIMESTAMP_SCHEMA),
                        "label": {
                            "type": "string",
                            "enum": ["bullish", "bearish", "neutral", "unknown"],
                        },
                        "available": {"type": "boolean"},
                        "eligible_for_unattended_use": {"type": "boolean"},
                        "artifact_hash": deepcopy(_HASH_SCHEMA),
                        "raw_post_text_stored": {"type": "boolean", "const": False},
                    },
                },
            ]
        },
        "record_hash": deepcopy(_HASH_SCHEMA),
    },
}
_ANALYSIS_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "analysis_hash",
        "analysis_record_hash",
        "learning_cycle_id",
        "learning_event_hash",
        "asset",
        "observed_at",
        "history",
        "descriptive_technical",
        "registered_signal",
        "sentiment",
        "assessment",
        "profitability_attested",
        "venue_writes_enabled",
        "order_submitted",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "asset_analysis.v1"},
        "analysis_hash": deepcopy(_HASH_SCHEMA),
        "analysis_record_hash": deepcopy(_HASH_SCHEMA),
        "learning_cycle_id": {
            "anyOf": [_text_schema(128), {"type": "null"}]
        },
        "learning_event_hash": {
            "anyOf": [deepcopy(_HASH_SCHEMA), {"type": "null"}]
        },
        "asset": deepcopy(_TRACKED_ASSET_SCHEMA),
        "observed_at": deepcopy(_TIMESTAMP_SCHEMA),
        "history": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "data_hash",
                "completed_candles",
                "coverage_complete",
                "truncated",
            ],
            "properties": {
                "data_hash": deepcopy(_HASH_SCHEMA),
                "completed_candles": {"type": "integer", "minimum": 1001},
                "coverage_complete": {"type": "boolean", "const": True},
                "truncated": {"type": "boolean", "const": False},
            },
        },
        "descriptive_technical": {
            "type": "object",
            "required": ["bias", "executable", "evidence_status"],
            "properties": {
                "bias": {"type": "string", "enum": ["buy", "sell", "nothing"]},
                "executable": {"type": "boolean", "const": False},
                "evidence_status": {
                    "type": "string",
                    "const": "research_candidate",
                },
            },
        },
        "registered_signal": {
            "type": "object",
            "required": [
                "strategy_hash",
                "signal_hash",
                "expires_at",
                "direction",
                "reason",
            ],
            "properties": {
                "strategy_hash": deepcopy(_HASH_SCHEMA),
                "signal_hash": deepcopy(_HASH_SCHEMA),
                "expires_at": deepcopy(_TIMESTAMP_SCHEMA),
                "direction": {
                    "type": "string",
                    "enum": ["buy", "sell", "nothing"],
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
        "sentiment": deepcopy(_LATEST_SENTIMENT_OUTPUT_SCHEMA),
        "assessment": {
            "type": "object",
            "required": [
                "verdict",
                "reason_codes",
                "eligible_for_risk_quote",
                "eligible_to_trade",
                "approval_created",
                "order_submitted",
            ],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["buy", "sell", "nothing", "unavailable"],
                },
                "reason_codes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "eligible_for_risk_quote": {"type": "boolean"},
                "eligible_to_trade": {"type": "boolean", "const": False},
                "approval_created": {"type": "boolean", "const": False},
                "order_submitted": {"type": "boolean", "const": False},
            },
        },
        "profitability_attested": {"type": "boolean", "const": False},
        "venue_writes_enabled": {"type": "boolean", "const": False},
        "order_submitted": {"type": "boolean", "const": False},
    },
}
_VALIDATION_SUMMARY_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "asset_id",
        "strategy_hash",
        "data_hash",
        "artifact_hash",
        "historical_status",
        "historical_reasons",
        "trade_count",
        "expectancy_r",
        "lower_95_r",
        "profit_factor",
        "max_drawdown_r",
        "stress_expectancy_r",
        "shadow_required",
        "deployment_qualified",
        "profit_guaranteed",
        "order_submitted",
    ],
    "properties": {
        "schema_version": {
            "type": "string",
            "const": "candidate_validation_summary.v1",
        },
        "asset_id": _text_schema(128),
        "strategy_hash": deepcopy(_HASH_SCHEMA),
        "data_hash": deepcopy(_HASH_SCHEMA),
        "artifact_hash": deepcopy(_HASH_SCHEMA),
        "historical_status": {
            "type": "string",
            "enum": ["PASS", "REJECTED", "INCONCLUSIVE"],
        },
        "historical_reasons": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
        },
        "trade_count": {"type": "integer", "minimum": 0},
        "expectancy_r": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "lower_95_r": {"anyOf": [deepcopy(_EXACT_DECIMAL_SCHEMA), {"type": "null"}]},
        "profit_factor": {"anyOf": [deepcopy(_EXACT_DECIMAL_SCHEMA), {"type": "null"}]},
        "max_drawdown_r": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "stress_expectancy_r": deepcopy(_EXACT_DECIMAL_SCHEMA),
        "shadow_required": {"type": "boolean", "const": True},
        "deployment_qualified": {"type": "boolean", "const": False},
        "profit_guaranteed": {"type": "boolean", "const": False},
        "order_submitted": {"type": "boolean", "const": False},
    },
}

_TRADE_STAGE_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "document",
        "state",
        "expired_at",
        "latest_event_sequence",
        "chain_hash",
        "authoritative",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": "trade_stage_view.v1"},
        "document": {
            "type": "object",
            "required": [
                "schema_version",
                "document_id",
                "request",
                "decision",
                "created_at",
                "expires_at",
                "authority",
                "document_hash",
            ],
            "properties": {
                "schema_version": {
                    "type": "string",
                    "const": "trade_staging_document.v1",
                },
                "document_id": _text_schema(80),
                "decision": {"type": "string", "enum": ["staged", "blocked"]},
                "document_hash": deepcopy(_HASH_SCHEMA),
                "created_at": deepcopy(_TIMESTAMP_SCHEMA),
                "expires_at": deepcopy(_TIMESTAMP_SCHEMA),
                "authority": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "boolean",
                        "const": False,
                    },
                },
            },
        },
        "state": {"type": "string", "enum": ["staged", "blocked", "expired"]},
        "expired_at": {
            "anyOf": [deepcopy(_TIMESTAMP_SCHEMA), {"type": "null"}]
        },
        "latest_event_sequence": {"type": "integer", "minimum": 1},
        "chain_hash": deepcopy(_HASH_SCHEMA),
        "authoritative": {"type": "boolean", "const": False},
    },
}


TOOL_CATALOG: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_harness_status",
        title="Get harness safety status",
        description=(
            "Read the harness capability status and verify that credential "
            "loading and venue writes remain disabled."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "maxProperties": 0,
        },
        output_schema=_STATUS_OUTPUT_SCHEMA,
    ),
    ToolDefinition(
        name="get_market_brief",
        title="Get public Hyperliquid market brief",
        description=(
            "Read a timestamped public Hyperliquid perpetual market brief. "
            "This tool does not read an account, credentials, or submit an order."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "maxProperties": 2,
            "required": ["symbol", "network"],
            "properties": {
                "symbol": {
                    **_text_schema(64),
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
                },
                "network": {"type": "string", "enum": ["mainnet", "testnet"]},
            },
        },
        output_schema=_MARKET_OUTPUT_SCHEMA,
        open_world=True,
    ),
    ToolDefinition(
        name="track_asset",
        title="Track a Hyperliquid asset",
        description=(
            "Create an idempotent local 4-hour research tracker. This writes "
            "only the harness research database and cannot authorize or submit a trade."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id", "symbol", "network", "sentiment_query"],
            "properties": {
                "asset_id": _text_schema(128),
                "symbol": {
                    **_text_schema(64),
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
                },
                "network": {"type": "string", "enum": ["mainnet", "testnet"]},
                "sentiment_query": _text_schema(1024),
                "poll_seconds": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 86400,
                    "default": 60,
                },
            },
        },
        output_schema=deepcopy(_LOCAL_WRITE_OUTPUT_SCHEMA),
        read_only=False,
    ),
    ToolDefinition(
        name="pause_tracked_asset",
        title="Pause a tracked asset",
        description=(
            "Pause one local tracker using its exact compare-and-swap revision. "
            "This cannot cancel an exchange order or change a position."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id", "expected_revision"],
            "properties": {
                "asset_id": _text_schema(128),
                "expected_revision": {"type": "integer", "minimum": 1},
            },
        },
        output_schema=deepcopy(_LOCAL_WRITE_OUTPUT_SCHEMA),
        read_only=False,
    ),
    ToolDefinition(
        name="list_tracked_assets",
        title="List tracked assets",
        description="Read the versioned local asset-tracking registry.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "maxProperties": 0,
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["count", "assets", "venue_writes_enabled"],
            "properties": {
                "count": {"type": "integer", "minimum": 0},
                "assets": {
                    "type": "array",
                    "items": deepcopy(_TRACKED_ASSET_SCHEMA),
                    "maxItems": 10000,
                },
                "venue_writes_enabled": {"type": "boolean", "const": False},
            },
        },
    ),
    ToolDefinition(
        name="record_manual_sentiment",
        title="Record manual X sentiment evidence",
        description=(
            "Store source IDs, hashes, timestamps and bounded manual polarities "
            "from an explicit browser research session. Raw post text and browser "
            "credentials are forbidden; this evidence is never unattended authority."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "asset_id",
                "window_start",
                "window_end",
                "evidence",
                "excluded_count",
                "collection_complete",
            ],
            "properties": {
                "asset_id": _text_schema(128),
                "window_start": deepcopy(_TIMESTAMP_SCHEMA),
                "window_end": deepcopy(_TIMESTAMP_SCHEMA),
                "evidence": {
                    "type": "array",
                    "items": deepcopy(_EVIDENCE_INPUT_SCHEMA),
                    "maxItems": 100,
                },
                "excluded_count": {"type": "integer", "minimum": 0},
                "collection_complete": {"type": "boolean"},
            },
        },
        output_schema=deepcopy(_SENTIMENT_WRITE_OUTPUT_SCHEMA),
        read_only=False,
    ),
    ToolDefinition(
        name="get_latest_sentiment",
        title="Get latest sentiment evidence",
        description="Read and integrity-check the latest local sentiment snapshot.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {"asset_id": _text_schema(128)},
        },
        output_schema=deepcopy(_LATEST_SENTIMENT_OUTPUT_SCHEMA),
    ),
    ToolDefinition(
        name="analyze_asset",
        title="Analyze a tracked asset now",
        description=(
            "Fetch strict completed candles, calculate descriptive TA and the frozen "
            "registered buy/sell/nothing signal, then combine the latest sentiment. "
            "The result cannot authorize or submit a trade."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {"asset_id": _text_schema(128)},
        },
        output_schema=deepcopy(_ANALYSIS_OUTPUT_SCHEMA),
        open_world=True,
        read_only=False,
    ),
    ToolDefinition(
        name="validate_candidate_profitability",
        title="Validate registered candidate historically",
        description=(
            "Run the frozen costed candidate-v0 historical evaluation. A PASS still "
            "requires prospective shadow evidence and never guarantees profit."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {"asset_id": _text_schema(128)},
        },
        output_schema=deepcopy(_VALIDATION_SUMMARY_OUTPUT_SCHEMA),
        open_world=True,
    ),
    ToolDefinition(
        name="stage_trade_candidate",
        title="Stage a non-authoritative trade candidate",
        description=(
            "Write an immutable local staging request for one exact saved analysis. "
            "The request cannot specify economics, account data, approval, credentials, "
            "or execution fields and never creates trade authority."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "asset_id",
                "expected_analysis_hash",
                "idempotency_key",
            ],
            "properties": {
                "asset_id": _text_schema(128),
                "expected_analysis_hash": deepcopy(_HASH_SCHEMA),
                "idempotency_key": {
                    **_text_schema(128),
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
            },
        },
        output_schema=deepcopy(_TRADE_STAGE_OUTPUT_SCHEMA),
        read_only=False,
        idempotent=True,
        open_world=True,
    ),
    ToolDefinition(
        name="get_trade_stage",
        title="Get an immutable trade stage",
        description=(
            "Read and integrity-check one local non-authoritative staging document."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["document_id"],
            "properties": {"document_id": _text_schema(80)},
        },
        output_schema=deepcopy(_TRADE_STAGE_OUTPUT_SCHEMA),
    ),
    ToolDefinition(
        name="get_learning_review",
        title="Get a deterministic learning-cycle review",
        description=(
            "Read a descriptive review of one immutable decision/trade cycle. "
            "The review makes no causality or profitability claim."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["cycle_id"],
            "properties": {"cycle_id": _text_schema(128)},
        },
        output_schema={"type": "object"},
    ),
    ToolDefinition(
        name="get_learning_summary",
        title="Get versioned descriptive learning metrics",
        description=(
            "Aggregate immutable learning cycles by exact strategy/configuration "
            "version without inferring causality or future profitability."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "maxProperties": 0,
        },
        output_schema={"type": "object"},
    ),
    ToolDefinition(
        name="get_node_status",
        title="Get always-on node status",
        description=(
            "Read the fenced local node runtime, lease and component heartbeats. "
            "Research-only nodes always keep new venue risk halted."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"node_id": _text_schema(128)},
            "maxProperties": 1,
        },
        output_schema={"type": "object"},
    ),
    ToolDefinition(
        name="validate_trade_intent",
        title="Validate and hash a semantic trade intent",
        description=(
            "Validate the bounded public intent schema and calculate its "
            "canonical hash. This does not perform risk review, create an "
            "approval, reserve exposure, or submit an order."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "maxProperties": 1,
            "required": ["intent"],
            "properties": {"intent": _INTENT_SCHEMA},
        },
        output_schema=_INTENT_OUTPUT_SCHEMA,
    ),
)

_TOOL_NAMES = frozenset(definition.name for definition in TOOL_CATALOG)


def tool_catalog() -> tuple[JsonObject, ...]:
    return tuple(definition.as_dict() for definition in TOOL_CATALOG)


def _default_market_brief_reader(
    symbol: str,
    network: str,
    *,
    transport: object | None = None,
) -> object:
    from .market_data import get_market_brief

    return get_market_brief(symbol, network, transport=transport)


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _IntentDocumentError("invalid_type", f"{field} must be a string")
    if not value or value != value.strip():
        raise _IntentDocumentError("invalid_text", f"{field} must be non-empty and trimmed")
    if len(value) > maximum:
        raise _IntentDocumentError("value_too_long", f"{field} exceeds its length limit")
    if any(ord(character) < 32 for character in value):
        raise _IntentDocumentError("invalid_text", f"{field} contains control characters")
    return value


def _check_intent_shape(document: dict[str, Any]) -> None:
    """Bound nested JSON work before encoding or semantic validation."""

    stack: list[tuple[object, int]] = [(document, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_INTENT_NODES:
            raise _IntentDocumentError(
                "document_too_complex",
                "intent contains too many JSON values",
            )
        if depth > _MAX_INTENT_DEPTH:
            raise _IntentDocumentError(
                "document_too_deep",
                "intent JSON nesting exceeds its depth limit",
            )
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                raise _IntentDocumentError(
                    "invalid_json",
                    "intent must not contain cyclic values",
                )
            seen_containers.add(identity)
            if len(value) > len(_INTENT_FIELDS):
                raise _IntentDocumentError(
                    "document_too_complex",
                    "intent contains too many object properties",
                )
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen_containers:
                raise _IntentDocumentError(
                    "invalid_json",
                    "intent must not contain cyclic values",
                )
            seen_containers.add(identity)
            if len(value) > 8:
                raise _IntentDocumentError(
                    "document_too_complex",
                    "intent contains an oversized JSON array",
                )
            stack.extend((item, depth + 1) for item in value)


def _validate_public_intent(intent: Mapping[str, Any]) -> dict[str, Any]:
    try:
        document = dict(intent)
    except Exception as error:
        raise _IntentDocumentError("invalid_object", "intent must be a plain object") from error
    if not all(isinstance(key, str) for key in document):
        raise _IntentDocumentError("invalid_field", "intent field names must be strings")
    if set(document) - _INTENT_FIELDS:
        raise _IntentDocumentError("unsupported_field", "intent contains an unsupported field")
    missing = _REQUIRED_INTENT_FIELDS - set(document)
    if missing:
        raise _IntentDocumentError("missing_field", "intent is missing a required field")
    _check_intent_shape(document)

    try:
        encoded = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise _IntentDocumentError("invalid_json", "intent must contain JSON-safe values") from error
    if len(encoded) > _MAX_INTENT_BYTES:
        raise _IntentDocumentError("document_too_large", "intent exceeds 1000000 bytes")

    for field, maximum in _TEXT_LIMITS.items():
        if field in document:
            _bounded_text(document[field], field, maximum)
    code_hash = document["code_hash"]
    if not isinstance(code_hash, str) or not _SHA256_RE.fullmatch(code_hash):
        raise _IntentDocumentError("invalid_hash", "code_hash must be a lowercase SHA-256 digest")
    if "signal_instance_hash" in document:
        signal_hash = document["signal_instance_hash"]
        if not isinstance(signal_hash, str) or not _SHA256_RE.fullmatch(signal_hash):
            raise _IntentDocumentError(
                "invalid_hash",
                "signal_instance_hash must be a lowercase SHA-256 digest",
            )

    enums = {
        "environment": {"testnet", "mainnet", "shadow"},
        "side": {"buy", "sell"},
        "order_type": {"market", "limit", "stop", "stop_limit"},
    }
    for field, allowed in enums.items():
        if not isinstance(document[field], str) or document[field] not in allowed:
            raise _IntentDocumentError("invalid_enum", f"{field} is invalid")
    for field in _DECIMAL_FIELDS:
        if field not in document:
            continue
        value = document[field]
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or not _DECIMAL_RE.fullmatch(value)
        ):
            raise _IntentDocumentError(
                "invalid_decimal", f"{field} must be an exact decimal string"
            )
    if "reduce_only" in document and type(document["reduce_only"]) is not bool:
        raise _IntentDocumentError("invalid_type", "reduce_only must be boolean")
    if "allowed_runtime_fields" in document:
        runtime_fields = document["allowed_runtime_fields"]
        if not isinstance(runtime_fields, list) or not 1 <= len(runtime_fields) <= 3:
            raise _IntentDocumentError(
                "invalid_runtime_fields", "allowed_runtime_fields must contain one to three values"
            )
        if (
            any(not isinstance(value, str) or value not in _RUNTIME_FIELDS for value in runtime_fields)
            or len(set(runtime_fields)) != len(runtime_fields)
        ):
            raise _IntentDocumentError(
                "invalid_runtime_fields", "allowed_runtime_fields contains an invalid value"
            )
    if "schema_version" in document and (
        type(document["schema_version"]) is not int or document["schema_version"] != 1
    ):
        raise _IntentDocumentError("invalid_schema_version", "schema_version must be integer 1")
    return document


def _intent_failure(code: str, message: str) -> JsonObject:
    return {
        "authorization_created": False,
        "order_submitted": False,
        "validation_scope": "schema_and_canonical_hash_only",
        "valid": False,
        "error": {"code": code, "message": message},
    }


class ToolService:
    """Concrete implementation of the bounded, read-only tool surface."""

    def __init__(
        self,
        *,
        market_brief_reader: MarketBriefReader | None = None,
        market_transport: object | None = None,
        research_service: ResearchService | None = None,
        research_store_path: str | Path | None = None,
        staging_inbox: TradeStagingInbox | None = None,
        learning_ledger_path: str | Path | None = None,
        learning_ledger: LearningLedger | None = None,
        learning_quote_configured: bool = False,
    ) -> None:
        if research_service is not None and not isinstance(
            research_service, ResearchService
        ):
            raise TypeError("research_service must be ResearchService or None")
        if staging_inbox is not None and not isinstance(
            staging_inbox, TradeStagingInbox
        ):
            raise TypeError("staging_inbox must be TradeStagingInbox or None")
        if learning_ledger is not None and not isinstance(
            learning_ledger, LearningLedger
        ):
            raise TypeError("learning_ledger must be LearningLedger or None")
        if type(learning_quote_configured) is not bool:
            raise TypeError("learning_quote_configured must be bool")
        self._market_brief_reader = market_brief_reader or _default_market_brief_reader
        self._market_transport = market_transport
        self._research_service = research_service
        self._research_store_path = (
            default_state_database()
            if research_store_path is None
            else Path(research_store_path)
        )
        self._staging_inbox = staging_inbox
        self._learning_ledger_path = (
            self._research_store_path.with_name(
                self._research_store_path.name + ".learning.sqlite3"
            )
            if learning_ledger_path is None
            else Path(learning_ledger_path)
        )
        self._learning_ledger = learning_ledger
        self._learning_quote_configured = learning_quote_configured

    def _learning(self) -> LearningLedger:
        if self._learning_ledger is None:
            self._learning_ledger = LearningLedger(self._learning_ledger_path)
        return self._learning_ledger

    def _research(self) -> ResearchService:
        if self._research_service is None:
            self._research_service = ResearchService(
                ResearchStore(self._research_store_path),
                learning_recorder=LearningRecorder(
                    self._learning()
                ),
            )
        return self._research_service

    def _default_quote_decision(
        self,
        request: TrustedQuoteRequest,
    ) -> TrustedQuoteDecision:
        try:
            record = self._research().store.get_asset_analysis(
                request.expected_analysis_hash
            )
        except RecordNotFound:
            return TrustedQuoteDecision.blocked(block_code="analysis_not_found")
        if record.asset_id != request.asset_id:
            return TrustedQuoteDecision.blocked(
                block_code="analysis_asset_mismatch",
                analysis_hash=record.analysis_hash,
            )
        assessment = record.payload.get("assessment")
        if not isinstance(assessment, Mapping):
            return TrustedQuoteDecision.blocked(
                block_code="analysis_unavailable",
                analysis_hash=record.analysis_hash,
            )
        verdict = assessment.get("verdict")
        if verdict == "nothing":
            code = "nothing_to_trade"
        elif verdict == "unavailable":
            code = "analysis_unavailable"
        elif verdict in {"buy", "sell"}:
            code = "infrastructure_quote_profile_not_configured"
        else:
            code = "analysis_invalid"
        return TrustedQuoteDecision.blocked(
            block_code=code,
            analysis_hash=record.analysis_hash,
        )

    def _staging(self) -> TradeStagingInbox:
        if self._staging_inbox is None:
            staging_path = self._research_store_path.with_name(
                self._research_store_path.name + ".staging.sqlite3"
            )
            self._staging_inbox = TradeStagingInbox(
                staging_path,
                quote_callback=self._default_quote_decision,
            )
        return self._staging_inbox

    @staticmethod
    def _stage_view(view: StagingView) -> JsonObject:
        document = {
            **view.document.as_dict(),
            "document_hash": view.document.document_hash,
        }
        return {
            "schema_version": "trade_stage_view.v1",
            "document": document,
            "state": view.state.value,
            "expired_at": (
                None
                if view.expired_at is None
                else view.expired_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                )
            ),
            "latest_event_sequence": view.latest_event_sequence,
            "chain_hash": view.chain_hash,
            "authoritative": False,
        }

    @staticmethod
    def _review_data(value: object) -> JsonObject:
        converted = canonical_data(value)
        if not isinstance(converted, dict):
            raise TypeError("learning review did not canonicalize to an object")
        return converted

    @property
    def catalog(self) -> tuple[JsonObject, ...]:
        return tool_catalog()

    def get_harness_status(self) -> JsonObject:
        execution = disabled_executor().status
        safe = (
            execution.adapter == "disabled"
            and not execution.venue_writes_enabled
            and not execution.credential_loading_enabled
        )
        return {
            "component": "trading-harness",
            "mode": (
                "research_and_testnet_learning_staging"
                if self._learning_quote_configured
                else "research_only"
            ),
            "ok": safe,
            "execution": execution.as_dict(),
            "exposed_tools": sorted(_TOOL_NAMES),
            "market_data": {
                "access": "public_read_only",
                "credentials_required": False,
                "enabled": True,
                "networks": ["mainnet", "testnet"],
            },
            "research": {
                "asset_tracking": True,
                "enabled": True,
                "local_state_writes_enabled": True,
                "manual_browser_unattended_eligible": False,
                "registered_strategy": "candidate-v0/1",
            },
            "learning": {
                "staging_profile_configured": self._learning_quote_configured,
                "stage_is_authoritative": False,
                "approval_tool_exposed": False,
                "execution_tool_exposed": False,
                "mainnet_authorized": False,
            },
            "venue_writes_enabled": False,
            "credential_loading_enabled": False,
        }

    def get_market_brief(self, symbol: str, network: str) -> JsonObject:
        try:
            checked_symbol = _bounded_text(symbol, "symbol", 64)
        except _IntentDocumentError as error:
            raise ToolInputError(error.safe_message) from error
        if not isinstance(network, str) or network not in {"mainnet", "testnet"}:
            raise ToolInputError("network must be 'mainnet' or 'testnet'")
        result = self._market_brief_reader(
            checked_symbol,
            network,
            transport=self._market_transport,
        )
        converted = canonical_data(result)
        if not isinstance(converted, dict):
            raise TypeError("market brief reader must return an object")
        return converted

    def validate_trade_intent(self, intent: Mapping[str, Any]) -> JsonObject:
        if not isinstance(intent, Mapping):
            return _intent_failure("invalid_object", "intent must be an object")
        try:
            document = _validate_public_intent(intent)
        except _IntentDocumentError as error:
            return _intent_failure(error.code, error.safe_message)
        try:
            parsed = SemanticIntent.from_mapping(document)
            digest = semantic_intent_hash(parsed)
        except (KeyError, TypeError, ValueError):
            return _intent_failure(
                "semantic_intent_invalid",
                "intent failed semantic validation",
            )
        return {
            "authorization_created": False,
            "order_submitted": False,
            "validation_scope": "schema_and_canonical_hash_only",
            "valid": True,
            "algorithm": "sha256",
            "domain": SEMANTIC_INTENT_HASH_DOMAIN,
            "intent_hash": digest,
            "intent_id": parsed.intent_id,
            "environment": parsed.environment.value,
            "instrument": parsed.instrument,
        }

    @staticmethod
    def _canonical_result(value: object) -> JsonObject:
        converted = canonical_data(value)
        if not isinstance(converted, dict):
            raise TypeError("research service must return an object")
        return converted

    def track_asset(
        self,
        asset_id: object,
        symbol: object,
        network: object,
        sentiment_query: object,
        poll_seconds: object = 60,
    ) -> JsonObject:
        return self._canonical_result(
            self._research().track_asset(
                asset_id=asset_id,
                symbol=symbol,
                network=network,
                sentiment_query=sentiment_query,
                poll_seconds=poll_seconds,
            )
        )

    def pause_tracked_asset(
        self,
        asset_id: object,
        expected_revision: object,
    ) -> JsonObject:
        return self._canonical_result(
            self._research().pause_asset(
                asset_id=asset_id,
                expected_revision=expected_revision,
            )
        )

    def list_tracked_assets(self) -> JsonObject:
        return self._canonical_result(self._research().list_assets())

    def record_manual_sentiment(
        self,
        *,
        asset_id: object,
        window_start: object,
        window_end: object,
        evidence: object,
        excluded_count: object,
        collection_complete: object,
    ) -> JsonObject:
        return self._canonical_result(
            self._research().record_manual_sentiment(
                asset_id=asset_id,
                window_start=window_start,
                window_end=window_end,
                evidence=evidence,
                excluded_count=excluded_count,
                collection_complete=collection_complete,
            )
        )

    def get_latest_sentiment(self, asset_id: object) -> JsonObject:
        return self._canonical_result(self._research().latest_sentiment(asset_id))

    def analyze_asset(self, asset_id: object) -> JsonObject:
        return self._canonical_result(self._research().analyze_asset(asset_id))

    def validate_candidate_profitability(self, asset_id: object) -> JsonObject:
        return self._canonical_result(self._research().validate_candidate(asset_id))

    def stage_trade_candidate(
        self,
        asset_id: object,
        expected_analysis_hash: object,
        idempotency_key: object,
    ) -> JsonObject:
        view = self._staging().stage(
            {
                "asset_id": asset_id,
                "expected_analysis_hash": expected_analysis_hash,
                "idempotency_key": idempotency_key,
            }
        )
        if (
            view.state is StagingState.STAGED
            and isinstance(view.document.ticket_payload, Mapping)
            and view.document.ticket_payload.get("schema_version")
            == "infrastructure_learning_ticket.v1"
        ):
            LearningRecorder(self._learning()).record_staged_ticket(
                view.document.ticket_payload
            )
        return self._stage_view(view)

    def get_trade_stage(self, document_id: object) -> JsonObject:
        if not isinstance(document_id, str):
            raise ToolInputError("document_id must be a string")
        return self._stage_view(self._staging().get(document_id))

    def get_learning_review(self, cycle_id: object) -> JsonObject:
        try:
            checked = _bounded_text(cycle_id, "cycle_id", 128)
        except _IntentDocumentError as error:
            raise ToolInputError(error.safe_message) from error
        return self._review_data(
            PostTradeReviewer(self._learning()).review_cycle(checked)
        )

    def get_learning_summary(self) -> JsonObject:
        reviewer = PostTradeReviewer(self._learning())
        values = canonical_data(reviewer.aggregate_by_version())
        if not isinstance(values, list):
            raise TypeError("learning summary did not canonicalize to an array")
        return {
            "schema_version": "learning_summary.v1",
            "groups": values,
            "group_count": len(values),
            "interpretation_boundary": (
                "descriptive_association_only_no_causality_or_future_profitability_claim"
            ),
        }

    def get_node_status(self, node_id: object = "trading-desk-research") -> JsonObject:
        return self._canonical_result(self._research().get_node_status(node_id))

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        if tool_name not in _TOOL_NAMES:
            raise ToolInputError("unknown tool")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ToolInputError("tool arguments must be an object")
        supplied = dict(arguments)
        if tool_name == "get_harness_status":
            if supplied:
                raise ToolInputError("get_harness_status accepts no arguments")
            return self.get_harness_status()
        if tool_name == "get_market_brief":
            if set(supplied) != {"symbol", "network"}:
                raise ToolInputError("get_market_brief requires exactly symbol and network")
            return self.get_market_brief(supplied["symbol"], supplied["network"])
        if tool_name == "validate_trade_intent":
            if set(supplied) != {"intent"}:
                raise ToolInputError("validate_trade_intent requires exactly intent")
            intent = supplied["intent"]
            if not isinstance(intent, Mapping):
                return _intent_failure("invalid_object", "intent must be an object")
            return self.validate_trade_intent(intent)
        if tool_name == "track_asset":
            if not {"asset_id", "symbol", "network", "sentiment_query"}.issubset(
                supplied
            ) or set(supplied) - {
                "asset_id",
                "symbol",
                "network",
                "sentiment_query",
                "poll_seconds",
            }:
                raise ToolInputError("track_asset arguments are invalid")
            return self.track_asset(
                supplied["asset_id"],
                supplied["symbol"],
                supplied["network"],
                supplied["sentiment_query"],
                supplied.get("poll_seconds", 60),
            )
        if tool_name == "pause_tracked_asset":
            if set(supplied) != {"asset_id", "expected_revision"}:
                raise ToolInputError(
                    "pause_tracked_asset requires asset_id and expected_revision"
                )
            return self.pause_tracked_asset(
                supplied["asset_id"], supplied["expected_revision"]
            )
        if tool_name == "list_tracked_assets":
            if supplied:
                raise ToolInputError("list_tracked_assets accepts no arguments")
            return self.list_tracked_assets()
        if tool_name == "record_manual_sentiment":
            required = {
                "asset_id",
                "window_start",
                "window_end",
                "evidence",
                "excluded_count",
                "collection_complete",
            }
            if set(supplied) != required:
                raise ToolInputError("record_manual_sentiment arguments are invalid")
            return self.record_manual_sentiment(**supplied)
        if tool_name in {
            "get_latest_sentiment",
            "analyze_asset",
            "validate_candidate_profitability",
        }:
            if set(supplied) != {"asset_id"}:
                raise ToolInputError(f"{tool_name} requires exactly asset_id")
            return getattr(self, tool_name)(supplied["asset_id"])
        if tool_name == "stage_trade_candidate":
            required = {
                "asset_id",
                "expected_analysis_hash",
                "idempotency_key",
            }
            if set(supplied) != required:
                raise ToolInputError("stage_trade_candidate arguments are invalid")
            return self.stage_trade_candidate(
                supplied["asset_id"],
                supplied["expected_analysis_hash"],
                supplied["idempotency_key"],
            )
        if tool_name == "get_trade_stage":
            if set(supplied) != {"document_id"}:
                raise ToolInputError("get_trade_stage requires document_id")
            return self.get_trade_stage(supplied["document_id"])
        if tool_name == "get_learning_review":
            if set(supplied) != {"cycle_id"}:
                raise ToolInputError("get_learning_review requires cycle_id")
            return self.get_learning_review(supplied["cycle_id"])
        if tool_name == "get_learning_summary":
            if supplied:
                raise ToolInputError("get_learning_summary accepts no arguments")
            return self.get_learning_summary()
        if tool_name == "get_node_status":
            if set(supplied) - {"node_id"}:
                raise ToolInputError("get_node_status accepts only node_id")
            return self.get_node_status(
                supplied.get("node_id", "trading-desk-research")
            )
        raise ToolInputError("unknown tool")


__all__ = (
    "TOOL_CATALOG",
    "ToolDefinition",
    "ToolInputError",
    "ToolService",
    "tool_catalog",
)
