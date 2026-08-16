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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("account", "0x" + "z" * 40, "hexadecimal"),
        ("cloid", "0x" + "z" * 32, "128-bit"),
        ("size", "NaN", "finite"),
        ("limit_px", "Infinity", "finite"),
        ("max_slippage_bps", "1001", "Slippage"),
        ("coin", "BTC BUY", "market identifier"),
    ],
)
def test_malformed_plan_fields_are_rejected(field: str, value: str, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        replace(plan(), **{field: value}).validate()


def test_plan_lifetime_is_bounded_by_an_outer_sanity_limit() -> None:
    """plans.py holds only the outer bound. The user's own, tighter lifetime is
    policy and lives in DeskConfig -- see test_config.py."""
    now = datetime.now(UTC)
    with pytest.raises(PlanError, match="hours"):
        replace(
            plan(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=25)).isoformat(),
        ).validate(now=now)


def test_a_plan_inside_the_sanity_bound_is_accepted() -> None:
    now = datetime.now(UTC)
    replace(
        plan(),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=31)).isoformat(),
    ).validate(now=now)


def test_save_refuses_to_overwrite_existing_plan(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    save_plan(plan(), path)
    with pytest.raises(PlanError, match="already exists"):
        save_plan(plan(), path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_save_reports_missing_parent_as_plan_error(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="Could not create"):
        save_plan(plan(), tmp_path / "missing" / "plan.json")
