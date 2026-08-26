"""Strict filesystem transport for signed TESTNET learning grants."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .execution_grant import (
    SignedInfrastructureGrant,
    TestnetInfrastructureGrantAuthority,
    TrustedInfrastructureGrant,
    signed_infrastructure_grant_from_dict,
)


MAX_GRANT_ARTIFACT_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("signed grant JSON contains a duplicate field")
        result[key] = value
    return result


def _reject_float(_value: str) -> object:
    raise ValidationError("signed grant JSON floats are forbidden")


def _reject_constant(_value: str) -> object:
    raise ValidationError("signed grant JSON non-finite values are forbidden")


def load_signed_infrastructure_grant(
    path: str | os.PathLike[str],
) -> SignedInfrastructureGrant:
    """Read one process- or root-owned regular artifact without symlinks."""

    selected = Path(path)
    if not selected.is_absolute():
        raise ValidationError("signed grant path must be absolute")
    try:
        if selected.is_symlink() or not selected.is_file():
            raise ValidationError("signed grant must be a regular non-symlink file")
        metadata = selected.stat()
        if metadata.st_mode & 0o077:
            raise ValidationError("signed grant must not be group/world accessible")
        if hasattr(os, "geteuid") and metadata.st_uid not in {os.geteuid(), 0}:
            raise ValidationError(
                "signed grant must be owned by the process user or root"
            )
        raw = selected.read_bytes()
    except ValidationError:
        raise
    except OSError as error:
        raise ValidationError("signed grant cannot be read") from error
    if not raw or len(raw) > MAX_GRANT_ARTIFACT_BYTES or b"\x00" in raw:
        raise ValidationError("signed grant size is invalid")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValidationError("signed grant is not strict UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise ValidationError("signed grant JSON must be an object")
    return signed_infrastructure_grant_from_dict(decoded)


def verify_signed_infrastructure_grant(
    grant: SignedInfrastructureGrant,
    *,
    secret: bytes,
    expected_issuer_id: str,
    expected_key_id: str,
    expected_audience: str,
    at: datetime,
) -> TrustedInfrastructureGrant:
    """Authenticate an artifact with the separately loaded configured secret."""

    if not isinstance(grant, SignedInfrastructureGrant):
        raise TypeError("grant must be SignedInfrastructureGrant")
    authority = TestnetInfrastructureGrantAuthority(
        secret,
        issuer_id=expected_issuer_id,
        key_id=expected_key_id,
        audience=expected_audience,
    )
    return authority.verify(grant, at=at)


__all__ = (
    "MAX_GRANT_ARTIFACT_BYTES",
    "load_signed_infrastructure_grant",
    "verify_signed_infrastructure_grant",
)
