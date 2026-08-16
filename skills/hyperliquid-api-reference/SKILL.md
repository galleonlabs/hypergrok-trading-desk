---
name: hyperliquid-api-reference
description: Compact reference for the Hyperliquid API as the desk uses it - endpoints and envelopes, every /info request type, every /exchange action with its signing scheme, order and status vocabularies, asset ids, tick and lot rules, rate limits, WebSocket subscription list, error strings, and where the official docs are. Use to look up an exact field, request type or limit before writing a call, and to map an error string to its cause.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: hyperliquid
  docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
---

# Hyperliquid API reference (desk edition)

Verified against the official docs on 2026-08-16. When in doubt, fetch the page: append `.md` to any docs URL for raw markdown, for example `https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint.md`.

## Endpoints

| | Mainnet | Testnet |
| --- | --- | --- |
| REST | `https://api.hyperliquid.xyz` | `https://api.hyperliquid-testnet.xyz` |
| WebSocket | `wss://api.hyperliquid.xyz/ws` | `wss://api.hyperliquid-testnet.xyz/ws` |
| HyperEVM RPC | `https://rpc.hyperliquid.xyz/evm` (chain 999) | `https://rpc.hyperliquid-testnet.xyz/evm` (chain 998) |

- `POST /info` body `{"type": "<request>", ...}`, unsigned; response is the bare payload. Unknown `type` or missing field: HTTP 422; invalid JSON: HTTP 400.
- `POST /exchange` body `{"action": {...}, "nonce": <ms>, "signature": {"r","s","v"}, "vaultAddress"?: "0x...", "expiresAfter"?: <ms>}`; response `{"status":"ok","response":{"type":"order"|"cancel"|"default"|..., "data"?: {...}}}` or `{"status":"err","response":"<string>"}`, both HTTP 200.
- Header `Content-Type: application/json`.

## /info request types

