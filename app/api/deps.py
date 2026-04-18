from __future__ import annotations

from fastapi import Header, HTTPException


def get_request_id(x_request_id: str | None = Header(default=None)) -> str | None:
    return x_request_id


def verify_local_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = "local-dev-token"
    if x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token
