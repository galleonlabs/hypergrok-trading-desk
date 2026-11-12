#!/usr/bin/env python3
"""Show what a stop really costs on Hyperliquid, from the live book, with no keys.

A triggered stop is a market order: it fills past its trigger and pays the
taker fee. This script sizes a trade twice, on the drawn stop and on the
stressed stop the desk uses (`desk-risk-limits` section 2.2), and prints what
a triggered stop would cost each way. Like the desk, it prices the entry at
the taker rate as well, so the budget holds even when a limit entry crosses.

Stop slippage is the largest of a floor in bps of the entry price (10 by
default, the desk's convention), the current spread, and the live book's
impact for the drawn-stop size on the side the stop would hit. The drawn-stop
size is the larger of the two sizes, so the impact is measured on the order
that matters.

The script uses only the public `/info` endpoint. It never reads environment
variables, account state, wallet material or the exchange write endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, DecimalException

from cli_contract import (
    ReadOnlyParser,
    add_network_arguments,
    describe,
    emit_error,
    market_selection,
    network_settings,
)
from opening_bell import (
    COARSER_RESOLUTIONS,
    FULL_PRECISION,
    decimal,
    perp_context,
    read_json,
    read_levels,
    read_market,
    request_book,
    resolution_label,
    top_of_book,
)

USER_AGENT = "hypergrok-stop-cost/1.0"
BASE_TAKER_FEE_PCT = Decimal("0.045")
DESK_SLIP_FLOOR_BPS = Decimal("10")
MIN_ORDER_VALUE_USD = Decimal("10")
BPS = Decimal(10_000)
SAFETY = "No key requested. No account read. No order created or sent."


class UnpriceableStop(ValueError):
    """The slippage the market implies leaves no positive stop fill to size from."""


def lot(sz_decimals: int) -> Decimal:
    return Decimal(1).scaleb(-sz_decimals)


def round_down(size: Decimal, sz_decimals: int) -> Decimal:
    return size.quantize(lot(sz_decimals), rounding=ROUND_DOWN)


def walk(levels: list[dict], size: Decimal) -> tuple[Decimal, Decimal]:
    """Fill `size` against one side of the book; return (filled, notional)."""
    filled = notional = Decimal(0)
    for level in levels:
        take = min(size - filled, decimal(level.get("sz"), "book size"))
        filled += take
        notional += take * decimal(level.get("px"), "book price")
        if filled >= size:
            break
    return filled, notional


def exit_impact(read_page, coin: str, size: Decimal, long: bool) -> dict:
    """Walk the side a triggered stop hits, finest page first.

    A long's stop sells into the bids; a short's buys from the asks. Only the
    full-precision page has the real top of book, so the mid and spread come
    from it. A coarser page buckets prices, which moves a fill by up to one
    bucket, so it is read only when the finer pages run out before `size` is
    filled. When no page fills it, the impact of what was filled is a floor.
    """
    first = read_page(None)
    finest = read_levels(first, coin)
    bid, ask, mid = top_of_book(finest)
    side = 0 if long else 1
    best = None
    for n_sig_figs in (None, *COARSER_RESOLUTIONS):
        if n_sig_figs is None:
            levels = finest
        else:
            try:
                levels = read_levels(read_page(n_sig_figs), coin)
            except ValueError:
                continue
        filled, notional = walk(levels[side], size)
        if filled <= 0:
            continue
        reading = {
            "mid": mid,
            "spread_bps": (ask - bid) / mid * BPS,
            "time": first.get("time"),
            "resolution": resolution_label(n_sig_figs),
            "filled": filled,
            "complete": filled >= size,
            "impact_bps": abs(mid - notional / filled) / mid * BPS,
        }
        if reading["complete"]:
            return reading
        if best is None or filled > best["filled"]:
            best = reading
    if best is None:
        raise ValueError(f"{coin} book has no size on the {'bid' if long else 'ask'} side")
    return best


def price_stop(entry: Decimal, stop: Decimal, equity: Decimal, risk_pct: Decimal,
               taker_fee_pct: Decimal, slip_bps: Decimal, sz_decimals: int) -> dict:
    """The desk's stressed-stop arithmetic, sized both ways."""
    if entry == stop:
        raise ValueError("stop must differ from entry")
    long = stop < entry
    risk_usd = equity * risk_pct / Decimal(100)
    drawn = abs(entry - stop)
    slip = entry * slip_bps / BPS
    stop_fill = stop - slip if long else stop + slip
    if stop_fill <= 0:
        raise UnpriceableStop(f"{slip_bps:,.1f} bps of slippage leaves the stop fill at or below zero")
    fees = (entry + stop_fill) * taker_fee_pct / Decimal(100)
    stressed = abs(entry - stop_fill) + fees

    def sized(distance: Decimal) -> dict:
        size = round_down(risk_usd / distance, sz_decimals)
        loss = size * stressed
        return {
            "size": size,
            "notional": size * entry,
            "loss_if_triggered": loss,
            "loss_pct_of_equity": loss / equity * Decimal(100),
            "over_budget_usd": loss - risk_usd,
            "meets_min_order_value": size * entry >= MIN_ORDER_VALUE_USD,
            "at_least_one_lot": size >= lot(sz_decimals),
        }

    return {
        "side": "long" if long else "short",
        "risk_usd": risk_usd,
        "drawn_distance": drawn,
        "slip_bps": slip_bps,
        "slip": slip,
        "stop_fill": stop_fill,
        "fees_per_unit": fees,
        "stressed_distance": stressed,
        "drawn_stop": sized(drawn),
        "stressed_stop": sized(stressed),
    }


