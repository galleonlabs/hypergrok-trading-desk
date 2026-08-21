from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import asyncio
import importlib.util
import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from trading_harness import mcp_server
from trading_harness.tool_api import ToolService
from tests.test_market_data import FixtureTransport, fixture_brief


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "trading-desk"


class FakeAnnotations:
    def __init__(self, **values: object) -> None:
        self.values = values


class FakeMCPServer:
    def __init__(self, **metadata: object) -> None:
        self.metadata = metadata
        self.tools: dict[str, object] = {}
        self.descriptions: dict[str, str] = {}
        self.annotations: dict[str, FakeAnnotations] = {}
        self.transport: str | None = None
        self.run_options: dict[str, object] = {}

    def tool(  # type: ignore[no-untyped-def]
        self,
        *,
        name: str,
        description: str,
        annotations: FakeAnnotations,
    ):
        def register(function):  # type: ignore[no-untyped-def]
            self.tools[name] = function
            self.descriptions[name] = description
            self.annotations[name] = annotations
            return function

        return register

    def run(self, transport: str = "stdio", **_kwargs: object) -> None:
        self.transport = transport
        self.run_options = dict(_kwargs)


class MCPAdapterTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional mcp runtime")
    def test_registers_exactly_the_read_only_service_surface(self) -> None:
        server = mcp_server.build_mcp_server(
            service=ToolService(market_brief_reader=lambda *_args, **_kwargs: {})
        )
        registered = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in registered}
        catalog = {tool.name: tool for tool in mcp_server.TOOL_CATALOG}

        self.assertEqual(
            set(by_name),
            {
                "get_harness_status",
                "get_market_brief",
                "validate_trade_intent",
            },
        )
        for name, tool in by_name.items():
            definition = catalog[name]
            self.assertEqual(tool.title, definition.title)
            self.assertTrue(tool.title)
            self.assertEqual(tool.input_schema, definition.input_schema)
            self.assertEqual(tool.output_schema, definition.output_schema)
            self.assertIsNotNone(tool.annotations)
            self.assertEqual(tool.annotations.title, definition.title)
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertEqual(
                tool.annotations.idempotent_hint,
                definition.idempotent,
            )
            self.assertEqual(tool.annotations.open_world_hint, definition.open_world)

        market_input = by_name["get_market_brief"].input_schema
        self.assertFalse(market_input["additionalProperties"])
        self.assertEqual(
            market_input["properties"]["network"]["enum"],
            ["mainnet", "testnet"],
        )
        intent = by_name["validate_trade_intent"].input_schema["properties"][
            "intent"
        ]
        self.assertFalse(intent["additionalProperties"])
        self.assertEqual(intent["maxProperties"], 27)

        status = asyncio.run(server.call_tool("get_harness_status", {}))
        self.assertFalse(status.is_error)
        self.assertFalse(status.structured_content["venue_writes_enabled"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional mcp runtime")
    def test_real_mcp_validation_does_not_echo_unknown_arguments_or_enum(self) -> None:
        server = mcp_server.build_mcp_server()
        secret_key = "PRIVATE-ARGUMENT-NAME"
        secret_value = "PRIVATE-ARGUMENT-VALUE"

        with self.assertRaises(Exception) as caught:
            asyncio.run(
                server.call_tool(
                    "get_harness_status",
                    {secret_key: secret_value},
                )
            )

        invalid = {
            "intent_id": "intent-001",
            "thesis_id": "thesis-001",
            "thesis_version": "1",
            "strategy_version": "1",
            "code_hash": "a" * 64,
            "venue": "hyperliquid",
            "account_id": "private-account-id",
            "environment": secret_value,
            "instrument": "ETH-PERP",
            "action": "place_order",
            "side": "buy",
            "quantity": "0.1",
            "order_type": "limit",
            "limit_price": "3000",
            "expires_at": "2099-01-01T00:00:00Z",
            "client_order_id": "intent-001",
        }
        result = asyncio.run(
            server.call_tool("validate_trade_intent", {"intent": invalid})
        )

        self.assertNotIn(secret_key, str(caught.exception))
        self.assertNotIn(secret_value, str(caught.exception))
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["valid"])
        self.assertNotIn(secret_value, repr(result.structured_content))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional mcp runtime")
    def test_real_mcp_market_result_satisfies_closed_output_schema(self) -> None:
        def reader(symbol: str, network: str, **_kwargs: object) -> dict[str, object]:
            return fixture_brief(symbol, network, FixtureTransport())

        server = mcp_server.build_mcp_server(
            service=ToolService(market_brief_reader=reader)
        )
        result = asyncio.run(
            server.call_tool(
                "get_market_brief",
                {"symbol": "ETH", "network": "mainnet"},
            )
        )

        self.assertFalse(result.is_error, result)
        brief = result.structured_content
        self.assertEqual(brief["symbol"], "ETH")
        self.assertEqual(
            set(brief["book"]["depth"]),
            {"5bps", "10bps", "25bps"},
        )

    def test_main_reports_missing_optional_runtime_without_traceback(self) -> None:
        stderr = StringIO()
        with (
            patch.object(
                mcp_server,
                "build_mcp_server",
                side_effect=mcp_server.MCPRuntimeUnavailable("install mcp"),
            ),
            redirect_stderr(stderr),
        ):
            result = mcp_server.main([])

        self.assertEqual(result, 2)
        self.assertIn("install mcp", stderr.getvalue())

    def test_main_defaults_to_stdio(self) -> None:
        server = FakeMCPServer()
        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            result = mcp_server.main([])

        self.assertEqual(result, 0)
        self.assertEqual(server.transport, "stdio")
        self.assertEqual(server.run_options, {})

    def test_streamable_http_is_loopback_only_on_fixed_mcp_path(self) -> None:
        server = FakeMCPServer()
        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            result = mcp_server.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.2",
                    "--port",
                    "8765",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(server.transport, "streamable-http")
        self.assertEqual(server.run_options["host"], "127.0.0.2")
        self.assertEqual(server.run_options["port"], 8765)
        self.assertEqual(server.run_options["streamable_http_path"], "/mcp")
        self.assertEqual(server.run_options["max_request_body_size"], 1_000_000)

    def test_streamable_http_refuses_public_binding_without_auth(self) -> None:
        stderr = StringIO()
        with (
            patch.object(mcp_server, "build_mcp_server") as build,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            mcp_server.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "0.0.0.0",
                ]
            )

        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(build.called)
        self.assertIn("authentication is not implemented", stderr.getvalue())

    def test_streamable_http_rejects_invalid_port(self) -> None:
        with (
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            mcp_server.main(["--transport", "streamable-http", "--port", "65536"])

        self.assertEqual(caught.exception.code, 2)


class PluginWiringTests(unittest.TestCase):
    def test_manifest_points_to_local_read_only_mcp_config(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["name"], "trading-desk")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertIn("Read", manifest["interface"]["capabilities"])
        self.assertNotIn("Write", manifest["interface"]["capabilities"])

    def test_mcp_config_launches_only_the_checked_in_stdio_server(self) -> None:
        config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))

        self.assertEqual(set(config["mcpServers"]), {"trading_desk"})
        server = config["mcpServers"]["trading_desk"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["./server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertNotIn("env", server)
        self.assertNotIn("env_vars", server)

    def test_optional_runtime_is_exactly_pinned_and_has_one_entrypoint(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            project["project"]["optional-dependencies"]["mcp"],
            ["mcp==2.0.0"],
        )
        self.assertEqual(
            project["project"]["scripts"]["trading-harness-mcp"],
            "trading_harness.mcp_server:main",
        )


if __name__ == "__main__":
    unittest.main()
