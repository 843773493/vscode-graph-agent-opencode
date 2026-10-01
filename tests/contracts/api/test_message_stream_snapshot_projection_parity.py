"""SSE 控制帧与 HTTP 快照必须逐键同形，零值与空 repeated 不得被 proto3 省略。

本用例只覆盖 f1010dc4 收敛出的公共投影出口在边界 state 下不产生第二份形状，
与 test_message_stream_snapshot_projection.py 的端到端订阅用例互补。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_message_stream_store
from app.api.message_stream import _public_event_json, _public_snapshot
from app.api.message_stream import router as message_stream_router
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure import message_stream_store as store_module
from app.services.infrastructure.message_stream_store import MessageStreamStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

_IDENTITY_FIELDS = ("session_id", "turn_id", "turn_stream_id")
_REPEATED_FIELDS = (
    "blocks",
    "tool_calls",
    "tool_executions",
    "model_calls",
    "activities",
    "resource_refs",
)


@pytest.fixture
def message_stream_api() -> tuple[FastAPI, MessageStreamStore, str, str]:
    output_root = (
        Path.cwd()
        / "out/tests/contracts/api/test_message_stream_snapshot_projection_parity"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    sessions_root = output_root / "workspace" / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    session_id = "ses_12345678123446788234567812345678"
    turn_id = "job_snapshot_parity"
    seed_catalog_session_bundle(sessions_root, session_id)

    store = MessageStreamStore(path_resolver=resolver)
    api = FastAPI()
    api.add_middleware(TraceMiddleware)
    api.include_router(message_stream_router, prefix="/api/v1")
    api.dependency_overrides[get_message_stream_store] = lambda: store
    try:
        yield api, store, session_id, turn_id
    finally:
        api.dependency_overrides.clear()


def _headers(request_id: str) -> dict[str, str]:
    return {"X-Local-Token": "local-dev-token", "X-Request-ID": request_id}


def _snapshot_core(payload: object) -> dict[str, object]:
    assert isinstance(payload, dict)
    return {key: value for key, value in payload.items() if key not in _IDENTITY_FIELDS}


def test_projection_matrix_is_key_identical_for_zero_and_edge_states(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    """直接对同一 state 跑两个出口，覆盖 proto3 会省略零值/空 repeated 的字段。"""
    _, _, session_id, turn_id = message_stream_api
    turn_stream_id = "strm_projection_matrix"

    def state(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_stream_id": turn_stream_id,
            "job_id": None,
            "workspace_id": None,
            "snapshot_seq": 0,
            "stream_status": "open",
            "agent_loop_status": "running",
            "current_model_call_id": None,
            "current_attempt": 0,
            "blocks": [],
            "tool_calls": [],
            "tool_executions": [],
            "model_calls": [],
            "activities": [],
            "resource_refs": [],
            "active_state": None,
            "interrupt_state": None,
            "failure": None,
            "recovery": None,
            "resumable": True,
        }
        base.update(overrides)
        return base

    cases = {
        # 全零 state：snapshot_seq/current_attempt=0、resumable=True、全部 repeated 为空。
        "empty_zero": state(),
        # 仅 workspace_id 非空，其余可选字段缺席，验证 None 字段被两侧一致省略。
        "workspace_id_only": state(snapshot_seq=1, workspace_id="ws_1"),
        # 中断中：interrupt_state 与 active_state 必须两侧同形。
        "interrupting": state(
            snapshot_seq=2,
            stream_status="interrupting",
            active_state={
                "kind": "interrupting",
                "phase": "stopping",
                "entity_id": "ir_1",
                "status": "stopping",
                "last_kind": "activity",
                "last_phase": "completed",
                "reason": "user_requested",
            },
            interrupt_state={
                "request_id": "ir_1",
                "status": "requested",
                "reason": "user_requested",
            },
        ),
        # 失败终态：failure/recovery 与零值 resumable 必须显式返回。
        "failed": state(
            snapshot_seq=3,
            stream_status="failed",
            agent_loop_status="failed",
            resumable=False,
            failure={
                "code": "execution_lost",
                "message": "后端重启",
                "after_interrupt_requested": True,
                "resumable": False,
            },
            recovery={
                "status": "execution_lost",
                "code": "execution_lost",
                "message": "后端重启",
                "resumable": False,
            },
        ),
        # block 的 items 空数组、布尔零值和 seq 零值不得被 proto3 省略。
        "zero_valued_block": state(
            snapshot_seq=1,
            blocks=[
                {
                    "block_id": "b1",
                    "items": [],
                    "text": "",
                    "status": "running",
                    "redacted": False,
                    "partial": False,
                    "started_seq": 0,
                    "last_event_seq": 0,
                }
            ],
        ),
        # 运行中集合：四个实体集合同时非空。
        "running_collections": state(
            snapshot_seq=4,
            tool_calls=[
                {
                    "tool_call_id": "tc1",
                    "tool_name": "shell",
                    "arguments": {},
                    "arguments_complete": False,
                    "status": "running",
                }
            ],
            tool_executions=[
                {
                    "tool_execution_id": "e1",
                    "tool_call_id": "tc1",
                    "tool_name": "shell",
                    "status": "running",
                }
            ],
            model_calls=[{"model_call_id": "m1", "attempt": 0, "status": "running"}],
            activities=[
                {
                    "activity_id": "a1",
                    "kind": "tool",
                    "status": "running",
                    "resource_refs": [],
                }
            ],
        ),
        # 顶层 resource_refs 非空，验证 repeated message 逐键一致。
        "resource_refs": state(
            snapshot_seq=5,
            resource_refs=[
                {
                    "resource_id": "r1",
                    "lease_id": "l1",
                    "operation_id": "o1",
                    "status": "held",
                }
            ],
        ),
    }

    matrix_dump: dict[str, object] = {}
    for name, snapshot in cases.items():
        event = {
            "event_id": f"snapshot_{name}",
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_stream_id": turn_stream_id,
            "event_seq": int(snapshot["snapshot_seq"]),
            "type": "stream.snapshot",
            "payload": snapshot,
            **(
                {"workspace_id": snapshot["workspace_id"]}
                if snapshot["workspace_id"]
                else {}
            ),
        }
        sse_payload = _public_event_json(event)["payload"]
        http_payload = _public_snapshot(
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=turn_stream_id,
            snapshot=snapshot,
        ).model_dump(mode="json", exclude_none=True)
        assert _snapshot_core(http_payload) == sse_payload, name
        matrix_dump[name] = sse_payload

    # 零值与空 repeated 必须在 SSE 侧显式存在，而不是只有 HTTP 侧有。
    empty = matrix_dump["empty_zero"]
    assert isinstance(empty, dict)
    assert empty["snapshot_seq"] == 0
    assert empty["current_attempt"] == 0
    assert empty["resumable"] is True
    for field in _REPEATED_FIELDS:
        assert empty[field] == [], field
    block = matrix_dump["zero_valued_block"]
    assert isinstance(block, dict)
    assert [item.get("items") for item in block["blocks"]] == [[]]


@pytest.mark.asyncio
async def test_recovery_control_frame_matches_http_snapshot(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 store 触发游标丢失恢复帧，抓取真正发往 SSE 的帧原文并比对。"""
    api, store, session_id, turn_id = message_stream_api
    # 收紧保留上限：游标早于保留范围，订阅必须改推 stream.snapshot 控制帧。
    monkeypatch.setattr(store_module, "MESSAGE_STREAM_MAX_BYTES", 1_000)
    monkeypatch.setattr(store_module, "MESSAGE_STREAM_RETAINED_BYTES", 260)
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "model.started",
        {"model_call_id": "mc_1", "attempt": 1, "model": "primary"},
    )
    await writer.commit(
        "block.started",
        {"block_id": "b1", "block_index": 0, "carrier_type": "text"},
    )
    for index in range(12):
        await writer.commit(
            "block.delta",
            {
                "block_id": "b1",
                "carrier_type": "text",
                "operation": "append",
                "text": f"增量-{index}",
            },
            block_id="b1",
        )
    await writer.commit("activity.started", {"activity_id": "a1", "kind": "tool"})
    await writer.commit(
        "activity.completed",
        {"activity_id": "a1", "kind": "tool", "status": "completed", "outcome": "success"},
    )
    await writer.close_failed(code="execution_lost", message="后端重启", resumable=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        stream_response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream",
            headers=_headers("req_recovery_control"),
        )
        snapshot_response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
            headers=_headers("req_recovery_http"),
        )

    assert stream_response.status_code == 200
    assert snapshot_response.status_code == 200
    control = next(
        json.loads(line.removeprefix("data:"))
        for frame in stream_response.text.strip().split("\n\n")
        if "event: stream.snapshot" in frame
        for line in frame.splitlines()
        if line.startswith("data:")
    )
    sse_payload = control["payload"]
    assert isinstance(sse_payload, dict)
    # 真实恢复帧与 HTTP 快照逐键一致；定位字段只保留在事件信封中。
    assert _snapshot_core(snapshot_response.json()["data"]) == sse_payload
    assert all(field not in sse_payload for field in _IDENTITY_FIELDS)
    assert control["event_seq"] == sse_payload["snapshot_seq"]
    for field in _REPEATED_FIELDS:
        assert isinstance(sse_payload[field], list), field

