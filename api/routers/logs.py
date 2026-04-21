"""GET /api/logs"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from auth import CurrentUser
from deps import get_os_client

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def search_logs(
    _: CurrentUser,
    service: Optional[str] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
) -> dict:
    os_client = get_os_client()
    now = datetime.now(timezone.utc)
    filters: list[dict] = [
        {
            "range": {
                "@timestamp": {
                    "gte": from_ or (now - timedelta(hours=1)).isoformat(),
                    "lte": to or now.isoformat(),
                }
            }
        }
    ]
    if service:
        filters.append({"term": {"service_name.keyword": service}})
    if level:
        filters.append({"term": {"log_level.keyword": level.upper()}})

    try:
        resp = await os_client.search(
            index="logs-*",
            body={
                "from": (page - 1) * size,
                "size": size,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {"bool": {"filter": filters}},
                "_source": [
                    "@timestamp", "service_name", "log_level",
                    "message", "trace_id", "span_id", "error",
                ],
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    total = resp["hits"]["total"]["value"]
    hits = [h["_source"] for h in resp["hits"]["hits"]]
    return {"total": total, "page": page, "size": size, "items": hits}
