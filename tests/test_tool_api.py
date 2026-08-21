from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from trading_harness.canonical import semantic_intent_hash
from trading_harness.domain import SemanticIntent
from trading_harness.tool_api import (
    TOOL_CATALOG,
    ToolInputError,
    ToolService,
    tool_catalog,
)


def valid_intent() -> dict[str, object]:
    return {
        "intent_id": "intent-001",
        "thesis_id": "thesis-001",
        "thesis_version": "1",
        "strategy_version": "1",
        "code_hash": "a" * 64,
        "venue": "hyperliquid",
        "account_id": "private-account-id",
        "environment": "shadow",
        "instrument": "ETH-PERP",
        "action": "place_order",
        "side": "buy",
        "quantity": "0.10",
        "order_type": "limit",
        "limit_price": "3000.00",
        "expires_at": "2026-08-21T22:00:00Z",
        "client_order_id": "intent-001",
    }


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_exposes_only_three_read_only_tools(self) -> None:
        self.assertEqual(
            {definition.name for definition in TOOL_CATALOG},
            {
                "get_harness_status",
                "get_market_brief",
                "validate_trade_intent",
            },
        )
        self.assertTrue(all(definition.read_only for definition in TOOL_CATALOG))
        forbidden = {"submit_order", "cancel_order", "approve_trade", "place_order"}
        self.assertTrue(forbidden.isdisjoint(definition.name for definition in TOOL_CATALOG))

    def test_catalog_returns_defensive_copies(self) -> None:
        first = tool_catalog()
        first[0]["input_schema"]["properties"]["unsafe"] = {"type": "string"}

        second = tool_catalog()

        self.assertNotIn("unsafe", second[0]["input_schema"]["properties"])

    def test_intent_schema_advertises_closed_bounded_exact_inputs(self) -> None:
        definition = next(
            item for item in TOOL_CATALOG if item.name == "validate_trade_intent"
        )
        intent = definition.input_schema["properties"]["intent"]

        self.assertFalse(intent["additionalProperties"])
        self.assertEqual(intent["maxProperties"], 27)
        self.assertEqual(intent["properties"]["intent_id"]["maxLength"], 128)
        self.assertEqual(intent["properties"]["quantity"]["type"], "string")
        self.assertEqual(
            intent["properties"]["allowed_runtime_fields"]["maxItems"],
            3,
        )

    def test_market_output_schema_closes_every_composed_object(self) -> None:
        definition = next(
            item for item in TOOL_CATALOG if item.name == "get_market_brief"
        )
        schema = definition.output_schema
        stack = [schema]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False, value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)


