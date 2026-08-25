"""End-to-end MCP 2.0 stdio qualification used by the optional CI job."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "trading-desk"
EXPECTED_TOOLS = {
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
}
LOCAL_WRITE_TOOLS = {
    "analyze_asset",
    "pause_tracked_asset",
    "record_manual_sentiment",
    "stage_trade_candidate",
    "track_asset",
}
OPEN_WORLD_TOOLS = {
    "analyze_asset",
    "get_market_brief",
    "stage_trade_candidate",
    "validate_candidate_profitability",
}


def _assert_contract(tools: list[object]) -> None:
    by_name = {tool.name: tool for tool in tools}  # type: ignore[attr-defined]
    if set(by_name) != EXPECTED_TOOLS:
        raise AssertionError(tools)
    for name, tool in by_name.items():
        if not tool.title:
            raise AssertionError(f"tool has no title: {name}")
        if tool.annotations is None or tool.annotations.title != tool.title:
            raise AssertionError(f"tool annotation has no matching title: {name}")
        if tool.annotations.read_only_hint is not (name not in LOCAL_WRITE_TOOLS):
            raise AssertionError(f"tool has wrong read-only hint: {name}")
        if tool.annotations.destructive_hint is not False:
            raise AssertionError(f"tool is marked destructive: {name}")
        if tool.annotations.idempotent_hint is not True:
            raise AssertionError(f"tool is not marked idempotent: {name}")
        expected_open_world = name in OPEN_WORLD_TOOLS
        if tool.annotations.open_world_hint is not expected_open_world:
            raise AssertionError(f"tool has wrong open-world hint: {name}")
        if tool.output_schema is None:
            raise AssertionError(f"tool has no output schema: {name}")

    market_input = by_name["get_market_brief"].input_schema
    if market_input.get("additionalProperties") is not False:
        raise AssertionError(market_input)
    if market_input["properties"]["network"].get("enum") != [
        "mainnet",
        "testnet",
    ]:
        raise AssertionError(market_input)
    market_output = by_name["get_market_brief"].output_schema
    for composed in (
        market_output,
        market_output["properties"]["timestamps"],
        market_output["properties"]["mid_consistency"],
        market_output["properties"]["book"],
        market_output["properties"]["book"]["properties"]["depth"],
    ):
        if composed.get("additionalProperties") is not False:
            raise AssertionError(composed)
    depth = market_output["properties"]["book"]["properties"]["depth"]
    if set(depth["required"]) != {"5bps", "10bps", "25bps"}:
        raise AssertionError(depth)

    intent = by_name["validate_trade_intent"].input_schema["properties"]["intent"]
    if intent.get("additionalProperties") is not False:
        raise AssertionError(intent)
    if intent.get("maxProperties") != 27:
        raise AssertionError(intent)
    if intent["properties"]["quantity"].get("type") != "string":
        raise AssertionError(intent)


async def qualify(
    *, live_symbol: str | None = None, network: str = "mainnet"
) -> dict[str, object] | None:
    with tempfile.TemporaryDirectory() as directory:
        isolated_plugin = Path(directory) / "trading-desk"
        shutil.copytree(PLUGIN, isolated_plugin)
        child_environment = dict(os.environ)
        child_environment.pop("PYTHONPATH", None)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(isolated_plugin / "server.py")],
            cwd=isolated_plugin,
            env=child_environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                return await _qualify_session(
                    session,
                    live_symbol=live_symbol,
                    network=network,
                )


async def _qualify_session(
    session: ClientSession,
    *,
    live_symbol: str | None,
    network: str,
) -> dict[str, object] | None:
            initialized = await session.initialize()
            if initialized.server_info.name != "trading-desk":
                raise AssertionError(initialized.server_info)

            listed = await session.list_tools()
            _assert_contract(listed.tools)

            result = await session.call_tool("get_harness_status", {})
            if result.is_error:
                raise AssertionError(result)
            status = result.structured_content
            if not isinstance(status, dict):
                raise AssertionError("status tool returned no structured content")
            if status.get("mode") != "research_only":
                raise AssertionError(status)
            if status.get("venue_writes_enabled") is not False:
                raise AssertionError(status)
            if status.get("credential_loading_enabled") is not False:
                raise AssertionError(status)

            validated = await session.call_tool(
                "validate_trade_intent",
                {
                    "intent": {
                        "intent_id": "mcp-smoke-intent",
                        "thesis_id": "mcp-smoke-thesis",
                        "thesis_version": "1",
                        "strategy_version": "1",
                        "code_hash": "a" * 64,
                        "venue": "hyperliquid",
                        "account_id": "testnet-smoke-account",
                        "environment": "testnet",
                        "instrument": "ETH-PERP",
                        "action": "simulate_order",
                        "side": "buy",
                        "quantity": "0.01",
                        "order_type": "limit",
                        "limit_price": "1000",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "client_order_id": "mcp-smoke-client-order",
                    }
                },
            )
            if validated.is_error or not isinstance(
                validated.structured_content, dict
            ):
                raise AssertionError(validated)
            validation = validated.structured_content
            if validation.get("valid") is not True:
                raise AssertionError(validation)
            if validation.get("authorization_created") is not False:
                raise AssertionError(validation)
            if validation.get("order_submitted") is not False:
                raise AssertionError(validation)

            if live_symbol is None:
                return None
            live = await session.call_tool(
                "get_market_brief",
                {"symbol": live_symbol, "network": network},
            )
            if live.is_error or not isinstance(live.structured_content, dict):
                raise AssertionError(live)
            brief = live.structured_content
            if brief.get("symbol") != live_symbol.upper():
                raise AssertionError(brief)
            age = brief.get("age_ms")
            if not isinstance(age, int) or not 0 <= age <= 60_000:
                raise AssertionError(brief)
            return brief


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-market")
    parser.add_argument("--network", choices=("mainnet", "testnet"), default="mainnet")
    arguments = parser.parse_args()
    brief = asyncio.run(
        qualify(live_symbol=arguments.live_market, network=arguments.network)
    )
    if brief is None:
        print("mcp stdio smoke: ok")
    else:
        print(
            "mcp stdio live market: ok "
            f"symbol={brief['symbol']} network={brief['network']} age_ms={brief['age_ms']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
