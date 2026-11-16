#!/usr/bin/env python3
"""Claude Code PreToolUse gate for Hyperliquid exchange writes. Stdlib only.

The ticket protocol (Risk PASS, approval by ticket id, one bounded send) is the
desk's control. This hook adds a mechanical second check in Claude Code: a Bash
command that looks like a signed `/exchange` request gets an "ask" decision, so
the user confirms it even in an auto-accepting session. Anything else gets no
opinion and follows the normal permission flow. The hook never approves.

Standing mandate. When the user has written a valid standing mandate in
`desk.md` (docs/STANDING_MANDATE.md), the hook stays silent instead of asking,
but only when every one of these holds; otherwise it asks as before:

- `HYPERGROK_DESK_ROOT` in the hook's environment names the desk directory, and
  `scripts/standing_mandate.py` reports the mandate in force for the network and
  account of this write;
- every write found is an order, cancel, modify or leverage call; transfers,
  withdrawals, agent approvals, raw `/exchange` posts, `getattr` calls and
  unscanned scripts always ask;
- the scanned text names exactly one network (testnet or mainnet endpoint or
  SDK constant, comment lines ignored) and it matches the mandate;
- the scanned text names the mandate's account and no other 0x address;
- the command names one ticket id whose proposal file records a PASS and the
  line `authority: standing mandate <written> sha <fingerprint>` for the
  mandate as it reads now, so a ticket checked against an edited mandate asks.

Silence is not approval: Claude Code's own permission rules still apply. The
hook cannot check sizing, open risk, the daily loss figure or the stop; those
stay with the Risk PASS and the desk's prose rules.

It also asks before a Write or Edit to a file named `desk.md` that adds or
changes a mandate block, and before a Bash command that names `desk.md` with a
redirect, `tee`, in-place edit, move, copy or delete. Removing or revoking a
mandate does not ask.

It reads the command text and any script file the command names. Relative paths
are resolved from the session working directory and from each literal ``cd`` in
the command. Shell scripts are included. A named script that exists but is too
large or unreadable to scan is treated as a write, so a skipped file cannot
slip past. The decision text redacts a 32-byte hex key and never includes the
command or file body.

It does not follow imports, expand globs, or resolve a ``cd`` target that is a
variable, command substitution or glob. A write hidden in a helper module, or
in a file the shell will name only after expansion, is not seen. Treat it as a
backstop, not as the approval.
"""
import json
import os
import re
import sys

