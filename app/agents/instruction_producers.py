"""OpenSpec 4.6：初始 instruction producer 的 typed 注册合同。

设计表 R01–R07/R09 的说明性上下文（Agent 基础说明、runtime identity、条件化
团队规则、Todo、Skill metadata、Filesystem、compact tool 说明和显式 memory）
在这里取得统一的 producer identity、root 资格声明与 ToolSet policy 绑定；
producer 只通过 typed spec/observation 提交 provenance 给唯一
``ContextSourceManager``，不再直接拼 HumanMessage/system 作为隐式来源。

本模块是纯值合同：不执行 I/O、不读写磁盘、不读取自由 metadata/extensions。
``root_placement`` 只能由 source owner 显式声明，CSM 不从文件名、路径、wire
role 或 extensions 推断资格。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.itemized.hashing import sha256_jcs, validate_hash_token
from app.domain.itemized.root_compilation import RootPlacement
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    ApplySourceLifecycleDecision,
    SourceObservation,
    SourceTrackingMode,
    build_source_lifecycle_decision,
)

InstructionProducerId = Literal["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R09"]
"""设计表 4.6 列出的初始 instruction producer 身份。"""

ConditionalPolicyKey = Literal[
    "team_coordination",
    "todo_list",
    "compact_conversation",
    "filesystem_rules",
    "agent_memory",
]
"""条件化 instruction producer 绑定的精确 ToolSet policy key。

未声明 policy_key 的 producer 是无条件 root，不随 ToolSet revision 变化。
"""


class InstructionProducerError(RuntimeError):
    """producer 注册/资格声明的闭合错误；code 固定，便于调用方精确断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class InstructionProducerSpec:
    """一个初始 instruction producer 的静态声明。

    ``revision`` 是该 producer 内容的权威 revision（Agent 配置 revision、
    ToolSet policy revision 或 runtime identity snapshot revision），由 producer
    显式传入，CSM 不重新推导。``content`` 是 producer 当前完整正文，首次组装
    时才由 owner 编译进 root；本模块不负责拼接或落盘。
    """

    producer_id: InstructionProducerId
    source_id: str
    source_kind: str
    name: str
    root_placement: RootPlacement
    content: str
    revision: str
    policy_key: ConditionalPolicyKey | None = None
    wired: bool = True

    def __post_init__(self) -> None:
        if self.producer_id not in _KNOWN_PRODUCER_IDS:
            raise InstructionProducerError(
                "instruction-producer-unknown",
                f"未登记的 instruction producer: {self.producer_id!r}",
            )
        for field_name in ("source_id", "source_kind", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"InstructionProducerSpec.{field_name} 必须是非空字符串"
                )
        if self.root_placement not in {"root_eligible", "tail_only"}:
            raise ValueError(
                "InstructionProducerSpec.root_placement 只能是 "
                f"root_eligible|tail_only: {self.root_placement!r}"
            )
        if self.policy_key is not None and self.policy_key not in _POLICY_KEYS:
            raise ValueError(
                f"InstructionProducerSpec.policy_key 未登记: {self.policy_key!r}"
            )
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("InstructionProducerSpec.content 必须是非空字符串")
        validate_hash_token(self.revision, "InstructionProducerSpec.revision")
        if not isinstance(self.wired, bool):
            raise TypeError("InstructionProducerSpec.wired 必须是 boolean")
        if self.policy_key is not None and self.root_placement != "root_eligible":
            raise InstructionProducerError(
                "instruction-producer-placement-conflict",
                f"producer {self.producer_id} 绑定了 ToolSet policy，"
                "只能在合法 hard rebase epoch 编译为 root_eligible",
            )

    @property
    def content_hash(self) -> str:
        """确定性内容 hash；可按同一 payload 独立重算核对。"""
        return sha256_jcs({"content": self.content})

    @property
    def is_conditional(self) -> bool:
        return self.policy_key is not None

    def revision_token(self) -> str:
        """producer 内容 + 条件绑定的稳定 revision token。

        Root placement 本身不参与：同一正文的资格声明变化由 owner 的 epoch
        边界决定，不得借此伪造新内容 revision。
        """
        return sha256_jcs(
            {
                "kind": "instruction-producer-revision",
                "producer_id": self.producer_id,
                "revision": self.revision,
                "policy_key": self.policy_key,
            }
        )


