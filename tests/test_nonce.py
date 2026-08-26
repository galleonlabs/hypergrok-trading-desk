from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.errors import StorageError, ValidationError
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.nonce import PersistentNonceAllocator


SIGNER = "0x" + "1" * 40
OTHER_SIGNER = "0x" + "2" * 40
NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


class PersistentNonceTests(unittest.TestCase):
    def test_existing_only_reopen_verifies_then_allows_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            before = path.read_bytes()
            query_only_values: list[int] = []
            verify_integrity = PersistentNonceAllocator._verify_integrity

            def observe_query_only(connection: sqlite3.Connection) -> None:
                query_only_values.append(
                    connection.execute("PRAGMA query_only").fetchone()[0]
                )
                verify_integrity(connection)

            with patch.object(
                PersistentNonceAllocator,
                "_verify_integrity",
                side_effect=observe_query_only,
            ):
                reopened = PersistentNonceAllocator(
                    path,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    clock=lambda: NOW,
                    must_exist=True,
                )

            self.assertEqual(query_only_values, [1])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(reopened.allocate(), int(NOW.timestamp() * 1000))

    def test_existing_only_rejects_unexpected_nonce_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TRIGGER executor_deployment_binding
                    AFTER INSERT ON hyperliquid_signer_nonces
                    BEGIN
                        UPDATE hyperliquid_signer_nonces
                        SET last_nonce = 0
                        WHERE signer_address = NEW.signer_address
                          AND network = NEW.network;
                    END
                    """
                )
            before = path.read_bytes()

            with self.assertRaisesRegex(StorageError, "deployment binding schema"):
                PersistentNonceAllocator(
                    path,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )

            self.assertEqual(before, path.read_bytes())

    def test_existing_only_rejects_zero_byte_and_schema_less_files_without_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            zero_byte = Path(directory) / "zero-byte.sqlite3"
            zero_byte.touch()
            before_zero = zero_byte.read_bytes()
            with self.assertRaises(StorageError):
                PersistentNonceAllocator(
                    zero_byte,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )
            self.assertEqual(zero_byte.read_bytes(), before_zero)

            schema_less = Path(directory) / "schema-less.sqlite3"
            with closing(sqlite3.connect(schema_less)) as connection, connection:
                connection.execute("PRAGMA journal_mode = WAL")
            before_schema_less = schema_less.read_bytes()
            with self.assertRaises(StorageError):
                PersistentNonceAllocator(
                    schema_less,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )
            self.assertEqual(schema_less.read_bytes(), before_schema_less)

    def test_existing_only_rejects_wrong_store_without_mutating_the_main_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-store.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("CREATE TABLE unrelated_state (value TEXT)")
            before = path.read_bytes()

            with self.assertRaises(StorageError):
                PersistentNonceAllocator(
                    path,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )

            self.assertEqual(path.read_bytes(), before)

    def test_existing_only_rejects_identity_and_hash_drift_without_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
            )
            before_identity_drift = path.read_bytes()
            with self.assertRaisesRegex(StorageError, "not bound"):
                PersistentNonceAllocator(
                    path,
                    signer_address=OTHER_SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )
            self.assertEqual(path.read_bytes(), before_identity_drift)

            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE hyperliquid_nonce_bindings SET binding_hash = ?",
                    ("0" * 64,),
                )
            before_hash_drift = path.read_bytes()
            with self.assertRaisesRegex(StorageError, "binding hash"):
                PersistentNonceAllocator(
                    path,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    must_exist=True,
                )
            self.assertEqual(path.read_bytes(), before_hash_drift)

    def test_existing_only_verification_reads_committed_crash_retained_wal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonce.sqlite3"
            allocator = PersistentNonceAllocator(
                path,
                signer_address=SIGNER,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: NOW,
            )
            keeper = sqlite3.connect(path)
            try:
                keeper.execute("PRAGMA wal_autocheckpoint = 0")
                keeper.execute("BEGIN")
                keeper.execute("SELECT * FROM hyperliquid_nonce_bindings").fetchall()
                allocated = allocator.allocate()
                wal = Path(f"{path}-wal")
                self.assertTrue(wal.is_file())
                self.assertGreater(wal.stat().st_size, 0)
                before = {
                    item.name: item.read_bytes()
                    for item in path.parent.iterdir()
                    if item.is_file()
                }

                with self.assertRaisesRegex(StorageError, "not bound"):
                    PersistentNonceAllocator(
                        path,
                        signer_address=OTHER_SIGNER,
                        network=HyperliquidNetwork.TESTNET,
                        clock=lambda: NOW,
                        must_exist=True,
                    )
                self.assertEqual(
                    before,
                    {
                        item.name: item.read_bytes()
                        for item in path.parent.iterdir()
                        if item.is_file()
                    },
                )

                reopened = PersistentNonceAllocator(
                    path,
                    signer_address=SIGNER,
                    network=HyperliquidNetwork.TESTNET,
                    clock=lambda: NOW,
                    must_exist=True,
                )
                self.assertEqual(
                    before,
                    {
                        item.name: item.read_bytes()
                        for item in path.parent.iterdir()
                        if item.is_file()
                    },
                )
                self.assertEqual(reopened.last_allocated(), allocated)
            finally:
                keeper.rollback()
                keeper.close()

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
