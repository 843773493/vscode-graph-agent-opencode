from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.rollout_context_reader import RolloutContextReader
from app.core.rollout_storage import RolloutStorage
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
    *,
    channel_version: int,
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        "messages": f"{channel_version:032d}.fixture"
    }
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


def _turn_messages(
    turn_index: int,
    *,
    tool_result_size: int = 0,
    tool_count: int = 1,
) -> list[object]:
    turn_id = f"job-{turn_index:04d}"
    created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=turn_index)
    stamp = created_at.isoformat()
    user = HumanMessage(
        id=f"user-{turn_index:04d}",
        content=f"用户问题 {turn_index}",
        response_metadata={
            "message_id": f"user-{turn_index:04d}",
            "created_at": stamp,
            "updated_at": stamp,
            "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
        },
    )
    call = AIMessage(
        id=f"assistant-tool-{turn_index:04d}",
        content=[
            {
                "type": "text",
                "text": f"我先检查第 {turn_index} 轮 fixture",
            }
        ],
        tool_calls=[
            {
                "name": "read_fixture",
                "args": {
                    "path": f"fixture/{turn_index:04d}"
                    f"{'-' + str(index) if tool_count > 1 else ''}.json"
                },
                "id": f"call-{turn_index:04d}"
                f"{'-' + str(index) if tool_count > 1 else ''}",
            }
            for index in range(tool_count)
        ],
    )
    results = []
    for index in range(tool_count):
        result_content = json.dumps(
            {"turn": turn_index, "tool": index, "result": "fixture result"},
            ensure_ascii=False,
        )
        if tool_result_size:
            result_content = "x" * tool_result_size
        results.append(
            ToolMessage(
                id=f"tool-result-{turn_index:04d}"
                f"{'-' + str(index) if tool_count > 1 else ''}",
                content=result_content,
                name="read_fixture",
                tool_call_id=f"call-{turn_index:04d}"
                f"{'-' + str(index) if tool_count > 1 else ''}",
            )
        )
    final = AIMessage(
        id=f"assistant-final-{turn_index:04d}",
        content=[
            {
                "type": "reasoning",
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": f"检查问题 {turn_index}",
                    }
                ],
                **(
                    {
                        "encrypted_content": f"encrypted-{turn_index:04d}"
                    }
                    if turn_index % 5 == 0
                    else {}
                ),
            },
            {
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": f"Provider 摘要 {turn_index}",
                    }
                ],
            },
            {
                "type": "text",
                "text": f"模型最终响应 {turn_index}",
            },
        ],
        response_metadata={
            "created_at": stamp,
            "updated_at": stamp,
            "phase": "final_answer",
        },
    )
    return [user, call, *results, final]


def _seed_rollout(
    workspace_root: Path,
    session_id: str,
    *,
    count: int,
    tool_result_size: int = 0,
    tool_count: int = 1,
) -> Path:
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(session_id)
    all_messages: list[object] = []
    for turn_index in range(1, count + 1):
        all_messages.extend(
            _turn_messages(
                turn_index,
                tool_result_size=tool_result_size,
                tool_count=tool_count,
            )
        )
        config = saver.put(
            config,
            _checkpoint(
                f"checkpoint-{turn_index:04d}",
                all_messages,
                channel_version=turn_index,
            ),
            {"source": "deterministic-rollout-stub", "turn": turn_index},
            {"messages": str(turn_index)},
        )
        saver.finalize_turn(
            session_id=session_id,
            turn_id=f"job-{turn_index:04d}",
            final_message_id=f"assistant-final-{turn_index:04d}",
        )
    session_dir = get_session_path_resolver(sessions_dir).resolve_session_node(
        session_id
    )
    return session_dir / "rollout"


