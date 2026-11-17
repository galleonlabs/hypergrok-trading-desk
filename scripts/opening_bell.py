#!/usr/bin/env python3
"""Print a zero-key Hyperliquid market snapshot for a new HyperGrok desk.

The script uses only the public `/info` endpoint. It never reads environment
variables, account state, wallet material or the exchange write endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation

from cli_contract import (
    MAINNET,
    ReadOnlyParser,
    add_network_arguments,
    describe,
    emit_error,
    failure_text,
    market_selection,
    network_label,
    network_settings,
    price,
)

DEFAULT_BASE_URL = MAINNET
USER_AGENT = "hypergrok-opening-bell/1.0"
BANDS_BPS = (5, 10, 25)
# `l2Book` returns at most this many price levels per side, at whatever price
# resolution was asked for. At full resolution twenty levels stop a few bps from
# the mid on a liquid perp - about 8 on ETH, under 3 on BTC - so the wider bands
# are cut off rather than measured. `nSigFigs` buckets the same book into coarser
# prices and pushes the page out past 25 bps, at the cost of moving a band edge by
# up to one bucket, so every band is read from the finest page that reaches it
# (the method `hyperliquid-market-data` gives the Market Analyst).
LEVEL_CAP = 20
FULL_PRECISION = "full precision"
COARSER_RESOLUTIONS = (4, 3)


def post_info(base_url: str, payload: dict, timeout: float = 15.0, user_agent: str = USER_AGENT):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/info",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_fixture(directory: str, request_type: str, coin: str):
    name = "metaAndAssetCtxs.json" if request_type == "metaAndAssetCtxs" else f"l2Book-{coin}.json"
    return read_json(os.path.join(directory, name))


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


def resolution_label(n_sig_figs: int | None) -> str:
    return FULL_PRECISION if n_sig_figs is None else f"{n_sig_figs} sig figs"


def read_levels(payload, coin: str) -> list[list[dict]]:
    """`l2Book` answers `null` for a coin or resolution it cannot page."""
    levels = payload.get("levels") if isinstance(payload, dict) else None
    if not isinstance(levels, list) or len(levels) != 2 or not levels[0] or not levels[1]:
        raise ValueError(f"{coin} order book has no two-sided depth")
    for side in levels:
        if not isinstance(side, list) or not all(isinstance(level, dict) for level in side):
            raise ValueError("order book levels must be objects")
        for level in side:
            if decimal(level.get("px"), "book price") <= 0 or decimal(level.get("sz"), "book size") < 0:
                raise ValueError("order book prices must be positive and sizes nonnegative")
    return levels


def top_of_book(levels: list[list[dict]]) -> tuple[Decimal, Decimal, Decimal]:
    best_bid = decimal(levels[0][0].get("px"), "best bid")
    best_ask = decimal(levels[1][0].get("px"), "best ask")
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        raise ValueError("order book top is invalid")
    return best_bid, best_ask, (best_bid + best_ask) / Decimal(2)


def page_view(n_sig_figs: int | None, payload: dict, coin: str, mid: Decimal) -> dict:
    """A book page reduced to what a band measurement needs, against the true mid."""
    levels = read_levels(payload, coin)
    return {
        "resolution": resolution_label(n_sig_figs),
        "levels": levels,
        "reach": (reach_bps(levels[0], mid), reach_bps(levels[1], mid)),
        "returned": (len(levels[0]), len(levels[1])),
    }


def page_views(pages: list[tuple[int | None, dict]], coin: str, mid: Decimal) -> list[dict]:
    """Views of every usable page, finest first.

    The full-precision page has to parse - it is the snapshot. A coarser page that
    came back empty is dropped rather than fatal: the bands it would have measured
    fall back to floors, which is the answer without it.
    """
    views = [page_view(pages[0][0], pages[0][1], coin, mid)]
    for n_sig_figs, payload in pages[1:]:
        try:
            views.append(page_view(n_sig_figs, payload, coin, mid))
        except ValueError:
            continue
    return views


def measures(views: list[dict], band_bps: int) -> bool:
    """True when some page already reaches this band on both sides."""
    return all(
        any(covers(view["levels"][side], view["reach"][side], band_bps) for view in views)
        for side in (0, 1)
    )


def request_book(base_url: str, coin: str, n_sig_figs: int | None, timeout: float,
                 user_agent: str = USER_AGENT) -> dict:
    payload = {"type": "l2Book", "coin": coin}
    if n_sig_figs is not None:
        payload["nSigFigs"] = n_sig_figs
    return post_info(base_url, payload, timeout, user_agent)


def fetch_books(coin: str, base_url: str, timeout: float) -> list[tuple[int | None, dict]]:
    """Page the book coarser until the widest band is measured rather than cut off.

    The full-precision page is always read first: it is the only one whose top of
    book and spread are the real ones. Coarser pages are requested only while a
    band would otherwise be quoted as a floor.
    """
    pages = [(None, request_book(base_url, coin, None, timeout))]
    mid = top_of_book(read_levels(pages[0][1], coin))[2]
    for n_sig_figs in COARSER_RESOLUTIONS:
        if measures(page_views(pages, coin, mid), max(BANDS_BPS)):
            break
        pages.append((n_sig_figs, request_book(base_url, coin, n_sig_figs, timeout)))
    return pages


def load_books(directory: str, coin: str) -> list[tuple[int | None, dict]]:
    """The fixture equivalent of `fetch_books`; coarser pages are optional files."""
    pages = [(None, load_fixture(directory, "l2Book", coin))]
    for n_sig_figs in COARSER_RESOLUTIONS:
        path = os.path.join(directory, f"l2Book-{coin}-{n_sig_figs}sf.json")
        if os.path.exists(path):
            pages.append((n_sig_figs, read_json(path)))
    return pages


def metadata_rows(meta_and_contexts):
    if not isinstance(meta_and_contexts, list) or len(meta_and_contexts) != 2 or not isinstance(meta_and_contexts[0], dict):
        raise ValueError("metaAndAssetCtxs returned an unexpected shape")
    universe = meta_and_contexts[0].get("universe")
    contexts = meta_and_contexts[1]
    if not isinstance(universe, list) or not isinstance(contexts, list) or len(universe) != len(contexts):
        raise ValueError("market metadata and contexts must be aligned lists")
    if not all(isinstance(asset, dict) and isinstance(asset.get("name"), str) for asset in universe):
        raise ValueError("market metadata must contain named objects")
    return universe, contexts


def perp_context(meta_and_contexts, coin: str) -> dict:
    universe, contexts = metadata_rows(meta_and_contexts)
    index = next((i for i, asset in enumerate(universe) if asset.get("name") == coin), None)
    if index is None or index >= len(contexts):
        raise ValueError(f"{coin} is not in the default perp universe or selected DEX")
    if universe[index].get("isDelisted"):
        raise ValueError(f"{coin} is delisted")
    if not isinstance(contexts[index], dict):
        raise ValueError(f"{coin} context must be an object")
    return contexts[index]


def resolve_coin(meta_and_contexts, requested: str, dex: str | None = None) -> str:
    universe, _ = metadata_rows(meta_and_contexts)
    target = f"{dex}:{requested}" if dex and ":" not in requested else requested
    matches = [asset.get("name") for asset in universe
               if isinstance(asset, dict) and isinstance(asset.get("name"), str)
               and asset["name"].casefold() == target.casefold()]
    if target in matches:
        matches = [target]  # An exact spelling wins over a case-insensitive twin.
    if not matches:
        where = f"on the {dex} DEX" if dex else "in the main perp universe; a HIP-3 market needs its DEX prefix, e.g. xyz:TSLA"
        raise ValueError(f"{target} is not listed {where}")
    if len(matches) > 1:
        raise ValueError(f"{target} matches more than one market ({', '.join(matches)}); use the exact spelling")
    coin = matches[0]
    if dex and not coin.startswith(dex + ":"):
        raise ValueError("market does not belong to the selected DEX")
    perp_context(meta_and_contexts, coin)
    return coin


def build_snapshot(meta_and_contexts, pages: list[tuple[int | None, dict]], coin: str, network: str) -> dict:
    context = perp_context(meta_and_contexts, coin)
    asset = next(asset for asset in meta_and_contexts[0]["universe"] if asset["name"] == coin)
    if not pages:
        raise ValueError(f"{coin} order book was not returned")
    finest = read_levels(pages[0][1], coin)
    best_bid, best_ask, book_mid = top_of_book(finest)

    mark = decimal(context.get("markPx"), "markPx")
    previous = decimal(context.get("prevDayPx"), "prevDayPx")
    if mark <= 0 or previous <= 0:
        raise ValueError("markPx and prevDayPx must be positive")
    funding = decimal(context.get("funding"), "funding")
    open_interest = decimal(context.get("openInterest"), "openInterest")
    timestamp_ms = int(pages[0][1].get("time", 0))
    if timestamp_ms <= 0:
        raise ValueError("l2Book time is missing")
    observed_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

    views = page_views(pages, coin, book_mid)
    depth = {}
    for band in BANDS_BPS:
        row = {}
        for side, name in ((0, "bid"), (1, "ask")):
            # The finest page that reaches this band measures it. When none does,
            # the page that reaches furthest gives the largest honest floor.
            view = next(
                (v for v in views if covers(v["levels"][side], v["reach"][side], band)),
                max(views, key=lambda v: v["reach"][side]),
            )
            size = depth_within(view["levels"][side], book_mid, band)
            row[f"{name}_base"] = str(size)
            row[f"{name}_usd"] = str(size * book_mid)
            row[f"{name}_complete"] = covers(view["levels"][side], view["reach"][side], band)
            row[f"{name}_source"] = view["resolution"]
        depth[str(band)] = row

    return {
        "schema_version": 1,
        "mode": "read-only",
        "network": network,
        "source": {
            "endpoint": "/info",
            "requests": ["metaAndAssetCtxs", "l2Book"],
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        },
        "market": f"{coin}-PERP",
        "coin": coin,
        "dex": coin.split(":", 1)[0] if ":" in coin else "",
        "metadata": {key: asset.get(key) for key in ("szDecimals", "maxLeverage", "onlyIsolated", "marginTableId")},
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
            "pages": [
                {
                    "resolution": view["resolution"],
                    "levels_returned": {"bid": view["returned"][0], "ask": view["returned"][1]},
                    "reach_bps": {"bid": str(view["reach"][0]), "ask": str(view["reach"][1])},
                }
                for view in views
            ],
            "depth": depth,
        },
        "safety": "No key requested. No account read. No order created or sent.",
    }


def read_market(coin: str, base_url: str, timeout: float, fixture_dir: str | None = None,
                dex: str | None = None, user_agent: str = USER_AGENT):
    """Market metadata and the canonical coin, resolved before any book is requested."""
    if fixture_dir:
        meta = load_fixture(fixture_dir, "metaAndAssetCtxs", coin)
    else:
        request = {"type": "metaAndAssetCtxs"}
        if dex:
            request["dex"] = dex
        meta = post_info(base_url, request, timeout, user_agent)
    return meta, resolve_coin(meta, coin, dex)


def fetch_snapshot(coin: str, base_url: str, timeout: float, fixture_dir: str | None = None, dex: str | None = None) -> dict:
    meta, coin = read_market(coin, base_url, timeout, fixture_dir, dex)
    if fixture_dir:
        pages = load_books(fixture_dir, coin)
        network = "fixture"
    else:
        pages = fetch_books(coin, base_url, timeout)
        network = network_label(base_url)
    snapshot = build_snapshot(meta, pages, coin, network)
    snapshot["source"]["base_url"] = base_url
    return snapshot


def compact(number: Decimal, prefix: str = "") -> str:
    absolute = abs(number)
    for threshold, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "K")):
        if absolute >= threshold:
            return f"{prefix}{number / threshold:,.2f}{suffix}"
    return f"{prefix}{number:,.2f}"


def amount(number: Decimal, complete: bool) -> str:
    """A band no page reaches is a floor; say so rather than implying a total."""
    return compact(number, "$") if complete else f">= {compact(number, '$')}"


def source_note(row: dict) -> str:
    """Name the page a band was read from, because bucketing moves the band edges."""
    bid, ask = row["bid_source"], row["ask_source"]
    if bid == ask:
        return "" if bid == FULL_PRECISION else f"  ({bid})"
    return f"  ({bid} bid / {ask} ask)"


def render(snapshot: dict) -> str:
    prices = snapshot["prices"]
    funding = snapshot["funding"]
    activity = snapshot["activity"]
    book = snapshot["book"]
    lines = [
        f"HYPERGROK OPENING BELL · {snapshot['market']}",
        f"READ ONLY · {snapshot['network'].upper()} · {snapshot['source']['observed_at']}",
        "Sources: Hyperliquid /info metaAndAssetCtxs + l2Book",
        "",
        f"Price       mid {price(Decimal(prices['book_mid']))} · mark {price(Decimal(prices['mark']))} · oracle {price(Decimal(prices['oracle']))}",
        f"24h change  {Decimal(prices['change_24h_pct']):+,.2f}%",
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
        lines.append(f"  {band:>2} bps     bid {bid:>13} · ask {ask:>13}{source_note(row)}")
    pages = book["pages"]
    if len(pages) > 1:
        lines.append(
            f"  {LEVEL_CAP} levels a side stop short of the wider bands, so the book was re-read at"
            f" {' then '.join(page['resolution'] for page in pages[1:])}."
        )
        lines.append("  A coarser page buckets prices, so its band edges move by up to one bucket.")
    if any(not row[side] for row in book["depth"].values() for side in ("bid_complete", "ask_complete")):
        reach = {side: max(Decimal(page["reach_bps"][side]) for page in pages) for side in ("bid", "ask")}
        lines.append(
            f"  No page read reaches past {reach['bid']:,.1f} bps bid or {reach['ask']:,.1f} bps ask."
        )
        lines.append("  Wider bands are floors, not totals: depth beyond that point is not in this response.")
    lines.extend(["", snapshot["safety"], "Facts only. This snapshot is not a trading signal."])
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ReadOnlyParser(description=__doc__, machine_readable="--json" in argv or "--schema" in argv)
    parser.add_argument("--coin", default="ETH", help="perp coin, e.g. ETH, kPEPE or xyz:TSLA")
    parser.add_argument("--dex", help="explicit HIP-3 DEX; also inferred from a prefixed coin")
    add_network_arguments(parser)
    parser.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(describe("opening_bell", {
            "schema_version": 1, "mode": "read-only", "network": "mainnet | testnet | custom | fixture",
            "coin": "canonical metadata name", "dex": "DEX name or empty string", "market": "<coin>-PERP",
            "metadata": "szDecimals, maxLeverage, onlyIsolated, marginTableId; null means not provided",
            "source": "endpoint, requests, base_url, observed_at (UTC)",
            "prices": "decimal strings", "funding": "decimal strings", "activity": "decimal strings",
            "book": "pages, spread and depth; inspect each bid_complete/ask_complete before calling a band measured",
            "safety": "No key requested. No account read. No order created or sent."
        }), indent=2))
        return 0
    base_url, _ = network_settings(args, parser)
    coin, dex = market_selection(parser, args.coin, args.dex)
    try:
        snapshot = fetch_snapshot(coin, base_url, args.timeout, args.fixture_dir, dex)
    except (OSError, ValueError, KeyError, TypeError, DecimalException, OverflowError) as exc:
        if args.json:
            emit_error("source_unavailable", failure_text(exc))
        else:
            print(f"Opening Bell unavailable: {failure_text(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot, indent=2, sort_keys=True) if args.json else render(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
