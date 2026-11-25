from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from itertools import pairwise
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.mapping.itemized.canonical_langchain import project_reasoning_carrier
from tests.harness.performance.record import stage
from tests.harness.python.run_context import TestRunContext
from tests.support.custom_tool_test_workspace import (
    prepare_custom_tool_test_workspace,
)

REAL_SESSION_ID = "ses_019c27f816b17ee394a67f9312bb669e"
STATIC_MOCK_SESSION_ID = "ses_019c642bc2ec78718d0a296b3319d6df"


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    """用 custom_tool_test_workspace 的完整副本作为本文件的集成测试工作区。"""

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    project_root = context.project_root
    with stage("fixture.workspace_setup"):
        workspace_root = prepare_custom_tool_test_workspace(
            workspace_root=context.workspace_root,
            artifacts_dir=context.run_root / "artifacts",
            template_root=project_root / "tests/fixtures/workspaces/custom_tool_test_workspace",
            shared_skill_root=project_root / "resources/skills",
        )
    return str(workspace_root)


@pytest.fixture
def real_rollout_workspace(
    integration_workspace_root_path: str,
) -> Path:
    return Path(integration_workspace_root_path)


def test_custom_tool_fixture_asset_contract(
    real_rollout_workspace: Path,
) -> None:
    manifest_path = real_rollout_workspace / "rollout-fixture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sessions = manifest["sessions"]
    session_ids = {item["session_id"] for item in sessions}
    assert REAL_SESSION_ID in session_ids
    assert STATIC_MOCK_SESSION_ID in session_ids
    assert REAL_SESSION_ID != STATIC_MOCK_SESSION_ID

    sessions_root = real_rollout_workspace / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    for item in sessions:
        session_id = item["session_id"]
        session_node = resolver.resolve_session_node(session_id)
        session_manifest = json.loads(
            (session_node / "session.json").read_text(encoding="utf-8")
        )
        assert session_manifest["workspace_id"] == resolver.catalog_store.get_node(
            session_id
        ).workspace_id
        rollout_root = resolver.resolve_thread_node(
            session_id,
            resolver.main_thread_id(session_id),
        ) / "rollout"
        # SQLite WAL 辅助文件可能在在线访问后保留；context-plan-details 是
        # assembly catalog 按 DetailRef 寻址的正式正文目录。二者均属于
        # rollout 持久化布局，未知文件仍会被严格拒绝。
        children = {child.name: child for child in rollout_root.iterdir()}
        assert {"index.sqlite", "rollout.jsonl"}.issubset(children)
        assert set(children).issubset({
            "index.sqlite",
            "index.sqlite-wal",
            "index.sqlite-shm",
            "rollout.jsonl",
            "context-plan-details",
        })
        assert all(
            children[name].is_file()
            for name in ("index.sqlite", "rollout.jsonl", "index.sqlite-wal", "index.sqlite-shm")
            if name in children
        )
        if "context-plan-details" in children:
            assert children["context-plan-details"].is_dir()
        assert not list(rollout_root.glob("segment-*.jsonl"))
        assert not (session_node / "payloads").exists()

    compact_session_id = "ses_019c3afc2c2674a58a36a8a7b1ad0f8b"
    compact_index = (
        resolver.resolve_thread_node(
            compact_session_id,
            resolver.main_thread_id(compact_session_id),
        )
        / "rollout"
        / "index.sqlite"
    )
    with closing(
        sqlite3.connect(compact_index.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        checkpoint_id = connection.execute(
            "SELECT checkpoint_id FROM checkpoints ORDER BY commit_id DESC LIMIT 1"
        ).fetchone()[0]
        messages_version = connection.execute(
            "SELECT channel_version FROM checkpoint_channels "
            "WHERE checkpoint_id = ? AND channel_name = 'messages'",
            (checkpoint_id,),
        ).fetchone()[0]
        assert str(messages_version).split(".", 1)[0].isdigit()
        applied_candidate, summary_id, applied_view = connection.execute(
            "SELECT candidate.candidate_id,candidate.applied_summary_item_id,candidate.applied_view_id "
            "FROM context_compaction_candidates candidate "
            "JOIN checkpoint_namespace_state namespace ON namespace.checkpoint_ns=candidate.checkpoint_ns "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "WHERE namespace.checkpoint_ns='' AND candidate.applied_view_id=branch.head_view_id "
            "ORDER BY candidate.created_at DESC LIMIT 1",
        ).fetchone()
        assert summary_id == f"item-summary-{applied_candidate}"
        assert connection.execute(
            "SELECT semantic_kind,status FROM item_catalog WHERE item_id=?", (summary_id,),
        ).fetchone() == ("compaction_summary", "completed")
        assert connection.execute(
            "SELECT visible,context_selected FROM context_view_items WHERE view_id=? AND item_id=?",
            (applied_view, summary_id),
        ).fetchone() == (1, 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM compaction_history_ranges history JOIN context_views view "
            "ON view.control_sequence=history.adopted_control_sequence WHERE view.view_id=?", (applied_view,),
        ).fetchone()[0] > 0

def test_provider_128_turn_fixture_preserves_canonical_semantics(
    real_rollout_workspace: Path,
) -> None:
    sessions_dir = real_rollout_workspace / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_dir)
    rollout_root = (
        resolver.resolve_thread_node(
            REAL_SESSION_ID,
            resolver.main_thread_id(REAL_SESSION_ID),
        )
        / "rollout"
    )
    jsonl_path = rollout_root / "rollout.jsonl"
    sqlite_path = rollout_root / "index.sqlite"
    assert jsonl_path.is_file()
    assert sqlite_path.is_file()
    assert not list(rollout_root.glob("segment-*.jsonl"))

    with closing(
        sqlite3.connect(sqlite_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 128
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] >= 128
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM turns WHERE status = 'completed' AND final_message_sequence IS NOT NULL"
            ).fetchone()[0]
            == 128
        )
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] >= 16
        assert (
            connection.execute("SELECT COUNT(*) FROM reasoning_blocks").fetchone()[0]
            > 0
        )
        reasoning_carriers = {
            carrier
            for row in connection.execute(
                "SELECT DISTINCT carrier_type FROM reasoning_blocks"
            )
            for carrier in row[0].split("+")
        }
        assert {
            "reasoning_content",
            "reasoning",
            "redacted_thinking",
        }.issubset(reasoning_carriers)
        checkpoint_providers = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT json_extract(metadata_json, '$.provider_id')
                FROM checkpoints
                WHERE json_extract(metadata_json, '$.provider_id') IS NOT NULL
                """
            )
        }
        assert len(checkpoint_providers) >= 2
        provider_sequence = [
            row[0]
            for row in connection.execute(
                """
                SELECT json_extract(c.metadata_json, '$.provider_id')
                FROM turns AS t
                JOIN checkpoints AS c
                  ON c.checkpoint_id = printf('real-checkpoint-%04d', t.turn_ordinal)
                ORDER BY t.turn_ordinal
                """
            )
        ]
        assert len(provider_sequence) == 128
        assert (
            sum(
                previous != current for previous, current in pairwise(provider_sequence)
            )
            >= 16
        )

    storage = RolloutContextStore(
        sessions_dir,
        message_codec=LangChainMessageCodec(),
    )
    items = storage.storage.read_items(REAL_SESSION_ID, resolver.main_thread_id(REAL_SESSION_ID))
    assert len(items) >= 288
    assert all(item.format_version == 3 for item in items)
    assert {item.semantic_kind for item in items} >= {
        "user_input",
        "assistant_output",
        "reasoning",
        "tool_call",
        "tool_result",
    }
    canonical_reasoning_carriers = {
        carrier["type"]
        for item in items
        if item.semantic_kind == "reasoning"
        and (carrier := project_reasoning_carrier(item)) is not None
    }
    assert {"reasoning", "reasoning_content", "redacted_thinking"}.issubset(
        canonical_reasoning_carriers
    )
    assert sum(item.semantic_kind == "user_input" for item in items) == 128
    tool_calls = [item for item in items if item.semantic_kind == "tool_call"]
    tool_results = [item for item in items if item.semantic_kind == "tool_result"]
    assert len(tool_calls) >= 16
    assert len(tool_results) >= 16
    assert any(
        isinstance(item.payload, dict)
        and "LARGE_CALL" in (serialized := json.dumps(item.payload, ensure_ascii=False))
        and len(serialized) >= 64 * 1024
        for item in tool_calls
    )
    assert any(
        isinstance(item.payload, dict)
        and isinstance(item.payload.get("content"), str)
        and len(item.payload["content"]) >= 64 * 1024
        for item in tool_results
    )
