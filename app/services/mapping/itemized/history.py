"""历史/展示投影的无 I/O入口。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage

from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.mapping.itemized.langchain import (
    project_canonical_items,
    project_context_plan,
)


def project_history_items(
    items: Sequence[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
) -> list[BaseMessage]:
    """历史只消费调用方给出的有序 canonical item，不访问 storage。"""
    return project_canonical_items(
        items,
        include_runtime_notices=include_runtime_notices,
        include_summaries=include_summaries,
        capability_losses=capability_losses,
        preserve_order=True,
    )


def project_history_plan(
    plan: ContextRequestPlan,
    items: Sequence[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
) -> list[BaseMessage]:
    """消费与 LangChain/provider 相同的 sealed selection 生成历史消息。

    history 不发送 request-only 正文，也不把 ToolSetRef 变成 message；它只
    materialize plan 中已经选择的 canonical item。selection 校验和 item
    顺序仍由 ``project_context_plan`` 统一执行，避免 history 自己重新排序。
    """
    return project_context_plan(
        plan,
        items,
        include_runtime_notices=include_runtime_notices,
        include_summaries=include_summaries,
        capability_losses=capability_losses,
        include_request_only=False,
    )


__all__ = ["project_history_items", "project_history_plan"]
