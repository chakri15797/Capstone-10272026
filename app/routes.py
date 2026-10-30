from importlib.resources import files
import asyncio
from datetime import datetime, timezone
from html import escape
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_HOURS,
    SESSION_MAX_AGE,
    AuthSession,
    cookie_secure,
    get_auth_store,
    optional_session,
    require_admin,
    require_prediction_auth,
    require_session,
    validate_csrf,
)
from app.data_store import DataStore
from app.observability import log_event
from app.prediction import MODEL_PATH, predict_features
from app.schemas import Features, PredictionRequest, PredictionResponse

router = APIRouter()
FORM_HTML = files("app").joinpath("templates/prediction_form.html").read_text(encoding="utf-8")
LOGIN_HTML = files("app").joinpath("templates/login.html").read_text(encoding="utf-8")
ADMIN_HTML = files("app").joinpath("templates/admin_users.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def form_page(request: Request):
    session = optional_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    account_link = '<a href="/admin/users">Account management</a>' if session.role == "admin" else ""
    html = FORM_HTML.replace("__CSRF_TOKEN__", escape(session.csrf_token))
    html = html.replace("__ACCOUNT_MANAGEMENT_LINK__", account_link)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _login_response(csrf_token: str, message: str = "", status_code: int = 200) -> HTMLResponse:
    html = LOGIN_HTML.replace("__LOGIN_CSRF_TOKEN__", escape(csrf_token))
    html = html.replace("__LOGIN_MESSAGE__", escape(message))
    response = HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf_token,
        max_age=600,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )
    return response


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    session = optional_session(request)
    if session is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _login_response(secrets.token_urlsafe(32))


@router.post("/login", include_in_schema=False)
async def login(request: Request):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    csrf_cookie = request.cookies.get(LOGIN_CSRF_COOKIE)
    if (
        not isinstance(csrf_token, str)
        or not csrf_cookie
        or not secrets.compare_digest(csrf_token, csrf_cookie)
    ):
        return _login_response(
            secrets.token_urlsafe(32),
            "Your sign-in page expired. Please try again.",
            status.HTTP_403_FORBIDDEN,
        )

    username = form.get("username")
    password = form.get("password")
    store = get_auth_store()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication storage is unavailable.",
        )
    user = await asyncio.to_thread(
        store.authenticate_user,
        username if isinstance(username, str) else "",
        password if isinstance(password, str) else "",
    )
    if user is None:
        return _login_response(
            secrets.token_urlsafe(32),
            "Invalid username or password.",
            status.HTTP_401_UNAUTHORIZED,
        )

    session_token, _ = await asyncio.to_thread(
        store.create_session,
        user["username"],
        SESSION_HOURS,
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=cookie_secure(), samesite="strict")
    return response


@router.post("/predict_form", response_model=PredictionResponse, include_in_schema=False)
async def predict_from_form(
    request: Request,
    session: AuthSession = Depends(require_session),
):
    form_data = await request.form()
    csrf_token = form_data.get("csrf_token") or request.headers.get("X-CSRF-Token")
    validate_csrf(session, csrf_token if isinstance(csrf_token, str) else None)
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="Model artifact not found. Train the model first.")

    try:
        features = Features.model_validate({
            key: None if value == "" else value
            for key, value in form_data.items()
            if key != "csrf_token"
        })
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors()) from error
    prediction, probability = predict_features(features)

    return PredictionResponse(
        prediction=prediction,
        probability=probability,
        model_name="readmission_risk_model",
    )


@router.get("/auth/csrf", include_in_schema=False)
async def get_csrf_token(
    session: AuthSession = Depends(require_session),
) -> dict[str, str]:
    return {"csrf_token": session.csrf_token}


@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    session: AuthSession = Depends(require_session),
):
    supplied_token = request.headers.get("X-CSRF-Token")
    if supplied_token is None:
        form = await request.form()
        value = form.get("csrf_token")
        supplied_token = value if isinstance(value, str) else None
    validate_csrf(session, supplied_token)
    token = request.cookies.get(SESSION_COOKIE)
    store = get_auth_store()
    if token and store is not None:
        await asyncio.to_thread(store.revoke_session, token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure(), httponly=True, samesite="strict")
    return response


