"""GET /api/reports/{anomaly_id}"""
from fastapi import APIRouter, HTTPException
from auth import CurrentUser
from deps import get_os_client

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/{anomaly_id}")
async def get_report(anomaly_id: str, _: CurrentUser) -> dict:
    os_client = get_os_client()
    try:
        doc = await os_client.get(index="incident-reports", id=anomaly_id)
        return doc["_source"]
    except Exception:
        pass

    try:
        resp = await os_client.search(
            index="incident-reports",
            body={"size": 1, "query": {"term": {"event_id.keyword": anomaly_id}}},
        )
        hits = resp["hits"]["hits"]
        if hits:
            return hits[0]["_source"]
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    raise HTTPException(status_code=404, detail="Report not found")
