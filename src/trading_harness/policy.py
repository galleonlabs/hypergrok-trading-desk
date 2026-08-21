"""Non-bypassable platform ceilings and stricter account risk policies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_CEILING,
    localcontext,
)
from typing import Any, Mapping

from .canonical import (
    CanonicalizationError,
    canonical_decimal,
    semantic_intent_hash,
    validate_decimal_bounds,
)
from .domain import SemanticIntent, Side
from .errors import PolicyViolation, ValidationError


ZERO = Decimal("0")
BASIS_POINTS = Decimal("10000")

# No caller or ambient thread-local Decimal context may alter capital math.
# ROUND_CEILING is conservative for the non-negative exposure calculations
# performed by this module and by the store allocation helpers.
_FIXED_DECIMAL_CONTEXT = Context(
    prec=96,
    rounding=ROUND_CEILING,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)
for _signal in (InvalidOperation, DivisionByZero, Overflow):
    _FIXED_DECIMAL_CONTEXT.traps[_signal] = True

# This is a compiled platform capability, not configurable grant or policy
# data.  A wildcard can narrow to this set; it can never widen it.
FOUNDATION_ALLOWED_ACTIONS = frozenset({"simulate_order"})


def exact_decimal(value: Decimal | str | int, *, field: str) -> Decimal:
    """Return a finite :class:`Decimal`, explicitly rejecting binary floats."""

    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"{field} must use Decimal, a decimal string, or int")
    if isinstance(value, str) and (not value or value != value.strip()):
        raise ValidationError(f"{field} must be a non-empty, trimmed decimal string")
    if not isinstance(value, Decimal):
        try:
            value = Decimal(value)
        except (ArithmeticError, ValueError, TypeError) as exc:
            raise ValidationError(f"{field} is not a valid decimal") from exc
    if not value.is_finite():
        raise ValidationError(f"{field} must be finite")
    try:
        validate_decimal_bounds(value, field=field)
    except CanonicalizationError as exc:
        raise ValidationError(str(exc)) from exc
    return value


def _checked_result(value: Decimal, *, field: str) -> Decimal:
    try:
        return validate_decimal_bounds(value, field=field)
    except CanonicalizationError as exc:
        raise ValidationError(str(exc)) from exc


def decimal_add(*values: Decimal, field: str = "decimal sum") -> Decimal:
    """Add exact values under the harness-owned context."""

    with localcontext(_FIXED_DECIMAL_CONTEXT) as context:
        result = ZERO
        for value in values:
            result = context.add(result, exact_decimal(value, field=field))
    return _checked_result(result, field=field)


def decimal_subtract(
    left: Decimal, right: Decimal, *, field: str = "decimal difference"
) -> Decimal:
    with localcontext(_FIXED_DECIMAL_CONTEXT) as context:
        result = context.subtract(
            exact_decimal(left, field=field), exact_decimal(right, field=field)
        )
    return _checked_result(result, field=field)


def decimal_multiply(
    left: Decimal, right: Decimal, *, field: str = "decimal product"
) -> Decimal:
    with localcontext(_FIXED_DECIMAL_CONTEXT) as context:
        result = context.multiply(
            exact_decimal(left, field=field), exact_decimal(right, field=field)
        )
    return _checked_result(result, field=field)


def decimal_divide(
    numerator: Decimal, denominator: Decimal, *, field: str = "decimal quotient"
) -> Decimal:
    denominator = exact_decimal(denominator, field=field)
    if denominator == ZERO:
        raise ValidationError(f"{field} denominator must not be zero")
    with localcontext(_FIXED_DECIMAL_CONTEXT) as context:
        result = context.divide(exact_decimal(numerator, field=field), denominator)
    return _checked_result(result, field=field)


def _positive(value: Decimal | str | int, *, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed <= ZERO:
        raise ValidationError(f"{field} must be greater than zero")
    return parsed


def _nonnegative(value: Decimal | str | int, *, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed < ZERO:
        raise ValidationError(f"{field} must not be negative")
    return parsed


@dataclass(frozen=True, slots=True)
class PlatformCeilings:
    """Absolute limits compiled into this harness release.

    Units are the account's settlement currency except for quantity,
    leverage and basis-point fields.  A deployment may choose lower values,
    but callers cannot raise these values through a grant or authorization.
    """

    version: str
    max_order_quantity: Decimal
    max_order_notional: Decimal
    max_order_worst_case_loss: Decimal
    max_account_gross_notional: Decimal
    max_account_worst_case_loss: Decimal
    max_leverage: Decimal
    max_slippage_bps: Decimal
    max_fee_bps: Decimal

    def __post_init__(self) -> None:
        if not self.version:
            raise ValidationError("platform ceiling version is required")
        for name in (
            "max_order_quantity",
            "max_order_notional",
            "max_account_gross_notional",
            "max_leverage",
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), field=name))
        for name in (
            "max_order_worst_case_loss",
            "max_account_worst_case_loss",
            "max_slippage_bps",
            "max_fee_bps",
        ):
            object.__setattr__(
                self, name, _nonnegative(getattr(self, name), field=name)
            )
        if self.max_order_notional > self.max_account_gross_notional:
            raise ValidationError("order notional ceiling exceeds account ceiling")
        if self.max_order_worst_case_loss > self.max_account_worst_case_loss:
            raise ValidationError("order loss ceiling exceeds account ceiling")


# These are fail-safe absolute caps, not recommended trading limits.  Every
# account grant must install a RiskPolicy whose limits are equal or stricter.
HARD_PLATFORM_CEILINGS = PlatformCeilings(
    version="platform-ceilings-v1",
    max_order_quantity=Decimal("1000000"),
    max_order_notional=Decimal("1000000"),
    max_order_worst_case_loss=Decimal("250000"),
    max_account_gross_notional=Decimal("5000000"),
    max_account_worst_case_loss=Decimal("500000"),
    max_leverage=Decimal("20"),
    max_slippage_bps=Decimal("2500"),
    max_fee_bps=Decimal("1000"),
)


@dataclass(frozen=True, slots=True)
class ExposureQuote:
    """Trusted, semantic-intent-bound worst-case exposure assessment."""

    intent_hash: str
    quantity: Decimal
    notional: Decimal
    worst_case_loss: Decimal
    slippage_bps: Decimal = ZERO
    fee_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.intent_hash:
            raise ValidationError("exposure quote requires an intent hash")
        object.__setattr__(self, "quantity", _positive(self.quantity, field="quantity"))
        object.__setattr__(self, "notional", _positive(self.notional, field="notional"))
        object.__setattr__(
            self,
            "worst_case_loss",
            _nonnegative(self.worst_case_loss, field="worst_case_loss"),
        )
        object.__setattr__(
            self,
            "slippage_bps",
            _nonnegative(self.slippage_bps, field="slippage_bps"),
        )
        object.__setattr__(
            self,
            "fee_bps",
            _nonnegative(self.fee_bps, field="fee_bps"),
        )


def derive_exposure_quote(intent: SemanticIntent) -> ExposureQuote:
    """Derive the only exposure quote accepted by foundation admission.

    The bound is the worst permitted execution price, not an indicative or
    last-traded price.  A stop trigger alone never reduces the reserve because
    triggering does not prove an executable exit.  Only an explicit
    ``protection_limit_price`` (the worst executable protection bound) can
    reduce full-notional loss.  Slippage and fee buffers are then added.
    """

    if not isinstance(intent, SemanticIntent):
        raise TypeError("derive_exposure_quote requires SemanticIntent")
    if intent.price_bound is None:
        raise ValidationError("price_bound is required for exposure derivation")
    if intent.leverage is None:
        raise ValidationError("explicit leverage is required for exposure derivation")

    bound = exact_decimal(intent.price_bound, field="price_bound")
    quantity = _positive(intent.quantity, field="quantity")
    if intent.limit_price is not None:
        if intent.side is Side.BUY and bound < intent.limit_price:
            raise ValidationError("buy price_bound must not be below limit_price")
        if intent.side is Side.SELL and bound > intent.limit_price:
            raise ValidationError("sell price_bound must not be above limit_price")

    notional = decimal_multiply(quantity, bound, field="derived notional")
    base_loss = notional
    if intent.protection_limit_price is not None:
        if intent.side is Side.BUY:
            if intent.protection_limit_price >= bound:
                raise ValidationError(
                    "buy protection_limit_price must be below price_bound"
                )
            distance = decimal_subtract(
                bound,
                intent.protection_limit_price,
                field="buy protection distance",
            )
        else:
            if intent.protection_limit_price <= bound:
                raise ValidationError(
                    "sell protection_limit_price must be above price_bound"
                )
            distance = decimal_subtract(
                intent.protection_limit_price,
                bound,
                field="sell protection distance",
            )
        base_loss = decimal_multiply(
            quantity, distance, field="protection-bounded loss"
        )

    total_bps = decimal_add(
        intent.max_slippage_bps,
        intent.fee_bps,
        field="risk buffer basis points",
    )
    buffer = decimal_divide(
        decimal_multiply(notional, total_bps, field="risk buffer numerator"),
        BASIS_POINTS,
        field="risk buffer",
    )
    return ExposureQuote(
        intent_hash=semantic_intent_hash(intent),
        quantity=quantity,
        notional=notional,
        worst_case_loss=decimal_add(base_loss, buffer, field="worst-case loss"),
        slippage_bps=intent.max_slippage_bps,
        fee_bps=intent.fee_bps,
    )


@dataclass(frozen=True, slots=True)
class AccountExposure:
    """Booked position exposure plus unconsumed order reservations."""

    reserved_notional: Decimal = ZERO
    reserved_loss: Decimal = ZERO
    booked_notional: Decimal = ZERO
    booked_loss: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in (
            "reserved_notional",
            "reserved_loss",
            "booked_notional",
            "booked_loss",
        ):
            object.__setattr__(self, name, _nonnegative(getattr(self, name), field=name))

    @property
    def gross_notional(self) -> Decimal:
        return decimal_add(
            self.reserved_notional,
            self.booked_notional,
            field="account gross notional",
        )

    @property
    def worst_case_loss(self) -> Decimal:
        return decimal_add(
            self.reserved_loss,
            self.booked_loss,
            field="account worst-case loss",
        )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Versioned limits carried by a deployment grant.

    The constructor accepts only exact decimal types.  ``validate_ceiling``
    is called both when a grant is persisted and during admission so a stale
    or manually altered database row cannot widen platform authority.
    """

    policy_id: str
    version: str
    max_order_quantity: Decimal
    max_order_notional: Decimal
    max_order_worst_case_loss: Decimal
    max_account_gross_notional: Decimal
    max_account_worst_case_loss: Decimal
    max_leverage: Decimal
    max_slippage_bps: Decimal
    max_fee_bps: Decimal
    allowed_instruments: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ("simulate_order",)
    allowed_order_types: tuple[str, ...] = ("market", "limit", "stop", "stop_limit")

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValidationError("policy_id and version are required")
        for name in (
            "max_order_quantity",
            "max_order_notional",
            "max_account_gross_notional",
            "max_leverage",
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), field=name))
        for name in (
            "max_order_worst_case_loss",
            "max_account_worst_case_loss",
            "max_slippage_bps",
            "max_fee_bps",
        ):
            object.__setattr__(
                self, name, _nonnegative(getattr(self, name), field=name)
            )
        for name in ("allowed_instruments", "allowed_actions", "allowed_order_types"):
            raw_values = getattr(self, name)
            if isinstance(raw_values, (str, bytes)):
                raise ValidationError(f"{name} must be an iterable of strings, not str")
            try:
                items = tuple(raw_values)
            except TypeError as exc:
                raise ValidationError(f"{name} must be an iterable of strings") from exc
            if any(
                not isinstance(item, str) or not item or item != item.strip()
                for item in items
            ):
                raise ValidationError(
                    f"{name} items must be non-empty, trimmed strings"
                )
            values = tuple(dict.fromkeys(items))
            if not values:
                raise ValidationError(f"{name} must not be empty")
            object.__setattr__(self, name, values)
        if self.max_order_notional > self.max_account_gross_notional:
            raise ValidationError("policy order notional exceeds its account limit")
        if self.max_order_worst_case_loss > self.max_account_worst_case_loss:
            raise ValidationError("policy order loss exceeds its account limit")

    def validate_ceiling(
        self, ceilings: PlatformCeilings = HARD_PLATFORM_CEILINGS
    ) -> None:
        pairs = (
            ("max_order_quantity", self.max_order_quantity, ceilings.max_order_quantity),
            ("max_order_notional", self.max_order_notional, ceilings.max_order_notional),
            (
                "max_order_worst_case_loss",
                self.max_order_worst_case_loss,
                ceilings.max_order_worst_case_loss,
            ),
            (
                "max_account_gross_notional",
                self.max_account_gross_notional,
                ceilings.max_account_gross_notional,
            ),
            (
                "max_account_worst_case_loss",
                self.max_account_worst_case_loss,
                ceilings.max_account_worst_case_loss,
            ),
            ("max_leverage", self.max_leverage, ceilings.max_leverage),
            ("max_slippage_bps", self.max_slippage_bps, ceilings.max_slippage_bps),
            ("max_fee_bps", self.max_fee_bps, ceilings.max_fee_bps),
        )
        for name, configured, configured_ceiling in pairs:
            # A caller may install a *stricter* deployment ceiling but cannot
            # inject a looser one to bypass the caps compiled into the module.
            ceiling = min(configured_ceiling, getattr(HARD_PLATFORM_CEILINGS, name))
            if configured > ceiling:
                raise PolicyViolation(
                    "PLATFORM_CEILING_EXCEEDED",
                    f"{name}={configured} exceeds hard ceiling {ceiling}",
                )

    def validate_order(
        self,
        *,
        instrument: str,
        action: str,
        order_type: str,
        leverage: Decimal,
        quote: ExposureQuote,
        current: AccountExposure,
        ceilings: PlatformCeilings = HARD_PLATFORM_CEILINGS,
    ) -> None:
        """Fail closed unless both hard and policy-specific limits pass."""

        self.validate_ceiling(ceilings)
        leverage = _positive(leverage, field="leverage")
        if action not in FOUNDATION_ALLOWED_ACTIONS:
            raise PolicyViolation(
                "PLATFORM_ACTION_NOT_ALLOWED",
                f"foundation release cannot perform action {action}",
            )
        if (
            self.allowed_instruments
            and "*" not in self.allowed_instruments
            and instrument not in self.allowed_instruments
        ):
            raise PolicyViolation("INSTRUMENT_NOT_ALLOWED", instrument)
        if "*" not in self.allowed_actions and action not in self.allowed_actions:
            raise PolicyViolation("ACTION_NOT_ALLOWED", action)
        if "*" not in self.allowed_order_types and order_type not in self.allowed_order_types:
            raise PolicyViolation("ORDER_TYPE_NOT_ALLOWED", order_type)

        checks = (
            ("ORDER_QUANTITY_LIMIT", quote.quantity, self.max_order_quantity),
            ("ORDER_NOTIONAL_LIMIT", quote.notional, self.max_order_notional),
            (
                "ORDER_LOSS_LIMIT",
                quote.worst_case_loss,
                self.max_order_worst_case_loss,
            ),
            ("LEVERAGE_LIMIT", leverage, self.max_leverage),
            ("SLIPPAGE_LIMIT", quote.slippage_bps, self.max_slippage_bps),
            ("FEE_LIMIT", quote.fee_bps, self.max_fee_bps),
            (
                "ACCOUNT_NOTIONAL_LIMIT",
                decimal_add(
                    current.gross_notional,
                    quote.notional,
                    field="post-admission account notional",
                ),
                self.max_account_gross_notional,
            ),
            (
                "ACCOUNT_LOSS_LIMIT",
                decimal_add(
                    current.worst_case_loss,
                    quote.worst_case_loss,
                    field="post-admission account loss",
                ),
                self.max_account_worst_case_loss,
            ),
        )
        for code, actual, limit in checks:
            if actual > limit:
                raise PolicyViolation(code, f"{actual} exceeds {limit}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "policy_id": self.policy_id,
            "version": self.version,
            "allowed_instruments": list(self.allowed_instruments),
            "allowed_actions": list(self.allowed_actions),
            "allowed_order_types": list(self.allowed_order_types),
        }
        for name in (
            "max_order_quantity",
            "max_order_notional",
            "max_order_worst_case_loss",
            "max_account_gross_notional",
            "max_account_worst_case_loss",
            "max_leverage",
            "max_slippage_bps",
            "max_fee_bps",
        ):
            result[name] = canonical_decimal(getattr(self, name))
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RiskPolicy":
        return cls(
            policy_id=str(value["policy_id"]),
            version=str(value["version"]),
            max_order_quantity=exact_decimal(
                value["max_order_quantity"], field="max_order_quantity"
            ),
            max_order_notional=exact_decimal(
                value["max_order_notional"], field="max_order_notional"
            ),
            max_order_worst_case_loss=exact_decimal(
                value["max_order_worst_case_loss"], field="max_order_worst_case_loss"
            ),
            max_account_gross_notional=exact_decimal(
                value["max_account_gross_notional"], field="max_account_gross_notional"
            ),
            max_account_worst_case_loss=exact_decimal(
                value["max_account_worst_case_loss"],
                field="max_account_worst_case_loss",
            ),
            max_leverage=exact_decimal(value["max_leverage"], field="max_leverage"),
            max_slippage_bps=exact_decimal(
                value["max_slippage_bps"], field="max_slippage_bps"
            ),
            max_fee_bps=exact_decimal(value["max_fee_bps"], field="max_fee_bps"),
            allowed_instruments=value.get("allowed_instruments", ()),  # type: ignore[arg-type]
            allowed_actions=value.get(  # type: ignore[arg-type]
                "allowed_actions", ("simulate_order",)
            ),
            allowed_order_types=value.get(  # type: ignore[arg-type]
                "allowed_order_types", ("market", "limit", "stop", "stop_limit")
            ),
        )
