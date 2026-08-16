from decimal import Decimal

import pytest

from hypergrok.builder import BuilderError, check_builder


def info_factory(value: str, fee: int, abstraction: str = "disabled"):
    def info(kind: str, **kwargs):
        del kwargs
        if kind == "clearinghouseState":
            return {"marginSummary": {"accountValue": value}}
        if kind == "userAbstraction":
            return abstraction
        return fee
    return info


def test_builder_must_hold_100_usdc() -> None:
    with pytest.raises(BuilderError, match="ineligible"):
        check_builder("0x" + "1" * 40, info_factory("99.99", 10))


def test_user_must_approve_one_bp() -> None:
    with pytest.raises(BuilderError, match="not approved"):
        check_builder("0x" + "1" * 40, info_factory("100", 9))


def test_eligible_builder_and_approval() -> None:
    status = check_builder("0x" + "1" * 40, info_factory("100", 10))
    assert status.eligible
    assert status.account_value == Decimal("100")


def test_builder_must_use_standard_mode() -> None:
    with pytest.raises(BuilderError, match="not in standard mode"):
        check_builder("0x" + "1" * 40, info_factory("100", 10, "unifiedAccount"))
