from __future__ import annotations

from typing import Literal

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationRecord,
)
from app.services.business.session_navigation.service import SessionCatalogService

_PARENT_UNSET = object()


def create_catalog_intent(
    catalog: SessionCatalogService,
    kind: Literal[
        "create_folder",
        "rename_node",
        "move_node",
        "delete_folder",
        "delete_session",
    ],
    *,
    target_node_id: str | None = None,
    name: str | None = None,
    parent_node_id: str | None | object = _PARENT_UNSET,
    recursive: bool = False,
) -> NavigationMutationIntentDTO:
    fields: dict[str, object] = {
        "client_operation_id": create_prefixed_id("op"),
        "client_sequence": 1,
        "kind": kind,
        "base_catalog_revision": catalog.path_resolver.revision,
    }
    if kind == "create_folder":
        if target_node_id is not None or name is None:
            raise ValueError("create_folder 需要 name 且不接受 target_node_id")
        fields["name"] = name
        if parent_node_id is not _PARENT_UNSET:
            fields["parent_node_id"] = parent_node_id
    else:
        if target_node_id is None:
            raise ValueError(f"{kind} 缺少 target_node_id")
        fields["target_node_id"] = target_node_id
        if kind in ("rename_node", "move_node"):
            fields["expected_revision"] = catalog.operations.node_revision(
                target_node_id
            )
        if kind == "rename_node":
            if name is None:
                raise ValueError("rename_node 缺少 name")
            fields["name"] = name
        elif kind == "move_node":
            if parent_node_id is _PARENT_UNSET:
                raise ValueError("move_node 必须显式给出 parent_node_id")
            fields["parent_node_id"] = parent_node_id
        elif kind == "delete_folder":
            fields["recursive"] = recursive
        elif recursive:
            raise ValueError("delete_session 不接受 recursive")
    return NavigationMutationIntentDTO.model_validate(fields)


async def submit_catalog_intent(
    catalog: SessionCatalogService,
    intent: NavigationMutationIntentDTO,
) -> NavigationMutationRecord:
    scope = local_navigation_scope(catalog.workspace_id)
    await catalog.operations.start()
    try:
        await catalog.operations.enqueue(
            NavigationMutationEnqueueRequest(intents=[intent]),
            scope,
        )
        return await catalog.operations.await_terminal(
            intent.client_operation_id,
            scope,
        )
    finally:
        await catalog.operations.stop()


__all__ = ["create_catalog_intent", "submit_catalog_intent"]
