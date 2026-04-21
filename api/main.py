"""FastAPI backend — observability dashboard API."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import KAFKA_BROKERS, KAFKA_ANOMALY_TOPIC, KAFKA_CONSUMER_GROUP_SSE
from deps import get_os_client, get_redis
from middleware import RateLimitMiddleware, TraceLoggingMiddleware
from routers import anomalies, logs, reports, services, silence, stream
from routers.stream import kafka_consumer_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm connection pools
    r = get_redis()
    await r.ping()
    try:
        await get_os_client().cluster.health(timeout="3s")
    except Exception:
        pass

    # Start Kafka → SSE fan-out in background
    consumer_task = asyncio.create_task(
        kafka_consumer_task(KAFKA_BROKERS, KAFKA_ANOMALY_TOPIC, KAFKA_CONSUMER_GROUP_SSE)
    )

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
    await get_os_client().close()


app = FastAPI(
    title="Log Monitor API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Middleware order: outermost first — logging wraps rate-limit wraps CORS
app.add_middleware(RateLimitMiddleware)
app.add_middleware(TraceLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
    expose_headers=["X-Trace-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

app.include_router(services.router)
app.include_router(silence.router)
app.include_router(anomalies.router)
app.include_router(reports.router)
app.include_router(logs.router)
app.include_router(stream.router)


@app.get("/api/health", tags=["health"], summary="Liveness + readiness probe")
async def health() -> dict:
    """Exempt from authentication and rate limiting."""
    checks: dict[str, str] = {}

    try:
        await get_os_client().cluster.health(timeout="2s")
        checks["opensearch"] = "ok"
    except Exception as exc:
        checks["opensearch"] = f"error: {exc}"

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    ready = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if ready else "degraded",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "checks": checks,
    }
