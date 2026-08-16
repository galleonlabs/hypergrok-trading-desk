---
name: hyperliquid-account
description: Read a Hyperliquid account from the desk computer - positions and margin, spot balances, open orders including trigger details, fills, funding paid, ledger updates, order status by oid or cloid, historical orders, portfolio history, fee tier and rate-limit budget - with curl and Python SDK examples. Read-only, needs only the account address. Use for sizing inputs, book checks, reconciliation and reviews.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: hyperliquid
  network-default: testnet
---

# Hyperliquid account reads

All reads are `POST /info`, unsigned. Use the **account** address (the main wallet the desk trades for), never the API wallet's address: queries on an agent address return empty results.

```bash
ADDR=$HYPERLIQUID_ACCOUNT_ADDRESS
BASE=$([ "$HYPERLIQUID_NETWORK" = mainnet ] && echo https://api.hyperliquid.xyz || echo https://api.hyperliquid-testnet.xyz)
hl() { curl -sS -m 15 -X POST "$BASE/info" -H 'Content-Type: application/json' -d "$1"; }
```

```python
import os
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
NETWORK = os.environ.get("HYPERLIQUID_NETWORK", "testnet")
info = Info(constants.MAINNET_API_URL if NETWORK == "mainnet" else constants.TESTNET_API_URL, skip_ws=True)
ADDR = os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"]
```

## Perp account state

```bash
hl "{\"type\":\"clearinghouseState\",\"user\":\"$ADDR\"}" | jq '{
  time, accountValue: .marginSummary.accountValue, totalNtlPos: .marginSummary.totalNtlPos,
  totalMarginUsed: .marginSummary.totalMarginUsed, withdrawable, crossMaintenanceMarginUsed,
  positions: [.assetPositions[].position | {coin, szi, entryPx, positionValue, unrealizedPnl, returnOnEquity,
              liquidationPx, marginUsed, leverage, maxLeverage, cumFunding: .cumFunding.sinceOpen}]}'
```

Python: `info.user_state(ADDR)`. `szi` is signed size; `leverage` is `{type: cross|isolated, value, rawUsd?}`; `crossMarginSummary` mirrors `marginSummary` for the cross portion. Margin ratio for a book check: `crossMaintenanceMarginUsed / crossMarginSummary.accountValue` (`marginSummary` also counts isolated margin).

Under the account's abstraction mode (`{"type":"userAbstraction","user":ADDR}` returns `default`, `disabled`, `unifiedAccount`, `portfolioMargin` or `dexAbstraction`), USDC may live in the spot state; check both when equity looks wrong.

## Spot balances

```bash
hl "{\"type\":\"spotClearinghouseState\",\"user\":\"$ADDR\"}" | jq '.balances[] | {coin, token, total, hold, entryNtl}'
```

Python: `info.spot_user_state(ADDR)`. `hold` is the amount locked in open orders.

## Open orders

```bash
hl "{\"type\":\"openOrders\",\"user\":\"$ADDR\"}" | jq '.[] | {coin, side, limitPx, sz, oid, timestamp}'
hl "{\"type\":\"frontendOpenOrders\",\"user\":\"$ADDR\"}" | jq '.[] | {coin, side, limitPx, sz, origSz, oid, cloid, orderType, tif, reduceOnly, isTrigger, triggerPx, triggerCondition, isPositionTpsl, children}'
```

`side` is `B` (bid/buy) or `A` (ask/sell). Use `frontendOpenOrders` whenever you need to know whether an order is a stop or take-profit and whether it is position-tied. Python: `info.open_orders(ADDR)`, `info.frontend_open_orders(ADDR)`.

## Order status by oid or cloid

```bash
hl "{\"type\":\"orderStatus\",\"user\":\"$ADDR\",\"oid\":1839201122}" | jq '{status, order: .order.status, ts: .order.statusTimestamp, o: .order.order}'
hl "{\"type\":\"orderStatus\",\"user\":\"$ADDR\",\"oid\":\"0x9f3e0c1a2b3c4d5e6f708192a3b4c5d6\"}"
```

Returns `{"status":"order","order":{"order":{...},"status":"...","statusTimestamp":...}}` or `{"status":"unknownOid"}`. Status vocabulary: `open`, `filled`, `canceled`, `triggered`, `rejected`, `marginCanceled`, `reduceOnlyCanceled`, `siblingFilledCanceled`, `scheduledCancel`, `liquidatedCanceled`, plus rejection reasons such as `tickRejected`, `minTradeNtlRejected`, `perpMarginRejected`, `badTriggerPxRejected`, `iocCancelRejected`, `marketOrderNoLiquidityRejected`. Python: `info.query_order_by_oid(ADDR, oid)`, `info.query_order_by_cloid(ADDR, Cloid.from_str("0x..."))`.

This is the reconciliation call after any send whose response was lost.

## Fills

```bash
hl "{\"type\":\"userFills\",\"user\":\"$ADDR\"}" | jq '.[:20][] | {time, coin, side, px, sz, dir, closedPnl, fee, feeToken, crossed, oid, cloid, tid, hash}'
START=$(( $(date +%s000) - 86400000 ))
hl "{\"type\":\"userFillsByTime\",\"user\":\"$ADDR\",\"startTime\":$START}" | jq 'length'
```

