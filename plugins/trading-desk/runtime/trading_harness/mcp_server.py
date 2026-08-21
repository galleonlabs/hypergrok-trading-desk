"""Optional MCP 2.0 adapter for the read-only :mod:`tool_api` service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import ip_address
import sys
from typing import Any, Protocol

from .tool_api import TOOL_CATALOG, ToolService


_STREAMABLE_HTTP_PATH = "/mcp"
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8000


class MCPRuntimeUnavailable(RuntimeError):
    """Raised when the optional MCP runtime has not been installed."""


class _MCPServer(Protocol):
    def tool(self, **metadata: object) -> Any:
        """Return a decorator that registers a tool."""

    def run(self, transport: str = "stdio", **kwargs: object) -> object:
        """Run the server using the requested transport."""


@dataclass(frozen=True, slots=True)
class _MCPRuntime:
    server_class: type[Any]
    tool_class: type[Any]
    annotations_class: type[Any]
    func_metadata_class: type[Any]
    arg_model_base: type[Any]
    config_class: type[Any]
    field: Any
    create_model: Any
    root_model: type[Any]
    model_validator: Any
    custom_error: type[Exception]
    schema_validator_class: type[Any]


def _loopback_host(value: str) -> str:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "host must be a numeric loopback address; public binding is disabled "
            "because authentication is not implemented"
        ) from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError(
            "host must be loopback-only; public binding is disabled because "
            "authentication is not implemented"
        )
    return str(address)


def _port(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the local transport CLI without any public-bind option."""

    parser = argparse.ArgumentParser(
        prog="trading-harness-mcp",
        description="Run the Trading Desk read-only MCP adapter.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="local MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        type=_loopback_host,
        default=_DEFAULT_HTTP_HOST,
        help="numeric loopback bind address for streamable HTTP",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=_DEFAULT_HTTP_PORT,
        help="loopback port for streamable HTTP (default: 8000)",
    )
    return parser


def _load_mcp_runtime() -> _MCPRuntime:
    """Import MCP 2.0 only when a caller actually starts an MCP server."""

    try:
        from jsonschema import Draft202012Validator
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.tools import Tool
        from mcp.server.mcpserver.utilities.func_metadata import (
            ArgModelBase,
            FuncMetadata,
        )
        from mcp.types import ToolAnnotations
        from pydantic import ConfigDict, Field, RootModel, create_model, model_validator
        from pydantic_core import PydanticCustomError
    except ImportError as error:  # pragma: no cover - environment dependent
        raise MCPRuntimeUnavailable(
            "the optional 'mcp==2.0.0' package is required to run the MCP adapter"
        ) from error
    return _MCPRuntime(
        server_class=MCPServer,
        tool_class=Tool,
        annotations_class=ToolAnnotations,
        func_metadata_class=FuncMetadata,
        arg_model_base=ArgModelBase,
        config_class=ConfigDict,
        field=Field,
        create_model=create_model,
        root_model=RootModel,
        model_validator=model_validator,
        custom_error=PydanticCustomError,
        schema_validator_class=Draft202012Validator,
    )


def build_mcp_server(
    *,
    service: ToolService | None = None,
) -> _MCPServer:
    """Build an MCP 2.0 server from explicit, bounded tool contracts."""

    tool_service = ToolService() if service is None else service
    runtime = _load_mcp_runtime()

    class StrictArguments(runtime.arg_model_base):
        model_config = runtime.config_class(
            arbitrary_types_allowed=True,
            extra="forbid",
            hide_input_in_errors=True,
        )

        @runtime.model_validator(mode="before")
        @classmethod
        def reject_unknown_arguments(cls, value: object) -> object:
            if isinstance(value, dict) and set(value) - set(cls.model_fields):
                raise runtime.custom_error(
                    "unsupported_arguments",
                    "tool call contains unsupported arguments",
                )
            return value

    status_arguments = runtime.create_model(
        "GetHarnessStatusArguments",
        __base__=StrictArguments,
    )
    market_arguments = runtime.create_model(
        "GetMarketBriefArguments",
        __base__=StrictArguments,
        symbol=(
            str,
            runtime.field(
                min_length=1,
                max_length=64,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
            ),
        ),
        network=(
            str,
            runtime.field(pattern=r"^(?:mainnet|testnet)$"),
        ),
    )
    intent_arguments = runtime.create_model(
        "ValidateTradeIntentArguments",
        __base__=StrictArguments,
        intent=(dict[str, Any], ...),
    )
    output_model = runtime.root_model[dict[str, Any]]

    definitions = {definition.name: definition for definition in TOOL_CATALOG}

    def validate_output(name: str, value: dict[str, Any]) -> dict[str, Any]:
        definition = definitions[name]
        validator = runtime.schema_validator_class(definition.output_schema)
        if next(validator.iter_errors(value), None) is not None:
            raise RuntimeError("tool produced output outside its declared schema")
        return value

    def get_harness_status() -> dict[str, Any]:
        return validate_output(
            "get_harness_status",
            tool_service.get_harness_status(),
        )

    def get_market_brief(symbol: str, network: str) -> dict[str, Any]:
        return validate_output(
            "get_market_brief",
            tool_service.get_market_brief(symbol, network),
        )

    def validate_trade_intent(intent: dict[str, Any]) -> dict[str, Any]:
        return validate_output(
            "validate_trade_intent",
            tool_service.validate_trade_intent(intent),
        )

    functions = {
        "get_harness_status": (get_harness_status, status_arguments),
        "get_market_brief": (get_market_brief, market_arguments),
        "validate_trade_intent": (validate_trade_intent, intent_arguments),
    }
    tools: list[Any] = []
    for name in sorted(functions):
        function, argument_model = functions[name]
        definition = definitions[name]
        runtime.schema_validator_class.check_schema(definition.input_schema)
        runtime.schema_validator_class.check_schema(definition.output_schema)
        metadata = runtime.func_metadata_class(
            arg_model=argument_model,
            output_model=output_model,
            output_schema=deepcopy(dict(definition.output_schema)),
            wrap_output=False,
        )
        tools.append(
            runtime.tool_class(
                fn=function,
                name=definition.name,
                title=definition.title,
                description=definition.description,
                parameters=deepcopy(dict(definition.input_schema)),
                fn_metadata=metadata,
                is_async=False,
                annotations=runtime.annotations_class(
                    title=definition.title,
                    readOnlyHint=definition.read_only,
                    destructiveHint=definition.destructive,
                    idempotentHint=definition.idempotent,
                    openWorldHint=definition.open_world,
                ),
            )
        )

    server = runtime.server_class(
        name="trading-desk",
        title="Trading Desk",
        description="Read-only tools for the deterministic trading harness.",
        instructions=(
            "These tools may read public market data or validate an intent. "
            "They cannot load credentials, authorize a trade, or write to a venue."
        ),
        version="0.1.0",
        tools=tools,
    )
    return server


def main(argv: Sequence[str] | None = None) -> int:
    """Run the optional adapter over stdio or loopback streamable HTTP."""

    arguments = build_parser().parse_args(argv)
    try:
        server = build_mcp_server()
    except MCPRuntimeUnavailable as error:
        print(f"trading-harness MCP: {error}", file=sys.stderr)
        return 2
    if arguments.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=arguments.host,
            port=arguments.port,
            streamable_http_path=_STREAMABLE_HTTP_PATH,
            max_request_body_size=1_000_000,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by plugin launcher
    raise SystemExit(main())
