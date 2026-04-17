from __future__ import annotations
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
import os
from deepagents import create_deep_agent, FilesystemMiddleware
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import ModelFallbackMiddleware
from langgraph.types import Overwrite
import asyncio


from collections.abc import Callable
from typing import Any
import json
import textwrap


import dotenv
dotenv.load_dotenv()  # 从 .env 文件加载环境变量

from collections.abc import Callable
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import AIMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver

class TraceMiddleware(AgentMiddleware):
    def _line(self, char: str = "─", n: int = 88):
        print(char * n)

    def _short(self, value: Any, width: int = 120) -> str:
        if value is None:
            return "None"

        if isinstance(value, str):
            text = value.strip().replace("\n", " ")
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)

        if len(text) > width:
            return text[: width - 3] + "..."
        return text

    def _pretty_block(self, value: Any, max_lines: int = 8, width: int = 100) -> str:
        if value is None:
            return "None"

        if not isinstance(value, str):
            try:
                value = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            except Exception:
                value = str(value)

        lines = []
        for raw_line in str(value).splitlines():
            wrapped = textwrap.wrap(raw_line, width=width) or [""]
            lines.extend(wrapped)

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append("...")

        return "\n".join(lines)

    def _section(self, title: str, subtitle: str | None = None):
        print()
        self._line("═")
        print(f"■ {title}")
        if subtitle:
            print(f"  {subtitle}")
        self._line("─")

    def _field(self, key: str, value: Any, multiline: bool = False):
        if multiline:
            print(f"• {key}")
            block = self._pretty_block(value)
            for line in block.splitlines():
                print(f"    {line}")
        else:
            print(f"• {key}: {self._short(value)}")

    def before_agent(self, state: dict[str, Any], runtime):
        self._section("Agent 开始", "收到用户输入")
        msgs = state.get("messages", [])
        self._field("消息数", len(msgs))
        if msgs:
            last = msgs[-1]
            content = getattr(last, "content", last)
            self._field("最后一条消息", content, multiline=True)
        return None

    def before_model(self, state: dict[str, Any], runtime):
        self._section("Model 调用前", "准备请求模型")
        msgs = state.get("messages", [])
        self._field("当前消息数", len(msgs))
        if msgs:
            last = msgs[-1]
            self._field("最后一条消息", getattr(last, "content", last), multiline=True)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        self._section("AI", "model_call")
        self._field("model", getattr(request.model, "model_name", str(request.model)))
        self._field("messages", len(request.messages))

        for i, msg in enumerate(request.messages[-3:], 1):
            self._field(f"input[{i}]", getattr(msg, "content", msg), multiline=True)

        response = handler(request)

        self._section("AI 返回", "model_response")
        try:
            for i, msg in enumerate(response.result, 1):
                self._field(f"response[{i}]", getattr(msg, "content", msg), multiline=True)
        except Exception:
            self._field("raw_response", response, multiline=True)

        return response

    def after_model(self, state: dict[str, Any], runtime):
        self._section("Model 调用后", "模型结果已写回 state")
        msgs = state.get("messages", [])
        if msgs:
            last = msgs[-1]
            self._field("最新消息类型", type(last).__name__)
            self._field("最新消息内容", getattr(last, "content", last), multiline=True)
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown_tool")
        tool_args = tool_call.get("args", {})

        self._section("Tool", tool_name)
        self._field("args", tool_args, multiline=True)

        result = handler(request)

        self._section("Tool 返回", tool_name)
        if isinstance(result, ToolMessage):
            self._field("content", result.content, multiline=True)
        else:
            self._field("result", result, multiline=True)

        return result

    def after_agent(self, state: dict[str, Any], runtime):
        self._section("Agent 结束", "最终输出")
        msgs = state.get("messages", [])
        self._field("总消息数", len(msgs))
        if msgs:
            last = msgs[-1]
            self._field("最终消息类型", type(last).__name__)
            self._field("最终消息内容", getattr(last, "content", last), multiline=True)
        return None
    
model = ChatOpenAI(
    model="bytedance-seed/dola-seed-2.0-pro:free",
    api_key=os.getenv("KILO_API_KEY"),
    base_url="https://api.kilo.ai/api/gateway",
    use_responses_api=False,
    max_retries=3,
)
fallback_model_1 = ChatOpenAI(
    model="bytedance-seed/dola-seed-2.0-pro:free",
    api_key=os.getenv("KILO_API_KEY"),
    base_url="https://api.kilo.ai/api/gateway",
    use_responses_api=False,
    max_retries=3,
)

backend = FilesystemBackend(
    root_dir=r"out",
    virtual_mode=True,
)

checkpointer = MemorySaver()

agent = create_deep_agent(
    model=model,
    backend=backend,
    system_prompt="You are a research assistant.",
    middleware=[
        TraceMiddleware(),
        ModelFallbackMiddleware(fallback_model_1),
    ],
    checkpointer=checkpointer,   # 关键
)

config = {
    "configurable": {
        "thread_id": "user_001"  # 同一个用户/会话固定一个 thread_id
    }
}

# 第一轮
result1 = agent.invoke(
    {"messages": [{"role": "user", "content": "你的知识截至日期是多少"}]},
    config=config
)
print(result1["messages"][-1].content)

# 第二轮：继续追问
result2 = agent.invoke(
    {"messages": [{"role": "user", "content": "我刚才的问题是什么"}]},
    config=config
)
print(result2["messages"][-1].content)




