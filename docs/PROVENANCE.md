# Provenance

HyperGrok is independently implemented. Sources were studied for public
interfaces and product gaps, not copied.

| Source | Use | Licence / status |
| --- | --- | --- |
| Hyperliquid docs and Python SDK `2fdb18f` | API fields, signing client, builder rules | Docs; SDK MIT |
| DefiLlama API docs and skills `f286bda` | Endpoint coverage and research gaps | Public docs; skills repo had no detected licence on 16 Aug 2026, so no code or prose reused |
| CoinGecko docs and skills `fcc056f` | Auth tiers and endpoint coverage | Official skills MIT; no code or prose reused |
| Senpi skills `c3b6df3` | Comparative capability survey only | Root MIT and skill-level Apache-2.0 declarations conflict; no code, prose, fixtures, names or strategies reused |
| Cursor and Grok Bot plugin documentation | Private installation and manifest conventions | Official documentation; Cursor route is a high-confidence inference because Grok Bot has no published GitHub-install contract |
| Agent Plugins schema | Portable manifest interoperability | Public specification |
| Community Grok plugin examples | Packaging evidence only | MIT; not treated as an official contract |

Canonical receipts:

+ https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes.md
+ https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint.md
+ https://github.com/hyperliquid-dex/hyperliquid-python-sdk
+ https://defillama.com/docs/api
+ https://docs.coingecko.com/ai-integration
+ https://docs.x.ai/grok-bot/skills-routines-and-automations
+ https://docs.x.ai/grok-bot/settings-and-notifications
+ https://cursor.com/docs/reference/plugins
+ https://cursor.com/docs/plugins#team-marketplaces
+ https://agent-plugins.org/schemas/1.0.0/plugin.schema.json
