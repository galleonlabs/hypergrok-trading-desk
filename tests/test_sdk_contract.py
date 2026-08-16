import inspect

from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid


def test_pinned_sdk_exposes_required_guarded_order_contract() -> None:
    order = inspect.signature(Exchange.order).parameters
    constructor = inspect.signature(Exchange).parameters
    assert {"cloid", "builder", "reduce_only"} <= set(order)
    assert "account_address" in constructor
    assert hasattr(Exchange, "set_expires_after")
    assert str(Cloid.from_str("0x" + "a" * 32)) == "0x" + "a" * 32
