"""真实 HTTP 与后端硬重启验证前缀冻结及原创建 owner 恢复。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.infrastructure.rollout_context.fork.source_capture import (
    read_captured_snapshot,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
    _wire_text,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用真实进程及model_stream
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry


@pytest.fixture
def fork_deadlines(restart_source_case: RestartRuntimeSourceCase) -> Iterator[ForkDeadlineRegistry]:
    registry = ForkDeadlineRegistry(restart_source_case.artifacts_dir)
    try:
        yield registry
    finally:
        registry.close()


def _read(database: Path, query: str, params: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(query, params).fetchall()


def _sql(case: RestartRuntimeSourceCase, session: str, thread: str, query: str, params: tuple = ()) -> list[tuple]:
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    return _read(resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite", query, params)


def _record(case: RestartRuntimeSourceCase, session: str, key: str, child: bool) -> dict[str, object]:
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    if not child:
        return asdict(resolver.catalog_store.get_creation_record(key))
    database = resolver.resolve_session_node(session) / CONTROL_DATABASE_NAME
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT * FROM thread_creation_records WHERE thread_creation_idempotency_key=?", (key,),
        ).fetchone())


@pytest.mark.parametrize("restart_source_case", [{"plugin_ids": ("terminal", "browser"), "managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("child,phase,anchor_mode", [
    (True, "copy.source.before_captured_cas", "inclusive"),
    (False, "copy.target.published", "before"),
])
async def test_public_prefix_hard_restart_reuses_capture_after_source_advances(
    restart_source_case: RestartRuntimeSourceCase, fork_deadlines: ForkDeadlineRegistry,
    child: bool, phase: str, anchor_mode: str,
) -> None:
    case = restart_source_case
    session = await create_session(case.client, "公开前缀硬重启")
    await case.plan(session, [{"content": "restart-prefix-frozen-answer"}, {"content": "restart-prefix-later-answer"}])
    accepted = await _send(case, session, "restart-prefix-frozen-question")
    thread = (await _job(case, accepted["job_id"]))["thread_id"]
    item_id, = _sql(case, session, thread,
        "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
        "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
        "JOIN item_catalog item USING(item_id) WHERE namespace.checkpoint_ns='' AND member.visible=1 "
        "AND member.context_selected=1 AND item.semantic_kind='assistant_output' ORDER BY member.context_ordinal LIMIT 1")[0]
    point_response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
        "thread_id": thread, "item_id": item_id, "anchor_mode": anchor_mode,
    })
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    key = "public-prefix-restart-" + ("child" if child else "main")
    creation_key = SessionContextForkService._creation_key(key)
    request = {
        "idempotency_key": key, "mode": "view_prefix_fork", "source_uri": point["source_uri"],
        "source_view_revision": point["source_view_revision"], "anchor_item_id": item_id, "anchor_mode": anchor_mode,
        "creation_kind": "new_child_thread" if child else "new_session_main",
        "target_container_uri": point["child_container_uri"] if child else point["main_container_uri"],
    }
    case.control.arm(creation_key, phase)
    deadline = fork_deadlines.for_operation(target=request["target_container_uri"], operation_key=key)
    operation = asyncio.create_task(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=request))
    try:
        await deadline.wait(asyncio.to_thread(case.control.acknowledged, creation_key, phase), "公开前缀中断边界")
        original = _record(case, session, creation_key, child)
        assert original["state"] == ("preparing" if child else "published")
        assert json.loads(original["request_intent_json"])["source_point"] is None
        target_session = session if child else original["session_id"]
        target_thread = original["child_thread_id"] if child else original["main_thread_id"]
        resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
        capture = resolver.catalog_store.get_fork_source_capture(creation_key=creation_key)
        assert capture.state == ("preparing" if child else "materialized")
        if child:
            snapshot, marker_hash = read_captured_snapshot(resolver.sessions_root, capture)
            frozen = snapshot.source_point
        else:
            # 发布已清理私有快照，实际坐标及完整性事实由原 journal 持久保存。
            frozen, marker_hash = capture.captured_point, capture.marker_sha256
            assert marker_hash is not None
        assert (frozen.source_view_id, frozen.source_view_revision, frozen.anchor_item_id) == (
            point["source_view_id"], point["source_view_revision"], item_id,
        )
        visible = await case.client.get(f"/api/v1/sessions/{session}/child-threads")
        assert visible.status_code == 200 and visible.json()["data"]["items"] == [], visible.text
        # 保持 fork 屏障，让来源通过正常执行形成更新候选。
        later = await deadline.wait(_send(case, session, "restart-prefix-later-question"), "冻结后来源追加")
        await deadline.wait(_job(case, later["job_id"]), "来源后继执行完成")
        latest_response = await deadline.wait(case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
            "thread_id": thread, "item_id": item_id,
        }), "核对后继候选")
        case.write_actual(f"prefix-{key}-latest-response", {
            "status": latest_response.status_code, "response": latest_response.json(),
            "frozen_point": frozen.model_dump(),
        })
        assert latest_response.status_code == 200, latest_response.text
        latest = latest_response.json()["data"]
        assert latest["source_view_id"] != frozen.source_view_id
        assert latest["source_view_revision"] > frozen.source_view_revision
        case.write_actual(f"prefix-{key}-before-kill", {
            "phase": phase, "record": original, "capture": asdict(capture),
            "frozen_point": frozen.model_dump(), "marker_hash": marker_hash, "latest": latest,
        })
        await deadline.wait(case.restart(hard=True, while_stopped=lambda: case.control.release(creation_key, phase)), "后端硬重启与原 owner 恢复")
        interrupted, = await asyncio.gather(operation, return_exceptions=True)
        assert isinstance(interrupted, httpx.TransportError), interrupted
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=request), "冷重试原请求")
        assert response.status_code == 200, response.text
        deadline.complete()
    except Exception:
        # 未完成 fork 的 watchdog 会终止运行组，先保存可能被 fixture 清理遮住的原异常。
        case.write_actual(f"prefix-{key}-failure", {"traceback": traceback.format_exc()})
        raise
    finally:
        case.control.release(creation_key, phase)
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)

    result = response.json()["data"]
    assert result["session"]["session_id"] == target_session and result["thread_id"] == target_thread
    assert (result["source_view_id"], result["source_view_revision"], result["anchor_item_id"]) == (
        frozen.source_view_id, frozen.source_view_revision, frozen.anchor_item_id,
    )
    recovered = _record(case, session, creation_key, child)
    assert recovered["state"] == "published"
    immutable = set(original) - {"state", "record_updated_at", "fork_completion_json"}
    assert {name: recovered[name] for name in immutable} == {name: original[name] for name in immutable}
    restored = resolver.catalog_store.get_fork_source_capture(creation_key=creation_key)
    assert restored.state == "materialized"
    assert (restored.snapshot_id, restored.snapshot_relative_locator, restored.preimage_hash, restored.marker_sha256) == (
        capture.snapshot_id, capture.snapshot_relative_locator, capture.preimage_hash, marker_hash,
    )
    assert (restored.captured_point.source_view_id, restored.captured_point.source_view_revision, restored.captured_point.anchor_item_id) == (
        frozen.source_view_id, frozen.source_view_revision, frozen.anchor_item_id,
    )
    assert _sql(case, target_session, target_thread,
        "SELECT materialization_id,fork_id,source_session_id,source_thread_id,target_session_id,target_thread_id,"
        "source_view_id,fork_mode,relationship,status,source_snapshot_sha256 FROM fork_materializations") == [
            (restored.materialization_id, restored.fork_id, session, thread, target_session, target_thread,
             frozen.source_view_id, "view_prefix_fork", "detached", "committed", restored.snapshot_sha256),
        ]
    if child:
        assert json.loads(recovered["fork_completion_json"]) == {
            "fork_id": restored.fork_id, "materialization_id": restored.materialization_id,
            "source_snapshot_sha256": restored.snapshot_sha256,
            "target_session_id": target_session, "target_thread_id": target_thread,
        }
    comparison = "<=" if anchor_mode == "inclusive" else "<"
    expected = _sql(case, session, thread,
        "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 "
        f"AND context_ordinal{comparison}(SELECT context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) "
        "ORDER BY context_ordinal", (frozen.source_view_id, frozen.source_view_id, item_id))
    actual = _sql(case, target_session, target_thread,
        "SELECT member.item_id FROM checkpoint_namespace_state namespace JOIN branches branch "
        "ON branch.branch_id=namespace.active_branch_id JOIN context_view_items member ON member.view_id=branch.head_view_id "
        "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal")
    assert actual == expected
    assert case.call_count(target_session, target_thread) == 0
    assert _sql(case, target_session, target_thread,
        "SELECT execution_id FROM executions WHERE session_id=? AND thread_id=?", (target_session, target_thread)) == []
    # 第二次启动及重试仍复用同一个发布结果，不能恢复导入的历史执行。
    retry_deadline = fork_deadlines.for_operation(target=request["target_container_uri"], operation_key=key + "-second-restart")
    await retry_deadline.wait(case.restart(hard=False), "第二次后端重启")
    repeated = await retry_deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json=request), "二次冷重试")
    assert repeated.status_code == 200 and repeated.json()["data"] == result, repeated.text
    retry_deadline.complete()
    assert _record(case, session, creation_key, child) == recovered
    child_call, = await case.plan(target_session, [{"content": "restart-prefix-target-answer"}], thread_id=target_thread)
    sent = await case.client.post(f"/api/v1/sessions/{target_session}/threads/{target_thread}/messages", json={
        "message": {"content": "restart-prefix-target-question"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert sent.status_code == 200, sent.text
    await _job(case, sent.json()["data"]["job_id"])
    wire = _wire_text(case.request(child_call)["messages"])
    assert "restart-prefix-frozen-question" in wire
    assert ("restart-prefix-frozen-answer" in wire) == (anchor_mode == "inclusive")
    assert "restart-prefix-later" not in wire
    assert case.call_count(target_session, target_thread) == 1
    case.write_actual(f"prefix-{key}-after-restart", {
        "record": recovered, "capture": asdict(restored), "result": result,
        "expected": expected, "actual": actual, "target_call": child_call,
        "backend_pids": [backend.process.pid for backend in case.backends],
    })
