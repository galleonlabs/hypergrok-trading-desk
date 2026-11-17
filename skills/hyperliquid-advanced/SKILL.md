---
name: hyperliquid-advanced
description: Less common Hyperliquid actions and their rules - dead-man's switch (scheduleCancel), TWAP orders, spot orders, expiresAfter and nonces, API wallet approval from code, sub-account and vault addressing, HIP-3 dexs, and what the desk deliberately does not do (transfers, withdrawals, builder fees, staking). Write actions are Execution Trader only, on an approved ticket. Use when a ticket asks for one of these or when a user asks whether the desk can.
license: MIT
metadata:
  version: "1.1.2"
  author: Galleon Labs
  category: hyperliquid
  network-default: testnet
---

# Hyperliquid advanced actions

Uses the same header as `hyperliquid-orders` (`info`, `exchange`, `ACCOUNT`, `round_px`, `round_sz`, `new_cloid`). Everything that signs is Execution Trader only, on a ticket.

## Dead-man's switch (`scheduleCancel`)

Tells the exchange to cancel **all** of the account's open orders at a future time unless you push the time out or clear it. Useful when the desk runs a watch that could die while resting orders sit on the book. The user asks for it explicitly; the report states the time.

```python
from hyperliquid.utils.signing import get_timestamp_ms
res = exchange.schedule_cancel(get_timestamp_ms() + 10 * 60 * 1000)   # 10 minutes out; must be >= 5 s in the future
res = exchange.schedule_cancel(None)                                    # clear it
```

TypeScript: `await exchange.scheduleCancel({ time: Date.now() + 600_000 })`; omit `time` to clear. Limits: at most 10 triggers per day per account (resets 00:00 UTC).

**It cancels protective stops too, and it is not position-aware.** If it fires while a position is open, that position is left naked on the exchange and nothing re-arms it automatically. So: it is for a desk with resting orders and no position, or for a user who has explicitly accepted that outcome for this account. Do not arm it as routine hygiene while a position is open. If it does fire with a position open, that is an unprotected position the moment it lands - declare it and run playbook D in `desk-incident-response` at priority, rather than treating re-arming as clean-up.

## TWAP

Splits a size into slices at fixed intervals (30 seconds minimum) over `m` minutes: 5 minutes to 7 days per the docs, though the TypeScript SDK's schema caps `m` at 1440 (24 hours), so longer runs need the raw action. Minimum 100 USD total; each slice capped at 3% slippage; `t: true` randomises slice sizes by up to 20%. Not in the Python SDK's high-level `Exchange`; use TypeScript or the raw action.

```ts
const twap = await exchange.twapOrder({ twap: { a, b: true, s: "10", r: false, m: 30, t: true } });
const twapId = (twap.response.data.status as { running: { twapId: number } }).running.twapId;
await exchange.twapCancel({ a, t: twapId });
```

Raw action: `{"type":"twapOrder","twap":{"a":1,"b":true,"s":"10","r":false,"m":30,"t":true}}`; response `{"status":{"running":{"twapId":N}}}` or `{"status":{"error":"..."}}`. Monitor with the `twapStates` / `userTwapSliceFills` WebSocket subscriptions or `userTwapSliceFills` reads; TWAP fills carry a zero hash. A TWAP is still one ticket; the ticket states size, minutes, randomisation and reduce-only.

## Spot orders

Same `order` action; asset id is `10000 + index` of the pair in `spotMeta.universe`; size decimals are the **base token's** `szDecimals`; price gets `8 - szDecimals` decimals (still 5 significant figures); minimum order value is 10 quote tokens. The Python SDK resolves `"PURR/USDC"` (and app-style aliases such as `"HYPE/USDC"` where unambiguous) to the spot asset id for you:

