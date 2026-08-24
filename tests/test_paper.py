from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import unittest

from trading_harness.canonical import canonical_data, canonical_json
from trading_harness.domain import Environment, OrderType, SemanticIntent, Side
from trading_harness.paper import (
    EntryFillStatus,
    LegStatus,
    PaperBookObservation,
    PaperCandleObservation,
    PaperCostModel,
    PaperEventType,
    PaperOMS,
    PaperProtectionObservation,
    PaperState,
    PaperStateError,
)
from trading_harness.planning import GroupingPolicy, ProtectedTradePlan


NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def protected_plan(side: Side = Side.BUY, *, quantity: Decimal = Decimal("1")) -> ProtectedTradePlan:
    buying = side is Side.BUY
    entry_bound = Decimal("101" if buying else "99")
    stop_trigger = Decimal("95" if buying else "105")
    stop_bound = Decimal("94" if buying else "106")
    target_trigger = Decimal("110" if buying else "90")
    target_bound = Decimal("109" if buying else "91")
    exit_side = Side.SELL if buying else Side.BUY
    common = {
        "thesis_id": "candidate-v0",
        "thesis_version": "1",
        "strategy_version": "1",
        "code_hash": digest("paper-code"),
        "venue": "hyperliquid",
        "account_id": "paper-account",
        "environment": Environment.TESTNET,
        "instrument": "ETH-PERP",
        "quantity": quantity,
        "expires_at": NOW + timedelta(hours=1),
        "leverage": Decimal("2"),
        "fee_bps": Decimal("10"),
    }
    entry = SemanticIntent(
        intent_id="paper-entry",
        action="place_order",
        side=side,
        order_type=OrderType.MARKET,
        client_order_id="0x" + "a" * 32,
        price_bound=entry_bound,
        stop_price=stop_trigger,
        protection_limit_price=stop_bound,
        max_slippage_bps=Decimal("100"),
        time_in_force="Ioc",
        **common,
    )
    stop = SemanticIntent(
        intent_id="paper-stop",
        action="place_stop",
        side=exit_side,
        order_type=OrderType.STOP,
        client_order_id="0x" + "b" * 32,
        price_bound=stop_bound,
        stop_price=stop_trigger,
        protection_limit_price=stop_bound,
        reduce_only=True,
        max_slippage_bps=Decimal("200"),
        **common,
    )
    target = SemanticIntent(
        intent_id="paper-target",
        action="place_take_profit",
        side=exit_side,
        order_type=OrderType.STOP,
        client_order_id="0x" + "c" * 32,
        price_bound=target_bound,
        stop_price=target_trigger,
        reduce_only=True,
        max_slippage_bps=Decimal("100"),
        **common,
    )
    assessment_hash = digest("assessment")
    payload = {
        "domain": "protected-trade-plan-v1",
        "assessment_hash": assessment_hash,
        "grouping": GroupingPolicy.NORMAL_TPSL.value,
        "legs": [canonical_data(entry), canonical_data(stop), canonical_data(target)],
    }
    plan_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return ProtectedTradePlan(
        assessment_hash=assessment_hash,
        entry=entry,
        protective_stop=stop,
        take_profit=target,
        grouping=GroupingPolicy.NORMAL_TPSL,
        plan_hash=plan_hash,
    )


def costs(**changes: object) -> PaperCostModel:
    values: dict[str, object] = {
        "model_id": "paper-costs-v1",
        "version": "1",
        "fee_bps_per_fill": Decimal("5"),
        "entry_slippage_bps": Decimal("10"),
        "exit_slippage_bps": Decimal("20"),
        "emergency_slippage_bps": Decimal("50"),
        "protection_timeout_seconds": 5,
    }
    values.update(changes)
    return PaperCostModel(**values)  # type: ignore[arg-type]


def fresh_oms(side: Side = Side.BUY) -> PaperOMS:
    return PaperOMS.create(
        account_id="paper-account",
        instrument="ETH-PERP",
        cost_model=costs(),
    ).submit_plan(protected_plan(side), at=NOW, event_id="plan")


def book(
    suffix: str,
    at: datetime,
    *,
    bid: Decimal = Decimal("99.9"),
    ask: Decimal = Decimal("100"),
    bid_size: Decimal = Decimal("10"),
    ask_size: Decimal = Decimal("10"),
) -> PaperBookObservation:
    return PaperBookObservation(
        observation_id=f"book-{suffix}",
        observed_at=at,
        bid_price=bid,
        ask_price=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        source_hash=digest(f"book-{suffix}"),
    )


