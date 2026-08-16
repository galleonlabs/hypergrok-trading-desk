# Set up the HyperGrok trading desk

You are a Grok Bot. The user has asked you to turn their Grok Bot workspace into a Hyperliquid trading desk using this repository. Follow this file from top to bottom. Do not skip steps, do not invent capabilities, and do not request keys or place orders during setup.

The result is a team of seven Bots (six on a **Trading Floor** group chat, one reviewer off the floor), a shared set of skills for working with Hyperliquid, a prepared desk computer, and a written desk record. It takes about fifteen minutes.

## 1. Get the repository onto the desk computer

```bash
mkdir -p /workspace && cd /workspace
git clone --depth 1 https://github.com/galleonlabs/hypergrok-trading-desk.git hypergrok \
  || (curl -L https://github.com/galleonlabs/hypergrok-trading-desk/archive/refs/heads/main.tar.gz | tar xz && mv hypergrok-trading-desk-main hypergrok)
ls /workspace/hypergrok/agents /workspace/hypergrok/skills
```

If you cannot reach GitHub from the computer, ask the user to attach the repository archive to the conversation and unpack it to `/workspace/hypergrok`.

## 2. Read the desk

Read these before creating anything:

1. `docs/ARCHITECTURE.md` - how the team fits together and what is deliberately excluded.
2. `skills/desk-operating-model/SKILL.md` - the constitution every Bot follows.
3. All seven files in `agents/` - each has a **Bot profile** (Name, Job, Description) and a full **System prompt**.
4. `skills/README.md` - the index of skills and which Bot uses which.

## 3. Prepare the desk computer (read-only)

Follow `skills/hyperliquid-setup/SKILL.md` sections 1-3 only: install the Python SDK, confirm both Hyperliquid networks answer a public read, and create the working folders:

```bash
mkdir -p /workspace/trading-desk/{proposals,briefs,research,strategies,data,journal/incidents,watch}
```

Do **not** do the API wallet steps yet. No key is needed to build the desk, research markets, or run the strategy lab.

## 4. Create the Bots

For each file in `agents/`, create one Bot. Use the profile card exactly:

| File | Name | Job |
| --- | --- | --- |
| `agents/desk-lead.md` | Desk Lead | Head of the Hyperliquid trading desk |
| `agents/market-analyst.md` | Market Analyst | Hyperliquid market data and microstructure |
| `agents/research-analyst.md` | Research Analyst | Fundamentals, news and catalyst research |
| `agents/strategist.md` | Strategist | Strategy design and testing partner |
| `agents/risk-manager.md` | Risk Manager | Risk limits, position sizing and book oversight |
| `agents/execution-trader.md` | Execution Trader | Order execution on Hyperliquid |
| `agents/trade-reviewer.md` | Trade Reviewer | Desk journal and post-trade review |

For each Bot:

