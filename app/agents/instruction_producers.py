"""OpenSpec 4.6：初始 instruction producer 的 typed 注册合同。

设计表 R01–R07 及 R10 的说明性上下文（Agent 基础说明、runtime identity、条件化
团队规则、Todo、Skill metadata、Filesystem、compact tool 说明及 CSM 协议）
在这里取得统一的 producer identity、root 资格声明与 ToolSet policy 绑定；
producer 通过 typed spec 登记 provenance 给唯一
``ContextSourceManager``，不再直接拼 HumanMessage/system 作为隐式来源。

本模块声明 producer 与发布绑定，并通过 register_instruction_producer 登记到 CSM；
source 生命周期决策由 CSM 生成。本模块不执行 I/O、不读写磁盘。
``root_placement`` 只能由 source owner 显式声明，CSM 不从文件名、路径、wire
role 或 extensions 推断资格。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.agents.middleware_prompts import (
    CSM_PROTOCOL_PROMPT,
    RUNTIME_IDENTITY_PROTOCOL,
)
from app.domain.itemized.epoch.root_compilation import (
    SOURCE_POLICY_KEYS,
    RootPlacement,
    SourcePolicyKey,
)
from app.domain.itemized.epoch.source_protocol import SOURCE_PROTOCOL
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.validation import _non_empty_string
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)

InstructionProducerId = Literal["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R10"]
"""设计表 4.6 列出的初始 instruction producer 与 CSM 协议身份。"""




class InstructionProducerError(RuntimeError):
    """producer 注册/资格声明的闭合错误；code 固定，便于调用方精确断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class InstructionUpdateBinding:
    """实际 owner 的发布版本及生效边界；由原配置、软件或 catalog 入口绑定。"""

    owner: Literal["workspace_config", "software_release", "skill_catalog"]
    publication_revision: str
    activation_boundary: Literal["next_job", "epoch", "resource_activation"]

    def __post_init__(self) -> None:
        boundaries = {
            "workspace_config": "next_job",
            "software_release": "epoch",
            "skill_catalog": "resource_activation",
        }
        if boundaries.get(self.owner) != self.activation_boundary:
            raise InstructionProducerError(
                "instruction-update-binding-invalid", "来源 owner 与发布激活边界不匹配",
            )
        _non_empty_string(self.publication_revision, "InstructionUpdateBinding.publication_revision")


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
    policy_key: SourcePolicyKey | None = None
    update_binding: InstructionUpdateBinding | None = None

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
        if self.policy_key is not None and self.policy_key not in SOURCE_POLICY_KEYS:
            raise ValueError(
                f"InstructionProducerSpec.policy_key 未登记: {self.policy_key!r}"
            )
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("InstructionProducerSpec.content 必须是非空字符串")
        _non_empty_string(self.revision, "InstructionProducerSpec.revision")
        if self.update_binding is not None and not isinstance(self.update_binding, InstructionUpdateBinding):
            raise TypeError("InstructionProducerSpec.update_binding 必须是 InstructionUpdateBinding 或 None")
        if self.policy_key is not None and self.root_placement != "root_eligible":
            raise InstructionProducerError(
                "instruction-producer-placement-conflict",
                f"producer {self.producer_id} 绑定了 ToolSet policy，"
                "只能在合法 hard rebase epoch 编译为 root_eligible",
            )

    @property
    def is_conditional(self) -> bool:
        return self.policy_key is not None


def assert_producer_registrable(spec: InstructionProducerSpec) -> None:
    """tracked 注册只接受与实际 producer 匹配的 owner 发布绑定。"""
    if not isinstance(spec, InstructionProducerSpec):
        raise TypeError("assert_producer_registrable 需要 InstructionProducerSpec")
    binding = spec.update_binding
    if binding is None:
        raise InstructionProducerError(
            "instruction-producer-not-wired",
            f"producer {spec.producer_id} 缺少 owner、发布 revision 与激活边界，不得登记 tracked",
        )
    expected_owner = (
        "workspace_config" if spec.producer_id in {"R01", "R02"}
        else "skill_catalog" if spec.producer_id == "R05" else "software_release"
    )
    if binding.owner != expected_owner or (
        spec.producer_id != "R02" and binding.publication_revision != spec.revision
    ):
        raise InstructionProducerError(
            "instruction-update-binding-conflict",
            f"producer {spec.producer_id} 的 owner 或已发布 revision 不匹配",
        )


def assert_toolset_policy_binding(
    spec: InstructionProducerSpec,
    enabled_policy_keys: Sequence[SourcePolicyKey],
) -> None:
    """条件化规则必须绑定精确 ToolSet policy，否则拒绝编译进 root。

    无条件 producer 不参与该判定；它们的启用不依赖 ToolSet revision。
    """
    assert_producer_registrable(spec)
    keys = set(enabled_policy_keys)
    unknown = keys - SOURCE_POLICY_KEYS
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
) -> tuple[SourcePolicyKey, ...]:
    """从最终可见工具名解析条件化 policy key。

    判定只使用工具名这一 ToolSet 事实，producer 正文不参与；返回顺序固定，
    因此同一 ToolSet 得到逐字节一致的 policy 集合。
    """
    names = {name for name in enabled_tool_names if isinstance(name, str) and name}
    keys: list[SourcePolicyKey] = []
    if "create_team" in names:
        keys.append("team_coordination")
    if "write_todos" in names:
        keys.append("todo_list")
    if "compact_conversation" in names:
        keys.append("compact_conversation")
    if names & {"ls", "read_file", "write_file", "edit_file", "glob", "grep"}:
        keys.append("filesystem_rules")
    return tuple(keys)


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
        root_placement=spec.root_placement,
        policy_key=spec.policy_key,
    )


