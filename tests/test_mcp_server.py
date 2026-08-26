from __future__ import annotations

from contextlib import redirect_stderr
from datetime import timedelta
from io import StringIO
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_harness import mcp_server
from trading_harness.errors import ValidationError
from trading_harness.tool_api import ToolService
from trading_harness.research_api import ResearchService
from trading_harness.research_store import ResearchStore
from tests.test_market_data import FixtureTransport, fixture_brief
from tests.test_node import AT, history_reader
from tests.test_research_api import evidence, iso


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
    @staticmethod
    def _current_umask() -> int:
        current = os.umask(0o777)
        os.umask(current)
        return current

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional mcp runtime")
    def test_registers_exactly_the_bounded_research_service_surface(self) -> None:
        server = mcp_server.build_mcp_server(
            service=ToolService(market_brief_reader=lambda *_args, **_kwargs: {})
        )
        registered = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in registered}
        catalog = {tool.name: tool for tool in mcp_server.TOOL_CATALOG}

        self.assertEqual(
            set(by_name),
            {
                "analyze_asset",
                "get_latest_sentiment",
                "get_learning_review",
                "get_learning_summary",
                "get_node_status",
                "get_harness_status",
                "get_market_brief",
                "get_trade_stage",
                "list_tracked_assets",
                "pause_tracked_asset",
                "record_manual_sentiment",
                "stage_trade_candidate",
                "track_asset",
                "validate_candidate_profitability",
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
            self.assertEqual(tool.annotations.read_only_hint, definition.read_only)
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
        self.assertTrue(
            status.structured_content["research"]["local_state_writes_enabled"]
        )

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

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional mcp runtime")
    def test_real_mcp_research_write_and_analysis_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            research = ResearchService(
                ResearchStore(Path(directory) / "research.sqlite3"),
                clock=lambda: AT,
                history_reader=history_reader,
                analysis_bars=1001,
                validation_bars=1001,
            )
            server = mcp_server.build_mcp_server(
                service=ToolService(research_service=research)
            )
            tracked = asyncio.run(
                server.call_tool(
                    "track_asset",
                    {
                        "asset_id": "eth",
                        "symbol": "ETH",
                        "network": "testnet",
                        "sentiment_query": "$ETH OR Ethereum",
                    },
                )
            )
            recorded = asyncio.run(
                server.call_tool(
                    "record_manual_sentiment",
                    {
                        "asset_id": "eth",
                        "window_start": iso(AT - timedelta(hours=4)),
                        "window_end": iso(AT),
                        "evidence": evidence(),
                        "excluded_count": 0,
                        "collection_complete": True,
                    },
                )
            )
            analyzed = asyncio.run(
                server.call_tool("analyze_asset", {"asset_id": "eth"})
            )

        self.assertFalse(tracked.is_error, tracked)
        self.assertFalse(tracked.structured_content["order_submitted"])
        self.assertFalse(recorded.is_error, recorded)
        self.assertFalse(recorded.structured_content["unattended_eligible"])
        self.assertFalse(analyzed.is_error, analyzed)
        self.assertEqual(
            analyzed.structured_content["registered_signal"]["direction"],
            "buy",
        )
        self.assertFalse(analyzed.structured_content["venue_writes_enabled"])

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

    def test_main_sanitizes_sqlite_profile_failure(self) -> None:
        stderr = StringIO()
        with (
            patch.object(
                mcp_server,
                "_configured_service",
                side_effect=sqlite3.OperationalError("sensitive database path"),
            ),
            redirect_stderr(stderr),
        ):
            result = mcp_server.main([])

        self.assertEqual(2, result)
        self.assertIn("OperationalError", stderr.getvalue())
        self.assertNotIn("sensitive database path", stderr.getvalue())

    def test_main_defaults_to_stdio(self) -> None:
        server = FakeMCPServer()
        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            result = mcp_server.main([])

        self.assertEqual(result, 0)
        self.assertEqual(server.transport, "stdio")
        self.assertEqual(server.run_options, {})

    def test_main_forces_private_umask_for_server_lifetime_and_restores_it(self) -> None:
        original = os.umask(0o022)
        self.addCleanup(os.umask, original)
        observed: list[int] = []
        server = FakeMCPServer()

        def run(*, transport: str, **options: object) -> None:
            observed.append(self._current_umask())
            FakeMCPServer.run(server, transport=transport, **options)

        with (
            patch.object(server, "run", side_effect=run),
            patch.object(mcp_server, "build_mcp_server", return_value=server),
        ):
            result = mcp_server.main([])

        self.assertEqual(0, result)
        self.assertEqual([0o077], observed)
        self.assertEqual(0o022, self._current_umask())

    def test_streamable_http_is_loopback_only_on_fixed_mcp_path(self) -> None:
        server = FakeMCPServer()
        with patch.object(mcp_server, "build_mcp_server", return_value=server):
            result = mcp_server.main(
                [
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.2",
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

    def test_configured_learning_profile_requires_all_three_absolute_paths(self) -> None:
        stderr = StringIO()
        with (
            patch.object(mcp_server, "build_mcp_server") as build,
            redirect_stderr(stderr),
        ):
            result = mcp_server.main(
                [
                    "--learning-executor-config",
                    "/private/executor.toml",
                ]
            )
        self.assertEqual(2, result)
        self.assertFalse(build.called)
        self.assertIn("ValueError", stderr.getvalue())

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            mcp_server.main(
                [
                    "--learning-executor-config",
                    "relative.toml",
                    "--learning-research-db",
                    "/private/research.sqlite3",
                    "--learning-grant",
                    "/private/grant.json",
                ]
            )

    def test_configured_learning_profile_requires_research_uid_before_grant_load(self) -> None:
        arguments = mcp_server.build_parser().parse_args(
            [
                "--learning-executor-config",
                "/private/executor.toml",
                "--learning-research-db",
                "/private/research.sqlite3",
                "--learning-grant",
                "/private/grant.json",
            ]
        )
        config = SimpleNamespace(research_uid=os.geteuid() + 1)
        with (
            patch.object(mcp_server, "load_executor_config", return_value=config),
            patch.object(
                mcp_server,
                "load_signed_infrastructure_grant",
                side_effect=AssertionError("wrong identity must not read the grant"),
            ),
            self.assertRaisesRegex(ValidationError, "research UID"),
        ):
            mcp_server._configured_service(arguments)


class PluginWiringTests(unittest.TestCase):
    def test_manifest_points_to_local_bounded_research_mcp_config(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["name"], "trading-desk")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertIn("Read", manifest["interface"]["capabilities"])
        self.assertIn("Write", manifest["interface"]["capabilities"])

    def test_mcp_config_targets_only_the_configured_loopback_service(self) -> None:
        config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))

        self.assertEqual(set(config["mcpServers"]), {"trading_desk"})
        server = config["mcpServers"]["trading_desk"]
        self.assertEqual(server["url"], "http://127.0.0.1:8765/mcp")
        self.assertNotIn("command", server)
        self.assertNotIn("args", server)
        self.assertNotIn("env", server)
        self.assertNotIn("env_vars", server)

    def test_optional_runtime_is_exactly_pinned_and_has_one_entrypoint(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            project["project"]["optional-dependencies"]["mcp"],
            ["mcp==2.0.0"],
        )
        self.assertEqual(
            project["project"]["optional-dependencies"]["execution"],
            ["hyperliquid-python-sdk==0.24.0"],
        )
        self.assertEqual(
            project["project"]["scripts"]["trading-harness-mcp"],
            "trading_harness.mcp_server:main",
        )


if __name__ == "__main__":
    unittest.main()
