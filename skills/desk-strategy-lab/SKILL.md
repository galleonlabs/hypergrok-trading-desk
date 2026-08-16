---
name: desk-strategy-lab
description: How the Strategist works with the user to turn their own trading idea into explicit rules, backtest it honestly on Hyperliquid candle and funding history, and paper-trade it on testnet through the desk lifecycle. Method only - the desk ships no strategies and makes no return claims. Use when the user wants to design, test, compare or paper-trade an idea.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: desk
---

# Strategy lab

The lab exists so the user can find out whether their idea holds up before risking money on it. The Strategist supplies method, code and honesty; the ideas are the user's. Nothing here recommends what to trade.

## Workspace

```
/workspace/trading-desk/strategies/<name>/
  RULES.md          the rules, in words, agreed with the user before any code
  data.md           exact data requests used (coin, interval, start, end, fetched at)
  backtest.py       readable, single-file backtest
  runs/YYYY-MM-DD-HHMM.md   one file per run: parameters, results, caveats
  POSTMORTEM.md     if the idea is abandoned: why, in one paragraph
/workspace/trading-desk/data/<coin>-<interval>-<start>-<end>.csv   fetched by the Market Analyst or the Strategist
```

## 1. Rules first

Interview the user until every field is unambiguous. Write `RULES.md`:

```markdown
# funding-fade-v1

- universe: ETH, BTC, SOL perps on Hyperliquid
- data: 4h candles (close), hourly funding from fundingHistory
- entry: when the average hourly funding over the last 8 hours is above +0.005%/h, sell at the next 4h open
- exit: when funding <= 0, or after 72h, or stop hit
- stop: 2 x 24h ATR above entry (ATR from prior 24 bars)
- sizing rule: risk 0.5% of equity to the stop (the desk's limits apply on top)
- one position per market; no adds
- what would make me abandon this: no edge after fees on 12 months of data across 3 markets
```

If a rule needs "it depends", it is not a rule yet. Do not proceed to code.

## 2. Data

Candles via `candleSnapshot` (`hyperliquid-market-data`). Only the most recent 5000 candles per interval exist, so the interval sets the lookback: about 208 days of `1h`, 2.3 years of `4h`, 13 years of `1d`. Funding via `fundingHistory` (hourly, paginate by `startTime`). Save to CSV under `data/` with the request recorded in `data.md`, and check gaps and duplicated timestamps before using the file. Newer markets have shorter histories still.

## 3. Backtest honestly

Keep it simple and readable; a single Python file the user can follow beats a framework. Requirements:

- **No look-ahead.** Signals at bar `t` use only data up to and including `t`; execution at `t+1` open (or `t` close, stated).
- **Costs.** Taker and maker fees at the account's tier (`userFees` or the published schedule), funding paid or received each hour the position is open, and slippage per side (default a few bps, or from the Market Analyst's depth read for the intended size).
- **Out-of-sample.** Hold back the most recent 15-25% of the period untouched until the rules and parameters are frozen. Report in-sample and out-of-sample separately.
- **Parameters.** Record every parameter and how it was chosen. A grid of three values is a grid; say so.
- **Position accounting.** Sizes from the sizing rule and the stop; PnL in R and USD; equity curve; drawdown from the equity curve.

Report per run:

```
run 2026-08-16-1540 | funding-fade-v1 | in-sample 2025-01-01..2026-07-31 | out-of-sample 2026-08-01..2026-08-15
trades 58 (IS) / 6 (OOS) | win 41% / 50% | avg win 1.9R / 1.6R | avg loss 1.0R / 1.0R
expectancy 0.19R / 0.30R | max DD 9.4% / 2.1% | fees+funding 31% of gross PnL
caveats: stop multiplier chosen from {1.5, 2, 3}; slippage 5 bps assumed; SOL history shorter than others
```

No annualised return headline. Distributions and costs are the result.

## 4. Sanity checks before believing anything

- Flip the sign of the entry rule: does the mirror strategy also "work"? Then you are measuring drift, not edge.
- Shuffle entry dates within the sample: how often does random timing beat the strategy?
- Remove the best two trades: does the result survive?
- Trade count under 30 in-sample: report as "not enough evidence", not as a result.
- Does the result depend on one market or one month?

Write what you checked into the run file.

## 5. Paper trade on testnet

When the user wants to see it live: the Strategist runs the rules on live data (polling or WebSocket per `hyperliquid-websocket`) and posts each signal as a **proposal** (`HG-...`) with the rules file cited as the idea. The Risk Manager sizes it, the user approves it, the Execution Trader sends it on **testnet**, the Trade Reviewer reviews it. The Strategist never sends orders and never asks another Bot to skip a stage "because the rules said so".

After a set number of paper trades agreed with the user (20 is a common choice), the Trade Reviewer's numbers and the backtest are compared side by side.

## 6. Abandon cleanly

If it does not hold up: `POSTMORTEM.md` with the data, the runs, and one paragraph on why. Post the conclusion to the user plainly. That is a good outcome for the lab.

## Never

- Never propose a strategy the user did not bring. Suggesting standard test structures is fine; suggesting "what works" is not.
- Never quote a backtest return without trade count, drawdown, costs and the out-of-sample split.
- Never send an order, on any network.
- Never let a live signal loop turn into unattended execution.
