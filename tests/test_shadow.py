from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

import trading_harness.shadow as shadow_module
from trading_harness.backtest import PromotionStatus
from trading_harness.shadow import (
    DriftAssessment,
    DriftStatus,
    SentimentAuthority,
    ShadowLedger,
    ShadowLedgerError,
    ShadowOutcomeRecord,
    ShadowProtocol,
    ShadowRecordStatus,
    ShadowSignalRecord,
    ShadowVariant,
    evaluate_shadow,
)
from trading_harness.strategy import SignalDirection


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def digest(value: int) -> str:
    return f"{value:064x}"


def protocol() -> ShadowProtocol:
    return ShadowProtocol(
        protocol_id="eth-shadow-v1",
        version="1",
        asset_id="hyperliquid:ETH-PERP",
        registered_at=START,
        started_at=START,
        ta_strategy_hash=digest(1),
        sentiment_strategy_hash=digest(2),
        cost_model_hash=digest(3),
        drift_policy_hash=digest(4),
    )


def signal_record(
    study: ShadowProtocol,
    index: int,
    variant: ShadowVariant,
    *,
    eligible: bool = True,
    recorded_delay: timedelta = timedelta(0),
) -> ShadowSignalRecord:
    observed = study.started_at + timedelta(days=index)
    variant_offset = 1 if variant is ShadowVariant.TA_ONLY else 2
    return ShadowSignalRecord(
        event_id=f"signal-event-{variant.value}-{index}",
        signal_id=f"signal-{variant.value}-{index}",
        comparison_id=f"comparison-{index:04d}",
        asset_id=study.asset_id,
        variant=variant,
        direction=SignalDirection.BUY,
        strategy_hash=study.strategy_hash_for(variant),
        signal_hash=digest(10_000 + index * 10 + variant_offset),
        data_hash=digest(20_000 + index),
        cost_model_hash=study.cost_model_hash,
        evidence_hash=digest(30_000 + index * 10 + variant_offset),
        observed_at=observed,
        expires_at=observed + timedelta(minutes=15),
        recorded_at=observed + recorded_delay,
        eligible=eligible,
    )


def outcome_record(
    signal: ShadowSignalRecord,
    index: int,
    *,
    net_r: Decimal = Decimal("0.1"),
    cost_r: Decimal = Decimal("0.01"),
    status: ShadowRecordStatus = ShadowRecordStatus.CLOSED,
) -> ShadowOutcomeRecord:
    closed = signal.observed_at + timedelta(hours=4)
    if status is ShadowRecordStatus.INVALID:
        return ShadowOutcomeRecord(
            event_id=f"outcome-event-{signal.variant.value}-{index}",
            signal_id=signal.signal_id,
            signal_event_hash=signal.event_hash,
            strategy_hash=signal.strategy_hash,
            signal_hash=signal.signal_hash,
            data_hash=signal.data_hash,
            cost_model_hash=signal.cost_model_hash,
            outcome_evidence_hash=digest(40_000 + index * 10 + 1),
            status=status,
            closed_at=closed,
            recorded_at=closed,
            invalid_reason="point_in_time_market_data_gap",
        )
    return ShadowOutcomeRecord(
        event_id=f"outcome-event-{signal.variant.value}-{index}",
        signal_id=signal.signal_id,
        signal_event_hash=signal.event_hash,
        strategy_hash=signal.strategy_hash,
        signal_hash=signal.signal_hash,
        data_hash=signal.data_hash,
        cost_model_hash=signal.cost_model_hash,
        outcome_evidence_hash=digest(40_000 + index * 10 + (1 if signal.variant is ShadowVariant.TA_ONLY else 2)),
        status=status,
        closed_at=closed,
        recorded_at=closed,
        gross_r=net_r + cost_r,
        cost_r=cost_r,
        net_r=net_r,
    )


def populated_ledger(
    study: ShadowProtocol,
    count: int,
    *,
    ta_net_r: Decimal = Decimal("0.1"),
    sentiment_net_r: Decimal = Decimal("0.2"),
) -> ShadowLedger:
    ledger = ShadowLedger.create(study)
    for index in range(count):
        ta = signal_record(study, index, ShadowVariant.TA_ONLY)
        sentiment = signal_record(study, index, ShadowVariant.TA_SENTIMENT)
        ledger = ledger.append_signal(study, ta)
        ledger = ledger.append_signal(study, sentiment)
        ledger = ledger.append_outcome(
            study, outcome_record(ta, index, net_r=ta_net_r)
        )
        ledger = ledger.append_outcome(
            study, outcome_record(sentiment, index, net_r=sentiment_net_r)
        )
    return ledger


def drift(study: ShadowProtocol, at: datetime, status: DriftStatus = DriftStatus.PASS) -> DriftAssessment:
    return DriftAssessment(
        policy_hash=study.drift_policy_hash,
        status=status,
        assessed_at=at,
        evidence_hash=digest(50_000),
    )


