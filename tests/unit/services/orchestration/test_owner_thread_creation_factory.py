"""OwnerThreadCreationFactory 的 SQLite catalog authority 契约测试。"""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)

SESSION_ID = "ses_019bfb6b5f3c72178c7e8393ce89e7a6"

WORKSPACE_ID = "0197d9a3-7d2a-7c29-8d76-58b3cf3f8a21"

# 有界性断言刻意使用字面量 64（不 import 分片常量），让上限实现的变异直接
# 命中被测行为。
_BOUND = 64


def _sessions_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path / label / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    return root


def test_factory_uses_catalog_resolver(tmp_path: Path) -> None:
    sessions_root = _sessions_root(tmp_path, "catalog-default")
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id="ws_catalog",
    )
    assert isinstance(factory._resolver, SessionCatalogPathResolver)
    # catalog 权威下入口不因 resolver 模式拒绝，按 catalog 内容正常工作
    #（空 catalog 查未知节点 → 显式 KeyError）。
    with pytest.raises(KeyError, match="会话目录节点不存在"):
        factory.owner_main_thread_id(SESSION_ID)


async def _create_sessions(sessions_root: Path, count: int) -> list[str]:
    catalog = SessionCatalogStore(
        sessions_root.parent / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    creation = SessionCreationService(
        store=catalog,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    session_ids: list[str] = []
    for index in range(count):
        result = await creation.create(
            idempotency_key=f"owner-key-{index}",
            title="owner",
            parent_node_id=None,
            session_metadata={
                "kind": "normal",
                "delegation": None,
                "generation_origin": None,
                "current_agent_id": "default",
                "current_provider_id": "default_provider",
                "context_source_session_id": None,
            },
        )
        session_ids.append(result.session_id)
    catalog.close()
    return session_ids


async def test_service_cache_and_open_control_stores_stay_bounded(
    tmp_path: Path,
) -> None:
    """访问远超上限的 owner session 后，缓存与打开的 sqlite 连接都保持有界。

    OwnerThreadCreationFactory 是容器级长驻单例，其每个缓存值都持有一个
    打开 ``session-control.sqlite`` 的 ``SessionControlStore``。若缓存随历史
    owner session 数无界增长，长驻进程内存与文件描述符都会被逐会话永久吃掉，
    最终撞 ``EMFILE``。上限内必须恒定；被淘汰的服务一旦不再被在飞调用持有，
    其 ``SessionControlStore`` 连接随之回收（缓存是唯一长活引用）。
    """
    sessions_root = _sessions_root(tmp_path, "bounded")
    session_ids = await _create_sessions(sessions_root, _BOUND + 40)
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        path_resolver=get_session_path_resolver(sessions_root),
    )

    def open_fds() -> int:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))

    baseline_fds = open_fds()
    for session_id in session_ids:
        factory.for_owner_session(session_id)
    # 被淘汰的服务在仍有在飞引用时由 CPython 引用计数/GC 回收其 sqlite 连接。
    # 生产路径中工厂缓存是唯一长活引用，淘汰后连接即被回收；显式 gc 只是把
    # 时序确定性钉死，避免测量窗口依赖 GC 触发点。
    gc.collect()
    settled_fds = open_fds()

    assert len(factory._services) <= _BOUND
    # 每个 control store 是一条打开的 sqlite 连接（数个 fd）；若淘汰不回收旧
    # 连接，fd 会随历史会话数线性增长。给一个宽松但恒定的上界。
    assert settled_fds - baseline_fds <= _BOUND * 4


async def test_same_owner_session_reuses_same_service(tmp_path: Path) -> None:
    """同一 owner session 在缓存上限内恒定命中同一实例（缓存语义不变）。"""
    sessions_root = _sessions_root(tmp_path, "identity")
    session_ids = await _create_sessions(sessions_root, 3)
    factory = OwnerThreadCreationFactory(
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        path_resolver=get_session_path_resolver(sessions_root),
    )
    first = factory.for_owner_session(session_ids[0])
    assert factory.for_owner_session(session_ids[0]) is first
