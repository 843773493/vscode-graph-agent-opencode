"""真实 Agent 调用 Python 后，Provider 与冷读历史不携带解释器位置。"""

from __future__ import annotations

import json

import pytest

from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - fixture 沿当前文件隔离
)
from tests.support.api_helpers import create_session


@pytest.fixture(autouse=True)
def local_model_metadata(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")


async def test_python_tool_result_preserves_streams_without_interpreter_locator(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "Python 结果位置边界")
    calls = await case.plan(session, [
        _tool("python_exec", {"code": (
            "import sys\nfrom pathlib import Path\n"
            "Path('python-result-proof.txt').write_text('target workspace\\n')\n"
            "print('python stdout Ω')\nprint('python stderr Ω', file=sys.stderr)"
        )}, "call_python_result"), {"content": "Python 结果验证完成"},
    ])
    accepted = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "python-result-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    accepted_data = accepted.json()["data"]
    job_id = accepted_data["job_id"]
    turn_id = accepted_data["turn_id"]
    await _completed(case.client, job_id)
    assert "python_exec" in {entry["function"]["name"] for entry in case.request(calls[0])["tools"]}
    tool_results = [message for message in case.request(calls[-1])["messages"] if message["role"] == "tool"]
    result, = [json.loads(message["content"]) for message in tool_results]
    assert result == {"returncode": 0, "stdout": "python stdout Ω\n", "stderr": "python stderr Ω\n"}
    assert str(case.workspace) not in json.dumps(result, ensure_ascii=False)
    assert len(tool_results) == 1
    assert (case.workspace / "python-result-proof.txt").read_text() == "target workspace\n"

    async def events():
        response = await case.client.get(
            f"/api/v1/sessions/{session}/turns/{turn_id}/message-stream/events",
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        wire = json.dumps(payload, ensure_ascii=False)
        assert "python stdout Ω" in wire and "python stderr Ω" in wire
        assert "python_executable" not in wire
        return payload

    async def history():
        response = await case.client.post(f"/api/v1/sessions/{session}/history", json={
            "turn_ids": [turn_id], "tool_call_ids": [canonical_tool_call_id],
            "include": ["tool_call", "tool_result"],
        })
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        detail, = payload["items"]
        result, = [part for part in detail["response_parts"] if part["kind"] == "tool_result"]
        assert result["source"]["item_id"] and result["tool_call_id"] == canonical_tool_call_id
        assert json.loads(result["result"]) == json.loads(tool_results[0]["content"])
        assert "python_executable" not in json.dumps(payload)
        return payload

    before = await events()
    completed, = [event["payload"] for event in before if event["type"] == "tool.completed"]
    assert completed["tool_name"] == "python_exec"
    canonical_tool_call_id = completed["tool_call_id"]
    before_history = await history()
    await case.hard_restart()
    assert await events() == before
    assert await history() == before_history
    case.record("python-tool-result", {
        "provider_tool_results": tool_results, "events": before, "history": before_history,
    })
