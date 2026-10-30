import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, update

from app.data_store import (
    DataStore,
    audit_events,
    auth_api_tokens,
    auth_sessions,
    auth_users,
    request_records,
)


def _store(retention_days=30):
    engine = create_engine("sqlite:///:memory:")
    store = DataStore(
        "sqlite:///:memory:",
        Fernet.generate_key().decode("ascii"),
        retention_days=retention_days,
        engine=engine,
    )
    store.initialize()
    return store, engine


def test_request_and_audit_payloads_are_encrypted():
    store, engine = _store()
    request_payload = b'{"patient_nbr":123,"diagnosis":"private"}'
    response_payload = b'{"prediction":1,"probability":0.8}'
    audit_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": "request-1",
        "patient_nbr": 123,
    }

    store.record_request(
        request_id="request-1",
        method="POST",
        path="/predict",
        status_code=200,
        duration_ms=12.5,
        request_payload=request_payload,
        response_payload=response_payload,
    )
    store.record_audit_event("prediction_completed", audit_payload)

    with engine.connect() as connection:
        request = connection.execute(select(request_records)).mappings().one()
        audit = connection.execute(select(audit_events)).mappings().one()

    assert request["request_payload"] != request_payload
    assert request["response_payload"] != response_payload
    assert store._cipher.decrypt(request["request_payload"]) == request_payload
    assert store._cipher.decrypt(request["response_payload"]) == response_payload
    assert audit["payload"] != json.dumps(audit_payload).encode()
    assert json.loads(store._cipher.decrypt(audit["payload"])) == audit_payload
    assert audit["request_id"] == "request-1"


def test_retention_purges_records_older_than_configured_days():
    store, engine = _store(retention_days=30)
    old_date = datetime.now(timezone.utc) - timedelta(days=31)
    recent_date = datetime.now(timezone.utc) - timedelta(days=1)
    for request_id, created_at in (("old", old_date), ("recent", recent_date)):
        store.record_request(
            request_id=request_id,
            method="GET",
            path="/health",
            status_code=200,
            duration_ms=1,
            request_payload=b"",
            response_payload=b"{}",
            created_at=created_at,
        )
        store.record_audit_event(
            "health_check",
            {"timestamp": created_at.isoformat(), "request_id": request_id},
        )

    assert store.purge_expired() == 2
    with engine.connect() as connection:
        request_ids = connection.execute(select(request_records.c.request_id)).scalars().all()
        audit_ids = connection.execute(select(audit_events.c.request_id)).scalars().all()
    assert request_ids == ["recent"]
    assert audit_ids == ["recent"]


def test_store_requires_valid_retention_and_encryption_key():
    with pytest.raises(ValueError, match="Fernet key"):
        DataStore("sqlite:///:memory:", "not-a-fernet-key")

    with pytest.raises(ValueError, match="at least 1"):
        DataStore("sqlite:///:memory:", Fernet.generate_key().decode(), retention_days=0)


def test_user_password_is_hashed_and_session_token_is_not_stored():
    store, engine = _store()
    store.create_user("Care.Team", "this-is-a-long-password", role="admin")

    user = store.authenticate_user("CARE.TEAM", "this-is-a-long-password")
    assert user is not None
    assert user["username"] == "care.team"
    assert user["role"] == "admin"
    assert store.authenticate_user("care.team", "wrong-password") is None

    session_token, csrf_token = store.create_session("care.team", 8)
    session = store.get_session(session_token)
    assert session is not None
    assert session["csrf_token"] == csrf_token
    with engine.connect() as connection:
        stored_user = connection.execute(select(auth_users)).mappings().one()
        stored_session = connection.execute(select(auth_sessions)).mappings().one()
    assert stored_user["password_hash"] != "this-is-a-long-password"
    assert session_token not in stored_session["token_hash"]


def test_disabling_user_revokes_sessions_and_prevents_login():
    store, _ = _store()
    store.create_user("clinical-user", "another-long-password")
    session_token, _ = store.create_session("clinical-user", 8)

    store.set_user_active("clinical-user", False)

    assert store.authenticate_user("clinical-user", "another-long-password") is None
    assert store.get_session(session_token) is None


def test_password_reset_revokes_existing_sessions():
    store, _ = _store()
    store.create_user("password-user", "original-password")
    session_token, _ = store.create_session("password-user", 8)

    assert store.reset_user_password("password-user", "replacement-password")

    assert store.authenticate_user("password-user", "original-password") is None
    assert store.authenticate_user("password-user", "replacement-password") is not None
    assert store.get_session(session_token) is None


def test_user_creation_validates_name_role_and_password():
    store, _ = _store()
    with pytest.raises(ValueError, match="Username"):
        store.create_user("x", "another-long-password")
    with pytest.raises(ValueError, match="12 and 128"):
        store.create_user("valid-name", "short")
    with pytest.raises(ValueError, match="role"):
        store.create_user("valid-name", "another-long-password", role="owner")


def test_bootstrap_admin_is_created_once_without_overwriting_password():
    store, _ = _store()
    store.ensure_bootstrap_admin("initial-admin", "first-admin-password")
    store.ensure_bootstrap_admin("initial-admin", "replacement-admin-password")

    assert store.authenticate_user("initial-admin", "first-admin-password") is not None
    assert store.authenticate_user("initial-admin", "replacement-admin-password") is None
    assert store.active_admin_count() == 1


def test_expired_sessions_are_rejected_and_purged():
    store, engine = _store()
    store.create_user("expired-user", "another-long-password")
    session_token, _ = store.create_session("expired-user", 8)
    with engine.begin() as connection:
        connection.execute(
            update(auth_sessions).values(
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
            )
        )

    assert store.get_session(session_token) is None
    assert store.purge_expired() == 0


def test_api_tokens_are_hashed_expire_and_can_be_revoked():
    store, engine = _store()
    store.create_user("token-user", "token-user-password")

    token, metadata = store.create_api_token("token-user")

    assert token.startswith("rr_")
    assert metadata["expires_at"] - metadata["created_at"] == timedelta(days=90)
    assert store.authenticate_api_token(token)["username"] == "token-user"
    with engine.connect() as connection:
        stored_token = connection.execute(select(auth_api_tokens)).mappings().one()
    assert token not in stored_token["token_hash"]
    assert store.revoke_api_token(metadata["id"])
    assert store.authenticate_api_token(token) is None
    assert not store.revoke_api_token(metadata["id"])


def test_api_tokens_expire_and_are_revoked_when_user_is_disabled_or_password_reset():
    store, engine = _store()
    store.create_user("token-user", "token-user-password")
    expired_token, expired_metadata = store.create_api_token("token-user")
    with engine.begin() as connection:
        connection.execute(
            update(auth_api_tokens)
            .where(auth_api_tokens.c.id == expired_metadata["id"])
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
    assert store.authenticate_api_token(expired_token) is None
    assert store.purge_expired() == 1

    disable_token, _ = store.create_api_token("token-user")
    assert store.set_user_active("token-user", False)
    assert store.authenticate_api_token(disable_token) is None

    assert store.set_user_active("token-user", True)
    reset_token, _ = store.create_api_token("token-user")
    assert store.reset_user_password("token-user", "replacement-password")
    assert store.authenticate_api_token(reset_token) is None
