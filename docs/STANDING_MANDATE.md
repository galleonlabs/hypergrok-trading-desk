# Standing mandate

A standing mandate is an optional block you write yourself in `desk.md`. While it is valid, the desk may place orders on the network and account it names without asking you for each ticket, inside the limits you wrote. You can also leave those runs unattended. Every other control stays in place.

Nothing changes unless you write one. With no valid mandate, every send needs your approval by ticket id, exactly as in earlier releases.

## Risk statement

Automated trading can lose the whole amount you allocate, and it can do so quickly: while you are asleep, during a gap, or through a run of losing trades that each followed the rules. A stop is not a loss cap. Past results, backtests and testnet runs do not predict anything. HyperGrok makes no profit or return claim, ships no strategies and is not financial advice. You are responsible for every order placed under your mandate. Perpetual futures can liquidate an account.

## Where a mandate works

A mandate needs something outside the model that refuses exchange writes the mandate does not cover. HyperGrok ships that check only for Claude Code. In every other runtime the desk ignores the mandate and keeps per-ticket approval.

| Runtime | What "unattended" means there | What you must configure | Mandate honoured |
| --- | --- | --- | --- |
| Claude Code | A `claude -p` run started by your own scheduler, or `/loop` in a local session nobody is watching | The HyperGrok plugin enabled, with its hook; `HYPERGROK_DESK_ROOT` set to the desk directory in the environment Claude Code starts with; a narrow Bash allow rule for your send script; no `--bare` (it skips plugin hooks) and no bypass-permissions mode (Claude Code's docs do not say whether a hook's ask still holds there) | Yes, for order, cancel, modify and leverage writes the hook can match to the mandate. Every other write still asks. In `claude -p` a prompt nobody can answer is denied |
| Grok Bot | A routine running a Bot while you are away | Not applicable | No. Its Auto-review **Ask first** rule cannot read `desk.md`. Turning that rule off would leave nothing outside the Bots enforcing the mandate, so per-ticket approval stays |
| Cursor | Not applicable | Not applicable | No. HyperGrok ships no Cursor check that reads the mandate |
| Codex | Not applicable | Not applicable | No. HyperGrok ships no Codex check that reads the mandate |
| Gemini CLI | Not applicable | Not applicable | No. HyperGrok ships no Gemini CLI check that reads the mandate |
| Grok Build | Not applicable | Not applicable | No. HyperGrok ships no Grok Build check that reads the mandate |

Claude Code's cloud scheduled tasks do not load plugins installed in your user settings, so the hook is absent there and they are not supported. For desktop scheduled tasks, Claude Code's docs do not say whether plugin hooks run. Treat them as unsupported until you have seen the hook ask in one.

## Template

Add this block to `desk.md` and replace every value. The desk shows you this template but never fills it in, extends it or renews it for you.

```markdown
## Standing mandate

- network: testnet
- account: 0xYOUR_ACCOUNT_ADDRESS
- markets: BTC, ETH
- max risk per trade: 0.5%
- max total open risk: 1.5%
- max leverage: 3x
- daily loss stop: -2%
- max new entries per day: 4        # optional
- written: 2026-11-16
- expires: 2026-12-16               # optional
- acknowledgement: I wrote this mandate myself. I accept that the desk will place testnet orders for this account without asking me first, within these limits, until I revoke it, and that I am responsible for every order placed under it.
```

For mainnet, write `mainnet` both in `network` and in the acknowledgement sentence.

| Field | Required | Meaning |
| --- | --- | --- |
| `network` | yes | `mainnet` or `testnet`. The mandate covers that network only. |
| `account` | yes | The 0x account the API wallet trades for. Writes for any other account need approval by id. |
| `markets` | yes | A comma-separated list such as `BTC, ETH, xyz:TSLA`, or `all` on its own. `*` and mixed forms are invalid. |
| `max risk per trade` | yes | Loss to the stressed stop, as a percentage of equity. |
| `max total open risk` | yes | Open positions plus pending entries, as a percentage of equity. |
| `max leverage` | yes | A whole number such as `3x`. The exchange's market and tier maximum still applies. |
| `daily loss stop` | yes | A negative percentage of start-of-day equity. Reaching it suspends the mandate until 00:00 UTC. |
| `max new entries per day` | no | New positions or adds per UTC day. Without it, entries are limited only by the risk fields. |
| `written` | yes | The date you wrote the block, `YYYY-MM-DD`, not later than today in UTC. |
| `expires` | no | The last UTC day the mandate is in force. There is no maximum. |
| `acknowledgement` | yes | The sentence above, word for word, with your network. |

The limits are your own. Without a mandate, the desk ceilings in `desk-risk-limits` section 0 stay the defaults: 2% per trade, 6% total open risk, 20x and a -10% daily loss stop. A mandate may set values above them. The validator marks any value above a default but does not reject it. The 1.7.3 ceilings still cap `risk-limits.md` and every ticket you approve by id.

For a ticket placed under the mandate, these fields replace the matching settings in `risk-limits.md`: per-trade risk, total open risk, leverage, daily loss stop and allowed markets. Its other settings still apply: position count, correlated clusters, slippage tolerance, mandatory stops and the unprotected-position deadline.

## What a mandate does not relax

Every order under a mandate still has:

