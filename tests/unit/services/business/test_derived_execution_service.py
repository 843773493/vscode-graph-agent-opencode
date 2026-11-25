"""服务保持首次冻结计划；有限观察不拥有后台执行的取消权。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.agents.tool_node_adapter import DerivedToolPolicyMiddleware
from app.core.identifier import create_prefixed_id
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.domain.background_work import BackgroundTaskHandle
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.schemas.internal_v2.derived_execution import DerivedExecutionSubmitRequest
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.business.derived_execution_service import (
    DerivedExecutionConflictError,
    DerivedExecutionService,
)


@pytest.fixture
def derived_case():
    session_id, thread_id = create_prefixed_id("ses"), create_prefixed_id("thr")
    state = SimpleNamespace(model="first-model", source="first-item", freezes=0, released=0, cancelled=0,
        promotions=0, handle=None, frozen=None, tool_enabled=True, tool_confirmation=False)
    state.tool_set = ToolSetSnapshot(selection_revision=sha256_jcs({"tools": "first"}), tools=({
        "type": "function", "function": {"name": "read_file", "description": "首版读取", "parameters": {"type": "object"}},
    },))

    @asynccontextmanager
    async def admission(_session):
        yield

    async def session_get(_session):
        return SimpleNamespace(current_agent_id="default")

    def freeze_input(session, thread, **kwargs):
        state.freezes += 1
        state.frozen = SimpleNamespace(**{key: kwargs[key] for key in (
            "task_id", "plan_id", "instruction", "allowed_tool_ids", "deadline", "max_model_calls", "submission_hash", "model_recipe", "gateway_id", "initial_tool_set",
        )}, checkpoint_ns="", source_view_id="view-first", refs=(SimpleNamespace(
            session_id=session, thread_id=thread, ref_id=state.source, content_hash="hash-first"),))
        return DetailRef(session, thread, "derived-input", "input")

    def release(*args, **kwargs):
        state.released += 1

    async def spawn(**kwargs):
        async with kwargs["descriptor"](state.handle) as descriptor:
            if state.handle is None:
                state.handle = BackgroundTaskHandle(kwargs["task_id"], kwargs["session_id"], kwargs["thread_id"],
                    kwargs["agent_id"], descriptor, {}, "pending", datetime.now(UTC), "lease-test")
            if kwargs["foreground"]:
                state.promotions += 1
        return state.handle

    async def recover(*args):
        return ()

    async def cancel(*args):
        state.cancelled += 1
        state.handle.status = "cancelled"
        return state.handle

    registry = SimpleNamespace(spawn_derived=spawn, get_handle=lambda *_: state.handle, recover_derived_thread=recover, cancel=cancel)
    handler = SimpleNamespace(kind="derived_execution", version=1, read_input=lambda _: state.frozen, completed_result=lambda _: None)
    config = SimpleNamespace(get_snapshot=lambda: SimpleNamespace(revision="config-first"), use_snapshot=lambda _: nullcontext(),
        resolve_agent_model=lambda *_: ModelReferenceDTO(provider_id="primary", model_id="default"),
        freeze_model_recipe=lambda _: {"model": state.model},
        get_agent_runtime_config=lambda *_, **__: {"system_prompt": "冻结规则", "runtime_identity_description": "本地运行时"})
    service = DerivedExecutionService(registry=registry, handler=handler,
        store=SimpleNamespace(admission=admission, resolve_thread=lambda *_: None),
        saver=SimpleNamespace(freeze_derived_input=freeze_input, read_derived_input=lambda *_: state.frozen, discard_derived_input=release),
        sessions=SimpleNamespace(get=session_get), config=config,
        tool_settings=lambda: SimpleNamespace(derived_tools=lambda *_: ({"read_file": SimpleNamespace(
            execution_enabled=state.tool_enabled, confirmation_required=state.tool_confirmation)}, state.tool_set)))
    request = DerivedExecutionSubmitRequest(session_id=session_id, thread_id=thread_id, idempotency_key="same",
        instruction="冻结资料分析", timeout_seconds=20, max_model_calls=2)
    return service, state, request


@pytest.mark.asyncio
async def test_retry_reuses_first_source_model_deadline_and_allows_promotion(derived_case):
    service, state, request = derived_case
    first = await service.submit(request)
    deadline = state.frozen.deadline
    state.model, state.source = "updated-default", "appended-item"
    again = await service.submit(request.model_copy(update={"foreground": True}))
    assert again.task_id == first.task_id
    assert again.source_last_ref.item_id == "first-item"
    assert state.frozen.model_recipe == {"model": "first-model"}
    assert state.frozen.deadline == deadline
    assert state.freezes == 1
    assert state.promotions == 1
    with pytest.raises(DerivedExecutionConflictError, match="不同的显式输入"):
        await service.submit(request.model_copy(update={"instruction": "different", "foreground": True}))
    with pytest.raises(DerivedExecutionConflictError, match="Thread"):
        await service.submit(request.model_copy(update={"thread_id": create_prefixed_id("thr")}))
    assert state.freezes == 1
    assert state.released == 0
    assert state.promotions == 1


@pytest.mark.asyncio
async def test_wait_timeout_and_observer_cancellation_preserve_background_task(derived_case):
    service, state, request = derived_case
    accepted = await service.submit(request)
    snapshot = await service.get(request.session_id, request.thread_id, accepted.task_id, wait_seconds=0.01)
    assert snapshot.status == "pending"
    observer = asyncio.create_task(service.get(request.session_id, request.thread_id, accepted.task_id, wait_seconds=30))
    await asyncio.sleep(0)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert state.handle.status == "pending"
    assert state.cancelled == 0
    cancelled = await service.cancel(request.session_id, request.thread_id, accepted.task_id)
    assert cancelled.status == "cancelled"
    assert state.cancelled == 1


@pytest.mark.asyncio
async def test_file_tool_requires_real_gateway_and_current_policy_before_freeze(derived_case):
    service, state, request = derived_case
    request = request.model_copy(update={"allowed_tool_ids": ["read_file"]})
    with pytest.raises(ValueError, match="真实 Gateway"):
        await service.submit(request)
    assert state.freezes == 0
    token = set_current_gateway_id("gateway_" + "1" * 32)
    try:
        state.tool_enabled = False
        with pytest.raises(ValueError, match="derived-tool-denied"):
            await service.submit(request)
        state.tool_enabled, state.tool_confirmation = True, True
        with pytest.raises(ValueError, match="derived-tool-denied"):
            await service.submit(request)
        assert state.freezes == 0
        state.tool_confirmation = False
        first = await service.submit(request)
        assert state.frozen.gateway_id == "gateway_" + "1" * 32
        first_tools = state.frozen.initial_tool_set
        state.tool_set = ToolSetSnapshot(selection_revision=sha256_jcs({"tools": "new"}), tools=())
        state.tool_enabled = False
    finally:
        reset_current_gateway_id(token)
    # 重试只观察首次受理计划；运行时会在每次实际调用重新核当前撤销。
    again = await service.submit(request)
    assert again.task_id == first.task_id
    assert state.freezes == 1
    assert state.frozen.gateway_id == "gateway_" + "1" * 32
    assert state.frozen.initial_tool_set == first_tools
    assert state.frozen.initial_tool_set.tools[0]["function"]["description"] == "首版读取"


@pytest.mark.asyncio
async def test_tool_revocation_is_checked_again_before_each_actual_invocation(derived_case):
    service, state, _ = derived_case
    executed = []

    def permit(tool_id):
        policies, _ = service._tool_settings().derived_tools("default", (tool_id,))
        policy = policies[tool_id]
        return policy.execution_enabled and not policy.confirmation_required

    middleware = DerivedToolPolicyMiddleware(("read_file",), permit=permit)
    request = ToolCallRequest(tool_call={"name": "read_file", "args": {}, "id": "read-one"},
                              tool=None, state={}, runtime=None)

    async def execute(request):
        executed.append(request.tool_call["id"])
        return ToolMessage(content="首个真实结果", name="read_file", tool_call_id=request.tool_call["id"])

    first = await middleware.awrap_tool_call(request, execute)
    state.tool_enabled = False
    denied = await middleware.awrap_tool_call(request.override(tool_call={**request.tool_call, "id": "read-two"}), execute)
    assert first.status == "success"
    assert denied.status == "error" and denied.tool_call_id == "read-two"
    assert "derived-tool-denied" in denied.content
    assert executed == ["read-one"]
