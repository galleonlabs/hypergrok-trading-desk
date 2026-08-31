# Set up the HyperGrok trading desk

You are a Grok Bot building the user's Hyperliquid trading desk from this repository. Follow this file top to bottom. When you finish, the user has seven specialist Bots (six on a **Trading Floor** group chat, one reviewer by DM), seventeen shared skills, a prepared desk computer and a written desk record. Setup is read-only: no keys, no orders.

If your runtime loaded this repository as a plugin, invoke `hypergrok-bootstrap`: it performs this runbook, rings a live zero-key Opening Bell first, and returns the same receipt. This file remains the complete manual path and source of truth.

## 1. Get the repository onto the desk computer

```bash
mkdir -p /workspace && cd /workspace
git clone --depth 1 --branch v1.4.0 https://github.com/galleonlabs/hypergrok-trading-desk.git hypergrok
cd /workspace/hypergrok && git rev-parse HEAD && bash scripts/check.sh
```

`v1.3.0` is a release tag, not a moving branch: the desk you build is the desk that was reviewed. Check the repository's releases for a newer tag before setting up, and do not swap the tag for `main` to pick up unreleased work.

`git clone` prints `warning: refs/tags/... is not a commit!` when it shallow-clones an annotated tag. That warning is expected and harmless: the clone still resolves to the tagged commit, which is why the command prints it. Judge the step by `scripts/check.sh`, not by that line.

`scripts/check.sh` is the desk's own structural check: it runs offline, touches no keys and no network, and confirms the skills, agents and manifests are intact and internally consistent. If it fails, stop and tell the user; do not build a desk from a tree that does not check out.

Record the commit `git rev-parse HEAD` printed. It goes into `desk.md` in section 8 as `instructions commit`, so the desk can always say which version of these rules it is running and the user can diff it later.

If the clone fails, do not fall back to downloading a loose archive over plain HTTP and unpacking it. An unverified tarball is exactly the thing a trading desk should refuse: nothing in it can be checked against the repository it claims to come from. Instead ask the user to attach the archive to the conversation, unpack it to `/workspace/hypergrok`, and run `bash scripts/check.sh` before going further.

## 2. Read the desk

Read these before creating anything:

1. `docs/ARCHITECTURE.md` - how the team fits together.
2. `skills/hypergrok-bootstrap/SKILL.md` and `skills/desk-operating-model/SKILL.md` - setup and the rules every Bot follows.
3. All seven files in `agents/` - each has a **Bot profile** (Name, Job, Description) and a full **System prompt**.
4. `skills/README.md` - the index of skills and which Bot uses which.

## 3. Prepare the desk computer (read-only)

Follow `skills/hyperliquid-setup/SKILL.md` sections 1-3 only: install the Python SDK, confirm both Hyperliquid networks answer a public read, and create the working folders:

```bash
mkdir -p /workspace/trading-desk/{proposals,briefs,research,strategies,data,journal/incidents,watch}
cd /workspace/hypergrok
python3 scripts/opening_bell.py --coin ETH
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
```

Show the Opening Bell output to the user. It is a timestamped public market snapshot and must say that it is not a trading signal. A `desk.md` warning from the doctor is expected until section 8; a repository or public API failure is not. The API wallet steps come later, when the user asks to trade. Research, briefs and the strategy lab need no key.

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
- **Avatar:** the desk mascot at `/workspace/hypergrok/assets/mascot.jpg` (attach it from the computer, or let the user pick their own).
- Then send the new Bot its full **System prompt** section as its first message, prefixed with: "These are your standing instructions. Confirm you have read them and state your job in one sentence." Ask it to keep the instructions in memory and to re-read its file at `/workspace/hypergrok/agents/<name>.md` whenever it is unsure.

Grok Bot lets existing Bots create focused Bots. If you can create them, do so now. If you cannot, give the user the seven profile cards as labelled copy-and-paste blocks and wait until they confirm the Bots exist. Seven Bots, not one: the separation between the Bots that read and the one Bot that writes is the design.

## 5. Install the skills

Skills in Grok Bot are shared across all of the user's Bots. Inspect the shared skills first: a Desk Lead added from the public HyperGrok template already carries this release's reviewed set, and setup must not create duplicates.

For each directory under `skills/`, read `SKILL.md` and compare its `name` and instructions with the shared skill when one exists. A matching skill is enabled and recorded as `template`. A missing skill is saved unchanged and recorded as `installed`. If the app cannot save a skill of that length, save a short pointer skill instead: "When this skill is used, read `/workspace/hypergrok/skills/<name>/SKILL.md` and follow it," and record `pointer`. A same-name skill with different instructions that cannot be replaced by the reviewed file is a `mismatch` and fails readiness. The receipt must list exactly seventeen unique names and one status for each; a name alone is not proof that its content is current.

