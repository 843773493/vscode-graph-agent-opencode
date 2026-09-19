"""skill_load 冻结 SkillCatalog activation snapshot binding 合同测试(OpenSpec 4.3)。"""

from __future__ import annotations

import hashlib
import json

from app.agents.tools.skill_loading import create_skill_load_tool
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _manager_with_binding(
    *,
    name: str = "shared",
    body: str = "gateway body\n",
    display_uri: str = "boxteam://gateway/local/resources/skills/shared/SKILL.md",
) -> tuple[ContextSourceManager, SkillCatalogBinding]:
    manager = ContextSourceManager()
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


def test_snapshot_mode_appends_frozen_revision_and_safe_receipt():
    manager, binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager)

    first = json.loads(skill_load.invoke({"name": "shared"}))

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


def test_frozen_call_is_unaffected_by_same_name_new_revision():
    manager, binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager)

    frozen = json.loads(skill_load.invoke({"name": "shared"}))

    # 模拟同名 catalog 发布新 revision:live state 被手工推进到新正文。
    manager.activate_skill_content("shared", "workspace v2 body\n")

    second = json.loads(skill_load.invoke({"name": "shared"}))

    assert second["status"] == "error"
    assert second["error"]["code"] == "skill-catalog-snapshot-conflict"
    # 冻结调用的结果不因新 revision 改变。
    assert frozen["revision"] == binding.activation_revision


def test_binding_mismatch_with_registration_fails_closed():
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:shared",
            source_kind="skill",
            name="shared",
            description="共享 Skill",
            internal_locator="/.boxteam/skills/shared/SKILL.md",
            resource_uri="boxteam://workspace/skill/shared",
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
    skill_load = create_skill_load_tool(manager)

    result = json.loads(skill_load.invoke({"name": "shared"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-catalog-snapshot-conflict"


def test_missing_snapshot_fails_closed_without_catalog_fallback():
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
    skill_load = create_skill_load_tool(manager)

    for mode in ("snapshot", "tracked"):
        result = json.loads(skill_load.invoke({"name": "shared", "mode": mode}))
        assert result["status"] == "error"
        assert result["error"]["code"] == "skill-catalog-snapshot-conflict"


def test_unknown_name_returns_skill_not_found():
    manager, _binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager)

    result = json.loads(skill_load.invoke({"name": "missing"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-not-found"


def test_untrack_freezes_original_registration_not_current_catalog():
    manager, binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager)
    assert json.loads(skill_load.invoke({"name": "shared", "mode": "tracked"}))[
        "tracked"
    ] is True
    tracked_batch = manager.prepare_pending()
    assert tracked_batch is not None
    manager.commit_model_call_pending(tracked_batch)

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

    result = json.loads(skill_load.invoke({"name": "shared", "mode": "untrack"}))

    assert result["status"] == "loaded"
    assert result["tracked"] is False
    assert result["append_status"] == "none"
    # 未向上下文追加任何新 item,也未读取 source。
    assert manager.prepare_pending() is None
    assert manager.observe("skill:shared", "workspace body\n") is False
    assert binding.display_uri in json.dumps(result)


def test_tracked_rebind_requires_explicit_tracked_load():
    manager, _binding = _manager_with_binding()
    skill_load = create_skill_load_tool(manager)
    assert json.loads(skill_load.invoke({"name": "shared", "mode": "tracked"}))[
        "tracked"
    ] is True
    manager.activate_skill_content("shared", "workspace v2\n")

    # 冻结 binding 与 live state 不一致时,旧 tracked 调用 fail closed,
    # 不会静默改绑新 entry。
    result = json.loads(skill_load.invoke({"name": "shared"}))

    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-catalog-snapshot-conflict"
