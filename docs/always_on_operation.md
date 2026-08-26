# Always-on research and isolated TESTNET operation

Status: deployable research node plus an isolated, mainnet-impossible TESTNET
worker. No account is configured or live-qualified by the repository, and no
agent tool can approve, sign, or submit an order.

The first always-on process is the credential-free, research-only node. It polls
registered assets, writes immutable research artifacts and heartbeats to one
explicit SQLite database, and exits cleanly on `SIGINT` or `SIGTERM`. The
current CLI deliberately has no execution command and no network/account
environment toggle.

## Trust boundary

Run the research node under a dedicated, non-administrator OS identity. That
identity may read the installed application and write only its research state
and log directories. It must not have a Hyperliquid private key, API wallet,
X bearer token, browser profile, approval key, signer socket, shell startup
file containing credentials, or access to the execution database.

The TESTNET signer/executor is a separate deployment, not another argument to
the research service. Its checked-in supervisor templates must be installed
only after foreground validation. Provision it under a different non-login OS
identity with its own reviewed binary, state directory, Keychain ACL, egress
policy and service definition. ChatGPT, Codex, OpenCode and the research/MCP
process must fail negative-access tests against the API-wallet and recovery
Keychain items.

Testnet and mainnet execution must use separate:

- OS users and service names;
- API wallets and secret-storage policies;
- nonce/command/OMS databases and backup sets;
- account IDs, deployment grants and approval trust roots;
- state, logs, monitoring and incident records.

Do not select mainnet with an environment variable such as `NETWORK=mainnet`
or by editing the research service. The execution service must bind its
network, account, database and signer identity in a separately reviewed,
environment-specific configuration and deployment grant. Success on testnet
does not authorize mainnet.

## Install a reviewed build with Python 3.11

Use a reviewed commit or release in a root/admin-owned installation directory;
do not run a mutable checkout owned by the service account. The examples below
use `/opt/trading-desk/research` as a neutral illustration. Substitute the
locally reviewed absolute path consistently.

```sh
cd /opt/trading-desk/research
python3.11 -m venv .venv
./.venv/bin/python -m pip install --no-deps .
./.venv/bin/trading-harness doctor
```

`doctor` must report Python `>=3.11`, `live_trading: false`, venue writes
disabled and credential loading disabled. The research node itself does not
need MCP. The separate Codex/OpenCode service does; install it in the reviewed
research venv with `./.venv/bin/python -m pip install '.[mcp]'` before enabling
the learning-MCP supervisor. A `--no-deps` install cannot run that service.

Create the state and log directories before starting a supervisor. The
research user owns those directories with mode `0700`; the database and backup
files use `0600`. The installed repository and virtual environment should be
read-only to that identity. Do not use `/tmp`, a home-directory shortcut,
relative paths or a network-mounted SQLite database.

Run once in the foreground before installing a service:

```sh
/opt/trading-desk/research/.venv/bin/trading-harness node run --state-db /var/db/trading-desk/research/research.sqlite3 --node-id trading-desk-research --poll-seconds 1 --history-bars 1200
```

Press `Ctrl-C` to exercise graceful shutdown. Then inspect persisted state:

```sh
/opt/trading-desk/research/.venv/bin/trading-harness node status --state-db /var/db/trading-desk/research/research.sqlite3 --node-id trading-desk-research
```

The status response is the application-level view. Supervisor status, process
existence and logs are separate operational evidence.

## macOS always-on computer (preferred first deployment)

Use a system LaunchDaemon for an unattended Mac; a LaunchAgent depends on an
interactive login. Start from
`deploy/launchd/com.jawndiego.trading-desk-research.plist.example` and render a
new file outside the repository. Replace every placeholder exactly once:

| Placeholder | Required reviewed value |
| --- | --- |
| `__REVIEWED_RESEARCH_USER__` | Dedicated non-admin research account |
| `__REVIEWED_RESEARCH_GROUP__` | Dedicated research group |
| `__REVIEWED_REPO_DIR__` | Absolute, admin-owned installed source directory |
| `__REVIEWED_VENV_BIN__` | Absolute virtual-environment `bin` directory |
| `__REVIEWED_STATE_DIR__` | Absolute local state directory owned by research user |
| `__REVIEWED_LOG_DIR__` | Absolute local log directory owned by research user |

Do not add `EnvironmentVariables`, credentials, a shell wrapper or a network
argument. Confirm no placeholder remains and validate the rendered plist
before placing it in `/Library/LaunchDaemons`:

