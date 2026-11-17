#!/usr/bin/env python3
"""Run the published Decimal helpers without importing a wallet or contacting a venue."""
import ast
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from pathlib import Path
import random
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

advanced = (ROOT / 'skills/hyperliquid-advanced/SKILL.md').read_text()
spot_header = re.search(r'## Spot orders.*?```python\n(.*?)\n```', advanced, re.S).group(1)
spot_tree = ast.parse(spot_header)
spot_helpers = ast.Module(body=[node for node in spot_tree.body if isinstance(node, ast.FunctionDef)
                              and node.name in {'round_px_spot', 'round_sz_spot', 'bounded_px_spot'}],
                          type_ignores=[])
spot_namespace = dict(Decimal=Decimal, ROUND_DOWN=ROUND_DOWN, ROUND_UP=ROUND_UP,
                      ROUND_HALF_UP=ROUND_HALF_UP, base_dec=3)
exec(compile(spot_helpers, '<published spot helpers>', 'exec'), spot_namespace)


class OrderRoundingTest(unittest.TestCase):
    def test_published_close_keeps_ticket_bound_for_longs_and_shorts(self):
        positions = (ROOT / 'skills/hyperliquid-positions/SKILL.md').read_text()
        snippet = re.search(r'## Close a position.*?```python\n(.*?)\n```', positions, re.S).group(1)
        for signed_size, reference in (('0.1234', '3000.04'), ('-0.1234', '3000.06')):
            with self.subTest(size=signed_size):
                sent = []
                info = SimpleNamespace(
                    user_state=lambda _: {'assetPositions': [{'position': {'coin': 'ETH', 'szi': signed_size}}]},
                    all_mids=lambda: {'ETH': reference},
                )
                exchange = SimpleNamespace(order=lambda *args, **kwargs: sent.append((args, kwargs)))
                scope = dict(namespace, info=info, exchange=exchange, ACCOUNT='offline-fixture',
                             new_cloid=lambda: SimpleNamespace(to_raw=lambda: 'offline-cloid'))
                with redirect_stdout(StringIO()):
                    exec(compile(snippet, '<published close>', 'exec'), scope)
                self.assertEqual(len(sent), 1)
                (coin, buy, size, price, order_type), options = sent[0]
                self.assertEqual(coin, 'ETH')
                self.assertEqual(buy, Decimal(signed_size) < 0)
                self.assertEqual(Decimal(str(size)), abs(Decimal(signed_size)))
                self.assertEqual(order_type, {'limit': {'tif': 'Ioc'}})
                self.assertTrue(options['reduce_only'])
                bound = Decimal(reference) * (1 + Decimal('0.003') if buy else 1 - Decimal('0.003'))
                encoded_price = Decimal(format(price, '.8f'))
                self.assertTrue(encoded_price <= bound if buy else encoded_price >= bound)

    def test_published_readiness_requires_matching_role_and_unexpired_agent(self):
        setup = (ROOT / 'skills/hyperliquid-setup/SKILL.md').read_text()
        snippet = re.search(r'## 5\. Readiness check.*?```python\n(.*?)\n```', setup, re.S).group(1)
        tree = ast.parse(snippet)
        # Inject offline substitutes instead of importing an SDK or reading host secrets.
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        code = compile(tree, '<published readiness>', 'exec')
        for role, acts_for, expiry, expected in (
            ('agent', 'account', 1000001, True),
            ('agent', 'account', 999999, False),
            ('missing', 'account', 1000001, False),
            ('agent', 'different-account', 1000001, False),
            ('agent', 'account', None, True),
        ):
            with self.subTest(role=role, account=acts_for, expiry=expiry):
                agents = [] if expiry is None else [{'address': 'agent', 'validUntil': expiry, 'name': 'desk'}]
                info = SimpleNamespace(
                    user_role=lambda _: {'role': role, 'data': {'user': acts_for}},
                    extra_agents=lambda _: agents,
                    user_state=lambda _: {'marginSummary': {'accountValue': '1000'}, 'assetPositions': []},
                )
                scope = dict(
                    os=SimpleNamespace(environ={'HYPERLIQUID_NETWORK': 'testnet',
                                                'HYPERLIQUID_ACCOUNT_ADDRESS': 'account',
                                                'HYPERLIQUID_PRIVATE_KEY': 'offline-fixture'}),
                    time=SimpleNamespace(time=lambda: 1000),
                    eth_account=SimpleNamespace(Account=SimpleNamespace(from_key=lambda _: SimpleNamespace(address='agent'))),
                    Info=lambda *args, **kwargs: info,
                    constants=SimpleNamespace(MAINNET_API_URL='mainnet-fixture', TESTNET_API_URL='testnet-fixture'),
                )
                with redirect_stdout(StringIO()):
                    exec(code, scope)
                self.assertEqual(scope['approved'], expected)

    def test_typescript_trigger_bounds(self):
        # Inspect the published grouped-order arguments without running a wallet or SDK.
        ts = text.split('## TypeScript (`@nktkas/hyperliquid`)', 1)[1].split('## Raw wire format', 1)[0]
        children = re.findall(
            r'b: false, p: boundedPrice\("([0-9.]+)", "([0-9.]+)", false, szDec\).*?'
            r'triggerPx: "([0-9.]+)", tpsl: "(tp|sl)"', ts,
        )
        self.assertEqual(len(children), 2)
        self.assertEqual({kind for *_, kind in children}, {'tp', 'sl'})
        for reference, slippage, trigger, kind in children:
            with self.subTest(kind=kind):
                self.assertEqual(reference, trigger)
                floor = Decimal(trigger) * (1 - Decimal(slippage))
                encoded = Decimal(str(namespace['bounded_px']('ETH', trigger, slippage, False)))
                self.assertGreaterEqual(encoded, floor)
                self.assertLess(encoded, Decimal(trigger))
        self.assertIn('Decimal.ROUND_UP', ts)
        self.assertNotIn("SDK's documented directional rounding", ts)

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

    def test_round_number_wire_drops_trailing_zeros(self):
        """SDK 0.24.0 float_to_wire strips trailing zeros; an integer price stays an integer string."""
        def float_to_wire(value):
            rounded = f"{float(value):.8f}"
            if abs(float(rounded) - float(value)) >= 1e-12:
                raise ValueError(rounded)
            return f"{Decimal(rounded).normalize():f}"

        for coin, reference, wire in (
            ("ETH", "999.996", "1000"),
            ("TINY", "9999.96", "10000"),
            ("BTC", "99999.9", "100000"),
        ):
            with self.subTest(coin=coin, reference=reference):
                self.assertEqual(float_to_wire(namespace["bounded_px"](coin, reference, "0", False)), wire)

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

    def test_spot_decimal_input_and_integer_exception(self):
        spot_namespace['base_dec'] = 3
        price = spot_namespace['round_px_spot']
        size = spot_namespace['round_sz_spot']
        self.assertEqual(price('0.123456'), Decimal('0.12346'))
        self.assertEqual(price('117234'), Decimal('117234'))
        self.assertEqual(price('117234.5', rounding=ROUND_DOWN), Decimal('117234'))
        self.assertEqual(price('117234.5', rounding=ROUND_UP), Decimal('117235'))
        self.assertEqual(size('0.1239'), Decimal('0.123'))
        self.assertIsInstance(price('0.123456'), Decimal)
        self.assertIsInstance(size('0.1239'), Decimal)
        spot_namespace['base_dec'] = 0
        self.assertEqual(price('0.00000123456'), Decimal('0.00000123'))

    def test_spot_directional_bounds_and_venue_precision(self):
        bound = spot_namespace['bounded_px_spot']
        for base_dec in (0, 2, 5):
            spot_namespace['base_dec'] = base_dec
            for reference in ('0.123456', '1.234567', '99.9999', '3000.06', '99999.9', '117234.5'):
                for slippage in ('0', '0.002', '0.05'):
                    for buy in (True, False):
                        with self.subTest(base_dec=base_dec, reference=reference, slippage=slippage, buy=buy):
                            actual = bound(reference, slippage, buy)
                            approved = Decimal(reference) * (1 + Decimal(slippage) if buy else 1 - Decimal(slippage))
                            self.assertTrue(actual <= approved if buy else actual >= approved)
                            self.assertGreater(actual, 0)
                            # SDK 0.24.0 serializes numeric order fields at eight decimal places.
                            encoded = Decimal(format(float(actual), '.8f'))
                            self.assertEqual(encoded, actual)
                            self.assertTrue(encoded <= approved if buy else encoded >= approved)
                            if actual != actual.to_integral_value():
                                self.assertLessEqual(len(actual.normalize().as_tuple().digits), 5)
                                self.assertGreaterEqual(actual.normalize().as_tuple().exponent, -(8 - base_dec))

    def test_spot_invalid_values_fail_before_sdk(self):
        spot_namespace['base_dec'] = 3
        for helper in ('round_px_spot', 'round_sz_spot'):
            for invalid in ('NaN', 'Infinity', '-Infinity', '0', '-1', '0.0000000001'):
                with self.subTest(helper=helper, value=invalid), self.assertRaises(ValueError):
                    spot_namespace[helper](invalid)
        for invalid in ('NaN', 'Infinity', '-Infinity', '0', '-1'):
            with self.subTest(reference=invalid), self.assertRaises(ValueError):
                spot_namespace['bounded_px_spot'](invalid, '0.01', True)
        for invalid in ('NaN', 'Infinity', '-Infinity', '-0.1', '1', '2'):
            with self.subTest(slippage=invalid), self.assertRaises(ValueError):
                spot_namespace['bounded_px_spot']('0.1234', invalid, True)


