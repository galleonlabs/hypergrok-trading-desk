from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from trading_harness.executor import (
    DisabledVenueAdapter,
    Executor,
    VenueWriteDisabled,
    disabled_executor,
)


class DisabledVenueAdapterTests(unittest.TestCase):
    def test_rejects_every_write_operation(self) -> None:
        adapter = DisabledVenueAdapter()

        operations = (
            "place_order",
            "cancel_order",
            "amend_order",
            "cancel_all",
            "",
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    VenueWriteDisabled, "venue writes are disabled"
                ):
                    adapter.write(operation, {"private": "must-not-appear"})

    def test_error_does_not_echo_operation_or_payload(self) -> None:
        adapter = DisabledVenueAdapter()

        with self.assertRaises(VenueWriteDisabled) as caught:
            adapter.write("secret-operation", {"api_key": "secret-value"})

        message = str(caught.exception)
        self.assertNotIn("secret-operation", message)
        self.assertNotIn("secret-value", message)

    def test_status_is_explicitly_fail_closed(self) -> None:
        status = DisabledVenueAdapter().status

        self.assertEqual(status.adapter, "disabled")
        self.assertFalse(status.venue_writes_enabled)
        self.assertFalse(status.credential_loading_enabled)


class ExecutorTests(unittest.TestCase):
    def test_default_executor_rejects_writes(self) -> None:
        with self.assertRaises(VenueWriteDisabled):
            Executor().write("place_order", {"symbol": "ETH"})

    def test_default_executor_rejects_without_traversing_payload(self) -> None:
        class PoisonedPayload(dict[str, object]):
            def __iter__(self):  # type: ignore[no-untyped-def]
                raise AssertionError("disabled executor traversed payload")

            def keys(self):  # type: ignore[no-untyped-def]
                raise AssertionError("disabled executor traversed payload")

        with self.assertRaises(VenueWriteDisabled):
            Executor().write("place_order", PoisonedPayload())

    def test_foundation_executor_cannot_inject_an_enabled_adapter(self) -> None:
        class EnabledAdapter:
            @property
            def status(self):  # type: ignore[no-untyped-def]
                raise AssertionError("enabled adapter status was reached")

            def write(self, operation, payload):  # type: ignore[no-untyped-def]
                raise AssertionError("enabled adapter write was reached")

        with self.assertRaises(TypeError):
            Executor(EnabledAdapter())  # type: ignore[call-arg]

    def test_environment_variables_cannot_enable_default_executor(self) -> None:
        fake_credentials = {
            "VENUE_PRIVATE_KEY": "not-a-real-key",
            "TRADING_HARNESS_LIVE_TRADING": "true",
        }

        with patch.dict(os.environ, fake_credentials, clear=False):
            executor = disabled_executor()

        self.assertFalse(executor.status.venue_writes_enabled)
        self.assertFalse(executor.status.credential_loading_enabled)
        with self.assertRaises(VenueWriteDisabled):
            executor.write("place_order")


if __name__ == "__main__":
    unittest.main()