```sh
rg -n '__REVIEWED_[A-Z_]+__' /absolute/path/to/rendered-research.plist
plutil -lint /absolute/path/to/rendered-research.plist
```

The `rg` command must return no matches. Review ownership and permissions, then
install using the site's administrative change process. The relevant launchd
operations are:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.jawndiego.trading-desk-research.plist
sudo launchctl print system/com.jawndiego.trading-desk-research
sudo launchctl kickstart -k system/com.jawndiego.trading-desk-research
sudo launchctl kill SIGTERM system/com.jawndiego.trading-desk-research
sudo launchctl bootout system /Library/LaunchDaemons/com.jawndiego.trading-desk-research.plist
```

The template starts at boot, restarts only after failure, waits at least ten
seconds between restarts, sends a normal termination signal on stop, fixes the
working directory and writes stdout/stderr to explicit files. A clean stop is
not automatically restarted. Configure rotation and retention for both log
files using the host's standard facility; never log credentials or raw social
session data.

For this macOS layout, use a local state path such as
`/var/db/trading-desk/research/research.sqlite3` and a log directory such as
`/var/log/trading-desk/research`. Do not share either with the isolated executor.

## Linux/systemd alternative

Render `deploy/systemd/trading-desk-research.service.example` using the same
six reviewed placeholders. A typical Linux state path is
`/var/lib/trading-desk/research/research.sqlite3`; use a separate local log
directory such as `/var/log/trading-desk/research`.

The example uses a dedicated user/group, fixed working directory, explicit
database and log paths, `Restart=on-failure`, a ten-second restart delay,
`SIGTERM`, restrictive umask, no capabilities, a read-only host filesystem and
write access only to state/log paths. Confirm the installed Python and SQLite
runtime work with those hardening settings before enabling at boot.

After rendering and review:

```sh
systemd-analyze verify /absolute/path/to/trading-desk-research.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-desk-research.service
sudo systemctl status trading-desk-research.service
sudo systemctl kill --signal=SIGTERM trading-desk-research.service
sudo systemctl stop trading-desk-research.service
```

Application status remains:

```sh
/opt/trading-desk/research/.venv/bin/trading-harness node status --state-db /var/lib/trading-desk/research/research.sqlite3 --node-id trading-desk-research
```

## Isolated TESTNET learning worker

This worker exists to collect trustworthy execution evidence. It does not
claim that the registered strategy is profitable, and every grant/ticket
records `profitability_qualified: false` and `mainnet_authorized: false`.

Install the `execution` extra in a separately reviewed Python 3.11 virtual
environment. Render
`deploy/config/testnet-executor.toml.example` to an absolute owner-only file,
replace every placeholder, and leave the compiled default risk-policy hash
unchanged unless the code and policy change together. The four Keychain items
must be distinct:

- API-wallet secp256k1 private key, readable only by the executor identity;
- approval HMAC key, readable only by the attended control identity;
- recovery HMAC key, readable only by the executor identity;
- learning-grant HMAC key, readable only by the grant issuer and attended
  control identities, never the research/MCP identity.

HMAC items are independent nonzero random 32-byte values stored as 64 hex
characters. Never put any of these values in TOML, an environment variable, a
shell argument, a log, chat, or the repository. Verify Keychain access under
each final service identity—including negative tests—before live use.

For a boot-time macOS LaunchDaemon, every credential stanza must name the
explicit `/Library/Keychains/System.keychain`; do not rely on a login-keychain
search list or `HOME`. Provision from an attended admin terminal with
`/usr/bin/security` itself in the item ACL—the harness invokes that executable,
not Python. `-w` remains last so the secret is prompted, never placed in argv:

```sh
sudo /usr/bin/security add-generic-password -U -a hyperliquid-api-wallet -s com.jawndiego.trading-desk.testnet-signer -T /usr/bin/security /Library/Keychains/System.keychain -w
sudo /usr/bin/security add-generic-password -U -a approval-hmac -s com.jawndiego.trading-desk.testnet-approval -T /usr/bin/security /Library/Keychains/System.keychain -w
sudo /usr/bin/security add-generic-password -U -a recovery-hmac -s com.jawndiego.trading-desk.testnet-recovery -T /usr/bin/security /Library/Keychains/System.keychain -w
sudo /usr/bin/security add-generic-password -U -a grant-hmac -s com.jawndiego.trading-desk.testnet-grant -T /usr/bin/security /Library/Keychains/System.keychain -w
```

The first prompt is a 32-byte API-wallet key encoded as 64 hex characters;
the other three are separately generated nonzero 32-byte HMAC keys in the same
encoding. Treat trusting `/usr/bin/security` as safe only together with strict
OS-user separation. Before installing launchd, positively test each permitted
lookup under its final UID with the explicit keychain path, and negatively test
the research UID. Do not proceed if a LaunchDaemon cannot read the intended
item after reboot without unlocking a login session.

Use three different local directory classes: executor-private state for
execution, nonce, daily-loss and the configured control-socket path;
learning-shared state for only `staging.sqlite3` and `learning.sqlite3`
(including their WAL/SHM sidecars); and research-private state for research
data. Never put these files under one
writable parent: directory write access permits unlink/replacement even when a
database is mode `0600`. The research/MCP identity must have no read or write
access to executor-private state. In particular, it must never open the
authoritative daily-loss database; agent quotes mark daily loss as deferred and
the executor performs the mandatory same-cycle refresh before any entry send.

Keep the reviewed config admin/root-owned, mode `0400`, and grant exact read
ACLs to the executor and attended-control identities. The loader accepts an
admin-owned file but rejects group/world mode bits. Create four
distinct writable parents beneath the executor-private root: `execution/`,
`nonce/`,
`daily-loss/` and `socket/`. Own them as the executor UID with mode `0700`.
Give attended control the inherited SQLite rights it needs only on
`execution/`; it must have no directory capability on the other three. Create
the learning-shared parent
with narrow per-identity ACLs for only research, executor and control. Run
`init` as the executor UID, never as root, so capital-state files have the final
owner. Do not run Codex/OpenCode as the executor or control UID.

A reviewed macOS layout is, for example:

- `/var/db/trading-desk/executor-private/execution/`: execution SQLite and
  sidecars; executor RW, attended control narrowly RW, research no access;
- `/var/db/trading-desk/executor-private/nonce/`,
  `/var/db/trading-desk/executor-private/daily-loss/` and
  `/var/db/trading-desk/executor-private/socket/`: executor only;
- `/var/db/trading-desk/learning-shared/`: staging and learning SQLite files;
  research, executor and control receive only the required ACL entries;
- `/var/db/trading-desk/control-private/grants/`: original signed grants;
  attended control only, mode `0700` parent and generation-specific `0600` files;
- `/var/db/trading-desk/research/`: research SQLite; research only.

Use inheritable ACLs on the exact shared directories so newly created SQLite
WAL/SHM sidecars receive the same narrow rights. Prove this across a fresh
sidecar creation and service restart. Then prove the research UID cannot list,
read, create, unlink or replace anything in `executor-private`, and prove the
control UID cannot do so in the nonce, daily-loss or socket parents.

Validate and initialize without credential or network access:

```sh
/opt/trading-desk/executor/.venv/bin/trading-harness-executor validate --config /etc/trading-desk/testnet-executor.toml
/opt/trading-desk/executor/.venv/bin/trading-harness-executor init --config /etc/trading-desk/testnet-executor.toml
/opt/trading-desk/executor/.venv/bin/trading-harness-executor status --config /etc/trading-desk/testnet-executor.toml
/opt/trading-desk/executor/.venv/bin/trading-harness-executor dry-run --config /etc/trading-desk/testnet-executor.toml
```

`validate`, `init`, `status`, and `dry-run` do not load Keychain items or call
Hyperliquid. `init` refuses missing/insecure parent directories, binds every
database to the exact config, and makes state files owner-only. `status` and
`dry-run` may verify/apply reviewed local SQLite schema migrations when opening
an older deployment; they make no runtime state transition or venue call.

Issue a short-lived infrastructure-learning grant in a direct terminal:

```sh
/opt/trading-desk/executor/.venv/bin/trading-harness-executor issue-grant --config /etc/trading-desk/testnet-executor.toml --output /var/db/trading-desk/control-private/grants/learning-grant-g1.json --grant-id testnet-learning-001 --ttl-seconds 3600
```

The command opens `/dev/tty` and requires the exact displayed confirmation. It
does not accept confirmation through stdin or an argument and never overwrites
an existing artifact. A PTY is not proof of human identity: the control UID
must never run Codex/OpenCode or expose an agent shell, and its Keychain ACLs
must be tested independently.

Run the configured agent-facing MCP service under the research identity:

```sh
/opt/trading-desk/research/.venv/bin/trading-harness-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --learning-executor-config /etc/trading-desk/research-testnet-profile.toml --learning-research-db /var/db/trading-desk/research/research.sqlite3 --learning-grant /var/db/trading-desk/research/learning-grant-g1.json
```

Before startup, use a research-readable root-owned config and a root-owned
mode-`0400` signed-grant copy with narrow read ACLs for the research identity.
The config and grant copies must have exact bytes/hashes matching their
control-plane artifacts; never make the control copy writable by research.
Research uses the signed grant only as a quote scope and does not receive its
symmetric HMAC key. Its fifteen tools can analyze and stage, but still
cannot approve, reserve, load the API wallet, sign, or write to `/exchange`.
It also cannot open the executor daily-loss database: staged quotes explicitly
defer that value, and an entry requires a complete authoritative loss refresh
in the exact executor tick that is allowed to dispatch.

Point Codex at the configured loopback service, not an ambient `python3`
process. The [official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
supports a URL-backed server; for this endpoint:

```sh
codex mcp add tradingDesk --url http://127.0.0.1:8000/mcp
```

The checked-in plugin MCP descriptor uses the same URL. [OpenCode MCP configuration](https://opencode.ai/docs/mcp-servers/)
uses a remote
entry with `"type": "remote"` and
`"url": "http://127.0.0.1:8000/mcp"`. Confirm the service is running and list
the tools from the actual client before relying on it; a config edit does not
make a server callable in an already-running agent session.
After Codex/ChatGPT returns
a staged document ID, review and authorize it from the separate attended
terminal:

```sh
/opt/trading-desk/executor/.venv/bin/trading-harness-executor show-stage --config /etc/trading-desk/testnet-executor.toml --document-id stg_REVIEWED_ID
/opt/trading-desk/executor/.venv/bin/trading-harness-executor authorize-stage --config /etc/trading-desk/testnet-executor.toml --grant /var/db/trading-desk/control-private/grants/learning-grant-g1.json --document-id stg_REVIEWED_ID --approver-id local-operator
```

After the configured MCP passes in the foreground, render the matching
`deploy/launchd/com.jawndiego.trading-desk-learning-mcp.plist.example` or
`deploy/systemd/trading-desk-learning-mcp.service.example`. It binds only to
numeric loopback and is not an authenticated public service.

The grant is loaded once at MCP startup. Renew it by issuing a new,
non-overwriting artifact with an incremented generation, verifying/copying its
exact bytes to a new root-owned mode-`0400` research-readable path, updating
the rendered MCP service to that path, and restarting only the MCP service.
Keep the matching control copy for `authorize-stage`; never overwrite or reuse
an expired generation.

Only then run the worker in the foreground. It synchronizes exact fills and
funding, refuses stale/incomplete daily-loss coverage, performs startup
reconciliation before READY, serializes safety ahead of new entry, always uses
the three-leg mandatory-stop group, and drains bounded safety work on SIGTERM:

```sh
/opt/trading-desk/executor/.venv/bin/trading-harness-executor run --config /etc/trading-desk/testnet-executor.toml --worker-id isolated-testnet-worker
```

After foreground qualification on macOS, render
`deploy/launchd/com.jawndiego.trading-desk-executor.plist.example`. It contains
no shell, environment override, credential value, mainnet switch, or agent
interface. A supervisor restart never bypasses startup reconciliation. Linux
execution is unsupported until a reviewed non-Keychain secret provider exists;
the systemd templates in this repository are for credential-free research/MCP
processes, not the executor.

Install the rendered executor and learning-MCP plists with admin ownership and
mode `0644`, then use their exact system labels:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.jawndiego.trading-desk-testnet-executor.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.jawndiego.trading-desk-learning-mcp.plist
sudo launchctl print system/com.jawndiego.trading-desk-testnet-executor
sudo launchctl print system/com.jawndiego.trading-desk-learning-mcp
sudo launchctl kickstart -k system/com.jawndiego.trading-desk-testnet-executor
sudo launchctl kill SIGTERM system/com.jawndiego.trading-desk-testnet-executor
sudo launchctl kill SIGTERM system/com.jawndiego.trading-desk-learning-mcp
sudo launchctl bootout system /Library/LaunchDaemons/com.jawndiego.trading-desk-testnet-executor.plist
sudo launchctl bootout system /Library/LaunchDaemons/com.jawndiego.trading-desk-learning-mcp.plist
```

