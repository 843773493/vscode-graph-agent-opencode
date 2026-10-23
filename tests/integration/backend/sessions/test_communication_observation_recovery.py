"""真实工具响应已到达来源、尚未提交时的 Backend 硬退出恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 显式注册共享 pytest fixture
)
from tests.support.api_helpers import create_session


async def _message(case: CommunicationCase, session_id: str, text: str) -> dict[str, object]:
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": text}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


def _tool_results(database: Path, call_id: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT item_id,status,execution_id,producer_ref_json,projection_manifest_json "
            "FROM item_catalog WHERE semantic_kind='tool_result' AND tool_call_id=?",
            (call_id,),
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "wait"])
async def test_hard_restart_replays_original_observation_and_one_tool_result(
    communication_case: CommunicationCase, operation: str,
) -> None:
    """产品入口生成历史；进程外屏障只暂停，不替产品填写恢复状态。"""
    case = communication_case
    source = await create_session(case.client, "观察恢复来源")
    target = await create_session(case.client, "观察恢复目标")
    await case.plan(target, [{"content": "冻结前的目标结果"}])
    target_dispatch = await _message(case, target, "生成冻结前历史")
    await _completed(case.client, target_dispatch["job_id"])
    if operation == "read":
        tool_name = "invoke_extension_tool"
        arguments = {"tool_name": "read_context", "arguments": {
            "resource_identity": str(session_resource_identity(case.backend.workspace_id, target)),
            "uri": session_resource_uri(case.backend.workspace_id, target),
            "scope": "workspace", "view": "messages",
        }}
    else:
        tool_name = "wait_for_session"
        arguments = {"target_session_id": target, "job_id": target_dispatch["job_id"]}
    provider_call = "observation-crash-" + operation
    first, final = await case.plan(source, [
        _tool(tool_name, arguments, provider_call), {"content": "原观察已恢复"},
    ])
    source_thread = case.planned_calls[first]["thread_id"]
    source_call = software_tool_operation_id(operation, source, source_thread, first, provider_call)
    scoped_call = first + ":tool-call:" + provider_call
    phase = "observation.source.response_received"
    case.replay.control.arm(source_call, phase)
    try:
        dispatch = await _message(case, source, "观察目标并保留原结果")
        await asyncio.to_thread(case.replay.control.acknowledged, source_call, phase, 60)
        source_node = await case.session_node(source)
        target_node = await case.session_node(target)
        database = case.main_thread_node(source) / "rollout/index.sqlite"
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            original, = store.list_remote_observations()
            assert original.side == "source"
            envelope = original.request["envelope"]
            assert envelope["source_call_id"] == source_call
            assert envelope["source"]["session_id"] == source
            assert envelope["source"]["thread_id"] == source_thread
            source_page, = store.list_remote_pages(original.observation_id, side="source")
            assert source_page.result is source_page.target_result is None
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            frozen = store.get_remote_observation(original.observation_id, side="target")
            target_page, = store.list_remote_pages(original.observation_id, side="target")
            assert frozen.snapshot is not None and target_page.result is not None
        assert _tool_results(database, scoped_call) == []
        assert not (case.replay.artifacts / (final + ".json")).exists()
        if operation == "read":
            assert "冻结前的目标结果" in json.dumps(target_page.result, ensure_ascii=False)
        else:
            assert target_page.result["status"] == "completed"
            assert target_page.result["observed"][0]["selector_id"] == target_dispatch["job_id"]
            assert envelope["query"]["deadline"]["timeout_seconds"] == 60

        # 冻结之后再通过真实消息推进目标，恢复不能重新读取最新历史或换 selector。
        await case.plan(target, [{"content": "冻结后不应混入的目标结果"}])
        newer = await _message(case, target, "生成冻结后历史")
        await _completed(case.client, newer["job_id"])
        await case.hard_restart()
    finally:
        case.replay.control.release(source_call, phase)

    recovered_job = await _completed(case.client, dispatch["job_id"])
    assert recovered_job["thread_id"] == source_thread
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        recovered, = store.list_remote_observations()
        recovered_page, = store.list_remote_pages(original.observation_id, side="source")
        assert recovered == original
        assert recovered_page.request == source_page.request
        assert recovered_page.target_result == target_page.result
        assert recovered_page.result is not None
        for identity in ("source-observation|" + original.observation_id, "source-page|" + source_page.page_identity):
            assert store.find_lease_by_operation(identity).state == "completed"
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        assert store.get_remote_observation(original.observation_id, side="target") == frozen
        assert store.list_remote_pages(original.observation_id, side="target") == (target_page,)
    request = case.request(final)
    result, = [message for message in request["messages"] if message["role"] == "tool"]
    assert result["tool_call_id"] == provider_call and result["name"] == tool_name
    assert json.loads(result["content"]) == recovered_page.result
    assert "冻结后不应混入的目标结果" not in result["content"]
    canonical, = _tool_results(database, scoped_call)
    assert canonical[1] == "completed"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT execution_id FROM executions WHERE job_id=?", (dispatch["job_id"],),
        ).fetchall() == [(canonical[2],)]
    history = await case.client.post(f"/api/v1/sessions/{source}/history", json={})
    assert history.status_code == 200, history.text
    assert history.json()["data"]["thread_id"] == source_thread
    case.record("observation-hard-restart-" + operation, {
        "source_call_id": source_call, "source_before": asdict(original),
        "source_page_before": asdict(source_page), "source_page_after": asdict(recovered_page),
        "target_frozen": asdict(frozen), "target_page": asdict(target_page),
        "job": recovered_job, "canonical_result": canonical, "history": history.json(),
    })
