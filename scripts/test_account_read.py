#!/usr/bin/env python3
"""Equity bounds for unpriced balances. No network and no account keys."""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("account_read", os.path.join(ROOT, "scripts", "account_read.py"))
account_read = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(account_read)

ADDRESS = "0x" + "1" * 40
START = 1704067200000
LONG = {
    "coin": "ETH", "szi": "0.1", "entryPx": "3000", "positionValue": "300",
    "unrealizedPnl": "0", "marginUsed": "15",
    "leverage": {"type": "cross", "value": 5}, "liquidationPx": None,
}


def state(value, positions=()):
    return {
        "marginSummary": {"accountValue": value, "totalNtlPos": "0", "totalRawUsd": value, "totalMarginUsed": "0"},
        "withdrawable": value,
        "assetPositions": [{"type": "oneWay", "position": position} for position in positions],
        "time": START,
    }


def report(mode, dex_states, balances=(), collateral=None, pairs=()):
    """`pairs` are (token index, name, mid, mark) USDC quotes. Missing pairs stay unpriced."""
    collateral = collateral if collateral is not None else {name: 0 for name in dex_states}
    names = list(dex_states)

    def read(payload):
        kind = payload["type"]
        if kind == "perpDexs":
            return [None, *({"name": name} for name in names if name)]
        if kind == "userAbstraction":
            return mode
        if kind == "clearinghouseState":
            return dex_states[payload.get("dex", "")]
        if kind == "spotClearinghouseState":
            return {"balances": list(balances)}
        if kind == "spotMetaAndAssetCtxs":
            tokens = [{"name": "USDC", "index": 0}]
            universe, contexts = [], []
            for index, name, mid, mark in pairs:
                tokens.append({"name": name, "index": index})
                coin = f"@{index}"
                universe.append({"tokens": [index, 0], "name": coin, "index": index})
                contexts.append({"coin": coin, "midPx": mid, "markPx": mark})
            known = {token["index"] for token in tokens}
            for token in collateral.values():
                if token not in known:
                    tokens.append({"name": f"T{token}", "index": token})
            return [{"tokens": tokens, "universe": universe}, contexts]
        if kind == "meta":
            return {"collateralToken": collateral[payload["dex"]]}
        raise AssertionError(kind)

    raw = account_read.read_account(read, ADDRESS)
    return account_read.build_report(raw, ADDRESS, "mainnet", "https://api.hyperliquid.xyz")


def equity_line(built):
    text = account_read.render(built)
    return next(line for line in text.splitlines() if line.startswith("Equity"))