If a transient internal failure leaves a sticky halt after the owning service
lease has expired, inspect `status`, then acknowledge only its exact revision
and reason from the attended control terminal:

```sh
/opt/trading-desk/executor/.venv/bin/trading-harness-executor acknowledge-halt --config /etc/trading-desk/testnet-executor.toml --expected-revision REVIEWED_REVISION --expected-reason internal_error
```

The command requires an exact `/dev/tty` phrase, loads no credential, performs
no venue write and leaves the risk gate HALTED. It opens only the execution
database, so the attended control identity needs no nonce, daily-loss or
control-socket access. Restart still has to complete startup reconciliation
before READY.

## Health, restart and graceful-stop checks

An operator check should verify all of the following:

1. Supervisor reports one running process, without a restart loop.
2. `trading-harness doctor` remains fail-closed.
3. `trading-harness node status` reports `capability: research_only`, venue
   writes disabled, credential loading disabled, an active lease and fresh
   heartbeats for registered assets.
4. The database and logs remain owned by the research identity and are not
   group/world writable.
5. Filesystem usage, log growth, request errors and clock offset remain within
   locally declared limits.
6. `trading-harness-executor status` shows one current fenced lease, a fresh
   heartbeat, exact config binding, complete fresh loss coverage, and no
   unresolved reconciliation/protection work before it can report READY.