| type | params | returns |
| --- | --- | --- |
| `allMids` | `dex?` | `{coin: midPx}` |
| `meta` | `dex?` | `{universe:[{name, szDecimals, maxLeverage, marginTableId?, onlyIsolated?, marginMode?, isDelisted?}], marginTables:[[id,{description, marginTiers:[{lowerBound, maxLeverage}]}]]}` |
| `metaAndAssetCtxs` | `dex?` | `[meta, [{funding, openInterest, prevDayPx, dayNtlVlm, premium, oraclePx, markPx, midPx, impactPxs}]]` |
| `spotMeta` | | `{tokens:[{name, szDecimals, weiDecimals, index, tokenId, ...}], universe:[{name, tokens:[base,quote], index}]}` |
| `spotMetaAndAssetCtxs` | | `[spotMeta, [{dayNtlVlm, markPx, midPx, prevDayPx, circulatingSupply}]]` |
| `l2Book` | `coin, nSigFigs? (2-5), mantissa? (1,2,5)` | `{coin, time, levels:[[{px,sz,n}...bids],[...asks]]}` up to 20 a side |
| `recentTrades` | `coin` | `[{coin, side, px, sz, time, hash, tid}]` |
| `candleSnapshot` | `req:{coin, interval, startTime, endTime}` | `[{t,T,s,i,o,c,h,l,v,n}]`, most recent 5000 only |
| `fundingHistory` | `coin, startTime, endTime?` | `[{coin, fundingRate (hourly), premium, time}]` |
| `predictedFundings` | | `[[coin, [[venue, {fundingRate, nextFundingTime}]]]]` (HlPerp hourly; CEX venues 8h) |
| `perpsAtOpenInterestCap` | `dex?` | `[coin]` |
| `perpDexs` | | `[null, {name, fullName, deployer, ...}]` |
| `clearinghouseState` | `user, dex?` | `{assetPositions:[{type, position:{coin, szi, entryPx, leverage{type,value,rawUsd?}, liquidationPx, marginUsed, positionValue, unrealizedPnl, returnOnEquity, cumFunding, maxLeverage}}], marginSummary{accountValue,totalNtlPos,totalRawUsd,totalMarginUsed}, crossMarginSummary, crossMaintenanceMarginUsed, withdrawable, time}` |
| `spotClearinghouseState` | `user` | `{balances:[{coin, token, hold, total, entryNtl}]}` |
| `openOrders` | `user, dex?` | `[{coin, side (A/B), limitPx, sz, oid, timestamp}]` |
| `frontendOpenOrders` | `user, dex?` | adds `origSz, cloid, orderType, tif, reduceOnly, isTrigger, triggerPx, triggerCondition, isPositionTpsl, children` |
| `orderStatus` | `user, oid (number or cloid hex)` | `{status:"order", order:{order, status, statusTimestamp}}` or `{status:"unknownOid"}` |
| `historicalOrders` | `user` | `[{order, status, statusTimestamp}]` up to 2000 |
| `userFills` | `user, aggregateByTime?` | `[{coin, px, sz, side, time, startPosition, dir, closedPnl, hash, oid, crossed, fee, feeToken, builderFee?, tid, cloid?}]` up to 2000 |
| `userFillsByTime` | `user, startTime, endTime?, aggregateByTime?` | same, up to 2000 per call from the last 10000 |
| `userFunding` | `user, startTime, endTime?` | `[{time, hash, delta:{type:"funding", coin, usdc, szi, fundingRate, nSamples}}]` |
| `userNonFundingLedgerUpdates` | `user, startTime, endTime?` | deposits, withdrawals, transfers, liquidations, vault flows |
| `userTwapSliceFills` | `user` | `[{fill, twapId}]` |
| `portfolio` | `user` | `[["day",{accountValueHistory, pnlHistory, vlm}], ["week",...], ["month",...], ["allTime",...], perp variants]` |
| `userFees` | `user` | `{userCrossRate, userAddRate, userSpotCrossRate, userSpotAddRate, feeSchedule, activeReferralDiscount, activeStakingDiscount}` |
| `userRateLimit` | `user` | `{cumVlm, nRequestsUsed, nRequestsCap, nRequestsSurplus}` |
| `userRole` | `user` | `{role: missing|user|agent|vault|subAccount, data?}` |
| `extraAgents` | `user` | `[{address, name, validUntil}]` |
| `activeAssetData` | `user, coin` | `{leverage, maxTradeSzs:[buy,sell], availableToTrade:[buy,sell], markPx}` |
| `maxBuilderFee` | `user, builder` | integer (tenths of a bp) |
| `subAccounts` | `user` | `[{name, subAccountUser, master, clearinghouseState, spotState}]` |
| `vaultDetails` | `vaultAddress, user?` | vault info |
| `userVaultEquities` | `user` | `[{vaultAddress, equity}]` |
| `referral`, `delegations`, `delegatorSummary`, `delegatorHistory`, `delegatorRewards`, `userAbstraction`, `borrowLendUserState`, `tokenDetails`, `spotDeployState`, `perpDexLimits`, `allPerpMetas` | | niche; see docs |

Pagination for time-ranged reads: 500 items per response for ledger/funding style queries; use the last `time` as the next `startTime`.

## /exchange actions

L1-signed actions can be signed by an API wallet; user-signed actions need the account's main wallet (the desk never has it).

