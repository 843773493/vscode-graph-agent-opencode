"""managed Backend 的唯一句柄、generation 与完整成员合同。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.core.workspace_identity import validate_workspace_id
from app.gateway.runtime.process_lifecycle import ManagedProcessHandle

if TYPE_CHECKING:
    from app.gateway.runtime.workspace import WorkspaceRuntime


@dataclass(frozen=True, slots=True)
class ManagedBackendMember:
    workspace_id: str
    backend_workspace_id: str

    def to_payload(self) -> dict[str, object]:
        return {"workspace_id": self.workspace_id, "backend_workspace_id": self.backend_workspace_id}


@dataclass(frozen=True, slots=True)
class ManagedBackendContract:
    owner_id: str
    generation: str
    members: tuple[ManagedBackendMember, ...]
    backend_url: str
    debug_port: int | None = None

    def __post_init__(self) -> None:
        if not self.owner_id or not self.generation or not self.members:
            raise ValueError("managed Backend 必须有 owner、generation 和完整非空成员")
        if not isinstance(self.owner_id, str) or not isinstance(self.generation, str):
            raise TypeError("managed Backend owner 与 generation 必须为字符串")
        if self.debug_port is not None and (
            isinstance(self.debug_port, bool) or not isinstance(self.debug_port, int) or not 1 <= self.debug_port <= 65535
        ):
            raise ValueError("managed Backend debug_port 必须为合法端口或 null")
        if self.backend_url:
            parsed = urlparse(self.backend_url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
                raise ValueError(f"managed Backend URL 必须为带端口的本机 HTTP 地址: {self.backend_url}")
        for member in self.members:
            if not isinstance(member.workspace_id, str) or not member.workspace_id:
                raise ValueError("managed Backend 成员缺少 Gateway target ID")
            validate_workspace_id(member.backend_workspace_id)
        for values in (
            [member.workspace_id for member in self.members],
            [member.backend_workspace_id for member in self.members],
        ):
            if len(values) != len(set(values)):
                raise ValueError("managed Backend 完整成员合同含重复身份")

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return tuple(member.workspace_id for member in self.members)

    def to_payload(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id, "generation": self.generation,
            "members": [member.to_payload() for member in self.members],
            "backend_url": self.backend_url, "debug_port": self.debug_port,
        }


class ManagedBackendOwner:
    """成员 runtime 只引用本对象；停止后保留最近公布合同供整组恢复。"""

    def __init__(self, contract: ManagedBackendContract) -> None:
        self.contract = contract
        self.process: ManagedProcessHandle | None = None
        self.adopted = False
        self.gateway_generation: str | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self.unpublished_runtimes: dict[str, WorkspaceRuntime] = {}

    def assert_healthy(self) -> None:
        if self.error is not None:
            raise RuntimeError(self.error)
        if self.process is None or not self.process.is_alive():
            raise RuntimeError(f"managed Backend 未运行: owner={self.contract.owner_id}")

    def set_process(self, process: ManagedProcessHandle, *, adopted: bool = False) -> None:
        if self.process is not None:
            raise RuntimeError("managed Backend 已有唯一进程句柄")
        self.process = process
        self.adopted = adopted
        self.error = None

    def close(self, *, timeout_seconds: float = 8) -> None:
        # 失败时保留同一句柄与全部辅助服务，下一次仍从这个关闭边界重试。
        if self.process is not None:
            self.process.close(timeout_seconds=timeout_seconds)
            self.process = None
            self.adopted = False
        errors = []
        for workspace_id, runtime in tuple(self.unpublished_runtimes.items()):
            try:
                runtime.close()
            except Exception as error:  # noqa: BLE001 — 失败句柄留在唯一 owner 中供下一次回收。
                errors.append(f"{workspace_id}: {error}")
            else:
                self.unpublished_runtimes.pop(workspace_id)
        if errors:
            raise RuntimeError("未发布成员辅助服务清理失败: " + "; ".join(errors))

    def detach(self) -> None:
        if self.process is not None:
            self.process.detach()
            self.process = None
            self.adopted = False
