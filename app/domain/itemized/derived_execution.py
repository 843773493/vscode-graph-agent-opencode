"""派生任务的冻结输入；正文由原 rollout detail owner 保存。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.core.identifier import validate_gateway_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import copy_json_value, validate_hash_token
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.serde.registry import parse_context_ref, parse_contribution
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class FrozenDerivedInput:
    task_id: str
    plan_id: str
    session_id: str
    thread_id: str
    checkpoint_ns: str
    source_view_id: str
    source_assembly_id: str | None
    refs: tuple[ContextRef, ...]
    instruction: str
    allowed_tool_ids: tuple[str, ...]
    deadline: datetime
    max_model_calls: int
    submission_hash: str
    model_recipe: Mapping[str, object]
    initial_sources: tuple[ContextContribution, ...] = ()
    summary_result_ref: DetailRef | None = None
    gateway_id: str | None = None
    initial_tool_set: ToolSetSnapshot | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "plan_id", "session_id", "thread_id", "source_view_id", "instruction"):
            _non_empty_string(getattr(self, name), f"FrozenDerivedInput.{name}")
        if not isinstance(self.checkpoint_ns, str):
            raise TypeError("派生 namespace 必须是字符串")
        if self.source_assembly_id is not None:
            _non_empty_string(self.source_assembly_id, "source_assembly_id")
        if not self.refs or any(not isinstance(ref, ContextRef) or ref.ref_type != "canonical_item" for ref in self.refs):
            raise ValueError("派生来源必须是非空 canonical 引用")
        if len({(ref.session_id, ref.thread_id, ref.ref_id) for ref in self.refs}) != len(self.refs):
            raise ValueError("派生来源不得重复")
        if any(not isinstance(tool, str) or not tool for tool in self.allowed_tool_ids) or len(set(self.allowed_tool_ids)) != len(self.allowed_tool_ids):
            raise ValueError("派生允许工具必须是唯一的稳定 ID")
        if self.deadline.tzinfo is None:
            raise ValueError("派生期限必须是带时区的绝对时间")
        if type(self.max_model_calls) is not int or self.max_model_calls < 1:
            raise ValueError("派生必须声明正整数调用上限")
        validate_hash_token(self.submission_hash, "submission_hash")
        if any(not isinstance(source, ContextContribution) or source.contribution_kind != "prompt" for source in self.initial_sources):
            raise ValueError("派生初始来源必须是原配置 producer 的完整 prompt")
        if self.source_assembly_id is not None and self.initial_sources:
            raise ValueError("派生来源已有 seal 时不得再次构造配置 root")
        if self.summary_result_ref is not None:
            self.summary_result_ref.require_owner(self.session_id, self.thread_id)
        if self.gateway_id is not None:
            validate_gateway_id(self.gateway_id)
        if self.initial_tool_set is not None and not isinstance(self.initial_tool_set, ToolSetSnapshot):
            raise TypeError("派生初始工具必须是原 ToolSetSnapshot")
        if self.source_assembly_id is not None and self.initial_tool_set is not None:
            raise ValueError("派生来源已有 seal 时不得重新冻结工具 schema")
        if self.initial_tool_set is not None and set(self.initial_tool_set.tool_names) - set(self.allowed_tool_ids):
            raise ValueError("派生初始可见工具必须属于首次允许列表")
        object.__setattr__(self, "model_recipe", copy_json_value(dict(self.model_recipe)))

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": 1, "task_id": self.task_id, "plan_id": self.plan_id,
            "session_id": self.session_id, "thread_id": self.thread_id, "checkpoint_ns": self.checkpoint_ns,
            "source_view_id": self.source_view_id, "source_assembly_id": self.source_assembly_id,
            "refs": [ref.to_dict() for ref in self.refs], "instruction": self.instruction,
            "allowed_tool_ids": list(self.allowed_tool_ids), "deadline": self.deadline.isoformat(),
            "max_model_calls": self.max_model_calls, "submission_hash": self.submission_hash,
            "model_recipe": copy_json_value(dict(self.model_recipe)),
            "initial_sources": [source.to_dict() for source in self.initial_sources],
            "summary_result_ref": None if self.summary_result_ref is None else self.summary_result_ref.to_dict(),
            "gateway_id": self.gateway_id,
            "initial_tool_set": None if self.initial_tool_set is None else {
                "selection_revision": self.initial_tool_set.selection_revision,
                "tools": [next(tool for tool in self.initial_tool_set.tools
                               if tool.get("function", tool).get("name") == name) for name in self.initial_tool_set.wire_tool_order],
                "policy_keys": list(self.initial_tool_set.policy_keys), "policy_version": self.initial_tool_set.policy_version,
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> FrozenDerivedInput:
        fields = {"format_version", "task_id", "plan_id", "session_id", "thread_id", "checkpoint_ns",
                  "source_view_id", "source_assembly_id", "refs", "instruction", "allowed_tool_ids",
                  "deadline", "max_model_calls", "submission_hash", "model_recipe", "initial_sources", "summary_result_ref", "gateway_id", "initial_tool_set"}
        if not isinstance(value, Mapping) or set(value) != fields or type(value["format_version"]) is not int or value["format_version"] != 1:
            raise ValueError("派生冻结输入格式不受支持")
        if any(not isinstance(value[name], list) for name in ("refs", "allowed_tool_ids", "initial_sources")):
            raise TypeError("派生冻结输入的引用和允许工具必须是 array")
        if not isinstance(value["deadline"], str):
            raise TypeError("派生冻结输入的期限必须是 ISO 时间字符串")
        return cls(**{key: item for key, item in value.items()
                      if key not in {"format_version", "refs", "allowed_tool_ids", "deadline", "initial_sources", "summary_result_ref", "initial_tool_set"}},
                   initial_tool_set=None if value["initial_tool_set"] is None else ToolSetSnapshot(**value["initial_tool_set"]),
                   refs=tuple(parse_context_ref(ref) for ref in value["refs"]),
                   initial_sources=tuple(parse_contribution(source, sealed=False) for source in value["initial_sources"]),
                   summary_result_ref=None if value["summary_result_ref"] is None else DetailRef.from_dict(value["summary_result_ref"]),
                   allowed_tool_ids=tuple(value["allowed_tool_ids"]), deadline=datetime.fromisoformat(value["deadline"]))
