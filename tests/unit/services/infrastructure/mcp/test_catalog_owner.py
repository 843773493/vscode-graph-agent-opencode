"""McpCatalogOwner typed 合同测试（OpenSpec C8-B / M01 基础）。"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from app.services.infrastructure.events.channel_events import (
    McpCatalogEvent,
    assert_mcp_catalog_event_is_lightweight,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
    EventChannelSpec,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.config import McpServerConfig


class _EchoInput(BaseModel):
    text: str


def _make_remote_tool(name: str) -> StructuredTool:
    async def _call(text: str) -> str:
        return f"{name}:{text}"

    return StructuredTool.from_function(
        coroutine=_call,
        name=name,
        description=f"{name} 工具",
        args_schema=_EchoInput,
    )


class _FakeSession:
    def __init__(
        self,
        *,
        tool_names: list[str],
        supports_notifications: bool = True,
    ) -> None:
        self.tool_names = list(tool_names)
        self.supports_notifications = supports_notifications
        self.relist_error: Exception | None = None

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> list[BaseTool]:
        if self.relist_error is not None:
            raise self.relist_error
        return [_make_remote_tool(name) for name in self.tool_names]

    def supports_tool_list_changed(self) -> bool:
        return self.supports_notifications


class _FakeSessionFactory:
    def __init__(self, initial_tools: dict[str, list[str]] | None = None) -> None:
        self._initial_tools = initial_tools or {}
        self.sessions: dict[str, _FakeSession] = {}
        self.notify_callbacks: dict[str, Callable[[], None]] = {}

    def __call__(
        self,
        server: McpServerConfig,
        on_tools_list_changed: Callable[[], None],
    ) -> object:
        session = _FakeSession(
            tool_names=list(self._initial_tools.get(server.server_id, [])),
        )
        self.sessions[server.server_id] = session
        self.notify_callbacks[server.server_id] = on_tools_list_changed

        @asynccontextmanager
        async def _session() -> AsyncIterator[_FakeSession]:
            yield session

        return _session()


def _stdio_server(server_id: str = "mini") -> dict[str, object]:
    return {
        server_id: {
            "enabled": True,
            "transport": "stdio",
            "command": "fake-server",
        }
    }


def _make_owner(
    tmp_path: Path,
    factory: _FakeSessionFactory,
    event_service: EventChannelService,
    servers: dict[str, object] | None = None,
) -> McpCatalogOwner:
    return McpCatalogOwner(
        raw_config={"servers": servers if servers is not None else _stdio_server()},
        workspace_root=tmp_path,
        event_service=event_service,
        session_factory=factory,
    )


async def _drain_pending_relists(owner: McpCatalogOwner) -> None:
    tasks = tuple(owner._pending_relist_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def _next_event(
    subscription: object,
    timeout: float = 2.0,
) -> McpCatalogEvent:
    delivery = await asyncio.wait_for(subscription.next(), timeout=timeout)  # type: ignore[attr-defined]
    assert not delivery.gap
    event = delivery.event
    assert isinstance(event, McpCatalogEvent)
    return event


async def test_start_discovers_namespaced_tools_and_calls_them(tmp_path: Path) -> None:
    event_service = EventChannelService()
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory, event_service)

    await owner.start()
    try:
        tools = owner.get_tools()
        assert [tool.name for tool in tools] == ["mcp__mini__echo"]
        assert tools[0].metadata["mcp_server_id"] == "mini"
        assert tools[0].metadata["mcp_remote_tool_name"] == "echo"
        assert await tools[0].ainvoke({"text": "hello"}) == "echo:hello"
        assert owner.get_tool_ids() == frozenset({"mcp__mini__echo"})
        snapshots = owner.list_servers()
        assert snapshots[0].status == "ready"
        assert snapshots[0].tools[0].remote_name == "echo"
        assert owner.catalog_revision().startswith("sha256:")
    finally:
        await owner.shutdown()
    assert not owner.started


async def test_empty_catalog_publishes_fixed_envelope_and_relist_does_not_advance(
    tmp_path: Path,
) -> None:
    """目录为空仍发布固定 envelope/revision；连续 relist payload 不变不推进。"""
    event_service = EventChannelService()
    factory = _FakeSessionFactory(initial_tools={"mini": []})
    owner = _make_owner(tmp_path, factory, event_service)
    subscription = event_service.channel("mcp.catalog/workspace").subscribe(
        label="test"
    )

    await owner.start()
    try:
        empty_revision = owner.catalog_revision()
        assert owner.get_tools() == []
        assert owner.get_tool_ids() == frozenset()
        event = await _next_event(subscription)
        assert event.kind == "published"
        assert event.revision == empty_revision
        assert event.previous_revision is None

        # 另一个相同空目录实例：固定 envelope 的 revision 必须逐字节一致。
        other = _make_owner(
            tmp_path,
            _FakeSessionFactory(initial_tools={"mini": []}),
            event_service,
        )
        await other.start()
        try:
            assert other.catalog_revision() == empty_revision
        finally:
            await other.shutdown()

        # other 的空目录发布也进入共享 channel；先消费掉再验证 unchanged。
        other_event = await _next_event(subscription)
        assert other_event.kind == "published"
        assert other_event.revision == empty_revision

        # 通知触发 relist，payload 仍为空 → unchanged，revision 不推进。
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        event = await _next_event(subscription)
        assert event.kind == "unchanged"
        assert event.revision == empty_revision
        assert event.server_id == "mini"
        assert owner.catalog_revision() == empty_revision
    finally:
        await owner.shutdown()


async def test_list_changed_publishes_new_revision_and_removal_tombstone(
    tmp_path: Path,
) -> None:
    event_service = EventChannelService()
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory, event_service)
    subscription = event_service.channel("mcp.catalog/workspace").subscribe(
        label="test"
    )

    await owner.start()
    try:
        first = await _next_event(subscription)
        assert first.kind == "published"

        # 新增工具 → published，revision 推进。
        factory.sessions["mini"].tool_names = ["echo", "extra"]
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        added = await _next_event(subscription)
        assert added.kind == "published"
        assert added.previous_revision == first.revision
        assert added.revision != first.revision
        assert "mcp__mini__extra" in owner.get_tool_ids()

        # 删除工具 → tombstone，revision 再推进，旧工具消失。
        factory.sessions["mini"].tool_names = []
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        removed = await _next_event(subscription)
        assert removed.kind == "tombstone"
        assert removed.previous_revision == added.revision
        assert owner.get_tool_ids() == frozenset()
        assert owner.catalog_revision() == removed.revision
    finally:
        await owner.shutdown()


async def test_stale_generation_callback_is_rejected_by_generation_lease(
    tmp_path: Path,
) -> None:
    event_service = EventChannelService()
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory, event_service)

    await owner.start()
    try:
        revision = owner.catalog_revision()
        subscription = event_service.channel(
            "mcp.catalog/workspace"
        ).subscribe(label="test")

        # 模拟旧连接回调：连接代际已推进，callback 仍持旧 generation。
        runtime = owner._runtimes["mini"]
        runtime.generation = 2
        stale_callback = owner._make_tools_list_changed_callback("mini", 1)
        factory.sessions["mini"].tool_names = ["echo", "smuggled"]
        stale_callback()
        await _drain_pending_relists(owner)
        assert owner.catalog_revision() == revision
        assert "mcp__mini__smuggled" not in owner.get_tool_ids()
        assert subscription.pending() == ()

        # 当前代际回调正常 relist 并发布。
        current_callback = owner._make_tools_list_changed_callback("mini", 2)
        current_callback()
        await _drain_pending_relists(owner)
        event = await _next_event(subscription)
        assert event.kind == "published"
        assert "mcp__mini__smuggled" in owner.get_tool_ids()
    finally:
        await owner.shutdown()


async def test_server_without_notifications_relists_at_activation_boundary(
    tmp_path: Path,
) -> None:
    event_service = EventChannelService()
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory, event_service)

    await owner.start()
    try:
        factory.sessions["mini"].supports_notifications = False
        assert owner.servers_without_change_notifications() == ("mini",)

        factory.sessions["mini"].tool_names = ["echo", "polled"]
        await owner.relist_servers_without_notifications()
        assert "mcp__mini__polled" in owner.get_tool_ids()

        # 显式 relist 失败 → 显式抛出，且保留旧 valid revision 与错误记录。
        factory.sessions["mini"].relist_error = RuntimeError("connection lost")
        with pytest.raises(McpCatalogRelistError, match="connection lost"):
            await owner.relist_servers_without_notifications()
        assert "mcp__mini__polled" in owner.get_tool_ids()
        snapshot = owner.list_servers()[0]
        assert snapshot.last_relist_error is not None
        assert "connection lost" in snapshot.last_relist_error
    finally:
        await owner.shutdown()


async def test_catalog_events_stay_out_of_job_events(tmp_path: Path) -> None:
    event_service = EventChannelService()
    job_channel = event_service.ensure_channel(
        EventChannelSpec(name="job.events/job-1", overflow_policy="fail_closed")
    )
    job_subscription = job_channel.subscribe(label="job-test")
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory, event_service)

    await owner.start()
    try:
        factory.sessions["mini"].tool_names = []
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        assert job_subscription.pending() == ()
        assert "mcp.catalog/workspace" in event_service.channel_names
    finally:
        await owner.shutdown()


def test_mcp_catalog_event_contract_rejects_smuggled_payload() -> None:
    digest = "sha256:" + "0" * 64
    event = McpCatalogEvent(kind="published", revision=digest, server_id="mini")
    assert_mcp_catalog_event_is_lightweight(event)

    @dataclasses.dataclass(frozen=True)
    class SmuggledCatalogEvent(McpCatalogEvent):
        schema_body: str = ""

    with pytest.raises(RuntimeError, match="schema_body"):
        assert_mcp_catalog_event_is_lightweight(
            SmuggledCatalogEvent(kind="published", revision=digest, schema_body="{}")
        )
    with pytest.raises(RuntimeError, match="sha256"):
        McpCatalogEvent(kind="published", revision="raw-text")
    with pytest.raises(ValueError, match="kind"):
        McpCatalogEvent(kind="teleported", revision=digest)
    with pytest.raises(RuntimeError, match="路径"):
        McpCatalogEvent(kind="published", revision=digest, server_id="/etc/passwd")


def test_catalog_owner_does_not_touch_resource_registry_or_csm() -> None:
    """AST 结构锁定：目录 owner 不 import/使用 ResourceRegistry 或 CSM。"""
    import ast
    import inspect

    import app.services.infrastructure.mcp.catalog_owner as module

    tree = ast.parse(inspect.getsource(module))
    forbidden = {
        "resource_platform",
        "ContextSourceManager",
        "ResourceRegistry",
        "ContextSourceEventPublisher",
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name for alias in node.names)
    assert names.isdisjoint(forbidden)
