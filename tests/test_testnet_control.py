from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import tempfile
import unittest

from trading_harness.approval import (
    TestnetApprovalAuthority,
    verified_execution_approval,
)
from trading_harness.daily_loss import DailyLossBinding, DailyLossLedger
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_grant import TrustedInfrastructureGrant
from trading_harness.execution_store import ExecutionStore
from trading_harness.executor_config import parse_executor_config
from trading_harness.hyperliquid_account import fetch_account_snapshot
from trading_harness.learning_bridge import LearningRecorder
from trading_harness.learning_ledger import LearningLedger
from trading_harness.learning_quote_service import InfrastructureLearningQuoteService
from trading_harness.planning import RiskSizingPolicy, risk_ticket_from_dict
from trading_harness.research_api import ResearchService
from trading_harness.research_store import ResearchStore
from trading_harness.staging_inbox import TradeStagingInbox
from trading_harness.testnet_control import AttendedTestnetControlPlane
from tests.test_account_risk import flat_clearing
from tests.test_hyperliquid_account import ACCOUNT, FixtureTransport
from tests.test_node import AT, history_reader
from tests.test_research_api import evidence, iso


def config_text(root: Path, policy_hash: str) -> str:
    for name in ("execution", "nonce", "daily-loss", "learning", "socket"):
        directory = root / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "attended-control"
executor_uid = {os.geteuid()}
research_uid = {os.geteuid() + 1}
control_uid = {os.geteuid() + 2}
account_id = "learning-account"
main_account_address = "{ACCOUNT}"
api_wallet_address = "0x2222222222222222222222222222222222222222"
daily_loss_limit = "25"
max_reserved_loss = "25"
max_reserved_notional = "1000"
max_leverage = "2"
risk_policy_hash = "{policy_hash}"
allowed_instruments = ["ETH-PERP"]
allowed_asset_ids = [1]
recovery_cloids = ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]
settlement_currency = "USDC"
poll_interval_ms = 1000
reconcile_interval_ms = 5000

[credential]
provider = "macos_keychain_generic_password"
service = "testnet-signer"
account = "api-wallet"
timeout_seconds = 5

[approval_credential]
provider = "macos_keychain_generic_password"
service = "testnet-approval"
account = "approval-hmac"
timeout_seconds = 5

[recovery_credential]
provider = "macos_keychain_generic_password"
service = "testnet-recovery"
account = "recovery-hmac"
timeout_seconds = 5

[grant_credential]
provider = "macos_keychain_generic_password"
service = "testnet-grant"
account = "grant-hmac"
timeout_seconds = 5

