# Changelog

## Unreleased

## 1.1.1 - 2026-08-29

Consistency pass over the surfaces 1.1.0 changed underneath. 1.1.0 corrected the sizing arithmetic but left the desk's worked example, `README.md` and `docs/ARCHITECTURE.md` describing the old behaviour, so the front page taught the bug the release had just fixed.

+ The `HG-20260816-01` example is now one trade end to end at 0.4827 ETH: `README.md`, `desk-trade-lifecycle`, `desk-monitoring`, `desk-post-trade-review`, `agents/execution-trader.md` and `agents/trade-reviewer.md`. Sizing is proportional, so the R multiple (+0.9R) and the cost figure (16 bps of notional) are unchanged; notional, margin, fees, funding and PnL scale with the size. The generic SDK snippets in `hyperliquid-orders` keep a round size on purpose - they teach API syntax and are not part of the narrative.
+ `README.md`: the ticket shows the stressed distance it was sized from, and the guarantees section no longer implies the approval phrase is itself the gate.
+ `docs/ARCHITECTURE.md`: trust boundaries carry the bounded-send and expiry rule, the approval boundary matches the operating model, and `unavailable` is documented as a verdict.
+ `CONTRIBUTING.md`: a release procedure, because `SETUP.md` pins a tag and a pin drifts silently the moment `main` moves ahead of it. Also records that the worked example is cross-file state, and corrects the stated skill body budget (320 lines, which is what `scripts/check.sh` enforces).

## 1.1.0 - 2026-08-29

Risk, evidence and recovery hardening. Several of these came out of reviewing a public fork of this repository, which audited the desk against production-custody standards; the findings that survived being a markdown desk rather than a signing service are below.

**Sizing now stresses the stop.** `desk-risk-limits` sized from the nominal stop distance, which assumes a triggered stop fills at its trigger price. It does not: a stop is a trigger order that becomes a market order, slips, and pays taker on both legs. Every trade therefore risked more than the budget said. Size now comes from `stressed_distance` (slippage plus both fee legs). On the desk's own worked example the old arithmetic spent 0.528% of equity against a 0.5% budget; the new arithmetic lands on the budget exactly. `agents/risk-manager.md` carries the recomputed example.

+ `desk-risk-limits` section 0: desk ceilings the user's limits file may only tighten, never loosen - 2% per trade, 6% total open risk, 20x, -10% daily, mandatory exchange-resting stop, and no standing approval covering a mainnet send. They are deliberately far looser than any sane setting; they exist so a mistyped or corrupted file cannot authorise a catastrophic ticket. Enforced in the sizing arithmetic, not only asserted.
+ `unavailable` is a verdict, not silence. `desk-operating-model` makes missing, stale, gapped, partial or cross-network data a first-class outcome that never collapses into "the condition did not fire". `desk-monitoring` applies it to watches: a dead feed reporting "not crossed" looks exactly like a calm market, so every watch carries a staleness bound and alerts when it cannot tell.
+ The approval line is evidence, not the gate. The Bots write the floor's messages, so an approval a Bot can read is one a Bot could have written. `desk-operating-model` and `desk-trade-lifecycle` now put enforcement out of band and forbid a Bot writing, quoting forward, inferring or simulating the user's approval.
+ Unknown results: a clean read was never proof. A send that timed out can still land after any number of quiet checks. `desk-execution-protocol` now requires `expiresAfter` on every send in the pre-send checklist and treats a ticket as dead only when the original is provably incapable of arriving, with `noop` as the fallback when no expiry was set.
+ `desk-strategy-lab`: a freeze checklist for claims imported from outside (a repository of settings, a thread, a screenshot), multiplicity across every variant tried including the source's, a block-bootstrap lower bound beside the mean expectancy, a cost-doubling sanity check, and the rule that looking at the holdout spends it.
+ `desk-execution-protocol`: repaired a rule that was truncated mid-sentence ("Never let a routine send.").
+ `SETUP.md`: the install no longer falls back to piping an unverified archive from a mutable branch into `tar`. It clones a pinned tag, runs `scripts/check.sh` before a desk is built from the tree, and records the installed commit in `desk.md` so the desk can say which rules it is running.
+ Docs: the exact Claude Code install commands (`/plugin marketplace add galleonlabs/hypergrok-trading-desk`, `/plugin install hypergrok@hypergrok`), verified end to end against the published repository. "Open the repository, enable it" was not an actionable path in Claude Code even though the marketplace manifest shipped and worked.
+ `docs/ARCHITECTURE.md` now lists the `.claude-plugin/` manifests alongside the others.
+ `scripts/check_manifests.py`: install commands in the docs must name a marketplace and plugin id this repository actually declares.
+ Fixed: `predictedFundings` is normalised by each venue's own funding interval.
+ Fixed: the WebSocket watch no longer points at the rejected `webData2` subscription.

Note on versions: the `v1.0.0` git tag pointed at an orphaned lineage (the earlier Python CLI, with `src/` and `pyproject.toml`) that is not an ancestor of `main`, while the 1.0.0 entry below describes this markdown desk. The stale tag has been removed; `v1.1.0` is the first tag that matches what this repository actually ships.

## 1.0.0 - 2026-08-16

Launch. There is no `v1.0.0` git tag for this entry; see the versioning note in 1.1.0 above.

+ `SETUP.md`: the file a Grok Bot follows to build the desk - repository onto the computer, seven Bots from profile cards, sixteen shared skills, one Trading Floor group chat, approval rules, desk record, read-only verification, receipt.
+ Seven roles with full system prompts: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer.
+ Eight Hyperliquid skills: setup and API wallets, market data, account reads, orders, positions and margin, WebSocket, advanced actions, API reference. `curl` for reads, official Python SDK and `@nktkas/hyperliquid` for writes; snippets verified against the live API.
+ Eight desk skills: operating model, trade lifecycle, risk limits and sizing, execution protocol, monitoring, post-trade review, incident response, strategy lab.
+ Docs: how the desk works, FAQ, provenance. Mascot in `assets/`.
+ `scripts/check.sh` and CI: frontmatter, links, one-writer rule.
+ Plugin manifests and rule so Grok Build, Cursor and Claude Code load the same roles and skills.
