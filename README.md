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

Mainnet, testnet, paper trading, autonomous trading, signing, and exchange API
writes are all out of scope for this foundation release.

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

## Codex and OpenCode interfaces

The Python core is agent-runtime neutral. Codex is the first supported
interface and OpenCode is a compatible second interface through:

- [`AGENTS.md`](AGENTS.md) for durable repository guidance.
- [`$validate-thesis`](.agents/skills/validate-thesis/SKILL.md) for frozen,
  falsifiable strategy evaluation.
- [`$scan-signals`](.agents/skills/scan-signals/SKILL.md) for read-only
  registered-rule observations.
- [`opencode.json`](opencode.json), which defaults actions to `ask`, denies
  unlisted shell commands, external-directory access, secret/database files,
  and `git push`, and exposes only the two repository skills.

Both products natively read root `AGENTS.md` and the open agent-skill layout
under `.agents/skills`. The skills contain workflow instructions only and
are not imported by the Python package. No OpenAI/OpenCode SDK, model call,
connector, MCP server, or plugin is required for the deterministic foundation.
A future MCP/plugin boundary may expose controlled read tools; venue writes
remain a separate qualification.

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

The replacement workflows under `.agents/skills/` are read-only research
interfaces for thesis validation and signal scanning. They cannot issue orders,
position sizes, approvals, or deployment grants, and they are not imported by
the deterministic Python core.

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
