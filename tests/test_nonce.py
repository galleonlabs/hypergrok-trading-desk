from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trading_harness.errors import StorageError, ValidationError
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.nonce import PersistentNonceAllocator


SIGNER = "0x" + "1" * 40
NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


class PersistentNonceTests(unittest.TestCase):
    def test_restart_and_clock_rollback_remain_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            first = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            nonce_one = first.allocate()
            nonce_two = first.allocate()
            restarted = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW - timedelta(hours=1),
            )
            nonce_three = restarted.allocate()

            self.assertEqual(nonce_two, nonce_one + 1)
            self.assertEqual(nonce_three, nonce_two + 1)
            self.assertEqual(restarted.last_allocated(), nonce_three)

    def test_one_thousand_concurrent_allocations_are_unique_and_contiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            allocator = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            with ThreadPoolExecutor(max_workers=16) as pool:
                values = list(pool.map(lambda _index: allocator.allocate(), range(1000)))

            self.assertEqual(len(values), len(set(values)))
            self.assertEqual(max(values) - min(values), 999)
            self.assertEqual(allocator.last_allocated(), max(values))

    def test_nonce_state_is_scoped_by_signer_and_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            testnet = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            mainnet = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.MAINNET,
                clock=lambda: NOW,
            )

            self.assertEqual(testnet.allocate(), mainnet.allocate())
            self.assertEqual(testnet.allocate(), mainnet.allocate())

    def test_corrupt_far_future_state_fails_instead_of_issuing_bad_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            allocator = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            allocator.allocate()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE hyperliquid_signer_nonces SET last_nonce = ?",
                    (int(NOW.timestamp() * 1000) + 86_400_001,),
                )
                connection.commit()

            with self.assertRaisesRegex(StorageError, "far ahead"):
                allocator.allocate()

    def test_invalid_identity_clock_or_ephemeral_store_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            with self.assertRaisesRegex(ValidationError, "signer_address"):
                PersistentNonceAllocator(
                    path,
                    signer_address="0xNOT-AN-ADDRESS",
                    network=HyperliquidNetwork.TESTNET,
                )
            with self.assertRaisesRegex(ValidationError, "database path"):
                PersistentNonceAllocator(
                    ":memory:",
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                )
            allocator = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: datetime(2026, 8, 24, 16, 0),
            )
            with self.assertRaisesRegex(ValidationError, "timezone-aware"):
                allocator.allocate()


if __name__ == "__main__":
    unittest.main()
