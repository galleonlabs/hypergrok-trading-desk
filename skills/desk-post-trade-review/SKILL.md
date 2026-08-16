---
name: desk-post-trade-review
description: The Trade Reviewer's procedure for journaling desk activity and reviewing trades from the exchange record - process graded separately from outcome, execution costs measured, one repeatable finding per review, plus the weekly desk review. Use after any send, when a trade closes, on the weekly routine, or when the user asks "how did that go".
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: desk
---

# Journal and post-trade review

The journal is the desk's memory; the review is how the desk learns. Both come from the exchange record first and chat second, and both keep process and outcome apart.

## 1. Journal

`/workspace/trading-desk/journal/YYYY-MM-DD.md`, one file per active day, appended in time order. Entry types:

```
14:02 UTC  HG-20260816-01 opened   ETH-PERP long idea (user); status evidence
14:12 UTC  HG-20260816-01 risk     PASS 0.51 ETH, stop 2,900, 0.5% risk (risk-limits v3)
14:29 UTC  HG-20260816-01 approval "approve HG-20260816-01" (user)
14:31 UTC  HG-20260816-01 sent     buy 0.51 @ 3,000 Gtc + sl 2,900 (normalTpsl); cloid 0x9f3e...; oid 1839201122, sl waitingForFill
16:05 UTC  HG-20260816-01 fill     0.51 @ 3,000.0 maker, fee $0.31
09:12 UTC  HG-20260816-01 closed   tp 3,089.6, fee $1.52; position flat; sl 2,900 cancelled 09:13
10:00 UTC  limits                  risk-limits v3 -> v4: max positions 3 -> 4 (user, reason: adding HYPE)
11:40 UTC  incident INC-20260817-01 send timeout on HG-20260817-02, reconciled not-on-exchange, re-approved and sent 11:47
18:00 UTC  note                    maker entries at 10 bps depth filled within 2h on both attempts this week
```

Rules: append only; corrections are new lines with `correction:`; every line has a UTC time and an id where one exists; no opinions in the journal (those go in reviews).

## 2. Trade review

Trigger: a proposal reaches `closed`, or the user asks. Inputs, always listed with timestamps:

- the proposal file (ticket, PASS, approval, execution, reconciliation)
- `userFills` / `userFillsByTime` for the window: price, size, fee, side, `crossed`
- `historicalOrders` and `orderStatus` by cloid: what rested when, what cancelled
- `userFunding` for the holding window
- optionally the Market Analyst's depth read at send time for expected slippage

Compute:

| Measure | How |
| --- | --- |
| Entry slippage | (fill avg - ticket price) / ticket price in bps, signed against the trade |
| Exit slippage | same for the exit versus its ticket or trigger price |
| Fees | sum of fill fees in USD and as bps of notional; note maker vs taker |
| Funding | sum of funding payments over the window, USD |
| Net result | realised PnL after fees and funding, in USD and in R (R from the ticket) |
| Protection | was a reduce-only stop resting on the exchange for the entire life of the position? gaps in minutes |
| Lifecycle | each stage present, in order, with timestamps; approval by id; single send; reconciled |
| Holding time | fill to flat |

Grade:

- **Process:** clean / minor break / major break, with the specific stage named. A major break is any send without PASS or approval by id, any missing protection, any resend on unknown result, or any limit breached.
- **Outcome:** result in R and USD, stated without adjectives.

Then **one thing**: a leak, a control that worked, or a break, chosen because it is repeatable. Not a list.

Write the review under `## review` in the proposal file and send the block (format in `agents/trade-reviewer.md`) by DM to the Desk Lead and the user. Set `status: closed`.

## 3. Weekly desk review

From the journal, proposals and the exchange record for the week:

- proposals opened / rejected / voided / executed / closed
- trades closed: count, hit rate, average win and loss in R, expectancy in R (mean of results), largest loss, largest drawdown in equity terms from `portfolio` or start/end equity
- costs: fees and funding in USD and as a share of gross PnL
- process: number of breaks by type; incidents and their status
- limits: any changes and why
- one pattern worth the user's attention, stated as a fact pattern

No recommendations about what to trade. If the user asks, hand strategy questions to the Strategist and sizing questions to the Risk Manager.

## 4. Incident review

For each `INC-YYYYMMDD-NN`: timeline (journal + exchange record), what the desk did, what the controls did, exposure during the incident, root cause where knowable, one corrective action with an owner and a date. Blameless in tone, exact in fact. Written to `/workspace/trading-desk/journal/incidents/<id>.md` and linked from the daily journal.

## Pitfalls

- Reviewing from chat memory rather than fills. Chat says what people meant; fills say what happened.
- Letting the outcome colour the process grade. Grade process first, then look at the outcome.
- Counterfactuals ("if we had held..."). Not evidence.
- Ten findings per review. Nobody acts on ten.
- Editing old journal lines. Append a correction.
