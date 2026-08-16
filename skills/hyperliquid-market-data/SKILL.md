---
name: hyperliquid-market-data
description: Read live Hyperliquid market data from the desk computer with curl or the Python SDK - mid, mark and oracle prices, order book depth, funding (current, predicted, historical), open interest, volume, candles, perp and spot metadata, margin tiers, and how to save datasets for the strategy lab. Read-only, no key. Use for any market brief, depth read, funding question or data pull.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: hyperliquid
  network-default: mainnet-for-reads
---

# Hyperliquid market data

All reads are `POST /info` with a JSON body; no key, no signing. Market data is usually read from **mainnet** even when the desk trades on testnet, because testnet prices and books are thin; say which network a figure came from. Every figure the desk reports carries source (request type), network and UTC time.

```bash
BASE=https://api.hyperliquid.xyz            # or https://api.hyperliquid-testnet.xyz
hl() { curl -sS -m 15 -X POST "$BASE/info" -H 'Content-Type: application/json' -d "$1"; }
```

Python header (SDK):

```python
from hyperliquid.info import Info
from hyperliquid.utils import constants
info = Info(constants.MAINNET_API_URL, skip_ws=True)     # TESTNET_API_URL for testnet
```

## Prices

```bash
hl '{"type":"allMids"}' | jq '{BTC, ETH, SOL}'                          # mid per coin, strings
```

`allMids` falls back to last trade when the book is empty. For mark, oracle and mid together use `metaAndAssetCtxs` below. Python: `info.all_mids()`.

## Market metadata, funding, open interest, volume

```bash
hl '{"type":"metaAndAssetCtxs"}' | jq -r '
  .[0].universe as $u | .[1] | to_entries[] | . as $e | $u[$e.key] as $m
  | select($m.name == "BTC" or $m.name == "ETH" or $m.name == "SOL")
  | [$m.name, $e.value.midPx, $e.value.markPx, $e.value.oraclePx, $e.value.funding, $e.value.openInterest, $e.value.dayNtlVlm, $e.value.premium, $m.maxLeverage, $m.szDecimals] | @tsv'
```

Fields per asset (same order as `meta.universe`): `midPx`, `markPx`, `oraclePx`, `funding` (**hourly** rate as a decimal: `0.0000125` = 0.00125%/h), `openInterest` (coin units), `dayNtlVlm` (24h USD volume), `premium` (impact bid/ask versus oracle, the input to funding), `prevDayPx`, `impactPxs`. Universe fields: `name`, `szDecimals`, `maxLeverage`, `marginTableId`, `onlyIsolated`/`marginMode`, `isDelisted`.

Python: `meta, ctxs = info.meta_and_asset_ctxs()`.

Derived numbers the desk uses (show the formula): OI notional = `openInterest x markPx`; annualised funding = `funding x 24 x 365`; 24h change = `markPx / prevDayPx - 1`.

Margin tiers (max leverage by notional) come from `meta`:

```bash
hl '{"type":"meta"}' | jq --arg c BTC '(.universe[] | select(.name==$c)) as $u | ($u.marginTableId // $u.maxLeverage) as $id
  | {name:$u.name, maxLeverage:$u.maxLeverage, marginTableId:$id,
     tiers: (if $id < 50 then [{lowerBound:"0.0", maxLeverage:$id}]
             else ((.marginTables[] | select(.[0]==$id) | .[1].marginTiers) // [{lowerBound:"0.0", maxLeverage:$u.maxLeverage}]) end)}'
```

Ids below 50 are single-tier tables whose max leverage equals the id, and they are not listed under `marginTables`, so the snippet synthesises that tier; ids of 50 and above are looked up.

## Order book and depth

```bash
hl '{"type":"l2Book","coin":"ETH"}' | jq '{time, bids: .levels[0][:5], asks: .levels[1][:5]}'
```

Up to 20 levels per side; each level is `{px, sz, n}` (`n` = number of orders). Optional `nSigFigs` (2-5) aggregates price levels; `mantissa` (1, 2 or 5) only with `nSigFigs: 5`.

Depth within a band, the way the Risk Manager and Execution Trader want it:

```bash
hl '{"type":"l2Book","coin":"ETH"}' | jq '
  (.levels[0][0].px|tonumber) as $bb | (.levels[1][0].px|tonumber) as $ba | (($bb+$ba)/2) as $mid
  | def within(side; bps): [side[] | select((((.px|tonumber) - $mid) | fabs) / $mid * 10000 <= bps) | .sz|tonumber] | add // 0;
  {mid: $mid, spread_bps: (($ba-$bb)/$mid*10000),
   bid_5bps: within(.levels[0]; 5), ask_5bps: within(.levels[1]; 5),
   bid_10bps: within(.levels[0]; 10), ask_10bps: within(.levels[1]; 10),
   bid_25bps: within(.levels[0]; 25), ask_25bps: within(.levels[1]; 25)}'
```

