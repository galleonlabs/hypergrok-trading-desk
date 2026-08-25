from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trading_harness.canonical import domain_hash
from trading_harness.errors import StateConflict
from trading_harness.execution_store import LegReconciliation
from trading_harness.hyperliquid_reconcile import (
    FillCoverage,
    ParsedOrderStatus,
    VenueOrderState,
    VenueReconciliationBundle,
    VENUE_RECONCILIATION_HASH_DOMAIN,
)
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.reconciliation_coordinator import (
    MainEntryReconciliationCoordinator,
    _bundle_material,
)
from tests.test_execution_store import ExecutionStoreTestCase, NOW
from tests.test_hyperliquid_account import (
    FixtureTransport,
    fetch,
    raw_position,
    valid_clearing,
)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def millis(value: datetime) -> int:
    delta = value - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def account_snapshot(*, foreign_position: bool = False):
    server = millis(NOW + timedelta(seconds=5))
    positions = (
        [
            raw_position(
                symbol="BTC",
                signed_size="0.01",
                entry_price="60000",
                position_value="600",
                max_leverage=40,
            )
        ]
        if foreign_position
        else []
    )
    clearing = valid_clearing(positions=positions, server_time=server)
    if not foreign_position:
        for key in ("marginSummary", "crossMarginSummary"):
            clearing[key]["totalNtlPos"] = "0"  # type: ignore[index]
            clearing[key]["totalMarginUsed"] = "0"  # type: ignore[index]
    snapshot, _ = fetch(
        FixtureTransport(clearing=clearing, orders=[]),
        received_at_ms=server + 500,
        network="testnet",
    )
    return snapshot


def make_bundle(store, snapshot, *, complete: bool = True, reason: str | None = None):
    command = store.get_command("command-1")
    durable = {item.role: item for item in store.get_legs("command-1")}
    statuses = []
    updates = []
    for index, role in enumerate(("entry", "protective_stop", "take_profit")):
        leg = durable[role]
        statuses.append(
            ParsedOrderStatus(
                role=role,
                requested_cloid=leg.cloid,
                state=VenueOrderState.ORDER,
                venue_status="canceled",
                status_timestamp_ms=snapshot.server_time_ms - 1,
                oid=100 + index,
                symbol="ETH",
                remaining_size=Decimal("0"),
                original_size=leg.requested_quantity,
                is_trigger=role != "entry",
                reduce_only=role != "entry",
            )
        )
        updates.append(
            LegReconciliation(
                role=role,
                cloid=leg.cloid,
                status="canceled",
                cumulative_filled=Decimal("0"),
                venue_oid=100 + index,
            )
        )
    saturated = reason == "page_saturated"
    retention_limited = reason == "retention_limited"
    coverage = FillCoverage(
        requested_start_time_ms=store.get_preflight(
            "command-1"
        ).account_server_time_ms,
        requested_end_time_ms=snapshot.server_time_ms,
        page_count=1,
        page_limit=2_000,
        retention_limit=10_000,
        returned_rows=0,
        unique_fills=0,
        duplicate_fills=0,
        unmatched_fills=0,
        page_saturated=saturated,
        retention_limited=retention_limited,
        complete=not saturated and not retention_limited,
        reason=(
            "page_saturated"
            if saturated
            else "retention_limited"
            if retention_limited
            else "range_exhausted"
        ),
    )
    reasons = () if complete else (reason or "incomplete_fixture",)
    provisional = VenueReconciliationBundle(
        network=HyperliquidNetwork.TESTNET,
        main_account_address=snapshot.main_account_address,
        account_id=store.account_id,
        command_id="command-1",
        plan_hash=command.plan_hash,
        account_snapshot_hash=snapshot.snapshot_hash,
        observed_at=EPOCH + timedelta(milliseconds=snapshot.server_time_ms),
        order_statuses=tuple(statuses),
        signed_fills=(),
        fill_coverage=coverage,
        legs=tuple(updates),
        fills=(),
        signed_position_quantity=Decimal("0"),
        protected_quantity=Decimal("0"),
        complete=complete,
        incomplete_reasons=reasons,
        reconciliation_hash="0" * 64,
    )
    return replace(
        provisional,
        reconciliation_hash=domain_hash(
            VENUE_RECONCILIATION_HASH_DOMAIN, _bundle_material(provisional)
        ),
    )


