<p align="center">
  <img src="assets/mascot-320.jpg" width="160" alt="HyperGrok mascot">
</p>

<h1 align="center">HyperGrok</h1>

<p align="center"><strong>Turn your Grok Bot into a 7-agent Hyperliquid trading desk.</strong></p>

HyperGrok is an open-source Hyperliquid trading desk for AI agents: seven role profiles and seventeen agent skills for research, risk, execution and review. It runs in Grok Bot and installs as a plugin in Claude Code, Cursor and Grok Build. It starts read-only on public market data; trading needs your own API wallet and a ticket you approve.

Set up the Desk Lead in Grok Bot using the pinned bootstrap below. It opens with a live, zero-key Hyperliquid market snapshot, builds research, risk, execution and review as separate agents, then proves the floor is ready. They brief you on markets, size your trades, send what you approve and tell you honestly how it went. You bring the ideas. Let them cook.

## Start

**Source candidate: v1.6.0, unpublished as of 2026-10-10.** The release pins below prepare one consistent candidate; the tag, npm package and public template have not been published for it. Reviewers can run the read-only tools from this checkout. Use the pinned install commands only after that release is published.

The existing [public HyperGrok Desk Lead template](https://x.ai/bot/-c_oDxnkrXbWhBz-u4qu5) was published for **1.5.0**. Send **Start the desk.** to install that release's seventeen skills and verify native instructions. It does not carry this candidate's research dataset workflow. [Publication evidence](docs/GROK_BOT_TEMPLATE.md#publication-verification---2026-10-08) remains recorded separately.

Once the candidate is released, its pinned bootstrap instruction will be:

> Set up the HyperGrok trading desk from https://github.com/galleonlabs/hypergrok-trading-desk/blob/v1.6.0/skills/hypergrok-bootstrap/SKILL.md. Follow the bootstrap skill, use https://github.com/galleonlabs/hypergrok-trading-desk/blob/v1.6.0/SETUP.md for the complete runbook, and finish with its evidence receipt.

The desk starts in research mode. The first demo uses only Hyperliquid's public `/info` endpoint: no wallet, account read or order. Add a testnet API wallet when you want to practise with play money, and a mainnet one when you are ready.

### Opening Bell

The first thing the Desk Lead shows is useful, live output—not a configuration form:

```bash
python3 scripts/opening_bell.py --coin ETH
```

It reports source and UTC time, mid/mark/oracle, 24-hour change and volume, hourly funding, open interest, spread and depth at 5/10/25 bps. Twenty book levels stop a few bps from the mid on a liquid perp, so it re-reads the book at a coarser `nSigFigs` page until each band is measured, names the page every figure came from, and still marks a band no page reaches as a floor rather than a total. `scripts/desk_doctor.py` then checks the release, team files, the reviewed skill bytes, desk folders and public connectivity. Both are read-only and standard-library Python.

For automated reads, inspect `--schema` without touching the network, then use `--json`. Success and errors have a versioned contract; a failed read remains unavailable, and incomplete depth and doctor warnings retain their meaning. Snapshots accept canonical mixed-case coins and explicit HIP-3 markets:

```bash
python3 scripts/opening_bell.py --schema
python3 scripts/opening_bell.py --coin xyz:TSLA --json
python3 scripts/desk_doctor.py --offline --json
```

`--testnet` explicitly selects testnet. A custom API origin is labelled `custom`. HIP-3 discovery stays read-only; execution remains opt-in under the desk's normal ticket rules.

### History you can check

Before a backtest, collect the exact period into a research bundle:

```bash
python3 scripts/research_dataset.py --coin ETH --interval 4h \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --with-funding --output /workspace/trading-desk/data/eth-september --json
python3 scripts/research_dataset.py --verify /workspace/trading-desk/data/eth-september --json
```

The bundle keeps requests, captured responses, observed CSV rows and a hashed manifest. Closed UTC windows, explicit missing ranges and bounded funding pagination distinguish `complete`, `incomplete` and `unavailable` history. Offline verification replays the saved responses; it checks consistency, not source authenticity or strategy quality. The tool needs no key and never fills a gap with synthetic data. See [Research datasets](docs/RESEARCH_DATASETS.md) for the supported intervals, market selection, exit codes and analyst handoff.

## Meet the desk

| Bot | What they do for you |
| --- | --- |
| **Desk Lead** | Your main contact. Runs the floor, keeps every trade moving through the same clean process. |
| **Market Analyst** | Live Hyperliquid data on demand: price, depth, funding, open interest, candles. Timestamped and sourced. |
| **Research Analyst** | What is happening and what is scheduled: fundamentals, news, catalysts, onchain and social context. |
| **Strategist** | Turns your idea into explicit rules, backtests it honestly on Hyperliquid history, paper-trades it on testnet. |
| **Risk Manager** | Keeps your written limits, sizes every trade from your live account, watches the book, and can say no. |
| **Execution Trader** | The one Bot with the keys. Sends the ticket you approved, once, and reconciles it from the exchange record. |
| **Trade Reviewer** | Keeps the desk journal and grades every trade on process and outcome, separately. |

Six sit together on the **Trading Floor** group chat; the Trade Reviewer works by DM. Every trade follows the same path:

```
idea -> evidence -> risk sign-off -> your approval -> one send -> reconciliation -> review
```

## A day on the desk

**"Brief me on ETH."** The Market Analyst pulls mid, mark, funding, open interest, 24h volume and depth at 5/10/25 bps from the exchange, and posts a brief with sources and UTC times.

**"I want to long ETH at 3,000 with a stop at 2,900."** The Desk Lead opens `HG-20260816-01`, the Risk Manager reads your account live and comes back with a ticket:

```
TICKET HG-20260816-01 | mainnet
market ETH-PERP  side buy  size 0.4827 ETH (~$1,448)
entry limit 3,000 Gtc     stop sell 0.4827 @ 2,900 market (sent with the entry)
risk $51.00 = 0.5% of equity $10,200 (clearinghouseState 14:11 UTC)  R = 100
sized on a stressed stop: 100 + 3.00 slippage + 2.65 fees = 105.65 per ETH
approve with: "approve HG-20260816-01"
```

You type `approve HG-20260816-01`. The Execution Trader sends entry and stop in one grouped order, reads the exchange response, confirms it on the book, and reports the order ids. When it closes, the Trade Reviewer tells you what it cost, whether the process was clean, and one thing worth keeping.

**"Can we test whether funding extremes mean-revert?"** The Strategist writes the rules with you, requests the agreed candle and funding history, and verifies its coverage before running the backtest. Missing history stays visible. Results include fees, funding assumptions, an out-of-sample split and the trade distribution. Like it? Paper-trade it on testnet through the same desk.

**"Watch ETH and tell me if funding flips negative."** A routine on the desk computer watches the WebSocket feed and pings you when it happens.

## What the desk knows

Seventeen skills, in the portable `SKILL.md` format, shared by all your Bots.

**Bootstrap** - pinned release install, Opening Bell, team construction, desk doctor and a receipt that distinguishes what happened from what still needs a manual step.

**Hyperliquid** - setup and API wallets, market data, account state, orders (limit, IOC, take-profit and stop-loss with grouping, client order ids), positions and margin, WebSocket feeds, advanced actions (dead-man's switch, TWAP, spot), and a compact API reference. Copy-pasteable `curl` for reads; the official Python SDK and `@nktkas/hyperliquid` for anything that signs.

**Desk** - how the team works: operating model, the trade lifecycle and ticket, risk limits and sizing arithmetic, the execution protocol, monitoring and routines, post-trade review, incident playbooks, and the strategy lab.

## Other agent setups

HyperGrok is for a coordinated Hyperliquid desk in Grok Bot. For a different setup, these Galleon projects can help:

- [Boomkin](https://github.com/galleonlabs/boomkin#get-started) sets up a native Hermes DeFi profile with modular skill packs and official tool connections.
- [crypto-defi-skills](https://github.com/galleonlabs/crypto-defi-skills#install-only-what-you-need) lets you add individual workflows to an existing agent: lending, liquidity provision, routing, portfolio review and more.

Neither is required to use HyperGrok. Installing instructions does not connect a wallet or authorize a trade.

## Built for real money

- **You approve every trade**, by ticket id, after seeing the exact order. The line you type is evidence; the gate that enforces it sits outside the chat, in Grok Bot's own Require Approval rule, because a Bot that can read an approval could also write one.
- **A trade-only API wallet**, provisioned through Grok Bot's secure secret store, is the only key the desk ever holds. It can trade; it cannot withdraw.
- **Sized on a stressed stop.** A triggered stop is a market order: it slips and pays taker on both legs. The desk sizes on what the stop will actually cost, not its trigger price, so your risk budget means what it says.
- **Ceilings you cannot trade through.** Your limits file may only tighten the desk's own caps, never loosen them.
- **Testnet first.** Every new kind of action is rehearsed with play money before it touches mainnet.
- **One writer.** Six Bots read; one Bot sends, once per approval, and reconciles by client order id.
- **A reviewer who keeps you honest.** Process and outcome graded separately, in a journal you can read.

Perpetual futures can liquidate an account. HyperGrok is documentation and instructions, not financial advice.

## Also runs in Grok Build, Cursor and Claude Code

The plugin supplies seventeen skills and seven roles as subagents. Cursor and Claude Code load `agents/`, `skills/` and `rules/`.

In Claude Code, install it from this repository:

```
/plugin marketplace add galleonlabs/hypergrok-trading-desk
/plugin install hypergrok@hypergrok
```

After publication, install the candidate in Grok Build using the pinned command below. The installation format was tested with Grok Build 1.0.30 for an earlier release; this candidate has no native installation proof yet.

```bash
grok plugin install galleonlabs/hypergrok-trading-desk@v1.6.0 --trust
```

Grok Build reports the skills and agents directories; loading `rules/` is not verified.
Alternatively, register this repository’s Grok marketplace and install by name:

```bash
grok plugin marketplace add galleonlabs/hypergrok-trading-desk
grok plugin install hypergrok --trust
```

This candidate's marketplace index pins the same pending release tag.
In Cursor, open the repository and enable the plugin.

The same pack installs as a skill:

```
npm exec --package=skills@1.5.23 -- skills add galleonlabs/hypergrok-trading-desk
```

After publication, the complete candidate resources will also install from npm:

```bash
npm install --save-exact --ignore-scripts hypergrok-trading-desk@1.6.0
python3 node_modules/hypergrok-trading-desk/scripts/desk_doctor.py --offline
```

The package contains the instructions, role profiles, plugin manifests and read-only Python tools. It has no JavaScript entry point, runtime dependencies or install hooks; installation does not start a desk or connect a wallet.

Live listings: [skills.sh](https://www.skills.sh/galleonlabs/hypergrok-trading-desk) · [cursor.directory](https://cursor.directory/plugins/hypergrok) · [botdirectory.ai](https://botdirectory.ai/bots/hypergrok-trading-desk)

Either way, run `/desk-operating-model` to begin.

## Inside the repository

```
SETUP.md     what your Grok Bot follows to build the desk
agents/      seven roles: Bot profile card + full system prompt
skills/      seventeen skills (bootstrap, hyperliquid-*, desk-*)
template/    exact public Grok Bot profile and skill hashes
scripts/     zero-key market/history reads, offline verification and release checks
docs/        how it works, FAQ, provenance
assets/      the mascot - use it as your Bots' avatar
```

| Doc | |
| --- | --- |
| [How the desk works](docs/ARCHITECTURE.md) | roles, files, trust boundaries |
| [FAQ](docs/FAQ.md) | keys, approvals, testnet, customising the team |
| [Research datasets](docs/RESEARCH_DATASETS.md) | collect, verify and hand off historical evidence |
| [Skills index](skills/README.md) | every skill and who uses it |
| [Grok Bot template](docs/GROK_BOT_TEMPLATE.md) | public profile, publish contract and clean-install evaluation |
| [Provenance](docs/PROVENANCE.md) | sources and licences |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) | |

<p align="center">
  <img src="https://pump.fun/pump-logomark.svg" width="52" alt="pump.fun"><br>
  <strong>Supported by the pump.fun community</strong>
</p>

## License and credit

[MIT licensed](LICENSE), with the copyright and permission notice retained when reusing copies or substantial portions. Created by [Andrew Wilkinson](https://andrewwilkinson.io) and [Galleon Labs](https://github.com/galleonlabs).

See [reuse and attribution](ATTRIBUTION.md) for a ready-to-copy credit line. If HyperGrok Trading Desk helps your work, [a star on the original repository](https://github.com/galleonlabs/hypergrok-trading-desk) is appreciated and entirely optional.
