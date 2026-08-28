from __future__ import annotations

from app.protocol.generated.boxteam.gateway.v1 import proxy_pb2


def proxy_target_to_proto(
    *,
    workspace_id: str,
    service: str,
    path: str,
) -> proxy_pb2.ProxyTarget:
    if not workspace_id:
        raise ValueError("Gateway ProxyTarget 缺少 workspace_id")
    if not service:
        raise ValueError("Gateway ProxyTarget 缺少 service")
    if not path.startswith("/"):
        raise ValueError(f"Gateway ProxyTarget path 必须以 / 开头: {path}")
    return proxy_pb2.ProxyTarget(
        workspace_id=workspace_id,
        service=service,
        path=path,
    )
