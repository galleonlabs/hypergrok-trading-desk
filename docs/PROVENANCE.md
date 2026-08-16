# Provenance

Everything in this repository is original prose and original snippets written against public documentation and open-source SDK code. Sources were studied for interfaces, conventions and gaps; no third-party skill text, prompts, fixtures or scripts were copied.

| Source | Used for | Licence / notes |
| --- | --- | --- |
| [Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs) (API, trading, onboarding sections, fetched 2026-08-16) | Every endpoint, action, field, limit and error string in the `hyperliquid-*` skills | Public documentation |
| [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) 0.24.0 | Python method signatures and response handling in the skills; the SDK is what the desk installs | MIT |
| [@nktkas/hyperliquid](https://github.com/nktkas/hyperliquid) 0.33.3 | TypeScript client shapes and formatting helpers referenced in the skills | MIT |
| [Grok Bot documentation](https://docs.x.ai/grok-bot) and [Cursor help for Grok Bot](https://cursor.com/help/grok-bot) | Bots, group chats, shared computer, skills, routines, approvals, secrets | Public documentation |
| [Grok Build skills, plugins and marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces) | Plugin layout and SKILL.md compatibility | Public documentation |
| [Agent Skills specification](https://agentskills.io) | SKILL.md frontmatter and directory conventions | Public specification |
| [Senpi skills](https://github.com/Senpi-ai/senpi-skills) | Comparative survey of skill structure and safety patterns only; platform-specific and includes strategy content, none of which was reused | Root MIT with Apache-2.0 declarations in skill files |
| [cezar-r/hyperliquid-skills](https://github.com/cezar-r/hyperliquid-skills) | Comparative survey of a reference-style skill pack; identifier gotchas cross-checked against official docs | MIT |
| [Hermes Agent hyperliquid skill](https://github.com/NousResearch/hermes-agent/tree/main/optional-skills/blockchain/hyperliquid) | Comparative survey of a read-only CLI-backed skill and Hermes frontmatter conventions | MIT |
| kaileycompact51/HyperLiquid-Claw | Reviewed and **not used**. The repository distributes an unverified Windows binary and an obfuscated npm install script; treat with caution. Nothing from it appears here. | Claimed MIT |

## What changed in 2.0

Version 1.x was a Python CLI (`hypergrok`) with its own plan/execute state machine, tests and packaging, wrapped by thin agent and skill files. Version 2.0 removes the CLI entirely. The repository is now instructions and resources for a user's Grok Bot: seven role definitions with full system prompts, sixteen skills that teach the Bots to work with Hyperliquid directly through the official SDKs and `curl`, and a setup file the Bot follows. The safety model moved from code gates to desk procedure plus Hyperliquid's own API-wallet permissions and Grok Bot's approval controls.
