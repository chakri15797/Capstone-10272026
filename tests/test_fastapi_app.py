import ast
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import get_args

import numpy as np
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app import auth
from app.data_store import DataStore
from app.main import Features, PredictionRequest, app
from app.auth import SESSION_COOKIE


client = TestClient(app)


def _authenticate_test_client(test_client, token, csrf_token):
    test_client.cookies.set(SESSION_COOKIE, token)
    test_client.headers["X-CSRF-Token"] = csrf_token


@pytest.fixture(autouse=True)
def authenticated_client(monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = DataStore(
        "sqlite://",
        Fernet.generate_key().decode("ascii"),
        engine=engine,
    )
    store.initialize()
    store.create_user("test-admin", "test-password-long-enough", role="admin")
    token, csrf_token = store.create_session("test-admin", 8)
    monkeypatch.setattr(auth, "get_auth_store", lambda: store)
    monkeypatch.setattr("app.routes.get_auth_store", lambda: store)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    _authenticate_test_client(client, token, csrf_token)
    yield store
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)

def _feature_values():
    values = {}
    for field_name, field in Features.model_fields.items():
        wire_name = field.alias or field_name
        if field_name == "weight":
            value = "[150-175)"
        elif field.annotation is int or int in get_args(field.annotation):
            value = 7
        else:
            value = f"test-{field_name}"
        values[wire_name] = value
    return values


class _FormControlNames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()
        self.numeric_minimums = {}
        self.required_names = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"input", "select", "textarea"}:
            attributes = dict(attrs)
            name = attributes.get("name")
            if name:
                self.names.add(name)
                if "required" in attributes:
                    self.required_names.add(name)
                if tag == "input" and attributes.get("type") == "number":
                    self.numeric_minimums[name] = attributes.get("min")