| action | params | scheme | desk |
| --- | --- | --- | --- |
| `order` | `orders:[{a,b,p,s,r,t,c?}], grouping: na|normalTpsl|positionTpsl, builder?` | L1 | yes |
| `cancel` | `cancels:[{a,o}], f? (fast, omit if false)` | L1 | yes |
| `cancelByCloid` | `cancels:[{asset, cloid}]` | L1 | yes |
| `modify` / `batchModify` | `oid (or cloid), order:{...}` / `modifies:[{oid, order}]`; without `a` (always_place) the new order must be non-trigger and rest (`Alo` or non-executable `Gtc`) | L1 | yes, resting limits only |
| `scheduleCancel` | `time? (ms >= now+5s)` | L1 | yes, on request |
| `updateLeverage` | `asset, isCross, leverage` | L1 | yes |
| `updateIsolatedMargin` | `asset, isBuy, ntli (USD x 1e6, negative removes)` | L1 | yes |
| `twapOrder` / `twapCancel` | `twap:{a,b,s,r,m (minutes),t (randomise)}` / `a, t (twapId)`; 5 min to 7 days, min 100 USD | L1 | yes, on request |
| `noop` | | L1 | rarely |
| `reserveRequestWeight` | `weight` | L1 | user's call |
| `vaultTransfer`, `subAccountTransfer`, `createSubAccount`, `subAccountSpotTransfer` | | L1 | **no** |
| `approveAgent` | `agentAddress, agentName?` | user | in app |
| `approveBuilderFee` | `maxFeeRate, builder` | user | **no** |
| `usdSend`, `spotSend`, `sendAsset`, `withdraw3`, `usdClassTransfer` | | user | **no** |
| `cDeposit`, `cWithdraw`, `tokenDelegate`, `userSetAbstraction` | | user | **no** |

Order fields: `a` asset index, `b` isBuy, `p` price string, `s` size string, `r` reduceOnly, `t` `{"limit":{"tif":"Alo"|"Ioc"|"Gtc"}}` or `{"trigger":{"isMarket":bool,"triggerPx":"...","tpsl":"tp"|"sl"}}`, `c` cloid (`0x` + 32 hex). Order statuses: `{"resting":{"oid"}}`, `{"filled":{"totalSz","avgPx","oid"}}`, `"waitingForTrigger"`, `"waitingForFill"`, `{"error":"..."}`. Cancel statuses: `"success"` or `{"error":"..."}`.

Order status vocabulary (`orderStatus`, `historicalOrders`, WS `orderUpdates`): `open, filled, canceled, triggered, rejected, marginCanceled, vaultWithdrawalCanceled, openInterestCapCanceled, selfTradeCanceled, reduceOnlyCanceled, siblingFilledCanceled, delistedCanceled, liquidatedCanceled, scheduledCancel, tickRejected, minTradeNtlRejected, perpMarginRejected, reduceOnlyRejected, badAloPxRejected, iocCancelRejected, badTriggerPxRejected, marketOrderNoLiquidityRejected, positionIncreaseAtOpenInterestCapRejected, positionFlipAtOpenInterestCapRejected, tooAggressiveAtOpenInterestCapRejected, openInterestIncreaseRejected, insufficientSpotBalanceRejected, oracleRejected, perpMaxPositionRejected`.

## Signing (what the SDKs do for you)

- **L1 actions:** msgpack the action, append nonce, vault flag/address and optional expiresAfter, keccak it, sign an EIP-712 `Agent {source, connectionId}` under domain `Exchange` (chainId 1337) where `source` is `a` for mainnet and `b` for testnet.
- **User-signed actions:** EIP-712 typed data under domain `HyperliquidSignTransaction` with the action's `hyperliquidChain` (`Mainnet`/`Testnet`) and `signatureChainId`; the action's `nonce`/`time` must equal the outer nonce.
- Nonce: unix ms; per signer; must exceed the 100th-highest used and lie within (now - 2 days, now + 1 day).
- A bad signature surfaces as `User or API Wallet 0x<recovered> does not exist.`, not as "bad signature".
- Addresses lowercase; numbers as strings without trailing zeros; `-0` becomes `0`.

## Asset ids and coin names

- Perp: index in `meta.universe` (default dex). Read it live; never hardcode.
- Spot: `10000 + index` in `spotMeta.universe`; coin name `PURR/USDC` or `@<index>`; size decimals from the base token.
- HIP-3 perps: `100000 + 10000 x dex index + index`; name `dex:COIN`.
- HIP-4 outcomes: `100000000 + encoding`; names `#<n>`.
- Ids differ between mainnet and testnet.

## Tick and lot

