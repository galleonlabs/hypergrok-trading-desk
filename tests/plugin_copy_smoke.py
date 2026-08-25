"""Qualify an isolated cached copy of the ChatGPT/Codex plugin."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PLUGIN = ROOT / "plugins" / "trading-desk"
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


async def qualify(plugin: Path) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(plugin / "server.py")],
        cwd=plugin,
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            if {tool.name for tool in listed.tools} != EXPECTED_TOOLS:
                raise AssertionError(listed.tools)
            status = await session.call_tool("get_harness_status", {})
            if status.is_error or status.structured_content is None:
                raise AssertionError(status)
            if status.structured_content.get("venue_writes_enabled") is not False:
                raise AssertionError(status.structured_content)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        plugin = Path(directory) / "trading-desk"
        shutil.copytree(SOURCE_PLUGIN, plugin, symlinks=False)
        if any(path.is_symlink() for path in plugin.rglob("*")):
            raise AssertionError("cached plugin copy contains a symlink")
        asyncio.run(qualify(plugin))
    print("isolated cached plugin smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