def fixture_reader(directory: str, coin: str):
    def read(n_sig_figs):
        name = f"l2Book-{coin}.json" if n_sig_figs is None else f"l2Book-{coin}-{n_sig_figs}sf.json"
        path = os.path.join(directory, name)
        return read_json(path) if os.path.exists(path) else None
    return read


def build_report(meta, coin: str, read_page, network: str, base_url: str, plan: dict) -> dict:
    asset = next(asset for asset in meta[0]["universe"] if asset["name"] == coin)
    sz_decimals = asset.get("szDecimals")
    # bool is an int subclass; JSON true would otherwise become a 1-decimal lot.
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 10:
        raise ValueError(f"{coin} szDecimals is missing or invalid")
    mark = decimal(perp_context(meta, coin).get("markPx"), "markPx")

    def priced_at(slip_bps: Decimal) -> dict:
        return price_stop(plan["entry"], plan["stop"], plan["equity"], plan["risk_pct"],
                          plan["taker_fee_pct"], slip_bps, sz_decimals)

    # The drawn-stop size does not depend on slippage, and it is the larger size.
    floor_priced = priced_at(plan["slip_floor_bps"])
    long = floor_priced["side"] == "long"
    walked = max(floor_priced["drawn_stop"]["size"], lot(sz_decimals))
    impact = exit_impact(read_page, coin, walked, long)
    # The first source wins a tie, so the label names the assumption the user set.
    slip_bps, slip_source = max(
        ((plan["slip_floor_bps"], "floor"), (impact["spread_bps"], "spread"), (impact["impact_bps"], "book")),
        key=lambda candidate: candidate[0],
    )
    priced = priced_at(slip_bps)

    timestamp_ms = int(impact["time"] or 0)
    if timestamp_ms <= 0:
        raise ValueError("l2Book time is missing")
    observed_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

    def strings(values: dict) -> dict:
        return {key: value if isinstance(value, bool) else str(value) for key, value in values.items()}

    return {
        "schema_version": 1,
        "mode": "read-only",
        "network": network,
        "source": {
            "endpoint": "/info",
            "requests": ["metaAndAssetCtxs", "l2Book"],
            "base_url": base_url,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        },
        "market": f"{coin}-PERP",
        "coin": coin,
        "dex": coin.split(":", 1)[0] if ":" in coin else "",
        "side": priced["side"],
        "inputs": strings({key: plan[key] for key in ("entry", "stop", "equity", "risk_pct", "taker_fee_pct", "slip_floor_bps")}),
        "book": {
            "mark": str(mark),
            "mid": str(impact["mid"]),
            "spread_bps": str(impact["spread_bps"]),
            "exit_side": "bid" if long else "ask",
            "size_walked": str(walked),
            "impact_bps": str(impact["impact_bps"]),
            "impact_complete": impact["complete"],
            "resolution": impact["resolution"],
        },
        "sizing": {
            **strings({"sz_decimals": sz_decimals, **{key: priced[key] for key in (
                "risk_usd", "drawn_distance", "slip_bps", "slip", "stop_fill", "fees_per_unit", "stressed_distance")}}),
            "slip_source": slip_source,
        },
        "drawn_stop": strings(priced["drawn_stop"]),
        "stressed_stop": strings(priced["stressed_stop"]),
        "safety": SAFETY,
    }


def fetch_report(coin: str, base_url: str, network: str, timeout: float, plan: dict,
                 fixture_dir: str | None = None, dex: str | None = None) -> dict:
    meta, coin = read_market(coin, base_url, timeout, fixture_dir, dex, USER_AGENT)
    if fixture_dir:
        read_page = fixture_reader(fixture_dir, coin)
        network, base_url = "fixture", ""
    else:
        def read_page(n_sig_figs):
            return request_book(base_url, coin, n_sig_figs, timeout, USER_AGENT)
    return build_report(meta, coin, read_page, network, base_url, plan)


