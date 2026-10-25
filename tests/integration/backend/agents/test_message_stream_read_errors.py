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


@pytest.mark.asyncio
async def test_product_stream_rejects_foreign_applied_event_then_recovers(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "消息流幂等归属")
    await case.plan(session, [{"content": "applied-owner-original"}])
    sent = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "applied-owner-trigger"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert sent.status_code == 200, sent.text
    accepted = sent.json()["data"]
    await _completed(case.client, accepted["job_id"])
    base = f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream"
    events = await case.client.get(base + "/events")
    assert events.status_code == 200, events.text
    expected = events.json()["data"]
    assert expected[-1]["type"] == "stream.completed"
    stream_id = expected[0]["turn_stream_id"]
    canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
    canonical_bytes = canonical.read_bytes()
    node = await case.session_node(session)
    sidecar = node / "message_streams" / f"{stream_id}.applied-event-ids.json"
    await case.hard_restart()
    original = sidecar.read_bytes() if sidecar.exists() else None
    marker = str(case.workspace / "foreign-applied-owner")
    # 只注入一条损坏的派生幂等记录；原事件、执行和canonical由产品生成。
    value = {**expected[0], "thread_id": marker}
    damaged = json.dumps({value["event_id"]: value}).encode()
    sidecar.write_bytes(damaged)
    errors = []
    try:
        for suffix in ("/snapshot", "/events", ""):
            failed = await case.client.get(base + suffix)
            assert failed.status_code == 404, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
            assert "artifact=applied_event_ids" in failed.text and f"session_id={session}" in failed.text
            assert marker not in failed.text and str(case.workspace) not in failed.text
            assert sidecar.read_bytes() == damaged and canonical.read_bytes() == canonical_bytes
            errors.append(failed.json())
    finally:
        if original is None:
            sidecar.unlink()
        else:
            sidecar.write_bytes(original)
    for restart in (False, True):
        if restart:
            await case.hard_restart()
        response = await case.client.get(base + "/events")
        assert response.status_code == 200, response.text
        assert response.json()["data"] == expected
        assert canonical.read_bytes() == canonical_bytes
    case.record("applied-event-owner-errors", errors)


@pytest.mark.asyncio
async def test_product_startup_rejects_corrupt_recovery_then_reopens_history(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "启动恢复统一校验")
    await case.plan(session, [{"content": "startup-recovery-original"}])
    sent = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "startup-recovery-trigger"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert sent.status_code == 200, sent.text
    accepted = sent.json()["data"]
    await _completed(case.client, accepted["job_id"])
    base = f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream"
    events = await case.client.get(base + "/events")
    assert events.status_code == 200, events.text
    expected = events.json()["data"]
    assert expected[-1]["type"] == "stream.completed"
    stream_id = expected[0]["turn_stream_id"]
    canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
    canonical_bytes = canonical.read_bytes()
    node = await case.session_node(session)
    state_path = node / "message_streams" / f"{stream_id}.state.json"
    applied_path = node / "message_streams" / f"{stream_id}.applied-event-ids.json"
    stderr = case.workspace / ".boxteam/logs/communication-restarted.stderr.log"
    receipts = []
    for fault in ("state_identity", "state_sequence", "applied_owner"):
        target = applied_path if fault == "applied_owner" else state_path
        original = target.read_bytes() if target.exists() else None
        marker = str(case.workspace / "startup-foreign-owner")
        if fault == "applied_owner":
            value = {**expected[0], "thread_id": marker}
            raw = {value["event_id"]: value}
            reason = "消息流事件关联键不匹配"
        else:
            raw = json.loads(original)
            if fault == "state_identity":
                raw["thread_id"] = marker
                reason = "消息流状态快照关联键不匹配"
            else:
                raw["snapshot_seq"] = expected[-1]["event_seq"] + 1
                reason = "消息流状态快照序号非法"
        damaged = json.dumps(raw).encode()
        target.write_bytes(damaged)
        log_before = stderr.stat().st_size if stderr.exists() else 0
        try:
            with pytest.raises(RuntimeError) as caught:
                await case.hard_restart()
            diagnostic = stderr.read_bytes()[log_before:].decode("utf-8")
            assert reason in diagnostic, diagnostic
            assert "artifact=" in diagnostic and f"session_id={session}" in diagnostic
            assert "Application startup failed" in diagnostic
            assert target.read_bytes() == damaged and canonical.read_bytes() == canonical_bytes
            receipts.append({"fault": fault, "error_type": type(caught.value).__name__, "diagnostic": diagnostic})
        finally:
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)
        await case.hard_restart()
        response = await case.client.get(base + "/events")
        assert response.status_code == 200, response.text
        assert response.json()["data"] == expected
        assert canonical.read_bytes() == canonical_bytes
    case.record("startup-recovery-errors", receipts)


@pytest.mark.asyncio
async def test_product_stream_locator_rejects_paths_and_empty_values_without_retargeting(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "消息流标识定位")
    await case.plan(session, [{"content": "stream-locator-original"}])
    sent = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "stream-locator-trigger"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert sent.status_code == 200, sent.text
    accepted = sent.json()["data"]
    await _completed(case.client, accepted["job_id"])
    base = f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream"
    response = await case.client.get(base + "/events")
    assert response.status_code == 200, response.text
    expected = response.json()["data"]
    assert expected[-1]["type"] == "stream.completed"
    thread = expected[0]["thread_id"]
    node = await case.session_node(session)
    index = node / "message_streams/index.json"
    original_index = index.read_bytes()
    canonical = case.main_thread_node(session) / "rollout/rollout.jsonl"
    canonical_bytes = canonical.read_bytes()
    foreign = case.workspace / "foreign-stream.jsonl"
    foreign.write_bytes(b"[]\n")
    values = [str(foreign.with_suffix("")), "../foreign-stream", r"C:\private\foreign-stream",
              r"\\host\share\foreign-stream", ""]
    receipts = []
    for source in ("request", "index"):
        for value in values:
            if source == "index":
                raw = json.loads(original_index)
                raw["threads"][thread][accepted["turn_id"]] = value
                index.write_text(json.dumps(raw), encoding="utf-8")
            damaged_index = index.read_bytes()
            try:
                for suffix in ("/snapshot", "/events", ""):
                    params = {"turn_stream_id": value} if source == "request" else {}
                    failed = await case.client.get(base + suffix, params=params)
                    assert failed.status_code == 404, failed.text
                    assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
                    assert "field=turn_stream_id" in failed.text and f"session_id={session}" in failed.text
                    if value:
                        assert value not in failed.text and json.dumps(value)[1:-1] not in failed.text
                    assert str(case.workspace) not in failed.text
                    assert index.read_bytes() == damaged_index
                    assert canonical.read_bytes() == canonical_bytes
                    receipts.append({"source": source, "response": failed.json()})
            finally:
                index.write_bytes(original_index)
    assert foreign.read_bytes() == b"[]\n"
    for restart in (False, True):
        if restart:
            await case.hard_restart()
        restored = await case.client.get(base + "/events")
        assert restored.status_code == 200, restored.text
        assert restored.json()["data"] == expected
        assert canonical.read_bytes() == canonical_bytes
    case.record("stream-locator-errors", receipts)
