# Hyperliquid testnet qualification

Status: **offline implementation complete; live venue qualification not run**.

The TESTNET execution functions are real and armed when the isolated worker is
explicitly constructed. No account, API wallet or worker service is configured
by the repository, and no Codex/MCP tool can invoke them.

This checklist is a release gate, not a setup shortcut. Unit tests, local paper
fills and valid SDK signatures do not prove that an API wallet is registered to
the intended account or that venue recovery works.

## User-provided prerequisites

Provision outside Codex/chat and outside the repository:

- a dedicated Hyperliquid testnet main/subaccount address;
- a newly registered API wallet used only by the isolated testnet signer;
- testnet collateral/faucet eligibility;
- a dedicated non-login OS identity and private credential store;
- separate file-backed execution, nonce, daily-loss, staging and learning
  databases, with execution/nonce/daily-loss/socket in distinct writable
  parents so attended-control SQLite access cannot reach the other
  executor-private artifacts;
- explicit account, asset, notional, loss and 2x leverage caps;
- the standard `default`/`disabled` account mode, not unified or portfolio margin.

Never paste the API-wallet private key into a task, config committed to Git,
environment variable visible to the agent, issue, log or test fixture. The
research/MCP/Codex OS identity must not be able to read the signer or recovery
credentials. Approval and grant HMAC items are also distinct from the signer.

For a boot-time macOS LaunchDaemon, use the explicit System keychain configured
in every credential stanza. `-w` must be the final option so `security` prompts
on the TTY; do not put the key in shell history or argv. Trust the executable
the harness actually invokes (`/usr/bin/security`), not Python:

```sh
sudo /usr/bin/security add-generic-password -U \
  -a hyperliquid-api-wallet \
  -s com.jawndiego.trading-desk.testnet-signer \
  -T /usr/bin/security \
  /Library/Keychains/System.keychain \
  -w
```

Provision the approval, recovery and grant HMAC items the same way under their
distinct service/account labels; each is an independently generated nonzero
32-byte value encoded as 64 hex characters. Positively test explicit-path
lookups under the final executor/control UIDs, negatively test the research
UID, reboot, and repeat before qualification. Do not rely on a login-keychain
search list or `HOME` in a LaunchDaemon.

The harness only reads that item, verifies the derived public signer address,
and zeroes its command-output buffers. It has no credential provisioning,
export, environment-variable, or plaintext-file path.

Install a reviewed commit in a Python 3.11 execution environment with the exact
optional SDK pin:

```sh
python3.11 -m venv .venv-execution
.venv-execution/bin/python -m pip install -e '.[execution]'
```

Installing dependencies does not enable execution.

Render the strict executor config from
`deploy/config/testnet-executor.toml.example`. Schema v2 requires the exact
numeric UIDs of three distinct non-root executor, research, and attended-control
identities. Validate and initialize it with `trading-harness-executor validate`
and `init` under the configured executor UID. Retain the redacted config hash
and confirm `status`/`dry-run` load no credential and make no network call.

Before `init`, retain a root-reviewed directory/ACL report. After `init`, the
required ownership matrix is:

| Database | Main-file owner | Allowed sidecar owners |
| --- | --- | --- |
| Execution | Executor | Executor or attended control |
| Nonce | Executor | Executor only |
| Daily loss | Executor | Executor only |
| Staging | Executor | Executor, attended control, or research |
| Learning | Executor | Executor, attended control, or research |

“Sidecar” means only the exact `-wal`, `-shm`, or `-journal` path for that
configured main database. Every artifact must remain a regular, single-link,
mode-`0600` file with the reviewed ACL. The owner exception never applies to a
main database. The control socket remains executor-only.

Retain the macOS first-writer probe as evidence: executor-first execution WAL
and SHM files are executor-owned; control-first WAL and SHM files are
control-owned while remaining readable and writable by executor through the
exact inherited ACL. Reopen the control-first database under the executor UID.
The attended CLI and configured MCP must establish umask `0077` even when
launched from a shell whose ambient umask is `0022`.
Prove wrong-UID executor/control command dispatch and wrong-UID configured MCP
startup fail before a database, grant, Keychain item, or venue client is opened.

On the execution and learning-shared parents, prove control/research have no
`delete_child`. Before `init`, inherit read/write/read-attribute but no delete;
after `init`, add delete only to future-file inheritance so existing durable
mains remain non-deleteable and new SQLite sidecars are cross-cleanable. Retain
negative unlink, rename and atomic-replacement probes for execution, staging
and learning mains, plus positive cross-owner WAL/SHM cleanup. Do not proceed
if main-path replacement is possible under either non-executor identity.
Permit only the additional direct `list,add_subdirectory` rights required for
stale-snapshot detection and a
mode-`0700` verification snapshot beside each DB. Prove snapshots never use
ambient system temp, clean up normally, remain inside the intended quota, and
receive only the inherit-only directory `delete` ACE needed to remove their own
snapshot. They must leave a crash artifact that causes an attended root-review
stop detected by runtime validation; parent `delete_child` remains forbidden.

Schema v1 is rejected, and the v2 UIDs change the canonical config hash. There
is no silent state migration, config rebinding, or empty-database recreation.
On this new machine, run `init` only after proving no harness state exists. If
any v1-bound or other nonempty state is discovered, preserve its main and
sidecar files and stop for a separately reviewed migration.

## Offline gates

Before connecting the signer process, retain passing evidence for:

