"""POST /api/services/{id}/silence"""
from fastapi import APIRouter, Body
from auth import CurrentUser
from deps import get_redis

router = APIRouter(prefix="/api/services", tags=["services"])


@router.post("/{service_id}/silence")
async def silence_service(
    service_id: str,
    _: CurrentUser,
    minutes: int = Body(60, embed=True, ge=1, le=1440),
) -> dict:
    r = get_redis()
    await r.set(f"silence:{service_id}", "1", ex=minutes * 60)
    return {"service": service_id, "silenced_for_minutes": minutes}
