from __future__ import annotations

import json
import sqlite3
from itertools import pairwise
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest

from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.rollout_context_reader import RolloutContextReader
from app.core.rollout_storage import RolloutStorage
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.support.paths import output_root_for_test
from tests.support.workspaces import prepare_default_test_workspace

REAL_SESSION_ID = "ses_8128d7f0a4b64aa0b3f1c9e7d2a65018"
STATIC_MOCK_SESSION_ID = "ses_a1b2c3d4e5f6478899aabbccddeeff00"


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    """用 custom_tool_test_workspace 的完整副本作为本文件的集成测试工作区。"""

    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace",
        shared_skill_root=project_root / "resources" / "skills",
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
    for item in sessions:
        session_id = item["session_id"]
        rollout_root = sessions_root / session_id / "rollout"
        assert {
            child.name for child in rollout_root.iterdir()
        } == {"index.sqlite", "rollout.jsonl"}
        assert not list(rollout_root.glob("segment-*.jsonl"))
        assert not (sessions_root / session_id / "payloads").exists()

    compact_index = sessions_root / "ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345" / "rollout" / "index.sqlite"
    with sqlite3.connect(compact_index) as connection:
        checkpoint_id = connection.execute(
            "SELECT checkpoint_id FROM checkpoints ORDER BY commit_id DESC LIMIT 1"
        ).fetchone()[0]
        event_version = connection.execute(
            "SELECT channel_version FROM checkpoint_channels "
            "WHERE checkpoint_id = ? AND channel_name = '_summarization_event'",
            (checkpoint_id,),
        ).fetchone()[0]
        messages_version = connection.execute(
            "SELECT channel_version FROM checkpoint_channels "
            "WHERE checkpoint_id = ? AND channel_name = 'messages'",
            (checkpoint_id,),
        ).fetchone()[0]
        assert str(event_version).split(".", 1)[0].isdigit()
        assert str(messages_version).split(".", 1)[0].isdigit()

    compact_checkpoint = RolloutCheckpointSaver(sessions_root).get_tuple(
        build_checkpoint_config(
            "ses_4c0a1d6e7f8b49a2b5c6d7e8f9012345",
            checkpoint_id="checkpoint-0024",
        )
    )
    assert compact_checkpoint is not None
    compact_event = compact_checkpoint.checkpoint["channel_values"][
        "_summarization_event"
    ]
    assert compact_event["strategy"] == "cache_preserving"
    assert compact_event["cutoff_index"] == 64
    assert compact_event["cache_prefix_messages"] == []
    assert compact_event["summary_message"].additional_kwargs["lc_source"] == (
        "summarization"
    )


def _reader(workspace_root: Path) -> RolloutHistoryReader:
    return RolloutHistoryReader(
        RolloutContextReader(
            RolloutStorage(workspace_root / ".boxteam" / "sessions")
        )
    )


def _advance_to_64_cursor(
    reader: RolloutHistoryReader,
) -> str:
    _, cursor, _ = reader.bootstrap(REAL_SESSION_ID)
    assert cursor is not None
    first = reader.load(
        REAL_SESSION_ID,
        TurnHistoryLoadRequest(direction="before", cursor=cursor),
    )
    assert [item.ordinal for item in first.items] == [125, 126, 127]
    second = reader.load(
        REAL_SESSION_ID,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=first.next_cursor,
            turns=16,
        ),
    )
    assert [item.ordinal for item in second.items] == list(range(109, 125))
    assert second.next_cursor is not None
    return second.next_cursor


def test_real_128_turn_rollout_has_complete_indexed_projection_and_latency(
    real_rollout_workspace: Path,
) -> None:
    sessions_dir = real_rollout_workspace / ".boxteam" / "sessions"
    rollout_root = sessions_dir / REAL_SESSION_ID / "rollout"
    jsonl_path = rollout_root / "rollout.jsonl"
    sqlite_path = rollout_root / "index.sqlite"
    assert jsonl_path.is_file()
    assert sqlite_path.is_file()
    assert not list(rollout_root.glob("segment-*.jsonl"))

    with sqlite3.connect(sqlite_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 128
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            >= 128
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
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT carrier_type FROM reasoning_blocks"
            )
        }
        assert {
            "reasoning_content",
            "reasoning_items",
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
        assert sum(
            previous != current
            for previous, current in pairwise(provider_sequence)
        ) >= 16

    records = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(records) >= 288
    assert {record["role"] for record in records} == {"user", "assistant", "tool"}
    assert all("payload_ref" not in record for record in records)
    message_providers = {
        record["message"]["data"]["response_metadata"]["provider_id"]
        for record in records
        if isinstance(record.get("message"), dict)
        and isinstance(record["message"].get("data"), dict)
        and isinstance(record["message"]["data"].get("response_metadata"), dict)
        and isinstance(
            record["message"]["data"]["response_metadata"].get("provider_id"),
            str,
        )
    }
    assert len(message_providers) >= 2
    assistant_records = [
        record["message"]["data"]
        for record in records
        if record.get("role") == "assistant"
    ]
    assert assistant_records
    assert all(
        "invalid_tool_calls" in message
        and isinstance(message["invalid_tool_calls"], list)
        for message in assistant_records
    )
    assert all(
        not {
            "reasoning_content",
            "thinking_blocks",
            "reasoning_items",
        }.intersection(message.get("additional_kwargs", {}))
        for message in assistant_records
    )
    reasoning_records = [
        message
        for message in assistant_records
        if any(
            isinstance(block, dict)
            and block.get("type") in {"reasoning", "thinking", "redacted_thinking"}
            for block in message.get("content", [])
        )
    ]
    assert len(reasoning_records) >= 64
    assert all(
        all(
            not (isinstance(block, dict) and block.get("type") == "litellm_payload")
            for block in message["content"]
        )
        for message in assistant_records
    )

    reader = _reader(real_rollout_workspace)
    tail = reader.load(
        REAL_SESSION_ID,
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert [item.ordinal for item in tail.items] == [128]
    assert tail.items[0].final_response
    assert tail.items[0].tool_summary
    assert tail.items[0].thinking_blocks

    detailed = reader.load(
        REAL_SESSION_ID,
        TurnHistoryLoadRequest(
            turn_ids=["real-turn-0128"],
            include=["user", "tool_call", "tool_result", "final_response"],
        ),
    )
    assert detailed.items[0].user_messages
    assert detailed.items[0].items
    assert detailed.items[0].final_response

    cursor = _advance_to_64_cursor(reader)
    samples: list[float] = []
    for _ in range(8):
        started = perf_counter()
        page = reader.load(
            REAL_SESSION_ID,
            TurnHistoryLoadRequest(
                direction="before",
                cursor=cursor,
                turns=64,
            ),
        )
        samples.append((perf_counter() - started) * 1000)
    assert len(page.items) == 64
    assert median(samples) < 200
