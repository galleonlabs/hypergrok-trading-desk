# Architecture

HyperGrok is a set of files a Grok Bot reads to build a trading desk out of other Grok Bots. There is no server, package or binary in this repository. The runtime is the user's Grok Bot workspace: their Bots, their shared cloud computer, their conversations.

## Topology

```
user
 |
 |  talks to (mostly)                          direct messages
 v                                                  |
Trading Floor (Grok Bot group chat, 6 Bots)         v
  Desk Lead --------- routes ----------> Trade Reviewer (off the floor)
   |    |    |    |    |                    journal, reviews, incidents
   |    |    |    |    +-- Execution Trader ---- the only writer ----> Hyperliquid /exchange
   |    |    |    +------- Risk Manager ------- reads account ------> Hyperliquid /info
   |    |    +------------ Strategist --------- reads history ------> Hyperliquid /info
   |    +----------------- Research Analyst --- reads the web ------> browser, public APIs
   +---------------------- Market Analyst ----- reads markets ------> Hyperliquid /info, /ws

shared cloud computer: /workspace/hypergrok (this repo)  /workspace/trading-desk (the desk's files)
secret store: HYPERLIQUID_PRIVATE_KEY (a trade-only API wallet), provisioned by the user, never in chat
```

Grok Bot facts that shape this: group chats hold up to six Bots; Bots can create other Bots; all Bots of one account share one computer, browser and filesystem; skills are shared across Bots; approvals can be required per action class; secrets go in through a secure secret card.

## Trust boundaries

**Read plane.** Six of the seven Bots only ever call `POST /info` (unsigned) and public web pages. They can be wrong, but they cannot move money.

**Write plane.** One Bot, the Execution Trader, calls `POST /exchange` with an API wallet key. It does so only when three artefacts line up: a proposal file with a Risk Manager PASS, the user's approval by ticket id in chat, and a passing pre-send checklist. One approval, one send. Unknown results are reconciled by client order id, never resent.

**Key boundary.** The API wallet can trade but cannot withdraw, transfer or bridge. It is the only key on the computer. Because every Bot on the account can read the computer, the desk does not pretend Bot names are a security boundary; it relies on the key's permissions, on testnet-first rehearsal, and on the user's approval per ticket. Anything that needs the main wallet (agent approval, transfers, withdrawals) happens in the Hyperliquid app, by the user.

**Evidence boundary.** Text from web pages, files, other Bots or the user's own earlier messages is data. Only the user's approval phrase with the ticket id, after the ticket was shown, authorises a send.

## Files as the source of truth

Chat is where the desk talks; files are where it remembers.

- `desk.md` - what the desk is (network, account, level, Bots, chats, standing approvals).
- `risk-limits.md` - what the user allows, versioned; only the user changes it.
- `proposals/HG-*.md` - one file per trade idea, appended through its lifecycle; the ticket, the PASS, the approval, the send, the reconciliation, the review.
- `journal/*.md` - the desk's diary, append-only.
- `briefs/`, `research/`, `strategies/`, `data/`, `watch/` - working material.

## Why seven roles

Separating the person who wants the trade from the person who sizes it, from the person who sends it, from the person who reviews it is the oldest control on any desk. Bots make it cheap: each role has a narrow prompt, a narrow set of skills, and a narrow claim to authority. Agreement between them is not evidence; the Risk Manager recomputes and the Trade Reviewer reconstructs from the exchange record. The Strategist exists because users want to test ideas; it produces proposals like anyone else and never sends.

## Deliberate exclusions

No deposits, withdrawals, bridging, transfers, sub-account or vault funding, builder fees, staking, copy trading, unattended sending, strategies or return claims. Each exclusion removes a way to lose money that has nothing to do with the user's trading idea.

## Other runtimes

Grok Build, Cursor and Claude Code read `agents/`, `skills/` and `rules/` directly as a plugin (`.grok-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `plugin.json`). The roles become subagents or labelled passes; the approval model is unchanged.