@router.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
async def admin_users_page(
    session: AuthSession = Depends(require_admin),
):
    store = get_auth_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Authentication storage is unavailable.")
    return await _render_admin_users(store, session)


async def _render_admin_users(
    store: DataStore,
    session: AuthSession,
    token_notice: str = "",
):
    users = await asyncio.to_thread(store.list_users)
    tokens = await asyncio.to_thread(store.list_api_tokens)
    tokens_by_user: dict[str, list[dict[str, Any]]] = {}
    for token in tokens:
        tokens_by_user.setdefault(token["username"], []).append(token)
    rows = []
    for user in users:
        username = escape(user["username"])
        role = escape(user["role"])
        active = bool(user["is_active"])
        state = "Active" if active else "Disabled"
        next_active = "false" if active else "true"
        action = "Disable" if active else "Enable"
        token_rows = []
        for token in tokens_by_user.get(user["username"], []):
            token_id = token["id"]
            token_expiry = escape(token["expires_at"].isoformat(sep=" ", timespec="minutes"))
            token_revoked = token["revoked_at"] is not None
            token_expired = token["expires_at"] <= datetime.now(timezone.utc).replace(tzinfo=None)
            if token_revoked or token_expired or not active:
                token_status = "Revoked" if token_revoked or not active else "Expired"
                token_rows.append(f"<div>Token {token_id}: {token_status} (expires {token_expiry})</div>")
                continue
            token_rows.append(
                f"<div>Token {token_id}: Active until {token_expiry} "
                "<form method=\"post\" action=\"/admin/users\" class=\"inline-form\">"
                f"<input type=\"hidden\" name=\"csrf_token\" value=\"{escape(session.csrf_token)}\">"
                "<input type=\"hidden\" name=\"action\" value=\"revoke_api_token\">"
                f"<input type=\"hidden\" name=\"token_id\" value=\"{token_id}\">"
                "<button type=\"submit\">Revoke token</button></form></div>"
            )
        rows.append(
            "<tr>"
            f"<td>{username}</td><td>{role}</td><td>{state}</td>"
            "<td><form method=\"post\" action=\"/admin/users\">"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{escape(session.csrf_token)}\">"
            "<input type=\"hidden\" name=\"action\" value=\"set_active\">"
            f"<input type=\"hidden\" name=\"username\" value=\"{username}\">"
            f"<input type=\"hidden\" name=\"is_active\" value=\"{next_active}\">"
            f"<button type=\"submit\">{action}</button></form></td>"
            "<td><form method=\"post\" action=\"/admin/users\">"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{escape(session.csrf_token)}\">"
            "<input type=\"hidden\" name=\"action\" value=\"reset_password\">"
            f"<input type=\"hidden\" name=\"username\" value=\"{username}\">"
            "<input type=\"password\" name=\"password\" minlength=\"12\" maxlength=\"128\" "
            "autocomplete=\"new-password\" placeholder=\"Temporary password\" required>"
            "<button type=\"submit\">Reset password</button></form></td>"
            "<td>"
            f"{''.join(token_rows)}"
            "<form method=\"post\" action=\"/admin/users\">"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{escape(session.csrf_token)}\">"
            "<input type=\"hidden\" name=\"action\" value=\"create_api_token\">"
            f"<input type=\"hidden\" name=\"username\" value=\"{username}\">"
            "<button type=\"submit\""
            + ("" if active else " disabled")
            + ">Create 90-day API token</button></form></td></tr>"
        )
    html = ADMIN_HTML.replace("__CSRF_TOKEN__", escape(session.csrf_token))
    html = html.replace("__USER_ROWS__", "".join(rows))
    html = html.replace("__API_TOKEN_NOTICE__", token_notice)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _api_token_notice(username: str, token: str, expires_at: datetime) -> str:
    return (
        '<section class="token-notice" role="status">'
        f"<p>API token for <strong>{escape(username)}</strong>. Copy it now; "
        "it will not be shown again.</p>"
        f"<code>{escape(token)}</code>"
        f"<p>Expires {escape(expires_at.isoformat(sep=' ', timespec='minutes'))} UTC.</p>"
        "</section>"
    )


