"""Typed failures raised by the deterministic trading harness.

The exception hierarchy deliberately separates a denied admission from a
storage conflict.  Callers may show the former to an approver, while the
latter must be reconciled or retried by deterministic control-plane code.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for expected harness failures."""


class ValidationError(HarnessError, ValueError):
    """A supplied value is malformed or violates a schema invariant."""


class AdmissionDenied(HarnessError):
    """Admission failed closed for a stable, machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PolicyViolation(AdmissionDenied):
    """A hard platform ceiling or a stricter account policy was exceeded."""


class StateConflict(HarnessError):
    """Persisted state does not permit the requested transition."""


class EntrySubmissionRevoked(HarnessError):
    """The runtime revoked an entry before its one-shot send authority."""


class RecordNotFound(HarnessError):
    """A required persisted record does not exist."""


class StorageError(HarnessError):
    """The durable store could not preserve the requested invariant."""
