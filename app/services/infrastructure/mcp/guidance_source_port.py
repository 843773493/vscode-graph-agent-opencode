"""MCP 工具指引的 tail_only typed provenance 唯一提交 port。

OpenSpec add-context-injection-lifecycle E4：指引作为 CSM source 必须由
source owner 显式声明 root_placement=tail_only，不得由消费侧从路径、
wire role 或自由 extensions 推断。本 port 是 mcp 包与上下文 owner 之间
的唯一 typed 边界：生产实现由 CSM 侧 owner 提供并走既有 typed 注册路径；
mcp 包本身不 import CSM、不触 SQLite、不建第二 writer。port 提交失败
必须在 snapshot 持久化之前显式抛出，不产生半发布。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

MCP_GUIDANCE_SOURCE_ID = "mcp-tool-guidance:v1"


@dataclass(frozen=True, slots=True)
class McpToolGuidanceSourceRegistration:
    """一次激活边界冻结提交的指引 source 登记；root_placement 固定 tail_only。"""

    source_id: str
    activation_snapshot_id: str
    catalog_revision: str
    guidance_revision: str
    provenance_hash: str
    root_placement: Literal["tail_only"] = "tail_only"

    def __post_init__(self) -> None:
        for field_name in (
            "source_id",
            "activation_snapshot_id",
            "catalog_revision",
            "guidance_revision",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"guidance source registration.{field_name} 必须是非空字符串"
                )
        if self.root_placement != "tail_only":
            raise ValueError(
                "MCP 工具指引 root_placement 固定为 tail_only，不得提升: "
                f"{self.root_placement!r}"
            )
        if not self.provenance_hash.startswith("sha256:"):
            raise ValueError(
                "guidance source registration.provenance_hash 必须是 sha256 形式: "
                f"{self.provenance_hash!r}"
            )


class McpToolGuidanceSourcePort(Protocol):
    """CSM 侧 owner 实现的 typed 提交口；实现必须走既有 typed 注册路径。"""

    def register_tail_only_guidance(
        self, registration: McpToolGuidanceSourceRegistration
    ) -> None:
        """按冻结 snapshot 提交 tail_only 指引登记；失败显式抛出。"""
