from __future__ import annotations

import inspect
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path

import pytest

from app.services.infrastructure.events.channel_events import (
    ResourceStateEvent,
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.lease_storage import (
    mutate_lease_payload,
    observe_lease_commits,
    read_lease_snapshot,
)


def test_optional_lease_expiry_reads_legacy_rows_and_round_trips_deadline(tmp_path):
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    ledger.register_external(resource_id="connection", kind="mcp_connection", lifetime_scope="workspace")
    legacy = ledger.acquire_operation(resource_id="connection", turn_stream_id="holder", operation_id="legacy")
    mutate_lease_payload(path, "leases", legacy.lease_id, lambda row: row.pop("expires_at"))
    cold = ExternalResourceLeaseLedger(state_path=path)
    retained = cold.get_lease(legacy.lease_id)
    assert retained is not None and retained.expires_at is None
    deadline = "2026-10-26T01:30:00+00:00"
    prepared = cold.acquire_operation(
        resource_id="connection", turn_stream_id="prepared-holder", operation_id="prepared", expires_at=deadline,
    )
    retained = ExternalResourceLeaseLedger(state_path=path).get_lease(prepared.lease_id)
    assert retained is not None and retained.expires_at == deadline
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="关联键不一致"):
        cold.acquire_operation(resource_id="connection", turn_stream_id="prepared-holder", operation_id="prepared",
                               expires_at="2026-10-26T02:30:00+00:00")
    assert path.read_bytes() == before


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("status", ["running", "stopped", "unknown", "orphaned"])
def test_repeated_registration_preserves_identity_state_and_lease_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold: bool, status: str,
) -> None:
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    ledger.register_external(resource_id="terminal", kind="terminal", lifetime_scope="thread", created_by_turn_id="original")
    lease = ledger.acquire_operation(resource_id="terminal", turn_stream_id="holder", operation_id="original")
    ledger.update_external_state("terminal", lifetime_scope="thread", status=status, unresolved_lease_id=lease.lease_id)
    if cold:
        ledger = ExternalResourceLeaseLedger(state_path=path)
    record, pending, original_bytes = ledger.get("terminal"), ledger.get_lease(lease.lease_id), path.read_bytes()
    assert record is not None and pending is not None

    def unexpected_write(_descriptor: int) -> None:
        pytest.fail("重复登记不应写盘或刷新时间戳")

    with monkeypatch.context() as fault:
        observe_lease_commits(fault, lambda _path, _changes: unexpected_write(0))
        for register in (ledger.register, ledger.register_external):
            repeated = register(resource_id="terminal", kind="terminal", lifetime_scope="workspace", created_by_turn_id="new")
            assert repeated == record and repeated is not record
            repeated.status = "changed-copy"
            assert ledger.get("terminal") == record
        with pytest.raises(RuntimeError, match="资源类型发生冲突"):
            ledger.register_external(resource_id="terminal", kind="browser_context")
        with pytest.raises(ValueError, match="未知资源生命周期范围"):
            ledger.register(resource_id="terminal", kind="terminal", lifetime_scope="invalid")
    assert path.read_bytes() == original_bytes
    assert ledger.get_lease(lease.lease_id) == pending
    restored = ExternalResourceLeaseLedger(state_path=path)
    assert restored.get("terminal") == record and restored.get_lease(lease.lease_id) == pending


@pytest.fixture(params=["register", "acquire", "release", "settle", "turn", "reconcile", "unknown"])
async def ledger_change(request: pytest.FixtureRequest) -> tuple[str, Callable[[ExternalResourceLeaseLedger], Awaitable[object]]]:
    mutations: dict[str, Callable[[ExternalResourceLeaseLedger], object]] = {
        "register": lambda ledger: ledger.register_external(resource_id="new", kind="terminal", lifetime_scope="workspace"),
        "acquire": lambda ledger: ledger.acquire_operation(resource_id="a", turn_stream_id="turn-new", operation_id="new", lease_id="new"),
        "release": lambda ledger: ledger.release("a1"),
        "settle": lambda ledger: ledger.settle("a1"),
        "turn": lambda ledger: ledger.release_turn_leases("turn-a"),
        "reconcile": lambda ledger: ledger.reconcile({"a": "stopped", "b": "running"}),
        "unknown": lambda ledger: ledger.update_external_state("a", lifetime_scope="thread", status="unknown", unresolved_lease_id="a1"),
    }
    async def mutate(ledger: ExternalResourceLeaseLedger) -> object:
        result = mutations[request.param](ledger)
        return await result if inspect.isawaitable(result) else result

    return request.param, mutate


