"""只查询 Gateway 登记与导航元数据，不读取工作区或探测目标服务。"""

import base64
import binascii
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.fuzzy import is_subsequence
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.registry.owners import workspace_owner
from app.schemas.gateway.search import (
    GatewayWorkspaceDiscoveryHitDTO,
    GatewayWorkspaceDiscoveryResultsDTO,
)
from app.schemas.gateway_control import WorkspaceNavigationNodeDTO

_MAX_DISCOVERY_NODES = 10_000


class _DiscoveryCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    gateway_id: str
    query: str
    registry_revision: int
    navigation_revision: str
    offset: int = Field(ge=0)


def discover_workspaces(
    registry: GatewayWorkspaceRegistry,
    navigation: WorkspaceNavigationStore,
    *,
    gateway_id: str,
    query: str,
    limit: int,
    cursor: str | None,
) -> GatewayWorkspaceDiscoveryResultsDTO:
    """按原导航顺序返回匹配工作区；空查询可有界浏览登记，不能读取领域正文。"""
    normalized = query.strip().casefold()
    targets = registry.targets()
    if len(targets) > _MAX_DISCOVERY_NODES:
        raise ValueError(f"工作区发现超过 {_MAX_DISCOVERY_NODES} 个登记目标的扫描上限")
    tree = navigation.list_tree(targets)
    if len(tree.nodes) > _MAX_DISCOVERY_NODES:
        raise ValueError(f"工作区发现超过 {_MAX_DISCOVERY_NODES} 个导航节点的扫描上限")
    state = _DiscoveryCursor(
        gateway_id=gateway_id, query=normalized, registry_revision=registry.registry_revision,
        navigation_revision=tree.revision, offset=0,
    )
    if cursor is not None:
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("工作区发现 cursor 格式无效") from error
        previous = _DiscoveryCursor.model_validate_json(raw)
        if previous.model_copy(update={"offset": 0}) != state:
            raise ValueError("工作区发现 cursor 与查询、Gateway 或登记导航版本不符，请重新发现")
        state = previous

    by_target = {target.workspace_id: target for target in targets}
    children: dict[str | None, list[WorkspaceNavigationNodeDTO]] = {}
    for node in tree.nodes:
        children.setdefault(node.parent_node_id, []).append(node)
    # 原 owner 已校验父链与兄弟顺序，按该顺序展开一次，不按搜索排名写回导航。
    frontier: list[tuple[WorkspaceNavigationNodeDTO, list[str], list[str]]] = [
        (node, [], []) for node in reversed(children.get(None, []))
    ]
    matches: list[GatewayWorkspaceDiscoveryHitDTO] = []
    skipped = 0
    while frontier:
        node, parent_ids, parent_names = frontier.pop()
        ids, names = [*parent_ids, node.node_id], [*parent_names, node.name]
        frontier.extend((child, ids, names) for child in reversed(children.get(node.node_id, [])))
        if node.kind != "workspace_ref":
            continue
        assert node.workspace_id is not None  # 导航 owner 保证 workspace_ref 携带目标 ID。
        target = by_target[node.workspace_id]
        owner = workspace_owner(target, registry, gateway_id)
        if normalized and not any(is_subsequence(normalized, term.casefold()) for term in (
            target.name, target.workspace_id, owner.workspace_id, "/".join(names),
        )):
            continue
        if skipped < state.offset:
            skipped += 1
            continue
        matches.append(GatewayWorkspaceDiscoveryHitDTO(
            owner=owner, target_id=target.workspace_id, name=target.name,
            navigation_node_id=node.node_id, breadcrumb_node_ids=ids, breadcrumb_names=names,
        ))
        # 多观察一个命中即可说明有下一页，不构造全部工作区候选或伪造精确总数。
        if len(matches) > limit:
            break
    if state.offset and not matches:
        raise ValueError("工作区发现 cursor 超出当前结果范围")
    page = matches[:limit]
    next_offset = state.offset + len(page)
    complete = len(matches) <= limit
    return GatewayWorkspaceDiscoveryResultsDTO(
        gateway_id=gateway_id, query=query, resolved_at=datetime.now(UTC), items=page, complete=complete,
        cursor=None if complete else base64.urlsafe_b64encode(
            state.model_copy(update={"offset": next_offset}).model_dump_json().encode(),
        ).decode().rstrip("="),
    )
