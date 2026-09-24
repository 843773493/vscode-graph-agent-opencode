"""构造绑定权威 main thread 解析器的 MessageService 测试替身。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.path_utils import get_session_path_resolver
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


__all__ = ["build_message_service"]
