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


@pytest.mark.parametrize("risk", ["0", "2.01", "NaN"])
def test_invalid_risk_fails_closed(risk: str) -> None:
    with pytest.raises(RiskError):
        size_for_stop(
            equity=Decimal("10000"), entry=Decimal("100"), stop=Decimal("95"),
            risk_pct=Decimal(risk), max_notional=Decimal("1000")
        )
