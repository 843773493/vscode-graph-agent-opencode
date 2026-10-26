"""局部 CSM 合同测试的冻结 Skill 输入；激活始终经过正式 load 入口。"""

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    SkillLoadMode,
    SkillLoadReceipt,
    _revision,
)


def load_fixture_skill(
    manager: ContextSourceManager, name: str, body: str, *, mode: SkillLoadMode = "snapshot",
) -> SkillLoadReceipt:
    descriptor = next(item for item in manager.descriptors() if item.name == name)
    identity = descriptor.source_id.removeprefix("skill:")
    binding = SkillCatalogBinding(
        name=name, resource_id=f"{identity}:activation", entry_identity=identity,
        display_uri=descriptor.resource_uri or f"boxteam://workspace/test/resources/skills/{name}/SKILL.md",
        activation_revision=_revision(body), body=body,
        description=descriptor.description,
    )
    manager.install_skill_activation_snapshot(SkillCatalogActivationSnapshot(
        catalog_revision="sha256:test-catalog", entries={name: binding},
    ))
    return manager.load_skill(name, mode=mode)
