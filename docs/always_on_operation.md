# Always-on research-node operation

Status: research-only deployment guide. This document does not enable an
exchange writer, signer, testnet order, or mainnet order.

The first always-on process is the credential-free research node. It polls
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
this service. Its code boundary exists, but its supervisor definition is
withheld until live qualification. Provision it under a different non-login
OS identity with its own reviewed binary, state directory, credential
boundary, egress policy and service definition. ChatGPT, Codex, OpenCode, the
MCP process and the research node must not be members of the signer's
credential group.

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
disabled and credential loading disabled. Installation does not need an MCP
runtime; the research-node implementation and application runtime are
standard-library-only.

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

Stop through launchd/systemd or send `SIGTERM`; do not use `kill -9` during
normal operation. The CLI signal handler completes the current bounded cycle,
marks runtime `stopping` then `stopped`, and releases its lease. After the
supervisor reports stopped, run the node-status command and retain the result
with the change record.

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

This deployment proves only that the read-only research node can run and
recover continuously. Paper profitability, Hyperliquid testnet mechanics and
mainnet canary authority are separate gates. Do not add a private key or an
execution command to either research template. The isolated TESTNET worker
uses its own execution/nonce databases and macOS Keychain item; install a
service definition for it only after the live checklist in
`docs/testnet_qualification.md` passes. Mainnet remains a separate future
deployment and is hard-disabled in this build.
