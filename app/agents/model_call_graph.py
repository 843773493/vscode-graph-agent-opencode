"""集中适配 LangChain 图：请求生产者之后直接进入唯一执行 owner。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from inspect import getclosurevars
from typing import Any

from langchain.agents import create_agent
from langchain.agents.factory import _build_commands
from langchain.agents.middleware import ModelRequest
from langchain_core.tools import BaseTool
from langgraph._internal._runnable import RunnableCallable
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.agents.model_call_execution import ModelCallExecution


def create_model_call_agent(model, *, execution: ModelCallExecution, **options) -> CompiledStateGraph:
    """保留框架图、输出解析及请求链，只替换 terminal model execution。"""
    graph = create_agent(model, **options)
    node = graph.builder.nodes["model"]
    runnable = node.runnable
    if not isinstance(runnable, RunnableCallable) or not callable(runnable.func) or not callable(runnable.afunc):
        raise TypeError("model-call graph 缺少同步/异步 model node")
    sync = getclosurevars(runnable.func).nonlocals
    asynchronous = getclosurevars(runnable.afunc).nonlocals
    terminal = getclosurevars(sync["_execute_model_sync"]).nonlocals
    binder = terminal["_get_bound_model"]
    registry = getclosurevars(binder).nonlocals["structured_output_tools"]
    if not callable(binder) or not isinstance(registry, Mapping):
        raise TypeError("model-call graph 缺少框架绑定/结构化输出注册端口")
    tools = {name: binding.tool for name, binding in registry.items()}
    if not all(isinstance(tool, BaseTool) for tool in tools.values()):
        raise TypeError("model-call graph 的结构化工具注册表非法")
    execution.bind_model(
        bind_model=binder, decode_output=terminal["_handle_model_output"],
        name=terminal["name"], structured_tools=tools,
    )

    def request_for(state, runtime):
        return ModelRequest(
            model=sync["model"], tools=sync["default_tools"],
            system_message=sync["system_message"],
            response_format=sync["initial_response_format"], messages=state["messages"],
            tool_choice=None, state=state, runtime=runtime,
        )

    def model_node(state, runtime: Runtime[Any]):
        request = request_for(state, runtime)
        chain = sync["wrap_model_call_handler"]
        if chain is None:
            return _build_commands(execution.execute(request))
        result = chain(request, execution.execute)
        return _build_commands(result.model_response, result.commands)

    async def amodel_node(state, runtime: Runtime[Any]):
        request = request_for(state, runtime)
        chain = asynchronous["awrap_model_call_handler"]
        if chain is None:
            return _build_commands(await execution.aexecute(request))
        result = await chain(request, execution.aexecute)
        return _build_commands(result.model_response, result.commands)

    graph.builder.nodes["model"] = replace(
        node, runnable=RunnableCallable(model_node, amodel_node, trace=False),
    )
    # 重新编译保留所有条件边、interrupt、transformer、checkpoint 与配置；
    # 不改框架闭包、类原型或已编译节点的 writer 链。
    return graph.builder.compile(
        checkpointer=graph.checkpointer, store=graph.store, cache=graph.cache,
        interrupt_before="*" if graph.interrupt_before_nodes == "*" else list(graph.interrupt_before_nodes),
        interrupt_after="*" if graph.interrupt_after_nodes == "*" else list(graph.interrupt_after_nodes), debug=graph.debug,
        name=graph.name, transformers=graph.stream_transformers,
    ).with_config(graph.config)
