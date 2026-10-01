from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef


@dataclass
class JobRuntimeState:
    job_id: str
    session_id: str
    message: str
    agent_id: str
    message_id: str
    message_created_at: str
    message_metadata: dict[str, object] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)
    # 请求级注入的真实 gateway_id（``X-BoxTeam-Gateway-Id``）：job 是独立执行根，
    # MUST 显式携带，MUST NOT 依赖请求 ContextVar 或进程级「当前 gateway」单例。
    gateway_id: str | None = None
    # 请求级注入的权威 request_id（``X-Request-ID``）：job 是独立执行根，
    # MUST 显式携带创建请求的 request_id，MUST NOT 在 job 内部补造第二个。
    request_id: str | None = None
    status: JobStatus = JobStatus.queued
    progress: int = 0
    current_step: str | None = None
    error_message: str | None = None
    result: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    task: asyncio.Task | None = None
    progress_reporter: Callable[[str], None] | None = None