def protection(
    suffix: str,
    at: datetime,
    *,
    stop_status: LegStatus = LegStatus.ACCEPTED,
    target_status: LegStatus = LegStatus.ACCEPTED,
    stop_quantity: Decimal = Decimal("1"),
    target_quantity: Decimal = Decimal("1"),
) -> PaperProtectionObservation:
    return PaperProtectionObservation(
        observation_id=f"protection-{suffix}",
        observed_at=at,
        stop_status=stop_status,
        take_profit_status=target_status,
        stop_quantity=stop_quantity,
        take_profit_quantity=target_quantity,
        source_hash=digest(f"protection-{suffix}"),
    )


def paper_candle(
    suffix: str,
    opened: datetime,
    *,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
) -> PaperCandleObservation:
    closed = opened + timedelta(hours=4)
    return PaperCandleObservation(
        observation_id=f"candle-{suffix}",
        open_time=opened,
        close_time=closed,
        observed_at=closed,
        open=open_price,
        high=high,
        low=low,
        close=close,
        source_hash=digest(f"candle-{suffix}"),
    )


def protected_oms(side: Side = Side.BUY) -> PaperOMS:
    entered = fresh_oms(side).observe_entry(
        book("entry", NOW + timedelta(seconds=1)), event_id="entry"
    )
    return entered.reconcile_protection(
        protection("active", NOW + timedelta(seconds=2)), event_id="protection"
    )


