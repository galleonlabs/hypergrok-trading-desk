from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_grant import (
    TestnetInfrastructureGrantAuthority,
    infrastructure_grant_confirmation,
)
from trading_harness.grant_artifact import (
    load_signed_infrastructure_grant,
    verify_signed_infrastructure_grant,
)


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
POLICY = "a" * 64
SECRET = b"g" * 32


def signed():
    return TestnetInfrastructureGrantAuthority(
        SECRET,
        issuer_id="local-learning-authority",
        key_id="grant-key-v1",
        audience="testnet-control",
    ).issue(
        grant_id="grant-1",
        generation=1,
        account_id="testnet-account",
        allowed_instruments=("ETH-PERP",),
        risk_policy_hash=POLICY,
        max_loss="5",
        max_notional="100",
        max_leverage="2",
        confirmation=infrastructure_grant_confirmation(
            grant_id="grant-1",
            generation=1,
            account_id="testnet-account",
            allowed_instruments=("ETH-PERP",),
            risk_policy_hash=POLICY,
            max_loss="5",
            max_notional="100",
            max_leverage="2",
            ttl_seconds=3_600,
        ),
        at=NOW,
    )


class SignedGrantArtifactTests(unittest.TestCase):
    def test_root_owned_copy_is_accepted_and_unrelated_owner_is_rejected(self) -> None:
        selected = MagicMock()
        selected.is_absolute.return_value = True
        selected.is_symlink.return_value = False
        selected.is_file.return_value = True
        selected.stat.return_value.st_mode = 0o100400
        selected.stat.return_value.st_uid = 0
        selected.read_bytes.return_value = json.dumps(
            signed().as_dict()
        ).encode("utf-8")

        with patch(
            "trading_harness.grant_artifact.Path",
            return_value=selected,
        ):
            parsed = load_signed_infrastructure_grant("/root-owned/grant.json")

        self.assertEqual(signed().grant_hash, parsed.grant_hash)

        selected.stat.return_value.st_uid = 502
        with (
            patch(
                "trading_harness.grant_artifact.Path",
                return_value=selected,
            ),
            patch(
                "trading_harness.grant_artifact.os.geteuid",
                return_value=501,
            ),
            self.assertRaisesRegex(ValidationError, "process user or root"),
        ):
            load_signed_infrastructure_grant("/unrelated-owner/grant.json")

    def test_owner_only_file_round_trips_and_authenticates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grant.json"
            path.write_text(json.dumps(signed().as_dict()), encoding="utf-8")
            path.chmod(0o600)

            parsed = load_signed_infrastructure_grant(path.absolute())
            trusted = verify_signed_infrastructure_grant(
                parsed,
                secret=SECRET,
                expected_issuer_id="local-learning-authority",
                expected_key_id="grant-key-v1",
                expected_audience="testnet-control",
                at=NOW + timedelta(seconds=1),
            )

        self.assertEqual(parsed.grant_hash, trusted.grant_hash)

    def test_relative_symlink_exposed_duplicate_float_and_bad_mac_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.json"
            good.write_text(json.dumps(signed().as_dict()), encoding="utf-8")
            good.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "absolute"):
                load_signed_infrastructure_grant(Path("grant.json"))

            link = root / "link.json"
            link.symlink_to(good)
            with self.assertRaisesRegex(ValidationError, "non-symlink"):
                load_signed_infrastructure_grant(link.absolute())

            good.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "group/world"):
                load_signed_infrastructure_grant(good.absolute())

            duplicate = root / "duplicate.json"
            duplicate.write_text('{"grant_id":"one","grant_id":"two"}', encoding="utf-8")
            duplicate.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "duplicate"):
                load_signed_infrastructure_grant(duplicate.absolute())

            floating = root / "float.json"
            floating.write_text('{"value":1.5}', encoding="utf-8")
            floating.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "floats"):
                load_signed_infrastructure_grant(floating.absolute())

        with self.assertRaisesRegex(StateConflict, "MAC"):
            verify_signed_infrastructure_grant(
                signed(),
                secret=b"x" * 32,
                expected_issuer_id="local-learning-authority",
                expected_key_id="grant-key-v1",
                expected_audience="testnet-control",
                at=NOW + timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