7. The executor log contains no address, Keychain label, secret, raw venue
   payload, approval token, or browser evidence; status uses fingerprints.
8. The learning review advances with command/fill evidence, or explicitly
   reports missing path/outcome evidence; it never silently declares profit.
9. Mainnet remains absent from config, signer, store, transport and service
   arguments.

Stop through launchd/systemd or send `SIGTERM`; do not use `kill -9` during
normal operation. A signal received before the final entry submission guard
prevents the send; once that guard has consumed the one-shot authority, the
bounded send is the point of no return and is reconciled before shutdown. The
CLI signal handler completes the current bounded cycle,
marks runtime `stopping` then `stopped`, and releases its lease. After the
supervisor reports stopped, run the applicable node/executor status command and
retain the result with the change record.

## Backup and recovery

Define an RPO, RTO, retention period and restore owner before unattended use.
SQLite uses WAL mode, so copying only `research.sqlite3` while the process is
running can create an inconsistent backup. Prefer one of these procedures:

1. Gracefully stop the node, verify it is stopped, then copy the database with
   mode `0600`; or
2. Use the SQLite CLI's online backup operation against the explicit database:

```sh
sqlite3 /var/db/trading-desk/research/research.sqlite3 ".backup '/absolute/backup/research-YYYYMMDDTHHMMSSZ.sqlite3'"
sqlite3 /absolute/backup/research-YYYYMMDDTHHMMSSZ.sqlite3 "PRAGMA integrity_check;"
```