@pytest.mark.parametrize("fault", ["write", "commit"])
async def test_failed_lease_publication_keeps_live_and_cold_facts_until_same_operation_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_change, fault: str,
) -> None:
    name, mutate = ledger_change
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    for resource_id in ("a", "b"):
        ledger.register_external(resource_id=resource_id, kind="terminal", lifetime_scope="workspace")
    for lease_id, resource_id, holder in (("a1", "a", "turn-a"), ("a2", "a", "turn-a"), ("b1", "b", "turn-b")):
        ledger.acquire_operation(resource_id=resource_id, turn_stream_id=holder, operation_id=lease_id, lease_id=lease_id)

    def view(owner: ExternalResourceLeaseLedger) -> tuple:
        return (tuple(owner.get(key) for key in ("a", "b", "new")),
                tuple(owner.get_lease(key) for key in ("a1", "a2", "b1", "new")),
                tuple(owner.leases_for_resource(key) for key in ("a", "b")))

    before, original_bytes = view(ledger), path.read_bytes()
    failure = OSError(5, "原 lease 发布失败", str(path))
    with monkeypatch.context() as io_fault:
        if fault == "write":
            def unavailable(_path, _changes):
                raise failure
            observe_lease_commits(io_fault, unavailable)
        else:
            connect = sqlite3.connect

            def denied_commit(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT" else sqlite3.SQLITE_OK)
                return connection

            io_fault.setattr(sqlite3, "connect", denied_commit)
        with pytest.raises(OSError if fault == "write" else sqlite3.DatabaseError) as raised:
            await mutate(ledger)
        if fault == "write":
            assert raised.value.__cause__ is failure
            assert type(raised.value) is type(failure) and raised.value.errno == failure.errno
        else:
            assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
        assert str(path) not in str(raised.value)
    assert path.read_bytes() == original_bytes
    assert view(ledger) == before
    assert view(ExternalResourceLeaseLedger(state_path=path)) == before
    # 相同登记/operation 重试不能因内存已前进而直接返回未持久的成功回执。
    await mutate(ledger)
    assert view(ledger) == view(ExternalResourceLeaseLedger(state_path=path))
    if name == "register":
        assert ledger.get("new").status == "running"
    elif name == "acquire":
        assert ledger.get_lease("new").status == "active"
    elif name in {"release", "settle", "turn"}:
        assert ledger.get_lease("a1").status == ("settled" if name == "settle" else "released")
        assert ledger.get_lease("a2").status == ("released" if name == "turn" else "active")
        assert ledger.get_lease("b1").status == "active"
    elif name == "reconcile":
        assert (ledger.get("a").status, ledger.get("b").status) == ("stopped", "recovered")
        assert all(ledger.get_lease(key).status == "reconcile_required" for key in ("a1", "a2", "b1"))
    elif name == "unknown":
        assert (ledger.get("a").status, ledger.get("a").lifetime_scope) == ("unknown", "thread")
        assert ledger.get_lease("a1").status == "reconcile_required"
        assert ledger.get_lease("a2").status == ledger.get_lease("b1").status == "active"


@pytest.mark.parametrize("status", ["unknown", "orphaned"])
def test_management_lease_can_occupy_unverified_resource_without_changing_original_facts(
    tmp_path: Path, status: str,
) -> None:
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    ledger.register_external(resource_id="terminal_a", kind="terminal", lifetime_scope="workspace")
    original = ledger.acquire_operation(resource_id="terminal_a", turn_stream_id="old", operation_id="old")
    ledger.update_external_state("terminal_a", lifetime_scope="workspace", status=status, unresolved_lease_id=original.lease_id)
    retained = ledger.get_lease(original.lease_id)
    with pytest.raises(RuntimeError, match="不可获取 lease"):
        ledger.acquire_operation(resource_id="terminal_a", turn_stream_id="ordinary", operation_id="ordinary", allow_stopped=True)
    management = ledger.acquire_operation(
        resource_id="terminal_a", turn_stream_id="management", operation_id="stop", allow_unverified=True,
    )
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.get("terminal_a").status == status
    assert cold.get_lease(original.lease_id) == retained
    assert cold.get_lease(management.lease_id).status == "active"


@pytest.mark.parametrize("status", ["unknown", "orphaned"])
@pytest.mark.parametrize("unresolved_lease_id", [None, "pending"])
async def test_external_state_retains_only_the_explicit_unresolved_operation(
    tmp_path: Path, status: str, unresolved_lease_id: str | None,
) -> None:
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    for resource_id in ("terminal_a", "terminal_b"):
        ledger.register_external(resource_id=resource_id, kind="terminal", lifetime_scope="workspace")
    for lease_id in ("pending", "released", "settled", "same-resource"):
        ledger.acquire_operation(resource_id="terminal_a", turn_stream_id="turn-a", operation_id=lease_id, lease_id=lease_id)
    ledger.release("released")
    await ledger.settle("settled")
    ledger.acquire_operation(resource_id="terminal_b", turn_stream_id="turn-a", operation_id="other", lease_id="other")

    ledger.update_external_state("terminal_a", lifetime_scope="workspace", status=status, unresolved_lease_id=unresolved_lease_id)
    ledger = ExternalResourceLeaseLedger(state_path=path)
    assert ledger.get("terminal_a").status == status
    assert ledger.get_lease("pending").status == ("active" if unresolved_lease_id is None else "reconcile_required")
    assert ledger.get_lease("released").status == "released"
    assert ledger.get_lease("settled").status == "settled"
    assert ledger.get_lease("same-resource").status == "active"
    assert ledger.get_lease("other").status == "active"
    assert [lease.lease_id for lease in ledger.release_turn_leases("turn-a")] == (
        ["pending", "same-resource", "other"] if unresolved_lease_id is None else ["same-resource", "other"]
    )
    assert ledger.get_lease("pending").status == ("released" if unresolved_lease_id is None else "reconcile_required")


def _ledger_with_state_events(
    tmp_path: Path,
    event_service: EventChannelService,
) -> ExternalResourceLeaseLedger:
    """带 resource.state 通知出口的账本与同一事件服务（供订阅断言）。"""
    return ExternalResourceLeaseLedger(
        state_path=tmp_path / "resources.sqlite",
        state_events=ResourceStateEventPublisher(
            event_service=event_service,
            owner_domain="external_resource_leases",
        ),
    )


@pytest.mark.asyncio
async def test_holder_settle_publishes_released_but_turn_release_does_not(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    """Turn 收尾释放不发事件；owner 核实终态的 settle 才发布 released。"""
    ledger = _ledger_with_state_events(tmp_path, event_channel_service)
    subscription = event_channel_service.channel(
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
    ledger.release("lease_1")
    assert subscription.pending() == ()
    settled = await ledger.settle("lease_2")
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
    event_channel_service: EventChannelService,
) -> None:
    """崩溃对账缺失证据：占用进入 reconcile_required，事件绝不虚报 released。"""
    ledger = _ledger_with_state_events(tmp_path, event_channel_service)
    subscription = event_channel_service.channel(
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
    reconciled = await ledger.reconcile({})
    assert reconciled[0].status == "orphaned"
    assert ledger.get_lease("lease_1").status == "reconcile_required"
    deliveries = subscription.pending()
    assert [delivery.event.state for delivery in deliveries] == ["unavailable"]
    assert [delivery.event.resource_id for delivery in deliveries] == ["mcp_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", ["/private/lease-event-secret", r"C:\private\lease-event-secret", r"\\server\private\lease-event-secret"])
@pytest.mark.parametrize("mutation", ["settle", "reconcile"])
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_event_unavailable_keeps_durable_truth_and_records_error(
    tmp_path: Path,
    event_channel_service: EventChannelService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    private: str, mutation: str, sink_fails: bool,
) -> None:
    """事件通道不可用：durable 事实不变，错误显式记录且可从账本读取。"""

    original_error = RuntimeError(f"event unavailable {private}；" + "完整诊断" * 80)
    sink_error = OSError("日志输出失败 /private/ledger-log-secret")
    original_call_handlers = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original_call_handlers(logger, record)
        if sink_fails and record.getMessage().startswith("resource.state 事件发布失败:"):
            raise sink_error

    # publisher 构造即 ensure channel；先建 publisher 再 patch 同一 channel。
    publisher = ResourceStateEventPublisher(
        event_service=event_channel_service,
        owner_domain="external_resource_leases",
    )
    channel = event_channel_service.channel("resource.state/external_resource_leases")
    subscription = channel.subscribe(label="healthy-peer")
    publish = channel.publish
    attempted: list[ResourceStateEvent] = []

    def fail_publish(event: ResourceStateEvent) -> object:
        attempted.append(event)
        if event.resource_id == "terminal_1":
            raise original_error
        return publish(event)

    monkeypatch.setattr(channel, "publish", fail_publish)
    ledger = ExternalResourceLeaseLedger(
        state_path=tmp_path / "resources.sqlite",
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
    ledger.register_external(resource_id="browser_1", kind="browser_context", lifetime_scope="workspace")
    ledger.acquire_operation(resource_id="browser_1", turn_stream_id="stream_2", operation_id="op_2")
    with caplog.at_level(
        logging.ERROR, logger="app.services.infrastructure.external_resource_leases"
    ):
        monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
        if mutation == "settle":
            settled = await ledger.settle("lease_1")
            assert settled.status == "settled"
        else:
            reconciled = await ledger.reconcile({"terminal_1": "stopped", "browser_1": "running"})
            assert [record.status for record in reconciled] == ["stopped", "recovered"]
    # durable 事实已经落盘，通知失败绝不回滚。
    status = "settled" if mutation == "settle" else "reconcile_required"
    assert ledger.get_lease("lease_1").status == status
    cold = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
    assert cold.get_lease("lease_1") == ledger.get_lease("lease_1")
    assert cold.get("terminal_1") == ledger.get("terminal_1")
    assert cold.get("browser_1") == ledger.get("browser_1")
    assert len(ledger.notification_errors) == 1
    detail, = ledger.notification_errors
    assert "resource_id=terminal_1, state=released" in detail
    assert "stage=resource-state-notify, error=RuntimeError" in detail
    assert private not in detail and "event unavailable" not in detail and "ledger-log-secret" not in detail
    assert any(record.exc_info and record.exc_info[1] is original_error for record in caplog.records)
    assert str(original_error) in caplog.text
    if sink_fails:
        assert "diagnostic_sink_error=OSError" in detail
        assert any(str(sink_error) in note for note in original_error.__notes__)
    else:
        assert "diagnostic_sink_error" not in detail
    deliveries = subscription.pending()
    if mutation == "reconcile":
        assert [event.resource_id for event in attempted] == ["terminal_1", "browser_1"]
        assert [delivery.event.resource_id for delivery in deliveries] == ["browser_1"]
        assert [delivery.event.state for delivery in deliveries] == ["unknown"]
    else:
        assert [event.resource_id for event in attempted] == ["terminal_1"]
        assert not deliveries
    assert any(
        "resource.state 事件发布失败" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_turn_cancel_releases_durable_leases_without_stopping_resources(
    tmp_path: Path,
) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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

    restored = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
    assert restored.leases_for_turn("stream_1")[0].status == "released"
    assert read_lease_snapshot(tmp_path / "resources.sqlite")["records"]


@pytest.mark.asyncio
async def test_reconcile_never_assumes_missing_external_resource_is_safe(
    tmp_path: Path,
) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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

    records = await manager.reconcile({})
    assert records[0].status == "orphaned"
    assert manager.leases_for_turn("stream_1")[0].status == "reconcile_required"


def test_external_resource_registration_requires_explicit_supported_kind(
    tmp_path: Path,
) -> None:
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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


async def test_node_debug_process_lease_kept_across_turns_and_settled_by_owner(
    tmp_path: Path,
) -> None:
    """typed ``node_debug_process`` 占用由 owner 结清，Turn 收尾不得误释放。"""
    manager = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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

    settled = await manager.settle(lease.lease_id)
    assert settled.status == "settled"
    # 结清结果 durable 落盘，backend 重启后仍可读到。
    restored = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
    restored_lease = restored.get_lease(lease.lease_id)
    assert restored_lease is not None
    assert restored_lease.status == "settled"

    # 缺失 lease 绝不静默当作已结清。
    assert manager.get_lease("node_debug_process:ses_1:main:missing") is None
    with pytest.raises(KeyError, match="资源 lease 不存在"):
        await manager.settle("node_debug_process:ses_1:main:missing")


def test_two_holders_share_resource_without_interference(tmp_path: Path) -> None:
    """同一资源的两个 holder 各自持有 lease，释放其一不影响另一方。"""
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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

    ledger.release("lease_a")
    still_active = ledger.get_lease("lease_b")
    assert still_active is not None
    assert still_active.status == "active"
    # 资源 record 不因单个 holder 释放而改变状态。
    record = ledger.get("browser_1")
    assert record is not None
    assert record.status == "running"


async def test_restart_without_stopper_never_claims_stopped(tmp_path: Path) -> None:
    """重启后账本没有内存 stopper：缺失证据只能进入 reconcile_required。

    进程重启后 in-process stopper 必然不存在，账本绝不会替 owner 宣称
    外部资源已停止；占用保持 reconcile_required 等待 owner 核实。
    """
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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

    restarted = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
    assert restarted.get_lease("lease_crash") is not None
    # 不提供任何 owner 证据时对账：资源 orphaned、lease reconcile_required。
    records = await restarted.reconcile({})
    assert records[0].status == "orphaned"
    lease = restarted.get_lease("lease_crash")
    assert lease is not None
    assert lease.status == "reconcile_required"
    # 账本层没有任何 stopper 可调用，也不存在“已停止”的虚构状态。
    assert not hasattr(restarted, "stop")
    assert not hasattr(restarted, "stopper")


async def test_provider_unreachable_is_not_reported_as_stopped(tmp_path: Path) -> None:
    """provider 不可达时对账不产生 stopped 语义，只标记 unknown。"""
    ledger = ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
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
    records = await ledger.reconcile({"mcp_1": "unreachable"})
    assert records[0].status == "unknown"
    lease = ledger.get_lease("lease_x")
    assert lease is not None
    assert lease.status == "reconcile_required"

    # 只有 owner 明确核实到 stopped 才允许记录 stopped；此时 lease 仍
    # 保持 reconcile_required，结清必须走 owner 的 settle。
    records = await ledger.reconcile({"mcp_1": "stopped"})
    assert records[0].status == "stopped"
    lease = ledger.get_lease("lease_x")
    assert lease is not None
    assert lease.status == "reconcile_required"
    with pytest.raises(KeyError, match="资源 lease 不存在"):
        await ledger.settle("mcp_1:missing")


def test_load_rejects_old_json_without_creating_an_empty_database(tmp_path: Path) -> None:
    legacy = tmp_path / "resources.json"
    legacy.write_text('{"records": [], "leases": []}')
    with pytest.raises(RuntimeError, match="旧 JSON"):
        ExternalResourceLeaseLedger(state_path=tmp_path / "resources.sqlite")
    assert legacy.read_text() == '{"records": [], "leases": []}'
    assert not (tmp_path / "resources.sqlite").exists()


@pytest.fixture
async def persisted_lease_ledger(tmp_path: Path) -> tuple[Path, ExternalResourceLeaseLedger]:
    path = tmp_path / "resources.sqlite"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    ledger.register_external(resource_id="terminal_a", kind="terminal", lifetime_scope="workspace")
    ledger.register_external(resource_id="browser_b", kind="browser_context", lifetime_scope="thread")
    for status in ("active", "released", "settled", "reconcile_required"):
        ledger.acquire_operation(
            resource_id="terminal_a", turn_stream_id="terminal_a", operation_id=status, lease_id=status,
            holder_session_id="session_a", holder_thread_id="thread_a",
        )
        if status == "released":
            ledger.release(status)
        elif status == "settled":
            await ledger.settle(status)
        elif status == "reconcile_required":
            ledger.update_external_state(
                "terminal_a", lifetime_scope="workspace", status="unknown", unresolved_lease_id=status,
            )
    ledger.acquire_operation(resource_id="browser_b", turn_stream_id="browser_b", operation_id="peer", lease_id="peer")
    return path, ledger


@pytest.mark.parametrize("section", ["records", "leases"])
def test_load_rejects_changed_identity_without_replacing_occupancy(
    persisted_lease_ledger: tuple[Path, ExternalResourceLeaseLedger], section: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _ledger = persisted_lease_ledger
    original = path.read_bytes()
    key, field = ("terminal_a", "resource_id") if section == "records" else ("active", "lease_id")
    mutate_lease_payload(path, section, key, lambda row: row.update({field: "changed-identity"}))
    damaged = path.read_bytes()
    with pytest.raises(TypeError, match="与原身份不一致") as caught:
        ExternalResourceLeaseLedger(state_path=path)
    assert path.read_bytes() == damaged
    assert str(path) not in str(caught.value)
    assert caught.value.__cause__ is not None
    assert any(record.exc_info and record.exc_info[1] is caught.value.__cause__ for record in caplog.records)
    assert str(path) in caplog.text
    path.write_bytes(original)
    repaired = ExternalResourceLeaseLedger(state_path=path)
    active = repaired.get_lease("active")
    assert active is not None and active.resource_id == "terminal_a" and active.holder_thread_id == "thread_a"
    assert [lease.lease_id for lease in repaired.leases_for_resource("terminal_a")] == ["active", "reconcile_required"]


@pytest.mark.parametrize("status", ["active", "released", "settled", "reconcile_required"])
def test_load_rejects_missing_resource_even_for_finished_lease(
    persisted_lease_ledger: tuple[Path, ExternalResourceLeaseLedger], status: str,
) -> None:
    path, _ledger = persisted_lease_ledger
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM resource_leases WHERE lease_id NOT IN (?, ?)", (status, "peer"))
        connection.execute("DELETE FROM resource_records WHERE resource_id='terminal_a'")
        connection.commit()
    damaged = path.read_bytes()
    with pytest.raises(TypeError, match="resource_id 未登记") as caught:
        ExternalResourceLeaseLedger(state_path=path)
    assert str(path) not in str(caught.value)
    assert path.read_bytes() == damaged


def test_load_preserves_all_valid_lease_states_and_original_owner(
    persisted_lease_ledger: tuple[Path, ExternalResourceLeaseLedger],
) -> None:
    path, ledger = persisted_lease_ledger
    original = path.read_bytes()
    cold = ExternalResourceLeaseLedger(state_path=path)
    assert cold.get("terminal_a") == ledger.get("terminal_a")
    for status in ("active", "released", "settled", "reconcile_required"):
        lease = cold.get_lease(status)
        assert lease is not None and lease == ledger.get_lease(status) and lease.status == status
        assert (lease.resource_id, lease.holder_session_id, lease.holder_thread_id) == ("terminal_a", "session_a", "thread_a")
    assert cold.leases_for_resource("terminal_a") == ledger.leases_for_resource("terminal_a")
    assert cold.get_lease("peer") == ledger.get_lease("peer")
    assert path.read_bytes() == original
