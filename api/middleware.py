"""Request logging (with trace IDs) and JWT-aware rate limiting."""
from __future__ import annotations

import logging
import time
import uuid

import jwt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from config import (
    JWT_ALGORITHM, JWT_SECRET,
    RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS,
)
from deps import get_redis

logger = logging.getLogger("api.access")

# Lua script: sliding-window counter using a Redis sorted set.
# Returns 1 (allowed) or 0 (rate-limited).  Atomic — no race conditions.
_RATE_LIMIT_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = tonumber(redis.call('ZCARD', key))
if count < limit then
    redis.call('ZADD', key, now, now .. '-' .. math.random(1, 1000000))
    redis.call('EXPIRE', key, window + 1)
    return count + 1
end
return -1
"""


class TraceLoggingMiddleware(BaseHTTPMiddleware):
    """Attach X-Trace-ID to every request; log method, path, status, latency."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())
        request.state.trace_id = trace_id

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        response.headers["X-Trace-ID"] = trace_id
        logger.info(
            "%s %s %s %.1fms trace=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            trace_id,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limit: RATE_LIMIT_REQUESTS per RATE_LIMIT_WINDOW_SECONDS per JWT subject.

    Unauthenticated requests are rejected before hitting the rate limiter
    (JWT middleware runs first in the dependency chain).  The /api/health
    endpoint is exempt.
    """

    _script_sha: str | None = None

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def _get_sha(self, r) -> str:
        if RateLimitMiddleware._script_sha is None:
            RateLimitMiddleware._script_sha = await r.script_load(_RATE_LIMIT_LUA)
        return RateLimitMiddleware._script_sha

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in ("/api/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # SSE endpoint authenticates via ?token= query param; extract sub from there
        if request.url.path.startswith("/api/stream/"):
            sub = _extract_sub_from_query(request)
        else:
            sub = _extract_sub(request)
        if sub is None:
            # No valid token — let the auth dependency handle the 401
            return await call_next(request)

        r = get_redis()
        sha = await self._get_sha(r)
        now_ms = int(time.time() * 1000)
        window_ms = RATE_LIMIT_WINDOW_SECONDS * 1000
        key = f"rl:{sub}"

        result = await r.evalsha(sha, 1, key, now_ms, window_ms, RATE_LIMIT_REQUESTS)

        if result == -1:
            trace_id = getattr(request.state, "trace_id", "")
            logger.warning("rate_limited sub=%s trace=%s", sub, trace_id)
            return Response(
                content='{"detail":"Rate limit exceeded"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(
            max(0, RATE_LIMIT_REQUESTS - int(result))
        )
        return response


def _extract_sub(request: Request) -> str | None:
    """Pull the JWT `sub` claim from Authorization header — None on any failure."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub", "anonymous")
    except Exception:
        return None


def _extract_sub_from_query(request: Request) -> str | None:
    """Pull the JWT `sub` claim from ?token= query param (used by SSE/EventSource)."""
    token = request.query_params.get("token", "")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub", "anonymous")
    except Exception:
        return None
