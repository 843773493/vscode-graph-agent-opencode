from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException

from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore, credential_file_error

LOCAL_TOKEN = "local-dev-token"


@dataclass(frozen=True, slots=True)
class GatewayAuthContext:
    kind: Literal["local", "federation"]
    peer_gateway_id: str | None = None


def get_gateway_local_token() -> str:
    credential_path = get_gateway_root() / "credentials" / "local-token"
    descriptor: int | None = None
    primary_error: BaseException | None = None
    operation: Literal["read", "write"] = "read"
    stage = "open"
    try:
        try:
            file = credential_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            file = None
        if file is not None:
            with file:
                stage = "permissions"
                require_private_file_access(credential_path, descriptor=file.fileno())
                stage = "decode"
                token = file.read().strip()
                stage = "close"
            stage = "validate"
            if not token:
                raise ValueError("Gateway 本地凭据文件为空")
            return token
        operation, stage = "write", "prepare"
        credential_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(48)
        stage = "create"
        descriptor = os.open(
            credential_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        stage = "open_stream"
        file = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None  # stream 创建成功才转交关闭责任；失败文件留给显式处理。
        with file:
            stage = "permissions"
            restrict_private_path_access(credential_path, descriptor=file.fileno())
            stage = "write"
            file.write(f"{token}\n")
            stage = "close"
        return token
    except (OSError, ValueError, RuntimeError) as error:
        primary_error = credential_file_error(credential_path, "local_token", operation, stage, error)
        raise primary_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = credential_file_error(credential_path, "local_token", "write", "close_descriptor", error)
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "Gateway 本地凭据创建与收尾均失败: artifact=local_token, stage=close_descriptor",
                        [primary_error, cleanup_error],
                    ) from primary_error
                raise cleanup_error from error


def verify_gateway_token(x_local_token: str | None = Header(default=None)) -> str:
    if x_local_token != get_gateway_local_token():
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token


def verify_gateway_access(
    x_local_token: str | None = Header(default=None),
    x_boxteam_federation_token: str | None = Header(default=None),
) -> GatewayAuthContext:
    if x_local_token == get_gateway_local_token():
        return GatewayAuthContext(kind="local")
    if x_boxteam_federation_token is None:
        raise HTTPException(status_code=401, detail="缺少 Gateway 访问凭据")
    store = FederationCredentialStore(
        storage_path=get_gateway_root() / "credentials" / "federation.json"
    )
    try:
        credential = store.verify(x_boxteam_federation_token)
    except PermissionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    return GatewayAuthContext(
        kind="federation",
        peer_gateway_id=credential.peer_gateway_id,
    )


def verify_federation_token(
    x_boxteam_federation_token: str | None = Header(default=None),
) -> GatewayAuthContext:
    if x_boxteam_federation_token is None:
        raise HTTPException(status_code=401, detail="缺少 Gateway 联邦凭据")
    return verify_gateway_access(
        x_local_token=None,
        x_boxteam_federation_token=x_boxteam_federation_token,
    )
