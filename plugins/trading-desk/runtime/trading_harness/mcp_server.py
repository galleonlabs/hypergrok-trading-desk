"""Optional MCP 2.0 adapter for the bounded research :mod:`tool_api` service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import ip_address
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Protocol

from .tool_api import TOOL_CATALOG, ToolService
from .executor_config import load_executor_config
from .grant_artifact import load_signed_infrastructure_grant
from .learning_tool_service import build_testnet_learning_tool_service
from .errors import HarnessError, ValidationError


_STREAMABLE_HTTP_PATH = "/mcp"
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8765


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


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise argparse.ArgumentTypeError("path must be normalized and absolute")
    return path


def build_parser() -> argparse.ArgumentParser:
    """Build the local transport CLI without any public-bind option."""

    parser = argparse.ArgumentParser(
        prog="trading-harness-mcp",
        description="Run the Trading Desk research MCP adapter.",
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
        help="loopback port for streamable HTTP (default: 8765)",
    )
    parser.add_argument(
        "--learning-executor-config",
        type=_absolute_path,
        help=(
            "owner-only strict TESTNET executor config for the configured "
            "non-authoritative learning profile"
        ),
    )
    parser.add_argument(
        "--learning-research-db",
        type=_absolute_path,
        help="absolute research SQLite path for the configured learning profile",
    )
    parser.add_argument(
        "--learning-grant",
        type=_absolute_path,
        help="owner-only signed infrastructure-learning grant artifact",
    )
    return parser


def _configured_service(arguments: argparse.Namespace) -> ToolService:
    values = (
        arguments.learning_executor_config,
        arguments.learning_research_db,
        arguments.learning_grant,
    )
    if not any(value is not None for value in values):
        return ToolService()
    if any(value is None for value in values):
        raise ValueError(
            "configured learning profile requires executor config, research DB, and grant"
        )
    config = load_executor_config(arguments.learning_executor_config)
    if not hasattr(os, "geteuid") or os.geteuid() != config.research_uid:
        raise ValidationError("configured learning profile requires the research UID")
    signed_grant = load_signed_infrastructure_grant(arguments.learning_grant)
    return build_testnet_learning_tool_service(
        config=config,
        research_database=arguments.learning_research_db,
        signed_grant=signed_grant,
    )


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
    track_arguments = runtime.create_model(
        "TrackAssetArguments",
        __base__=StrictArguments,
        asset_id=(str, runtime.field(min_length=1, max_length=128)),
        symbol=(
            str,
            runtime.field(
                min_length=1,
                max_length=64,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
            ),
        ),
        network=(str, runtime.field(pattern=r"^(?:mainnet|testnet)$")),
        sentiment_query=(str, runtime.field(min_length=1, max_length=1024)),
        poll_seconds=(int, runtime.field(default=60, ge=10, le=86400)),
    )
    pause_arguments = runtime.create_model(
        "PauseTrackedAssetArguments",
        __base__=StrictArguments,
        asset_id=(str, runtime.field(min_length=1, max_length=128)),
        expected_revision=(int, runtime.field(ge=1)),
    )
    empty_arguments = runtime.create_model(
        "ListTrackedAssetsArguments",
        __base__=StrictArguments,
    )
    asset_arguments = runtime.create_model(
        "AssetIdArguments",
        __base__=StrictArguments,
        asset_id=(str, runtime.field(min_length=1, max_length=128)),
    )
    sentiment_arguments = runtime.create_model(
        "RecordManualSentimentArguments",
        __base__=StrictArguments,
        asset_id=(str, runtime.field(min_length=1, max_length=128)),
        window_start=(str, runtime.field(min_length=20, max_length=64)),
        window_end=(str, runtime.field(min_length=20, max_length=64)),
        evidence=(list[dict[str, Any]], runtime.field(max_length=100)),
        excluded_count=(int, runtime.field(ge=0)),
        collection_complete=(bool, ...),
    )
    node_arguments = runtime.create_model(
        "GetNodeStatusArguments",
        __base__=StrictArguments,
        node_id=(
            str,
            runtime.field(
                default="trading-desk-research",
                min_length=1,
                max_length=128,
            ),
        ),
    )
    stage_arguments = runtime.create_model(
        "StageTradeCandidateArguments",
        __base__=StrictArguments,
        asset_id=(str, runtime.field(min_length=1, max_length=128)),
        expected_analysis_hash=(
            str,
            runtime.field(pattern=r"^[0-9a-f]{64}$"),
        ),
        idempotency_key=(
            str,
            runtime.field(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            ),
        ),
    )
    get_stage_arguments = runtime.create_model(
        "GetTradeStageArguments",
        __base__=StrictArguments,
        document_id=(str, runtime.field(min_length=1, max_length=80)),
    )
    learning_review_arguments = runtime.create_model(
        "GetLearningReviewArguments",
        __base__=StrictArguments,
        cycle_id=(str, runtime.field(min_length=1, max_length=128)),
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

    def track_asset(
        asset_id: str,
        symbol: str,
        network: str,
        sentiment_query: str,
        poll_seconds: int = 60,
    ) -> dict[str, Any]:
        return validate_output(
            "track_asset",
            tool_service.track_asset(
                asset_id,
                symbol,
                network,
                sentiment_query,
                poll_seconds,
            ),
        )

    def pause_tracked_asset(asset_id: str, expected_revision: int) -> dict[str, Any]:
        return validate_output(
            "pause_tracked_asset",
            tool_service.pause_tracked_asset(asset_id, expected_revision),
        )

    def list_tracked_assets() -> dict[str, Any]:
        return validate_output(
            "list_tracked_assets",
            tool_service.list_tracked_assets(),
        )

    def record_manual_sentiment(
        asset_id: str,
        window_start: str,
        window_end: str,
        evidence: list[dict[str, Any]],
        excluded_count: int,
        collection_complete: bool,
    ) -> dict[str, Any]:
        return validate_output(
            "record_manual_sentiment",
            tool_service.record_manual_sentiment(
                asset_id=asset_id,
                window_start=window_start,
                window_end=window_end,
                evidence=evidence,
                excluded_count=excluded_count,
                collection_complete=collection_complete,
            ),
        )

    def get_latest_sentiment(asset_id: str) -> dict[str, Any]:
        return validate_output(
            "get_latest_sentiment",
            tool_service.get_latest_sentiment(asset_id),
        )

    def analyze_asset(asset_id: str) -> dict[str, Any]:
        return validate_output(
            "analyze_asset",
            tool_service.analyze_asset(asset_id),
        )

    def validate_candidate_profitability(asset_id: str) -> dict[str, Any]:
        return validate_output(
            "validate_candidate_profitability",
            tool_service.validate_candidate_profitability(asset_id),
        )

    def get_node_status(
        node_id: str = "trading-desk-research",
    ) -> dict[str, Any]:
        return validate_output(
            "get_node_status",
            tool_service.get_node_status(node_id),
        )

    def stage_trade_candidate(
        asset_id: str,
        expected_analysis_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return validate_output(
            "stage_trade_candidate",
            tool_service.stage_trade_candidate(
                asset_id,
                expected_analysis_hash,
                idempotency_key,
            ),
        )

    def get_trade_stage(document_id: str) -> dict[str, Any]:
        return validate_output(
            "get_trade_stage",
            tool_service.get_trade_stage(document_id),
        )

    def get_learning_review(cycle_id: str) -> dict[str, Any]:
        return validate_output(
            "get_learning_review",
            tool_service.get_learning_review(cycle_id),
        )

    def get_learning_summary() -> dict[str, Any]:
        return validate_output(
            "get_learning_summary",
            tool_service.get_learning_summary(),
        )

    functions = {
        "analyze_asset": (analyze_asset, asset_arguments),
        "get_latest_sentiment": (get_latest_sentiment, asset_arguments),
        "get_node_status": (get_node_status, node_arguments),
        "get_harness_status": (get_harness_status, status_arguments),
        "get_learning_review": (get_learning_review, learning_review_arguments),
        "get_learning_summary": (get_learning_summary, empty_arguments),
        "get_market_brief": (get_market_brief, market_arguments),
        "get_trade_stage": (get_trade_stage, get_stage_arguments),
        "list_tracked_assets": (list_tracked_assets, empty_arguments),
        "pause_tracked_asset": (pause_tracked_asset, pause_arguments),
        "record_manual_sentiment": (
            record_manual_sentiment,
            sentiment_arguments,
        ),
        "stage_trade_candidate": (stage_trade_candidate, stage_arguments),
        "track_asset": (track_asset, track_arguments),
        "validate_candidate_profitability": (
            validate_candidate_profitability,
            asset_arguments,
        ),
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
        description="Bounded research tools for the deterministic trading harness.",
        instructions=(
            "These tools may read public market data, write local research state, "
            "or validate an intent. They cannot load credentials, authorize a "
            "trade, or write to a venue."
        ),
        version="0.2.0",
        tools=tools,
    )
    return server


def main(argv: Sequence[str] | None = None) -> int:
    """Run the optional adapter over stdio or loopback streamable HTTP."""

    arguments = build_parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        try:
            server = build_mcp_server(service=_configured_service(arguments))
        except MCPRuntimeUnavailable as error:
            print(f"trading-harness MCP: {error}", file=sys.stderr)
            return 2
        except (
            HarnessError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ) as error:
            print(
                f"trading-harness MCP profile failed: {type(error).__name__}",
                file=sys.stderr,
            )
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
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":  # pragma: no cover - exercised by plugin launcher
    raise SystemExit(main())
