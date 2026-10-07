"""Tool producer 正向发布的不可变 ToolSet 快照，不读取 Provider 请求。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.itemized.epoch.prefix_epoch import toolset_compatibility_key
from app.domain.itemized.epoch.root_compilation import (
    SOURCE_POLICY_KEYS,
    SourcePolicyKey,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    sha256_jcs,
    validate_hash_token,
)
from app.domain.itemized.identity.refs import ToolSetRef, _tool_identity
from app.domain.itemized.validation import _non_empty_string


def tool_set_manifest_compatibility_key(ref: ToolSetRef) -> str:
    """从最终 manifest 的非正文 token 联校工具与完整 policy。

    manifest token 已包含 tools、policy 与 version；脱敏后沿用其稳定摘要，
    不能把不可见的 policy 当成空对象重新散列。
    """
    ref.validate_manifest()
    manifest_token = ref.content_hash
    return toolset_compatibility_key(
        ref, policy_hash=sha256_jcs({"tool_manifest": manifest_token}),
    )


@dataclass(frozen=True, slots=True, init=False)
class ToolSetSnapshot:
    """模型调用边界的 desired 快照；正文只有一份规范 JSON。

    selection_revision 是 ToolSelectionStore 的原子快照版本；Provider 可见
    manifest/policy 决定 compatibility key。隐藏工具的执行开关变化可以改变
    selection_revision，但不得借此伪造 ToolSet hard rebase。
    """

    selection_revision: str
    policy_keys: tuple[SourcePolicyKey, ...]
    policy_version: str
    wire_tool_order: tuple[str, ...]
    _tools_json: bytes

    def __init__(
        self,
        *,
        selection_revision: str,
        tools: Sequence[Mapping[str, object]],
        policy_keys: Sequence[SourcePolicyKey] = (),
        policy_version: str = "v1",
    ) -> None:
        validate_hash_token(selection_revision, "ToolSetSnapshot.selection_revision")
        _non_empty_string(policy_version, "ToolSetSnapshot.policy_version")
        keys = tuple(sorted(policy_keys))
        if len(set(keys)) != len(keys) or set(keys) - SOURCE_POLICY_KEYS:
            raise ItemSchemaError("ToolSetSnapshot.policy_keys 必须唯一且已登记")
        if isinstance(tools, (str, bytes)) or not all(
            isinstance(tool, Mapping) for tool in tools
        ):
            raise ItemSchemaError("ToolSetSnapshot.tools 必须是 object 序列")
        # 正文按稳定 identity 规范排序；真实 wire 顺序独立进入同一 manifest，
        # 不由数组规范排序或实时 Provider binding 猜测。
        ordered = sorted(tools, key=_tool_identity)
        object.__setattr__(self, "selection_revision", selection_revision)
        object.__setattr__(self, "policy_keys", keys)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "wire_tool_order", tuple(_tool_identity(tool) for tool in tools))
        object.__setattr__(self, "_tools_json", canonical_json_bytes(ordered))
        names = self.tool_names
        if len(names) != len(set(names)):
            raise ItemSchemaError("ToolSetSnapshot 存在重复工具名")

    @property
    def tools(self) -> tuple[Mapping[str, object], ...]:
        return tuple(json.loads(self._tools_json))

    @property
    def tool_names(self) -> tuple[str, ...]:
        result = []
        for tool in self.tools:
            function = tool.get("function")
            name = function.get("name") if isinstance(function, Mapping) else tool.get("name")
            result.append(_non_empty_string(name, "ToolSetSnapshot.tools[].name"))
        return tuple(result)

    @property
    def revision(self) -> str:
        return sha256_jcs({
            "tools": self.tools,
            "policy_keys": self.policy_keys,
            "policy_version": self.policy_version,
            "wire_tool_order": self.wire_tool_order,
        })

    @property
    def snapshot_id(self) -> str:
        return "tool-set:" + self.revision

    def bind(self, *, session_id: str, thread_id: str, plan_id: str) -> ToolSetRef:
        """只在真正的 ContextStore owner 创建 plan 时取得 plan 归属。"""
        return ToolSetRef.from_tool_snapshot(
            snapshot_id=self.snapshot_id,
            session_id=session_id,
            thread_id=thread_id,
            plan_id=plan_id,
            source_revision=self.revision,
            tools=self.tools,
            tool_policy={
                "enabled_policy_keys": list(self.policy_keys),
                "wire_tool_order": list(self.wire_tool_order),
            },
            tool_policy_version=self.policy_version,
        )

    def compatibility_key(self, ref: ToolSetRef) -> str:
        """兼容键必须使用本快照创建的精确 ToolSetRef，不允许伪造 owner。"""
        if (
            ref.ref_id != self.snapshot_id or ref.source_revision != self.revision
            or ref.tools != self.tools
            or ref.tool_policy != {
                "enabled_policy_keys": list(self.policy_keys),
                "wire_tool_order": list(self.wire_tool_order),
            }
            or ref.tool_policy_version != self.policy_version
        ):
            raise ItemSchemaError("ToolSetSnapshot 与 ToolSetRef manifest 不一致")
        return tool_set_manifest_compatibility_key(ref)
