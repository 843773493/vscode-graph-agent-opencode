"""真实 stdio 的进度、原请求取消与断线，不以本地取消冒充远端回滚。"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    sealed_extension_catalog_binding_resolver,
)
from app.core.identifier import create_uuid_hex
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.schemas.internal_v2.mcp import McpInvocationReceipt
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.invocation import (
    mcp_cancelled_receipt,
    mcp_progress_scope,
)
from app.services.orchestration.activity_runtime import (
    ActivityHandlerRegistry,
    ActivityRuntime,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.schema_server import build_server
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    SchemaServerCase,
)
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    schema_server_case as schema_server_case,  # noqa: PLC0414 - fixture 使用当前正式测试输出目录
)


def call(invoker, call_id: str, mode: str):
    return invoker.ainvoke({
        "type": "tool_call", "id": call_id, "name": invoker.name,
        "args": {"tool_name": "mcp__schema__probe", "arguments": {"call_mode": mode}},
    })


@pytest.fixture
def progress_case():
    return SimpleNamespace(values=[], seen=asyncio.Event())


async def test_real_progress_and_cancel_only_the_original_parallel_request(
    schema_server_case: SchemaServerCase, progress_case,
) -> None:
    async with schema_server_case.started() as case:
        async def progress(value, total):
            progress_case.values.append((value, total))
            progress_case.seen.set()

        invoker = case.invoker()
        with mcp_progress_scope(progress):
            # 正式信封自己拥有进度 scope；直接 SDK 工具验证原回调和 RPC ID。
            target = case.owner.get_tools()[0]
            pending = asyncio.create_task(target.ainvoke({
                "type": "tool_call", "id": "cancel-original", "name": target.name,
                "args": {"call_mode": "wait"},
            }))
        await asyncio.wait_for(progress_case.seen.wait(), 5)
        sibling = asyncio.create_task(call(invoker, "sibling", "progress"))
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await pending
        receipt = mcp_cancelled_receipt(cancelled.value)
        assert receipt is not None and receipt.cancellation_delivery == "sent"
        assert receipt.retryable is False
        result = await asyncio.wait_for(sibling, 5)
        assert result.status == "success" and result.tool_call_id == "sibling"
        for _ in range(100):
            if any(row.get("cancelled") for row in case.received()):
                break
            await asyncio.sleep(0.01)
        assert len([row for row in case.received() if row.get("cancelled")]) == 1
        assert [row["arguments"]["call_mode"] for row in case.received() if row.get("cancelled")] == ["wait"]
        assert progress_case.values == [(1, 2)]


async def test_real_disconnect_returns_paired_non_retryable_unknown(
    schema_server_case: SchemaServerCase,
) -> None:
    async with schema_server_case.started() as case:
        result = await asyncio.wait_for(call(case.invoker(), "lost-call", "disconnect"), 5)
        assert result.status == "error" and result.tool_call_id == "lost-call"
        receipt = McpInvocationReceipt.from_artifact(result.artifact)
        assert receipt is not None and receipt.retryable is False
        assert receipt.effect == "unknown" and receipt.error_type == "McpError"
        assert json.loads(result.content) == receipt.model_dump()
        assert case.received() == [{"name": "probe", "arguments": {"call_mode": "disconnect"}}]


async def test_cancelled_envelope_keeps_durable_unknown_after_turn_cleanup_and_close(
    schema_server_case: SchemaServerCase,
) -> None:
    case = schema_server_case
    async with case.started():
        binding = case.owner.binding_snapshot().resolve("mcp__schema__probe")
        assert binding.connection_ref is not None
        pending = asyncio.create_task(call(case.invoker(), "durable-cancel", "wait"))
        for _ in range(100):
            if case.received():
                break
            await asyncio.sleep(0.01)
        assert case.received() == [{"name": "probe", "arguments": {"call_mode": "wait"}}]
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await pending
        assert mcp_cancelled_receipt(cancelled.value) is not None
        ledger = case.owner._leases
        lease, = ledger.leases_for_resource(binding.connection_ref)
        assert lease.status == "reconcile_required"
        ledger.release_turn_leases(lease.turn_stream_id)
        sibling = await call(case.invoker(), "durable-sibling", "progress")
        assert sibling.status == "success"
        assert (record := ledger.get(binding.connection_ref)) is not None and record.status == "running"
        assert ledger.leases_for_resource(binding.connection_ref) == [lease]
    cold = ExternalResourceLeaseLedger(state_path=case.workspace_root / ".boxteam/resources.json")
    assert (record := cold.get(binding.connection_ref)) is not None and record.status == "stopped"
    assert (retained := cold.get_lease(lease.lease_id)) is not None and retained.status == "reconcile_required"


async def test_direct_dictionary_unknown_result_does_not_release_original_occupancy(
    schema_server_case: SchemaServerCase,
) -> None:
    async with schema_server_case.started() as case:
        result = await case.invoker().ainvoke({"tool_name": "mcp__schema__probe", "arguments": {"call_mode": "disconnect"}})
        assert json.loads(result)["retryable"] is False
        resource_id = case.owner.binding_snapshot().resolve("mcp__schema__probe").connection_ref
        assert resource_id is not None
        lease, = case.owner._leases.leases_for_resource(resource_id)
        assert lease.status == "reconcile_required"


@pytest.fixture
def activity_case():
    writer = SimpleNamespace(commit=AsyncMock())
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())
    context = ToolInvocationContext(thread_binding=ThreadRuntimeBinding("session", f"thr_{create_uuid_hex()}"))
    return SimpleNamespace(writer=writer, runtime=runtime, context=context)


@pytest.mark.parametrize("mode, expected", [("progress", "success"), ("disconnect", "outcome_unknown")])
async def test_envelope_publishes_real_progress_and_terminal_activity(
    schema_server_case: SchemaServerCase, activity_case, mode, expected,
) -> None:
    async with schema_server_case.started() as case:
        from app.domain.itemized.identity.item_projection import ItemProjectionIdentity

        context = activity_case.context
        context_token = context.set_tool_call_id("activity-call", projection_identity=ItemProjectionIdentity(model_call_id=create_uuid_hex()))
        thread = context.thread_binding
        with case.owner.execution_handles.reserve_model_call(
            case.owner.binding_snapshot(), session_id=thread.session_id, thread_id=thread.thread_id,
            model_call_id=context.require_model_call_id(),
        ) as retain:
            retain([{"id": "activity-call", "name": "invoke_extension_tool", "args": {"tool_name": "mcp__schema__probe"}}])
        sink_token = set_current_model_delta_sink(SimpleNamespace(activities=activity_case.runtime))
        try:
            invoker = create_extension_tool_invoker_tool(
                case.owner.get_tools(), invocation_context=context,
                catalog_binding_resolver=sealed_extension_catalog_binding_resolver(case.owner.binding_snapshot()),
                target_resolver=case.owner.execution_handles.execution_target,
            )
            result = await asyncio.wait_for(call(invoker, "activity-call", mode), 5)
        finally:
            reset_current_model_delta_sink(sink_token)
            context.reset_tool_call_id(context_token)
        commits = activity_case.writer.commit.await_args_list
        assert commits[0].args[0] == "activity.started"
        assert commits[-1].args[1]["outcome"] == expected
        assert {entry.args[1]["activity_id"] for entry in commits} == {commits[0].args[1]["activity_id"]}
        if mode == "progress":
            assert [entry.args[1]["detail"] for entry in commits if entry.args[0] == "activity.updated"] == [
                {"phase": "running", "progress": 1, "total": 2},
                {"phase": "running", "progress": 2, "total": 2},
            ]
            assert McpInvocationReceipt.from_artifact(result.artifact) is None
        assert "/private/" not in repr(commits)


@pytest.mark.parametrize("schema_server_case", [1], indirect=True)
async def test_expired_sealed_call_is_paired_and_never_reaches_real_server(
    schema_server_case: SchemaServerCase, activity_case,
) -> None:
    from app.domain.itemized.identity.item_projection import ItemProjectionIdentity

    async with schema_server_case.started() as case:
        context = activity_case.context
        thread = context.thread_binding
        model_call_id = create_uuid_hex()
        token = context.set_tool_call_id("expired-call", projection_identity=ItemProjectionIdentity(model_call_id=model_call_id))
        try:
            binding = case.owner.binding_snapshot()
            with case.owner.execution_handles.reserve_model_call(
                binding, session_id=thread.session_id, thread_id=thread.thread_id, model_call_id=model_call_id,
            ) as retain:
                retain([{"id": "expired-call", "name": "invoke_extension_tool", "args": {"tool_name": "mcp__schema__probe"}}])
            await asyncio.sleep(1.1)
            invoker = create_extension_tool_invoker_tool(
                case.owner.get_tools(), invocation_context=context,
                catalog_binding_resolver=sealed_extension_catalog_binding_resolver(binding),
                target_resolver=case.owner.execution_handles.execution_target,
            )
            result = await call(invoker, "expired-call", "progress")
        finally:
            context.reset_tool_call_id(token)
        assert result.tool_call_id == "expired-call" and result.status == "error"
        assert "资格已失效" in result.content
        assert case.received() == []
        resource_id = binding.resolve("mcp__schema__probe").connection_ref
        assert resource_id is not None
        cold = ExternalResourceLeaseLedger(state_path=case.workspace_root / ".boxteam/resources.json")
        record = cold.get(resource_id)
        assert record is not None and record.status == "running"
        assert not cold.leases_for_resource(resource_id)


async def test_progress_writer_failure_is_reported_instead_of_sdk_swallowing_it(
    schema_server_case: SchemaServerCase,
) -> None:
    async with schema_server_case.started() as case:
        async def fail_progress(_value, _total):
            raise RuntimeError("进度写入失败")

        target = case.owner.get_tools()[0]
        with mcp_progress_scope(fail_progress):
            result = await asyncio.wait_for(target.ainvoke({
                "type": "tool_call", "id": "progress-failed", "name": target.name,
                "args": {"call_mode": "wait"},
            }), 5)
        receipt = McpInvocationReceipt.from_artifact(result.artifact)
        assert receipt is not None and receipt.error_type == "RuntimeError"
        assert receipt.cancellation_delivery == "sent" and result.status == "error"
        # 取消通知与错误回复仍会异步到达；后继真实调用须能使用同一共享连接。
        sibling = await call(case.invoker(), "after-observer-failure", "progress")
        assert sibling.status == "success"


@pytest.fixture
def http_schema_endpoint_factory(request: pytest.FixtureRequest):
    @asynccontextmanager
    async def endpoint(label: str):
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        definitions = context.artifacts_dir / f"{label}-definitions.json"
        definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
        receipts = context.artifacts_dir / f"{label}-receipts.jsonl"
        manager = StreamableHTTPSessionManager(app=build_server(definitions, receipts))

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    if task.done():
                        await task
                        raise RuntimeError("HTTP Server 提前退出")
                    await asyncio.sleep(0.01)
                if not server.started:
                    raise TimeoutError("HTTP Server 未就绪")
                yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp/", context=context,
                                      definitions=definitions, receipts=receipts)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
    return endpoint


@pytest.fixture
async def http_schema_server_case(http_schema_endpoint_factory):
    async with http_schema_endpoint_factory("http") as endpoint:
        owner = McpCatalogOwner(
            leases=ExternalResourceLeaseLedger(state_path=endpoint.context.workspace_root / ".boxteam/resources.json"),
            raw_config={"servers": {"schema": {"enabled": True, "transport": "streamable_http", "url": endpoint.url}}},
            workspace_root=endpoint.context.workspace_root, event_service=EventChannelService(),
        )
        async with SchemaServerCase(owner, endpoint.definitions, endpoint.receipts, endpoint.context.workspace_root).started() as case:
            yield case


async def test_real_http_progress_cancel_and_following_request(http_schema_server_case, progress_case):
    case = http_schema_server_case

    async def progress(value, total):
        progress_case.values.append((value, total))
        progress_case.seen.set()

    target = case.owner.get_tools()[0]
    with mcp_progress_scope(progress):
        pending = asyncio.create_task(target.ainvoke({"type": "tool_call", "id": "http-cancel", "name": target.name,
                                                     "args": {"call_mode": "wait"}}))
    await asyncio.wait_for(progress_case.seen.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await pending
    assert mcp_cancelled_receipt(cancelled.value).cancellation_delivery == "sent"
    result = await call(case.invoker(), "http-following", "progress")
    assert result.status == "success" and result.tool_call_id == "http-following"
    assert progress_case.values == [(1, 2)]
    assert any(row.get("cancelled") for row in case.received())
