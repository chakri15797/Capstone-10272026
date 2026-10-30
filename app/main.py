import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import bootstrap_admin, cookie_secure, optional_session
from app.data_store import get_data_store
from app.observability import AUDIT_EVENTS, configure_observability, log_event
from app.prediction import MODEL_PATH, _load_model
from app.routes import router
from app.schemas import Features, PredictionRequest, PredictionResponse


async def _retention_worker():
    store = get_data_store()
    while True:
        await asyncio.sleep(24 * 60 * 60)
        if store is not None:
            await asyncio.to_thread(store.purge_expired)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cookie_secure()
    store = get_data_store()
    retention_task = None
    if store is not None:
        await asyncio.to_thread(store.initialize)
        await asyncio.to_thread(bootstrap_admin, store)
        await asyncio.to_thread(store.purge_expired)
        retention_task = asyncio.create_task(_retention_worker())
    try:
        yield
    finally:
        if retention_task is not None:
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Readmission Risk API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    openapi_url=None,
)
configure_observability(app)
app.include_router(router)


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
async def swagger_ui(request: Request) -> Response:
    if optional_session(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Swagger UI",
        swagger_ui_parameters={"withCredentials": True},
    )
    html = response.body.decode("utf-8")
    swagger_config = """const ui = SwaggerUIBundle({
    requestInterceptor: async (request) => {
        const method = (request.method || "GET").toUpperCase();
        const headers = request.headers || {};
        const authorizationEntry = Object.entries(headers).find(
            ([name]) => name.toLowerCase() === "authorization"
        );
        const authorization = authorizationEntry ? authorizationEntry[1] : undefined;
        const usesBearerToken = typeof authorization === "string"
            && authorization.toLowerCase().startsWith("bearer ");
        if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && !usesBearerToken) {
            const csrfResponse = await fetch(
                new URL("auth/csrf", window.location.href),
                { credentials: "same-origin" }
            );
            if (csrfResponse.ok) {
                const csrf = await csrfResponse.json();
                request.headers = request.headers || {};
                request.headers["X-CSRF-Token"] = csrf.csrf_token;
            }
        }
        return request;
    },
"""
    marker = "const ui = SwaggerUIBundle({"
    if marker not in html:
        raise RuntimeError("Could not configure the Swagger UI request interceptor.")
    html = html.replace(marker, swagger_config, 1)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/openapi.json", include_in_schema=False, response_class=JSONResponse)
async def openapi_schema(request: Request) -> Response:
    if optional_session(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(app.openapi())


__all__ = [
    "AUDIT_EVENTS",
    "Features",
    "MODEL_PATH",
    "PredictionRequest",
    "PredictionResponse",
    "_load_model",
    "app",
    "log_event",
]
