#!/usr/bin/env python3
"""Claude Code PreToolUse gate for Hyperliquid exchange writes. Stdlib only.

The ticket protocol (Risk PASS, approval by ticket id, one bounded send) is the
desk's control. This hook adds a mechanical second check in Claude Code: a Bash
command that looks like a signed `/exchange` request gets an "ask" decision, so
the user confirms it even in an auto-accepting session. Anything else gets no
opinion and follows the normal permission flow. The hook never approves.

It reads the command text and any script file the command names (resolved from
the session's working directory). It does not follow imports, so a write hidden
in a helper module is not seen. Treat it as a backstop, not as the approval.
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
SDK_WRITE_RE = re.compile(
    r"\b\w*exch\w*\s*\.\s*(" + "|".join(WRITE_METHODS) + r")\s*\(",
    re.IGNORECASE,
)
# A raw POST: the API host's /exchange path, or a base URL joined to it.
ENDPOINT_RE = re.compile(r"\.xyz/exchange\b|\}/exchange\b|\+\s*[\"']/exchange[\"']")
SCRIPT_RE = re.compile(r"[\w./~-]+\.(?:py|ts|mts|cts|js|mjs|cjs)\b")
MAX_SCRIPT_BYTES = 2_000_000


def find_write(text):
    match = SDK_WRITE_RE.search(text)
    if match:
        return match.group(0).rstrip("( ")
    match = ENDPOINT_RE.search(text)
    if match:
        return "a POST to /exchange"
    return None


def named_scripts(command, cwd):
    for token in SCRIPT_RE.findall(command):
        path = os.path.expanduser(token)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= MAX_SCRIPT_BYTES:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    yield token, handle.read()
        except OSError:
            continue


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
        for token, text in named_scripts(command, cwd):
            found = find_write(text)
            if found:
                where = f"in {token}"
                break
    if not found:
        return None
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
