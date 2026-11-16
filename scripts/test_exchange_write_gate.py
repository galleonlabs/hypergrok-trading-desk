#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "hooks", "exchange_write_gate.py")
SPEC = importlib.util.spec_from_file_location("exchange_write_gate", GATE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import standing_mandate  # noqa: E402

ACCOUNT = "0x" + "1a" * 20
OTHER = "0x" + "2b" * 20


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
            "exchange.multi_sig(multi_sig_user, inner_action, signatures, nonce)",
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
            "await exchange.userOutcome({ outcome, action })",
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

    def test_short_receiver_and_getattr_still_ask(self):
        self.assertAsks(bash("python3 -c 'ex.order(coin, True, sz, px, t)'"))
        self.assertAsks(bash("python3 -c 'hl.cancel(\"ETH\", oid)'"))
        self.assertAsks(bash('python3 -c \'getattr(exchange, "order")(coin, True, sz, px, t)\''))
        self.assertAsks(bash("""python3 -c 'getattr(client, "withdraw3")(amount)'"""))
        for command in (
            "python3 -c 'cart.order(1)'",
            "python3 -c 'hex.order(1)'",
            "python3 -c 'html.order(1)'",
            "python3 -c 'getattr(row, \"note\")(1)'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(gate.decide(bash(command)))

    def test_exchange_host_is_case_insensitive_and_reads_stay_quiet(self):
        self.assertAsks(bash("curl -s -X POST https://api.hyperliquid.XYZ/exchange -d @signed.json"))
        self.assertAsks(bash("curl -s -X POST https://API.HYPERLIQUID.XYZ/exchange -d @signed.json"))
        self.assertIsNone(gate.decide(bash("curl -s -X POST https://API.HYPERLIQUID.XYZ/info -d '{\"type\":\"allMids\"}'")))

    def test_shell_script_and_cd_target_ask(self):
        with tempfile.TemporaryDirectory() as parent:
            cwd = os.path.join(parent, "cwd")
            other = os.path.join(parent, "other")
            os.makedirs(cwd)
            os.makedirs(other)
            with open(os.path.join(other, "send.py"), "w", encoding="utf-8") as handle:
                handle.write("res = exchange.order(coin, is_buy, sz, px, order_type)\n")
            with open(os.path.join(cwd, "send.sh"), "w", encoding="utf-8") as handle:
                handle.write("curl -s -X POST https://api.hyperliquid.xyz/exchange -d @signed.json\n")
            self.assertIsNone(gate.decide(bash("python3 send.py", cwd=cwd)))
            reason = self.assertAsks(bash(f"cd '{other}' && python3 send.py", cwd=cwd))
            self.assertIn("send.py", reason)
            self.assertAsks(bash("bash send.sh", cwd=cwd))
            self.assertAsks(bash('bash "send.sh"', cwd=cwd))

    def test_unreadable_script_asks_without_echoing_a_key(self):
        key = "0x" + "ab" * 32
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "send.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"res = exchange.order('{key}')\n")
            original = gate.MAX_SCRIPT_BYTES
            gate.MAX_SCRIPT_BYTES = 8
            try:
                reason = self.assertAsks(bash("python3 send.py", cwd=directory))
            finally:
                gate.MAX_SCRIPT_BYTES = original
            self.assertIn("unscanned", reason)
            self.assertNotIn(key, reason)
            self.assertNotIn("exchange.order", reason)

    def test_decision_does_not_echo_a_private_key(self):
        key = "0x" + "cd" * 32
        event = bash(f"python3 -c 'exchange.order(\"{key}\")'")
        reason = self.assertAsks(event)
        self.assertNotIn(key, reason)
        self.assertIn("order()", reason)
        completed = subprocess.run(
            [sys.executable, GATE], input=json.dumps(event), capture_output=True, text=True, check=True,
        )
        self.assertNotIn(key, completed.stdout)
        self.assertNotIn(key, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

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
        self.assertEqual(entry["matcher"], "Bash|Write|Edit|MultiEdit")
        (hook,) = entry["hooks"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/exchange_write_gate.py", hook["command"])


def mandate_record(network="testnet", **overrides):
    import datetime as dt
    values = {
        "network": network, "account": ACCOUNT, "markets": "BTC, ETH", "max risk per trade": "0.5%",
        "max total open risk": "1.5%", "max leverage": "3x", "daily loss stop": "-2%",
        "written": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "acknowledgement": standing_mandate.ACKNOWLEDGEMENT.format(network=network),
    }
    values.update(overrides)
    lines = ["# Desk record", f"- network: {network}", "", "## Standing mandate"]
    lines += [f"- {key}: {value}" for key, value in values.items() if value is not None]
    return "\n".join(lines) + "\n"


SEND = ("exchange = Exchange(wallet, constants.{url}, account_address=\"{account}\")\n"
        "res = exchange.order(\"ETH\", True, 0.1, 3000, order_type, cloid=cloid)\n")


class StandingMandateGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.desk = os.path.join(self.temp.name, "trading-desk")
        for folder in ("proposals", "journal"):
            os.makedirs(os.path.join(self.desk, folder))
        self.env = patch.dict(os.environ, {"HYPERGROK_DESK_ROOT": self.desk})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def desk_md(self, text):
        with open(os.path.join(self.desk, "desk.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def ticket(self, ticket="HG-20261116-01", authority=True, passed=True):
        report = standing_mandate.evaluate(self.desk)
        lines = [f"# {ticket}", "## risk", "RISK | PASS" if passed else "RISK | REJECT"]
        if authority and report["mandate"]:
            lines.append(f"authority: standing mandate {report['mandate']['written']} sha {report['mandate']['fingerprint']}")
        with open(os.path.join(self.desk, "proposals", f"{ticket}.md"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def script(self, body, name="send.py"):
        path = os.path.join(self.temp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def run_send(self, body, ticket="HG-20261116-01"):
        path = self.script(body)
        return gate.decide(bash(f"python3 {path} --ticket {ticket}", cwd=self.temp.name))

    def reason(self, decision):
        self.assertIsNotNone(decision)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "ask")
        return decision["hookSpecificOutput"]["permissionDecisionReason"]

    def valid_testnet(self, **overrides):
        self.desk_md(mandate_record(**overrides))
        self.ticket()

    def test_valid_mandate_for_the_write_is_silent(self):
        self.valid_testnet()
        self.assertIsNone(self.run_send(SEND.format(url="TESTNET_API_URL", account=ACCOUNT)))
        cancel = ("exchange = Exchange(wallet, \"https://api.hyperliquid-testnet.xyz\", account_address=\"%s\")\n"
                  "exchange.cancel_by_cloid(\"ETH\", cloid)\nexchange.update_leverage(3, \"ETH\", is_cross=True)\n" % ACCOUNT)
        self.assertIsNone(self.run_send(cancel))

    def test_mainnet_mandate_is_silent_without_testnet_history(self):
        self.desk_md(mandate_record("mainnet"))
        self.ticket()
        self.assertIsNone(self.run_send(SEND.format(url="MAINNET_API_URL", account=ACCOUNT)))

    def test_limits_above_default_still_silent(self):
        self.valid_testnet(**{"max risk per trade": "5%", "max leverage": "40x", "markets": "all"})
        self.assertIsNone(self.run_send(SEND.format(url="TESTNET_API_URL", account=ACCOUNT)))

    def test_no_block_or_unset_root_asks(self):
        self.desk_md("# Desk record\n- standing mandate: none\n")
        self.ticket()
        self.assertIn("no standing mandate in force", self.reason(self.run_send(SEND.format(url="TESTNET_API_URL", account=ACCOUNT))))
        self.valid_testnet()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("HYPERGROK_DESK_ROOT", self.reason(self.run_send(SEND.format(url="TESTNET_API_URL", account=ACCOUNT))))

    def test_invalid_revoked_expired_duplicate_and_suspended_mandates_ask(self):
        body = SEND.format(url="TESTNET_API_URL", account=ACCOUNT)
        for name, text in (
            ("missing limit", mandate_record(**{"max leverage": None})),
            ("malformed", mandate_record(**{"daily loss stop": "two percent"})),
            ("wildcard", mandate_record(markets="*")),
            ("future written", mandate_record(written="2999-01-01")),
            ("expired", mandate_record(written="2026-01-01", expires="2026-01-31")),
            ("revoked", mandate_record() + "- standing mandate: revoked\n"),
            ("duplicate", mandate_record() + "\n## Standing mandate\n- network: testnet\n"),
        ):
            with self.subTest(case=name):
                self.desk_md(text)
                self.ticket()
                self.assertIn("no standing mandate in force", self.reason(self.run_send(body)))
        self.valid_testnet()
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with open(os.path.join(self.desk, "journal", f"{today}.md"), "w", encoding="utf-8") as handle:
            handle.write("12:00 UTC  mandate suspended  reason daily-loss-stop\n")
        self.assertIn("suspended", self.reason(self.run_send(body)))

    def test_wrong_network_or_unclear_network_asks(self):
        self.valid_testnet()
        self.assertIn("no standing mandate in force for mainnet", self.reason(self.run_send(SEND.format(url="MAINNET_API_URL", account=ACCOUNT))))
        both = SEND.format(url="TESTNET_API_URL", account=ACCOUNT) + "fallback = constants.MAINNET_API_URL\n"
        self.assertIn("exactly one network", self.reason(self.run_send(both)))
        implicit = "exchange = Exchange(wallet, account_address=\"%s\")\nexchange.order(c, True, 1, 2, t)\n" % ACCOUNT
        self.assertIn("exactly one network", self.reason(self.run_send(implicit)))
        commented = "# constants.TESTNET_API_URL\n" + implicit
        self.assertIn("exactly one network", self.reason(self.run_send(commented)))

    def test_wrong_or_extra_account_asks(self):
        self.valid_testnet()
        self.assertIn("account", self.reason(self.run_send(SEND.format(url="TESTNET_API_URL", account=OTHER))))
        vault = SEND.format(url="TESTNET_API_URL", account=ACCOUNT) + "vault = \"%s\"\n" % OTHER
        self.assertIn("no other address", self.reason(self.run_send(vault)))

    def test_writes_outside_a_mandate_always_ask(self):
        self.valid_testnet()
        prefix = "exchange = Exchange(wallet, constants.TESTNET_API_URL, account_address=\"%s\")\n" % ACCOUNT
        for call in ("exchange.withdraw_from_bridge(10, dest)", "exchange.usd_transfer(10, dest)", "exchange.approve_agent()",
                     "exchange.vault_usd_transfer(v, True, 10)", "exchange.schedule_cancel(None)", "exchange.update_isolated_margin(5, \"ETH\")"):
            with self.subTest(call=call):
                self.assertIn("outside any standing mandate", self.reason(self.run_send(prefix + call + "\n")))
        raw = prefix + "requests.post(\"https://api.hyperliquid-testnet.xyz/exchange\", json=signed)\n"
        self.assertIn("cannot be checked", self.reason(self.run_send(raw)))
        dynamic = prefix + "getattr(exchange, \"order\")(c, True, 1, 2, t)\n"
        self.assertIn("cannot be checked", self.reason(self.run_send(dynamic)))

    def test_ticket_record_binds_the_current_mandate(self):
        self.valid_testnet()
        body = SEND.format(url="TESTNET_API_URL", account=ACCOUNT)
        path = self.script(body)
        self.assertIn("ticket id", self.reason(gate.decide(bash(f"python3 {path}", cwd=self.temp.name))))
        self.assertIn("proposal file is missing", self.reason(self.run_send(body, ticket="HG-20261116-09")))
        self.ticket(authority=False)
        self.assertIn("authority line", self.reason(self.run_send(body)))
        self.ticket(passed=False)
        self.assertIn("Risk PASS", self.reason(self.run_send(body)))
        self.ticket()
        self.desk_md(mandate_record(**{"max risk per trade": "0.25%"}))
        self.assertIn("authority line", self.reason(self.run_send(body)), "an edited mandate needs a re-checked ticket")
        self.ticket()
        self.assertIsNone(self.run_send(body))

    def test_edits_that_add_or_change_a_mandate_ask(self):
        path = os.path.join(self.desk, "desk.md")
        self.desk_md("# Desk record\n- standing mandate: none\n")
        written = gate.decide({"tool_name": "Write", "tool_input": {"file_path": path, "content": mandate_record()}})
        self.assertIn("standing mandate", self.reason(written))
        self.desk_md(mandate_record())
        edit = {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": "- max leverage: 3x", "new_string": "- max leverage: 10x"}}
        self.reason(gate.decide(edit))
        unrelated = {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": "# Desk record", "new_string": "# Desk record (testnet)"}}
        self.assertIsNone(gate.decide(unrelated))
        revoke = {"tool_name": "Write", "tool_input": {"file_path": path, "content": mandate_record() + "- standing mandate: revoked\n"}}
        self.assertIsNone(gate.decide(revoke))
        other = {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.desk, "notes.md"), "content": mandate_record()}}
        self.assertIsNone(gate.decide(other))
        multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": path, "edits": [{"old_string": "0.5%", "new_string": "1%"}]}}
        self.reason(gate.decide(multi))

    def test_bash_writes_to_the_desk_record_ask(self):
        for command in ("echo '- max leverage: 10x' >> trading-desk/desk.md", "sed -i '' 's/3x/10x/' desk.md",
                        "cp /tmp/new.md desk.md", "python3 -c \"open('desk.md','a').write('x')\""):
            with self.subTest(command=command):
                self.assertIn("desk.md", self.reason(gate.decide(bash(command))))
        for command in ("cat desk.md", "grep -n mandate trading-desk/desk.md", "python3 scripts/standing_mandate.py --desk-root trading-desk"):
            with self.subTest(command=command):
                self.assertIsNone(gate.decide(bash(command)))


if __name__ == "__main__":
    unittest.main()
