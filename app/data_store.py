import json
import os
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Boolean,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

request_records = Table(
    "api_request_records",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(32), nullable=False, index=True),
    Column("method", String(12), nullable=False),
    Column("path", String(1024), nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("request_payload", LargeBinary(length=16_777_215), nullable=False),
    Column("response_payload", LargeBinary(length=16_777_215), nullable=False),
    Column("created_at", DateTime, nullable=False, index=True),
)

audit_events = Table(
    "api_audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(32), nullable=True, index=True),
    Column("event_type", String(100), nullable=False),
    Column("payload", LargeBinary(length=16_777_215), nullable=False),
    Column("occurred_at", DateTime, nullable=False, index=True),
)

auth_users = Table(
    "auth_users",
    metadata,
    Column("username", String(64), primary_key=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(16), nullable=False, default="user"),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime, nullable=False),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("username", String(64), nullable=False, index=True),
    Column("csrf_token", String(64), nullable=False),
    Column("expires_at", DateTime, nullable=False, index=True),
)

auth_api_tokens = Table(
    "auth_api_tokens",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("username", String(64), nullable=False, index=True),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False, index=True),
    Column("revoked_at", DateTime, nullable=True),
)


class DataStore:
    def __init__(
        self,
        database_url: str,
        encryption_key: str,
        retention_days: int = 30,
        engine: Engine | None = None,
    ):
        if retention_days < 1:
            raise ValueError("DATA_RETENTION_DAYS must be at least 1")
        try:
            self._cipher = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise ValueError("APP_DATA_ENCRYPTION_KEY must be a valid Fernet key") from error
        self._engine = engine or create_engine(database_url, pool_pre_ping=True)
        self._retention_days = retention_days

    def initialize(self) -> None:
        metadata.create_all(self._engine)

    def record_request(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        request_payload: bytes,
        response_payload: bytes,
        created_at: datetime | None = None,
    ) -> None:
        values = {
            "request_id": request_id,
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "request_payload": self._encrypt(request_payload),
            "response_payload": self._encrypt(response_payload),
            "created_at": _naive_utc(created_at or datetime.now(timezone.utc)),
        }
        with self._engine.begin() as connection:
            connection.execute(insert(request_records).values(**values))

    def record_audit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        occurred_at = _parse_timestamp(payload.get("timestamp"))
        request_id = payload.get("request_id")
        encoded_payload = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        values = {
            "request_id": str(request_id) if request_id is not None else None,
            "event_type": event_type[:100],
            "payload": self._encrypt(encoded_payload),
            "occurred_at": _naive_utc(occurred_at),
        }
        with self._engine.begin() as connection:
            connection.execute(insert(audit_events).values(**values))

    def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = _naive_utc(now or datetime.now(timezone.utc)) - timedelta(days=self._retention_days)
        with self._engine.begin() as connection:
            request_result = connection.execute(
                delete(request_records).where(request_records.c.created_at < cutoff)
            )
            audit_result = connection.execute(
                delete(audit_events).where(audit_events.c.occurred_at < cutoff)
            )
            session_result = connection.execute(
                delete(auth_sessions).where(auth_sessions.c.expires_at < _naive_utc(now or datetime.now(timezone.utc)))
            )
            api_token_result = connection.execute(
                delete(auth_api_tokens).where(
                    auth_api_tokens.c.expires_at < _naive_utc(now or datetime.now(timezone.utc))
                )
            )
        return (
            (request_result.rowcount or 0)
            + (audit_result.rowcount or 0)
            + (session_result.rowcount or 0)
            + (api_token_result.rowcount or 0)
        )

    def create_user(self, username: str, password: str, role: str = "user") -> None:
        normalized_username = _validate_username(username)
        if role not in {"admin", "user"}:
            raise ValueError("role must be 'admin' or 'user'")
        password_hash = _hash_password(password)
        with self._engine.begin() as connection:
            connection.execute(
                insert(auth_users).values(
                    username=normalized_username,
                    password_hash=password_hash,
                    role=role,
                    is_active=True,
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )

    def ensure_bootstrap_admin(self, username: str, password: str) -> None:
        normalized_username = _validate_username(username)
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(auth_users.c.username).where(auth_users.c.username == normalized_username)
            ).first()
            if existing is None:
                connection.execute(
                    insert(auth_users).values(
                        username=normalized_username,
                        password_hash=_hash_password(password),
                        role="admin",
                        is_active=True,
                        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        normalized_username = username.strip().lower()
        with self._engine.connect() as connection:
            user = connection.execute(
                select(auth_users).where(auth_users.c.username == normalized_username)
            ).mappings().first()
        password_hash = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
        password_valid = _verify_password(password, password_hash)
        if user is None or not user["is_active"] or not password_valid:
            return None
        return _public_user(user)

    def create_session(self, username: str, session_hours: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=session_hours)
        with self._engine.begin() as connection:
            connection.execute(
                insert(auth_sessions).values(
                    token_hash=_token_hash(token),
                    username=username,
                    csrf_token=csrf_token,
                    expires_at=expires_at.replace(tzinfo=None),
                )
            )
        return token, csrf_token

    def get_session(self, token: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            session = connection.execute(
                select(auth_sessions, auth_users.c.role, auth_users.c.is_active).join(
                    auth_users, auth_users.c.username == auth_sessions.c.username
                ).where(auth_sessions.c.token_hash == _token_hash(token))
            ).mappings().first()
            if session is None:
                return None
            if session["expires_at"] <= now or not session["is_active"]:
                connection.execute(
                    delete(auth_sessions).where(auth_sessions.c.token_hash == _token_hash(token))
                )
                return None
            return {
                "username": session["username"],
                "role": session["role"],
                "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
            }

    def revoke_session(self, token: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(auth_sessions).where(auth_sessions.c.token_hash == _token_hash(token))
            )

    def create_api_token(self, username: str, token_days: int = 90) -> tuple[str, dict[str, Any]]:
        if token_days < 1:
            raise ValueError("API token lifetime must be at least one day.")
        token = f"rr_{secrets.token_urlsafe(32)}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = now + timedelta(days=token_days)
        with self._engine.begin() as connection:
            user = connection.execute(
                select(auth_users.c.username, auth_users.c.is_active).where(
                    auth_users.c.username == username
                )
            ).mappings().first()
            if user is None:
                raise ValueError("User not found.")
            if not user["is_active"]:
                raise ValueError("Cannot create an API token for a disabled user.")
            result = connection.execute(
                insert(auth_api_tokens).values(
                    token_hash=_token_hash(token),
                    username=username,
                    created_at=now,
                    expires_at=expires_at,
                    revoked_at=None,
                )
            )
            token_id = result.inserted_primary_key[0]
        return token, {
            "id": token_id,
            "username": username,
            "created_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
        }

    def authenticate_api_token(self, token: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.connect() as connection:
            user = connection.execute(
                select(auth_users)
                .join(auth_api_tokens, auth_api_tokens.c.username == auth_users.c.username)
                .where(
                    auth_api_tokens.c.token_hash == _token_hash(token),
                    auth_api_tokens.c.expires_at > now,
                    auth_api_tokens.c.revoked_at.is_(None),
                    auth_users.c.is_active.is_(True),
                )
            ).mappings().first()
        return _public_user(user) if user is not None else None

    def list_api_tokens(self) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            tokens = connection.execute(
                select(
                    auth_api_tokens.c.id,
                    auth_api_tokens.c.username,
                    auth_api_tokens.c.created_at,
                    auth_api_tokens.c.expires_at,
                    auth_api_tokens.c.revoked_at,
                ).order_by(auth_api_tokens.c.username, auth_api_tokens.c.created_at.desc())
            ).mappings()
            return [dict(token) for token in tokens]

    def revoke_api_token(self, token_id: int) -> bool:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(auth_api_tokens)
                .where(
                    auth_api_tokens.c.id == token_id,
                    auth_api_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        return bool(result.rowcount)

    def list_users(self) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            users = connection.execute(
                select(
                    auth_users.c.username,
                    auth_users.c.role,
                    auth_users.c.is_active,
                    auth_users.c.created_at,
                ).order_by(auth_users.c.username)
            ).mappings()
            return [dict(user) for user in users]

    def set_user_active(self, username: str, is_active: bool) -> bool:
        with self._engine.begin() as connection:
            target = connection.execute(
                select(auth_users.c.role, auth_users.c.is_active)
                .where(auth_users.c.username == username)
                .with_for_update()
            ).mappings().first()
            if target is None:
                return False
            if target["role"] == "admin" and target["is_active"] and not is_active:
                active_admins = connection.execute(
                    select(auth_users.c.username)
                    .where(
                        auth_users.c.role == "admin",
                        auth_users.c.is_active.is_(True),
                    )
                    .with_for_update()
                ).scalars().all()
                if len(active_admins) <= 1:
                    raise ValueError("The last active administrator cannot be disabled.")
            connection.execute(
                update(auth_users)
                .where(auth_users.c.username == username)
                .values(is_active=is_active)
            )
            if not is_active:
                connection.execute(
                    delete(auth_sessions).where(auth_sessions.c.username == username)
                )
                connection.execute(
                    update(auth_api_tokens)
                    .where(
                        auth_api_tokens.c.username == username,
                        auth_api_tokens.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc).replace(tzinfo=None))
                )
        return True

    def reset_user_password(self, username: str, password: str) -> bool:
        password_hash = _hash_password(password)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(auth_users)
                .where(auth_users.c.username == username)
                .values(password_hash=password_hash)
            )
            if result.rowcount:
                connection.execute(
                    delete(auth_sessions).where(auth_sessions.c.username == username)
                )
                connection.execute(
                    update(auth_api_tokens)
                    .where(
                        auth_api_tokens.c.username == username,
                        auth_api_tokens.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc).replace(tzinfo=None))
                )
        return bool(result.rowcount)

    def active_admin_count(self) -> int:
        with self._engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(auth_users).where(
                    auth_users.c.role == "admin",
                    auth_users.c.is_active.is_(True),
                )
            ).scalar_one()

    def _encrypt(self, payload: bytes) -> bytes:
        return self._cipher.encrypt(payload)


_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}$")


def _validate_username(username: str) -> str:
    normalized = username.strip().lower()
    if not _USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("Username must be 3-64 characters using letters, numbers, '.', '_' or '-'.")
    return normalized


def _hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 128:
        raise ValueError("Password must be between 12 and 128 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = password_hash.split("$", maxsplit=2)
        if algorithm != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(user: Any) -> dict[str, Any]:
    return {
        "username": user["username"],
        "role": user["role"],
        "is_active": user["is_active"],
        "created_at": user["created_at"],
    }


_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_urlsafe(24))


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def get_data_store() -> DataStore | None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None

    encryption_key = os.getenv("APP_DATA_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("APP_DATA_ENCRYPTION_KEY is required when DATABASE_URL is configured")

    try:
        retention_days = int(os.getenv("DATA_RETENTION_DAYS", "30"))
    except ValueError as error:
        raise RuntimeError("DATA_RETENTION_DAYS must be a positive integer") from error
    if retention_days < 1:
        raise RuntimeError("DATA_RETENTION_DAYS must be a positive integer")

    return DataStore(database_url, encryption_key, retention_days)