async def _load_history(
    client: httpx.AsyncClient,
    session_id: str,
    payload: dict[str, object],
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/history",
        json=payload,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


@pytest.mark.asyncio
async def test_history_loads_rollout_summary_and_tool_details(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "rollout 历史投影")
    rollout_root = _seed_rollout(
        Path(integration_workspace_root_path),
        session_id,
        count=24,
    )
    assert (rollout_root / "index.sqlite").is_file()
    assert (rollout_root / "rollout.jsonl").is_file()
    assert not list(rollout_root.glob("segment-*.jsonl"))

    bootstrap_response = await integration_client.get(
        f"/api/v1/sessions/{session_id}/bootstrap"
    )
    assert bootstrap_response.status_code == 200, bootstrap_response.text
    bootstrap = bootstrap_response.json()["data"]
    assert bootstrap["latest_turn"]["ordinal"] == 24
    assert bootstrap["older_cursor"]

    summary = await _load_history(
        integration_client,
        session_id,
        {"direction": "tail", "turns": 1},
    )
    assert [item["ordinal"] for item in summary["items"]] == [24]
    latest = summary["items"][0]
    assert latest["user_messages"][0]["content"] == "用户问题 24"
    assert latest["user_messages"][0]["metadata"] == {}
    assert latest["final_response"] == "模型最终响应 24"
    assert isinstance(latest["thinking_blocks"], list)
    assert latest["tool_summary"]
    assert [part["kind"] for part in latest["response_parts"]] == [
        "tool_call",
        "final_text",
    ]
    assert latest["response_parts"][0]["source"]["call_index"] == 0
    assert len(latest["items"]) == 2
    assert all(item["raw"] == {} for item in latest["items"])
    assert "encrypted-0024" not in json.dumps(latest, ensure_ascii=False)

    storage = RolloutStorage(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    )
    checkpoint = storage.latest_checkpoint(session_id, "", None)
    assert checkpoint is not None
    restored_messages = storage.materialize_messages(
        session_id,
        "",
        checkpoint.message_sequence,
    )
    restored_tool_message = next(
        message
        for message in restored_messages
        if getattr(message, "id", None) == "assistant-tool-0024"
    )
    assert isinstance(restored_tool_message, AIMessage)
    assert restored_tool_message.tool_calls[0]["name"] == "read_fixture"
    assert restored_tool_message.content == [
        {
            "type": "text",
            "text": "我先检查第 24 轮 fixture",
        }
    ]

    details = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": [latest["turn_id"]],
            "include": ["user", "tool_call", "tool_result", "final_response"],
        },
    )
    detailed = details["items"][0]
    assert detailed["items"][0]["raw"]["payload"]["args"]["path"] == "fixture/0024.json"
    assert detailed["items"][1]["raw"]["payload"]["result"]
    assert detailed["final_response"] == "模型最终响应 24"
    assert [part["kind"] for part in detailed["response_parts"]] == [
        "tool_call",
        "tool_result",
        "final_text",
    ]

    reasoning = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": [latest["turn_id"]],
            "include": ["user", "thinking", "final_response"],
        },
    )
    assert reasoning["items"][0]["thinking_blocks"]
    assert reasoning["items"][0]["thinking_blocks"][0] == {
        "kind": "reasoning",
        "text": "检查问题 24",
    }
    assert "encrypted-0024" not in json.dumps(reasoning, ensure_ascii=False)

    metadata = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": [latest["turn_id"]],
            "include": ["user", "metadata"],
        },
    )
    assert (
        metadata["items"][0]["user_messages"][0]["metadata"]["message_id"]
        == "user-0024"
    )

    reloaded_details = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": [latest["turn_id"]],
            "include": ["user", "tool_call", "tool_result", "final_response"],
        },
    )
    assert reloaded_details == details


@pytest.mark.asyncio
async def test_history_tool_selector_only_materializes_requested_tool(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _create_session(integration_client, "定点工具详情")
    _seed_rollout(
        Path(integration_workspace_root_path),
        session_id,
        count=1,
        tool_count=2,
    )
    storage = RolloutStorage(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    )
    reader = RolloutHistoryReader(RolloutContextReader(storage))
    read_sequences: list[list[int]] = []
    original_read = storage._read_record_envelopes

    def capture_read(
        thread_id: str,
        checkpoint_ns: str,
        rows: Iterable[tuple[int, str, int, int]],
    ) -> list[dict[str, object]]:
        indexed_rows = list(rows)
        read_sequences.append([int(row[0]) for row in indexed_rows])
        return original_read(thread_id, checkpoint_ns, indexed_rows)

    monkeypatch.setattr(storage, "_read_record_envelopes", capture_read)
    reader.load(
        session_id,
        TurnHistoryLoadRequest(
            turn_ids=["job-0001"],
            tool_call_ids=["call-0001-1"],
            include=["tool_call", "tool_result"],
        ),
    )
    assert read_sequences == [[2, 4]]

    page = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": ["job-0001"],
            "tool_call_ids": ["call-0001-1"],
            "include": ["tool_call", "tool_result"],
        },
    )
    detail = page["items"][0]
    assert [part["kind"] for part in detail["response_parts"]] == [
        "tool_call",
        "tool_result",
    ]
    assert {
        part["tool_call_id"] for part in detail["response_parts"]
    } == {"call-0001-1"}
    assert len(detail["items"]) == 2
    assert all(
        item["part_id"] == "call-0001-1" for item in detail["items"]
    )
    assert detail["items"][0]["raw"]["payload"]["args"]["path"] == (
        "fixture/0001-1.json"
    )
    assert "fixture result" in detail["items"][1]["raw"]["payload"]["result"]
    assert "call-0001-0" not in json.dumps(detail, ensure_ascii=False)


