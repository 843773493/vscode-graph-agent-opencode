"""真实消息／工具执行生成历史，验证工具正文类别、完整输出和原详情定位。"""

import asyncio
import json

import httpx
import pytest

from app.agents.tools.testing import LARGE_TEST_OUTPUT, LARGE_TEST_TARGET_VALUE
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.services.infrastructure.rollout_history import message_search
from tests.integration.backend.sessions.test_history_message_search import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_workspace_message_search import (
    workspace_query as workspace_query,  # noqa: PLC0414
)


@pytest.fixture
async def tool_history(runtime_case: RuntimeCase, history_client: httpx.AsyncClient):
    case = runtime_case

    def reply(_request):
        if len(case.model_requests) == 1:
            delta = {"role": "assistant", "tool_calls": [
                {"index": index, "id": call_id, "type": "function",
                 "function": {"name": "invoke_extension_tool", "arguments": json.dumps({
                     "tool_name": tool_name, "arguments": {},
                 })}}
                for index, (call_id, tool_name) in enumerate([
                    ("search-output", "large_test_output"), ("search-inline", "test_tool_2"),
                ])
            ]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "工具执行完成"}, "stop"
        return [{"choices": [{"index": 0, "delta": delta}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]

    case.response_chunks = reply
    container = case.runner.container
    session, main = await _session(case.runner)
    child = await _child(case, session)
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        base = f"/api/v1/sessions/{session}/threads"
        response = await history_client.post(f"{base}/{child}/messages", json={"message": {"content": "执行大工具"}, "run": {}})
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        job = container.job_service._jobs[accepted["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 25)
        assert job.status.value == "completed", job.error_message
        node = get_session_path_resolver(case.runner.workspace / ".boxteam" / "sessions").resolve_session_node(session)
        output_path, = (node / "tool-results").iterdir()
        assert output_path.read_text(encoding="utf-8") == LARGE_TEST_OUTPUT
        yield base, main, child, accepted, output_path


async def test_tool_result_search_uses_full_output_and_exact_original_details(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, tool_history, workspace_query,
):
    base, main, child, accepted, _output_path = tool_history
    payload = {"query": LARGE_TEST_TARGET_VALUE, "category": "tool_result"}
    ordinary = await history_client.post(f"{base}/{child}/history/search", json={"query": LARGE_TEST_TARGET_VALUE})
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["data"]["items"] == []
    absent = await history_client.post(f"{base}/{main}/history/search", json=payload)
    assert absent.status_code == 200 and absent.json()["data"]["items"] == []
    response = await history_client.post(f"{base}/{child}/history/search", json=payload)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["complete"] and page["cursor"] is None
    hit, = page["items"]
    assert hit["turn_id"] == accepted["turn_id"] and hit["role"] == "tool"
    assert hit["tool_call_id"].endswith(":tool-call:search-output")
    assert hit["reference"]["category"] == "tool_result"
    reference = hit["reference"]
    assert reference["start_codepoint"] > 65536
    assert LARGE_TEST_OUTPUT[reference["start_codepoint"]:reference["end_codepoint"]] == LARGE_TEST_TARGET_VALUE
    assert page["scanned_bytes"] >= len(LARGE_TEST_OUTPUT.encode())
    around = await history_client.post(f"{base}/{child}/history", json={
        "direction": "around", "anchor_turn_id": hit["turn_id"], "anchor_message": reference,
        "before_turns": 0, "after_turns": 0,
    })
    assert around.status_code == 200, around.text
    located = around.json()["data"]
    assert located["located_message"]["content"] == LARGE_TEST_OUTPUT
    assert located["located_message"]["tool_call_id"] == hit["tool_call_id"]
    parts = located["items"][0]["response_parts"]
    assert any(part["kind"] == "tool_result" and part["source"]["item_id"] == reference["item_id"] for part in parts)
    workspace = await workspace_query(json=payload)
    assert workspace.status_code == 200, workspace.text
    workspace_hit, = workspace.json()["data"]["items"]
    assert workspace_hit["thread"]["thread_id"] == child and workspace_hit["match"] == hit
    inline = await history_client.post(f"{base}/{child}/history/search", json={
        "query": "4568", "category": "tool_result",
    })
    assert inline.status_code == 200, inline.text
    inline_hit, = inline.json()["data"]["items"]
    assert inline_hit["tool_call_id"].endswith(":tool-call:search-inline")
    assert inline_hit["reference"]["item_id"] != reference["item_id"]
    inline_details = await history_client.post(f"{base}/{child}/history", json={
        "direction": "around", "anchor_turn_id": inline_hit["turn_id"], "anchor_message": inline_hit["reference"],
    })
    assert inline_details.status_code == 200, inline_details.text
    assert inline_details.json()["data"]["located_message"]["content"] == "4568"
    assert inline_details.json()["data"]["located_message"]["tool_call_id"] == inline_hit["tool_call_id"]
    assert len(runtime_case.model_requests) == 2


async def test_tool_result_cursor_category_limits_and_stale_reference(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, tool_history, monkeypatch: pytest.MonkeyPatch,
):
    base, _main, child, _accepted, output_path = tool_history
    query = {"query": "large-output-line", "category": "tool_result", "limit": 1}
    response = await history_client.post(f"{base}/{child}/history/search", json=query)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["cursor"] and page["limited_reason"] == "result_limit"
    wrong_category = await history_client.post(f"{base}/{child}/history/search", json={
        **query, "category": "message", "cursor": page["cursor"],
    })
    assert wrong_category.status_code == 400, wrong_category.text
    following = await history_client.post(f"{base}/{child}/history/search", json={**query, "cursor": page["cursor"]})
    assert following.status_code == 200, following.text
    assert following.json()["data"]["items"][0]["reference"]["start_codepoint"] > page["items"][0]["reference"]["start_codepoint"]
    hit = page["items"][0]
    stale = await history_client.post(f"{base}/{child}/history", json={
        "direction": "around", "anchor_turn_id": hit["turn_id"],
        "anchor_message": {**hit["reference"], "content_hash": "changed"},
    })
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_turn_reference"
    with monkeypatch.context() as patch:
        patch.setattr(message_search, "MAX_SEARCH_BYTES", 64 * 1024)
        limited = await history_client.post(f"{base}/{child}/history/search", json=query)
        assert limited.status_code == 200, limited.text
        data = limited.json()["data"]
        assert not data["complete"] and data["cursor"] is None and data["limited_reason"] == "item_size_limit"
        assert data["scanned_bytes"] <= 64 * 1024
    # 保持字节长度仍须识别版本变化，不能在原详情中展示已改写的正文。
    original = output_path.read_bytes()
    try:
        output_path.write_bytes(b"X" + original[1:])
        changed = await history_client.post(f"{base}/{child}/history", json={
            "direction": "around", "anchor_turn_id": hit["turn_id"], "anchor_message": hit["reference"],
        })
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "stale_turn_reference"
        output_path.unlink()
        deleted = await history_client.post(f"{base}/{child}/history", json={
            "direction": "around", "anchor_turn_id": hit["turn_id"], "anchor_message": hit["reference"],
        })
        assert deleted.status_code == 409 and deleted.json()["detail"]["code"] == "stale_turn_reference"
    finally:
        output_path.write_bytes(original)
    assert len(runtime_case.model_requests) == 2
