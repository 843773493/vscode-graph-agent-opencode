"""构造绑定权威 main thread 解析器的 MessageService 测试替身。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.message_service import MessageService


def build_message_service(
    sessions_dir: Path,
    **kwargs: Any,
) -> MessageService:
    """用同一 sessions 根的 catalog 解析器构造 MessageService。

    产品容器装配总是注入权威 main pointer 解析器；测试也必须走同一口径，
    否则普通 Session 入口会退回 session_id 冒充 thread_id。
    """
    resolver = get_session_path_resolver(sessions_dir)
    return MessageService(
        main_thread_resolver=resolver.main_thread_id,
        **kwargs,
    )


def accept_queued_message(service: MessageService, message: MessageDTO) -> None:
    """通过真实接受入口冻结现有消息与排队输入，不伪造执行状态。"""
    identity = UserExecutionIdentity(
        job_id=create_prefixed_id("job"), turn_id=create_prefixed_id("turn"),
        admission_idempotency_key=create_prefixed_id("adm"),
    )
    service.accept_user_message(message, execution_identity=identity, queue_input=ExecutionQueueInput(
        execution_binding=identity, message_id=message.message_id, agent_id="default", provider_id=None,
        delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=message.created_at.isoformat(),
    ))


__all__ = ["accept_queued_message", "build_message_service"]
