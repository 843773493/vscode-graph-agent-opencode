from pathlib import Path

import pytest

from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.schemas import (
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationPlacementRequest,
)
from app.gateway.registry import WorkspaceTarget


def _target(workspace_id: str) -> WorkspaceTarget:
    return WorkspaceTarget(
        workspace_id=workspace_id,
        name=workspace_id,
        root_path=f"/tmp/{workspace_id}",
        backend_url="http://127.0.0.1:8010",
        connection_kind="local",
    )


def _sibling_ids(
    store: WorkspaceNavigationStore,
    targets: tuple[WorkspaceTarget, ...],
    parent_node_id: str | None,
) -> list[str]:
    return [
        node.node_id
        for node in store.list_tree(targets).nodes
        if node.parent_node_id == parent_node_id
    ]


def test_place_moves_and_renumbers_workspace_navigation(tmp_path: Path) -> None:
    store = WorkspaceNavigationStore(storage_path=tmp_path / "workspace-tree.json")
    targets = (_target("gw_a"), _target("gw_b"), _target("gw_c"))
    initial = store.list_tree(targets)
    refs = {
        node.workspace_id: node
        for node in initial.nodes
        if node.kind == "workspace_ref"
    }
    folder = next(
        node
        for node in store.create_folder(
            WorkspaceFolderCreateRequest(name="项目"),
            targets,
        ).nodes
        if node.kind == "workspace_folder"
    )

    reordered = store.place(
        WorkspaceNavigationPlacementRequest(
            node_id=folder.node_id,
            parent_node_id=None,
            mode="before",
            target_node_id=refs["gw_b"].node_id,
        ),
        targets,
    )
    assert [
        node.node_id
        for node in reordered.nodes
        if node.parent_node_id is None
    ] == [
        refs["gw_a"].node_id,
        folder.node_id,
        refs["gw_b"].node_id,
        refs["gw_c"].node_id,
    ]
    assert [
        node.position
        for node in reordered.nodes
        if node.parent_node_id is None
    ] == [0, 1, 2, 3]

    moved = store.place(
        WorkspaceNavigationPlacementRequest(
            node_id=refs["gw_c"].node_id,
            parent_node_id=folder.node_id,
            mode="last",
        ),
        targets,
    )
    assert _sibling_ids(store, targets, folder.node_id) == [refs["gw_c"].node_id]
    assert [
        node.position
        for node in moved.nodes
        if node.parent_node_id is None
    ] == [0, 1, 2]


def test_place_rejects_target_from_another_parent(tmp_path: Path) -> None:
    store = WorkspaceNavigationStore(storage_path=tmp_path / "workspace-tree.json")
    targets = (_target("gw_a"), _target("gw_b"))
    initial = store.list_tree(targets)
    refs = [node for node in initial.nodes if node.kind == "workspace_ref"]
    folder = next(
        node
        for node in store.create_folder(
            WorkspaceFolderCreateRequest(name="项目"),
            targets,
        ).nodes
        if node.kind == "workspace_folder"
    )
    store.place(
        WorkspaceNavigationPlacementRequest(
            node_id=refs[0].node_id,
            parent_node_id=folder.node_id,
            mode="last",
        ),
        targets,
    )

    with pytest.raises(ValueError, match="排序目标不在目标父节点下"):
        store.place(
            WorkspaceNavigationPlacementRequest(
                node_id=refs[1].node_id,
                parent_node_id=None,
                mode="before",
                target_node_id=refs[0].node_id,
            ),
            targets,
        )
