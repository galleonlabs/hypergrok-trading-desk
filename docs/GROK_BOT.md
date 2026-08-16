# Grok Bot setup

Grok Bot and Grok Build are different products. This repository is directly packageable as an Agent Plugin for Grok Build and Cursor. Grok Bot's public documentation does not currently provide an arbitrary GitHub-repository installer, and its Plugins screen primarily connects services. Do not treat a fetched README or repository URL as proof of installation.

## Create the desk manually

1. Open Grok Bot's **Bots** area and create seven named Bots: desk lead, market analyst, onchain analyst, risk officer, execution trader, portfolio manager and trade reviewer.
2. Copy the matching file from [`agents/`](../agents/) into each Bot's instructions.
3. Use Grok Bot's documented skill creation routes to teach, paste or upload the relevant [`skills/*/SKILL.md`](../skills/) files. Skills can be created conversationally, from written instructions, by file upload or with **Teach a task**.
4. Give the desk lead the prompt in [`BOOTSTRAP.md`](../BOOTSTRAP.md). If the product exposes Bot collaboration, verify each named Bot before delegating. If it does not, run explicit role-separated passes and say so.
5. Treat this as an instruction-only research and review team. Grok Bot's public integration surface does not install or expose HyperGrok's local Python CLI, so this mode cannot run `doctor`, query through the CLI or submit an order.

| Bot | Role instructions | Skills to teach or upload |
| --- | --- | --- |
| Desk lead | `agents/desk-lead.md` | `crew-bootstrap`, `desk-setup` |
| Market analyst | `agents/market-analyst.md` | `hyperliquid-intelligence` |
| Onchain analyst | `agents/onchain-analyst.md` | `defillama-research`, `coingecko-research` |
| Risk officer | `agents/risk-officer.md` | `thesis-construction`, `pretrade-risk` |
| Execution trader | `agents/execution-trader.md` | `order-execution` |
| Portfolio manager | `agents/portfolio-manager.md` | `portfolio-control` |
| Trade reviewer | `agents/trade-reviewer.md` | `posttrade-review`, `incident-response` |

## What is not automatic

+ A bare repository URL does not install this package in Grok Bot.
+ HyperGrok cannot silently create persistent sibling Bots through a documented public API.
+ The manual Grok Bot team cannot access the guarded execution gateway without a separately documented tool integration, which this repository does not ship.
+ A Bot name is not a credential boundary. Grok Bots for one user share a cloud computer, files and sign-ins.
+ Creating the crew does not approve builder fees, fund an account or enable order execution.

## Official references

+ [Grok Bot: Bots](https://docs.x.ai/grok-bot/bots)
+ [Grok Bot: skills, routines and automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
+ [Grok Bot: connect plugins](https://cursor.com/help/grok-bot/connect-plugins)
+ [Grok Build: skills, plugins and marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces)
+ [Grok Build: subagents](https://docs.x.ai/build/features/subagents)
