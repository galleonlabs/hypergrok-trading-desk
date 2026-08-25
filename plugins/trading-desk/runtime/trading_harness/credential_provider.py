"""Isolated, read-only macOS Keychain credential provider.

The provider has one capability: retrieve an API-wallet private key from a
generic-password item and return an ``eth-account`` wallet whose public address
matches the configured signer.  It has no provisioning, update, delete,
environment-variable, plaintext-file, logging, network, or signing endpoint.

The ``security`` process is invoked with a fixed executable and argv list;
there is no shell.  Both output streams and execution time are bounded.  Raw
secret bytes are held in mutable buffers and overwritten in ``finally``.  No
error or status value includes command output or the private key.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
import os
import platform
import re
import selectors
import subprocess
import time
from typing import Callable, Protocol

from .canonical import domain_hash
from .errors import HarnessError, ValidationError
from .hyperliquid_signer import (
    OFFICIAL_SDK_DISTRIBUTION,
    OFFICIAL_SDK_VERSION,
)


SECURITY_EXECUTABLE = "/usr/bin/security"
ETH_ACCOUNT_DISTRIBUTION = "eth-account"
ETH_ACCOUNT_MIN_VERSION = (0, 10, 0)
ETH_ACCOUNT_MAX_VERSION = (0, 14, 0)
MAX_SECRET_OUTPUT_BYTES = 256
MAX_ERROR_OUTPUT_BYTES = 4_096
SECP256K1_ORDER = int(
    "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141",
    16,
)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRIVATE_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class CredentialProviderError(HarnessError):
    """Base class for sanitized credential-provider failures."""


class CredentialPlatformError(CredentialProviderError):
    pass


class CredentialDependencyError(CredentialProviderError):
    pass


class CredentialCommandUnavailable(CredentialProviderError):
    pass


class CredentialNotFoundError(CredentialProviderError):
    pass


class CredentialTimeoutError(CredentialProviderError):
    pass


class CredentialOutputError(CredentialProviderError):
    pass


class CredentialMalformedError(CredentialProviderError):
    pass


class CredentialAddressMismatch(CredentialProviderError):
    pass


class WalletFactory(Protocol):
    def __call__(self, key: str) -> object: ...


class VersionReader(Protocol):
    def __call__(self, distribution: str) -> str: ...


@dataclass(slots=True)
class BoundedCommandResult:
    returncode: int
    stdout: bytearray
    stderr: bytearray


CommandRunner = Callable[
    [tuple[str, ...], float, int, int], BoundedCommandResult
]


def _text(value: object, field: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("-")
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} must be a bounded, trimmed Keychain label")
    return value


def _address(value: object) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValidationError("expected_signer_address must be a 20-byte address")
    return value.lower()


def _keychain_path(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError("keychain_path is invalid")
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValidationError("keychain_path must be normalized and absolute")
    return value


def _zero(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_argv_bounded(
    argv: tuple[str, ...],
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> BoundedCommandResult:
    """Run one fixed argv command with bounded pipes and no shell."""

    if (
        not isinstance(argv, tuple)
        or not argv
        or argv[0] != SECURITY_EXECUTABLE
        or any(not isinstance(value, str) or not value for value in argv)
    ):
        raise CredentialCommandUnavailable("credential command is unavailable")
    if not 0 < timeout_seconds <= 10:
        raise CredentialTimeoutError("credential lookup timeout is invalid")
    if stdout_limit != MAX_SECRET_OUTPUT_BYTES or stderr_limit != MAX_ERROR_OUTPUT_BYTES:
        raise CredentialOutputError("credential output bounds are invalid")
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    except (FileNotFoundError, PermissionError, OSError):
        raise CredentialCommandUnavailable("credential command is unavailable") from None
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        _stop_process(process)
        raise CredentialCommandUnavailable("credential command pipes are unavailable")

    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_seconds
    try:
        selector.register(process.stdout, selectors.EVENT_READ, (stdout, stdout_limit))
        selector.register(process.stderr, selectors.EVENT_READ, (stderr, stderr_limit))
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise CredentialTimeoutError("credential lookup timed out")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                # Pipes may still have EOF/readable bytes; continue through one
                # selector iteration rather than trusting process exit alone.
                events = selector.select(0)
            for key, _ in events:
                target, limit = key.data
                try:
                    chunk = os.read(key.fileobj.fileno(), min(4096, limit + 1))
                except OSError:
                    _stop_process(process)
                    raise CredentialOutputError("credential command output failed") from None
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > limit:
                    _stop_process(process)
                    raise CredentialOutputError("credential command output exceeded limit")
        remaining = max(0.001, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise CredentialTimeoutError("credential lookup timed out") from None
        return BoundedCommandResult(returncode, stdout, stderr)
    except Exception:
        _stop_process(process)
        _zero(stdout)
        _zero(stderr)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


@dataclass(frozen=True, slots=True)
class KeychainCredentialConfig:
    service: str
    account: str
    expected_signer_address: str
    timeout_seconds: int = 5
    keychain_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", _text(self.service, "service"))
        object.__setattr__(self, "account", _text(self.account, "account"))
        object.__setattr__(
            self,
            "expected_signer_address",
            _address(self.expected_signer_address),
        )
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 10:
            raise ValidationError("timeout_seconds must be an integer from 1 to 10")
        object.__setattr__(self, "keychain_path", _keychain_path(self.keychain_path))


@dataclass(frozen=True, slots=True)
class CredentialProviderStatus:
    provider: str
    configured: bool
    service_fingerprint: str
    account_fingerprint: str
    signer_fingerprint: str
    security_executable: str
    sdk_requirement: str
    wallet_dependency_requirement: str
    credential_loaded: bool = False
    secret_exposed: bool = False
    provisioning_supported: bool = False
    write_supported: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "service_fingerprint": self.service_fingerprint,
            "account_fingerprint": self.account_fingerprint,
            "signer_fingerprint": self.signer_fingerprint,
            "security_executable": self.security_executable,
            "sdk_requirement": self.sdk_requirement,
            "wallet_dependency_requirement": self.wallet_dependency_requirement,
            "credential_loaded": self.credential_loaded,
            "secret_exposed": self.secret_exposed,
            "provisioning_supported": self.provisioning_supported,
            "write_supported": self.write_supported,
        }


def _parse_version(value: object, distribution: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise CredentialDependencyError("credential dependency version is unavailable")
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise CredentialDependencyError("credential dependency version is unsupported")
    del distribution
    return tuple(int(component) for component in match.groups())  # type: ignore[return-value]


def _load_default_wallet_factory() -> tuple[WalletFactory, type[object]]:
    try:
        from eth_account import Account
        from eth_account.signers.local import LocalAccount
        from hyperliquid.utils.signing import sign_l1_action
    except (ImportError, ModuleNotFoundError):
        raise CredentialDependencyError("credential wallet dependencies are unavailable") from None
    factory = getattr(Account, "from_key", None)
    if not callable(factory) or not callable(sign_l1_action):
        raise CredentialDependencyError("credential wallet dependencies are invalid")
    if not isinstance(LocalAccount, type):
        raise CredentialDependencyError("credential wallet type is invalid")
    return factory, LocalAccount


class MacOSKeychainCredentialProvider:
    """Load one expected API-wallet identity from macOS Keychain."""

    def __init__(
        self,
        config: KeychainCredentialConfig,
        *,
        _runner: CommandRunner = run_argv_bounded,
        _wallet_factory: WalletFactory | None = None,
        _version_reader: VersionReader = importlib_metadata.version,
        _platform_system: Callable[[], str] = platform.system,
    ) -> None:
        if not isinstance(config, KeychainCredentialConfig):
            raise TypeError("config must be KeychainCredentialConfig")
        for field, value in (
            ("runner", _runner),
            ("version_reader", _version_reader),
            ("platform_system", _platform_system),
        ):
            if not callable(value):
                raise TypeError(f"{field} must be callable")
        if _wallet_factory is not None and not callable(_wallet_factory):
            raise TypeError("wallet_factory must be callable")
        self._config = config
        self._runner = _runner
        self._wallet_factory = _wallet_factory
        self._version_reader = _version_reader
        self._platform_system = _platform_system

    def status(self) -> CredentialProviderStatus:
        return CredentialProviderStatus(
            provider="macos_keychain_generic_password",
            configured=True,
            service_fingerprint=domain_hash(
                "trading-harness/keychain-service/v1", self._config.service
            ),
            account_fingerprint=domain_hash(
                "trading-harness/keychain-account/v1", self._config.account
            ),
            signer_fingerprint=domain_hash(
                "trading-harness/expected-signer/v1",
                self._config.expected_signer_address,
            ),
            security_executable=SECURITY_EXECUTABLE,
            sdk_requirement=f"{OFFICIAL_SDK_DISTRIBUTION}=={OFFICIAL_SDK_VERSION}",
            wallet_dependency_requirement="eth-account>=0.10.0,<0.14.0",
        )

    def _dependencies(self) -> tuple[WalletFactory, type[object] | None]:
        try:
            sdk_version = self._version_reader(OFFICIAL_SDK_DISTRIBUTION)
            account_version = self._version_reader(ETH_ACCOUNT_DISTRIBUTION)
        except Exception:
            raise CredentialDependencyError(
                "credential dependency version is unavailable"
            ) from None
        if sdk_version != OFFICIAL_SDK_VERSION:
            raise CredentialDependencyError("credential SDK version is unsupported")
        parsed_account = _parse_version(account_version, ETH_ACCOUNT_DISTRIBUTION)
        if not ETH_ACCOUNT_MIN_VERSION <= parsed_account < ETH_ACCOUNT_MAX_VERSION:
            raise CredentialDependencyError(
                "credential wallet dependency version is unsupported"
            )
        if self._wallet_factory is not None:
            return self._wallet_factory, None
        return _load_default_wallet_factory()

    @staticmethod
    def _normalize_key(buffer: bytearray) -> str:
        raw = bytes(buffer)
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or b"\n" in raw or b"\r" in raw:
            raise CredentialMalformedError("Keychain credential format is invalid")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise CredentialMalformedError("Keychain credential format is invalid") from None
        if not _PRIVATE_KEY_RE.fullmatch(text):
            raise CredentialMalformedError("Keychain credential format is invalid")
        digits = text[2:] if text.startswith("0x") else text
        scalar = int(digits, 16)
        if not 1 <= scalar < SECP256K1_ORDER:
            raise CredentialMalformedError("Keychain credential scalar is invalid")
        return "0x" + digits.lower()

    def load_wallet(self) -> object:
        if self._platform_system() != "Darwin":
            raise CredentialPlatformError("macOS Keychain provider requires Darwin")
        wallet_factory, expected_wallet_type = self._dependencies()
        argv = (
            SECURITY_EXECUTABLE,
            "find-generic-password",
            "-s",
            self._config.service,
            "-a",
            self._config.account,
            "-w",
            *((self._config.keychain_path,) if self._config.keychain_path else ()),
        )
        try:
            result = self._runner(
                argv,
                float(self._config.timeout_seconds),
                MAX_SECRET_OUTPUT_BYTES,
                MAX_ERROR_OUTPUT_BYTES,
            )
        except CredentialProviderError:
            raise
        except (FileNotFoundError, PermissionError, OSError):
            raise CredentialCommandUnavailable(
                "credential command is unavailable"
            ) from None
        except Exception:
            raise CredentialCommandUnavailable("credential lookup failed") from None
        if not isinstance(result, BoundedCommandResult):
            raise CredentialOutputError("credential command returned invalid output")
        try:
            if (
                len(result.stdout) > MAX_SECRET_OUTPUT_BYTES
                or len(result.stderr) > MAX_ERROR_OUTPUT_BYTES
            ):
                raise CredentialOutputError("credential command output exceeded limit")
            if type(result.returncode) is not int or result.returncode != 0:
                raise CredentialNotFoundError("Keychain credential is unavailable")
            normalized = self._normalize_key(result.stdout)
            try:
                wallet = wallet_factory(normalized)
            except Exception:
                raise CredentialMalformedError(
                    "Keychain credential could not construct a wallet"
                ) from None
            if expected_wallet_type is not None and not isinstance(
                wallet, expected_wallet_type
            ):
                raise CredentialMalformedError(
                    "credential dependency returned an unexpected wallet type"
                )
            try:
                derived = getattr(wallet, "address")
            except Exception:
                raise CredentialMalformedError(
                    "constructed wallet has no valid public address"
                ) from None
            if not isinstance(derived, str) or not _ADDRESS_RE.fullmatch(derived):
                raise CredentialMalformedError(
                    "constructed wallet has no valid public address"
                )
            if derived.lower() != self._config.expected_signer_address:
                raise CredentialAddressMismatch(
                    "constructed wallet does not match expected signer"
                )
            return wallet
        finally:
            _zero(result.stdout)
            _zero(result.stderr)


__all__ = (
    "BoundedCommandResult",
    "CredentialAddressMismatch",
    "CredentialCommandUnavailable",
    "CredentialDependencyError",
    "CredentialMalformedError",
    "CredentialNotFoundError",
    "CredentialOutputError",
    "CredentialPlatformError",
    "CredentialProviderError",
    "CredentialProviderStatus",
    "CredentialTimeoutError",
    "KeychainCredentialConfig",
    "MacOSKeychainCredentialProvider",
    "run_argv_bounded",
)
