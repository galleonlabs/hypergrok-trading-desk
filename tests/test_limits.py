import argparse
import json

import pytest

from hypergrok import cli

META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 51},
        {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10, "marginTableId": 52},
    ],
    "marginTables": [
        [51, {"description": "tiered", "marginTiers": [
            {"lowerBound": "0.0", "maxLeverage": 40},
            {"lowerBound": "10000.0", "maxLeverage": 25},
        ]}],
        [52, {"description": "", "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 10}]}],
    ],
}


@pytest.fixture(autouse=True)
def meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: META)


def report(capsys, **kwargs):
    cli._limits(argparse.Namespace(coin=kwargs.pop("coin", "BTC"),
                                   equity=kwargs.pop("equity", None)))
    return json.loads(capsys.readouterr().out)


def test_reports_the_exchange_limits(capsys: pytest.CaptureFixture[str]) -> None:
    data = report(capsys)
    limits = data["exchange_limits"]
    assert limits["max_leverage"] == 40
    assert limits["size_decimals"] == 5
    assert limits["min_order_value_usd"] == "10"
    assert data["coin"] == "BTC"


def test_margin_tiers_are_resolved_for_the_right_table(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """Headline leverage is the top tier only; the tiers are the real constraint."""
    tiers = report(capsys)["exchange_limits"]["margin_tiers"]
    assert tiers == [
        {"lower_bound_usd": "0.0", "max_leverage": 40},
        {"lower_bound_usd": "10000.0", "max_leverage": 25},
    ]
    hype = report(capsys, coin="HYPE")["exchange_limits"]["margin_tiers"]
    assert hype == [{"lower_bound_usd": "0.0", "max_leverage": 10}]


def test_reports_that_hypergrok_imposes_no_ceiling_by_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HYPERGROK_MAX_RISK_PCT", "HYPERGROK_MAX_ORDER_NOTIONAL_USD"):
        monkeypatch.delenv(name, raising=False)
    ceilings = report(capsys)["hypergrok_ceilings"]
    assert ceilings["max_risk_pct"] is None
    assert ceilings["max_order_notional_usd"] is None


def test_reports_an_opt_in_ceiling_when_one_is_set(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPERGROK_MAX_RISK_PCT", "3")
    assert report(capsys)["hypergrok_ceilings"]["max_risk_pct"] == "3"


def test_equity_yields_max_position_notional(capsys: pytest.CaptureFixture[str]) -> None:
    scoped = report(capsys, equity="10000")["for_your_equity"]
    assert scoped["max_position_notional_usd"] == "400000"
    assert scoped["equity_usd"] == "10000"


def test_equity_is_optional(capsys: pytest.CaptureFixture[str]) -> None:
    assert "for_your_equity" not in report(capsys)


@pytest.mark.parametrize("equity", ["0", "-5", "abc"])
def test_bad_equity_is_refused(equity: str) -> None:
    with pytest.raises(cli.RiskError):
        cli._limits(argparse.Namespace(coin="BTC", equity=equity))


def test_unknown_market_is_refused() -> None:
    with pytest.raises(cli.ApiError, match="Unknown perp market"):
        cli._limits(argparse.Namespace(coin="NOTACOIN", equity=None))


def test_coin_lookup_is_case_insensitive(capsys: pytest.CaptureFixture[str]) -> None:
    assert report(capsys, coin="btc")["coin"] == "BTC"