Expected slippage for a size: walk the relevant side accumulating `sz` until the target size is reached; report the volume-weighted price versus mid in bps. If the size exceeds the visible 20 levels, say "beyond visible depth". Python: `info.l2_snapshot("ETH")`.

Recent trades: `hl '{"type":"recentTrades","coin":"ETH"}'` (public prints: `px, sz, side, time`).

## Candles

```bash
END=$(date +%s000); START=$((END - 48*3600*1000))
hl "{\"type\":\"candleSnapshot\",\"req\":{\"coin\":\"ETH\",\"interval\":\"1h\",\"startTime\":$START,\"endTime\":$END}}" \
 | jq -r '.[] | [.t, .o, .h, .l, .c, .v, .n] | @csv'
```

Fields: `t` open time ms, `T` close time ms, `o h l c` strings, `v` base volume, `n` trade count. Intervals: `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M`. **Only the most recent 5000 candles per market and interval exist on the API**, so history depth depends on the interval: about 3.5 days of `1m`, 208 days of `1h`, 2.3 years of `4h`, 13 years of `1d`. Choose the interval to fit the history you need; requests for older candles return nothing. Python: `info.candles_snapshot(coin, interval, start_ms, end_ms)`.

Saving a dataset for the strategy lab (walks back until the API runs out):

```python
import csv, time
from hyperliquid.info import Info
from hyperliquid.utils import constants
info = Info(constants.MAINNET_API_URL, skip_ws=True)
coin, interval, days = "ETH", "4h", 365            # 4h keeps a year inside the 5000-candle ceiling
end = int(time.time() * 1000); start = end - days * 86_400_000
rows = {}
cursor_end = end
while cursor_end > start:
    batch = info.candles_snapshot(coin, interval, start, cursor_end)
    if not batch: break
    for c in batch: rows[c["t"]] = c
    oldest = min(c["t"] for c in batch)
    if oldest <= start or len(batch) < 2: break
    cursor_end = oldest - 1
path = f"/workspace/trading-desk/data/{coin}-{interval}-{days}d.csv"
with open(path, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["t","T","o","h","l","c","v","n"])
    for t in sorted(rows): c = rows[t]; w.writerow([c["t"],c["T"],c["o"],c["h"],c["l"],c["c"],c["v"],c["n"]])
print(path, len(rows), "candles", "fetched", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
```

Record the exact request (coin, interval, start, end, fetched-at, network) next to the file.

## Funding history and predictions

```bash
START=$(( $(date +%s000) - 7*86400000 ))
hl "{\"type\":\"fundingHistory\",\"coin\":\"ETH\",\"startTime\":$START}" | jq -r '.[] | [.time, .fundingRate, .premium] | @tsv'
hl '{"type":"predictedFundings"}' | jq -r '.[] | select(.[0]=="ETH") | .[1][] | [.[0], .[1].fundingRate, .[1].nextFundingTime] | @tsv'
```

`fundingHistory` returns hourly rates (up to 500 per call; paginate by `startTime`). `predictedFundings` compares venues: the `HlPerp` entry is an hourly rate; the `BinPerp`/`BybitPerp` entries are 8-hour rates, so do not compare them raw. Funding is paid every hour at `size x oracle price x hourly rate`; longs pay shorts when positive. Python: `info.funding_history(coin, start_ms)`.

## Spot markets

```bash
hl '{"type":"spotMeta"}' | jq '.universe[] | select(.name=="PURR/USDC" or .name=="@107")'
hl '{"type":"spotMetaAndAssetCtxs"}' | jq '.[1][:3]'
```

Spot pairs are named `PURR/USDC` or `@<index>` on the API (the app shows `HYPE/USDC`); `tokens: [base, quote]` indexes into `spotMeta.tokens`. A pair's asset id for orders is `10000 + universe index`; its size decimals are the **base token's** `szDecimals`. Ids differ between mainnet and testnet.

## HIP-3 builder perps

Other perp dexs exist beside the main one: `hl '{"type":"perpDexs"}'` lists them; coins are named `dex:COIN`, and `meta`, `metaAndAssetCtxs`, `clearinghouseState` accept `"dex": "<name>"`. The desk uses the default dex unless the user says otherwise.

## Brief format

Use the block in `agents/market-analyst.md`: sources and time on the first line, then facts, derived, read, unknown, next.

## Rate limits

`/info` weight per IP is 1200 per minute: `allMids`, `l2Book`, `clearinghouseState`, `orderStatus` cost 2; most others cost 20; `candleSnapshot` adds 1 per 60 candles. Batch questions, do not poll faster than the desk needs, and prefer `hyperliquid-websocket` for anything continuous. HTTP 429 means back off.

## Pitfalls

- Reporting a number without the request type, network and UTC time.
- Treating `funding` as an 8h or daily rate; it is hourly.
- Comparing `predictedFundings` venues without converting the 8h CEX rates.
- Reading a book once and calling it "the depth" ten minutes later.
- Forgetting spot `@index` naming and base-token `szDecimals`.
- Expecting deep history at fine intervals; only the latest 5000 candles per interval exist, so pick the interval to fit the lookback.
