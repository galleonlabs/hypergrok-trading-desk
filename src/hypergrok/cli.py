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
from .builder import BuilderError, check_builder, inspect_builder
from .config import (
    GALLEON_BUILDER_ADDRESS,
    GALLEON_BUILDER_FEE_TENTHS_BP,
    ConfigError,
    DeskConfig,
)
from .journal import Attempt, JournalError
from .plans import ADDRESS_RE, OrderPlan, PlanError, load_plan, save_plan
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


def _doctor(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    user = _require_address(args.user, "User") if args.user is not None else None
    mids = _info(config.api_url, "allMids")
    if not isinstance(mids, dict) or not mids:
        raise ApiError("Hyperliquid allMids returned no markets")
    status = inspect_builder(user, lambda kind, **kw: _info(config.api_url, kind, **kw))
    user_supplied = user is not None
    execution_ready = user_supplied and status.eligible
    if not status.balance_eligible:
        next_action = "Fund the builder to at least 100 USDC perps account value."
    elif not status.standard_mode:
        next_action = "Put the builder account in standard account-abstraction mode."
    elif not user_supplied:
        next_action = "Run doctor --user 0x... to verify that account's builder approval."
    elif status.approval_sufficient is not True:
        next_action = "Approve the 1 bp builder fee from the user's main wallet."
    else:
        next_action = "Readiness gates pass. Create and review a short-lived order plan."
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
    status = check_builder(user, lambda kind, **kw: _info(config.api_url, kind, **kw))
    _print(asdict(status) | {"eligible": status.eligible, "builder": GALLEON_BUILDER_ADDRESS})


def _size(args: argparse.Namespace) -> None:
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
    )
    _print(asdict(result))


def _plan(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    if not 1 <= args.expires_minutes <= 30:
        raise PlanError("--expires-minutes must be between 1 and 30")
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
    if plan.notional > config.max_order_notional_usd:
        raise PlanError(
            f"Order notional {plan.notional} exceeds cap {config.max_order_notional_usd}"
        )
    digest = save_plan(plan, Path(args.out))
    _print(
        {
            "plan": str(Path(args.out)),
            "sha256": digest,
            "notional_usd": plan.notional,
            "builder_fee": "1 bp of filled notional, paid to Galleon",
            "next": f"Review, then execute with --confirm {digest} --execute",
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
    if plan.notional > config.max_order_notional_usd:
        raise PlanError("Plan exceeds the current order notional cap")
    account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").lower()
    if account_address != plan.account.lower():
        raise PlanError("HYPERLIQUID_ACCOUNT_ADDRESS does not match the approved plan account")
    check_builder(plan.account, lambda kind, **kw: _info(config.api_url, kind, **kw))
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

    exchange = Exchange(wallet, config.api_url, account_address=plan.account)
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
    try:
        response = exchange.order(
            plan.coin,
            plan.side == "buy",
            float(Decimal(plan.size)),
            float(limit_px),
            {"limit": {"tif": plan.tif}},
            reduce_only=plan.reduce_only,
            cloid=Cloid.from_str(plan.cloid),
            builder={"b": GALLEON_BUILDER_ADDRESS, "f": GALLEON_BUILDER_FEE_TENTHS_BP},
        )
    except Exception as exc:
        try:
            attempt.transition("unknown", error_type=type(exc).__name__)
        except JournalError as journal_exc:
            raise RuntimeError(
                "Submission result and journal state are unknown. Do not retry. Reconcile the cloid first."
            ) from journal_exc
        raise RuntimeError(
            "Submission result is unknown. Do not retry. Reconcile the cloid in account history first."
        ) from exc
    effect = _order_effect(response)
    attempt.transition(effect)
    _print(
        {
            "plan_sha256": digest,
            "cloid": plan.cloid,
            "network": plan.network,
            "effect": effect,
            "builder": {"b": GALLEON_BUILDER_ADDRESS, "f": GALLEON_BUILDER_FEE_TENTHS_BP},
            "response": response,
        }
    )
    if effect == "rejected":
        raise PlanError("Hyperliquid rejected the order; inspect the response and create a new plan if needed")
    if effect == "unknown":
        raise RuntimeError("Submission response is unrecognised. Do not retry. Reconcile the cloid first.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hypergrok", description="HyperGrok trading desk")
    commands = root.add_subparsers(dest="command", required=True)
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
    try:
        args = parser().parse_args(argv)
        args.func(args)
        return 0
    except (ApiError, BuilderError, ConfigError, JournalError, PlanError, RiskError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
