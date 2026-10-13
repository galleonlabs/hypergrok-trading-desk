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

The shared Desk Lead starts on the read plane. `hypergrok-bootstrap` installs the pinned release, runs `scripts/opening_bell.py` against public `/info`, prepares the desk, creates the team when the product allows it, and finishes with `scripts/desk_doctor.py`. None of those first-run paths reads a key or calls `/exchange`.

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

## Research evidence

`scripts/research_dataset.py` runs on the read plane. It resolves a perp from public metadata, requests a bounded closed UTC candle window and optionally pages hourly funding. Each new `data/<name>/` bundle stores captured request/response records, normalized observed CSVs and a manifest with source identity, file hashes, coverage gaps and `complete`, `incomplete` or `unavailable` status. Existing bundles are never overwritten.

The Market Analyst hands the complete bundle to the Strategist, who runs offline `--verify` before analysis. Verification replays responses and checks files and claimed coverage; it cannot authenticate an exchange response or prove historical universe membership. A missing hour stays missing. Run records cite the bundle and manifest hash so changed data cannot silently replace a prior experiment. See [Research datasets](RESEARCH_DATASETS.md) for the contract and limitations.

Data completeness, statistical evidence, Risk PASS and a user's exact ticket approval are separate decisions. The dataset tool does not supply historical oracle prices, reconstruct intrabar fills, read an account or touch an exchange write path.

## Trust boundaries

**Read plane.** Six Bots read: `POST /info` and public web pages. Plenty of judgement, no keys.

**Write plane.** One Bot writes: `POST /exchange` with the API wallet, only when a proposal carries a Risk PASS, your approval by id, and a passing pre-send checklist. One approval, one send. Every send is bounded by `expiresAfter`, so a lost response is reconciled by client order id and a replacement waits until the original is provably incapable of arriving. A clean read is not proof; an expired original is.

**Key.** The API wallet can trade and cannot withdraw. It is the only key on the computer, provided by you through the secure secret store. Anything that needs your main wallet stays in the Hyperliquid app, with you.

**Evidence.** Web pages, files and other Bots' messages are information; none of them authorises anything. Your approval phrase with the ticket id is the desk's record that you agreed, but the Bots write the floor's messages, so the phrase alone cannot be the gate: an approval a Bot can read is one a Bot could have written. Enforcement lives outside the conversation, in Grok Bot's native approval control covering SDK, script and HTTP exchange writes. Until that coverage is verified, the desk stays research-only without a provisioned key. No Bot may type, quote forward, infer or simulate your approval.

**Uncertainty.** Missing, stale, gapped or partial data is `unavailable`, a verdict of its own. It never collapses into "the condition did not fire" or "the check passed". A watch that reports silence on a dead feed looks exactly like a calm market, so the desk is required to tell the two apart and say which it has.

## Why seven

Separating the person who wants the trade from the one who sizes it, the one who sends it and the one who reviews it is the oldest control on any desk. Bots make it cheap: each role has a narrow prompt, a narrow set of skills, and a narrow claim to authority.

## Other runtimes

The plugin manifests (`plugin.json`, `.grok-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.claude-plugin/plugin.json`) supply skills and agents to Grok Build, Cursor and Claude Code. Cursor and Claude Code also load `rules/`. Grok Build installs with `grok plugin install galleonlabs/hypergrok-trading-desk@v1.6.0 --trust`. The format was tested with Grok Build 1.0.30 for an earlier release; 1.6.0 has no native install proof, and loading `rules/` in Grok Build is not verified. `.grok-plugin/marketplace.json` uses this repository’s remote URL and release ref because Grok Build rejects a source path pointing at the marketplace root. Claude Code resolves the repository through `.claude-plugin/marketplace.json`, so it installs with `/plugin marketplace add galleonlabs/hypergrok-trading-desk` then `/plugin install hypergrok@hypergrok`. Codex reads `.agents/plugins/marketplace.json` before the Claude-compatible index; its entry pins this repository at the release tag, like the Grok index, and installs with `codex plugin marketplace add galleonlabs/hypergrok-trading-desk` then `codex plugin add hypergrok@hypergrok`. Gemini CLI reads the root `gemini-extension.json` and installs the latest GitHub release with `gemini extensions install https://github.com/galleonlabs/hypergrok-trading-desk`. Both load `skills/` but not `rules/` or `agents/` as subagents, so `GEMINI.md` repeats the desk rule's approval paragraph and `check_manifests.py` fails if it drifts. Roles become subagents or labelled passes; the approval model is the same. Claude Code also loads `hooks/exchange-write-gate.json` through `.claude-plugin/plugin.json`: a `PreToolUse` hook that returns `ask` for Bash commands, or the scripts they name, matching an SDK write method or a raw `/exchange` POST. It never returns `allow` and does not follow imports. The other runtimes do not load it.
