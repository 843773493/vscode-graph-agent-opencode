from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver


def seed_compactable_checkpoint(
    *,
    workspace_root: Path,
    session_id: str,
    pair_count: int = 32,
) -> FileSystemCheckpointSaver:
    """写入明显超过普通测试长度、可被压缩的模型上下文。"""

    saver = FileSystemCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    messages = []
    for index in range(pair_count):
        ordinal = index + 1
        created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
            seconds=ordinal * 10
        )
        messages.extend(
            (
                HumanMessage(
                    id=f"msg_compact_user_{index:04d}",
                    content=(
                        f"压缩夹具用户消息 {index:04d}。"
                        + "需要保留的长上下文资料。" * 160
                    ),
                    response_metadata={
                        "message_id": f"msg_turn_e2e_{ordinal:04d}",
                        "created_at": created_at.isoformat(),
                        "updated_at": created_at.isoformat(),
                        "message_metadata": {
                            "job_id": f"job_turn_e2e_{ordinal:04d}"
                        },
                    },
                ),
                AIMessage(
                    id=f"msg_compact_ai_{index:04d}",
                    content=(
                        f"压缩夹具助手消息 {index:04d}。"
                        + "这是长上下文的处理结果。" * 160
                    ),
                    response_metadata={
                        "message_id": f"msg_compact_ai_{index:04d}",
                        "created_at": (created_at + timedelta(seconds=1)).isoformat(),
                        "updated_at": (created_at + timedelta(seconds=1)).isoformat(),
                        "message_metadata": {},
                    },
                ),
            )
        )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = str(uuid4())
    version = saver.get_next_version(None, None)
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version}
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        metadata={"source": "turn_history_e2e", "step": -1, "writes": {}},
        new_versions={"messages": version},
    )
    return saver


def replace_with_compacted_checkpoint(
    *,
    saver: FileSystemCheckpointSaver,
    session_id: str,
) -> str:
    """模拟压缩完成后的 checkpoint 变更，并返回新的 checkpoint ID。"""

    checkpoint_tuple = saver.get_tuple(build_checkpoint_config(session_id))
    if checkpoint_tuple is None:
        raise RuntimeError(f"压缩夹具 checkpoint 不存在: session_id={session_id}")
    checkpoint = checkpoint_tuple.checkpoint.copy()
    checkpoint_id = str(uuid4())
    checkpoint["id"] = checkpoint_id
    channel_values = dict(checkpoint.get("channel_values", {}))
    original_messages = list(channel_values.get("messages", []))
    channel_values["messages"] = [
        original_messages[0],
        SystemMessage(
            id="msg_compaction_summary",
            content="压缩摘要：保留首轮稳定前缀，并汇总中间长上下文。",
            additional_kwargs={"lc_source": "summarization"},
        ),
        original_messages[-1],
    ]
    channel_values["_summarization_event"] = {
        "strategy": "cache_preserving",
        "cutoff_index": max(1, len(original_messages) - 1),
    }
    checkpoint["channel_values"] = channel_values
    versions = dict(checkpoint.get("channel_versions", {}))
    version = saver.get_next_version(versions.get("messages"), None)
    versions["messages"] = version
    checkpoint["channel_versions"] = versions
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        checkpoint_tuple.config,
        checkpoint,
        metadata={"source": "turn_history_e2e_compacted", "step": -1, "writes": {}},
        new_versions={"messages": version},
    )
    return checkpoint_id
