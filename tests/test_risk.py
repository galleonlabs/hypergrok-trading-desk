from decimal import Decimal

import pytest

from hypergrok.risk import RiskError, size_for_stop


def test_size_is_bounded_by_notional_cap() -> None:
    result = size_for_stop(
        equity=Decimal("10000"),
        entry=Decimal("100"),
        stop=Decimal("99"),
        risk_pct=Decimal("1"),
        max_notional=Decimal("1000"),
    )
    assert result.size == Decimal("10.00000000")
    assert result.notional == Decimal("1000.00000000")
    assert result.risk_usd == Decimal("10.00000000")


@pytest.mark.parametrize("risk", ["0", "-1", "NaN"])
def test_invalid_risk_fails_closed(risk: str) -> None:
    with pytest.raises(RiskError):
        size_for_stop(
            equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
            risk_pct=Decimal(risk), max_notional=Decimal("1000")
        )


def test_no_ceiling_is_imposed_by_default() -> None:
    """HyperGrok has no opinion on risk appetite; the risk officer does."""
    result = size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                           risk_pct=Decimal("25"), max_notional=Decimal("1000000"))
    assert result.risk_usd == Decimal("2500.00000000")


def test_an_opt_in_ceiling_is_honoured_when_set() -> None:
    with pytest.raises(RiskError, match="exceeds your configured ceiling"):
        size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                      risk_pct=Decimal("5"), max_notional=Decimal("100000"),
                      max_risk_pct=Decimal("2"))
    allowed = size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                            risk_pct=Decimal("5"), max_notional=Decimal("100000"),
                            max_risk_pct=Decimal("10"))
    assert allowed.risk_usd == Decimal("500.00000000")


def test_setting_a_ceiling_never_changes_the_arithmetic() -> None:
    tight = size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                          risk_pct=Decimal("1"), max_notional=Decimal("100000"))
    loose = size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                          risk_pct=Decimal("1"), max_notional=Decimal("100000"),
                          max_risk_pct=Decimal("50"))
    assert tight == loose


def test_a_nonsensical_ceiling_is_still_refused() -> None:
    with pytest.raises(RiskError, match="maximum risk"):
        size_for_stop(equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
                      risk_pct=Decimal("1"), max_notional=Decimal("1000"),
                      max_risk_pct=Decimal("0"))
