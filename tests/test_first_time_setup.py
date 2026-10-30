import base64
import json

import pytest
from cryptography.fernet import Fernet

import first_time_setup


def test_compose_environment_generates_secrets_and_preserves_them(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    credentials_file = tmp_path / "credentials.json"
    monkeypatch.setattr(first_time_setup, "COMPOSE_ENV", env_file)
    monkeypatch.setattr(first_time_setup, "CREDENTIALS_FILE", credentials_file)

    settings, generated_password = first_time_setup._ensure_compose_env()

    assert generated_password
    assert settings["MYSQL_ROOT_PASSWORD"] != "replace-with-a-strong-random-password"
    assert settings["MYSQL_PASSWORD"] != "replace-with-a-strong-random-password"
    Fernet(settings["APP_DATA_ENCRYPTION_KEY"].encode("ascii"))
    assert len(settings["AUTH_BOOTSTRAP_ADMIN_PASSWORD"]) >= 12
    first_password = settings["AUTH_BOOTSTRAP_ADMIN_PASSWORD"]
    first_time_setup._save_credentials(
        "compose",
        settings["AUTH_BOOTSTRAP_ADMIN_USERNAME"],
        first_password,
    )
    saved_credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert saved_credentials["compose"]["password"] == first_password

    rerun_settings, generated_password = first_time_setup._ensure_compose_env()

    assert not generated_password
    assert rerun_settings["AUTH_BOOTSTRAP_ADMIN_PASSWORD"] == first_password


def test_existing_kubernetes_secret_is_never_replaced_or_incompletely_accepted():
    with pytest.raises(RuntimeError, match="missing keys"):
        first_time_setup._secret_values({"data": {}}, None)

    complete_secret = {
        "data": {key: "existing" for key in first_time_setup.SECRET_KEYS}
    }
    assert first_time_setup._secret_values(complete_secret, None) == {}


def test_new_kubernetes_secret_reuses_compose_admin_credentials():
    compose_settings = {
        "AUTH_BOOTSTRAP_ADMIN_USERNAME": "first-admin",
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD": "known-bootstrap-password",
    }
    secret_values = first_time_setup._secret_values(None, compose_settings)
    secret = json.loads(first_time_setup._secret_manifest(secret_values))
    decoded = {
        key: base64.b64decode(value).decode("utf-8")
        for key, value in secret["data"].items()
    }

    assert set(decoded) == set(first_time_setup.SECRET_KEYS)
    assert decoded["AUTH_BOOTSTRAP_ADMIN_USERNAME"] == "first-admin"
    assert decoded["AUTH_BOOTSTRAP_ADMIN_PASSWORD"] == "known-bootstrap-password"
    assert "known-bootstrap-password" not in first_time_setup._secret_manifest(secret_values)
