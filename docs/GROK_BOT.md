# Grok Bot role map

Start by giving Grok Bot [`BOOTSTRAP.md`](../BOOTSTRAP.md). That file tells it what to create, what to verify and what receipt to return.

If Grok can create the seven Bots itself, let it. If it asks you to do that part, create the seven named Bots and use the files below for each profile. Skills can be taught conversationally, pasted as written instructions or uploaded as files.

| Bot | Role instructions | Skills to teach or upload |
| --- | --- | --- |
| Desk lead | `agents/desk-lead.md` | `crew-bootstrap`, `desk-setup` |
| Market analyst | `agents/market-analyst.md` | `hyperliquid-intelligence` |
| Onchain analyst | `agents/onchain-analyst.md` | `defillama-research`, `coingecko-research` |
| Risk officer | `agents/risk-officer.md` | `thesis-construction`, `pretrade-risk` |
| Execution trader | `agents/execution-trader.md` | `order-execution` |
| Portfolio manager | `agents/portfolio-manager.md` | `portfolio-control` |
| Trade reviewer | `agents/trade-reviewer.md` | `posttrade-review`, `incident-response` |

## Setup boundary

The setup is complete only when Grok returns the receipt requested in [`BOOTSTRAP.md`](../BOOTSTRAP.md). If the `hypergrok` command is unavailable, the Bots remain a research-and-review team. A Bot name is not a credential boundary because Bots owned by one user share a cloud computer and sign-ins.

## Official references

+ [Grok Bot: Bots](https://docs.x.ai/grok-bot/bots)
+ [Grok Bot: skills, routines and automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
+ [Grok Bot: connect plugins](https://cursor.com/help/grok-bot/connect-plugins)
+ [Grok Build: skills, plugins and marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces)
+ [Grok Build: subagents](https://docs.x.ai/build/features/subagents)