def assert_producer_registrable(spec: InstructionProducerSpec) -> None:
    """注册门禁：未接线的 producer 不得伪装启用。

    默认未接线的能力（例如生产 runtime 未传入 sources 的 R09 memory）在注册点
    直接失败，而不是登记一个永远不生效的 source 冒充已启用。
    """
    if not isinstance(spec, InstructionProducerSpec):
        raise TypeError("assert_producer_registrable 需要 InstructionProducerSpec")
    if not spec.wired:
        raise InstructionProducerError(
            "instruction-producer-not-wired",
            f"producer {spec.producer_id} 默认未接线，不得注册为已启用 source",
        )


def assert_toolset_policy_binding(
    spec: InstructionProducerSpec,
    enabled_policy_keys: Sequence[ConditionalPolicyKey],
) -> None:
    """条件化规则必须绑定精确 ToolSet policy，否则拒绝编译进 root。

    无条件 producer 不参与该判定；它们的启用不依赖 ToolSet revision。
    """
    assert_producer_registrable(spec)
    keys = set(enabled_policy_keys)
    unknown = keys - _POLICY_KEYS
    if unknown:
        raise InstructionProducerError(
            "instruction-producer-policy-unknown",
            f"ToolSet policy 含未登记 key: {sorted(unknown)}",
        )
    if spec.policy_key is None:
        return
    if spec.policy_key not in keys:
        raise InstructionProducerError(
            "instruction-producer-policy-mismatch",
            f"producer {spec.producer_id} 需要 policy {spec.policy_key}，"
            "当前 ToolSet policy 未启用",
        )


def build_toolset_policy_keys(
    enabled_tool_names: Sequence[str],
) -> tuple[ConditionalPolicyKey, ...]:
    """从最终可见工具名解析条件化 policy key。

    判定只使用工具名这一 ToolSet 事实，producer 正文不参与；返回顺序固定，
    因此同一 ToolSet 得到逐字节一致的 policy 集合。
    """
    names = {name for name in enabled_tool_names if isinstance(name, str) and name}
    keys: list[ConditionalPolicyKey] = []
    if "create_team" in names:
        keys.append("team_coordination")
    if "write_todos" in names:
        keys.append("todo_list")
    if "compact_conversation" in names:
        keys.append("compact_conversation")
    if names & {"ls", "read_file", "write_file", "edit_file", "glob", "grep"}:
        keys.append("filesystem_rules")
    return tuple(keys)