```python
# header from hyperliquid-orders, then spot-specific rounding from spotMeta
from hyperliquid.utils.signing import float_to_wire
pair_name = "PURR/USDC"
spot = info.spot_meta()
pair = next(p for p in spot["universe"] if p["name"] == pair_name)
base_dec = next(t for t in spot["tokens"] if t["index"] == pair["tokens"][0])["szDecimals"]

def round_px_spot(px, rounding=ROUND_HALF_UP):
    value = Decimal(str(px))
    if not value.is_finite() or value <= 0:
        raise ValueError("spot price must be finite and positive")
    max_dec = max(8 - base_dec, 0)
    exponent = min(0, max(value.adjusted() - 4, -max_dec))
    result = value.quantize(Decimal(1).scaleb(exponent), rounding=rounding)
    if result <= 0:
        raise ValueError("spot price rounds to zero")
    return result

def round_sz_spot(sz):
    value = Decimal(str(sz))
    if not value.is_finite() or value <= 0:
        raise ValueError("spot size must be finite and positive")
    result = value.quantize(Decimal(1).scaleb(-base_dec), rounding=ROUND_DOWN)
    if result <= 0:
        raise ValueError("spot size rounds to zero")
    return result

def bounded_px_spot(reference, slippage, is_buy):
    reference, slippage = Decimal(str(reference)), Decimal(str(slippage))
    if not reference.is_finite() or reference <= 0:
        raise ValueError("spot reference must be finite and positive")
    if not slippage.is_finite() or not 0 <= slippage < 1:
        raise ValueError("slippage must be a fraction in [0, 1)")
    bound = reference * (1 + slippage if is_buy else 1 - slippage)
    return round_px_spot(bound, rounding=ROUND_DOWN if is_buy else ROUND_UP)

sz, px = round_sz_spot("120"), round_px_spot("0.1234")
assert sz * px >= 10, "below the 10 quote-token minimum"
sdk_sz, sdk_px = float(sz), float(px)
assert Decimal(float_to_wire(sdk_sz)) == sz, "SDK changed the approved size"
assert Decimal(float_to_wire(sdk_px)) == px, "SDK changed the approved price"
res = exchange.order(pair_name, True, sdk_sz, sdk_px, {"limit": {"tif": "Gtc"}}, cloid=new_cloid())
```

These helpers retain Decimal values until the SDK boundary, preserve valid integer prices, and reject nonfinite or zero results. For a bounded spot IOC use `bounded_px_spot` with the ticket's reference and fractional slippage: buys round down within the ceiling and sells round up within the floor. Verify the final encoded SDK price against that bound before signing; never widen it to obtain a fill.

Read balances with `spotClearinghouseState` (`hyperliquid-account`). Spot has no leverage and no liquidation. Under `unifiedAccount`/`portfolioMargin` abstraction modes balances behave differently; the desk stays in the default mode unless the user changes it in the app.

## expiresAfter and nonces

- Every signed action carries a `nonce` (unix ms). The SDKs manage it. Nonces are per **signer**, so two processes signing with the same API wallet at the same time will collide; the desk keeps one Execution Trader and one process at a time.
- `expiresAfter` (ms) makes an action void if it reaches the exchange after that time. Useful protection against a delayed duplicate after a reconnect: `exchange.set_expires_after(get_timestamp_ms() + 60_000)` in Python (applies to following L1 actions; must be `None` for user-signed actions), or per-call `{ expiresAfter }` in TypeScript. A stale rejection costs 5x rate-limit weight, so keep it generous (a minute) rather than tight.
- Hyperliquid retains the highest 100 nonces for each signer and permits out-of-order acceptance within that window. A newer nonce does not invalidate every lower nonce.
- `noop` marks its own nonce used. To invalidate an unknown in-flight action, a recovery ticket must bind it to the **original exact signed nonce**, the same API wallet signer and the same network. Risk PASS, user approval by id and the native approval gate still apply; only the Execution Trader sends it, once. Confirm that the noop was accepted, then reconcile the original order and account. A nonce-reuse rejection does not confirm the noop landed or establish whether the original executed. If the original nonce is unavailable or the noop result is unconfirmed, keep the reservation and pause new risk; escalate to the Desk Lead and user. See `desk-execution-protocol` for expiry-based recovery.