def test_predict_endpoint_returns_probability_and_label():
    payload = {
        "features": {
            "encounter_id": 123,
            "patient_nbr": 987,
            "race": "Caucasian",
            "gender": "Female",
            "age": "[50-60)",
            "weight": "[150-175)",
            "admission_type_id": 1,
            "discharge_disposition_id": 1,
            "admission_source_id": 7,
            "time_in_hospital": 4,
            "payer_code": None,
            "medical_specialty": "Cardiology",
            "num_lab_procedures": 12,
            "num_procedures": 1,
            "num_medications": 20,
            "number_outpatient": 0,
            "number_emergency": 0,
            "number_inpatient": 0,
            "diag_1": "250.83",
            "diag_2": "276.2",
            "diag_3": "401.9",
            "number_diagnoses": 5,
            "max_glu_serum": "None",
            "A1Cresult": "None",
            "metformin": "No",
            "repaglinide": "No",
            "nateglinide": "No",
            "chlorpropamide": "No",
            "glimepiride": "No",
            "acetohexamide": "No",
            "glipizide": "No",
            "glyburide": "No",
            "tolbutamide": "No",
            "pioglitazone": "No",
            "rosiglitazone": "No",
            "acarbose": "No",
            "miglitol": "No",
            "troglitazone": "No",
            "tolazamide": "No",
            "examide": "No",
            "citoglipton": "No",
            "insulin": "No",
            "glyburide-metformin": "No",
            "glipizide-metformin": "No",
            "glimepiride-pioglitazone": "No",
            "metformin-rosiglitazone": "No",
            "metformin-pioglitazone": "No",
            "change": "No",
            "diabetesMed": "Yes",
        }
    }

    response = client.post("/predict", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert "prediction" in body
    assert "probability" in body
    assert body["prediction"] in {0, 1}
    assert 0.0 <= float(body["probability"]) <= 1.0


def test_predict_endpoint_passes_every_feature_to_model(monkeypatch):
    from app import prediction

    class CapturingModel:
        frame = None

        def predict(self, frame):
            self.frame = frame.copy()
            return [1]

        def predict_proba(self, frame):
            self.frame = frame.copy()
            return np.array([[0.25, 0.75]])

    model = CapturingModel()
    monkeypatch.setattr(prediction, "_load_model", lambda: model)
    feature_values = _feature_values()

    response = client.post("/predict", json={"features": feature_values})

    assert response.status_code == 200
    assert model.frame is not None
    assert set(model.frame.columns) == set(feature_values)
    actual_values = model.frame.iloc[0].to_dict()
    for field_name, expected_value in feature_values.items():
        assert actual_values[field_name] == expected_value


def test_locust_payload_matches_and_validates_all_prediction_fields():
    locust_path = Path(__file__).resolve().parents[1] / "locustfile.py"
    locust_tree = ast.parse(locust_path.read_text(encoding="utf-8"))
    payload_assignment = next(
        node
        for node in ast.walk(locust_tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "PREDICTION_PAYLOAD"
            for target in node.targets
        )
    )
    payload = ast.literal_eval(payload_assignment.value)

    feature_values = payload["features"]
    expected_names = {
        field.alias or field_name
        for field_name, field in Features.model_fields.items()
    }

    assert set(feature_values) == expected_names
    PredictionRequest.model_validate(payload)


def test_form_has_a_control_for_every_prediction_feature():
    response = client.get("/")
    assert response.status_code == 200
    assert '<dialog id="result-modal"' in response.text
    assert 'id="prediction-form"' in response.text
    assert 'fetch(form.action' in response.text
    parser = _FormControlNames()
    parser.feed(response.text)

    expected_names = {
        field.alias or field_name
        for field_name, field in Features.model_fields.items()
    }
    assert parser.names == expected_names | {"csrf_token"}
    assert parser.numeric_minimums
    assert all(minimum == "0" for minimum in parser.numeric_minimums.values())
    assert parser.required_names == {
        "race",
        "gender",
        "age",
        "admission_type_id",
        "discharge_disposition_id",
        "admission_source_id",
        "time_in_hospital",
    }
    assert "/admin/users" in response.text


def test_prediction_api_requires_patient_and_admission_fields():
    required_fields = {
        "race",
        "gender",
        "age",
        "admission_type_id",
        "discharge_disposition_id",
        "admission_source_id",
        "time_in_hospital",
    }

    for field_name in required_fields:
        feature_values = _feature_values()
        del feature_values[field_name]
        response = client.post("/predict", json={"features": feature_values})
        assert response.status_code == 422, field_name

    for field_name in ("race", "gender", "age"):
        feature_values = _feature_values()
        feature_values[field_name] = ""
        response = client.post("/predict", json={"features": feature_values})
        assert response.status_code == 422, field_name


def test_prediction_api_rejects_negative_numeric_features():
    negative_fields = [
        "encounter_id",
        "patient_nbr",
        "admission_type_id",
        "discharge_disposition_id",
        "admission_source_id",
        "time_in_hospital",
        "num_lab_procedures",
        "num_procedures",
        "num_medications",
        "number_outpatient",
        "number_emergency",
        "number_inpatient",
        "number_diagnoses",
    ]

    for field_name in negative_fields:
        feature_values = _feature_values()
        feature_values[field_name] = -1

        response = client.post("/predict", json={"features": feature_values})

        assert response.status_code == 422, field_name


def test_health_endpoint_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_include_request_audit_events():
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'readmission_audit_events_total{event="started"}' in response.text
    assert 'readmission_audit_events_total{event="completed"}' in response.text
    assert "http_request_duration_highr_seconds_bucket" in response.text


def test_mysql_storage_records_encrypted_request_response_and_audit(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app import data_store
    from app.data_store import audit_events, request_records

    class CapturingModel:
        def predict(self, frame):
            return [1]

        def predict_proba(self, frame):
            return np.array([[0.2, 0.8]])

    database_path = tmp_path / "request-records.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("APP_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
    data_store.get_data_store.cache_clear()
    try:
        monkeypatch.setattr("app.prediction._load_model", lambda: CapturingModel())
        with TestClient(app) as stored_client:
            session_token, csrf_token = auth.get_auth_store().create_session(
                "test-admin",
                8,
            )
            _authenticate_test_client(stored_client, session_token, csrf_token)
            health_response = stored_client.get("/health")
            prediction_response = stored_client.post(
                "/predict",
                json={"features": _feature_values()},
            )
        assert health_response.status_code == 200
        assert prediction_response.status_code == 200

        store = data_store.get_data_store()
        with store._engine.connect() as connection:
            health_record = connection.execute(
                select(request_records).where(request_records.c.path == "/health")
            ).mappings().one()
            events = connection.execute(
                select(audit_events).where(audit_events.c.request_id == health_record["request_id"])
            ).mappings().all()
            prediction_record = connection.execute(
                select(request_records).where(request_records.c.path == "/predict")
            ).mappings().one()

        assert store._cipher.decrypt(health_record["request_payload"]) == b""
        assert {event["event_type"] for event in events} >= {
            "http_request_started",
            "http_request_completed",
            "health_check",
        }
        raw_request = store._cipher.decrypt(prediction_record["request_payload"])
        raw_response = store._cipher.decrypt(prediction_record["response_payload"])
        assert b'"patient_nbr"' in raw_request
        assert b'"probability":0.8' in raw_response
        assert prediction_record["status_code"] == 200
        assert prediction_record["duration_ms"] >= 0
    finally:
        data_store.get_data_store.cache_clear()


def test_mysql_storage_records_internal_server_error_response(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app import data_store
    from app.data_store import request_records

    database_path = tmp_path / "request-error-records.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("APP_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
    data_store.get_data_store.cache_clear()

    async def failing_endpoint():
        raise RuntimeError("controlled test failure")

    original_routes = list(app.router.routes)
    app.add_api_route("/_test-storage-failure", failing_endpoint, methods=["GET"])
    try:
        with TestClient(app) as stored_client:
            response = stored_client.get("/_test-storage-failure")
        assert response.status_code == 500
        assert response.text == "Internal Server Error"

        store = data_store.get_data_store()
        with store._engine.connect() as connection:
            record = connection.execute(
                select(request_records).where(
                    request_records.c.path == "/_test-storage-failure"
                )
            ).mappings().one()
        assert record["status_code"] == 500
        assert store._cipher.decrypt(record["response_payload"]) == b"Internal Server Error"
    finally:
        app.router.routes[:] = original_routes
        data_store.get_data_store.cache_clear()


def test_model_loader_caches_loaded_model(monkeypatch):
    from app import prediction

    prediction._load_model.cache_clear()
    loads = []
    sentinel = object()

    def fake_load(path):
        loads.append(path)
        return sentinel

    monkeypatch.setattr(prediction.joblib, "load", fake_load)
    try:
        assert prediction._load_model() is sentinel
        assert prediction._load_model() is sentinel
        assert len(loads) == 1
    finally:
        prediction._load_model.cache_clear()


def test_prediction_form_accepts_model_fields():
    response = client.post(
        "/predict_form",
        data={
            "race": "Caucasian",
            "gender": "Female",
            "age": "[50-60)",
            "admission_type_id": "1",
            "discharge_disposition_id": "1",
            "admission_source_id": "7",
            "time_in_hospital": "4",
            "weight": "[150-175)",
            "payer_code": "MC",
            "medical_specialty": "Cardiology",
            "tolazamide": "Steady",
            "glyburide-metformin": "Steady",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prediction"] in {0, 1}
    assert 0.0 <= body["probability"] <= 1.0
    assert body["model_name"] == "readmission_risk_model"


def test_unauthenticated_user_is_redirected_to_sign_in():
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_prediction_api_requires_session_and_csrf():
    response = client.post(
        "/predict",
        json={"features": _feature_values()},
        headers={"X-CSRF-Token": "invalid"},
    )
    assert response.status_code == 403

    client.cookies.clear()
    response = client.post(
        "/predict",
        json={"features": _feature_values()},
        headers={"X-CSRF-Token": "token"},
    )
    assert response.status_code == 401


def test_login_uses_csrf_and_creates_eight_hour_session():
    client.cookies.clear()
    login_page = client.get("/login")
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert csrf_match is not None
    csrf_token = csrf_match.group(1)

    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": "test-admin",
            "password": "test-password-long-enough",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in response.cookies
    assert client.get("/").status_code == 200


def test_login_rejects_invalid_password():
    client.cookies.clear()
    login_page = client.get("/login")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)

    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": "test-admin",
            "password": "incorrect-password",
        },
    )

    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_login_rejects_invalid_csrf_token():
    client.cookies.clear()
    client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": "not-the-cookie-token",
            "username": "test-admin",
            "password": "test-password-long-enough",
        },
    )
    assert response.status_code == 403


