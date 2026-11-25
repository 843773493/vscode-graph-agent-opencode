"""候选的冻结来源与结果引用；任务状态仍由原后台任务 owner 持有。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import copy_json_value, validate_hash_token
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.serde.registry import parse_context_ref, parse_contribution
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class CanonicalTextRange:
    """单一纯文本原件的字符范围；引用仍指向未修改的 canonical Item。"""

    ref: ContextRef
    start: int
    end: int
    text_length: int

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ContextRef) or self.ref.ref_type != "canonical_item":
            raise ValueError("compaction-text-range-invalid: 分片必须绑定 canonical 原件")
        if (any(type(value) is not int for value in (self.start, self.end, self.text_length))
                or not 0 <= self.start < self.end <= self.text_length):
            raise ValueError("compaction-text-range-invalid: 字符范围必须非空且位于原件内")

    def to_dict(self) -> dict[str, object]:
        return {"ref": self.ref.to_dict(), "start": self.start, "end": self.end, "text_length": self.text_length}

    @classmethod
    def from_dict(cls, value: object) -> CanonicalTextRange:
        if not isinstance(value, Mapping) or set(value) != {"ref", "start", "end", "text_length"}:
            raise ValueError("compaction-text-range-invalid: 文本范围字段非法")
        return cls(parse_context_ref(value["ref"]), value["start"], value["end"], value["text_length"])


def summary_output_text(item: CanonicalItemRecord) -> str:
    """只接受摘要输出的真实文本形态；不把媒体、reasoning 或任意结构转成正文。"""
    if item.semantic_kind != "assistant_output" or item.status != "completed":
        raise ValueError("compaction-result-unconfirmed: 摘要输出不是已完成的 assistant_output")
    payload = item.payload
    blocks = payload if isinstance(payload, (list, tuple)) else (payload,)
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            raise ValueError("compaction-result-not-text: 摘要输出包含非文本内容")
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class CompactionPredecessor:
    """只冻结一个直接前序；覆盖仍在候选 refs 中扁平保存。"""

    candidate_id: str
    summary_item_id: str
    result_ref: DetailRef
    coverage_count: int

    def __post_init__(self) -> None:
        _non_empty_string(self.candidate_id, "predecessor.candidate_id")
        if self.summary_item_id != f"item-summary-{self.candidate_id}":
            raise ValueError("前序摘要必须绑定稳定 canonical Item 身份")
        if not isinstance(self.result_ref, DetailRef) or type(self.coverage_count) is not int or self.coverage_count < 1:
            raise ValueError("前序摘要必须绑定真实结果与非空覆盖")

    def to_dict(self) -> dict[str, object]:
        return {"candidate_id": self.candidate_id, "summary_item_id": self.summary_item_id,
                "result_ref": self.result_ref.to_dict(), "coverage_count": self.coverage_count}

    @classmethod
    def from_dict(cls, value: object) -> CompactionPredecessor:
        if not isinstance(value, Mapping) or set(value) != {"candidate_id", "summary_item_id", "result_ref", "coverage_count"}:
            raise ValueError("前序压缩依赖格式不受支持")
        return cls(value["candidate_id"], value["summary_item_id"], DetailRef.from_dict(value["result_ref"]), value["coverage_count"])


@dataclass(frozen=True, slots=True)
class FrozenCompactionRange:
    candidate_id: str
    task_id: str
    session_id: str
    thread_id: str
    checkpoint_ns: str
    source_view_id: str
    source_branch_id: str
    model_config_hash: str
    refs: tuple[ContextRef, ...]
    # 只登记摘要实际读取的来源；未读取的 CSM 来源变化不使候选失效。
    source_versions: tuple[tuple[str, str], ...] = ()
    source_assembly_id: str | None = None
    predecessor: CompactionPredecessor | None = None
    target_constraint_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("candidate_id", "task_id", "session_id", "thread_id", "source_view_id", "source_branch_id"):
            _non_empty_string(getattr(self, name), f"FrozenCompactionRange.{name}")
        if not isinstance(self.checkpoint_ns, str):
            raise TypeError("候选 namespace 必须是字符串")
        if self.source_assembly_id is not None:
            _non_empty_string(self.source_assembly_id, "source_assembly_id")
        validate_hash_token(self.model_config_hash, "model_config_hash")
        if self.target_constraint_hash is not None:
            validate_hash_token(self.target_constraint_hash, "target_constraint_hash")
        if not isinstance(self.refs, tuple) or not self.refs or any(
            not isinstance(ref, ContextRef) or ref.ref_type != "canonical_item" for ref in self.refs
        ):
            raise ValueError("候选必须冻结非空、有序的 canonical ContextRef")
        if len({(ref.session_id, ref.thread_id, ref.ref_id) for ref in self.refs}) != len(self.refs):
            raise ValueError("候选来源不得重复")
        if not isinstance(self.source_versions, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2
            or any(not isinstance(value, str) or not value for value in pair)
            for pair in self.source_versions
        ):
            raise ValueError("候选来源版本必须是非空 ID／revision 对")
        if len({pair[0] for pair in self.source_versions}) != len(self.source_versions):
            raise ValueError("候选不得同时读取同一来源的多个版本")
        if self.predecessor is not None:
            if not isinstance(self.predecessor, CompactionPredecessor) or self.predecessor.coverage_count > len(self.refs):
                raise ValueError("前序摘要覆盖不得超过候选扁平范围")
            self.predecessor.result_ref.require_owner(self.session_id, self.thread_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": 1, "candidate_id": self.candidate_id, "task_id": self.task_id,
            "session_id": self.session_id, "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns, "source_view_id": self.source_view_id,
            "source_branch_id": self.source_branch_id, "model_config_hash": self.model_config_hash,
            "refs": [ref.to_dict() for ref in self.refs],
            "source_versions": [list(pair) for pair in self.source_versions], "source_assembly_id": self.source_assembly_id,
            "predecessor": None if self.predecessor is None else self.predecessor.to_dict(),
            "target_constraint_hash": self.target_constraint_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> FrozenCompactionRange:
        if not isinstance(value, Mapping) or set(value) != {
            "format_version", "candidate_id", "task_id", "session_id", "thread_id", "checkpoint_ns",
            "source_view_id", "source_branch_id", "model_config_hash", "refs", "source_versions", "source_assembly_id", "predecessor", "target_constraint_hash",
        } or type(value["format_version"]) is not int or value["format_version"] != 1:
            raise ValueError("候选冻结来源格式不受支持")
        if not isinstance(value["refs"], list) or not isinstance(value["source_versions"], list):
            raise TypeError("候选来源必须是数组")
        if any(not isinstance(pair, list) or len(pair) != 2 for pair in value["source_versions"]):
            raise ValueError("候选来源版本必须是 ID／revision 数组对")
        return cls(
            candidate_id=value["candidate_id"], task_id=value["task_id"],
            session_id=value["session_id"], thread_id=value["thread_id"],
            checkpoint_ns=value["checkpoint_ns"], source_view_id=value["source_view_id"],
            source_branch_id=value["source_branch_id"], model_config_hash=value["model_config_hash"],
            refs=tuple(parse_context_ref(ref) for ref in value["refs"]),
            source_versions=tuple(tuple(pair) for pair in value["source_versions"]), source_assembly_id=value["source_assembly_id"],
            predecessor=None if value["predecessor"] is None else CompactionPredecessor.from_dict(value["predecessor"]),
            target_constraint_hash=value["target_constraint_hash"],
        )


def compaction_selection_ids(frozen: FrozenCompactionRange, selected_ids: Sequence[str]) -> tuple[str, ...] | None:
    """对调用方已冻结的选集识别原 A+B 或稳定 S1+B，不读取当前可变状态。"""
    selected = tuple(selected_ids)
    variants = [tuple(ref.ref_id for ref in frozen.refs)]
    if frozen.predecessor is not None:
        variants.append((frozen.predecessor.summary_item_id, *variants[0][frozen.predecessor.coverage_count:]))
    for ids in variants:
        if ids[0] in selected:
            start = selected.index(ids[0])
            if selected[start:start + len(ids)] == ids:
                return ids
    return None


@dataclass(frozen=True, slots=True)
class FrozenSummaryResult:
    """已保存结果的短期读取值；正文权威仍是 output_refs 指向的有序 canonical Item。"""

    text: str
    dispatch_ref: SealedContextDispatchRef
    result_ref: DetailRef
    output_refs: tuple[ContextRef, ...]
    coverage: tuple[ContextRef, ...]
    source_versions: tuple[tuple[str, str], ...] = ()
    text_ranges: tuple[CanonicalTextRange, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("summary-source-mismatch: 已保存摘要正文为空")
        if not isinstance(self.dispatch_ref, SealedContextDispatchRef) or not isinstance(self.result_ref, DetailRef):
            raise TypeError("summary-source-mismatch: 摘要结果缺少 typed dispatch/detail ref")
        self.result_ref.require_owner(self.dispatch_ref.session_id, self.dispatch_ref.thread_id)
        if not isinstance(self.output_refs, tuple) or not self.output_refs or any(
            not isinstance(ref, ContextRef) or ref.ref_type != "canonical_item"
            or (ref.session_id, ref.thread_id) != (self.result_ref.session_id, self.result_ref.thread_id)
            for ref in self.output_refs
        ) or len({ref.ref_id for ref in self.output_refs}) != len(self.output_refs):
            raise ValueError("summary-source-mismatch: 摘要结果缺少真实 canonical output ref")
        if self.dispatch_ref.turn_id is not None:
            raise ValueError("summary-source-mismatch: 摘要结果不能来自普通 Turn")
        if not isinstance(self.coverage, tuple) or not self.coverage or any(
            not isinstance(ref, ContextRef) or ref.ref_type != "canonical_item"
            for ref in self.coverage
        ):
            raise ValueError("summary-source-mismatch: 摘要结果必须冻结真实 canonical coverage")
        if not isinstance(self.source_versions, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2
            or any(not isinstance(value, str) or not value for value in pair)
            for pair in self.source_versions
        ) or len({pair[0] for pair in self.source_versions}) != len(self.source_versions):
            raise ValueError("summary-source-mismatch: 摘要来源版本必须是唯一非空 ID／revision 对")
        if (not isinstance(self.text_ranges, tuple)
                or any(not isinstance(part, CanonicalTextRange) or part.ref not in self.coverage for part in self.text_ranges)
                or len({part.ref for part in self.text_ranges}) != len(self.text_ranges)):
            raise ValueError("summary-source-mismatch: 摘要文本范围必须唯一且属于真实 coverage")

    def detail(self) -> dict[str, object]:
        return {"output_refs": [ref.to_dict() for ref in self.output_refs], "dispatch_ref": self.dispatch_ref.to_dict(),
                "coverage": [ref.to_dict() for ref in self.coverage]}


@dataclass(frozen=True, slots=True)
class ApplyCompactionCandidateIntent:
    """调用方完成最终请求模拟后，原 checkpoint 事务消费这一个候选。"""

    frozen: FrozenCompactionRange
    expected_view_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.frozen, FrozenCompactionRange):
            raise TypeError("候选采用必须绑定真实冻结范围")
        _non_empty_string(self.expected_view_id, "ApplyCompactionCandidateIntent.expected_view_id")


@dataclass(frozen=True, slots=True)
class ReadyCompactionCandidate:
    """候选查询值；手动采用意图归原候选协调行，不改写冻结生成输入。"""

    frozen: FrozenCompactionRange
    result: FrozenSummaryResult
    apply_on_next_request: bool


@dataclass(frozen=True, slots=True)
class FrozenCompactionTaskInput:
    """原受保护 detail 中的首次任务输入；不复制凭据到 descriptor。"""

    frozen: FrozenCompactionRange
    model_recipe: Mapping[str, object]
    deadline: datetime
    target_constraint: Mapping[str, object] | None = None
    initial_sources: tuple[ContextContribution, ...] = ()
    initial_tool_set: ToolSetSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.frozen, FrozenCompactionRange) or self.deadline.tzinfo is None:
            raise ValueError("压缩任务必须绑定冻结范围与带时区的绝对期限")
        if not isinstance(self.initial_sources, tuple) or any(
            not isinstance(source, ContextContribution) or source.contribution_kind != "prompt"
            for source in self.initial_sources
        ):
            raise ValueError("压缩初始来源必须是当前完整的 prompt 来源")
        if self.initial_tool_set is not None and not isinstance(self.initial_tool_set, ToolSetSnapshot):
            raise TypeError("压缩初始工具必须是原 ToolSetSnapshot")
        if self.initial_sources and self.initial_tool_set is None:
            raise ValueError("压缩当前来源必须同时冻结真实 ToolSet")
        object.__setattr__(self, "model_recipe", copy_json_value(dict(self.model_recipe)))
        if self.target_constraint is not None:
            object.__setattr__(self, "target_constraint", copy_json_value(dict(self.target_constraint)))

    def to_dict(self) -> dict[str, object]:
        return {"format_version": 1, "frozen": self.frozen.to_dict(),
                "model_recipe": copy_json_value(dict(self.model_recipe)), "deadline": self.deadline.isoformat(),
                "target_constraint": None if self.target_constraint is None else copy_json_value(dict(self.target_constraint)),
                "initial_sources": [source.to_dict() for source in self.initial_sources],
                "initial_tool_set": None if self.initial_tool_set is None else {
                    "selection_revision": self.initial_tool_set.selection_revision,
                    "tools": [next(tool for tool in self.initial_tool_set.tools
                                   if tool.get("function", tool).get("name") == name) for name in self.initial_tool_set.wire_tool_order],
                    "policy_keys": list(self.initial_tool_set.policy_keys), "policy_version": self.initial_tool_set.policy_version,
                }}

    @classmethod
    def from_dict(cls, value: object) -> FrozenCompactionTaskInput:
        if (not isinstance(value, Mapping) or set(value) != {"format_version", "frozen", "model_recipe", "deadline", "target_constraint", "initial_sources", "initial_tool_set"}
                or type(value["format_version"]) is not int or value["format_version"] != 1
                or not isinstance(value["model_recipe"], Mapping) or not isinstance(value["deadline"], str)
                or not isinstance(value["initial_sources"], list)):
            raise ValueError("压缩任务冻结输入格式不受支持")
        return cls(FrozenCompactionRange.from_dict(value["frozen"]), value["model_recipe"], datetime.fromisoformat(value["deadline"]), value["target_constraint"],
                   tuple(parse_contribution(source, sealed=False) for source in value["initial_sources"]),
                   None if value["initial_tool_set"] is None else ToolSetSnapshot(**value["initial_tool_set"]))
