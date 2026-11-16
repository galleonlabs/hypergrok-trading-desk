#!/usr/bin/env python3
"""Validate the standing mandate in a desk record. Read-only; stdlib only.

A standing mandate is an optional block the user writes themselves in
`desk.md`. Without one, every exchange write needs approval by ticket id, as
before. This tool parses the block, reports each field, and says whether a
mandate is in force for a network and account. It never writes, never reads
keys or the environment, and makes no network request.

Exit codes: 0 when there is no mandate, it is revoked, or it is valid and in
force; 1 when a block exists but is invalid, expired, suspended for the UTC day
or does not apply to the requested network or account; 2 for invalid
arguments. Exit 0 does not mean a mandate is in force: read `in_force`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys

HEADING_RE = re.compile(r"^#{1,6}\s+standing mandate\s*$", re.IGNORECASE)
ANY_HEADING_RE = re.compile(r"^#{1,6}\s")
STATE_LINE_RE = re.compile(r"^\s*[-*]?\s*standing mandate\s*:\s*(.*?)\s*$", re.IGNORECASE)
FIELD_RE = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z -]*?)\s*:\s*(.*?)\s*$")
ACCOUNT_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
MARKET_RE = re.compile(r"^[A-Za-z0-9]+(?::[A-Za-z0-9]+)?$")
PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%(?:\s+of\s+equity)?$", re.IGNORECASE)
LOSS_RE = re.compile(r"^-\s*(\d+(?:\.\d+)?)\s*%(?:\s+of\s+start-of-day\s+equity)?$", re.IGNORECASE)
LEVERAGE_RE = re.compile(r"^(\d+)\s*x?$", re.IGNORECASE)
COUNT_RE = re.compile(r"^(\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"0x[0-9a-fA-F]{64}")
SUSPENSION_RE = re.compile(r"\bmandate suspended\b.*\breason\s+daily-loss-stop\b", re.IGNORECASE)

ACKNOWLEDGEMENT = (
    "I wrote this mandate myself. I accept that the desk will place {network} orders for this account "
    "without asking me first, within these limits, until I revoke it, and that I am responsible for "
    "every order placed under it."
)

# (key in desk.md, required). Order is the canonical order for the fingerprint.
FIELDS = (
    ("network", True),
    ("account", True),
    ("markets", True),
    ("max risk per trade", True),
    ("max total open risk", True),
    ("max leverage", True),
    ("daily loss stop", True),
    ("max new entries per day", False),
    ("written", True),
    ("expires", False),
    ("acknowledgement", True),
)
FIELD_NAMES = tuple(name for name, _required in FIELDS)

# The desk ceilings from desk-risk-limits section 0. They stay the defaults without a mandate; a mandate
# may set its own values above them, and the report says so.
DEFAULTS = {
    "max risk per trade": (2.0, "2% of equity"),
    "max total open risk": (6.0, "6% of equity"),
    "max leverage": (20, "20x"),
    "daily loss stop": (10.0, "-10% of start-of-day equity"),
}


def _strip_comment(value: str) -> str:
    return re.sub(r"\s+#.*$", "", value).strip()


def _normalise(value: str) -> str:
    return " ".join(value.split())


def parse(text: str) -> dict:
    """Locate the mandate in a desk record without judging its values.

    Returns {"state": "none" | "revoked" | "block" | "invalid", ...}. A revoke
    line wins over everything else. Two blocks, or a block beside an explicit
    `standing mandate: none`, are ambiguous and fail closed.
    """
    lines = text.splitlines()
    state_values = []
    for line in lines:
        match = STATE_LINE_RE.match(line)
        if match:
            state_values.append(_strip_comment(match.group(1)).lower())
    if "revoked" in state_values:
        return {"state": "revoked", "reason": "`standing mandate: revoked` is present"}

    starts = [index for index, line in enumerate(lines) if HEADING_RE.match(line.strip())]
    unknown_states = sorted({value for value in state_values if value not in ("none", "revoked")})
    if unknown_states:
        return {"state": "invalid", "reason": f"`standing mandate: {redact(unknown_states[0])[:40]}` is not understood; use the block, `none` or `revoked`"}
    if len(starts) > 1:
        return {"state": "invalid", "reason": f"{len(starts)} standing mandate blocks found; keep exactly one"}
    if not starts:
        return {"state": "none", "reason": "no standing mandate block"}
    if "none" in state_values:
        return {"state": "invalid", "reason": "a mandate block and `standing mandate: none` are both present; remove one"}

    body = []
    for line in lines[starts[0] + 1:]:
        if ANY_HEADING_RE.match(line.strip()):
            break
        body.append(line)
    raw, duplicates, notes = {}, [], []
    for line in body:
        if not line.strip():
            continue
        match = FIELD_RE.match(line)
        if not match:
            notes.append(f"ignored line: {redact(line.strip())[:60]}")
            continue
        key = _normalise(match.group(1).lower())
        value = match.group(2) if key == "acknowledgement" else _strip_comment(match.group(2))
        if key not in FIELD_NAMES:
            notes.append(f"ignored unknown field: {redact(key)[:40]}")
            continue
        if key in raw:
            duplicates.append(key)
        raw[key] = _normalise(value)
    if duplicates:
        return {"state": "invalid", "reason": f"field written more than once: {', '.join(sorted(set(duplicates)))}"}
    return {"state": "block", "raw": raw, "notes": notes}


def fingerprint(raw: dict) -> str:
    canonical = "".join(f"{name}: {raw.get(name, '')}\n" for name in FIELD_NAMES)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def block_signature(text: str):
    """A comparable summary used by the Claude Code gate to spot mandate edits."""
    parsed = parse(text)
    if parsed["state"] == "block":
        return ("block", fingerprint(parsed["raw"]))
    return (parsed["state"], parsed.get("reason", ""))


def redact(value):
    return KEY_RE.sub("0x[redacted]", value) if isinstance(value, str) else value


def _field(name, status, value, detail, **extra):
    value, detail = redact(value), redact(detail)
    entry = {"field": name, "status": status, "value": value, "detail": detail}
    entry.update(extra)
    return entry


def _date(value):
    if not DATE_RE.match(value):
        raise ValueError("use YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def check_fields(raw: dict, today: dt.date) -> list[dict]:
    results = []
    network = raw.get("network", "").lower()
    written = None
    for name, required in FIELDS:
        value = raw.get(name)
        if value is None or value == "":
            if required:
                results.append(_field(name, "missing", None, "required"))
            else:
                results.append(_field(name, "absent", None, "optional; not set"))
            continue
        try:
            if name == "network":
                if value.lower() not in ("mainnet", "testnet"):
                    raise ValueError("must be mainnet or testnet")
                results.append(_field(name, "valid", value.lower(), "applies only to this network"))
            elif name == "account":
                if not ACCOUNT_RE.match(value):
                    raise ValueError("must be a 0x address of 40 hex characters")
                results.append(_field(name, "valid", value.lower(), "applies only to this account"))
            elif name == "markets":
                markets = [part.strip() for part in value.split(",")]
                if [m.lower() for m in markets] == ["all"]:
                    results.append(_field(name, "valid", "all", "every market the exchange lists"))
                    continue
                if any(not m for m in markets):
                    raise ValueError("empty market in the list")
                bad = [m for m in markets if not MARKET_RE.match(m) or m.lower() == "all"]
                if bad:
                    raise ValueError(f"not a market name: {bad[0]} (write `all` on its own, or list markets)")
                if len({m.upper() for m in markets}) != len(markets):
                    raise ValueError("a market is listed twice")
                results.append(_field(name, "valid", markets, f"{len(markets)} listed"))
            elif name in ("max risk per trade", "max total open risk"):
                match = PERCENT_RE.match(value)
                if not match:
                    raise ValueError("write a percentage of equity, such as 0.5%")
                number = float(match.group(1))
                if not 0 < number <= 100:
                    raise ValueError("must be above 0% and at most 100%")
                results.append(_with_default(name, number, f"{match.group(1)}% of equity"))
            elif name == "max leverage":
                match = LEVERAGE_RE.match(value)
                if not match or int(match.group(1)) < 1:
                    raise ValueError("write a whole number of at least 1, such as 3x")
                results.append(_with_default(name, int(match.group(1)), f"{int(match.group(1))}x; the exchange's market and tier maximum still applies"))
            elif name == "daily loss stop":
                match = LOSS_RE.match(value)
                if not match:
                    raise ValueError("write a negative percentage of start-of-day equity, such as -2%")
                number = float(match.group(1))
                if not 0 < number <= 100:
                    raise ValueError("must be below 0% and no lower than -100%")
                results.append(_with_default(name, number, f"-{match.group(1)}% suspends the mandate for the rest of the UTC day"))
            elif name == "max new entries per day":
                match = COUNT_RE.match(value)
                if not match or int(match.group(1)) < 1:
                    raise ValueError("write a whole number of at least 1")
                results.append(_field(name, "valid", int(match.group(1)), "new entries per UTC day"))
            elif name == "written":
                written = _date(value)
                if written > today:
                    raise ValueError(f"{value} is after today ({today.isoformat()} UTC)")
                results.append(_field(name, "valid", value, "date the user wrote this mandate"))
            elif name == "expires":
                expires = _date(value)
                base = written or (_safe_date(raw.get("written")))
                if base and expires < base:
                    raise ValueError("is before the written date")
                if today > expires:
                    results.append(_field(name, "expired", value, f"ended at the end of {value} UTC"))
                else:
                    results.append(_field(name, "valid", value, f"in force through {value} UTC"))
            elif name == "acknowledgement":
                if network not in ("mainnet", "testnet"):
                    raise ValueError("cannot be checked until network is valid")
                expected = ACKNOWLEDGEMENT.format(network=network)
                if _normalise(value) != expected:
                    raise ValueError("does not match the acknowledgement sentence in docs/STANDING_MANDATE.md for this network")
                results.append(_field(name, "valid", "matches", "written by the user, word for word"))
        except ValueError as exc:
            results.append(_field(name, "invalid", value if name != "acknowledgement" else "differs", str(exc)))
    return results


def _safe_date(value):
    try:
        return _date(value) if value else None
    except ValueError:
        return None


def _with_default(name, number, detail):
    default, label = DEFAULTS[name]
    above = number > default
    if above:
        detail = f"{detail}; above the default desk ceiling of {label}"
    return _field(name, "valid", number, detail, above_default=above)


def suspended_today(desk_root: str, today: dt.date) -> bool:
    path = os.path.join(desk_root, "journal", f"{today.isoformat()}.md")
    try:
        with open(path, encoding="utf-8") as handle:
            return any(SUSPENSION_RE.search(line) for line in handle)
    except OSError:
        return False


def evaluate(desk_root: str, today: dt.date | None = None, network: str | None = None, account: str | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    record = os.path.join(desk_root, "desk.md")
    report = {"schema_version": 1, "mode": "read-only", "desk_record": record, "date_utc": today.isoformat(),
              "verdict": "none", "in_force": False, "mandate": None, "fields": [], "reasons": [], "notes": []}
    try:
        with open(record, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        report["reasons"].append("desk.md not found; every send needs approval by ticket id")
        return report
    except (OSError, UnicodeError) as exc:
        report.update(verdict="invalid", reasons=[f"desk.md unreadable: {exc}"])
        return report

    parsed = parse(text)
    if parsed["state"] in ("none", "revoked"):
        report.update(verdict=parsed["state"], reasons=[parsed["reason"] + "; every send needs approval by ticket id"])
        return report
    if parsed["state"] == "invalid":
        report.update(verdict="invalid", reasons=[parsed["reason"]])
        return report

    raw = parsed["raw"]
    fields = check_fields(raw, today)
    report["fields"] = fields
    report["notes"] = parsed["notes"]
    valid = {entry["field"]: entry["value"] for entry in fields if entry["status"] in ("valid", "expired")}
    report["mandate"] = {"written": valid.get("written"), "network": valid.get("network"),
                         "account": valid.get("account"), "fingerprint": fingerprint(raw)}
    failed = [f for f in fields if f["status"] in ("missing", "invalid")]
    if failed:
        report["verdict"] = "invalid"
        report["reasons"] = [f"{f['field']}: {f['status']} ({f['detail']})" for f in failed]
        return report
    expired = [f for f in fields if f["status"] == "expired"]
    if expired:
        report["verdict"] = "expired"
        report["reasons"] = [f"expires: {expired[0]['detail']}"]
        return report
    mismatch = []
    if network and network.lower() != report["mandate"]["network"]:
        mismatch.append(f"mandate is for {report['mandate']['network']}, not {network.lower()}")
    if account and account.lower() != report["mandate"]["account"]:
        mismatch.append("mandate names a different account")
    if mismatch:
        report.update(verdict="not-applicable", reasons=mismatch)
        return report
    if suspended_today(desk_root, today):
        report.update(verdict="suspended", reasons=[f"daily loss stop recorded in journal/{today.isoformat()}.md; suspended until 00:00 UTC"])
        return report
    report.update(verdict="valid", in_force=True,
                  reasons=[f"in force for {report['mandate']['network']} account {report['mandate']['account']}"])
    return report


def render(report: dict) -> str:
    lines = ["HYPERGROK STANDING MANDATE", "READ ONLY · no key access · no writes · no network", "",
             f"verdict   {report['verdict']}{' (in force)' if report['in_force'] else ''}"]
    if report["mandate"]:
        lines.append(f"mandate   written {report['mandate']['written']} · fingerprint {report['mandate']['fingerprint']}")
    for entry in report["fields"]:
        value = entry["value"]
        shown = ", ".join(value) if isinstance(value, list) else value
        lines.append(f"{entry['status']:<8}  {entry['field']:<24} {shown if shown is not None else '-'}  {entry['detail']}")
    lines.extend(["", *report["reasons"], *report["notes"]])
    return "\n".join(lines)


def main(argv=None) -> int:
    from cli_contract import ReadOnlyParser, emit_error

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ReadOnlyParser(description=__doc__.splitlines()[0], machine_readable="--json" in argv or "--schema" in argv)
    parser.add_argument("--desk-root", help="the trading-desk directory that holds desk.md")
    parser.add_argument("--network", choices=("mainnet", "testnet"), help="check that the mandate applies to this network")
    parser.add_argument("--account", help="check that the mandate applies to this 0x account")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--schema", action="store_true", help="describe the JSON output and exit")
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps({"schema_version": 1, "command": "standing_mandate", "mode": "read-only",
                          "network_access": "none",
                          "exit_codes": {"0": "no mandate, revoked, or valid (read in_force)",
                                         "1": "invalid, expired, suspended or not applicable", "2": "invalid arguments"},
                          "success": {"verdict": "none | revoked | valid | invalid | expired | suspended | not-applicable",
                                      "in_force": "boolean", "fields": [{"field": "string", "status": "valid | missing | invalid | expired | absent",
                                                                          "value": "parsed value", "detail": "string", "above_default": "boolean, limits only"}]},
                          "safety": "Reads desk.md and today's journal file only. Never writes, never reads keys or the environment."}, indent=2))
        return 0
    if not args.desk_root:
        parser.error("--desk-root is required")
    if args.account and not ACCOUNT_RE.match(args.account):
        parser.error("account must be a 0x address of 40 hex characters")
    try:
        report = evaluate(os.path.abspath(args.desk_root), network=args.network, account=args.account)
    except (OSError, ValueError) as exc:
        if args.json:
            emit_error("source_unavailable", str(exc))
        else:
            print(f"Standing mandate check unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["verdict"] in ("none", "revoked", "valid") else 1


if __name__ == "__main__":
    sys.exit(main())