- a ticket with an id, opened and recorded under `proposals/` like any other;
- a Risk Manager PASS computed from live account state against the mandate's limits, using the sizing arithmetic in `desk-risk-limits`;
- an exchange-resting reduce-only stop, placed with the entry (`normalTpsl`) or immediately after it;
- one bounded send with `expiresAfter`, reconciliation from the exchange record, and a journal entry;
- the exchange's own constraints: tiered maximum leverage, minimum order value and size decimals.

Only one step changes. Instead of "the user approved this ticket id", the Execution Trader checks that the ticket is inside the mandate. The ticket records which mandate authorised it and the check against each field:

```
authority: standing mandate 2026-11-16 sha 3f1c0a9b2d4e6f70
mandate check: network testnet ok | account 0xabc...def ok | market ETH in [BTC, ETH] ok | risk 0.48% <= 0.5% ok | open risk after 1.2% <= 1.5% ok | leverage 3x <= 3x ok | entries today 2 of 4 ok | day pnl -0.4% above -2% ok | expires 2026-12-16 ok
```

`sha` is the fingerprint that `scripts/standing_mandate.py` prints. If the block changes, the fingerprint changes. The desk re-reads the block before every ticket and applies the new values. A ticket checked against the old values is checked again before it is sent.

## Never covered by a mandate

These always need approval by ticket id, or stay in the Hyperliquid app with you:

- deposits, withdrawals, transfers between accounts, sub-accounts or vaults, bridging, and sending USDC or spot tokens;
- approving an API wallet or agent key, or a builder fee;
- any action for an account other than the one named, or on a network other than the one named;
- margin-mode changes and isolated-margin top-ups beyond what the ticket itself needs, dead-man's switch, and TWAP orders;
- writing, widening, extending or renewing the mandate. Only you edit the block. The desk never suggests loosening it, and certainly not after a loss.

## Suspension, alerts and revocation

- **Daily loss stop.** When the day's loss reaches the mandate's `daily loss stop`, the Risk Manager suspends the mandate for the rest of the UTC day and alerts you. The Trade Reviewer journals `HH:MM UTC  mandate suspended  mandate <written>  reason daily-loss-stop` in `journal/YYYY-MM-DD.md`. The validator and the Claude Code hook read that line, so neither the desk nor a rewritten block can lift the suspension before 00:00 UTC. Until then, every new send needs approval by id. Exits and protective stops follow the normal rules.
- **Position without a resting stop, rejected order or stale data.** The desk alerts you and handles that ticket safely, but the mandate stays in force. A position without a stop gets a protective reduce-only stop at priority. A rejected order voids its ticket, and any fix is a new ticket, never a tweak-and-resend. Stale or unreadable account or market data makes that ticket a REJECT for `missing input`. An unknown send result follows `desk-incident-response` playbook A and is never resent.
- **Edits.** Changing the block does not suspend it. The desk re-reads it before each ticket and applies the new values.
- **Expiry.** If you wrote `expires`, the mandate ends after that UTC day.
- **Revoke.** Delete the block, or write the line `standing mandate: revoked` anywhere in `desk.md`. Either takes effect on the next check, before the next send. A revoke line overrides any block.

A block that cannot be parsed, has a required field missing or malformed, appears twice, or sits beside `standing mandate: none` is treated as no mandate. The validator names the field that failed.

HyperGrok sends no notifications. In an unattended run, an alert is the run's output and the journal entry. If you want to be paged, have your scheduler surface the run output.

## Claude Code setup

1. Write and check the mandate: `python3 scripts/standing_mandate.py --desk-root <desk> --network testnet`. It reads `desk.md` and today's journal file only, makes no network request and never writes. It exits non-zero on an invalid, expired, suspended or mismatched mandate. `python3 scripts/desk_doctor.py --desk-root <desk>` runs the same check.
2. Set `HYPERGROK_DESK_ROOT` to the desk directory in the environment Claude Code starts with. If it is unset, the hook keeps asking on every write.
3. Make the send script name its network endpoint or SDK constant (`constants.TESTNET_API_URL` or `constants.MAINNET_API_URL`), the mandate's account address, and no other address. Pass the ticket id on the command line, for example `python3 tickets/send.py --ticket HG-20261116-03`.
4. Add a narrow Bash allow rule for that script. A silent hook does not approve anything. Claude Code's own permission rules decide whether the command runs.

The hook stays silent only when all of these hold: the mandate is in force for the network the write names; every write in the command and in the scripts it names is an order, cancel, modify or leverage call; the write names the mandate's account and no other address; and the named ticket's proposal file holds a PASS and the current `authority:` line. Otherwise it asks, and says why.

## What the checks cannot prove

The Claude Code hook reads text. It cannot check sizing, open risk, the day's loss figure, the entry count, the market or the stop. It does not follow imports, so a write in a helper module goes unseen. A network constant that appears in code the script never runs can mislead it. It can only see that the proposal file holds a PASS line, not that the Risk Manager's arithmetic is right. The desk writes the journal and proposal files, so a model that ignored its instructions could write them too. The hook also asks before a Write or Edit that adds or changes a mandate block, and before a Bash command that redirects into, edits, moves, copies or deletes `desk.md`. That makes it harder, but not impossible, for the model to author its own mandate. These checks are a backstop, not proof that every order fits your mandate. Review the journal and the exchange record.
