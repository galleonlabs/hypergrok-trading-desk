# Testing

## Deterministic suite

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest -q --cov=hypergrok --cov-fail-under=75
python -m build
```

The suite uses fake SDK modules and API responses. It proves the only send carries the exact required metadata and cloid, and that malformed plans, wrong confirmations, network mismatches, account mismatches, readiness failures, duplicates, stale prices and unauthorised signing wallets all produce zero sends. Journal tests prove one plan hash can reserve the local send boundary only once and that terminal/unknown records cannot return to sending.

## Live verification harnesses

Three harnesses exercise the real CLI against live endpoints. None signs, submits
an order or handles a private key.

```bash
python scripts/verify_cli.py .     # 54 checks: every command, both networks, all execution gates
python scripts/verify_repo.py .    # 43 checks: packaging, plugin manifests, docs, install paths
python scripts/verify_skills.py .  # 68 checks: every SKILL.md procedure, executed
```

`verify_cli` proves each fail-closed gate refuses independently, and that a valid
plan clears every pre-signing gate against live Hyperliquid before stopping at
the key boundary. `verify_repo` builds and installs the wheel, checks both
documented install paths and validates every plugin manifest and internal link.
`verify_skills` runs each skill's numbered procedure and asserts its own
Verification clause is satisfiable from the real output, including the
market-analyst to risk-officer handoff.

## Live read-only smoke

```bash
python scripts/live_smoke.py
```

This calls public read endpoints only:

+ Hyperliquid testnet `allMids`
+ Hyperliquid mainnet `allMids`
+ DefiLlama protocol data for Hyperliquid
+ CoinGecko data for Hyperliquid

It imports no signing module and submits no action. A green smoke test proves connectivity and basic response shape, not trading readiness.

## What is never automated in CI

No order, cancellation, transfer, approval, deposit, withdrawal, bridge or reward claim. Mainnet and testnet execution are verified through the same fake one-send contract and fail-closed gates.
