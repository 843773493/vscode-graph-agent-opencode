from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest.mock import MagicMock, create_autospec

import pytest

from app.abstractions.session_context import (
    SessionContextAssemblySourceProtocol,
    SessionContextHistoryProjection,
    SessionContextRevisionChangedError,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef, ToolSetRef
from app.domain.itemized.selection import ContextSelectionEntry
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
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)

BACKEND_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True, slots=True)
class AssemblySelectionQueryFixture:
    query_service: SessionContextQueryService
    assembly_snapshot: MagicMock


def _resign_cursor_offset(cursor: str, offset: int) -> str:
    """保留原 cursor 的签名身份，只改写 offset，用于伪造越界游标。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    return SessionContextCursorCodec.encode(
        resource_identity=payload["resource_identity"],
        uri=payload["uri"],
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
        uri=session_resource_uri(BACKEND_WORKSPACE_ID, session_id),
        scope="workspace",
        **kwargs,
    )


def _workspace_inventory_request(**kwargs: object) -> SessionContextReadRequest:
    return SessionContextReadRequest(
        resource_identity=workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
        uri=workspace_session_inventory_uri(BACKEND_WORKSPACE_ID),
        scope="workspace",
        **kwargs,
    )


def _session_search_request(session_id: str = "ses_target", **kwargs: object) -> SessionContextSearchRequest:
    return SessionContextSearchRequest(
        resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, session_id),
        uri=session_resource_uri(BACKEND_WORKSPACE_ID, session_id),
        scope="workspace",
        **kwargs,
    )


def _workspace_inventory_search_request(**kwargs: object) -> SessionContextSearchRequest:
    return SessionContextSearchRequest(
        resource_identity=workspace_session_inventory_identity(BACKEND_WORKSPACE_ID),
        uri=workspace_session_inventory_uri(BACKEND_WORKSPACE_ID),
        scope="workspace",
        **kwargs,
    )


class _FakeSessionLookup:
    def session_read_admission(self, session_id: str):
        return nullcontext()

    def __init__(self) -> None:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        self.sessions = [
            SessionDTO(
                session_id="ses_target",
                workspace_id=BACKEND_WORKSPACE_ID,
                resource_identity=session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target"),
                uri=session_resource_uri(BACKEND_WORKSPACE_ID, "ses_target"),
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
                uri=session_resource_uri(BACKEND_WORKSPACE_ID, "ses_other"),
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
            ),
            execution=SessionInformationExecutionDTO(
                status="running",
                current_tool="read",
            ),
            trace=SessionInformationTraceDTO(),
        )


@pytest.fixture
def session_context_service_factory() -> Callable[
    [_FakeMessageSource], SessionContextQueryService
]:
    assembly_source = create_autospec(
        SessionContextAssemblySourceProtocol, spec_set=True
    )
    assembly_source.get_context_assembly.side_effect = AssertionError(
        "active view 不得读取上一请求的 assembly"
    )
    assembly_source.project_context_plan_to_history_with_diagnostics.side_effect = (
        AssertionError("active view 不得裁剪为 assembly selection")
    )

    def build_session_context_query_service(
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

    return build_session_context_query_service


@pytest.mark.asyncio
async def test_overview_defaults_to_initial_goal_recent_three_rounds_and_safe_content(
    session_context_service_factory,
):
    message_source = _FakeMessageSource()
    result = await session_context_service_factory(message_source).read_context(
        _session_request()
    )

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
    assembly_source.project_context_plan_to_history_with_diagnostics.return_value = SessionContextHistoryProjection(
        assembly=snapshot, messages=[], losses=(), resource_activation=None,
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


@pytest.fixture
def assembly_selection_query() -> AssemblySelectionQueryFixture:
    selection = []
    for protection in ("public", "protected"):
        body = f"overlay 正文 {protection}"
        source_revision = sha256_jcs(f"来源版本 {protection}")
        content_hash = contribution_content_hash("overlay_delta", body)
        detail_ref = DetailRef("ses_target", "thread_target", "asm_target", f"detail-{protection}")
        ref = ContextRef(
            "request_only", f"source-{protection}",
            session_id="ses_target", thread_id="thread_target", plan_id="plan_target",
            source_revision=source_revision, content_length=len(canonical_json_bytes(body)),
            content_hash=content_hash, source_ref=detail_ref, protection=protection,
            base_delta_role="delta", source_overlay_epoch=1,
            overlay_from_revision=sha256_jcs(f"旧版本 {protection}"),
            overlay_to_revision=source_revision, overlay_diff_hash=sha256_jcs(body),
        )
        selection.append(ContextSelectionEntry(
            assembly_id="asm_target", plan_ordinal=len(selection), ref=ref,
            selection_kind="overlay_delta", protection=protection,
            source_revision=ref.source_revision, content_length=ref.content_length,
            content_hash=ref.content_hash, detail_ref=detail_ref,
            contribution_id=f"contribution-{protection}", contribution_ordinal=0,
            base_delta_role="delta", source_overlay_epoch=1,
            overlay_from_revision=ref.overlay_from_revision,
            overlay_to_revision=ref.overlay_to_revision, overlay_diff_hash=ref.overlay_diff_hash,
        ))
        tools = [{
            "type": "function",
            "function": {
                "name": f"tool_{protection}",
                "description": f"ToolSet 正文 {protection}",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        tool_ref = replace(ToolSetRef.from_tool_snapshot(
            snapshot_id=f"tools-{protection}", session_id="ses_target",
            thread_id="thread_target", plan_id="plan_target", assembly_id="asm_target",
            tools=tools, source_revision=sha256_jcs(tools),
            tool_policy={"mode": f"policy-{protection}"},
        ), protection=protection)
        tool_ref.validate_manifest()
        selection.append(ContextSelectionEntry(
            assembly_id="asm_target", plan_ordinal=len(selection), ref=tool_ref,
            selection_kind="tool_set", protection=protection,
            source_revision=tool_ref.source_revision, content_length=tool_ref.content_length,
            content_hash=tool_ref.content_hash,
        ))
    snapshot = MagicMock(
        assembly_id="asm_target", session_id="ses_target", plan_id="plan_target",
        plan_hash="plan-hash", request_hash="request-hash", history_view_revision=1,
        source_overlay_epoch=1, target_format="langchain", compiler_version="compiler",
        provider_version="provider", projector_id="projector", projector_version="1",
        selection=tuple(selection), loss=(),
    )
    assembly_source = create_autospec(SessionContextAssemblySourceProtocol, spec_set=True)
    assembly_source.list_context_assemblies.return_value = (snapshot,)
    assembly_source.project_context_plan_to_history_with_diagnostics.return_value = SessionContextHistoryProjection(
        assembly=snapshot, messages=[], losses=(), resource_activation=None,
    )
    service = SessionContextQueryService(
        message_source=_FakeMessageSource(), session_lookup=_FakeSessionLookup(),
        assembly_source=assembly_source, workspace_id=BACKEND_WORKSPACE_ID,
    )
    return AssemblySelectionQueryFixture(
        query_service=service,
        assembly_snapshot=snapshot,
    )


@pytest.mark.asyncio
async def test_default_assembly_projection_hides_sensitive_hashes_and_tool_body(
    assembly_selection_query: AssemblySelectionQueryFixture,
):
    service = assembly_selection_query.query_service
    snapshot = assembly_selection_query.assembly_snapshot
    internal = tuple(entry.to_dict() for entry in snapshot.selection)
    assembly_ref = context_assembly_identity(
        session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target"), "asm_target",
    )

    result = await service.read_context(_session_request(
        view="assembly", assembly_ref=assembly_ref, max_chars=65_536,
    ))

    selections = [item for item in result.items if item.kind == "selection"]
    assert len(selections) == len(snapshot.selection)
    serialized = result.model_dump_json()
    assert snapshot.plan_hash not in serialized
    assert snapshot.request_hash not in serialized
    assert "plan_hash" not in result.items[0].data
    assert "request_hash" not in result.items[0].data
    assert "ToolSet 正文 public" in serialized
    assert "ToolSet 正文 protected" not in serialized
    for item, entry, original in zip(selections, snapshot.selection, internal, strict=True):
        assert item.record_index == entry.plan_ordinal
        if entry.protection == "public":
            assert item.data == original
            continue
        assert item.data["protection"] == entry.protection
        assert item.data["content_length"] == entry.content_length
        assert item.data["detail_ref"] == original["detail_ref"]
        assert item.data["content_hash"] is None
        assert item.data["source_revision"] is None
        assert item.data["ref"]["content_hash"] is None
        assert item.data["ref"]["source_revision"] is None
        assert entry.content_hash not in serialized
        assert entry.source_revision not in serialized
        if isinstance(entry.ref, ToolSetRef):
            assert "tools" not in item.data["ref"]
            assert "tool_policy" not in item.data["ref"]
            entry.ref.validate_manifest()
        else:
            assert item.data["ref"]["source_ref"] == entry.detail_ref.to_dict()
            for name in ("overlay_from_revision", "overlay_to_revision", "overlay_diff_hash"):
                assert item.data[name] is None
                assert item.data["ref"][name] is None
                assert original[name] not in serialized
    assert tuple(entry.to_dict() for entry in snapshot.selection) == internal


@pytest.mark.asyncio
async def test_default_assembly_revision_and_cursor_use_structure_without_body_hashes(
    assembly_selection_query: AssemblySelectionQueryFixture,
):
    service = assembly_selection_query.query_service
    snapshot = assembly_selection_query.assembly_snapshot
    assembly_ref = context_assembly_identity(
        session_resource_identity(BACKEND_WORKSPACE_ID, "ses_target"), "asm_target",
    )
    request = _session_request(
        view="assembly", assembly_ref=assembly_ref, include=["visible_text"],
        limit=1, max_chars=65_536,
    )
    detail = await service.read_context(request)
    expected_revision = "assembly:asm_target:" + hashlib.sha256(
        json.dumps(["visible_text"]).encode("utf-8"),
    ).hexdigest()
    assert detail.revision == expected_revision
    assert detail.has_more and detail.next_cursor is not None
    payload = json.loads(base64.urlsafe_b64decode(
        detail.next_cursor + "=" * (-len(detail.next_cursor) % 4),
    ))
    assert payload["revision"] == expected_revision
    assert payload["assembly_ref"] == assembly_ref
    assert snapshot.plan_hash not in json.dumps(payload)
    assert snapshot.request_hash not in json.dumps(payload)
    original_hashes = (snapshot.plan_hash, snapshot.request_hash)
    snapshot.plan_hash, snapshot.request_hash = "changed-plan-hash", "changed-request-hash"
    continued = await service.read_context(request.model_copy(update={
        "cursor": detail.next_cursor, "revision": detail.revision,
    }))
    assert continued.revision == detail.revision
    assert continued.items[0].record_index == 0
    changed_include = request.model_copy(update={
        "cursor": detail.next_cursor, "include": ["reasoning"],
    })
    with pytest.raises(SessionContextRevisionChangedError, match="revision changed"):
        await service.read_context(changed_include)
    snapshot.plan_hash, snapshot.request_hash = original_hashes

    second = MagicMock(
        assembly_id="asm_second", session_id=snapshot.session_id,
        plan_id="plan_second", plan_hash="second-sensitive-plan-hash",
        history_view_revision=2, source_overlay_epoch=2,
        selection=snapshot.selection, target_format=snapshot.target_format,
    )
    service._assembly_source.list_context_assemblies.return_value = (snapshot, second)
    list_request = _session_request(view="assemblies", limit=1, max_chars=65_536)
    listing = await service.read_context(list_request)
    list_revision = "assemblies:" + hashlib.sha256(
        json.dumps(["asm_second", "asm_target"]).encode("utf-8"),
    ).hexdigest()
    assert listing.revision == list_revision
    assert listing.has_more and listing.next_cursor is not None
    assert listing.items[0].data["assembly_id"] == "asm_second"
    assert "plan_hash" not in listing.items[0].data
    list_payload = json.loads(base64.urlsafe_b64decode(
        listing.next_cursor + "=" * (-len(listing.next_cursor) % 4),
    ))
    assert list_payload["revision"] == list_revision
    assert original_hashes[0] not in json.dumps(list_payload)
    assert second.plan_hash not in json.dumps(list_payload)
    second.plan_hash = "changed-second-sensitive-plan-hash"
    next_listing = await service.read_context(list_request.model_copy(update={
        "cursor": listing.next_cursor, "revision": listing.revision,
    }))
    assert next_listing.revision == listing.revision
    assert next_listing.items[0].data["assembly_id"] == "asm_target"
    assert next_listing.has_more is False
    assert (snapshot.plan_hash, snapshot.request_hash) == original_hashes


@pytest.mark.asyncio
async def test_overview_handles_fewer_user_rounds_than_default_window(
    session_context_service_factory,
):
    message_source = _FakeMessageSource()
    message_source.records = [
        {"role": "user", "content": "唯一问题"},
        {"role": "assistant", "content": "唯一回答"},
    ]

    result = await session_context_service_factory(message_source).read_context(
        _session_request()
    )

    serialized = result.model_dump_json()
    assert "唯一问题" in serialized
    assert "唯一回答" in serialized


@pytest.mark.asyncio
async def test_records_can_explicitly_include_detailed_fields(
    session_context_service_factory,
):
    result = await session_context_service_factory(_FakeMessageSource()).read_context(
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
async def test_read_cursor_is_opaque_and_fails_when_revision_changes(
    session_context_service_factory,
):
    source = _FakeMessageSource()
    service = session_context_service_factory(source)
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
    session_context_service_factory,
):
    service = session_context_service_factory(_FakeMessageSource())
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
    assert match.uri == session_resource_uri(BACKEND_WORKSPACE_ID, "ses_target")
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
async def test_inventory_information_and_output_budget(
    session_context_service_factory,
):
    service = session_context_service_factory(_FakeMessageSource())
    inventory = await service.read_context(
        _workspace_inventory_request(
            view="inventory",
            limit=1,
        )
    )
    assert len(inventory.items) == 1
    assert inventory.has_more is True
    assert inventory.items[0].kind == "session"
    assert inventory.items[0].uri == session_resource_uri(BACKEND_WORKSPACE_ID, "ses_target")

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
async def test_overview_complete_dto_respects_budget_and_cursor_advances(
    session_context_service_factory,
):
    service = session_context_service_factory(_FakeMessageSource())
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
async def test_oversized_single_record_can_be_reassembled_through_cursor(
    session_context_service_factory,
):
    message_source = _FakeMessageSource()
    oversized_text = "分段内容" * 20_000 + "-END"
    message_source.records = [
        {"role": "assistant", "type": "ai", "content": oversized_text}
    ]
    service = session_context_service_factory(message_source)
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
async def test_read_cursor_offset_beyond_items_is_rejected(
    session_context_service_factory,
) -> None:
    """游标 offset 越过真实 item 数时必须 fail-closed，不能静默返回空页。

    回归：cursor offset 是客户端可伪造的分页输入，越界时 ``items[offset:...]``
    只返回空列表且 ``has_more=False``。调用方按契约继续翻页会永远拿到空页，
    与同族已修的「limit<=0 返回永不前进空页」是同一类静默失败。
    """
    service = session_context_service_factory(_FakeMessageSource())
    request = _session_request(
        view="messages",
    )
    first = await service.read_context(request)
    assert len(first.items) == 9
    assert first.next_cursor is None

    beyond = SessionContextCursorCodec.encode(
        resource_identity=request.resource_identity,
        uri=request.uri,
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
                    uri=request.uri,
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
async def test_search_cursor_offset_beyond_matches_is_rejected(
    session_context_service_factory,
) -> None:
    """search 的 cursor offset 越过总匹配数时同样必须 fail-closed。"""
    service = session_context_service_factory(_FakeMessageSource())
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
