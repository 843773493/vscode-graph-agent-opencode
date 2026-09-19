from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage, message_to_dict
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"


def _checkpoint(checkpoint_id: str, messages: list[HumanMessage]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages, "counter": len(messages)}
    checkpoint["channel_versions"] = {
        "messages": str(len(messages)),
        "counter": str(len(messages)),
    }
    checkpoint["updated_channels"] = ["messages", "counter"]
    return checkpoint


def test_long_session_persists_message_deltas_without_snapshot_growth(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    messages: list[HumanMessage] = []
    naive_snapshot_bytes = 0
    config = build_checkpoint_config(SESSION_ID)

    for index in range(128):
        message = HumanMessage(
            content=f"历史消息 {index:03d}: " + ("x" * 256),
            id=f"message-{index:03d}",
        )
        messages = [*messages, message]
        naive_snapshot_bytes += len(
            json.dumps(
                [message_to_dict(item) for item in messages],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        config = saver.put(
            config,
            _checkpoint(f"checkpoint-{index:03d}", messages),
            {"source": "benchmark", "step": index},
            {"messages": str(index + 1), "counter": str(index + 1)},
        )

    root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
    )
    rollout_path = root / "rollout.jsonl"
    records = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
    ]
    message_records = [
        record for record in records if record["semantic_kind"] == "user_input"
    ]
    rollout_bytes = rollout_path.stat().st_size

    assert all(record["format_version"] == 2 for record in records)
    assert all(record["record_type"] == "item" for record in records)
    assert all(
        "role" not in record and "message" not in record and "sequence" not in record
        for record in records
    )
    assert len(message_records) == 128
    assert len({record["item_id"] for record in message_records}) == 128
    assert all(record["wire_role"] == "user" for record in message_records)
    assert rollout_bytes * 8 < naive_snapshot_bytes
    assert not list(root.glob("segment-*.jsonl"))
