# HyperGrok

[![CI](https://github.com/galleonlabs/hypergrok-trading-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/galleonlabs/hypergrok-trading-desk/actions/workflows/ci.yml)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Create a risk-bounded Hyperliquid trading desk with Grok Build, Cursor or a manually configured Grok Bot.**

HyperGrok packages seven specialist agents, eleven original skills and a guarded Python execution gateway. It covers Hyperliquid market and account intelligence, DefiLlama and CoinGecko research, thesis construction, deterministic risk, portfolio control, execution and post-trade review.

Both Hyperliquid **testnet and mainnet** are supported. Testnet is the safe default; mainnet requires an explicit acknowledgement and passes through the same plan, risk, builder, API-wallet and duplicate-order gates.

## The desk

| Role | Responsibility |
| --- | --- |
| Desk lead | Routes evidence and approvals between specialists |
| Market analyst | Hyperliquid structure, liquidity, open interest and funding |
| Onchain analyst | Protocol, token, governance and dependency research |
| Portfolio manager | Whole-book exposure, margin and protection state |
| Risk officer | Independent sizing and deterministic rejection gates |
| Execution trader | The sole guarded order path |
| Trade reviewer | Plan-versus-effect and execution-quality review |

The Cursor plugin manifest discovers `agents/`, `skills/` and the persistent team rule. Start with [BOOTSTRAP.md](BOOTSTRAP.md). Grok Bot documents collaboration between named Bots but not a public API for silently creating them, so `crew-bootstrap` verifies the installed product and falls back to plugin agents or role-separated passes rather than inventing a one-click claim.

## Install

```bash
git clone https://github.com/galleonlabs/hypergrok-trading-desk.git
cd hypergrok-trading-desk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
hypergrok doctor
```

For development:

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest -q --cov=hypergrok --cov-fail-under=75
python -m build
```

## Install on the right surface

**Grok Build and Cursor:** import this repository as an Agent Plugin. Both surfaces discover `rules/`, `agents/` and `skills/`; run `crew-bootstrap` after enabling it.

**Grok Bot:** its public Plugins screen installs service connectors, not repository Agent Plugins. Grok Bot does not currently document arbitrary GitHub-repository installation, a shell/tool bridge for this CLI or programmatic creation of sibling Bots. [docs/GROK_BOT.md](docs/GROK_BOT.md) describes a manual, instruction-only team for research and review. It cannot use the guarded execution gateway unless a future documented integration exposes the installed CLI. [BOOTSTRAP.md](BOOTSTRAP.md) contains the exact owning-Bot prompt. Do not claim the repo URL installed anything unless the runtime confirms each role and skill.

## Read both networks

```bash
# Testnet, the default
hypergrok doctor
hypergrok market BTC

# Mainnet read-only and execution configuration
HYPERGROK_NETWORK=mainnet \
HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND \
hypergrok doctor

HYPERGROK_NETWORK=mainnet \
HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND \
hypergrok market BTC

# All public data-source smoke checks, no key or signing
python scripts/live_smoke.py
```

`doctor --user 0x...` separates endpoint health, builder balance, account-abstraction mode and that user's fee approval. It reports `execution-ready` only when every builder gate passes.

## Research and sizing

```bash
hypergrok account 0xYourTradingAccount
hypergrok defillama hyperliquid
hypergrok coingecko hyperliquid
hypergrok size \
  --equity 10000 \
  --entry 100 \
  --stop 95 \
  --risk-pct 0.5 \
  --max-notional 1000
```

Outputs are JSON. Hyperliquid reads include their network, source and observation time. Missing or stale data must remain unknown rather than becoming a trading opinion.

## Guarded order flow

1. Specialists produce cited research and deterministic risk inputs.
2. `hypergrok plan-order` writes a short-lived plan and exact SHA-256.
3. The user reviews account, network, side, size, limit, expiry, cloid and fee.
4. `hypergrok execute-order --plan ... --confirm <sha256> --execute` rechecks every live gate.
5. The official Hyperliquid SDK performs the sole send using a narrowly authorised API wallet.
6. A private local journal atomically reserves the plan before the send. Any exception or timeout is an unknown result. Never retry; reconcile the cloid first.

Supported fills include a **1 bp Galleon builder fee** (`f=10`) bound to the configured builder address. Hyperliquid requires the user's main wallet to approve that maximum for the address, and the user can revoke it. HyperGrok never automates approval. The builder must also hold at least 100 USDC perps account value and use standard account-abstraction mode; `doctor` checks live state before any send.

## Safety boundaries

+ Never provide a seed phrase or main-wallet key. Use a scoped Hyperliquid API wallet.
+ All Grok Bots belonging to one user share a cloud computer and sign-ins. Bot names are not credential boundaries.
+ Mainnet requires `HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND`.
+ Plans are network-bound, hash-bound, capped and valid for at most 30 minutes.
+ Strict address, cloid, decimal, slippage and expiry validation runs before signing.
+ The API wallet's live `userRole` must point to the planned trading account.
+ No deposits, withdrawals, transfers, bridging, reward claims or unattended execution.
+ No automatic retry after the only send.

See [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PROVENANCE.md](docs/PROVENANCE.md).

## Status

Version 1.0.0 is the first public release of the guarded command surface. Read-only data, sizing, planning and fail-closed execution logic are verified. Live funded submission has not been exercised and remains unavailable until the fixed builder is eligible and the user approval passes. No strategy, profitability or autonomous 24/7 trading claim is made.

Perpetual futures can liquidate an account. This software is not financial advice.
