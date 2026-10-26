"""显式 snapshot 在真实 rewind/compaction transition 期间仍须由同一 seal 接受。"""

import json

import pytest
from langchain_core.messages import AIMessage

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
    source_frames = tuple(
        frame for frame in sealed.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == "skill:one-shot"
    )
    assert len(source_frames) == 1
    assert json.loads(source_frames[0].content)["sources"][0]["body"] == binding.body
    assert source_frames[0].item_ref.source_tracking_at_seal == "untracked"
    assert sealed.seal_binding.prefix.consumed_transition.transition_id == transition[0]
    assert case.manager.prepare_pending() is None
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:one-shot"] == (
        "untracked", binding.activation_revision, binding.activation_revision,
    )
    assert binding.display_uri not in case.publisher.reads
    assert case.prepare("explicit-snapshot-during-" + boundary)["assembly_id"] == sealed.assembly_id