@dataclass(frozen=True, slots=True)
class InstructionProducerObservation:
    """producer 向唯一 CSM 提交的一次 typed observation。

    只承载 typed 字段：owner thread、producer spec、published revision、
    base/delta 关系与激活边界。没有 extensions 入口，因此自由 metadata 无法
    影响 tracking、role、ordinal 或 stable prefix。
    """

    owner: ContextSourceOwnerKey
    spec: InstructionProducerSpec
    revision: str
    tracking_mode: SourceTrackingMode = "tracked"
    activation_boundary: Literal["turn", "model_call"] = "turn"
    from_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ContextSourceOwnerKey):
            raise TypeError(
                "InstructionProducerObservation.owner 必须是 "
                "ContextSourceOwnerKey"
            )
        if not isinstance(self.spec, InstructionProducerSpec):
            raise TypeError(
                "InstructionProducerObservation.spec 必须是 "
                "InstructionProducerSpec"
            )
        assert_producer_registrable(self.spec)
        validate_hash_token(self.revision, "InstructionProducerObservation.revision")
        if self.from_revision is not None:
            validate_hash_token(
                self.from_revision,
                "InstructionProducerObservation.from_revision",
            )
            if self.from_revision == self.revision:
                raise ValueError(
                    "InstructionProducerObservation: from_revision 不得等于 revision"
                )

    @property
    def decision_kind(self) -> Literal["base", "delta"]:
        return "base" if self.from_revision is None else "delta"

    def descriptor(self) -> ContextSourceDescriptor:
        """生成 CSM descriptor；locator 只留在服务端，不进入模型协议。"""
        return instruction_producer_descriptor(self.spec)

    def source_observation(self) -> SourceObservation:
        """生成唯一 CSM 的 typed observation；extensions 保持原生空。"""
        return SourceObservation(
            owner=self.owner,
            source_id=self.spec.source_id,
            source_kind=self.spec.source_kind,
            name=self.spec.name,
            revision=self.revision,
            tracking_mode=self.tracking_mode,
            activation_boundary=self.activation_boundary,
            from_revision=self.from_revision,
            content=self.spec.content,
        )

    def lifecycle_decision(
        self, *, pending_only: bool = False
    ) -> ApplySourceLifecycleDecision:
        """映射为 owner 可消费的 lifecycle decision，复用唯一映射实现。"""
        return build_source_lifecycle_decision(
            self.source_observation(),
            decision_kind=self.decision_kind,
            pending_only=pending_only,
        )


def instruction_producer_descriptor(
    spec: InstructionProducerSpec,
) -> ContextSourceDescriptor:
    """无 owner 上下文的注册入口（首次组装时按 source identity 登记）。"""
    assert_producer_registrable(spec)
    return ContextSourceDescriptor(
        source_id=spec.source_id,
        source_kind=spec.source_kind,
        name=spec.name,
        description=spec.name,
        internal_locator=f"instruction-producer://{spec.producer_id}",
    )


def register_instruction_producer(
    manager: object,
    spec: InstructionProducerSpec,
    *,
    metadata_payload: Mapping[str, object] | None = None,
) -> None:
    """把 producer 登记到唯一 CSM，并拒绝任何自由 metadata 控制字段。

    调用方只能传已封存的描述性 metadata；出现控制 flag/ordinal/tracking/role
    等键时显式失败，不静默忽略也不扩大 extensions 的解释权。
    """
    if metadata_payload is not None:
        forbidden = sorted(set(metadata_payload) & _FORBIDDEN_METADATA_KEYS)
        if forbidden:
            raise InstructionProducerError(
                "instruction-producer-metadata-forbidden",
                "producer 注册不得携带控制字段: "
                f"{forbidden}",
            )
    register = getattr(manager, "register", None)
    if not callable(register):
        raise TypeError("register_instruction_producer 需要唯一 ContextSourceManager")
    register(instruction_producer_descriptor(spec))


_KNOWN_PRODUCER_IDS: frozenset[str] = frozenset(
    {"R01", "R02", "R03", "R04", "R05", "R06", "R07", "R09"}
)
_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "team_coordination",
        "todo_list",
        "compact_conversation",
        "filesystem_rules",
        "agent_memory",
    }
)
_FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "root_placement",
        "tracking",
        "tracking_status",
        "role",
        "wire_role",
        "ordinal",
        "source_ordinal",
        "selection_role",
        "replacement_policy",
        "activation_boundary",
    }
)


__all__ = [
    "ConditionalPolicyKey",
    "InstructionProducerError",
    "InstructionProducerId",
    "InstructionProducerObservation",
    "InstructionProducerSpec",
    "RootPlacement",
    "assert_producer_registrable",
    "assert_toolset_policy_binding",
    "build_toolset_policy_keys",
    "instruction_producer_descriptor",
    "register_instruction_producer",
]
