from __future__ import annotations

import inspect
import json
import unittest

from trading_harness.credential_provider import (
    BoundedCommandResult,
    CredentialAddressMismatch,
    CredentialCommandUnavailable,
    CredentialDependencyError,
    CredentialMalformedError,
    CredentialNotFoundError,
    CredentialOutputError,
    CredentialPlatformError,
    CredentialTimeoutError,
    ETH_ACCOUNT_DISTRIBUTION,
    KeychainCredentialConfig,
    MAX_ERROR_OUTPUT_BYTES,
    MAX_SECRET_OUTPUT_BYTES,
    MacOSKeychainCredentialProvider,
    OFFICIAL_SDK_DISTRIBUTION,
    OFFICIAL_SDK_VERSION,
    SECP256K1_ORDER,
    SECURITY_EXECUTABLE,
)
from trading_harness import credential_provider
from trading_harness.errors import ValidationError


SERVICE = "com.jawndiego.trading-desk.testnet-signer"
ACCOUNT = "hyperliquid-api-wallet"
EXPECTED = "0x1111111111111111111111111111111111111111"
PRIVATE_KEY = "1" * 64


class FakeWallet:
    def __init__(self, address: str = EXPECTED) -> None:
        self.address = address


class FakeRunner:
    def __init__(self, result: BoundedCommandResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], float, int, int]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> BoundedCommandResult:
        self.calls.append((argv, timeout, stdout_limit, stderr_limit))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def versions(**changes: str):
    selected = {
        OFFICIAL_SDK_DISTRIBUTION: OFFICIAL_SDK_VERSION,
        ETH_ACCOUNT_DISTRIBUTION: "0.13.7",
    }
    selected.update(changes)

    def read(distribution: str) -> str:
        if distribution not in selected:
            raise LookupError("missing")
        return selected[distribution]

    return read


def provider(
    runner: FakeRunner,
    *,
    wallet_factory=lambda _key: FakeWallet(),
    version_reader=None,
    system: str = "Darwin",
) -> MacOSKeychainCredentialProvider:
    return MacOSKeychainCredentialProvider(
        KeychainCredentialConfig(SERVICE, ACCOUNT, EXPECTED),
        _runner=runner,
        _wallet_factory=wallet_factory,
        _version_reader=versions() if version_reader is None else version_reader,
        _platform_system=lambda: system,
    )


class SuccessfulLoadTests(unittest.TestCase):
    def test_uses_exact_argv_and_returns_only_verified_wallet(self) -> None:
        result = BoundedCommandResult(
            0,
            bytearray((PRIVATE_KEY + "\n").encode()),
            bytearray(),
        )
        runner = FakeRunner(result)
        received: list[str] = []

        def wallet_factory(key: str) -> FakeWallet:
            received.append(key)
            return FakeWallet()

        wallet = provider(runner, wallet_factory=wallet_factory).load_wallet()
        self.assertIsInstance(wallet, FakeWallet)
        self.assertEqual(wallet.address, EXPECTED)
        self.assertEqual(received, ["0x" + PRIVATE_KEY])
        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        SECURITY_EXECUTABLE,
                        "find-generic-password",
                        "-s",
                        SERVICE,
                        "-a",
                        ACCOUNT,
                        "-w",
                    ),
                    5.0,
                    MAX_SECRET_OUTPUT_BYTES,
                    MAX_ERROR_OUTPUT_BYTES,
                )
            ],
        )
        self.assertTrue(all(value == 0 for value in result.stdout))
        self.assertEqual(result.stderr, bytearray())

    def test_key_with_canonical_prefix_and_crlf_is_accepted(self) -> None:
        result = BoundedCommandResult(
            0,
            bytearray(("0x" + PRIVATE_KEY.upper() + "\r\n").encode()),
            bytearray(),
        )
        received: list[str] = []
        wallet = provider(
            FakeRunner(result),
            wallet_factory=lambda key: received.append(key) or FakeWallet(),
        ).load_wallet()
        self.assertIsInstance(wallet, FakeWallet)
        self.assertEqual(received, ["0x" + PRIVATE_KEY])

    def test_explicit_system_keychain_path_is_bound_into_argv(self) -> None:
        result = BoundedCommandResult(
            0, bytearray((PRIVATE_KEY + "\n").encode()), bytearray()
        )
        runner = FakeRunner(result)
        selected = MacOSKeychainCredentialProvider(
            KeychainCredentialConfig(
                SERVICE,
                ACCOUNT,
                EXPECTED,
                keychain_path="/Library/Keychains/System.keychain",
            ),
            _runner=runner,
            _wallet_factory=lambda _key: FakeWallet(),
            _version_reader=versions(),
            _platform_system=lambda: "Darwin",
        )

        selected.load_wallet()

        self.assertEqual(
            "/Library/Keychains/System.keychain",
            runner.calls[0][0][-1],
        )

    def test_status_is_static_redacted_and_performs_no_lookup(self) -> None:
        runner = FakeRunner(AssertionError("must not run"))
        selected = provider(runner)
        status = selected.status().as_dict()
        encoded = json.dumps(status, sort_keys=True)
        self.assertEqual(runner.calls, [])
        for sensitive in (SERVICE, ACCOUNT, EXPECTED, PRIVATE_KEY):
            self.assertNotIn(sensitive, encoded)
        self.assertFalse(status["credential_loaded"])
        self.assertFalse(status["secret_exposed"])
        self.assertFalse(status["provisioning_supported"])
        self.assertFalse(status["write_supported"])
        self.assertEqual(status["security_executable"], "/usr/bin/security")


