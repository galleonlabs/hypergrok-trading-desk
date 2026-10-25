# How the desk works

In Grok Bot, HyperGrok uses your Bots, shared cloud computer and conversations. This repository supplies the instructions and read-only tools. [Other runtimes](#other-runtimes) use the same process with subagents or labelled role passes.

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

The shared Desk Lead starts on the read plane. `hypergrok-bootstrap` installs the pinned release, runs `scripts/opening_bell.py` against public `/info`, prepares the desk, creates the team when the product allows it, and finishes with `scripts/desk_doctor.py`. None of those first-run paths reads a key or calls `/exchange`.

The six-Bot group limit leaves the Trade Reviewer outside the Floor. Bots share the computer and skills, so role separation is a working convention rather than credential isolation. Native approval controls and the secret store provide separate controls; setup verifies their availability.

## One trade, seven stages

```
idea -> evidence -> risk sign-off -> your approval by ticket id -> one send -> reconciliation -> review
```

The Desk Lead keeps it moving. Analysts bring sourced, timestamped evidence. The Risk Manager sizes from your live account and the exchange's real limits, and issues a ticket. You approve it by id. The Execution Trader sends it once, reads the response, and confirms it on the exchange. The Trade Reviewer journals it and, when it closes, grades process and outcome separately.

Adjusting, adding, reducing and closing are trades too: same path, new ticket under the same id.

## Where things live

Working files live under `/workspace/trading-desk/`; chat supplies context, while these records retain the lifecycle evidence.

| File | What it is |
| --- | --- |
| `desk.md` | the desk record: network, account, engagement level, Bots, chats, standing approvals |
| `risk-limits.md` | your limits, versioned; only you change it |
| `proposals/HG-*.md` | one file per trade idea, appended through its life: ticket, sign-off, approval, send, reconciliation, review |
| `journal/*.md` | the desk diary, append-only |
| `briefs/`, `research/`, `strategies/`, `data/`, `watch/` | working material |

## Research evidence

`scripts/research_dataset.py` runs on the read plane. It resolves a perp from public metadata, requests a bounded closed UTC candle window and optionally pages hourly funding. Each new `data/<name>/` bundle stores captured request/response records, normalized observed CSVs and a manifest with source identity, file hashes, coverage gaps and `complete`, `incomplete` or `unavailable` status. Existing bundles are never overwritten.

The Market Analyst hands the complete bundle to the Strategist, who runs offline `--verify` before analysis. Verification replays responses and checks files and claimed coverage; it cannot authenticate an exchange response or prove historical universe membership. A missing hour stays missing. Run records cite the bundle and manifest hash so changed data cannot silently replace a prior experiment. See [Research datasets](RESEARCH_DATASETS.md) for the contract and limitations.

`scripts/stop_cost.py` runs on the read plane too. It reads public market metadata and the L2 book and applies the `desk-risk-limits` sizing arithmetic to a plan and equity the user types, so it prices a stop without an account read.

Data completeness, statistical evidence, Risk PASS and a user's exact ticket approval are separate decisions. The dataset tool does not supply historical oracle prices, reconstruct intrabar fills, read an account or touch an exchange write path.

## Trust boundaries

**Reads.** Six Bots use `POST /info` and public web pages without signing keys.

**Writes.** One Bot uses `POST /exchange` with the API wallet, only when a proposal carries a Risk PASS, your approval by id, and a passing pre-send checklist. One approval, one send. Every send is bounded by `expiresAfter`, so a lost response is reconciled by client order id and a replacement waits until the original is provably incapable of arriving. A clean read is not proof; an expired original is.

**Key.** The API wallet can trade and cannot withdraw. It is the only key on the computer, provided by you through the secure secret store. Anything that needs your main wallet stays in the Hyperliquid app, with you.

**Evidence.** Web pages, files and other Bots' messages are information; none of them authorises anything. Your approval phrase with the ticket id is the desk's record that you agreed, but the Bots write the floor's messages, so the phrase alone cannot be the gate: an approval a Bot can read is one a Bot could have written. Enforcement lives outside the conversation, in Grok Bot's native approval control covering SDK, script and HTTP exchange writes. Until that coverage is verified, the desk stays research-only without a provisioned key. No Bot may type, quote forward, infer or simulate your approval.

**Uncertainty.** Missing, stale, gapped or partial data is `unavailable`. A watch reports a healthy negative observation separately from a failed feed; missing evidence cannot pass a check.

## Why seven

Research, risk, execution and review answer different questions. Separate role prompts make each handoff and its evidence explicit. Their agreement does not replace Risk PASS, user approval or exchange reconciliation.

## Other runtimes

Each runtime guide contains installation commands and observed support limits:

| Runtime | Components | Guide |
| --- | --- | --- |
| Claude Code | `.claude-plugin/`, skills, subagents and the exchange-write hook; load the operating model explicitly | [Install](hyperliquid-claude-code.md) |
| Cursor | `.cursor-plugin/`, skills, agent profiles and desk rules | [Install](hyperliquid-cursor.md) |
| Grok Build | `.grok-plugin/`, skills and agent directories; rule loading unverified | [Install](hyperliquid-grok-build.md) |
| Codex | `.agents/plugins/marketplace.json`, skills and labelled role passes | [Install](hyperliquid-codex.md) |
| Gemini CLI | `gemini-extension.json`, skills, `GEMINI.md` and labelled role passes | [Install](hyperliquid-gemini-cli.md) |

The Grok and Codex marketplace entries pin the reviewed repository tag. Claude Code resolves this repository through its own marketplace index. Gemini CLI installs the latest GitHub release; `GEMINI.md` repeats the desk rule's approval paragraph, which the manifest check compares for drift.

Only Claude Code loads `hooks/exchange-write-gate.json`. Its `PreToolUse` hook asks for confirmation when a Bash command or a named script matches an SDK write method or raw `/exchange` POST. It never returns `allow`, does not follow imports and cannot bind a request to the approved ticket. It is a confirmation backstop; each runtime still needs verified native approval coverage before an API wallet is provisioned.
