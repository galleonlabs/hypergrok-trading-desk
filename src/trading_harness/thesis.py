"""Versioned thesis evidence state, kept separate from deployment authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from .domain import DeploymentGrant, SemanticIntent, _instant, _text


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class EvidenceStatus(_StringEnum):
    DRAFT = "draft"
    REGISTERED = "registered"
    EXPLORATORY_TESTED = "exploratory_tested"
    HOLDOUT_PASSED = "holdout_passed"
    SHADOW_CONFIRMED = "shadow_confirmed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class EvidenceTransitionError(ValueError):
    """Raised when a review attempts an illegal evidence-state transition."""


_EVIDENCE_TRANSITIONS: dict[EvidenceStatus, frozenset[EvidenceStatus]] = {
    EvidenceStatus.DRAFT: frozenset(
        {EvidenceStatus.REGISTERED, EvidenceStatus.REJECTED, EvidenceStatus.RETIRED}
    ),
    EvidenceStatus.REGISTERED: frozenset(
        {
            EvidenceStatus.EXPLORATORY_TESTED,
            EvidenceStatus.REJECTED,
            EvidenceStatus.INCONCLUSIVE,
            EvidenceStatus.RETIRED,
        }
    ),
    EvidenceStatus.EXPLORATORY_TESTED: frozenset(
        {
            EvidenceStatus.HOLDOUT_PASSED,
            EvidenceStatus.REJECTED,
            EvidenceStatus.INCONCLUSIVE,
            EvidenceStatus.RETIRED,
        }
    ),
    EvidenceStatus.HOLDOUT_PASSED: frozenset(
        {
            EvidenceStatus.SHADOW_CONFIRMED,
            EvidenceStatus.REJECTED,
            EvidenceStatus.INCONCLUSIVE,
            EvidenceStatus.RETIRED,
        }
    ),
    EvidenceStatus.SHADOW_CONFIRMED: frozenset(
        {
            EvidenceStatus.VALIDATED,
            EvidenceStatus.REJECTED,
            EvidenceStatus.INCONCLUSIVE,
            EvidenceStatus.RETIRED,
        }
    ),
    EvidenceStatus.VALIDATED: frozenset(
        {EvidenceStatus.SUSPENDED, EvidenceStatus.RETIRED}
    ),
    EvidenceStatus.SUSPENDED: frozenset(
        {EvidenceStatus.VALIDATED, EvidenceStatus.RETIRED}
    ),
    EvidenceStatus.REJECTED: frozenset({EvidenceStatus.RETIRED}),
    EvidenceStatus.INCONCLUSIVE: frozenset({EvidenceStatus.RETIRED}),
    EvidenceStatus.RETIRED: frozenset(),
}


def _status(value: Any, field_name: str = "evidence_status") -> EvidenceStatus:
    if isinstance(value, EvidenceStatus):
        return value
    if isinstance(value, str):
        try:
            return EvidenceStatus(value)
        except ValueError as error:
            raise ValueError(f"invalid {field_name}: {value!r}") from error
    raise TypeError(f"{field_name} must be EvidenceStatus or str")


@dataclass(frozen=True, slots=True)
class ThesisVersion:
    """One immutable, code-bound version of a falsifiable thesis.

    ``evidence_status`` reports what the research supports.  It deliberately
    carries no venue permission or deployment flag; those live in independent
    :class:`DeploymentGrant` records.
    """

    thesis_id: str
    thesis_version: str
    strategy_version: str
    code_hash: str
    author_id: str
    title: str
    rationale: str
    created_at: datetime
    evidence_status: EvidenceStatus = EvidenceStatus.DRAFT
    specification_hash: str | None = None
    supersedes_version: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "thesis_id",
            "thesis_version",
            "strategy_version",
            "code_hash",
            "author_id",
            "title",
            "rationale",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "created_at", _instant(self.created_at, "created_at"))
        object.__setattr__(
            self, "evidence_status", _status(self.evidence_status)
        )
        if self.specification_hash is not None:
            object.__setattr__(
                self,
                "specification_hash",
                _text(self.specification_hash, "specification_hash"),
            )
        if self.supersedes_version is not None:
            object.__setattr__(
                self,
                "supersedes_version",
                _text(self.supersedes_version, "supersedes_version"),
            )
            if self.supersedes_version == self.thesis_version:
                raise ValueError("a thesis version cannot supersede itself")

    @property
    def is_validated(self) -> bool:
        return self.evidence_status is EvidenceStatus.VALIDATED

    def supersede(
        self,
        *,
        thesis_version: str,
        strategy_version: str,
        code_hash: str,
        created_at: datetime,
        specification_hash: str | None = None,
        author_id: str | None = None,
    ) -> "ThesisVersion":
        """Create a new draft after any material strategy or model change."""

        return ThesisVersion(
            thesis_id=self.thesis_id,
            thesis_version=thesis_version,
            strategy_version=strategy_version,
            code_hash=code_hash,
            author_id=self.author_id if author_id is None else author_id,
            title=self.title,
            rationale=self.rationale,
            created_at=created_at,
            evidence_status=EvidenceStatus.DRAFT,
            specification_hash=specification_hash,
            supersedes_version=self.thesis_version,
        )


@dataclass(frozen=True, slots=True)
class EvidenceReview:
    """Auditable evidence-state decision made by a named reviewer."""

    review_id: str
    thesis_id: str
    thesis_version: str
    code_hash: str
    from_status: EvidenceStatus
    to_status: EvidenceStatus
    reviewer_id: str
    reviewed_at: datetime
    evidence_artifact_hash: str
    reason: str

    def __post_init__(self) -> None:
        for field_name in (
            "review_id",
            "thesis_id",
            "thesis_version",
            "code_hash",
            "reviewer_id",
            "evidence_artifact_hash",
            "reason",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "from_status", _status(self.from_status, "from_status"))
        object.__setattr__(self, "to_status", _status(self.to_status, "to_status"))
        object.__setattr__(
            self, "reviewed_at", _instant(self.reviewed_at, "reviewed_at")
        )


def allowed_evidence_transitions(status: EvidenceStatus) -> frozenset[EvidenceStatus]:
    """Return the immutable next-state set for a status."""

    return _EVIDENCE_TRANSITIONS[_status(status)]


def apply_evidence_review(
    thesis: ThesisVersion, review: EvidenceReview
) -> ThesisVersion:
    """Apply a matching, legal review and return a new thesis snapshot."""

    if not isinstance(thesis, ThesisVersion):
        raise TypeError("thesis must be ThesisVersion")
    if not isinstance(review, EvidenceReview):
        raise TypeError("review must be EvidenceReview")
    if (
        review.thesis_id != thesis.thesis_id
        or review.thesis_version != thesis.thesis_version
        or review.code_hash != thesis.code_hash
    ):
        raise EvidenceTransitionError("review does not match the exact thesis version")
    if review.from_status is not thesis.evidence_status:
        raise EvidenceTransitionError("review from_status is stale")
    if review.to_status not in _EVIDENCE_TRANSITIONS[thesis.evidence_status]:
        raise EvidenceTransitionError(
            f"illegal evidence transition: {thesis.evidence_status.value} -> "
            f"{review.to_status.value}"
        )
    if review.reviewed_at < thesis.created_at:
        raise EvidenceTransitionError("review cannot predate the thesis version")
    if (
        review.to_status is EvidenceStatus.VALIDATED
        and review.reviewer_id == thesis.author_id
    ):
        raise EvidenceTransitionError("a thesis author cannot validate their own evidence")
    return replace(thesis, evidence_status=review.to_status)


@dataclass(frozen=True, slots=True)
class DeploymentEligibility:
    """Deterministic explanation of the evidence-and-authority gate."""

    eligible: bool
    reasons: tuple[str, ...]


def assess_deployment(
    thesis: ThesisVersion,
    grant: DeploymentGrant | None,
    *,
    at: datetime,
    intent: SemanticIntent | None = None,
) -> DeploymentEligibility:
    """Require both validated evidence and a live, exactly scoped grant."""

    if not isinstance(thesis, ThesisVersion):
        raise TypeError("thesis must be ThesisVersion")
    at = _instant(at, "at")
    reasons: list[str] = []
    if thesis.evidence_status is not EvidenceStatus.VALIDATED:
        reasons.append("evidence_not_validated")
    if grant is None:
        reasons.append("deployment_grant_missing")
        return DeploymentEligibility(False, tuple(reasons))
    if not isinstance(grant, DeploymentGrant):
        raise TypeError("grant must be DeploymentGrant or None")
    if (
        grant.thesis_id != thesis.thesis_id
        or grant.thesis_version != thesis.thesis_version
        or grant.strategy_version != thesis.strategy_version
        or grant.code_hash != thesis.code_hash
    ):
        reasons.append("deployment_grant_version_mismatch")
    if not grant.is_active(at):
        reasons.append("deployment_grant_inactive")
    if intent is not None:
        if not isinstance(intent, SemanticIntent):
            raise TypeError("intent must be SemanticIntent or None")
        if not grant.matches_scope(intent):
            reasons.append("intent_outside_deployment_scope")
    return DeploymentEligibility(not reasons, tuple(reasons))
