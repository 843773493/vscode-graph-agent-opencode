"""Gateway 联邦授权目标的共享值合同；不签发或校验 grant。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class GrantTarget:
    """规范 target：gateway/workspace-qualified session，不含 connection/channel locator。

    ``gateway_id`` 是稳定身份（不是连接、channel 或 route）；它是 operation
    grant 的 audience 判定与 hub transit 转发的路由依据。
    """

    gateway_id: str
    workspace_id: str
    session_id: str

    def to_payload(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class ContextGrantTarget:
    """read grant 的真实上下文资源目标，和 Session 通信地址保持各自字段闭集。"""

    gateway_id: str
    resource_identity: str
    uri: str
    scope: str

    def __post_init__(self) -> None:
        from app.services.business.session_context_query.resources import (
            gateway_session_inventory_identity,
            parse_session_context_ref,
            session_resource_identity,
            workspace_session_inventory_identity,
        )
        resource = parse_session_context_ref(
            resource_identity=self.resource_identity, uri=self.uri, scope=self.scope,
            local_gateway_id=self.gateway_id,
        )
        if resource.gateway_authority is not None:
            raise ValueError("资源 URI authority 与 grant audience 不一致")
        if resource.scope == "workspace":
            if resource.kind == "session" and resource.session_id is not None:
                expected = session_resource_identity(resource.scope_id, resource.session_id)
            elif resource.kind == "workspace_sessions":
                expected = workspace_session_inventory_identity(resource.scope_id)
            else:
                raise ValueError("read grant 只支持现有 Session 上下文资源")
        elif resource.scope == "gateway" and resource.kind == "workspace_sessions" and resource.scope_id == self.gateway_id:
            expected = gateway_session_inventory_identity(self.gateway_id)
        else:
            raise ValueError("read grant 资源 scope 与实际 Gateway 不一致")
        if resource.resource_identity != expected:
            raise ValueError("read grant ResourceIdentity 与 URI owner 不一致")

    def to_payload(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id, "resource_identity": self.resource_identity,
            "uri": self.uri, "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ResourceNavigationGrantTarget:
    """轻量只读导航绑定固定 URI，不为普通文件访问制造持久身份。"""

    gateway_id: str
    uri: str

    def __post_init__(self) -> None:
        from app.core.resource_uri import parse_resource_uri

        resource = parse_resource_uri(self.uri, local_gateway_id=self.gateway_id)
        if resource.gateway_authority is not None or resource.scope not in {"workspace", "gateway"}:
            raise ValueError("导航 URI 必须属于固定 audience 的 Workspace 或 Gateway")
        if resource.scope == "gateway" and resource.scope_id != self.gateway_id:
            raise ValueError("导航 URI 的 Gateway scope 与 audience 不一致")

    def to_payload(self) -> dict[str, object]:
        return {"gateway_id": self.gateway_id, "uri": self.uri}


@dataclass(frozen=True, slots=True)
class TextMutationGrantTarget:
    """独立 file_write grant 只授权固定工作区文件根或单文件。"""

    gateway_id: str
    uri: str
    mutation_kind: Literal["text"] = "text"

    def __post_init__(self) -> None:
        from app.core.resource_uri import parse_resource_uri

        resource = parse_resource_uri(self.uri, local_gateway_id=self.gateway_id)
        if (self.mutation_kind != "text" or resource.gateway_authority is not None
                or resource.scope != "workspace" or resource.kind not in {"files", "attachments"}):
            raise ValueError("文本写入 grant 必须绑定 audience 的 Workspace 文件或附件目标")

    def to_payload(self) -> dict[str, object]:
        return {"gateway_id": self.gateway_id, "uri": self.uri, "mutation_kind": self.mutation_kind}


@dataclass(frozen=True, slots=True)
class ForkGrantTarget:
    """fork capability 的精确领域 URI；来源导出和目标创建分别授权。"""

    gateway_id: str
    uri: str
    fork_role: Literal["source", "target"]

    def __post_init__(self) -> None:
        from app.core.resource_uri import parse_resource_uri, parse_session_thread_uri

        if self.fork_role not in {"source", "target"}:
            raise ValueError("fork grant role 必须是 source 或 target")
        resource = parse_resource_uri(self.uri, local_gateway_id=self.gateway_id)
        if resource.gateway_authority is not None or resource.scope != "workspace":
            raise ValueError("fork grant URI 不属于固定 audience Workspace")
        if self.fork_role == "source":
            parse_session_thread_uri(self.uri, local_gateway_id=self.gateway_id)
        elif resource.kind not in {"session", "folder"}:
            raise ValueError("fork target grant 必须指向 Session 集合、Session 或 Folder 容器")

    def to_payload(self) -> dict[str, object]:
        return {"gateway_id": self.gateway_id, "uri": self.uri, "fork_role": self.fork_role}


FederationGrantTarget: TypeAlias = (
    GrantTarget
    | ContextGrantTarget
    | ForkGrantTarget
    | ResourceNavigationGrantTarget
    | TextMutationGrantTarget
)
