"""原 Owner 的连接登记、持久 operation 占用和冷启动核对。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
    ResourceLease,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.invocation import McpResourceUse
from tests.support.mcp.session_doubles import FakeMcpSessionFactory


@pytest.fixture
async def lease_case(tmp_path, resource_use, request):
    path = tmp_path / "resources.json"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    owner = McpCatalogOwner(
        raw_config={"supervision": {"sealed_call_timeout_seconds": getattr(request, "param", 1800)},
                    "servers": {"mini": {"enabled": True, "transport": "stdio", "command": "fixture"}}},
        workspace_root=tmp_path, event_service=EventChannelService(), leases=ledger,
        session_factory=FakeMcpSessionFactory({"mini": ["echo", "other"]}),
    )
    await owner.start()
    with owner.execution_handles.reserve_model_call(
        owner.binding_snapshot(), session_id=resource_use.session_id,
        thread_id=resource_use.thread_id, model_call_id=resource_use.model_call_id,
    ) as retain:
        retain([{"name": "invoke_extension_tool", "id": "original-call", "args": {"tool_name": "mcp__mini__echo"}}])
    try:
        yield owner, ledger, path
    finally:
        await owner.shutdown()


@pytest.fixture
def resource_use():
    return McpResourceUse(holder_id="original-holder", operation_id=software_tool_operation_id("mcp_call", "original-session", "original-thread", "original-model", "original-call"),
                          session_id="original-session", thread_id="original-thread", model_call_id="original-model")


@pytest.fixture
def committed_result():
    def result(*, model_call_id="original-model", call_id="original-call", outcome="failure", status="completed"):
        return CanonicalItemRecord.create(
            item_sequence=1, item_id=f"result-{model_call_id}-{call_id}",
            semantic_kind="tool_result", payload_kind="tool_result", status=status,
            producer_ref={"producer_kind": "tool", "producer_id": "attempt", "invocation_id": model_call_id},
            payload={"tool_call_id": f"{model_call_id}:tool-call:{call_id}", "result_id": "attempt",
                     "name": "invoke_extension_tool", "content": "调用已结算",
                     "tool_outcome": outcome, "execution_confirmed": outcome != "unknown"},
            projection_identity=ItemProjectionIdentity(model_call_id=model_call_id), wire_role="tool",
        )
    return result


def test_committed_refusal_releases_only_the_original_qualification(lease_case, resource_use, committed_result):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    qualification, = ledger.leases_for_resource(binding.connection_ref)
    for session, thread, item in (
        ("other-session", resource_use.thread_id, committed_result()),
        (resource_use.session_id, "other-thread", committed_result()),
        (resource_use.session_id, resource_use.thread_id, committed_result(model_call_id="other-model")),
        (resource_use.session_id, resource_use.thread_id, committed_result(call_id="other-call")),
        (resource_use.session_id, resource_use.thread_id, committed_result(status="partial", outcome="unknown")),
    ):
        owner.execution_handles.acknowledge_tool_results(session, thread, (item,))
        retained = ledger.get_lease(qualification.lease_id)
        assert retained is not None and retained.status == "active"
    owner.execution_handles.acknowledge_tool_results(resource_use.session_id, resource_use.thread_id, (committed_result(),))
    cold = ExternalResourceLeaseLedger(state_path=path)
    retained = cold.get_lease(qualification.lease_id)
    assert retained is not None and retained.status == "released"
    assert not cold.leases_for_resource(binding.connection_ref)
    resource = cold.get(binding.connection_ref)
    assert resource is not None and resource.status == "running"
    with pytest.raises(ExtensionTargetResolutionError, match="资格已失效"), owner.execution_handles.execution_target(binding, resource_use):
        pytest.fail("已提交拒绝结果不能再次使用原资格")


def test_committed_unknown_does_not_confirm_an_active_external_operation(lease_case, resource_use, committed_result):
    owner, _ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with owner.execution_handles.execution_target(binding, resource_use) as execution:
        execution.started = True
        owner.execution_handles.acknowledge_tool_results(resource_use.session_id, resource_use.thread_id, (committed_result(outcome="unknown"),))
        lease, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(binding.connection_ref)
        assert lease.lease_id.startswith("mcp-invocation:") and lease.status == "active"
    retained = ExternalResourceLeaseLedger(state_path=path).get_lease(lease.lease_id)
    assert retained is not None and retained.status == "reconcile_required"


@pytest.mark.parametrize("invoked", [False, True])
async def test_execution_close_releases_future_qualifications_but_keeps_unknown_operation(lease_case, invoked):
    owner, ledger, path = lease_case
    snapshot = owner.binding_snapshot()
    target = snapshot.resolve("mcp__mini__echo")
    scope = LifetimeScope("cancelled-execution")
    for model in ("waiting-first", "waiting-second"):
        with owner.execution_handles.reserve_model_call(
            snapshot, session_id="cancelled-session", thread_id="cancelled-thread", model_call_id=model,
            execution_id="cancelled-execution", execution_scope=scope,
        ) as retain:
            retain([{"name": "invoke_extension_tool", "id": "waiting-call", "args": {"tool_name": target.target_id}}])
    use = McpResourceUse("cancelled-holder", software_tool_operation_id(
        "mcp_call", "cancelled-session", "cancelled-thread", "waiting-first", "waiting-call",
    ), "cancelled-session", "cancelled-thread", "waiting-first")
    if invoked:
        with owner.execution_handles.execution_target(target, use) as execution:
            execution.started = True
            await scope.close()
            active = [lease for lease in ledger.leases_for_resource(target.connection_ref)
                      if lease.holder_session_id == "cancelled-session"]
            assert len(active) == 1 and active[0].lease_id.startswith("mcp-invocation:")
    else:
        await scope.close()
    cold = ExternalResourceLeaseLedger(state_path=path)
    retained = [lease for lease in cold.leases_for_resource(target.connection_ref)
                if lease.holder_session_id == "cancelled-session"]
    assert len(retained) == int(invoked)
    if invoked:
        assert retained[0].status == "reconcile_required"
    with pytest.raises(ExtensionTargetResolutionError), owner.execution_handles.execution_target(target, use):
        pytest.fail("执行关闭后不能消费审批资格或重放未知调用")
    # 关闭一个执行不能释放兄弟执行／无该 scope 的原资格。
    assert any(lease.holder_session_id == "original-session" for lease in cold.leases_for_resource(target.connection_ref))


def test_committed_local_edit_or_rejection_does_not_release_sibling_call(lease_case, committed_result):
    owner, ledger, _path = lease_case
    binding = owner.binding_snapshot()
    with owner.execution_handles.reserve_model_call(
        binding, session_id="parallel-session", thread_id="parallel-thread", model_call_id="parallel-model",
    ) as retain:
        retain([{"name": "invoke_extension_tool", "id": call, "args": {"tool_name": "mcp__mini__echo"}}
                for call in ("first", "second")])
    qualification, = [lease for lease in ledger.leases_for_resource(binding.resolve("mcp__mini__echo").connection_ref)
                      if lease.holder_session_id == "parallel-session"]
    first = committed_result(model_call_id="parallel-model", call_id="first", outcome="success")
    owner.execution_handles.acknowledge_tool_results("parallel-session", "parallel-thread", (first, first))
    assert ledger.get_lease(qualification.lease_id).status == "active"
    use = McpResourceUse("holder", software_tool_operation_id("mcp_call", "parallel-session", "parallel-thread", "parallel-model", "second"),
                         "parallel-session", "parallel-thread", "parallel-model")
    with owner.execution_handles.execution_target(binding.resolve("mcp__mini__other"), use) as execution:
        execution.started = execution.result_confirmed = True
    assert ledger.get_lease(qualification.lease_id).status == "released"


def test_connection_registration_and_exact_cold_occupancy(lease_case, resource_use):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    record, = ledger.records_by_kind("mcp_connection")
    assert (record.resource_id, record.kind, record.lifetime_scope, record.status) == (
        binding.connection_ref, "mcp_connection", "workspace", "running",
    )
    with owner.execution_handles.execution_target(binding, resource_use) as execution:
        lease, = [lease for lease in ExternalResourceLeaseLedger(state_path=path).leases_for_resource(record.resource_id)
                  if lease.lease_id.startswith("mcp-invocation:")]
        assert (lease.turn_stream_id, lease.operation_id, lease.holder_session_id, lease.holder_thread_id) == (
            resource_use.holder_id, resource_use.operation_id, resource_use.session_id, resource_use.thread_id,
        )
        execution.started = execution.result_confirmed = True
    assert ledger.get_lease(lease.lease_id).status == "released"
    assert not ledger.leases_for_resource(record.resource_id)
    assert ledger.get(record.resource_id).status == "running"


@pytest.mark.parametrize("started,confirmed", [(False, False), (True, True), (True, False)])
def test_invocation_exit_distinguishes_unsent_confirmed_and_unknown(lease_case, resource_use, started, confirmed):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with owner.execution_handles.execution_target(binding, resource_use) as execution:
        execution.started, execution.result_confirmed = started, confirmed
    lease, = ledger.leases_for_turn(resource_use.holder_id)
    status = "reconcile_required" if started and not confirmed else "released"
    assert lease.status == status
    ledger.release_turn_leases(resource_use.holder_id)
    assert (retained := ExternalResourceLeaseLedger(state_path=path).get_lease(lease.lease_id)) is not None and retained.status == status
    with pytest.raises(ExtensionTargetResolutionError, match="禁止自动重放"), owner.execution_handles.execution_target(binding, resource_use):
        pytest.fail("同一 operation 不能二次执行远端副作用")


async def test_restart_keeps_unknown_operation_and_rejects_original_handle(lease_case, resource_use):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with owner.execution_handles.execution_target(binding, resource_use) as execution:
        execution.started = True
    lease, = ledger.leases_for_turn(resource_use.holder_id)
    await owner.shutdown()
    assert ledger.get(binding.connection_ref).status == "stopped"
    assert ledger.get_lease(lease.lease_id).status == "reconcile_required"
    await owner.start()
    fresh = owner.binding_snapshot().resolve("mcp__mini__echo")
    assert fresh.schema_hash == binding.schema_hash and fresh.connection_ref != binding.connection_ref
    with pytest.raises(ExtensionTargetResolutionError, match="禁止自动重放"), owner.execution_handles.execution_target(binding, resource_use):
        pytest.fail("旧调用不能迁投到相同 schema 的新连接")
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert (retained := cold.get(binding.connection_ref)) is not None and retained.status == "stopped"
    assert (retained := cold.get_lease(lease.lease_id)) is not None and retained.status == "reconcile_required"
    assert (retained := cold.get(fresh.connection_ref)) is not None and retained.status == "running"


async def test_startup_reconciles_only_previous_mcp_instances(lease_case):
    owner, ledger, path = lease_case
    await owner.shutdown()
    for resource_id, kind in (("lost-connection", "mcp_connection"), ("running-terminal", "terminal")):
        ledger.register_external(resource_id=resource_id, kind=kind, lifetime_scope="workspace")
        ledger.acquire_operation(resource_id=resource_id, turn_stream_id="lost-holder", operation_id=resource_id)
        ledger.acquire_operation(
            resource_id=resource_id, turn_stream_id="prepared-holder", operation_id="prepared",
            lease_id="mcp-prepared:v1:" + resource_id, expires_at="2026-10-26T01:30:00+00:00",
        )
    await owner.start()
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert (retained := cold.get("lost-connection")) is not None and retained.status == "orphaned"
    assert cold.leases_for_resource("lost-connection")[0].status == "reconcile_required"
    assert len(cold.leases_for_resource("lost-connection")) == 1
    assert (lease := cold.get_lease("mcp-prepared:v1:lost-connection")) is not None and lease.status == "released"
    assert (retained := cold.get("running-terminal")) is not None and retained.status == "running"
    assert cold.leases_for_resource("running-terminal")[0].status == "active"
    assert (lease := cold.get_lease("mcp-prepared:v1:running-terminal")) is not None and lease.status == "active"


def test_missing_use_or_failed_admission_does_not_issue_a_handle(lease_case, resource_use, monkeypatch):
    owner, ledger, _path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with pytest.raises(ExtensionTargetResolutionError, match="typed resource-use"), owner.execution_handles.execution_target(binding, None):
        pytest.fail("没有受信归属不能占用连接")

    def fail(**_updates):
        raise OSError("测试账本写盘失败")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_commit", fail)
        with pytest.raises(OSError, match="写盘失败"), owner.execution_handles.execution_target(binding, resource_use):
            pytest.fail("占用持久化失败不能发送远端请求")
    assert {lease.lease_id.split(":")[0] for lease in ledger.leases_for_resource(binding.connection_ref)} == {"mcp-prepared"}


def test_failed_completion_write_preserves_original_lease(lease_case, resource_use, monkeypatch):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    original = ledger._commit

    def fail_release(**updates):
        if any(lease.status == "released" for lease in updates.get("leases", ())):
            raise OSError("测试释放写盘失败")
        original(**updates)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_commit", fail_release)
        with pytest.raises(OSError, match="释放写盘失败"), owner.execution_handles.execution_target(binding, resource_use) as execution:
            execution.started = execution.result_confirmed = True
    lease, = [lease for lease in ExternalResourceLeaseLedger(state_path=path).leases_for_resource(binding.connection_ref)
              if lease.lease_id.startswith("mcp-invocation:")]
    assert lease.status == "active"
    with pytest.raises(ExtensionTargetResolutionError, match="禁止自动重放"), owner.execution_handles.execution_target(binding, resource_use):
        pytest.fail("写盘失败后不能重发已确认的远端操作")


def test_sealed_qualification_is_durable_without_starting_an_operation(lease_case, resource_use):
    owner, _ledger, path = lease_case
    target = owner.binding_snapshot().resolve("mcp__mini__echo")
    cold = ExternalResourceLeaseLedger(state_path=path)
    qualification, = cold.leases_for_resource(target.connection_ref)
    assert qualification.lease_id.startswith("mcp-prepared:")
    assert qualification.operation_id == resource_use.model_call_id
    assert qualification.turn_stream_id == qualification.lease_id
    assert (qualification.holder_session_id, qualification.holder_thread_id) == (resource_use.session_id, resource_use.thread_id)
    assert qualification.status == "active"
    assert qualification.expires_at is not None
    assert datetime.fromisoformat(qualification.expires_at) > datetime.fromisoformat(qualification.updated_at)
    with pytest.raises(ExtensionTargetResolutionError, match="不可重复发放"), owner.execution_handles.reserve_model_call(
        owner.binding_snapshot(), session_id=resource_use.session_id,
        thread_id=resource_use.thread_id, model_call_id=resource_use.model_call_id,
    ):
        pytest.fail("重入不能延长已封存资格")
    retained = ExternalResourceLeaseLedger(state_path=path).get_lease(qualification.lease_id)
    assert retained is not None and retained.expires_at == qualification.expires_at


@pytest.mark.parametrize("owner_field", ["session_id", "thread_id", "model_call_id", "operation_id"])
def test_sealed_qualification_rejects_wrong_owner_or_unemitted_call(lease_case, resource_use, owner_field):
    owner, ledger, _path = lease_case
    target = owner.binding_snapshot().resolve("mcp__mini__echo")
    incorrect = replace(resource_use, **{owner_field: "different-identity"})
    with pytest.raises(ExtensionTargetResolutionError), owner.execution_handles.execution_target(target, incorrect):
        pytest.fail("资格不能跨线程或为未输出的调用使用")
    assert all(lease.lease_id.startswith("mcp-prepared:") for lease in ledger.leases_for_resource(target.connection_ref))
    with owner.execution_handles.execution_target(target, resource_use) as execution:
        execution.started = execution.result_confirmed = True


def test_confirmation_edit_keeps_original_catalog_target_choices(lease_case, resource_use):
    owner, _ledger, _path = lease_case
    edited_target = owner.binding_snapshot().resolve("mcp__mini__other")
    with owner.execution_handles.execution_target(edited_target, resource_use) as execution:
        assert execution.tool.name == edited_target.target_id
        execution.started = execution.result_confirmed = True


@pytest.mark.parametrize("lease_case", [1], indirect=True)
async def test_qualification_expires_without_a_tool_call_and_cannot_be_renewed(lease_case, resource_use):
    owner, ledger, path = lease_case
    target = owner.binding_snapshot().resolve("mcp__mini__echo")
    qualification, = ledger.leases_for_resource(target.connection_ref)
    await asyncio.sleep(1.1)
    retained = ExternalResourceLeaseLedger(state_path=path).get_lease(qualification.lease_id)
    assert retained is not None and retained.status == "released"
    assert not ledger.leases_for_resource(target.connection_ref)
    with pytest.raises(ExtensionTargetResolutionError, match="资格已失效"), owner.execution_handles.execution_target(target, resource_use):
        pytest.fail("未确认调用不能在原期限外开始")
    with pytest.raises(ExtensionTargetResolutionError, match="不能重新发放"), owner.execution_handles.reserve_model_call(
        owner.binding_snapshot(), session_id=resource_use.session_id, thread_id=resource_use.thread_id,
        model_call_id=resource_use.model_call_id,
    ):
        pytest.fail("同一封存调用不能重新计时")


@pytest.mark.parametrize("lease_case", [1], indirect=True)
async def test_expired_qualification_does_not_settle_an_executing_unknown_operation(lease_case, resource_use):
    owner, ledger, path = lease_case
    target = owner.binding_snapshot().resolve("mcp__mini__echo")
    with owner.execution_handles.execution_target(target, resource_use) as execution:
        execution.started = True
        await asyncio.sleep(1.1)
        active, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(target.connection_ref)
        assert active.lease_id.startswith("mcp-invocation:") and active.status == "active"
    active, = ledger.leases_for_resource(target.connection_ref)
    assert active.status == "reconcile_required"
    assert ledger.get(target.connection_ref).status == "running"


@pytest.mark.parametrize("provider_failed", [False, True])
def test_failed_or_non_tool_model_response_releases_prepared_handles(lease_case, provider_failed):
    owner, ledger, _path = lease_case
    snapshot = owner.binding_snapshot()
    qualification = None
    expected_error = pytest.raises(OSError, match="模型响应失败") if provider_failed else nullcontext()
    with (
        expected_error,
        owner.execution_handles.reserve_model_call(
            snapshot, session_id="other-session", thread_id="other-thread", model_call_id="other-model",
        ) as retain,
    ):
        qualification, = [lease for lease in ledger.leases_for_resource(snapshot.resolve("mcp__mini__echo").connection_ref)
                          if lease.holder_session_id == "other-session"]
        if provider_failed:
            raise OSError("测试模型响应失败")
        retain([])
    assert qualification is not None
    retained = ledger.get_lease(qualification.lease_id)
    assert retained is not None and retained.status == "released"


def test_sibling_calls_hold_the_same_original_connection_until_both_settle(lease_case):
    owner, ledger, path = lease_case
    snapshot = owner.binding_snapshot()
    calls = [{"name": "invoke_extension_tool", "id": call_id, "args": {"tool_name": target_id}}
             for call_id, target_id in (("first", "mcp__mini__echo"), ("second", "mcp__mini__other"))]
    with owner.execution_handles.reserve_model_call(
        snapshot, session_id="parallel-session", thread_id="parallel-thread", model_call_id="parallel-model",
    ) as retain:
        retain(calls)
    operations: list[str] = []
    for index, call in enumerate(calls):
        operation = software_tool_operation_id("mcp_call", "parallel-session", "parallel-thread", "parallel-model", call["id"])
        operations.append(operation)
        use = McpResourceUse(operation, operation, "parallel-session", "parallel-thread", "parallel-model")
        with owner.execution_handles.execution_target(snapshot.resolve(call["args"]["tool_name"]), use) as execution:
            execution.started = execution.result_confirmed = True
        prepared = [lease for lease in ExternalResourceLeaseLedger(state_path=path).leases_for_resource(snapshot.resolve("mcp__mini__echo").connection_ref)
                    if lease.holder_session_id == "parallel-session"]
        assert len(prepared) == (1 if index == 0 else 0)
    for operation in operations:
        invocation, = ledger.leases_for_turn(operation)
        assert invocation.status == "released"


async def test_unexecuted_qualification_does_not_survive_connection_restart(lease_case, resource_use):
    owner, ledger, path = lease_case
    original = owner.binding_snapshot().resolve("mcp__mini__echo")
    qualification, = ledger.leases_for_resource(original.connection_ref)
    await owner.shutdown()
    await owner.start()
    current = owner.binding_snapshot().resolve("mcp__mini__echo")
    assert current.schema_hash == original.schema_hash and current.connection_ref != original.connection_ref
    for target in (original, current):
        with pytest.raises(ExtensionTargetResolutionError, match="资格已失效"), owner.execution_handles.execution_target(target, resource_use):
            pytest.fail("重启不能把等待中的原调用迁投新同名连接")
    cold = ExternalResourceLeaseLedger(state_path=path)
    retained = cold.get_lease(qualification.lease_id)
    assert retained is not None and retained.status == "released"
    assert not cold.leases_for_turn(resource_use.holder_id)


@dataclass
class StartupRecoveryWrite:
    owner: McpCatalogOwner
    ledger: ExternalResourceLeaseLedger
    path: Path
    factory: FakeMcpSessionFactory
    leases: tuple[ResourceLease, ...]
    blocked_index: int
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    threads: list[int] = field(default_factory=list)
    failure: OSError | None = None


@pytest.fixture
async def startup_recovery_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> AsyncIterator[StartupRecoveryWrite]:
    path = tmp_path / "resources.json"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    # 局部启动恢复合同直接经唯一账本准备占用，不冒称真实模型或进程崩溃验收。
    for resource_id, kind in (("old-mcp", "mcp_connection"), ("peer-terminal", "terminal")):
        ledger.register_external(resource_id=resource_id, kind=kind, lifetime_scope="workspace")
    pending = tuple(ledger.acquire_operation(
        resource_id=resource_id, turn_stream_id=lease_id, operation_id=lease_id, lease_id=lease_id,
    ) for resource_id, lease_id in (("old-mcp", "mcp-prepared:v1:prepared"),
                                   ("old-mcp", "mcp-execution:v1:execution"),
                                   ("old-mcp", "mcp-invocation:v1:unknown"),
                                   ("peer-terminal", "mcp-prepared:v1:peer")))
    ledger.mark_operation_dispatched(pending[2].lease_id)
    dispatched = ledger.get_lease(pending[2].lease_id)
    assert dispatched is not None and dispatched.status == "dispatching"
    pending = (pending[0], pending[1], dispatched, pending[3])
    factory = FakeMcpSessionFactory({"mini": ["echo"]})
    owner = McpCatalogOwner(
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "fixture"}}},
        workspace_root=tmp_path, event_service=EventChannelService(), leases=ledger, session_factory=factory,
    )
    case = StartupRecoveryWrite(owner, ledger, path, factory, pending, request.param)
    publishing, loop = Path.replace, asyncio.get_running_loop()
    close_handles, loop_thread = owner.execution_handles.close, get_ident()

    def close_on_loop() -> None:
        assert get_ident() == loop_thread, "asyncio定时器及内存handle清理应留在原事件循环"
        close_handles()

    def publish(source: Path, target: Path) -> Path:
        if target == path:
            candidate = json.loads(source.read_text())
            if any(item["lease_id"] == pending[case.blocked_index].lease_id and item["status"] == "released"
                   for item in candidate["leases"]) and not case.entered.is_set():
                case.threads.append(get_ident())
                loop.call_soon_threadsafe(case.entered.set)
                if not case.release.wait(4):
                    raise TimeoutError("测试未放行原MCP冷恢复写入，事件循环可能被阻塞")
                if case.failure is not None:
                    raise case.failure
        return publishing(source, target)

    with monkeypatch.context() as observe:
        observe.setattr(Path, "replace", publish)
        observe.setattr(owner.execution_handles, "close", close_on_loop)
        try:
            yield case
        finally:
            case.release.set()
    await owner.shutdown()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
@pytest.mark.parametrize("startup_recovery_write", [0, 1], indirect=True)
async def test_cold_startup_settles_original_writer_before_admission_or_cancellation(
    startup_recovery_write: StartupRecoveryWrite, cancel: bool, write_fails: bool,
) -> None:
    case = startup_recovery_write
    original_bytes = case.path.read_bytes()
    case.failure = OSError(28, "原MCP启动资格释放失败", str(case.path)) if write_fails else None
    startup = asyncio.create_task(case.owner.start())
    try:
        await asyncio.wait_for(case.entered.wait(), 2)
        before = case.path.read_bytes()
        prior_commit = case.ledger.get_lease(case.leases[0].lease_id)
        assert prior_commit is not None
        if case.blocked_index == 0:
            assert before == original_bytes and prior_commit == case.leases[0]
        else:
            assert prior_commit.status == "released" and before != original_bytes
        assert case.threads and all(thread != get_ident() for thread in case.threads)
        # 真实目录读取取得同一publication锁；慢账本写入不能占住它。
        with pytest.raises(RuntimeError, match="尚未启动"):
            case.owner.get_tool_ids()
        assert not startup.done() and not case.owner.started and case.factory.sessions == {}
        assert case.path.read_bytes() == before
        if cancel:
            startup.cancel("原MCP启动恢复取消")
            for _ in range(5):
                await asyncio.sleep(0)
            startup.cancel("重复取消不得遗弃原回收")
            await asyncio.sleep(0)
            assert not startup.done() and case.path.read_bytes() == before and case.factory.sessions == {}
        case.release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as caught:
                await startup
            assert caught.value.args == ("原MCP启动恢复取消",)
            failure = caught.value.__cause__
        elif write_fails:
            with pytest.raises(OSError) as caught_failure:
                await startup
            failure = caught_failure.value
        else:
            await startup
            failure = None
        if write_fails:
            assert isinstance(failure, OSError) and failure.__cause__ is case.failure
            assert case.path.read_bytes() == before
        else:
            assert failure is None
        cold = ExternalResourceLeaseLedger(state_path=case.path)
        for index, lease in enumerate(case.leases):
            retained = cold.get_lease(lease.lease_id)
            assert retained is not None
            status = ("released" if index < case.blocked_index or index < 2 and not write_fails else lease.status)
            if not cancel and not write_fails and index == 2:
                status = "reconcile_required"
            assert retained == replace(lease, status=status, updated_at=retained.updated_at)
        if cancel or write_fails:
            assert not case.owner.started and case.factory.sessions == {} and case.owner._session_scope is None
            case.failure = None
            await case.owner.start()
        assert case.owner.started and case.owner.get_tool_ids() == {"mcp__mini__echo"}
        assert len(case.factory.sessions) == 1
        cold = ExternalResourceLeaseLedger(state_path=case.path)
        for lease in case.leases[:2]:
            retained = cold.get_lease(lease.lease_id)
            assert retained is not None and retained.status == "released"
        if case.blocked_index == 1:
            assert cold.get_lease(case.leases[0].lease_id) == prior_commit, "显式重试不重写已成功的资格释放"
        unknown = cold.get_lease(case.leases[2].lease_id)
        assert unknown is not None and unknown.status == "reconcile_required"
        assert cold.get_lease(case.leases[3].lease_id) == case.leases[3]
        resource = cold.get("old-mcp")
        assert resource is not None and resource.status == "orphaned"
    finally:
        case.release.set()
        await asyncio.gather(startup, return_exceptions=True)
