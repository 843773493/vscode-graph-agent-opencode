"""集中适配 LangChain 图：请求生产者之后直接进入唯一执行 owner。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import replace
from datetime import datetime
from functools import partial
from inspect import getclosurevars
from typing import Annotated, Any

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from langchain.agents import create_agent
from langchain.agents.factory import _build_commands
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import AnyMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from langgraph._internal._runnable import RunnableCallable
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.agents.model_call_execution import (
    DerivedModelRequest,
    DerivedModelResult,
    ModelCallExecution,
)
from app.agents.providers.wire_finalize import chat_wire_profile
from app.agents.tool_node_adapter import (
    DerivedToolPolicyMiddleware,
    adapt_agent_tool_node,
)
from app.core.execution_pause import bind_execution_pause, execution_pause_boundary
from app.core.failures import is_provider_failure, public_error
from app.core.job_event_bus import EVENT_FACTORY_REGISTRY, EventType
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.shielded_cleanup import run_sync_shielded
from app.core.turn_execution_scope import (
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.context_compaction import ContextCapacityError
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
)


class IncrementalAgentState(AgentState):
    messages: Annotated[list[AnyMessage], MessagesDeltaChannel()]


def create_model_call_agent(
    model, *, execution: ModelCallExecution,
    model_call: Runnable[ModelRequest, Any] | None = None,
    **options,
) -> CompiledStateGraph:
    """保留框架图、输出解析及请求链，只替换 terminal model execution。"""
    graph = create_agent(model, state_schema=IncrementalAgentState, **options)
    if "tools" in graph.builder.nodes:
        tools_node = graph.builder.nodes["tools"]
        if not isinstance(tools_node.runnable, ToolNode):
            raise TypeError("model-call graph 缺少框架 ToolNode")
        graph.builder.nodes["tools"] = replace(
            tools_node, runnable=adapt_agent_tool_node(tools_node.runnable),
        )
    node = graph.builder.nodes["model"]
    runnable = node.runnable
    if not isinstance(runnable, RunnableCallable) or not callable(runnable.func) or not callable(runnable.afunc):
        raise TypeError("model-call graph 缺少同步/异步 model node")
    sync = getclosurevars(runnable.func).nonlocals
    asynchronous = getclosurevars(runnable.afunc).nonlocals
    if model_call is None:
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
        model_call = RunnableLambda(execution.execute, afunc=execution.aexecute)

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
        with execution.graph_request_scope() as adopted:
            if chain is None:
                response, extra = model_call.invoke(request), None
            else:
                result = chain(request, model_call.invoke)
                response, extra = result.model_response, result.commands
            return build_commands(response, extra, adopted())

    def build_commands(response, extra, adopted):
        commands = _build_commands(response, extra)
        if adopted is not None:
            # 当前模型节点的 delta 不能把已被采用事务排除的旧成员带回内存。
            update = dict(commands[0].update)
            update["messages"] = Overwrite([*adopted, *response.result])
            commands[0] = replace(commands[0], update=update)
        return commands

    async def execute_model(request):
        async with execution_pause_boundary():
            return await model_call.ainvoke(request)

    async def amodel_node(state, runtime: Runtime[Any]):
        request = request_for(state, runtime)
        chain = asynchronous["awrap_model_call_handler"]
        with execution.graph_request_scope() as adopted:
            if chain is None:
                response, extra = await execute_model(request), None
            else:
                result = await chain(request, execute_model)
                response, extra = result.model_response, result.commands
            return build_commands(response, extra, adopted())

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


async def run_derived_agent(
    initial: DerivedModelRequest, *, execution: ModelCallExecution,
    max_model_calls: int,
    tools: Sequence[BaseTool] = (), middleware: Sequence[AgentMiddleware] = (),
    allowed_tool_ids: tuple[str, ...] = (),
    derived_tool_permit: Callable[[str], bool] | None = None,
    publication_gate: AbstractAsyncContextManager[None] | None = None,
    completion_deadline: datetime | None = None,
    trace_sink: TraceEventSinkProtocol | None = None,
) -> DerivedModelResult:
    """复用原图的工具循环；单次调用保存输出，整图完成才结束独立 execution。"""
    if type(max_model_calls) is not int or max_model_calls < 1:
        raise ValueError("派生执行必须声明有限的模型调用上限")
    calls = 0
    last_result: DerivedModelResult | None = None
    last_request = initial
    completed = False

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        if trace_sink is not None:
            # 派生沿原 Trace owner 写诊断，不发布普通 Job 调度事件。
            event = EVENT_FACTORY_REGISTRY[event_type].build(
                job_id=initial.admission.job_id, payload=payload,
                step_id=None, agent_id=None,
            )
            await trace_sink.append(initial.admission.session_id, event)

    async def request_for(request: ModelRequest) -> DerivedModelRequest:
        nonlocal calls, last_request
        if calls == 0:
            planned = initial
        else:
            if last_result is None:
                raise ValueError("派生执行没有后续工具循环请求绑定")
            planned = await run_sync_shielded(
                execution.continue_derived_request, last_request, last_result, request,
            )
            if planned.admission != initial.admission or planned.reference != initial.reference:
                raise ValueError("source-mismatch: 派生工具循环改变了 execution 归属")
        if calls >= max_model_calls:
            raise ValueError("derived-call-budget-exhausted: 派生模型调用次数已耗尽")
        calls += 1
        last_request = replace(planned, model_call_attempt=calls)
        return last_request

    def receive(result: DerivedModelResult) -> None:
        nonlocal last_result
        last_result = result

    async def converge(outcome: str, error: BaseException | None = None) -> None:
        nonlocal completed
        with bind_execution_pause(None):
            delta_token = set_current_model_delta_sink(None)
            scope_token = set_current_turn_execution_scope(None)
            try:
                await run_sync_shielded(
                    execution._checkpointer.converge_execution, initial.admission.session_id,
                    thread_id=initial.admission.thread_id, turn_id=None,
                    execution_id=initial.admission.execution_id, outcome=outcome, turn_status=None,
                    assembly_id=None if last_result is None else last_result.dispatch_ref.assembly_id,
                    checkpoint_ns=initial.checkpoint_ns,
                    completion_deadline=completion_deadline if outcome == "completed" else None,
                    failure=None if error is None else public_error(error,
                        code="provider_failure" if is_provider_failure(error) else "derived_execution_failed", stage="derived-execution"),
                )
            finally:
                reset_current_turn_execution_scope(scope_token)
                reset_current_model_delta_sink(delta_token)
        completed = outcome == "completed"

    async def invoke_derived(request: ModelRequest):
        result = await execution.aexecute_derived(await request_for(request), publish=publish)
        receive(result)
        return result.response

    try:
        admission = initial.admission
        summaries = tuple(source for source in initial.plan.contributions
                          if source.source_binding is not None and source.source_binding.summary_result_ref is not None)
        # 摘要输出只作 backing；来源截止点仍取首次冻结的原文，不能指向任务指令或摘要输出。
        summary_outputs = {(ref["session_id"], ref["thread_id"], ref["ref_id"])
                           for source in summaries for ref in initial.request_only_content[source.contribution_id]["output_refs"]}
        source_cutoff = next((ref for ref in reversed(initial.plan.refs) if ref.ref_type == "canonical_item"
                              and (ref.session_id, ref.thread_id, ref.ref_id) not in summary_outputs), None)
        await publish(EventType.JOB_CREATED, {
            "session_id": admission.session_id, "thread_id": admission.thread_id,
            "message": "", "agent_id": "derived",
            "message_metadata": {
                "execution_id": admission.execution_id,
                "task_id": initial.reference.task_id, "plan_id": initial.reference.plan_id,
                "checkpoint_ns": initial.checkpoint_ns,
                "model": chat_wire_profile(initial.model).model,
                "source_cutoff": None if source_cutoff is None else source_cutoff.to_dict(),
                "summary_selection": "explicit_plan" if initial.model.invocation_purpose == "compaction" else
                    "ready_replacement" if summaries else "no_applicable_ready",
            },
        })
        await publish(EventType.AGENT_START, {"agent_id": "derived"})
        graph = create_model_call_agent(
            initial.model, execution=execution,
            model_call=RunnableLambda(lambda request: asyncio.run(invoke_derived(request)), afunc=invoke_derived),
            tools=list(tools), middleware=[DerivedToolPolicyMiddleware(allowed_tool_ids, permit=derived_tool_permit), *middleware],
        )
        with bind_execution_pause(None):
            delta_token = set_current_model_delta_sink(None)
            scope_token = set_current_turn_execution_scope(None)
            try:
                await graph.ainvoke({"messages": []}, config={
                    "callbacks": [], "tags": [], "metadata": {}, "recursion_limit": max_model_calls * 2 + 1,
                })
            finally:
                reset_current_turn_execution_scope(scope_token)
                reset_current_model_delta_sink(delta_token)
        if last_result is None:
            raise RuntimeError("派生图未产生模型结果")
        output = last_result.response.result[-1]
        if output.tool_calls or output.invalid_tool_calls or not output.text.strip():
            raise ValueError("派生图未生成完整文本答案")
        async with publication_gate if publication_gate is not None else nullcontext():
            await run_cleanup_shielded(lambda: converge("completed"))
        await run_cleanup_shielded(lambda: publish(EventType.JOB_COMPLETED, {}))
        return last_result
    except BaseException as error:
        if completed:
            raise
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        failure_payload = {}
        if outcome == "failed":
            if isinstance(error, ContextCapacityError):
                capacity = error.budget.capacity.model_dump(mode="json")
                estimate = f"input_estimate={error.budget.estimate.known_tokens}; count_quality={error.budget.estimate.quality}"
            else:
                profile = initial.model.profile or {}
                capacity = {"max_input_tokens": profile.get("max_input_tokens"), "context_window": profile.get("context_window")}
                # seal 未保留本次估算时只报告缺口，不能为上游错误重算或伪造精确值。
                estimate = "input_estimate=not_recorded; count_quality=not_recorded"
            failure_payload["error"] = f"capacity={capacity}; {estimate}; {public_error(error)['message']}"
        try:
            await run_cleanup_shielded(partial(converge, outcome, error))
            await run_cleanup_shielded(lambda: publish(
                EventType.JOB_CANCELLED if outcome == "cancelled" else EventType.JOB_FAILED,
                failure_payload,
            ))
        except Exception as settlement_error:  # noqa: BLE001 — 保留原故障并明确附加结算失败。
            error.add_note(f"派生执行结算失败: {type(settlement_error).__name__}: {settlement_error}")
        raise
