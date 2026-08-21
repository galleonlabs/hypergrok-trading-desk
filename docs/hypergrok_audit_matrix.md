# HyperGrok Audit Disposition Matrix

Status: static source audit complete for current `main`; behavioral qualification pending
Audit fork: [`jawndiego/hypergrok-trading-desk`](https://github.com/jawndiego/hypergrok-trading-desk)
Commit: `62cbe227a2ec531e0efa37254d4b6fae043fbfe5`
Tree: `3b2fbd379c9956cde8be4038690ac1e94188e2b2`
Archive SHA-256: `5bc53e9b62845cb4f3871dec099ec9debe804739f3569e077c8106182b91bccc`
Provenance: [`UPSTREAM.md`](../UPSTREAM.md)
Target architecture: [`trading_harness_spec.md`](trading_harness_spec.md)

## 1. Scope and Method

This matrix covers every artifact category in the 49-file current-`main` snapshot. The audit reviewed source text, embedded snippets, setup commands, manifests, CI, security claims, role boundaries, Hyperliquid assumptions, lifecycle procedures, and failure handling.

The upstream structural check passes:

```text
ok: 16 skills, 7 agents, 32 markdown files checked
```

That result verifies document shape only. It does not validate exchange behavior, strategy performance, custody, authorization, risk, idempotency, recovery, or production safety.

The disconnected upstream `v1.0.0` Python lineage is captured and hashed in `UPSTREAM.md` but is a separate product. It is not covered by the current-main dispositions below and must receive its own code audit.

## 2. Disposition and Severity

| Disposition | Meaning |
| --- | --- |
| `retain` | May be preserved unchanged, subject to provenance and license obligations |
| `rewrite` | Useful intent exists, but the artifact must be redesigned or encoded as deterministic controls |
| `reference_only` | Keep as human documentation, historical evidence, or adversarial fixture; never place it on the capital path |
| `reject` | Do not reuse the artifact's authority model or executable procedure |

| Severity | Meaning if adopted as a production control |
| --- | --- |
| Critical | Can plausibly cause unauthorized/duplicate execution, materially wrong risk, stranded exposure, or key compromise |
| High | Can materially corrupt state, evidence, monitoring, or decision quality |
| Medium | Operational, maintenance, provenance, or research-reliability weakness |
| Low | Informational or license-preservation issue |

## 3. Executive Result

No current-main artifact qualifies for unchanged reuse in the capital-bearing path.

- Retain the MIT license unchanged.
- Preserve desk vocabulary, evidence discipline, testnet-first intent, no-blind-retry intent, incident structure, and process/outcome review as requirements.
- Rewrite read-only research, market-data, thesis-validation, monitoring, lifecycle, and review behavior against typed services and immutable stores.
- Reject prompt-conferred financial authority, shared-host key handling, chat approval, agent-authored risk PASS, agent execution, mutable Markdown state, and copy-paste write snippets.
- Use Hyperliquid examples as contract-test and fault-test fixtures, not executable skills.

## 4. Critical Cross-Cutting Findings

| ID | Finding | Consequence | Required control |
| --- | --- | --- | --- |
| HG-P0-001 | Named roles, `writes_to_exchange` metadata, and `alwaysApply` prompts are treated as authority | Any same-host agent/process can potentially reach the key and venue | Agents have no credentials; mTLS/RBAC control plane; isolated signer and egress allowlist |
| HG-P0-002 | Approval is a replayable chat phrase, not an authorization object | Approval can be edited, replayed, or applied to changed economics | Trusted approval UI; semantic-intent hash; signed, expiring, audience-bound, single-use command authorization |
| HG-P0-003 | Risk is sized to the stop trigger, not stressed executable loss | Example states $51 while its own bound permits $124.95 before costs, with larger gap risk possible | Exact-decimal risk engine; worst-fill/gap/liquidity stress; hard platform ceilings; segregated capped account |
| HG-P0-004 | CLOIDs/nonces and `send once` exist only as prose/file conventions | Concurrent or late requests can duplicate exposure | Serializable per-account admission; atomic nonce/CLOID allocation; durable outbox; action-specific unknown-outcome contract |
| HG-P0-005 | Partial or per-leg fills can leave exposure without protection | Live position may wait for another agent/human turn | Standing account-safety policy; continuous protected-size reconciliation; serialized reduce-only protection/flatten |
| HG-P0-006 | Monitoring is optional and shares the execution host | Silent disconnect or host failure removes visibility and response | Independent reconciler/protection watchdog; WebSocket gap recovery; REST resnapshot; paging/freshness SLOs |
| HG-P0-007 | Proposal, limits, approval, execution, and journal state are mutable Markdown | Races, undetected edits, and unverifiable history | Transactional OMS plus tamper-evident append-only event ledger and compensating corrections |
| HG-P0-008 | Setup installs mutable dependencies and exposes a raw agent key | Supply-chain drift or prompt/process compromise reaches signing authority | Locked/hashes dependencies, SBOM, isolated signer principal, managed key boundary, fixed account/network/actions |
| HG-P0-009 | Current `main` and tagged `v1.0.0` both claim version 1.0.0 but are unrelated roots | Release identity and reproducibility are ambiguous | New fork namespace/version line; signed releases; explicit dual-lineage provenance |
| HG-P0-010 | Strategy hygiene is exploratory, not an admission gate | Backtest or agent narrative can be mislabeled as opportunity | Immutable thesis registry, full trial ledger, burned holdout, multiplicity correction, prospective shadow, independent grant |

## 5. Repository, Setup, Supply Chain, and Documentation

| Artifact | Disposition | Severity | Finding | Replacement / verification |
| --- | --- | --- | --- | --- |
| `README.md` | `rewrite` | Critical | Mutable-main paste/install path and “built for real money” claims overstate an instruction-only system; links/identity point upstream | Fork identity, experimental status, pinned release digest, explicit trust limits, no mainnet-ready claim before qualification |
| `SETUP.md` | `reject` | Critical | Clones mutable upstream; fallback pipes an unverified archive into extraction; “read-only” setup mutates/install software | Exact fork commit/release, signature and digest verification before extraction, isolated install, research/testnet default |
| `CHANGELOG.md` | `rewrite` | Critical | Claims 1.0.0 without a matching current-main tag/release and collides with disconnected Python v1 | Preserve as upstream snapshot history; start a new fork version namespace and reviewed release record |
| `SECURITY.md` | `rewrite` | Critical | Prompt boundaries and “1.x” support are not enforceable; secret-store claim conflicts with plaintext-file fallback | Fork security contact/SLA, exact supported versions, signer threat model, custody policy, revocation and incident drills |
| `CONTRIBUTING.md` | `rewrite` | High | Structural lint is treated as the bar; no mandatory write-path reviewer, contract/fault test, or release gate | CODEOWNERS/quorum, safety-negative tests, API fixtures, provenance/SBOM updates, release checklist |
| `LICENSE` | `retain` | Low | Standard MIT; upstream notice must remain | Preserve unchanged and add fork NOTICE/attribution for derived work |
| `docs/ARCHITECTURE.md`, `docs/FAQ.md` | `reference_only` | High | Prompt conventions are described as trust boundaries and enforced approval/write separation | Map each useful workflow concept to a typed component, state transition, credential boundary, and test |
| `docs/PROVENANCE.md` | `rewrite` | Critical | Live-verification claim lacks commands/results; sources lack exact commits/digests; asset provenance is absent | Exact source hashes/dates, reproducible evidence, dependency/license inventory, asset provenance, patch ledger |
| `skills/README.md` | `reference_only` | High | Useful catalog, but assigning a skill to a role does not constrain capability | Generate a catalog from registered service capabilities and policy, not prompts |
| Root and platform manifests (`plugin.json`, `.claude-plugin/*`, `.cursor-plugin/*`, `.grok-plugin/*`) | `rewrite` | High | All claim Galleon/upstream identity and version 1.0.0; no schema/cross-manifest CI | Fork namespace/repository/maintainer/version; one version source; platform schema and asset checks |
| `.github/workflows/ci.yml` | `rewrite` | High | Pinned checkout/read-only token are good; CI only runs structural lint on mutable `ubuntu-latest` | Fixed runtimes; schema, shell, secret, license, dependency, unit, contract, property, fault, and provenance tests |
| `scripts/check.sh` | `rewrite` | High | Passes, but its home-grown parser ignores nested semantics; “one writer” means one metadata string | Keep structural checks; use real schemas and negative fixtures; prove unauthorized principals cannot reach writes |
| `.github/dependabot.yml` | `rewrite` | Medium | Tracks Actions only because Python/npm signer dependencies live in prose | Move dependencies to exact lockfiles and add reviewed update coverage |
| `.github/ISSUE_TEMPLATE/*` | `rewrite` | Medium | Useful secret warnings; security/reporting links point upstream | Fork advisories, fork version/commit fields, safe reproduction/redaction requirements |
| `.gitignore` | `rewrite` | Medium | Does not cover common secret/runtime artifacts | Add defense-in-depth patterns and mandatory secret scanning; ignore rules are not the primary control |
| `assets/mascot.jpg`, `assets/mascot-320.jpg` | `reference_only` | Medium | Branding/asset provenance and reuse rights are not documented beyond repository license | Verify original source and licensing before fork branding or distribution |
| Release pipeline/artifacts | `reject` | Critical | No GitHub release, signed current tag, checksum, SBOM, provenance attestation, or reproducible package | Protected reviewed release branch; signed tag; immutable archive; checksums; SBOM/SLSA provenance; clean-install test |

## 6. Agent and Team Rules

| Artifact | Disposition | Severity | Finding | Replacement / verification |
| --- | --- | --- | --- | --- |
| `agents/desk-lead.md` | `reference_only` | High | Useful routing model, but mutable chat/files cannot enforce stage order or authority | Read-only coordinator over typed proposal/query APIs; identity denied admission/signer/venue-write access |
| `agents/execution-trader.md` | `reject` | Critical | Gives an LLM direct write authority and an environment-readable key; file-persisted CLOID and retry logic are unsafe | Deterministic isolated executor, outbox, unique command/CLOID, serialization, no model/browser/shell |
| `agents/market-analyst.md` | `rewrite` | High | Source/time discipline is useful, but evidence is model-driven and mutable; freshness is vague | Normalized read service, dual timestamps, sequence/freshness status, immutable source hashes, stale denial |
| `agents/research-analyst.md` | `rewrite` | High | Browses adversarial content in the shared workspace; prompt says not to follow injected text | Quarantined ingestion, captured source/hash, typed claims/confidence, no control-plane/signer access |
| `agents/strategist.md` | `rewrite` | High | Agent writes arbitrary backtest code and hands selected results toward trading without a trial registry | `thesis-register`/`thesis-evaluate`, hashes, complete attempts, corrected inference, independent attestation |
| `agents/risk-manager.md` | `reject` | Critical | Probabilistic agent performs monetary arithmetic and issues PASS; example materially understates risk | Exact-decimal deterministic risk and atomic reservation; agent may explain signed result only |
| `agents/trade-reviewer.md` | `rewrite` | High | Mutable Markdown and exchange history cannot prove authorization or continuous protection | Immutable ledger, deterministic metrics/invariants, independent reconciliation; narrative only |
| `rules/hypergrok-team.mdc` | `reject` | Critical | `alwaysApply` prompt creates appearance of access control and authorizes a named model role | No prompt confers financial capability; enforce trusted UI, RBAC/mTLS, signer policy, and network isolation |

## 7. Desk Skills

| Artifact | Disposition | Severity | Finding | Replacement / verification |
| --- | --- | --- | --- | --- |
| `skills/desk-operating-model/SKILL.md` | `reference_only` | Critical | Explicitly relies on shared-host discipline, editable files, shared secret environment, and chat approval | Retain vocabulary/evidence labels/exclusions as requirements; replace all trust and state boundaries |
| `skills/desk-trade-lifecycle/SKILL.md` | `rewrite` | Critical | Markdown is the single record; tickets can change under one ID; approval is an unbound phrase | Typed immutable versions, OMS FSM, trusted token, atomic admission/reservation/authorization/outbox |
| `skills/desk-risk-limits/SKILL.md` | `reject` | Critical | User can choose all limits; trigger-distance sizing omits execution failure, costs, and nonterminal exposure | Non-overridable system ceilings plus stricter user policy; stressed risk; exact arithmetic; property tests |
| `skills/desk-execution-protocol/SKILL.md` | `reject` | Critical | Direct agent/key path; mutable integrity; file CLOID; two negative checks may precede a late duplicate | Deterministic adapter/signer; original-incapable-of-arrival rule; expiry/nonces; fault-tested recovery |
| `skills/desk-incident-response/SKILL.md` | `rewrite` | Critical | Recognizes failure modes but containment waits for chat approval while exposure can remain live | Standing account-safety policy, deadlines, watchdog, serialized risk-reducing actions, drills |
| `skills/desk-monitoring/SKILL.md` | `rewrite` | Critical | Monitoring optional; 4–8-hour position checks; shared-host scripts only alert | Always-on independent reconciler/protection worker, freshness SLOs, paging, redundancy, safety actions |
| `skills/desk-strategy-lab/SKILL.md` | `rewrite` | High | Useful hygiene but arbitrary split/cost/trade-count rules and no inherited-search correction | Harness thesis protocol, full funnel, burned holdout, PBO/DSR/FDR or SPA, prospective evidence |
| `skills/desk-post-trade-review/SKILL.md` | `rewrite` | High | “Append-only” Markdown is mutable and reviewer changes proposal state | Hash-chain/WORM event ledger, compensating records, raw venue payloads, deterministic review |

## 8. Hyperliquid Skills

| Artifact | Disposition | Severity | Finding | Replacement / contract tests |
| --- | --- | --- | --- | --- |
| `skills/hyperliquid-api-reference/SKILL.md` | `reference_only` | High | Static “exhaustive” reference is stale; max market order claim is $30M while current official spec says $15M | Generated/versioned schemas; nightly drift alert; signing/precision/asset-ID fixtures; conservative platform limits |
| `skills/hyperliquid-setup/SKILL.md` | `reject` | Critical | Shared environment/plaintext-file key, mutable dependency ranges, env-selected mainnet, non-failing readiness | Separate signer principal/KMS, fixed account/network/actions, locked hashes/SBOM, hard startup reconciliation |
| `skills/hyperliquid-orders/SKILL.md` | `reject` | Critical | No outbox/nonce coordinator; TS bracket omits CLOIDs; per-leg/IOC partials can be unprotected; float arithmetic | ACID reserve/consume/nonce/CLOID/outbox; Decimal properties; timeout, duplicate, partial-leg, gap, restart tests |
| `skills/hyperliquid-positions/SKILL.md` | `rewrite` | Critical | “Any close” cleanup conflicts with partial close and may remove residual protection; no concurrency control | Position FSM/per-market serialization; residual protection, reduce-only, stop-replacement, tier/mode tests |
| `skills/hyperliquid-account/SKILL.md` | `rewrite` | High | Unified/portfolio margin treated as exception; unsafe free-margin proxy; history cannot prove continuous protection | Typed reconciled state; supported abstraction math or fail closed; consistent snapshots, pagination/dedup tests |
| `skills/hyperliquid-advanced/SKILL.md` | `reject` | Critical | DMS cancels stops; TWAP lacks continuous protection/lost-response path; mutable global expiry; nonce claims unsafe | State-aware kill switch; TWAP FSM/protection; per-request expiry; exact-nonce and HIP-3 contract tests |
| `skills/hyperliquid-market-data/SKILL.md` | `reference_only` | Medium | Research snippets lack execution-grade source/freshness/schema enforcement and immutable manifests | Separate research/execution feeds; network/time/max-age checks; gap/open-bar/depth/delist tests; dataset hashes |
| `skills/hyperliquid-websocket/SKILL.md` | `rewrite` | High | `nohup` is not supervision; Python path does not reconnect; no gap repair/resnapshot/dedup/durable queue | Supervised ingestion, REST resnapshot, independent watchdog, stale halt, drop/reorder/crash/disk/schema tests |

## 9. Required Hyperliquid Contract and Fault Tests

Before any adapter is admitted to testnet strategy activity:

- Golden signing fixtures for every permitted action and network.
- Exact decimal serialization, conservative side-aware rounding, tick/lot precision, and asset-ID mapping.
- Atomic nonce and command/CLOID uniqueness under same-millisecond and concurrent submissions.
- Timeout/crash before send, during send, after venue acceptance, and after local response loss.
- Duplicate delivery, delayed acceptance, action expiry, and unknown-result reconciliation.
- Batch/per-leg partial acceptance, resting partial fill, IOC partial fill, trigger no-fill, and gap beyond bound.
- Protection size alignment after entry, add, reduce, partial close, cancel, replace, trigger, and TWAP slice.
- Cross, isolated, strict-isolated, unified, portfolio-margin, margin-tier, and account-abstraction fixtures; unsupported modes fail closed.
- WebSocket half-open/drop/reconnect, duplicate/reordered events, sequence gap, REST resnapshot, crash/restart, and disk-full recovery.
- DMS with open positions proving protective-order behavior.
- Rate-limit, schema-drift, null/empty book, metadata remap, delisting, and network-confusion tests.
- Signer denial for transfers, withdrawals, vault/subaccount fund movement, builder fees, unsupported actions, wrong account, and wrong network.

## 10. Current-Main Audit Exit Status

| Gate | Status |
| --- | --- |
| Exact fork commit/tree/archive recorded | Pass |
| Current-main artifact inventory | Pass |
| Structural upstream linter | Pass, non-security |
| Static role/desk/API review | Pass |
| Every artifact assigned a disposition | Pass by artifact or explicit group |
| Hyperliquid factual claims contract-tested | Pending |
| Replacement signer/risk/OMS implemented | Pending |
| Behavioral and fault tests | Pending |
| Zero unresolved critical/high findings in replacement | Pending |
| Mainnet eligibility | Denied |

## 11. Next Actions

1. Add the disconnected `v1.0.0` as an immutable audit ref without merging it into a harness branch.
2. Protect audit refs and verify inherited Actions/hooks/apps/secrets are disabled or empty.
3. Start a new fork version namespace and replace upstream identity in manifests/docs.
4. Produce the separate `v1.0.0` Python code audit.
5. Continue implementing the clean read model, thesis registry, semantic-intent schema, risk reservation, OMS/outbox, signer boundary, and reconciler on working harness branches.
6. Convert retained concepts into tests and typed requirements; do not copy capital-path prompts or snippets.
