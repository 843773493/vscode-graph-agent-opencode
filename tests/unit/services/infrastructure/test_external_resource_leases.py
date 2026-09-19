from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


def _ledger_with_events(tmp_path: Path) -> tuple[
    ExternalResourceLeaseLedger,
    EventChannelService,
]:
    """带 resource.state 通知出口的账本与同一事件服务（供订阅断言）。"""
    service = EventChannelService()
    ledger = ExternalResourceLeaseLedger(
        state_path=tmp_path / "resources.json",
        state_events=ResourceStateEventPublisher(
            event_service=service,
            owner_domain="external_resource_leases",
        ),
    )
    return ledger, service


@pytest.mark.asyncio
async def test_holder_settle_publishes_released_but_turn_release_does_not(
    tmp_path: Path,
) -> None:
    """Turn 收尾释放不发事件；owner 核实终态的 settle 才发布 released。"""
    ledger, service = _ledger_with_events(tmp_path)
    subscription = service.channel(
        "resource.state/external_resource_leases"
    ).subscribe(label="test")
    ledger.register(
        resource_id="terminal_1",
        kind="terminal",
        lifetime_scope="turn",
    )
    ledger.acquire(
        resource_id="terminal_1",
        turn_stream_id="stream_1",
        lease_id="lease_1",
        operation_id="op_1",
    )
    ledger.acquire(
        resource_id="terminal_1",
        turn_stream_id="stream_2",
        lease_id="lease_2",
        operation_id="op_2",
    )
    ledger.release("lease_1", reason="turn_cancelled")
    assert subscription.pending() == ()
    settled = ledger.settle("lease_2")
    assert settled.status == "settled"
    deliveries = subscription.pending()
    assert [delivery.event.state for delivery in deliveries] == ["released"]
    assert [delivery.event.resource_id for delivery in deliveries] == ["terminal_1"]
    assert [delivery.event.owner_domain for delivery in deliveries] == [
        "external_resource_leases"
    ]


@pytest.mark.asyncio
async def test_reconcile_required_publishes_unavailable_never_released(
    tmp_path: Path,
) -> None:
    """崩溃对账缺失证据：占用进入 reconcile_required，事件绝不虚报 released。"""
    ledger, service = _ledger_with_events(tmp_path)
    subscription = service.channel(
        "resource.state/external_resource_leases"
    ).subscribe(label="test")
    ledger.register(
        resource_id="mcp_1",
        kind="mcp_connection",
        lifetime_scope="workspace",
    )
    ledger.acquire(
        resource_id="mcp_1",
        turn_stream_id="stream_1",
        lease_id="lease_1",
        operation_id="op_1",
    )
    reconciled = ledger.reconcile({})
    assert reconciled[0].status == "orphaned"
    assert ledger.get_lease("lease_1").status == "reconcile_required"
    deliveries = subscription.pending()
    assert [delivery.event.state for delivery in deliveries] == ["unavailable"]
    assert [delivery.event.resource_id for delivery in deliveries] == ["mcp_1"]


