"""显式 Skill 刷新、互斥模式和失败原子性；applied 由真实 seal 推进。"""

from dataclasses import replace

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.serde.epoch import context_seal_binding_to_dict
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    SkillCatalogSnapshotConflict,
    _revision,
)


def _catalog(body: str, *, revision: str | None = None) -> SkillCatalogActivationSnapshot:
    binding = SkillCatalogBinding(
        name="demo", resource_id="demo:activation", entry_identity="demo",
        display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
        activation_revision=revision or _revision(body), body=body,
    )
    return SkillCatalogActivationSnapshot("catalog:" + binding.activation_revision, {"demo": binding})


@pytest.fixture
def skill_manager(source_seal_case):
    manager = source_seal_case.manager()
    binding = _catalog("v1\n").entries["demo"]
    manager.register(ContextSourceDescriptor(
        source_id="skill:demo", source_kind="skill", name="demo", description="测试",
        internal_locator=binding.display_uri, resource_uri=binding.display_uri,
    ))
    return manager


@pytest.mark.parametrize("first,second", [("tracked", "snapshot"), ("snapshot", "tracked")])
def test_same_visible_revision_changes_mode_without_new_frame(source_seal_case, skill_manager, first, second):
    manager = skill_manager
    frozen = _catalog("v1\n")
    manager.load_skill("demo", first, activation_snapshot=frozen)
    _, binding = source_seal_case.seal(manager)
    before = canonical_json_bytes(context_seal_binding_to_dict(binding))
    controls = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    for _ in range(2):
        receipt = manager.load_skill("demo", second, activation_snapshot=frozen)
        assert (receipt.mode, receipt.tracked, receipt.append_status, receipt.queued) == (
            second, second == "tracked", "already_active", False,
        )
        assert manager.prepare_pending() is None
    stored = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)[0]
    assert stored.latest_visible_committed_revision == frozen.entries["demo"].activation_revision
    assert stored.state_revision == controls[0].state_revision + 1
    assert canonical_json_bytes(context_seal_binding_to_dict(binding)) == before


@pytest.mark.parametrize("mode", ["snapshot", "tracked"])
def test_explicit_frozen_revision_replaces_automatic_pending(source_seal_case, skill_manager, mode):
    manager = skill_manager
    old = _catalog("v1\n")
    manager.load_skill("demo", "tracked", activation_snapshot=old)
    _, sealed = source_seal_case.seal(manager)
    manager.observe("skill:demo", "automatic-v3\n").require_tracked()
    manager.mark_pending_observation("skill:demo", "automatic-v4")
    explicit = _catalog("explicit-v2\n")
    receipt = manager.load_skill("demo", mode, activation_snapshot=explicit)
    pending = manager.prepare_pending()
    assert pending is not None
    assert pending.sources[0].content == "explicit-v2\n"
    assert pending.sources[0].applied_revision == old.entries["demo"].activation_revision
    assert pending.sources[0].tracked == (mode == "tracked")
    assert receipt.revision == explicit.entries["demo"].activation_revision
    assert manager.pending_observation_count() == 0
    if mode == "snapshot":
        assert manager.observe("skill:demo", "ignored-v5").status == "not_tracked"
    _, refreshed = source_seal_case.seal(manager)
    assert refreshed.prefix.state.frames[:len(sealed.prefix.state.frames)] == sealed.prefix.state.frames
    assert refreshed.prefix.state.frames[-1].item_ref.source_revision == receipt.revision
    assert "explicit-v2" in refreshed.prefix.state.frames[-1].content
    assert "automatic-v3" not in refreshed.prefix.state.frames[-1].content


def test_failed_snapshot_keeps_control_pending_and_published_state(source_seal_case, skill_manager, monkeypatch):
    manager = skill_manager
    manager.load_skill("demo", "tracked", activation_snapshot=_catalog("v1\n"))
    source_seal_case.seal(manager)
    manager.observe("skill:demo", "automatic-v3\n").require_tracked()
    manager.mark_pending_observation("skill:demo", "automatic-v4")
    controls = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    pending = manager.prepare_sources()
    published = dict(manager._published_source_revisions)
    persist = source_seal_case.saver.apply_context_source_control_mutation

    def refuse(_mutation):
        raise RuntimeError("injected activation failure")

    monkeypatch.setattr(source_seal_case.saver, "apply_context_source_control_mutation", refuse)
    with pytest.raises(RuntimeError, match="injected activation failure"):
        manager.load_skill("demo", "snapshot", activation_snapshot=_catalog("explicit-v2\n"))
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == controls
    assert manager.prepare_sources() == pending
    assert manager.pending_observation_count() == 1
    assert manager._published_source_revisions == published
    monkeypatch.setattr(source_seal_case.saver, "apply_context_source_control_mutation", persist)
    receipt = manager.load_skill("demo", "snapshot", activation_snapshot=_catalog("explicit-v2\n"))
    assert receipt.tracked is False
    source_seal_case.seal(manager)


def test_same_revision_different_body_cannot_change_mode(skill_manager):
    manager = skill_manager
    frozen = _catalog("v1\n")
    manager.load_skill("demo", "tracked", activation_snapshot=frozen)
    pending = manager.prepare_sources()
    forged = replace(frozen.entries["demo"], body="forged\n")
    with pytest.raises(SkillCatalogSnapshotConflict, match="同一 Skill revision"):
        manager.load_skill("demo", "snapshot", activation_snapshot=replace(
            frozen, entries={"demo": forged}, sources={forged.display_uri: forged},
        ))
    assert manager.prepare_sources() == pending
    assert manager.source_observation_state("skill:demo")[0] == "tracked"


def test_source_switch_does_not_promote_cancelled_candidate_on_tool_rebase(source_seal_case):
    manager = source_seal_case.manager()
    original = _catalog("sealed-old\n")
    binding = original.entries["demo"]
    manager.register(ContextSourceDescriptor(
        source_id="skill:demo", source_kind="skill", name="demo", description="原来源",
        internal_locator=binding.display_uri, resource_uri=binding.display_uri,
        root_placement="root_eligible",
    ))
    manager.install_skill_activation_snapshot(original)
    manager.load_skill("demo", "tracked")
    source_seal_case.seal(manager)
    manager.observe("skill:demo", "cancelled-auto\n").require_tracked()
    replacement = replace(
        binding, entry_identity="replacement", resource_id="replacement:activation",
        display_uri=binding.display_uri.replace("/demo/", "/replacement/"),
        activation_revision=_revision("new-body\n"), body="new-body\n",
    )
    manager.load_skill("demo", activation_snapshot=SkillCatalogActivationSnapshot("catalog-new", {"demo": replacement}))
    pending = manager.prepare_pending()
    assert pending is not None
    assert {source.descriptor.source_id: source.content for source in pending.sources} == {
        "skill:demo": "sealed-old\n", "skill:replacement": "new-body\n",
    }
    _, sealed = source_seal_case.seal(manager, tool_set=ToolSetSnapshot(
        selection_revision=sha256_jcs({"changed": True}),
        tools=({"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},),
    ))
    assert sealed.prefix.state.epoch.ordinal == 2
    contents = [frame.content for frame in sealed.prefix.state.frames]
    assert all("cancelled-auto" not in content for content in contents if isinstance(content, str))
    assert any("new-body" in content for content in contents if isinstance(content, str))
