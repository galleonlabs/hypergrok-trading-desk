"""Honest, deterministic evaluation for the registered candidate-v0 strategy.

The runner consumes the completed four-hour candles defined in
``strategy.py``.  A signal produced at candle ``t`` can first execute at the
next candle's open; the signal candle's own prices are never used as a fill.
Only one position may be open.  Exit rules are frozen at 1.5 ATR stop, 3 ATR
target, or the close of the twelfth holding bar.  If stop and target are both
touched in one bar, the stop wins.

The profitability gate is intentionally demanding and has only three
outcomes: ``PASS``, ``REJECTED``, or ``INCONCLUSIVE``.  Sparse evidence is
inconclusive rather than profitable by assertion.

Bootstrap method
================

The one-sided 95% lower bound is a deterministic circular moving-block
bootstrap over ordered trade returns.  Block length is ``floor(sqrt(n))``;
1,024 resamples are constructed from SHA-256-derived block starts under a
fixed protocol domain.  There is no process-global PRNG or runtime seed, so
identical ordered trades produce identical bounds on every run.  This is a
dependence-aware uncertainty diagnostic, not proof that future returns will
match the sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
from math import isqrt
from typing import Iterable, Sequence

from .canonical import canonical_data, domain_hash, validate_decimal_bounds
from .strategy import (
    CANDIDATE_V0,
    Candle,
    RegisteredStrategy,
    SignalDirection,
    StrategySignal,
    scan_signals,
    validate_candle_series,
)


BACKTEST_DATA_HASH_DOMAIN = "trading-harness/backtest-data/v1"
BACKTEST_RUN_HASH_DOMAIN = "trading-harness/backtest-run/v1"
VALIDATION_ARTIFACT_HASH_DOMAIN = "trading-harness/validation-artifact/v1"
BOOTSTRAP_DOMAIN = b"trading-harness/moving-block-bootstrap/v1"
BOOTSTRAP_SAMPLES = 1024
BASIS_POINTS = Decimal("10000")

_CALCULATION_CONTEXT = Context(
    prec=64,
    rounding=ROUND_HALF_EVEN,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)


class PromotionStatus(str, Enum):
    PASS = "PASS"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ExitReason(str, Enum):
    STOP = "stop"
    GAP_STOP = "gap_stop"
    STOP_FIRST_AMBIGUOUS_BAR = "stop_first_ambiguous_bar"
    TARGET = "target"
    TIME = "time"


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal, int, or exact string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        validate_decimal_bounds(result, field=field)
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a bounded finite decimal") from error
    if nonnegative and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _sum(values: Iterable[Decimal]) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        total = Decimal("0")
        for value in values:
            total = context.add(total, value)
        return total


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.multiply(left, right)


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise ZeroDivisionError("deterministic decimal denominator is zero")
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.divide(numerator, denominator)


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.add(left, right)


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.subtract(left, right)


def _bps_amount(price: Decimal, bps: Decimal) -> Decimal:
    return _divide(_multiply(price, bps), BASIS_POINTS)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Exact, preregistered per-unit execution costs.

    Fees apply independently to entry and exit notional.  Slippage is applied
    adversely to both fills.  ``holding_cost_bps_per_bar`` is always charged;
    this conservative field can represent a registered funding/borrow stress
    when exact point-in-time funding is not supplied by this minimal runner.
    """

    model_id: str
    version: str
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    holding_cost_bps_per_bar: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for field in ("model_id", "version"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be a non-empty, trimmed string")
        for field in (
            "fee_bps_per_side",
            "slippage_bps_per_side",
            "holding_cost_bps_per_bar",
        ):
            parsed = _decimal(getattr(self, field), field, nonnegative=True)
            if parsed > Decimal("1000"):
                raise ValueError(f"{field} exceeds the research runner bound")
            object.__setattr__(self, field, parsed)

    @property
    def model_hash(self) -> str:
        return domain_hash("trading-harness/cost-model/v1", self)

    def stressed(self) -> "CostModel":
        """Return the registered 2x adverse-cost scenario."""

        return CostModel(
            model_id=f"{self.model_id}-stress-2x",
            version=self.version,
            fee_bps_per_side=_multiply(self.fee_bps_per_side, Decimal("2")),
            slippage_bps_per_side=_multiply(
                self.slippage_bps_per_side, Decimal("2")
            ),
            holding_cost_bps_per_bar=_multiply(
                self.holding_cost_bps_per_bar, Decimal("2")
            ),
        )


@dataclass(frozen=True, slots=True)
class TradeResult:
    signal_hash: str
    direction: SignalDirection
    signal_index: int
    entry_index: int
    exit_index: int
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    exit_price: Decimal
    bars_held: int
    exit_reason: ExitReason
    gross_pnl_per_unit: Decimal
    total_cost_per_unit: Decimal
    net_pnl_per_unit: Decimal
    net_r: Decimal

    def __post_init__(self) -> None:
        if self.direction not in {SignalDirection.BUY, SignalDirection.SELL}:
            raise ValueError("a trade requires a buy or sell signal")
        if not (0 <= self.signal_index < self.entry_index <= self.exit_index):
            raise ValueError("trade bar indices are inconsistent")
        if self.bars_held != self.exit_index - self.entry_index + 1:
            raise ValueError("bars_held does not match trade indices")


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    trade_count: int
    total_net_r: Decimal
    expectancy_r: Decimal
    gross_profit_r: Decimal
    gross_loss_r: Decimal
    profit_factor: Decimal | None
    max_drawdown_r: Decimal
    best_trade_contribution: Decimal | None
    bootstrap_lower_95_r: Decimal | None
    bootstrap_block_length: int
    bootstrap_samples: int


@dataclass(frozen=True, slots=True)
class BacktestRun:
    strategy_hash: str
    data_hash: str
    cost_model_hash: str
    trades: tuple[TradeResult, ...]
    metrics: PerformanceMetrics
    ignored_while_open: int
    expired_signals: int
    unresolved_positions: int

    @property
    def run_hash(self) -> str:
        return domain_hash(BACKTEST_RUN_HASH_DOMAIN, self)


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    fold_index: int
    signal_start_index: int
    signal_end_index: int
    metrics: PerformanceMetrics


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    actual: Decimal | int | str | None
    requirement: str


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    status: PromotionStatus
    checks: tuple[GateCheck, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidationArtifact:
    schema_version: int
    strategy: RegisteredStrategy
    data_hash: str
    base_cost_model: CostModel
    stress_cost_model: CostModel
    base_run: BacktestRun
    stress_run: BacktestRun
    folds: tuple[FoldMetrics, ...]
    promotion: PromotionDecision
    bootstrap_method: str

    @property
    def artifact_hash(self) -> str:
        return domain_hash(VALIDATION_ARTIFACT_HASH_DOMAIN, self)

    def to_dict(self) -> dict[str, object]:
        value = canonical_data(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("canonical validation artifact must be an object")
        return {**value, "artifact_hash": self.artifact_hash}


def _adverse_fill(
    reference_price: Decimal,
    *,
    direction: SignalDirection,
    entering: bool,
    slippage_bps: Decimal,
) -> Decimal:
    slip = _bps_amount(reference_price, slippage_bps)
    # Buy fills are worse above reference; sell fills are worse below it.
    is_buy = (direction is SignalDirection.BUY) == entering
    return _add(reference_price, slip) if is_buy else _subtract(reference_price, slip)


def _exit_reference(
    candle: Candle,
    *,
    direction: SignalDirection,
    stop_price: Decimal,
    target_price: Decimal,
    time_exit: bool,
) -> tuple[Decimal, ExitReason] | None:
    if direction is SignalDirection.BUY:
        if candle.open <= stop_price:
            return candle.open, ExitReason.GAP_STOP
        if candle.open >= target_price:
            return target_price, ExitReason.TARGET
        stop_touched = candle.low <= stop_price
        target_touched = candle.high >= target_price
    else:
        if candle.open >= stop_price:
            return candle.open, ExitReason.GAP_STOP
        if candle.open <= target_price:
            return target_price, ExitReason.TARGET
        stop_touched = candle.high >= stop_price
        target_touched = candle.low <= target_price

    if stop_touched and target_touched:
        return stop_price, ExitReason.STOP_FIRST_AMBIGUOUS_BAR
    if stop_touched:
        return stop_price, ExitReason.STOP
    if target_touched:
        return target_price, ExitReason.TARGET
    if time_exit:
        return candle.close, ExitReason.TIME
    return None


def _simulate_trade(
    candles: Sequence[Candle],
    signal: StrategySignal,
    strategy: RegisteredStrategy,
    costs: CostModel,
) -> TradeResult | None:
    entry_index = signal.bar_index + 1
    if entry_index >= len(candles):
        return None
    if signal.observed_at > signal.expires_at:
        return None
    entry_candle = candles[entry_index]
    if entry_candle.open_time > signal.expires_at:
        return None

    entry_price = _adverse_fill(
        entry_candle.open,
        direction=signal.direction,
        entering=True,
        slippage_bps=costs.slippage_bps_per_side,
    )
    stop_distance = _multiply(
        signal.features.atr, strategy.stop_atr_multiple
    )
    target_distance = _multiply(
        signal.features.atr, strategy.target_atr_multiple
    )
    if signal.direction is SignalDirection.BUY:
        stop_price = _subtract(entry_price, stop_distance)
        target_price = _add(entry_price, target_distance)
        if stop_price <= 0:
            return None
    else:
        stop_price = _add(entry_price, stop_distance)
        target_price = _subtract(entry_price, target_distance)
        if target_price <= 0:
            return None

    final_index = min(
        len(candles) - 1,
        entry_index + strategy.max_holding_bars - 1,
    )
    for exit_index in range(entry_index, final_index + 1):
        holding_complete = exit_index == entry_index + strategy.max_holding_bars - 1
        exit_reference = _exit_reference(
            candles[exit_index],
            direction=signal.direction,
            stop_price=stop_price,
            target_price=target_price,
            time_exit=holding_complete,
        )
        if exit_reference is None:
            continue
        reference_price, exit_reason = exit_reference
        exit_price = _adverse_fill(
            reference_price,
            direction=signal.direction,
            entering=False,
            slippage_bps=costs.slippage_bps_per_side,
        )
        if exit_price <= 0:
            return None
        bars_held = exit_index - entry_index + 1
        if signal.direction is SignalDirection.BUY:
            gross_pnl = _subtract(exit_price, entry_price)
        else:
            gross_pnl = _subtract(entry_price, exit_price)
        entry_fee = _bps_amount(entry_price, costs.fee_bps_per_side)
        exit_fee = _bps_amount(exit_price, costs.fee_bps_per_side)
        holding_cost = _multiply(
            _bps_amount(entry_price, costs.holding_cost_bps_per_bar),
            Decimal(bars_held),
        )
        total_cost = _sum((entry_fee, exit_fee, holding_cost))
        net_pnl = _subtract(gross_pnl, total_cost)
        net_r = _divide(net_pnl, stop_distance)
        return TradeResult(
            signal_hash=signal.signal_hash,
            direction=signal.direction,
            signal_index=signal.bar_index,
            entry_index=entry_index,
            exit_index=exit_index,
            signal_time=signal.signal_time,
            entry_time=entry_candle.open_time,
            exit_time=candles[exit_index].close_time,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            exit_price=exit_price,
            bars_held=bars_held,
            exit_reason=exit_reason,
            gross_pnl_per_unit=gross_pnl,
            total_cost_per_unit=total_cost,
            net_pnl_per_unit=net_pnl,
            net_r=net_r,
        )
    # The dataset ended before the frozen time exit and neither bracket leg
    # resolved.  Censor the position instead of manufacturing a final close.
    return None


def deterministic_block_bootstrap_lower_bound(
    returns: Sequence[Decimal],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
) -> tuple[Decimal | None, int]:
    """Return deterministic one-sided fifth-percentile mean and block length."""

    if not returns:
        return None, 0
    if type(samples) is not int or samples < 100:
        raise ValueError("bootstrap samples must be an integer of at least 100")
    count = len(returns)
    block_length = max(1, isqrt(count))
    block_count = (count + block_length - 1) // block_length
    means: list[Decimal] = []
    for replicate in range(samples):
        resample: list[Decimal] = []
        for block in range(block_count):
            material = (
                BOOTSTRAP_DOMAIN
                + b"\x00"
                + count.to_bytes(8, "big")
                + block_length.to_bytes(8, "big")
                + replicate.to_bytes(8, "big")
                + block.to_bytes(8, "big")
            )
            start = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % count
            for offset in range(block_length):
                resample.append(returns[(start + offset) % count])
                if len(resample) == count:
                    break
            if len(resample) == count:
                break
        means.append(_divide(_sum(resample), Decimal(count)))
    means.sort()
    percentile_index = ((samples - 1) * 5) // 100
    return means[percentile_index], block_length


def calculate_metrics(
    trades: Iterable[TradeResult],
    *,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> PerformanceMetrics:
    values = tuple(trades)
    returns = tuple(trade.net_r for trade in values)
    trade_count = len(values)
    total = _sum(returns)
    expectancy = (
        _divide(total, Decimal(trade_count)) if trade_count else Decimal("0")
    )
    gross_profit = _sum(value for value in returns if value > 0)
    gross_loss = abs(_sum(value for value in returns if value < 0))
    profit_factor = (
        _divide(gross_profit, gross_loss) if gross_loss > 0 else None
    )

    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for value in returns:
        equity = _add(equity, value)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, _subtract(peak, equity))

    best_contribution = None
    if total > 0 and returns:
        best_contribution = _divide(max(returns), total)
    if bootstrap_samples:
        lower, block_length = deterministic_block_bootstrap_lower_bound(
            returns, samples=bootstrap_samples
        )
    else:
        lower, block_length = None, max(1, isqrt(trade_count)) if trade_count else 0
    return PerformanceMetrics(
        trade_count=trade_count,
        total_net_r=total,
        expectancy_r=expectancy,
        gross_profit_r=gross_profit,
        gross_loss_r=gross_loss,
        profit_factor=profit_factor,
        max_drawdown_r=max_drawdown,
        best_trade_contribution=best_contribution,
        bootstrap_lower_95_r=lower,
        bootstrap_block_length=block_length,
        bootstrap_samples=bootstrap_samples if trade_count else 0,
    )


def run_backtest(
    candles: Iterable[Candle],
    costs: CostModel,
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> BacktestRun:
    """Run the frozen rule with next-bar fills and a single open position."""

    if not isinstance(costs, CostModel):
        raise TypeError("costs must be CostModel")
    if not isinstance(strategy, RegisteredStrategy):
        raise TypeError("strategy must be RegisteredStrategy")
    values = validate_candle_series(candles)
    signals = scan_signals(values, strategy)
    by_index = {signal.bar_index: signal for signal in signals}
    directional_indices = {
        signal.bar_index
        for signal in signals
        if signal.direction is not SignalDirection.NOTHING
    }
    trades: list[TradeResult] = []
    ignored_while_open = 0
    expired_signals = 0
    unresolved_positions = 0
    index = strategy.warmup_bars
    while index < len(values) - 1:
        signal = by_index.get(index)
        if signal is None or signal.direction is SignalDirection.NOTHING:
            index += 1
            continue
        if signal.observed_at > signal.expires_at or values[index + 1].open_time > signal.expires_at:
            expired_signals += 1
            index += 1
            continue
        trade = _simulate_trade(values, signal, strategy, costs)
        if trade is None:
            # No terminal outcome before data ends, or invalid bracket geometry.
            unresolved_positions += 1
            break
        trades.append(trade)
        ignored_while_open += sum(
            1
            for skipped in range(index + 1, trade.exit_index)
            if skipped in directional_indices
        )
        # A signal at the exit candle close may execute on the following bar.
        index = trade.exit_index

    data_hash = domain_hash(BACKTEST_DATA_HASH_DOMAIN, values)
    return BacktestRun(
        strategy_hash=strategy.registration_hash,
        data_hash=data_hash,
        cost_model_hash=costs.model_hash,
        trades=tuple(trades),
        metrics=calculate_metrics(trades),
        ignored_while_open=ignored_while_open,
        expired_signals=expired_signals,
        unresolved_positions=unresolved_positions,
    )


def chronological_folds(
    run: BacktestRun,
    *,
    total_bars: int,
    warmup_bars: int = 1000,
    fold_count: int = 4,
) -> tuple[FoldMetrics, ...]:
    """Partition the immutable OOS signal interval into contiguous time folds."""

    if type(total_bars) is not int or total_bars <= warmup_bars + 1:
        raise ValueError("not enough bars for chronological OOS folds")
    if type(fold_count) is not int or fold_count != 4:
        raise ValueError("candidate-v0 profitability gate requires exactly four folds")
    start = warmup_bars
    stop = total_bars - 1  # final bar cannot originate a next-bar entry
    eligible = stop - start
    if eligible < fold_count:
        raise ValueError("not enough eligible signal bars for four folds")
    base_width, remainder = divmod(eligible, fold_count)
    folds: list[FoldMetrics] = []
    cursor = start
    for fold_index in range(fold_count):
        width = base_width + (1 if fold_index < remainder else 0)
        end = cursor + width
        fold_trades = tuple(
            trade for trade in run.trades if cursor <= trade.signal_index < end
        )
        folds.append(
            FoldMetrics(
                fold_index=fold_index,
                signal_start_index=cursor,
                signal_end_index=end,
                metrics=calculate_metrics(fold_trades, bootstrap_samples=0),
            )
        )
        cursor = end
    return tuple(folds)


def assess_promotion(
    base_run: BacktestRun,
    stress_run: BacktestRun,
    folds: Sequence[FoldMetrics],
) -> PromotionDecision:
    """Apply the frozen profitability gate without discretionary overrides."""

    positive_folds = sum(
        1
        for fold in folds
        if fold.metrics.trade_count > 0 and fold.metrics.expectancy_r > 0
    )
    metrics = base_run.metrics
    if metrics.gross_loss_r == 0:
        profit_factor_passed = metrics.gross_profit_r > 0
        profit_factor_actual: Decimal | str | None = (
            "no_losses" if profit_factor_passed else None
        )
    else:
        profit_factor_passed = (
            metrics.profit_factor is not None
            and metrics.profit_factor >= Decimal("1.2")
        )
        profit_factor_actual = metrics.profit_factor

    checks = (
        GateCheck(
            "minimum_oos_trades",
            metrics.trade_count >= 100,
            metrics.trade_count,
            ">=100",
        ),
        GateCheck(
            "positive_chronological_folds",
            len(folds) == 4 and positive_folds >= 3,
            positive_folds,
            ">=3 of exactly 4",
        ),
        GateCheck(
            "profit_factor",
            profit_factor_passed,
            profit_factor_actual,
            ">=1.2 (or positive returns with no losses)",
        ),
        GateCheck(
            "max_drawdown_r",
            metrics.max_drawdown_r <= Decimal("10"),
            metrics.max_drawdown_r,
            "<=10R",
        ),
        GateCheck(
            "cost_stress_expectancy",
            stress_run.metrics.expectancy_r > 0,
            stress_run.metrics.expectancy_r,
            ">0R at 2x registered costs",
        ),
        GateCheck(
            "best_trade_contribution",
            metrics.best_trade_contribution is not None
            and metrics.best_trade_contribution <= Decimal("0.2"),
            metrics.best_trade_contribution,
            "<=20% of total net R",
        ),
        GateCheck(
            "block_bootstrap_lower_bound",
            metrics.bootstrap_lower_95_r is not None
            and metrics.bootstrap_lower_95_r > 0,
            metrics.bootstrap_lower_95_r,
            ">0R one-sided 95% lower bound",
        ),
    )
    inconclusive_reasons: list[str] = []
    if metrics.trade_count < 100:
        inconclusive_reasons.append("fewer_than_100_oos_trades")
    if len(folds) != 4:
        inconclusive_reasons.append("exactly_four_chronological_folds_required")
    if base_run.unresolved_positions or stress_run.unresolved_positions:
        inconclusive_reasons.append("dataset_ends_with_unresolved_position")
    if inconclusive_reasons:
        return PromotionDecision(
            PromotionStatus.INCONCLUSIVE,
            checks,
            tuple(inconclusive_reasons),
        )
    failed = tuple(check.name for check in checks if not check.passed)
    if failed:
        return PromotionDecision(PromotionStatus.REJECTED, checks, failed)
    return PromotionDecision(PromotionStatus.PASS, checks, ())


def validate_profitability(
    candles: Iterable[Candle],
    costs: CostModel,
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> ValidationArtifact:
    """Run base/stress OOS evaluation and return a hashable evidence artifact."""

    values = validate_candle_series(candles)
    base_run = run_backtest(values, costs, strategy)
    stress_costs = costs.stressed()
    stress_run = run_backtest(values, stress_costs, strategy)
    eligible_signal_bars = len(values) - 1 - strategy.warmup_bars
    folds = (
        chronological_folds(
            base_run,
            total_bars=len(values),
            warmup_bars=strategy.warmup_bars,
        )
        if eligible_signal_bars >= 4
        else ()
    )
    promotion = assess_promotion(base_run, stress_run, folds)
    return ValidationArtifact(
        schema_version=1,
        strategy=strategy,
        data_hash=base_run.data_hash,
        base_cost_model=costs,
        stress_cost_model=stress_costs,
        base_run=base_run,
        stress_run=stress_run,
        folds=folds,
        promotion=promotion,
        bootstrap_method=(
            "deterministic circular moving-block bootstrap; block_length="
            "floor(sqrt(n)); 1024 SHA-256-derived resamples; fifth percentile "
            "of ordered-trade mean net R"
        ),
    )


__all__ = (
    "BOOTSTRAP_SAMPLES",
    "BacktestRun",
    "CostModel",
    "ExitReason",
    "FoldMetrics",
    "GateCheck",
    "PerformanceMetrics",
    "PromotionDecision",
    "PromotionStatus",
    "TradeResult",
    "ValidationArtifact",
    "assess_promotion",
    "calculate_metrics",
    "chronological_folds",
    "deterministic_block_bootstrap_lower_bound",
    "run_backtest",
    "validate_profitability",
)