class ToolServiceTests(unittest.TestCase):
    def test_status_is_explicitly_fail_closed(self) -> None:
        status = ToolService(market_brief_reader=lambda *_args, **_kwargs: {}).get_harness_status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["mode"], "read_only")
        self.assertFalse(status["venue_writes_enabled"])
        self.assertFalse(status["credential_loading_enabled"])
        self.assertEqual(status["execution"]["adapter"], "disabled")
        self.assertEqual(status["market_data"]["access"], "public_read_only")
        self.assertEqual(status["market_data"]["networks"], ["mainnet", "testnet"])
        self.assertFalse(status["market_data"]["credentials_required"])

    def test_market_brief_delegates_to_read_only_reader_and_canonicalizes(self) -> None:
        calls: list[tuple[str, str, object | None]] = []
        transport = object()

        def reader(
            symbol: str,
            network: str,
            *,
            transport: object | None,
        ) -> dict[str, object]:
            calls.append((symbol, network, transport))
            return {
                "symbol": symbol,
                "mid": Decimal("3000.1000"),
                "as_of": datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc),
            }

        service = ToolService(
            market_brief_reader=reader,
            market_transport=transport,
        )
        result = service.get_market_brief("ETH", "testnet")

        self.assertEqual(calls, [("ETH", "testnet", transport)])
        self.assertEqual(result["mid"], "3000.1")
        self.assertEqual(result["as_of"], "2026-08-21T16:00:00.000000Z")

    def test_market_brief_rejects_custom_network_before_transport(self) -> None:
        called = False

        def reader(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal called
            called = True
            return {}

        service = ToolService(market_brief_reader=reader)

        with self.assertRaisesRegex(ToolInputError, "mainnet.*testnet"):
            service.get_market_brief("ETH", "https://attacker.invalid")

        self.assertFalse(called)

    def test_validates_and_hashes_without_authorizing_or_echoing_account(self) -> None:
        document = valid_intent()
        expected = semantic_intent_hash(SemanticIntent.from_mapping(document))

        result = ToolService(
            market_brief_reader=lambda *_args, **_kwargs: {}
        ).validate_trade_intent(document)

        self.assertTrue(result["valid"])
        self.assertEqual(result["intent_hash"], expected)
        self.assertFalse(result["authorization_created"])
        self.assertFalse(result["order_submitted"])
        self.assertNotIn("private-account-id", repr(result))

    def test_invalid_intent_returns_structured_failure_without_side_effects(self) -> None:
        document = valid_intent()
        document["quantity"] = 0.1

        result = ToolService(
            market_brief_reader=lambda *_args, **_kwargs: {}
        ).validate_trade_intent(document)

        self.assertFalse(result["valid"])
        self.assertEqual(result["error"]["code"], "invalid_decimal")
        self.assertIn("exact decimal string", result["error"]["message"])
        self.assertFalse(result["authorization_created"])
        self.assertFalse(result["order_submitted"])

    def test_integer_decimal_is_rejected_to_match_public_schema(self) -> None:
        document = valid_intent()
        document["quantity"] = 1

        result = ToolService(
            market_brief_reader=lambda *_args, **_kwargs: {}
        ).validate_trade_intent(document)

        self.assertFalse(result["valid"])
        self.assertEqual(result["error"]["code"], "invalid_decimal")

    def test_bad_hash_and_secret_enum_values_are_not_echoed(self) -> None:
        secret_hash = "PRIVATE-HASH-MATERIAL"
        secret_environment = "PRIVATE-ENVIRONMENT-MATERIAL"
        service = ToolService(market_brief_reader=lambda *_args, **_kwargs: {})

        bad_hash = valid_intent()
        bad_hash["code_hash"] = secret_hash
        hash_result = service.validate_trade_intent(bad_hash)

        bad_environment = valid_intent()
        bad_environment["environment"] = secret_environment
        environment_result = service.validate_trade_intent(bad_environment)

        self.assertEqual(hash_result["error"]["code"], "invalid_hash")
        self.assertEqual(environment_result["error"]["code"], "invalid_enum")
        self.assertNotIn(secret_hash, repr(hash_result))
        self.assertNotIn(secret_environment, repr(environment_result))

    def test_oversized_identifier_and_document_are_bounded_without_echo(self) -> None:
        secret = "PRIVATE-MATERIAL-" * 100_000
        service = ToolService(market_brief_reader=lambda *_args, **_kwargs: {})

        oversized = valid_intent()
        oversized["account_id"] = "x" * 257
        identifier_result = service.validate_trade_intent(oversized)

        huge = valid_intent()
        huge["account_id"] = secret
        document_result = service.validate_trade_intent(huge)

        self.assertEqual(identifier_result["error"]["code"], "value_too_long")
        self.assertEqual(document_result["error"]["code"], "document_too_large")
        self.assertNotIn(secret, repr(document_result))

    def test_unexpected_secret_field_and_deep_json_are_not_echoed(self) -> None:
        secret_key = "PRIVATE-FIELD-NAME"
        secret_value = "PRIVATE-FIELD-VALUE"
        service = ToolService(market_brief_reader=lambda *_args, **_kwargs: {})

        unexpected = valid_intent()
        unexpected[secret_key] = secret_value
        unexpected_result = service.validate_trade_intent(unexpected)

        nested: object = "PRIVATE-DEEP-VALUE"
        for _ in range(1_100):
            nested = [nested]
        deep = valid_intent()
        deep["quantity"] = nested
        deep_result = service.validate_trade_intent(deep)

        self.assertEqual(unexpected_result["error"]["code"], "unsupported_field")
        self.assertEqual(deep_result["error"]["code"], "document_too_deep")
        rendered = repr((unexpected_result, deep_result))
        self.assertNotIn(secret_key, rendered)
        self.assertNotIn(secret_value, rendered)
        self.assertNotIn("PRIVATE-DEEP-VALUE", rendered)

    def test_dispatch_rejects_unknown_or_surplus_arguments(self) -> None:
        service = ToolService(market_brief_reader=lambda *_args, **_kwargs: {})

        with self.assertRaisesRegex(ToolInputError, "unknown tool"):
            service.invoke("submit_order", {"symbol": "ETH"})
        with self.assertRaisesRegex(ToolInputError, "accepts no arguments"):
            service.invoke("get_harness_status", {"write": True})


if __name__ == "__main__":
    unittest.main()
