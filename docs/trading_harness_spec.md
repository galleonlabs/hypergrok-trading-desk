# Agent-Assisted Trading Harness Specification

Status: Draft v0.2
Decision date: 2026-08-21
Upstream source: [`galleonlabs/hypergrok-trading-desk`](https://github.com/galleonlabs/hypergrok-trading-desk)
Working fork: [`jawndiego/hypergrok-trading-desk`](https://github.com/jawndiego/hypergrok-trading-desk)
Verified fork `main`: `62cbe227a2ec531e0efa37254d4b6fae043fbfe5`
Provenance record: [`UPSTREAM.md`](../UPSTREAM.md)
Current-main disposition matrix: [`hypergrok_audit_matrix.md`](hypergrok_audit_matrix.md)

## 1. Decision

Fork HyperGrok for provenance, operating procedures, role prompts, and Hyperliquid reference material. Do not treat it as the production execution foundation.

The capital-bearing path will be implemented as a deterministic harness with typed interfaces, hard risk invariants, an isolated signer, durable state, independent reconciliation, and fault-tested recovery. Agents may research, validate theses, explain evidence, and draft proposals. Agents may not hold exchange credentials or directly mutate exchange state.

Use two trust zones inside the fork:

1. Immutable upstream refs and hashed archives retained only for audit and provenance, with no production credentials.
2. Working harness branches that receive reviewed requirements, tests, and implementation through provenance-recorded commits.

This distinction is fundamental:

- HyperGrok is useful as an operating-model specification.
- Current HyperGrok `main` is unsafe for mainnet execution as-is.
- Prompt-driven multi-agent chat is the wrong critical-path architecture for autonomous or systematic execution.
- A separately built deterministic harness can eventually support guarded mainnet and systematic operation.

## 2. Product Goal

Build a trading desk that can:

1. Turn an idea into a falsifiable, versioned thesis.
2. Validate the thesis without data leakage or unreported trial selection.
3. Detect only registered signals using deterministic calculations.
4. Revalidate portfolio and venue risk immediately before submission.
5. Bind approval to a canonical semantic order intent and deterministically account for every runtime wire field.
6. Submit once, recover safely from unknown outcomes, and reconcile from venue truth.
7. Maintain a tamper-evident record from thesis through post-trade review.
8. Promote capabilities gradually from research to tightly bounded systematic operation.

The system is a research and execution harness. It does not create alpha merely by adding agents, indicators, or roles.

### 2.1 Agent-runtime abstraction

The deterministic core must not import, invoke, or depend on ChatGPT, Codex, Grok, Claude, or another model runtime.

- ChatGPT and Codex are the first supported interfaces through the installable [`trading-desk` plugin](../plugins/trading-desk), repository [`AGENTS.md`](../AGENTS.md), and five focused packaged skills. ChatGPT and Codex share the same plugin and typed MCP contract.
- OpenCode is a compatible second interface over a byte-identical mirror under `.agents/skills` and the same local MCP server. Its checked-in [`opencode.json`](../opencode.json) must default to `ask`, deny external-directory and sensitive-file access, omit a model/provider, and allow only the three reviewed read-only MCP tools by exact name.
- Repository skills contain workflow guidance only; they call typed core interfaces and cannot confer credentials, evidence status, deployment grants, or exchange authority.
- The current MCP surface is limited to fail-closed harness status, public Hyperliquid market briefs, and semantic-intent schema/hash validation. It cannot read credentials, authorize or admit an intent, reserve exposure, sign, or write to a venue.
- Future private data, authentication, authorization, and controlled actions belong in narrow server-side tools rather than skill prose. Any write tool requires a separate qualification milestone and must enforce its own authorization at the side-effect boundary.
- The same domain, validation, risk, admission, OMS, ledger, and adapter APIs must work without any agent attached.
- Removing or replacing the agent interface must not change deterministic results or capital-path behavior.

This follows the official Codex model: [`AGENTS.md`](https://learn.chatgpt.com/docs/agent-configuration/agents-md) for durable repository guidance, [repo skills](https://learn.chatgpt.com/docs/build-skills) for focused repeatable workflows, and a [plugin/MCP server](https://developers.openai.com/plugins/concepts/plugins) only when installable connected tools are needed. OpenCode documents the same [`AGENTS.md`](https://opencode.ai/docs/rules/) and [`.agents/skills`](https://opencode.ai/docs/skills/) conventions.

## 3. Why HyperGrok Is Unsafe for Mainnet As-Is

### 3.1 Controls are instructions rather than enforcement

Current upstream is primarily Markdown prompts, skills, examples, and a structural linter. The one-writer rule, Risk PASS, ticket expiry, single-send discipline, and append-only records are conventions. There is no production service that makes those states non-bypassable.

Consequences:

- A model can misunderstand, omit, or rewrite a required step.
- A proposal, approval, or Risk PASS can be edited after the fact.
- An approval is not bound to an immutable order payload.
- There is no atomic consume-once operation for an approval.
- Two processes can race despite the conversational claim that there is one writer.

### 3.2 Agent roles are not security boundaries

The upstream design places agents on a shared computer and gives the execution environment access to the API-wallet secret. Naming one agent `Execution Trader` does not prevent another process, injected instruction, generated script, or dependency from accessing the same capability.

The harness therefore requires:

- No private key in any agent process, chat, shared environment, or agent-readable file.
- An isolated signer/executor with a narrow, typed, allowlisted API.
- Egress restricted to approved venue endpoints.
- Action-level policy enforcement inside the signer boundary.

### 3.3 Risk is not reliably bounded by the documented stop

Upstream examples size risk from entry to stop trigger while allowing a materially worse executable stop bound. A stop can also gap beyond its limit and remain unfilled. The harness must model stressed executable loss rather than treating trigger distance as a hard bound. No model can cap losses during venue failure, liquidation, auto-deleveraging, oracle failure, or insolvency; the final hard containment is a segregated account holding only capped risk capital with transfer authority disabled.

The deterministic risk engine must include:

- Worst permitted entry and exit prices.
- Fees, funding, spread, slippage, and a gap/liquidity stress.
- Existing positions, open orders, concentration, and correlated exposure.
- Current margin mode, margin tiers, free collateral, and liquidation distance.
- Failure of a protective order to place, resize, or remain live.
- Versioned instrument metadata, explicit units, exact decimal arithmetic, and conservative side-aware rounding.

### 3.4 Brackets and partial fills are not automatically safe

A grouped entry and protective orders can return different per-leg states. A partially filled parent can leave real exposure before protection exists. A production harness cannot wait for a conversational loop to discover or repair that state.

Required behavior:

- Treat each leg response and subsequent venue state explicitly.
- Fail closed for new entries when protection requirements cannot be met.
- After any exposure exists, fail safe under a standing account-safety policy that permits only cancel-entry, place or resize reduce-only protection, and reduce-only flatten actions.
- Route emergency actions through the same serialized executor, with explicit deadlines, precedence, retry/reconciliation rules, and paging; a second writer is forbidden.
- Continuously compare protected size with live position size.
- Never infer protection from the submitted request; verify it from venue state.

### 3.5 Unknown outcomes require durable idempotency

Client order IDs are useful only when generated, persisted, and uniquely constrained before submission. A timeout is an unknown result, not a failed order.

Required behavior:

- Durable transactional outbox written before network submission.
- Immutable command ID and endpoint-specific unknown-outcome recovery contract for every venue mutation, including order, cancel, modify, leverage, protection-resize, and emergency actions.
- Unique client-order-ID constraint per venue/account.
- Serializable per-account admission that atomically reserves worst-case portfolio exposure across pending, working, partially filled, unknown-outcome, contingent, and emergency orders.
- Authorization consumption, risk reservation, and durable command/outbox creation in one transaction before network I/O.
- A consumed authorization is never reusable. A command proven not to have reached the network may be voided and re-approved; its token is not revived.
- Risk reservations remain until venue reconciliation proves the associated exposure or command terminal and releasable.
- Explicit nonce sequencing where the venue requires it.
- Submission expiry on every supported action.
- Restart recovery that reconciles every nonterminal outbox row before accepting new risk.
- No replacement send until the first action is confirmed terminal or cryptographically/temporally incapable of arriving.
- For each mutation type, document whether identical bytes may be retransmitted safely or whether reconciliation is mandatory before any follow-up.

### 3.6 Monitoring cannot be optional

Production operation requires an always-on reconciliation and protection worker, not an agent routine that occasionally checks a log.

Required behavior:

- WebSocket monitoring with reconnect and snapshot recovery.
- Periodic REST reconciliation independent of WebSocket health.
- Heartbeats, freshness gates, paging, and acknowledged incidents.
- Trading disabled when account, market, clock, or order state is stale.
- Dead-man behavior that cannot silently remove protective exits while leaving positions open.

## 4. Why Prompt-Driven Autonomy Is the Wrong Architecture

Autonomous or systematic trading is not merely mainnet trading without the final approval message. It has different correctness requirements.

Prompt-driven agents are probabilistic and conversational. They cannot provide hard guarantees for:

- Atomic state transitions.
- Exactly-once authorization consumption.
- Concurrency and nonce control.
- Deterministic replay.
- Bounded decision and submission latency.
- Crash recovery and reconciliation.
- Reproducible signal calculations.
- Non-bypassable portfolio limits.

Better prompts do not solve those properties. For systematic operation, agents must remain outside the critical path. A deterministic strategy module may act under a pre-approved, versioned policy, but an agent may not change the live rule, parameters, universe, limits, or execution behavior.

## 5. Target Architecture

```text
LLM research agents (no credentials)
        |
        v
typed, versioned thesis proposal
        |
        v
thesis validator + immutable trial registry
        |
        v
promoted deterministic signal definition
        |
        v
live read-only signal scanner
        |
        v
deterministic signal-to-intent compiler
        |
        v
canonical semantic order intent + preliminary risk quote
        |
        +----> per-ticket human approval, or
        +----> pre-approved systematic policy
        |
        v
atomic send-time admission
  fresh risk check + portfolio reservation
  authorization consumption + durable outbox
        |
        v
isolated signer/executor
        |
        v
venue
        |
        v
independent reconciler + immutable ledger + protection watchdog
```

### 5.1 Agent plane

Agents may:

- Gather market and research evidence.
- Propose a thesis using the required schema.
- Run approved offline validation tools.
- Explain scanner output and validation status.
- Draft explanatory proposal text around a compiler-produced, non-executable candidate intent.
- Reconstruct reviews from immutable events.

Agents may not:

- Read or derive a signing key.
- Call a venue write endpoint.
- Mark their own thesis as validated.
- Alter a promoted rule without creating a new version and restarting validation.
- Convert an exploratory observation into an actionable signal.
- Choose or alter live side, size, entry, exit, or protection fields outside a frozen strategy and deterministic compiler.
- Override a hard risk or freshness failure.

### 5.2 Deterministic control plane

The control plane owns:

- Thesis and rule schemas.
- Validation states and trial accounting.
- Signal calculation.
- Deterministic compilation from a signal-instance hash and frozen strategy version to a semantic order intent.
- Portfolio state and pre-trade risk.
- Atomic worst-case portfolio-risk reservation.
- Canonical ticket construction and hashing.
- Authorization validation and consumption.
- OMS state transitions and durable outbox.
- Execution, recovery, reconciliation, and incident state.
- Exact decimal/integer monetary arithmetic; binary floating point is forbidden for quantities, prices, fees, and risk limits.
- Canonical time, exchange calendars, clock-health checks, and stale-state cutoffs.

Any discretionary change to the compiler's side, size, entry, exit, or protection output is a new unvalidated thesis version. It cannot inherit the source signal's validation attestation.

### 5.3 Signer boundary

The signer accepts only canonical payloads that carry a valid single-use authorization and a current risk attestation. It independently enforces:

- Network, venue, account, and instrument allowlists.
- Jurisdiction, account-eligibility, venue-status, and instrument-compliance gates.
- Permitted action and order types.
- Maximum quantity, notional, leverage, slippage, and aggregate exposure.
- Trading hours and data freshness.
- Required protective-order policy.
- Payload expiry and client-order-ID uniqueness.
- Explicit denial of transfers, withdrawals, vault operations, builder fees, and other excluded actions.

The approved artifact is a schema-versioned semantic intent, not an unknowable future signature blob. Its domain-separated hash covers every economic field and the allowed runtime-field policy. Venue nonce, signing timestamp, signature, and other non-economic transport fields may be added only by deterministic translation inside the signer. The signer records the semantic-intent hash, final wire hash, and every runtime-only field.

### 5.4 Security and approval threat model

Treat agents, webpages, social content, imported repositories, generated code, market data, and external messages as untrusted. Treat the deterministic control plane, trusted approval UI, isolated signer, immutable ledger, and independently authenticated reconciler as trusted only within their documented boundaries.

Required controls:

- Human approvers authenticate through a trusted UI with strong identity and MFA; approval in agent chat is invalid.
- Approval tokens are signed, audience-bound, expiring, single-use, and protected against replay and cross-environment use.
- Services use mutually authenticated identities and least-privilege authorization.
- Signer keys are generated, stored, rotated, and revoked in a managed KMS/HSM or equivalent isolated secret boundary.
- Signer hosts have no browser, model runtime, general shell workflow, or unrestricted egress.
- Logs and evidence artifacts redact secrets and sensitive authorization material.
- Break-glass access is time-limited, independently approved, fully logged, and cannot silently widen strategy limits.
- Builds pin dependencies and produce provenance/SBOM artifacts; unreviewed binaries and mutable downloads are forbidden.

### 5.5 OMS ownership and reconciliation

The harness is the sole normal writer for a capital-bearing account. Foreign UI/API orders, unexplained fills, or position changes halt new risk and open an incident.

Model orthogonal dimensions rather than one overloaded status:

- Order lifecycle: `created`, `admission_reserved`, `queued`, `submitted_unknown`, `acknowledged`, `open`, `cancel_pending`, `canceled`, `filled`, `triggered`, `replaced`, `rejected`, or `expired`.
- Cumulative quantities: requested, acknowledged, filled, remaining, canceled, and protected.
- Reconciliation confidence: unreconciled, provisional, venue-confirmed, or contradictory.
- Protection state: not-required, pending, protected, under-protected, or failed.
- Incident state: none, declared, contained, or closed.

Legal transitions and cross-dimension invariants must cover partially-filled-then-canceled orders and every replacement/trigger path. Venue events are deduplicated and ordered by explicit precedence rules; corrections append compensating records rather than rewriting history. Contradictory venue reports fail closed for new risk and remain unresolved until reconciled.

## 6. Thesis Validation Is a Mandatory Gate

Every strategy, indicator, event rule, agent-generated idea, or imported framework begins as an unvalidated thesis. This includes the SMA configurations documented by `unfairmarket/SMA-outfits`.

The harness must never equate a plausible narrative, chart match, social-media case study, backtest winner, or agent consensus with evidence of tradable edge.

### 6.1 Evidence status and deployment grants

Scientific evidence and permission to trade are independent dimensions.

```text
evidence_status:
  draft
    -> registered
    -> exploratory_tested
    -> holdout_passed
    -> shadow_confirmed
    -> validated

side or terminal evidence states:
  rejected | inconclusive | suspended | retired
```

Any material rule, parameter, data, execution, or cost-model change creates a new thesis version at `draft`. A previously inspected or failed holdout becomes discovery data forever; a new version cannot reuse it as untouched evidence.

Deployment permission is represented by a separate signed grant scoped to:

- Thesis and strategy version plus code hash.
- Venue, account, environment, and authorization model.
- Allowed instruments, sessions, actions, and policy limits.
- Start, expiry, review date, and revocation state.

Grant types are `infrastructure_testnet`, `strategy_testnet`, `manual_mainnet_canary`, `systematic_testnet`, `systematic_shadow`, and `systematic_mainnet_capped`. Validation alone grants no exchange authority. Only an active environment-specific grant may reach send-time admission.

### 6.2 Required thesis specification

Before outcomes are inspected, register and hash:

- Thesis ID, version, author, and registration time.
- Economic or behavioral rationale, separated from causal claims.
- Instruments, point-in-time universe, venues, and tradable proxies.
- Data sources, adjustment policy, exchange calendar, session, and bar construction.
- Exact feature and signal calculations.
- Direction, entry timing, expiry, exit, stop, and invalidation rules.
- Position-sizing rule and portfolio interaction.
- Spread, fees, slippage, borrow, funding, latency, and impact model.
- Primary outcome, benchmark, minimum economically useful effect, and sample-size plan.
- Every parameter family and variation that will be tried.
- Holdout boundary, forward-shadow duration, and promotion thresholds.

Undefined terms such as `touch`, `precision`, `heightened volatility`, `drawdown`, or `confirmation` are not executable rules.

### 6.3 Data integrity

Validation must use immutable snapshots with source and content hashes.

- Use point-in-time universes and include delistings and symbol changes.
- Handle splits, dividends, rolls, and corporate actions explicitly.
- Use completed bars only; earliest simulated action is after signal observability.
- Do not delete genuine adverse moves because they conflict with news or expectations.
- Do not forward-fill prices through feed gaps for signal generation.
- Quarantine missing, stale, disputed, or out-of-order data.
- Separate regular and extended sessions unless the rule explicitly combines them.
- Cluster overlapping and correlated events when estimating uncertainty.

### 6.4 Experimental protocol

Every validation run must:

1. Record all attempted hypotheses, including failures and manual variants.
2. Freeze the primary metric, multiple-testing family, statistical method, holdout boundary, and stopping rule before any holdout access.
3. Separate discovery, model selection, untouched holdout, and prospective shadow periods; once inspected, a holdout is permanently burned.
4. Use next-observable execution with conservative costs.
5. Compare against buy-and-hold, simple momentum/trend, matched random rules, and nearby parameter values.
6. Apply multiple-testing correction across the complete attempted and inherited selection funnel. If that funnel cannot be reconstructed, treat all historical evidence as discovery and require fresh prospective confirmation.
7. Report effect size and uncertainty, not win rate or headline return alone.
8. Test stability across time, instruments, regimes, data vendors, and reasonable execution delays.
9. Retain the complete result set rather than only the winners.

Acceptable statistical controls include White's Reality Check, Hansen's SPA test, false-discovery-rate control, Deflated Sharpe Ratio, and Probability of Backtest Overfitting, chosen and documented before any holdout access.

### 6.5 Promotion gate

A thesis may receive `evidence_status=validated` only when:

- The untouched holdout meets the predeclared economic and statistical threshold after costs.
- The result survives correction for every trial actually performed.
- Nearby parameters do not reveal a single unstable numerical spike.
- Performance is not dependent on one instrument, day, or regime.
- Conservative cost and slower-fill stress tests remain acceptable.
- A forward-only, append-only shadow run passes its predeclared duration or event count.
- Data quality, code version, configuration, and results are independently reproducible.

After validation, continuously monitor signal frequency, feature distribution, realized costs, calibration, net expectancy, concentration, and execution quality against predeclared drift and decay limits. A breach automatically sets evidence to `suspended`, revokes strategy deployment grants for new risk, and preserves only account-safety authority until independent review.

Failure or insufficient power results in `rejected` or `inconclusive`; it must not be reframed by an agent as a live opportunity.

### 6.6 SMA thesis intake

The SMA repository is treated as a hypothesis catalog, not as validation evidence.

Initial candidates are limited to its partly specified systems:

- SPX 30-minute SMA 10/50 state change.
- Nasdaq 20-minute SMA 20/100 state change.
- Nasdaq 30-minute SMA 20/100 state change.
- Dow 15-minute SMA 90/300 state change.
- Dow 60-minute SMA 90/300 state change.

These labels are intake leads, not registered rules. All five remain `draft` until the calculation instrument and tradable proxy, timezone and session, bar alignment, input price field, crossover/equality semantics, warm-up, direction, actionable time, exit, stop, and cost assumptions are exact. Each ambiguous timeframe is a separate hypothesis. Long-period `touch` claims and other outfits remain `draft` until direction, tolerance, actionable time, horizon, exit, and cost assumptions are defined.

The repository selected these systems from a much larger advertised search. Its linked social-media examples and all historical results belong to discovery evidence unless the complete original trial funnel can be reconstructed and corrected; they may not be used as an untouched holdout. Even a profitable SMA rule would validate only that narrow predictive rule; it would not validate claims of institutional coordination or market control.

### 6.7 Skill boundary

The ChatGPT/Codex plugin begins with five focused skills, mirrored exactly for OpenCode:

- `operate-trading-desk`: coordinates stages and reports unavailable capabilities without manufacturing a fallback.
- `brief-market`: calls the typed public Hyperliquid market-data tool and preserves network, source, receipt, and freshness evidence.
- `validate-thesis`: structures validation plans and reviews deterministic evidence artifacts. The foundation does not yet persist thesis evidence or run backtests; future registry/evaluation writes must use typed core interfaces and have no venue access.
- `scan-signals`: interprets read-only registered-rule scans. Until the deterministic scanner and normalized data adapter exist, it must return `unavailable`.
- `test-strategy`: designs or reviews leakage-resistant historical evaluation; until a deterministic runner exists, it must not claim a run occurred.

Scanner statuses are `unavailable`, `observation`, `research_candidate`, or `validated_research_signal`. A skill never returns an order or position size. A custom parameter override creates a new draft thesis and forces `exploratory=true` and `no_trade=true`. If registration and evaluation later become materially different workflows, split `validate-thesis` without changing the core API.

## 7. Authorization Models

### 7.1 Per-ticket human approval

Human approval occurs only in the trusted approval UI, never in agent chat. It signs a schema-versioned, domain-separated semantic-intent hash covering:

- Venue, network, and account.
- Instrument, side, quantity, and reduce-only state.
- Order type, time in force, entry price or bound, and slippage limit.
- Stop, take-profit, grouping, and protection requirements.
- Strategy/thesis version and validation attestation.
- Displayed preliminary risk snapshot, hard risk-policy version, and maximum permitted drift.
- Client order IDs, intent expiry, and allowed runtime-field policy.

Any economic-field change invalidates the approval. Runtime-only venue nonce, timestamp, and signature fields do not alter the semantic intent and are produced only by the isolated deterministic signer. The authorization token is signed, audience-bound, identity-attributed, expiring, single-use, and anti-replay protected.

### 7.2 Account-safety policy

Every capital-bearing account has a standing safety policy independent of strategy authorization. It may cancel pending risk-increasing orders at any time. After exposure exists, it permits only bounded risk-reducing actions: cancel an unfilled entry remainder, place or resize reduce-only protection, and reduce-only flatten. It cannot open, add, reverse, transfer, or widen exposure.

The safety policy defines deadlines, maximum slippage, action precedence, retry/reconciliation rules, alert escalation, counters, renewal, and expiry. It prohibits new exposure when insufficient policy lifetime remains. Expiry or revocation of a strategy or deployment grant halts new risk but never disables safety authority. The safety policy remains valid, or automatically narrows to flatten-only authority, until every position and working order is flat.

Each safety action receives a unique single-use command authorization derived from the immutable multi-use policy envelope. The protection watchdog invokes it through the same serialized executor and risk-reservation system.

### 7.3 Systematic strategy policy

Systematic execution is allowed only for a deterministic, frozen strategy version operating inside a signed policy envelope. The policy must define:

- Strategy and code hashes.
- Allowed markets and sessions.
- Signal freshness and data-quality requirements.
- Position, notional, leverage, turnover, and concentration limits.
- Daily loss, drawdown, and consecutive-error circuit breakers.
- Permitted order and emergency action types.
- Start, expiry, review interval, and revocation method.

Each qualifying signal produces a single-use derived intent authorization referencing the immutable multi-use policy envelope; the policy itself is not consumed per trade. Agents cannot modify the policy or live parameters. Policy renewal is an explicit human governance action.

### 7.4 Atomic send-time admission

Human approval or a systematic policy establishes permission bounds; it does not prove that a trade is safe at send time. Immediately before network I/O, admission must:

1. Load fresh market, account, order, position, protection, clock, metadata, and venue-status snapshots within predeclared maximum ages.
2. Recalculate risk against the unchanged semantic intent. Revalidation may allow or deny; it may not silently alter the approved economics.
3. In one serializable per-account transaction, reserve worst-case portfolio exposure, transition the single-use command authorization to `consuming`, update all applicable policy counters such as turnover, loss, exposure, and action count, and write the durable command/outbox row.
4. Translate the semantic intent deterministically, add allowed runtime-only fields, record the final wire hash, and submit through the isolated signer.
5. Reconcile the command and exposure. A fill atomically converts reserved order exposure into booked position exposure; only unused, rejected, expired, or canceled quantity is released. Position exposure remains charged until reduced or closed. Crash recovery resumes from the durable row; it never reuses the authorization.

### 7.5 Governance and revocation

No agent or thesis author may validate its own evidence or grant itself deployment authority.

- An independent research reviewer may set `evidence_status=validated` after reproducibility checks.
- A deployment authority may issue or revoke environment-specific grants only for the exact validated versions.
- Independent risk and security approvers sign account-safety and systematic strategy policies.
- Mainnet and systematic grants require a predeclared approval quorum with at least two distinct human approvers.
- Signer trust roots, approver identities, and policy-signing keys require separated administration and audited rotation.
- Revocation propagates to admission and signer services within a measured SLA. It halts new risk and cancels pending risk-increasing commands while preserving account-safety authority for protection and flattening.
- In-flight commands retain their risk reservations and are reconciled under the policy version active when admitted.

### 7.6 Kill-switch modes

Emergency control is ordered and state-aware:

1. `HALT_NEW`: deny new risk admission.
2. `CANCEL_INCREASING`: cancel pending risk-increasing orders.
3. `ENSURE_PROTECTION`: preserve, place, or resize reduce-only protection.
4. `FLATTEN`: invoke bounded reduce-only flattening when explicitly authorized by the account-safety policy.
5. `REVOKE_KEYS`: revoke trading keys only when flat, or under a documented break-glass decision that accounts for stranded exposure.

Generic cancel-all is forbidden while positions remain if it would remove protective exits.

## 8. Capability Grants and Promotion

| Grant | Capability | Exchange writes | Promotion requirement |
| --- | --- | --- | --- |
| Research | Thesis registration, validation, and live observation | None | Data, schema, and research-isolation checks |
| Infrastructure testnet | Synthetic/fixture adapter and fault qualification | Testnet only | Non-economic test policy; no alpha claim required |
| Strategy testnet | Strategy-specific paper/testnet tickets | Testnet only | Validated thesis plus manual or systematic-testnet grant |
| Manual mainnet canary | Human-approved bounded intents | Capped mainnet account | `evidence_status=validated`; exact strategy/compiler version qualified on testnet; incidents rehearsed; dedicated capped account |
| Systematic shadow | Frozen live strategy evaluation | None | Prospective evidence and production-feed qualification |
| Systematic testnet | Frozen strategy under expiring policy | Testnet only | Deterministic replay and systematic fault qualification |
| Systematic mainnet capped | Frozen strategy under expiring policy | Capped mainnet account | Independent approval, production SLOs, revoke/flatten drills |

Grants are capability-, environment-, account-, and thesis-version-specific, reversible, expiring, and recorded. Mainnet is never inferred from successful testnet operation. Testnet validates mechanics; it does not establish mainnet liquidity or execution quality. Infrastructure qualification may use synthetic strategies, but strategy-specific exchange activity requires the corresponding thesis and deployment grant.

The current foundation implements only local `infrastructure_testnet` simulation admission and ships no venue adapter. Every strategy, shadow, mainnet, and systematic grant type is modeled for schema planning but must be rejected by persistence/admission until its evidence, governance, attestation, and execution milestones are implemented.

## 9. Fork and Audit Plan

### 9.1 Preserve provenance

- Preserve every upstream ref without modification under immutable audit refs in this fork.
- Build the harness on working branches; never merge upstream audit refs wholesale or auto-merge upstream changes.
- Disable inherited Actions, hooks, apps, bots, deploy keys, environments, packages, and secrets before the first checkout or run.
- Record repository URL, commit, retrieval time, and archive hash in `UPSTREAM.md`.
- Maintain a source-to-derived ledger for every imported requirement, fixture, test, or code fragment.
- Do not execute instructions fetched from a mutable branch.
- Preserve license and attribution requirements.
- Audit current `main` and the unrelated `v1.0.0` history as separate products; do not assume either validates the other.

### 9.2 Audit every upstream artifact

Inventory every ref, tag, commit history, workflow, hook, submodule, LFS object, release artifact, package, dependency, binary, generated file, agent, skill, rule, example, setup command, and factual claim. For each relevant artifact, record:

- Classification: `retain`, `rewrite`, `reference_only`, or `reject`.
- Exact source commit and line.
- Intended role and authority.
- Required tools, credentials, filesystem access, and network access.
- Prompt-injection and untrusted-input exposure.
- Whether the claim is verified against current official documentation.
- Whether code examples compile and pass recorded/testnet contract tests.
- Failure modes, recovery behavior, and missing invariants.
- License and third-party provenance.

Audit domains include:

- Hyperliquid asset IDs, precision, order types, grouping, margin, account abstraction, and API-wallet permissions.
- Timeouts, action expiry, nonces, client IDs, retries, and rate limits.
- Partial fills, per-leg rejections, triggers, cancels, modifies, and dead-man behavior.
- Key storage, dependency installation, mutable downloads, and generated scripts.
- Ticket lifecycle, risk arithmetic, incident response, monitoring, and review.

Audit completion requires:

- A complete artifact and ref inventory with source hashes.
- Every API assertion classified as verified, contradicted, or explicitly unverified.
- Reproducible pinned builds and independent reruns of retained tests and examples.
- Zero unresolved critical or high findings.
- Every accepted medium finding to have an owner, mitigation, review date, and written risk acceptance.
- No mainnet write and no production credential present anywhere in the audit environment.

### 9.3 Retain versus replace

Retain or adapt:

- Clear specialist responsibilities as an interface pattern.
- Testnet-first progression.
- Exact-ticket approval intent.
- Exchange-record reconciliation.
- No blind retry intent.
- Incident and post-trade review procedures.
- Read-only market and account references after verification.

Replace with deterministic implementation:

- One-writer enforcement.
- Risk calculation and limit enforcement.
- Ticket integrity, approval, and expiry.
- Order construction, signing, and submission.
- Client-ID/nonce allocation and idempotency.
- Persistent lifecycle state and audit history.
- Monitoring, protection, recovery, and kill switches.
- Strategy and thesis promotion.

Reject:

- Direct key exposure to an agent workspace.
- Prompt-only financial permissions.
- Unversioned or mutable remote setup instructions.
- Unvalidated strategies presented as opportunities.
- Subsecond or latency-sensitive execution through an LLM conversation.
- Causal market-control claims inferred only from price geometry.

## 10. Verification and Fault Testing

Before any mainnet promotion, the harness must pass deterministic tests for:

- Timeout after venue acceptance.
- Duplicate request delivery.
- Concurrent submissions and nonce collision.
- Partial entry fill without protective fill.
- Parent accepted with child rejected.
- Stale ticket, approval, risk snapshot, or market data.
- WebSocket disconnect, sequence gap, and snapshot recovery.
- Process crash before send, during send, and after response receipt.
- Restart with nonterminal outbox rows.
- Database corruption, backup restoration, and point-in-time recovery.
- Forward and rollback schema migrations with nonterminal commands present.
- Tamper-chain verification and detection of missing or rewritten ledger events.
- Loss and failover of an executor, reconciler, protection watchdog, or telemetry worker.
- Venue/API disagreement and delayed order appearance.
- Rate limiting and venue degradation.
- Clock skew and action expiry.
- Signer denial and key rotation/revocation.
- Dead-man trigger with open positions and protective orders.
- Daily-loss, drawdown, exposure, and data-quality circuit breakers.

Tests require recorded fixtures, property tests for monetary arithmetic and state transitions, testnet contract tests, and scheduled recovery drills. Recovery drills must demonstrate predeclared RPO and RTO for command state, ledger evidence, reconciliation, protection, and admission availability.

## 11. Mainnet Acceptance Criteria

A `manual_mainnet_canary` grant may begin only when:

- The exact thesis, strategy, and compiler versions have `evidence_status=validated` and have passed strategy-specific testnet qualification.
- Agents have no path to signing credentials.
- The signer independently enforces the account-safety policy and the scoped deployment grant.
- Risk is recalculated from fresh venue and portfolio state at send time.
- Authorization is hash-bound, single-use, and expiring.
- Durable outbox, client-ID uniqueness, execution locks, and restart reconciliation are proven.
- Protection watchdog and emergency reduce-only path are proven.
- All supported order types have testnet and fault-injection coverage.
- Monitoring, paging, incident ownership, and kill switch are operational.
- Time synchronization, reconciliation latency, protection latency, data freshness, and recovery SLOs are defined and continuously measured.
- Backup/restore, corruption recovery, migration rollback, worker failover, tamper verification, RPO, and RTO drills pass with retained evidence.
- Testnet and mainnet use separate credentials, account identifiers, configuration, persistence, and deployment approval.
- A dedicated account contains only explicitly capped risk capital.
- Key revocation and full shutdown have been rehearsed.
- Mainnet starts with the smallest supported scope and limits; expansion requires reviewed evidence.

## 12. Systematic Acceptance Criteria

Systematic operation inherits all technical, security, account-safety, reconciliation, and reliability requirements of manual mainnet; it does not inherit per-ticket human authorization. Per-ticket authorization is replaced by an expiring systematic strategy policy. Systematic testnet, shadow, and capped-mainnet authority require separate grants.

- `evidence_status=validated` with untouched holdout and prospective shadow evidence.
- A frozen deterministic strategy implementation and signed policy envelope.
- No LLM or mutable remote content in the live decision path.
- Deterministic replay from source data to signal, order, and authorization.
- Bounded evaluation/submission latency with an explicit stale-signal cutoff.
- Independent reconciliation and risk workers.
- Automated circuit breakers that fail closed without conversational approval.
- Governance for version changes, rollback, policy expiry, and emergency revocation.

## 13. Delivery Sequence

1. **Mirror and audit:** preserve upstream, inventory every artifact, verify claims, and publish the disposition matrix.
2. **Read plane:** implement normalized market/account reads and immutable evidence snapshots.
3. **Thesis laboratory:** implement registry, trial ledger, backtest protocol, holdout evaluation, and shadow scanner.
4. **Trade kernel:** implement canonical schemas, risk engine, authorization service, OMS, outbox, and ledger.
5. **Signer and adapter:** add isolated signing and one pinned venue SDK behind an adapter.
6. **Reconciliation and protection:** implement independent watchers, recovery, incidents, and paging.
7. **Testnet qualification:** rehearse every supported action and fault scenario.
8. **Capped mainnet:** use a dedicated funded account with per-ticket approval and conservative limits.
9. **Systematic qualification:** promote only frozen validated strategies into expiring policy envelopes.

## 14. Definition of Done

The harness is complete only when a reviewer can reproduce and prove, from immutable artifacts:

- Why a thesis was eligible to generate a signal.
- Which exact data and code generated it.
- Which signal-instance hash, strategy version, and deterministic compiler produced the semantic intent.
- Which active deployment grant authorized that thesis, account, venue, environment, and authorization model.
- Which portfolio state and limits permitted the ticket.
- Which semantic intent was authorized, which fresh admission decision allowed it, and which final wire payload was signed.
- Whether the venue accepted, rejected, rested, partially filled, filled, triggered, or canceled each leg.
- How the system recovered from missing or contradictory responses.
- Whether protection remained aligned with exposure.
- What the trade cost and whether process and outcome met their separate criteria.

An agent's assertion is never the final evidence for any of these questions.