- **Name** and **Job** from the profile card.
- **Description** (the Bot's enduring rules) from the profile card, verbatim.
- Then send the new Bot its full **System prompt** section as its first message, prefixed with: "These are your standing instructions. Confirm you have read them and state your job in one sentence." Ask it to keep the instructions in memory and to re-read its file at `/workspace/hypergrok/agents/<name>.md` whenever it is unsure.

Grok Bot lets existing Bots suggest or create focused Bots. If you can create them, do so now. If you cannot, give the user the seven profile cards as clearly labelled copy-and-paste blocks and wait until they confirm the Bots exist. Do not merge seven roles into one Bot; the separation between the Bots that read and the one Bot that writes is the design.

## 5. Install the skills

Skills in Grok Bot are shared across all of the user's Bots. For each directory under `skills/`, read `SKILL.md` and save it as a skill using the `name` in its frontmatter (for example: "Save these instructions as a skill called `hyperliquid-market-data`"). Keep the content unchanged. If the app cannot save a skill of that length, save a short pointer skill instead: "When this skill is used, read `/workspace/hypergrok/skills/<name>/SKILL.md` and follow it."

Skills to install (16):

- Hyperliquid: `hyperliquid-setup`, `hyperliquid-market-data`, `hyperliquid-account`, `hyperliquid-orders`, `hyperliquid-positions`, `hyperliquid-websocket`, `hyperliquid-advanced`, `hyperliquid-api-reference`
- Desk: `desk-operating-model`, `desk-trade-lifecycle`, `desk-risk-limits`, `desk-execution-protocol`, `desk-monitoring`, `desk-post-trade-review`, `desk-incident-response`, `desk-strategy-lab`

Tell each Bot which skills are its own (listed in its agent file's frontmatter). Any Bot may read any skill; only the Execution Trader may act on the write paths in `hyperliquid-orders`, `hyperliquid-positions` and `hyperliquid-advanced`.

## 6. Create the Trading Floor

Create one group chat named **Trading Floor** with exactly these six Bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader. (Grok Bot group chats hold up to six Bots; the Trade Reviewer works from its own conversation and by direct message.)

Post this as the first message in the group:

> This is the Trading Floor of the HyperGrok desk. Desk Lead routes; Market Analyst and Research Analyst bring evidence; Strategist helps the user test their own ideas; Risk Manager sizes and can refuse; Execution Trader is the only Bot that sends orders, only on a ticket the user approved by id. Rules: `/workspace/hypergrok/skills/desk-operating-model/SKILL.md`. Trade Reviewer is reached by DM. Nothing is sent to the exchange today.

## 7. Approvals

Tell the user to open **Settings, General, Auto-review** and add a **Require Approval** rule for financial actions and for commands that call the Hyperliquid exchange endpoint. If the rule syntax cannot express that exactly, say so and rely on the desk's own protocol: the Execution Trader sends only after the user writes "approve <ticket id>" in chat. Never suggest an Always Allow rule for exchange writes.

## 8. Write the desk record

Ask the user two questions, then write `/workspace/trading-desk/desk.md`:

1. Engagement level: **research** (no key), **testnet** (play money, recommended to start), or **mainnet**.
2. If testnet or mainnet: the Hyperliquid account address the desk should read (the main account, not an API wallet address).

```markdown
# Desk record

- created: 2026-08-16 15:00 UTC
- engagement level: testnet
- network: testnet
- account: 0x...            # or "none" for a research desk
- bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer
- group chats: Trading Floor (6)
- risk limits: not yet written  (Risk Manager runs the interview: skills/desk-risk-limits)
- standing approvals: none
- status: research-only until an API wallet is provisioned
```

Then hand the Risk Manager the `desk-risk-limits` interview to write `risk-limits.md` with the user, even on a research desk; it is short and it teaches the user how the desk thinks.

## 9. Verify the desk (read-only)

Run these and record the results:

1. In the Trading Floor, ask: "@Market Analyst brief us on BTC." Expect a timestamped brief with sources.
2. Ask: "@Risk Manager assuming equity of 10,000 USD and the current limits, size a hypothetical long BTC with a 1% stop." Expect a PASS or REJECT with the arithmetic and a ticket, and a note that nothing will be sent.
3. Ask: "@Execution Trader what would you need before sending that ticket?" Expect the pre-send checklist and a refusal to send without approval by id.
4. DM the Trade Reviewer: "Open today's journal and record that the desk was set up." Expect a journal entry.
5. Ask the Research Analyst for one sourced fact about Hyperliquid itself.

## 10. Return the receipt

Finish by giving the user:

- the seven Bots and how each was created (by you, or by the user from the cards)
- the skills installed and how (saved in full, or as pointers to files)
- the Trading Floor group and its members
- the desk record and its engagement level
- the results of the five verification checks
- confirmation that no key was requested, no order was placed, and no Always Allow rule was created

Then say: "The desk is ready. When you want to trade with play money, tell the Desk Lead 'set up a testnet API wallet' and it will walk you through `hyperliquid-setup` step 4."

## If you are not Grok Bot

Grok Build, Cursor and Claude Code load `agents/`, `skills/` and `rules/` from this repository as a plugin. Open the repository, enable the plugin, and run `/desk-operating-model` to begin. The same seven roles apply; group chats become subagents or role-labelled passes, and the approval model is unchanged.
