# Trading Harness

> **NO LIVE ACCOUNT IS CONFIGURED OR QUALIFIED.** The repository contains an
> armed, TESTNET-only execution boundary, but no Codex/MCP tool can reach it
> and mainnet is compiled off.  Invoking the isolated worker with a provisioned
> API wallet can submit real Hyperliquid testnet actions.

This fork is becoming a Codex-first, agent-runtime-neutral trading desk for
Hyperliquid. It can track an asset, ingest completed candles, calculate
deterministic TA, record sourced sentiment evidence, classify the registered
setup as buy/sell/nothing/unavailable, and evaluate a strategy after costs.
Capital-bearing actions remain behind a separate local approval, isolated
credential, and live-qualification path.

The immediate objective is infrastructure learning, not a profitability
claim: every analysis, abstention, staged bracket, approval reference,
execution state, fill, fee, venue-reported PnL, latency and later review is
kept as immutable evidence. The first registered ETH strategy was tested
honestly and rejected, but small attended TESTNET experiments can still be
staged under an explicit `profitability_qualified: false` grant.

## Capability status

| Capability | Status |
|---|---|
| Public Hyperliquid brief and completed-candle history | Implemented and live-smoke-tested |
| Local asset registry and always-on research node | Implemented; credential-free |
| Descriptive EMA/RSI/ATR TA | Implemented; research only |
| Registered EMA/Donchian/ATR buy/sell/nothing signal | Implemented |
| Manual X sentiment evidence | Implemented for explicit browser research; attended approval only |
| Unattended sentiment | Requires an official X API or compliant provider |
| Costed historical validation and prospective shadow ledger | Implemented |
| Immutable analysis/trade learning ledger and deterministic reviews | Implemented |
| Codex/OpenCode staging inbox | Implemented; all authority flags false |
| Bounded infrastructure-learning grant | Implemented; TESTNET-only, <=24h, no profitability/mainnet claim |
| Mandatory-stop risk ticket and exact three-leg plan | Implemented |
| Local paper OMS/protection watchdog | Implemented |
| Approval/reservation/outbox/preflight/dispatcher persistence | Implemented; local isolated boundary, not MCP |
| Read-only account/metadata/reconciliation | Implemented with typed, hash-checked coordinators |
| Hyperliquid exact wire, durable nonce, isolated signing and one-shot entry transport | Implemented and armed for TESTNET only |
| Reduce-only close/cancel/same-nonce recovery | Implemented with durable permit, outbox, dispatch and reconciliation |
| Isolated credential provider | macOS Keychain reader implemented; no env/file key loader |
| Always-on serialized executor runtime | Implemented with fenced lease, daily-loss sync, strict recovery priority and graceful drain |
| Direct attended control CLI | Implemented; confirmation is read from `/dev/tty`, never MCP/chat/stdin |
| Local Ubuntu VM egress router | Secret-free renderer/templates implemented; `local_nat_lab` only, not installed or VPN-qualified |
| Live Hyperliquid testnet | **Code armed; no account configured; first responsible write blocked by commissioning and qualification-workflow gaps** |
| Live Hyperliquid mainnet | **Hard-disabled in store, signer and transport** |

The research/MCP executor remains disabled. Environment variables cannot turn
venue writes on. The TESTNET signer is reachable only through the separate
durable execution path with an exact account, policy, permit and claim.

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
        +--> immutable analysis/learning records
        |
        v
non-authoritative TESTNET staging inbox
        |
        v
direct-terminal approval + atomic execution store (not MCP/chat)
        |
        v
daily-loss sync + independent reconciliation + protection watchdog
        |
        v
isolated signer process + one-shot TESTNET transport
        |
        v
local Ubuntu VM router lab (optional, no credentials or authority)
        |
        v
Hyperliquid TESTNET API
        |
        v
