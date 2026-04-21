"""GET /api/stream/anomalies — Server-Sent Events for live anomaly feed.

Architecture
------------
One shared AIOKafkaConsumer (started in app lifespan) reads from the
``anomaly-events`` topic.  A background task fans each message out to every
connected client via its own asyncio.Queue.  The SSE endpoint pulls from
that queue and yields text/event-stream chunks.

Authentication
--------------
EventSource (browser) cannot set custom headers, so the JWT is passed as
?token=<jwt>.  The middleware exempts /api/stream/* from header-based auth
so this path is not blocked before we can validate the query param.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

import jwt as pyjwt
from config import JWT_ALGORITHM, JWT_SECRET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])

# ---------------------------------------------------------------------------
# Fan-out bus
# ---------------------------------------------------------------------------

class _FanoutBus:
    """Broadcast Kafka messages to all active SSE subscribers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    async def publish(self, message: str) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer — drop this message for them rather than block
                dead.append(q)
        for q in dead:
            self._queues.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)


bus = _FanoutBus()


# ---------------------------------------------------------------------------
# Kafka consumer background task (started/stopped by app lifespan)
# ---------------------------------------------------------------------------

async def kafka_consumer_task(brokers: str, topic: str, group: str) -> None:
    """Consume anomaly-events indefinitely and push to the fan-out bus."""
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=brokers,
        group_id=group,
        auto_offset_reset="latest",   # only new events while the API is running
        enable_auto_commit=True,
        value_deserializer=lambda b: b.decode("utf-8", errors="replace"),
    )

    backoff = 2
    while True:
        try:
            await consumer.start()
            logger.info("SSE Kafka consumer started — topic=%s group=%s", topic, group)
            backoff = 2
            async for msg in consumer:
                await bus.publish(msg.value)
        except asyncio.CancelledError:
            logger.info("SSE Kafka consumer shutting down")
            break
        except Exception as exc:
            logger.warning("SSE Kafka consumer error: %s — retrying in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            try:
                await consumer.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: str, id: Optional[str] = None) -> str:
    lines = []
    if id:
        lines.append(f"id: {id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    lines.append("\n")
    return "\n".join(lines)


async def _event_stream(
    request: Request,
    q: asyncio.Queue,
    service_filter: Optional[str],
) -> AsyncGenerator[str, None]:
    yield _sse("connected", json.dumps({"subscribers": bus.subscriber_count}))

    try:
        while True:
            if await request.is_disconnected():
                break

            try:
                raw = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # Heartbeat keeps proxies/browsers from timing out the connection
                yield ": heartbeat\n\n"
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if service_filter and payload.get("service") != service_filter:
                continue

            yield _sse(
                event=payload.get("anomaly_type", "anomaly"),
                data=raw,
                id=payload.get("event_id"),
            )
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(q)
        logger.debug("SSE client disconnected, %d remaining", bus.subscriber_count)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get(
    "/anomalies",
    summary="Live anomaly event stream (SSE)",
    response_description="text/event-stream of AnomalyEvent JSON objects",
    responses={
        200: {"content": {"text/event-stream": {}}},
        401: {"description": "Invalid or missing JWT"},
    },
)
async def stream_anomalies(
    request: Request,
    token: str = Query(..., description="JWT bearer token (EventSource cannot set headers)"),
    service: Optional[str] = Query(None, description="Filter by service name"),
) -> StreamingResponse:
    # Validate JWT — same rules as the header-based auth dependency
    try:
        pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    q = bus.subscribe()
    return StreamingResponse(
        _event_stream(request, q, service),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
            "Connection": "keep-alive",
        },
    )
