#!/usr/bin/env python3
"""Claude Code PreToolUse gate for Hyperliquid exchange writes. Stdlib only.

The ticket protocol (Risk PASS, approval by ticket id, one bounded send) is the
desk's control. This hook adds a mechanical second check in Claude Code: a Bash
command that looks like a signed `/exchange` request gets an "ask" decision, so
the user confirms it even in an auto-accepting session. Anything else gets no
opinion and follows the normal permission flow. The hook never approves.

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
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command:
        return None
    found = find_write(command)
    where = "in the command"
    if not found:
        cwd = event.get("cwd") or os.getcwd()
        try:
            for token, text in named_scripts(command, cwd):
                found = find_write(text)
                if found:
                    where = f"in {redact(token)}"
                    break
        except Unscanned as exc:
            found = "an unscanned script"
            where = f"in {redact(exc.token)}"
    if not found:
        return None
    if not found.startswith(("a ", "an ")):
        found = f"{found}()"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"HyperGrok: this looks like a Hyperliquid exchange write ({found} {where}). "
                "Approve only if it is the single send for a ticket with a Risk PASS "
                "and your approval by that ticket id."
            ),
        }
    }


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