Skills to install (17):

- Bootstrap: `hypergrok-bootstrap`
- Hyperliquid: `hyperliquid-setup`, `hyperliquid-market-data`, `hyperliquid-account`, `hyperliquid-orders`, `hyperliquid-positions`, `hyperliquid-websocket`, `hyperliquid-advanced`, `hyperliquid-api-reference`
- Desk: `desk-operating-model`, `desk-trade-lifecycle`, `desk-risk-limits`, `desk-execution-protocol`, `desk-monitoring`, `desk-post-trade-review`, `desk-incident-response`, `desk-strategy-lab`

Tell each Bot which skills are its own (listed in its agent file's frontmatter). Any Bot may read any skill; the Execution Trader is the one Bot that acts on the write paths in `hyperliquid-orders`, `hyperliquid-positions` and `hyperliquid-advanced`.

## 6. Create the Trading Floor

Create one group chat named **Trading Floor** with exactly these six Bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader. (Grok Bot group chats hold up to six Bots; the Trade Reviewer works from its own conversation and by direct message.)

Post this as the first message in the group:

> Welcome to the Trading Floor. Desk Lead routes; Market Analyst and Research Analyst bring evidence; Strategist helps the user test their own ideas; Risk Manager sizes and can refuse; Execution Trader is the one Bot that sends orders, on a ticket the user approved by id. Rules: `/workspace/hypergrok/skills/desk-operating-model/SKILL.md`. Trade Reviewer is a DM away. Today is setup: nothing goes to the exchange.

## 7. Approvals

Ask the user to open **Settings, General, Auto-review** and add a **Require Approval** rule for financial actions and for commands that call the Hyperliquid exchange endpoint. If the rule syntax cannot express that exactly, say so; the desk's own protocol still holds: the Execution Trader sends only after the user writes "approve <ticket id>" in chat. Exchange writes always stay behind approval.

That rule is the gate. The approval phrase in chat is the desk's record that the user agreed, but the Bots write the floor's messages, so it cannot be the only thing standing in the way of a send. Set the rule up here, not later.

Then ask the user one question: **may the desk place a protective stop for a position that has none, without waiting for approval?** It is reduce-only, so it can only reduce exposure, and the alternative is a naked position waiting on someone to read a message. If yes, record it in `desk.md` under `standing approvals` with the date. If no, record that too, along with how long the desk should chase them before telling them to fix it in the Hyperliquid app themselves.

## 8. Write the desk record

Ask the user two questions, then write `/workspace/trading-desk/desk.md`:

1. Engagement level: **research** (no key), **testnet** (play money, recommended to start), or **mainnet**.
2. If testnet or mainnet: the Hyperliquid account address the desk should read (the main account, not an API wallet address).

```markdown
# Desk record

- created: 2026-08-16 15:00 UTC
- instructions commit: 0000000        # git rev-parse HEAD from step 1
- engagement level: testnet
- network: testnet
- account: 0x...            # or "none" for a research desk
- bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer
- group chats: Trading Floor (6)
- risk limits: not yet written  (Risk Manager runs the interview: skills/desk-risk-limits)
- standing approvals: none          # recommended: protective stops (reduce-only), any network
- unprotected position deadline: 15m  # chase this long, then tell the user to fix it in the app
- status: research-only until an API wallet is provisioned
```

Then hand the Risk Manager the `desk-risk-limits` interview to write `risk-limits.md` with the user. It is short, and it is where the user decides how the desk trades for them.

## 9. Verify the desk (read-only)

Run the desk-wide checks first:

```bash
cd /workspace/hypergrok
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
python3 scripts/opening_bell.py --coin BTC
```

Then run these and record the results:

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
- the desk doctor and Opening Bell results
- the results of the five verification checks
- confirmation that setup stayed read-only: no key requested, no order placed

Then say: "The desk is ready. Ask the Desk Lead for a market brief to see it work. When you want to trade with play money, say 'set up a testnet API wallet' and it will walk you through `hyperliquid-setup` step 4."

## If you are not Grok Bot

Grok Build, Cursor and Claude Code load `agents/`, `skills/` and `rules/` from this repository as a plugin. The same seven roles apply; group chats become subagents or role-labelled passes, and the approval model is unchanged.

In Claude Code, add this repository as a marketplace and install the plugin:

```
/plugin marketplace add galleonlabs/hypergrok-trading-desk
/plugin install hypergrok@hypergrok
```

In Grok Build and Cursor, open the repository and enable the plugin.

Either way, run `/desk-operating-model` to begin.
