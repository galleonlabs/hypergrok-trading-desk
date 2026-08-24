# Trading Harness

> **LIVE TRADING IS NOT ENABLED.** The repository now contains reviewed paper,
> wire, signer, nonce, transport, account-read and reconciliation primitives,
> but no Codex/MCP tool or default runtime can sign or submit an order.

This fork is becoming a Codex-first, agent-runtime-neutral trading desk for
Hyperliquid. It can track an asset, ingest completed candles, calculate
deterministic TA, record sourced sentiment evidence, classify the registered
setup as buy/sell/nothing/unavailable, and evaluate a strategy after costs.
Capital-bearing actions remain behind a separate, unfinished qualification
path.

Profitability is a revocable evidence gate, not a product promise. The first
registered ETH strategy was tested honestly and rejected; the harness will
abstain instead of relabelling a failed backtest as an opportunity.

## Capability status

| Capability | Status |
|---|---|
| Public Hyperliquid brief and completed-candle history | Implemented and live-smoke-tested |
| Local asset registry and always-on research node | Implemented; credential-free |
| Descriptive EMA/RSI/ATR TA | Implemented; research only |
| Registered EMA/Donchian/ATR buy/sell/nothing signal | Implemented |
| Manual X sentiment evidence | Implemented for explicit browser research; never unattended |
| Unattended sentiment | Requires an official X API or compliant provider |
| Costed historical validation and prospective shadow ledger | Implemented |
| Mandatory-stop risk ticket and exact three-leg plan | Implemented |
| Local paper OMS/protection watchdog | Implemented |
| Approval/reservation/outbox/preflight/dispatcher persistence | Implemented; not exposed |
| Read-only account/metadata/reconciliation | Implemented |
| Hyperliquid exact wire, durable nonce, isolated signing and one-shot entry transport | Implemented as disabled primitives |
| Reduce-only close/cancel/noop recovery | Typed and signer-tested, but public signing/submission hard-disabled until durable recovery outbox exists |
| Live Hyperliquid testnet | **Not qualified or enabled** |
| Live Hyperliquid mainnet | **Disabled** |

The only shipped default executor remains disabled. Environment variables
cannot turn venue writes on.

## Honest strategy result

`candidate-v0/1` uses completed 4h bars, EMA(50/200), a Donchian(20)
breakout transition excluding the signal bar, Wilder ATR(14), next-bar fills,
a 1.5 ATR stop, 3 ATR target, and 12-bar time exit.

On 2026-08-24, its first run over the latest 4,999 completed ETH 4h mainnet
bars produced:

- 116 trades;
- mean net expectancy: **-0.0331R**;
- profit factor: **0.9401**;
- one-sided block-bootstrap lower bound: **-0.2484R**;
- maximum drawdown: **19.4628R**;
- negative expectancy under the registered cost stress.

Result: `REJECTED`. That inspected window is failed/discovery evidence; it will
not be tuned until it passes. See [the SMA-outfits disposition](docs/sma_outfits_validation.md)
for how imported indicator claims are handled.

## Architecture

```text
Codex / ChatGPT / OpenCode (no credentials)
        |
        v
bounded MCP research tools + local research database
        |
        +--> completed candles --> descriptive TA
        |                       --> registered signal
        +--> sourced sentiment evidence
        +--> buy / sell / nothing / unavailable
        +--> costed historical + prospective shadow gates
        |
        v
mandatory-stop risk ticket (not trade authority)
        |
        v
trusted local approval + atomic execution store (not MCP/chat)
        |
        v
isolated signer process + one-shot transport (disabled pending testnet)
        |
        v
independent reconciliation + protection watchdog
```

Agents explain and route evidence. Deterministic code owns indicators,
classification, risk arithmetic, hashes, state transitions, signing policy,
and reconciliation. A chat message is never approval.

## Codex/ChatGPT plugin

[`plugins/trading-desk`](plugins/trading-desk) packages six skills and eleven
bounded MCP tools. Three tools write only local research state; none writes to
an exchange.

Research tools:

