from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.daily_loss import (
    DailyLossBinding,
    DailyLossLedger,
    IncompleteDailyLossCoverage,
)
from trading_harness.domain import Environment
from trading_harness.executor_config import (
    EXECUTOR_CONFIG_SCHEMA_VERSION,
    ExecutorConfig,
    ExecutorCredentialConfig,
    ExecutorPaths,
)
from trading_harness.errors import StateConflict
from trading_harness.hyperliquid_loss_sync import (
    TESTNET_INFO_ENDPOINT,
    HyperliquidDailyLossSynchronizer,
    HyperliquidLossSyncResponseError,
    HyperliquidLossSyncTransportError,
)


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
DAY_START = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
DAY_START_MS = int(DAY_START.timestamp() * 1_000)
NOW_MS = int(NOW.timestamp() * 1_000)
MAIN_ACCOUNT = "0x" + "1" * 40
API_WALLET = "0x" + "2" * 40


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class FixedClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def raw_fill(
    *,
    tid: int = 1,
    time_ms: int = DAY_START_MS + 1_000,
    closed_pnl: str = "-3.25",
    fee: str = "0.125",
    tx_digit: str = "a",
) -> dict[str, object]:
    return {
        "closedPnl": closed_pnl,
        "coin": "ETH",
        "crossed": True,
        "dir": "Close Long",
        "hash": "0x" + tx_digit * 64,
        "oid": 100 + tid,
        "px": "2500.50",
        "side": "A",
        "startPosition": "1",
        "sz": "0.25",
        "time": time_ms,
        "fee": fee,
        "feeToken": "USDC",
        "tid": tid,
    }


def raw_funding(
    *,
    coin: str = "ETH",
    time_ms: int = DAY_START_MS + 3_600_000,
    usdc: str = "-0.75",
    tx_digit: str = "b",
) -> dict[str, object]:
    return {
        "time": time_ms,
        "hash": "0x" + tx_digit * 64,
        "delta": {
            "type": "funding",
            "coin": coin,
            "fundingRate": "0.0000125",
            "szi": "0.25",
            "usdc": usdc,
            "nSamples": 60,
        },
    }


class FixtureTransport:
    def __init__(
        self,
        *,
        fills: list[object] | None = None,
        funding: list[object] | None = None,
    ) -> None:
        self.fills = [] if fills is None else fills
        self.funding = [] if funding is None else funding
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload: object) -> object:
        assert isinstance(payload, dict)
        self.calls.append((endpoint, deepcopy(payload)))
        if payload["type"] == "userFillsByTime":
            return deepcopy(self.fills)
        if payload["type"] == "userFunding":
            return deepcopy(self.funding)
        raise AssertionError("unexpected info request")


class CursorTransport:
    def __init__(
        self,
        *,
        fill_pages: dict[int, list[object]] | None = None,
        funding_pages: dict[int, list[object]] | None = None,
    ) -> None:
        self.fill_pages = {} if fill_pages is None else fill_pages
        self.funding_pages = {} if funding_pages is None else funding_pages
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload: object) -> object:
        assert isinstance(payload, dict)
        self.calls.append((endpoint, deepcopy(payload)))
        cursor = payload["startTime"]
        assert isinstance(cursor, int)
        pages = (
            self.fill_pages
            if payload["type"] == "userFillsByTime"
            else self.funding_pages
        )
        return deepcopy(pages.get(cursor, []))


class HyperliquidDailyLossSynchronizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).absolute()
        state_parents = {
            name: root / name
            for name in ("execution", "nonce", "daily-loss", "learning", "socket")
        }
        for parent in state_parents.values():
            parent.mkdir(mode=0o700)
        self.config = ExecutorConfig(
            schema_version=EXECUTOR_CONFIG_SCHEMA_VERSION,
            environment=Environment.TESTNET,
            venue="hyperliquid",
            node_id="loss-sync-node",
            executor_uid=os.geteuid(),
            research_uid=os.geteuid() + 1,
            control_uid=os.geteuid() + 2,
            account_id="dedicated-testnet",
            main_account_address=MAIN_ACCOUNT,
            api_wallet_address=API_WALLET,
            daily_loss_limit="25",  # type: ignore[arg-type]
            max_reserved_loss="5",  # type: ignore[arg-type]
            max_reserved_notional="1000",  # type: ignore[arg-type]
            max_leverage="2",  # type: ignore[arg-type]
            risk_policy_hash=digest("risk-policy"),
            allowed_instruments=("ETH-PERP",),
            allowed_asset_ids=(1,),
            recovery_cloids=("0x" + "e" * 32,),
            settlement_currency="USDC",
            poll_interval_ms=1_000,
            reconcile_interval_ms=2_000,
            credential=ExecutorCredentialConfig(
                provider="macos_keychain_generic_password",
                service="hypergrok-testnet",
                account="api-wallet",
                timeout_seconds=3,
            ),
            approval_credential=ExecutorCredentialConfig(
                provider="macos_keychain_generic_password",
                service="hypergrok-testnet-approval",
                account="approval-hmac",
                timeout_seconds=3,
            ),
            recovery_credential=ExecutorCredentialConfig(
                provider="macos_keychain_generic_password",
                service="hypergrok-testnet-recovery",
                account="recovery-hmac",
                timeout_seconds=3,
            ),
            grant_credential=ExecutorCredentialConfig(
                provider="macos_keychain_generic_password",
                service="hypergrok-testnet-grant",
                account="grant-hmac",
                timeout_seconds=3,
            ),
            paths=ExecutorPaths(
                execution_database=state_parents["execution"] / "execution.sqlite3",
                nonce_database=state_parents["nonce"] / "nonce.sqlite3",
                daily_loss_database=state_parents["daily-loss"] / "daily-loss.sqlite3",
                learning_database=state_parents["learning"] / "learning.sqlite3",
                staging_database=state_parents["learning"] / "staging.sqlite3",
                control_socket=state_parents["socket"] / "executor.sock",
            ),
        )
        self.clock = FixedClock()
        self.ledger = DailyLossLedger(
            self.config.paths.daily_loss_database,
            binding=DailyLossBinding(
                account_id=self.config.account_id,
                environment=self.config.environment,
                config_hash=self.config.config_hash,
                daily_loss_limit=self.config.daily_loss_limit,
                settlement_currency=self.config.settlement_currency,
            ),
            clock=self.clock,
        )

    def synchronizer(self, transport: object) -> HyperliquidDailyLossSynchronizer:
        return HyperliquidDailyLossSynchronizer(
            environment=self.config.environment,
            account_id=self.config.account_id,
            main_account_address=self.config.main_account_address,
            config_hash=self.config.config_hash,
            settlement_currency=self.config.settlement_currency,
            ledger=self.ledger,
            transport=transport,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def test_complete_read_records_exact_losses_and_coverage(self) -> None:
        transport = FixtureTransport(fills=[raw_fill()], funding=[raw_funding()])
        result = self.synchronizer(transport).synchronize()

        self.assertTrue(result.complete)
        self.assertTrue(result.fills.coverage_inserted)
        self.assertTrue(result.funding.coverage_inserted)
        self.assertEqual(result.fills.inserted_events, 2)
        self.assertEqual(result.funding.inserted_events, 1)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.realized_loss_debit, Decimal("3.25"))
        self.assertEqual(snapshot.fee_debit, Decimal("0.125"))
        self.assertEqual(snapshot.funding_debit, Decimal("0.75"))
        self.assertEqual(snapshot.used, Decimal("4.125"))

        self.assertEqual(
            {call[1]["type"] for call in transport.calls},
            {"userFillsByTime", "userFunding"},
        )
        for endpoint, payload in transport.calls:
            self.assertEqual(endpoint, TESTNET_INFO_ENDPOINT)
            self.assertEqual(payload["user"], MAIN_ACCOUNT)
            self.assertEqual(payload["startTime"], DAY_START_MS)
            self.assertEqual(payload["endTime"], NOW_MS)
            self.assertNotIn("api_wallet", payload)
            self.assertNotIn("private_key", payload)

    def test_repeating_exact_sync_is_idempotent(self) -> None:
        transport = FixtureTransport(fills=[raw_fill()], funding=[raw_funding()])
        first = self.synchronizer(transport).synchronize()
        second = self.synchronizer(transport).synchronize()

        self.assertEqual(first.fills.cursor_hash, second.fills.cursor_hash)
        self.assertEqual(first.funding.cursor_hash, second.funding.cursor_hash)
        self.assertEqual(second.fills.inserted_events, 0)
        self.assertEqual(second.fills.existing_events, 2)
        self.assertEqual(second.funding.inserted_events, 0)
        self.assertEqual(second.funding.existing_events, 1)
        self.assertFalse(second.fills.coverage_inserted)
        self.assertFalse(second.funding.coverage_inserted)
        self.assertEqual(self.ledger.snapshot().event_count, 3)

    def test_inclusive_pagination_deduplicates_boundary_without_skipping(self) -> None:
        t1 = DAY_START_MS + 1_000
        t2 = DAY_START_MS + 2_000
        t3 = DAY_START_MS + 3_000
        pages = {
            DAY_START_MS: [raw_fill(tid=1, time_ms=t1), raw_fill(tid=2, time_ms=t2)],
            t2: [raw_fill(tid=2, time_ms=t2), raw_fill(tid=3, time_ms=t3)],
            t3: [raw_fill(tid=3, time_ms=t3)],
        }
        transport = CursorTransport(fill_pages=pages)
        with patch(
            "trading_harness.hyperliquid_loss_sync.USER_FILLS_PAGE_LIMIT", 2
        ):
            result = self.synchronizer(transport).synchronize()

        self.assertTrue(result.fills.complete)
        self.assertEqual(result.fills.page_count, 3)
        self.assertEqual(result.fills.returned_rows, 5)
        self.assertEqual(result.fills.unique_rows, 3)
        self.assertEqual(result.fills.duplicate_rows, 2)
        self.assertEqual(result.fills.inserted_events, 6)
        fill_cursors = [
            call[1]["startTime"]
            for call in transport.calls
            if call[1]["type"] == "userFillsByTime"
        ]
        self.assertEqual(fill_cursors, [DAY_START_MS, t2, t3])

    def test_missing_inclusive_page_overlap_cannot_claim_contiguous_coverage(self) -> None:
        t1 = DAY_START_MS + 1_000
        t2 = DAY_START_MS + 2_000
        t3 = DAY_START_MS + 3_000
        t4 = DAY_START_MS + 4_000
        pages = {
            DAY_START_MS: [raw_fill(tid=1, time_ms=t1), raw_fill(tid=2, time_ms=t2)],
            # The documented inclusive cursor must return tid=2 again.  Its
            # absence means this pagination chain cannot prove continuity.
            t2: [
                raw_fill(tid=3, time_ms=t3, tx_digit="c"),
                raw_fill(tid=4, time_ms=t4, tx_digit="d"),
            ],
        }
        with (
            patch("trading_harness.hyperliquid_loss_sync.USER_FILLS_PAGE_LIMIT", 2),
            self.assertRaisesRegex(
                HyperliquidLossSyncResponseError, "page overlap is incomplete"
            ),
        ):
            self.synchronizer(CursorTransport(fill_pages=pages)).synchronize()
        self.assertEqual(self.ledger.snapshot(require_complete=False).event_count, 0)

    def test_full_inclusive_boundary_is_incomplete_and_never_gets_coverage(self) -> None:
        rows = [
            raw_fill(tid=1, time_ms=DAY_START_MS),
            raw_fill(tid=2, time_ms=DAY_START_MS, tx_digit="c"),
        ]
        transport = CursorTransport(fill_pages={DAY_START_MS: rows})
        with patch(
            "trading_harness.hyperliquid_loss_sync.USER_FILLS_PAGE_LIMIT", 2
        ):
            result = self.synchronizer(transport).synchronize()

        self.assertFalse(result.complete)
        self.assertFalse(result.fills.complete)
        self.assertEqual(
            result.fills.incomplete_reason, "inclusive_fill_boundary_saturated"
        )
        self.assertFalse(result.fills.coverage_inserted)
        with self.assertRaises(IncompleteDailyLossCoverage):
            self.ledger.snapshot()

    def test_fill_retention_ceiling_is_incomplete_even_at_exact_cap(self) -> None:
        rows = [
            raw_fill(tid=1, time_ms=DAY_START_MS + 1_000),
            raw_fill(tid=2, time_ms=DAY_START_MS + 2_000, tx_digit="c"),
        ]
        transport = CursorTransport(fill_pages={DAY_START_MS: rows})
        with (
            patch("trading_harness.hyperliquid_loss_sync.USER_FILLS_PAGE_LIMIT", 2),
            patch("trading_harness.hyperliquid_loss_sync.USER_FILLS_RETENTION_LIMIT", 2),
        ):
            result = self.synchronizer(transport).synchronize()

        self.assertFalse(result.fills.complete)
        self.assertEqual(
            result.fills.incomplete_reason, "latest_10000_fill_retention_limit"
        )
        self.assertFalse(result.fills.coverage_inserted)

    def test_short_final_page_cannot_override_fill_retention_ceiling(self) -> None:
        t1 = DAY_START_MS + 1_000
        t2 = DAY_START_MS + 2_000
        t3 = DAY_START_MS + 3_000
        t4 = DAY_START_MS + 4_000
        pages = {
            DAY_START_MS: [
                raw_fill(tid=1, time_ms=t1),
                raw_fill(tid=2, time_ms=t2, tx_digit="c"),
                raw_fill(tid=3, time_ms=t3, tx_digit="d"),
            ],
            t3: [
                raw_fill(tid=3, time_ms=t3, tx_digit="d"),
                raw_fill(tid=4, time_ms=t4, tx_digit="e"),
            ],
        }
        with (
            patch("trading_harness.hyperliquid_loss_sync.USER_FILLS_PAGE_LIMIT", 3),
            patch("trading_harness.hyperliquid_loss_sync.USER_FILLS_RETENTION_LIMIT", 4),
        ):
            result = self.synchronizer(
                CursorTransport(fill_pages=pages)
            ).synchronize()

        self.assertFalse(result.fills.complete)
        self.assertEqual(
            result.fills.incomplete_reason, "latest_10000_fill_retention_limit"
        )
        self.assertFalse(result.fills.coverage_inserted)

    def test_funding_page_budget_exhaustion_is_incomplete(self) -> None:
        t1 = DAY_START_MS + 1_000
        t2 = DAY_START_MS + 2_000
        funding = [
            raw_funding(coin="ETH", time_ms=t1),
            raw_funding(coin="BTC", time_ms=t2, tx_digit="c"),
        ]
        transport = CursorTransport(funding_pages={DAY_START_MS: funding})
        with (
            patch("trading_harness.hyperliquid_loss_sync.USER_FUNDING_PAGE_LIMIT", 2),
            patch("trading_harness.hyperliquid_loss_sync.MAX_FUNDING_PAGES", 1),
        ):
            result = self.synchronizer(transport).synchronize()

        self.assertTrue(result.fills.complete)
        self.assertFalse(result.funding.complete)
        self.assertEqual(
            result.funding.incomplete_reason, "maximum_funding_pages_exhausted"
        )
        self.assertFalse(result.funding.coverage_inserted)

    def test_funding_inclusive_pagination_is_exact_and_idempotent(self) -> None:
        t1 = DAY_START_MS + 1_000
        t2 = DAY_START_MS + 2_000
        t3 = DAY_START_MS + 3_000
        pages = {
            DAY_START_MS: [
                raw_funding(coin="ETH", time_ms=t1, tx_digit="b"),
                raw_funding(coin="BTC", time_ms=t2, tx_digit="c"),
            ],
            t2: [
                raw_funding(coin="BTC", time_ms=t2, tx_digit="c"),
                raw_funding(coin="SOL", time_ms=t3, tx_digit="d"),
            ],
            t3: [raw_funding(coin="SOL", time_ms=t3, tx_digit="d")],
        }
        with patch(
            "trading_harness.hyperliquid_loss_sync.USER_FUNDING_PAGE_LIMIT", 2
        ):
            result = self.synchronizer(
                CursorTransport(funding_pages=pages)
            ).synchronize()

        self.assertTrue(result.funding.complete)
        self.assertEqual(result.funding.page_count, 3)
        self.assertEqual(result.funding.unique_rows, 3)
        self.assertEqual(result.funding.duplicate_rows, 2)
        self.assertEqual(result.funding.inserted_events, 3)
        self.assertTrue(result.funding.coverage_inserted)

    def test_exact_duplicate_rows_are_deduplicated_but_conflicts_fail_closed(self) -> None:
        exact = raw_fill()
        exact_transport = FixtureTransport(fills=[exact, deepcopy(exact)])
        exact_result = self.synchronizer(exact_transport).synchronize()
        self.assertEqual(exact_result.fills.unique_rows, 1)
        self.assertEqual(exact_result.fills.duplicate_rows, 1)

        # The venue identity remains stable across runs.  Changed economics
        # must therefore conflict with the immutable ledger rather than being
        # accepted under a newly derived event id.
        with self.assertRaises(StateConflict):
            self.synchronizer(
                FixtureTransport(fills=[raw_fill(closed_pnl="-8")])
            ).synchronize()

        other_root = Path(self.temporary.name).absolute() / "other"
        other_root.mkdir()
        other_database = other_root / "daily-loss.sqlite3"
        other_ledger = DailyLossLedger(
            other_database,
            binding=DailyLossBinding(
                account_id=self.config.account_id,
                environment=self.config.environment,
                config_hash=self.config.config_hash,
                daily_loss_limit=self.config.daily_loss_limit,
                settlement_currency=self.config.settlement_currency,
            ),
            clock=self.clock,
        )
        conflicting = raw_fill(closed_pnl="-9")
        with self.assertRaisesRegex(
            HyperliquidLossSyncResponseError, "conflicting economics"
        ):
            HyperliquidDailyLossSynchronizer(
                environment=self.config.environment,
                account_id=self.config.account_id,
                main_account_address=self.config.main_account_address,
                config_hash=self.config.config_hash,
                settlement_currency=self.config.settlement_currency,
                ledger=other_ledger,
                transport=FixtureTransport(fills=[exact, conflicting]),
                clock=self.clock,
            ).synchronize()

    def test_funding_duplicate_identity_conflict_fails_before_any_write(self) -> None:
        first = raw_funding(usdc="-1")
        second = raw_funding(usdc="-2")
        transport = FixtureTransport(fills=[raw_fill()], funding=[first, second])
        with self.assertRaisesRegex(
            HyperliquidLossSyncResponseError, "conflicting economics"
        ):
            self.synchronizer(transport).synchronize()
        self.assertEqual(self.ledger.snapshot(require_complete=False).event_count, 0)

    def test_decimal_json_numbers_unknown_fields_and_out_of_order_rows_are_rejected(self) -> None:
        numeric = raw_fill()
        numeric["fee"] = 0.1
        with self.assertRaisesRegex(
            HyperliquidLossSyncResponseError, "exact decimal string"
        ):
            self.synchronizer(FixtureTransport(fills=[numeric])).synchronize()

        unknown = raw_funding()
        assert isinstance(unknown["delta"], dict)
        unknown["delta"]["surprise"] = "unsafe"
        with self.assertRaisesRegex(
            HyperliquidLossSyncResponseError, "fields are unsupported"
        ):
            self.synchronizer(FixtureTransport(funding=[unknown])).synchronize()

        later = raw_fill(tid=1, time_ms=DAY_START_MS + 2_000)
        earlier = raw_fill(tid=2, time_ms=DAY_START_MS + 1_000, tx_digit="c")
        with self.assertRaisesRegex(HyperliquidLossSyncResponseError, "not ordered"):
            self.synchronizer(FixtureTransport(fills=[later, earlier])).synchronize()

    def test_wrong_currency_and_records_outside_requested_window_are_rejected(self) -> None:
        currency = raw_fill()
        currency["feeToken"] = "HYPE"
        with self.assertRaisesRegex(
            HyperliquidLossSyncResponseError, "settlement currency"
        ):
            self.synchronizer(FixtureTransport(fills=[currency])).synchronize()

        old = raw_funding(time_ms=DAY_START_MS - 1)
        with self.assertRaisesRegex(HyperliquidLossSyncResponseError, "outside its request"):
            self.synchronizer(FixtureTransport(funding=[old])).synchronize()

    def test_mainnet_configuration_is_refused_before_transport(self) -> None:
        object.__setattr__(self.config, "environment", Environment.MAINNET)
        transport = FixtureTransport()
        with self.assertRaisesRegex(ValueError, "TESTNET-only"):
            HyperliquidDailyLossSynchronizer(
                environment=self.config.environment,
                account_id=self.config.account_id,
                main_account_address=self.config.main_account_address,
                config_hash=self.config.config_hash,
                settlement_currency=self.config.settlement_currency,
                ledger=self.ledger,
                transport=transport,
                clock=self.clock,
            )
        self.assertEqual(transport.calls, [])

    def test_transport_failures_are_sanitized_and_write_no_coverage(self) -> None:
        def failed(_endpoint: str, _payload: object) -> object:
            raise RuntimeError("secret response body")

        with self.assertRaises(HyperliquidLossSyncTransportError) as raised:
            self.synchronizer(failed).synchronize()
        self.assertNotIn("secret response body", str(raised.exception))
        snapshot = self.ledger.snapshot(require_complete=False)
        self.assertFalse(snapshot.coverage_complete)
        self.assertEqual(snapshot.event_count, 0)


if __name__ == "__main__":
    unittest.main()
