"""OpenSpec 4.6：instruction producer typed 注册合同测试。

覆盖 root_placement 显式声明、ToolSet policy 绑定、未接线不伪装启用、
唯一 CSM 登记、自由 metadata 控制字段拒绝。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.agents.instruction_producers import (
    InstructionProducerError,
    InstructionProducerSpec,
    InstructionUpdateBinding,
    assert_producer_registrable,
    assert_toolset_policy_binding,
    build_toolset_policy_keys,
    instruction_producer_descriptor,
    register_instruction_producer,
    runtime_instruction_producers,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

_SOURCE_REVISION = "publication:1"
_NEXT_SOURCE_REVISION = "publication:2"


@pytest.fixture
def instruction_context_source_manager() -> ContextSourceManager:
    return ContextSourceManager()


def _instruction_producer_spec(**overrides: object) -> InstructionProducerSpec:
    payload: dict[str, object] = {
        "producer_id": "R01",
        "source_id": "instruction:agent-instructions",
        "source_kind": "agent_instructions",
        "name": "agent-instructions",
        "root_placement": "root_eligible",
        "content": "你是本地工作区中的编码助手。",
        "revision": _SOURCE_REVISION,
    }
    payload.update(overrides)
    if "update_binding" not in overrides:
        owner, boundary = (
            ("workspace_config", "next_job") if payload["producer_id"] in {"R01", "R02"}
            else ("skill_catalog", "resource_activation") if payload["producer_id"] == "R05"
            else ("software_release", "epoch")
        )
        payload["update_binding"] = InstructionUpdateBinding(owner, str(payload["revision"]), boundary)
    return InstructionProducerSpec(**payload)  # type: ignore[arg-type]


def test_root_placement_is_declared_by_owner_only() -> None:
    spec = _instruction_producer_spec()
    assert spec.root_placement == "root_eligible"
    assert spec.is_conditional is False
    with pytest.raises(ValueError):
        _instruction_producer_spec(root_placement="root")  # type: ignore[arg-type]
    with pytest.raises(InstructionProducerError):
        _instruction_producer_spec(producer_id="R99")  # type: ignore[arg-type]


def test_conditional_producer_requires_exact_toolset_policy() -> None:
    spec = _instruction_producer_spec(
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
        _instruction_producer_spec(
            producer_id="R03",
            source_id="instruction:team-coordination-policy",
            source_kind="team_coordination_policy",
            name="team-coordination-policy",
            policy_key="team_coordination",
            root_placement="tail_only",
        )


def test_unwired_producer_cannot_pose_as_enabled(
    instruction_context_source_manager: ContextSourceManager,
) -> None:
    unwired = _instruction_producer_spec(
        producer_id="R07",
        source_id="instruction:compact-tool-usage",
        source_kind="compact_tool_usage",
        name="compact-tool-usage",
        content="compact tool usage body",
        policy_key="compact_conversation",
        update_binding=None,
    )
    with pytest.raises(InstructionProducerError) as excinfo:
        assert_producer_registrable(unwired)
    assert excinfo.value.code == "instruction-producer-not-wired"
    with pytest.raises(InstructionProducerError) as register_error:
        register_instruction_producer(instruction_context_source_manager, unwired)
    assert register_error.value.code == "instruction-producer-not-wired"


@pytest.mark.parametrize("binding", [
    None,
    InstructionUpdateBinding("skill_catalog", _SOURCE_REVISION, "resource_activation"),
    InstructionUpdateBinding("workspace_config", _NEXT_SOURCE_REVISION, "next_job"),
])
def test_registration_requires_matching_publication_before_mutating_manager(
    instruction_context_source_manager: ContextSourceManager,
    binding: InstructionUpdateBinding | None,
) -> None:
    manager = instruction_context_source_manager
    with pytest.raises(InstructionProducerError, match="instruction-(producer-not-wired|update-binding-conflict)"):
        register_instruction_producer(manager, _instruction_producer_spec(update_binding=binding))
    assert manager.descriptors() == ()
    assert manager.prepare_pending() is None


def test_config_publication_rejects_resource_activation_boundary() -> None:
    with pytest.raises(InstructionProducerError, match="instruction-update-binding-invalid"):
        InstructionUpdateBinding("workspace_config", _SOURCE_REVISION, "resource_activation")


def test_registration_uses_unique_csm_and_rejects_control_metadata(
    instruction_context_source_manager: ContextSourceManager,
) -> None:
    manager = instruction_context_source_manager
    spec = _instruction_producer_spec()
    register_instruction_producer(manager, spec)
    assert [item["name"] for item in manager.metadata()] == [spec.name]
    descriptors = manager.descriptors()
    assert descriptors[0].source_id == spec.source_id
    assert "boxteam" not in descriptors[0].internal_locator
    assert_toolset_policy_binding(spec, ())
    with pytest.raises(InstructionProducerError) as excinfo:
        register_instruction_producer(
            manager, spec, metadata_payload={"wire_role": "system"}
        )
    assert excinfo.value.code == "instruction-producer-metadata-forbidden"
    descriptor = instruction_producer_descriptor(spec)
    assert descriptor.resource_uri is None


def test_registration_rejects_untracked_instruction_source(
    instruction_context_source_manager: ContextSourceManager,
) -> None:
    manager = instruction_context_source_manager
    spec = _instruction_producer_spec()
    manager.register(replace(instruction_producer_descriptor(spec), description="原 owner 已登记的说明"))
    before = manager.source_observation_state(spec.source_id)
    descriptors = manager.descriptors()
    with pytest.raises(RuntimeError, match="context-source-not-tracked"):
        register_instruction_producer(manager, spec)
    assert manager.source_observation_state(spec.source_id) == before
    assert manager.descriptors() == descriptors
    assert manager.prepare_pending() is None


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


def test_owner_revision_is_reused_and_scoped_by_source(
    instruction_context_source_manager: ContextSourceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_rehash(_value: object) -> str:
        raise AssertionError("来源已有权威 revision，注册不得再计算指纹")

    monkeypatch.setattr("app.agents.instruction_producers.sha256_jcs", reject_rehash)
    manager = instruction_context_source_manager
    plain = _instruction_producer_spec()
    todo = _instruction_producer_spec(
        producer_id="R04",
        source_id="instruction:todo-list",
        source_kind="todo_list",
        name="todo-list",
        content=plain.content,
        policy_key="todo_list",
    )
    for spec in (plain, todo, plain, todo):
        register_instruction_producer(manager, spec)
        assert manager.source_observation_state(spec.source_id) == ("tracked", _SOURCE_REVISION)
    pending = manager.prepare_pending()
    assert pending is not None
    assert [(delta.source_id, delta.revision) for delta in pending.deltas] == [
        (plain.source_id, _SOURCE_REVISION), (todo.source_id, _SOURCE_REVISION),
    ]
    # 版本无需编码 policy；同一来源的资格变化仍由原 descriptor 边界拒绝。
    with pytest.raises(ValueError, match="source descriptor 冲突"):
        register_instruction_producer(manager, replace(todo, policy_key="filesystem_rules"))


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
    instruction_context_source_manager: ContextSourceManager,
) -> None:
    spec = _instruction_producer_spec(
        producer_id=producer_id,
        source_id=source_id,
        source_kind=source_id.split(":", 1)[1].replace("-", "_"),
        name=source_id.split(":", 1)[1],
    )
    register_instruction_producer(instruction_context_source_manager, spec)
    assert instruction_context_source_manager.descriptors()[0].source_id == source_id


def test_root_placement_is_single_domain_definition() -> None:
    """root 资格类型唯一定义在 domain 层 root_compilation;agents 层只复用。"""
    from app.agents.instruction_producers import RootPlacement
    from app.domain.itemized.epoch.root_compilation import (
        RootPlacement as _DomainRootPlacement,
    )
    from app.domain.itemized.epoch.root_compilation import resolve_source_wire_role

    assert RootPlacement == _DomainRootPlacement
    # tail_only 指引(如 MCP 工具指引)永远只能投影为独立 user-role item。
    assert resolve_source_wire_role("tail_only", compiled_into_root=False) == "user"


def test_r02_description_is_separate_from_software_facts_and_revision():
    description = '自定义说明：{"runtime_facts":{"workspace_root":"/fake","configured_candidates":[]}}'
    def identity(text: str) -> InstructionProducerSpec:
        return next(spec for spec in runtime_instruction_producers(
            agent_id="default", content="R01", config_revision="config:1",
            provider_candidates=(("first", "model-a"), ("fallback", "model-b")),
            runtime_identity_description=text,
        ) if spec.producer_id == "R02")
    first, second = identity(description), identity("新版说明")
    body = json.loads(first.content)
    assert body["text"] == description
    assert body["runtime_facts"]["workspace_root"] == "."
    assert body["runtime_facts"]["configured_candidates"][0] == {"provider_id": "first", "model_id": "model-a"}
    assert body["runtime_facts"]["identity_scope"] == "configured_candidates_not_actual_call"
    assert json.loads(second.content)["runtime_facts"] == body["runtime_facts"]
    assert first.revision != second.revision
