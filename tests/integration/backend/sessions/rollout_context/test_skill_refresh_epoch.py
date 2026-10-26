"""显式 snapshot 在真实 rewind/compaction transition 期间仍须由同一 seal 接受。"""

import json

import pytest
from langchain_core.messages import AIMessage

from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,  # noqa: PLC0414 - 使用当前测试文件的正式隔离工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as epoch_db,  # noqa: PLC0414 - epoch_case 的依赖
)


@pytest.mark.parametrize("boundary", ["rewind", "compaction"])
def test_explicit_snapshot_after_pending_transition_is_sealed_once(epoch_case, boundary):
    case = epoch_case
    if boundary == "rewind":
        case.rewind("checkpoint-first")
    else:
        case.checkpoint("refresh-compaction", compaction=True, messages=[
            case.user, AIMessage(content="摘要", id="refresh-summary"),
        ])
    transition = case.pending_rows()[-1]
    assert transition[3:] == ("pending", None)
    binding = case.load_snapshot()
    pending = case.manager.prepare_pending()
    assert pending is not None
    assert any(source.descriptor.source_id == "skill:one-shot" and not source.tracked for source in pending.sources)
    prepared = case.prepare("explicit-snapshot-during-" + boundary)
    sealed = case.snapshot(prepared)
    (ref, block), = ((ref, block) for frame in sealed.seal_binding.prefix.state.frames
                    if frame.role == "user" and frame.source_protocol is not None
                    for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                    if ref.source_identity == "skill:one-shot")
    assert block.body == binding.body
    assert ref.source_tracking_at_seal == "untracked"
    assert sealed.seal_binding.prefix.consumed_transition.transition_id == transition[0]
    assert case.manager.prepare_pending() is None
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:one-shot"] == (
        "untracked", binding.activation_revision, binding.activation_revision,
    )
    assert binding.display_uri not in case.publisher.reads
    assert case.prepare("explicit-snapshot-during-" + boundary)["assembly_id"] == sealed.assembly_id


@pytest.mark.parametrize("boundary", ["rewind", "compaction"])
def test_named_selection_follows_checkpoint_and_does_not_revive_old_source(epoch_case, boundary):
    case = epoch_case
    a = SkillCatalogBinding("switch", "a:activation", "a", "boxteam://workspace/test/resources/skills/a/SKILL.md", "a:1", "选择A\n")
    b = SkillCatalogBinding("switch", "b:activation", "b", "boxteam://workspace/test/resources/skills/b/SKILL.md", "b:1", "选择B\n")
    catalogs = {entry.entry_identity: SkillCatalogActivationSnapshot("catalog:" + entry.entry_identity, {"switch": entry}) for entry in (a, b)}
    snapshots = []
    for index, entry in enumerate((a, b, a)):
        receipt = case.manager.load_skill("switch", activation_snapshot=catalogs[entry.entry_identity])
        assert receipt.queued
        snapshots.append(case.snapshot(case.prepare("choice-" + str(index))))
        case.checkpoint("choice-checkpoint-" + str(index))
    before = tuple(canonical_json_bytes(snapshot.to_dict()) for snapshot in snapshots)
    if boundary == "rewind":
        case.rewind("choice-checkpoint-1")
        expected, excluded = b, a
    else:
        case.checkpoint("choice-compaction", compaction=True, messages=[case.user, AIMessage(content="摘要", id="choice-summary")])
        expected, excluded = a, b
    case.restart()
    rebuilt = case.snapshot(case.prepare("choice-rebuilt-" + boundary))
    blocks = [block for frame in rebuilt.seal_binding.prefix.state.frames if frame.source_protocol is not None
              for block in json.loads(frame.content)["sources"] if block["name"] == "switch"]
    assert [(block["source_identity"], block["body"]) for block in blocks] == [("skill:" + expected.entry_identity, expected.body)]
    assert "skill:" + excluded.entry_identity in case.manager.deselected_skill_source_ids()
    assert tuple(canonical_json_bytes(case.snapshot({"assembly_id": snapshot.assembly_id}).to_dict()) for snapshot in snapshots) == before
    receipt = case.manager.load_skill("switch", activation_snapshot=catalogs[expected.entry_identity])
    assert receipt.append_status == "already_active" and receipt.queued is False
