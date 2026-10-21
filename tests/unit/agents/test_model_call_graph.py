"""框架 model node 的闭包适配与同步/异步消息合并合同。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.model_call_graph import create_model_call_agent


@dataclass
class FrameworkExecution:
    """只隔离封存业务，直接使用适配器绑定的真实模型与输出解析器。"""
    calls: list[tuple[str, ModelRequest]] = field(default_factory=list)

    def bind_model(self, *, bind_model, decode_output, name, structured_tools):
        self.bind = bind_model
        self.decode = decode_output
        assert structured_tools == {}

    def execute(self, request):
        self.calls.append(("sync", request))
        bound, response_format = self.bind(request)
        output = bound.invoke(request.messages)
        return ModelResponse(result=self.decode(output, response_format)["messages"])

    async def aexecute(self, request):
        self.calls.append(("async", request))
        bound, response_format = self.bind(request)
        output = await bound.ainvoke(request.messages)
        return ModelResponse(result=self.decode(output, response_format)["messages"])


@pytest.fixture
def model_graph():
    executor = FrameworkExecution()
    model = FakeMessagesListChatModel(responses=[AIMessage(content="框架响应", id="assistant")])
    return create_model_call_agent(model, execution=executor), executor


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_adapter_executes_and_merges_messages(model_graph, asynchronous):
    graph, executor = model_graph
    user = HumanMessage(content="框架输入", id="user")
    inputs = {"messages": [user]}
    result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)
    assert [(message.id, message.content) for message in result["messages"]] == [
        ("user", "框架输入"), ("assistant", "框架响应"),
    ]
    assert [kind for kind, request in executor.calls] == ["async" if asynchronous else "sync"]
    assert executor.calls[0][1].messages == [user]
