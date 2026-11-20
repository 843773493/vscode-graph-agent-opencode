from __future__ import annotations

from collections.abc import Mapping

import pytest
from langchain_core.messages import HumanMessage

from app.prompting import PromptSection, internal_message_factory


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

