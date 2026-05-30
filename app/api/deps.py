from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.services.config_service import ConfigService


def get_request_id(x_request_id: str | None = Header(default=None)) -> str | None:
    return x_request_id


def verify_local_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = "local-dev-token"
    if x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token


def get_config_service(request: Request) -> ConfigService:
    config_service = getattr(request.app.state, "config_service", None)
    if not isinstance(config_service, ConfigService):
        raise RuntimeError("ConfigService 尚未在应用启动阶段初始化")
    return config_service
