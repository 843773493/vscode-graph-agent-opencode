"""分页预算跨十进制位数后仍与真实 DTO 序列化一致。"""

from __future__ import annotations

import pytest

from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
)
from app.services.business.session_context_query.pagination import paginate_read_items
from app.services.business.session_context_query.projection import (
    SessionContextReadSnapshot,
)
from app.services.business.session_context_query.resources import (
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


def test_unavailable_original_count_keeps_records_and_diagnostic_inside_each_page_budget(context_request):
    item = SessionContextItemDTO(
        kind="record", resource_identity=context_request.resource_identity,
        uri=context_request.uri, scope=context_request.scope,
        text='历史摘要"与保留尾部\\\n' * 400, record_index=0,
    )
    snapshot = SessionContextReadSnapshot.model_validate_json(SessionContextReadSnapshot(
        revision="summary-only-fork", items=[item], compacted=True,
        raw_message_count=None, effective_record_count=1,
    ).model_dump_json())
    request = context_request.model_copy(update={"max_chars": 2048})
    chunks = []
    for _ in range(100):
        page = snapshot.page(request)
        assert page.raw_message_count is None and page.compacted
        assert page.effective_record_count == 1
        assert len(page.partial_errors) == 1
        assert page.partial_errors[0].resource_identity == request.resource_identity
        assert page.partial_errors[0].error.startswith("detail-unavailable:")
        assert page.returned_chars == len(page.model_dump_json()) <= request.max_chars
        assert len(page.items) == 1 and page.items[0].kind == "record_chunk"
        chunks.append(page.items[0].text)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        request = request.model_copy(update={"cursor": page.next_cursor})
    else:
        pytest.fail("带计数诊断的上下文分页未收敛")
    assert "".join(chunks) == item.model_dump_json()
