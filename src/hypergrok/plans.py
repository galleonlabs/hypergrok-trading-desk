"""Immutable, expiring order plans."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import GALLEON_BUILDER_ADDRESS, GALLEON_BUILDER_FEE_TENTHS_BP


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
        if not self.account.startswith("0x") or len(self.account) != 42:
            raise PlanError("Account must be a 20-byte hexadecimal address")
        if self.side not in {"buy", "sell"} or self.tif not in {"Gtc", "Ioc", "Alo"}:
            raise PlanError("Invalid side or time in force")
        if Decimal(self.size) <= 0 or Decimal(self.limit_px) <= 0:
            raise PlanError("Size and limit price must be positive")
        if self.builder_address.lower() != GALLEON_BUILDER_ADDRESS.lower():
            raise PlanError("Builder attribution changed")
        if self.builder_fee_tenths_bp != GALLEON_BUILDER_FEE_TENTHS_BP:
            raise PlanError("Builder fee changed")
        if datetime.fromisoformat(self.expires_at) <= now:
            raise PlanError("Plan has expired")
        if not self.cloid.startswith("0x") or len(self.cloid) != 34:
            raise PlanError("cloid must be a 128-bit hex string")


def save_plan(plan: OrderPlan, path: Path) -> str:
    plan.validate()
    document = {"plan": asdict(plan), "sha256": plan.digest()}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return plan.digest()


def load_plan(path: Path) -> tuple[OrderPlan, str]:
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
