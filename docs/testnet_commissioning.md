# TESTNET commissioning and first-write gap register

Status: **offline engine and guest-configuration renderer implemented; VM provisioning, machine, network and live qualification incomplete; first harness order write remains blocked**.

This document records the remaining work from a reviewed source commit to the
first responsible harness-originated Hyperliquid TESTNET order. It is not
authorization to provision a venue credential, initialize state, start a
worker or call `/exchange`.
The normative live sequence remains `docs/testnet_qualification.md`.

## What is already true

- Mainnet is hard-disabled in configuration, state, signer and transport.
- The executor can sign and submit only its reviewed TESTNET action families.
- Admission, authorization consumption, reservation, outbox and attempt
  persistence are durable before network I/O.
- Unknown outcomes remain reserved and are reconciled; no write is blindly
  resent.
- Python 3.11/3.12/3.13 and pinned MCP/SDK suites have passed on the reviewed
  baseline.
- Three disabled local identities exist in the current machine plan: research
  UID 450, executor UID 451 and control UID 452.
- A private-key-field-free local Ubuntu router profile and deterministic
  renderer exist; operator public-key provenance is still required.

These facts do not make the machine transaction-ready.

## Venue-credential-free machine work

Complete and retain evidence for every item before provisioning a venue or
Keychain secret. Router-only WireGuard keys are generated during item 6 on
their owning machines because their derived public keys are renderer inputs.

1. **macOS security update.** Completed on the current host on 2026-08-26:
   macOS was updated from 15.3.1 to 26.6.2 build 25G83 and rebooted. The pinned
   Python 3.11.16/OpenSSL 3.5.8 runtime and all 849 tests were requalified on
   Python 3.11, 3.12 and 3.13. Retain that evidence and repeat this gate after
   any later OS/runtime change.
2. **Root inventory.** Seal the exact-commit deployment pack and retain owner,
   mode, ACL, mount, LaunchDaemon and empty-state evidence from an attended
   root console.
3. **Storage quota implementation.** The current pack's quota tool is
   audit-only. Implement the APFS research/executor volume creation,
   encryption decision, resumable failure handling, persistent mounts,
   executor emergency reserve, log bounds and post-reboot exhaustion probes.
   Implement the missing free-space/shutdown monitor; no current runtime
   consumer enforces the staged threshold values.
4. **Final-path pre-init ACL implementation.** The existing pre-init ACL script
   targets the old `/var/db/trading-desk` skeleton while the staged config
   targets `/var/db/trading-desk-volumes`. Add and review exact pre-init ACL
   handling for the final quota-volume paths. Nonce, daily-loss and socket
   parents remain executor-only; no cross-UID parent receives `delete_child`.
5. **Admin installation.** Install the root-owned Python runtime and exact
   reviewed application/wheel bundle. Prove UID 501 and all service identities
   cannot modify or replace source, runtime or venv paths.
6. **Ubuntu router lab.** Provision the two-NIC VM manually, generate the VM
   and Mac WireGuard private keys only on their owning machines, derive and
   attest the public keys, then render and qualify `local_nat_lab` using
   `docs/ubuntu_vm_router.md`. It does not change the public IP and does not
   prevent host bypass. Do not call it VPN-qualified. A separately reviewed
   macOS PF/Network Extension or non-bypassable physical router remains a later
   egress-isolation gate; if absent during an attended functional TESTNET
   canary, the qualification artifact must say network isolation is unqualified.
7. **Proxy/trust environment.** Prove the executor rejects ambient proxy and CA
   override variables and that its urllib openers install an empty proxy
   handler. Retain the root-owned CA path and TLS hostname-verification
   evidence.
8. **Public config plan.** Review asset/instrument mapping, recovery CLOID
   allowlist, exact risk caps, UIDs and final state paths. Final account/API
   wallet addresses and config hash are bound after attended API-wallet
   registration. Use MCP port 8765 consistently in service and client configs.
9. **Quota and recovery headroom.** Fill only the isolated research probe to
   ENOSPC and prove executor execution/nonce/daily-loss WAL commits and one
   verification snapshot still succeed. Prove the executor shutdown threshold
   leaves recovery headroom.

No `init`, venue/Keychain secret, grant issuance, launchd installation or
harness venue write belongs in this phase.

## Missing reviewed application capabilities

Machine commissioning alone cannot execute the published live checklist. The
following are code gaps, not operator commands waiting to be discovered:

