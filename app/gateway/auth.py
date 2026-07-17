from __future__ import annotations

from fastapi import Header, HTTPException


LOCAL_TOKEN = "local-dev-token"


def verify_gateway_token(x_local_token: str | None = Header(default=None)) -> str:
    if x_local_token != LOCAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token
