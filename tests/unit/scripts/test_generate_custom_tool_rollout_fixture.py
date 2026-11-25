"""静态历史生成器须走当前候选链，后续 checkpoint 不复活被摘要替换的正文。"""

from itertools import pairwise
from pathlib import Path

import pytest

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from scripts import generate_custom_tool_rollout_fixture as generator
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def fixture_workspace(request: pytest.FixtureRequest) -> TestRunContext:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    return context


@pytest.mark.parametrize("checkpoint_each_turn", [False, True])
def test_generated_compaction_survives_later_checkpoints_and_cold_read(
    fixture_workspace: TestRunContext, checkpoint_each_turn: bool,
) -> None:
    context = fixture_workspace
    sessions = context.workspace_root / ".boxteam/sessions"
    session_id = generator.COMPACTION_SESSION_ID
    generator._create_session(
        sessions_dir=sessions, artifacts_dir=context.artifacts_dir,
        session_id=session_id, title="真实候选历史", turn_count=6,
        compaction_points=(2, 4), checkpoint_each_turn=checkpoint_each_turn,
    )
    with RolloutCheckpointSaver(sessions) as cold:
        thread_id = cold._storage.storage.main_thread_id(session_id)
        owner = MutationIntentOwner(session_id, thread_id)
        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            restored = cold.get_tuple(build_checkpoint_config(thread_id))
            assert restored is not None
            messages = checkpoint_messages(cold, restored)
            assert "user-0006" in {message.id for message in messages}
            assert "user-0001" not in {message.id for message in messages}
        with cold._storage.storage.read_connection(session_id, thread_id, "") as connection:
            candidates = connection.execute(
                "SELECT candidate_id,applied_summary_item_id,applied_view_id "
                "FROM context_compaction_candidates ORDER BY created_at,rowid",
            ).fetchall()
            assert len(candidates) == (3 if checkpoint_each_turn else 2)
            candidate_id, summary_id, view_id = candidates[-1]
            assert candidate_id == f"fixture-{session_id}-0006"
            assert summary_id == f"item-summary-{candidate_id}"
            assert connection.execute(
                "SELECT item_id FROM context_view_items "
                "WHERE view_id=? AND item_id=? AND visible=1 AND context_selected=1",
                (view_id, summary_id),
            ).fetchone() == (summary_id,)
            assert connection.execute(
                "SELECT COUNT(*) FROM checkpoint_channels WHERE channel_name='_summarization_event'",
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM compaction_history_ranges history JOIN context_views view "
                "ON view.control_sequence=history.adopted_control_sequence WHERE view.view_id=?", (view_id,),
            ).fetchone()[0] > 0
            subsequent_checkpoints = connection.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE commit_id >= ("
                "SELECT MIN(checkpoint.commit_id) FROM checkpoints checkpoint "
                "JOIN context_compaction_candidates candidate ON candidate.applied_view_id=checkpoint.view_id) "
                "ORDER BY commit_id",
            ).fetchall()
            versions = [int(row[0].split(".", 1)[0]) for row in connection.execute(
                "SELECT channel.channel_version FROM checkpoints checkpoint JOIN checkpoint_channels channel "
                "ON channel.checkpoint_id=checkpoint.checkpoint_id WHERE channel.channel_name='messages' "
                "ORDER BY checkpoint.commit_id",
            )]
            assert all(right > left for left, right in pairwise(versions))
        # 逐个核对采用后的冻结状态，避免末次压缩掩盖中间 checkpoint 的旧正文复活。
        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            for checkpoint_id, in subsequent_checkpoints:
                historical = cold.get_tuple(build_checkpoint_config(thread_id, checkpoint_id=checkpoint_id))
                assert historical is not None
                assert "user-0001" not in {message.id for message in checkpoint_messages(cold, historical)}
        original, = cold._storage.storage.read_items(session_id, thread_id, item_ids=("item-user-0001",))
        assert original.semantic_kind == "user_input"
        assert original.producer_ref["producer_id"] == "ingress-0001"
        assert original.projection_identity.projection_message_id == "user-0001"
