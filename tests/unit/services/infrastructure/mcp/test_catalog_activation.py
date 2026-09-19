"""McpCatalogActivationBinder focused 合同测试（OpenSpec E4 第二段）。

覆盖：binding+指引原子冻结、默认 Turn 不漂移、model_call 下一安全边界刷新、
relist 失败 fail closed、revision 冲突显式拒绝、未启动显式失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import (
    MCP_GUIDANCE_SOURCE_ID,
    McpCatalogActivationBinder,
    McpCatalogActivationConflictError,
    McpCatalogActivationSnapshot,
    McpCatalogOwner,
    McpCatalogRelistError,
    McpToolGuidanceProducer,
    McpToolGuidanceSourceRegistration,
    build_extension_catalog_binding,
)
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.orchestration.resource_activation.contracts import (
    ResourceActivationPolicySnapshot,
)


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
    def __init__(self, *, tool_names: list[str], supports_notifications: bool = True):
        self.tool_names = list(tool_names)
        self.supports_notifications = supports_notifications
        self.relist_error: Exception | None = None
        self.list_calls = 0

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> list[BaseTool]:
        self.list_calls += 1
        if self.relist_error is not None:
            raise self.relist_error
        return [_make_remote_tool(name) for name in self.tool_names]

    def supports_tool_list_changed(self) -> bool:
        return self.supports_notifications


class _FakeSessionFactory:
    def __init__(self, initial_tools: dict[str, list[str]] | None = None):
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


class _MemorySaver:
    def __init__(self) -> None:
        self.snapshots: list[McpCatalogActivationSnapshot] = []

    async def save_mcp_catalog_activation_snapshot(
        self, snapshot: McpCatalogActivationSnapshot
    ) -> None:
        self.snapshots.append(snapshot)


class _GuidancePort:
    def __init__(self) -> None:
        self.registrations: list[McpToolGuidanceSourceRegistration] = []
        self.error: Exception | None = None

    def register_tail_only_guidance(
        self, registration: McpToolGuidanceSourceRegistration
    ) -> None:
        if self.error is not None:
            raise self.error
        self.registrations.append(registration)


def _policy(overrides: dict[str, str] | None = None) -> ResourceActivationPolicySnapshot:
    return ResourceActivationPolicySnapshot.from_config(
        {
            "context": {
                "resource_activation": {
                    "default_boundary": "turn",
                    "overrides": overrides or {},
                }
            }
        }
    )


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
    servers: dict[str, object] | None = None,
) -> McpCatalogOwner:
    return McpCatalogOwner(
        raw_config={"servers": servers if servers is not None else _stdio_server()},
        workspace_root=tmp_path,
        event_service=EventChannelService(),
        session_factory=factory,
    )


async def _drain_pending_relists(owner: McpCatalogOwner) -> None:
    tasks = tuple(owner._pending_relist_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def test_freeze_turn_seals_binding_and_guidance_atomically(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        # 原子性：同一 snapshot 内 binding 与指引 revision 逐字段一致。
        assert snapshot.snapshot_kind == "turn"
        assert snapshot.effective_boundary == "turn"
        assert snapshot.binding_ref.catalog_revision == snapshot.guidance.catalog_revision
        assert snapshot.binding_ref.catalog_revision == owner.catalog_revision()
        assert snapshot.binding_ref.generation == owner.catalog_generation()
        assert [
            entry.tool_id for entry in snapshot.guidance.entries
        ] == ["mcp__mini__echo"]
        assert snapshot.activation_provenance_hash.startswith("sha256:")
        assert len(saver.snapshots) == 1
    finally:
        await owner.shutdown()


async def test_freeze_registers_tail_only_guidance_before_saving_snapshot(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    guidance_port = _GuidancePort()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy(),
        saver=saver,
        guidance_source_port=guidance_port,
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        assert len(guidance_port.registrations) == 1
        registration = guidance_port.registrations[0]
        assert registration.source_id == MCP_GUIDANCE_SOURCE_ID
        assert registration.activation_snapshot_id == snapshot.activation_snapshot_id
        assert registration.catalog_revision == snapshot.binding_ref.catalog_revision
        assert registration.guidance_revision == snapshot.guidance.guidance_revision
        assert registration.root_placement == "tail_only"
        assert len(saver.snapshots) == 1
    finally:
        await owner.shutdown()


async def test_guidance_registration_failure_blocks_snapshot_save(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    guidance_port = _GuidancePort()
    guidance_port.error = RuntimeError("guidance owner unavailable")
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy(),
        saver=saver,
        guidance_source_port=guidance_port,
    )
    try:
        with pytest.raises(RuntimeError, match="guidance owner unavailable"):
            await binder.freeze_turn_activation(
                owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
            )
        assert saver.snapshots == []
    finally:
        await owner.shutdown()


async def test_default_turn_model_call_reuses_parent(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        parent = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        prepared = await binder.prepare_model_call_activation(
            parent=parent, model_call_id="mc_1"
        )
        # 默认 turn 策略：同一 Turn 内 snapshot 不漂移，不重复冻结。
        assert prepared is parent
        assert len(saver.snapshots) == 1
        # 通知能力 server 不参与无通知 relist：list_tools 仅 startup 一次。
        assert factory.sessions["mini"].list_calls == 1
    finally:
        await owner.shutdown()


async def test_model_call_boundary_refreshes_at_next_safe_boundary(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy({"mcp_tool_catalog": "model_call"}),
        saver=saver,
    )
    try:
        parent = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        assert parent.effective_boundary == "model_call"
        # 目录在两个 model call 之间发布新增 target。
        factory.sessions["mini"].tool_names.append("extra")
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)

        prepared = await binder.prepare_model_call_activation(
            parent=parent, model_call_id="mc_2"
        )
        assert prepared is not parent
        assert prepared.snapshot_kind == "model_call"
        assert prepared.parent_activation_id == parent.activation_snapshot_id
        assert prepared.model_call_id == "mc_2"
        assert prepared.binding_ref.catalog_revision == owner.catalog_revision()
        assert prepared.binding_ref.generation > parent.binding_ref.generation
        assert prepared.guidance.added_tool_ids == ("mcp__mini__extra",)
        assert len(saver.snapshots) == 2
    finally:
        await owner.shutdown()


async def test_relist_failure_fails_closed(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    factory.sessions["mini"].supports_notifications = False
    factory.sessions["mini"].relist_error = RuntimeError("relist 爆炸")
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        with pytest.raises(McpCatalogRelistError):
            await binder.freeze_turn_activation(
                owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
            )
        assert saver.snapshots == []
    finally:
        await owner.shutdown()


def test_snapshot_rejects_binding_guidance_revision_mismatch() -> None:

    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    guidance = McpToolGuidanceProducer().produce(
        catalog_revision="sha256:" + "1" * 64,
        descriptors=[],
        tools_by_id={},
    )
    with pytest.raises(McpCatalogActivationConflictError):
        McpCatalogActivationSnapshot(
            activation_snapshot_id="mcp-activation:s:thr:turn",
            snapshot_kind="turn",
            owner_session_id="ses_1",
            owner_thread_id="thr_1",
            turn_id="turn_1",
            effective_boundary="turn",
            binding_ref=binding,
            guidance=guidance,
        )


async def test_freeze_before_owner_start_fails_closed(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    with pytest.raises(RuntimeError):
        await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
    assert saver.snapshots == []
