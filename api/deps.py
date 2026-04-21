"""Async dependency factories with connection pooling."""
from __future__ import annotations

import json
from functools import lru_cache

from opensearchpy import AsyncOpenSearch
from redis.asyncio import Redis, ConnectionPool

from config import (
    OPENSEARCH_URL, OPENSEARCH_USER, OPENSEARCH_PASS,
    REDIS_URL, SERVICE_REGISTRY_PATH,
)


@lru_cache(maxsize=1)
def get_os_client() -> AsyncOpenSearch:
    """AsyncOpenSearch with a persistent connection pool (default: 10 connections)."""
    kwargs: dict = {
        "hosts": [OPENSEARCH_URL],
        "timeout": 5,
        "max_retries": 2,
        "retry_on_timeout": True,
        # opensearch-py async uses aiohttp under the hood; pool size is per-host
        "pool_maxsize": 10,
    }
    if OPENSEARCH_USER and OPENSEARCH_PASS:
        kwargs["http_auth"] = (OPENSEARCH_USER, OPENSEARCH_PASS)
    return AsyncOpenSearch(**kwargs)


@lru_cache(maxsize=1)
def _redis_pool() -> ConnectionPool:
    return ConnectionPool.from_url(
        REDIS_URL,
        decode_responses=True,
        max_connections=20,
    )


def get_redis() -> Redis:
    """Return an async Redis client sharing the module-level connection pool."""
    return Redis(connection_pool=_redis_pool())


@lru_cache(maxsize=1)
def get_registry() -> dict:
    try:
        with open(SERVICE_REGISTRY_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