class EntryAndProtectionTests(unittest.TestCase):
    def test_bounded_ioc_full_fill_waits_for_normal_tpsl_children(self) -> None:
        pending = fresh_oms()
        entered = pending.observe_entry(
            book("full", NOW + timedelta(seconds=1)), event_id="entry-full"
        )
        self.assertIs(entered.entry_fill_status, EntryFillStatus.FULL)
        self.assertIs(entered.state, PaperState.UNPROTECTED)
        self.assertEqual(entered.position_quantity, Decimal("1"))
        self.assertEqual(entered.active_stop_quantity, 0)
        self.assertEqual(entered.active_take_profit_quantity, 0)

        active = entered.reconcile_protection(
            protection("full", NOW + timedelta(seconds=2)),
            event_id="protect-full",
        )
        self.assertIs(active.state, PaperState.PROTECTED)
        self.assertEqual(active.active_stop_quantity, active.position_quantity)
        self.assertEqual(active.active_take_profit_quantity, active.position_quantity)

    def test_unfilled_ioc_returns_flat_without_creating_exposure(self) -> None:
        result = fresh_oms().observe_entry(
            book(
                "unfilled",
                NOW + timedelta(seconds=1),
                ask=Decimal("102"),
            ),
            event_id="entry-unfilled",
        )
        self.assertIs(result.entry_fill_status, EntryFillStatus.UNFILLED)
        self.assertIs(result.state, PaperState.FLAT)
        self.assertEqual(result.position_quantity, 0)
        self.assertIsNone(result.plan)
        self.assertIs(result.events[-1].event_type, PaperEventType.ENTRY_UNFILLED)

    def test_partial_entry_never_activates_children_and_requires_flatten(self) -> None:
        partial = fresh_oms().observe_entry(
            book(
                "partial",
                NOW + timedelta(seconds=1),
                ask_size=Decimal("0.4"),
            ),
            event_id="entry-partial",
        )
        self.assertIs(partial.entry_fill_status, EntryFillStatus.PARTIAL)
        self.assertIs(partial.state, PaperState.UNDERPROTECTED)
        self.assertTrue(partial.halted)
        self.assertTrue(partial.emergency_flatten_required)
        self.assertEqual(partial.active_stop_quantity, 0)
        with self.assertRaisesRegex(PaperStateError, "requires full entry"):
            partial.reconcile_protection(
                protection(
                    "forbidden",
                    NOW + timedelta(seconds=2),
                    stop_quantity=Decimal("0.4"),
                    target_quantity=Decimal("0.4"),
                ),
                event_id="forbidden-protect",
            )

    def test_missing_stop_halts_new_risk_and_enters_flatten_path(self) -> None:
        entered = fresh_oms().observe_entry(
            book("entry", NOW + timedelta(seconds=1)), event_id="entry"
        )
        halted = entered.reconcile_protection(
            protection(
                "missing-stop",
                NOW + timedelta(seconds=2),
                stop_status=LegStatus.MISSING,
                stop_quantity=Decimal("0"),
            ),
            event_id="missing-stop",
        )
        self.assertIs(halted.state, PaperState.HALTED)
        self.assertTrue(halted.halted)
        self.assertTrue(halted.emergency_flatten_required)
        with self.assertRaisesRegex(PaperStateError, "non-halted flat"):
            halted.submit_plan(
                protected_plan(),
                at=NOW + timedelta(seconds=3),
                event_id="second-plan",
            )

    def test_missing_target_also_keeps_fully_stopped_position_halted(self) -> None:
        entered = fresh_oms().observe_entry(
            book("entry", NOW + timedelta(seconds=1)), event_id="entry"
        )
        halted = entered.reconcile_protection(
            protection(
                "missing-target",
                NOW + timedelta(seconds=2),
                target_status=LegStatus.MISSING,
                target_quantity=Decimal("0"),
            ),
            event_id="missing-target",
        )
        self.assertIs(halted.state, PaperState.HALTED)
        self.assertEqual(halted.active_stop_quantity, Decimal("1"))
        partially_flattened = halted.emergency_flatten(
            book(
                "partial-after-target-failure",
                NOW + timedelta(seconds=3),
                bid_size=Decimal("0.5"),
            ),
            event_id="partial-after-target-failure",
        )
        self.assertIs(partially_flattened.state, PaperState.HALTED)
        self.assertEqual(partially_flattened.position_quantity, Decimal("0.5"))
        self.assertEqual(
            partially_flattened.active_stop_quantity, Decimal("0.5")
        )

    def test_protection_disappearing_after_activation_halts(self) -> None:
        active = protected_oms()
        halted = active.reconcile_protection(
            protection(
                "lost-stop",
                NOW + timedelta(seconds=3),
                stop_status=LegStatus.MISSING,
                stop_quantity=Decimal("0"),
            ),
            event_id="lost-stop",
        )
        self.assertIs(halted.state, PaperState.HALTED)
        self.assertEqual(halted.active_stop_quantity, 0)
        self.assertTrue(halted.emergency_flatten_required)

    def test_expired_entry_observation_is_explicitly_unfilled(self) -> None:
        result = fresh_oms().observe_entry(
            book("expired", NOW + timedelta(hours=1)), event_id="expired-entry"
        )
        self.assertIs(result.entry_fill_status, EntryFillStatus.UNFILLED)
        self.assertEqual(
            result.events[-1].reason, "entry_expired_before_observation"
        )

    def test_undersized_stop_is_distinct_underprotected_state(self) -> None:
        entered = fresh_oms().observe_entry(
            book("entry", NOW + timedelta(seconds=1)), event_id="entry"
        )
        result = entered.reconcile_protection(
            protection(
                "undersized",
                NOW + timedelta(seconds=2),
                stop_quantity=Decimal("0.5"),
            ),
            event_id="underprotected",
        )
        self.assertIs(result.state, PaperState.UNDERPROTECTED)
        self.assertEqual(result.active_stop_quantity, Decimal("0.5"))
        self.assertTrue(result.halted)

    def test_missing_protection_ack_times_out_to_halted(self) -> None:
        entered = fresh_oms().observe_entry(
            book("entry", NOW + timedelta(seconds=1)), event_id="entry"
        )
        with self.assertRaisesRegex(PaperStateError, "has not elapsed"):
            entered.protection_timeout(
                at=NOW + timedelta(seconds=5), event_id="too-early"
            )
        halted = entered.protection_timeout(
            at=NOW + timedelta(seconds=6), event_id="timeout"
        )
        self.assertIs(halted.state, PaperState.HALTED)
        self.assertTrue(halted.emergency_flatten_required)

    def test_only_one_plan_or_position_per_account_instrument(self) -> None:
        pending = fresh_oms()
        with self.assertRaisesRegex(PaperStateError, "non-halted flat"):
            pending.submit_plan(
                protected_plan(), at=NOW + timedelta(seconds=1), event_id="duplicate-plan"
            )
        active = protected_oms()
        with self.assertRaisesRegex(PaperStateError, "non-halted flat"):
            active.submit_plan(
                protected_plan(), at=NOW + timedelta(seconds=3), event_id="second-position"
            )