immutable fill/fee/slippage/PnL review by exact component version
```

Agents explain and route evidence. Deterministic code owns indicators,
classification, risk arithmetic, hashes, state transitions, signing policy,
and reconciliation. A chat message is never approval.

## Codex/ChatGPT plugin

[`plugins/trading-desk`](plugins/trading-desk) packages six skills and fifteen
bounded MCP tools. Five tools write only local research, analysis, sentiment,
or non-authoritative staging state; none approves, signs, reserves capital, or
writes to an exchange.

Research tools:

- `get_harness_status`
- `get_market_brief`
- `track_asset` — local database write
- `pause_tracked_asset` — local database write
- `list_tracked_assets`
- `record_manual_sentiment` — local database write
- `get_latest_sentiment`
- `analyze_asset` — immutable local analysis/learning write
- `validate_candidate_profitability`
- `stage_trade_candidate` — immutable all-false-authority staging write
- `get_trade_stage`
- `get_learning_review`
- `get_learning_summary`
- `get_node_status`
- `validate_trade_intent` — schema/hash only, not risk or approval

Use [`$assess-asset`](plugins/trading-desk/skills/assess-asset/SKILL.md) for the
end-to-end research workflow. Other packaged skills cover market briefs,
thesis registration, signal interpretation, backtests, and desk coordination.

Manual X research uses the user's visible signed-in browser session only for
an explicit request. X forbids non-API website scripting, so the always-on node
does not automate the website. It stores post IDs/URLs/hashes/timestamps and
bounded polarity—not raw text, cookies, or tokens—and marks the result
unusable for unattended trading. It may support a fresh, attended TESTNET
learning quote, but the exact ticket still requires the separate
direct-terminal approval authority.

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
trading-harness-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

The endpoint is `http://127.0.0.1:8765/mcp`. Public binding is rejected because
the local server has no user-authentication layer. The checked-in Codex plugin
and OpenCode config target this loopback URL; they do not launch ambient
`python3`. A server started without the three learning arguments remains
research-only and returns a configuration blocker for directional staging. A
dedicated local research service enables real staging with:

```bash
trading-harness-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8765 \
  --learning-executor-config /absolute/private/testnet-executor.toml \
  --learning-research-db /absolute/state/research.sqlite3 \
  --learning-grant /absolute/private/active-learning-grant.json
```

This profile loads the signed grant only as a non-authoritative quote scope;
the agent-facing process never receives the symmetric grant key. The separate
attended control plane verifies the MAC before admission.
Agent quotes do not open the executor daily-loss database; that amount is
explicitly deferred, and the isolated worker requires a complete authoritative
loss refresh in the same tick before it can dispatch an entry.

