"""公开 catalog 节点到可复用搜索键的纯投影，不读取工作区文件。"""

from collections.abc import Mapping
from datetime import UTC

from app.schemas.internal_v2.session_navigation import SessionCatalogNodeDTO

# 名称、稳定 ID、逻辑目录路径、冻结 UTC 创建位置；字段之间不能拼接匹配。
CatalogSearchTerms = tuple[str, str, str, str]


def catalog_search_terms(
    nodes: Mapping[str, SessionCatalogNodeDTO],
) -> dict[str, CatalogSearchTerms]:
    paths: dict[str, str] = {}
    for node in nodes.values():
        chain: list[SessionCatalogNodeDTO] = []
        visited: set[str] = set()
        current = node
        while current.node_id not in paths:
            if current.node_id in visited:
                raise ValueError(f"会话目录搜索父链包含循环: {current.node_id}")
            visited.add(current.node_id)
            chain.append(current)
            if current.parent_node_id is None:
                break
            current = nodes[current.parent_node_id]
        path = paths.get(current.node_id, "")
        for item in reversed(chain):
            path = f"{path}/{item.name}" if path else item.name
            paths[item.node_id] = path
    return {
        node.node_id: (
            node.name.casefold(),
            node.node_id.casefold(),
            paths[node.node_id].casefold(),
            f"{node.created_at.astimezone(UTC):%Y/%m/%d}/{node.session_id}".casefold()
            if node.created_at is not None and node.session_id is not None
            else "",
        )
        for node in nodes.values()
    }
