from __future__ import annotations

import hashlib
import re

from app.gateway.schemas import GatewayConnectionKind


_LEGACY_WORKSPACE_ID_PATTERN = re.compile(r"^gw_[0-9a-f]{12}$")
_WORKSPACE_ID_HEX_LENGTH = 32


def build_workspace_id(
    kind: GatewayConnectionKind,
    root_path: str,
    backend_url: str,
) -> str:
    digest = hashlib.sha256(
        f"{kind}\n{root_path}\n{backend_url}".encode("utf-8")
    ).hexdigest()
    return f"gw_{digest[:_WORKSPACE_ID_HEX_LENGTH]}"


def build_ssh_workspace_id(
    *,
    root_path: str,
    host: str,
    port: int,
    username: str,
    remote_backend_host: str,
    remote_backend_port: int,
) -> str:
    signature = "\n".join(
        [
            "ssh",
            root_path,
            f"{username}@{host}:{port}",
            f"{remote_backend_host}:{remote_backend_port}",
        ]
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    return f"gw_{digest[:_WORKSPACE_ID_HEX_LENGTH]}"


def is_legacy_workspace_id(workspace_id: str) -> bool:
    return _LEGACY_WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is not None
