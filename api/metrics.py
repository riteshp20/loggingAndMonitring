"""
Prometheus metrics registry for the Dashboard API service.

Metrics exposed
───────────────
  http_request_duration_seconds  Histogram — per-route request latency (p50/p99/max)
  http_requests_total            Counter   — total requests by method, path, status code
  http_errors_total              Counter   — 4xx/5xx responses by method, path, status
  active_sse_connections         Gauge     — currently open /api/stream/anomalies connections

The /metrics endpoint is mounted directly on the FastAPI app as a plain route that
returns the Prometheus text format — no separate HTTP server needed for an async app.

Path normalisation: variable segments like /api/reports/abc-123 are collapsed to
/api/reports/{anomaly_id} so cardinality stays bounded.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match
from starlette.types import ASGIApp

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ── metric definitions ────────────────────────────────────────────────────────

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by method and normalised path",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests by method, normalised path, and status code",
    ["method", "path", "status"],
)

HTTP_ERRORS_TOTAL = Counter(
    "http_errors_total",
    "HTTP 4xx/5xx responses by method, normalised path, and status code",
    ["method", "path", "status"],
)

ACTIVE_SSE_CONNECTIONS = Gauge(
    "active_sse_connections",
    "Number of currently open SSE /api/stream/anomalies connections",
)

# ── path normalisation ────────────────────────────────────────────────────────

# UUIDs and other dynamic segments that should be collapsed
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_NUMERIC_RE = re.compile(r"/\d+")


def _normalise_path(path: str) -> str:
    """Collapse UUID and numeric path segments to keep Prometheus cardinality low."""
    path = _UUID_RE.sub("{id}", path)
    path = _NUMERIC_RE.sub("/{id}", path)
    return path


# ── middleware ────────────────────────────────────────────────────────────────

# Paths that don't need per-route tracking (very high frequency / internal)
_SKIP_PATHS = frozenset(["/api/health", "/metrics", "/docs", "/redoc", "/openapi.json"])


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record request duration and counts for every non-internal route."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        normalised = _normalise_path(path)
        method = request.method

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        status = str(response.status_code)
        REQUEST_DURATION.labels(method=method, path=normalised).observe(duration)
        REQUESTS_TOTAL.labels(method=method, path=normalised, status=status).inc()

        if response.status_code >= 400:
            HTTP_ERRORS_TOTAL.labels(method=method, path=normalised, status=status).inc()

        return response


# ── /metrics route handler ────────────────────────────────────────────────────

async def metrics_endpoint(request: Request) -> Response:
    """Return Prometheus text exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
