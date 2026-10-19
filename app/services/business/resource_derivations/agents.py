"""AGENTS 原文 facet 与已登记有序指令链的纯业务派生。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.services.infrastructure.resource_platform.derivation.types import (
    SemanticInput,
    SemanticPayload,
)

WORKSPACE_AGENTS_RESOURCE_ID = "workspace-agents:chain:v1"
WORKSPACE_AGENTS_SOURCE_ID = "agents:workspace"


def agents_content(payload: object) -> str:
    """正文只取版本化 loader 已发布的精确文本，不解释扩展或来源 metadata。"""
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ValueError("AGENTS facet payload 必须仅包含 content 文本")
    return payload["content"]


@dataclass(frozen=True, slots=True)
class AgentsFileLoader:
    """整文件 facet 保留已接受 UTF-8 原文，不剥除 BOM、换行或空白。"""

    source_id: str
    version = "agents-raw-file:v1"

    def __call__(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload:
        content = inputs[self.source_id].content
        if not isinstance(content, str):
            return SemanticPayload(available=False, error="AGENTS 已接受来源必须是文本")
        return SemanticPayload(payload={"content": content})


@dataclass(frozen=True, slots=True)
class AgentsChainLoader:
    """按显式登记顺序合成 AGENTS 链，不发现目录或读取当前文件。"""

    resource_ids: tuple[str, ...]
    version = "agents-chain:v1"

    def __call__(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload:
        bodies = [agents_content(inputs[resource_id].content) for resource_id in self.resource_ids]
        return SemanticPayload(payload={"content": "\n\n".join(body for body in bodies if body)})
