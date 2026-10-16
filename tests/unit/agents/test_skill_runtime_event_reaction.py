"""WorkspaceSkillsMiddleware 的事件驱动上下文来源消费测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agents.skill_runtime import (
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
)
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from tests.unit.services.infrastructure.rollout_context.conftest import source_seal_case

__all__ = ["source_seal_case"]

from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)

SKILL_NAME = "demo"


def _write_skill(workspace: Path, body: str) -> Path:
    skill_path = workspace / ".boxteam" / "skills" / SKILL_NAME / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        "description: 事件驱动测试 Skill\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )
    return skill_path


class _CountingRegistry(WorkspaceFileResourceRegistry):
    """统计权威快照读取次数，用来证明请求路径不再遍历轮询。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.snapshot_reads = 0

    def snapshot(self, uri: str):
        self.snapshot_reads += 1
        return super().snapshot(uri)


def _build_middleware(tmp_path: Path, source_seal_case):
    workspace = tmp_path / "workspace"
    _write_skill(workspace, "demo v1\n")
    watch_service = WorkspaceFileWatchService(workspace_root=workspace)
    registry = _CountingRegistry(
        workspace_root=workspace,
        watch_service=watch_service,
        project_root=tmp_path,
    )
    manager = source_seal_case.manager()
    lifetime_scope = LifetimeScope("test-agent")
    reactor = ContextSourceReactor(
        sources=registry,
        context_sources=manager,
        lifetime_scope=lifetime_scope,
        reactor_id="test-middleware",
    )
    # D1:metadata 只来自 ResourceRegistry 发布的 immutable SkillCatalog
    # revision;middleware 不再持有 backend 或任何 skill 挂载。
    catalog = build_workspace_skill_catalog(
        workspace,
        registry=ResourceRegistry(),
        file_registry=registry,
        project_root=tmp_path,
    )
    middleware = WorkspaceSkillsMiddleware(
        catalog=catalog,
        context_source_manager=manager,
        source_registry=registry,
        context_source_reactor=reactor,
    )
    skill_uri = catalog.entries[0].display_uri
    return middleware, registry, manager, reactor, lifetime_scope, watch_service, skill_uri


@pytest.mark.asyncio
async def test_before_model_consumes_event_driven_observation_only(
    tmp_path: Path, source_seal_case,
) -> None:
    middleware, registry, manager, reactor, lifetime_scope, watch_service, skill_uri = (
        _build_middleware(tmp_path, source_seal_case)
    )
    initialization_reads = registry.snapshot_reads
    try:
        # before_agent 消费已发布 metadata、注册来源并
        # 标记首次观察；该阶段是会话初始化边界，不属于请求路径轮询。
        assert middleware.before_agent({}, None, {}) is not None
        assert manager.pending_observation_count() == 2
        # Skill 与 optional AGENTS absence 都排首帧 reconcile；接线不读取快照。
        assert registry.snapshot_reads == initialization_reads

        # 模型调用 skill_load：从冻结 SkillCatalog binding 激活正文并进入
        # tracked 模式；工具路径零 registry/磁盘读取。
        receipt = manager.load_skill(SKILL_NAME, mode="tracked")
        assert receipt.tracked is True
        assert receipt.display_uri == skill_uri
        reads_after_load = registry.snapshot_reads

        assert middleware.before_model({}, None) is None
        first = manager.prepare_pending()
        assert first is not None
        first_delta = next(delta for delta in first.deltas if delta.source_id == "skill:demo")
        assert "demo v1" in first_delta.content and first_delta.source_kind == "skill"
        absent_agents = next(delta for delta in first.deltas if delta.source_id == "agents:workspace")
        assert absent_agents.content == "" and absent_agents.revision
        # 每个来源各读两次内存快照：解析 observation revision 与取正文；
        # skill_load 本身零读取（正文来自冻结 binding）。
        assert reads_after_load == initialization_reads
        assert registry.snapshot_reads == reads_after_load + 4
        receipt, binding = source_seal_case.seal(manager)
        assert receipt.deltas == first.deltas
        frames = [frame for frame in binding.prefix.state.frames
                  if frame.item_ref.source_identity == "skill:demo"]
        assert len(frames) == 1 and frames[0].role == "user"
        assert frames[0].content == first_delta.content
        assert frames[0].item_ref.source_revision == first_delta.revision
        assert manager.prepare_pending() is None
        reads_after_first = registry.snapshot_reads

        # 没有来源事件时 before_model 不再读取任何来源快照。
        assert middleware.before_model({}, None) is None
        assert registry.snapshot_reads == reads_after_first

        # 来源变化：watcher 侧定点刷新发布轻量事件，下一次请求才消费。
        _write_skill(tmp_path / "workspace", "demo v2\n")
        registry.refresh(skill_uri)
        assert registry.snapshot_reads == reads_after_first
        assert reactor.ingest_notifications() == 1
        assert manager.pending_observation_count() == 1

        assert middleware.before_model({}, None) is None
        second = manager.prepare_pending()
        assert second is not None
        second_delta = next(delta for delta in second.deltas if delta.source_id == "skill:demo")
        assert "demo v2" in second_delta.content and "-demo v1" in second_delta.content
        assert registry.snapshot_reads == reads_after_first + 2
        receipt, second_binding = source_seal_case.seal(manager)
        assert receipt.deltas == second.deltas
        frames = [frame for frame in second_binding.prefix.state.frames
                  if frame.item_ref.source_identity == "skill:demo"
                  and frame.item_ref.source_revision == second_delta.revision]
        assert len(frames) == 1 and frames[0].role == "user"
        assert frames[0].content == second_delta.content
        assert manager.prepare_pending() is None
        reads_after_second = registry.snapshot_reads

        # 事件已经消费完毕，后续请求保持零快照读取。
        assert middleware.before_model({}, None) is None
        assert registry.snapshot_reads == reads_after_second

        await lifetime_scope.close()
        assert registry.observation_channel.subscriber_ids == ()
    finally:
        await watch_service.shutdown()


@pytest.mark.asyncio
async def test_unregistered_source_is_rejected_before_body_consumption(
    tmp_path: Path, source_seal_case,
) -> None:
    """未登记来源必须显式失败；来源种类由 typed descriptor 声明。"""
    middleware, _registry, manager, _reactor, scope, watch_service, _uri = (
        _build_middleware(tmp_path, source_seal_case)
    )
    try:
        middleware.before_agent({}, None, {})
        before = manager.prepare_pending()
        with pytest.raises(RuntimeError, match="观察来源缺少 descriptor: source_id=mystery:1"):
            middleware._source_body("mystery:1", "正文")
        assert manager.prepare_pending() == before
        assert middleware._source_body("agents:workspace", "已知正文") == "已知正文"
        descriptors = {descriptor.source_id: descriptor for descriptor in manager.descriptors()}
        assert descriptors["agents:workspace"].source_kind == "workspace_agents"
        await scope.close()
    finally:
        await watch_service.shutdown()
