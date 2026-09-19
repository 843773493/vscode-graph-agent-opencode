"""catalog_workspace_helper 自身的正确性验证（8.2-切片3b-1 示例用例）。

同时作为 R17 测试适配的使用示例：树构造、manifest 剥离形态、逻辑移动。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import pytest

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from tests.unit.core.catalog_workspace_helper import (
    DEFAULT_WORKSPACE_ID,
    build_catalog_workspace,
)


def test_build_catalog_workspace_wires_full_chain(tmp_path: pytest.TempdirFactory) -> None:
    """helper 装配完整新链：store/services/resolver 同根且可用。"""
    workspace = build_catalog_workspace(tmp_path)

    try:
        assert isinstance(workspace.store, SessionCatalogStore)
        assert isinstance(workspace.creation_service, SessionCreationService)
        assert isinstance(workspace.delete_service, SessionSubtreeDeleteService)
        assert isinstance(workspace.resolver, SessionCatalogPathResolver)
        assert workspace.workspace_id == DEFAULT_WORKSPACE_ID
        assert workspace.sessions_root == (
            workspace.workspace_root / ".boxteam" / "sessions"
        )
        assert workspace.resolver.sessions_root == workspace.sessions_root
        # 空 catalog 初始化：无节点、一致性校验通过。
        assert workspace.resolver.list_nodes() == []
        workspace.resolver.initialize()
    finally:
        workspace.close()


def test_create_session_and_folder_builds_tree_with_derived_parents(
    tmp_path,
) -> None:
    """create_session/create_folder 构造树；父关系按 catalog 派生。"""
    with build_catalog_workspace(tmp_path) as workspace:
        folder_id = workspace.create_folder("团队")
        parent_id = workspace.create_session("根会话", folder_id)
        child_id = workspace.create_session("子会话", parent_id)

        parent_node = workspace.node(parent_id)
        child_node = workspace.node(child_id)
        assert parent_node.name == "根会话"
        assert parent_node.parent_node_id == folder_id
        assert child_node.parent_node_id == parent_id

        # folder 是 SQLite-only 节点：无物理目录、无时间投影。
        folder_node = workspace.node(folder_id)
        assert folder_node.kind == "folder"
        assert folder_node.path is None

        # manifest 为剥离形态：不含 title/title_source/parent_session_id。
        manifest = workspace.manifest(child_id)
        assert "title" not in manifest
        assert "title_source" not in manifest
        assert "parent_session_id" not in manifest
        assert manifest["session_id"] == child_id
        assert manifest["workspace_id"] == workspace.workspace_id

        # 显示名只存 catalog：读回走 resolver 投影。
        assert workspace.node(child_id).name == "子会话"


def test_logical_move_keeps_manifest_and_updates_catalog_parents(
    tmp_path,
) -> None:
    """逻辑移动只改 catalog 父关系：不搬磁盘、不改 manifest 字节。"""
    with build_catalog_workspace(tmp_path) as workspace:
        first_folder = workspace.create_folder("目录一")
        second_folder = workspace.create_folder("目录二")
        session_id = workspace.create_session("移动会话", first_folder)
        manifest_before = workspace.manifest(session_id)
        session_dir = workspace.session_dir(session_id)

        workspace.resolver.move_node(
            node_id=first_folder,
            parent_node_id=second_folder,
        )

        # 物理目录与 manifest 字节保持不变（逻辑移动不搬磁盘）。
        assert workspace.session_dir(session_id) == session_dir
        assert workspace.manifest(session_id) == manifest_before
        # 父关系按 catalog 即时派生。
        assert workspace.node(first_folder).parent_node_id == second_folder
        assert workspace.node(session_id).parent_node_id == first_folder
