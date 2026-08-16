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
from decimal import Decimal
from pathlib import Path
from typing import Any

from .api import ApiError, coingecko_coin, defillama_protocol, request_json
from .builder import BuilderError, check_builder
from .config import (
    GALLEON_BUILDER_ADDRESS,
    GALLEON_BUILDER_FEE_TENTHS_BP,
    ConfigError,
    DeskConfig,
)
from .plans import OrderPlan, PlanError, load_plan, save_plan
from .risk import RiskError, size_for_stop


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, sort_keys=True))


def _info(base_url: str, kind: str, **params: Any) -> Any:
    return request_json(f"{base_url}/info", method="POST", payload={"type": kind, **params})


def _market(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    meta, contexts = _info(config.api_url, "metaAndAssetCtxs")
    for asset, context in zip(meta["universe"], contexts, strict=True):
        if asset["name"].upper() == args.coin.upper():
            _print({"asset": asset, "context": context, "source": f"{config.api_url}/info"})
            return
    raise ApiError(f"Unknown perp market: {args.coin}")


def _account(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    state = _info(config.api_url, "clearinghouseState", user=args.address)
    orders = _info(config.api_url, "openOrders", user=args.address)
    _print({"state": state, "open_orders": orders, "source": f"{config.api_url}/info"})


def _health(args: argparse.Namespace) -> None:
    del args
    config = DeskConfig.from_env()
    builder_state = _info(config.api_url, "clearinghouseState", user=GALLEON_BUILDER_ADDRESS)
    value = builder_state.get("marginSummary", {}).get("accountValue")
    abstraction = _info(config.api_url, "userAbstraction", user=GALLEON_BUILDER_ADDRESS)
    _print(
        {
            "status": "ok",
            "network": config.network,
            "mainnet_enabled": config.mainnet_enabled,
            "builder": {
                "address": GALLEON_BUILDER_ADDRESS,
                "fee_tenths_bp": GALLEON_BUILDER_FEE_TENTHS_BP,
                "fee_bp": "1",
                "perps_account_value_usdc": value,
                "eligible_by_balance": Decimal(str(value or "0")) >= Decimal("100"),
                "account_abstraction": abstraction,
                "standard_mode": abstraction == "disabled",
            },
            "execution": "two-phase; disabled unless every live gate passes",
        }
    )


def _builder_status(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    status = check_builder(args.user, lambda kind, **kw: _info(config.api_url, kind, **kw))
    _print(asdict(status) | {"eligible": status.eligible, "builder": GALLEON_BUILDER_ADDRESS})


def _size(args: argparse.Namespace) -> None:
    result = size_for_stop(
        equity=Decimal(args.equity),
        entry=Decimal(args.entry),
        stop=Decimal(args.stop),
        risk_pct=Decimal(args.risk_pct),
        max_notional=Decimal(args.max_notional),
    )
    _print(asdict(result))


def _plan(args: argparse.Namespace) -> None:
    config = DeskConfig.from_env()
    now = datetime.now(UTC)
    plan = OrderPlan(
        schema_version=1,
        network=config.network,
        account=args.account.lower(),
        coin=args.coin.upper(),
        side=args.side,
        size=str(Decimal(args.size)),
        limit_px=str(Decimal(args.limit_px)),
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
            "builder_fee": "1 bp, paid to Galleon on fills",
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
    key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
    if not key:
        raise ConfigError("HYPERLIQUID_PRIVATE_KEY is required only at execution time")

    try:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.types import Cloid
    except ImportError as exc:
        raise ConfigError("Install the project dependencies before execution") from exc

    wallet = Account.from_key(key)
    if wallet.address.lower() != plan.account.lower():
        raise PlanError("Signing wallet does not match the approved plan account")
    check_builder(plan.account, lambda kind, **kw: _info(config.api_url, kind, **kw))
    if _seen_cloid(config.api_url, plan.account, plan.cloid):
        raise PlanError("This cloid already exists; refusing duplicate submission")

    mids = _info(config.api_url, "allMids")
    try:
        mid = Decimal(str(mids[plan.coin]))
    except Exception as exc:
        raise PlanError("Could not revalidate the live market price") from exc
    limit_px = Decimal(plan.limit_px)
    drift_bps = abs(limit_px - mid) / mid * Decimal("10000")
    if drift_bps > Decimal(plan.max_slippage_bps):
        raise PlanError(f"Live price drift is {drift_bps:.2f} bps; plan cap is {plan.max_slippage_bps}")

    exchange = Exchange(wallet, config.api_url)
    exchange.set_expires_after(int(datetime.fromisoformat(plan.expires_at).timestamp() * 1000))
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
        raise RuntimeError(
            "Submission result is unknown. Do not retry. Reconcile the cloid in account history first."
        ) from exc
    _print(
        {
            "plan_sha256": digest,
            "cloid": plan.cloid,
            "builder": {"b": GALLEON_BUILDER_ADDRESS, "f": GALLEON_BUILDER_FEE_TENTHS_BP},
            "response": response,
        }
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hypergrok", description="HyperGrok trading desk")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Validate config and live builder balance").set_defaults(func=_health)

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
    except (ApiError, BuilderError, ConfigError, PlanError, RiskError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