class ProtectedLifecycleTests(unittest.TestCase):
    def test_stop_wins_when_both_legs_touch_same_completed_candle(self) -> None:
        active = protected_oms()
        observation = paper_candle(
            "both",
            NOW + timedelta(seconds=2),
            open_price=Decimal("100"),
            high=Decimal("111"),
            low=Decimal("94"),
            close=Decimal("100"),
        )
        closed = active.observe_protected_candle(observation, event_id="both")
        self.assertIs(closed.state, PaperState.FLAT)
        self.assertIs(closed.events[-1].event_type, PaperEventType.STOP_FILLED)
        self.assertEqual(
            closed.events[-1].reason,
            "stop_first_when_stop_and_target_share_completed_bar",
        )
        self.assertEqual(closed.events[-1].price, Decimal("94.81"))
        self.assertLess(closed.realized_net_pnl, 0)

    def test_gap_and_slippage_use_observed_open_not_trigger(self) -> None:
        active = protected_oms()
        observation = paper_candle(
            "gap",
            NOW + timedelta(seconds=2),
            open_price=Decimal("94.5"),
            high=Decimal("96"),
            low=Decimal("94"),
            close=Decimal("95"),
        )
        closed = active.observe_protected_candle(observation, event_id="gap")
        self.assertIs(closed.state, PaperState.FLAT)
        self.assertEqual(closed.events[-1].reason, "long_gap_through_stop")
        self.assertEqual(closed.events[-1].price, Decimal("94.311"))

    def test_gap_beyond_bounded_stop_halts_instead_of_claiming_fill(self) -> None:
        active = protected_oms()
        observation = paper_candle(
            "gap-too-far",
            NOW + timedelta(seconds=2),
            open_price=Decimal("90"),
            high=Decimal("91"),
            low=Decimal("89"),
            close=Decimal("90"),
        )
        halted = active.observe_protected_candle(observation, event_id="gap-failed")
        self.assertIs(halted.state, PaperState.HALTED)
        self.assertEqual(halted.position_quantity, Decimal("1"))
        self.assertTrue(halted.emergency_flatten_required)
        self.assertIs(
            halted.events[-1].event_type, PaperEventType.STOP_UNFILLED_HALT
        )

    def test_target_fill_charges_exit_fee(self) -> None:
        active = protected_oms()
        observation = paper_candle(
            "target",
            NOW + timedelta(seconds=2),
            open_price=Decimal("100"),
            high=Decimal("111"),
            low=Decimal("99"),
            close=Decimal("110"),
        )
        closed = active.observe_protected_candle(observation, event_id="target")
        self.assertIs(closed.events[-1].event_type, PaperEventType.TARGET_FILLED)
        self.assertEqual(closed.events[-1].price, Decimal("109.78"))
        self.assertGreater(closed.events[-1].fee, 0)
        self.assertGreater(closed.realized_net_pnl, 0)

    def test_completed_candle_cannot_begin_before_protection_observation(self) -> None:
        active = protected_oms()
        observation = paper_candle(
            "lookahead",
            NOW,
            open_price=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
        with self.assertRaisesRegex(PaperStateError, "before protection"):
            active.observe_protected_candle(observation, event_id="lookahead")

    def test_short_side_is_symmetric_and_stop_first(self) -> None:
        active = protected_oms(Side.SELL)
        observation = paper_candle(
            "short-both",
            NOW + timedelta(seconds=2),
            open_price=Decimal("100"),
            high=Decimal("106"),
            low=Decimal("89"),
            close=Decimal("100"),
        )
        closed = active.observe_protected_candle(observation, event_id="short-both")
        self.assertIs(closed.events[-1].event_type, PaperEventType.STOP_FILLED)
        self.assertEqual(
            closed.events[-1].reason,
            "stop_first_when_stop_and_target_share_completed_bar",
        )
        self.assertEqual(closed.events[-1].price, Decimal("105.21"))


class EmergencyAndRecoveryTests(unittest.TestCase):
    def test_partial_entry_flattens_reduce_only_and_remains_halted_for_review(self) -> None:
        partial = fresh_oms().observe_entry(
            book(
                "partial",
                NOW + timedelta(seconds=1),
                ask_size=Decimal("0.4"),
            ),
            event_id="entry-partial",
        )
        flattened = partial.emergency_flatten(
            book(
                "flatten",
                NOW + timedelta(seconds=2),
                bid=Decimal("99"),
            ),
            event_id="flatten",
        )
        self.assertIs(flattened.state, PaperState.HALTED)
        self.assertEqual(flattened.position_quantity, 0)
        self.assertFalse(flattened.emergency_flatten_required)
        self.assertIs(
            flattened.events[-1].event_type,
            PaperEventType.EMERGENCY_FLATTEN_FULL,
        )
        self.assertLess(flattened.realized_net_pnl, 0)

        resumed = flattened.acknowledge_halt(
            at=NOW + timedelta(seconds=3),
            event_id="review",
            review_hash=digest("review"),
        )
        self.assertIs(resumed.state, PaperState.FLAT)
        self.assertFalse(resumed.halted)

    def test_emergency_flatten_can_be_partial_or_unfilled_without_losing_exposure(self) -> None:
        partial = fresh_oms().observe_entry(
            book(
                "partial",
                NOW + timedelta(seconds=1),
                ask_size=Decimal("0.4"),
            ),
            event_id="entry-partial",
        )
        unfilled = partial.emergency_flatten(
            book(
                "empty",
                NOW + timedelta(seconds=2),
                bid_size=Decimal("0"),
            ),
            event_id="empty-flatten",
        )
        self.assertIs(unfilled.state, PaperState.HALTED)
        self.assertEqual(unfilled.position_quantity, Decimal("0.4"))

        reduced = unfilled.emergency_flatten(
            book(
                "thin",
                NOW + timedelta(seconds=3),
                bid_size=Decimal("0.2"),
            ),
            event_id="partial-flatten",
        )
        self.assertIs(reduced.state, PaperState.UNDERPROTECTED)
        self.assertEqual(reduced.position_quantity, Decimal("0.2"))
        self.assertTrue(reduced.emergency_flatten_required)

    def test_restart_verifies_snapshot_and_event_chain(self) -> None:
        active = protected_oms()
        restored = PaperOMS.restore(active, expected_hash=active.snapshot_hash)
        self.assertEqual(restored, active)
        tampered = replace(active, realized_net_pnl=Decimal("1"))
        with self.assertRaisesRegex(PaperStateError, "hash mismatch"):
            PaperOMS.restore(tampered, expected_hash=active.snapshot_hash)
        changed_event = replace(active.events[-1], reason="tampered")
        with self.assertRaisesRegex(PaperStateError, "event chain"):
            replace(active, events=active.events[:-1] + (changed_event,))

    def test_duplicate_or_out_of_order_events_fail_closed(self) -> None:
        active = protected_oms()
        with self.assertRaisesRegex(PaperStateError, "event_id"):
            active.observe_protected_candle(
                paper_candle(
                    "duplicate",
                    NOW + timedelta(seconds=2),
                    open_price=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                ),
                event_id="protection",
            )
        with self.assertRaisesRegex(PaperStateError, "already consumed"):
            active.reconcile_protection(
                protection("active", NOW + timedelta(seconds=2)),
                event_id="different-event-same-source",
            )
        observed = active.observe_protected_candle(
            paper_candle(
                "no-trigger",
                NOW + timedelta(seconds=2),
                open_price=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
            ),
            event_id="no-trigger",
        )
        with self.assertRaisesRegex(PaperStateError, "before protection"):
            observed.observe_protected_candle(
                paper_candle(
                    "overlap",
                    NOW + timedelta(hours=1),
                    open_price=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                ),
                event_id="overlap",
            )

    def test_public_snapshot_never_claims_venue_or_testnet_execution(self) -> None:
        payload = protected_oms().to_dict()
        self.assertEqual(payload["mode"], "local_paper")
        self.assertFalse(payload["venue_execution"])
        self.assertFalse(payload["testnet_execution"])
        self.assertFalse(payload["mainnet_execution"])

    def test_costs_and_observations_reject_binary_float(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be Decimal"):
            costs(fee_bps_per_fill=0.5)
        with self.assertRaisesRegex(TypeError, "must be Decimal"):
            book("float", NOW, bid=99.0)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
