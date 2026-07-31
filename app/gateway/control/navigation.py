from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.gateway.control.schemas import (
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationBreadcrumbDTO,
    WorkspaceNavigationNodeDTO,
    WorkspaceNavigationNodeUpdateRequest,
    WorkspaceNavigationPlacementRequest,
    WorkspaceNavigationReorderRequest,
    WorkspaceNavigationTreeDTO,
)
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.registry import WorkspaceTarget


class WorkspaceNavigationStore:
    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path

    def list_tree(self, targets: tuple[WorkspaceTarget, ...]) -> WorkspaceNavigationTreeDTO:
        nodes = self._load_nodes()
        changed = self._synchronize_workspace_refs(nodes, targets)
        self._validate(nodes, {target.workspace_id for target in targets})
        ordered = self._ordered(nodes)
        if changed:
            self._save(ordered)
        return WorkspaceNavigationTreeDTO(
            revision=self._revision(ordered),
            nodes=ordered,
        )

    def create_folder(
        self,
        payload: WorkspaceFolderCreateRequest,
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationTreeDTO:
        nodes = self.list_tree(targets).nodes
        if payload.parent_node_id is not None:
            parent = self._require_node(nodes, payload.parent_node_id)
            if parent.kind != "workspace_folder":
                raise ValueError("工作区文件夹只能挂在另一个工作区文件夹下")
        siblings = [node for node in nodes if node.parent_node_id == payload.parent_node_id]
        position = payload.position if payload.position is not None else len(siblings)
        nodes.append(
            WorkspaceNavigationNodeDTO(
                node_id=create_prefixed_id("gwn"),
                kind="workspace_folder",
                name=payload.name,
                parent_node_id=payload.parent_node_id,
                position=position,
            )
        )
        return self._persist(nodes, targets)

    def update_node(
        self,
        node_id: str,
        payload: WorkspaceNavigationNodeUpdateRequest,
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationTreeDTO:
        nodes = self.list_tree(targets).nodes
        node = self._require_node(nodes, node_id)
        values = payload.model_dump(exclude_unset=True)
        if "parent_node_id" in values:
            parent_id = values["parent_node_id"]
            if parent_id == node_id:
                raise ValueError("导航节点不能成为自己的父节点")
            if parent_id is not None:
                parent = self._require_node(nodes, parent_id)
                if parent.kind != "workspace_folder":
                    raise ValueError("导航节点只能挂在工作区文件夹下")
            node.parent_node_id = parent_id
        if "name" in values:
            if node.kind == "workspace_ref":
                raise ValueError("工作区引用名称由 Gateway 工作区注册表管理")
            node.name = values["name"]
        if "position" in values:
            node.position = values["position"]
        return self._persist(nodes, targets)

    def reorder(
        self,
        payload: WorkspaceNavigationReorderRequest,
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationTreeDTO:
        nodes = self.list_tree(targets).nodes
        siblings = [node for node in nodes if node.parent_node_id == payload.parent_node_id]
        sibling_ids = {node.node_id for node in siblings}
        if len(payload.node_ids) != len(set(payload.node_ids)):
            raise ValueError("工作区导航排序包含重复节点 ID")
        if set(payload.node_ids) != sibling_ids:
            raise ValueError("工作区导航排序必须包含父节点下的全部直接子节点")
        position_by_id = {
            node_id: position for position, node_id in enumerate(payload.node_ids)
        }
        for node in siblings:
            node.position = position_by_id[node.node_id]
        return self._persist(nodes, targets)

    def place(
        self,
        payload: WorkspaceNavigationPlacementRequest,
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationTreeDTO:
        nodes = self.list_tree(targets).nodes
        node = self._require_node(nodes, payload.node_id)
        previous_parent_node_id = node.parent_node_id
        if payload.parent_node_id is not None:
            parent = self._require_node(nodes, payload.parent_node_id)
            if parent.kind != "workspace_folder":
                raise ValueError("导航节点只能挂在工作区文件夹下")

        node.parent_node_id = payload.parent_node_id
        self._validate(nodes, {target.workspace_id for target in targets})

        siblings = self._siblings_in_position_order(
            nodes,
            payload.parent_node_id,
            excluding_node_id=node.node_id,
        )
        if payload.mode == "last":
            insertion_index = len(siblings)
        else:
            target = self._require_node(nodes, payload.target_node_id or "")
            if target.parent_node_id != payload.parent_node_id:
                raise ValueError(
                    "排序目标不在目标父节点下: "
                    f"target={target.node_id}, parent={payload.parent_node_id}"
                )
            target_index = next(
                index
                for index, sibling in enumerate(siblings)
                if sibling.node_id == target.node_id
            )
            insertion_index = target_index + (1 if payload.mode == "after" else 0)
        siblings.insert(insertion_index, node)
        self._renumber(siblings)

        if previous_parent_node_id != payload.parent_node_id:
            self._renumber(
                self._siblings_in_position_order(nodes, previous_parent_node_id)
            )
        return self._persist(nodes, targets)

    def delete_folder(
        self,
        node_id: str,
        targets: tuple[WorkspaceTarget, ...],
        *,
        recursive: bool = False,
    ) -> WorkspaceNavigationTreeDTO:
        nodes = self.list_tree(targets).nodes
        node = self._require_node(nodes, node_id)
        if node.kind != "workspace_folder":
            raise ValueError("只能删除工作区文件夹")
        children = [item.node_id for item in nodes if item.parent_node_id == node_id]
        if children and not recursive:
            raise ValueError(
                f"工作区文件夹非空，不能删除: node_id={node_id}, children={children}"
            )
        if recursive:
            folder_ids = {node_id}
            pending = [node_id]
            while pending:
                parent_id = pending.pop()
                for item in nodes:
                    if (
                        item.kind == "workspace_folder"
                        and item.parent_node_id == parent_id
                        and item.node_id not in folder_ids
                    ):
                        folder_ids.add(item.node_id)
                        pending.append(item.node_id)
            promoted_refs = [
                item
                for item in self._ordered(nodes)
                if item.kind == "workspace_ref" and item.parent_node_id in folder_ids
            ]
            promoted_ref_ids = {item.node_id for item in promoted_refs}
            remaining = [item for item in nodes if item.node_id not in folder_ids]
            next_position = max(
                (
                    item.position
                    for item in remaining
                    if item.parent_node_id == node.parent_node_id
                    and item.node_id not in promoted_ref_ids
                ),
                default=-1,
            ) + 1
            for workspace_ref in promoted_refs:
                workspace_ref.parent_node_id = node.parent_node_id
                workspace_ref.position = next_position
                next_position += 1
            return self._persist(remaining, targets)
        return self._persist(
            [item for item in nodes if item.node_id != node_id],
            targets,
        )

    def breadcrumb(
        self,
        node_id: str,
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationBreadcrumbDTO:
        tree = self.list_tree(targets)
        nodes_by_id = {node.node_id: node for node in tree.nodes}
        node = nodes_by_id.get(node_id)
        if node is None:
            raise KeyError(f"Gateway 导航节点不存在: {node_id}")
        items: list[WorkspaceNavigationNodeDTO] = []
        visited: set[str] = set()
        while node is not None:
            if node.node_id in visited:
                raise RuntimeError(f"Gateway 工作区导航包含循环关系: {node.node_id}")
            visited.add(node.node_id)
            items.append(node)
            node = (
                nodes_by_id.get(node.parent_node_id)
                if node.parent_node_id is not None
                else None
            )
        items.reverse()
        return WorkspaceNavigationBreadcrumbDTO(revision=tree.revision, items=items)

    def _persist(
        self,
        nodes: list[WorkspaceNavigationNodeDTO],
        targets: tuple[WorkspaceTarget, ...],
    ) -> WorkspaceNavigationTreeDTO:
        valid_workspace_ids = {target.workspace_id for target in targets}
        self._validate(nodes, valid_workspace_ids)
        ordered = self._ordered(nodes)
        self._save(ordered)
        return WorkspaceNavigationTreeDTO(
            revision=self._revision(ordered),
            nodes=ordered,
        )

    def _load_nodes(self) -> list[WorkspaceNavigationNodeDTO]:
        payload = read_json_object(
            self._storage_path,
            default={"schema_version": 1, "nodes": []},
        )
        raw_nodes = payload.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise RuntimeError(
                f"Gateway 工作区导航 nodes 必须是数组: {self._storage_path}"
            )
        return [WorkspaceNavigationNodeDTO.model_validate(item) for item in raw_nodes]

    def _save(self, nodes: list[WorkspaceNavigationNodeDTO]) -> None:
        atomic_write_json(
            self._storage_path,
            {
                "schema_version": 1,
                "nodes": [node.model_dump(mode="json") for node in nodes],
            },
        )

    @staticmethod
    def _synchronize_workspace_refs(
        nodes: list[WorkspaceNavigationNodeDTO],
        targets: tuple[WorkspaceTarget, ...],
    ) -> bool:
        changed = False
        targets_by_id = {target.workspace_id: target for target in targets}
        for node in list(nodes):
            if node.kind != "workspace_ref":
                continue
            if node.workspace_id not in targets_by_id:
                nodes.remove(node)
                changed = True
                continue
            target_name = targets_by_id[node.workspace_id].name
            if node.name != target_name:
                node.name = target_name
                changed = True
        existing_workspace_ids = {
            node.workspace_id for node in nodes if node.kind == "workspace_ref"
        }
        next_position = max(
            (node.position for node in nodes if node.parent_node_id is None),
            default=-1,
        ) + 1
        for target in targets:
            if target.workspace_id in existing_workspace_ids:
                continue
            nodes.append(
                WorkspaceNavigationNodeDTO(
                    node_id=create_prefixed_id("gwn"),
                    kind="workspace_ref",
                    name=target.name,
                    workspace_id=target.workspace_id,
                    position=next_position,
                )
            )
            next_position += 1
            changed = True
        return changed

    @classmethod
    def _validate(
        cls,
        nodes: list[WorkspaceNavigationNodeDTO],
        valid_workspace_ids: set[str],
    ) -> None:
        nodes_by_id: dict[str, WorkspaceNavigationNodeDTO] = {}
        workspace_refs: set[str] = set()
        for node in nodes:
            if node.node_id in nodes_by_id:
                raise ValueError(f"Gateway 导航节点 ID 重复: {node.node_id}")
            nodes_by_id[node.node_id] = node
            if node.kind == "workspace_ref":
                assert node.workspace_id is not None
                if node.workspace_id not in valid_workspace_ids:
                    raise ValueError(f"Gateway 导航引用未知工作区: {node.workspace_id}")
                if node.workspace_id in workspace_refs:
                    raise ValueError(f"Gateway 工作区存在重复规范引用: {node.workspace_id}")
                workspace_refs.add(node.workspace_id)
        for node in nodes:
            if node.parent_node_id is not None:
                parent = nodes_by_id.get(node.parent_node_id)
                if parent is None:
                    raise ValueError(
                        f"Gateway 导航父节点不存在: {node.parent_node_id}"
                    )
                if parent.kind != "workspace_folder":
                    raise ValueError(
                        f"Gateway 导航父节点不是工作区文件夹: {parent.node_id}"
                    )
            ancestor_id = node.parent_node_id
            visited = {node.node_id}
            while ancestor_id is not None:
                if ancestor_id in visited:
                    raise ValueError(f"Gateway 工作区导航包含循环关系: {node.node_id}")
                visited.add(ancestor_id)
                ancestor = nodes_by_id.get(ancestor_id)
                if ancestor is None:
                    break
                ancestor_id = ancestor.parent_node_id

    @staticmethod
    def _ordered(
        nodes: list[WorkspaceNavigationNodeDTO],
    ) -> list[WorkspaceNavigationNodeDTO]:
        return sorted(nodes, key=lambda node: (node.parent_node_id or "", node.position, node.node_id))

    @staticmethod
    def _siblings_in_position_order(
        nodes: list[WorkspaceNavigationNodeDTO],
        parent_node_id: str | None,
        *,
        excluding_node_id: str | None = None,
    ) -> list[WorkspaceNavigationNodeDTO]:
        return sorted(
            (
                node
                for node in nodes
                if node.parent_node_id == parent_node_id
                and node.node_id != excluding_node_id
            ),
            key=lambda node: (node.position, node.node_id),
        )

    @staticmethod
    def _renumber(nodes: list[WorkspaceNavigationNodeDTO]) -> None:
        for position, node in enumerate(nodes):
            node.position = position

    @staticmethod
    def _revision(nodes: list[WorkspaceNavigationNodeDTO]) -> str:
        encoded = json.dumps(
            [node.model_dump(mode="json") for node in nodes],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _require_node(
        nodes: list[WorkspaceNavigationNodeDTO],
        node_id: str,
    ) -> WorkspaceNavigationNodeDTO:
        node = next((item for item in nodes if item.node_id == node_id), None)
        if node is None:
            raise KeyError(f"Gateway 导航节点不存在: {node_id}")
        return node