`crossed: true` means taker; `fee` includes any builder fee and is negative for a rebate; `dir` reads like `Open Long`, `Close Short`; `startPosition` is the size before the fill; `closedPnl` is realised on that fill. `userFills` returns the most recent 2000; `userFillsByTime` returns up to 2000 per call from the last 10,000. Paginate with `startTime` = the last `time` you received (inclusive, because many fills share one millisecond) and de-duplicate by `tid`. Python: `info.user_fills(ADDR)`, `info.user_fills_by_time(ADDR, start_ms, end_ms)`.

## Funding paid and ledger

```bash
hl "{\"type\":\"userFunding\",\"user\":\"$ADDR\",\"startTime\":$START}" | jq '.[] | {time, coin: .delta.coin, usdc: .delta.usdc, rate: .delta.fundingRate, szi: .delta.szi}'
hl "{\"type\":\"userNonFundingLedgerUpdates\",\"user\":\"$ADDR\",\"startTime\":$START}" | jq '.[] | {time, type: .delta.type, delta}'
```

Funding `usdc` is signed from the account's point of view. Ledger updates cover deposits, withdrawals, transfers, liquidations and vault flows. Python: `info.user_funding_history(ADDR, start_ms)`, `info.user_non_funding_ledger_updates(ADDR, start_ms)`.

## Historical orders and TWAP fills

```bash
hl "{\"type\":\"historicalOrders\",\"user\":\"$ADDR\"}" | jq '.[:20][] | {status, statusTimestamp, o: (.order | {coin, side, limitPx, sz, origSz, oid, cloid, orderType, tif, reduceOnly, isTrigger, triggerPx})}'
hl "{\"type\":\"userTwapSliceFills\",\"user\":\"$ADDR\"}" | jq '.[:5]'
```

Up to 2000 recent orders with their final status; the Trade Reviewer's source for "was the stop on the exchange the whole time". Python: `info.historical_orders(ADDR)`, `info.user_twap_slice_fills(ADDR)`.

## Portfolio history, fees, rate limit, role

```bash
hl "{\"type\":\"portfolio\",\"user\":\"$ADDR\"}" | jq '.[] | select(.[0]=="day" or .[0]=="week") | {period: .[0], pnl: .[1].pnlHistory[-1], value: .[1].accountValueHistory[-1], vlm: .[1].vlm}'
hl "{\"type\":\"userFees\",\"user\":\"$ADDR\"}" | jq '{userCrossRate, userAddRate, userSpotCrossRate, userSpotAddRate, activeReferralDiscount, activeStakingDiscount}'
hl "{\"type\":\"userRateLimit\",\"user\":\"$ADDR\"}" | jq .
hl "{\"type\":\"userRole\",\"user\":\"$ADDR\"}" | jq .
hl "{\"type\":\"extraAgents\",\"user\":\"$ADDR\"}" | jq '.[] | {address, name, validUntil}'
```

`portfolio` gives PnL and account-value history per period (`day`, `week`, `month`, `allTime`, and perp-only variants); `userFees` gives the effective taker (`userCrossRate`) and maker (`userAddRate`) rates for the strategy lab and reviews; `userRateLimit` shows the address's action budget (`nRequestsUsed`, `nRequestsCap`, `cumVlm`); `userRole` classifies an address (`user`, `agent`, `vault`, `subAccount`, `missing`); `extraAgents` lists approved API wallets with expiry. Python: `info.portfolio(ADDR)`, `info.user_fees(ADDR)`, `info.user_rate_limit(ADDR)`, `info.user_role(ADDR)`, `info.extra_agents(ADDR)`.

Per-market account data (leverage setting, available to trade, max trade sizes) without opening a position: `hl "{\"type\":\"activeAssetData\",\"user\":\"$ADDR\",\"coin\":\"ETH\"}"`.

## Sub-accounts and vaults (read only on this desk)

`{"type":"subAccounts","user":ADDR}` lists sub-accounts with their states; `{"type":"userVaultEquities","user":ADDR}` lists vault deposits; `{"type":"vaultDetails","vaultAddress":"0x..."}` describes a vault. The desk reads these for completeness and does not move funds between them.

## Book check recipe (Risk Manager)

1. `clearinghouseState` for equity, positions, margin, liquidation prices.
2. `frontendOpenOrders` for protection (reduce-only triggers per position) and orphans.
3. `metaAndAssetCtxs` for mark prices to compute liquidation distance.
4. `userFunding` since start of day for funding paid.
5. `portfolio` or the journal for start-of-day equity, to compute day PnL against the daily loss stop.

Report per `desk-risk-limits` section 3.

## Pitfalls

- Querying the API wallet's address. Everything comes back empty and looks like "no positions".
- Assuming `openOrders` shows trigger details; it does not. Use `frontendOpenOrders`.
- Reading `userFills` and forgetting the 2000-item window; use `userFillsByTime` with pagination for reviews.
- Treating `withdrawable` as free margin for new positions; use `accountValue - totalMarginUsed` with headroom, and check the tier.
- Mixing networks: a mainnet address on the testnet endpoint is a different (probably empty) account.
