"""分页预算跨十进制位数后仍与真实 DTO 序列化一致。"""

from __future__ import annotations

import pytest

from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
)
from app.services.business.session_context_projection import paginate_read_items
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)


@pytest.fixture
def context_request() -> SessionContextReadRequest:
    workspace = "00000000-0000-4000-8000-000000000001"
    return SessionContextReadRequest(
        resource_identity=session_resource_identity(workspace, "ses_budget"),
        uri=session_resource_uri(workspace, "ses_budget"),
        scope="workspace", view="messages", max_chars=65_536,
    )


@pytest.mark.parametrize("text_length", [1, 1_000, 9_000, 10_000, 25_000])
@pytest.mark.parametrize("item_count", [1, 2])
def test_complete_items_keep_exact_budget_across_decimal_boundaries(
    context_request: SessionContextReadRequest, text_length: int, item_count: int,
) -> None:
    text = "文\"\\\n" * (text_length // 4) + "末" * (text_length % 4)
    items = [
        SessionContextItemDTO(
            kind="message", resource_identity=context_request.resource_identity,
            uri=context_request.uri, scope=context_request.scope,
            text=text + str(index),
        )
        for index in range(item_count)
    ]
    result = paginate_read_items(
        request=context_request, revision="budget-revision", items=items,
        offset=0, char_offset=0,
    )
    assert result.returned_chars == len(result.model_dump_json())
    assert result.returned_chars <= context_request.max_chars
    # 大响应沿正常分页保留完整 item；不能靠分片或丢条目绕过计算错误。
    if result.has_more:
        assert result.next_cursor is not None
    else:
        assert result.items == items
    assert result.items and all(item.kind == "message" for item in result.items)
    assert result.items[0].text == items[0].text
