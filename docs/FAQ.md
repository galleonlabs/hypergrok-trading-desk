# FAQ

**What do I actually get?**
Seven Bots in your Grok Bot workspace, one Trading Floor group chat, seventeen shared skills, a live zero-key Opening Bell and a written way of working. Ask for a market brief, a sized trade, a backtest or a review, and the right Bot answers with live data and sources.

**What do I need to start?**
The [public HyperGrok Desk Lead template](https://x.ai/bot/-c_oDxnkrXbWhBz-u4qu5) was published for 1.5.0. Send **Start the desk.** to install that release. This checkout prepares the unpublished v1.6.0 candidate; its dataset tool can be reviewed locally, while the [candidate bootstrap pins](../README.md#start) wait for publication. The desk starts in research mode and proves public market access without asking for a key. Add a testnet API wallet only when you want to practise; add a mainnet one when you decide to trade real money.

**What does the desk doctor inspect?**
The release version and tag pin, all agent and skill files, the desk folders and record, and a public Hyperliquid `allMids` response. It never reads environment variables, keys or account state, and never calls the exchange write endpoint.

**How does the desk trade?**
Through a Hyperliquid API (agent) wallet you create in the Hyperliquid app and hand over through Grok Bot's secure secret card. It can place, modify and cancel orders. Withdrawals, transfers and bridging stay in the app with your main wallet.

**How do I approve a trade?**
The Risk Manager posts a ticket with an id like `HG-20260816-01`. You type `approve HG-20260816-01`. That exact phrase, after seeing that exact ticket, is the desk's record that you agreed, and without it nothing is sent. It is deliberately not the only thing standing in the way: the Bots write the floor's messages, so the enforcing gate is Grok Bot's own Require Approval rule on the exchange write path, outside the conversation. Set it up during setup. No Bot may type, quote forward or infer your approval.

**Where do the ideas come from?**
From you. The Strategist turns your idea into rules and tests it on Hyperliquid history with fees, funding and an out-of-sample split; the desk then paper-trades it on testnet if you like. The Market and Research Analysts give you the picture; you decide.

**Can I verify the history behind a backtest?**
Yes. The candidate's [research dataset tool](RESEARCH_DATASETS.md) saves requests, decoded responses, CSVs and a manifest with file hashes and coverage gaps. `--verify` replays that bundle offline. A missing candle or funding hour prevents complete coverage; absent funding is never assumed to be zero. Integrity and coverage do not prove source authenticity, strategy edge or exact USD funding costs without historical oracle prices.

**Why do the Bots use a "computer"?**
That is Grok Bot's name for the cloud VM every account gets. Reads are plain `curl` against `/info`; the Execution Trader runs the official SDK there to sign orders with the API wallet. The browser is only used for reading pages.

**Why six on the floor and one off it?**
Grok Bot group chats hold six Bots, and reviews are calmer after the noise. The Trade Reviewer works from its own conversation and by DM.

**Can I rename the Bots or add my own?**
Yes. Names and avatars are yours. To add a role, copy the shape of a file in `agents/`. Keep one writer: only one Bot has the exchange write skills.

**Can it run while I sleep?**
Routines can brief you, watch prices and funding, check the book and alert you. Sending always waits for your approval by id.

**Which network does market data come from?**
Mainnet, even when you trade on testnet, because testnet books are thin. Every number says which network it came from.

**What if a send times out?**
It never resends. It checks the exchange by client order id, and if the order is not there it still does not assume it failed, because a delayed send can land minutes later. Every send carries an expiry, so the desk waits for that deadline to pass, checks once more, and only then tells you the send is dead and asks for a fresh approval. You will be told which of those two happened.

**How do I set Grok Bot's own approvals?**
Settings, General, Auto-review: add a Require Approval rule for financial actions and for commands that call the Hyperliquid exchange endpoint. Require Approval wins over Always Allow.

**Does it work outside Grok Bot?**
Yes. The plugin supplies the same skills and agents. After candidate publication, the Grok Build command is `grok plugin install galleonlabs/hypergrok-trading-desk@v1.6.0 --trust`. The format was tested with Grok Build 1.0.30 on an earlier release, not on this candidate; loading `rules/` there is not verified. In Claude Code, use `/plugin marketplace add galleonlabs/hypergrok-trading-desk` then `/plugin install hypergrok@hypergrok`. In Cursor, open the repository and enable the plugin. Cursor and Claude Code also load `rules/`.

**Is this financial advice?**
It is documentation and instructions. Perpetual futures can liquidate an account.
