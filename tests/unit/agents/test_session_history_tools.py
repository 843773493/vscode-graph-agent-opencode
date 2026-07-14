from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.tools.session_history import (
    create_grep_session_context_jsonl_tool,
    create_read_session_context_jsonl_tool,
    create_read_session_recent_text_messages_tool,
)


class _FakeSessionService:
    async def get(self, session_id: str) -> object:
        return {"session_id": session_id}


class _FakeMessageService:
    def __init__(self) -> None:
        self.checkpoint_id = "ckpt-1"
        self.records = [
            {"role": "user", "type": "human", "content": "ALPHA 用户问题"},
            {"role": "assistant", "type": "ai", "content": "ALPHA 模型回答"},
        ]

    async def get_agent_context_state(self, session_id: str) -> dict[str, object]:
        return {
            "records": list(self.records),
            "checkpoint_id": self.checkpoint_id,
            "raw_message_count": len(self.records),
            "compacted": False,
            "compaction_cutoff": None,
            "history_file_path": None,
        }


def _context(message_service: _FakeMessageService) -> SimpleNamespace:
    return SimpleNamespace(
        message_service=message_service,
        session_service=_FakeSessionService(),
    )


@pytest.mark.asyncio
async def test_recent_grep_and_read_share_snapshot_metadata():
    message_service = _FakeMessageService()
    context = _context(message_service)
    recent_tool = create_read_session_recent_text_messages_tool(context)
    grep_tool = create_grep_session_context_jsonl_tool(context)
    read_tool = create_read_session_context_jsonl_tool(context)

    recent = json.loads(
        await recent_tool.ainvoke({"session_id": "ses_target", "rounds": 1})
    )
    snapshot_id = recent["context_snapshot"]["snapshot_id"]
    assert snapshot_id == "ckpt-1"
    assert recent["context_snapshot"]["line_count"] == 2
    assert recent["context_snapshot"]["consistency"] == "not_checked"

    grep_result = json.loads(
        await grep_tool.ainvoke(
            {
                "session_id": "ses_target",
                "pattern": "ALPHA",
                "expected_snapshot_id": snapshot_id,
            }
        )
    )
    assert grep_result["context_snapshot"]["consistency"] == "matched"
    assert [match["line_number"] for match in grep_result["matches"]] == [1, 2]

    read_result = json.loads(
        await read_tool.ainvoke(
            {
                "session_id": "ses_target",
                "line_start": 2,
                "line_count": 1,
                "expected_snapshot_id": snapshot_id,
            }
        )
    )
    assert read_result["context_snapshot"]["consistency"] == "matched"
    assert read_result["lines"][0]["line_number"] == 2
    assert "ALPHA 模型回答" in read_result["lines"][0]["text"]


@pytest.mark.asyncio
async def test_read_warns_when_context_changed_after_grep():
    message_service = _FakeMessageService()
    context = _context(message_service)
    grep_tool = create_grep_session_context_jsonl_tool(context)
    read_tool = create_read_session_context_jsonl_tool(context)

    grep_result = json.loads(
        await grep_tool.ainvoke(
            {"session_id": "ses_target", "pattern": "ALPHA"}
        )
    )
    old_snapshot_id = grep_result["context_snapshot"]["snapshot_id"]

    message_service.checkpoint_id = "ckpt-2"
    message_service.records.append(
        {"role": "user", "type": "human", "content": "BETA 新消息"}
    )
    read_result = json.loads(
        await read_tool.ainvoke(
            {
                "session_id": "ses_target",
                "expected_snapshot_id": old_snapshot_id,
            }
        )
    )

    metadata = read_result["context_snapshot"]
    assert metadata["snapshot_id"] == "ckpt-2"
    assert metadata["consistency"] == "changed"
    assert "不要与旧 grep/read 结果" in metadata["warning"]


@pytest.mark.asyncio
async def test_read_clips_large_jsonl_lines_with_hash():
    message_service = _FakeMessageService()
    message_service.records = [
        {"role": "assistant", "type": "ai", "content": "x" * 5000}
    ]
    read_tool = create_read_session_context_jsonl_tool(_context(message_service))

    result = json.loads(
        await read_tool.ainvoke(
            {
                "session_id": "ses_target",
                "max_chars_per_line": 200,
            }
        )
    )

    line = result["lines"][0]
    assert line["truncated"] is True
    assert len(line["text"]) == 200
    assert len(line["line_sha256"]) == 64