| Required qualification behavior | Current gap |
| --- | --- |
| Far non-marketable GTC canary, exact query and cancel | Signer accepts only a three-leg `normalTpsl` group with IOC entry; no qualification GTC CLI exists |
| Retained pre-write account/metadata/order snapshot | Runtime reconciliation is internal; no attended evidence-export command exists |
| Ordinary attended reduce-only close, including an unexpected GTC-canary fill | Reduce-only close is incident-driven account-safety recovery only |
| WebSocket disconnect/fill/recovery exercise | No WebSocket client or monitor is implemented; current runtime is REST polling |
| Forward request but drop the real response | No bounded qualification fault injection exists |
| Router health as an admission capability | No application router-health field or pre-admission guard exists |
| Executor free-space shutdown threshold | No runtime monitor consumes the staged storage thresholds |
| Signed qualification artifact | No artifact builder/signing workflow exists; the deliverable is still manual |

Implement these as narrow TESTNET-only, attended and durable workflows with
observable failure tests. They may not become MCP tools, accept chat approval,
widen signer actions generically, expose mainnet, or weaken the one-shot
unknown-outcome contract. Until then, the first harness order write remains
blocked.

## Credential provisioning

After the venue-credential-free gates pass:

1. Create or select a dedicated standard-mode Hyperliquid TESTNET account and
   minimum test collateral.
2. Generate and register a dedicated API wallet outside chat and the
   repository. Registration is itself an attended out-of-band `approveAgent`
   `/exchange` write; it is account provisioning, not the first harness order
   or qualification evidence.
3. Query fresh `userRole`/agent evidence for the API-wallet address and retain
   proof that it maps to the intended main account/subaccount and has not been
   pruned or replaced.
4. Generate independent signer, approval, recovery and grant secrets.
5. Provision the four explicit System Keychain items from an attended terminal.
6. Positively test only the intended executor/control lookups and negatively
   test research UID access. Reboot and repeat.
7. Render the final public executor config with the lowercase main/API wallet
   addresses, account ID and all previously reviewed values; retain its hash.

Do not put a secret in TOML, an environment variable, argv, a VM profile,
cloud-init, logs, a shared folder or an agent-readable path.

## One-time init and post-init ACL work

1. Preserve a fresh empty-directory, inode, owner, mode and pre-init ACL report.
2. Run credential-free `validate` against the final schema-v2 config.
3. Run `init` exactly once as executor UID 451.
4. Implement and run a separately reviewed post-init ACL tool. It may add
   `delete` only to future-file inheritance; it must not modify the existing
   execution, staging or learning mains.
5. Prove durable mains remain executor-owned, mode 0600, single-link and
   non-replaceable by control/research. Prove cross-owner sidecar cleanup,
   snapshot cleanup and the exact owner matrix.
6. Run credential-free `status` and `dry-run`. Wrong-UID commands must fail
   before state, Keychain or network access.

There is currently no reviewed post-init ACL script in the deployment pack.

## Foreground no-write qualification

1. Start the Ubuntu router and activate the Mac full-tunnel peer.
2. Prove read-only `/info`, DNS and TLS work through the VM; record the known
   local-lab host-bypass limitation.
3. Ensure no executable command is queued.
4. Start the executor in the foreground, complete startup REST reconciliation,
   observe READY/no-work cycles, send SIGTERM and retain clean drain evidence.
5. Issue a short-lived infrastructure-only grant, preserve the control copy,
   install its exact root-owned research-readable copy, then start the learning
   MCP on `127.0.0.1:8765`. Stage one document and confirm every authority flag
   remains false.
6. Do not authorize a bracket merely to prove the worker can send. First close
   the qualification capability gaps above.

## First harness order write and full qualification

Before the GTC canary, retain clock synchronization and maximum-offset evidence
and implement/qualify the attended reduce-only close: a far order can still
fill during a market jump. The narrow canary needs minimum exposure, an owned
CLOID, an exact readback, a bounded cancel timeout and an unexpected-fill hard
halt/flatten path. Only after terminal cancellation or terminal-flat recovery
should a grant-backed bracket be staged and authorized, followed by the
three-leg IOC plus mandatory stop/target lifecycle.

Full qualification additionally requires WebSocket disconnect recovery,
response-loss fault injection, tunnel-loss unknown-outcome reconciliation,
long and short bracket lifecycles, partial-fill recovery, stop disappearance,
restart points and a final flat account with no unresolved attempt or reserved
risk.

Launchd remains last. Do not install it until foreground live qualification,
safe log rotation, quota/reboot tests, router boot ordering, clock checks and a
backup/restore drill pass.
