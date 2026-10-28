"""真实工具历史及冻结副本损坏不能触发前缀回退。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from contextlib import closing

import pytest

from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
    read_captured_snapshot,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    _completed_parent,
    _first_response_item,
)
from tests.integration.backend.sessions.fork.test_task_context import (
    deadlines as deadlines,  # noqa: PLC0414 - 保留正式60秒fork监督器
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用真实服务及model_stream
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry

pytestmark = pytest.mark.parametrize("source_case", [{
    "managed_gateway": True, "terminal": True, "browser": True,
}], indirect=True)


async def test_public_prefix_rejects_open_real_tool_group(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    session = await create_session(case.client, "真实工具组前缀")
    await case.plan(session, [
        _tool_reply("exec_command", {"cmd": "printf prefix-protocol-result", "yield_time_ms": 1000}, "prefix-protocol-call"),
        {"content": "prefix-protocol-completed"},
    ])
    accepted = await _send(case, session, "执行工具后验证安全截止点")
    thread = (await _job(case, accepted["job_id"]))["thread_id"]
    items = case.container.checkpointer._storage.storage.read_items(session, thread)
    selected = {row[0] for row in case.sql(session, thread,
        "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
        "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
        "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1")}
    calls = [item for item in items if item.item_id in selected and item.semantic_kind == "tool_call"
             and item.projection_identity.projection_message_id is not None]
    results = [item for item in items if item.item_id in selected and item.semantic_kind == "tool_result"
               and item.projection_identity.projection_message_id is not None]
    call, = calls
    result, = results
    assert result.payload["tool_call_id"] == call.payload["tool_calls"][0]["id"] == "prefix-protocol-call"
    assert "prefix-protocol-result" in result.payload["content"]
    safe = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
        "thread_id": thread, "item_id": _first_response_item(case, session, thread),
    })
    assert safe.status_code == 200, safe.text
    point = safe.json()["data"]
    for anchor, mode in ((call.item_id, "inclusive"), (result.item_id, "before")):
        key = f"protocol-open-{mode}"
        deadline = deadlines.for_operation(target=point["child_container_uri"], operation_key=key)
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
            "idempotency_key": key, "mode": "view_prefix_fork", "source_uri": point["source_uri"],
            "target_container_uri": point["child_container_uri"], "anchor_item_id": anchor, "anchor_mode": mode,
        }), "拒绝未闭合的真实工具组")
        assert response.status_code == 500, response.text
        assert "ToolProtocolBoundaryConflict: tool-protocol-boundary-conflict" in response.json()["message"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        deadline.complete()
    children = await case.client.get(f"/api/v1/sessions/{session}/child-threads")
    assert children.status_code == 200 and children.json()["data"]["items"] == [], children.text
    with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
        assert not owner.control_store.list_child_thread_rows()
        assert not owner.control_store.list_initial_execution_intents()
    case.write_actual("prefix-open-tool", {"call": call.item_id, "result": result.item_id, "point": point})


@pytest.mark.parametrize("fault", ["revision", "manifest", "body_hash"])
async def test_public_prefix_retry_rejects_corrupted_frozen_facts(
    source_case: RuntimeSourceCase, deadlines: ForkDeadlineRegistry,
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    case = source_case
    session, thread = await _completed_parent(case)
    item = _first_response_item(case, session, thread)
    point_response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
        "thread_id": thread, "item_id": item,
    })
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    key = "frozen-prefix-corruption-" + fault
    creation_key = case.container.session_context_fork_service._creation_key(key)
    body = {"idempotency_key": key, "mode": "view_prefix_fork", "source_uri": point["source_uri"],
            "target_container_uri": point["child_container_uri"], "anchor_item_id": item}
    phase = "copy.source.captured"
    case.control.arm(creation_key, phase)
    deadline = deadlines.for_operation(target=point["child_container_uri"], operation_key=key)
    operation = asyncio.create_task(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=body))
    try:
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, creation_key, phase), "原前缀已冻结")
        operation.cancel("模拟冻结后调用中断")
        with pytest.raises(asyncio.CancelledError, match="冻结后调用中断"):
            await deadline.wait(operation, "退出原创建调用")
    finally:
        case.control.release(creation_key, phase)
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    resolver = case.container.session_service.path_resolver
    catalog = resolver.catalog_store
    capture = catalog.get_fork_source_capture(creation_key=creation_key)
    assert capture.state == "source_captured"
    snapshot, marker_hash = read_captured_snapshot(resolver.sessions_root, capture)
    original_point = capture.captured_point
    root = capture_directory(resolver.sessions_root, capture)
    # 只破坏产品已经冻结的原件，不手工构造业务历史或修补binding。
    if fault == "revision":
        envelope = json.loads(capture.source_point_json)
        envelope["point"]["source_view_revision"] += 1
        corrupted = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(resolver.sessions_root.parent / "navigation/session-catalog.sqlite")) as connection:
            connection.execute("UPDATE fork_source_captures SET source_point_json=? WHERE creation_key=?", (corrupted, creation_key))
            connection.commit()
        expected = "journal 绑定 hash 冲突"
    elif fault == "manifest":
        manifest = snapshot.captured_root / "snapshot-manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        expected = "manifest/owner 不一致"
    else:
        jsonl = snapshot.captured_root / "rollout/rollout.jsonl"
        original = jsonl.read_bytes()
        assert b"task-history-answer-unique" in original
        jsonl.write_bytes(original.replace(b"task-history-answer-unique", b"task-history-answer-tamper"))
        expected = "files hash/length 不一致"

    async def forbid_recapture(**_kwargs):
        pytest.fail("冻结原件损坏后不得回源捕获另一个前缀")

    monkeypatch.setattr(case.container.checkpointer.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
    try:
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=body), "拒绝损坏的原冻结事实")
        case.write_actual("prefix-corruption-response-" + fault, {"status": response.status_code, "body": response.text})
        assert response.status_code == 500, response.text
        assert response.json()["code"] == 500 and response.json()["data"] is None
        assert response.json()["message"].startswith("RuntimeError: ") and expected in response.json()["message"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
    except BaseException:
        # 原watchdog继续监督未确认结束的操作，先保留不会被teardown遮蔽的测试错误。
        case.write_actual("prefix-corruption-failure-" + fault, {"traceback": traceback.format_exc()})
        raise
    deadline.complete()
    children = await case.client.get(f"/api/v1/sessions/{session}/child-threads")
    assert children.status_code == 200 and children.json()["data"]["items"] == [], children.text
    with case.container.thread_initial_execution_service._factory.for_owner_session(session) as owner:
        record = owner.control_store.get_thread_creation_record(creation_key)
        assert record.state != "published" and record.child_thread_id == capture.target_thread_id
        assert not owner.control_store.list_child_thread_rows()
        assert not owner.control_store.list_initial_execution_intents()
    case.write_actual("prefix-corruption-" + fault, {
        "original_point": original_point.model_dump(), "snapshot_id": capture.snapshot_id,
        "marker_hash": marker_hash, "rejection": response.json(), "capture_root_exists": root.exists(),
    })