1. exact plan/ticket/approval/preflight hash round trips;
2. one-time approval and one nonterminal command for the dedicated account;
3. concurrent nonce uniqueness and restart/clock rollback;
4. official SDK 0.24.0 golden signer recovery;
5. persist-before-send and one-shot unknown-outcome behavior;
6. full, partial and unfilled paper IOC cases;
7. rejected/disappearing/undersized stop detection;
8. reduce-only close, owned-CLOID cancel and same-nonce noop construction;
9. crash-before-send, crash-after-attempt and tamper tests;
10. research strategy and deployment authority remaining independent.
11. single-use recovery signing/submission authority, exact noop-default
    response persistence, expired-unsent permit terminalization, and parent
    risk release only after terminal-flat reconciliation.
12. complete fills/funding coverage from UTC midnight to a fresh exact query
    watermark, with retention, pagination, schema and clock gaps failing closed;
13. staged-ticket, approval, command, parent/recovery fill, fee, latency and
    venue-reported PnL projection into the immutable learning ledger,
    including incomplete-read replay;
14. exact venue-server fill-window watermarks, canonical cross-lane fill
    attribution, parent-stop/recovery-close interleaving, and late recovery
    requests remaining blocked until signed expiry plus settlement grace;
15. the research/MCP UID failing read/write access to execution, nonce and
    daily-loss state, while every entry requires a complete same-tick loss
    refresh even across an IDLE-preview/admission race.
16. the exact v2 UID binding and main/sidecar owner matrix, including a
    control-first execution WAL/SHM followed by a successful executor reopen;
17. ambient-`0022` attended CLI launch still producing mode-`0600` SQLite
    sidecars, with extra ACL principals, wrong owners, hard links, and
    symlinks all failing closed;
18. config v1 and nonempty v1-bound state being rejected without mutation,
    credential loading, database recreation, or venue I/O.
19. `init` rejecting both a complete rerun and every partial existing/missing
    state mixture without changing any inode or byte;
20. existing-only opens rejecting zero-byte, schema-less, wrong-role, drifted
    or integrity-invalid databases without creating, migrating or rebinding them.
21. oversized and integrity-invalid shared learning failing fast, blocking all
    entries, and still allowing core startup reconciliation and each documented
    account-safety recovery lane from independently verified private state.
22. exhausting the research UID's dedicated storage quota without consuming
    executor-private reserve or preventing a nonce/daily-loss/execution WAL
    commit; and independently approaching the executor-volume shutdown
    threshold must halt before the 1 GiB reopen ceiling while retaining tested
    emergency WAL/recovery headroom, both before and after reboot.

## Live testnet sequence

Run with minimum notional and a hard operator stop condition. Persist every
request identity, response hash, account snapshot and reconciliation result.

1. Issue a short-lived `profitability_qualified: false` infrastructure grant,
   run one Codex/ChatGPT analysis, stage its exact hash, and prove every staging
   authority flag is false. Review and authorize it only through the
   direct-terminal CLI; preserve the learning cycle and command IDs.
2. Query `userAbstraction`, metadata, account state and frontend orders using
   the main account address. Verify signer address differs and the account is
   flat with no foreign orders.
3. Place a far non-marketable GTC test order with an owned 128-bit CLOID, query
   it by CLOID/OID, cancel by CLOID and prove terminal cancellation.
4. Submit a minimum-size long IOC + reduce-only SL + TP as `normalTpsl`.
   Accept only a full entry plus an independently visible stop covering the
   exact signed position.
5. Reduce-only close to exactly flat; verify children/orphans are terminal.
6. Repeat the full lifecycle short.
7. Create a tightly bounded partial IOC. Prove the children are not relied on,
   a critical incident is durable, and the priority reduce-only flatten leaves
   the account flat.
8. Drop a real HTTP response after forwarding. Recover by CLOID/account state;
   do not send a replacement entry. Exercise the same-original-nonce noop only
   through its durable incident-bound recovery command. Treat only the exact
   documented `{"status":"ok","response":{"type":"default"}}` body as an
   accepted fence; every other body remains unknown.
9. Disconnect WebSocket monitoring across a fill, then recover through REST
   without duplicate events or fills.
10. Simulate stop rejection/disappearance and prove account-wide new risk is
   halted before recovery.
11. Restart every worker at its documented crash points. Finish with zero
    position, zero open/orphan orders, zero unresolved attempts, zero reserved
    risk, verified event chains and reconciled fills.
12. Review the final learning cycle and exact-version aggregate. Confirm that
    missing market-path/funding/close evidence is flagged rather than inferred,
    and that no report upgrades the experiment into a profitability claim.

Do not use Hyperliquid `scheduleCancel` while a position depends on a venue
stop: it cancels all open orders, including protection.

## Qualification artifact

The signed review artifact must identify the reviewed commit, SDK/package lock,
testnet account and API-wallet public addresses, database identities, asset
metadata hashes, each test command/CLOID/nonce, UTC times, raw-response hashes,
final account snapshot, incidents and reviewer identities. It contains no
private key or reusable approval token.

Any unresolved or contradictory state fails qualification. Re-running after a
code, dependency, account-mode, signer, policy or venue-contract change creates
a new artifact.

## Mainnet boundary

Testnet qualification proves mechanics only. It does not establish strategy
profitability or mainnet execution quality. Mainnet remains hard-disabled until:

- testnet passes this complete sequence;
- a strategy independently passes historical and prospective shadow gates;
- a separate mainnet OS identity, API wallet, database and asymmetric/hardware
  approval authority are reviewed;
- a capped account and 0.10% equity-risk canary policy are approved.