class ReconciliationCoordinatorTests(ExecutionStoreTestCase):
    def prepare(self, *, foreign_position: bool = False):
        ticket, fencing = self.prepare_unknown()
        snapshot = account_snapshot(foreign_position=foreign_position)
        coordinator = MainEntryReconciliationCoordinator(
            self.store,
            network=HyperliquidNetwork.TESTNET,
            clock=lambda: EPOCH
            + timedelta(milliseconds=snapshot.received_at_ms),
        )
        return ticket, fencing, snapshot, coordinator

    def test_public_boundary_rejects_scalar_dict_and_forged_hash(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare()
        before = self.store.get_reserved_exposure()
        with self.assertRaisesRegex(TypeError, "VenueReconciliationBundle"):
            coordinator.apply_bundle(  # type: ignore[arg-type]
                {
                    "account_snapshot_hash": "a" * 64,
                    "complete": True,
                    "signed_position_quantity": "0",
                },
                snapshot,
                worker_id="reconciler",
                fencing_token=fencing,
                reconciliation_id="scalar",
            )
        forged = replace(
            make_bundle(self.store, snapshot),
            signed_position_quantity=Decimal("999"),
        )
        with self.assertRaisesRegex(StateConflict, "hash"):
            coordinator.apply_bundle(
                forged,
                snapshot,
                worker_id="reconciler",
                fencing_token=fencing,
                reconciliation_id="forged",
            )
        self.assertEqual(self.store.get_reserved_exposure(), before)
        self.assertEqual(before[0], ticket.stressed_loss)

    def test_saturated_incomplete_bundle_cannot_release_any_risk(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare()
        bundle = make_bundle(
            self.store, snapshot, complete=False, reason="page_saturated"
        )
        result = coordinator.apply_bundle(
            bundle,
            snapshot,
            worker_id="reconciler",
            fencing_token=fencing,
            reconciliation_id="saturated",
        )
        self.assertFalse(result.terminal)
        self.assertFalse(result.evidence_complete)
        self.assertEqual(result.command_state, "reconciling")
        self.assertEqual(result.risk_released_loss, Decimal("0"))
        self.assertEqual(result.risk_released_notional, Decimal("0"))
        self.assertEqual(result.residual_command_reserved_loss, ticket.stressed_loss)
        self.assertEqual(result.account_reserved_loss, ticket.stressed_loss)

    def test_retention_limited_bundle_cannot_terminalize_or_release_risk(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare()
        bundle = make_bundle(
            self.store, snapshot, complete=False, reason="retention_limited"
        )
        result = coordinator.apply_bundle(
            bundle,
            snapshot,
            worker_id="reconciler",
            fencing_token=fencing,
            reconciliation_id="retention-limited",
        )
        self.assertFalse(result.terminal)
        self.assertEqual(result.risk_released_loss, Decimal("0"))
        self.assertEqual(result.residual_command_reserved_loss, ticket.stressed_loss)

    def test_foreign_state_cannot_claim_complete_or_release_risk(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare(foreign_position=True)
        malicious = make_bundle(self.store, snapshot, complete=True)
        before = self.store.get_reserved_exposure()
        with self.assertRaisesRegex(StateConflict, "foreign"):
            coordinator.apply_bundle(
                malicious,
                snapshot,
                worker_id="reconciler",
                fencing_token=fencing,
                reconciliation_id="foreign-complete",
            )
        self.assertEqual(self.store.get_reserved_exposure(), before)

        incomplete = make_bundle(
            self.store,
            snapshot,
            complete=False,
            reason="foreign_positions",
        )
        result = coordinator.apply_bundle(
            incomplete,
            snapshot,
            worker_id="reconciler",
            fencing_token=fencing,
            reconciliation_id="foreign-incomplete",
        )
        self.assertFalse(result.terminal)
        self.assertEqual(result.risk_released_loss, Decimal("0"))
        self.assertEqual(result.residual_command_reserved_loss, ticket.stressed_loss)

    def test_valid_fresh_flat_complete_bundle_terminalizes_and_reports_zero_residual(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare()
        bundle = make_bundle(self.store, snapshot)
        result = coordinator.apply_bundle(
            bundle,
            snapshot,
            worker_id="reconciler",
            fencing_token=fencing,
            reconciliation_id="valid-flat",
        )
        self.assertTrue(result.terminal)
        self.assertTrue(result.evidence_complete)
        self.assertEqual(result.command_state, "terminal")
        self.assertEqual(result.protection_state, "flat")
        self.assertEqual(result.risk_released_loss, ticket.stressed_loss)
        self.assertGreater(result.risk_released_notional, 0)
        self.assertEqual(result.residual_command_reserved_loss, Decimal("0"))
        self.assertEqual(result.residual_command_reserved_notional, Decimal("0"))
        self.assertEqual(result.account_reserved_loss, Decimal("0"))
        self.assertEqual(result.account_reserved_notional, Decimal("0"))

    def test_stale_snapshot_and_hash_valid_complete_claim_fail_before_store(self) -> None:
        ticket, fencing, snapshot, _ = self.prepare()
        coordinator = MainEntryReconciliationCoordinator(
            self.store,
            network=HyperliquidNetwork.TESTNET,
            clock=lambda: EPOCH
            + timedelta(milliseconds=snapshot.server_time_ms + 5_001),
        )
        before = self.store.get_reserved_exposure()
        with self.assertRaisesRegex(StateConflict, "stale"):
            coordinator.apply_bundle(
                make_bundle(self.store, snapshot),
                snapshot,
                worker_id="reconciler",
                fencing_token=fencing,
                reconciliation_id="stale",
            )
        self.assertEqual(self.store.get_reserved_exposure(), before)

    def test_replaced_snapshot_fields_with_old_hash_are_rejected(self) -> None:
        ticket, fencing, snapshot, coordinator = self.prepare()
        forged_snapshot = replace(snapshot, withdrawable=Decimal("999999"))
        before = self.store.get_reserved_exposure()
        with self.assertRaisesRegex(StateConflict, "snapshot hash"):
            coordinator.apply_bundle(
                make_bundle(self.store, snapshot),
                forged_snapshot,
                worker_id="reconciler",
                fencing_token=fencing,
                reconciliation_id="forged-snapshot",
            )
        self.assertEqual(self.store.get_reserved_exposure(), before)


if __name__ == "__main__":
    unittest.main()