def price(value: Decimal) -> str:
    """Two decimals from 1 up; four significant figures, never an exponent, below."""
    if abs(value) >= 1 or value == 0:
        return f"{value:,.2f}"
    rounded = value.quantize(Decimal(1).scaleb(value.adjusted() - 3)).normalize()
    return f"{rounded:.2f}" if rounded.as_tuple().exponent > -2 else f"{rounded:f}"


def usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def slip_label(source: str, floor: Decimal) -> str:
    if source == "book":
        return "the live book"
    if source == "spread":
        return "the current spread"
    return "the desk floor" if floor == DESK_SLIP_FLOOR_BPS else "your floor"


def render(report: dict) -> str:
    inputs, book, sizing = report["inputs"], report["book"], report["sizing"]
    d = {key: Decimal(value) for key, value in sizing.items() if key != "slip_source"}
    unit = report["coin"].split(":")[-1]
    places = int(d["sz_decimals"])
    entry, stop = Decimal(inputs["entry"]), Decimal(inputs["stop"])
    impact = Decimal(book["impact_bps"])
    complete = book["impact_complete"]
    verb = "sold into the bids" if book["exit_side"] == "bid" else "bought from the asks"
    impact_text = f"{impact:,.1f} bps from mid" if complete else f"at least {impact:,.1f} bps from mid; the book read does not hold this size"
    if book["resolution"] != FULL_PRECISION:
        impact_text += f" (read at {book['resolution']})"
    sign = "-" if report["side"] == "long" else "+"
    lines = [
        f"HYPERGROK STOP COST · {report['market']} · {report['side'].upper()}",
        f"READ ONLY · {report['network'].upper()} · {report['source']['observed_at']}",
        "Sources: Hyperliquid /info metaAndAssetCtxs + l2Book",
        "",
        f"Plan        entry {price(entry)} · stop {price(stop)} · risk {Decimal(inputs['risk_pct']):g}% of {usd(Decimal(inputs['equity']))} = {usd(d['risk_usd'])}",
        f"Book now    mark {price(Decimal(book['mark']))} · spread {Decimal(book['spread_bps']):,.1f} bps",
        f"            {Decimal(book['size_walked']):.{places}f} {unit} {verb}: {impact_text}",
        f"Stop fill   {price(d['stop_fill'])} = {price(stop)} {sign} {price(d['slip'])} slippage"
        f" ({d['slip_bps']:,.1f} bps of entry, {slip_label(sizing['slip_source'], Decimal(inputs['slip_floor_bps']))})",
        f"Fees        {price(d['fees_per_unit'])} per {unit} = ({price(entry)} + {price(d['stop_fill'])}) x {Decimal(inputs['taker_fee_pct']):g}% taker, both legs",
        f"Distance    drawn {price(d['drawn_distance'])} · stressed {price(d['stressed_distance'])} per {unit}",
        "",
    ]
    rows = []
    for label, key in (("Drawn stop", "drawn_stop"), ("Stressed stop", "stressed_stop")):
        row = {name: (value if isinstance(value, bool) else Decimal(value)) for name, value in report[key].items()}
        # The percentage is taken from the cents shown, so a reader can check it.
        over = row["over_budget_usd"].quantize(Decimal("0.01"))
        if row["over_budget_usd"] > 0:
            verdict = (f"{usd(over)} over budget (+{over / d['risk_usd'] * Decimal(100):,.1f}%)"
                       if over > 0 else "under $0.01 over budget")
        else:
            verdict = "within budget" if complete else "budget unproven: the book read is too shallow"
        rows.append((label, f"{row['size']:.{places}f} {unit}", usd(row["loss_if_triggered"]),
                     f"{row['loss_pct_of_equity']:.3f}%", verdict))
    header = ("Sized on", "size", "stop costs", "of equity", "")
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(4)]
    for row in (header, *rows):
        cells = [row[0].ljust(widths[0]), *(cell.rjust(width) for cell, width in zip(row[1:4], widths[1:]))]
        lines.append("   ".join(cells + [row[4]]).rstrip())
    stressed = report["stressed_stop"]
    if not stressed["at_least_one_lot"]:
        lines.append(f"The budget is too small for this stop: under one lot ({lot(places)} {unit}).")
    elif not stressed["meets_min_order_value"]:
        lines.append(f"The stressed size is under Hyperliquid's {usd(MIN_ORDER_VALUE_USD)} minimum order value.")
    lines.extend([
        "",
        "Size from the stressed distance and the budget means what it says.",
        "A gap through the stop can still cost more: this is a sizing input, not a promise.",
        SAFETY,
        "Facts only. This is not a trading signal.",
    ])
    return "\n".join(lines)