@pytest.mark.asyncio
async def test_event_unavailable_keeps_durable_truth_and_records_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """事件通道不可用：durable 事实不变，错误显式记录且可从账本读取。"""
    service = EventChannelService()
    def fail_publish(_event) -> None:
        raise RuntimeError("event unavailable")

    # publisher 构造即 ensure channel；先建 publisher 再 patch 同一 channel。
    publisher = ResourceStateEventPublisher(
        event_service=service,
        owner_domain="external_resource_leases",
    )
    channel = service.channel("resource.state/external_resource_leases")
    monkeypatch.setattr(channel, "publish", fail_publish)
    ledger = ExternalResourceLeaseLedger(
        state_path=tmp_path / "resources.json",
        state_events=publisher,
    )
    ledger.register(
        resource_id="terminal_1",
        kind="terminal",
        lifetime_scope="turn",
    )
    ledger.acquire(
        resource_id="terminal_1",
        turn_stream_id="stream_1",
        lease_id="lease_1",
        operation_id="op_1",
    )
    with caplog.at_level(logging.ERROR, logger="app.services.infrastructure.external_resource_leases"):
        settled = ledger.settle("lease_1")
    # durable 事实已经落盘，通知失败绝不回滚。
    assert settled.status == "settled"
    assert ledger.get_lease("lease_1").status == "settled"
    assert ledger.notification_errors
    assert "event unavailable" in ledger.notification_errors[-1]
    assert any("resource.state 事件发布失败" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_turn_cancel_releases_durable_leases_without_stopping_resources(tmp_path: Path) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    manager.register(
        resource_id="browser_1",
        kind="browser_context",
        lifetime_scope="session",
    )
    manager.register(
        resource_id="terminal_1",
        kind="terminal",
        lifetime_scope="turn",
    )
    manager.acquire(
        resource_id="browser_1",
        turn_stream_id="stream_1",
        lease_id="lease_browser",
        operation_id="op_1",
    )
    manager.acquire(
        resource_id="terminal_1",
        turn_stream_id="stream_1",
        lease_id="lease_terminal",
        operation_id="op_2",
    )

    released = manager.release_turn_leases("stream_1")
    assert {lease.lease_id for lease in released} == {
        "lease_browser",
        "lease_terminal",
    }
    browser = manager.get("browser_1")
    terminal = manager.get("terminal_1")
    assert browser is not None and browser.status == "running"
    assert terminal is not None and terminal.status == "running"

    restored = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    assert restored.leases_for_turn("stream_1")[0].status == "released"
    assert json.loads((tmp_path / "resources.json").read_text())


@pytest.mark.asyncio
async def test_reconcile_never_assumes_missing_external_resource_is_safe(tmp_path: Path) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    manager.register(
        resource_id="mcp_1",
        kind="mcp_connection",
        lifetime_scope="workspace",
    )
    manager.acquire(
        resource_id="mcp_1",
        turn_stream_id="stream_1",
        lease_id="lease_1",
        operation_id="op_1",
    )

    records = manager.reconcile({})
    assert records[0].status == "orphaned"
    assert manager.leases_for_turn("stream_1")[0].status == "reconcile_required"


def test_external_resource_registration_requires_explicit_supported_kind(tmp_path: Path) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    manager.register_external(
        resource_id="browser_1",
        kind="browser_context",
    )
    lease = manager.acquire_operation(
        resource_id="browser_1",
        turn_stream_id="stream_1",
        operation_id="open_page",
    )

    assert lease.resource_id == "browser_1"
    with pytest.raises(ValueError, match="不支持的外部持久资源"):
        manager.register_external(
            resource_id="unknown_1",
            kind="unknown_process",
        )


def test_node_debug_process_lease_kept_across_turns_and_settled_by_owner(
    tmp_path: Path,
) -> None:
    """typed ``node_debug_process`` 占用由 owner 结清，Turn 收尾不得误释放。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    manager.register_external(
        resource_id="node_debug_process:ses_1:main",
        kind="node_debug_process",
        lifetime_scope="session",
    )
    lease = manager.acquire(
        resource_id="node_debug_process:ses_1:main",
        turn_stream_id="node-debug-owner:ses_1:main",
        lease_id="node_debug_process:ses_1:main:node-debug-proc_1",
        operation_id="node-debug-proc_1",
    )
    assert lease.status == "active"

    # holder 不是任何 Turn：Turn 结束释放不得解除跨 Turn 的进程占用。
    assert manager.release_turn_leases("strm_1") == []
    still_active = manager.get_lease(lease.lease_id)
    assert still_active is not None
    assert still_active.status == "active"

    settled = manager.settle(lease.lease_id)
    assert settled.status == "settled"
    # 结清结果 durable 落盘，backend 重启后仍可读到。
    restored = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    restored_lease = restored.get_lease(lease.lease_id)
    assert restored_lease is not None
    assert restored_lease.status == "settled"

    # 缺失 lease 绝不静默当作已结清。
    assert manager.get_lease("node_debug_process:ses_1:main:missing") is None
    with pytest.raises(KeyError, match="资源 lease 不存在"):
        manager.settle("node_debug_process:ses_1:main:missing")


def test_two_holders_share_resource_without_interference(tmp_path: Path) -> None:
    """同一资源的两个 holder 各自持有 lease，释放其一不影响另一方。"""
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    ledger.register_external(
        resource_id="browser_1",
        kind="browser_context",
        lifetime_scope="session",
    )
    lease_a = ledger.acquire(
        resource_id="browser_1",
        turn_stream_id="stream_a",
        lease_id="lease_a",
        operation_id="op_a",
    )
    lease_b = ledger.acquire(
        resource_id="browser_1",
        turn_stream_id="stream_b",
        lease_id="lease_b",
        operation_id="op_b",
    )
    assert lease_a.status == "active"
    assert lease_b.status == "active"

    ledger.release("lease_a", reason="holder_a_finished")
    still_active = ledger.get_lease("lease_b")
    assert still_active is not None
    assert still_active.status == "active"
    # 资源 record 不因单个 holder 释放而改变状态。
    record = ledger.get("browser_1")
    assert record is not None
    assert record.status == "running"


def test_restart_without_stopper_never_claims_stopped(tmp_path: Path) -> None:
    """重启后账本没有内存 stopper：缺失证据只能进入 reconcile_required。

    进程重启后 in-process stopper 必然不存在，账本绝不会替 owner 宣称
    外部资源已停止；占用保持 reconcile_required 等待 owner 核实。
    """
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    ledger.register_external(
        resource_id="dev_server_1",
        kind="development_server",
        lifetime_scope="workspace",
    )
    ledger.acquire(
        resource_id="dev_server_1",
        turn_stream_id="stream_crash",
        lease_id="lease_crash",
        operation_id="serve",
    )

    restarted = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    assert restarted.get_lease("lease_crash") is not None
    # 不提供任何 owner 证据时对账：资源 orphaned、lease reconcile_required。
    records = restarted.reconcile({})
    assert records[0].status == "orphaned"
    lease = restarted.get_lease("lease_crash")
    assert lease is not None
    assert lease.status == "reconcile_required"
    # 账本层没有任何 stopper 可调用，也不存在“已停止”的虚构状态。
    assert not hasattr(restarted, "stop")
    assert not hasattr(restarted, "stopper")


def test_provider_unreachable_is_not_reported_as_stopped(tmp_path: Path) -> None:
    """provider 不可达时对账不产生 stopped 语义，只标记 unknown。"""
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.json")
    ledger.register_external(
        resource_id="mcp_1",
        kind="mcp_connection",
        lifetime_scope="workspace",
    )
    ledger.acquire(
        resource_id="mcp_1",
        turn_stream_id="stream_x",
        lease_id="lease_x",
        operation_id="call",
    )

    # owner 无法核实 provider 状态（如网络不可达）时，传入非闭合集合的
    # 观察值：账本保守标记 unknown，绝不推断为 stopped。
    records = ledger.reconcile({"mcp_1": "unreachable"})
    assert records[0].status == "unknown"
    lease = ledger.get_lease("lease_x")
    assert lease is not None
    assert lease.status == "reconcile_required"

    # 只有 owner 明确核实到 stopped 才允许记录 stopped；此时 lease 仍
    # 保持 reconcile_required，结清必须走 owner 的 settle。
    records = ledger.reconcile({"mcp_1": "stopped"})
    assert records[0].status == "stopped"
    lease = ledger.get_lease("lease_x")
    assert lease is not None
    assert lease.status == "reconcile_required"
    with pytest.raises(KeyError, match="资源 lease 不存在"):
        ledger.settle("mcp_1:missing")
