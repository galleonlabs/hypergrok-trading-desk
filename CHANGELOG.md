# Changelog

## Unreleased

+ Docs: the exact Claude Code install commands (`/plugin marketplace add galleonlabs/hypergrok-trading-desk`, `/plugin install hypergrok@hypergrok`), verified end to end against the published repository. "Open the repository, enable it" was not an actionable path in Claude Code even though the marketplace manifest shipped and worked.
+ `docs/ARCHITECTURE.md` now lists the `.claude-plugin/` manifests alongside the others.
+ `scripts/check_manifests.py`: install commands in the docs must name a marketplace and plugin id this repository actually declares.

## 1.0.0 - 2026-08-16

Launch.

+ `SETUP.md`: the file a Grok Bot follows to build the desk - repository onto the computer, seven Bots from profile cards, sixteen shared skills, one Trading Floor group chat, approval rules, desk record, read-only verification, receipt.
+ Seven roles with full system prompts: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer.
+ Eight Hyperliquid skills: setup and API wallets, market data, account reads, orders, positions and margin, WebSocket, advanced actions, API reference. `curl` for reads, official Python SDK and `@nktkas/hyperliquid` for writes; snippets verified against the live API.
+ Eight desk skills: operating model, trade lifecycle, risk limits and sizing, execution protocol, monitoring, post-trade review, incident response, strategy lab.
+ Docs: how the desk works, FAQ, provenance. Mascot in `assets/`.
+ `scripts/check.sh` and CI: frontmatter, links, one-writer rule.
+ Plugin manifests and rule so Grok Build, Cursor and Claude Code load the same roles and skills.
