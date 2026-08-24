from __future__ import annotations

from decimal import Decimal
import unittest

from trading_harness.hyperliquid_response import (
    LegSubmissionState,
    SubmissionResponseError,
    parse_order_response,
)


SIZES = (Decimal("0.2"), Decimal("0.2"), Decimal("0.2"))


def response(statuses: list[object]) -> dict[str, object]:
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": statuses},
        },
    }


class BatchResponseTests(unittest.TestCase):
    def test_full_entry_and_resting_children_still_require_reconciliation(self) -> None:
        parsed = parse_order_response(
            response(
                [
                    {"filled": {"totalSz": "0.200", "avgPx": "2500.10", "oid": 1}},
                    {"resting": {"oid": 2}},
                    {"resting": {"oid": 3}},
                ]
            ),
            requested_sizes=SIZES,
        )

        self.assertTrue(parsed.entry_fully_filled)
        self.assertFalse(parsed.entry_partially_filled)
        self.assertEqual(parsed.legs[0].state, LegSubmissionState.FILLED)
        self.assertEqual(parsed.legs[0].filled_size, Decimal("0.2"))
        self.assertEqual(parsed.legs[0].average_price, Decimal("2500.10"))
        self.assertTrue(parsed.requires_reconciliation)
        self.assertFalse(parsed.protected_position_confirmed)
        self.assertFalse(parsed.as_dict()["protected_position_confirmed"])

    def test_partial_entry_is_explicit_emergency_state(self) -> None:
        parsed = parse_order_response(
            response(
                [
                    {"filled": {"totalSz": "0.05", "avgPx": "2501", "oid": 10}},
                    {"error": "Child canceled after IOC remainder"},
                    {"error": "Child canceled after IOC remainder"},
                ]
            ),
            requested_sizes=SIZES,
        )

        self.assertTrue(parsed.entry_partially_filled)
        self.assertFalse(parsed.entry_fully_filled)
        self.assertEqual(parsed.legs[1].state, LegSubmissionState.ERROR)

    def test_outer_and_single_prevalidation_errors_are_batch_errors(self) -> None:
        outer = parse_order_response(
            {"status": "err", "response": "Invalid nonce"},
            requested_sizes=SIZES,
        )
        prevalidation = parse_order_response(
            response([{"error": "Order has invalid tick"}]),
            requested_sizes=SIZES,
        )

        self.assertEqual(outer.whole_batch_error, "Invalid nonce")
        self.assertEqual(prevalidation.whole_batch_error, "Order has invalid tick")
        self.assertEqual(outer.legs, ())
        self.assertEqual(prevalidation.legs, ())

    def test_mixed_results_are_preserved_not_upgraded_to_success(self) -> None:
        parsed = parse_order_response(
            response(
                [
                    {"resting": {"oid": 1}},
                    {"error": "badTriggerPxRejected"},
                    {"resting": {"oid": 3}},
                ]
            ),
            requested_sizes=SIZES,
        )

        self.assertFalse(parsed.entry_fully_filled)
        self.assertEqual(
            [leg.state for leg in parsed.legs],
            [
                LegSubmissionState.RESTING,
                LegSubmissionState.ERROR,
                LegSubmissionState.RESTING,
            ],
        )

    def test_schema_drift_overfill_floats_and_wrong_counts_fail_closed(self) -> None:
        cases = (
            response([{"filled": {"totalSz": "0.3", "avgPx": "1", "oid": 1}}] * 3),
            response([{"filled": {"totalSz": 0.2, "avgPx": "1", "oid": 1}}] * 3),
            response([{"resting": {"oid": 1, "unexpected": True}}] * 3),
            response([{"resting": {"oid": 1}}, {"resting": {"oid": 2}}]),
            {"status": "mystery", "response": "private material"},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(SubmissionResponseError):
                    parse_order_response(value, requested_sizes=SIZES)

    def test_response_hash_is_canonical_and_stable(self) -> None:
        first = response(
            [
                {"resting": {"oid": 1}},
                {"resting": {"oid": 2}},
                {"resting": {"oid": 3}},
            ]
        )
        second = {
            "response": first["response"],
            "status": "ok",
        }
        self.assertEqual(
            parse_order_response(first, requested_sizes=SIZES).response_hash,
            parse_order_response(second, requested_sizes=SIZES).response_hash,
        )


if __name__ == "__main__":
    unittest.main()
