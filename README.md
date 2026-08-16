# HyperGrok

[![CI](https://github.com/galleonlabs/hypergrok-trading-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/galleonlabs/hypergrok-trading-desk/actions/workflows/ci.yml)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Turn your Grok bots into a Hyperliquid trading desk.**

HyperGrok gives your AI agents seven specialist roles, eleven skills and one
guarded order path. They research markets, build a thesis, size it against real
exchange limits, and put a single reviewed order in front of you for approval.

Nothing is sent that you have not read first. HyperGrok never sees your seed
phrase, cannot withdraw or transfer funds, and refuses any order that differs by
a single character from the one you approved.

---

## The desk

| Role | What it does |
| --- | --- |
| **Desk lead** | Routes evidence and approvals between the specialists |
| **Market analyst** | Hyperliquid structure, liquidity, open interest, funding |
| **Onchain analyst** | Protocol, token, governance and dependency research |
| **Portfolio manager** | Whole-book exposure, margin and protection state |
| **Risk officer** | Independent sizing, and the authority to refuse |
| **Execution trader** | The only role that can reach the order path |
| **Trade reviewer** | Plan versus effect, and execution quality |

Six of the seven cannot place an order at all. That separation is the point.

---

## Quick start

You need **Python 3.11+** and about ten minutes. You do **not** need funds, a
wallet or a key to research and plan trades — only to place them.

```bash
git clone https://github.com/galleonlabs/hypergrok-trading-desk.git
cd hypergrok-trading-desk
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
hypergrok quickstart
```

`quickstart` checks your setup in plain English and tells you exactly what to do
next. It never prints a key.

```
HyperGrok setup

Network: testnet  (https://api.hyperliquid-testnet.xyz)
Testnet is the safe default. No real money is at risk here.

Configuration
  [ok] Config file: /your/repo/.env
  [ok] Hyperliquid reachable (2702 markets)

You can research and plan orders. Placing them needs the account steps above.
```

Then try it:

```bash
hypergrok market BTC                 # a live market
hypergrok limits BTC --equity 10000  # what the exchange actually allows you
hypergrok size --equity 10000 --entry 100 --stop 95 --risk-pct 0.5 --max-notional 1000
```

### To place orders as well

Three steps, in order:

1. **Create an API wallet.** At [app.hyperliquid.xyz](https://app.hyperliquid.xyz)
   open **Settings → API** and generate one. It can trade but cannot withdraw.
   **Never** use your seed phrase or main wallet key.
2. **Put it in `.env`.** Set `HYPERLIQUID_ACCOUNT_ADDRESS` to your trading
   account and `HYPERLIQUID_PRIVATE_KEY` to the API wallet's key. `.env` is
   already gitignored.
3. **Run `hypergrok quickstart` again.** It confirms the API wallet is
   authorised for that account.

You stay on testnet until you deliberately opt into mainnet.

---

## Set up your agent team

### Grok Bot

Open Grok Bot and paste this:

> Set up HyperGrok from https://github.com/galleonlabs/hypergrok-trading-desk/blob/main/BOOTSTRAP.md.
> Follow that file from top to bottom, create the seven-role desk, and finish by
> showing me which roles and checks are working.

[`BOOTSTRAP.md`](BOOTSTRAP.md) is the single entry point. If Grok cannot open the
link, download the file and attach it to the conversation.

Grok Bot has no public API for silently creating sibling Bots, so if it cannot
create the seven itself it will hand you seven labelled setup blocks instead. We
would rather tell you that than pretend it is one click. Without the `hypergrok`
command available, a Grok Bot desk is research-and-review only.

### Grok Build or Cursor

The Agent Plugin supplies the roles and skills; the Python install supplies the
`hypergrok` command.

```bash
uv sync --frozen
. .venv/bin/activate
grok --plugin-dir .   # Grok Build
```

Then run `/crew-bootstrap`. In Cursor, open the same repository, enable its local
plugin and run `crew-bootstrap`. The setup receipt confirms whether the CLI is
available before any CLI-backed skill is used.

---

## Placing an order

Two commands, never one. That is deliberate.

**1. Plan it.** Writes the order to a file. Touches no network, signs nothing.

```bash
hypergrok plan-order --account 0xYourAccount --coin BTC --side buy \
  --size 0.001 --limit-px 95000 --out my-order.json
```

**2. Read the file, then run the command it gives you.** The output includes a
`next_command` line you can copy verbatim.

```bash
hypergrok execute-order --plan my-order.json --confirm <sha256> --execute
```

### What the SHA-256 is for

It is a fingerprint of your exact order file. Change one character — size, price,
account, anything — and the fingerprint changes and the order is refused.

You are not expected to read or understand the hash. You copy it. Its job is to
guarantee that what gets sent is byte-for-byte what you reviewed, with no room
for anything to alter it in between. Plans expire after 30 minutes for the same
reason.

### Every gate between you and a send

`execute-order` checks all of these before a signing key is even imported:

| Gate | Refuses when |
| --- | --- |
| Hash match | The plan file changed after you read it |
| Expiry | The plan is stale |
| Network | A testnet plan is run against mainnet, or vice versa |
| Declared account | Your configured account differs from the plan |
| Notional | The order exceeds a ceiling you opted into |
| Price drift | The live price has moved beyond your tolerance |
| Precision | Size or price breaks Hyperliquid tick and lot rules |
| API wallet | The signing key is not an authorised wallet for that account |
| Duplicate | The client order ID was already used |
| Journal | This exact plan already reached the send boundary once |

A timeout is an **unknown** result, not a failure. HyperGrok never retries;
reconcile the order first with `hypergrok order-status`.

---

## Risk limits come from the exchange

HyperGrok imposes **no risk-per-trade or notional ceiling of its own**. The real
constraints are Hyperliquid's, they differ per asset, and they are tiered:

```bash
hypergrok limits BTC --equity 10000
```

reports max leverage, the margin tiers that apply as a position grows, size
decimals and the 10 USD minimum order value.

Headline leverage is the **top tier only** — BTC is 40x below 10k notional, 25x
above it, 10x above 50k — so a size that looks financeable at the headline number
may not be at the size you intend. Judging an appropriate risk budget from those
limits is the risk officer's job; see [`skills/pretrade-risk`](skills/pretrade-risk/SKILL.md).

### Optional guardrails

Opt in if you want the CLI itself to catch a fat-fingered figure. Unset means no
ceiling.

| Setting | Default | Effect |
| --- | --- | --- |
| `HYPERGROK_MAX_RISK_PCT` | unset | Refuse a `size` request above this percent of equity |
| `HYPERGROK_MAX_ORDER_NOTIONAL_USD` | unset | Refuse a plan above this USD notional |

These two are **not** ceilings and always have a value:

| Setting | Default | Effect |
| --- | --- | --- |
| `HYPERGROK_MAX_SLIPPAGE_BPS` | 30 | How far live price may drift from your approved limit |
| `HYPERGROK_MAX_PLAN_MINUTES` | 30 | Longest a plan may stay valid, up to 1440 |

What is **not** configurable is correctness: hash matching, account matching,
API-wallet authorisation, duplicate protection, drift checking and tick rules.
Those are not preferences.

---

## Safety

+ **Never provide a seed phrase or main-wallet key.** Use a scoped Hyperliquid
  API wallet, which can trade but cannot withdraw.
+ **All Grok Bots belonging to one user share a cloud computer and sign-ins.**
  Bot names are not credential boundaries.
+ **Mainnet requires `HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND`.** Testnet is the
  default and stays the default until you say otherwise.
+ **No deposits, withdrawals, transfers, bridging, reward claims or unattended
  execution.** Those commands do not exist in this tool.
+ **No automatic retry** after the only send.

Report a vulnerability via [SECURITY.md](SECURITY.md).

---

## Both networks

```bash
# Testnet, the default
hypergrok doctor
hypergrok market BTC

# Mainnet, explicitly
HYPERGROK_NETWORK=mainnet HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND hypergrok doctor

# Public data-source smoke checks, no key or signing
python scripts/live_smoke.py
```

`hypergrok doctor --user 0x...` separates endpoint health from your own
account-specific execution readiness.

---

## Status

Version 1.0.0. Read-only data, sizing, planning and fail-closed execution logic
are verified by 92 unit tests plus three live verification harnesses covering the
CLI, the packaging and every skill procedure — see [docs/TESTING.md](docs/TESTING.md).

Live funded submission has not been exercised. **No strategy, profitability or
autonomous 24/7 trading claim is made.**

Perpetual futures can liquidate an account. This software is not financial
advice.

---

## Documentation

| Document | Contents |
| --- | --- |
| [BOOTSTRAP.md](BOOTSTRAP.md) | The single entry point for setting up the desk |
| [docs/GROK_BOT.md](docs/GROK_BOT.md) | Which skills belong to which role |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Trust boundaries and the execution gateway |
| [docs/TESTING.md](docs/TESTING.md) | How to verify a checkout yourself |
| [docs/SHAPE.md](docs/SHAPE.md) | What HyperGrok is and is not |
| [docs/PROVENANCE.md](docs/PROVENANCE.md) | Origin of the agents and skills |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

### Development

```bash
python -m pip install -e '.[dev]'
ruff check . && mypy src && pytest -q --cov=hypergrok --cov-fail-under=75
```

MIT licensed. Built by [Galleon Labs](https://github.com/galleonlabs).
