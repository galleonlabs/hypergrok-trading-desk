from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from trading_harness.backtest import (
    BacktestRun,
    CostModel,
    ExitReason,
    FoldMetrics,
    PromotionStatus,
    TradeResult,
    assess_promotion,
    calculate_metrics,
    chronological_folds,
    deterministic_block_bootstrap_lower_bound,
    run_backtest,
    validate_profitability,
)
from trading_harness.strategy import Candle, SignalDirection


START = datetime(2024, 1, 1, tzinfo=timezone.utc)
ZERO_COSTS = CostModel(
    model_id="zero-cost-fixture",
    version="1",
    fee_bps_per_side=Decimal("0"),
    slippage_bps_per_side=Decimal("0"),
)


def bar(
    index: int,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    *,
    received_delay: timedelta = timedelta(0),
) -> Candle:
    opened = START + timedelta(hours=4 * index)
    return Candle(
        instrument="ETH",
        interval="4h",
        open_time=opened,
        close_time=opened + timedelta(hours=4),
        received_at=opened + timedelta(hours=4) + received_delay,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=Decimal("1000"),
    )


def breakout_prefix(*, late_signal: bool = False) -> list[Candle]:
    values: list[Candle] = []
    for index in range(1000):
        close = Decimal("100") + Decimal(index) / Decimal("100")
        values.append(
            bar(index, close, close + Decimal("0.5"), close - Decimal("0.5"), close)
        )
    delay = timedelta(minutes=16) if late_signal else timedelta(0)
    values.append(
        bar(
            1000,
            Decimal("112"),
            Decimal("112.5"),
            Decimal("111.5"),
            Decimal("112"),
            received_delay=delay,
        )
    )
    return values


def fake_trade(index: int, net_r: Decimal) -> TradeResult:
    signal_time = START + timedelta(hours=4 * index)
    return TradeResult(
        signal_hash=f"{index:064x}",
        direction=SignalDirection.BUY,
        signal_index=index,
        entry_index=index + 1,
        exit_index=index + 1,
        signal_time=signal_time,
        entry_time=signal_time + timedelta(hours=4),
        exit_time=signal_time + timedelta(hours=8),
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        target_price=Decimal("102"),
        exit_price=Decimal("100") + net_r,
        bars_held=1,
        exit_reason=ExitReason.TIME,
        gross_pnl_per_unit=net_r,
        total_cost_per_unit=Decimal("0"),
        net_pnl_per_unit=net_r,
        net_r=net_r,
    )


def run_fixture(
    trades: tuple[TradeResult, ...],
    *,
    unresolved: int = 0,
    cost_hash: str = "cost",
) -> BacktestRun:
    return BacktestRun(
        strategy_hash="strategy",
        data_hash="data",
        cost_model_hash=cost_hash,
        trades=trades,
        metrics=calculate_metrics(trades),
        ignored_while_open=0,
        expired_signals=0,
        unresolved_positions=unresolved,
    )


def fold_fixture(trades: tuple[TradeResult, ...]) -> tuple[FoldMetrics, ...]:
    chunks = tuple(trades[index * 25 : (index + 1) * 25] for index in range(4))
    return tuple(
        FoldMetrics(
            fold_index=index,
            signal_start_index=1000 + index * 250,
            signal_end_index=1000 + (index + 1) * 250,
            metrics=calculate_metrics(chunk, bootstrap_samples=0),
        )
        for index, chunk in enumerate(chunks)
    )


