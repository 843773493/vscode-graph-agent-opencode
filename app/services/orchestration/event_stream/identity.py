"""绑定和校验一条 Agent 事件流的 session/job 身份。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STREAM_SESSION_ID_METADATA_KEY = "boxteam_session_id"
STREAM_JOB_ID_METADATA_KEY = "boxteam_job_id"


def event_run_id(event: dict[str, Any]) -> str:
    run_id = event.get("run_id")
    return run_id if isinstance(run_id, str) else ""


def build_isolated_stream_config(
    config: dict[str, Any],
    *,
    session_id: str,
    job_id: str,
) -> dict[str, Any]:
    """构造独立 Agent 根事件流配置，阻止继承调用方的 LangChain callbacks。"""
    stream_config = dict(config)
    if stream_config.get("callbacks") is None:
        stream_config["callbacks"] = []

    raw_metadata = stream_config.get("metadata")
    if raw_metadata is None:
        metadata: dict[str, Any] = {}
    elif isinstance(raw_metadata, Mapping):
        metadata = dict(raw_metadata)
    else:
        raise TypeError(
            f"Agent 事件流 config.metadata 必须是 mapping，实际类型: {type(raw_metadata).__name__}"
        )

    expected_identity = {
        STREAM_SESSION_ID_METADATA_KEY: session_id,
        STREAM_JOB_ID_METADATA_KEY: job_id,
    }
    for key, expected_value in expected_identity.items():
        existing_value = metadata.get(key)
        if existing_value is not None and existing_value != expected_value:
            raise RuntimeError(
                "Agent 事件流根配置身份冲突: "
                f"{key}={existing_value!r} expected={expected_value!r}"
            )
        metadata[key] = expected_value
    stream_config["metadata"] = metadata
    return stream_config


def validate_stream_event_identity(
    metadata: object,
    *,
    session_id: str,
    job_id: str,
    event_type: str,
    name: str,
) -> None:
    if not isinstance(metadata, Mapping):
        raise TypeError(
            f"LangChain 事件 metadata 必须是 mapping，实际类型: {type(metadata).__name__}"
        )
    event_session_id = metadata.get(STREAM_SESSION_ID_METADATA_KEY)
    event_job_id = metadata.get(STREAM_JOB_ID_METADATA_KEY)
    if event_session_id != session_id or event_job_id != job_id:
        raise RuntimeError(
            "检测到跨 Agent job 的 LangChain 事件串入: "
            f"event={event_type} name={name!r} "
            f"expected_session_id={session_id!r} actual_session_id={event_session_id!r} "
            f"expected_job_id={job_id!r} actual_job_id={event_job_id!r}"
        )