class ShadowLedgerTests(unittest.TestCase):
    def test_append_is_immutable_and_status_is_derived(self) -> None:
        study = protocol()
        empty = ShadowLedger.create(study)
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        pending = empty.append_signal(study, signal)
        closed = pending.append_outcome(study, outcome_record(signal, 0))

        self.assertEqual(empty.events, ())
        self.assertIs(pending.status_for(signal.signal_id), ShadowRecordStatus.PENDING)
        self.assertIs(closed.status_for(signal.signal_id), ShadowRecordStatus.CLOSED)
        self.assertNotEqual(empty.chain_hash, pending.chain_hash)
        self.assertNotEqual(pending.chain_hash, closed.chain_hash)

    def test_invalid_is_distinct_from_pending_and_closed(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        ledger = ShadowLedger.create(study).append_signal(study, signal)
        ledger = ledger.append_outcome(
            study,
            outcome_record(signal, 0, status=ShadowRecordStatus.INVALID),
        )
        self.assertIs(ledger.status_for(signal.signal_id), ShadowRecordStatus.INVALID)

    def test_duplicate_signal_and_second_terminal_outcome_are_rejected(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        ledger = ShadowLedger.create(study).append_signal(study, signal)
        with self.assertRaisesRegex(ShadowLedgerError, "signal_id"):
            ledger.append_signal(study, replace(signal, event_id="another-event"))
        closed = ledger.append_outcome(study, outcome_record(signal, 0))
        with self.assertRaisesRegex(ShadowLedgerError, "terminal outcome"):
            closed.append_outcome(
                study,
                replace(outcome_record(signal, 0), event_id="another-outcome"),
            )

    def test_outcome_must_bind_every_signal_hash(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        ledger = ShadowLedger.create(study).append_signal(study, signal)
        mismatched = replace(outcome_record(signal, 0), data_hash=digest(999))
        with self.assertRaisesRegex(ShadowLedgerError, "exact signal evidence"):
            ledger.append_outcome(study, mismatched)

    def test_hash_chain_detects_event_tampering(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        ledger = ShadowLedger.create(study).append_signal(study, signal)
        tampered = replace(signal, evidence_hash=digest(999))
        with self.assertRaisesRegex(ShadowLedgerError, "hash chain"):
            replace(ledger, events=(tampered,))

    def test_evaluation_replays_invariants_after_direct_construction(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        duplicate = replace(signal, event_id="second-event")
        events = (signal, duplicate)
        forged = ShadowLedger(
            protocol_hash=study.protocol_hash,
            events=events,
            chain_hash=shadow_module._ledger_hash(study.protocol_hash, events),
        )
        as_of = study.started_at + timedelta(days=90)
        with self.assertRaisesRegex(ShadowLedgerError, "signal_id"):
            evaluate_shadow(study, forged, drift(study, as_of), as_of=as_of)

    def test_late_signal_and_future_outcome_evidence_fail_closed(self) -> None:
        study = protocol()
        with self.assertRaisesRegex(ValueError, "before expiry"):
            signal_record(
                study,
                0,
                ShadowVariant.TA_ONLY,
                recorded_delay=timedelta(minutes=16),
            )

        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        ledger = ShadowLedger.create(study).append_signal(study, signal)
        ledger = ledger.append_outcome(study, outcome_record(signal, 0))
        as_of = signal.observed_at + timedelta(hours=1)
        with self.assertRaisesRegex(ShadowLedgerError, "after as_of"):
            evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)

    def test_costed_outcome_requires_exact_net_equation_and_no_floats(self) -> None:
        study = protocol()
        signal = signal_record(study, 0, ShadowVariant.TA_ONLY)
        with self.assertRaisesRegex(ValueError, "exactly equal"):
            replace(outcome_record(signal, 0), net_r=Decimal("0.2"))
        with self.assertRaisesRegex(TypeError, "must be Decimal"):
            replace(outcome_record(signal, 0), cost_r=0.01)  # type: ignore[arg-type]

    def test_protocol_thresholds_cannot_be_weakened(self) -> None:
        with self.assertRaisesRegex(ValueError, "frozen at 90"):
            replace(protocol(), minimum_elapsed_days=30)
        with self.assertRaisesRegex(ValueError, "frozen at 50"):
            replace(protocol(), minimum_closed_signals=10)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            replace(protocol(), minimum_incremental_r=Decimal("-0.01"))


class ShadowProfitabilityTests(unittest.TestCase):
    def test_requires_both_ninety_days_and_fifty_closed_signals(self) -> None:
        study = protocol()
        fifty = populated_ledger(study, 50)
        at_89_days = study.started_at + timedelta(days=89)
        time_short = evaluate_shadow(
            study,
            fifty,
            drift(study, at_89_days),
            as_of=at_89_days,
        )
        self.assertIs(time_short.promotion.status, PromotionStatus.INCONCLUSIVE)
        self.assertIn("fewer_than_90_elapsed_days", time_short.promotion.reasons)

        forty_nine = populated_ledger(study, 49)
        at_90_days = study.started_at + timedelta(days=90)
        count_short = evaluate_shadow(
            study,
            forty_nine,
            drift(study, at_90_days),
            as_of=at_90_days,
        )
        self.assertIs(count_short.promotion.status, PromotionStatus.INCONCLUSIVE)
        self.assertIn(
            "fewer_than_50_closed_eligible_signals", count_short.promotion.reasons
        )

    def test_positive_prospective_and_incremental_evidence_passes(self) -> None:
        study = protocol()
        ledger = populated_ledger(study, 50)
        as_of = study.started_at + timedelta(days=90)
        first = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)
        second = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)

        self.assertIs(first.promotion.status, PromotionStatus.PASS)
        self.assertEqual(first.sentiment_metrics.trade_count, 50)
        self.assertGreater(first.sentiment_metrics.expectancy_r, 0)
        self.assertGreater(first.sentiment_metrics.bootstrap_lower_95_r, 0)  # type: ignore[operator]
        self.assertIs(first.incremental.promotion.status, PromotionStatus.PASS)
        self.assertIs(
            first.sentiment_authority, SentimentAuthority.DIRECTIONAL_ELIGIBLE
        )
        self.assertEqual(first.artifact_hash, second.artifact_hash)
        encoded = json.dumps(first.to_dict(), allow_nan=False, sort_keys=True)
        self.assertIn(first.artifact_hash, encoded)

    def test_no_incremental_sentiment_edge_keeps_veto_only(self) -> None:
        study = protocol()
        ledger = populated_ledger(
            study,
            50,
            ta_net_r=Decimal("0.1"),
            sentiment_net_r=Decimal("0.1"),
        )
        as_of = study.started_at + timedelta(days=90)
        artifact = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)
        self.assertIs(artifact.promotion.status, PromotionStatus.PASS)
        self.assertIs(
            artifact.incremental.promotion.status, PromotionStatus.REJECTED
        )
        self.assertEqual(artifact.incremental.mean_incremental_r, Decimal("0"))
        self.assertIs(artifact.sentiment_authority, SentimentAuthority.VETO_ONLY)

    def test_predeclared_minimum_incremental_effect_is_enforced(self) -> None:
        study = replace(protocol(), minimum_incremental_r=Decimal("0.15"))
        ledger = populated_ledger(
            study,
            50,
            ta_net_r=Decimal("0.1"),
            sentiment_net_r=Decimal("0.2"),
        )
        as_of = study.started_at + timedelta(days=90)
        artifact = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)
        self.assertIs(
            artifact.incremental.promotion.status, PromotionStatus.REJECTED
        )
        self.assertIs(artifact.sentiment_authority, SentimentAuthority.VETO_ONLY)

    def test_negative_costed_edge_is_rejected_after_sufficient_evidence(self) -> None:
        study = protocol()
        ledger = populated_ledger(
            study,
            50,
            ta_net_r=Decimal("-0.1"),
            sentiment_net_r=Decimal("-0.1"),
        )
        as_of = study.started_at + timedelta(days=90)
        artifact = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)
        self.assertIs(artifact.promotion.status, PromotionStatus.REJECTED)
        self.assertIn("positive_costed_expectancy", artifact.promotion.reasons)

    def test_unknown_and_failed_drift_cannot_pass(self) -> None:
        study = protocol()
        ledger = populated_ledger(study, 50)
        as_of = study.started_at + timedelta(days=90)
        unknown = evaluate_shadow(
            study,
            ledger,
            drift(study, as_of, DriftStatus.UNKNOWN),
            as_of=as_of,
        )
        failed = evaluate_shadow(
            study,
            ledger,
            drift(study, as_of, DriftStatus.FAIL),
            as_of=as_of,
        )
        self.assertIs(unknown.promotion.status, PromotionStatus.INCONCLUSIVE)
        self.assertIs(failed.promotion.status, PromotionStatus.REJECTED)

    def test_invalid_eligible_outcome_prevents_cherry_picked_pass(self) -> None:
        study = protocol()
        ledger = populated_ledger(study, 50)
        extra = signal_record(study, 50, ShadowVariant.TA_SENTIMENT)
        ledger = ledger.append_signal(study, extra)
        ledger = ledger.append_outcome(
            study,
            outcome_record(extra, 50, status=ShadowRecordStatus.INVALID),
        )
        as_of = study.started_at + timedelta(days=90)
        artifact = evaluate_shadow(study, ledger, drift(study, as_of), as_of=as_of)
        self.assertIs(artifact.promotion.status, PromotionStatus.INCONCLUSIVE)
        self.assertIn(
            "eligible_signal_has_invalid_outcome", artifact.promotion.reasons
        )
        self.assertIs(artifact.sentiment_authority, SentimentAuthority.VETO_ONLY)


if __name__ == "__main__":
    unittest.main()