def register_instruction_producer(
    manager: ContextSourceManager,
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
    if not isinstance(manager, ContextSourceManager):
        raise TypeError("register_instruction_producer 需要唯一 ContextSourceManager")
    descriptor = instruction_producer_descriptor(spec)
    assert spec.update_binding is not None
    manager.register(descriptor, tracking_status="tracked",
                     binding_revision=spec.update_binding.publication_revision)
    # source_id 隔离来源，policy 由 descriptor/ToolSet 约束；复用 owner 版本命中原观察状态。
    manager.observe(spec.source_id, spec.content, revision=spec.revision).require_tracked()


def runtime_instruction_producers(
    *, agent_id: str, content: str, config_revision: str,
    provider_candidates: Sequence[tuple[str, str]],
    runtime_identity_description: str,
) -> tuple[InstructionProducerSpec, ...]:
    """分别冻结配置说明和运行时身份，禁止从最终 system prompt 反捕。"""
    if not provider_candidates:
        raise ValueError("运行时身份至少需要一个 provider/model 候选")
    identity_prompt = canonical_json_bytes({
        "protocol": RUNTIME_IDENTITY_PROTOCOL,
        "text": runtime_identity_description,
        "runtime_facts": {
            "agent_id": agent_id, "workspace_root": ".",
            "identity_scope": "configured_candidates_not_actual_call",
            "configured_candidates": [
                {"provider_id": provider_id, "model_id": model_id}
                for provider_id, model_id in provider_candidates
            ],
        },
    }).decode("utf-8")
    config_binding = InstructionUpdateBinding("workspace_config", config_revision, "next_job")
    protocol_revision = sha256_jcs({"protocol": SOURCE_PROTOCOL, "content": CSM_PROTOCOL_PROMPT})
    return (
        InstructionProducerSpec(
            producer_id="R10", source_id="instruction:csm-protocol",
            source_kind="csm_protocol", name="CSM 来源协议",
            root_placement="root_eligible", content=CSM_PROTOCOL_PROMPT,
            revision=protocol_revision,
            update_binding=InstructionUpdateBinding("software_release", protocol_revision, "epoch"),
        ),
        InstructionProducerSpec(
            producer_id="R01", source_id="instruction:agent-instructions",
            source_kind="agent_instructions", name="Agent 基础说明",
            root_placement="root_eligible", content=content, revision=config_revision,
            update_binding=config_binding,
        ),
        InstructionProducerSpec(
            producer_id="R02", source_id="instruction:runtime-identity",
            source_kind="runtime_identity", name="运行时身份与路径",
            root_placement="root_eligible", content=identity_prompt,
            revision=sha256_jcs({
                "agent_id": agent_id, "content": identity_prompt,
                "provider_candidates": provider_candidates,
            }),
            update_binding=config_binding,
        ),
    )


def freeze_initial_derived_sources(
    *, agent_id: str, runtime_config: Mapping[str, object], config_revision: str,
    provider_candidates: Sequence[tuple[str, str]],
) -> tuple[ContextContribution, ...]:
    """无来源 seal 时复用原配置 producer 和纯投影，不构建父 Agent 或消费 CSM。"""
    from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceSnapshot,
        PendingContextSourceBatch,
    )
    from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
        source_contributions,
    )

    prompt, identity = runtime_config["system_prompt"], runtime_config["runtime_identity_description"]
    if not isinstance(prompt, str) or not isinstance(identity, str):
        raise TypeError("派生初始配置缺少已解析的说明及运行时身份")
    specs = runtime_instruction_producers(
        agent_id=agent_id, content=prompt, config_revision=config_revision,
        provider_candidates=provider_candidates, runtime_identity_description=identity,
    )
    return source_contributions(PendingContextSourceBatch(deltas=(), sources=tuple(
        ContextSourceSnapshot(instruction_producer_descriptor(spec), spec.revision, spec.content, None, False)
        for spec in specs
    )))


_KNOWN_PRODUCER_IDS: frozenset[str] = frozenset(
    {"R01", "R02", "R03", "R04", "R05", "R06", "R07", "R10"}
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
    "InstructionProducerError",
    "InstructionProducerId",
    "InstructionProducerSpec",
    "InstructionUpdateBinding",
    "RootPlacement",
    "SourcePolicyKey",
    "assert_producer_registrable",
    "assert_toolset_policy_binding",
    "build_toolset_policy_keys",
    "instruction_producer_descriptor",
    "register_instruction_producer",
    "runtime_instruction_producers",
]
