"""Immutable, expiring order plans."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import GALLEON_BUILDER_ADDRESS, GALLEON_BUILDER_FEE_TENTHS_BP

ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
CLOID_RE = re.compile(r"0x[0-9a-fA-F]{32}\Z")
COIN_RE = re.compile(r"[A-Z0-9@][A-Z0-9._:/-]{0,31}\Z")
MAX_PLAN_LIFETIME = 30 * 60
MAX_PLAN_BYTES = 64 * 1024


class PlanError(ValueError):
    """An order plan is invalid, stale or changed."""


@dataclass(frozen=True)
class OrderPlan:
    schema_version: int
    network: str
    account: str
    coin: str
    side: str
    size: str
    limit_px: str
    reduce_only: bool
    tif: str
    max_slippage_bps: str
    builder_address: str
    builder_fee_tenths_bp: int
    cloid: str
    created_at: str
    expires_at: str

    @property
    def notional(self) -> Decimal:
        return Decimal(self.size) * Decimal(self.limit_px)

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def validate(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self.schema_version != 1:
            raise PlanError("Unsupported plan schema")
        if self.network not in {"testnet", "mainnet"}:
            raise PlanError("Invalid network")
        if ADDRESS_RE.fullmatch(self.account) is None:
            raise PlanError("Account must be a 20-byte hexadecimal address")
        if COIN_RE.fullmatch(self.coin) is None:
            raise PlanError("Coin is not a valid Hyperliquid market identifier")
        if self.side not in {"buy", "sell"} or self.tif not in {"Gtc", "Ioc", "Alo"}:
            raise PlanError("Invalid side or time in force")
        try:
            size = Decimal(self.size)
            limit_px = Decimal(self.limit_px)
            slippage = Decimal(self.max_slippage_bps)
        except Exception as exc:
            raise PlanError("Size, price and slippage must be decimal numbers") from exc
        if not all(value.is_finite() for value in (size, limit_px, slippage)):
            raise PlanError("Size, price and slippage must be finite")
        if size <= 0 or limit_px <= 0:
            raise PlanError("Size and limit price must be positive")
        if not Decimal("0") < slippage <= Decimal("100"):
            raise PlanError("Slippage cap must be between 0 and 100 bps")
        if self.builder_address.lower() != GALLEON_BUILDER_ADDRESS.lower():
            raise PlanError("Builder attribution changed")
        if self.builder_fee_tenths_bp != GALLEON_BUILDER_FEE_TENTHS_BP:
            raise PlanError("Builder fee changed")
        try:
            created = datetime.fromisoformat(self.created_at)
            expires = datetime.fromisoformat(self.expires_at)
            if created.tzinfo is None or expires.tzinfo is None:
                raise ValueError("timestamps must include a timezone")
        except Exception as exc:
            raise PlanError("Plan timestamps must be timezone-aware ISO 8601 values") from exc
        if created > now + timedelta(seconds=30):
            raise PlanError("Plan creation time is in the future")
        if expires <= now:
            raise PlanError("Plan has expired")
        lifetime = (expires - created).total_seconds()
        if lifetime <= 0 or lifetime > MAX_PLAN_LIFETIME:
            raise PlanError("Plan lifetime must be greater than zero and no more than 30 minutes")
        if CLOID_RE.fullmatch(self.cloid) is None:
            raise PlanError("cloid must be a 128-bit hex string")


def save_plan(plan: OrderPlan, path: Path) -> str:
    plan.validate()
    document = {"plan": asdict(plan), "sha256": plan.digest()}
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PlanError(f"Plan file already exists: {path}") from exc
    except OSError as exc:
        raise PlanError(f"Could not create plan file: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2) + "\n")
    except OSError as exc:
        raise PlanError(f"Could not write plan file; inspect before retrying: {path}") from exc
    return plan.digest()


def load_plan(path: Path) -> tuple[OrderPlan, str]:
    try:
        if path.stat().st_size > MAX_PLAN_BYTES:
            raise PlanError(f"Plan exceeds {MAX_PLAN_BYTES} bytes")
    except OSError as exc:
        raise PlanError(f"Could not read plan: {path}") from exc
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        plan = OrderPlan(**document["plan"])
        recorded = str(document["sha256"])
    except Exception as exc:
        raise PlanError("Plan file is malformed") from exc
    plan.validate()
    if not hmac.compare_digest(plan.digest(), recorded):
        raise PlanError("Plan hash does not match its contents")
    return plan, recorded