- `get_harness_status`
- `get_market_brief`
- `track_asset` — local database write
- `pause_tracked_asset` — local database write
- `list_tracked_assets`
- `record_manual_sentiment` — local database write
- `get_latest_sentiment`
- `analyze_asset`
- `validate_candidate_profitability`
- `get_node_status`
- `validate_trade_intent` — schema/hash only, not risk or approval

Use [`$assess-asset`](plugins/trading-desk/skills/assess-asset/SKILL.md) for the
end-to-end research workflow. Other packaged skills cover market briefs,
thesis registration, signal interpretation, backtests, and desk coordination.

Manual X research uses the user's visible signed-in browser session only for
an explicit request. X forbids non-API website scripting, so the always-on node
does not automate the website. It stores post IDs/URLs/hashes/timestamps and
bounded polarity—not raw text, cookies, or tokens—and marks the result
unusable for unattended trading.

OpenCode consumes the same plugin tools and byte-identical skill mirror through
[`opencode.json`](opencode.json). Its local research writes require review;
unlisted shell commands, secret/database reads, external directories, and
`git push` remain denied. Do not use OpenCode `--auto` here.

## Run locally

The research runtime is standard-library-only:

```bash
export PYTHONPATH=src
python3 -m trading_harness.cli doctor
python3 -m unittest discover -s tests -v
python3 -m compileall -q -f src tests
```

For an editable Python 3.11 environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-deps -e .
trading-harness doctor
```

### Run the always-on research node

```bash
trading-harness node run \
  --state-db "$HOME/.local/state/trading-harness/research.sqlite3" \
  --node-id trading-desk-research
```

Inspect it from another terminal:

```bash
trading-harness node status \
  --state-db "$HOME/.local/state/trading-harness/research.sqlite3"
```

The node starts with new risk halted, holds a fenced singleton lease, persists
heartbeats, and degrades on missing/gapped data. It has no account or signer
configuration. See [always-on operation](docs/always_on_operation.md) for
reviewed launchd/systemd templates.

### Run the local MCP server

```bash
python -m pip install -e '.[mcp]'
trading-harness-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The endpoint is `http://127.0.0.1:8000/mcp`. Public binding is rejected because
the local server has no user-authentication layer. ChatGPT requires a reviewed
authenticated HTTPS deployment or secure tunnel; doing that still does not
enable exchange writes.

### Execution development only

The isolated signing boundary lazily accepts exactly the official
`hyperliquid-python-sdk==0.24.0`:

```bash
python3.11 -m venv .venv-execution
source .venv-execution/bin/activate
python -m pip install -e '.[execution]'
```

No key loader is included. A wallet object must be injected by a separate OS
identity/process, and testnet/mainnet require separate accounts, API wallets,
databases, policies and qualification. Installing the extra does not enable a
venue path.

## Testnet before mainnet

Testnet qualification must prove, with a dedicated API wallet and capped
account (see the [full qualification checklist](docs/testnet_qualification.md)):

1. signer/main-account registration and standard account mode;
2. exact CLOID place/query/cancel behavior;
3. full long and short IOC+SL+TP bracket lifecycles;
4. partial fill detection and emergency reduce-only flatten;
5. lost HTTP response recovery without duplicate submission;
6. WebSocket disconnect plus REST reconciliation;
7. stop disappearance/under-protection detection;
8. restart with zero unresolved outbox records;
9. final flat account with no orphan orders.

Testnet proves mechanics, not profit or mainnet fill quality. Mainnet remains
disabled until execution qualification and independent profitability/shadow
promotion both pass; the first canary is separately capped.

## Provenance and safety

The upstream fork is retained for provenance and operating-model ideas, not as
a trusted execution implementation. See [UPSTREAM.md](UPSTREAM.md), the
[audit matrix](docs/hypergrok_audit_matrix.md), and the normative
[harness specification](docs/trading_harness_spec.md).

- Never commit real credentials, account IDs, wallet material, approval
  tokens, or private logs.
- A stop is mandatory but cannot guarantee an exit price during gaps, venue
  failure, liquidation, or insolvency.
- Mainnet cannot be selected by a single environment-variable toggle.
- Unknown submission outcomes are reconciled; they are never blindly retried.

This is experimental research infrastructure, not financial advice. Perpetual
futures can lose more than the expected stop amount and may liquidate an
account.

MIT licensed; see [LICENSE](LICENSE).
