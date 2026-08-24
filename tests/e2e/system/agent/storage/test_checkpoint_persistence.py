"""Rollout checkpoint 保存与读取端到端测试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes import (
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.text import normalize_text


async def _send_simple_message(
    client: httpx.AsyncClient, session_id: str, content: str
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["job_id"]


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


def _read_rollout_messages(rollout_jsonl: Path) -> list[dict]:
    records: list[dict] = []
    with rollout_jsonl.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


@pytest.mark.asyncio
async def test_checkpoint_save_reload_and_survive_restart(
    client: httpx.AsyncClient,
    e2e_backend_process: subprocess.Popen[str],
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
):
    """checkpoint 保存到磁盘、多次运行读取上下文、后端重启后仍能恢复。"""
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Checkpoint Persistence Full Flow Test"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_simple_message(
        client,
        session_id,
        "请记住这个数字：42。只回复'已记住'。",
    )
    first_job_data = await wait_for_job_done(client, first_job_id)
    assert first_job_data["status"] in {"completed", "succeeded"}

    session_dir = get_session_path_resolver(
        Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    rollout_root = session_dir / "rollout"
    rollout_jsonl = rollout_root / "rollout.jsonl"
    index_sqlite = rollout_root / "index.sqlite"
    assert rollout_jsonl.exists(), "第一次运行后 rollout JSONL 文件应已创建"
    assert index_sqlite.exists(), "第一次运行后 rollout SQLite 索引应已创建"

    first_records = _read_rollout_messages(rollout_jsonl)
    assert first_records, "第一次运行后 rollout JSONL 文件为空"
    assert {record["role"] for record in first_records} <= {
        "user",
        "assistant",
        "tool",
    }
    with sqlite3.connect(index_sqlite) as connection:
        first_checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints"
        ).fetchone()[0]
        assert first_checkpoint_count >= 1
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoint_channels").fetchone()[0]
            >= 1
        )

    first_history = await _load_history(
        client,
        session_id,
        {"direction": "tail"},
    )
    first_items = first_history["items"]
    assert len(first_items) == 1
    assert first_items[0]["user_messages"][0]["content"] == (
        "请记住这个数字：42。只回复'已记住'。"
    )
    assert first_items[0]["final_response"]

    second_job_id = await _send_simple_message(
        client,
        session_id,
        "我刚才让你记住的数字是多少？只回复数字。",
    )
    second_job_data = await wait_for_job_done(client, second_job_id)
    assert second_job_data["status"] in {"completed", "succeeded"}

    second_records = _read_rollout_messages(rollout_jsonl)
    assert len(second_records) > len(first_records), "第二次运行后 rollout 消息应增加"
    with sqlite3.connect(index_sqlite) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            > first_checkpoint_count
        )

    second_history = await _load_history(
        client,
        session_id,
        {"direction": "head", "turns": 2},
    )
    second_items = second_history["items"]
    assert len(second_items) == 2
    assert [item["user_messages"][0]["content"] for item in second_items] == [
        "请记住这个数字：42。只回复'已记住'。",
        "我刚才让你记住的数字是多少？只回复数字。",
    ]

    second_reply = normalize_text(second_items[-1]["final_response"])
    assert "42" in second_reply, f"助手未从 checkpoint 恢复上下文，回复: {second_reply}"

    terminate_process(e2e_backend_process)

    restarted_backend = start_backend_process(
        workspace_root=e2e_workspace_root_path,
        port=e2e_backend_port,
        log_name="e2e-backend-restart",
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{e2e_backend_port}",
            timeout=30,
            headers={"X-Local-Token": "local-dev-token"},
        ) as restarted_client:
            restarted_history = await _load_history(
                restarted_client,
                session_id,
                {"direction": "head", "turns": 2},
            )
            restarted_items = restarted_history["items"]
            assert len(restarted_items) == 2, (
                f"重启后应能读取历史 Turn: {len(restarted_items)}"
            )

            third_job_id = await _send_simple_message(
                restarted_client,
                session_id,
                "把之前记住的数字加 1 等于多少？只回复数字。",
            )
            third_job_data = await wait_for_job_done(restarted_client, third_job_id)
            assert third_job_data["status"] in {"completed", "succeeded"}

            final_history = await _load_history(
                restarted_client,
                session_id,
                {"direction": "head", "turns": 3},
            )
            final_items = final_history["items"]
            assert len(final_items) == 3

            third_reply = normalize_text(final_items[-1]["final_response"])
            assert "43" in third_reply, f"重启后续对话未恢复上下文，回复: {third_reply}"
    finally:
        close_backend_process(restarted_backend)
