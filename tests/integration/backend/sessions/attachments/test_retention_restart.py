"""真实Gateway/Backend执行与硬重启证明29天重关联、重新起算和精确到期。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from app.core.atomic_fs import native_io_path
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session

BODY = b"retention-restart-original"
DAY_SECONDS = 24 * 60 * 60


def directory_snapshot(case: CommunicationCase) -> dict[str, object]:
    database = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row, = connection.execute(
            "SELECT d.attachment_id,d.relative_locator,d.created_at,d.unattached_since,d.gc_after,d.availability,"
            "a.binding_token,a.state AS association_state "
            "FROM attachment_directories d JOIN attachment_directory_associations a USING(attachment_id)",
        ).fetchall()
        return dict(row)


async def test_day29_reuse_and_restart(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    session = await create_session(case.client, "附件重关联与真实重启期限")
    await case.plan(session, [{"content": "首轮附件执行完成"}])
    response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "保存本次附件", "attachments": [upload(BODY).model_dump(mode="json")]},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    history = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
    assert history.status_code == 200, history.text
    original, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    active = directory_snapshot(case)
    assert active["association_state"] == "active" and active["gc_after"] is None
    assert str(active["binding_token"]).startswith("runtime:")
    directory = case.workspace / ".boxteam/attachments"
    directory = native_io_path(directory.joinpath(*PurePosixPath(str(active["relative_locator"])).parts))
    assert (directory / "original/content").read_bytes() == BODY
    canonical = native_io_path(case.main_thread_node(session) / "rollout/rollout.jsonl")
    first_history = canonical.read_bytes()

    # SIGKILL不发送detach，启动恢复由原owner提交零关联及期限。
    await case.hard_restart()
    released = directory_snapshot(case)
    assert released["binding_token"] == active["binding_token"]
    assert released["association_state"] == "released"
    first_start = datetime.fromisoformat(str(released["unattached_since"]))
    first_deadline = datetime.fromisoformat(str(released["gc_after"]))
    assert first_deadline == first_start + timedelta(days=30)
    assert canonical.read_bytes() == first_history
    case.replay.control.advance_wait(29 * DAY_SECONDS)
    await case.hard_restart()
    assert directory_snapshot(case) == released
    assert (directory / "original/content").read_bytes() == BODY

    call, = await case.plan(session, [{"content": "再次使用相同附件完成"}])
    case.replay.control.arm(call, "model.response_ready")
    try:
        response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "第29天再次使用", "attachments": [original]},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        await asyncio.to_thread(case.replay.control.acknowledged, call, "model.response_ready", 60)
        rebound = directory_snapshot(case)
        assert rebound["attachment_id"] == active["attachment_id"]
        assert rebound["relative_locator"] == active["relative_locator"]
        assert rebound["binding_token"] != active["binding_token"]
        assert rebound["association_state"] == "active"
        assert rebound["unattached_since"] is rebound["gc_after"] is None
        available = await case.client.get("/api/v1/workspace/files/content", params={"path": original["file_id"]})
        assert available.status_code == 200 and available.json()["data"]["content"] == BODY.decode(), available.text
    finally:
        case.replay.control.release(call, "model.response_ready")
    await _completed(case.client, response.json()["data"]["job_id"])
    second_history = canonical.read_bytes()
    assert second_history.startswith(first_history) and len(second_history) > len(first_history)
    await case.hard_restart()
    second_release = directory_snapshot(case)
    assert second_release["association_state"] == "released"
    assert second_release["binding_token"] == rebound["binding_token"]
    second_start = datetime.fromisoformat(str(second_release["unattached_since"]))
    second_deadline = datetime.fromisoformat(str(second_release["gc_after"]))
    assert second_start == first_start + timedelta(days=29)
    assert second_deadline == first_deadline + timedelta(days=29)
    assert second_deadline == second_start + timedelta(days=30)
    assert canonical.read_bytes() == second_history

    case.replay.control.advance_wait(29 * DAY_SECONDS)
    await case.hard_restart()
    assert directory_snapshot(case) == second_release
    assert (directory / "original/content").read_bytes() == BODY
    case.replay.control.advance_wait(DAY_SECONDS)
    await case.hard_restart()
    expired = directory_snapshot(case)
    assert expired["availability"] == "tombstoned" and not directory.exists()
    assert (expired["unattached_since"], expired["gc_after"]) == (second_release["unattached_since"], second_release["gc_after"])
    assert (expired["attachment_id"], expired["relative_locator"]) == (active["attachment_id"], active["relative_locator"])
    unavailable = await case.client.get("/api/v1/workspace/files/content", params={"path": original["file_id"]})
    assert unavailable.status_code == 404, unavailable.text
    assert canonical.read_bytes() == second_history
    # 再次重启不复活目录、不重复删除，也不改变canonical或已提交期限。
    await case.hard_restart()
    assert directory_snapshot(case) == expired and not directory.exists()
    assert canonical.read_bytes() == second_history
    case.record("retention-restart", {
        "initial": active, "first_release": released, "rebound": rebound,
        "second_release": second_release, "expired": expired,
        "scope": "真实Gateway/Backend/model_stream、正式消息执行、外置UTC时钟和原生硬退出恢复；未直接写入关联/期限/队列/canonical，只证明当前运行平台，不代表全部生命周期故障组合",
    })
