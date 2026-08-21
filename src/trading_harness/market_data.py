"""Read-only, exact-decimal Hyperliquid market briefs.

This module is deliberately smaller than a general Hyperliquid client.  It
can only POST two public request types to an allowlisted ``/info`` endpoint:
``metaAndAssetCtxs`` and ``l2Book``.  It has no account, signing, credential,
or exchange-write functionality.

All exchange decimal strings are parsed as :class:`~decimal.Decimal`,
validated, used for calculations without binary floating point, and emitted
as canonical JSON strings.  The public result can therefore be passed
directly across an MCP boundary with ``json.dumps``.

``metaAndAssetCtxs`` has no exchange timestamp, so its fields are labelled
with local receipt time only.  ``l2Book.time`` is reported separately as the
book observation time.  Context and book mids must agree within 25 bps, and
depth completeness is reported per side because Hyperliquid caps each side at
20 returned levels.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, localcontext
import json
import re
from types import MappingProxyType
from typing import TypeAlias
from urllib import error as urlerror
from urllib import request as urlrequest

from .canonical import canonical_decimal, validate_decimal_bounds
from .errors import HarnessError, ValidationError


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
InfoTransport: TypeAlias = Callable[[str, Mapping[str, JSONValue]], object]
Clock: TypeAlias = Callable[[], datetime]

_INFO_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "mainnet": "https://api.hyperliquid.xyz/info",
        "testnet": "https://api.hyperliquid-testnet.xyz/info",
    }
)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_DEPTH_BANDS_BPS = (5, 10, 25)
_L2_LEVEL_CAP_PER_SIDE = 20
_HTTP_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# The desk's market-analyst contract treats a book older than one minute as
# unknown.  A small forward allowance prevents normal host/exchange clock skew
# from turning a fresh read into a false failure while still rejecting a bad
# or replayed future timestamp.
_MAX_BOOK_AGE_MS = 60_000
_MAX_FUTURE_SKEW_MS = 5_000
# If the separately fetched context mid and book mid differ by more than the
# widest depth band, combining them into one actionable brief is unsafe.  The
# exact comparison is cross-multiplied; no rounded display value decides it.
_MAX_CONTEXT_BOOK_DIVERGENCE_BPS = Decimal("25")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EXACT_ARITHMETIC_CONTEXT = Context(prec=256)
_DISPLAY_ARITHMETIC_CONTEXT = Context(prec=34)


class MarketDataError(HarnessError):
    """Base class for expected public market-data failures."""


class MarketDataTransportError(MarketDataError):
    """The allowlisted public ``/info`` endpoint could not be read."""


class MarketDataResponseError(MarketDataError, ValueError):
    """Hyperliquid returned data that does not satisfy the expected schema."""


class _RejectRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Stop urllib before it follows any redirect from an allowlisted host."""

    def redirect_request(
        self,
        request: urlrequest.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        raise MarketDataTransportError(
            "Hyperliquid info endpoint attempted a forbidden redirect"
        )


def _default_transport(
    endpoint: str, payload: Mapping[str, JSONValue]
) -> object:
    """POST a JSON request to one of the two compiled-in public endpoints."""

    if endpoint not in _INFO_ENDPOINTS.values():
        raise MarketDataTransportError("refusing a non-allowlisted info endpoint")

    encoded = json.dumps(
        dict(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    http_request = urlrequest.Request(
        endpoint,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "trading-harness-market-data/1",
        },
        method="POST",
    )

    try:
        # urllib's global opener follows redirects by default.  A private
        # opener with an explicit rejecting handler preserves the endpoint
        # allowlist before a second outbound request can occur.
        opener = urlrequest.build_opener(_RejectRedirectHandler())
        with opener.open(http_request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if final_url != endpoint:
                raise MarketDataTransportError(
                    "Hyperliquid info request redirected away from its allowlisted URL"
                )
            status = getattr(response, "status", None)
            if status != 200:
                raise MarketDataTransportError(
                    f"Hyperliquid info request returned HTTP {status!r}"
                )
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except MarketDataTransportError:
        raise
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError, OSError) as error:
        raise MarketDataTransportError(
            f"Hyperliquid info request failed: {type(error).__name__}"
        ) from error

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise MarketDataTransportError("Hyperliquid info response exceeded size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise MarketDataResponseError(
            "Hyperliquid info response could not be safely decoded as UTF-8 JSON"
        ) from error


def _post_info(
    endpoint: str,
    payload: Mapping[str, JSONValue],
    transport: InfoTransport,
) -> object:
    try:
        return transport(endpoint, payload)
    except MarketDataError:
        raise
    except Exception as error:
        # Transport implementations are an integration boundary.  Do not leak
        # response bodies, headers, or arbitrary exception text to the caller.
        raise MarketDataTransportError(
            f"market-data transport failed: {type(error).__name__}"
        ) from error


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MarketDataResponseError(f"{field} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise MarketDataResponseError(f"{field} must have string keys")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise MarketDataResponseError(f"{field} must be a JSON array")
    return value


def _exact_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    # Hyperliquid's numeric market fields are documented as decimal strings.
    # Rejecting JSON numbers prevents an injected decoder from introducing a
    # binary float before this trust boundary.
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketDataResponseError(f"{field} must be an exact decimal string")
    try:
        result = Decimal(value)
        validate_decimal_bounds(result, field=field)
    except (DecimalException, ValueError) as error:
        raise MarketDataResponseError(f"{field} must be a bounded finite decimal") from error
    if positive and result <= 0:
        raise MarketDataResponseError(f"{field} must be greater than zero")
    if nonnegative and result < 0:
        raise MarketDataResponseError(f"{field} must be non-negative")
    return result


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataResponseError(f"{field} must be an integer")
    if value < minimum:
        raise MarketDataResponseError(f"{field} must be at least {minimum}")
    return value


def _clock_read(clock: Clock) -> datetime:
    """Read one local receipt instant and normalize it to UTC."""

    try:
        received_at = clock()
    except Exception as error:
        raise ValidationError(f"clock failed: {type(error).__name__}") from error
    if not isinstance(received_at, datetime):
        raise ValidationError("clock must return datetime")
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValidationError("clock must return a timezone-aware datetime")
    try:
        return received_at.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError("clock returned an unsupported datetime") from error


def _market_context(
    response: object, requested_symbol: str
) -> tuple[str, dict[str, Decimal]]:
    root = _array(response, "metaAndAssetCtxs response")
    if len(root) != 2:
        raise MarketDataResponseError(
            "metaAndAssetCtxs response must contain metadata and contexts"
        )
    metadata = _mapping(root[0], "metaAndAssetCtxs metadata")
    universe = _array(metadata.get("universe"), "metaAndAssetCtxs universe")
    contexts = _array(root[1], "metaAndAssetCtxs contexts")
    if not universe or len(universe) != len(contexts):
        raise MarketDataResponseError(
            "metaAndAssetCtxs universe and contexts must be non-empty and aligned"
        )

    requested_key = requested_symbol.casefold()
    matches: list[tuple[str, dict[str, object]]] = []
    seen_names: set[str] = set()
    for index, raw_asset in enumerate(universe):
        asset = _mapping(raw_asset, f"metaAndAssetCtxs universe[{index}]")
        name = asset.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or not _SYMBOL_RE.fullmatch(name)
        ):
            raise MarketDataResponseError(
                f"metaAndAssetCtxs universe[{index}].name is invalid"
            )
        folded = name.casefold()
        if folded in seen_names:
            raise MarketDataResponseError(
                "metaAndAssetCtxs universe contains duplicate symbols"
            )
        seen_names.add(folded)
        if folded == requested_key:
            matches.append(
                (name, _mapping(contexts[index], f"metaAndAssetCtxs contexts[{index}]"))
            )

    if len(matches) != 1:
        raise MarketDataResponseError(
            f"symbol {requested_symbol!r} was not uniquely present in perp metadata"
        )

    canonical_symbol, context = matches[0]
    values = {
        "mid": _exact_decimal(context.get("midPx"), "midPx", positive=True),
        "mark": _exact_decimal(context.get("markPx"), "markPx", positive=True),
        "oracle": _exact_decimal(
            context.get("oraclePx"), "oraclePx", positive=True
        ),
        "funding_hourly": _exact_decimal(context.get("funding"), "funding"),
        "open_interest": _exact_decimal(
            context.get("openInterest"), "openInterest", nonnegative=True
        ),
        "day_notional_volume": _exact_decimal(
            context.get("dayNtlVlm"), "dayNtlVlm", nonnegative=True
        ),
    }
    return canonical_symbol, values


def _book_level(value: object, side: str, index: int) -> tuple[Decimal, Decimal]:
    level = _mapping(value, f"l2Book {side}[{index}]")
    price = _exact_decimal(level.get("px"), f"l2Book {side}[{index}].px", positive=True)
    size = _exact_decimal(level.get("sz"), f"l2Book {side}[{index}].sz", positive=True)
    _integer(level.get("n"), f"l2Book {side}[{index}].n", minimum=1)
    return price, size


def _book_snapshot(
    response: object,
    symbol: str,
    *,
    received_at: datetime,
) -> dict[str, object]:
    root = _mapping(response, "l2Book response")
    response_symbol = root.get("coin")
    if response_symbol != symbol:
        raise MarketDataResponseError(
            f"l2Book coin must exactly match canonical symbol {symbol!r}"
        )
    time_ms = _integer(root.get("time"), "l2Book time", minimum=0)
    levels = _array(root.get("levels"), "l2Book levels")
    if len(levels) != 2:
        raise MarketDataResponseError("l2Book levels must contain bids and asks")

    raw_bids = _array(levels[0], "l2Book bids")
    raw_asks = _array(levels[1], "l2Book asks")
    if not raw_bids or not raw_asks:
        raise MarketDataResponseError("l2Book bids and asks must both be non-empty")
    if (
        len(raw_bids) > _L2_LEVEL_CAP_PER_SIDE
        or len(raw_asks) > _L2_LEVEL_CAP_PER_SIDE
    ):
        raise MarketDataResponseError(
            f"l2Book exceeds the {_L2_LEVEL_CAP_PER_SIDE}-level per-side cap"
        )

    bids = [_book_level(level, "bids", index) for index, level in enumerate(raw_bids)]
    asks = [_book_level(level, "asks", index) for index, level in enumerate(raw_asks)]
    if any(left[0] <= right[0] for left, right in zip(bids, bids[1:])):
        raise MarketDataResponseError("l2Book bids must be strictly descending")
    if any(left[0] >= right[0] for left, right in zip(asks, asks[1:])):
        raise MarketDataResponseError("l2Book asks must be strictly ascending")

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_bid >= best_ask:
        raise MarketDataResponseError("l2Book is crossed or locked")

    try:
        # Inputs are bounded to 96 coefficient digits.  A wider local context
        # makes midpoint, sums, and band comparisons deterministic and avoids
        # dependence on an application's process-global Decimal context.
        with localcontext(_EXACT_ARITHMETIC_CONTEXT):
            midpoint = (best_bid + best_ask) / Decimal(2)
            depth: dict[str, dict[str, str | bool]] = {}
            for band in _DEPTH_BANDS_BPS:
                bound = midpoint * Decimal(band)
                bid_size = sum(
                    (size for price, size in bids if (midpoint - price) * 10_000 <= bound),
                    start=Decimal(0),
                )
                ask_size = sum(
                    (size for price, size in asks if (price - midpoint) * 10_000 <= bound),
                    start=Decimal(0),
                )
                depth[f"{band}bps"] = {
                    "bid_size": canonical_decimal(bid_size),
                    "ask_size": canonical_decimal(ask_size),
                    # Hyperliquid returns at most 20 levels per side.  With a
                    # full side, depth is provably complete only when the last
                    # returned level lies outside this band; otherwise unseen
                    # levels may also belong to the band.
                    "bid_complete": (
                        len(bids) < _L2_LEVEL_CAP_PER_SIDE
                        or (midpoint - bids[-1][0]) * 10_000 > bound
                    ),
                    "ask_complete": (
                        len(asks) < _L2_LEVEL_CAP_PER_SIDE
                        or (asks[-1][0] - midpoint) * 10_000 > bound
                    ),
                }
    except (DecimalException, ValueError) as error:
        raise MarketDataResponseError("l2Book decimal calculation failed") from error

    try:
        seconds, milliseconds = divmod(time_ms, 1000)
        observed = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
            milliseconds=milliseconds
        )
    except (OverflowError, OSError, ValueError) as error:
        raise MarketDataResponseError("l2Book time is outside the supported range") from error

    since_epoch = received_at - _EPOCH
    received_us = (
        (since_epoch.days * 86_400 + since_epoch.seconds) * 1_000_000
        + since_epoch.microseconds
    )
    age_us = received_us - time_ms * 1_000
    if age_us > _MAX_BOOK_AGE_MS * 1_000:
        raise MarketDataResponseError(
            f"l2Book is stale by more than {_MAX_BOOK_AGE_MS} ms"
        )
    if age_us < -_MAX_FUTURE_SKEW_MS * 1_000:
        raise MarketDataResponseError(
            f"l2Book is future-dated by more than {_MAX_FUTURE_SKEW_MS} ms"
        )
    # Age is a non-negative freshness measure.  A tolerated sub-threshold
    # future skew is represented as zero rather than a misleading negative age.
    age_ms = 0 if age_us <= 0 else (age_us + 999) // 1_000

    return {
        "time_ms": time_ms,
        "observed_at": observed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "received_at": received_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "age_ms": age_ms,
        "mid": canonical_decimal(midpoint),
        "_mid_decimal": midpoint,
        "best_bid": canonical_decimal(best_bid),
        "best_ask": canonical_decimal(best_ask),
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "level_cap_per_side": _L2_LEVEL_CAP_PER_SIDE,
        "depth": depth,
    }


def _mid_consistency(
    context_mid: Decimal,
    book_mid: Decimal,
) -> dict[str, object]:
    """Validate and describe context/book divergence without float rounding.

    The availability decision uses the exact inequality
    ``difference * 10000 <= book_mid * threshold_bps``.  Because a bps ratio
    can be a repeating decimal, the response preserves its exact rational
    representation and also supplies a 34-significant-digit display decimal.
    """

    try:
        with localcontext(_EXACT_ARITHMETIC_CONTEXT):
            difference = abs(context_mid - book_mid)
            within_limit = (
                difference * Decimal(10_000)
                <= book_mid * _MAX_CONTEXT_BOOK_DIVERGENCE_BPS
            )
        if not within_limit:
            raise MarketDataResponseError(
                "market context and l2Book mids diverge by more than "
                f"{canonical_decimal(_MAX_CONTEXT_BOOK_DIVERGENCE_BPS)} bps"
            )
        with localcontext(_DISPLAY_ARITHMETIC_CONTEXT):
            divergence_bps = difference * Decimal(10_000) / book_mid
        return {
            "context_mid": canonical_decimal(context_mid),
            "book_mid": canonical_decimal(book_mid),
            "absolute_difference": canonical_decimal(difference),
            "divergence_bps": canonical_decimal(divergence_bps),
            "divergence_bps_exact": {
                "numerator": canonical_decimal(difference),
                "denominator": canonical_decimal(book_mid),
                "multiplier": "10000",
            },
            "divergence_bps_display_precision_digits": 34,
            "max_divergence_bps": canonical_decimal(
                _MAX_CONTEXT_BOOK_DIVERGENCE_BPS
            ),
            "comparison": "difference*10000 <= book_mid*max_divergence_bps",
            "within_limit": True,
        }
    except MarketDataResponseError:
        raise
    except (DecimalException, ValueError) as error:
        raise MarketDataResponseError(
            "context/book mid consistency calculation failed"
        ) from error


def get_market_brief(
    symbol: str,
    network: str,
    transport: InfoTransport | None = None,
    *,
    clock: Clock | None = None,
) -> dict[str, object]:
    """Fetch and validate a JSON-safe Hyperliquid perpetual market brief.

    ``network`` must be exactly ``"mainnet"`` or ``"testnet"``.  The
    optional transport receives ``(allowlisted_info_url, request_mapping)``
    and returns already-decoded JSON; it exists for deterministic tests and
    controlled host integration.  ``clock`` is an optional UTC-aware receipt
    clock for deterministic testing.  Regardless of either injection, this
    function emits only the two public, unsigned ``/info`` request types
    documented at module level.

    Monetary and quantity values in the returned mapping are exact decimal
    strings.  Depth is base-asset size on each side within 5, 10, and 25 bps
    of the fresh order-book midpoint.  Each total includes a completeness flag:
    a full 20-level side whose last level remains inside the band is potentially
    truncated.  Top-level ``observed_at``, ``received_at``, and ``age_ms`` are
    compatibility aliases for the book only; ``timestamps`` explicitly scopes
    context and book time bases.
    """

    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise ValidationError("symbol must be a non-empty, trimmed string")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValidationError("symbol contains unsupported characters")
    if not isinstance(network, str) or network not in _INFO_ENDPOINTS:
        raise ValidationError("network must be exactly 'mainnet' or 'testnet'")
    if transport is not None and not callable(transport):
        raise TypeError("transport must be callable or None")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable or None")

    endpoint = _INFO_ENDPOINTS[network]
    selected_transport = transport or _default_transport
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    context_response = _post_info(
        endpoint, {"type": "metaAndAssetCtxs"}, selected_transport
    )
    context_received_at = _clock_read(selected_clock)
    canonical_symbol, context = _market_context(context_response, symbol)
    book_response = _post_info(
        endpoint,
        {"type": "l2Book", "coin": canonical_symbol},
        selected_transport,
    )
    book_received_at = _clock_read(selected_clock)
    book = _book_snapshot(
        book_response,
        canonical_symbol,
        received_at=book_received_at,
    )
    book_mid = book["_mid_decimal"]
    if not isinstance(book_mid, Decimal):  # Defensive internal invariant.
        raise MarketDataResponseError("l2Book midpoint calculation is unavailable")
    mid_consistency = _mid_consistency(context["mid"], book_mid)

    context_received_text = context_received_at.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    return {
        "schema_version": "hyperliquid.market_brief.v1",
        "venue": "hyperliquid",
        "network": network,
        "symbol": canonical_symbol,
        "observed_at": book["observed_at"],
        "received_at": book["received_at"],
        "age_ms": book["age_ms"],
        "context_received_at": context_received_text,
        "timestamps": {
            "market_context": {
                "exchange_observed_at": None,
                "received_at": context_received_text,
                "basis": "local_receipt_only",
                "reason": "metaAndAssetCtxs has no exchange timestamp",
            },
            "book": {
                "observed_at": book["observed_at"],
                "received_at": book["received_at"],
                "age_ms": book["age_ms"],
                "basis": "hyperliquid_l2Book.time",
            },
            "top_level_observed_at_scope": "book_only",
        },
        "sources": [
            {
                "url": endpoint,
                "endpoint": "/info",
                "request_type": "metaAndAssetCtxs",
            },
            {
                "url": endpoint,
                "endpoint": "/info",
                "request_type": "l2Book",
            },
        ],
        "mid": canonical_decimal(context["mid"]),
        "mark": canonical_decimal(context["mark"]),
        "oracle": canonical_decimal(context["oracle"]),
        "funding_hourly": canonical_decimal(context["funding_hourly"]),
        "open_interest": canonical_decimal(context["open_interest"]),
        "day_notional_volume": canonical_decimal(context["day_notional_volume"]),
        "mid_consistency": mid_consistency,
        "book": {
            "time_ms": book["time_ms"],
            "mid": book["mid"],
            "best_bid": book["best_bid"],
            "best_ask": book["best_ask"],
            "bid_level_count": book["bid_level_count"],
            "ask_level_count": book["ask_level_count"],
            "level_cap_per_side": book["level_cap_per_side"],
            "depth": book["depth"],
        },
    }


__all__ = (
    "Clock",
    "InfoTransport",
    "MarketDataError",
    "MarketDataResponseError",
    "MarketDataTransportError",
    "get_market_brief",
)
