---
name: hyperliquid-market-data
description: Read live Hyperliquid market data from the desk computer with curl or the Python SDK - mid, mark and oracle prices, order book depth, funding (current, predicted, historical), open interest, volume, candles, perp and spot metadata, margin tiers, and how to save datasets for the strategy lab. Read-only, no key. Use for any market brief, depth read, funding question or data pull.
license: MIT
metadata:
  version: "1.2.0"
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

## Read-only snapshot contract

Run `python3 scripts/opening_bell.py --schema` or `python3 scripts/desk_doctor.py --schema` to inspect their machine-readable contracts without file or network reads. With `--json`, successful output carries `schema_version: 1` and `mode: read-only`; invalid arguments return a JSON `error` with exit 2, while unavailable snapshot data or failed doctor checks exit 1. Decimal quantities remain strings. A depth floor is still a floor: inspect each `bid_complete`/`ask_complete` flag, and do not promote a doctor's `WARN` or offline `SKIP` to trading readiness.

```bash
python3 scripts/opening_bell.py --coin ETH --json
python3 scripts/opening_bell.py --coin kPEPE --json
python3 scripts/opening_bell.py --coin xyz:TSLA --json
python3 scripts/opening_bell.py --dex xyz --coin TSLA --testnet --json
```

A HIP-3 snapshot requests `metaAndAssetCtxs` with the explicit DEX and `l2Book` with the canonical prefixed coin from that universe. It retains the metadata's spelling and reports `coin`, `dex`, precision and leverage metadata; unknown, ambiguous or delisted markets fail before the book request. Metadata fields absent from the response are `null`, never invented. `--testnet` selects the official testnet origin; an explicit different `--base-url` is labelled `custom` and included in the source. Read-only discovery does not enable HIP-3 execution or transfer collateral; the desk's opt-in and ticket rules still apply.

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

**Twenty levels is a page, not the book.** On a liquid perp those levels stop a few bps from the mid - around 8 bps on ETH and under 3 bps on BTC at normal spreads - so a 25 bps band summed from the default response is whatever the page happened to contain, not the depth within 25 bps. Read the reach before quoting a band: when the side came back with 20 levels and the furthest one is nearer than the band, the number is a floor. To reach further, re-request with `nSigFigs: 4`, which buckets prices coarsely enough to push 20 levels out to roughly 20-25 bps on a major perp (measured live: 82 bps ETH, 25 BTC, 24 HYPE, 20 SOL), and drop to `nSigFigs: 3` when even that stops short. Read each band off the finest page that reaches it - the coarse page moves the band edges by up to one bucket, and its top of book is not the real one - and say which page a figure came from. `scripts/opening_bell.py` is that ladder in code.

Depth within a band, the way the Risk Manager and Execution Trader want it:

```bash
hl '{"type":"l2Book","coin":"ETH"}' | jq '
  (.levels[0][0].px|tonumber) as $bb | (.levels[1][0].px|tonumber) as $ba | (($bb+$ba)/2) as $mid
  | def within(side; bps): [side[] | select((((.px|tonumber) - $mid) | fabs) / $mid * 10000 <= bps) | .sz|tonumber] | add // 0;
    def reach(side): (((side[-1].px|tonumber) - $mid) | fabs) / $mid * 10000;
  {mid: $mid, spread_bps: (($ba-$bb)/$mid*10000),
   levels: [(.levels[0]|length), (.levels[1]|length)], reach_bps: [reach(.levels[0]), reach(.levels[1])],
   bid_5bps: within(.levels[0]; 5), ask_5bps: within(.levels[1]; 5),
   bid_10bps: within(.levels[0]; 10), ask_10bps: within(.levels[1]; 10),
   bid_25bps: within(.levels[0]; 25), ask_25bps: within(.levels[1]; 25)}'
```

`reach_bps` is the answer's own scope: any band wider than it, on a side that returned 20 levels, is a floor and is quoted as `>= size`. Two bands reporting the same total is that cut-off, not a flat book.

Expected slippage for a size: walk the relevant side accumulating `sz` until the target size is reached; report the volume-weighted price versus mid in bps. If the size exceeds the visible 20 levels, say "beyond visible depth". Python: `info.l2_snapshot("ETH")`.

Recent trades: `hl '{"type":"recentTrades","coin":"ETH"}'` (public prints: `px, sz, side, time`).

## Candles

```bash
END=$(date +%s000); START=$((END - 48*3600*1000))
hl "{\"type\":\"candleSnapshot\",\"req\":{\"coin\":\"ETH\",\"interval\":\"1h\",\"startTime\":$START,\"endTime\":$END}}" \
 | jq -r '.[] | [.t, .o, .h, .l, .c, .v, .n] | @csv'
```