@pytest.mark.asyncio
async def test_rollout_history_cursor_windows_are_complete_and_unique(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "rollout 游标")
    _seed_rollout(Path(integration_workspace_root_path), session_id, count=24)

    tail = await _load_history(
        integration_client,
        session_id,
        {"direction": "tail", "turns": 1},
    )
    tail_cursor = tail["next_cursor"]
    assert isinstance(tail_cursor, str)

    before_without_cursor = await _load_history(
        integration_client,
        session_id,
        {"direction": "before", "turns": 1},
    )
    assert [item["ordinal"] for item in before_without_cursor["items"]] == [24]

    before_one = await _load_history(
        integration_client,
        session_id,
        {"direction": "before", "cursor": tail_cursor, "turns": 1},
    )
    assert [item["ordinal"] for item in before_one["items"]] == [23]
    middle_cursor = before_one["next_cursor"]
    assert isinstance(middle_cursor, str)

    before_five = await _load_history(
        integration_client,
        session_id,
        {"direction": "before", "cursor": tail_cursor, "turns": 5},
    )
    assert [item["ordinal"] for item in before_five["items"]] == [19, 20, 21, 22, 23]
    assert all(item["items"] == [] for item in before_five["items"])

    around = await _load_history(
        integration_client,
        session_id,
        {
            "direction": "around",
            "cursor": middle_cursor,
            "before_turns": 1,
            "after_turns": 1,
        },
    )
    assert [item["ordinal"] for item in around["items"]] == [22, 23, 24]
    assert len({item["turn_id"] for item in around["items"]}) == 3

    head = await _load_history(
        integration_client,
        session_id,
        {"direction": "head", "turns": 4},
    )
    assert [item["ordinal"] for item in head["items"]] == [1, 2, 3, 4]
    after = await _load_history(
        integration_client,
        session_id,
        {"direction": "after", "cursor": head["next_cursor"], "turns": 4},
    )
    assert [item["ordinal"] for item in after["items"]] == [5, 6, 7, 8]


@pytest.mark.asyncio
async def test_rollout_history_around_anchor_returns_bidirectional_cursors(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "around 锚点双向窗口")
    _seed_rollout(Path(integration_workspace_root_path), session_id, count=32)

    around = await _load_history(
        integration_client,
        session_id,
        {
            "direction": "around",
            "anchor_turn_id": "job-0020",
        },
    )

    assert [item["ordinal"] for item in around["items"]] == list(range(17, 24))
    assert isinstance(around["before_cursor"], str)
    assert isinstance(around["after_cursor"], str)
    assert around["has_before"] is True
    assert around["has_after"] is True
    activity_stats = around["items"][3]["activity_stats"]
    assert isinstance(activity_stats, dict)
    assert isinstance(activity_stats["duration_ms"], int)
    assert activity_stats["duration_ms"] >= 0
    assert activity_stats["message_count"] == 4
    assert set(activity_stats) == {"duration_ms", "message_count"}
    assert all(item["items"] == [] for item in around["items"])

    before = await _load_history(
        integration_client,
        session_id,
        {"direction": "before", "cursor": around["before_cursor"]},
    )
    after = await _load_history(
        integration_client,
        session_id,
        {"direction": "after", "cursor": around["after_cursor"]},
    )
    assert [item["ordinal"] for item in before["items"]] == [14, 15, 16]
    assert [item["ordinal"] for item in after["items"]] == [24, 25, 26]

    anchor_only = await _load_history(
        integration_client,
        session_id,
        {
            "direction": "around",
            "anchor_turn_id": "job-0020",
            "before_turns": 0,
            "after_turns": 0,
        },
    )
    assert [item["ordinal"] for item in anchor_only["items"]] == [20]
    assert anchor_only["has_before"] is True
    assert anchor_only["has_after"] is True


