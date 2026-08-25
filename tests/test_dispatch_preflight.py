from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from trading_harness.account_risk import (
    AccountRiskLimits,
    compile_account_risk_snapshot,
)
from trading_harness.dispatch_preflight import build_dispatch_preflight
from trading_harness.domain import Environment
from trading_harness.errors import AdmissionDenied
from trading_harness.execution_store import ExecutionStore
from trading_harness.hyperliquid_account import fetch_account_snapshot
from trading_harness.planning import RiskSizingPolicy
from tests.test_account_risk import flat_clearing
from tests.test_execution_store import (
    NOW,
    make_approval,
    make_infrastructure_grant,
    make_ticket,
)
from tests.test_hyperliquid_account import ACCOUNT, FixtureTransport, valid_clearing


def setup_account():
    clearing = flat_clearing()
    clearing["time"] = int(NOW.timestamp() * 1000)
    venue = fetch_account_snapshot(
        ACCOUNT,
        "testnet",
        transport=FixtureTransport(clearing=clearing, orders=[]),
        clock=lambda: NOW + timedelta(milliseconds=500),
    )
    limits = AccountRiskLimits(
        account_id="testnet-account",
        main_account_address=ACCOUNT,
        environment=Environment.TESTNET,
        daily_loss_limit=Decimal("100"),
        aggregate_open_risk_limit=Decimal("100"),
        max_notional=Decimal("1000"),
        leverage=Decimal("2"),
    )
    account = compile_account_risk_snapshot(
        venue,
        symbol="ETH",
        limits=limits,
        daily_loss_used=0,
        open_risk_used=0,
    )
    return venue, account, venue.metadata.instrument("ETH").to_wire_metadata()


def positioned_account():
    clearing = valid_clearing()
    clearing["time"] = int(NOW.timestamp() * 1000)
    return fetch_account_snapshot(
        ACCOUNT,
        "testnet",
        transport=FixtureTransport(clearing=clearing),
        clock=lambda: NOW + timedelta(milliseconds=500),
    )


def market(ticket, *, at=NOW + timedelta(seconds=2)) -> dict[str, object]:
    bound = ticket.plan.entry.price_bound
    return {
        "network": "testnet",
        "symbol": "ETH",
        "received_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "mid_consistency": {"within_limit": True},
        "book": {
            "best_bid": str(bound - Decimal("1")),
            "best_ask": str(bound),
            "depth": {
                "25bps": {
                    "bid_size": "100",
                    "ask_size": "100",
                    "bid_complete": True,
                    "ask_complete": True,
                }
            },
        },
    }


class DispatchPreflightBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ExecutionStore(
            Path(self.temporary.name) / "execution.sqlite3",
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        self.ticket = make_ticket()
        grant = make_infrastructure_grant(self.ticket)
        self.store.register_infrastructure_grant(grant, at=NOW)
        self.store.register_ticket(
            self.ticket,
            infrastructure_grant_hash=grant.grant_hash,
            stored_at=NOW + timedelta(milliseconds=1),
        )
        approval = make_approval(self.ticket)
        self.store.register_approval(approval)
        self.store.admit(
            command_id="command-1",
            approval_id=approval.approval_id,
            token_hash=approval.token_hash,
            audience=approval.audience,
            at=NOW + timedelta(milliseconds=3),
        )
        self.claim = self.store.claim_next(
            "dispatcher",
            at=NOW + timedelta(seconds=1),
            lease_seconds=10,
        )
        assert self.claim is not None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_and_persists_exact_fresh_passed_attestation(self) -> None:
        venue, account, metadata = setup_account()
        at = NOW + timedelta(seconds=2)
        preflight = build_dispatch_preflight(
            command=self.store.get_command("command-1"),
            ticket=self.ticket,
            account=account,
            venue_account=venue,
            metadata=metadata,
            market=market(self.ticket, at=at),
            policy=RiskSizingPolicy(),
            at=at,
        )
        stored = self.store.register_preflight(
            preflight,
            at=at + timedelta(milliseconds=1),
        )

        self.assertTrue(stored.passed)
        self.assertEqual(stored.command_id, "command-1")
        self.assertEqual(stored.account_snapshot_hash, account.artifact_hash)
        self.assertEqual(stored.metadata_hash, metadata.source_hash)
        self.assertLessEqual(stored.expires_at - stored.observed_at, timedelta(seconds=5))
        self.assertEqual(stored, self.store.get_preflight("command-1"))

    def test_stale_risky_nonflat_or_uncrossable_state_is_denied(self) -> None:
        venue, account, metadata = setup_account()
        at = NOW + timedelta(seconds=2)
        positioned = positioned_account()
        unsafe_market = market(self.ticket, at=at)
        unsafe_market["book"] = deepcopy(unsafe_market["book"])
        unsafe_market["book"]["best_ask"] = str(  # type: ignore[index]
            self.ticket.plan.entry.price_bound + Decimal("1")
        )
        cases = (
            (
                account,
                venue,
                market(self.ticket, at=NOW),
                NOW + timedelta(seconds=6),
                "market_snapshot_stale",
            ),
            (account, positioned, market(self.ticket, at=at), at, "account_not_flat"),
            (account, venue, unsafe_market, at, "entry_bound_not_crossable"),
        )
        for selected_account, selected_venue, selected_market, selected_at, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(AdmissionDenied) as caught:
                    build_dispatch_preflight(
                        command=self.store.get_command("command-1"),
                        ticket=self.ticket,
                        account=selected_account,
                        venue_account=selected_venue,
                        metadata=metadata,
                        market=selected_market,
                        policy=RiskSizingPolicy(),
                        at=selected_at,
                    )
                self.assertIn(reason, caught.exception.message)

    def test_fresh_budget_drop_and_incomplete_depth_deny(self) -> None:
        venue, account, metadata = setup_account()
        at = NOW + timedelta(seconds=2)
        no_budget = type(account)(
            account_id=account.account_id,
            environment=account.environment,
            observed_at=account.observed_at,
            received_at=account.received_at,
            equity=account.equity,
            available_collateral=account.available_collateral,
            daily_loss_remaining=Decimal("0"),
            open_risk_remaining=Decimal("0"),
            max_notional=account.max_notional,
            lot_size=account.lot_size,
            leverage=account.leverage,
            artifact_hash="f" * 64,
        )
        incomplete = market(self.ticket, at=at)
        incomplete["book"]["depth"]["25bps"]["ask_complete"] = False  # type: ignore[index]
        for selected_account, selected_market, reason in (
            (no_budget, market(self.ticket, at=at), "fresh_risk_budget_exceeded"),
            (account, incomplete, "depth_completeness_unknown"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(AdmissionDenied) as caught:
                    build_dispatch_preflight(
                        command=self.store.get_command("command-1"),
                        ticket=self.ticket,
                        account=selected_account,
                        venue_account=venue,
                        metadata=metadata,
                        market=selected_market,
                        policy=RiskSizingPolicy(),
                        at=at,
                    )
                self.assertIn(reason, caught.exception.message)

    def test_ticket_requires_the_exact_risk_policy_hash(self) -> None:
        venue, account, metadata = setup_account()
        at = NOW + timedelta(seconds=2)
        changed = RiskSizingPolicy(
            version="different-policy",
            risk_fraction=Decimal("0.001"),
        )
        with self.assertRaises(AdmissionDenied) as caught:
            build_dispatch_preflight(
                command=self.store.get_command("command-1"),
                ticket=self.ticket,
                account=account,
                venue_account=venue,
                metadata=metadata,
                market=market(self.ticket, at=at),
                policy=changed,
                at=at,
            )
        self.assertIn("risk_policy_mismatch", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