- Price: at most 5 significant figures **and** at most `6 - szDecimals` decimals (perps) or `8 - szDecimals` (spot). Integer prices are always valid.
- Size: rounded to `szDecimals`. Round down on the desk.
- Minimum order value: 10 USD (perps) / 10 quote tokens (spot). Max market order value scales with max leverage (30M for 25x+, down to 500k); limit orders 10x that.
- Open orders per account: 1000 + 1 per 5M USDC volume, cap 5000; at 1000 open orders new reduce-only and trigger orders are rejected.

## Rate limits

- Per IP: 1200 weight per minute across REST. `/exchange` weight `1 + floor(n/40)`. `/info` weight 2 for `l2Book, allMids, clearinghouseState, orderStatus, spotClearinghouseState, exchangeStatus`; 60 for `userRole`; 20 for the rest; +1 per 20 items for list queries; `candleSnapshot` +1 per 60 candles.
- Per address (actions): 10,000 buffer + 1 per 1 USDC cumulative volume; when exhausted, 1 action per 10 s; cancels get `min(limit + 100000, 2 x limit)`. Stale `expiresAfter` rejections cost 5x. Unified/portfolio-margin accounts capped at 50k actions per day.
- WebSocket per IP: 10 connections, 30 new per minute, 1000 subscriptions, 10 distinct users, 2000 messages per minute, 100 in-flight posts.

## WebSocket

`{"method":"subscribe","subscription":{...}}`; heartbeat `{"method":"ping"}` / `{"channel":"pong"}`; idle connections closed after 60 s. Types: `allMids, notification, webData3, candle, l2Book, trades, orderUpdates, userEvents (channel "user"), userFills, userFundings, userNonFundingLedgerUpdates, activeAssetCtx, activeAssetData, userTwapSliceFills, userTwapHistory, twapStates, bbo, clearinghouseState, openOrders, spotState, allDexsClearinghouseState, allDexsAssetCtxs, fastAssetCtxs`. Post `/info` or signed actions over the socket with `{"method":"post","id":n,"request":{"type":"info"|"action","payload":{...}}}`.

## Error strings

`Price must be divisible by tick size.` | `Order must have minimum value of $10.` | `Insufficient margin to place order.` | `Reduce only order would increase position.` | `Post only order would have immediately matched, bbo was ...` | `Order could not immediately match against any resting orders.` | `Invalid TP/SL price.` | `No liquidity available for market order.` | `Order price too far from oracle` | `Order would cause position to exceed margin tier limit at current leverage` | `Order was never placed, already canceled, or filled.` | `User or API Wallet 0x... does not exist.` | `Must deposit before performing actions. User: 0x...` | `Invalid TWAP duration: ...`

## Trading facts the desk quotes

- Funding: hourly, peer to peer, computed from an 8h formula paid 1/8 each hour, capped 4%/h, paid on **oracle** price; API rates are hourly.
- Mark price (median of oracle-adjusted mid, book mid and CEX perp mids) drives margining, liquidation and TP/SL triggers.
- Maintenance margin = half the initial margin at the tier's max leverage; partial liquidations first for large positions; cross liquidation price ignores the leverage setting.
- Fees: base taker 0.045% / maker 0.015% perps, 0.07% / 0.04% spot, tiered by 14-day volume, staking and referral discounts; effective rates via `userFees`.
- Testnet: same API; faucet at `app.hyperliquid-testnet.xyz/drip` (needs an address that has deposited on mainnet); much tighter margin tiers; asset ids differ.

## Official pages worth fetching

`for-developers/api/info-endpoint`, `.../info-endpoint/perpetuals`, `.../info-endpoint/spot`, `.../exchange-endpoint`, `.../signing`, `.../nonces-and-api-wallets`, `.../rate-limits-and-user-limits`, `.../tick-and-lot-size`, `.../asset-ids`, `.../error-responses`, `.../websocket/subscriptions`, `.../websocket/post-requests`; `trading/order-types`, `trading/take-profit-and-stop-loss-orders-tp-sl`, `trading/margining`, `trading/margin-tiers`, `trading/liquidations`, `trading/funding`, `trading/fees`, `trading/sub-accounts`; `onboarding/testnet-faucet`. All under `https://hyperliquid.gitbook.io/hyperliquid-docs/`.