# Seeded generated inputs for the properties below. A failure prints its case, and the
# fixed seed regenerates the same cases on every run.
SEED = 20261117
CASES = 1500
MODES = (ROUND_DOWN, ROUND_UP, ROUND_HALF_UP)
generated_decimals = {f'D{places}': places for places in range(7)}
generated = dict(namespace, SZ_DECIMALS=generated_decimals)
exec(compile(helpers, '<published order helpers>', 'exec'), generated)
generated_spot = dict(spot_namespace)
exec(compile(spot_helpers, '<published spot helpers>', 'exec'), generated_spot)


def random_decimal(rng, low=-6, high=6):
    """One to nine significant digits with the leading digit between 1e-6 and 1e6."""
    digits = rng.randint(1, 10 ** rng.randint(1, 9) - 1)
    return Decimal(digits).scaleb(rng.randint(low, high) - len(str(digits)) + 1)


def random_slippage(rng):
    return Decimal(0) if rng.random() < 0.2 else Decimal(rng.randint(1, 9999)) / Decimal(10000)


def price_tick(value, max_decimals):
    """The venue grid at this price: five significant figures, capped decimals, whole numbers allowed."""
    return Decimal(1).scaleb(min(0, max(value.adjusted() - 4, -max_decimals)))


def sdk_wire(value):
    """SDK 0.24.0 float_to_wire: eight decimals, refusing a value that rounding would change."""
    rounded = f'{float(value):.8f}'
    if abs(float(rounded) - float(value)) >= 1e-12:
        raise ValueError(rounded)
    return Decimal(rounded)


