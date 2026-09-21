#!/usr/bin/env python3
"""Run the published Decimal helpers without importing a wallet or contacting a venue."""
import ast
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
text = (ROOT / 'skills/hyperliquid-orders/SKILL.md').read_text()
header = re.search(r'```python\n(.*?)\n```', text, re.S).group(1)
tree = ast.parse(header)
# Execute only the pure helpers, never the header's account or Exchange setup.
helpers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name in {'round_px', 'round_sz', 'bounded_px'}], type_ignores=[])
namespace = dict(Decimal=Decimal, ROUND_DOWN=ROUND_DOWN, ROUND_UP=ROUND_UP,
                 ROUND_HALF_UP=ROUND_HALF_UP, SZ_DECIMALS={'ETH': 4, 'BTC': 5, 'TINY': 0})
exec(compile(helpers, '<published order helpers>', 'exec'), namespace)


class OrderRoundingTest(unittest.TestCase):
    def test_buy_ceiling_and_sell_floor(self):
        bound = namespace['bounded_px']
        self.assertEqual(bound('ETH', '3000.06', '0', True), 3000.0)
        self.assertEqual(bound('ETH', '3000.04', '0', False), 3000.1)
        for coin in ('ETH', 'BTC', 'TINY'):
            for reference in ('0.123456', '1.234567', '99.9999', '3000.06', '99999.9', '117234.5'):
                for slippage in ('0', '0.002', '0.05'):
                    for buy in (True, False):
                        actual = Decimal(str(bound(coin, reference, slippage, buy)))
                        maximum = Decimal(reference) * (1 + Decimal(slippage) if buy else 1 - Decimal(slippage))
                        self.assertTrue(actual <= maximum if buy else actual >= maximum)
                        self.assertGreater(actual, 0)
                        # Venue permits integer prices regardless of significant figures.
                        if actual != actual.to_integral_value():
                            self.assertLessEqual(len(actual.normalize().as_tuple().digits), 5)
                            self.assertGreaterEqual(actual.normalize().as_tuple().exponent,
                                                    -(6 - namespace['SZ_DECIMALS'][coin]))

    def test_whole_dollar_precision(self):
        self.assertEqual(namespace['round_px']('BTC', '117234.5', rounding=ROUND_DOWN), 117234)
        self.assertEqual(namespace['round_px']('BTC', '117234.5', rounding=ROUND_UP), 117235)

    def test_size_never_increases(self):
        self.assertEqual(namespace['round_sz']('ETH', '0.29'), 0.29)
        self.assertEqual(namespace['round_sz']('ETH', '0.12349'), 0.1234)

    def test_invalid_values_fail_before_sdk(self):
        for helper in ('round_px', 'round_sz'):
            for invalid in ('NaN', 'Infinity', '-Infinity', '0', '-1', '0.0000000001'):
                with self.subTest(helper=helper, value=invalid), self.assertRaises(ValueError):
                    namespace[helper]('ETH', invalid)
        for invalid in ('NaN', 'Infinity', '-0.1', '1', '2'):
            with self.subTest(slippage=invalid), self.assertRaises(ValueError):
                namespace['bounded_px']('ETH', '3000', invalid, True)


if __name__ == '__main__':
    unittest.main()
