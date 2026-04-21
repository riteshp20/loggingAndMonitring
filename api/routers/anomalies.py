"""GET /api/anomalies  and  POST /api/alerts/{id}/acknowledge"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from auth import CurrentUser
from deps import get_os_client, get_redis

router = APIRouter(tags=["anomalies"])

_DEFAULT_PAGE = 50


@router.get("/api/anomalies")
async def list_anomalies(
    _: CurrentUser,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    svc: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(_DEFAULT_PAGE, ge=1, le=200),
) -> dict:
    os_client = get_os_client()
    now = datetime.now(timezone.utc)
    filters = [
        {
            "range": {
                "timestamp": {
                    "gte": from_ or (now - timedelta(hours=24)).isoformat(),
                    "lte": to or now.isoformat(),
                }
            }
        }
    ]
    if svc:
        filters.append({"term": {"service.keyword": svc}})

    try:
        resp = await os_client.search(
            index="incident-reports",
            body={
                "from": (page - 1) * size,
                "size": size,
                "sort": [{"timestamp": {"order": "desc"}}],
                "query": {"bool": {"filter": filters}},
                "_source": [
                    "event_id", "report_id", "service", "severity",
                    "anomaly_type", "z_score", "timestamp", "summary",
                ],
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    total = resp["hits"]["total"]["value"]
    hits = [h["_source"] for h in resp["hits"]["hits"]]

    r = get_redis()
    for h in hits:
        eid = h.get("event_id", "")
        h["acknowledged"] = bool(await r.exists(f"ack:{eid}"))

    return {"total": total, "page": page, "size": size, "items": hits}


@router.post("/api/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str, _: CurrentUser) -> dict:
    r = get_redis()
    key = f"ack:{alert_id}"
    if await r.exists(key):
        return {"acknowledged": True, "already_set": True}
    await r.set(key, "1")
    return {"acknowledged": True, "already_set": False}
