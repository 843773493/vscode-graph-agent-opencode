from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, create_autospec

import pytest

from app.abstractions.session_context import (
    SessionContextAssemblySourceProtocol,
    SessionContextRevisionChangedError,
)
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionInformationExecutionDTO,
    SessionInformationSessionDTO,
    SessionInformationSnapshotDTO,
    SessionInformationTraceDTO,
    SessionInformationWorkspaceDTO,
    SessionListResultDTO,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_context_resource import (
    SessionContextCursorCodec,
    context_assembly_identity,
    session_context_read_operation,
    session_resource_identity,
    session_resource_vrn,
    workspace_session_inventory_identity,
    workspace_session_inventory_vrn,
)

BACKEND_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


def _resign_cursor_offset(cursor: str, offset: int) -> str:
    """保留原 cursor 的签名身份，只改写 offset，用于伪造越界游标。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    return SessionContextCursorCodec.encode(
        resource_identity=payload["resource_identity"],
        vrn=payload["vrn"],
        scope=payload["scope"],
        revision=payload["revision"],
        operation=payload["operation"],
        record_index=payload["record_index"],
        assembly_ref=payload["assembly_ref"],
        offset=offset,
        char_offset=payload.get("char_offset", 0),
    )


def _session_request(session_id: str = "ses_target", **kwargs: object) -> SessionContextReadRequest:
    return SessionContextReadRequest(
        resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, session_id),
        vrn=session_resource_vrn(BACKEND_WORKSPACE_ID, session_id),
        scope="workspace",
        **kwargs,
    )


def _workspace_inventory_request(**kwargs: object) -> SessionContextReadRequest:
    return SessionContextReadRequest(
        resource_identity=workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
        vrn=workspace_session_inventory_vrn(BACKEND_WORKSPACE_ID),
        scope="workspace",
        **kwargs,
    )


def _session_search_request(session_id: str = "ses_target", **kwargs: object) -> SessionContextSearchRequest:
    return SessionContextSearchRequest(
        resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, session_id),
        vrn=session_resource_vrn(BACKEND_WORKSPACE_ID, session_id),
        scope="workspace",
        **kwargs,
    )


def _workspace_inventory_search_request(**kwargs: object) -> SessionContextSearchRequest:
    return SessionContextSearchRequest(
        resource_identity=workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
        vrn=workspace_session_inventory_vrn(BACKEND_WORKSPACE_ID),
        scope="workspace",
        **kwargs,
    )


class _FakeSessionLookup:
    def __init__(self) -> None:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        self.sessions = [
            SessionDTO(
                session_id="ses_target",
                workspace_id=BACKEND_WORKSPACE_ID,
                resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target"),
                vrn=session_resource_vrn(BACKEND_WORKSPACE_ID, "ses_target"),
                scope="workspace",
                title="目标会话",
                current_agent_id="default",
                thread_id="thread_target",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            SessionDTO(
                session_id="ses_other",
                workspace_id=BACKEND_WORKSPACE_ID,
                resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, "ses_other"),
                vrn=session_resource_vrn(BACKEND_WORKSPACE_ID, "ses_other"),
                scope="workspace",
                title="其他会话",
                current_agent_id="reviewer",
                thread_id="thread_other",
                created_at=timestamp,
                updated_at=timestamp,
            ),
        ]

    async def get(self, session_id: str) -> SessionDTO:
        return next(item for item in self.sessions if item.session_id == session_id)

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO:
        del cursor
        items = [
            item
            for item in self.sessions
            if workspace_id is None or item.workspace_id == workspace_id
        ]
        return SessionListResultDTO(
            items=items[skip : skip + limit],
            total=len(items),
        )


class _FakeMessageSource:
    def __init__(self) -> None:
        self.checkpoint_id = "ckpt-1"
        self.context_state_owners: list[tuple[str, str]] = []
        self.records = [
            {"role": "system", "content": "SECRET SYSTEM"},
            {"role": "user", "type": "human", "content": "最初目标 ALPHA"},
            {
                "role": "assistant",
                "type": "ai",
                "content": [
                    {"type": "reasoning", "reasoning": "SECRET REASONING"},
                    {"type": "text", "text": "第一次回答"},
                    {"type": "image", "data": "SECRET MEDIA"},
                ],
                "tool_calls": [{"name": "read", "args": {"path": "SECRET ARG"}}],
            },
            {"role": "tool", "name": "read", "content": "SECRET RESULT"},
            {"role": "user", "type": "human", "content": "第二个问题 BETA"},
            {"role": "assistant", "type": "ai", "content": "第二次回答"},
            {"role": "user", "type": "human", "content": "第三个问题 GAMMA"},
            {"role": "assistant", "type": "ai", "content": "第三次回答"},
            {"role": "user", "type": "human", "content": "第四个问题 DELTA"},
            {"role": "assistant", "type": "ai", "content": "第四次回答"},
        ]

    async def get_agent_context_state(
        self,
        session_id: str,
        thread_id: str,
    ) -> dict[str, object]:
        self.context_state_owners.append((session_id, thread_id))
        return {
            "records": list(self.records),
            "checkpoint_id": self.checkpoint_id,
            "raw_message_count": len(self.records),
            "compacted": True,
            "compaction_cutoff": 2,
            "history_file_path": "/private/history.jsonl",
        }


class _FakeInformationSource:
    async def get_information(
        self,
        session_id: str,
    ) -> SessionInformationSnapshotDTO:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        session = await _FakeSessionLookup().get(session_id)
        return SessionInformationSnapshotDTO(
            generated_at=timestamp,
            session=SessionInformationSessionDTO(
                session_id=session.session_id,
                workspace_id=session.workspace_id,
                title=session.title,
                current_agent_id=session.current_agent_id,
                parent_session_id=session.parent_session_id,
                kind=session.kind,
                created_at=session.created_at,
                updated_at=session.updated_at,
            ),
            workspace=SessionInformationWorkspaceDTO(
                workspace_id=BACKEND_WORKSPACE_ID,
                name="测试工作区",
                root_path="/workspace",
            ),
            storage_path=(
                "/workspace/.boxteam/sessions/项目会话--12345678/目标会话--87654321"
            ),
            execution=SessionInformationExecutionDTO(
                status="running",
                current_tool="read",
            ),
            trace=SessionInformationTraceDTO(),
        )


@pytest.fixture
def _service():
    assembly_source = create_autospec(
        SessionContextAssemblySourceProtocol, spec_set=True
    )
    assembly_source.get_context_assembly.side_effect = AssertionError(
        "active view 不得读取上一请求的 assembly"
    )
    assembly_source.project_context_plan_to_history_with_diagnostics.side_effect = (
        AssertionError("active view 不得裁剪为 assembly selection")
    )

    def create_service(
        message_source: _FakeMessageSource,
    ) -> SessionContextQueryService:
        service = SessionContextQueryService(
            message_source=message_source,
            session_lookup=_FakeSessionLookup(),
            assembly_source=assembly_source,
            workspace_id=BACKEND_WORKSPACE_ID,
        )
        service.bind_information_source(_FakeInformationSource())
        return service

    return create_service


@pytest.mark.asyncio
async def test_overview_defaults_to_initial_goal_recent_three_rounds_and_safe_content(
    _service,
):
    message_source = _FakeMessageSource()
    result = await _service(message_source).read_context(_session_request())

    serialized = result.model_dump_json()
    assert result.revision.startswith("content:")
    assert result.compacted is True
    assert result.compaction_cutoff == 2
    assert "最初目标 ALPHA" in serialized
    assert "第二个问题 BETA" in serialized
    assert "第四次回答" in serialized
    assert "SECRET SYSTEM" not in serialized
    assert "SECRET REASONING" not in serialized
    assert "SECRET ARG" not in serialized
    assert "SECRET RESULT" not in serialized
    assert "SECRET MEDIA" not in serialized
    assert any(item.kind == "execution" for item in result.items)
    assert message_source.context_state_owners == [
        ("ses_target", "thread_target")
    ]


@pytest.mark.asyncio
async def test_assemblies_read_uses_catalog_resolved_main_thread():
    assembly_source = create_autospec(
        SessionContextAssemblySourceProtocol, spec_set=True
    )
    assembly_source.list_context_assemblies.return_value = ()
    service = SessionContextQueryService(
        message_source=_FakeMessageSource(),
        session_lookup=_FakeSessionLookup(),
        assembly_source=assembly_source,
        workspace_id=BACKEND_WORKSPACE_ID,
    )

    result = await service.read_context(_session_request(view="assemblies"))

    assert result.items == []
    assembly_source.list_context_assemblies.assert_called_once_with(
        "ses_target",
        "thread_target",
    )


@pytest.mark.asyncio
async def test_assembly_history_projection_uses_session_thread_pair():
    assembly_source = create_autospec(
        SessionContextAssemblySourceProtocol, spec_set=True
    )
    snapshot = MagicMock()
    snapshot.assembly_id = "asm_target"
    snapshot.session_id = "ses_target"
    snapshot.plan_id = "plan_target"
    snapshot.plan_hash = "plan-hash"
    snapshot.history_view_revision = 1
    snapshot.source_overlay_epoch = 0
    snapshot.target_format = "langchain"
    snapshot.request_hash = "request-hash"
    snapshot.compiler_version = "compiler"
    snapshot.provider_version = "provider"
    snapshot.projector_id = "projector"
    snapshot.projector_version = "1"
    snapshot.selection = ()
    snapshot.loss = ()
    plan = object()
    snapshot.as_sealed_plan.return_value = plan
    assembly_source.list_context_assemblies.return_value = (snapshot,)
    assembly_source.project_context_plan_to_history_with_diagnostics.return_value = (
        [],
        (),
    )
    service = SessionContextQueryService(
        message_source=_FakeMessageSource(),
        session_lookup=_FakeSessionLookup(),
        assembly_source=assembly_source,
        workspace_id=BACKEND_WORKSPACE_ID,
    )
    assembly_ref = context_assembly_identity(
        session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target"),
        "asm_target",
    )

    await service.read_context(
        _session_request(view="assembly", assembly_ref=assembly_ref)
    )

    assembly_source.list_context_assemblies.assert_called_once_with(
        "ses_target",
        "thread_target",
    )
    assembly_source.project_context_plan_to_history_with_diagnostics.assert_called_once_with(
        "ses_target",
        "thread_target",
        plan,
    )


@pytest.mark.asyncio
async def test_overview_handles_fewer_user_rounds_than_default_window(_service):
    message_source = _FakeMessageSource()
    message_source.records = [
        {"role": "user", "content": "唯一问题"},
        {"role": "assistant", "content": "唯一回答"},
    ]

    result = await _service(message_source).read_context(
        _session_request()
    )

    serialized = result.model_dump_json()
    assert "唯一问题" in serialized
    assert "唯一回答" in serialized


@pytest.mark.asyncio
async def test_records_can_explicitly_include_detailed_fields(_service):
    result = await _service(_FakeMessageSource()).read_context(
        _session_request(
            view="records",
            include=[
                "visible_text",
                "reasoning",
                "tool_calls",
                "tool_results",
                "system",
                "raw_record",
            ],
            limit=20,
        )
    )

    serialized = result.model_dump_json()
    assert "SECRET SYSTEM" in serialized
    assert "SECRET REASONING" in serialized
    assert "SECRET ARG" in serialized
    assert "SECRET RESULT" in serialized
    assert result.effective_record_count == 10


@pytest.mark.asyncio
async def test_read_cursor_is_opaque_and_fails_when_revision_changes(_service):
    source = _FakeMessageSource()
    service = _service(source)
    first = await service.read_context(
        _session_request(
            view="messages",
            limit=2,
        )
    )
    assert first.has_more is True
    assert first.next_cursor is not None

    second = await service.read_context(
        _session_request(
            view="messages",
            limit=2,
            cursor=first.next_cursor,
        )
    )
    assert second.items[0].record_index != first.items[0].record_index

    source.records[-1]["content"] = "第四次回答已变化"
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_context(
            _session_request(
                view="messages",
                cursor=first.next_cursor,
            )
        )
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_context(
            _session_request(
                revision=first.revision,
            )
        )


@pytest.mark.asyncio
async def test_search_literal_by_default_regex_explicit_and_structured_match_can_be_read(
    _service,
):
    service = _service(_FakeMessageSource())
    literal = await service.search_context(
        _session_search_request(
            query="问题 BETA",
            max_chars=1024,
        )
    )
    assert literal.returned_chars == len(literal.model_dump_json())
    assert literal.returned_chars <= 1024
    assert literal.total_matches == 1
    match = literal.matches[0]
    assert match.vrn == session_resource_vrn(BACKEND_WORKSPACE_ID, "ses_target")
    assert match.resource_identity == session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target")
    assert match.record_index == 4
    assert match.revision.startswith("content:")

    expanded = await service.read_context(
        _session_request(
            view="records",
            record_index=match.record_index,
            revision=match.revision,
        )
    )
    assert expanded.items[0].text == "第二个问题 BETA"

    regex = await service.search_context(
        _session_search_request(
            query=r"(GAMMA|DELTA)$",
            match_mode="regex",
        )
    )
    assert regex.total_matches == 2


@pytest.mark.asyncio
async def test_inventory_information_and_output_budget(_service):
    service = _service(_FakeMessageSource())
    inventory = await service.read_context(
        _workspace_inventory_request(
            view="inventory",
            limit=1,
        )
    )
    assert len(inventory.items) == 1
    assert inventory.has_more is True
    assert inventory.items[0].kind == "session"
    assert inventory.items[0].vrn == session_resource_vrn(BACKEND_WORKSPACE_ID, "ses_target")

    information = await service.read_context(
        _session_request(
            view="information",
        )
    )
    assert information.items[0].data is not None
    assert information.items[0].data["execution"]["status"] == "running"

    with pytest.raises(ValueError, match="max_chars 太小.*资源身份"):
        await service.read_context(
            _session_request(
                view="records",
                include=["raw_record"],
                max_chars=1024,
            )
        )

    budgeted = await service.read_context(
        _session_request(
            view="records",
            include=["raw_record"],
            max_chars=2048,
        )
    )
    assert budgeted.returned_chars == len(budgeted.model_dump_json())
    assert budgeted.returned_chars <= 2048
    assert budgeted.truncated is True


@pytest.mark.asyncio
async def test_overview_complete_dto_respects_budget_and_cursor_advances(_service):
    service = _service(_FakeMessageSource())
    cursor = None
    seen_cursors: set[str] = set()
    pages = 0
    chunk_items = 0

    for _ in range(30):
        page = await service.read_context(
            _session_request(
                max_chars=1600,
                cursor=cursor,
            )
        )
        pages += 1
        assert page.returned_chars == len(page.model_dump_json())
        assert page.returned_chars <= 1600
        chunk_items += sum(item.kind.endswith("_chunk") for item in page.items)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        assert page.next_cursor not in seen_cursors
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        pytest.fail("overview cursor 未在 30 页内结束")
    assert pages > 1
    assert chunk_items > 0


@pytest.mark.asyncio
async def test_oversized_single_record_can_be_reassembled_through_cursor(_service):
    message_source = _FakeMessageSource()
    oversized_text = "分段内容" * 20_000 + "-END"
    message_source.records = [
        {"role": "assistant", "type": "ai", "content": oversized_text}
    ]
    service = _service(message_source)
    cursor = None
    chunks: list[str] = []

    for _ in range(100):
        page = await service.read_context(
            _session_request(
                view="records",
                record_index=0,
                include=["raw_record"],
                max_chars=4096,
                cursor=cursor,
            )
        )
        assert page.returned_chars == len(page.model_dump_json())
        assert page.returned_chars <= 4096
        assert len(page.items) == 1
        assert page.items[0].text is not None
        chunks.append(page.items[0].text)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    else:
        pytest.fail("超大单记录在 100 页内未完成读取")

    reassembled = "".join(chunks)
    assert oversized_text in reassembled
    assert reassembled.endswith("}")


@pytest.mark.asyncio
async def test_read_cursor_offset_beyond_items_is_rejected(_service) -> None:
    """游标 offset 越过真实 item 数时必须 fail-closed，不能静默返回空页。

    回归：cursor offset 是客户端可伪造的分页输入，越界时 ``items[offset:...]``
    只返回空列表且 ``has_more=False``。调用方按契约继续翻页会永远拿到空页，
    与同族已修的「limit<=0 返回永不前进空页」是同一类静默失败。
    """
    service = _service(_FakeMessageSource())
    request = _session_request(
        view="messages",
    )
    first = await service.read_context(request)
    assert len(first.items) == 9
    assert first.next_cursor is None

    beyond = SessionContextCursorCodec.encode(
        resource_identity=request.resource_identity,
        vrn=request.vrn,
        scope=request.scope,
        revision=first.revision,
        operation=session_context_read_operation(
            view=request.view,
            include=request.include,
            recent_rounds=request.recent_rounds,
            include_initial_goal=request.include_initial_goal,
        ),
        offset=len(first.items) + 1,
    )
    with pytest.raises(ValueError, match="offset 越界"):
        await service.read_context(request.model_copy(update={"cursor": beyond}))

    # 恰好等于 item 数是合法的「已读完」边界，仍返回空页且不报错。
    tail = await service.read_context(
        request.model_copy(
            update={
                "cursor": SessionContextCursorCodec.encode(
                    resource_identity=request.resource_identity,
                    vrn=request.vrn,
                    scope=request.scope,
                    revision=first.revision,
                    operation=session_context_read_operation(
                        view=request.view,
                        include=request.include,
                        recent_rounds=request.recent_rounds,
                        include_initial_goal=request.include_initial_goal,
                    ),
                    offset=len(first.items),
                )
            }
        )
    )
    assert tail.items == []
    assert tail.has_more is False


@pytest.mark.asyncio
async def test_search_cursor_offset_beyond_matches_is_rejected(_service) -> None:
    """search 的 cursor offset 越过总匹配数时同样必须 fail-closed。"""
    service = _service(_FakeMessageSource())
    request = _session_search_request(
        query="回答",
        sources=["effective_context"],
        match_mode="literal",
        max_results=1,
    )
    first = await service.search_context(request)
    assert first.total_matches > 0
    assert first.next_cursor is not None

    beyond = _resign_cursor_offset(first.next_cursor, first.total_matches + 1)
    with pytest.raises(ValueError, match="offset 越界"):
        await service.search_context(request.model_copy(update={"cursor": beyond}))
