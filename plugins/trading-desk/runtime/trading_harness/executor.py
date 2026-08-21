"""Fail-closed execution boundary for the foundation release.

This module intentionally contains no venue SDK, network client, credential
loader, or enabled adapter.  The only adapter shipped by the project rejects
every request.  A later execution implementation must live behind this narrow
boundary and pass a separate safety review before it can be selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Protocol, runtime_checkable


class VenueWriteDisabled(PermissionError):
    """Raised when code attempts a venue mutation in the disabled harness."""


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    """Non-sensitive description of the configured execution boundary."""

    adapter: str
    venue_writes_enabled: bool
    credential_loading_enabled: bool
    reason: str

    def as_dict(self) -> dict[str, str | bool]:
        """Return a JSON-safe status mapping."""

        return {
            "adapter": self.adapter,
            "venue_writes_enabled": self.venue_writes_enabled,
            "credential_loading_enabled": self.credential_loading_enabled,
            "reason": self.reason,
        }


@runtime_checkable
class VenueAdapter(Protocol):
    """Minimal boundary that a future venue implementation would satisfy."""

    @property
    def status(self) -> ExecutionStatus:
        """Describe whether the adapter can mutate venue state."""

    def write(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Attempt a venue mutation.

        The foundation adapter specializes this return type to ``NoReturn``
        because all writes are disabled.
        """


class DisabledVenueAdapter:
    """Null adapter that fails closed for every venue write."""

    _REASON = (
        "venue writes are disabled: this foundation contains no live trading "
        "adapter"
    )
    _STATUS = ExecutionStatus(
        adapter="disabled",
        venue_writes_enabled=False,
        credential_loading_enabled=False,
        reason=_REASON,
    )

    @property
    def status(self) -> ExecutionStatus:
        return self._STATUS

    def write(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> NoReturn:
        """Reject ``operation`` without reading or logging its payload."""

        # Do not interpolate the operation or payload into this exception.  A
        # rejected request can contain account or strategy data, and failure
        # messages must not turn into an accidental disclosure channel.
        del operation, payload
        raise VenueWriteDisabled(self._REASON)


class Executor:
    """Application-facing execution boundary, unconditionally disabled.

    The foundation constructor accepts no adapter.  An enabled implementation
    therefore cannot be smuggled in through dependency injection; adding one
    requires a later source change and separate safety review.
    """

    def __init__(self) -> None:
        self._adapter = DisabledVenueAdapter()

    @property
    def status(self) -> ExecutionStatus:
        return self._adapter.status

    def write(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Pass a mutation to the configured boundary.

        With the default (and only shipped) adapter this always raises
        :class:`VenueWriteDisabled`.
        """

        # Delegating without traversing the mapping ensures the disabled
        # adapter rejects even a lazy or malformed payload before it is read.
        request: Mapping[str, Any] = {} if payload is None else payload
        return self._adapter.write(operation, request)


def disabled_executor() -> Executor:
    """Construct the explicit fail-closed executor used by the application."""

    return Executor()
