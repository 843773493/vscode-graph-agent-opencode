"""产品执行生成历史后，真实Gateway消息流错误保留身份及原件。"""

from __future__ import annotations

import json

import pytest

from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 — 只复用产品进程和model_stream装具
)
from tests.support.api_helpers import create_session


@pytest.mark.asyncio
async def test_product_stream_reads_reject_corruption_then_reopen_original_history(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    histories = []
    for artifact in ("events", "state", "index", "applied_event_ids"):
        session = await create_session(case.client, f"消息流读取-{artifact}")
        await case.plan(session, [{"content": f"stream-original-{artifact}"}])
        sent = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": f"stream-trigger-{artifact}"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert sent.status_code == 200, sent.text
        accepted = sent.json()["data"]
        await _completed(case.client, accepted["job_id"])
        base = f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream"
        events = await case.client.get(base + "/events")
        assert events.status_code == 200, events.text
        values = events.json()["data"]
        assert values[-1]["type"] == "stream.completed"
        stream_id = values[0]["turn_stream_id"]
        node = await case.session_node(session)
        canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
        assert canonical.is_file()
        histories.append((artifact, session, stream_id, base, node, canonical, canonical.read_bytes(), values))
    # 原历史和恢复缓存都由产品入口产生；终态恢复后的读取仍须核对磁盘。
    await case.hard_restart()
    receipts = []
    for artifact, session, stream_id, base, node, canonical, canonical_bytes, expected in histories:
        files = node / "message_streams"
        paths = {
            "events": files / f"{stream_id}.jsonl",
            "state": files / f"{stream_id}.state.json",
            "index": files / "index.json",
            "applied_event_ids": files / f"{stream_id}.applied-event-ids.json",
        }
        target = paths[artifact]
        original = target.read_bytes() if target.exists() else None
        marker = str(case.workspace / f"private-{artifact}-marker")
        if artifact == "events":
            lines = original.splitlines(keepends=True)
            raw = json.loads(lines[0])
            raw["event"]["session_id"] = marker
            damaged = (json.dumps(raw) + "\n").encode() + b"".join(lines[1:])
        elif artifact == "state":
            raw = json.loads(original)
            raw["thread_id"] = marker
            damaged = json.dumps(raw).encode()
        elif artifact == "index":
            raw = json.loads(original)
            raw["format_version"] = marker
            damaged = json.dumps(raw).encode()
        else:
            damaged = json.dumps({marker: marker}).encode()
        target.write_bytes(damaged)
        errors = []
        try:
            for suffix in ("/snapshot", "/events", ""):
                failed = await case.client.get(base + suffix)
                assert failed.status_code == 404, failed.text
                assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
                assert f"session_id={session}" in failed.text and f"artifact={artifact}" in failed.text
                assert marker not in failed.text and str(case.workspace) not in failed.text
                assert target.read_bytes() == damaged
                assert canonical.read_bytes() == canonical_bytes
                errors.append(failed.json())
        finally:
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)
        reopened = await case.client.get(base + "/events")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["data"] == expected
        assert canonical.read_bytes() == canonical_bytes
        receipts.append({"artifact": artifact, "session_id": session, "turn_stream_id": stream_id, "errors": errors})
    await case.hard_restart()
    for _, _, _, base, _, canonical, canonical_bytes, expected in histories:
        response = await case.client.get(base + "/events")
        assert response.status_code == 200, response.text
        assert response.json()["data"] == expected
        assert canonical.read_bytes() == canonical_bytes
    case.record("stream-read-errors", receipts)