@router.post("/admin/users", include_in_schema=False)
async def manage_users(
    request: Request,
    session: AuthSession = Depends(require_admin),
):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    validate_csrf(session, csrf_token if isinstance(csrf_token, str) else None)
    store = get_auth_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Authentication storage is unavailable.")

    action = form.get("action")
    try:
        if action == "create":
            username = form.get("username")
            password = form.get("password")
            role = form.get("role")
            if not all(isinstance(value, str) for value in (username, password, role)):
                raise ValueError("Username, password, and role are required.")
            if role not in {"admin", "user"}:
                raise ValueError("Choose a valid user role.")
            await asyncio.to_thread(store.create_user, username, password, role)
        elif action == "create_api_token":
            username = form.get("username")
            if not isinstance(username, str):
                raise ValueError("Username is required.")
            api_token, token_metadata = await asyncio.to_thread(
                store.create_api_token,
                username,
            )
            return await _render_admin_users(
                store,
                session,
                _api_token_notice(username, api_token, token_metadata["expires_at"]),
            )
        elif action == "revoke_api_token":
            token_id = form.get("token_id")
            if not isinstance(token_id, str) or not token_id.isdecimal():
                raise ValueError("Invalid API token.")
            if not await asyncio.to_thread(store.revoke_api_token, int(token_id)):
                raise ValueError("API token not found or already revoked.")
        elif action == "set_active":
            username = form.get("username")
            active_value = form.get("is_active")
            if not isinstance(username, str) or active_value not in {"true", "false"}:
                raise ValueError("Invalid user status request.")
            is_active = active_value == "true"
            if not await asyncio.to_thread(store.set_user_active, username, is_active):
                raise ValueError("User not found.")
            if username == session.username and not is_active:
                response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
                response.delete_cookie(
                    SESSION_COOKIE,
                    path="/",
                    secure=cookie_secure(),
                    httponly=True,
                    samesite="strict",
                )
                return response
        elif action == "reset_password":
            username = form.get("username")
            password = form.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("Username and new password are required.")
            if not await asyncio.to_thread(store.reset_user_password, username, password):
                raise ValueError("User not found.")
            if username == session.username:
                response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
                response.delete_cookie(
                    SESSION_COOKIE,
                    path="/",
                    secure=cookie_secure(),
                    httponly=True,
                    samesite="strict",
                )
                return response
        else:
            raise ValueError("Invalid account-management action.")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="That username already exists.") from error

    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/health")
def health_check() -> dict[str, str]:
    log_event("health_check", status="ok")
    return {"status": "ok"}


@router.post("/predict", response_model=PredictionResponse)
def predict_risk(
    payload: PredictionRequest,
    request: Request,
    session: AuthSession = Depends(require_prediction_auth),
):
    if not session.uses_api_token:
        validate_csrf(session, request.headers.get("X-CSRF-Token"))
    patient_id = payload.features.patient_nbr
    encounter_id = payload.features.encounter_id
    log_event(
        "prediction_request_received",
        patient_nbr=patient_id,
        encounter_id=encounter_id,
        endpoint="/predict",
    )

    if not MODEL_PATH.exists():
        log_event(
            "prediction_failed",
            patient_nbr=patient_id,
            encounter_id=encounter_id,
            reason="model_not_found",
            status_code=503,
        )
        raise HTTPException(status_code=503, detail="Model artifact not found. Train the model first.")

    prediction, probability = predict_features(payload.features)

    log_event(
        "prediction_completed",
        patient_nbr=patient_id,
        encounter_id=encounter_id,
        prediction=prediction,
        probability=probability,
        model_name="readmission_risk_model",
    )

    return PredictionResponse(
        prediction=prediction,
        probability=probability,
        model_name="readmission_risk_model",
    )
