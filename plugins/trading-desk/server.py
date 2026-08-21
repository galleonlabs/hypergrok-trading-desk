"""Launch the Trading Desk MCP server from its self-contained runtime."""

from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    plugin_root = Path(__file__).resolve().parent
    runtime_root = plugin_root / "runtime"
    runtime_package = runtime_root / "trading_harness" / "__init__.py"
    if not runtime_package.is_file() or runtime_package.is_symlink():
        print("trading-desk plugin runtime is missing or unsafe", file=sys.stderr)
        return 2
    if "trading_harness" in sys.modules:
        print("refusing a preloaded trading_harness module", file=sys.stderr)
        return 2
    sys.path.insert(0, str(runtime_root))

    from trading_harness.mcp_server import main as run_mcp

    loaded = Path(sys.modules["trading_harness"].__file__).resolve()
    if not loaded.is_relative_to((runtime_root / "trading_harness").resolve()):
        print("refusing a trading_harness module outside the plugin runtime", file=sys.stderr)
        return 2

    return run_mcp(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
