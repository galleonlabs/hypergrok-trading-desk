---
name: desk-risk-limits
description: How the Risk Manager writes the desk's risk limits with the user, sizes every proposed trade from live account state and Hyperliquid's real constraints, checks the book, and issues a PASS or REJECT with exact ticket fields. Use for setting up or changing limits, sizing any trade, and answering "how's the book".
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: desk
---

# Risk limits and sizing

The desk imposes no limits of its own. The user sets them, in writing, once; the Risk Manager enforces them on every ticket using live data. Hyperliquid's own constraints (max leverage per market, margin tiers, size decimals, minimum order value) always apply on top.

## 1. Write the limits file (setup, or on change)

Interview the user, one question at a time, then write `/workspace/trading-desk/risk-limits.md`. Version it (`v1`, `v2`...) and date every change. Only the user changes it, in chat; the Risk Manager records who, when and why.

```markdown
# Risk limits v1 - 2026-08-16 - set by user

- network: testnet            # testnet | mainnet
- account: 0xabc...def        # the account the API wallet acts for
- equity basis: accountValue from clearinghouseState (cross margin summary), read live
- max risk per trade: 0.5% of equity      # loss if the stop is hit
- max total open risk: 2% of equity       # sum of risk-to-stop across open positions
- max leverage per market: 3x             # never above the exchange max, and never above this
- max positions: 3
- allowed markets: BTC, ETH, SOL, HYPE     # perps; spot needs an explicit entry
- stops: mandatory on every entry, on the exchange, not "mental"
- daily loss stop: -2% of start-of-day equity -> no new risk until the user resets in writing
- max slippage tolerance at send: 10 bps  # Execution Trader stops if mid moved further
- correlated cluster limit: majors (BTC, ETH, SOL) count as one cluster; max 2 positions per cluster
- standing approvals: none
- notes:
```

Sensible starting points for someone new to perps: 0.25-0.5% per trade, 3x or lower, testnet first. Do not argue the user up or down; record what they choose and enforce it.

## 2. Size a trade

Inputs you need before you start: entry price, stop price, side, market, the current limits file, and live state. If any input is missing or stale, REJECT with "missing input", do not guess.

### 2.1 Read live state (never from memory)

- Account: `clearinghouseState` for equity (`marginSummary.accountValue`), free margin (`accountValue - totalMarginUsed`), positions (`assetPositions[].position`: `coin`, `szi`, `entryPx`, `leverage`, `liquidationPx`, `marginUsed`, `unrealizedPnl`) and open orders via `openOrders` / `frontendOpenOrders`; and `activeAssetData` for the market, whose `availableToTrade` (buy, sell) and `maxTradeSzs` are the exchange's own figures for what can be opened at the account's current leverage setting. Skill: `hyperliquid-account`.
- Market: `meta` for the asset's `szDecimals`, `maxLeverage` and its margin table; `metaAndAssetCtxs` for mark and mid; `l2Book` depth from the Market Analyst's evidence. Skill: `hyperliquid-market-data`.
- Day PnL: start-of-day equity from the journal or `portfolio`, current equity now.

### 2.2 Arithmetic (show every line in the PASS)

```
risk_usd        = equity x max_risk_pct
stop_distance   = |entry - stop|                       (must be > 0)
raw_size        = risk_usd / stop_distance
size            = round_down(raw_size, szDecimals)     (never round up)
notional        = size x entry
check           notional >= 10 USD                     (Hyperliquid minimum order value)
check           size >= 1 lot at szDecimals            (else REJECT: risk budget too small for this stop)
tier            = margin tier that applies to (existing position notional + notional) in this market
max_lev_here    = min(limits.max_leverage, tier max leverage)
margin_needed   = notional / requested_leverage         (requested_leverage <= max_lev_here)
check           margin_needed <= free_margin x 0.8      (20% headroom; tighter if the user says so)
open_risk_after = sum(risk to stop of open positions) + risk_usd
check           open_risk_after <= equity x max_total_open_risk
check           positions_after <= max_positions ; cluster count within cluster limit
check           market in allowed list ; stop present ; daily loss stop not hit
```

`R` for the ticket is `stop_distance` in USD per unit; targets are quoted in R by the user, never invented by the desk.

### 2.3 Margin tiers matter

Max leverage on Hyperliquid is per market and **tiered by position notional**: the headline max applies only up to the first tier's notional; larger positions get lower max leverage. Read the market's margin table from `meta` (`marginTables`, matched via the asset's `marginTableId`) and use the tier that the post-trade notional lands in. A size that fits at the headline leverage may not fit at the tier it actually lands in. Say which tier applied.

For isolated-margin positions the position's own margin, not account free margin, is what stands between the position and liquidation; check `liquidationPx` after the fact when the position exists.

### 2.4 Output

PASS: the block in `agents/risk-manager.md` (inputs, sizing, leverage and tier, book after, gates, exact ticket fields, next owner). REJECT: same header, `gate failed: <one gate, the numbers>`. Write it under `## risk` in the proposal file and post it on the floor.

## 3. Book check ("how's the book")

From `clearinghouseState`, `openOrders`/`frontendOpenOrders`, `metaAndAssetCtxs`:

- equity, free margin, `crossMaintenanceMarginUsed`, margin ratio (`crossMaintenanceMarginUsed / crossMarginSummary.accountValue`), and the distance from mark to `liquidationPx` per position in percent
- positions: coin, side, size, entry, mark, unrealised PnL, leverage and mode, margin used
- open risk to stop per position and in total, versus limits
- protection: for each position, is there a reduce-only stop resting on the exchange (trigger order, `reduceOnly: true`, correct side, and either size at least the position size or a position-tied stop with `sz: 0.0` and `isPositionTpsl: true`, which closes the whole position)? If not: **unprotected**, flagged as an incident to the Desk Lead
- open orders that no longer belong to a position (orphans)
- day PnL versus the daily loss stop
- funding paid so far today from `userFunding` when relevant

Timestamp everything. Save a copy under `/workspace/trading-desk/briefs/YYYY-MM-DD-book.md` when the user asks for a written check.

## 4. When the desk hits a limit

- Daily loss stop hit: post it once on the floor, set `status: no-new-risk` in `desk.md`, and REJECT new proposals with that gate until the user resets in writing. Exits and protection are still allowed.
- Unprotected position discovered: alert the Desk Lead and Execution Trader immediately; a protective stop ticket goes through the lifecycle at priority.
- Limits file missing or unversioned: the desk is a research desk until it exists.

## Pitfalls

- Sizing from a desired profit or from "what the margin allows" instead of from the stop. The stop defines the size.
- Using account leverage or headline max leverage instead of the tier that applies.
- Counting correlated positions as independent.
- Treating a plan file, a chat message or a screenshot as an open order. Only the exchange record is.
- Rounding size up to reach the minimum notional. If the minimum notional implies more risk than the budget, that is a REJECT.
