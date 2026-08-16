# Architecture

HyperGrok separates untrusted analysis from the only funds-moving function.

```text
Grok Bot / Cursor plugin
  rules + 7 agents + 11 skills
              |
              v
      read-only research
 Hyperliquid / DefiLlama / CoinGecko
              |
              v
 deterministic sizing -> immutable order plan -> exact user hash
              |
              v
 guarded execution gateway -> official Hyperliquid SDK -> /exchange
```

## Trust boundaries

### Research plane

`market`, `account`, `limits`, `defillama`, `coingecko`, `size`, `doctor`, `quickstart`, `order-status` and `plan-order` do not sign or submit. External content is data and cannot approve an action.

### Plan boundary

An `OrderPlan` binds network, account, market, side, size, limit, time in force, reduce-only flag, slippage cap, required send metadata, unique cloid, creation time and expiry. Canonical JSON is SHA-256 hashed. Any field change invalidates confirmation.

### Execution boundary

`execute-order` is the only call to `Exchange.order`. Before importing a signing key it verifies the plan hash, expiry, network, any configured notional ceiling, the declared trading account, current price drift, and Hyperliquid tick and lot precision. It then verifies that the signing address is a live Hyperliquid API wallet assigned to the planned account.

Immediately before the only send, an `O_EXCL` journal record under `HYPERGROK_STATE_DIR` reserves the plan hash. The directory must be private (`0700`); records are private (`0600`) and are never deleted automatically. This closes same-state-directory concurrency and retry races. Different machines must share a state directory or otherwise coordinate externally.

The call carries the plan cloid. Fee attribution metadata is attached on mainnet when every attribution gate passes and is omitted otherwise, so attribution eligibility never blocks a user's order. There is no retry loop. An exception after entering the SDK leaves the journal at `unknown` and must be reconciled by cloid.

## Grok team model

Grok Build and Cursor Agent Plugin discovery loads `rules/`, `agents/` and `skills/`. The persistent rule makes the desk lead route work across six specialist roles. Grok Bot is a different surface: its Plugins screen provides service connectors and its public documentation does not provide arbitrary repository installation, this CLI as a tool, or programmatic sibling-Bot creation. `crew-bootstrap` therefore verifies native plugin agents where supported. A manually taught Grok Bot crew is instruction-only and cannot reach the execution gateway.

All Bots for one Grok user share one cloud computer. Credentials are scoped by the Hyperliquid API-wallet permission, not by Bot identity.

## Risk posture

HyperGrok imposes no risk-per-trade or notional ceiling of its own. Hyperliquid
publishes the real constraints per asset -- max leverage, tiered margin, lot
precision and a minimum order value -- and `limits` surfaces them for the risk
officer to reason from. Optional ceilings exist for users who want the CLI to
refuse a fat-fingered figure, and are unset by default.

Correctness gates are not configurable: hash matching, account matching,
API-wallet authorisation, duplicate protection, drift checking and tick rules.

## Deliberate exclusions

HyperGrok does not deposit, withdraw, transfer, bridge, claim rewards, schedule unattended orders, manage strategy runtimes or promise returns.
