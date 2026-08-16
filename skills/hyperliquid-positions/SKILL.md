---
name: hyperliquid-positions
description: Manage Hyperliquid perp positions and margin from the desk computer - read positions and margin, set leverage and cross/isolated mode, add isolated margin, understand margin tiers and liquidation price, close a position with a reduce-only IOC, and clean up orphaned orders. Write actions are Execution Trader only, on an approved ticket. Use for leverage changes, closes, protection checks and margin questions.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: hyperliquid
  network-default: testnet
---

# Hyperliquid positions and margin

Reads here are for everyone (Risk Manager first); the write actions (`updateLeverage`, `updateIsolatedMargin`, closes) are Execution Trader only, on a ticket. Order mechanics are in `hyperliquid-orders`.

## Concepts

- **Cross margin** (default): all cross positions share the account's margin, and unrealised PnL counts as margin; liquidation is account-wide. **Isolated margin:** the position has its own margin; only that margin is at risk, and you can add to or remove from it (some markets are `strictIsolated` and refuse removal; HIP-3 markets may be `noCross`).
- **Leverage** is set per market and per mode with `updateLeverage`; it caps position size against margin, it is not "how much you win". Max leverage is per market and **tiered by notional** (see margin tiers).
- **Margin tiers:** each perp has a margin table (`meta.marginTables`, matched to the asset via `marginTableId`) listing notional thresholds and the max leverage allowed above each. Bigger positions get less leverage.
- **Maintenance margin** is half of the initial margin at the max leverage of the applicable tier (so between 1.25% for a 40x market and 16.7% for a 3x market); liquidation happens when account (cross) or position (isolated) equity falls below maintenance margin, on **mark** price. Large positions are liquidated partially first; the cross liquidation price does not depend on your leverage setting.
- **Liquidation price** is reported by the exchange per position (`liquidationPx`); use that, do not recompute.
- **Funding** is exchanged every hour between longs and shorts at the market's funding rate; it changes equity while a position is open.
- **Closing** is an order in the opposite direction with `reduceOnly`. Full close at market: reduce-only IOC at a slippage-bounded price for the live position size.

## Read positions and margin

```bash
ADDR=$HYPERLIQUID_ACCOUNT_ADDRESS
BASE=$([ "$HYPERLIQUID_NETWORK" = mainnet ] && echo https://api.hyperliquid.xyz || echo https://api.hyperliquid-testnet.xyz)
curl -sS -X POST $BASE/info -H 'Content-Type: application/json' -d "{\"type\":\"clearinghouseState\",\"user\":\"$ADDR\"}" | jq '{
  accountValue: .marginSummary.accountValue, totalMarginUsed: .marginSummary.totalMarginUsed,
  withdrawable, crossMaintenanceMarginUsed,
  positions: [.assetPositions[].position | {coin, szi, entryPx, positionValue, unrealizedPnl, liquidationPx, marginUsed,
    leverage: (.leverage.type + " " + (.leverage.value|tostring)), returnOnEquity, cumFunding: .cumFunding.sinceOpen}]}'
```

`szi` is signed size (positive long, negative short). `leverage.type` is `cross` or `isolated`; isolated positions also carry `leverage.rawUsd` (the isolated margin).

Margin tiers for a market:

```bash
curl -sS -X POST $BASE/info -H 'Content-Type: application/json' -d '{"type":"meta"}' \
 | jq --arg c ETH '(.universe[] | select(.name==$c)) as $u | ($u.marginTableId // $u.maxLeverage) as $id
      | {name:$u.name, szDecimals:$u.szDecimals, maxLeverage:$u.maxLeverage, marginTableId:$id,
         tiers: (if $id < 50 then [{lowerBound:"0.0", maxLeverage:$id}]
                 else ((.marginTables[] | select(.[0]==$id) | .[1].marginTiers) // [{lowerBound:"0.0", maxLeverage:$u.maxLeverage}]) end)}'
```

`marginTables` is a list of `[id, {description, marginTiers: [{lowerBound, maxLeverage}, ...]}]` for ids of 50 and above; the tier whose `lowerBound` (position notional in USD) is the largest one at or below your notional applies. Ids below 50 are single-tier tables whose max leverage equals the id and they are **not** listed in `marginTables` (most altcoins), which the snippet handles. Testnet tiers are far tighter than mainnet (BTC drops from 40x above only 10k notional on testnet), which is one reason testnet rehearsals differ from mainnet.

Python equivalents: `info.user_state(ADDR)`, `info.meta()`; the header from `hyperliquid-orders` applies.

