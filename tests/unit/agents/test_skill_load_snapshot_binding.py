"""skill_load 冻结 SkillCatalog activation snapshot binding 合同测试(OpenSpec 4.3)。"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

import pytest

from app.agents.instruction_producers import (
    InstructionProducerSpec,
    register_instruction_producer,
)
from app.agents.tools.skill_loading import create_skill_load_tool
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)
from tests.support.rollout.skill_catalog import load_fixture_skill
from tests.unit.services.infrastructure.rollout_context.conftest import (
    source_seal_case as source_seal_case,  # noqa: PLC0414 - 当前测试文件独立正式产物
)


@pytest.fixture
def skill_control_admission():
    """CSM 消费者测试显式隔离 Session 生命周期准入。"""
    return lambda _name, _mode: nullcontext()


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("mode", ["snapshot", "tracked", "untrack"])
@pytest.mark.parametrize("instruction_last", [False, True])
async def test_skill_mode_and_cold_restore_preserve_same_name_instruction(
    source_seal_case, skill_control_admission, mode, instruction_last,
):
    manager, _binding = _manager_with_binding(source_seal_case)
    skill = manager.descriptors()[0]
    producer = InstructionProducerSpec(
        producer_id="R01", source_id="instruction:agent-instructions",
        source_kind="agent_instructions", name="shared", content="持续生效的 Agent 指令",
        revision="instruction-owner-v1", root_placement="root_eligible",
    )
    register_instruction_producer(manager, producer)
    if not instruction_last:
        manager.register(skill)
    assert len(manager.descriptors()) == 2
    tool = create_skill_load_tool(manager, control_admission=skill_control_admission)
    initial = json.loads(await tool.ainvoke({"name": "shared", "mode": "tracked"}))
    assert initial["status"] == "loaded", initial
    source_seal_case.seal(manager)
    sealed = source_seal_case.saver._storage.assemblies.list_context_assemblies(
        source_seal_case.owner.session_id, source_seal_case.owner.thread_id,
    )[-1]
    sealed_bytes = canonical_json_bytes(sealed.to_dict())
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    instruction = next(row for row in before if row.source_id == producer.source_id)
    result = json.loads(await tool.ainvoke({"name": "shared", "mode": mode}))
    assert result["error"] is None, result
    assert result["tracked"] is (mode == "tracked")
    after = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    assert next(row for row in after if row.source_id == producer.source_id) == instruction
    cold = source_seal_case.manager()
    assert cold.source_observation_state(producer.source_id) == ("tracked", producer.revision_token())
    assert cold.source_observation_state(skill.source_id)[0] == ("tracked" if mode == "tracked" else "untracked")
    # 冷 owner 重绑 descriptor 后两种来源仍独立，不修改已发布指令或其版本。
    register_instruction_producer(cold, producer)
    cold.register(skill)
    assert {source.source_kind for source in cold.descriptors()} == {"agent_instructions", "skill"}
    restored = source_seal_case.saver.get_context_assembly(
        source_seal_case.owner.session_id, source_seal_case.owner.thread_id, assembly_id=sealed.assembly_id,
    )
    restored.validate_hashes()
    assert canonical_json_bytes(restored.to_dict()) == sealed_bytes


async def test_public_untrack_cannot_target_instruction_without_a_skill(
    source_seal_case, skill_control_admission,
):
    manager = source_seal_case.manager()
    producer = InstructionProducerSpec(
        producer_id="R01", source_id="instruction:agent-instructions",
        source_kind="agent_instructions", name="Agent 说明", content="持续生效的 Agent 指令",
        revision="instruction-owner-v1", root_placement="root_eligible",
    )
    register_instruction_producer(manager, producer)
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    pending = manager.prepare_sources()
    tool = create_skill_load_tool(manager, control_admission=skill_control_admission)
    result = json.loads(await tool.ainvoke({"name": producer.name, "mode": "untrack"}))
    assert result["status"] == "error" and result["error"]["code"] == "skill-not-found"
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == before
    assert manager.prepare_sources() == pending


def _manager_with_binding(
    source_seal_case=None,
    *,
    name: str = "shared",
    body: str = "gateway body\n",
    display_uri: str = "boxteam://gateway/local/resources/skills/shared/SKILL.md",
) -> tuple[ContextSourceManager, SkillCatalogBinding]:
    manager = source_seal_case.manager() if source_seal_case is not None else ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id=f"skill:{name}",
            source_kind="skill",
            name=name,
            description="共享 Skill",
            internal_locator="/.boxteam/skills/shared/SKILL.md",
            resource_uri=display_uri,
        )
    )
    binding = SkillCatalogBinding(
        name=name,
        resource_id=f"skill-entry:gateway:{name}:activation",
        entry_identity=f"skill-entry:gateway:{name}",
        display_uri=display_uri,
        activation_revision=_revision(body),
        body=body,
    )
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:catalog-v1",
            entries={name: binding},
        )
    )
    return manager, binding


@pytest.mark.asyncio
async def test_snapshot_mode_appends_frozen_revision_and_safe_receipt(skill_control_admission):
    manager, binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)

    first = json.loads(await skill_load.ainvoke({"name": "shared"}))

    assert first["status"] == "loaded"
    assert first["append_status"] == "appended"
    assert first["display_uri"] == binding.display_uri
    assert first["revision"] == binding.activation_revision
    assert first["content_hash"] == binding.content_hash
    assert first["tracked"] is False
    # 正文/物理路径/locator 不得出现在任何字段。
    serialized = json.dumps(first)
    assert "gateway body" not in serialized
    assert "/.boxteam" not in serialized


@pytest.mark.asyncio
async def test_frozen_call_is_unaffected_by_same_name_new_revision(skill_control_admission):
    manager, binding = _manager_with_binding()
    catalog = manager.frozen_skill_catalog()
    skill_load = create_skill_load_tool(
        manager, control_admission=skill_control_admission,
        activation_snapshot_resolver=lambda: catalog,
    )

    frozen = json.loads(await skill_load.ainvoke({"name": "shared"}))

    # 新版本的显式加载不改变旧冻结调用的输入。
    load_fixture_skill(manager, "shared", "workspace v2 body\n")

    second = json.loads(await skill_load.ainvoke({"name": "shared"}))

    assert second["status"] == "loaded"
    assert second["error"] is None
    assert second["revision"] == binding.activation_revision
    assert frozen["revision"] == binding.activation_revision


@pytest.mark.asyncio
async def test_binding_mismatch_with_registration_fails_closed(skill_control_admission):
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:shared",
            source_kind="skill",
            name="shared",
            description="共享 Skill",
            internal_locator="/.boxteam/skills/shared/SKILL.md",
            resource_uri="boxteam://workspace/test/resources/skills/shared/SKILL.md",
        )
    )
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:catalog-v1",
            entries={
                "shared": SkillCatalogBinding(
                    name="shared",
                    resource_id="skill-entry:gateway:shared:activation",
                    entry_identity="skill-entry:gateway:shared",
                    display_uri="boxteam://gateway/local/resources/skills/shared/SKILL.md",
                    activation_revision=_revision("body\n"),
                    body="body\n",
                )
            },
        )
    )
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)

    result = json.loads(await skill_load.ainvoke({"name": "shared"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-catalog-snapshot-conflict"


@pytest.mark.asyncio
async def test_missing_snapshot_fails_closed_without_catalog_fallback(skill_control_admission):
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:shared",
            source_kind="skill",
            name="shared",
            description="共享 Skill",
            internal_locator="/.boxteam/skills/shared/SKILL.md",
        )
    )
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)

    for mode in ("snapshot", "tracked"):
        result = json.loads(await skill_load.ainvoke({"name": "shared", "mode": mode}))
        assert result["status"] == "error"
        assert result["error"]["code"] == "skill-catalog-snapshot-conflict"


@pytest.mark.asyncio
async def test_unknown_name_returns_skill_not_found(skill_control_admission):
    manager, _binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)

    result = json.loads(await skill_load.ainvoke({"name": "missing"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-not-found"


@pytest.mark.asyncio
async def test_untrack_freezes_original_registration_not_current_catalog(source_seal_case, skill_control_admission):
    manager, binding = _manager_with_binding(source_seal_case)
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)
    assert json.loads(await skill_load.ainvoke({"name": "shared", "mode": "tracked"}))[
        "tracked"
    ] is True
    tracked_batch = manager.prepare_pending()
    assert tracked_batch is not None
    source_seal_case.seal(manager)

    # 同名高优先级覆盖:当前 catalog 已指向 workspace entry,untrack 仍冻结原 gateway binding。
    rebound = SkillCatalogBinding(
        name="shared",
        resource_id="skill-entry:workspace:shared:activation",
        entry_identity="skill-entry:workspace:shared",
        display_uri="boxteam://workspace/ws-id/resources/skills/shared/SKILL.md",
        activation_revision=_revision("workspace body\n"),
        body="workspace body\n",
    )
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:catalog-v2",
            entries={"shared": rebound},
        )
    )

    result = json.loads(await skill_load.ainvoke({"name": "shared", "mode": "untrack"}))

    assert result["status"] == "loaded"
    assert result["tracked"] is False
    assert result["append_status"] == "none"
    # 未向上下文追加任何新 item,也未读取 source。
    assert manager.prepare_pending() is None
    assert manager.observe("skill:shared", "workspace body\n").status == "not_tracked"
    assert binding.display_uri in json.dumps(result)


@pytest.mark.asyncio
async def test_explicit_snapshot_supersedes_pending_tracked_revision(skill_control_admission):
    manager, _binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager, control_admission=skill_control_admission)
    assert json.loads(await skill_load.ainvoke({"name": "shared", "mode": "tracked"}))[
        "tracked"
    ] is True
    manager.observe("skill:shared", "workspace v2\n").require_tracked()

    # 本次 snapshot 采用调用冻结的旧版本，并停止 tracking、替换未封存候选。
    result = json.loads(await skill_load.ainvoke({"name": "shared"}))

    assert result["status"] == "loaded"
    assert result["error"] is None
    assert result["tracked"] is False
    assert result["revision"] == _binding.activation_revision
    pending = manager.prepare_pending()
    assert pending is not None
    assert pending.sources[0].content == _binding.body
