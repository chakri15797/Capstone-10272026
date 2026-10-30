import hmac
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.data_store import DataStore, get_data_store

SESSION_COOKIE = "readmission_session"
LOGIN_CSRF_COOKIE = "readmission_login_csrf"
SESSION_HOURS = 8
SESSION_MAX_AGE = SESSION_HOURS * 60 * 60
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthSession:
    username: str
    role: str
    csrf_token: str
    uses_api_token: bool = False


def get_auth_store() -> DataStore | None:
    return get_data_store()


def cookie_secure() -> bool:
    value = os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise RuntimeError("AUTH_COOKIE_SECURE must be true or false")
    return value in {"true", "1"}


def bootstrap_admin(store: DataStore | None) -> None:
    username = os.getenv("AUTH_BOOTSTRAP_ADMIN_USERNAME")
    password = os.getenv("AUTH_BOOTSTRAP_ADMIN_PASSWORD")
    if bool(username) != bool(password):
        raise RuntimeError(
            "AUTH_BOOTSTRAP_ADMIN_USERNAME and AUTH_BOOTSTRAP_ADMIN_PASSWORD must be set together"
        )
    if username and password:
        if store is None:
            raise RuntimeError("DATABASE_URL is required to initialize the bootstrap admin")
        store.ensure_bootstrap_admin(username, password)


def optional_session(request: Request) -> AuthSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    store = get_auth_store()
    if store is None:
        return None
    session = store.get_session(token)
    if session is None:
        return None
    return AuthSession(
        username=session["username"],
        role=session["role"],
        csrf_token=session["csrf_token"],
    )


def require_session(
    request: Request,
) -> AuthSession:
    if get_auth_store() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication storage is unavailable.",
        )
    session = optional_session(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return session


def require_prediction_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthSession:
    store = get_auth_store()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication storage is unavailable.",
        )
    if credentials is not None:
        user = store.authenticate_api_token(credentials.credentials)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AuthSession(
            username=user["username"],
            role=user["role"],
            csrf_token="",
            uses_api_token=True,
        )
    session = optional_session(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return session


def require_admin(
    session: AuthSession = Depends(require_session),
) -> AuthSession:
    if session.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required.")
    return session


def validate_csrf(session: AuthSession, supplied_token: str | None) -> None:
    if not supplied_token or not hmac.compare_digest(session.csrf_token, supplied_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token.")
