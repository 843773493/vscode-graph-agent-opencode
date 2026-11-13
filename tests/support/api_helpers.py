"""HTTP API 集成测试的共享确定性命中辅助函数。

多个 integration 文件都要经真实 HTTP 边界建会话并构造 LangGraph checkpoint
样板块；这两者与业务语义无关，收敛到唯一实现，避免逐文件复制漂移。
"""

from __future__ import annotations

import httpx
from langgraph.checkpoint.base import empty_checkpoint

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO


def new_message_operation_id() -> str:
    """为一个逻辑消息请求生成稳定身份。"""
    return create_prefixed_id("op")


def with_message_run_operation_id(payload: dict[str, object]) -> dict[str, object]:
    """给消息请求补上身份；重试时复用同一个 payload 会保留原身份。"""
    if "operation_id" not in payload:
        payload["operation_id"] = new_message_operation_id()
    return payload


def running_job_dispatch_snapshot(
    session_id: str,
    thread_id: str,
    job_id: str,
    *,
    queued_job_count: int,
    queue_snapshot_version: int,
) -> JobDispatchSnapshotDTO:
    """构造具有完整队列水位的运行中 JOB_STARTED 测试快照。"""
    return JobDispatchSnapshotDTO(
        session_id=session_id,
        thread_id=thread_id,
        job_id=job_id,
        job_status="running",
        active_job_id=job_id,
        blocked_by_job_id=None,
        queued_jobs_ahead=0,
        queued_job_count=queued_job_count,
        pending_job_count=queued_job_count + 1,
        delivery_policy=None,
        enqueue_sequence=None,
        queue_snapshot_version=queue_snapshot_version,
    )


async def create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    if response.status_code != 200:
        raise AssertionError(
            f"创建会话失败: status={response.status_code}, body={response.text}"
        )
    session_id = response.json()["data"]["session_id"]
    if not isinstance(session_id, str) or not session_id:
        raise AssertionError(f"创建会话成功响应缺少有效 session_id: {response.text}")
    return session_id


def checkpoint_fixture(
    checkpoint_id: str,
    messages: list[object],
    *,
    channel_version: int,
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        "messages": f"{channel_version:032d}.fixture"
    }
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


async def read_llm_request_logs(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    """审计测试显式读取摘要对应的全部正文，不改变产品的按需读取入口。"""
    response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    records = []
    for summary in response.json()["data"]:
        detail = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs/{summary['file_name']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["request_id"] == detail.headers["X-Request-ID"]
        records.append(detail.json()["data"])
    return records


__all__ = ["checkpoint_fixture", "create_session", "read_llm_request_logs"]
