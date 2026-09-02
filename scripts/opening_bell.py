#!/usr/bin/env python3
"""Print a zero-key Hyperliquid market snapshot for a new HyperGrok desk.

The script uses only the public `/info` endpoint. It never reads environment
variables, account state, wallet material or the exchange write endpoint.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

DEFAULT_BASE_URL = "https://api.hyperliquid.xyz"
BANDS_BPS = (5, 10, 25)
# `l2Book` returns at most this many price levels per side. A side that comes
# back full is a page, not a book: it stops wherever the twentieth level sits,
# which on a liquid perp is a few bps from the mid. Depth asked for beyond that
# point is a floor, and printing it as a total makes a deep book look flat.
LEVEL_CAP = 20


def post_info(base_url: str, payload: dict, timeout: float = 15.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/info",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "hypergrok-opening-bell/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def load_fixture(directory: str, request_type: str, coin: str):
    name = "metaAndAssetCtxs.json" if request_type == "metaAndAssetCtxs" else f"l2Book-{coin}.json"
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return json.load(handle)


def decimal(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} is not finite")
    return number


def depth_within(levels: list[dict], mid: Decimal, band_bps: int) -> Decimal:
    limit = Decimal(band_bps) / Decimal(10_000)
    total = Decimal(0)
    for level in levels:
        px = decimal(level.get("px"), "book price")
        size = decimal(level.get("sz"), "book size")
        if abs(px - mid) / mid <= limit:
            total += size
    return total


def reach_bps(levels: list[dict], mid: Decimal) -> Decimal:
    """How far from the mid the furthest returned level on this side sits."""
    return max(abs(decimal(level.get("px"), "book price") - mid) for level in levels) / mid * Decimal(10_000)


def covers(levels: list[dict], reach: Decimal, band_bps: int) -> bool:
    """True when the band is measured, rather than cut off by the level cap."""
    return len(levels) < LEVEL_CAP or Decimal(band_bps) <= reach


def build_snapshot(meta_and_contexts, book: dict, coin: str, network: str) -> dict:
    if not isinstance(meta_and_contexts, list) or len(meta_and_contexts) != 2:
        raise ValueError("metaAndAssetCtxs returned an unexpected shape")
    universe = meta_and_contexts[0].get("universe") or []
    contexts = meta_and_contexts[1]
    index = next((i for i, asset in enumerate(universe) if asset.get("name") == coin), None)
    if index is None or index >= len(contexts):
        raise ValueError(f"{coin} is not in the default perp universe")

    context = contexts[index]
    levels = book.get("levels") or []
    if len(levels) != 2 or not levels[0] or not levels[1]:
        raise ValueError(f"{coin} order book has no two-sided depth")

    best_bid = decimal(levels[0][0].get("px"), "best bid")
    best_ask = decimal(levels[1][0].get("px"), "best ask")
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        raise ValueError("order book top is invalid")
    book_mid = (best_bid + best_ask) / Decimal(2)

    mark = decimal(context.get("markPx"), "markPx")
    previous = decimal(context.get("prevDayPx"), "prevDayPx")
    funding = decimal(context.get("funding"), "funding")
    open_interest = decimal(context.get("openInterest"), "openInterest")
    timestamp_ms = int(book.get("time", 0))
    if timestamp_ms <= 0:
        raise ValueError("l2Book time is missing")
    observed_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

    bid_reach = reach_bps(levels[0], book_mid)
    ask_reach = reach_bps(levels[1], book_mid)
    depth = {}
    for band in BANDS_BPS:
        bids = depth_within(levels[0], book_mid, band)
        asks = depth_within(levels[1], book_mid, band)
        depth[str(band)] = {
            "bid_base": str(bids),
            "ask_base": str(asks),
            "bid_usd": str(bids * book_mid),
            "ask_usd": str(asks * book_mid),
            "bid_complete": covers(levels[0], bid_reach, band),
            "ask_complete": covers(levels[1], ask_reach, band),
        }

    return {
        "mode": "read-only",
        "network": network,
        "source": {
            "endpoint": "/info",
            "requests": ["metaAndAssetCtxs", "l2Book"],
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        },
        "market": f"{coin}-PERP",
        "prices": {
            "book_mid": str(book_mid),
            "mark": str(mark),
            "oracle": str(decimal(context.get("oraclePx"), "oraclePx")),
            "previous_day": str(previous),
            "change_24h_pct": str((mark / previous - 1) * Decimal(100)),
        },
        "funding": {
            "hourly_rate": str(funding),
            "hourly_pct": str(funding * Decimal(100)),
            "annualized_simple_pct": str(funding * Decimal(24 * 365 * 100)),
        },
        "activity": {
            "open_interest_base": str(open_interest),
            "open_interest_usd": str(open_interest * mark),
            "volume_24h_usd": str(decimal(context.get("dayNtlVlm"), "dayNtlVlm")),
        },
        "book": {
            "best_bid": str(best_bid),
            "best_ask": str(best_ask),
            "spread_bps": str((best_ask - best_bid) / book_mid * Decimal(10_000)),
            "levels_returned": {"bid": len(levels[0]), "ask": len(levels[1])},
            "visible_reach_bps": {"bid": str(bid_reach), "ask": str(ask_reach)},
            "depth": depth,
        },
        "safety": "No key requested. No account read. No order created or sent.",
    }


def fetch_snapshot(coin: str, base_url: str, timeout: float, fixture_dir: str | None = None) -> dict:
    if fixture_dir:
        meta = load_fixture(fixture_dir, "metaAndAssetCtxs", coin)
        book = load_fixture(fixture_dir, "l2Book", coin)
        network = "fixture"
    else:
        meta = post_info(base_url, {"type": "metaAndAssetCtxs"}, timeout)
        book = post_info(base_url, {"type": "l2Book", "coin": coin}, timeout)
        network = "testnet" if "testnet" in base_url.lower() else "mainnet"
    return build_snapshot(meta, book, coin, network)


def compact(number: Decimal, prefix: str = "") -> str:
    absolute = abs(number)
    for threshold, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "K")):
        if absolute >= threshold:
            return f"{prefix}{number / threshold:,.2f}{suffix}"
    return f"{prefix}{number:,.2f}"


def amount(number: Decimal, complete: bool) -> str:
    """A band the level cap cut off is a floor; say so rather than implying a total."""
    return compact(number, "$") if complete else f">= {compact(number, '$')}"


def render(snapshot: dict) -> str:
    price = snapshot["prices"]
    funding = snapshot["funding"]
    activity = snapshot["activity"]
    book = snapshot["book"]
    lines = [
        f"HYPERGROK OPENING BELL — {snapshot['market']}",
        f"READ ONLY · {snapshot['network'].upper()} · {snapshot['source']['observed_at']}",
        "Sources: Hyperliquid /info metaAndAssetCtxs + l2Book",
        "",
        f"Price       mid {Decimal(price['book_mid']):,.2f} · mark {Decimal(price['mark']):,.2f} · oracle {Decimal(price['oracle']):,.2f}",
        f"24h change  {Decimal(price['change_24h_pct']):+,.2f}%",
        f"Funding     {Decimal(funding['hourly_pct']):+,.5f}%/h · {Decimal(funding['annualized_simple_pct']):+,.2f}% annualized (simple)",
        f"Open int.   {compact(Decimal(activity['open_interest_base']))} {snapshot['market'].removesuffix('-PERP')} · {compact(Decimal(activity['open_interest_usd']), '$')}",
        f"24h volume  {compact(Decimal(activity['volume_24h_usd']), '$')}",
        f"Spread      {Decimal(book['spread_bps']):,.3f} bps",
        "",
        "Visible depth from the book mid",
    ]
    for band in BANDS_BPS:
        row = book["depth"][str(band)]
        bid = amount(Decimal(row["bid_usd"]), row["bid_complete"])
        ask = amount(Decimal(row["ask_usd"]), row["ask_complete"])
        lines.append(f"  {band:>2} bps     bid {bid:>13} · ask {ask:>13}")
    reach = book["visible_reach_bps"]
    if any(not row[side] for row in book["depth"].values() for side in ("bid_complete", "ask_complete")):
        lines.append(
            f"  The book returned {book['levels_returned']['bid']}/{book['levels_returned']['ask']} levels, reaching"
            f" {Decimal(reach['bid']):,.1f} bps bid and {Decimal(reach['ask']):,.1f} bps ask."
        )
        lines.append("  Wider bands are floors, not totals: depth beyond that point is not in this response.")
    lines.extend(["", snapshot["safety"], "Facts only. This snapshot is not a trading signal."])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="ETH", help="default perp coin, e.g. ETH or BTC")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    coin = args.coin.strip().upper()
    if not coin or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:@_-" for character in coin):
        parser.error("coin contains unsupported characters")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive")
    try:
        snapshot = fetch_snapshot(coin, args.base_url, args.timeout, args.fixture_dir)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Opening Bell unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot, indent=2, sort_keys=True) if args.json else render(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
