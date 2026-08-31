# Changelog

## Unreleased

+ Added a versioned Grok Bot template contract for **HyperGrok Desk Lead**: exact public profile, pinned source URLs, mascot, seventeen skill paths and SHA-256 hashes, with plugins, memories and routines explicitly empty.
+ `hypergrok-bootstrap` and `SETUP.md` now recognise skills imported with the public template, install only what is missing and fail readiness on same-name content drift instead of creating duplicates or trusting a name alone.
+ `scripts/check_grok_template.py` verifies the release pin, public safety copy, avatar, exact skill inventory and hashes, empty optional capabilities and published-link wiring. Five negative fixtures prove the gate rejects release drift, missing skills, stale hashes, bundled plugins and invalid share URLs.
+ `docs/GROK_BOT_TEMPLATE.md` defines authoring, logged-out preview and clean-install acceptance. The supported path is one click and one message: **Add to Grok Bot**, then **Start the desk.**

## 1.3.0 - 2026-08-31

Fast-start Trading Floor release. A fresh Grok Bot workspace can now build the full seven-role desk through one bounded bootstrap skill, show useful live data before asking setup questions, and return machine-checkable readiness evidence.

+ `hypergrok-bootstrap`: installs the pinned release, rings the Opening Bell, prepares the desk, creates the seven role profiles and private Trading Floor where supported, installs all seventeen skills, writes a research-only desk record and returns a result for every required check. It refuses keys, exchange writes and public sharing during bootstrap.
+ `scripts/opening_bell.py`: a zero-dependency, zero-key snapshot from Hyperliquid public `/info`, covering mid, mark, oracle, 24-hour change and volume, hourly funding, open interest, spread and visible 5/10/25 bps depth. Every output identifies source, network and UTC time and states that no account or order path was touched.
+ `scripts/desk_doctor.py`: deterministic release, inventory, workspace and public-connectivity checks. It never reads environment variables or account state. A missing desk record or risk limits is a visible warning; a broken release, missing component or dead public API is a failure.
+ Four Opening Bell tests and three desk-doctor tests now run in `scripts/check.sh`, including depth arithmetic, crossed-book rejection, missing-desk warnings and a key-like material guard.
+ `README.md`, `SETUP.md`, the architecture, FAQ and skills index now present the fast path and retain the complete source-driven fallback. The distribution carries seventeen skills and pins setup to `v1.3.0`.

+ `SETUP.md` section 1 pins the clone to a release tag, and `CONTRIBUTING.md` already recorded that a drifted pin is worse than none - "if step 4 is skipped, the release ships instructions pointing at the previous one". Nothing enforced it: bumping all six manifests and leaving the `--branch` pin behind passed `scripts/check.sh` green, so the one command every new desk runs first would have installed the previous release while the docs described the new one. `check_manifests.py` now fails when a documented `git clone` of this repository names a tag other than `v<manifest version>`, names a moving branch, or carries no ref at all, with three negative fixtures covering those cases.
+ README linked the live skills.sh listing at `/galleonlabs/hypergrok-trading-desk/hypergrok`. skills.sh indexes skill directories, not the plugin id, so that page reports `hypergrok` is not in the repository even though the pack page (16 skills) is live. The link now points at the pack. `check_manifests.py` fails if a documented skills.sh URL or `skills add` argument names a slug this repository does not ship.
+ `desk-risk-limits` and `agents/risk-manager.md` quoted the worked example's `slip_stop` of 3.00 as "10 bps" *on the trigger*, but 10 bps of the 2,900 trigger is 2.90, which gives a stressed distance of 105.55 and a size of 0.4831 ETH - not the 105.65 and 0.4827 ETH those files, `README.md` and `desk-trade-lifecycle` all carry. 3.00 is 10 bps of the 3,000 ticket price, which is how `desk-trade-lifecycle` already quotes slippage. Both glosses now name that reference price, so a Risk Manager showing every line of the arithmetic reproduces the desk's own numbers instead of contradicting them. `slip_stop` stays defined in price units; no figure in the worked example moves.

## 1.2.0 - 2026-08-29

Correctness pass across the Hyperliquid skills and the incident playbooks, closing the last findings from the fork audit that genuinely apply to a markdown desk. Three of these could strand a real position.

**A partial close stripped the remainder's protection.** `hyperliquid-positions` said to cancel orphaned TP/SL "after any close". After a *partial* close the position still exists and still needs its stop, so the sweep removed the protection from what was left. Clean-up is now conditional on reading `clearinghouseState`: cancel orphans only on a confirmed full close; on a partial close, replace a fixed-size stop before cancelling the old one, and leave a position-tied stop alone. Same fix in the `desk-execution-protocol` close procedure, which had the same reflex.

**The TypeScript bracket carried no client order ids.** The grouped entry/tp/sl example omitted `c:` on all three legs, while `desk-execution-protocol` requires a cloid on every send and the whole unknown-result recovery path is a lookup by cloid. A send built from that example could not be reconciled. Every leg now carries its own.

**The dead-man's switch is not position-aware.** `scheduleCancel` cancels protective stops along with everything else, so arming it with a position open leaves that position naked when it fires, and nothing re-arms it. It is now scoped to a desk with resting orders and no position, and firing with a position open is an unprotected-position incident rather than clean-up.

+ `desk-incident-response`: playbook A matches the recovery rule from 1.1.0 - a clean check is not proof, and a replacement waits for the original to expire. Playbook D no longer lets an unprotected position wait forever on a human: the first alert carries exposure and distance to liquidation, escalation runs on a deadline recorded in `desk.md`, and when it passes the desk tells the user to fix it in the Hyperliquid app rather than pinging a channel nobody is reading.
+ Standing approvals are scoped by what they can do rather than by network. The ceiling is now "no standing approval for a mainnet send that can open or increase exposure", with reduce-only protection as the explicit carve-out on any network - it can only reduce risk, and the alternative is a naked position waiting on a message. `SETUP.md` asks the user that question during setup and records the answer, with a deadline, in `desk.md`.
+ `docs/FAQ.md`, `SECURITY.md` and `agents/execution-trader.md` described the pre-1.1.0 approval and unknown-result rules. They now match the skills.
+ `SETUP.md`: note that `git clone --depth 1 --branch <tag>` prints a harmless `refs/tags/... is not a commit!` warning for an annotated tag, so a Bot following the file does not read it as a failed install and abort.

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
