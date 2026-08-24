"""Strict parsing of Hyperliquid batch-order responses.

An HTTP 200 response and outer ``status: ok`` never imply a protected position.
This parser classifies every returned leg, preserves exact fill quantities, and
leaves venue/account reconciliation as an explicit mandatory next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import Enum
import hashlib
from typing import Any, Iterable

from .canonical import canonical_decimal, canonical_json, validate_decimal_bounds
from .errors import ValidationError


_ZERO = Decimal("0")


class SubmissionResponseError(ValidationError):
    """The venue response does not satisfy the reviewed order schema."""


class LegSubmissionState(str, Enum):
    FILLED = "filled"
    RESTING = "resting"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LegSubmissionResult:
    index: int
    state: LegSubmissionState
    requested_size: Decimal
    oid: int | None
    filled_size: Decimal
    average_price: Decimal | None
    venue_error: str | None

    @property
    def fully_filled(self) -> bool:
        return self.state is LegSubmissionState.FILLED and self.filled_size == self.requested_size

    @property
    def partially_filled(self) -> bool:
        return self.state is LegSubmissionState.FILLED and _ZERO < self.filled_size < self.requested_size

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "state": self.state.value,
            "requested_size": canonical_decimal(self.requested_size),
            "oid": self.oid,
            "filled_size": canonical_decimal(self.filled_size),
            "average_price": (
                None if self.average_price is None else canonical_decimal(self.average_price)
            ),
            "venue_error": self.venue_error,
            "fully_filled": self.fully_filled,
            "partially_filled": self.partially_filled,
        }


@dataclass(frozen=True, slots=True)
class BatchSubmissionResult:
    response_hash: str
    outer_status: str
    whole_batch_error: str | None
    legs: tuple[LegSubmissionResult, ...]
    requires_reconciliation: bool = True
    protected_position_confirmed: bool = False

    @property
    def entry_fully_filled(self) -> bool:
        return bool(self.legs) and self.legs[0].fully_filled

    @property
    def entry_partially_filled(self) -> bool:
        return bool(self.legs) and self.legs[0].partially_filled

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hyperliquid.batch_submission_result.v1",
            "response_hash": self.response_hash,
            "outer_status": self.outer_status,
            "whole_batch_error": self.whole_batch_error,
            "legs": [leg.as_dict() for leg in self.legs],
            "entry_fully_filled": self.entry_fully_filled,
            "entry_partially_filled": self.entry_partially_filled,
            "requires_reconciliation": self.requires_reconciliation,
            "protected_position_confirmed": self.protected_position_confirmed,
        }


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SubmissionResponseError(f"{field} must be a JSON object")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise SubmissionResponseError(f"{field} must be a JSON array")
    return value


def _exact_decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SubmissionResponseError(f"{field} must be an exact decimal string")
    try:
        result = Decimal(value)
        validate_decimal_bounds(result, field=field)
    except (DecimalException, ValueError) as error:
        raise SubmissionResponseError(f"{field} must be a bounded finite decimal") from error
    if result < _ZERO or (positive and result <= _ZERO):
        raise SubmissionResponseError(f"{field} has an invalid sign")
    return result


def _oid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SubmissionResponseError("order oid must be a non-negative integer")
    return value


def _venue_error(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise SubmissionResponseError("venue error must be bounded printable text")
    return value


def _requested_sizes(values: Iterable[Decimal]) -> tuple[Decimal, ...]:
    result = tuple(values)
    if not 1 <= len(result) <= 20:
        raise ValidationError("requested_sizes must contain one to twenty values")
    for index, value in enumerate(result):
        if not isinstance(value, Decimal):
            raise TypeError("requested sizes must be Decimal")
        validate_decimal_bounds(value, field=f"requested_sizes[{index}]")
        if value <= _ZERO:
            raise ValidationError("requested sizes must be positive")
    return result


def _response_hash(response: object) -> str:
    try:
        encoded = canonical_json(response).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SubmissionResponseError("response is not canonical JSON data") from error
    return hashlib.sha256(encoded).hexdigest()


def _parse_leg(index: int, value: object, requested: Decimal) -> LegSubmissionResult:
    item = _mapping(value, f"statuses[{index}]")
    if set(item) == {"error"}:
        return LegSubmissionResult(
            index=index,
            state=LegSubmissionState.ERROR,
            requested_size=requested,
            oid=None,
            filled_size=_ZERO,
            average_price=None,
            venue_error=_venue_error(item["error"]),
        )
    if set(item) == {"resting"}:
        resting = _mapping(item["resting"], f"statuses[{index}].resting")
        if set(resting) != {"oid"}:
            raise SubmissionResponseError("resting status has unsupported fields")
        return LegSubmissionResult(
            index=index,
            state=LegSubmissionState.RESTING,
            requested_size=requested,
            oid=_oid(resting["oid"]),
            filled_size=_ZERO,
            average_price=None,
            venue_error=None,
        )
    if set(item) == {"filled"}:
        filled = _mapping(item["filled"], f"statuses[{index}].filled")
        if set(filled) != {"totalSz", "avgPx", "oid"}:
            raise SubmissionResponseError("filled status has unsupported fields")
        size = _exact_decimal(filled["totalSz"], "filled.totalSz", positive=True)
        if size > requested:
            raise SubmissionResponseError("venue filled more than the requested size")
        return LegSubmissionResult(
            index=index,
            state=LegSubmissionState.FILLED,
            requested_size=requested,
            oid=_oid(filled["oid"]),
            filled_size=size,
            average_price=_exact_decimal(filled["avgPx"], "filled.avgPx", positive=True),
            venue_error=None,
        )
    raise SubmissionResponseError("order status has an unsupported shape")


def parse_order_response(
    response: object,
    *,
    requested_sizes: Iterable[Decimal],
) -> BatchSubmissionResult:
    """Parse one response without inferring account state or stop protection."""

    requested = _requested_sizes(requested_sizes)
    digest = _response_hash(response)
    root = _mapping(response, "exchange response")
    if set(root) != {"status", "response"}:
        raise SubmissionResponseError("exchange response has unsupported fields")
    status = root["status"]
    if status == "err":
        return BatchSubmissionResult(
            response_hash=digest,
            outer_status="err",
            whole_batch_error=_venue_error(root["response"]),
            legs=(),
        )
    if status != "ok":
        raise SubmissionResponseError("exchange response status is unsupported")
    body = _mapping(root["response"], "response")
    if set(body) != {"type", "data"} or body["type"] != "order":
        raise SubmissionResponseError("exchange response is not an order result")
    data = _mapping(body["data"], "response.data")
    if set(data) != {"statuses"}:
        raise SubmissionResponseError("order response data has unsupported fields")
    statuses = _array(data["statuses"], "response.data.statuses")
    if len(statuses) == 1 and len(requested) > 1:
        candidate = _mapping(statuses[0], "statuses[0]")
        if set(candidate) == {"error"}:
            return BatchSubmissionResult(
                response_hash=digest,
                outer_status="ok",
                whole_batch_error=_venue_error(candidate["error"]),
                legs=(),
            )
    if len(statuses) != len(requested):
        raise SubmissionResponseError("order response status count does not match request")
    legs = tuple(
        _parse_leg(index, value, requested[index])
        for index, value in enumerate(statuses)
    )
    return BatchSubmissionResult(
        response_hash=digest,
        outer_status="ok",
        whole_batch_error=None,
        legs=legs,
    )


__all__ = (
    "BatchSubmissionResult",
    "LegSubmissionResult",
    "LegSubmissionState",
    "SubmissionResponseError",
    "parse_order_response",
)