`PRAGMA integrity_check` must return `ok`. Store backups outside the live state
directory with access limited to the research backup operator. Do not put
executor keys, authorization tokens or browser/X credentials in this
backup set.

Back up execution, nonce, daily-loss, staging and learning databases as one
documented consistency set after a graceful executor stop. Never restore only
the outbox without its nonce/reconciliation state, copy a live WAL database as
a lone main file, or reuse a restored API wallet against two active executor
instances. Grant artifacts and config contain no raw secret but remain
owner-only deployment authority and belong in a separately controlled backup.

For a restore drill: stop the service, preserve the failed database and WAL
sidecars for investigation, restore a verified backup to a new file, set the
reviewed owner/mode, point a staging node at it, run `node status`, then start
one cycle under observation. Never start two nodes against copied states with
the same production node identity. Record achieved RPO/RTO and reconcile gaps
from source evidence; do not silently forward-fill missing candles.

## Clock discipline

All evidence, expiry, lease and freshness decisions use UTC instants. Enable
the host's supported network-time service and alert on loss of synchronization
or excessive offset. On macOS inspect network time with:

```sh
sudo systemsetup -getusingnetworktime
sudo systemsetup -getnetworktimeserver
```

On systemd-based Linux inspect and enable synchronization with:

```sh
timedatectl status
sudo timedatectl set-ntp true
```

After sleep, reboot or a large clock correction, confirm synchronization and
fresh node heartbeats before relying on new research output. Clock uncertainty
must halt freshness-sensitive decisions; changing the wall clock is not a
recovery technique.

## Promotion boundary

The research deployment proves continuous evidence collection. The isolated
worker can separately prove TESTNET mechanics and produce learning evidence;
that does not establish strategy profitability or mainnet safety. Do not add a
private key or execution command to a research/agent template. Install the
TESTNET service only after the live checklist in
`docs/testnet_qualification.md` passes. Mainnet remains a separate future
architecture and is hard-disabled in this build.