## Approving an API wallet from code

The desk's path is the app (`hyperliquid-setup` section 4), because approval must be signed by the **main** wallet and that key never touches the desk computer. For reference only: `Exchange(main_wallet).approve_agent(name)` in Python generates a fresh agent key and returns `(result, agent_private_key)`; TypeScript `exchange.approveAgent({ agentAddress, agentName })` approves an address you generated. Named agents can carry an expiry via `"name valid_until <ms>"` (up to 180 days); an account may hold 1 unnamed and up to 3 named agents, plus 2 named per sub-account; re-approving the same name (or a new unnamed agent) replaces the previous key. Never reuse a revoked agent address.

## Sub-accounts and vaults

Orders can be sent **for** a sub-account or vault by setting `vaultAddress` (Python: `Exchange(..., vault_address="0x...")`; TypeScript: `defaultVaultAddress` or per-call `{ vaultAddress }`); the API wallet of the master signs. Reads for that address use the sub-account/vault address as `user`. The desk supports this only if the user asks and records it in `desk.md`; it never creates sub-accounts or transfers funds into or out of them.

## HIP-3 builder dexs

Other perp dexs exist beside the main one (`perpDexs`). Coins are `dex:COIN`, asset ids are `100000 + 10000 x dex index + index in that dex's meta`, margin is often isolated-only, and reads take a `dex` parameter. Off by default on the desk; if the user wants one, everything above applies with the prefixed coin name and the dex's own `meta`.

## Rate limits and `reserveRequestWeight`

Address-based limits for actions: a buffer of 10,000 plus 1 per 1 USDC of cumulative volume; when exhausted, 1 action per 10 seconds (cancels get extra headroom). A batch of n orders or cancels counts n times. Check `userRateLimit`. An account can buy more with `reserveRequestWeight` (0.0005 USDC each, paid from the perp balance). The desk does not do this automatically; mention it if the user hits the limit. Per-IP `/exchange` weight is `1 + floor(batch size / 40)`.

## Deliberately not on this desk

These exist in the API and SDKs; the desk does not use them, and the user does them in the Hyperliquid app with their main wallet:

| Action | What it does |
| --- | --- |
| `usdSend`, `spotSend`, `sendAsset` | send USDC or tokens to another address or between dexs (`agentSendAsset`, the self-only variant, is L1-signed and agent-capable) |
| `withdraw3` | withdraw USDC to Arbitrum |
| `usdClassTransfer` | move USDC between perp and spot balances |
| `vaultTransfer`, `subAccountTransfer`, `createSubAccount` | move funds into or out of vaults and sub-accounts (L1-signed, so an API wallet technically can; the desk's rule is the guard) |
| `approveBuilderFee`, `builder` on orders | let a builder charge a fee on your orders |
| `cDeposit`, `cWithdraw`, `tokenDelegate` | HYPE staking (`cDeposit2`, the agent deposit variant, is L1-signed, so an API wallet technically can; the desk's rule is the guard) |
| `userSetAbstraction` and friends | change the account's margin mode (`agentSetAbstraction` is L1-signed and agent-capable; same guard) |

If a ticket asks for one of these, the Execution Trader declines and the Desk Lead explains why (`desk-operating-model`, "Excluded on purpose").

## Pitfalls

- A dead-man's switch that fires also removes stops. Re-arm protection.
- TWAP minimum size and duration errors (`Invalid TWAP duration`) come back inside `status`, not as `resting`.
- Spot size decimals come from the base token, not the pair; ids differ per network.
- Reusing an agent address after revocation can replay old signed actions; generate fresh keys.
- Two signers on one API wallet in parallel: nonce collisions and rejected actions.
