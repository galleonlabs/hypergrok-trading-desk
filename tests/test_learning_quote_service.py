from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.daily_loss import DailyLossBinding, DailyLossLedger
from trading_harness.domain import Environment
from trading_harness.execution_grant import TrustedInfrastructureGrant
from trading_harness.executor_config import parse_executor_config
from trading_harness.hyperliquid_account import fetch_account_snapshot
from trading_harness.learning_quote_service import InfrastructureLearningQuoteService
from trading_harness.planning import RiskSizingPolicy
from trading_harness.research_api import ResearchService
from trading_harness.research_store import ResearchStore
from trading_harness.staging_inbox import TrustedQuoteRequest
from trading_harness.strategy import SignalDirection
from tests.test_account_risk import flat_clearing
from tests.test_hyperliquid_account import ACCOUNT, FixtureTransport
from tests.test_node import AT, history_reader
from tests.test_research_api import evidence, iso
from tests.test_registered_decision import SIGNAL


def config_text(root: Path, policy_hash: str) -> str:
    for name in ("execution", "nonce", "daily-loss", "learning", "socket"):
        directory = root / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "learning-executor"
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


class LearningQuoteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).absolute()
        self.research = ResearchStore(self.root / "research.sqlite3")
        self.research_service = ResearchService(
            self.research,
            clock=lambda: AT,
            history_reader=history_reader,
            analysis_bars=1001,
            validation_bars=1001,
        )
        self.research_service.track_asset(
            asset_id="eth",
            symbol="ETH",
            network="testnet",
            sentiment_query="$ETH OR Ethereum",
        )
        self.research_service.record_manual_sentiment(
            asset_id="eth",
            window_start=iso(AT - timedelta(hours=4)),
            window_end=iso(AT),
            evidence=evidence(),
            excluded_count=0,
            collection_complete=True,
        )
        self.analysis = self.research_service.analyze_asset("eth")
        self.policy = RiskSizingPolicy(
            version="learning-mechanics-v1",
            entry_slippage_bps=Decimal("0"),
            exit_slippage_bps=Decimal("0"),
            stop_gap_bps=Decimal("0"),
            round_trip_fee_bps=Decimal("0"),
        )
        self.config = parse_executor_config(
            config_text(self.root, self.policy.policy_hash),
            environ={},
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
        binding = DailyLossBinding(
            account_id=self.config.account_id,
            environment=Environment.TESTNET,
            config_hash=self.config.config_hash,
            daily_loss_limit=self.config.daily_loss_limit,
            settlement_currency="USDC",
        )
        self.loss = DailyLossLedger(
            self.root / "daily.sqlite3",
            binding=binding,
            clock=lambda: AT,
        )
        start = datetime.combine(AT.date(), datetime.min.time(), tzinfo=timezone.utc)
        for source in ("fills", "funding"):
            self.loss.record_coverage(
                coverage_id=f"{source}-coverage",
                source=source,
                covered_from=start,
                covered_through=AT,
                source_cursor_hash=("c" if source == "fills" else "d") * 64,
            )
        clearing = flat_clearing()
        clearing["time"] = int((AT - timedelta(milliseconds=500)).timestamp() * 1000)
        self.venue = fetch_account_snapshot(
            ACCOUNT,
            "testnet",
            transport=FixtureTransport(clearing=clearing, orders=[]),
            clock=lambda: AT,
        )

    def service(self, reader=None) -> InfrastructureLearningQuoteService:
        return InfrastructureLearningQuoteService(
            self.research,
            config=self.config,
            policy=self.policy,
            grant=self.grant,
            account_reader=(lambda _address, _network: self.venue) if reader is None else reader,
            clock=lambda: AT,
        )

    def test_directional_manual_analysis_stages_bounded_non_profitability_ticket(self) -> None:
        decision = self.service()(
            TrustedQuoteRequest("eth", self.analysis["analysis_hash"])
        )

        self.assertEqual("staged", decision.decision.value)
        ticket = decision.ticket_payload
        assert ticket is not None
        self.assertEqual("infrastructure_learning", ticket["purpose"])
        self.assertFalse(ticket["profitability_qualified"])
        self.assertFalse(ticket["mainnet_authorized"])
        self.assertFalse(ticket["grant_authentication_deferred_to_control"])
        self.assertTrue(ticket["daily_loss_deferred_to_executor"])
        self.assertTrue(ticket["manual_sentiment_confirmation_required"])
        plan = ticket["risk_ticket"]["plan"]
        self.assertEqual("normalTpsl", plan["grouping"])
        self.assertTrue(plan["stop_mandatory"])
        self.assertTrue(plan["protective_stop"]["reduce_only"])

    def test_nothing_short_circuits_before_account_read(self) -> None:
        nothing = replace(
            SIGNAL,
            direction=SignalDirection.NOTHING,
            reason="no_donchian_transition",
        )
        with patch("trading_harness.research_api.latest_signal", return_value=nothing):
            analysis = self.research_service.analyze_asset("eth")
        calls: list[object] = []
        decision = self.service(
            reader=lambda *args: calls.append(args),
        )(TrustedQuoteRequest("eth", analysis["analysis_hash"]))

        self.assertEqual("blocked", decision.decision.value)
        self.assertEqual("nothing_to_trade", decision.block_code)
        self.assertEqual([], calls)

    def test_agent_quote_defers_daily_loss_to_executor_preflight(self) -> None:
        calls: list[object] = []
        service = InfrastructureLearningQuoteService(
            self.research,
            config=self.config,
            policy=self.policy,
            grant=self.grant,
            account_reader=lambda *args: calls.append(args) or self.venue,
            clock=lambda: AT,
        )
        decision = service(TrustedQuoteRequest("eth", self.analysis["analysis_hash"]))
        self.assertEqual("staged", decision.decision.value)
        assert decision.ticket_payload is not None
        self.assertTrue(
            decision.ticket_payload["daily_loss_deferred_to_executor"]
        )
        self.assertEqual([(ACCOUNT, "testnet")], calls)


if __name__ == "__main__":
    unittest.main()
