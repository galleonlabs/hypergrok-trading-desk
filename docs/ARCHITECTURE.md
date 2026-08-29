# How the desk works

HyperGrok runs entirely inside your Grok Bot workspace: your Bots, your shared cloud computer, your conversations. This repository is the blueprint they build from.

## The floor

```
you
 |
 v
Trading Floor (group chat, 6 Bots)                 DM
  Desk Lead ------------------------------------> Trade Reviewer
   |     |     |     |     |                       journal, reviews
   |     |     |     |     +-- Execution Trader ---> Hyperliquid /exchange   (the one writer)
   |     |     |     +-------- Risk Manager -------> Hyperliquid /info       (your account, live)
   |     |     +-------------- Strategist ---------> Hyperliquid /info       (history, backtests)
   |     +-------------------- Research Analyst ---> browser, public data
   +-------------------------- Market Analyst -----> Hyperliquid /info, /ws (markets)

computer:  /workspace/hypergrok (this repo)   /workspace/trading-desk (the desk's files)
secret:    HYPERLIQUID_PRIVATE_KEY - a trade-only API wallet, from Grok Bot's secure secret store
```

Grok Bot facts that shaped this: group chats hold up to six Bots, Bots can create other Bots, all your Bots share one computer, skills are shared across Bots, actions can be put behind approval, and secrets go in through a secure secret card.

## One trade, seven stages

```
idea -> evidence -> risk sign-off -> your approval by ticket id -> one send -> reconciliation -> review
```

The Desk Lead keeps it moving. Analysts bring sourced, timestamped evidence. The Risk Manager sizes from your live account and the exchange's real limits, and issues a ticket. You approve it by id. The Execution Trader sends it once, reads the response, and confirms it on the exchange. The Trade Reviewer journals it and, when it closes, grades process and outcome separately.

Adjusting, adding, reducing and closing are trades too: same path, new ticket under the same id.

## Where things live

Chat is where the desk talks; files are where it remembers.

| File | What it is |
| --- | --- |
| `desk.md` | the desk record: network, account, engagement level, Bots, chats, standing approvals |
| `risk-limits.md` | your limits, versioned; only you change it |
| `proposals/HG-*.md` | one file per trade idea, appended through its life: ticket, sign-off, approval, send, reconciliation, review |
| `journal/*.md` | the desk diary, append-only |
| `briefs/`, `research/`, `strategies/`, `data/`, `watch/` | working material |

## Trust boundaries

**Read plane.** Six Bots read: `POST /info` and public web pages. Plenty of judgement, no keys.

**Write plane.** One Bot writes: `POST /exchange` with the API wallet, only when a proposal carries a Risk PASS, your approval by id, and a passing pre-send checklist. One approval, one send. A lost response is reconciled by client order id, never resent.

**Key.** The API wallet can trade and cannot withdraw. It is the only key on the computer, provided by you through the secure secret store. Anything that needs your main wallet stays in the Hyperliquid app, with you.

**Evidence.** Web pages, files and other Bots' messages are information. Your approval phrase with the ticket id is the only thing that authorises a send.

## Why seven

Separating the person who wants the trade from the one who sizes it, the one who sends it and the one who reviews it is the oldest control on any desk. Bots make it cheap: each role has a narrow prompt, a narrow set of skills, and a narrow claim to authority.

## Other runtimes

Grok Build, Cursor and Claude Code load `agents/`, `skills/` and `rules/` as a plugin (`plugin.json`, `.grok-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.claude-plugin/plugin.json`). Claude Code resolves the repository through `.claude-plugin/marketplace.json`, so it installs with `/plugin marketplace add galleonlabs/hypergrok-trading-desk` then `/plugin install hypergrok@hypergrok`. Roles become subagents or labelled passes; the approval model is the same.