Using the [official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
add the configured URL-backed server with:

```bash
codex mcp add tradingDesk --url http://127.0.0.1:8765/mcp
```

### Isolated TESTNET execution environment

The isolated signing boundary lazily accepts exactly the official
`hyperliquid-python-sdk==0.24.0`:

```bash
python3.11 -m venv .venv-execution
source .venv-execution/bin/activate
python -m pip install -e '.[execution]'
```

The optional local Ubuntu 24.04 router is composed from public values with:

```bash
python3 scripts/render_ubuntu_router.py \
  --spec /absolute/reviewed/router-spec.json \
  --output-dir /absolute/new/router-bundle
python3 scripts/render_ubuntu_router.py \
  --check-bundle /absolute/new/router-bundle \
  --expected-manifest-sha256 REVIEWED_DIGEST_FROM_RENDER_OUTPUT
```

Start from
[`deploy/ubuntu-router/router-spec.json.example`](deploy/ubuntu-router/router-spec.json.example)
and follow [`docs/ubuntu_vm_router.md`](docs/ubuntu_vm_router.md). The rendered
`local_nat_lab` bundle emits no `PrivateKey` field or venue credential. Because
WireGuard public/private strings share an encoding, the operator must verify
that both supplied key strings came from the public-key derivation step. It
routes through the same home/office public IP and does not stop macOS from
bypassing the VM, so it is functional TESTNET infrastructure rather than VPN
qualification. The complete first-write blockers are tracked in
[`docs/testnet_commissioning.md`](docs/testnet_commissioning.md).

Start from
[`deploy/config/testnet-executor.toml.example`](deploy/config/testnet-executor.toml.example),
render every placeholder, including the three distinct numeric
`executor_uid`, `research_uid`, and `control_uid` values required by config
schema v2. Keep the config admin-owned and mode `0400` with narrow read ACLs,
and create each state-directory parent with mode `0700`.
Keep execution, nonce, daily-loss and control-socket state in four distinct
executor-owned parents beneath the executor-private root. This lets the
attended control identity reach execution SQLite sidecars without gaining
directory-write access to nonce, daily-loss or socket state. Keep only
staging/learning in a separately ACL-scoped shared-learning directory.
Own that shared parent as the executor UID with mode `0700`, just like the
execution parent. Do not grant `delete_child` on either cross-UID parent.
Before `init`, inherit only exact file-level read/write/read-attribute rights
to the permitted SQLite roles, so each exclusively reserved main is
non-replaceable immediately. After `init`, add `delete` only to the directory's
inherit-only file ACE for files created in the future; existing mains do not
inherit it retroactively, while future sidecars do. Prove those roles cannot
unlink, rename, or replace a main path before any foreground service starts.

Shared staging/learning main and sidecar files have a live 64 MiB application
cap; 1 GiB is the fail-closed existing-open ceiling for each executor-private
file, not its live filesystem quota. An invalid or oversized shared-learning
store disables learning projection and blocks every new entry,
but it does not prevent the executor from opening core capital state for
startup reconciliation, protection checks, flattening, cancellation, or noop
fencing. Those application caps do not replace an OS storage boundary: keep
all research-writable database, log, and temporary growth on a separately
quota-limited APFS volume (or equivalent) whose exhaustion cannot consume the
executor-private reserve. Apply a separate executor-volume quota, monitoring,
and shutdown threshold below the 1 GiB reopen ceiling. Verification snapshots
are private temporary directories beside their source database, never ambient
system temp, so quota headroom must cover one bounded snapshot copy.

Every main SQLite file is created by `init` and must remain owned by the
configured executor UID. SQLite sidecars are different: the exact
`-wal`, `-shm`, and `-journal` files for execution may be owned by executor or
control; those for staging/learning may be owned by executor, control, or
research; nonce and daily-loss sidecars remain executor-only. This narrow
exception reflects SQLite's first-sidecar-writer behavior and does not permit
research to traverse executor-private state. The attended CLI establishes
umask `0077` before it can create a control-owned sidecar, and the MCP entry
point does the same before research can create a shared-learning sidecar.

Schema-v1 executor configs are rejected rather than silently reinterpreted.
The v2 UID policy is part of the canonical config hash and durable database
binding. Do not point a hand-edited v2 config at v1-bound state or silently
reinitialize a nonempty deployment. Preserve such state for review; only a
proved-empty, never-qualified setup may be deliberately initialized anew.
`init` is an all-empty, one-time transition: it rejects both a complete prior
state set and any partial mixture instead of repairing or recreating it.
Validate and initialize as the configured executor UID without
loading credentials or touching the venue:

```bash
trading-harness-executor validate --config /absolute/private/testnet-executor.toml
trading-harness-executor init --config /absolute/private/testnet-executor.toml
trading-harness-executor status --config /absolute/private/testnet-executor.toml
trading-harness-executor dry-run --config /absolute/private/testnet-executor.toml
```

Except for read-only config validation, the CLI rejects execution commands
outside `executor_uid` and attended commands outside `control_uid`. The
configured learning MCP likewise refuses startup outside `research_uid`.

Grant issuance and trade authorization require direct controlling-terminal
input. There is intentionally no `--confirmation` argument and piping stdin is
not accepted:

```bash
trading-harness-executor issue-grant \
  --config /absolute/private/testnet-executor.toml \
  --output /absolute/private/active-learning-grant.json \
  --grant-id testnet-learning-001 --ttl-seconds 3600

trading-harness-executor show-stage \
  --config /absolute/private/testnet-executor.toml \
  --document-id stg_REVIEWED_ID

trading-harness-executor authorize-stage \
  --config /absolute/private/testnet-executor.toml \
  --grant /absolute/private/active-learning-grant.json \
  --document-id stg_REVIEWED_ID --approver-id local-operator
```

Only after foreground qualification should the isolated worker run:

```bash
trading-harness-executor run \
  --config /absolute/private/testnet-executor.toml \
  --worker-id isolated-testnet-worker
```

The isolated process can load one expected API-wallet key from macOS Keychain;
it rejects environment variables and plaintext key files and verifies the
derived signer address. Use a dedicated non-login OS identity so the
research/MCP/Codex identity cannot read that Keychain item. Installing the
extra alone does not configure an account or invoke a venue write.
The LaunchDaemon profile uses the explicitly configured
`/Library/Keychains/System.keychain` and `/usr/bin/security` item ACL; it never
depends on `HOME` or a login-keychain search list.

The execution path now includes:

- live account/book send-time preflight and exact three-leg entry construction;
- persist-before-send entry and recovery dispatchers with one-shot transport;
- HMAC-authenticated testnet approvals and short-lived recovery permits;
- reduce-only close, role-aware cancel and same-original-nonce fencing;
- canonical noop response persistence and restart-safe reconciliation;
- automatic risk release only after a fresh flat, terminal account proof;
- exact TESTNET fill/funding daily-loss synchronization with gap/retention detection;
- immutable projection of command states plus fully evidenced parent and
  recovery-close fills into the learning ledger.

There is intentionally no execution MCP tool. A chat message is not a trusted
approval, and the model process never receives the wallet object. The macOS
launchd executor template is separate from the credential-free research
service. Linux execution is not advertised because no Linux secret provider
is implemented; the systemd template is for the credential-free research/MCP
process only.

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

This is a target checklist, not the current CLI surface. The GTC canary/cancel,
ordinary attended close, WebSocket monitor and response-drop injection remain
implementation gaps tracked in
[`docs/testnet_commissioning.md`](docs/testnet_commissioning.md); the first
harness order write remains blocked until they are closed.

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
