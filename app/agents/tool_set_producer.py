"""从图的受信注册目录正向生产 Provider ToolSet，不读取请求工具列表。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.abstractions.tool_selection import ToolSelectionReader, ToolSelectionSnapshot
from app.agents.instruction_producers import build_toolset_policy_keys
from app.agents.policy import ToolMetadata, ToolPolicyResolver
from app.agents.tool_identity import (
    EXTENSION_TOOL_INVOKER_NAME,
    ToolDefinitionLike,
    tool_definition_name,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes


class RegisteredToolSetProducer:
    """schema 在图注册边界冻结；每次新 assembly 只原子读取 selection。"""

    def __init__(
        self, *, agent_id: str, selection_reader: ToolSelectionReader,
        policy_resolver: ToolPolicyResolver,
        tools: Sequence[ToolDefinitionLike], metadata: Mapping[str, ToolMetadata],
    ) -> None:
        self._agent_id = agent_id
        self._selection_reader = selection_reader
        self._policy_resolver = policy_resolver
        names = [tool_definition_name(tool) for tool in tools]
        if not all(names) or len(set(names)) != len(names) or set(names) != set(metadata):
            raise ValueError("受信工具注册目录必须具备唯一工具名与完整 policy metadata")
        self._metadata = dict(metadata)
        self._schemas_json = canonical_json_bytes([
            convert_to_openai_tool(tool) for tool in tools
        ])
        self._structured_tools_json: bytes | None = None

    def freeze_structured_output_tools(self, tools: Mapping[str, BaseTool]) -> None:
        """只冻结图已经注册的结构化输出工具，不从请求或绑定结果补造 schema。"""
        if self._structured_tools_json is not None:
            raise RuntimeError("ToolSet producer 的图结构化输出注册表已冻结")
        if any(name != tool.name for name, tool in tools.items()):
            raise ValueError("source-mismatch: 图结构化输出工具名与注册表不一致")
        normal_names = {
            schema["function"]["name"] for schema in json.loads(self._schemas_json)
        }
        if normal_names.intersection(tools):
            raise ValueError("source-mismatch: 结构化输出工具与普通工具名称重复")
        self._structured_tools_json = canonical_json_bytes([
            convert_to_openai_tool(tool) for tool in tools.values()
        ])

    @property
    def structured_tool_names(self) -> tuple[str, ...]:
        if self._structured_tools_json is None:
            raise RuntimeError("ToolSet producer 尚未绑定图结构化输出注册表")
        return tuple(
            schema["function"]["name"]
            for schema in json.loads(self._structured_tools_json)
        )

    def snapshot(self, include_structured_output: bool = False) -> ToolSetSnapshot:
        if self._structured_tools_json is None:
            raise RuntimeError("ToolSet producer 尚未绑定图结构化输出注册表")
        selection = self._selection_reader.snapshot(self._agent_id)
        if not isinstance(selection, ToolSelectionSnapshot) or selection.agent_id != self._agent_id:
            raise TypeError("ToolSelectionReader 返回了不匹配的原子选择快照")
        execution = selection.execution_overrides
        visibility = selection.model_visibility_overrides
        visible = []
        for schema in json.loads(self._schemas_json):
            name = schema["function"]["name"]
            # 固定扩展信封不依赖内部 target 的数量或开关。
            if name == EXTENSION_TOOL_INVOKER_NAME:
                visible.append(schema)
                continue
            policy = self._policy_resolver.resolve(
                self._metadata[name], execution_override=execution.get(name),
                model_visibility_override=visibility.get(name),
            )
            if policy.model_visible:
                visible.append(schema)
        # 普通工具维持既有 sealed wire 的稳定顺序，再由框架追加结构化工具。
        visible.sort(key=lambda schema: schema["function"]["name"])
        policy_keys = build_toolset_policy_keys([
            schema["function"]["name"] for schema in visible
        ])
        if include_structured_output:
            # 输出 schema 不属于可执行工具 policy，沿用图注册顺序与框架策略。
            visible.extend(json.loads(self._structured_tools_json))
        return ToolSetSnapshot(
            selection_revision=selection.revision, tools=visible,
            policy_keys=policy_keys,
        )


__all__ = ["RegisteredToolSetProducer"]
