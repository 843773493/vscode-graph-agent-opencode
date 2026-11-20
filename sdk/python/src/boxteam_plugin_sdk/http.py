"""FastAPI 请求 ID 与插件本机管理通道认证。"""

import os
import secrets

from fastapi import Header, HTTPException, Request
from starlette.requests import HTTPConnection


def get_request_id(request: HTTPConnection) -> str:
    """读取 TraceMiddleware 注入的权威 request_id。"""
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("TraceMiddleware 未向当前请求注入 request_id")
    return request_id


def verify_runtime_control_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = os.environ.get("BOXTEAM_RUNTIME_CONTROL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="此入口未配置本机运行时控制凭据")
    if x_local_token is None or not secrets.compare_digest(
        x_local_token.encode("utf-8"), expected.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="invalid runtime control token")
    return x_local_token


def verify_local_control_request(request: Request) -> None:
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers
        or "sec-fetch-site" in request.headers
    ):
        raise HTTPException(status_code=403, detail="运行时控制只接受本机管理通道")


__all__ = ["get_request_id", "verify_local_control_request", "verify_runtime_control_token"]
