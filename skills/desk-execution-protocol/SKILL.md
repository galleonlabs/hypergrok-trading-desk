---
name: desk-execution-protocol
description: The Execution Trader's procedure for turning an approved ticket into one Hyperliquid action and reconciling it - the pre-send checklist, order construction rules, single-send discipline, unknown-result handling and the execution report. Use before and after every send, cancel, modify, leverage change or close.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: desk
---

# Execution protocol

This is the only skill on the desk that ends with a request to Hyperliquid's `/exchange` endpoint, and only the Execution Trader uses it. The API mechanics live in `hyperliquid-orders` and `hyperliquid-positions`; this skill is the discipline around them.

## Inputs

- The proposal file `/workspace/trading-desk/proposals/<id>.md` with a Risk Manager PASS and exact ticket fields.
- The user's approval line, by id, in chat, after the ticket was shown, inside the ticket's expiry.
- The desk computer configured per `hyperliquid-setup`: `HYPERLIQUID_NETWORK`, `HYPERLIQUID_ACCOUNT_ADDRESS`, and the API wallet key available to scripts from the environment (never printed).

## Pre-send checklist

Run every item and write the result under `## execution` before sending. Any failure: do not send, name the item, hand back to the Desk Lead.

1. **Ticket integrity.** Id, PASS and approval refer to the same ticket text. If the Desk Lead edited the ticket after the PASS, it goes back to Risk.
2. **Network.** `HYPERLIQUID_NETWORK` equals the ticket's network. Mainnet is never assumed.
3. **Account and wallet.** `HYPERLIQUID_ACCOUNT_ADDRESS` equals the ticket's account. The API wallet still acts for it (a read that requires the agent to be approved, or `extraAgents` for the account, shows the wallet address). If the desk has never sent from this wallet on this network, send a tiny testnet-style rehearsal on testnet first, not on mainnet.
4. **Price still valid.** Fresh mid from `allMids` is within the ticket's slippage tolerance of the ticket price. For a stop or take-profit ticket, the trigger price is on the correct side of the current mark.
5. **Formatting.** Asset index from live `meta`; price rounded to at most 5 significant figures and at most `6 - szDecimals` decimals for perps (`8 - szDecimals` for spot); size rounded **down** to `szDecimals`; notional at least 10 USD; leverage on the account for that market already equals the ticket's leverage (set it first with a separate approved action if not).
6. **cloid.** Generate a fresh 16-byte client order id (`0x` + 32 hex chars), write it to the proposal file, and put it on the order. One cloid per order, never reused.
7. **One action.** Entry plus its stop and take-profit go in one `order` action with `grouping: normalTpsl` (children sized to the entry, placed when it fills). Protection for an existing position is a standalone reduce-only trigger. Anything else in the ticket that is a different action type (leverage change, cancel) is a separate, separately approved step.
8. **Nothing else pending.** No other unreconciled send from this desk in the last few minutes. If there is, reconcile it first.

## Send

- Send exactly once. Do not wrap the send in a retry loop. Set a finite timeout (10-15 seconds is plenty).
- Capture the raw response and the send timestamp to the millisecond.
- Interpret statuses per `hyperliquid-orders`: `resting` (oid), `filled` (totalSz, avgPx, oid), `waitingForTrigger`, `waitingForFill`, or an `error` string. A top-level `status: err` is an action-level rejection: nothing was placed.

## Unknown results

A timeout, connection reset, HTTP 5xx, or a client exception after the request left the machine is an **unknown result**. Treat it as possibly executed:

1. Do not resend.
2. Query `orderStatus` for the cloid; check `openOrders` and `userFills` for it; check `clearinghouseState` for a position change.
3. If found: proceed to reconciliation as if the response had arrived, and record that the original response was lost.
4. If not found after two checks a few seconds apart: report "unconfirmed, not on the exchange" to the Desk Lead. A new send needs a fresh approval by id because the user must know the first one may still appear.

## Reconciliation

Immediately after the response, and again when fills arrive:

- `orderStatus` by cloid or oid: the exchange's view of the order.
- `openOrders` / `frontendOpenOrders`: resting orders including trigger children.
- `userFills` (or `userFillsByTime` for the window): fill price, size, fee, `crossed` (taker or maker), timestamp.
- `clearinghouseState`: position size, entry, leverage, liquidation price, margin used.

Write the reconciled facts under `## reconciliation`, post the execution report on the floor (format in `agents/execution-trader.md`), and DM the Trade Reviewer with the id and the report.

## Cancels, modifies, leverage, closes

Each is its own ticket (suffix `-B`, `-C`...) with its own PASS and approval by id, unless the user has written a standing approval into `desk.md` for that exact class of action on that network.

- **Cancel:** by cloid where you have it, else by oid from `openOrders`. Confirm removal from `openOrders`.
- **Modify:** Hyperliquid's `modify`/`batchModify` cancels a resting limit order and places the replacement in one action (new oid, fresh cloid); the replacement must itself rest (`Alo`, or a non-executable `Gtc`). Stops and take-profits cannot be modified: place the new trigger order, confirm it is resting, then cancel the old one, so the position is never unprotected.
- **Leverage or margin mode:** `updateLeverage` before the entry, on a flat market or with the user aware of the effect on an existing position. Confirm from `clearinghouseState`.
- **Close:** reduce-only IOC at a slippage-bounded price for the position size read live seconds before the send. Then cancel orphaned protective orders and report both.
- **Dead-man's switch:** `scheduleCancel` only when the user asks for it, with the time written in the report; remember it cancels all of the account's open orders when it fires.

## Rehearsal rule

Any action type the desk has not performed before (first TP/SL bracket, first modify, first close, first isolated-margin trade) is rehearsed on testnet with the same ticket format before it is done on mainnet. Record the rehearsal in the journal.

## Report

Post the block from `agents/execution-trader.md` on the floor: sent, response, reconciled, fees/funding, next. Keep the raw response in the proposal file, not in chat.

## Never

- Never send without a PASS and approval by id for this exact ticket.
- Never send from a main-wallet key or with a key pasted in chat.
- Never withdraw, deposit, bridge, transfer, send tokens, approve builder fees or touch vaults and sub-accounts.
- Never resend on unknown result. Never cancel-all as a reflex.
- Never let a routine send.