class FailureTests(unittest.TestCase):
    def test_non_darwin_fails_before_dependency_or_command_access(self) -> None:
        runner = FakeRunner(AssertionError("must not run"))
        with self.assertRaises(CredentialPlatformError):
            provider(runner, system="Linux").load_wallet()
        self.assertEqual(runner.calls, [])

    def test_missing_command_and_item_are_sanitized(self) -> None:
        missing_command = FakeRunner(FileNotFoundError(PRIVATE_KEY))
        with self.assertRaises(CredentialCommandUnavailable) as command_error:
            provider(missing_command).load_wallet()
        self.assertNotIn(PRIVATE_KEY, str(command_error.exception))

        result = BoundedCommandResult(
            44,
            bytearray(PRIVATE_KEY.encode()),
            bytearray(("item missing " + PRIVATE_KEY).encode()),
        )
        with self.assertRaises(CredentialNotFoundError) as item_error:
            provider(FakeRunner(result)).load_wallet()
        self.assertNotIn(PRIVATE_KEY, str(item_error.exception))
        self.assertTrue(all(value == 0 for value in result.stdout))
        self.assertTrue(all(value == 0 for value in result.stderr))

    def test_timeout_and_oversized_output_fail_closed(self) -> None:
        timeout = FakeRunner(CredentialTimeoutError("credential lookup timed out"))
        with self.assertRaises(CredentialTimeoutError):
            provider(timeout).load_wallet()

        result = BoundedCommandResult(
            0,
            bytearray(b"a" * (MAX_SECRET_OUTPUT_BYTES + 1)),
            bytearray(),
        )
        with self.assertRaises(CredentialOutputError):
            provider(FakeRunner(result)).load_wallet()
        self.assertTrue(all(value == 0 for value in result.stdout))

    def test_malformed_private_key_forms_are_rejected_without_wallet_call(self) -> None:
        malformed = (
            b"",
            b"1" * 63,
            b"g" * 64,
            b"0" * 64,
            f"{SECP256K1_ORDER:064x}".encode(),
            (b"1" * 64) + b"\nextra",
            b" " + (b"1" * 64),
            b"\xff" * 64,
        )
        for index, raw in enumerate(malformed):
            with self.subTest(index=index):
                calls: list[str] = []
                result = BoundedCommandResult(0, bytearray(raw), bytearray())
                with self.assertRaises(CredentialMalformedError):
                    provider(
                        FakeRunner(result),
                        wallet_factory=lambda key: calls.append(key) or FakeWallet(),
                    ).load_wallet()
                self.assertEqual(calls, [])
                self.assertTrue(all(value == 0 for value in result.stdout))

    def test_wallet_factory_error_and_wrong_address_do_not_disclose_secret(self) -> None:
        result = BoundedCommandResult(0, bytearray(PRIVATE_KEY.encode()), bytearray())

        def fail(key: str) -> object:
            raise ValueError("bad key " + key)

        with self.assertRaises(CredentialMalformedError) as error:
            provider(FakeRunner(result), wallet_factory=fail).load_wallet()
        self.assertNotIn(PRIVATE_KEY, str(error.exception))
        self.assertIsNone(error.exception.__cause__)

        wrong = BoundedCommandResult(0, bytearray(PRIVATE_KEY.encode()), bytearray())
        with self.assertRaises(CredentialAddressMismatch) as address_error:
            provider(
                FakeRunner(wrong),
                wallet_factory=lambda _key: FakeWallet("0x" + "2" * 40),
            ).load_wallet()
        self.assertNotIn("0x" + "2" * 40, str(address_error.exception))
        self.assertTrue(all(value == 0 for value in wrong.stdout))

    def test_sdk_and_eth_account_versions_are_checked_before_keychain(self) -> None:
        cases = (
            versions(**{OFFICIAL_SDK_DISTRIBUTION: "0.23.0"}),
            versions(**{ETH_ACCOUNT_DISTRIBUTION: "0.9.9"}),
            versions(**{ETH_ACCOUNT_DISTRIBUTION: "0.14.0"}),
            versions(**{ETH_ACCOUNT_DISTRIBUTION: "0.13.0rc1"}),
            lambda _distribution: (_ for _ in ()).throw(LookupError("missing")),
        )
        for index, reader in enumerate(cases):
            with self.subTest(index=index):
                runner = FakeRunner(AssertionError("must not run"))
                with self.assertRaises(CredentialDependencyError):
                    provider(runner, version_reader=reader).load_wallet()
                self.assertEqual(runner.calls, [])

    def test_invalid_configuration_is_rejected(self) -> None:
        for service, account, address, timeout in (
            ("-malicious", ACCOUNT, EXPECTED, 5),
            (SERVICE, "", EXPECTED, 5),
            (SERVICE, ACCOUNT, "0x1234", 5),
            (SERVICE, ACCOUNT, EXPECTED, 0),
            (SERVICE, ACCOUNT, EXPECTED, 11),
        ):
            with self.subTest(service=service, account=account, timeout=timeout):
                with self.assertRaises((TypeError, ValidationError)):
                    KeychainCredentialConfig(service, account, address, timeout)


class StaticCapabilityTests(unittest.TestCase):
    def test_module_has_no_shell_environment_file_or_provisioning_path(self) -> None:
        source = inspect.getsource(credential_provider)
        for forbidden in (
            "shell=True",
            "os.environ",
            "os.getenv",
            "getenv(",
            "Path(",
            ".open(",
            "set-generic-password",
            "add-generic-password",
            "delete-generic-password",
        ):
            self.assertNotIn(forbidden, source)
        selected = provider(FakeRunner(AssertionError("unused")))
        for name in ("provision", "write", "delete", "export_secret", "load_from_file"):
            self.assertFalse(hasattr(selected, name))


if __name__ == "__main__":
    unittest.main()