# Signed actions from the Python SDK `Exchange` class and the TypeScript
# `ExchangeClient`: orders, transfers, margin modes, nonces and account changes.
WRITE_METHODS = (
    "order", "bulk_orders", "market_open", "market_close", "cancel", "bulk_cancel",
    "cancel_by_cloid", "bulk_cancel_by_cloid", "modify_order", "bulk_modify_orders_new",
    "update_leverage", "update_isolated_margin", "schedule_cancel", "usd_transfer",
    "spot_transfer", "usd_class_transfer", "sub_account_transfer", "vault_usd_transfer",
    "withdraw_from_bridge", "approve_agent", "approve_builder_fee", "send_asset",
    "sub_account_spot_transfer", "token_delegate", "create_sub_account", "user_set_abstraction",
    "agent_set_abstraction", "user_dex_abstraction", "agent_enable_dex_abstraction",
    "convert_to_multi_sig_user", "multi_sig", "use_big_blocks", "set_referrer", "noop",
    "batchModify", "cancelByCloid", "modify", "scheduleCancel", "twapOrder", "twapCancel",
    "updateLeverage", "updateIsolatedMargin", "usdSend", "spotSend", "withdraw3",
    "usdClassTransfer", "subAccountTransfer", "vaultTransfer", "approveAgent",
    "approveBuilderFee", "sendAsset", "agentSendAsset", "subAccountSpotTransfer", "tokenDelegate",
    "createSubAccount", "userSetAbstraction", "agentSetAbstraction", "userDexAbstraction",
    "agentEnableDexAbstraction", "topUpIsolatedOnlyMargin", "reserveRequestWeight", "cDeposit",
    "cWithdraw", "borrowLend", "sendToEvmWithData", "userPortfolioMargin", "convertToMultiSigUser",
    "setReferrer", "userOutcome", "claimRewards", "createVault", "vaultDistribute", "vaultModify",
    "subAccountModify", "hip3LiquidatorTransfer",
)
_METHODS = "|".join(WRITE_METHODS)
# The receiver has to look like an exchange client. Matching every `.order(`
# would ask on unrelated code; `ex` and `hl` are the short names that otherwise
# miss the `exch` requirement. `getattr` is matched separately.
SDK_WRITE_RE = re.compile(
    r"\b(?:\w*exch\w*|ex|hl)\s*\.\s*(" + _METHODS + r")\s*\(",
    re.IGNORECASE,
)
GETATTR_WRITE_RE = re.compile(
    r"""getattr\s*\(\s*\w+\s*,\s*["'](""" + _METHODS + r""")["']\s*\)\s*\(""",
    re.IGNORECASE,
)
# A raw POST: the API host's /exchange path, or a base URL joined to it.
# Hosts are case-insensitive, so `API.HYPERLIQUID.XYZ` is the same exchange.
# Built from pieces so this file's own source is not itself an /exchange match.
ENDPOINT_RE = re.compile(
    r"\.xyz" + r"/exchange\b|" + r"\}" + r"/exchange\b|" + r"\+\s*[\"']" + r"/exchange[\"']",
    re.IGNORECASE,
)
_SCRIPT_EXT = r"py|ts|mts|cts|js|mjs|cjs|sh|bash|zsh"
SCRIPT_RE = re.compile(
    rf"""(?:"([^"\n]+\.(?:{_SCRIPT_EXT}))"|'([^'\n]+\.(?:{_SCRIPT_EXT}))'|([\w./~-]+\.(?:{_SCRIPT_EXT}))\b)""",
    re.IGNORECASE,
)
CD_RE = re.compile(
    r"""(?:^|&&|\|\||;|\n)\s*cd\s+("[^"\n]+"|'[^'\n]+'|~?[^\s;&|]+)"""
)
KEY_RE = re.compile(r"0x[0-9a-fA-F]{64}")
# Writes a standing mandate may cover. Everything else in WRITE_METHODS asks.
MANDATE_METHODS = {name.lower() for name in (
    "order", "bulk_orders", "market_open", "market_close", "cancel", "bulk_cancel",
    "cancel_by_cloid", "bulk_cancel_by_cloid", "modify_order", "bulk_modify_orders_new",
    "update_leverage", "batchModify", "cancelByCloid", "modify", "updateLeverage",
)}
TESTNET_RE = re.compile(r"hyperliquid-testnet\.xyz|TESTNET_API_URL|isTestnet\s*:\s*true", re.IGNORECASE)
MAINNET_RE = re.compile(r"api\.hyperliquid\.xyz|MAINNET_API_URL|isTestnet\s*:\s*false", re.IGNORECASE)
ADDRESS_RE = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
TICKET_RE = re.compile(r"\b(HG-\d{8}-\d{2})(?:-[A-Z]+)?\b")
DESK_RECORD_WRITE_RE = re.compile(
    r">>?\s*['\"]?[^\s&|;'\"]*desk\.md|\btee\b|\bsed\s+-[a-zA-Z]*i|\bperl\s+-[a-zA-Z]*i|"
    r"\b(?:mv|cp|rm|truncate|ln|dd|install)\s|open\s*\(|write_text|writeFile|os\.replace|os\.rename"
)
EDIT_TOOLS = ("Write", "Edit", "MultiEdit")
MAX_SCRIPT_BYTES = 2_000_000
MAX_SCRIPTS = 32


class Unscanned(Exception):
    def __init__(self, token):
        self.token = token


def redact(text):
    return KEY_RE.sub("0x[redacted]", text)


def find_write(text):
    match = SDK_WRITE_RE.search(text) or GETATTR_WRITE_RE.search(text)
    if match:
        return match.group(1)
    if ENDPOINT_RE.search(text):
        return "a POST to /exchange"
    return None


def all_writes(text):
    """Every write in the text: (SDK method names, any write a mandate cannot cover)."""
    methods = {match.group(1).lower() for match in SDK_WRITE_RE.finditer(text)}
    uncoverable = bool(GETATTR_WRITE_RE.search(text) or ENDPOINT_RE.search(text))
    return methods, uncoverable


