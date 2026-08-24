# SMA-outfits validation disposition

Reviewed source: `unfairmarket/SMA-outfits` at commit `4f14aa262fcd9524722f5cc1e2b767587327de7b` on 2026-08-24.

## Finding

The repository is a hypothesis catalog, not a strategy implementation or validation artifact. At the reviewed commit it contains only `README.md` and `LICENSE`; the data, scripts, models, methodology files and charts described in its README are not present.

The README supplies:

- many proposed SMA period sets, including `19/37/73/143/279/548`;
- a generic statement that a short-SMA crossover above/below a long SMA is a buy/sell signal;
- three more-specific public-equity descriptions: SPX 30m `10/50/200`, IXIC 20m/30m `20/100/250`, and DJI 15m/1h `30/60/90/300/600/900`;
- retrospective X case-study links and broad claims about institutional behavior.

It does not supply a complete reproducible trading rule: exact OHLC input, bar/session construction, transition semantics, entry observability, signal expiry, exit, stop, sizing, fees, spread, slippage, funding, data snapshots, trial count, holdout, code, or prospective results are missing. Claims of precision, causation, institutional coordination, or profitability are therefore unverified.

## Harness use

SMA-outfits may inform new `draft` theses. It may not be imported as a validated skill, profitability attestation, deployment grant, or execution rule.

Each proposed outfit must independently freeze:

1. Instrument and venue; crypto cannot silently inherit an equity-index claim.
2. Exact timeframe and completed-bar construction.
3. Price field and SMA formula.
4. Exact transition/hierarchy rule and abstention cases.
5. Next-observable entry, expiry, mandatory stop, target and time exit.
6. Costs, funding, liquidity and rejected-fill model.
7. Full parameter/asset/timeframe family for multiplicity correction.
8. Chronological development/OOS boundaries and an uninspected holdout.
9. Prospective append-only shadow duration and promotion criteria.

The current harness does not select the best outfit after inspecting outcomes. A failed or inspected dataset remains discovery data. Any implemented SMA thesis will get a new strategy/hash and must pass the same costed historical and prospective gates as every other strategy.

## Current empirical benchmark

The separately preregistered `candidate-v0/1` EMA/Donchian/ATR strategy is not derived from SMA-outfits. Its first live-data historical run on the latest 4,999 completed ETH 4h bars was `REJECTED`: 116 trades, -0.0331R mean net expectancy, 0.9401 profit factor, -0.2484R one-sided bootstrap lower bound, 19.4628R maximum drawdown, and negative stressed expectancy. This result is retained as failed evidence and is not a license to tune the inspected holdout.
