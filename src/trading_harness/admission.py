"""Deterministic send-time admission with no venue-write capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .domain import AuthorizationModel, SemanticIntent
from .errors import ValidationError
from .policy import HARD_PLATFORM_CEILINGS, ExposureQuote, PlatformCeilings
from .store import CommandRecord, SQLiteStore


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Everything needed to atomically queue one already-approved intent.

    ``ExposureQuote`` is only a caller assertion.  Admission deterministically
    derives the economics again from the semantic intent and requires exact
    equality before opening a persistence transaction.
    """

    intent: SemanticIntent
    exposure: ExposureQuote
    authorization_id: str
    command_id: str
    audience: str
    authorization_model: AuthorizationModel

    def __post_init__(self) -> None:
        if not isinstance(self.intent, SemanticIntent):
            raise TypeError("intent must be SemanticIntent")
        if not isinstance(self.exposure, ExposureQuote):
            raise TypeError("exposure must be ExposureQuote")
        if not isinstance(self.authorization_model, AuthorizationModel):
            try:
                object.__setattr__(
                    self,
                    "authorization_model",
                    AuthorizationModel(self.authorization_model),
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError("invalid authorization_model") from exc
        for field in ("authorization_id", "command_id", "audience"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValidationError(f"{field} must be a non-empty, trimmed string")


class AdmissionService:
    """Application boundary for one atomic, fail-closed admission decision."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        audience: str,
        ceilings: PlatformCeilings = HARD_PLATFORM_CEILINGS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be SQLiteStore")
        if not isinstance(audience, str) or not audience or audience != audience.strip():
            raise ValidationError("audience must be a non-empty, trimmed string")
        self._store = store
        self._audience = audience
        self._ceilings = ceilings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def admit(
        self, request: AdmissionRequest, *, now: datetime | None = None
    ) -> CommandRecord:
        """Queue one command or raise without leaving any partial state.

        This method cannot call a venue.  Success means only that the
        authorization is ``consuming``, risk is reserved, and a durable
        ``queued`` command plus ``pending`` outbox item exist in one commit.
        """

        if not isinstance(request, AdmissionRequest):
            raise TypeError("request must be AdmissionRequest")
        if request.audience != self._audience:
            # Refuse before touching persistence, while the store repeats the
            # check against the authorization record inside its transaction.
            from .errors import AdmissionDenied

            raise AdmissionDenied(
                "ADMISSION_AUDIENCE_MISMATCH",
                "request targets a different admission service",
            )
        decision_time = self._clock() if now is None else now
        return self._store.atomically_admit(
            intent=request.intent,
            quote=request.exposure,
            authorization_id=request.authorization_id,
            command_id=request.command_id,
            audience=self._audience,
            authorization_model=request.authorization_model,
            now=decision_time,
            ceilings=self._ceilings,
        )