Fields: `t` open time ms, `T` close time ms, `o h l c` strings, `v` base volume, `n` trade count. Intervals: `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M`. **Only the most recent 5000 candles per market and interval exist on the API**, so history depth depends on the interval: about 3.5 days of `1m`, 208 days of `1h`, 2.3 years of `4h`, 13 years of `1d`. Choose the interval to fit the history you need; requests for older candles return nothing. Python: `info.candles_snapshot(coin, interval, start_ms, end_ms)`.

For the strategy lab, use a research bundle instead of saving an unverified CSV:

```bash
cd /workspace/hypergrok
python3 scripts/research_dataset.py --schema
python3 scripts/research_dataset.py --coin ETH --interval 4h \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --with-funding --output /workspace/trading-desk/data/eth-september --json
python3 scripts/research_dataset.py --verify /workspace/trading-desk/data/eth-september --json
```

Choose explicit aligned UTC boundaries for the question, with end no later than collection time. The tool uses `[start,end)`, closed candles only, at most 5,000 requested slots and fixed intervals from `1m` through `1d` (excluding `3d`, `1w`, `1M`). With funding, boundaries must also be whole hours. It records canonical perp/DEX identity, origin/network, requests, decoded responses and retrieval times in `sources.json`; saves observed CSV rows; and writes a hashed manifest with `complete`, `incomplete` or `unavailable` coverage. It refuses an existing output directory. `--schema` and `--verify` do not use the network.

Return the bundle path, manifest hash, requested range, coverage gaps and offline verification result. Exit zero requires complete coverage, including funding if requested; an intact incomplete bundle still exits nonzero. No gap is filled, no absent rate becomes zero, and conflicting duplicates fail. Current metadata may include a delisted perp for historical research, flagged in the bundle; this does not enable trading it. A selected HIP-3 prefix/`--dex`, testnet or custom origin remains explicit. Spot is not supported by this collector.

See [Research datasets](../../docs/RESEARCH_DATASETS.md) for request bounds, pagination and handoff details. Replay and hashes establish internal consistency, not source authenticity, historical universe completeness, strategy edge or Risk PASS. Never describe a requested year as a collected year until the coverage proves it.

## Funding history and predictions

```bash
START=$(( $(date +%s000) - 7*86400000 ))
hl "{\"type\":\"fundingHistory\",\"coin\":\"ETH\",\"startTime\":$START}" | jq -r '.[] | [.time, .fundingRate, .premium] | @tsv'
# venue, raw rate, its interval in hours, rate per hour, next funding
hl '{"type":"predictedFundings"}' | jq -r '.[] | select(.[0]=="ETH") | .[1][] | select(.[1]) | [.[0], .[1].fundingRate, (.[1].fundingIntervalHours // "?"), (if .[1].fundingIntervalHours then (.[1].fundingRate|tonumber) / .[1].fundingIntervalHours else "?" end), .[1].nextFundingTime] | @tsv'
```

`fundingHistory` returns hourly rates (up to 500 per call; paginate from the last returned timestamp, retaining overlap and checking duplicates). For research, use `research_dataset.py --with-funding`: actual millisecond timestamps are preserved and coverage is checked by UTC hour bucket. Requests are paced; `--max-pages` bounds funding collection (default 100, range 1-250). A page limit, missing hour or failed read is not a complete dataset.

`predictedFundings` compares venues, and the venue rates are over **different periods**, so never compare them raw. Every coin carries the same three slots in the same order - `BinPerp`, `HlPerp`, `BybitPerp` - but match on the venue name rather than the position, and each slot is either a payload or `null` when the coin is not listed on that venue. Divide `fundingRate` by the payload's own `fundingIntervalHours` to get a per-hour rate. `HlPerp` is always 1; the CEX venues are **4 or 8 depending on the coin**, not a fixed 8: on mainnet on 2026-08-28, BinPerp was 4h for 126 coins and 8h for 63, BybitPerp 4h for 118 and 8h for 69. BTC, ETH and DOGE are all 8h, so an assumed 8 looks right on the majors and is wrong by 2x across most alts. A few BinPerp payloads (19 that day, including `TON`, `MKR` and `IP`) omit `fundingIntervalHours` entirely; treat the interval as unknown and say so rather than defaulting it.

Funding is paid every hour at `size x oracle price x hourly rate`; longs pay shorts when positive. Python: `info.funding_history(coin, start_ms)`.

Rates plus candle closes do not reconstruct exact historical USD payments: that needs oracle prices and the position at payment time. Label a candle-price proxy as an assumption, never an observed funding cash flow.

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
- Comparing `predictedFundings` venues without dividing each by its own `fundingIntervalHours`, or assuming the CEX venues are 8h when most coins are 4h.
- Reading a book once and calling it "the depth" ten minutes later.
- Forgetting spot `@index` naming and base-token `szDecimals`.
- Expecting deep history at fine intervals; only the latest 5000 candles per interval exist, so pick the interval to fit the lookback.