def test_logout_revokes_the_current_session(authenticated_client):
    session_token = client.cookies.get(SESSION_COOKIE)
    response = client.post(
        "/logout",
        data={"csrf_token": client.headers["X-CSRF-Token"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert authenticated_client.get_session(session_token) is None


def test_json_api_can_retrieve_session_csrf_token(authenticated_client):
    response = client.get("/auth/csrf")
    assert response.status_code == 200
    assert response.json()["csrf_token"] == client.headers["X-CSRF-Token"]


def test_swagger_ui_attaches_session_csrf_token_for_mutating_requests():
    response = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert '"withCredentials": true' in response.text
    assert "usesBearerToken" in response.text
    assert 'new URL("auth/csrf", window.location.href)' in response.text
    assert 'request.headers["X-CSRF-Token"] = csrf.csrf_token' in response.text


def test_swagger_and_openapi_redirect_unauthenticated_clients_to_login():
    client.cookies.clear()

    for path in ("/docs", "/openapi.json"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_openapi_schema_hides_login_and_browser_management_routes():
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]) == {"/health", "/predict"}
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert {"HTTPBearer": []} in schema["paths"]["/predict"]["post"]["security"]


def test_prediction_api_accepts_a_bearer_token_without_csrf(monkeypatch, authenticated_client):
    from app import routes

    store = authenticated_client
    token, _ = store.create_api_token("test-admin")
    monkeypatch.setattr(routes, "predict_features", lambda _features: (1, 0.75))
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)

    response = client.post(
        "/predict",
        json={"features": _feature_values()},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["prediction"] == 1


def test_prediction_api_rejects_revoked_bearer_token(authenticated_client):
    store = authenticated_client
    token, metadata = store.create_api_token("test-admin")
    assert store.revoke_api_token(metadata["id"])

    response = client.post(
        "/predict",
        json={"features": _feature_values()},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_admin_can_create_one_time_and_revoke_api_tokens(authenticated_client):
    store = authenticated_client
    csrf_token = client.headers["X-CSRF-Token"]
    create_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "create_api_token",
            "username": "test-admin",
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 200
    token_match = re.search(r"<code>(rr_[^<]+)</code>", create_response.text)
    assert token_match is not None
    token = token_match.group(1)
    assert store.authenticate_api_token(token) is not None
    assert token not in client.get("/admin/users").text

    metadata = next(
        token_row
        for token_row in store.list_api_tokens()
        if token_row["username"] == "test-admin"
    )
    revoke_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "revoke_api_token",
            "token_id": str(metadata["id"]),
        },
        follow_redirects=False,
    )

    assert revoke_response.status_code == 303
    assert store.authenticate_api_token(token) is None


def test_admin_can_create_and_disable_accounts(authenticated_client):
    store = authenticated_client
    admin_page = client.get("/admin/users")
    assert admin_page.status_code == 200
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text)
    assert csrf_match is not None
    csrf_token = csrf_match.group(1)

    create_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "create",
            "username": "new-user",
            "password": "another-password-123",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert store.authenticate_user("new-user", "another-password-123") is not None

    disable_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "set_active",
            "username": "new-user",
            "is_active": "false",
        },
        follow_redirects=False,
    )
    assert disable_response.status_code == 303
    assert store.authenticate_user("new-user", "another-password-123") is None


