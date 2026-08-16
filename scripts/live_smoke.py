"""Read-only live smoke checks for every public HyperGrok data source."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from hypergrok.api import coingecko_coin, defillama_protocol, request_json

NETWORKS = {
    "testnet": "https://api.hyperliquid-testnet.xyz",
    "mainnet": "https://api.hyperliquid.xyz",
}


def main() -> int:
    checks: dict[str, object] = {}
    for network, base_url in NETWORKS.items():
        mids = request_json(
            f"{base_url}/info",
            method="POST",
            payload={"type": "allMids"},
        )
        if not isinstance(mids, dict) or "BTC" not in mids:
            raise RuntimeError(f"{network} allMids returned an unusable response")
        checks[f"hyperliquid_{network}"] = {"markets": len(mids), "btc": mids["BTC"]}

    llama = defillama_protocol("hyperliquid")
    if not isinstance(llama, dict) or not llama.get("name"):
        raise RuntimeError("DefiLlama returned an unusable response")
    checks["defillama"] = {"name": llama["name"]}

    gecko = coingecko_coin("hyperliquid")
    if not isinstance(gecko, dict) or gecko.get("id") != "hyperliquid":
        raise RuntimeError("CoinGecko returned an unusable response")
    checks["coingecko"] = {"id": gecko["id"], "symbol": gecko.get("symbol")}

    print(json.dumps({"observed_at": datetime.now(UTC).isoformat(), "checks": checks}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
