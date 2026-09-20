"""SessionService 在 SQLite catalog authority 下的换源冒烟测试。

8.2-切片3b-1 的 §2.1 换源目标态验证：title/parent_session_id 从 resolver
读、manifest 剥离三键、marker 回读 session_id、逻辑移动签名。只使用
tmp_path。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.path_utils import (
    get_session_creation_service,
    get_session_path_resolver,
)
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.session import SessionCreateRequest, SessionUpdateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture()
def catalog_service(tmp_path: Path) -> tuple[SessionService, object]:
    """直构新链 + SessionService 注入（不经环境变量开关）。"""
    workspace = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    sessions_dir = workspace.sessions_root
    service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )
    try:
        yield service, workspace
    finally:
        workspace.close()


@pytest.fixture()
def factory_resolver(tmp_path: Path):
    """经 path_utils 唯一 catalog 工厂取得 resolver 的冒烟入口。"""
    workspace_root = tmp_path / "workspace"
    sessions_root = workspace_root / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    assert isinstance(resolver, SessionCatalogPathResolver)
    from app.core.workspace_identity import load_or_create_workspace_id

    workspace_id = load_or_create_workspace_id(workspace_root)
    service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=sessions_root),
        workspace_id=workspace_id,
        path_resolver=resolver,
        creation_service=get_session_creation_service(sessions_root),
    )
    return service, resolver, workspace_id


@pytest.mark.asyncio
async def test_create_and_get_reads_title_and_parent_from_resolver(
    catalog_service,
) -> None:
    service, workspace = catalog_service

    parent = await service.create(SessionCreateRequest(title="父会话"))
    # R25：child thread 是 owner Session 内的 durable thread；导航子会话
    # 用普通 Session + move 验证 title/parent 换源与 manifest 剥离口径。
    child = await service.create(SessionCreateRequest(title="委派子会话"))
    child = await service.move_session(child.session_id, parent.session_id)

    # 创建走 marker 回读：真实 session_id 即软件分配 ID（ses_ 前缀）。
    assert parent.session_id.startswith("ses_")
    assert child.session_id.startswith("ses_")
    assert child.session_id != parent.session_id

    # 换源：get 的 title 来自 catalog display_name，parent 由父链派生。
    got_parent = await service.get(parent.session_id)
    got_child = await service.get(child.session_id)
    assert got_parent.title == "父会话"
    assert got_child.parent_session_id == parent.session_id
    assert got_child.kind == "normal"

    # 剥离口径：manifest 不含 title/title_source/parent_session_id。
    manifest = workspace.manifest(child.session_id)
    assert "title" not in manifest
    assert "title_source" not in manifest
    assert "parent_session_id" not in manifest
    # 其余字段仍读 manifest。
    assert manifest["kind"] == "normal"

    # child thread 列表读 owner control store：普通 Session 无控制库 →
    # 空列表（delegated child Session manifest 不再参与投影）。
    threads = await service.list_child_threads(parent.session_id)
    assert threads.total == 0
    assert threads.items == []


@pytest.mark.asyncio
async def test_rename_updates_catalog_name_and_strips_manifest(
    catalog_service,
) -> None:
    service, workspace = catalog_service
    session = await service.create(SessionCreateRequest(title="原名"))

    updated = await service.update(
        session.session_id,
        SessionUpdateRequest(title="新名"),
    )

    assert updated.title == "新名"
    assert updated.title_source == "user"
    # 权威显示名已进 catalog。
    assert workspace.node(session.session_id).name == "新名"
    # manifest 保持剥离形态（不再回写 title）。
    assert "title" not in workspace.manifest(session.session_id)


@pytest.mark.asyncio
async def test_move_session_uses_logical_relocation(catalog_service) -> None:
    service, workspace = catalog_service
    parent = await service.create(SessionCreateRequest(title="父会话"))
    session = await service.create(SessionCreateRequest(title="待移动"))
    manifest_before = workspace.manifest(session.session_id)
    session_dir = workspace.session_dir(session.session_id)

    moved = await service.move_session(session.session_id, parent.session_id)

    # 逻辑移动：父关系进 catalog，物理目录与 manifest 字节不变。
    assert moved.parent_session_id == parent.session_id
    assert (await service.get(session.session_id)).parent_session_id == (
        parent.session_id
    )
    assert workspace.session_dir(session.session_id) == session_dir
    assert workspace.manifest(session.session_id) == manifest_before

    # 解绑回根：context_fork 降级等业务规则由服务层维护（此处验证 None 目标）。
    unbound = await service.move_session(session.session_id, None)
    assert unbound.parent_session_id is None


@pytest.mark.asyncio
async def test_folder_move_persists_context_fork_demotion_across_restart(
    catalog_service,
    tmp_path: Path,
) -> None:
    service, workspace = catalog_service
    parent = await service.create(SessionCreateRequest(title="父会话"))
    child = await service.create_context_fork(
        title="上下文副本",
        agent_id=parent.current_agent_id,
        parent_session_id=parent.session_id,
        context_source_session_id=parent.session_id,
    )
    folder = workspace.create_folder("子目录", parent=parent.session_id)
    await service.move_session(child.session_id, folder)

    await service.relocate_folder_tree(
        folder_id=folder,
        parent_node_id=None,
        name="根目录",
    )

    assert (await service.get(child.session_id)).kind == "normal"
    assert workspace.manifest(child.session_id)["kind"] == "normal"

    restarted_workspace = build_catalog_workspace(
        tmp_path,
        workspace_id=WORKSPACE_ID,
    )
    try:
        restarted = SessionService(
            config_service=ConfigService(),
            trace_event_store=TraceEventStore(
                sessions_dir=restarted_workspace.sessions_root
            ),
            workspace_id=WORKSPACE_ID,
            path_resolver=restarted_workspace.resolver,
            creation_service=restarted_workspace.creation_service,
        )
        restored = await restarted.get(child.session_id)
        assert restored.kind == "normal"
        assert restarted_workspace.manifest(child.session_id)["kind"] == "normal"
    finally:
        restarted_workspace.close()


@pytest.mark.asyncio
async def test_list_projects_resolver_titles(catalog_service) -> None:
    service, _ = catalog_service
    await service.create(SessionCreateRequest(title="会话甲"))
    await service.create(SessionCreateRequest(title="会话乙"))

    result = await service.list()

    assert {item.title for item in result.items} == {"会话甲", "会话乙"}


@pytest.mark.asyncio
async def test_factory_wired_service_end_to_end(factory_resolver) -> None:
    """经 path_utils 工厂 + 容器同款接线的全链路冒烟。"""
    service, resolver, workspace_id = factory_resolver

    session = await service.create(SessionCreateRequest(title="工厂会话"))
    got = await service.get(session.session_id)

    assert isinstance(resolver, SessionCatalogPathResolver)
    assert got.title == "工厂会话"
    assert got.workspace_id == workspace_id
    assert got.current_provider_id is not None or got.current_agent_id


# ----------------------------------------------------------------------
# get() 的 parent 派生：folder 间接场景（R16 审查 M6 补锁定）
#
# 变异敏感点：get() 的 parent_session_id 沿权威索引父链游走到最近
# 「会话」祖先（_nearest_session_ancestor_in_projection），folder 不得
# 成为 parent_session_id，无会话祖先时为 None。若变异为直接返回
# node.parent_node_id（R16 审查 M6），以下用例必须失败。
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parent_derivation_none_for_session_in_root_folder(
    catalog_service,
) -> None:
    """场景 1：folder 直接挂根 + 会话在 folder 内 → parent_session_id 为 None。

    folder 不是会话，不得成为 parent_session_id（变异直接取
    node.parent_node_id 时本用例得到 folder_id，必须失败）。
    """
    service, workspace = catalog_service

    folder_id = workspace.create_folder("根目录")
    session_id = await workspace.create_session("目录内会话", parent=folder_id)

    got = await service.get(session_id)

    assert got.parent_session_id is None
    assert got.title == "目录内会话"


@pytest.mark.asyncio
async def test_get_parent_derivation_walks_to_session_ancestor_through_folders(
    catalog_service,
) -> None:
    """场景 2：锚点会话 > folderA > folderB > 会话，且 folderA 内另有会话。

    深层会话跨两层 folder 游走到最近会话祖先：parent 是链顶锚点会话，
    而非直接父节点 folderB_id / folderA_id，也非 folderA 内的兄弟会话
    （folderA 内有另一会话是派生不得被带偏的干扰项）。
    """
    service, workspace = catalog_service

    anchor_id = await workspace.create_session("锚点会话")
    folder_a_id = workspace.create_folder("目录A", parent=anchor_id)
    sibling_id = await workspace.create_session("目录A内会话", parent=folder_a_id)
    folder_b_id = workspace.create_folder("目录B", parent=folder_a_id)
    deep_id = await workspace.create_session("深层会话", parent=folder_b_id)

    got_deep = await service.get(deep_id)
    got_sibling = await service.get(sibling_id)
    got_anchor = await service.get(anchor_id)

    # 深层会话：最近会话祖先是锚点会话（而非 folderB_id）。
    assert got_deep.parent_session_id == anchor_id
    assert got_deep.parent_session_id != folder_b_id
    # 一层 folder 内的会话同样派生到锚点会话（而非兄弟会话）。
    assert got_sibling.parent_session_id == anchor_id
    # 锚点会话自身挂根，无会话祖先。
    assert got_anchor.parent_session_id is None


@pytest.mark.asyncio
async def test_get_parent_derivation_none_without_session_ancestor(
    catalog_service,
) -> None:
    """场景 3：folderA > folderB > 会话、folderA 内另有会话、全链无会话祖先。

    父链上没有任何会话祖先时 parent_session_id 为 None：同在 folderA 内
    的兄弟会话不是祖先，不得被派生为 parent（锁定派生只沿父链游走，
    不取索引中「最近」的任何会话）。
    """
    service, workspace = catalog_service

    folder_a_id = workspace.create_folder("目录A")
    sibling_id = await workspace.create_session("目录A内会话", parent=folder_a_id)
    folder_b_id = workspace.create_folder("目录B", parent=folder_a_id)
    deep_id = await workspace.create_session("深层会话", parent=folder_b_id)

    got_deep = await service.get(deep_id)
    got_sibling = await service.get(sibling_id)

    assert got_deep.parent_session_id is None
    assert got_deep.parent_session_id != folder_b_id
    assert got_sibling.parent_session_id is None
