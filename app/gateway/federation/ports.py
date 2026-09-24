"""联邦 RPC 依赖的端口协议与稳定身份工具。

把这些定义独立出来，让 channel/RPC 组合根只依赖协议而不依赖具体实现；
实现分别由 ``workspace_port``（本地工作区）与组合根注入。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol


class FederationCatalogPort(Protocol):
    """受控 cold catalog 查询面；只读本地工作区会话目录，不物化 runtime。"""

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]: ...


class FederationSpokeDirectoryPort(Protocol):
    """hub 持有的 active spoke 目录：只暴露稳定 gateway 身份与连接身份。"""

    def active_spokes(self) -> tuple[FederationSpoke, ...]: ...


class FederationSessionMainPort(Protocol):
    """target 侧权威 main thread 解析面（由工作区权威 catalog 提供）。"""

    async def resolve_main_thread(
        self, *, gateway_id: str, workspace_id: str, session_id: str
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class FederationSpoke:
    """hub 视角的直接 spoke：稳定 ``gateway_id`` 与它当前的持久连接身份。"""

    gateway_id: str
    connection_id: str


def principal_ref_for_gateway(gateway_id: str) -> str:
    """不可逆 principal ref：授权判定用，不泄露对端 credential。"""

    digest = hashlib.sha256(f"federation-principal\0{gateway_id}".encode()).hexdigest()
    return f"prn_{digest[:32]}"


def new_origin_nonce() -> str:
    """origin 侧每次网络尝试的新 nonce；业务幂等仍由原 operation 承担。"""

    return secrets.token_urlsafe(24)


__all__ = [
    "FederationCatalogPort",
    "FederationSessionMainPort",
    "FederationSpoke",
    "FederationSpokeDirectoryPort",
    "new_origin_nonce",
    "principal_ref_for_gateway",
]
