from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionInformationExecutionDTO,
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


class _FakeSessionLookup:
    def __init__(self) -> None:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        self.sessions = [
            SessionDTO(
                session_id="ses_target",
                workspace_id="ws_one",
                title="目标会话",
                current_agent_id="default",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            SessionDTO(
                session_id="ses_other",
                workspace_id="ws_one",
                title="其他会话",
                current_agent_id="reviewer",
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
            items=items[skip:skip + limit],
            total=len(items),
        )


class _FakeMessageSource:
    def __init__(self) -> None:
        self.checkpoint_id = "ckpt-1"
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

    async def get_agent_context_state(self, session_id: str) -> dict[str, object]:
        del session_id
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
            session=session,
            workspace=SessionInformationWorkspaceDTO(
                workspace_id="ws_one",
                name="测试工作区",
                root_path="/workspace",
            ),
            storage_path=(
                "/workspace/.boxteam/sessions/项目会话--12345678/"
                "目标会话--87654321"
            ),
            execution=SessionInformationExecutionDTO(
                status="running",
                current_tool="read",
            ),
            trace=SessionInformationTraceDTO(),
        )


def _service(message_source: _FakeMessageSource) -> SessionContextQueryService:
    service = SessionContextQueryService(
        message_source=message_source,
        session_lookup=_FakeSessionLookup(),
    )
    service.bind_information_source(_FakeInformationSource())
    return service


@pytest.mark.asyncio
async def test_overview_defaults_to_initial_goal_recent_three_rounds_and_safe_content():
    result = await _service(_FakeMessageSource()).read_context(
        SessionContextReadRequest(resource="boxteam://session/ses_target")
    )

    serialized = result.model_dump_json()
    assert result.revision == "ckpt-1"
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


@pytest.mark.asyncio
async def test_overview_handles_fewer_user_rounds_than_default_window():
    message_source = _FakeMessageSource()
    message_source.records = [
        {"role": "user", "content": "唯一问题"},
        {"role": "assistant", "content": "唯一回答"},
    ]

    result = await _service(message_source).read_context(
        SessionContextReadRequest(resource="boxteam://session/ses_target")
    )

    serialized = result.model_dump_json()
    assert "唯一问题" in serialized
    assert "唯一回答" in serialized


@pytest.mark.asyncio
async def test_records_can_explicitly_include_detailed_fields():
    result = await _service(_FakeMessageSource()).read_context(
        SessionContextReadRequest(
            resource="boxteam://session/ses_target",
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
async def test_read_cursor_is_opaque_and_fails_when_revision_changes():
    source = _FakeMessageSource()
    service = _service(source)
    first = await service.read_context(
        SessionContextReadRequest(
            resource="boxteam://session/ses_target",
            view="messages",
            limit=2,
        )
    )
    assert first.has_more is True
    assert first.next_cursor is not None

    second = await service.read_context(
        SessionContextReadRequest(
            resource="boxteam://session/ses_target",
            view="messages",
            limit=2,
            cursor=first.next_cursor,
        )
    )
    assert second.items[0].locator != first.items[0].locator

    source.checkpoint_id = "ckpt-2"
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_context(
            SessionContextReadRequest(
                resource="boxteam://session/ses_target",
                view="messages",
                cursor=first.next_cursor,
            )
        )
    with pytest.raises(SessionContextRevisionChangedError):
        await service.read_context(
            SessionContextReadRequest(
                resource="boxteam://session/ses_target",
                expected_revision="ckpt-1",
            )
        )


@pytest.mark.asyncio
async def test_search_literal_by_default_regex_explicit_and_locator_can_be_read():
    service = _service(_FakeMessageSource())
    literal = await service.search_context(
        SessionContextSearchRequest(
            resource="boxteam://session/ses_target",
            query="问题 BETA",
            max_chars=1024,
        )
    )
    assert literal.returned_chars == len(literal.model_dump_json())
    assert literal.returned_chars <= 1024
    assert literal.total_matches == 1
    match = literal.matches[0]
    assert match.locator.endswith("#record=4")
    assert match.revision == "ckpt-1"

    expanded = await service.read_context(
        SessionContextReadRequest(
            resource=match.locator,
            expected_revision=match.revision,
        )
    )
    assert expanded.items[0].text == "第二个问题 BETA"

    regex = await service.search_context(
        SessionContextSearchRequest(
            resource="boxteam://session/ses_target",
            query=r"(GAMMA|DELTA)$",
            match_mode="regex",
        )
    )
    assert regex.total_matches == 2


@pytest.mark.asyncio
async def test_inventory_information_and_output_budget():
    service = _service(_FakeMessageSource())
    inventory = await service.read_context(
        SessionContextReadRequest(
            resource="boxteam://workspace/ws_one/sessions",
            view="inventory",
            limit=1,
        )
    )
    assert len(inventory.items) == 1
    assert inventory.has_more is True
    assert inventory.items[0].locator.startswith("boxteam://workspace/ws_one/session/")

    information = await service.read_context(
        SessionContextReadRequest(
            resource="boxteam://workspace/ws_one/session/ses_target",
            view="information",
        )
    )
    assert information.items[0].data is not None
    assert information.items[0].data["execution"]["status"] == "running"

    budgeted = await service.read_context(
        SessionContextReadRequest(
            resource="boxteam://session/ses_target",
            view="records",
            include=["raw_record"],
            max_chars=1024,
        )
    )
    assert budgeted.returned_chars == len(budgeted.model_dump_json())
    assert budgeted.returned_chars <= 1024
    assert budgeted.truncated is True


@pytest.mark.asyncio
async def test_overview_complete_dto_respects_budget_and_cursor_advances():
    service = _service(_FakeMessageSource())
    cursor = None
    seen_cursors: set[str] = set()

    for _ in range(30):
        page = await service.read_context(
            SessionContextReadRequest(
                resource="boxteam://session/ses_target",
                max_chars=1024,
                cursor=cursor,
            )
        )
        assert page.returned_chars == len(page.model_dump_json())
        assert page.returned_chars <= 1024
        if not page.has_more:
            break
        assert page.next_cursor is not None
        assert page.next_cursor not in seen_cursors
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        pytest.fail("overview cursor 未在 30 页内结束")


@pytest.mark.asyncio
async def test_oversized_single_record_can_be_reassembled_through_cursor():
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
            SessionContextReadRequest(
                resource="boxteam://session/ses_target#record=0",
                view="records",
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
