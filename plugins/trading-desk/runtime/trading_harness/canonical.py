"""Deterministic serialization and domain-separated hashing.

This module deliberately supports a small value vocabulary.  In particular,
binary floating point is rejected rather than silently rounded.  Prices,
quantities, fees, and limits enter the domain model as :class:`Decimal` values
and are emitted as normalized JSON strings.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


SEMANTIC_INTENT_HASH_DOMAIN = "trading-harness/semantic-intent/v1"

# Decimal inputs are deliberately much wider than the compiled capital limits,
# but still bounded before ``format(value, "f")`` can expand exponent notation
# into an attacker-controlled amount of text.  Arithmetic uses a separate,
# private fixed context in ``policy.py``.
MAX_DECIMAL_DIGITS = 96
MIN_DECIMAL_EXPONENT = -96
MAX_DECIMAL_EXPONENT = 48
MAX_DECIMAL_ADJUSTED_EXPONENT = 48


class CanonicalizationError(ValueError):
    """Raised when a value cannot be serialized without ambiguity."""


def validate_decimal_bounds(value: Decimal, *, field: str = "Decimal") -> Decimal:
    """Reject finite-but-pathological Decimals before expansion or arithmetic."""

    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise CanonicalizationError(f"{field} must be finite")
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # Defensive: specials were rejected above.
        raise CanonicalizationError(f"{field} has an invalid exponent")
    if len(digits) > MAX_DECIMAL_DIGITS:
        raise CanonicalizationError(
            f"{field} exceeds {MAX_DECIMAL_DIGITS} coefficient digits"
        )
    if exponent < MIN_DECIMAL_EXPONENT or exponent > MAX_DECIMAL_EXPONENT:
        raise CanonicalizationError(f"{field} exponent is outside supported bounds")
    if not value.is_zero() and value.adjusted() > MAX_DECIMAL_ADJUSTED_EXPONENT:
        raise CanonicalizationError(
            f"{field} integer magnitude is outside supported bounds"
        )
    return value


def canonical_decimal(value: Decimal) -> str:
    """Return the unique, non-exponent JSON string for a finite Decimal.

    Numerically equivalent values have the same representation: ``1.2300``
    becomes ``"1.23"`` and every signed or exponent-form zero becomes
    ``"0"``.  The function intentionally accepts only ``Decimal``; callers
    must never pass monetary values through a float first.
    """

    if not isinstance(value, Decimal):
        raise TypeError("canonical_decimal requires Decimal")
    validate_decimal_bounds(value)
    if value.is_zero():
        return "0"

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("naive datetimes are forbidden")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_data(value: Any) -> Any:
    """Convert a supported object into a JSON-native canonical value.

    Dataclass field names form the schema.  Mapping keys must be strings;
    unordered sets are sorted by their own canonical JSON encodings.  Floats
    are rejected everywhere so a future monetary field cannot accidentally
    bypass the exact-decimal rule.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return canonical_data(value.value)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical mapping keys must be strings")
            converted[key] = canonical_data(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [canonical_data(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted_items = [canonical_data(item) for item in value]
        return sorted(
            converted_items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    if isinstance(value, float):
        raise CanonicalizationError("binary floating point is forbidden")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise CanonicalizationError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Serialize a supported value as compact, key-sorted UTF-8 JSON text."""

    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical JSON encoding used by all harness hashes."""

    return canonical_json(value).encode("utf-8")


def domain_hash(domain: str, value: Any) -> str:
    """Hash ``value`` under an explicit, NUL-delimited protocol domain."""

    if not isinstance(domain, str) or not domain or "\x00" in domain:
        raise ValueError("hash domain must be a non-empty string without NUL")
    material = domain.encode("utf-8") + b"\x00" + canonical_bytes(value)
    return hashlib.sha256(material).hexdigest()


def semantic_intent_hash(intent: Any) -> str:
    """Return the versioned semantic-intent SHA-256 digest.

    The local import avoids a domain/canonicalization import cycle while still
    preventing callers from blessing an arbitrary mapping as an order intent.
    """

    from .domain import SemanticIntent

    if not isinstance(intent, SemanticIntent):
        raise TypeError("semantic_intent_hash requires SemanticIntent")
    return domain_hash(SEMANTIC_INTENT_HASH_DOMAIN, intent)
