"""Agent-neutral, bounded, read-only tools for every model interface.

This module has no dependency on MCP or a model SDK.  Protocol adapters use
the immutable catalog and :class:`ToolService`; the service repeats all
security-relevant validation instead of trusting client-advertised schemas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any

from .canonical import (
    SEMANTIC_INTENT_HASH_DOMAIN,
    canonical_data,
    semantic_intent_hash,
)
from .domain import SemanticIntent
from .executor import disabled_executor


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
        "venue_writes_enabled",
        "credential_loading_enabled",
    ],
    "properties": {
        "component": {"type": "string", "const": "trading-harness"},
        "mode": {"type": "string", "const": "read_only"},
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
                    "get_harness_status",
                    "get_market_brief",
                    "validate_trade_intent",
                ],
            },
            "minItems": 3,
            "maxItems": 3,
            "uniqueItems": True,
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
    ) -> None:
        self._market_brief_reader = market_brief_reader or _default_market_brief_reader
        self._market_transport = market_transport

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
            "mode": "read_only",
            "ok": safe,
            "execution": execution.as_dict(),
            "exposed_tools": sorted(_TOOL_NAMES),
            "market_data": {
                "access": "public_read_only",
                "credentials_required": False,
                "enabled": True,
                "networks": ["mainnet", "testnet"],
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
        if set(supplied) != {"intent"}:
            raise ToolInputError("validate_trade_intent requires exactly intent")
        intent = supplied["intent"]
        if not isinstance(intent, Mapping):
            return _intent_failure("invalid_object", "intent must be an object")
        return self.validate_trade_intent(intent)


__all__ = (
    "TOOL_CATALOG",
    "ToolDefinition",
    "ToolInputError",
    "ToolService",
    "tool_catalog",
)
