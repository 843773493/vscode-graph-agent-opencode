"""真实 Gateway/Agent 工具循环的块身份跨 HTTP、SSE 与 Backend 重启保持一致。"""

from __future__ import annotations

import json

from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 复用隔离进程及 model_stream
)
from tests.support.api_helpers import create_session


async def test_product_snapshots_keep_original_block_model_calls_after_restart(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    (case.workspace / "identity-source.txt").write_text("identity-tool-result", encoding="utf-8")
    session = await create_session(case.client, "消息块模型归属")
    calls = await case.plan(session, [
        {**_tool("read_file", {"path": "identity-source.txt"}, "identity-read"), "content": "identity-before-tool"},
        {"content": "identity-after-tool"},
    ])
    accepted = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "读取 identity-source.txt 后答复"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()["data"]
    await _completed(case.client, result["job_id"])
    tool_result = case.tool_result(session, calls[0] + ":tool-call:identity-read")
    assert tool_result["tool_outcome"] == "success" and tool_result["execution_confirmed"] is True
    content = tool_result["content"]
    assert isinstance(content, str) and "identity-tool-result" in content
    base = f"/api/v1/sessions/{session}/turns/{result['turn_id']}/message-stream"
    response = await case.client.get(base + "/events")
    assert response.status_code == 200, response.text
    events = response.json()["data"]
    block_owners = {event["block_id"]: event["model_call_id"] for event in events if event["type"] == "block.started"}
    assert set(block_owners.values()) == set(calls)
    canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    snapshots = []
    for cold in (False, True):
        if cold:
            await case.hard_restart()
        response = await case.client.get(base + "/snapshot")
        assert response.status_code == 200, response.text
        snapshot = response.json()["data"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert {block["block_id"]: block["model_call_id"] for block in snapshot["blocks"]} == block_owners
        assert snapshot["current_model_call_id"] == calls[-1]
        assert {block["text"] for block in snapshot["blocks"]} == {"identity-before-tool", "identity-after-tool"}
        # 已收到全部事件的客户端以高水位重连，服务端返回终态快照控制帧。
        stream = await case.client.get(base, params={"after_seq": snapshot["snapshot_seq"]})
        assert stream.status_code == 200, stream.text
        frames = [json.loads(line.removeprefix("data:")) for line in stream.text.splitlines() if line.startswith("data:")]
        control, = [frame for frame in frames if frame["type"] == "stream.snapshot"]
        core = {key: value for key, value in snapshot.items() if key not in {"session_id", "thread_id", "turn_id", "turn_stream_id"}}
        assert control["payload"] == core
        assert canonical.read_bytes() == original
        snapshots.append(snapshot)
    assert snapshots[1] == snapshots[0]
    case.record("block-model-call-publication", {"calls": calls, "events": events, "snapshots": snapshots,
        "requests": [case.request(call) for call in calls], "tool_result": tool_result})
