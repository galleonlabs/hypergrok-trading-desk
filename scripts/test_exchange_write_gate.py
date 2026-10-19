#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "hooks", "exchange_write_gate.py")
SPEC = importlib.util.spec_from_file_location("exchange_write_gate", GATE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def bash(command, cwd=ROOT):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


class ExchangeWriteGateTest(unittest.TestCase):
    def assertAsks(self, event):
        decision = gate.decide(event)
        self.assertIsNotNone(decision, event["tool_input"])
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("ticket", output["permissionDecisionReason"])
        return output["permissionDecisionReason"]

    def test_python_sdk_writes_ask(self):
        for call in (
            'exchange.order(coin, True, sz, px, {"limit": {"tif": "Gtc"}}, reduce_only=False, cloid=cloid)',
            "exchange.market_close(coin, sz=None, px=None, slippage=0.003)",
            'exchange.cancel("ETH", oid)',
            'exchange.update_leverage(3, "ETH", is_cross=True)',
            "exchange.schedule_cancel(None)",
            "hl_exchange.bulk_orders(orders, grouping='normalTpsl')",
            'exchange.send_asset(dest, "", "spot", "USDC", 10.0)',
            "exchange.agent_set_abstraction('u')",
            "exchange.noop(nonce)",
        ):
            with self.subTest(call=call):
                self.assertAsks(bash(f"python3 - <<'PY'\n{call}\nPY"))

    def test_typescript_sdk_writes_ask(self):
        for call in (
            "await exchange.order({ orders: [order], grouping: 'na' })",
            "await exchangeClient.cancelByCloid({ cancels })",
            "await exchange.updateLeverage({ asset: a, isCross: true, leverage: 3 })",
            "await exchange.sendAsset({ destination, token, amount })",
            "await exchangeClient.topUpIsolatedOnlyMargin({ asset: a, leverage: 3 })",
            "await exchange.reserveRequestWeight({ weight: 10 })",
        ):
            with self.subTest(call=call):
                self.assertAsks(bash(f'node -e "{call}"'))

    def test_raw_exchange_posts_ask(self):
        for command in (
            "curl -s -X POST https://api.hyperliquid.xyz/exchange -d @signed.json",
            "curl -s -X POST https://api.hyperliquid-testnet.xyz/exchange -d @signed.json",
            'python3 -c \'requests.post(f"{BASE}/exchange", json=payload)\'',
            "node -e 'fetch(BASE + \"/exchange\", opts)'",
        ):
            with self.subTest(command=command):
                self.assertAsks(bash(command))

    def test_protective_and_reduce_only_sends_still_ask(self):
        # The gate tightens and never loosens: no class of send is auto-approved.
        self.assertAsks(bash('python3 -c \'exchange.order("ETH", False, 0.5, 2900, {"trigger": {"triggerPx": 2900, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)\''))

    def test_write_inside_a_named_script_asks(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "tickets"))
            with open(os.path.join(directory, "tickets", "send.py"), "w", encoding="utf-8") as handle:
                handle.write("res = exchange.order(coin, is_buy, sz, px, order_type, cloid=cloid)\n")
            reason = self.assertAsks(bash("cd /workspace && python3 tickets/send.py --ticket HG-20260816-01", cwd=directory))
            self.assertIn("tickets/send.py", reason)

    def test_reads_and_research_pass_through(self):
        for command in (
            "python3 scripts/opening_bell.py --json",
            "python3 scripts/desk_doctor.py --offline",
            "python3 scripts/research_dataset.py --verify data/eth-1h --json",
            "bash scripts/check.sh",
            'curl -s https://api.hyperliquid.xyz/info -H "Content-Type: application/json" -d \'{"type":"allMids"}\'',
            "python3 -c 'info.user_state(ADDR); info.open_orders(ADDR)'",
            "grep -rn 'exchange endpoint' skills/",
            "git log --oneline -5",
        ):
            with self.subTest(command=command):
                self.assertIsNone(gate.decide(bash(command)))

    def test_repository_tools_do_not_trip_the_gate(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "scripts"))):
            if name.endswith(".py") and not name.startswith("test_"):
                with self.subTest(script=name):
                    self.assertIsNone(gate.decide(bash(f"python3 scripts/{name} --help")))

    def test_other_tools_and_malformed_input_have_no_opinion(self):
        self.assertIsNone(gate.decide({"tool_name": "Write", "tool_input": {"content": "exchange.order(x)"}}))
        self.assertIsNone(gate.decide({"tool_name": "Bash", "tool_input": {}}))
        self.assertIsNone(gate.decide({"tool_name": "Bash"}))

    def test_hook_process_contract(self):
        ask = subprocess.run([sys.executable, GATE], input=json.dumps(bash("python3 -c 'exchange.order(c, True, 1, 2, t)'")), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(ask.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        quiet = subprocess.run([sys.executable, GATE], input=json.dumps(bash("ls")), capture_output=True, text=True, check=True)
        self.assertEqual(quiet.stdout, "")
        garbage = subprocess.run([sys.executable, GATE], input="not json", capture_output=True, text=True, check=True)
        self.assertEqual(garbage.stdout, "")

    def test_plugin_registers_the_hook(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        with open(os.path.join(ROOT, manifest["hooks"]), encoding="utf-8") as handle:
            config = json.load(handle)
        (entry,) = config["hooks"]["PreToolUse"]
        self.assertEqual(entry["matcher"], "Bash")
        (hook,) = entry["hooks"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/exchange_write_gate.py", hook["command"])


if __name__ == "__main__":
    unittest.main()
