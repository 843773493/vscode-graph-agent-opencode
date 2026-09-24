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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
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


def _build_middleware(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _write_skill(workspace, "demo v1\n")
    watch_service = WorkspaceFileWatchService(workspace_root=workspace)
    registry = _CountingRegistry(
        workspace_root=workspace,
        watch_service=watch_service,
        project_root=tmp_path,
    )
    manager = ContextSourceManager()
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
    tmp_path: Path,
) -> None:
    middleware, registry, manager, reactor, lifetime_scope, watch_service, skill_uri = (
        _build_middleware(tmp_path)
    )
    try:
        # before_agent 用真实 workspace backend 解析 metadata、注册来源并
        # 标记首次观察；该阶段是会话初始化边界，不属于请求路径轮询。
        assert middleware.before_agent({}, None, {}) is not None
        assert manager.pending_observation_count() == 1
        # AGENTS.md 尚未创建：注册仅做一次注册期读取，无首帧 pending。
        assert registry.snapshot_reads == 2

        # 模型调用 skill_load：从冻结 SkillCatalog binding 激活正文并进入
        # tracked 模式；工具路径零 registry/磁盘读取。
        receipt = manager.load_skill(SKILL_NAME, mode="tracked")
        assert receipt.tracked is True
        assert receipt.display_uri == skill_uri
        reads_after_load = registry.snapshot_reads

        first = middleware.before_model({}, None)
        assert first is not None
        first_message = first["messages"][0]
        assert "demo v1" in first_message.content
        assert first_message.response_metadata["context_source_id"] == "skill:demo"
        # 请求路径只读取已绑定来源的内存快照：一次解析 observation revision，
        # 一次取正文；skill_load 本身零读取（正文来自冻结 binding）。
        assert reads_after_load == 2
        assert registry.snapshot_reads == reads_after_load + 2
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

        second = middleware.before_model({}, None)
        assert second is not None
        second_message = second["messages"][0]
        assert "demo v2" in second_message.content
        assert "-demo v1" in second_message.content
        reads_after_second = registry.snapshot_reads
        assert reads_after_second == reads_after_first + 2

        # 事件已经消费完毕，后续请求保持零快照读取。
        assert middleware.before_model({}, None) is None
        assert registry.snapshot_reads == reads_after_second

        await lifetime_scope.close()
        assert registry.observation_channel.subscriber_ids == ()
    finally:
        await watch_service.shutdown()


@pytest.mark.asyncio
async def test_unknown_source_kind_is_rejected(tmp_path: Path) -> None:
    """未知 source_kind 必须显式失败，不回退到通用措辞。"""
    from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
        ContextSourceDelta,
    )

    middleware, _registry, _manager, _reactor, _scope, watch_service, _uri = (
        _build_middleware(tmp_path)
    )
    try:
        unknown = ContextSourceDelta(
            source_id="mystery:1",
            source_name="mystery",
            source_kind="mystery_kind",
            revision="rev_1",
            previous_revision=None,
            content="正文",
            content_hash="hash_1",
        )
        with pytest.raises(RuntimeError, match="未知 context source kind"):
            middleware._build_source_delta_message(unknown)

        known = ContextSourceDelta(
            source_id="agents:workspace",
            source_name="AGENTS.md",
            source_kind="workspace_agents",
            revision="rev_2",
            previous_revision=None,
            content="已知正文",
            content_hash="hash_2",
        )
        message = middleware._build_source_delta_message(known)
        assert message.content == "已知正文"
        assert message.response_metadata["context_source_kind"] == "workspace_agents"
    finally:
        await watch_service.shutdown()
