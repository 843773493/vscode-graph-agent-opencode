"""原 Owner 的连接登记、持久 operation 占用和冷启动核对。"""

from __future__ import annotations

import pytest

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
async def lease_case(tmp_path):
    path = tmp_path / "resources.json"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    owner = McpCatalogOwner(
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "fixture"}}},
        workspace_root=tmp_path, event_service=EventChannelService(), leases=ledger,
        session_factory=FakeMcpSessionFactory({"mini": ["echo"]}),
    )
    await owner.start()
    try:
        yield owner, ledger, path
    finally:
        await owner.shutdown()


@pytest.fixture
def resource_use():
    return McpResourceUse(holder_id="original-holder", operation_id="original-operation",
                          session_id="original-session", thread_id="original-thread")


def test_connection_registration_and_exact_cold_occupancy(lease_case, resource_use):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    record, = ledger.records_by_kind("mcp_connection")
    assert (record.resource_id, record.kind, record.lifetime_scope, record.status) == (
        binding.connection_ref, "mcp_connection", "workspace", "running",
    )
    with owner.execution_target(binding, resource_use) as execution:
        lease, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(record.resource_id)
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
    with owner.execution_target(binding, resource_use) as execution:
        execution.started, execution.result_confirmed = started, confirmed
    lease, = ledger.leases_for_turn(resource_use.holder_id)
    status = "reconcile_required" if started and not confirmed else "released"
    assert lease.status == status
    ledger.release_turn_leases(resource_use.holder_id)
    assert (retained := ExternalResourceLeaseLedger(state_path=path).get_lease(lease.lease_id)) is not None and retained.status == status
    with pytest.raises(ExtensionTargetResolutionError, match="禁止自动重放"), owner.execution_target(binding, resource_use):
        pytest.fail("同一 operation 不能二次执行远端副作用")


async def test_restart_keeps_unknown_operation_and_rejects_original_handle(lease_case, resource_use):
    owner, ledger, path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with owner.execution_target(binding, resource_use) as execution:
        execution.started = True
    lease, = ledger.leases_for_turn(resource_use.holder_id)
    await owner.shutdown()
    assert ledger.get(binding.connection_ref).status == "stopped"
    assert ledger.get_lease(lease.lease_id).status == "reconcile_required"
    await owner.start()
    fresh = owner.binding_snapshot().resolve("mcp__mini__echo")
    assert fresh.schema_hash == binding.schema_hash and fresh.connection_ref != binding.connection_ref
    with pytest.raises(ExtensionTargetResolutionError, match="原连接"), owner.execution_target(binding, resource_use):
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
    await owner.start()
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert (retained := cold.get("lost-connection")) is not None and retained.status == "orphaned"
    assert cold.leases_for_resource("lost-connection")[0].status == "reconcile_required"
    assert (retained := cold.get("running-terminal")) is not None and retained.status == "running"
    assert cold.leases_for_resource("running-terminal")[0].status == "active"


def test_missing_use_or_failed_admission_does_not_issue_a_handle(lease_case, resource_use, monkeypatch):
    owner, ledger, _path = lease_case
    binding = owner.binding_snapshot().resolve("mcp__mini__echo")
    with pytest.raises(ExtensionTargetResolutionError, match="typed resource-use"), owner.execution_target(binding, None):
        pytest.fail("没有受信归属不能占用连接")

    def fail(**_updates):
        raise OSError("测试账本写盘失败")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_commit", fail)
        with pytest.raises(OSError, match="写盘失败"), owner.execution_target(binding, resource_use):
            pytest.fail("占用持久化失败不能发送远端请求")
    assert not ledger.leases_for_resource(binding.connection_ref)


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
        with pytest.raises(OSError, match="释放写盘失败"), owner.execution_target(binding, resource_use) as execution:
            execution.started = execution.result_confirmed = True
    lease, = ExternalResourceLeaseLedger(state_path=path).leases_for_resource(binding.connection_ref)
    assert lease.status == "active"
    with pytest.raises(ExtensionTargetResolutionError, match="禁止自动重放"), owner.execution_target(binding, resource_use):
        pytest.fail("写盘失败后不能重发已确认的远端操作")
