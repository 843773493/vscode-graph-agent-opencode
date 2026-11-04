import json
from pathlib import Path

import pytest

from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import WorkspaceTarget
from app.schemas.gateway_control import (
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationPlacementRequest,
)

GATEWAY = "gateway_" + "a" * 32
WORKSPACES = tuple(f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 4))


def _target(workspace_id: str) -> WorkspaceTarget:
    return WorkspaceTarget(
        gateway_id=GATEWAY,
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
    targets = tuple(_target(workspace_id) for workspace_id in WORKSPACES)
    initial = store.list_tree(targets)
    refs = {
        (node.workspace.gateway_id, node.workspace.workspace_id): node
        for node in initial.nodes
        if node.kind == "workspace_ref" and node.workspace is not None
    }
    assert set(refs) == {target.key for target in targets}
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
            target_node_id=refs[targets[1].key].node_id,
        ),
        targets,
    )
    assert [
        node.node_id
        for node in reordered.nodes
        if node.parent_node_id is None
    ] == [
        refs[targets[0].key].node_id,
        folder.node_id,
        refs[targets[1].key].node_id,
        refs[targets[2].key].node_id,
    ]
    assert [
        node.position
        for node in reordered.nodes
        if node.parent_node_id is None
    ] == [0, 1, 2, 3]

    moved = store.place(
        WorkspaceNavigationPlacementRequest(
            node_id=refs[targets[2].key].node_id,
            parent_node_id=folder.node_id,
            mode="last",
        ),
        targets,
    )
    assert _sibling_ids(store, targets, folder.node_id) == [refs[targets[2].key].node_id]
    assert [
        node.position
        for node in moved.nodes
        if node.parent_node_id is None
    ] == [0, 1, 2]
    persisted = json.loads((tmp_path / "workspace-tree.json").read_text())
    assert persisted["schema_version"] == 2
    assert {
        node["node_id"]: node["workspace"]
        for node in persisted["nodes"]
        if node["kind"] == "workspace_ref"
    } == {
        refs[target.key].node_id: {"gateway_id": target.gateway_id, "workspace_id": target.workspace_id}
        for target in targets
    }


def test_place_rejects_target_from_another_parent(tmp_path: Path) -> None:
    store = WorkspaceNavigationStore(storage_path=tmp_path / "workspace-tree.json")
    targets = tuple(_target(workspace_id) for workspace_id in WORKSPACES[:2])
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