def _code_lines(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def _load_mandate_module():
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import standing_mandate  # noqa: E402  (stdlib-only module shipped beside the hook)
    return standing_mandate


def mandate_refusal(command, texts, uncoverable, methods):
    """None when a valid standing mandate covers this write; otherwise the reason it does not."""
    desk_root = os.environ.get("HYPERGROK_DESK_ROOT")
    if not desk_root:
        return "no standing mandate checked: HYPERGROK_DESK_ROOT is not set"
    if uncoverable:
        return "a raw /exchange post, getattr call or unscanned script cannot be checked against a standing mandate"
    outside = sorted(methods - MANDATE_METHODS)
    if outside:
        return f"{outside[0]}() is outside any standing mandate and always needs your approval"
    try:
        module = _load_mandate_module()
    except Exception:  # A missing or broken validator must not silence the gate.
        return "the standing mandate validator could not be loaded"
    code = _code_lines("\n".join(texts))
    networks = {name for name, pattern in (("testnet", TESTNET_RE), ("mainnet", MAINNET_RE)) if pattern.search(code)}
    if len(networks) != 1:
        return "the write does not name exactly one network, so it cannot be matched to a standing mandate"
    network = networks.pop()
    addresses = {address.lower() for address in ADDRESS_RE.findall(code)}
    try:
        report = module.evaluate(os.path.abspath(desk_root), network=network)
    except Exception:
        return "the standing mandate could not be read"
    if not report.get("in_force"):
        return f"no standing mandate in force for {network}: {'; '.join(report.get('reasons') or ['unknown'])}"
    mandate = report["mandate"]
    if addresses != {mandate["account"]}:
        return "the write must name the mandate's account and no other address"
    tickets = set(TICKET_RE.findall(command))
    if len(tickets) != 1:
        return "the command must name exactly one ticket id"
    proposal = os.path.join(os.path.abspath(desk_root), "proposals", f"{tickets.pop()}.md")
    try:
        with open(proposal, encoding="utf-8") as handle:
            record = handle.read()
    except OSError:
        return "the ticket's proposal file is missing"
    authority = f"authority: standing mandate {mandate['written']} sha {mandate['fingerprint']}"
    if authority not in record or "PASS" not in record:
        return "the ticket does not record a Risk PASS and the current standing mandate's authority line"
    return None


def desk_record_change(event):
    """Ask before a file edit adds or changes a standing mandate in desk.md."""
    tool_input = event.get("tool_input") or {}
    path = tool_input.get("file_path")
    if not isinstance(path, str) or os.path.basename(path) != "desk.md":
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
    except FileNotFoundError:
        before = ""
    except OSError:
        before = None
    tool = event.get("tool_name")
    if tool == "Write":
        after = tool_input.get("content") if isinstance(tool_input.get("content"), str) else ""
    else:
        edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
        after = before
        for edit in edits if isinstance(edits, list) else []:
            old, new = (edit or {}).get("old_string"), (edit or {}).get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or after is None or old not in after:
                after = None
                break
            after = after.replace(old, new) if edit.get("replace_all") else after.replace(old, new, 1)
        if after is None:
            text = " ".join(str((edit or {}).get("new_string", "")) for edit in (edits if isinstance(edits, list) else []))
            return "mandate" in text.lower()
    try:
        module = _load_mandate_module()
    except Exception:
        return "mandate" in after.lower()
    signature = module.block_signature(after)
    if signature[0] in ("none", "revoked"):
        return False
    return before is None or signature != module.block_signature(before)


def ask(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def script_tokens(command):
    tokens = []
    for match in SCRIPT_RE.finditer(command):
        token = next(part for part in match.groups() if part)
        if token not in tokens:
            tokens.append(token)
    return tokens


def directory_roots(command, cwd):
    """Literal `cd` targets only. A variable or glob stays unresolved."""
    roots = [cwd]
    current = cwd
    for match in CD_RE.finditer(command):
        raw = match.group(1)
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            raw = raw[1:-1]
        if not raw or raw == "-" or any(char in raw for char in "$`*?[]"):
            continue
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(current, path)
        path = os.path.normpath(path)
        current = path
        if path not in roots:
            roots.append(path)
    return roots


def candidate_paths(token, roots):
    path = os.path.expanduser(token)
    if os.path.isabs(path):
        return [os.path.normpath(path)]
    return [os.path.normpath(os.path.join(root, path)) for root in roots]


def named_scripts(command, cwd):
    tokens = script_tokens(command)
    if len(tokens) > MAX_SCRIPTS:
        raise Unscanned(f"{len(tokens)} named scripts")
    roots = directory_roots(command, cwd)
    seen = set()
    for token in tokens:
        for path in candidate_paths(token, roots):
            if path in seen:
                continue
            seen.add(path)
            try:
                if not os.path.isfile(path):
                    continue
                if os.path.getsize(path) > MAX_SCRIPT_BYTES:
                    raise Unscanned(token)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    yield token, handle.read()
            except OSError:
                raise Unscanned(token)


def decide(event):
    tool = event.get("tool_name")
    if tool in EDIT_TOOLS:
        if desk_record_change(event):
            return ask(
                "HyperGrok: this edit adds or changes the standing mandate in desk.md. "
                "Only you write, widen or renew a mandate; approve only if you are making this change yourself."
            )
        return None
    if tool != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command:
        return None
    if "desk.md" in command and DESK_RECORD_WRITE_RE.search(command):
        return ask(
            "HyperGrok: this command may change desk.md, which holds any standing mandate. "
            "Only you write, widen or renew a mandate; approve only if this is your own change."
        )
    found = find_write(command)
    where = "in the command"
    texts = [command]
    unscanned = False
    cwd = event.get("cwd") or os.getcwd()
    try:
        for token, text in named_scripts(command, cwd):
            texts.append(text)
            if not found:
                found = find_write(text)
                if found:
                    where = f"in {redact(token)}"
    except Unscanned as exc:
        unscanned = True
        if not found:
            found = "an unscanned script"
            where = f"in {redact(exc.token)}"
    if not found:
        return None
    methods, uncoverable = set(), unscanned
    for text in texts:
        text_methods, text_uncoverable = all_writes(text)
        methods |= text_methods
        uncoverable = uncoverable or text_uncoverable
    refusal = mandate_refusal(command, texts, uncoverable, methods)
    if refusal is None:
        return None
    if not found.startswith(("a ", "an ")):
        found = f"{found}()"
    return ask(
        f"HyperGrok: this looks like a Hyperliquid exchange write ({found} {where}). "
        "Approve only if it is the single send for a ticket with a Risk PASS "
        f"and your approval by that ticket id. Standing mandate: {redact(refusal)}."
    )


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    decision = decide(event)
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
