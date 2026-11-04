#!/usr/bin/env python3
"""Read one Hyperliquid account across every perp DEX and spot, with no keys.

Hyperliquid runs the main perp DEX beside builder-deployed (HIP-3) DEXs such
as `xyz`. Each keeps its own clearinghouse, so `clearinghouseState` without a
`dex` parameter shows only the main DEX and omits HIP-3 positions and margin.
This script lists every DEX with `perpDexs`, reads `clearinghouseState` for
each one, reads spot balances, and reports positions with per-DEX and total
equity in USD.

Equity is summed only where Hyperliquid documents separate balances: the
standard account mode. There, total equity is each DEX's
`marginSummary.accountValue`, converted from the DEX's collateral token, plus
every spot balance valued at its USDC pair. Unified-account and
portfolio-margin modes share one balance between spot and perps, so the
script reports the parts without a total rather than double counting.

The account address is typed or read from HYPERGROK_ACCOUNT; nothing else is
read from the environment. The script uses only the public `/info` endpoint.
It never reads wallet material, signs, or calls the exchange write endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, DecimalException

from cli_contract import ReadOnlyParser, add_network_arguments, describe, emit_error, network_settings
from opening_bell import decimal, post_info

USER_AGENT = "hypergrok-account-read/1.0"
ADDRESS_ENV = "HYPERGROK_ACCOUNT"
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
USDC_TOKEN = 0
# Hyperliquid's account abstraction modes with separate spot and per-DEX
# balances. Any other mode shares collateral, so a sum could count it twice.
SEPARATE_MODES = ("default", "disabled")
SHARED_NOTE = ("This mode shares collateral between spot and perp DEXs; Hyperliquid documents that "
               "spotClearinghouseState then holds all balances and per-DEX user states are not meaningful, "
               "so no total is summed.")
SAFETY = "No key requested. No signature. No order, transfer or exchange request."


def mapping(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} is not an object")
    return value


def dex_names(payload) -> list[str]:
    """`perpDexs` lists `null` for the main DEX, then each builder DEX."""
    if not isinstance(payload, list) or not payload or payload[0] is not None:
        raise ValueError("perpDexs must be a list that starts with the main DEX")
    names = [""]
    for entry in payload[1:]:
        name = mapping(entry, "perpDexs entry").get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in names:
            raise ValueError("perpDexs names must be unique lowercase DEX names")
        names.append(name)
    return names


def spot_prices(payload) -> tuple[dict[int, str], dict[int, Decimal]]:
    """Token names, and a USD price for every token with a priced USDC pair."""
    if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[1], list):
        raise ValueError("spotMetaAndAssetCtxs must be [meta, contexts]")
    meta = mapping(payload[0], "spot meta")
    tokens = {mapping(token, "spot token")["index"]: str(token["name"]) for token in meta.get("tokens", [])}
    contexts = {ctx.get("coin"): ctx for ctx in payload[1] if isinstance(ctx, dict)}
    prices = {USDC_TOKEN: Decimal(1)}
    for pair in meta.get("universe", []):
        base_quote = mapping(pair, "spot pair").get("tokens")
        if not isinstance(base_quote, list) or len(base_quote) != 2 or base_quote[1] != USDC_TOKEN:
            continue
        ctx = contexts.get(pair.get("name")) or {}
        for field in ("midPx", "markPx"):
            if ctx.get(field) is not None:
                px = decimal(ctx[field], f"{pair.get('name')} {field}")
                if px > 0:
                    prices.setdefault(base_quote[0], px)
                    break
    return tokens, prices


def position(entry) -> dict:
    data = mapping(mapping(entry, "assetPositions entry").get("position"), "position")
    szi = decimal(data.get("szi"), "position szi")
    leverage = data.get("leverage") if isinstance(data.get("leverage"), dict) else {}
    liquidation = data.get("liquidationPx")
    return {
        "coin": str(data.get("coin")),
        "side": "long" if szi > 0 else "short",
        "size": str(abs(szi)),
        "entry_px": str(decimal(data.get("entryPx"), "entryPx")),
        "position_value": str(decimal(data.get("positionValue"), "positionValue")),
        "unrealized_pnl": str(decimal(data.get("unrealizedPnl"), "unrealizedPnl")),
        "margin_used": str(decimal(data.get("marginUsed"), "marginUsed")),
        "leverage_type": str(leverage.get("type", "")),
        "leverage": str(leverage.get("value", "")),
        "liquidation_px": None if liquidation is None else str(decimal(liquidation, "liquidationPx")),
    }


def read_account(read, address: str) -> dict:
    """Every request this read makes, in order; `read` posts one `/info` payload."""
    names = dex_names(read({"type": "perpDexs"}))
    mode = read({"type": "userAbstraction", "user": address})
    if not isinstance(mode, str) or not mode:
        raise ValueError("userAbstraction did not return a mode")
    states = {}
    for name in names:
        payload = {"type": "clearinghouseState", "user": address}
        if name:
            payload["dex"] = name
        states[name] = mapping(read(payload), f"clearinghouseState for {name or 'main'}")
    spot = mapping(read({"type": "spotClearinghouseState", "user": address}), "spotClearinghouseState")
    tokens, prices = spot_prices(read({"type": "spotMetaAndAssetCtxs"}))
    collateral = {}
    for name, state in states.items():
        held = decimal(mapping(state.get("marginSummary"), "marginSummary").get("accountValue"), "accountValue")
        if held != 0 or state.get("assetPositions"):
            token = mapping(read({"type": "meta", "dex": name}), f"meta for {name or 'main'}").get("collateralToken")
            if not isinstance(token, int):
                raise ValueError(f"meta for {name or 'main'} has no collateralToken")
            collateral[name] = token
    return {"names": names, "mode": mode, "states": states, "spot": spot,
            "tokens": tokens, "prices": prices, "collateral": collateral}


def build_report(raw: dict, address: str, network: str, base_url: str) -> dict:
    tokens, prices, unpriced = raw["tokens"], raw["prices"], []
    dexes, times, perp_usd = [], [], Decimal(0)
    for name in raw["names"]:
        state = raw["states"][name]
        summary = mapping(state.get("marginSummary"), "marginSummary")
        value = decimal(summary.get("accountValue"), "accountValue")
        token = raw["collateral"].get(name)
        price = prices.get(token) if token is not None else None
        value_usd = None
        if token is None:
            value_usd = Decimal(0)  # An empty DEX: no balance, no positions.
        elif price is not None:
            value_usd = value * price
        elif value != 0:
            unpriced.append(f"{name or 'main'} collateral {tokens.get(token, token)}")
        if value_usd is not None:
            perp_usd += value_usd
        if isinstance(state.get("time"), int):
            times.append(state["time"])
        dexes.append({
            "dex": name,
            "label": name or "main",
            "collateral": None if token is None else tokens.get(token, str(token)),
            "collateral_usd_price": None if price is None else str(price),
            "account_value": str(value),
            "account_value_usd": None if value_usd is None else str(value_usd),
            "total_ntl_pos": str(decimal(summary.get("totalNtlPos"), "totalNtlPos")),
            "total_margin_used": str(decimal(summary.get("totalMarginUsed"), "totalMarginUsed")),
            "withdrawable": str(decimal(state.get("withdrawable"), "withdrawable")),
            "positions": [position(entry) for entry in state.get("assetPositions") or []],
        })
    balances, spot_usd = [], Decimal(0)
    for entry in raw["spot"].get("balances") or []:
        entry = mapping(entry, "spot balance")
        total = decimal(entry.get("total"), "spot total")
        if total == 0:
            continue
        price = prices.get(entry.get("token"))
        value_usd = None if price is None else total * price
        if value_usd is None:
            unpriced.append(f"spot {entry.get('coin')}")
        else:
            spot_usd += value_usd
        balances.append({
            "coin": str(entry.get("coin")),
            "token": entry.get("token"),
            "total": str(total),
            "hold": str(decimal(entry.get("hold", "0"), "spot hold")),
            "usd_price": None if price is None else str(price),
            "value_usd": None if value_usd is None else str(value_usd),
        })
    separate = raw["mode"] in SEPARATE_MODES
    if not times:
        raise ValueError("clearinghouseState time is missing")

    def utc(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": 1,
        "mode": "read-only",
        "network": network,
        "account": address,
        "abstraction": raw["mode"],
        "source": {
            "endpoint": "/info",
            "requests": ["perpDexs", "userAbstraction", "clearinghouseState", "spotClearinghouseState",
                         "spotMetaAndAssetCtxs", "meta"],
            "clearinghouse_reads": len(raw["names"]),
            "base_url": base_url,
            "observed_from": utc(min(times)),
            "observed_to": utc(max(times)),
        },
        "dexes": dexes,
        "spot": {"balances": balances, "value_usd": str(spot_usd)},
        "equity": {
            "basis": "separate" if separate else "shared",
            "perp_usd": str(perp_usd),
            "spot_usd": str(spot_usd),
            "total_usd": str(perp_usd + spot_usd) if separate else None,
            "complete": separate and not unpriced,
            "unpriced": unpriced,
            "note": None if separate else SHARED_NOTE,
        },
        "safety": SAFETY,
    }


def fetch_report(address: str, base_url: str, network: str, timeout: float) -> dict:
    raw = read_account(lambda payload: post_info(base_url, payload, timeout, USER_AGENT), address)
    return build_report(raw, address, network, base_url)


def amount(value: str | None) -> str:
    return "unpriced" if value is None else f"{Decimal(value):,.2f}"


def usd(value: str | None) -> str:
    if value is None:
        return "unpriced"
    number = Decimal(value)
    return f"-${-number:,.2f}" if number < 0 else f"${number:,.2f}"


def render(report: dict) -> str:
    equity, spot = report["equity"], report["spot"]
    address = report["account"]
    lines = [
        f"HYPERGROK ACCOUNT READ · {address[:6]}...{address[-4:]} · {report['abstraction'].upper()} MODE",
        f"READ ONLY · {report['network'].upper()} · {report['source']['observed_to']}",
        f"Sources: Hyperliquid /info perpDexs, userAbstraction, clearinghouseState x{report['source']['clearinghouse_reads']},"
        " spotClearinghouseState, spotMetaAndAssetCtxs, meta",
        "",
    ]
    held = [dex for dex in report["dexes"] if dex["collateral"] is not None]
    rows = [("DEX", "collateral", "account value", "USD", "positions")]
    rows += [(dex["label"], dex["collateral"], amount(dex["account_value"]), usd(dex["account_value_usd"]),
              str(len(dex["positions"]))) for dex in held]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    for row in rows:
        lines.append("   ".join([row[0].ljust(widths[0]), row[1].ljust(widths[1]),
                                 *(cell.rjust(width) for cell, width in zip(row[2:], widths[2:]))]).rstrip())
    empty = len(report["dexes"]) - len(held)
    if not held:
        lines.append("No perp balance or position on any DEX.")
    if empty:
        lines.append(f"{empty} other DEX{'s' if empty != 1 else ''} empty.")
    positions = [p for dex in report["dexes"] for p in dex["positions"]]
    if positions:
        lines.extend(["", "Positions"])
        for p in positions:
            liq = "none" if p["liquidation_px"] is None else amount(p["liquidation_px"])
            pnl = Decimal(p["unrealized_pnl"])
            lines.append(f"  {p['coin']}  {p['side']} {Decimal(p['size']):f} @ {amount(p['entry_px'])}"
                         f" · value {amount(p['position_value'])} · uPnL {'+' if pnl >= 0 else '-'}{abs(pnl):,.2f}"
                         f" · {p['leverage_type']} {p['leverage']}x · liq {liq}")
    lines.extend(["", f"Spot        {usd(spot['value_usd'])} across {len(spot['balances'])} balance{'s' if len(spot['balances']) != 1 else ''}"])
    if equity["unpriced"]:
        lines.append(f"Unpriced    {', '.join(equity['unpriced'])} (no USDC pair price; left out of the sum)")
    if equity["basis"] == "separate":
        prefix = "" if equity["complete"] else "at least "
        lines.append(f"Equity      perps {usd(equity['perp_usd'])} + spot {usd(equity['spot_usd'])} = {prefix}{usd(equity['total_usd'])}")
    else:
        lines.append(f"Equity      not summed: {equity['note']}")
    lines.extend(["", "Reads are sequential, not one atomic snapshot. Staking, vaults and sub-accounts are not included.",
                  SAFETY, "Facts only. This is not a trading signal."])
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ReadOnlyParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                            machine_readable="--json" in argv or "--schema" in argv)
    parser.add_argument("--address", help=f"account (main wallet) address; defaults to ${ADDRESS_ENV}")
    add_network_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(describe("account_read", {
            "schema_version": 1, "mode": "read-only", "network": "mainnet | testnet | custom",
            "account": "the address read", "abstraction": "userAbstraction mode, e.g. default | unifiedAccount | portfolioMargin",
            "source": "endpoint, requests, clearinghouse_reads, base_url, observed_from and observed_to (UTC clearinghouse times)",
            "dexes": "one entry per perpDexs DEX, main first: dex ('' for main), label, collateral (null when empty and unread), "
                     "collateral_usd_price, account_value, account_value_usd (null when unpriced), total_ntl_pos, total_margin_used, "
                     "withdrawable, positions[coin, side, size, entry_px, position_value, unrealized_pnl, margin_used, "
                     "leverage_type, leverage, liquidation_px]",
            "spot": "balances[coin, token, total, hold, usd_price, value_usd] for nonzero balances; value_usd of priced balances",
            "equity": "basis (separate | shared), perp_usd, spot_usd, total_usd (null when basis is shared), complete, unpriced, note",
            "safety": SAFETY,
        }), indent=2))
        return 0
    address = (args.address or os.environ.get(ADDRESS_ENV, "")).strip()
    if not address:
        parser.error(f"required: --address or {ADDRESS_ENV}")
    if not ADDRESS_RE.fullmatch(address):
        parser.error("address must be 0x followed by 40 hex characters")
    base_url, network = network_settings(args, parser)
    try:
        report = fetch_report(address.lower(), base_url, network, args.timeout)
    except (OSError, ValueError, KeyError, TypeError, DecimalException, OverflowError) as exc:
        if args.json:
            emit_error("source_unavailable", str(exc))
        else:
            print(f"Account read unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
