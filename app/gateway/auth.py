from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore


LOCAL_TOKEN = "local-dev-token"


@dataclass(frozen=True, slots=True)
class GatewayAuthContext:
    kind: Literal["local", "federation"]
    peer_gateway_id: str | None = None


def get_gateway_local_token() -> str:
    credential_path = get_gateway_root() / "credentials" / "local-token"
    if credential_path.exists():
        if credential_path.stat().st_mode & 0o077:
            raise PermissionError(
                f"Gateway 本地凭据权限必须为 0600: {credential_path}"
            )
        token = credential_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"Gateway 本地凭据文件为空: {credential_path}")
        return token
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    descriptor = os.open(
        credential_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(f"{token}\n")
    return token


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
