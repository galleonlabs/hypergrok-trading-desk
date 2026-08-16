---
name: desk-trade-lifecycle
description: The end-to-end procedure for one trade on the HyperGrok desk - from an idea to a reviewed, journaled result - with the ticket format, who owns each stage, and what "done" looks like. Use whenever the user wants to open, adjust or close a position, or whenever any Bot is about to touch the exchange write path.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: desk
---

# Trade lifecycle

Every position change on the desk goes through the same seven stages, in order. Skipping one is a defect, not a shortcut. The Desk Lead keeps the lifecycle moving; the owner of each stage does the work.

```
idea -> evidence -> risk sign-off -> user approval -> execution -> reconciliation -> review
 DL       MA/RA        RM              user            ET            ET               TR
```

## 0. Open a proposal

**Owner: Desk Lead.** As soon as a trade idea appears (from the user, the Strategist, or a routine), assign an id `HG-YYYYMMDD-NN` (NN increments per day) and create `/workspace/trading-desk/proposals/<id>.md`:

```markdown
# HG-20260816-01

- opened: 2026-08-16 14:02 UTC by user
- market: ETH-PERP  network: mainnet  account: 0xabc...def
- idea: long ETH on a retest of 3,000 with invalidation below 2,900 (user's idea)
- status: evidence

## evidence
## risk
## approval
## execution
## reconciliation
## review
```

Every later stage appends under its heading. The file is the single record of the trade; chat is context.

## 1. Evidence

**Owner: Market Analyst, and Research Analyst when the idea depends on anything beyond exchange data.**

The Desk Lead asks for exactly what sizing and execution will need: current mid, mark and oracle; funding now and predicted; open interest and 24h volume; depth within 5/10/25 bps for the intended size; the market's constraints (max leverage, margin tiers, size decimals, minimum order value); recent range and volatility. Research adds anything scheduled or breaking that touches the market. Both write their blocks under `## evidence` with sources and UTC times.

Done when: the Risk Manager has every input it needs and nothing is older than a few minutes.

## 2. Risk sign-off

**Owner: Risk Manager.** Reads `risk-limits.md`, reads live account state, computes size from the user's stop and risk budget, checks every gate, and writes PASS or REJECT with exact ticket fields under `## risk` (procedure and arithmetic in `desk-risk-limits`).

A PASS produces the **ticket**:

```
TICKET HG-20260816-01 | mainnet | account 0xabc...def
market: ETH-PERP (asset 1)      side: buy      size: 0.51 ETH (~$1,530)
entry: limit 3,000.0 Gtc        reduce-only: no
stop: sell 0.51 trigger 2,900 market (worst 2,755, 5% bound), normalTpsl with the entry
take-profit: none               leverage: 3x cross (set before entry if different)
slippage tolerance: 10 bps from ticket price at send time
risk: $51.00 = 0.5% of equity $10,200.00 (clearinghouseState 14:11 UTC), R = 100 USD/ETH
risk sign-off: PASS 14:12 UTC, risk-limits.md v3
expires: 14:42 UTC
approve with: "approve HG-20260816-01"
```

A REJECT names one failed gate and what would have to change. A REJECT ends the lifecycle for that proposal unless the user changes the idea (new evidence, new stop, new size) - then it goes back to stage 1 under the same id with a note.

## 3. User approval

**Owner: the user.** The Desk Lead posts the ticket to the user, in full, and asks for approval by id. Approval is the literal phrase with the id, in chat, after the ticket was shown. "Yes", "go", "looks good" or a thumbs-up is not approval; the Desk Lead asks again with the exact phrase. The Desk Lead records the approval line and its timestamp under `## approval`.

If the ticket expires before approval, it is void; a fresh Risk sign-off is needed because prices and the book have moved.

## 4. Execution

**Owner: Execution Trader.** Runs the pre-send checklist in `desk-execution-protocol`, sends the ticket as one action, and records the request, `cloid`, response and timestamps under `## execution`. Anything other than a clean response is handled per `desk-incident-response`.

## 5. Reconciliation

**Owner: Execution Trader.** Confirms from the exchange record - `orderStatus` by cloid, `openOrders`, `userFills`, `clearinghouseState` - what happened, and writes it under `## reconciliation`. Posts the execution report on the floor and DMs the Trade Reviewer. Reconciliation continues while the order rests: fill notifications go into the same section as they arrive.

## 6. Review

**Owner: Trade Reviewer.** Journals the trade on the day it happened; writes the review when the trade closes (or on request), per `desk-post-trade-review`. Sets `status: closed` in the proposal file.

## Adjustments and exits are trades too

Moving a stop, adding to a position, reducing, closing, changing leverage: each is a new ticket under the same proposal id with a suffix (`HG-20260816-01-B`), goes to the Risk Manager for a PASS, and needs the user's approval by that id. Closing a position at market needs a ticket stating the reduce-only size read live from the account and the slippage bound.

The only exception is a pre-authorised protective action the user has written into `desk.md` (for example "the Execution Trader may cancel orphaned stops after a position closes without asking"). Even then, the action is journaled.

## Paper trading and testnet

The lifecycle is identical on testnet. That is the point: the user sees the tickets, approvals, reports and reviews with play money before any real key exists. Strategist-generated signals enter at stage 0 as proposals like any other idea.

## Definitions of done

- A proposal is **live** once stage 5 shows a resting or filled order.
- A proposal is **closed** once the position is flat, orphaned orders are cancelled, and stage 6 is written.
- A proposal is **void** if it expired or was rejected without a retry.

## Common failure modes and the fix

| Symptom | Fix |
| --- | --- |
| The user asks the Execution Trader directly to "just buy some" | Execution Trader routes to the Desk Lead; a proposal is opened; the lifecycle runs. It can be quick, but it runs. |
| Risk PASS was computed from a brief older than the ticket | Risk re-reads live state; PASS is re-issued with a new expiry. |
| Approval given with "yes" | Desk Lead re-asks for the phrase with the id. |
| Two Bots each think they own the next step | The proposal file's `status` line names the stage; the stage owner acts. |
| Ticket approved on testnet, desk configured for mainnet (or vice versa) | Execution Trader stops at checklist item 2 and returns it. |
