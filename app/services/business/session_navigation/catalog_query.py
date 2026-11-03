"""原 catalog owner 的纯查询：在线快照与 Gateway 派生镜像共用分页、排序和定位。"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from heapq import nsmallest
from typing import Literal

from app.schemas.internal_v2.session_navigation import (
    SessionCatalogNodeDTO,
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.mapping.session_catalog_search import (
    CatalogSearchTerms,
    iter_catalog_matches,
)

_SEARCH_TOP_K_MIN_NODE_COUNT = 2_000
_SEARCH_TOP_K_MIN_SCAN_RATIO = 16


class CatalogCursorError(ValueError):
    """目录 owner 在原校验点区分无效输入与已过期的目录版本。"""

    def __init__(
        self,
        reason: Literal["format", "payload", "revision", "catalog_revision", "scope", "offset", "offset_out_of_range"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = "stale_catalog_cursor" if reason in {"revision", "catalog_revision"} else "invalid_catalog_cursor"

    def detail(self) -> dict[str, str]:
        return {"owner": "session_catalog", "code": self.code, "reason": self.reason, "message": str(self)}


def search_catalog(
    *, nodes: list[SessionCatalogNodeDTO], nodes_by_id: dict[str, SessionCatalogNodeDTO],
    folded_search_terms: dict[str, CatalogSearchTerms], revision: str, catalog_revision: int,
    query: str, limit: int, cursor: str | None, budget: NameSearchBudget,
) -> SessionCatalogSearchResultsDTO:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        raise ValueError("会话目录搜索词不能为空")
    # 搜索的 cursor 作用域是查询词：把目录分页或其它查询词的 cursor 拿来翻本
    # 次搜索，会让 offset 落到错误匹配集的中间，静默跳项。
    scope = f"search:subsequence-v1\n{normalized_query}"
    match_count = 0

    def matching_nodes() -> Iterator[
        tuple[tuple[str, str, str], SessionCatalogNodeDTO]
    ]:
        nonlocal match_count
        for match in iter_catalog_matches(
            nodes, folded_search_terms, normalized_query,
            check=budget.check,
        ):
            match_count += 1
            yield match

    if (
        cursor is None
        and limit > 0
        and len(nodes)
        > max(_SEARCH_TOP_K_MIN_NODE_COUNT, limit * _SEARCH_TOP_K_MIN_SCAN_RATIO)
    ):
        # 大目录首屏只保留排序最靠前的一页，仍扫描全目录以准确报告 total。
        page_matches = nsmallest(
            limit,
            matching_nodes(),
            key=lambda match: match[0],
        )
        offset = 0
    else:
        matches = list(matching_nodes())
        matches.sort(key=lambda match: match[0])
        offset = (
            0
            if cursor is None
            else decode_catalog_cursor(
                cursor,
                revision,
                catalog_revision,
                scope,
                collection_size=match_count,
            )
        )
        page_matches = matches[offset : offset + limit]

    budget.check()
    page_nodes = [node for _, node in page_matches]
    results: list[SessionCatalogSearchResultDTO] = []
    for node in page_nodes:
        budget.check()
        breadcrumb = catalog_breadcrumb(node, nodes_by_id, check=budget.check)
        display_path = "/".join(item.name for item in breadcrumb)
        results.append(
            SessionCatalogSearchResultDTO(
                node=node,
                breadcrumb=breadcrumb,
                relative_path=display_path,
            )
        )
    next_offset = offset + len(results)
    budget.check()
    return SessionCatalogSearchResultsDTO(
        revision=revision,
        catalog_revision=catalog_revision,
        items=results,
        cursor=(
            encode_catalog_cursor(
                next_offset,
                revision,
                catalog_revision,
                scope,
            )
            if next_offset < match_count
            else None
        ),
        total=match_count,
    )


def catalog_breadcrumb(
    node: SessionCatalogNodeDTO,
    nodes_by_id: dict[str, SessionCatalogNodeDTO],
    *, check: Callable[[], None] = lambda: None,
) -> list[SessionCatalogNodeDTO]:
    items: list[SessionCatalogNodeDTO] = []
    visited: set[str] = set()
    current: SessionCatalogNodeDTO | None = node
    while current is not None:
        check()
        if current.node_id in visited:
            raise RuntimeError(f"会话物理目录包含循环关系: {current.node_id}")
        visited.add(current.node_id)
        items.append(current)
        if current.parent_node_id is None:
            current = None
            continue
        parent = nodes_by_id.get(current.parent_node_id)
        if parent is None:
            raise RuntimeError(
                "会话物理目录父节点不存在: "
                f"node_id={current.node_id}, parent={current.parent_node_id}"
            )
        current = parent
    items.reverse()
    return items


def encode_catalog_cursor(
    offset: int,
    revision: str,
    catalog_revision: int,
    scope: str | None,
) -> str:
    """签发 cursor：绑定 hash、navigation revision 与分页作用域。

    ``scope`` 是发牌时的分页上下文（list_children 为 ``parent_node_id``、
    search 为查询词）：同一 revision 下把 A 列表的 cursor 交给 B 列表，offset
    会落在 B 自身切片的中间，静默跳项且不报错。navigation revision 使其它节点变更
    也能使旧 cursor 失效；字符串 revision 仅是内容 hash，不充当 CAS 值。
    """
    payload = json.dumps(
        {
            "offset": offset,
            "revision": revision,
            "catalog_revision": catalog_revision,
            "scope": scope,
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")

def decode_catalog_cursor(
    cursor: str | None,
    revision: str,
    catalog_revision: int,
    scope: str | None,
    *,
    collection_size: int,
) -> int:
    if cursor is None:
        return 0
    try:
        payload = json.loads(
            base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
        )
    except ValueError as error:
        raise CatalogCursorError("format", "会话目录 cursor 无效") from error
    if not isinstance(payload, dict):
        raise CatalogCursorError("payload", "会话目录 cursor 格式无效")
    if not isinstance(payload.get("revision"), str) or type(payload.get("catalog_revision")) is not int:
        raise CatalogCursorError("payload", "会话目录 cursor 版本字段无效")
    if payload.get("revision") != revision:
        raise CatalogCursorError("revision", "会话目录已更新，请从第一页重新加载")
    if payload.get("catalog_revision") != catalog_revision:
        raise CatalogCursorError("catalog_revision", "会话目录已更新，请从第一页重新加载")
    if payload.get("scope") != scope:
        raise CatalogCursorError("scope", "会话目录 cursor 与当前列表不匹配，请从第一页重新加载")
    offset = payload.get("offset")
    if type(offset) is not int or offset < 0:
        raise CatalogCursorError("offset", "会话目录 cursor offset 无效")
    if offset >= collection_size:
        raise CatalogCursorError("offset_out_of_range", "会话目录 cursor offset 超出当前列表范围")
    return offset
