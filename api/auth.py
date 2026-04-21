"""JWT authentication dependency.

Expected token format (HS256):
  {"sub": "<subject>", "exp": <unix-ts>}

Issue tokens out-of-band (e.g. a management script or identity provider).
The /api/health endpoint is exempt from authentication.
"""
from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import JWT_ALGORITHM, JWT_SECRET

_bearer = HTTPBearer(auto_error=True)


def verify_token(
    creds: Annotated[HTTPAuthorizationCredentials, Security(_bearer)],
) -> dict:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))


CurrentUser = Annotated[dict, Depends(verify_token)]
