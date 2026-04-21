"""GET /api/services  and  GET /api/services/{id}/metrics"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from auth import CurrentUser
from deps import get_os_client, get_redis, get_registry

router = APIRouter(prefix="/api/services", tags=["services"])


async def _health_from_redis(r, service: str) -> str:
    if await r.exists(f"suppress:cooldown:{service}"):
        return "degraded"
    return "healthy"


@router.get("")
async def list_services(_: CurrentUser) -> list[dict]:
    registry = get_registry()
    r = get_redis()
    os_client = get_os_client()

    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    try:
        resp = await os_client.search(
            index="incident-reports",
            body={
                "size": 0,
                "query": {"range": {"timestamp": {"gte": since}}},
                "aggs": {"svcs": {"terms": {"field": "service.keyword", "size": 200}}},
            },
        )
        dynamic = {b["key"] for b in resp["aggregations"]["svcs"]["buckets"]}
    except Exception:
        dynamic = set()

    all_svcs = set(registry.keys()) - {"default"} | dynamic
    result = []
    for svc in sorted(all_svcs):
        info = registry.get(svc, registry.get("default", {}))
        result.append(
            {
                "id": svc,
                "team": info.get("team"),
                "tier": info.get("tier"),
                "oncall": info.get("oncall"),
                "slack_channel": info.get("slack_channel"),
                "health": await _health_from_redis(r, svc),
                "silenced": bool(await r.exists(f"silence:{svc}")),
            }
        )
    return result


@router.get("/{service_id}/metrics")
async def service_metrics(service_id: str, _: CurrentUser) -> dict:
    os_client = get_os_client()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    try:
        resp = await os_client.search(
            index="incident-reports",
            body={
                "size": 200,
                "sort": [{"timestamp": {"order": "asc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"service.keyword": service_id}},
                            {"range": {"timestamp": {"gte": since}}},
                        ]
                    }
                },
                "_source": ["timestamp", "z_score", "observed_metrics", "severity", "anomaly_type"],
            },
        )
        hits = [h["_source"] for h in resp["hits"]["hits"]]
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {"service": service_id, "window_hours": 24, "points": hits}
