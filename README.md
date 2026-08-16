# HyperGrok

**Create your Hyperliquid trading desk on Grok Bot.**

HyperGrok is an original Agent Plugin from Galleon Labs. It gives Grok Bot a
small trading team rather than one prompt wearing seven hats: market and
onchain research, thesis construction, deterministic risk, disclosed execution,
portfolio control and post-trade review.

Private v0.1 is the foundation. It is testnet-first, source-led and deliberately
rather difficult to make trade by accident.

## What ships

+ 7 specialist agents with explicit handoffs
+ 10 workflow skills for Hyperliquid, DefiLlama and CoinGecko
+ A Python CLI for live read-only research, sizing and hashed order plans
+ Two-phase execution with exact confirmation, expiry, caps, cloid duplicate
  checks and live builder approval gates
+ No deposits, withdrawals, transfers, bridges or unattended scheduler

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
hypergrok health
hypergrok market BTC
hypergrok size --equity 10000 --entry 100 --stop 95 --risk-pct 0.5 --max-notional 1000
```

Grok Bot plugin installation from a private GitHub repository is still being
verified against the live product. The repository follows the Agent Plugins
manifest plus the observed `.grok-plugin` layout. Do not mistake a plausible
click path for a tested one. For SKILL-compatible agents, clone the repository
or use `npx skills add galleonlabs/hypergrok-trading-desk` once it is public.

## Order flow

1. Research and risk skills produce cited evidence and a bounded size.
2. `hypergrok plan-order` writes an immutable, expiring JSON plan and SHA-256.
3. The user reviews the side, size, price, account, network and disclosed fee.
4. `hypergrok execute-order --plan ... --confirm <sha256> --execute` rechecks
   every gate immediately before the one send.
5. A timeout is an unknown result, never permission to retry. Reconcile by
   cloid first.

## Builder fee disclosure

Hyperliquid builder codes are not text referral codes. Each supported order
contains:

```json
{"b": "0xC141Cbe4f4a9CAbc3cc78159a9268a4e008922CD", "f": 10}
```

`f=10` is **1 basis point** of filled notional, paid to the Galleon builder.
The user's main wallet must approve a maximum fee for this address first and
may revoke it at any time. HyperGrok does not automate that approval.

The candidate Galleon Treasury address had **0 USDC** Hyperliquid perps account
value when checked on 16 August 2026. Hyperliquid requires at least 100 USDC and
standard account abstraction, so monetisation is currently inactive. The
`health` and `builder-status` commands check live state rather than trusting
this dated receipt. Execution fails closed whilst the builder is ineligible.

## Data sources

+ Hyperliquid official `/info` and `/exchange` APIs
+ DefiLlama documented free protocol endpoint, with paid MCP/API left optional
+ CoinGecko keyless, Demo or Pro REST API

Source lineage and licences are recorded in [docs/PROVENANCE.md](docs/PROVENANCE.md).

## Safety model

Private keys are read only at execution time and never persisted or printed.
Mainnet is disabled unless explicitly enabled. Every plan expires, contains its
builder attribution and cloid, and is covered by an exact hash. The sole send
path revalidates the signing account, live price drift, notional cap, builder
balance, user approval and duplicate cloid. Tests assert zero sends when a gate
fails.

Perpetual futures can liquidate an account. This is software, not financial
advice, and no strategy or return claim is made.

## Roadmap to public launch

+ Verify private installation in Grok Bot
+ Fund and verify a dedicated eligible Galleon builder account
+ Run testnet execution and incident drills
+ Independent funds-path and security review
+ Add websocket monitoring and strategy packages only after the control plane
  earns it
