# Changelog

All notable changes to HyperGrok are recorded here.

## Unreleased

### Setup and usability

+ Add `hypergrok quickstart`, a plain-English readiness check that prints the exact next step and never prints a key.
+ Load `.env` from the working directory or any parent. Previously `.env.example` documented settings that nothing read, so configuration was silently ignored. Real environment variables still win.
+ Make `plan-order` emit a copy-pasteable `next_command` including `--plan`.
+ Report account-specific setup in `doctor` rather than operational tasks that are not the user's to perform.
+ Rewrite the README around a ten-minute first run and explain the hash confirmation in plain language.

### Risk limits

+ Add `hypergrok limits <COIN>`, reporting the constraints Hyperliquid itself enforces: per-asset max leverage, tiered margin, size decimals and the 10 USD minimum order value.
+ Remove HyperGrok's own risk-per-trade and notional ceilings. Both are now opt-in via `HYPERGROK_MAX_RISK_PCT` and `HYPERGROK_MAX_ORDER_NOTIONAL_USD`; unset means no ceiling. Sizing judgment belongs to the risk officer working from exchange limits.
+ Enforce Hyperliquid's 10 USD minimum order value at plan time.
+ Widen the slippage tolerance range and make the plan lifetime configurable, keeping the 30-minute default.

### Execution

+ Stop attribution eligibility from blocking execution. An order that cannot carry attribution is now sent without it rather than refused; every user-safety gate is unchanged.
+ Apply attribution on mainnet only.

### Verification

+ Add three live verification harnesses: `scripts/verify_cli.py`, `scripts/verify_repo.py` and `scripts/verify_skills.py`.

+ Make `BOOTSTRAP.md` the single Grok Bot entry point and define a supported two-group desk topology.
+ Add a finite SDK send timeout and read-only cloid reconciliation command.
+ Enforce the current configured slippage cap again at execution time.
+ Align Grok Build metadata with the official plugin marketplace shape and mark non-execution agents read-only.

## 1.0.0 - 2026-08-16

+ Support Hyperliquid testnet and mainnet through one guarded command surface.
+ Add seven specialist agents, eleven original skills and verified team bootstrap instructions.
+ Add live readiness diagnostics, immutable short-lived plans, exact hash confirmation, account binding, API-wallet role checks, price revalidation and duplicate cloid protection.
+ Bind the only order send to fixed, integrity-checked metadata with fail-closed live readiness gates.
+ Add Python 3.11-3.13 CI, CodeQL for public runs, dependency updates, package builds, coverage enforcement and read-only live smoke checks.
