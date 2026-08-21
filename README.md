# Trading Harness

> **NO LIVE TRADING.** This repository cannot place, amend, or cancel an order.
> It contains no exchange SDK, signer, key loader, or enabled venue adapter.

This fork is being rebuilt as a deterministic, testable harness for researching
and validating trading theses. The current foundation establishes typed domain
objects, canonical intent hashing, policy admission boundaries, durable local
records, and an execution boundary that fails closed. It does **not** claim a
profitable strategy and is not ready to control capital.

## Current status

The foundation is pre-alpha and suitable for local development only:

- Python 3.11 or newer; runtime dependencies are standard-library only.
- Public Hyperliquid perpetual market briefs are available through an
  allowlisted, read-only `/info` client with exact decimal parsing and freshness
  checks. It cannot access an account or the `/exchange` endpoint.
- Semantic intents can be normalized and fingerprinted deterministically.
- Risk and authorization policies can be evaluated without venue access.
- Persisted admission is limited to local `infrastructure_testnet`
  `simulate_order` commands; strategy, mainnet, and systematic grants are
  rejected by the foundation.
- The local store supports development and recovery tests; it is not yet a
  production database or immutable ledger.
- `DisabledVenueAdapter` is the only shipped execution adapter and rejects
  every venue mutation, regardless of environment variables.
- The command-line interface is read-only. It provides diagnostics and intent
  hashing; there is no execute command.

Mainnet/testnet exchange writes, paper trading, autonomous trading, signing,
and credential loading are all out of scope for this foundation release.

## Architecture direction

```text
untrusted research / agent output
              |
              v
typed thesis and deterministic validation
              |
              v
canonical semantic intent
              |
              v
deterministic policy/admission scaffolding + durable reservation
              |
              v
isolated signer/executor (NOT IMPLEMENTED)
              |
              v
venue writes (DISABLED)
```

Agents may eventually gather evidence, propose falsifiable theses, and explain
results. They must remain outside the capital-bearing path: they cannot hold
keys, approve their own work, change promoted rules, or call venue write APIs.
See [the harness specification](docs/trading_harness_spec.md) for the proposed
trust boundaries, validation gates, and staged path toward any future trading.

## ChatGPT/Codex first, OpenCode second

The Python core and tool service are agent-runtime neutral. The primary
interface is the installable [`trading-desk` plugin](plugins/trading-desk),
which packages five ChatGPT/Codex skills and one MCP server. OpenCode consumes
the same skills and exact same MCP tools through the checked-in configuration.

The MCP server exposes only:

- `get_harness_status`: prove execution and credential loading are disabled.
- `get_market_brief`: read a fresh public Hyperliquid perp brief with mid,
  mark, oracle, hourly funding, open interest, 24h notional volume, spread, and
  depth at 5/10/25 bps.
- `validate_trade_intent`: validate an intent schema and calculate its canonical
  hash. It does not perform risk review, create authorization, or submit an
  order.

The packaged workflows are:

- [`AGENTS.md`](AGENTS.md) for durable repository guidance.
- [`$operate-trading-desk`](plugins/trading-desk/skills/operate-trading-desk/SKILL.md)
  for manager-style lifecycle coordination.
- [`$brief-market`](plugins/trading-desk/skills/brief-market/SKILL.md) for the
  typed public market-data tool.
- [`$validate-thesis`](plugins/trading-desk/skills/validate-thesis/SKILL.md) for frozen,
  falsifiable strategy evaluation.
- [`$scan-signals`](plugins/trading-desk/skills/scan-signals/SKILL.md) for read-only
  registered-rule observations.
- [`$test-strategy`](plugins/trading-desk/skills/test-strategy/SKILL.md) for
  leakage-resistant historical test plans and artifact review.
- [`opencode.json`](opencode.json), which defaults actions to `ask`, denies
  unlisted shell commands, external-directory access, secret/database files,
  and `git push`, and allows only those five skills and the three exact
  read-only MCP tools.

The plugin copy under `plugins/trading-desk/skills` is canonical. The mirror
under `.agents/skills` exists for repository-native Codex and OpenCode
discovery; CI rejects drift. A generated copy of `trading_harness` under the
plugin makes a cached plugin independent of the repository checkout; CI also
requires that runtime to be byte-identical to `src/trading_harness`. No OpenAI
or OpenCode model SDK is imported by the core. The optional MCP dependency is a
protocol adapter over the same pure Python `ToolService` used by tests. Venue
writes remain a separate qualification.

Do not run OpenCode with `--auto` in this repository. OpenCode documents that
auto mode approves requests that would otherwise ask; explicit deny rules
remain enforced, but the review checkpoint would be lost.

## Run locally

No installation is required to inspect or test the foundation:

```bash
export PYTHONPATH=src
python3 -m trading_harness.cli doctor
python3 -m unittest discover -s tests -v
python3 -m compileall -q -f src tests
```

For an editable command-line installation, create an isolated environment and
install the local package:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-deps -e .
trading-harness doctor
```

The package has no runtime dependencies. The local build step uses setuptools.

### Run the ChatGPT/Codex plugin locally

Install the pinned optional MCP runtime into the isolated environment:

```bash
python -m pip install -e '.[mcp]'
```

Codex can load [`plugins/trading-desk`](plugins/trading-desk) directly. For
local Streamable HTTP protocol qualification, run:

```bash
trading-harness-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The local endpoint is `http://127.0.0.1:8000/mcp`. ChatGPT cannot connect to a
bare loopback URL: developer mode requires the
[Secure MCP Tunnel or a reachable HTTPS endpoint](https://developers.openai.com/plugins/deploy/connect-chatgpt).
Public binding is deliberately rejected because this foundation has no
user-authentication layer. Production publication requires a separately
deployed authenticated HTTPS endpoint; it does not enable exchange writes.

When using OpenCode, activate this environment before starting OpenCode so its
local MCP process resolves the pinned dependency. Do not use OpenCode `--auto`.

## Read-only CLI

Inspect the safety posture:

```bash
trading-harness doctor
```

Hash a schema-valid semantic-intent JSON document without sending it anywhere:

```bash
trading-harness hash-intent path/to/intent.json
```

Intent hashing is an identity primitive, not approval, risk admission, a trade
signal, or permission to execute.

## Upstream legacy material

The inherited model-specific plugins, trading prompts, order snippets, and setup
instructions have been removed from the working tree. They remain available
through Git history and the recorded upstream provenance for audit; they are
**not** active controls, production code, or evidence that live execution is
safe.

The replacement workflows package selected upstream research and desk knowledge
without copying any private-key loader or direct exchange-write snippet. They
cannot issue orders, position sizes, approvals, or deployment grants, and they
are not imported by the deterministic Python core.

The audit record is in [UPSTREAM.md](UPSTREAM.md), with source dispositions in
[docs/hypergrok_audit_matrix.md](docs/hypergrok_audit_matrix.md).

## Safety and contribution policy

- Never add real credentials, account identifiers, approval tokens, or wallet
  material to this repository, fixtures, logs, issues, or CI.
- A venue adapter, signer, credential path, or order command requires a separate
  design review and explicit implementation milestone; it must not be smuggled
  into a research or CLI change.
- Tests must continue to prove that the default executor is disabled and that
  environment variables cannot enable it.

This software is experimental research infrastructure, not financial advice.
Perpetual futures and other leveraged products can cause losses beyond expected
stop levels and may liquidate an account.

MIT licensed; see [LICENSE](LICENSE).
