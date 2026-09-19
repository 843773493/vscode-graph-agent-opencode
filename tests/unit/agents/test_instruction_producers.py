"""OpenSpec 4.6：instruction producer typed 注册合同测试。

覆盖 root_placement 显式声明、ToolSet policy 绑定、未接线不伪装启用、
typed observation 复用唯一 CSM 映射、自由 metadata 控制字段拒绝。
"""

from __future__ import annotations

import pytest

from app.agents.instruction_producers import (
    InstructionProducerError,
    InstructionProducerObservation,
    InstructionProducerSpec,
    assert_producer_registrable,
    assert_toolset_policy_binding,
    build_toolset_policy_keys,
    instruction_producer_descriptor,
    register_instruction_producer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

_OWNER = ContextSourceOwnerKey(session_id="ses_00000000000040008000000000000001", thread_id="main")
_REVISION = "sha256:jcs:v1:" + "a" * 64
_REVISION_NEXT = "sha256:jcs:v1:" + "b" * 64


def _spec(**overrides: object) -> InstructionProducerSpec:
    payload: dict[str, object] = {
        "producer_id": "R01",
        "source_id": "instruction:agent-instructions",
        "source_kind": "agent_instructions",
        "name": "agent-instructions",
        "root_placement": "root_eligible",
        "content": "你是本地工作区中的编码助手。",
        "revision": _REVISION,
    }
    payload.update(overrides)
    return InstructionProducerSpec(**payload)  # type: ignore[arg-type]


def test_root_placement_is_declared_by_owner_only() -> None:
    spec = _spec()
    assert spec.root_placement == "root_eligible"
    assert spec.is_conditional is False
    assert spec.content_hash.startswith("sha256:jcs:v1:")
    with pytest.raises(ValueError):
        _spec(root_placement="root")  # type: ignore[arg-type]
    with pytest.raises(InstructionProducerError):
        _spec(producer_id="R99")  # type: ignore[arg-type]


def test_conditional_producer_requires_exact_toolset_policy() -> None:
    spec = _spec(
        producer_id="R03",
        source_id="instruction:team-coordination-policy",
        source_kind="team_coordination_policy",
        name="team-coordination-policy",
        content="Team collaboration is event-driven.",
        policy_key="team_coordination",
    )
    assert spec.is_conditional is True
    assert_toolset_policy_binding(spec, ("team_coordination",))
    with pytest.raises(InstructionProducerError) as excinfo:
        assert_toolset_policy_binding(spec, ("todo_list",))
    assert excinfo.value.code == "instruction-producer-policy-mismatch"
    with pytest.raises(InstructionProducerError) as unknown:
        assert_toolset_policy_binding(spec, ("nope",))  # type: ignore[arg-type]
    assert unknown.value.code == "instruction-producer-policy-unknown"
    with pytest.raises(InstructionProducerError):
        _spec(
            producer_id="R03",
            source_id="instruction:team-coordination-policy",
            source_kind="team_coordination_policy",
            name="team-coordination-policy",
            policy_key="team_coordination",
            root_placement="tail_only",
        )


def test_unwired_producer_cannot_pose_as_enabled() -> None:
    memory = _spec(
        producer_id="R09",
        source_id="instruction:agent-memory",
        source_kind="agent_memory",
        name="agent-memory",
        content="memory body",
        policy_key="agent_memory",
        wired=False,
    )
    with pytest.raises(InstructionProducerError) as excinfo:
        assert_producer_registrable(memory)
    assert excinfo.value.code == "instruction-producer-not-wired"
    with pytest.raises(InstructionProducerError) as register_error:
        register_instruction_producer(ContextSourceManager(), memory)
    assert register_error.value.code == "instruction-producer-not-wired"


def test_observation_reuses_single_csm_typed_mapping() -> None:
    spec = _spec()
    base = InstructionProducerObservation(owner=_OWNER, spec=spec, revision=_REVISION)
    assert base.decision_kind == "base"
    decision = base.lifecycle_decision()
    assert decision.decision_kind == "base"
    assert decision.from_revision is None
    assert decision.source_id == spec.source_id
    assert decision.owner.session_id == _OWNER.session_id
    observation = base.source_observation()
    assert observation.extensions == {}
    assert observation.content == spec.content
    assert observation.observation_id.endswith(f"{spec.source_id}:{_REVISION}")
    delta = InstructionProducerObservation(
        owner=_OWNER,
        spec=spec,
        revision=_REVISION_NEXT,
        from_revision=_REVISION,
    )
    assert delta.decision_kind == "delta"
    assert delta.lifecycle_decision(pending_only=True).pending_only is True
    with pytest.raises(ValueError):
        InstructionProducerObservation(
            owner=_OWNER, spec=spec, revision=_REVISION, from_revision=_REVISION
        )


def test_registration_uses_unique_csm_and_rejects_control_metadata() -> None:
    manager = ContextSourceManager()
    spec = _spec()
    register_instruction_producer(manager, spec)
    assert [item["name"] for item in manager.metadata()] == [spec.name]
    descriptors = manager.descriptors()
    assert descriptors[0].source_id == spec.source_id
    assert "boxteam" not in descriptors[0].internal_locator
    assert_toolset_policy_binding(spec, ())
    with pytest.raises(InstructionProducerError) as excinfo:
        register_instruction_producer(manager, spec, metadata_payload={"wire_role": "system"})
    assert excinfo.value.code == "instruction-producer-metadata-forbidden"
    descriptor = instruction_producer_descriptor(spec)
    assert descriptor.resource_uri is None


def test_toolset_policy_keys_are_deterministic_and_content_free() -> None:
    keys = build_toolset_policy_keys(
        ("grep", "write_todos", "create_team", "compact_conversation", "skill_load")
    )
    assert keys == (
        "team_coordination",
        "todo_list",
        "compact_conversation",
        "filesystem_rules",
    )
    assert build_toolset_policy_keys(("skill_load",)) == ()
    assert build_toolset_policy_keys(()) == ()


def test_producer_revision_token_covers_policy_binding() -> None:
    plain = _spec()
    todo = _spec(
        producer_id="R04",
        source_id="instruction:todo-list",
        source_kind="todo_list",
        name="todo-list",
        content=plain.content,
        policy_key="todo_list",
    )
    assert plain.revision_token() != todo.revision_token()
    assert todo.revision_token() == _spec(
        producer_id="R04",
        source_id="instruction:todo-list",
        source_kind="todo_list",
        name="todo-list",
        content=plain.content,
        policy_key="todo_list",
    ).revision_token()


@pytest.mark.parametrize(
    "producer_id, source_id",
    (
        ("R02", "instruction:runtime-identity"),
        ("R05", "instruction:skill-metadata"),
        ("R06", "instruction:filesystem-rules"),
        ("R07", "instruction:compact-tool-usage"),
    ),
)
def test_all_declared_instruction_producers_are_registerable(
    producer_id: str,
    source_id: str,
) -> None:
    manager = ContextSourceManager()
    spec = _spec(
        producer_id=producer_id,
        source_id=source_id,
        source_kind=source_id.split(":", 1)[1].replace("-", "_"),
        name=source_id.split(":", 1)[1],
    )
    register_instruction_producer(manager, spec)
    assert manager.descriptors()[0].source_id == source_id


def test_root_placement_is_single_domain_definition() -> None:
    """root 资格类型唯一定义在 domain 层 root_compilation;agents 层只复用。"""
    from app.agents.instruction_producers import RootPlacement
    from app.domain.itemized.root_compilation import (
        RootPlacement as _DomainRootPlacement,
    )
    from app.domain.itemized.root_compilation import resolve_source_wire_role

    assert RootPlacement == _DomainRootPlacement
    # tail_only 指引(如 MCP 工具指引)永远只能投影为独立 user-role item。
    assert resolve_source_wire_role("tail_only", compiled_into_root=False) == "user"
