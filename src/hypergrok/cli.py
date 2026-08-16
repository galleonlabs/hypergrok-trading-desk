"""HyperGrok command line interface."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .api import ApiError, coingecko_coin, defillama_protocol, request_json
from .builder import BuilderError, inspect_builder, resolve_attribution
from .config import (
    GALLEON_BUILDER_ADDRESS,
    GALLEON_BUILDER_FEE_TENTHS_BP,
    HYPERLIQUID_MIN_ORDER_VALUE_USD,
    ConfigError,
    DeskConfig,
)
from .env import find_dotenv, load_dotenv
from .journal import Attempt, JournalError
from .plans import ADDRESS_RE, CLOID_RE, OrderPlan, PlanError, load_plan, save_plan
from .risk import RiskError, size_for_stop


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, sort_keys=True))


def _info(base_url: str, kind: str, **params: Any) -> Any:
    return request_json(f"{base_url}/info", method="POST", payload={"type": kind, **params})


def _require_address(value: str, label: str = "Address") -> str:
    if ADDRESS_RE.fullmatch(value) is None:
        raise PlanError(f"{label} must be a 20-byte hexadecimal address")
    return value.lower()


def _market(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    meta, contexts = _info(config.api_url, "metaAndAssetCtxs")
    for asset, context in zip(meta["universe"], contexts, strict=True):
        if asset["name"].upper() == args.coin.upper():
            _print(
                {
                    "asset": asset,
                    "context": context,
                    "network": config.network,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "source": f"{config.api_url}/info",
                }
            )
            return
    raise ApiError(f"Unknown perp market: {args.coin}")


def _account(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    address = _require_address(args.address)
    state = _info(config.api_url, "clearinghouseState", user=address)
    orders = _info(config.api_url, "openOrders", user=address)
    _print(
        {
            "state": state,
            "open_orders": orders,
            "network": config.network,
            "observed_at": datetime.now(UTC).isoformat(),
            "source": f"{config.api_url}/info",
        }
    )


def _limits(args: argparse.Namespace) -> None:
    """Report the constraints Hyperliquid itself enforces for one market.

    These are the real limits a risk officer should reason from. HyperGrok adds
    no ceiling of its own unless the user opts into one.
    """
    config = DeskConfig.from_env()
    meta = _info(config.api_url, "meta")
    universe = meta.get("universe") if isinstance(meta, dict) else None
    asset = next(
        (
            row
            for row in universe or []
            if isinstance(row, dict) and str(row.get("name", "")).upper() == args.coin.upper()
        ),
        None,
    )
    if asset is None:
        raise ApiError(f"Unknown perp market: {args.coin}")

    tiers: list[dict[str, Any]] = []
    for entry in meta.get("marginTables") or []:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == asset.get("marginTableId"):
            table = entry[1] if isinstance(entry[1], dict) else {}
            for tier in table.get("marginTiers") or []:
                tiers.append(
                    {
                        "lower_bound_usd": tier.get("lowerBound"),
                        "max_leverage": tier.get("maxLeverage"),
                    }
                )

    max_leverage = asset.get("maxLeverage")
    report: dict[str, Any] = {
        "coin": asset.get("name"),
        "network": config.network,
        "observed_at": datetime.now(UTC).isoformat(),
        "source": f"{config.api_url}/info",
        "exchange_limits": {
            "max_leverage": max_leverage,
            "size_decimals": asset.get("szDecimals"),
            "min_order_value_usd": str(HYPERLIQUID_MIN_ORDER_VALUE_USD),
            "margin_tiers": tiers,
            "note": (
                "Leverage above a tier's lower bound is capped at that tier's "
                "maximum. Liquidation is governed by Hyperliquid's margin engine."
            ),
        },
        "hypergrok_ceilings": {
            "max_order_notional_usd": config.max_order_notional_usd,
            "max_risk_pct": config.max_risk_pct,
            "note": (
                "null means HyperGrok imposes no ceiling. These are opt-in "
                "guardrails, not house rules; sizing judgment belongs to the "
                "risk officer working from the exchange limits above."
            ),
        },
    }
    if args.equity is not None:
        try:
            equity = Decimal(args.equity)
        except (InvalidOperation, ValueError) as exc:
            raise RiskError("--equity must be a decimal number") from exc
        if equity <= 0:
            raise RiskError("--equity must be positive")
        if isinstance(max_leverage, int):
            report["for_your_equity"] = {
                "equity_usd": str(equity),
                "max_position_notional_usd": str(equity * Decimal(max_leverage)),
                "min_order_value_usd": str(HYPERLIQUID_MIN_ORDER_VALUE_USD),
            }
    _print(report)


def _order_status(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    account = _require_address(args.account, "Account")
    cloid = args.cloid.lower()
    if CLOID_RE.fullmatch(cloid) is None:
        raise PlanError("cloid must be a 128-bit hexadecimal string")
    status = _info(config.api_url, "orderStatus", user=account, oid=cloid)
    _print(
        {
            "account": account,
            "cloid": cloid,
            "network": config.network,
            "observed_at": datetime.now(UTC).isoformat(),
            "source": f"{config.api_url}/info",
            "status": status,
        }
    )


def _doctor(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    user = _require_address(args.user, "User") if args.user is not None else None
    mids = _info(config.api_url, "allMids")
    if not isinstance(mids, dict) or not mids:
        raise ApiError("Hyperliquid allMids returned no markets")
    status = inspect_builder(user, lambda kind, **kw: _info(config.api_url, kind, **kw))
    user_supplied = user is not None
    attribution = (
        resolve_attribution(
            config.network, user, lambda kind, **kw: _info(config.api_url, kind, **kw)
        )
        if user is not None
        else None
    )
    account_set = bool(os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS"))
    key_set = bool(os.getenv("HYPERLIQUID_PRIVATE_KEY"))
    # Execution readiness is about the user's own setup. Builder attribution is
    # Galleon's revenue concern and never gates whether an order can be sent.
    execution_ready = user_supplied and account_set and key_set
    if not user_supplied:
        next_action = "Run: hypergrok doctor --user 0xYourTradingAccount"
    elif not account_set:
        next_action = "Set HYPERLIQUID_ACCOUNT_ADDRESS to your trading account (see .env.example)."
    elif not key_set:
        next_action = "Set HYPERLIQUID_PRIVATE_KEY to a scoped Hyperliquid API wallet key, never your seed phrase."
    else:
        next_action = "Setup looks complete. Run: hypergrok quickstart"
    _print(
        {
            "status": "execution-ready" if execution_ready else "read-only-ready",
            "network": config.network,
            "endpoint": config.api_url,
            "observed_at": datetime.now(UTC).isoformat(),
            "markets_seen": len(mids),
            "mainnet_enabled": config.mainnet_enabled,
            "builder": {
                "address": GALLEON_BUILDER_ADDRESS,
                "fee_tenths_bp": GALLEON_BUILDER_FEE_TENTHS_BP,
                "fee_bp": "1",
                "perps_account_value_usdc": status.account_value,
                "eligible_by_balance": status.balance_eligible,
                "account_abstraction": status.abstraction,
                "standard_mode": status.standard_mode,
                "user": user,
                "user_max_fee_tenths_bp": status.max_fee_tenths_bp,
                "user_approval_sufficient": status.approval_sufficient,
                "attribution_active": attribution.active if attribution else None,
                "attribution_reason": attribution.reason if attribution else None,
                "note": (
                    "Builder attribution is how HyperGrok is funded. It never gates "
                    "your ability to trade: when it is inactive your order is sent "
                    "without the 1 bp fee. Nothing here asks you to send funds anywhere."
                ),
            },
            "execution_ready": execution_ready,
            "next_action": next_action,
        }
    )


def _health(args: argparse.Namespace) -> None:
    _doctor(argparse.Namespace(user=None))


def _builder_status(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    user = _require_address(args.user, "User")
    info = lambda kind, **kw: _info(config.api_url, kind, **kw)  # noqa: E731
    status = inspect_builder(user, info)
    attribution = resolve_attribution(config.network, user, info)
    _print(
        asdict(status)
        | {
            "eligible": status.eligible,
            "builder": GALLEON_BUILDER_ADDRESS,
            "attribution_active": attribution.active,
            "attribution_reason": attribution.reason,
        }
    )


def _size(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    try:
        equity = Decimal(args.equity)
        entry = Decimal(args.entry)
        stop = Decimal(args.stop)
        risk_pct = Decimal(args.risk_pct)
        max_notional = Decimal(args.max_notional)
    except (InvalidOperation, ValueError) as exc:
        raise RiskError("Sizing inputs must be decimal numbers") from exc
    result = size_for_stop(
        equity=equity,
        entry=entry,
        stop=stop,
        risk_pct=risk_pct,
        max_notional=max_notional,
        max_risk_pct=config.max_risk_pct,
    )
    _print(asdict(result) | {"max_risk_pct": config.max_risk_pct})


def _plan(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    if not 1 <= args.expires_minutes <= config.max_plan_minutes:
        raise PlanError(
            f"--expires-minutes must be between 1 and {config.max_plan_minutes}. "
            "Raise HYPERGROK_MAX_PLAN_MINUTES to allow a longer plan."
        )
    try:
        size = Decimal(args.size)
        limit_px = Decimal(args.limit_px)
    except (InvalidOperation, ValueError) as exc:
        raise PlanError("--size and --limit-px must be decimal numbers") from exc
    now = datetime.now(UTC)
    plan = OrderPlan(
        schema_version=1,
        network=config.network,
        account=args.account.lower(),
        coin=args.coin.upper(),
        side=args.side,
        size=str(size),
        limit_px=str(limit_px),
        reduce_only=args.reduce_only,
        tif=args.tif,
        max_slippage_bps=str(config.max_slippage_bps),
        builder_address=GALLEON_BUILDER_ADDRESS,
        builder_fee_tenths_bp=GALLEON_BUILDER_FEE_TENTHS_BP,
        cloid="0x" + secrets.token_hex(16),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=args.expires_minutes)).isoformat(),
    )
    if config.max_order_notional_usd is not None and plan.notional > config.max_order_notional_usd:
        raise PlanError(
            f"Order notional {plan.notional} exceeds your configured ceiling "
            f"{config.max_order_notional_usd}. Raise or unset HYPERGROK_MAX_ORDER_NOTIONAL_USD."
        )
    if plan.notional < HYPERLIQUID_MIN_ORDER_VALUE_USD:
        raise PlanError(
            f"Order notional {plan.notional} is below Hyperliquid's "
            f"{HYPERLIQUID_MIN_ORDER_VALUE_USD} USD minimum order value"
        )
    digest = save_plan(plan, Path(args.out))
    out_path = Path(args.out)
    _print(
        {
            "plan": str(out_path),
            "sha256": digest,
            "notional_usd": plan.notional,
            "expires_at": plan.expires_at,
            "review_first": (
                f"Open {out_path} and check account, network, side, size, limit price and expiry. "
                "The hash below is what you are approving; it only matches this exact file."
            ),
            "next_command": (
                f"hypergrok execute-order --plan {out_path} --confirm {digest} --execute"
            ),
        }
    )


def _seen_cloid(base_url: str, user: str, cloid: str) -> bool:
    for kind in ("openOrders", "historicalOrders", "userFills"):
        rows = _info(base_url, kind, user=user)
        for row in rows:
            observed = row.get("cloid") or row.get("order", {}).get("cloid")
            if str(observed or "").lower() == cloid.lower():
                return True
    return False


def _validate_asset_precision(base_url: str, plan: OrderPlan) -> None:
    meta = _info(base_url, "meta")
    universe = meta.get("universe") if isinstance(meta, dict) else None
    asset = next(
        (row for row in universe or [] if isinstance(row, dict) and row.get("name") == plan.coin),
        None,
    )
    if asset is None:
        raise PlanError(f"Could not find {plan.coin} in live Hyperliquid metadata")
    try:
        size_decimals = int(asset["szDecimals"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError("Live asset metadata is missing szDecimals") from exc
    size = Decimal(plan.size).normalize()
    price = Decimal(plan.limit_px).normalize()
    size_places = max(0, -int(size.as_tuple().exponent))
    price_places = max(0, -int(price.as_tuple().exponent))
    price_significant = len(price.as_tuple().digits)
    if size_places > size_decimals:
        raise PlanError(f"Order size has {size_places} decimals; {plan.coin} permits {size_decimals}")
    if price != price.to_integral() and (price_places > 6 - size_decimals or price_significant > 5):
        raise PlanError("Limit price violates Hyperliquid tick-size rules")


def _order_effect(response: Any) -> str:
    if not isinstance(response, dict) or response.get("status") != "ok":
        return "rejected" if isinstance(response, dict) and response.get("status") == "err" else "unknown"
    payload = response.get("response")
    if not isinstance(payload, dict) or payload.get("type") != "order":
        return "unknown"
    data = payload.get("data")
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict):
        return "unknown"
    status = statuses[0]
    if "error" in status:
        return "rejected"
    if "resting" in status or "filled" in status:
        return "accepted"
    return "unknown"


def _execute(args: argparse.Namespace) -> None:
    if not args.execute:
        raise PlanError("Execution requires the literal --execute flag")
    config = DeskConfig.from_env()
    plan, digest = load_plan(Path(args.plan))
    if not secrets.compare_digest(digest, args.confirm):
        raise PlanError("Confirmation does not exactly match the plan hash")
    if plan.network != config.network:
        raise PlanError("Plan network differs from current configuration")
    if config.max_order_notional_usd is not None and plan.notional > config.max_order_notional_usd:
        raise PlanError("Plan exceeds the current order notional cap")
    if Decimal(plan.max_slippage_bps) > config.max_slippage_bps:
        raise PlanError("Plan slippage cap exceeds the current configured cap")
    account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").lower()
    if account_address != plan.account.lower():
        raise PlanError("HYPERLIQUID_ACCOUNT_ADDRESS does not match the approved plan account")
    attribution = resolve_attribution(
        plan.network, plan.account, lambda kind, **kw: _info(config.api_url, kind, **kw)
    )
    mids = _info(config.api_url, "allMids")
    try:
        mid = Decimal(str(mids[plan.coin]))
        if not mid.is_finite() or mid <= 0:
            raise ValueError("invalid mid")
    except Exception as exc:
        raise PlanError("Could not revalidate the live market price") from exc
    limit_px = Decimal(plan.limit_px)
    drift_bps = abs(limit_px - mid) / mid * Decimal("10000")
    if drift_bps > Decimal(plan.max_slippage_bps):
        raise PlanError(f"Live price drift is {drift_bps:.2f} bps; plan cap is {plan.max_slippage_bps}")
    _validate_asset_precision(config.api_url, plan)

    key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
    if not key:
        raise ConfigError("HYPERLIQUID_PRIVATE_KEY is required only at execution time")
    try:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.types import Cloid
    except ImportError as exc:
        raise ConfigError("Install the project dependencies before execution") from exc

    try:
        wallet = Account.from_key(key)
    except Exception as exc:
        raise ConfigError("HYPERLIQUID_PRIVATE_KEY is not a valid signing key") from exc
    role = _info(config.api_url, "userRole", user=wallet.address)
    role_user = role.get("data", {}).get("user") if isinstance(role, dict) else None
    if not isinstance(role, dict) or role.get("role") != "agent" or str(role_user).lower() != plan.account.lower():
        raise PlanError("Signing wallet is not an authorised API wallet for the approved plan account")

    exchange = Exchange(
        wallet,
        config.api_url,
        account_address=plan.account,
        timeout=float(config.http_timeout_seconds),
    )
    exchange.set_expires_after(int(datetime.fromisoformat(plan.expires_at).timestamp() * 1000))
    attempt = Attempt.acquire(
        config.state_dir,
        digest=digest,
        cloid=plan.cloid,
        network=plan.network,
        account=plan.account,
    )
    if _seen_cloid(config.api_url, plan.account, plan.cloid):
        raise PlanError("This cloid already exists; refusing duplicate submission")
    attempt.transition("sending")
    order_kwargs: dict[str, Any] = {
        "reduce_only": plan.reduce_only,
        "cloid": Cloid.from_str(plan.cloid),
    }
    if attribution.payload is not None:
        order_kwargs["builder"] = attribution.payload
    try:
        response = exchange.order(
            plan.coin,
            plan.side == "buy",
            float(Decimal(plan.size)),
            float(limit_px),
            {"limit": {"tif": plan.tif}},
            **order_kwargs,
        )
    except Exception as exc:
        try:
            attempt.transition("unknown", error_type=type(exc).__name__)
        except JournalError as journal_exc:
            raise RuntimeError(
                "Submission result and journal state are unknown. Do not retry. Reconcile the cloid first."
            ) from journal_exc
        raise RuntimeError(
            "Submission result is unknown. Do not retry. Run hypergrok order-status for the cloid first."
        ) from exc
    effect = _order_effect(response)
    attempt.transition(effect)
    _print(
        {
            "plan_sha256": digest,
            "cloid": plan.cloid,
            "network": plan.network,
            "effect": effect,
            "builder": attribution.payload,
            "builder_attribution": attribution.reason,
            "response": response,
        }
    )
    if effect == "rejected":
        raise PlanError("Hyperliquid rejected the order; inspect the response and create a new plan if needed")
    if effect == "unknown":
        raise RuntimeError(
            "Submission response is unrecognised. Do not retry. Run hypergrok order-status for the cloid first."
        )


def _quickstart(args: argparse.Namespace) -> None:
    """Plain-English readiness check. Prints what to do next, never a secret."""
    del args
    lines: list[str] = []
    todo: list[str] = []

    def check(ok: bool | None, label: str, fix: str | None = None) -> None:
        mark = {True: "[ok]", False: "[--]", None: "[??]"}[ok]
        lines.append(f"  {mark} {label}")
        if ok is not True and fix:
            todo.append(fix)

    try:
        config = DeskConfig.from_env()
    except ConfigError as exc:
        print("HyperGrok setup\n")
        print(f"  [--] Configuration is invalid: {exc}")
        print("\nFix that first, then run: hypergrok quickstart")
        return

    dotenv = find_dotenv()
    account = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")
    key_present = bool(os.getenv("HYPERLIQUID_PRIVATE_KEY"))

    print("HyperGrok setup\n")
    print(f"Network: {config.network}  ({config.api_url})")
    if config.network == "testnet":
        print("Testnet is the safe default. No real money is at risk here.\n")
    else:
        print("MAINNET. Orders here use real funds.\n")

    none = "not set (no ceiling)"
    lines.append("Optional guardrails  (unset by default -- HyperGrok imposes no risk ceiling)")
    for label, value, var in (
        ("max risk per trade",
         f"{config.max_risk_pct}%" if config.max_risk_pct is not None else none,
         "HYPERGROK_MAX_RISK_PCT"),
        ("max order notional",
         f"{config.max_order_notional_usd} USD" if config.max_order_notional_usd is not None else none,
         "HYPERGROK_MAX_ORDER_NOTIONAL_USD"),
        ("price drift tolerance", f"{config.max_slippage_bps} bps", "HYPERGROK_MAX_SLIPPAGE_BPS"),
        ("plan lifetime", f"{config.max_plan_minutes} min", "HYPERGROK_MAX_PLAN_MINUTES"),
    ):
        lines.append(f"       {label:<22} {value:<22} {var}")
    lines.append("       Real limits come from the exchange:  hypergrok limits BTC")
    lines.append("")
    lines.append("Configuration")
    check(
        dotenv is not None,
        f"Config file: {dotenv}" if dotenv else "Config file: none found",
        "Copy the template:  cp .env.example .env",
    )

    reachable: bool | None
    try:
        mids = _info(config.api_url, "allMids")
        reachable = isinstance(mids, dict) and bool(mids)
        markets = len(mids) if isinstance(mids, dict) else 0
    except Exception:  # noqa: BLE001 - a readiness report should not crash
        reachable, markets = False, 0
    check(
        reachable,
        f"Hyperliquid reachable ({markets} markets)" if reachable else "Hyperliquid unreachable",
        "Check your internet connection, then re-run.",
    )

    lines.append("")
    lines.append("Trading account (only needed to place orders)")
    valid_account = bool(account) and ADDRESS_RE.fullmatch(account) is not None
    check(
        valid_account if account else False,
        f"HYPERLIQUID_ACCOUNT_ADDRESS = {account}" if account else "HYPERLIQUID_ACCOUNT_ADDRESS not set",
        "Set HYPERLIQUID_ACCOUNT_ADDRESS in .env to your Hyperliquid trading account.",
    )
    check(
        key_present,
        "HYPERLIQUID_PRIVATE_KEY is set" if key_present else "HYPERLIQUID_PRIVATE_KEY not set",
        "Create an API wallet at app.hyperliquid.xyz (Settings -> API), then put its key in .env. "
        "Never use your seed phrase or main wallet key.",
    )

    wallet_ok: bool | None = None
    if key_present and valid_account:
        try:
            from eth_account import Account

            address = Account.from_key(os.environ["HYPERLIQUID_PRIVATE_KEY"]).address
            role = _info(config.api_url, "userRole", user=address)
            role_user = role.get("data", {}).get("user") if isinstance(role, dict) else None
            wallet_ok = (
                isinstance(role, dict)
                and role.get("role") == "agent"
                and str(role_user).lower() == account.lower()
            )
        except Exception:  # noqa: BLE001
            wallet_ok = None
        check(
            wallet_ok,
            "API wallet is authorised for that account"
            if wallet_ok
            else "API wallet does not match that account",
            "Approve the API wallet for this trading account at app.hyperliquid.xyz.",
        )

    print("\n".join(lines))

    ready = bool(reachable)
    can_trade = ready and valid_account and key_present and wallet_ok is True
    print()
    if can_trade:
        print("You can research, plan and place orders.")
    elif ready:
        print("You can research and plan orders. Placing them needs the account steps above.")
    else:
        print("Not ready yet.")

    if todo:
        print("\nNext steps:")
        for index, item in enumerate(todo, 1):
            print(f"  {index}. {item}")
    else:
        print("\nTry it:")
        print("  hypergrok market BTC")
        print("  hypergrok size --equity 1000 --entry 100 --stop 95 --risk-pct 0.5 --max-notional 200")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hypergrok", description="HyperGrok trading desk")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "quickstart", help="Plain-English setup check and what to do next. Start here."
    ).set_defaults(func=_quickstart)
    commands.add_parser("health", help="Validate config and live builder balance").set_defaults(func=_health)

    doctor = commands.add_parser("doctor", help="Report read and execution readiness")
    doctor.add_argument("--user", help="Trading account whose builder approval should be checked")
    doctor.set_defaults(func=_doctor)

    market = commands.add_parser("market", help="Read a Hyperliquid perp market")
    market.add_argument("coin")
    market.set_defaults(func=_market)

    account = commands.add_parser("account", help="Read account positions and orders")
    account.add_argument("address")
    account.set_defaults(func=_account)

    order_status = commands.add_parser(
        "order-status", help="Reconcile one order by client order ID without signing"
    )
    order_status.add_argument("--account", required=True)
    order_status.add_argument("--cloid", required=True)
    order_status.set_defaults(func=_order_status)

    llama = commands.add_parser("defillama", help="Read a DefiLlama protocol")
    llama.add_argument("slug")
    llama.set_defaults(func=lambda args: _print(defillama_protocol(args.slug)))

    gecko = commands.add_parser("coingecko", help="Read a CoinGecko coin")
    gecko.add_argument("coin_id")
    gecko.set_defaults(
        func=lambda args: _print(
            coingecko_coin(
                args.coin_id,
                os.getenv("COINGECKO_API_KEY"),
                os.getenv("COINGECKO_TIER", "keyless").lower() == "pro",
            )
        )
    )

    builder = commands.add_parser("builder-status", help="Verify builder eligibility and approval")
    builder.add_argument("user")
    builder.set_defaults(func=_builder_status)

    limits = commands.add_parser(
        "limits", help="Report the limits Hyperliquid itself enforces for a market"
    )
    limits.add_argument("coin")
    limits.add_argument("--equity", help="Optional: show max position notional for this equity")
    limits.set_defaults(func=_limits)

    size = commands.add_parser("size", help="Size a position from a stop")
    for name in ("equity", "entry", "stop", "risk-pct", "max-notional"):
        size.add_argument("--" + name, required=True)
    size.set_defaults(func=_size)

    plan = commands.add_parser("plan-order", help="Create a hashed, expiring order plan")
    plan.add_argument("--account", required=True)
    plan.add_argument("--coin", required=True)
    plan.add_argument("--side", choices=("buy", "sell"), required=True)
    plan.add_argument("--size", required=True)
    plan.add_argument("--limit-px", required=True)
    plan.add_argument("--tif", choices=("Gtc", "Ioc", "Alo"), default="Gtc")
    plan.add_argument("--reduce-only", action="store_true")
    plan.add_argument("--expires-minutes", type=int, default=5)
    plan.add_argument("--out", required=True)
    plan.set_defaults(func=_plan)

    execute = commands.add_parser("execute-order", help="Execute an approved plan once")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--confirm", required=True)
    execute.add_argument("--execute", action="store_true")
    execute.set_defaults(func=_execute)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    try:
        args = parser().parse_args(argv)
        args.func(args)
        return 0
    except (ApiError, BuilderError, ConfigError, JournalError, PlanError, RiskError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
