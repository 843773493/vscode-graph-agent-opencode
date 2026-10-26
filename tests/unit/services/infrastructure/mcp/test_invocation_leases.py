"""原 Owner 的连接登记、持久 operation 占用和冷启动核对。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime

import pytest

from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
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