## Set leverage and margin mode (write)

Do this **before** the entry the ticket refers to. Leverage is checked when a position is opened (`margin required = size x mark / leverage`); the leverage of an existing position can be raised without closing it, which frees margin and moves the isolated liquidation price, and lowering it needs enough free margin to cover the higher initial margin. Either way, tell the user what changes.

```python
# header from hyperliquid-orders (info, exchange, ACCOUNT)
res = exchange.update_leverage(3, "ETH", is_cross=True)     # 3x cross
# res = exchange.update_leverage(5, "ETH", is_cross=False)  # 5x isolated
print(res)                                                    # {"status":"ok","response":{"type":"default"}}
print(next((p["position"]["leverage"] for p in info.user_state(ACCOUNT)["assetPositions"] if p["position"]["coin"] == "ETH"), "no open ETH position"))
```

If the market has no open position yet, confirm afterwards by placing the entry and reading `leverage` on the resulting position, or via `activeAssetData` (`{"type":"activeAssetData","user":ADDR,"coin":"ETH"}`), which reports the account's current leverage setting and available-to-trade for that market.

Add isolated margin to an existing isolated position (USD amount):

```python
res = exchange.update_isolated_margin(50.0, "ETH")            # adds 50 USDC of margin to the ETH isolated position
```

TypeScript: `await exchange.updateLeverage({ asset: a, isCross: true, leverage: 3 })`; `await exchange.updateIsolatedMargin({ asset: a, isBuy: true, ntli: 50 * 1e6 })` (`ntli` is USD x 1e6).

## Close a position (write)

Read the size live seconds before sending; the ticket states the slippage bound.

```python
coin, slippage = "ETH", 0.003                                  # 30 bps bound
pos = next((p["position"] for p in info.user_state(ACCOUNT)["assetPositions"] if p["position"]["coin"] == coin), None)
if not pos: raise SystemExit("no open position")
szi = float(pos["szi"]); is_buy = szi < 0                      # closing a short buys
sz = round_sz(coin, abs(szi))
mid = float(info.all_mids()[coin])
px = round_px(coin, mid * (1 + slippage) if is_buy else mid * (1 - slippage))
cloid = new_cloid(); print("cloid", cloid.to_raw(), "bound", px, "size", sz)
res = exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=True, cloid=cloid)
print(res)
# SDK shortcut with the same semantics: exchange.market_close(coin, sz=None, px=None, slippage=0.003, cloid=cloid)
# It returns None (not an error) when there is no position, and it does not round a caller-supplied sz.
```

Partial close: pass the reduced size. After any close, list `open_orders(ACCOUNT)` and cancel orphaned TP/SL for that market (their own ticket or the standing approval in `desk.md`), then confirm `assetPositions` no longer contains the coin.

## Protection check (read)

A position is protected when a **reduce-only trigger order** on the opposite side is resting on the exchange, either sized at least to the position or position-tied (`sz: "0.0"`, `isPositionTpsl: true`, meaning "the whole position"). Verify with `frontendOpenOrders`, which exposes trigger fields:

```bash
curl -sS -X POST $BASE/info -H 'Content-Type: application/json' -d "{\"type\":\"frontendOpenOrders\",\"user\":\"$ADDR\"}" \
 | jq '[.[] | select(.isTrigger==true) | {coin, side, sz, triggerPx, triggerCondition, orderType, reduceOnly, isPositionTpsl, oid, cloid}]'
```

`side` is `B` (buy) or `A` (sell). A long's stop is an `A` trigger below mark with `reduceOnly: true`; a short's is a `B` trigger above mark. Missing, or undersized without being position-tied: report **unprotected** to the Desk Lead (`desk-incident-response` playbook D).

## Liquidation distance

```
distance_pct = (mark - liquidationPx) / mark x 100      for longs (negative of that for shorts)
```

Report it per position in every book check. Cross accounts liquidate together; the per-position `liquidationPx` already accounts for that.

## Pitfalls

- Changing leverage with an open position without telling the user; margin used (and, for isolated, the liquidation price) move at once.
- Closing with a `Gtc` order by mistake; use `Ioc` and `reduceOnly` so nothing rests and nothing can flip the position.
- Reading position size from a brief instead of `clearinghouseState` seconds before the close.
- Assuming headline `maxLeverage` applies to your notional; read the tier.
- Leaving TP/SL orphans after a close; a stale reduce-only trigger fires against the next position you open in that market.
- Isolated margin adds are in USD; the TS `ntli` field is USD x 1e6.