[paths]
execution_database = "{root / 'execution' / 'execution.sqlite3'}"
nonce_database = "{root / 'nonce' / 'nonce.sqlite3'}"
daily_loss_database = "{root / 'daily-loss' / 'daily-loss.sqlite3'}"
learning_database = "{root / 'learning' / 'learning.sqlite3'}"
staging_database = "{root / 'learning' / 'staging.sqlite3'}"
control_socket = "{root / 'socket' / 'executor.sock'}"
'''


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class AttendedTestnetControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.clock = MutableClock(AT)
        self.policy = RiskSizingPolicy(
            version="learning-mechanics-v1",
            entry_slippage_bps=Decimal("0"),
            exit_slippage_bps=Decimal("0"),
            stop_gap_bps=Decimal("0"),
            round_trip_fee_bps=Decimal("0"),
        )
        self.config = parse_executor_config(
            config_text(self.root, self.policy.policy_hash), environ={}
        )
        self.grant = TrustedInfrastructureGrant(
            grant_hash="b" * 64,
            grant_id="learning-grant",
            generation=1,
            account_id=self.config.account_id,
            environment=Environment.TESTNET,
            allowed_instruments=("ETH-PERP",),
            risk_policy_hash=self.policy.policy_hash,
            max_loss=Decimal("25"),
            max_notional=Decimal("1000"),
            max_leverage=Decimal("2"),
            issuer_id="test-authority",
            audience="test-executor",
            issued_at=AT - timedelta(minutes=1),
            not_before=AT - timedelta(minutes=1),
            expires_at=AT + timedelta(hours=1),
        )
        self.research = ResearchStore(self.root / "research.sqlite3")
        service = ResearchService(
            self.research,
            clock=self.clock,
            history_reader=history_reader,
            analysis_bars=1001,
            validation_bars=1001,
        )
        service.track_asset(
            asset_id="eth",
            symbol="ETH",
            network="testnet",
            sentiment_query="$ETH OR Ethereum",
        )
        service.record_manual_sentiment(
            asset_id="eth",
            window_start=iso(AT - timedelta(hours=4)),
            window_end=iso(AT),
            evidence=evidence(),
            excluded_count=0,
            collection_complete=True,
        )
        self.analysis = service.analyze_asset("eth")
        binding = DailyLossBinding(
            account_id=self.config.account_id,
            environment=Environment.TESTNET,
            config_hash=self.config.config_hash,
            daily_loss_limit=self.config.daily_loss_limit,
            settlement_currency="USDC",
        )
        self.loss = DailyLossLedger(
            self.config.paths.daily_loss_database,
            binding=binding,
            clock=self.clock,
        )
        start = datetime.combine(AT.date(), datetime.min.time(), tzinfo=timezone.utc)
        for source, digest in (("fills", "c" * 64), ("funding", "d" * 64)):
            self.loss.record_coverage(
                coverage_id=f"{source}-coverage",
                source=source,
                covered_from=start,
                covered_through=AT,
                source_cursor_hash=digest,
            )
        clearing = flat_clearing()
        clearing["time"] = int((AT - timedelta(milliseconds=500)).timestamp() * 1000)
        venue = fetch_account_snapshot(
            ACCOUNT,
            "testnet",
            transport=FixtureTransport(clearing=clearing, orders=[]),
            clock=self.clock,
        )
        quote = InfrastructureLearningQuoteService(
            self.research,
            config=self.config,
            policy=self.policy,
            grant=self.grant,
            account_reader=lambda _address, _network: venue,
            clock=self.clock,
        )
        self.inbox = TradeStagingInbox(
            self.config.paths.staging_database,
            quote_callback=quote,
            clock=self.clock,
        )
        self.view = self.inbox.stage(
            {
                "asset_id": "eth",
                "expected_analysis_hash": self.analysis["analysis_hash"],
                "idempotency_key": "authorize-learning-eth-0001",
            }
        )
        self.store = ExecutionStore(
            self.config.paths.execution_database,
            environment=Environment.TESTNET,
            account_id=self.config.account_id,
            max_reserved_loss=self.config.max_reserved_loss,
            max_reserved_notional=self.config.max_reserved_notional,
        )
        self.authority = TestnetApprovalAuthority(
            b"a" * 32,
            key_id="testnet-approval-key",
            audience="isolated-testnet-executor",
        )
        self.learning = LearningRecorder(
            LearningLedger(self.config.paths.learning_database, clock=self.clock)
        )
        self.control = AttendedTestnetControlPlane(
            self.inbox,
            self.store,
            config=self.config,
            grant=self.grant,
            approval_authority=self.authority,
            learning_recorder=self.learning,
            clock=self.clock,
        )

    def confirmation(self) -> str:
        payload = self.view.document.ticket_payload
        assert payload is not None
        ticket = risk_ticket_from_dict(payload["risk_ticket"])
        return self.control.confirmation_for(ticket)

    def test_exact_confirmation_queues_one_protected_command_and_learning_refs(self) -> None:
        result = self.control.authorize_stage(
            self.view.document.document_id,
            confirmation=self.confirmation(),
            approver_id="local-operator",
        )

        self.assertEqual("queued", result.command_state)
        self.assertEqual(1, len(self.store.list_commands()))
        legs = self.store.get_legs(result.command_id)
        self.assertEqual(
            {"entry", "protective_stop", "take_profit"},
            {item.role for item in legs},
        )
        public = result.as_dict()
        self.assertFalse(public["order_submitted"])
        self.assertFalse(public["venue_write_attempted"])
        self.assertTrue(public["stop_mandatory"])
        self.assertFalse(public["profitability_qualified"])
        events = self.learning.ledger.events(cycle_id=result.learning_cycle_id)
        self.assertEqual(
            ["decision_cycle", "approval_reference", "execution_reference"],
            [event.event_type for event in events],
        )

    def test_repeat_is_idempotent_and_does_not_create_second_authority(self) -> None:
        first = self.control.authorize_stage(
            self.view.document.document_id,
            confirmation=self.confirmation(),
            approver_id="local-operator",
        )
        second = self.control.authorize_stage(
            self.view.document.document_id,
            confirmation=self.confirmation(),
            approver_id="local-operator",
        )

        self.assertEqual(first.command_id, second.command_id)
        self.assertEqual(1, len(self.store.list_commands()))
        self.assertEqual(3, len(self.learning.ledger.events(cycle_id=first.learning_cycle_id)))

    def test_wrong_confirmation_fails_before_capital_store_mutation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "confirmation"):
            self.control.authorize_stage(
                self.view.document.document_id,
                confirmation="approve something else",
                approver_id="local-operator",
            )
        self.assertEqual((), self.store.list_commands())
        self.assertEqual((), self.store.list_events())

    def test_expired_stage_and_grant_are_rejected(self) -> None:
        self.clock.value = self.view.document.expires_at
        with self.assertRaises(StateConflict):
            self.control.authorize_stage(
                self.view.document.document_id,
                confirmation=self.confirmation(),
                approver_id="local-operator",
            )
        self.assertEqual((), self.store.list_commands())

    def test_recovers_registered_approval_after_pre_admission_crash(self) -> None:
        payload = self.view.document.ticket_payload
        assert payload is not None
        ticket = risk_ticket_from_dict(payload["risk_ticket"])
        self.store.register_infrastructure_grant(self.grant, at=AT)
        self.store.register_ticket(
            ticket,
            infrastructure_grant_hash=self.grant.grant_hash,
            stored_at=AT,
        )
        approval_id, expected_command = self.control._identities(
            self.view.document.document_hash, ticket.ticket_hash
        )
        raw = self.authority.issue(
            ticket,
            approval_id=approval_id,
            approver_id="local-operator",
            confirmation=self.confirmation(),
            at=AT,
        )
        trusted = verified_execution_approval(
            self.authority, raw, ticket, at=AT
        )
        self.store.register_approval(trusted)

        result = self.control.authorize_stage(
            self.view.document.document_id,
            confirmation=self.confirmation(),
            approver_id="local-operator",
        )
        self.assertEqual(expected_command, result.command_id)
        self.assertEqual("consumed", self.store.approval_state(approval_id))


if __name__ == "__main__":
    unittest.main()
