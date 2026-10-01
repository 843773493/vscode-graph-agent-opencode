"""把压缩摘要 handler 的 ModelResponse 解释为摘要正文。

单点拒绝伪装的失败结果与工具调用请求；摘要正文一律经 validate_summary_text。"""

from __future__ import annotations

from langchain.agents.middleware import ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage

from app.agents.summarization.retry import validate_summary_text
from app.agents.summarization.state import SummaryToolCallError


def _summary_response_text(response: ModelResponse | ExtendedModelResponse) -> str:
    model_response = (
        response.model_response
        if isinstance(response, ExtendedModelResponse)
        else response
    )
    if not isinstance(model_response, ModelResponse) or not model_response.result:
        raise TypeError("压缩摘要 handler 必须返回包含消息的 ModelResponse")
    message = model_response.result[-1]
    if not isinstance(message, AIMessage):
        raise TypeError(
            f"压缩摘要响应必须是 AIMessage，实际类型: {type(message).__name__}"
        )
    if message.tool_calls or message.invalid_tool_calls:
        names = [call.get("name", "<unknown>") for call in message.tool_calls]
        raise SummaryToolCallError(
            f"压缩摘要模型不得调用工具，实际请求: {names or ['<invalid>']}"
        )
    return validate_summary_text(message.text)
