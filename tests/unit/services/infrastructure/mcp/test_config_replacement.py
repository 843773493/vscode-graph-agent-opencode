"""候选隔离、共同切换、原连接资格与退役排空。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
import pytest
from langchain_core.tools import BaseTool, StructuredTool

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.services.infrastructure.events.channel_events import (
    resource_state_channel_name,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogError, McpCatalogOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.invocation import McpResourceUse
from tests.support.mcp.session_doubles import FakeMcpSession, FakeMcpSessionFactory


def config(version: str, *, beta: bool = True):
    return {"servers": {name: {"enabled": True, "transport": "streamable_http",
                              "url": f"http://localhost/{name}/{version}"}
                        for name in (("alpha", "beta") if beta else ("alpha",))}}


class ReplacementFactory(FakeMcpSessionFactory):
    def __init__(self):
        super().__init__({"alpha": ["echo"], "beta": ["echo"]})
        self.opened = []
        self.closed = []
        self.history = []
        self.fail_server = None
        self.notify_during_list = False

    def __call__(self, server, notify):
        original = super().__call__(server, notify)
        address = server.connection["command"] if server.transport == "stdio" else server.connection["url"]
        session = self.sessions[server.server_id]

        async def list_tools() -> list[BaseTool]:
            if server.server_id == self.fail_server:
                raise RuntimeError("第二个候选完整目录失败")
            if self.notify_during_list:
                self.notify_during_list = False
                notify()
            if session.tool_names != ["echo"]:
                return await FakeMcpSession.list_tools(session)
            async def echo(text: str) -> str:
                return address + ":" + text
            return [StructuredTool.from_function(coroutine=echo, name="echo", description="echo 工具")]

        session.list_tools = list_tools

        @asynccontextmanager
        async def connected():
            async with anyio.create_task_group() as group, original as port:
                task = asyncio.current_task()
                self.opened.append((address, task))
                self.history.append(("open", address))
                try:
                    yield port
                finally:
                    self.closed.append((address, asyncio.current_task()))
                    self.history.append(("close", address))
                    group.cancel_scope.cancel()
        return connected()


@dataclass
class ReplacementCase:
    factory: ReplacementFactory
    owner: McpCatalogOwner
    scopes: list[LifetimeScope] = field(default_factory=list)
    effects: list[dict[str, object]] = field(default_factory=list)
    cleanup_failed: bool = False

    async def prepare(self, payload):
        scope = LifetimeScope("test-config-candidate")
        self.scopes.append(scope)
        return await self.owner.prepare_config(payload, scope, self.effects.append)

    def reserve(self, model_id: str = "model"):
        binding = self.owner.binding_snapshot()
        with self.owner.execution_handles.reserve_model_call(binding, session_id="session", thread_id="thread", model_call_id=model_id) as retain:
            retain([{"name": "invoke_extension_tool", "id": "call", "args": {"tool_name": "mcp__alpha__echo"}}])
        operation = software_tool_operation_id("mcp_call", "session", "thread", model_id, "call")
        return binding.resolve("mcp__alpha__echo"), McpResourceUse("holder", operation, "session", "thread", model_id)


@pytest.fixture
async def replacement_case(tmp_path, event_channel_service, request):
    factory = ReplacementFactory()
    option = getattr(request, "param", 1800)
    payload = {**config("old"), "supervision": {"sealed_call_timeout_seconds": 1800 if option == "stdio" else option}}
    if option == "stdio":
        payload["servers"]["alpha"] = {"transport": "stdio", "command": "stdio-old", "enabled": True}
    owner = McpCatalogOwner(raw_config=payload, workspace_root=tmp_path,
                            event_service=event_channel_service, leases=ExternalResourceLeaseLedger(), session_factory=factory)
    case = ReplacementCase(factory, owner)
    await owner.start()
    try:
        yield case
    finally:
        for scope in reversed(case.scopes):
            await scope.close()
        if case.cleanup_failed:
            with pytest.raises(ExceptionGroup, match="MCP Owner 关闭失败"):
                await owner.shutdown()
        else:
            await owner.shutdown()
        assert sorted(factory.opened, key=lambda row: row[0]) == sorted(factory.closed, key=lambda row: row[0])


async def wait_closed(runtime):
    async def closing():
        while not runtime.scope.is_closed:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(closing(), 3)


async def test_all_candidates_switch_together_and_same_schema_changes_only_connection(replacement_case):
    case = replacement_case
    owner = case.owner
    before = owner.binding_snapshot()
    old_runtimes = dict(owner._runtimes)
    publications = []
    unsubscribe = owner.subscribe_publication(publications.append)
    try:
        publish, ids = await case.prepare(config("new"))
        assert owner.binding_snapshot() == before and len(publications) == 1
        assert owner._runtimes == old_runtimes
        assert ids == {"mcp__alpha__echo", "mcp__beta__echo"}
        publish()
        after = owner.binding_snapshot()
        assert after.catalog_revision == before.catalog_revision and after.generation == before.generation
        assert len(publications) == 2
        for name in ("alpha", "beta"):
            assert after.resolve(f"mcp__{name}__echo").connection_ref != before.resolve(f"mcp__{name}__echo").connection_ref
            await wait_closed(old_runtimes[name])
        await case.scopes[-1].close()
        assert all(runtime.running for runtime in owner._runtimes.values())
    finally:
        unsubscribe()


async def test_partial_candidate_failure_keeps_all_old_connections_and_cleans_candidates(replacement_case):
    case = replacement_case
    before = case.owner.publication_snapshot()
    old_runtimes = dict(case.owner._runtimes)
    case.factory.fail_server = "beta"
    with pytest.raises(McpCatalogError, match="server_id=beta stage=configuration"):
        await case.prepare(config("new"))
    await case.scopes[-1].close()
    assert case.owner.publication_snapshot() is before and case.owner._runtimes == old_runtimes
    assert all(runtime.running for runtime in old_runtimes.values())
    assert {address for address, _ in case.factory.closed} == {"http://localhost/alpha/new", "http://localhost/beta/new"}


async def test_candidate_rejects_normalized_name_collision_with_unchanged_server(replacement_case):
    case = replacement_case
    before = case.owner.publication_snapshot()
    case.factory._initial_tools["alpha."] = ["echo"]
    payload = config("old")
    payload["servers"]["alpha."] = {"enabled": True, "transport": "streamable_http", "url": "http://localhost/collision"}
    with pytest.raises(McpCatalogError, match="stage=configuration"):
        await case.prepare(payload)
    await case.scopes[-1].close()
    assert case.owner.publication_snapshot() is before
    assert case.owner.get_tool_ids() == {"mcp__alpha__echo", "mcp__beta__echo"}


async def test_unchanged_server_relist_waits_for_candidate_commit(replacement_case):
    case = replacement_case
    payload = config("old")
    payload["servers"]["alpha"]["url"] = "http://localhost/alpha/new"
    publish, _ = await case.prepare(payload)
    case.factory.sessions["beta"].tool_names = ["changed"]
    case.factory.notify_callbacks["beta"]()
    await asyncio.sleep(0)
    assert case.owner.get_tool_ids() == {"mcp__alpha__echo", "mcp__beta__echo"}
    publish()
    await asyncio.gather(*tuple(case.owner._pending_relist_tasks))
    assert case.owner.get_tool_ids() == {"mcp__alpha__echo", "mcp__beta__changed"}


async def test_confirmation_crosses_replacement_but_cannot_grant_new_old_generation_qualification(replacement_case):
    case = replacement_case
    binding, use = case.reserve()
    old = case.owner._runtimes["alpha"]
    old_snapshot = case.owner.binding_snapshot()
    publish, _ = await case.prepare(config("new"))
    publish()
    assert old.running
    with pytest.raises(ExtensionTargetResolutionError), case.owner.execution_handles.reserve_model_call(
        old_snapshot, session_id="session", thread_id="thread", model_call_id="late",
    ):
        pytest.fail("退役代际不能取得新的资格")
    with case.owner.execution_handles.execution_target(binding, use) as execution:
        execution.started = True
        assert await execution.tool.ainvoke({"text": "confirmed"}) == "http://localhost/alpha/old:confirmed"
        execution.result_confirmed = True
    await wait_closed(old)
    current, use = case.reserve("current-model")
    with case.owner.execution_handles.execution_target(current, use) as execution:
        execution.started = True
        assert await execution.tool.ainvoke({"text": "current"}) == "http://localhost/alpha/new:current"
        execution.result_confirmed = True


async def test_later_disable_revokes_already_retired_generation_and_keeps_unknown_fact(replacement_case):
    case = replacement_case
    binding, use = case.reserve()
    old = case.owner._runtimes["alpha"]
    publish, _ = await case.prepare(config("new"))
    publish()
    payload = config("new")
    payload["servers"]["alpha"]["enabled"] = False
    disable, _ = await case.prepare(payload)
    disable()
    with pytest.raises(ExtensionTargetResolutionError), case.owner.execution_handles.execution_target(binding, use):
        pytest.fail("禁用必须撤销更早原资格")
    assert old.running


async def test_only_changed_server_is_prepared_and_old_notification_cannot_overwrite_new(replacement_case):
    case = replacement_case
    old_notify = case.factory.notify_callbacks["alpha"]
    beta = case.owner._runtimes["beta"]
    payload = config("old")
    payload["servers"]["alpha"]["url"] = "http://localhost/alpha/new"
    publish, _ = await case.prepare(payload)
    publish()
    assert case.owner._runtimes["beta"] is beta and beta.running
    publication = case.owner.publication_snapshot()
    old_notify()
    assert case.owner.publication_snapshot() is publication
    assert len(case.factory.opened) == 3


async def test_notification_between_candidate_relist_and_commit_closes_readiness_until_refresh(replacement_case):
    case = replacement_case
    publish, _ = await case.prepare(config("new"))
    case.factory.sessions["alpha"].tool_names = ["changed"]
    case.factory.notify_callbacks["alpha"]()
    publish()
    assert case.owner.publication_snapshot().status == "dirty"
    await asyncio.gather(*tuple(case.owner._pending_relist_tasks))
    assert case.owner.publication_snapshot().status == "available"
    assert case.owner.get_tool_ids() == {"mcp__alpha__changed", "mcp__beta__echo"}


@pytest.mark.parametrize("replacement_case", [1], indirect=True)
async def test_retirement_deadline_closes_socket_without_settling_unknown_operation(replacement_case):
    case = replacement_case
    binding, use = case.reserve()
    old = case.owner._runtimes["alpha"]
    with case.owner.execution_handles.execution_target(binding, use) as execution:
        execution.started = True
        publish, _ = await case.prepare(config("new"))
        publish()
        await wait_closed(old)
        lease, = case.owner._leases.leases_for_resource(binding.connection_ref)
        assert lease.status == "active" and lease.lease_id.startswith("mcp-invocation:")
    lease, = case.owner._leases.leases_for_resource(binding.connection_ref)
    assert lease.status == "reconcile_required"
    assert case.owner._leases.get(binding.connection_ref).status == "stopped"


async def test_cleanup_failure_is_observable_without_reverting_new_catalog(
    replacement_case, event_channel_service,
):
    case = replacement_case
    old = case.owner._runtimes["alpha"]
    binding, use = case.reserve()
    channel = event_channel_service.channel(resource_state_channel_name("mcp"))
    subscription = channel.subscribe(label="test retirement failure")

    def fail_release():
        raise RuntimeError("/private/path token=must-never-leak")

    old.scope.register(fail_release, label="injected-transport-cleanup")
    case.cleanup_failed = True
    try:
        publish, _ = await case.prepare(config("new"))
        publish()
        current = case.owner.binding_snapshot()
        row, = [row for row in case.owner.cleanup_diagnostics() if row.connection_ref == binding.connection_ref]
        assert row.phase == "draining" and not row.release_verified and row.error_type is None
        with case.owner.execution_handles.execution_target(binding, use) as execution:
            execution.started = execution.result_confirmed = True
        event = await asyncio.wait_for(subscription.next(), 3)
        assert event.event.resource_id == binding.connection_ref
        assert event.event.state == "release_failed"
        row, = [row for row in case.owner.cleanup_diagnostics() if row.connection_ref == binding.connection_ref]
        assert row.phase == "cleanup_failed" and row.error_type == "ExceptionGroup"
        assert not row.release_verified and old.scope.state == "close_failed"
        assert "private" not in row.model_dump_json() and "token" not in row.model_dump_json()
        assert case.owner.binding_snapshot() is current
        assert case.owner.publication_snapshot().status == "available"
        assert case.owner._runtimes["alpha"].running
        with pytest.raises(RuntimeError, match="拒绝冲突替换"):
            await case.prepare(config("third"))
        await case.scopes[-1].close()
        assert case.owner.binding_snapshot() is current
        # 不相关 Server 可以更新，不让一个旧资源释放错误封锁整个工作区。
        payload = config("new")
        payload["servers"]["beta"]["url"] = "http://localhost/beta/third"
        publish, _ = await case.prepare(payload)
        publish()
        assert case.owner.binding_snapshot().resolve("mcp__alpha__echo") == current.resolve("mcp__alpha__echo")
    finally:
        channel.unsubscribe(subscription)


@pytest.mark.parametrize("replacement_case", [1], indirect=True)
async def test_cleanup_timeout_retains_failure_until_release_is_verified(replacement_case):
    case = replacement_case
    old = case.owner._runtimes["alpha"]
    released = asyncio.Event()
    old.scope.register(released.wait, label="blocked-transport-cleanup")
    case.cleanup_failed = True
    publish, _ = await case.prepare(config("new"))
    publish()
    try:
        async with asyncio.timeout(3):
            while not any(row.phase == "cleanup_failed" for row in case.owner.cleanup_diagnostics()):
                await asyncio.sleep(0.01)
        row, = case.owner.cleanup_diagnostics()
        assert row.error_type == "TimeoutError" and not row.release_verified
        assert old.scope.state == "closing"
        with pytest.raises(RuntimeError, match="拒绝冲突替换"):
            await case.prepare(config("third"))
        await case.scopes[-1].close()
    finally:
        released.set()
        await wait_closed(old)
    # 迟到但已核实的关闭不被伪报为仍未释放；原超时事实继续可见。
    row, = case.owner.cleanup_diagnostics()
    assert row.phase == "cleanup_failed" and row.release_verified
    publish, _ = await case.prepare(config("third"))
    publish()


async def test_new_stdio_server_prepares_without_stopping_the_old_http_connection(replacement_case):
    case = replacement_case
    before = case.owner.publication_snapshot()
    payload = config("new")
    payload["servers"]["beta"] = {"transport": "stdio", "command": "single-instance", "enabled": True}
    publish, _ = await case.prepare(payload)
    assert case.owner.publication_snapshot() is before and len(case.factory.opened) == 4
    assert not case.effects
    publish()
    assert case.owner._runtimes["beta"].server.transport == "stdio"


@pytest.mark.parametrize("replacement_case", ["stdio"], indirect=True)
async def test_stdio_stop_is_journaled_before_close_and_replacement_cannot_reuse_old_qualification(replacement_case, monkeypatch):
    case = replacement_case
    binding, use = case.reserve()
    old = case.owner._runtimes["alpha"]
    old_close = old.scope.close

    async def observe_close():
        assert case.effects == [{"resource": "mcp_connection", "action": "stop", "server_id": "alpha", "connection_ref": binding.connection_ref}]
        assert case.owner.publication_snapshot().status == "unavailable"
        assert not old.running
        with pytest.raises(ExtensionTargetResolutionError), case.owner.execution_handles.execution_target(binding, use):
            pytest.fail("停止准入后不能派发原调用")
        await old_close()

    # 只观察真实关闭回调的顺序，不代替 scope 或 session 生命周期。
    payload = config("old")
    payload["servers"]["alpha"] = {"transport": "stdio", "command": "stdio-new", "enabled": True}
    with monkeypatch.context() as patch:
        patch.setattr(old.scope, "close", observe_close)
        publish, _ = await case.prepare(payload)
    assert old.scope.is_closed and case.factory.history.index(("close", "stdio-old")) < case.factory.history.index(("open", "stdio-new"))
    publish()
    assert case.owner.publication_snapshot().status == "available"
    assert case.owner.binding_snapshot().resolve(binding.target_id).connection_ref != binding.connection_ref
    with pytest.raises(ExtensionTargetResolutionError), case.owner.execution_handles.execution_target(binding, use):
        pytest.fail("新 stdio 不能接管原封存调用")


@pytest.mark.parametrize("replacement_case", ["stdio"], indirect=True)
async def test_http_candidate_failure_does_not_stop_unchanged_or_replaced_stdio(replacement_case):
    case = replacement_case
    before = case.owner.publication_snapshot()
    old = case.owner._runtimes["alpha"]
    case.factory.fail_server = "beta"
    payload = config("new")
    payload["servers"]["alpha"] = {"transport": "stdio", "command": "stdio-new", "enabled": True}
    with pytest.raises(McpCatalogError, match="server_id=beta"):
        await case.prepare(payload)
    await case.scopes[-1].close()
    assert old.running and case.owner.publication_snapshot() is before and not case.effects


async def test_turn_inherits_original_qualification_without_granting_another_execution(replacement_case):
    case = replacement_case
    scope = LifetimeScope("original-execution")
    case.scopes.append(scope)
    owner = case.owner
    binding = owner.binding_snapshot()
    old = owner._runtimes["alpha"]
    with owner.execution_handles.reserve_model_call(
        binding, session_id="session", thread_id="thread", model_call_id="first-model",
        execution_id="original-execution", execution_scope=scope,
    ) as retain:
        retain([])
    original, = [lease for lease in owner._leases.leases_for_resource(binding.resolve("mcp__alpha__echo").connection_ref)
                 if lease.lease_id.startswith("mcp-execution:")]
    publish, _ = await case.prepare(config("new"))
    publish()
    with owner.execution_handles.reserve_model_call(
        binding, session_id="session", thread_id="thread", model_call_id="next-model",
        execution_id="original-execution", execution_scope=scope,
    ) as retain:
        prepared, = [lease for lease in owner._leases.leases_for_resource(original.resource_id)
                     if lease.lease_id.startswith("mcp-prepared:")]
        assert prepared.expires_at == original.expires_at
        retain([])
    other_scope = LifetimeScope("other-execution")
    case.scopes.append(other_scope)
    with pytest.raises(ExtensionTargetResolutionError), owner.execution_handles.reserve_model_call(
        binding, session_id="session", thread_id="thread", model_call_id="other-model",
        execution_id="other-execution", execution_scope=other_scope,
    ):
        pytest.fail("其它执行不能继承原 Turn 的旧代际资格")
    assert old.running
    await scope.close()
    await wait_closed(old)
    assert owner._leases.get_lease(original.lease_id).status == "released"


@pytest.mark.parametrize("replacement_case", [1], indirect=True)
async def test_expired_execution_can_finish_model_response_without_renewing_tool_qualification(replacement_case):
    case = replacement_case
    owner = case.owner
    scope = LifetimeScope("expired-execution")
    case.scopes.append(scope)
    binding = owner.binding_snapshot()
    target = binding.resolve("mcp__alpha__echo")
    with owner.execution_handles.reserve_model_call(
        binding, session_id="session", thread_id="thread", model_call_id="before-expiry",
        execution_id="expired-execution", execution_scope=scope,
    ) as retain:
        retain([])
    qualification, = owner._leases.leases_for_resource(target.connection_ref)
    await asyncio.sleep(1.1)
    assert owner._leases.get_lease(qualification.lease_id).status == "released"
    with owner.execution_handles.reserve_model_call(
        binding, session_id="session", thread_id="thread", model_call_id="after-expiry",
        execution_id="expired-execution", execution_scope=scope,
    ) as retain:
        retain([{"name": "invoke_extension_tool", "id": "late-call", "args": {"tool_name": target.target_id}}])
    assert not owner._leases.leases_for_resource(target.connection_ref)
    use = McpResourceUse("holder", software_tool_operation_id("mcp_call", "session", "thread", "after-expiry", "late-call"),
                         "session", "thread", "after-expiry")
    with pytest.raises(ExtensionTargetResolutionError, match="资格已失效"), owner.execution_handles.execution_target(target, use):
        pytest.fail("后继模型调用不能重新获得已到期执行的工具资格")
    assert owner._runtimes["alpha"].running