class GeneratedOrderRoundingTest(unittest.TestCase):
    def assert_on_grid(self, price, max_decimals, case):
        self.assertGreater(price, 0, case)
        if price != price.to_integral_value():
            shape = price.normalize().as_tuple()
            self.assertLessEqual(len(shape.digits), 5, case)
            self.assertGreaterEqual(shape.exponent, -max_decimals, case)

    def test_perp_bound_holds_stays_within_one_tick_and_survives_the_wire(self):
        rng = random.Random(SEED)
        bound_px, round_px = generated['bounded_px'], generated['round_px']
        accepted = 0
        for index in range(CASES):
            coin = rng.choice(sorted(generated_decimals))
            max_decimals = 6 - generated_decimals[coin]
            reference, slippage = random_decimal(rng), random_slippage(rng)
            for buy in (True, False):
                case = f'seed={SEED} case={index} {coin} reference={reference} slippage={slippage} buy={buy}'
                approved = reference * (1 + slippage if buy else 1 - slippage)
                tick = price_tick(approved, max_decimals)
                try:
                    sent = bound_px(coin, str(reference), str(slippage), buy)
                except ValueError:
                    # Only a buy whose ceiling sits under the first tick has no price to send.
                    self.assertTrue(buy and approved < tick, case)
                    continue
                accepted += 1
                price = Decimal(str(sent))
                self.assertTrue(price <= approved if buy else price >= approved, case)
                self.assertLess(abs(price - approved), tick, case)
                self.assert_on_grid(price, max_decimals, case)
                self.assertEqual(sdk_wire(sent), price, case)
                for mode in MODES:
                    self.assertEqual(round_px(coin, sent, rounding=mode), sent, f'{case} mode={mode}')
        self.assertGreater(accepted, CASES)

    def test_perp_nearest_price_is_within_half_a_tick(self):
        rng = random.Random(SEED + 1)
        for index in range(CASES):
            coin = rng.choice(sorted(generated_decimals))
            max_decimals = 6 - generated_decimals[coin]
            value = random_decimal(rng)
            case = f'seed={SEED + 1} case={index} {coin} price={value}'
            tick = price_tick(value, max_decimals)
            try:
                price = Decimal(str(generated['round_px'](coin, str(value))))
            except ValueError:
                self.assertLess(value, tick / 2, case)
                continue
            self.assertLessEqual(abs(price - value), tick / 2, case)
            self.assert_on_grid(price, max_decimals, case)

    def test_perp_size_rounds_down_by_less_than_one_lot(self):
        rng = random.Random(SEED + 2)
        for index in range(CASES):
            coin = rng.choice(sorted(generated_decimals))
            lot = Decimal(1).scaleb(-generated_decimals[coin])
            requested = random_decimal(rng, -7, 7)
            case = f'seed={SEED + 2} case={index} {coin} size={requested}'
            try:
                sent = generated['round_sz'](coin, str(requested))
            except ValueError:
                self.assertLess(requested, lot, case)
                continue
            size = Decimal(str(sent))
            self.assertLessEqual(size, requested, case)
            self.assertLess(requested - size, lot, case)
            self.assertEqual(size % lot, 0, case)
            self.assertEqual(generated['round_sz'](coin, sent), sent, case)

    def test_spot_bound_holds_stays_within_one_tick_and_survives_the_wire(self):
        rng = random.Random(SEED + 3)
        accepted = 0
        for index in range(CASES):
            base_dec = rng.randint(0, 8)
            generated_spot['base_dec'] = base_dec
            max_decimals = 8 - base_dec
            reference, slippage = random_decimal(rng), random_slippage(rng)
            for buy in (True, False):
                case = f'seed={SEED + 3} case={index} base_dec={base_dec} reference={reference} slippage={slippage} buy={buy}'
                approved = reference * (1 + slippage if buy else 1 - slippage)
                tick = price_tick(approved, max_decimals)
                try:
                    price = generated_spot['bounded_px_spot'](str(reference), str(slippage), buy)
                except ValueError:
                    self.assertTrue(buy and approved < tick, case)
                    continue
                accepted += 1
                self.assertTrue(price <= approved if buy else price >= approved, case)
                self.assertLess(abs(price - approved), tick, case)
                self.assert_on_grid(price, max_decimals, case)
                self.assertEqual(sdk_wire(float(price)), price, case)
                for mode in MODES:
                    self.assertEqual(generated_spot['round_px_spot'](price, rounding=mode), price, f'{case} mode={mode}')
            requested = random_decimal(rng, -7, 7)
            lot = Decimal(1).scaleb(-base_dec)
            case = f'seed={SEED + 3} case={index} base_dec={base_dec} size={requested}'
            try:
                size = generated_spot['round_sz_spot'](str(requested))
            except ValueError:
                self.assertLess(requested, lot, case)
                continue
            self.assertLessEqual(size, requested, case)
            self.assertLess(requested - size, lot, case)
            self.assertEqual(size % lot, 0, case)
            # A float holds about sixteen digits, so eight wire decimals are exact only
            # below ten million units; above that the published snippet's own assert
            # (wire string equals the approved size) is what stops a changed size.
            if size < 10 ** 7:
                self.assertEqual(sdk_wire(float(size)), size, case)
        self.assertGreater(accepted, CASES)


if __name__ == '__main__':
    unittest.main()
