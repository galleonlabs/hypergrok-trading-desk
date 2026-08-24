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
- separate file-backed execution and nonce databases;
- explicit account, asset, notional, loss and 2x leverage caps;
- the standard `default`/`disabled` account mode, not unified or portfolio margin.

Never paste the API-wallet private key into a task, config committed to Git,
environment variable visible to the agent, issue, log or test fixture. The
research/MCP/Codex OS identity must not be able to read the signer credential.

On macOS, create the generic-password item while logged in as the dedicated
executor identity. `-w` must be the final argument so `security` prompts on the
TTY; do not put the key in shell history or argv. Replace the Python path with
the reviewed execution virtual environment:

```sh
/usr/bin/security add-generic-password -U \
  -a hyperliquid-api-wallet \
  -s com.jawndiego.trading-desk.testnet-signer \
  -T /absolute/path/to/.venv-execution/bin/python3.11 \
  -w
```

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

## Live testnet sequence

Run with minimum notional and a hard operator stop condition. Persist every
request identity, response hash, account snapshot and reconciliation result.

1. Query `userAbstraction`, metadata, account state and frontend orders using
   the main account address. Verify signer address differs and the account is
   flat with no foreign orders.
2. Place a far non-marketable GTC test order with an owned 128-bit CLOID, query
   it by CLOID/OID, cancel by CLOID and prove terminal cancellation.
3. Submit a minimum-size long IOC + reduce-only SL + TP as `normalTpsl`.
   Accept only a full entry plus an independently visible stop covering the
   exact signed position.
4. Reduce-only close to exactly flat; verify children/orphans are terminal.
5. Repeat the full lifecycle short.
6. Create a tightly bounded partial IOC. Prove the children are not relied on,
   a critical incident is durable, and the priority reduce-only flatten leaves
   the account flat.
7. Drop a real HTTP response after forwarding. Recover by CLOID/account state;
   do not send a replacement entry. Exercise the same-original-nonce noop only
   through its durable incident-bound recovery command. Treat only the exact
   documented `{"status":"ok","response":{"type":"default"}}` body as an
   accepted fence; every other body remains unknown.
8. Disconnect WebSocket monitoring across a fill, then recover through REST
   without duplicate events or fills.
9. Simulate stop rejection/disappearance and prove account-wide new risk is
   halted before recovery.
10. Restart every worker at its documented crash points. Finish with zero
    position, zero open/orphan orders, zero unresolved attempts, zero reserved
    risk, verified event chains and reconciled fills.

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