class AccountReadTest(unittest.TestCase):
    def test_mark_prices_collateral_when_the_book_has_no_mid(self):
        built = report("default", {"": state("200")}, collateral={"": 360}, pairs=[(360, "USDH", None, "0.999")])
        self.assertEqual(Decimal(built["dexes"][0]["account_value_usd"]), Decimal("199.8"))
        self.assertEqual(built["equity"]["bound"], "exact")
        self.assertTrue(built["equity"]["complete"])
        self.assertNotIn("at least", equity_line(built))
        self.assertNotIn("at most", equity_line(built))

    def test_positive_omission_is_a_lower_bound(self):
        built = report(
            "default",
            {"": state("1000")},
            balances=[{"coin": "JUNK", "token": 999, "total": "5", "hold": "0", "entryNtl": "1"}],
        )
        self.assertEqual(built["equity"]["bound"], "lower")
        self.assertFalse(built["equity"]["complete"])
        self.assertEqual(Decimal(built["equity"]["total_usd"]), Decimal("1000"))
        self.assertIn("at least $1,000.00", equity_line(built))

    def test_negative_omission_is_an_upper_bound(self):
        built = report("default", {"": state("-100")}, collateral={"": 999})
        self.assertEqual(built["equity"]["bound"], "upper")
        self.assertFalse(built["equity"]["complete"])
        self.assertIsNone(built["dexes"][0]["account_value_usd"])
        self.assertEqual(Decimal(built["equity"]["total_usd"]), Decimal("0"))
        line = equity_line(built)
        self.assertIn("at most $0.00", line)
        self.assertNotIn("at least", line)

    def test_negative_spot_omission_is_an_upper_bound(self):
        built = report(
            "default",
            {"": state("1000")},
            balances=[{"coin": "JUNK", "token": 999, "total": "-40", "hold": "0", "entryNtl": "0"}],
        )
        self.assertEqual(built["equity"]["bound"], "upper")
        line = equity_line(built)
        self.assertIn("at most $1,000.00", line)
        self.assertNotIn("at least", line)

    def test_mixed_omission_is_not_a_bound(self):
        built = report(
            "default",
            {"": state("1000")},
            balances=[
                {"coin": "JUNK", "token": 999, "total": "5", "hold": "0", "entryNtl": "1"},
                {"coin": "DUST", "token": 998, "total": "-3", "hold": "0", "entryNtl": "0"},
            ],
        )
        self.assertEqual(built["equity"]["bound"], "unknown")
        line = equity_line(built)
        self.assertIn("incomplete $1,000.00", line)
        self.assertNotIn("at least", line)
        self.assertNotIn("at most", line)

    def test_zero_collateral_units_need_no_price(self):
        built = report("default", {"": state("0", [LONG])}, collateral={"": 999})
        dex = built["dexes"][0]
        self.assertEqual(dex["account_value_usd"], "0")
        self.assertEqual(dex["positions"][0]["coin"], "ETH")
        self.assertEqual(built["equity"]["bound"], "exact")
        self.assertTrue(built["equity"]["complete"])
        rendered = account_read.render(built)
        self.assertNotIn("unpriced", rendered)
        self.assertIn("$0.00", equity_line(built))

    def test_disabled_mode_sums_and_shared_modes_do_not(self):
        balances = [{"coin": "USDC", "token": 0, "total": "1", "hold": "0", "entryNtl": "0"}]
        separate = report("disabled", {"": state("10")}, balances=balances)
        self.assertEqual(separate["equity"]["basis"], "separate")
        self.assertEqual(separate["equity"]["bound"], "exact")
        self.assertEqual(Decimal(separate["equity"]["total_usd"]), Decimal("11"))
        shared = report("dexAbstraction", {"": state("10")}, balances=balances)
        self.assertEqual(shared["equity"]["basis"], "shared")
        self.assertIsNone(shared["equity"]["total_usd"])
        self.assertIsNone(shared["equity"]["bound"])
        self.assertIn("not summed", equity_line(shared))

    def test_malformed_perp_dexs_and_clearinghouse_raise(self):
        for payload in ([], [{"name": "xyz"}], [None, {"name": "XYZ"}], [None, {"name": "xyz"}, {"name": "xyz"}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                account_read.dex_names(payload)

        def read(payload):
            kind = payload["type"]
            if kind == "perpDexs":
                return [None]
            if kind == "userAbstraction":
                return "default"
            if kind == "clearinghouseState":
                return {"time": START}
            if kind == "spotClearinghouseState":
                return {"balances": []}
            if kind == "spotMetaAndAssetCtxs":
                return [{"tokens": [], "universe": []}, []]
            raise AssertionError(kind)

        with self.assertRaises(ValueError):
            account_read.read_account(read, ADDRESS)

    def test_json_rejects_a_missing_or_bad_address_without_a_read(self):
        with patch.object(account_read, "fetch_report", side_effect=AssertionError("unexpected read")):
            for argv in (["--json"], ["--json", "--address", "0x123"]):
                with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as exit_error:
                        account_read.main(argv)
                    self.assertEqual(exit_error.exception.code, 2)
                    self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")
                    self.assertEqual(errors.getvalue(), "")

    def test_failed_read_exits_without_a_partial_total(self):
        with patch.object(account_read, "fetch_report", side_effect=ValueError("perpDexs must be a list that starts with the main DEX")):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                code = account_read.main(["--json", "--address", ADDRESS])
        self.assertEqual(code, 1)
        body = json.loads(output.getvalue())
        self.assertEqual(body["error"]["code"], "source_unavailable")
        self.assertNotIn("total_usd", body)

    def test_schema_names_the_bound_and_does_not_read(self):
        with patch.object(account_read, "fetch_report", side_effect=AssertionError("unexpected read")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(account_read.main(["--schema"]), 0)
        described = json.loads(output.getvalue())["success"]["equity"]
        self.assertIn("lower", described)
        self.assertIn("upper", described)


if __name__ == "__main__":
    unittest.main()