@pytest.mark.asyncio
async def test_default_history_window_loads_five_turns_and_three_turn_anchor_sides(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "默认历史窗口")
    _seed_rollout(Path(integration_workspace_root_path), session_id, count=24)

    tail = await _load_history(
        integration_client,
        session_id,
        {"direction": "tail"},
    )
    assert [item["ordinal"] for item in tail["items"]] == [20, 21, 22, 23, 24]
    assert tail["has_before"] is True
    assert isinstance(tail["before_cursor"], str)

    around = await _load_history(
        integration_client,
        session_id,
        {
            "direction": "around",
            "anchor_turn_id": "job-0012",
        },
    )
    assert [item["ordinal"] for item in around["items"]] == [9, 10, 11, 12, 13, 14, 15]


@pytest.mark.asyncio
async def test_history_does_not_fallback_to_old_trace_projection(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "不回退旧投影")
    old_trace_root = (
        Path(integration_workspace_root_path)
        / ".boxteam"
        / "sessions"
        / session_id
        / "logs"
        / "traces"
    )
    old_trace_root.mkdir(parents=True, exist_ok=True)
    (old_trace_root / "events.jsonl").write_text(
        '{"type":"job_created","job_id":"old-job"}\n',
        encoding="utf-8",
    )

    page = await _load_history(
        integration_client,
        session_id,
        {"direction": "tail"},
    )
    assert page["items"] == []
    assert not (
        Path(integration_workspace_root_path)
        / ".boxteam"
        / "sessions"
        / session_id
        / "turn_history"
    ).exists()


