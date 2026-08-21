"""SQLite state store for atomic admission, reservations, and outbox rows.

All monetary values are stored as canonical decimal strings.  SQLite's
numeric affinity is intentionally not used for risk arithmetic because it can
coerce values through binary floating point.  Every risk-increasing admission
runs under ``BEGIN IMMEDIATE`` and updates one account/environment row before
the transaction commits.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from .canonical import canonical_decimal, canonical_json, semantic_intent_hash
from .domain import (
    Authorization,
    AuthorizationModel,
    DeploymentGrant,
    Environment,
    GrantType,
    SemanticIntent,
)
from .errors import (
    AdmissionDenied,
    PolicyViolation,
    RecordNotFound,
    StateConflict,
    StorageError,
    ValidationError,
)
from .policy import (
    HARD_PLATFORM_CEILINGS,
    AccountExposure,
    ExposureQuote,
    FOUNDATION_ALLOWED_ACTIONS,
    PlatformCeilings,
    RiskPolicy,
    decimal_add,
    decimal_divide,
    decimal_multiply,
    decimal_subtract,
    derive_exposure_quote,
    exact_decimal,
)


ZERO = Decimal("0")
TERMINAL_COMMAND_STATES = frozenset({"canceled", "rejected", "expired", "filled"})
RELEASING_COMMAND_STATES = frozenset({"canceled", "rejected", "expired"})
DISPATCHABLE_OUTBOX_STATES = frozenset({"pending"})


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw:
        raise ValidationError("enum-like persisted values must be non-empty strings")
    return raw


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime, *, field: str) -> str:
    return _utc(value, field=field).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    # Canonical strings retain exact value and never cross SQLite numeric
    # affinity. Numerically equivalent values therefore persist identically.
    return canonical_decimal(exact_decimal(value, field="persisted decimal"))


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command_id: str
    authorization_id: str
    grant_id: str
    account_id: str
    environment: str
    venue: str
    client_order_id: str
    signal_instance_hash: str | None
    intent_hash: str
    state: str
    requested_quantity: Decimal
    reserved_quantity: Decimal
    booked_quantity: Decimal
    released_quantity: Decimal
    original_notional: Decimal
    reserved_notional: Decimal
    booked_notional: Decimal
    released_notional: Decimal
    original_loss: Decimal
    reserved_loss: Decimal
    booked_loss: Decimal
    released_loss: Decimal
    payload_json: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: int
    command_id: str
    state: str
    topic: str
    payload_json: str
    attempts: int
    created_at: datetime
    updated_at: datetime

    @property
    def dispatchable(self) -> bool:
        return self.state in DISPATCHABLE_OUTBOX_STATES


class SQLiteStore:
    """Durable harness store.

    ``path`` must be file-backed.  A file database allows independent worker
    processes and is required for meaningful WAL durability; tests should use
    a temporary directory rather than ``:memory:``.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValidationError("SQLiteStore requires a file-backed database")
        if busy_timeout_ms <= 0:
            raise ValidationError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StorageError(f"SQLite refused WAL mode: {mode}")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS deployment_grants (
                    grant_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    thesis_version TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    grant_type TEXT NOT NULL,
                    authorization_model TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    allowed_instruments_json TEXT NOT NULL,
                    allowed_actions_json TEXT NOT NULL,
                    max_notional TEXT,
                    max_loss TEXT,
                    policy_json TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    revoked_at TEXT,
                    review_at TEXT
                );

                CREATE TABLE IF NOT EXISTS authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    intent_hash TEXT NOT NULL,
                    grant_id TEXT NOT NULL REFERENCES deployment_grants(grant_id),
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    state TEXT NOT NULL,
                    command_id TEXT UNIQUE,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_exposure (
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    reserved_notional TEXT NOT NULL,
                    reserved_loss TEXT NOT NULL,
                    booked_notional TEXT NOT NULL,
                    booked_loss TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, environment)
                );

                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    authorization_id TEXT NOT NULL UNIQUE
                        REFERENCES authorizations(authorization_id),
                    grant_id TEXT NOT NULL REFERENCES deployment_grants(grant_id),
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    signal_instance_hash TEXT,
                    intent_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_quantity TEXT NOT NULL,
                    reserved_quantity TEXT NOT NULL,
                    booked_quantity TEXT NOT NULL,
                    released_quantity TEXT NOT NULL,
                    original_notional TEXT NOT NULL,
                    reserved_notional TEXT NOT NULL,
                    booked_notional TEXT NOT NULL,
                    released_notional TEXT NOT NULL,
                    original_loss TEXT NOT NULL,
                    reserved_loss TEXT NOT NULL,
                    booked_loss TEXT NOT NULL,
                    released_loss TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (venue, account_id, environment, client_order_id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE REFERENCES commands(command_id),
                    state TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_commands_account_state
                    ON commands(account_id, environment, state);
                CREATE INDEX IF NOT EXISTS idx_outbox_state_id
                    ON outbox(state, outbox_id);
                COMMIT;
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(commands)")
            }
            if "signal_instance_hash" not in columns:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "ALTER TABLE commands ADD COLUMN signal_instance_hash TEXT"
                )
                connection.commit()
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_signal_instance
                ON commands(account_id, environment, signal_instance_hash)
                WHERE signal_instance_hash IS NOT NULL
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            # SQLite serializes writers here, before any authorization or
            # account exposure is read.  This prevents two admissions from
            # observing the same remaining account capacity.
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def register_deployment_grant(
        self,
        grant: DeploymentGrant,
        policy: RiskPolicy,
        *,
        ceilings: PlatformCeilings = HARD_PLATFORM_CEILINGS,
    ) -> None:
        if not isinstance(grant, DeploymentGrant):
            raise TypeError("grant must be DeploymentGrant")
        # Foundation persistence is deliberately narrower than the domain
        # vocabulary reserved for later, separately reviewed milestones.
        if grant.grant_type is not GrantType.INFRASTRUCTURE_TESTNET:
            raise ValidationError(
                "foundation persists only infrastructure_testnet grants"
            )
        if grant.authorization_model is not AuthorizationModel.INFRASTRUCTURE:
            raise ValidationError(
                "foundation persists only infrastructure authorizations"
            )
        if grant.environment is not Environment.TESTNET:
            raise ValidationError("foundation persists only testnet grants")
        if set(grant.allowed_actions) != FOUNDATION_ALLOWED_ACTIONS:
            raise ValidationError(
                "foundation grant allowed_actions must be exactly simulate_order"
            )
        policy.validate_ceiling(ceilings)
        values = (
            grant.grant_id,
            grant.thesis_id,
            str(grant.thesis_version),
            grant.strategy_version,
            grant.code_hash,
            grant.venue,
            grant.account_id,
            _enum_value(grant.environment),
            _enum_value(grant.grant_type),
            _enum_value(grant.authorization_model),
            _time_text(grant.issued_at, field="grant.issued_at"),
            _time_text(grant.starts_at, field="grant.starts_at"),
            _time_text(grant.expires_at, field="grant.expires_at"),
            _enum_value(grant.state),
            canonical_json(tuple(grant.allowed_instruments)),
            canonical_json(tuple(grant.allowed_actions)),
            _optional_decimal_text(grant.max_notional),
            _optional_decimal_text(grant.max_loss),
            canonical_json(policy.to_dict()),
            policy.policy_id,
            policy.version,
            (
                _time_text(grant.revoked_at, field="grant.revoked_at")
                if grant.revoked_at is not None
                else None
            ),
            (
                _time_text(grant.review_at, field="grant.review_at")
                if grant.review_at is not None
                else None
            ),
        )
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO deployment_grants (
                        grant_id, thesis_id, thesis_version, strategy_version,
                        code_hash, venue, account_id, environment, grant_type,
                        authorization_model, issued_at, starts_at, expires_at,
                        state, allowed_instruments_json, allowed_actions_json,
                        max_notional, max_loss, policy_json, policy_id,
                        policy_version, revoked_at, review_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise StateConflict(f"deployment grant already exists: {grant.grant_id}") from exc

    def register_authorization(self, authorization: Authorization) -> None:
        if not isinstance(authorization, Authorization):
            raise TypeError("authorization must be Authorization")
        state = _enum_value(authorization.state)
        if state != "issued":
            raise ValidationError("new authorization must be in issued state")
        if authorization.environment is not Environment.TESTNET:
            raise ValidationError(
                "foundation persists only testnet infrastructure authorizations"
            )
        issued_at = _time_text(authorization.issued_at, field="authorization.issued_at")
        expires_at = _time_text(
            authorization.expires_at, field="authorization.expires_at"
        )
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO authorizations (
                        authorization_id, intent_hash, grant_id, account_id,
                        environment, issued_at, expires_at, audience, state,
                        command_id, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        authorization.authorization_id,
                        authorization.intent_hash,
                        authorization.grant_id,
                        authorization.account_id,
                        _enum_value(authorization.environment),
                        issued_at,
                        expires_at,
                        authorization.audience,
                        state,
                        issued_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StateConflict(
                f"authorization cannot be registered: {authorization.authorization_id}"
            ) from exc

    def revoke_grant(self, grant_id: str, *, now: datetime) -> None:
        timestamp = _time_text(now, field="now")
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE deployment_grants
                SET state = 'revoked', revoked_at = ?
                WHERE grant_id = ? AND state != 'revoked'
                """,
                (timestamp, grant_id),
            )
            if result.rowcount != 1:
                raise StateConflict(f"grant missing or already revoked: {grant_id}")
            connection.execute(
                """
                UPDATE outbox
                SET state = 'revoked', updated_at = ?
                WHERE state = 'pending' AND command_id IN (
                    SELECT command_id FROM commands WHERE grant_id = ?
                )
                """,
                (timestamp, grant_id),
            )

    def revoke_authorization(self, authorization_id: str, *, now: datetime) -> None:
        """Revoke unused/queued authority and atomically block its outbox item."""

        timestamp = _time_text(now, field="now")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, command_id FROM authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(
                    f"authorization not found: {authorization_id}"
                )
            if row["state"] not in {"issued", "consuming"}:
                raise StateConflict(
                    f"authorization cannot be revoked from state {row['state']}"
                )
            connection.execute(
                """
                UPDATE authorizations SET state = 'revoked', updated_at = ?
                WHERE authorization_id = ?
                """,
                (timestamp, authorization_id),
            )
            if row["command_id"] is not None:
                self._make_outbox_nondispatchable_locked(
                    connection,
                    command_id=str(row["command_id"]),
                    state="revoked",
                    now_text=timestamp,
                )

    def _ensure_account_row(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        environment: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO account_exposure (
                account_id, environment, reserved_notional, reserved_loss,
                booked_notional, booked_loss, updated_at
            ) VALUES (?, ?, '0', '0', '0', '0', ?)
            """,
            (account_id, environment, now_text),
        )

    def _read_exposure_locked(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        environment: str,
    ) -> AccountExposure:
        row = connection.execute(
            """
            SELECT reserved_notional, reserved_loss, booked_notional, booked_loss
            FROM account_exposure WHERE account_id = ? AND environment = ?
            """,
            (account_id, environment),
        ).fetchone()
        if row is None:
            raise StorageError("account exposure row disappeared inside transaction")
        return AccountExposure(
            reserved_notional=Decimal(row["reserved_notional"]),
            reserved_loss=Decimal(row["reserved_loss"]),
            booked_notional=Decimal(row["booked_notional"]),
            booked_loss=Decimal(row["booked_loss"]),
        )

    def _write_exposure_locked(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        environment: str,
        exposure: AccountExposure,
        now_text: str,
    ) -> None:
        result = connection.execute(
            """
            UPDATE account_exposure SET
                reserved_notional = ?, reserved_loss = ?,
                booked_notional = ?, booked_loss = ?, updated_at = ?
            WHERE account_id = ? AND environment = ?
            """,
            (
                _decimal_text(exposure.reserved_notional),
                _decimal_text(exposure.reserved_loss),
                _decimal_text(exposure.booked_notional),
                _decimal_text(exposure.booked_loss),
                now_text,
                account_id,
                environment,
            ),
        )
        if result.rowcount != 1:
            raise StorageError("account exposure update lost its target row")

    def atomically_admit(
        self,
        *,
        intent: SemanticIntent,
        quote: ExposureQuote,
        authorization_id: str,
        command_id: str,
        audience: str,
        authorization_model: Any,
        now: datetime,
        ceilings: PlatformCeilings = HARD_PLATFORM_CEILINGS,
    ) -> CommandRecord:
        """Consume authority, reserve risk, and create command/outbox atomically."""

        if not isinstance(intent, SemanticIntent):
            raise TypeError("intent must be SemanticIntent")
        if not command_id:
            raise ValidationError("command_id is required")
        if not authorization_id or not audience or not authorization_model:
            raise ValidationError(
                "authorization_id, audience, and authorization_model are required"
            )
        now_utc = _utc(now, field="now")
        now_text = _time_text(now_utc, field="now")
        environment = _enum_value(intent.environment)
        authorization_model_value = _enum_value(authorization_model)
        intent_hash = semantic_intent_hash(intent)
        if environment != Environment.TESTNET.value:
            raise AdmissionDenied(
                "PLATFORM_ENVIRONMENT_NOT_ALLOWED",
                "foundation admission supports testnet only",
            )
        if authorization_model_value != AuthorizationModel.INFRASTRUCTURE.value:
            raise AdmissionDenied(
                "PLATFORM_AUTHORIZATION_MODEL_NOT_ALLOWED",
                "foundation admission supports infrastructure authority only",
            )
        action = _enum_value(intent.action)
        if action not in FOUNDATION_ALLOWED_ACTIONS:
            raise AdmissionDenied(
                "PLATFORM_ACTION_NOT_ALLOWED",
                f"foundation release cannot perform action {action}",
            )
        try:
            derived_quote = derive_exposure_quote(intent)
        except ValidationError as exc:
            raise AdmissionDenied("RISK_ECONOMICS_INVALID", str(exc)) from exc
        if quote != derived_quote:
            raise AdmissionDenied(
                "RISK_QUOTE_ECONOMICS_MISMATCH",
                "caller quote differs from deterministic semantic-intent economics",
            )
        quote = derived_quote
        if now_utc >= _utc(intent.expires_at, field="intent.expires_at"):
            raise AdmissionDenied("INTENT_EXPIRED", "semantic intent has expired")

        with self._transaction() as connection:
            authorization = connection.execute(
                "SELECT * FROM authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            if authorization is None:
                raise AdmissionDenied("AUTHORIZATION_NOT_FOUND", authorization_id)
            if authorization["state"] != "issued":
                raise AdmissionDenied(
                    "AUTHORIZATION_ALREADY_USED",
                    f"authorization state is {authorization['state']}",
                )
            if authorization["intent_hash"] != intent_hash:
                raise AdmissionDenied(
                    "AUTHORIZATION_HASH_MISMATCH",
                    "authorization does not cover the semantic intent",
                )
            if (
                authorization["account_id"] != intent.account_id
                or authorization["environment"] != environment
            ):
                raise AdmissionDenied(
                    "AUTHORIZATION_SCOPE_MISMATCH",
                    "authorization account or environment differs",
                )
            if authorization["audience"] != audience:
                raise AdmissionDenied(
                    "AUTHORIZATION_AUDIENCE_MISMATCH",
                    "authorization audience differs from this admission service",
                )
            if not (
                _parse_time(authorization["issued_at"])
                <= now_utc
                < _parse_time(authorization["expires_at"])
            ):
                raise AdmissionDenied(
                    "AUTHORIZATION_INACTIVE", "authorization is not currently active"
                )

            grant = connection.execute(
                "SELECT * FROM deployment_grants WHERE grant_id = ?",
                (authorization["grant_id"],),
            ).fetchone()
            if grant is None:  # Foreign keys make this corruption, not user input.
                raise StorageError("authorization references a missing grant")
            if (
                grant["grant_type"] != GrantType.INFRASTRUCTURE_TESTNET.value
                or grant["authorization_model"]
                != AuthorizationModel.INFRASTRUCTURE.value
                or grant["environment"] != Environment.TESTNET.value
            ):
                raise AdmissionDenied(
                    "PLATFORM_GRANT_TYPE_NOT_ALLOWED",
                    "persisted grant is outside the foundation capability",
                )
            if grant["state"] != "active":
                raise AdmissionDenied(
                    "GRANT_INACTIVE", f"deployment grant state is {grant['state']}"
                )
            if not (
                _parse_time(grant["starts_at"])
                <= now_utc
                < _parse_time(grant["expires_at"])
            ):
                raise AdmissionDenied(
                    "GRANT_INACTIVE", "deployment grant is outside its active window"
                )
            if (
                grant["account_id"] != intent.account_id
                or grant["environment"] != environment
                or grant["venue"] != intent.venue
            ):
                raise AdmissionDenied(
                    "GRANT_SCOPE_MISMATCH",
                    "grant account, environment, or venue differs",
                )
            if grant["authorization_model"] != authorization_model_value:
                raise AdmissionDenied(
                    "GRANT_AUTHORIZATION_MODEL_MISMATCH",
                    "grant does not permit this authorization model",
                )
            expected_version_fields = (
                ("thesis_id", intent.thesis_id),
                ("thesis_version", str(intent.thesis_version)),
                ("strategy_version", intent.strategy_version),
                ("code_hash", intent.code_hash),
            )
            for field, actual in expected_version_fields:
                if grant[field] != actual:
                    raise AdmissionDenied(
                        "GRANT_VERSION_MISMATCH",
                        f"grant {field} differs from semantic intent",
                    )
            allowed_instruments = tuple(json.loads(grant["allowed_instruments_json"]))
            allowed_actions = tuple(json.loads(grant["allowed_actions_json"]))
            if "*" not in allowed_instruments and intent.instrument not in allowed_instruments:
                raise AdmissionDenied(
                    "GRANT_INSTRUMENT_NOT_ALLOWED", intent.instrument
                )
            if "*" not in allowed_actions and action not in allowed_actions:
                raise AdmissionDenied("GRANT_ACTION_NOT_ALLOWED", action)
            if grant["max_notional"] is not None and quote.notional > Decimal(
                grant["max_notional"]
            ):
                raise PolicyViolation(
                    "GRANT_NOTIONAL_LIMIT",
                    f"{quote.notional} exceeds {grant['max_notional']}",
                )
            if grant["max_loss"] is not None and quote.worst_case_loss > Decimal(
                grant["max_loss"]
            ):
                raise PolicyViolation(
                    "GRANT_LOSS_LIMIT",
                    f"{quote.worst_case_loss} exceeds {grant['max_loss']}",
                )

            policy = RiskPolicy.from_dict(json.loads(grant["policy_json"]))
            self._ensure_account_row(
                connection,
                account_id=intent.account_id,
                environment=environment,
                now_text=now_text,
            )
            current = self._read_exposure_locked(
                connection,
                account_id=intent.account_id,
                environment=environment,
            )
            leverage = intent.leverage if intent.leverage is not None else Decimal("1")
            policy.validate_order(
                instrument=intent.instrument,
                action=action,
                order_type=_enum_value(intent.order_type),
                leverage=leverage,
                quote=quote,
                current=current,
                ceilings=ceilings,
            )

            # CAS makes the consume-once invariant explicit even though the
            # transaction already owns SQLite's writer lock.
            changed = connection.execute(
                """
                UPDATE authorizations
                SET state = 'consuming', command_id = ?, updated_at = ?
                WHERE authorization_id = ? AND state = 'issued'
                """,
                (command_id, now_text, authorization_id),
            )
            if changed.rowcount != 1:
                raise StateConflict("authorization was consumed concurrently")

            payload_json = canonical_json(intent)
            zero = "0"
            try:
                connection.execute(
                    """
                    INSERT INTO commands (
                        command_id, authorization_id, grant_id, account_id,
                        environment, venue, client_order_id, signal_instance_hash,
                        intent_hash, state,
                        requested_quantity, reserved_quantity, booked_quantity,
                        released_quantity, original_notional, reserved_notional,
                        booked_notional, released_notional, original_loss,
                        reserved_loss, booked_loss, released_loss, payload_json,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        command_id,
                        authorization_id,
                        grant["grant_id"],
                        intent.account_id,
                        environment,
                        intent.venue,
                        intent.client_order_id,
                        intent.signal_instance_hash,
                        intent_hash,
                        "queued",
                        _decimal_text(quote.quantity),
                        _decimal_text(quote.quantity),
                        zero,
                        zero,
                        _decimal_text(quote.notional),
                        _decimal_text(quote.notional),
                        zero,
                        zero,
                        _decimal_text(quote.worst_case_loss),
                        _decimal_text(quote.worst_case_loss),
                        zero,
                        zero,
                        payload_json,
                        now_text,
                        now_text,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO outbox (
                        command_id, state, topic, payload_json, attempts,
                        created_at, updated_at
                    ) VALUES (?, 'pending', 'execute_semantic_intent', ?, 0, ?, ?)
                    """,
                    (command_id, payload_json, now_text, now_text),
                )
            except sqlite3.IntegrityError as exc:
                raise StateConflict(
                    "command ID, authorization, or client order ID is not unique"
                ) from exc

            self._write_exposure_locked(
                connection,
                account_id=intent.account_id,
                environment=environment,
                exposure=AccountExposure(
                    reserved_notional=decimal_add(
                        current.reserved_notional,
                        quote.notional,
                        field="admission reserved notional",
                    ),
                    reserved_loss=decimal_add(
                        current.reserved_loss,
                        quote.worst_case_loss,
                        field="admission reserved loss",
                    ),
                    booked_notional=current.booked_notional,
                    booked_loss=current.booked_loss,
                ),
                now_text=now_text,
            )
            row = connection.execute(
                "SELECT * FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise StorageError("queued command disappeared before commit")
            return self._command_from_row(row)

    def get_exposure(self, account_id: str, environment: Any) -> AccountExposure:
        environment_value = _enum_value(environment)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT reserved_notional, reserved_loss, booked_notional, booked_loss
                FROM account_exposure WHERE account_id = ? AND environment = ?
                """,
                (account_id, environment_value),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return AccountExposure()
        return AccountExposure(
            reserved_notional=Decimal(row["reserved_notional"]),
            reserved_loss=Decimal(row["reserved_loss"]),
            booked_notional=Decimal(row["booked_notional"]),
            booked_loss=Decimal(row["booked_loss"]),
        )

    def authorization_state(self, authorization_id: str) -> str:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"authorization not found: {authorization_id}")
        return str(row["state"])

    def get_command(self, command_id: str) -> CommandRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"command not found: {command_id}")
        return self._command_from_row(row)

    def get_outbox(self, command_id: str) -> OutboxRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM outbox WHERE command_id = ?", (command_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound(f"outbox row not found: {command_id}")
        return OutboxRecord(
            outbox_id=int(row["outbox_id"]),
            command_id=str(row["command_id"]),
            state=str(row["state"]),
            topic=str(row["topic"]),
            payload_json=str(row["payload_json"]),
            attempts=int(row["attempts"]),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    def mark_unknown(self, command_id: str, *, now: datetime) -> CommandRecord:
        """Record an unknown venue outcome without releasing any reservation."""

        now_text = _time_text(now, field="now")
        with self._transaction() as connection:
            row = self._command_row_locked(connection, command_id)
            if row["state"] in TERMINAL_COMMAND_STATES:
                raise StateConflict(f"terminal command cannot become unknown: {command_id}")
            connection.execute(
                """
                UPDATE commands SET state = 'submitted_unknown', updated_at = ?
                WHERE command_id = ?
                """,
                (now_text, command_id),
            )
            connection.execute(
                """
                UPDATE authorizations SET state = 'consumed', updated_at = ?
                WHERE authorization_id = ? AND state = 'consuming'
                """,
                (now_text, row["authorization_id"]),
            )
            self._make_outbox_nondispatchable_locked(
                connection,
                command_id=command_id,
                state="blocked_unknown",
                now_text=now_text,
            )
            updated = self._command_row_locked(connection, command_id)
            return self._command_from_row(updated)

    def record_fill(
        self,
        command_id: str,
        *,
        cumulative_filled_quantity: Decimal,
        now: datetime,
    ) -> CommandRecord:
        """Convert the filled fraction of a reservation to booked exposure."""

        cumulative = exact_decimal(
            cumulative_filled_quantity, field="cumulative_filled_quantity"
        )
        if cumulative < ZERO:
            raise ValidationError("cumulative_filled_quantity must not be negative")
        now_text = _time_text(now, field="now")
        with self._transaction() as connection:
            row = self._command_row_locked(connection, command_id)
            requested = Decimal(row["requested_quantity"])
            previous_booked_qty = Decimal(row["booked_quantity"])
            released_qty = Decimal(row["released_quantity"])
            if row["state"] in RELEASING_COMMAND_STATES:
                raise StateConflict("a released terminal command cannot receive a fill")
            if cumulative < previous_booked_qty:
                raise StateConflict("cumulative fill quantity cannot decrease")
            unreleased_quantity = decimal_subtract(
                requested,
                released_qty,
                field="command unreleased quantity",
            )
            if cumulative > unreleased_quantity:
                raise StateConflict("fill exceeds the command's unreleased quantity")

            original_notional = Decimal(row["original_notional"])
            original_loss = Decimal(row["original_loss"])
            if cumulative == requested:
                target_booked_notional = original_notional
                target_booked_loss = original_loss
            else:
                target_booked_notional = decimal_divide(
                    decimal_multiply(
                        original_notional,
                        cumulative,
                        field="partial-fill notional numerator",
                    ),
                    requested,
                    field="partial-fill booked notional",
                )
                target_booked_loss = decimal_divide(
                    decimal_multiply(
                        original_loss,
                        cumulative,
                        field="partial-fill loss numerator",
                    ),
                    requested,
                    field="partial-fill booked loss",
                )
            previous_booked_notional = Decimal(row["booked_notional"])
            previous_booked_loss = Decimal(row["booked_loss"])
            delta_notional = decimal_subtract(
                target_booked_notional,
                previous_booked_notional,
                field="fill notional delta",
            )
            delta_loss = decimal_subtract(
                target_booked_loss,
                previous_booked_loss,
                field="fill loss delta",
            )
            filled_quantity_delta = decimal_subtract(
                cumulative,
                previous_booked_qty,
                field="fill quantity delta",
            )
            reserved_notional = decimal_subtract(
                Decimal(row["reserved_notional"]),
                delta_notional,
                field="remaining reserved notional",
            )
            reserved_loss = decimal_subtract(
                Decimal(row["reserved_loss"]),
                delta_loss,
                field="remaining reserved loss",
            )
            reserved_quantity = decimal_subtract(
                Decimal(row["reserved_quantity"]),
                filled_quantity_delta,
                field="remaining reserved quantity",
            )
            if min(reserved_notional, reserved_loss, reserved_quantity) < ZERO:
                raise StorageError("fill conversion would create negative reservation")

            self._ensure_account_row(
                connection,
                account_id=row["account_id"],
                environment=row["environment"],
                now_text=now_text,
            )
            exposure = self._read_exposure_locked(
                connection,
                account_id=row["account_id"],
                environment=row["environment"],
            )
            updated_exposure = AccountExposure(
                reserved_notional=decimal_subtract(
                    exposure.reserved_notional,
                    delta_notional,
                    field="account reserved notional after fill",
                ),
                reserved_loss=decimal_subtract(
                    exposure.reserved_loss,
                    delta_loss,
                    field="account reserved loss after fill",
                ),
                booked_notional=decimal_add(
                    exposure.booked_notional,
                    delta_notional,
                    field="account booked notional after fill",
                ),
                booked_loss=decimal_add(
                    exposure.booked_loss,
                    delta_loss,
                    field="account booked loss after fill",
                ),
            )
            state = "filled" if cumulative == requested else "partially_filled"
            connection.execute(
                """
                UPDATE commands SET
                    state = ?, reserved_quantity = ?, booked_quantity = ?,
                    reserved_notional = ?, booked_notional = ?,
                    reserved_loss = ?, booked_loss = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (
                    state,
                    _decimal_text(reserved_quantity),
                    _decimal_text(cumulative),
                    _decimal_text(reserved_notional),
                    _decimal_text(target_booked_notional),
                    _decimal_text(reserved_loss),
                    _decimal_text(target_booked_loss),
                    now_text,
                    command_id,
                ),
            )
            connection.execute(
                """
                UPDATE authorizations SET state = 'consumed', updated_at = ?
                WHERE authorization_id = ? AND state = 'consuming'
                """,
                (now_text, row["authorization_id"]),
            )
            self._make_outbox_nondispatchable_locked(
                connection,
                command_id=command_id,
                state="observed_fill",
                now_text=now_text,
            )
            self._write_exposure_locked(
                connection,
                account_id=row["account_id"],
                environment=row["environment"],
                exposure=updated_exposure,
                now_text=now_text,
            )
            return self._command_from_row(
                self._command_row_locked(connection, command_id)
            )

    def mark_terminal(
        self,
        command_id: str,
        *,
        state: str,
        now: datetime,
    ) -> CommandRecord:
        """Release only the command's still-unused reservation."""

        if state not in RELEASING_COMMAND_STATES:
            raise ValidationError("terminal state must be canceled, rejected, or expired")
        now_text = _time_text(now, field="now")
        with self._transaction() as connection:
            row = self._command_row_locked(connection, command_id)
            if row["state"] in TERMINAL_COMMAND_STATES:
                raise StateConflict(f"command is already terminal: {command_id}")
            if row["state"] != "queued":
                raise StateConflict(
                    "foundation cannot release a submitted or partially filled "
                    "command without reconciliation-specific evidence"
                )
            reserved_quantity = Decimal(row["reserved_quantity"])
            reserved_notional = Decimal(row["reserved_notional"])
            reserved_loss = Decimal(row["reserved_loss"])
            exposure = self._read_exposure_locked(
                connection,
                account_id=row["account_id"],
                environment=row["environment"],
            )
            updated_exposure = AccountExposure(
                reserved_notional=decimal_subtract(
                    exposure.reserved_notional,
                    reserved_notional,
                    field="terminal account reserved notional",
                ),
                reserved_loss=decimal_subtract(
                    exposure.reserved_loss,
                    reserved_loss,
                    field="terminal account reserved loss",
                ),
                booked_notional=exposure.booked_notional,
                booked_loss=exposure.booked_loss,
            )
            connection.execute(
                """
                UPDATE commands SET
                    state = ?, reserved_quantity = '0',
                    reserved_notional = '0', reserved_loss = '0',
                    released_quantity = ?, released_notional = ?,
                    released_loss = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (
                    state,
                    _decimal_text(
                        decimal_add(
                            Decimal(row["released_quantity"]),
                            reserved_quantity,
                            field="released quantity",
                        )
                    ),
                    _decimal_text(
                        decimal_add(
                            Decimal(row["released_notional"]),
                            reserved_notional,
                            field="released notional",
                        )
                    ),
                    _decimal_text(
                        decimal_add(
                            Decimal(row["released_loss"]),
                            reserved_loss,
                            field="released loss",
                        )
                    ),
                    now_text,
                    command_id,
                ),
            )
            connection.execute(
                """
                UPDATE authorizations SET state = 'consumed', updated_at = ?
                WHERE authorization_id = ? AND state = 'consuming'
                """,
                (now_text, row["authorization_id"]),
            )
            self._make_outbox_nondispatchable_locked(
                connection,
                command_id=command_id,
                state="terminal",
                now_text=now_text,
            )
            self._write_exposure_locked(
                connection,
                account_id=row["account_id"],
                environment=row["environment"],
                exposure=updated_exposure,
                now_text=now_text,
            )
            return self._command_from_row(
                self._command_row_locked(connection, command_id)
            )

    @staticmethod
    def _make_outbox_nondispatchable_locked(
        connection: sqlite3.Connection,
        *,
        command_id: str,
        state: str,
        now_text: str,
    ) -> None:
        """Atomically remove an admitted item from the dispatchable set."""

        changed = connection.execute(
            """
            UPDATE outbox SET state = ?, updated_at = ?
            WHERE command_id = ? AND state = 'pending'
            """,
            (state, now_text, command_id),
        )
        if changed.rowcount not in (0, 1):
            raise StorageError("outbox state update affected multiple rows")

    def _command_row_locked(
        self, connection: sqlite3.Connection, command_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM commands WHERE command_id = ?", (command_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"command not found: {command_id}")
        return row

    @staticmethod
    def _command_from_row(row: Mapping[str, Any]) -> CommandRecord:
        return CommandRecord(
            command_id=str(row["command_id"]),
            authorization_id=str(row["authorization_id"]),
            grant_id=str(row["grant_id"]),
            account_id=str(row["account_id"]),
            environment=str(row["environment"]),
            venue=str(row["venue"]),
            client_order_id=str(row["client_order_id"]),
            signal_instance_hash=(
                str(row["signal_instance_hash"])
                if row["signal_instance_hash"] is not None
                else None
            ),
            intent_hash=str(row["intent_hash"]),
            state=str(row["state"]),
            requested_quantity=Decimal(row["requested_quantity"]),
            reserved_quantity=Decimal(row["reserved_quantity"]),
            booked_quantity=Decimal(row["booked_quantity"]),
            released_quantity=Decimal(row["released_quantity"]),
            original_notional=Decimal(row["original_notional"]),
            reserved_notional=Decimal(row["reserved_notional"]),
            booked_notional=Decimal(row["booked_notional"]),
            released_notional=Decimal(row["released_notional"]),
            original_loss=Decimal(row["original_loss"]),
            reserved_loss=Decimal(row["reserved_loss"]),
            booked_loss=Decimal(row["booked_loss"]),
            released_loss=Decimal(row["released_loss"]),
            payload_json=str(row["payload_json"]),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )
