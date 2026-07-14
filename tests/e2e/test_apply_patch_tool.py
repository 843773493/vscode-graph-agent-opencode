from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.utils import get_trace_payload, read_sse_events_until, wait_for_job_done


@dataclass(frozen=True, slots=True)
class ApplyPatchRun:
    session_id: str
    tool_names: list[str | None]


async def _run_model_apply_patch(
    client: httpx.AsyncClient,
    *,
    title: str,
    prompt: str,
    timeout: float,
) -> ApplyPatchRun:
    create_session_response = await client.post("/api/v1/sessions", json={"title": title})
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    async with client.stream(
        "GET",
        f"/api/v1/sessions/{session_id}/traces/stream",
        timeout=None,
    ) as stream_response:
        assert stream_response.status_code == 200
        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": prompt},
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        )
        assert message_response.status_code == 200
        job_id = message_response.json()["data"]["job_id"]
        events_task = asyncio.create_task(
            read_sse_events_until(
                stream_response,
                lambda event: event.get("type") == "agent_end",
                timeout_seconds=timeout,
            )
        )
        try:
            job_data = await wait_for_job_done(client, job_id, max_attempts=120)
            assert job_data["status"] in {"completed", "succeeded"}
            events = await asyncio.wait_for(events_task, timeout=5.0)
        except BaseException:
            events_task.cancel()
            await asyncio.gather(events_task, return_exceptions=True)
            raise
    tool_names = [
        get_trace_payload(event).get("tool_name")
        for event in events
        if event.get("type") == "tool_call_start"
    ]
    assert "apply_patch" in tool_names, f"模型未调用 apply_patch，实际工具调用: {tool_names}"
    assert "write_file" not in tool_names
    assert "edit_file" not in tool_names
    return ApplyPatchRun(session_id=session_id, tool_names=tool_names)


@pytest.mark.asyncio
async def test_model_uses_apply_patch_update_hunk(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    is_debug: bool,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    target_file = workspace_root / "apply_patch_model_target.txt"
    target_file.write_text("alpha\nbeta\n", encoding="utf-8")
    timeout = 100000 if is_debug else 120.0

    run = await _run_model_apply_patch(
        client,
        title="apply-patch-update-hunk",
        timeout=timeout,
        prompt=(
            "必须真实调用 apply_patch 工具修改工作区相对路径 apply_patch_model_target.txt。\n"
            "文件当前内容精确为：alpha\\nbeta\\n。\n"
            "用一个 Update File hunk 把 beta 改成 gamma，并在后面新增一行 delta。\n"
            "不要调用 write_file、edit_file、python_exec、persistent_terminal 或 execute。\n"
            "工具调用成功后最终只回复：apply_patch_done"
        ),
    )

    assert target_file.read_text(encoding="utf-8") == "alpha\ngamma\ndelta\n"
    changeset_response = await client.get(
        f"/api/v1/sessions/{run.session_id}/changesets/all"
    )
    assert changeset_response.status_code == 200
    changeset = changeset_response.json()["data"]
    assert changeset["summary"]["files"] == 1
    assert changeset["files"][0]["file_path"] == "/apply_patch_model_target.txt"
    assert changeset["files"][0]["tool_call_ids"]
    assert "+gamma" in changeset["files"][0]["diff_text"]
    assert "+delta" in changeset["files"][0]["diff_text"]


@pytest.mark.asyncio
async def test_model_uses_one_apply_patch_for_add_and_delete(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    is_debug: bool,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    deleted_file = workspace_root / "apply_patch_delete_me.txt"
    created_file = workspace_root / "apply_patch_created.txt"
    deleted_file.write_text("obsolete\n", encoding="utf-8")
    timeout = 100000 if is_debug else 120.0

    run = await _run_model_apply_patch(
        client,
        title="apply-patch-add-delete",
        timeout=timeout,
        prompt=(
            "只能调用一次 apply_patch，在同一个 V4A patch 中完成两个操作：\n"
            "1. 删除工作区相对路径 apply_patch_delete_me.txt。\n"
            "2. 新增工作区相对路径 apply_patch_created.txt，内容精确为 created\\nsecond（文件末尾不要换行）。\n"
            "不要调用其它文件编辑或 shell 工具。工具成功后只回复：multi_file_done"
        ),
    )

    assert run.tool_names.count("apply_patch") == 1
    assert not deleted_file.exists()
    assert created_file.read_text(encoding="utf-8") == "created\nsecond"
    changeset_response = await client.get(
        f"/api/v1/sessions/{run.session_id}/changesets/all"
    )
    assert changeset_response.status_code == 200
    paths = {
        item["file_path"] for item in changeset_response.json()["data"]["files"]
    }
    assert paths == {"/apply_patch_delete_me.txt", "/apply_patch_created.txt"}


@pytest.mark.asyncio
async def test_model_uses_apply_patch_hints_and_move(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
    is_debug: bool,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    source_file = workspace_root / "apply_patch_classes.py"
    target_file = workspace_root / "apply_patch_renamed.py"
    source_file.write_text(
        """class First:
    def value(self):
        return "same"

class Second:
    def value(self):
        return "same"
""",
        encoding="utf-8",
    )
    timeout = 100000 if is_debug else 120.0

    run = await _run_model_apply_patch(
        client,
        title="apply-patch-hints-move",
        timeout=timeout,
        prompt=(
            "必须调用 apply_patch，把工作区相对路径 apply_patch_classes.py 移动到 apply_patch_renamed.py，"
            "并且只把 class Second 的 value 返回值从 same 改成 changed，class First 保持不变。\n"
            "补丁必须使用 *** Move to，并用 @@ class Second: 和 @@     def value(self): 两级提示定位重复代码。\n"
            "不要调用其它文件编辑或 shell 工具。成功后只回复：hint_move_done"
        ),
    )

    assert run.tool_names.count("apply_patch") == 1
    assert not source_file.exists()
    target_content = target_file.read_text(encoding="utf-8")
    ast.parse(target_content)
    assert 'return "same"' in target_content
    assert 'return "changed"' in target_content
    assert target_content.index('return "same"') < target_content.index("class Second:")
    assert target_content.index('return "changed"') > target_content.index("class Second:")
    changeset_response = await client.get(
        f"/api/v1/sessions/{run.session_id}/changesets/all"
    )
    assert changeset_response.status_code == 200
    paths = {
        item["file_path"] for item in changeset_response.json()["data"]["files"]
    }
    assert paths == {"/apply_patch_classes.py", "/apply_patch_renamed.py"}