def number(name: str, allow_zero: bool = False):
    def parse(text: str) -> Decimal:
        try:
            value = Decimal(text)
        except DecimalException:
            raise argparse.ArgumentTypeError(f"{name} must be a number")
        if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
            raise argparse.ArgumentTypeError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
        return value
    return parse


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ReadOnlyParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                            machine_readable="--json" in argv or "--schema" in argv)
    parser.add_argument("--coin", default="ETH", help="perp coin, e.g. ETH, kPEPE or xyz:TSLA")
    parser.add_argument("--dex", help="explicit HIP-3 DEX; also inferred from a prefixed coin")
    parser.add_argument("--entry", type=number("entry"), help="planned entry price")
    parser.add_argument("--stop", type=number("stop"), help="stop trigger price; below entry is a long, above is a short")
    parser.add_argument("--equity", type=number("equity"), help="account equity in USD (typed, never read from an account)")
    parser.add_argument("--risk-pct", type=number("risk-pct"), help="risk budget as a percent of equity, e.g. 0.5")
    parser.add_argument("--taker-fee", type=number("taker-fee", allow_zero=True),
                        help=f"taker fee in percent (default {BASE_TAKER_FEE_PCT}, the base perp tier; required for HIP-3 markets)")
    parser.add_argument("--slip-bps", type=number("slip-bps", allow_zero=True), default=DESK_SLIP_FLOOR_BPS,
                        help=f"stop slippage floor in bps of entry (default {DESK_SLIP_FLOOR_BPS}); the spread and live book can raise it")
    add_network_arguments(parser)
    parser.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    if args.schema:
        schema = describe("stop_cost", {
            "schema_version": 1, "mode": "read-only", "network": "mainnet | testnet | custom | fixture",
            "coin": "canonical metadata name", "dex": "DEX name or empty string", "market": "<coin>-PERP",
            "side": "long (stop below entry) | short (stop above entry)",
            "source": "endpoint, requests, base_url, observed_at (UTC)",
            "inputs": "entry, stop, equity, risk_pct, taker_fee_pct, slip_floor_bps as typed; decimal strings",
            "book": "mark, mid, spread_bps, exit_side, size_walked, impact_bps, impact_complete, resolution; impact_complete false means impact_bps is a floor and the budget is unproven",
            "sizing": "sz_decimals, risk_usd, drawn_distance, slip_bps, slip, stop_fill, fees_per_unit, stressed_distance (decimal strings), slip_source (floor | spread | book)",
            "drawn_stop": "size, notional, loss_if_triggered, loss_pct_of_equity, over_budget_usd (decimal strings), meets_min_order_value, at_least_one_lot (booleans)",
            "stressed_stop": "same fields as drawn_stop",
            "safety": SAFETY,
        })
        schema["error"]["error"]["code"] = "invalid_arguments | source_unavailable | unpriceable_stop"
        print(json.dumps(schema, indent=2))
        return 0
    missing = [flag for flag in ("entry", "stop", "equity", "risk_pct") if getattr(args, flag) is None]
    if missing:
        parser.error("required: " + ", ".join("--" + flag.replace("_", "-") for flag in missing))
    if args.entry == args.stop:
        parser.error("stop must differ from entry")
    if args.risk_pct > 100:
        parser.error("risk-pct must be at most 100")
    if args.slip_bps >= BPS:
        parser.error("slip-bps must be under 10000")
    if args.taker_fee is not None and args.taker_fee >= 100:
        parser.error("taker-fee must be under 100 percent")
    if args.stop < args.entry and args.stop - args.entry * args.slip_bps / BPS <= 0:
        parser.error("slippage pushes the stop fill to zero or below")
    base_url, network = network_settings(args, parser)
    coin, dex = market_selection(parser, args.coin, args.dex)
    if dex and args.taker_fee is None:
        parser.error("HIP-3 markets set their own fees; pass --taker-fee")
    plan = {
        "entry": args.entry, "stop": args.stop, "equity": args.equity, "risk_pct": args.risk_pct,
        "taker_fee_pct": args.taker_fee if args.taker_fee is not None else BASE_TAKER_FEE_PCT,
        "slip_floor_bps": args.slip_bps,
    }
    try:
        report = fetch_report(coin, base_url, network, args.timeout, plan=plan,
                              fixture_dir=args.fixture_dir, dex=dex)
    except UnpriceableStop as exc:
        code, heading, error = "unpriceable_stop", "Stop cannot be priced", exc
    except (OSError, ValueError, KeyError, TypeError, DecimalException, OverflowError) as exc:
        code, heading, error = "source_unavailable", "Stop cost unavailable", exc
    else:
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
        return 0
    if args.json:
        emit_error(code, str(error))
    else:
        print(f"{heading}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