def test_admin_can_reset_password_and_revoke_user_session(authenticated_client):
    store = authenticated_client
    store.create_user("reset-user", "original-password-123")
    session_token, _ = store.create_session("reset-user", 8)
    response = client.post(
        "/admin/users",
        data={
            "csrf_token": client.headers["X-CSRF-Token"],
            "action": "reset_password",
            "username": "reset-user",
            "password": "replacement-password-123",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert store.authenticate_user("reset-user", "original-password-123") is None
    assert store.authenticate_user("reset-user", "replacement-password-123") is not None
    assert store.get_session(session_token) is None


def test_non_admin_cannot_manage_accounts(authenticated_client):
    store = authenticated_client
    store.create_user("regular-user", "regular-password-123")
    token, csrf_token = store.create_session("regular-user", 8)
    client.cookies.clear()
    _authenticate_test_client(client, token, csrf_token)

    assert "/admin/users" not in client.get("/").text
    assert client.get("/admin/users").status_code == 403


def test_last_active_admin_cannot_be_disabled(authenticated_client):
    response = client.post(
        "/admin/users",
        data={
            "csrf_token": client.headers["X-CSRF-Token"],
            "action": "set_active",
            "username": "test-admin",
            "is_active": "false",
        },
    )
    assert response.status_code == 422
    assert "last active administrator" in response.json()["detail"]


def test_prediction_form_rejects_missing_required_admission_details():
    response = client.post(
        "/predict_form",
        data={"race": "Caucasian", "gender": "Female", "age": "[50-60)"},
    )
    assert response.status_code == 422
