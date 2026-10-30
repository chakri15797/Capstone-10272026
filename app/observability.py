import asyncio
from contextvars import ContextVar
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator.metrics import default as default_metrics
from starlette.responses import PlainTextResponse, Response

from app.data_store import get_data_store

_current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)

AUDIT_EVENTS = Counter(
    "readmission_audit_events",
    "HTTP request audit events emitted by the API.",
    labelnames=("event",),
)

logger = logging.getLogger("readmission_api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
logger.propagate = False


def log_event(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    request_id = _current_request_id.get()
    if request_id is not None:
        payload.setdefault("request_id", request_id)
    redacted_payload = {
        key: "[REDACTED]" if key in {"patient_nbr", "encounter_id"} else value
        for key, value in payload.items()
    }
    logger.info(json.dumps(redacted_payload, default=str))
    store = get_data_store()
    if store is not None:
        store.record_audit_event(event, payload)


async def _response_body(response: Response) -> bytes:
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return response.body

    chunks = []
    async for chunk in body_iterator:
        chunks.append(chunk.encode(response.charset) if isinstance(chunk, str) else chunk)
    return b"".join(chunks)


def configure_observability(app: FastAPI) -> None:
    Instrumentator().add(
        default_metrics(
            latency_lowr_buckets=(
                0.01,
                0.025,
                0.05,
                0.075,
                0.1,
                0.25,
                0.5,
                0.75,
                1,
                1.5,
                2,
                2.5,
                3,
                3.5,
                4,
                4.5,
                5,
                7.5,
                10,
                30,
                60,
            )
        )
    ).instrument(app).expose(app, include_in_schema=False, endpoint="/metrics")

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        request_id = uuid4().hex
        start = time.perf_counter()
        store = get_data_store()
        request_payload = await request.body() if store is not None else b""

        await asyncio.to_thread(
            log_event,
            "http_request_started",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        AUDIT_EVENTS.labels(event="started").inc()

        request_id_token = _current_request_id.set(request_id)
        try:
            response = await call_next(request)
        except Exception as error:
            await asyncio.to_thread(
                log_event,
                "http_request_failed",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                error_type=type(error).__name__,
            )
            AUDIT_EVENTS.labels(event="failed").inc()
            if store is not None:
                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                await asyncio.to_thread(
                    store.record_request,
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=500,
                    duration_ms=duration_ms,
                    request_payload=request_payload,
                    response_payload=b"Internal Server Error",
                )
                return PlainTextResponse("Internal Server Error", status_code=500)
            raise
        finally:
            _current_request_id.reset(request_id_token)

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if store is not None:
            response_headers = response.raw_headers
            response_payload = await _response_body(response)
            await asyncio.to_thread(
                store.record_request,
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                request_payload=request_payload,
                response_payload=response_payload,
            )
            stored_response = Response(
                content=response_payload,
                status_code=response.status_code,
                background=response.background,
            )
            stored_response.raw_headers = response_headers
            response = stored_response
        await asyncio.to_thread(
            log_event,
            "http_request_completed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        AUDIT_EVENTS.labels(event="completed").inc()
        return response