class BacktestExecutionTests(unittest.TestCase):
    def test_executes_at_next_bar_not_signal_bar(self) -> None:
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("120"),
                Decimal("124"),
                Decimal("119"),
                Decimal("121"),
            )
        )
        run = run_backtest(values, ZERO_COSTS)
        self.assertEqual(len(run.trades), 1)
        trade = run.trades[0]
        self.assertEqual(trade.signal_index, 1000)
        self.assertEqual(trade.entry_index, 1001)
        self.assertEqual(trade.entry_price, Decimal("120"))
        self.assertEqual(trade.entry_time, values[1001].open_time)

    def test_stop_wins_when_stop_and_target_touch_same_bar(self) -> None:
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("200"),
                Decimal("1"),
                Decimal("112"),
            )
        )
        trade = run_backtest(values, ZERO_COSTS).trades[0]
        self.assertIs(trade.exit_reason, ExitReason.STOP_FIRST_AMBIGUOUS_BAR)
        self.assertEqual(trade.exit_price, trade.stop_price)
        self.assertEqual(
            trade.net_r.quantize(Decimal("0.000000000001")), Decimal("-1.000000000000")
        )

    def test_target_is_two_r_before_costs(self) -> None:
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("200"),
                Decimal("111"),
                Decimal("112"),
            )
        )
        trade = run_backtest(values, ZERO_COSTS).trades[0]
        self.assertIs(trade.exit_reason, ExitReason.TARGET)
        self.assertEqual(
            trade.net_r.quantize(Decimal("0.000000000001")), Decimal("2.000000000000")
        )

    def test_time_exit_occurs_on_twelfth_holding_bar(self) -> None:
        values = breakout_prefix()
        for index in range(1001, 1013):
            values.append(
                bar(
                    index,
                    Decimal("112"),
                    Decimal("112.2"),
                    Decimal("111.8"),
                    Decimal("112"),
                )
            )
        trade = run_backtest(values, ZERO_COSTS).trades[0]
        self.assertIs(trade.exit_reason, ExitReason.TIME)
        self.assertEqual(trade.bars_held, 12)
        self.assertEqual(trade.exit_index, 1012)
        self.assertEqual(trade.net_r, Decimal("0"))

    def test_expired_signal_is_not_traded(self) -> None:
        values = breakout_prefix(late_signal=True)
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("200"),
                Decimal("111"),
                Decimal("112"),
            )
        )
        run = run_backtest(values, ZERO_COSTS)
        self.assertEqual(run.trades, ())
        self.assertEqual(run.expired_signals, 1)

    def test_registered_costs_reduce_net_r_and_stress_doubles_them(self) -> None:
        costs = CostModel(
            model_id="costs",
            version="1",
            fee_bps_per_side=Decimal("5"),
            slippage_bps_per_side=Decimal("10"),
            holding_cost_bps_per_bar=Decimal("1"),
        )
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("200"),
                Decimal("111"),
                Decimal("112"),
            )
        )
        base = run_backtest(values, costs).trades[0]
        stressed = run_backtest(values, costs.stressed()).trades[0]
        self.assertLess(base.net_r, Decimal("2"))
        self.assertLess(stressed.net_r, base.net_r)
        self.assertEqual(costs.stressed().fee_bps_per_side, Decimal("10"))

    def test_does_not_force_close_an_unresolved_final_position(self) -> None:
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("112.2"),
                Decimal("111.8"),
                Decimal("112"),
            )
        )
        run = run_backtest(values, ZERO_COSTS)
        self.assertEqual(run.trades, ())
        self.assertEqual(run.unresolved_positions, 1)

    def test_cost_model_rejects_floats(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be Decimal"):
            CostModel(
                model_id="bad",
                version="1",
                fee_bps_per_side=0.5,  # type: ignore[arg-type]
                slippage_bps_per_side=Decimal("1"),
            )


class ProfitabilityGateTests(unittest.TestCase):
    def test_deterministic_block_bootstrap_is_reproducible(self) -> None:
        returns = tuple(Decimal("0.1") for _ in range(100))
        first = deterministic_block_bootstrap_lower_bound(returns)
        second = deterministic_block_bootstrap_lower_bound(returns)
        self.assertEqual(first, second)
        self.assertEqual(first, (Decimal("0.1"), 10))

    def test_pass_requires_every_registered_gate(self) -> None:
        trades = tuple(fake_trade(1000 + index * 10, Decimal("0.1")) for index in range(100))
        base = run_fixture(trades)
        stress = run_fixture(trades, cost_hash="stress")
        decision = assess_promotion(base, stress, fold_fixture(trades))
        self.assertIs(decision.status, PromotionStatus.PASS)
        self.assertTrue(all(check.passed for check in decision.checks))

    def test_sparse_evidence_is_inconclusive_not_rejected_or_passed(self) -> None:
        trades = tuple(fake_trade(1000 + index * 10, Decimal("0.1")) for index in range(99))
        base = run_fixture(trades)
        decision = assess_promotion(base, base, fold_fixture(trades))
        self.assertIs(decision.status, PromotionStatus.INCONCLUSIVE)
        self.assertIn("fewer_than_100_oos_trades", decision.reasons)

    def test_concentrated_profit_is_rejected(self) -> None:
        returns = (Decimal("30"),) + tuple(Decimal("0.1") for _ in range(99))
        trades = tuple(
            fake_trade(1000 + index * 10, value)
            for index, value in enumerate(returns)
        )
        base = run_fixture(trades)
        decision = assess_promotion(base, base, fold_fixture(trades))
        self.assertIs(decision.status, PromotionStatus.REJECTED)
        self.assertIn("best_trade_contribution", decision.reasons)

    def test_unresolved_position_forces_inconclusive(self) -> None:
        trades = tuple(fake_trade(1000 + index * 10, Decimal("0.1")) for index in range(100))
        base = run_fixture(trades, unresolved=1)
        decision = assess_promotion(base, base, fold_fixture(trades))
        self.assertIs(decision.status, PromotionStatus.INCONCLUSIVE)
        self.assertIn("dataset_ends_with_unresolved_position", decision.reasons)

    def test_chronological_fold_boundaries_are_contiguous(self) -> None:
        trades = tuple(fake_trade(1000 + index * 5, Decimal("0.1")) for index in range(100))
        run = run_fixture(trades)
        folds = chronological_folds(run, total_bars=2001)
        self.assertEqual(len(folds), 4)
        self.assertEqual(folds[0].signal_start_index, 1000)
        self.assertEqual(folds[-1].signal_end_index, 2000)
        for left, right in zip(folds, folds[1:]):
            self.assertEqual(left.signal_end_index, right.signal_start_index)

    def test_validation_artifact_is_canonical_and_reproducible(self) -> None:
        values = breakout_prefix()
        values.append(
            bar(
                1001,
                Decimal("112"),
                Decimal("200"),
                Decimal("111"),
                Decimal("112"),
            )
        )
        first = validate_profitability(values, ZERO_COSTS)
        second = validate_profitability(values, ZERO_COSTS)
        self.assertEqual(first.artifact_hash, second.artifact_hash)
        self.assertIs(first.promotion.status, PromotionStatus.INCONCLUSIVE)
        encoded = json.dumps(first.to_dict(), allow_nan=False, sort_keys=True)
        self.assertIn(first.artifact_hash, encoded)


if __name__ == "__main__":
    unittest.main()
