import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hypergrok.config import GALLEON_BUILDER_ADDRESS
from hypergrok.plans import OrderPlan, PlanError, load_plan, save_plan


def plan() -> OrderPlan:
    now = datetime.now(UTC)
    return OrderPlan(
        schema_version=1, network="testnet", account="0x" + "1" * 40,
        coin="BTC", side="buy", size="0.01", limit_px="100000",
        reduce_only=False, tif="Gtc", max_slippage_bps="30",
        builder_address=GALLEON_BUILDER_ADDRESS, builder_fee_tenths_bp=10,
        cloid="0x" + "a" * 32, created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )


def test_round_trip_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "order.plan.json"
    original = plan()
    digest = save_plan(original, path)
    loaded, recorded = load_plan(path)
    assert loaded == original
    assert recorded == digest


def test_tamper_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "order.plan.json"
    save_plan(plan(), path)
    document = json.loads(path.read_text())
    document["plan"]["size"] = "10"
    path.write_text(json.dumps(document))
    with pytest.raises(PlanError, match="hash"):
        load_plan(path)


def test_expired_and_changed_builder_are_rejected() -> None:
    with pytest.raises(PlanError, match="expired"):
        replace(plan(), expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()).validate()
    with pytest.raises(PlanError, match="Builder attribution"):
        replace(plan(), builder_address="0x" + "2" * 40).validate()
