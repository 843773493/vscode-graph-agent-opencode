from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import HumanMessage

from app.prompting import PromptSection, internal_message_factory

if TYPE_CHECKING:
    from app.core.session_catalog_resolver import SessionCatalogPathResolver
    from app.services.infrastructure.message_stream_store import MessageStreamWriter
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )


def compaction_summary_message(
    *, content: str, id: str, response_metadata: Mapping[str, object] | None = None,
    additional_kwargs: dict[str, object] | None = None,
) -> HumanMessage:
    """存储合同测试的摘要输入；完整执行验收仍由产品压缩入口生成。"""
    prepared = internal_message_factory.build(
        kind="compaction_summary", control="以下是历史对话摘要，仅作为上下文使用。",
        sections=(PromptSection("summary", content),), metadata=response_metadata,
    )
    return HumanMessage(
        content=prepared.content, id=id,
        response_metadata={**prepared.metadata, "compaction_summary": True},
        additional_kwargs=additional_kwargs or {},
    )


def last_assistant_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = message.get("content", "")
            if content:
                return content
    pytest.fail("未找到非空 assistant 消息")



def admit_message_stream_writer(writer: MessageStreamWriter, resolver: SessionCatalogPathResolver) -> RolloutCheckpointSaver:
    """消息流孤立测试显式创建真实 rollout admission，恢复不再自行捏造终态。"""
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )

    if writer.job_id is None:
        raise ValueError("测试消息流必须显式提供 job_id")
    saver = RolloutCheckpointSaver(resolver.sessions_root)
    saver.accept_turn(
        writer.session_id, thread_id=writer.thread_id, turn_id=writer.turn_id,
        accepted_ingress_id=f"input:{writer.turn_id}", acceptance_idempotency_key=f"accept:{writer.turn_id}",
        job_id=writer.job_id, admission_idempotency_key=f"admit:{writer.turn_id}",
        payload="消息流恢复测试", initial_execution_id=f"execution:{writer.turn_id}",
    )
    return saver
