"""End-to-end loopback Streamable HTTP qualification for ChatGPT tooling."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "trading-desk"
EXPECTED_TOOLS = {
    "get_harness_status",
    "get_market_brief",
    "validate_trade_intent",
}


def _assert_contract(tools: list[object]) -> None:
    by_name = {tool.name: tool for tool in tools}  # type: ignore[attr-defined]
    if set(by_name) != EXPECTED_TOOLS:
        raise AssertionError(tools)
    for name, tool in by_name.items():
        if not tool.title or tool.output_schema is None:
            raise AssertionError(f"incomplete metadata: {name}")
        if tool.annotations is None or tool.annotations.title != tool.title:
            raise AssertionError(f"missing annotation title: {name}")
        if tool.annotations.read_only_hint is not True:
            raise AssertionError(f"tool is not read-only: {name}")
        if tool.annotations.destructive_hint is not False:
            raise AssertionError(f"tool is destructive: {name}")
        if tool.annotations.idempotent_hint is not True:
            raise AssertionError(f"tool is not idempotent: {name}")
        if tool.annotations.open_world_hint is not (name == "get_market_brief"):
            raise AssertionError(f"wrong open-world hint: {name}")
    market = by_name["get_market_brief"]
    if market.input_schema["properties"]["network"].get("enum") != [
        "mainnet",
        "testnet",
    ]:
        raise AssertionError(market.input_schema)
    intent = by_name["validate_trade_intent"].input_schema["properties"]["intent"]
    if intent.get("additionalProperties") is not False:
        raise AssertionError(intent)
    if intent.get("maxProperties") != 27:
        raise AssertionError(intent)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = "" if process.stderr is None else process.stderr.read()
            raise RuntimeError(f"MCP HTTP server exited early: {stderr}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError("MCP HTTP server did not listen within 10 seconds")


async def _qualify(url: str) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            _assert_contract(listed.tools)
            result = await session.call_tool("get_harness_status", {})
            if result.is_error or result.structured_content is None:
                raise AssertionError(result)
            if result.structured_content.get("mode") != "read_only":
                raise AssertionError(result.structured_content)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        isolated_plugin = Path(directory) / "trading-desk"
        shutil.copytree(PLUGIN, isolated_plugin)
        child_environment = dict(os.environ)
        child_environment.pop("PYTHONPATH", None)
        port = _available_port()
        process = subprocess.Popen(
            [
                sys.executable,
                str(isolated_plugin / "server.py"),
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=isolated_plugin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=child_environment,
        )
        try:
            _wait_for_listener(process, port)
            asyncio.run(_qualify(f"http://127.0.0.1:{port}/mcp"))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("mcp streamable HTTP smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