@pytest.mark.asyncio
async def test_turn_finalize_pointer_wins_over_heuristic_final_response(
    integration_workspace_root_path: str,
    integration_client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(integration_client, "finalization 指针优先")
    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(session_id)
    user = HumanMessage(
        id="user-finalization",
        content="检查最终响应指针",
        response_metadata={
            "message_metadata": {
                "turn_id": "job-finalization",
                "job_id": "job-finalization",
            }
        },
    )
    marked_final = AIMessage(id="assistant-marked-final", content="标记的最终响应")
    later_unmarked = AIMessage(
        id="assistant-later-unmarked", content="后写的未标记消息"
    )
    saver.put(
        config,
        _checkpoint(
            "checkpoint-finalization",
            [user, marked_final, later_unmarked],
            channel_version=1,
        ),
        {"source": "deterministic-finalization-stub"},
        {"messages": "1"},
    )
    saver.finalize_turn(
        session_id=session_id,
        turn_id="job-finalization",
        final_message_id="assistant-marked-final",
    )

    reader = RolloutHistoryReader(
        RolloutContextReader(RolloutStorage(sessions_dir))
    )
    page = reader.load(session_id, TurnHistoryLoadRequest(direction="tail"))
    assert page.items[0].final_response == "标记的最终响应"


@pytest.mark.asyncio
async def test_finalization_window_keeps_unfinalized_history_readable(
    integration_workspace_root_path: str,
    integration_client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(integration_client, "finalization 崩溃窗口")
    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(session_id)
    turn_id = "job-finalization-window"
    user = HumanMessage(
        id="user-finalization-window",
        content="模拟 finalization 提交前崩溃",
        response_metadata={"message_metadata": {"turn_id": turn_id, "job_id": turn_id}},
    )
    final = AIMessage(id="assistant-finalization-window", content="仍可恢复的最终响应")
    saver.put(
        config,
        _checkpoint(
            "checkpoint-finalization-window",
            [user, final],
            channel_version=1,
        ),
        {"source": "deterministic-finalization-window-stub"},
        {"messages": "1"},
    )
    # 故意不调用 finalize_turn，模拟最终消息 JSONL 已提交但控制记录尚未提交。
    page = RolloutHistoryReader(
        RolloutContextReader(RolloutStorage(sessions_dir))
    ).load(
        session_id,
        TurnHistoryLoadRequest(direction="tail"),
    )
    assert page.items[0].final_response == "仍可恢复的最终响应"


@pytest.mark.asyncio
async def test_bounded_history_uses_sqlite_turn_spans_without_materializing_all_messages(
    integration_workspace_root_path: str,
    integration_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通 append-only rollout 的有界加载不能重放完整消息列表。"""
    session_id = await _create_session(
        integration_client,
        "索引历史性能回归",
    )
    _seed_rollout(Path(integration_workspace_root_path), session_id, count=128)

    storage = RolloutStorage(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    )
    reader = RolloutHistoryReader(RolloutContextReader(storage))

    def fail_if_materialized(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("有 SQLite Turn 索引时不应 materialize 全量消息")

    monkeypatch.setattr(storage, "materialize_messages", fail_if_materialized)
    decoded_types: list[str] = []
    decode_indexed_message = storage.decode_indexed_message

    def record_decoded_message(*args: object, **kwargs: object) -> object:
        value = args[2] if len(args) > 2 else kwargs.get("value")
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            decoded_types.append(value["type"])
        return decode_indexed_message(*args, **kwargs)

    monkeypatch.setattr(storage, "decode_indexed_message", record_decoded_message)

    latest, older_cursor, _ = reader.bootstrap(session_id)
    assert latest is not None
    assert latest.ordinal == 128
    assert older_cursor is not None

    tail = reader.load(
        session_id,
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert [item.ordinal for item in tail.items] == [128]
    assert tail.items[0].final_response == "模型最终响应 128"

    older = reader.load(
        session_id,
        TurnHistoryLoadRequest(
            direction="before",
            cursor=tail.next_cursor,
            turns=4,
        ),
    )
    assert [item.ordinal for item in older.items] == [124, 125, 126, 127]
    assert "tool" not in decoded_types


@pytest.mark.asyncio
async def test_internal_leading_messages_do_not_create_empty_indexed_turn(
    integration_workspace_root_path: str,
    integration_client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(
        integration_client,
        "内部消息不创建空 Turn",
    )
    saver = RolloutCheckpointSaver(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    )
    config = build_checkpoint_config(session_id)
    internal = AIMessage(
        content="内部委派已开始",
        id="internal-start",
        response_metadata={"message_metadata": {"internal": True}},
    )
    messages = [internal, *_turn_messages(1)]
    saver.put(
        config,
        _checkpoint(
            "internal-leading-checkpoint",
            messages,
            channel_version=1,
        ),
        {"source": "deterministic-rollout-stub"},
        {"messages": "1"},
    )

    reader = RolloutHistoryReader(
        RolloutContextReader(
            RolloutStorage(
                Path(integration_workspace_root_path) / ".boxteam" / "sessions"
            )
        )
    )
    page = reader.load(session_id, TurnHistoryLoadRequest(direction="tail"))
    assert [item.ordinal for item in page.items] == [1]
    assert page.items[0].user_messages[0].content == "用户问题 1"


@pytest.mark.asyncio
async def test_history_detail_enforces_per_item_budget(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "详情预算")
    _seed_rollout(
        Path(integration_workspace_root_path),
        session_id,
        count=1,
        tool_result_size=100_000,
    )

    storage = RolloutStorage(
        Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    )
    reader = RolloutHistoryReader(RolloutContextReader(storage))

    summary = reader.load(
        session_id,
        TurnHistoryLoadRequest(direction="tail"),
    )
    assert summary.items[0].items[1].raw == {}

    page = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": ["job-0001"],
            "include": ["user", "tool_call", "tool_result", "final_response"],
        },
    )
    item = page["items"][0]
    assert item["detail_truncated"] is True
    assert len(item["items"]) == 1


@pytest.mark.asyncio
async def test_history_tool_summary_truncates_long_turn_without_response_400(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    session_id = await _create_session(integration_client, "长工具摘要")
    _seed_rollout(
        Path(integration_workspace_root_path),
        session_id,
        count=1,
        tool_count=33,
    )

    summary = await _load_history(
        integration_client,
        session_id,
        {"direction": "tail", "turns": 1},
    )
    summary_item = summary["items"][0]
    assert len(summary_item["tool_summary"]) == 64
    assert summary_item["tool_summary_truncated"] is True

    detail = await _load_history(
        integration_client,
        session_id,
        {
            "turn_ids": ["job-0001"],
            "include": ["user", "tool_summary", "tool_call", "tool_result", "final_response"],
        },
    )
    detail_item = detail["items"][0]
    assert len(detail_item["tool_summary"]) == 64
    assert detail_item["tool_summary_truncated"] is True
    assert detail_item["detail_truncated"] is True
    assert len(detail_item["items"]) == 66
