#!/usr/bin/env python3
"""Fixtures for the standing mandate validator. No network, no keys."""
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
SPEC = importlib.util.spec_from_file_location("standing_mandate", os.path.join(ROOT, "scripts", "standing_mandate.py"))
mandate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mandate
SPEC.loader.exec_module(mandate)

TODAY = dt.date(2026, 11, 16)
ACCOUNT = "0x" + "1a" * 20
OTHER = "0x" + "2b" * 20
KEY = "0x" + "cd" * 32


def fields(network="testnet", **overrides):
    values = {
        "network": network,
        "account": ACCOUNT,
        "markets": "BTC, ETH",
        "max risk per trade": "0.5%",
        "max total open risk": "1.5%",
        "max leverage": "3x",
        "daily loss stop": "-2%",
        "max new entries per day": "4",
        "written": "2026-11-16",
        "expires": "2026-12-16",
        "acknowledgement": mandate.ACKNOWLEDGEMENT.format(network=network),
    }
    values.update(overrides)
    return values


def record(values=None, extra="", heading="## Standing mandate"):
    lines = ["# Desk record", "", "- network: testnet", f"- account: {ACCOUNT}", "- standing approvals: none", ""]
    if values is not None:
        lines.append(heading)
        lines.extend(f"- {key}: {value}" for key, value in values.items() if value is not None)
        lines.append("")
    return "\n".join(lines) + extra


class MandateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        os.makedirs(os.path.join(self.root, "journal"))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text):
        with open(os.path.join(self.root, "desk.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def evaluate(self, text, **kwargs):
        self.write(text)
        return mandate.evaluate(self.root, today=kwargs.pop("today", TODAY), **kwargs)

    def status(self, report, name):
        return next(entry for entry in report["fields"] if entry["field"] == name)

    def test_no_record_and_no_block_are_no_mandate(self):
        self.assertEqual(mandate.evaluate(self.root, today=TODAY)["verdict"], "none")
        report = self.evaluate(record())
        self.assertEqual((report["verdict"], report["in_force"]), ("none", False))
        report = self.evaluate(record(extra="- standing mandate: none   # optional\n"))
        self.assertEqual(report["verdict"], "none")

    def test_valid_block_is_in_force(self):
        report = self.evaluate(record(fields()))
        self.assertEqual((report["verdict"], report["in_force"]), ("valid", True), report["reasons"])
        self.assertEqual(report["mandate"]["account"], ACCOUNT)
        self.assertEqual(len(report["mandate"]["fingerprint"]), 16)
        self.assertTrue(all(entry["status"] == "valid" for entry in report["fields"]))
        self.assertEqual(self.status(report, "markets")["value"], ["BTC", "ETH"])

    def test_mainnet_block_is_valid_without_any_testnet_history(self):
        report = self.evaluate(record(fields("mainnet")), network="mainnet")
        self.assertTrue(report["in_force"], report["reasons"])

    def test_each_missing_required_field_fails_closed(self):
        for name, required in mandate.FIELDS:
            with self.subTest(field=name):
                report = self.evaluate(record(fields(**{name: None})))
                if required:
                    self.assertEqual(report["verdict"], "invalid")
                    self.assertFalse(report["in_force"])
                    self.assertEqual(self.status(report, name)["status"], "missing")
                    self.assertTrue(any(reason.startswith(name) for reason in report["reasons"]))
                else:
                    self.assertTrue(report["in_force"], report["reasons"])
                    self.assertEqual(self.status(report, name)["status"], "absent")

    def test_markets_all_or_list_and_wildcards_fail(self):
        self.assertTrue(self.evaluate(record(fields(markets="all")))["in_force"])
        self.assertTrue(self.evaluate(record(fields(markets="BTC, xyz:TSLA")))["in_force"])
        for value in ("*", "BTC, *", "BTC, all", "any market", "BTC ETH etc", "BTC/ETH"):
            with self.subTest(markets=value):
                report = self.evaluate(record(fields(markets=value)))
                self.assertEqual(self.status(report, "markets")["status"], "invalid")
                self.assertFalse(report["in_force"])

    def test_limits_above_default_ceilings_are_reported_not_rejected(self):
        report = self.evaluate(record(fields(**{
            "max risk per trade": "5%", "max total open risk": "25%", "max leverage": "40x", "daily loss stop": "-30%"})))
        self.assertTrue(report["in_force"], report["reasons"])
        for name in ("max risk per trade", "max total open risk", "max leverage", "daily loss stop"):
            entry = self.status(report, name)
            self.assertEqual(entry["status"], "valid")
            self.assertTrue(entry["above_default"])
            self.assertIn("above the default", entry["detail"])
        self.assertFalse(self.status(self.evaluate(record(fields())), "max leverage")["above_default"])

    def test_malformed_values_fail_closed(self):
        cases = {
            "network": "mainnet-ish",
            "account": "0x1234",
            "max risk per trade": "half a percent",
            "max total open risk": "0%",
            "max leverage": "2.5x",
            "daily loss stop": "two percent",
            "max new entries per day": "0",
            "written": "16/11/2026",
            "expires": "next month",
        }
        for name, value in cases.items():
            with self.subTest(field=name):
                report = self.evaluate(record(fields(**{name: value})))
                self.assertEqual(report["verdict"], "invalid")
                self.assertEqual(self.status(report, name)["status"], "invalid")
        report = self.evaluate(record(fields(**{"max risk per trade": "150%"})))
        self.assertEqual(report["verdict"], "invalid")

    def test_acknowledgement_must_match_for_the_network(self):
        report = self.evaluate(record(fields("mainnet", acknowledgement=mandate.ACKNOWLEDGEMENT.format(network="testnet"))))
        self.assertEqual(self.status(report, "acknowledgement")["status"], "invalid")
        report = self.evaluate(record(fields(acknowledgement="I agree.")))
        self.assertEqual(report["verdict"], "invalid")

    def test_expiry_is_optional_and_honoured(self):
        self.assertTrue(self.evaluate(record(fields(expires=None)))["in_force"])
        self.assertTrue(self.evaluate(record(fields(written="2026-01-01", expires="2027-12-31")))["in_force"], "no 30-day maximum")
        self.assertTrue(self.evaluate(record(fields(expires="2026-11-16")))["in_force"], "valid through the expiry day")
        report = self.evaluate(record(fields(written="2026-10-01", expires="2026-11-15")))
        self.assertEqual((report["verdict"], report["in_force"]), ("expired", False))
        report = self.evaluate(record(fields(written="2026-11-10", expires="2026-11-01")))
        self.assertEqual(report["verdict"], "invalid")

    def test_future_written_date_fails(self):
        report = self.evaluate(record(fields(written="2026-11-17", expires=None)))
        self.assertEqual(report["verdict"], "invalid")
        self.assertIn("after today", self.status(report, "written")["detail"])

    def test_wrong_network_or_account_does_not_apply(self):
        report = self.evaluate(record(fields()), network="mainnet")
        self.assertEqual((report["verdict"], report["in_force"]), ("not-applicable", False))
        report = self.evaluate(record(fields()), account=OTHER)
        self.assertEqual((report["verdict"], report["in_force"]), ("not-applicable", False))
        self.assertTrue(self.evaluate(record(fields()), network="testnet", account=ACCOUNT.upper().replace("0X", "0x"))["in_force"])

    def test_revoked_line_wins(self):
        report = self.evaluate(record(fields(), extra="- standing mandate: revoked\n"))
        self.assertEqual((report["verdict"], report["in_force"]), ("revoked", False))
        report = self.evaluate("- Standing Mandate: REVOKED\n")
        self.assertEqual(report["verdict"], "revoked")

    def test_duplicate_or_conflicting_blocks_fail_closed(self):
        text = record(fields()) + "\n## Standing mandate\n- network: testnet\n"
        self.assertEqual(self.evaluate(text)["verdict"], "invalid")
        self.assertEqual(self.evaluate(record(fields(), extra="- standing mandate: none\n"))["verdict"], "invalid")
        self.assertEqual(self.evaluate(record(extra="- standing mandate: active\n"))["verdict"], "invalid")
        duplicate_field = record(fields()) .replace("- max leverage: 3x", "- max leverage: 3x\n- max leverage: 5x")
        self.assertEqual(self.evaluate(duplicate_field)["verdict"], "invalid")

    def test_block_ends_at_next_heading_and_ignores_prose(self):
        text = record(fields()) + "Written after the September review.\n\n## Bots\n- network: mainnet\n"
        report = self.evaluate(text)
        self.assertTrue(report["in_force"], report["reasons"])
        self.assertTrue(any("ignored" in note for note in report["notes"]))

    def test_daily_loss_stop_suspends_for_the_utc_day_only(self):
        self.write(record(fields()))
        journal = os.path.join(self.root, "journal", "2026-11-16.md")
        with open(journal, "w", encoding="utf-8") as handle:
            handle.write("14:05 UTC  mandate suspended  mandate 2026-11-16  reason daily-loss-stop  day pnl -2.1%\n")
        report = mandate.evaluate(self.root, today=TODAY)
        self.assertEqual((report["verdict"], report["in_force"]), ("suspended", False))
        self.assertTrue(mandate.evaluate(self.root, today=TODAY + dt.timedelta(days=1))["in_force"])

    def test_other_journal_events_do_not_suspend(self):
        self.write(record(fields()))
        with open(os.path.join(self.root, "journal", "2026-11-16.md"), "w", encoding="utf-8") as handle:
            handle.write("09:00 UTC  HG-20261116-02 rejected  minTradeNtlRejected; ticket void\n"
                         "09:05 UTC  alert  ETH position found without a resting stop; protection ticket opened\n")
        self.assertTrue(mandate.evaluate(self.root, today=TODAY)["in_force"])

    def test_editing_the_block_applies_new_values_and_changes_the_fingerprint(self):
        first = self.evaluate(record(fields()))
        second = self.evaluate(record(fields(**{"max risk per trade": "0.25%"})))
        self.assertTrue(second["in_force"])
        self.assertNotEqual(first["mandate"]["fingerprint"], second["mandate"]["fingerprint"])
        self.assertEqual(self.status(second, "max risk per trade")["value"], 0.25)

    def test_block_signature_tracks_mandate_changes_only(self):
        base = record(fields())
        self.assertEqual(mandate.block_signature(base), mandate.block_signature(base.replace("- standing approvals: none", "- standing approvals: stops")))
        self.assertNotEqual(mandate.block_signature(base), mandate.block_signature(base.replace("0.5%", "1%")))
        self.assertEqual(mandate.block_signature(record())[0], "none")
        self.assertEqual(mandate.block_signature(base + "- standing mandate: revoked\n")[0], "revoked")

    def test_cli_exit_codes_json_and_no_key_echo(self):
        self.write(record(fields(account=KEY)))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mandate.main(["--desk-root", self.root, "--json"]), 1)
        self.assertNotIn(KEY, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["verdict"], "invalid")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mandate.main(["--desk-root", self.root]), 1)
        self.assertNotIn(KEY, output.getvalue())
        self.write(record())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mandate.main(["--desk-root", self.root, "--json"]), 0)
        self.assertFalse(json.loads(output.getvalue())["in_force"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mandate.main(["--schema"]), 0)
        self.assertEqual(json.loads(output.getvalue())["command"], "standing_mandate")
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_error:
                mandate.main(["--json", "--desk-root", self.root, "--account", "nope"])
        self.assertEqual(exit_error.exception.code, 2)

    def test_validator_never_writes(self):
        self.write(record(fields()))
        before = {name: os.path.getmtime(os.path.join(self.root, name)) for name in ("desk.md",)}
        listing = sorted(os.listdir(self.root))
        mandate.evaluate(self.root, today=TODAY)
        self.assertEqual(sorted(os.listdir(self.root)), listing)
        self.assertEqual(before["desk.md"], os.path.getmtime(os.path.join(self.root, "desk.md")))


    def test_documented_template_parses_once_filled_in(self):
        import re
        with open(os.path.join(ROOT, "docs", "STANDING_MANDATE.md"), encoding="utf-8") as handle:
            block = re.search(r"```markdown\n(## Standing mandate.*?)```", handle.read(), re.S).group(1)
        self.assertEqual(self.evaluate("# Desk record\n\n" + block)["verdict"], "invalid", "the placeholder account must not validate")
        filled = block.replace("0xYOUR_ACCOUNT_ADDRESS", ACCOUNT)
        report = self.evaluate("# Desk record\n\n" + filled, today=dt.date(2026, 11, 20))
        self.assertTrue(report["in_force"], report["reasons"])


    def test_revoke_in_any_markdown_form_wins(self):
        for line in ("`standing mandate: revoked`", "**standing mandate:** revoked", "**standing mandate: revoked**",
                     "> standing mandate: revoked", "1. standing mandate: revoked", "standing mandate revoked",
                     "standing mandate - revoked", "+ Standing Mandate: REVOKED", "<!-- standing mandate: revoked -->",
                     "\ufeffstanding mandate: revoked"):
            with self.subTest(line=line):
                report = self.evaluate(record(fields(), extra=line + "\n"))
                self.assertEqual((report["verdict"], report["in_force"]), ("revoked", False))

    def test_block_inside_an_html_comment_is_ignored(self):
        text = "# Desk record\n<!--\n" + record(fields()).split("\n", 1)[1] + "-->\n"
        self.assertEqual(self.evaluate(text)["verdict"], "none")
        self.assertEqual(self.evaluate("<!-- unfinished\n" + record(fields()))["verdict"], "none", "an unclosed comment hides the rest")

    def test_unknown_field_fails_closed_and_synonyms_are_read(self):
        report = self.evaluate(record(fields(), extra="").replace("- expires:", "- expires in:"))
        self.assertEqual(report["verdict"], "invalid")
        self.assertIn("expires in", " ".join(report["reasons"]))
        report = self.evaluate(record(fields(**{"max new entries per day": None}), extra="").replace("## Standing mandate", "## Standing mandate\n- max entries per day: 2"))
        self.assertTrue(report["in_force"], report["reasons"])
        self.assertEqual(self.status(report, "max new entries per day")["value"], 2)
        for synonym in ("expiry", "expires on"):
            with self.subTest(synonym=synonym):
                report = self.evaluate(record(fields(written="2026-10-01", expires="2026-11-01")).replace("- expires:", f"- {synonym}:"))
                self.assertEqual(report["verdict"], "expired", "a renamed expiry must still be honoured")
                self.assertTrue(any("read field" in note for note in report["notes"]))

    def test_reasonable_formatting_is_accepted_and_reported(self):
        ack = mandate.ACKNOWLEDGEMENT.format(network="testnet")
        variants = {
            "bom": "\ufeff" + record(fields()),
            "plus bullets": record(fields()).replace("\n- ", "\n+ "),
            "numbered": "\n".join(f"{i}. {line[2:]}" if line.startswith("- ") else line for i, line in enumerate(record(fields()).splitlines())),
            "no bullet and bold keys": "\n".join(f"**{line[2:].split(':', 1)[0]}:**{line[2:].split(':', 1)[1]}" if line.startswith("- ") else line for line in record(fields()).splitlines()),
            "heading colon": record(fields()).replace("## Standing mandate", "## Standing mandate:"),
            "bold heading": record(fields()).replace("## Standing mandate", "**Standing mandate**"),
            "blockquote": "\n".join("> " + line for line in record(fields()).splitlines()),
        }
        for name, text in variants.items():
            with self.subTest(form=name):
                report = self.evaluate(text)
                self.assertTrue(report["in_force"], report["reasons"])
        values = {
            "max risk per trade": (".5%", 0.5), "max total open risk": ("1,5%", 1.5),
        }
        report = self.evaluate(record(fields(**{k: v for k, (v, _) in values.items()})))
        self.assertTrue(report["in_force"], report["reasons"])
        for key, (_raw, parsed) in values.items():
            self.assertEqual(self.status(report, key)["value"], parsed)
        self.assertTrue(any("decimal comma" in note for note in report["notes"]))
        self.assertEqual(self.status(self.evaluate(record(fields(**{"max risk per trade": "0.5 percent"}))), "max risk per trade")["value"], 0.5)
        for loss in ("-2", "2%", "\u22122%", "-2% of equity", "-2 percent"):
            with self.subTest(loss=loss):
                report = self.evaluate(record(fields(**{"daily loss stop": loss})))
                self.assertTrue(report["in_force"], report["reasons"])
                self.assertEqual(self.status(report, "daily loss stop")["value"], 2.0)
        for leverage in ("x3", "3.0x", "3", "3X"):
            with self.subTest(leverage=leverage):
                self.assertEqual(self.status(self.evaluate(record(fields(**{"max leverage": leverage}))), "max leverage")["value"], 3)
        for written in ("2026/11/16", "16 Nov 2026", "November 16, 2026", "2026-11-16T09:30:00Z", "2026-11-16T09:30:00+01:00"):
            with self.subTest(written=written):
                report = self.evaluate(record(fields(written=written, expires=None)))
                self.assertTrue(report["in_force"], report["reasons"])
        for never in ("never", "none", "None"):
            with self.subTest(expires=never):
                report = self.evaluate(record(fields(expires=never)))
                self.assertEqual(self.status(report, "expires")["status"], "absent")
                self.assertTrue(report["in_force"])
        for markets, parsed in (("BTC ETH", ["BTC", "ETH"]), ("BTC-PERP, ETH-perp", ["BTC", "ETH"]), ("BTC, ETH,", ["BTC", "ETH"]), ("BTC, btc", ["BTC"])):
            with self.subTest(markets=markets):
                report = self.evaluate(record(fields(markets=markets)))
                self.assertEqual(self.status(report, "markets")["value"], parsed)
                self.assertTrue(report["notes"])
        cut = ack.index(" ", 60)
        wrapped = record(fields(acknowledgement=ack[:cut])).replace(ack[:cut], ack[:cut] + "\n  " + ack[cut + 1:])
        self.assertTrue(self.evaluate(wrapped)["in_force"], "wrapped acknowledgement")
        for variant in (f'"{ack}"', ack.rstrip("."), ack.upper(), "  " + ack.replace(" ", "   ")):
            with self.subTest(acknowledgement=variant[:20]):
                self.assertTrue(self.evaluate(record(fields(acknowledgement=variant)))["in_force"])

    def test_ambiguous_values_still_fail_closed(self):
        for name, value in (("written", "11/16/2026"), ("written", "16/11/2026"), ("max risk per trade", "0.5"),
                            ("daily loss stop", "2"), ("max leverage", "x3x"), ("max leverage", "3.5x"), ("expires", "soon")):
            with self.subTest(field=name, value=value):
                report = self.evaluate(record(fields(**{name: value})))
                self.assertEqual(report["verdict"], "invalid")
                self.assertEqual(self.status(report, name)["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
