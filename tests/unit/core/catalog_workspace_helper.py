"""新形态（SQLite catalog 权威）工作区共享测试 helper（8.2-切片3b-1）。

为 R17 的 30+ 测试文件适配提供机械替换友好的构造入口：把「store →
creation/delete service → 新 resolver → session/folder 树」的装配收敛为
``build_catalog_workspace(tmp_path)`` 一个调用，旧测试中
手写 manifest + register 的样板
替换为 ``workspace.create_session(title, parent)`` /
``workspace.create_folder(name, parent)``。

只直接构造新链（不经 path_utils 工厂与 lru_cache，避免缓存串扰）；path_utils
唯一 catalog 工厂行为由 test_path_utils_catalog.py 覆盖。
只使用调用方传入的 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from app.core.session_catalog_legacy_layout import SESSION_MANIFEST_NAME
from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
)
from app.core.session_catalog_store import (
    SessionCatalogStore,
)
from app.core.session_creation import SessionCreationService
from app.core.session_subtree_delete import SessionSubtreeDeleteService

# 与 R15 resolver 测试同款默认 workspace_id（标准 UUID 文本）。
DEFAULT_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"

# 调用方六字段闭集的默认值（与 R15 make_metadata 同构；真实调用方值由
# create_session 的 metadata 覆盖）。
DEFAULT_SESSION_METADATA: dict[str, Any] = {
    "kind": "normal",
    "delegation": None,
    "generation_origin": None,
    "current_agent_id": "default",
    "current_provider_id": None,
    "context_source_session_id": None,
}


def generate_workspace_id() -> str:
    """生成一个满足 UUID 文本口径的测试 workspace_id。"""
    return str(uuid4())


def make_session_metadata(**overrides: Any) -> dict[str, Any]:
    """构造调用方 session_metadata 六字段闭集（可覆盖）。"""
    metadata: dict[str, Any] = dict(DEFAULT_SESSION_METADATA)
    metadata.update(overrides)
    return metadata


def complete_manifest(
    session_id: str,
    title: str,
    *,
    workspace_id: str,
    parent_session_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """构造测试读取用的完整 session.json 内容。"""
    now = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        **make_session_metadata(),
        "session_id": session_id,
        "workspace_id": workspace_id,
        "title": title,
        "title_source": "user",
        "parent_session_id": parent_session_id,
        "created_at": now,
        "updated_at": now,
    }
    manifest.update(overrides)
    return manifest


def read_manifest(directory: Path) -> dict[str, Any]:
    """读取目录内 session.json（helper 自身与示例测试使用）。"""
    return json.loads(
        (directory / SESSION_MANIFEST_NAME).read_text(encoding="utf-8")
    )


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    """写入测试用 session.json。"""
    (directory / SESSION_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass(slots=True)
class CatalogWorkspaceContext:
    """新形态工作区上下文：store/services/resolver 与构造入口的聚合。"""

    workspace_root: Path
    sessions_root: Path
    workspace_id: str
    store: SessionCatalogStore
    creation_service: SessionCreationService
    delete_service: SessionSubtreeDeleteService
    resolver: SessionCatalogPathResolver

    # ------------------------------------------------------------------
    # 机械替换友好的树构造入口
    # ------------------------------------------------------------------

    async def create_session(
        self,
        title: str,
        parent: str | None = None,
        **metadata: Any,
    ) -> str:
        """创建并发布一个会话，返回真实 session_id。

        ``parent`` 是父节点 ID（session 或 folder 均可，None=根）。
        ``metadata`` 覆盖六字段闭集（kind/delegation/...）。
        所有会话创建统一走原子 ``SessionCreationService`` 流。
        """
        result = await self.creation_service.create(
            idempotency_key=f"test-session-{uuid4().hex}",
            title=title,
            parent_node_id=parent,
            session_metadata=make_session_metadata(**metadata),
        )
        return result.session_id

    def create_folder(self, name: str, parent: str | None = None) -> str:
        """创建 folder（SQLite-only 节点，无物理目录），返回 folder_id。"""
        node = self.resolver.create_folder(name=name, parent_node_id=parent)
        return node.node_id

    # ------------------------------------------------------------------
    # 便捷读取
    # ------------------------------------------------------------------

    def node(self, node_id: str) -> SessionCatalogNodeProjection:
        """读取 SQLite catalog 节点投影。"""
        return self.resolver.get_node(node_id)

    def session_dir(self, session_id: str) -> Path:
        """解析会话物理目录（日期桶下的绝对路径）。"""
        return self.resolver.resolve_session_node(session_id)

    def manifest(self, session_id: str) -> dict[str, Any]:
        """读取会话目录内（剥离后的）session.json。"""
        return read_manifest(self.session_dir(session_id))

    def close(self) -> None:
        """关闭 store 的 SQLite 连接（fixture teardown 调用）。"""
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_catalog_workspace(
    tmp_path: Path,
    *,
    workspace_id: str | None = None,
    sessions_dirname: str = "sessions",
) -> CatalogWorkspaceContext:
    """构造新形态工作区：标准 ``<root>/.boxteam/sessions`` 布局 + 全新链。

    ``workspace_id`` 缺省使用固定测试 UUID（可传 ``generate_workspace_id()``
    换取独立标识）。store 直接构造（空 catalog 起步），不经
    path_utils 工厂。
    """
    resolved_workspace_root = tmp_path / "workspace"
    sessions_root = resolved_workspace_root / ".boxteam" / sessions_dirname
    navigation_root = sessions_root.parent / "navigation"
    store = SessionCatalogStore(
        navigation_root / "session-catalog.sqlite",
        sessions_root,
    )
    resolved_workspace_id = workspace_id or DEFAULT_WORKSPACE_ID
    UUID(resolved_workspace_id)  # workspace_id 必须是标准 UUID 文本口径
    creation_service = SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=resolved_workspace_id,
    )
    delete_service = SessionSubtreeDeleteService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=resolved_workspace_id,
    )
    resolver = SessionCatalogPathResolver(
        store=store,
        sessions_root=sessions_root,
        workspace_id=resolved_workspace_id,
        delete_service=delete_service,
    )
    return CatalogWorkspaceContext(
        workspace_root=resolved_workspace_root,
        sessions_root=sessions_root,
        workspace_id=resolved_workspace_id,
        store=store,
        creation_service=creation_service,
        delete_service=delete_service,
        resolver=resolver,
    )
