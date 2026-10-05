from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.abstractions.session_context import (
    SessionContextAssemblySourceProtocol,
    SessionContextMessageSourceProtocol,
    SessionInformationSourceProtocol,
    SessionLookupProtocol,
)
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchMatchDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
    SessionContextSearchSource,
)
from app.services.business.session_context_assembly_query_service import (
    SessionContextAssemblyQueryService,
)
from app.services.business.session_context_projection import (
    is_effective_user,
    paginate_read_items,
    project_record_items,
    public_session_data,
    sessions_revision,
    tool_summary,
    visible_text,
)
from app.services.business.session_context_resource import (
    ParsedSessionContextResource,
    SessionContextCursorCodec,
    parse_session_context_ref,
    require_cursor_offset_in_range,
    require_session_context_revision,
    session_context_read_operation,
    session_resource_identity,
    session_resource_uri,
    set_exact_returned_chars,
    validate_session_context_read_view,
    workspace_session_inventory_identity,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceIdentity,
)


@dataclass(frozen=True, slots=True)
class _SessionContextSnapshot:
    resource_identity: ResourceIdentity
    uri: str
    scope: str
    session_id: str
    revision: str
    records: list[dict[str, object]]
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None


@dataclass(frozen=True, slots=True)
class _SearchCandidate:
    resource_identity: ResourceIdentity
    uri: str
    scope: str
    source: SessionContextSearchSource
    text: str
    revision: str
    record_index: int | None = None


class SessionContextQueryService:
    """提供类似 read/grep 的渐进式结构化上下文查询。"""

    def __init__(
        self,
        *,
        message_source: SessionContextMessageSourceProtocol,
        session_lookup: SessionLookupProtocol,
        assembly_source: SessionContextAssemblySourceProtocol,
        workspace_id: str,
    ) -> None:
        self._message_source = message_source
        self._session_lookup = session_lookup
        self._assembly_source = assembly_source
        self._assembly_query = SessionContextAssemblyQueryService(
            assembly_source=assembly_source
        )
        self._workspace_id = workspace_id
        self._information_source: SessionInformationSourceProtocol | None = None

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def bind_information_source(
        self,
        information_source: SessionInformationSourceProtocol,
    ) -> None:
        """延迟绑定组合服务，避免容器构造形成循环依赖。"""

        self._information_source = information_source

    async def read_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        resource = parse_session_context_ref(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
        )
        validate_session_context_read_view(
            resource,
            request.view,
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )
        self._require_workspace_resource(resource)

        if request.view == "assembly":
            session = await self._load_session(resource)
            return await self._assembly_query.read_assembly(
                resource,
                request,
                session,
            )
        if request.view == "assemblies":
            session = await self._load_session(resource)
            return await self._assembly_query.read_assemblies(
                resource,
                request,
                session,
            )

        if resource.kind == "workspace_sessions":
            return await self._read_inventory(resource, request)

        snapshot = await self._load_snapshot(resource)
        require_session_context_revision(request.revision, snapshot.revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            revision=snapshot.revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )

        if request.record_index is not None:
            items = self._selected_record_items(snapshot, resource, request)
        elif request.view == "information":
            items = [await self._information_item(snapshot)]
        elif request.view == "overview":
            items = await self._overview_items(snapshot, request)
        else:
            items = project_record_items(
                resource_identity=resource.resource_identity,
                uri=resource.uri,
                scope=resource.scope,
                records=snapshot.records,
                include=set(request.include),
                messages_only=request.view == "messages",
            )
        return paginate_read_items(
            request=request,
            revision=snapshot.revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            compacted=snapshot.compacted,
            compaction_cutoff=snapshot.compaction_cutoff,
            raw_message_count=snapshot.raw_message_count,
            effective_record_count=len(snapshot.records),
        )

    def _selected_record_items(
        self,
        snapshot: _SessionContextSnapshot,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> list[SessionContextItemDTO]:
        record_index = request.record_index
        if record_index is None:
            raise RuntimeError("record_index 结构化选择缺失")
        if record_index >= len(snapshot.records):
            raise ValueError(
                f"record_index 越界: index={record_index}, total={len(snapshot.records)}"
            )
        return project_record_items(
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            records=snapshot.records,
            include=set(request.include),
            messages_only=False,
            indexes=[record_index],
        )

    async def search_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        resource = parse_session_context_ref(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
        )
        self._require_workspace_resource(resource)
        candidates, revision = await self._search_candidates(resource, request.sources)
        require_session_context_revision(request.revision, revision)
        query_hash = hashlib.sha256(
            json.dumps(
                {
                    "query": request.query,
                    "sources": request.sources,
                    "match_mode": request.match_mode,
                    "case_sensitive": request.case_sensitive,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            revision=revision,
            operation=f"search:{query_hash}",
        )
        if char_offset != 0:
            raise ValueError("search cursor 不支持 item 内字符偏移")
        expression = self._compile_search_expression(request)
        found: list[tuple[_SearchCandidate, re.Match[str]]] = []
        for candidate in candidates:
            match = expression.search(candidate.text)
            if match is not None:
                found.append((candidate, match))

        require_cursor_offset_in_range(offset, len(found), uri=resource.uri)
        selected = found[offset : offset + request.max_results]
        matches: list[SessionContextSearchMatchDTO] = []
        budget_truncated = False
        for candidate, match in selected:
            preview_start = max(0, match.start() - 180)
            preview_end = min(len(candidate.text), match.end() + 180)
            preview = candidate.text[preview_start:preview_end]
            dto = SessionContextSearchMatchDTO(
                resource_identity=candidate.resource_identity,
                uri=candidate.uri,
                scope=candidate.scope,
                preview=preview,
                source=candidate.source,
                revision=candidate.revision,
                record_index=candidate.record_index,
                match_start=match.start(),
                match_end=match.end(),
            )
            candidate_result = self._build_search_result(
                request=request,
                revision=revision,
                query_hash=query_hash,
                matches=[*matches, dto],
                offset=offset,
                total_matches=len(found),
                budget_truncated=budget_truncated,
            )
            if candidate_result.returned_chars <= request.max_chars:
                matches.append(dto)
                continue
            if matches:
                budget_truncated = True
                break
            clipped = self._largest_fitting_search_match(
                request=request,
                revision=revision,
                query_hash=query_hash,
                match=dto,
                offset=offset,
                total_matches=len(found),
            )
            if clipped is None:
                raise ValueError("max_chars 太小，无法返回包含资源身份的最小搜索结果")
            matches.append(clipped)
            budget_truncated = True
        result = self._build_search_result(
            request=request,
            revision=revision,
            query_hash=query_hash,
            matches=matches,
            offset=offset,
            total_matches=len(found),
            budget_truncated=budget_truncated,
        )
        if result.returned_chars > request.max_chars:
            raise RuntimeError("上下文搜索器生成了超过 max_chars 的响应")
        return result

    @classmethod
    def _largest_fitting_search_match(
        cls,
        *,
        request: SessionContextSearchRequest,
        revision: str,
        query_hash: str,
        match: SessionContextSearchMatchDTO,
        offset: int,
        total_matches: int,
    ) -> SessionContextSearchMatchDTO | None:
        low = 0
        high = len(match.preview)
        best: SessionContextSearchMatchDTO | None = None
        while low <= high:
            length = (low + high) // 2
            clipped = match.model_copy(update={"preview": match.preview[:length]})
            result = cls._build_search_result(
                request=request,
                revision=revision,
                query_hash=query_hash,
                matches=[clipped],
                offset=offset,
                total_matches=total_matches,
                budget_truncated=True,
            )
            if result.returned_chars <= request.max_chars:
                best = clipped
                low = length + 1
            else:
                high = length - 1
        return best

    @staticmethod
    def _build_search_result(
        *,
        request: SessionContextSearchRequest,
        revision: str,
        query_hash: str,
        matches: list[SessionContextSearchMatchDTO],
        offset: int,
        total_matches: int,
        budget_truncated: bool,
    ) -> SessionContextSearchResultDTO:
        consumed = len(matches)
        has_more = offset + consumed < total_matches
        next_cursor = None
        if has_more:
            next_cursor = SessionContextCursorCodec.encode(
                resource_identity=request.resource_identity,
                uri=request.uri,
                scope=request.scope,
                revision=revision,
                operation=f"search:{query_hash}",
                offset=offset + consumed,
            )
        result = SessionContextSearchResultDTO(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            query=request.query,
            match_mode=request.match_mode,
            revision=revision,
            truncated=budget_truncated or has_more,
            has_more=has_more,
            next_cursor=next_cursor,
            total_matches=total_matches,
            matches=matches,
        )
        return set_exact_returned_chars(result, operation="search_context")

    async def _overview_items(
        self,
        snapshot: _SessionContextSnapshot,
        request: SessionContextReadRequest,
    ) -> list[SessionContextItemDTO]:
        session = await self._session_lookup.get(snapshot.session_id)
        items = [
            SessionContextItemDTO(
                kind="session",
                resource_identity=snapshot.resource_identity,
                uri=snapshot.uri,
                scope=snapshot.scope,
                data=public_session_data(session),
            )
        ]
        user_indexes = [
            index
            for index, record in enumerate(snapshot.records)
            if is_effective_user(record)
        ]
        selected_indexes: list[int] = []
        if request.include_initial_goal and user_indexes:
            selected_indexes.append(user_indexes[0])
        if user_indexes:
            recent_start = (
                user_indexes[-request.recent_rounds]
                if len(user_indexes) >= request.recent_rounds
                else user_indexes[0]
            )
            selected_indexes.extend(range(recent_start, len(snapshot.records)))

        seen: set[int] = set()
        unique_indexes: list[int] = []
        for index in selected_indexes:
            if index not in seen:
                seen.add(index)
                unique_indexes.append(index)
        record_items = project_record_items(
            resource_identity=snapshot.resource_identity,
            uri=snapshot.uri,
            scope=snapshot.scope,
            records=snapshot.records,
            include=set(request.include),
            messages_only=True,
            indexes=unique_indexes,
        )
        items.extend(record_items)
        if self._information_source is not None:
            information = await self._information_source.get_information(
                snapshot.session_id
            )
            items.append(
                SessionContextItemDTO(
                    kind="execution",
                    resource_identity=snapshot.resource_identity,
                    uri=snapshot.uri,
                    scope=snapshot.scope,
                    data=information.execution.model_dump(mode="json"),
                )
            )
        return items

    async def _information_item(
        self,
        snapshot: _SessionContextSnapshot,
    ) -> SessionContextItemDTO:
        if self._information_source is None:
            raise RuntimeError(
                "SessionContextQueryService 尚未绑定 information source，无法读取 information view"
            )
        information = await self._information_source.get_information(
            snapshot.session_id
        )
        return SessionContextItemDTO(
            kind="information",
            resource_identity=snapshot.resource_identity,
            uri=snapshot.uri,
            scope=snapshot.scope,
            data=information.model_dump(mode="json"),
        )

    async def _read_inventory(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        sessions = await self._list_sessions(self._workspace_id)
        revision = sessions_revision(sessions)
        require_session_context_revision(request.revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            revision=revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )
        items = [
            SessionContextItemDTO(
                kind="session",
                resource_identity=session_resource_identity(
                    session.workspace_id,
                    session.session_id,
                ),
                uri=session_resource_uri(session.workspace_id, session.session_id),
                scope="workspace",
                data=public_session_data(session),
            )
            for session in sessions
        ]
        return paginate_read_items(
            request=request,
            revision=revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
        )

    async def _search_candidates(
        self,
        resource: ParsedSessionContextResource,
        sources: list[SessionContextSearchSource],
    ) -> tuple[list[_SearchCandidate], str]:
        if not sources:
            raise ValueError("sources 不能为空")
        candidates: list[_SearchCandidate] = []
        revisions: list[str] = []
        if resource.kind == "session":
            snapshot = await self._load_snapshot(resource)
            revisions.append(snapshot.revision)
            if "session_catalog" in sources:
                session = await self._session_lookup.get(snapshot.session_id)
                candidates.append(
                    _SearchCandidate(
                        resource_identity=snapshot.resource_identity,
                        uri=snapshot.uri,
                        scope=snapshot.scope,
                        source="session_catalog",
                        text=json.dumps(
                            public_session_data(session), ensure_ascii=False
                        ),
                        revision=snapshot.revision,
                    )
                )
            await self._append_session_search_candidates(
                candidates,
                snapshot,
                sources,
            )
            return candidates, snapshot.revision
        else:
            sessions = await self._list_sessions(self._workspace_id)
            revisions.append(sessions_revision(sessions))
            for session in sessions:
                session_id = session.session_id
                session_uri = session_resource_uri(
                    session.workspace_id,
                    session_id,
                )
                session_identity = session_resource_identity(
                    session.workspace_id,
                    session_id,
                )
                session_resource = parse_session_context_ref(
                    resource_identity=session_identity,
                    uri=session_uri,
                    scope="workspace",
                )
                snapshot = await self._load_snapshot(session_resource)
                revisions.append(snapshot.revision)
                if "session_catalog" in sources:
                    candidates.append(
                        _SearchCandidate(
                            resource_identity=session_identity,
                            uri=session_uri,
                            scope="workspace",
                            source="session_catalog",
                            text=json.dumps(
                                public_session_data(session), ensure_ascii=False
                            ),
                            revision=snapshot.revision,
                        )
                    )
                if {"effective_context", "session_information"}.intersection(sources):
                    await self._append_session_search_candidates(
                        candidates,
                        snapshot,
                        sources,
                    )
        revision = hashlib.sha256("\n".join(revisions).encode("utf-8")).hexdigest()
        return candidates, revision

    async def _append_session_search_candidates(
        self,
        candidates: list[_SearchCandidate],
        snapshot: _SessionContextSnapshot,
        sources: list[SessionContextSearchSource],
    ) -> None:
        if "effective_context" in sources:
            for index, record in enumerate(snapshot.records):
                text = visible_text(record)
                summary = tool_summary(record)
                searchable = "\n".join([text, *summary]).strip()
                if searchable:
                    candidates.append(
                        _SearchCandidate(
                            resource_identity=snapshot.resource_identity,
                            uri=snapshot.uri,
                            scope=snapshot.scope,
                            source="effective_context",
                            text=searchable,
                            revision=snapshot.revision,
                            record_index=index,
                        )
                    )
        if "session_information" in sources:
            if self._information_source is None:
                raise RuntimeError(
                    "SessionContextQueryService 尚未绑定 information source，"
                    "无法搜索 session_information"
                )
            information = await self._information_source.get_information(
                snapshot.session_id
            )
            candidates.append(
                _SearchCandidate(
                    resource_identity=snapshot.resource_identity,
                    uri=snapshot.uri,
                    scope=snapshot.scope,
                    source="session_information",
                    text=information.model_dump_json(),
                    revision=snapshot.revision,
                )
            )

    async def _load_snapshot(
        self,
        resource: ParsedSessionContextResource,
    ) -> _SessionContextSnapshot:
        if resource.session_id is None:
            raise ValueError(f"URI 不是单个 session 资源: {resource.uri}")
        session = await self._load_session(resource)
        if session.thread_id is None:
            raise RuntimeError(
                "SessionDTO 缺少权威 main thread_id: "
                f"session_id={session.session_id}"
            )
        state = await self._message_source.get_agent_context_state(
            session.session_id,
            session.thread_id,
        )
        encoded = json.dumps(
            {
                "records": state["records"],
                "raw_message_count": state["raw_message_count"],
                "compacted": state["compacted"],
                "compaction_cutoff": state["compaction_cutoff"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        # checkpoint_id 只用于 Saver 定位，不能作为查询 revision：同一份
        # 有效快照可能通过不同查询入口返回，而 checkpoint id 不表示投影内容。
        # revision 必须由查询面实际看到的快照计算，保证 read/search 可以复用。
        revision = f"content:{hashlib.sha256(encoded).hexdigest()}"
        return _SessionContextSnapshot(
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            session_id=resource.session_id,
            revision=revision,
            records=state["records"],
            raw_message_count=state["raw_message_count"],
            compacted=state["compacted"],
            compaction_cutoff=state["compaction_cutoff"],
        )

    def _require_workspace_resource(
        self,
        resource: ParsedSessionContextResource,
    ) -> None:
        if resource.scope != "workspace" or resource.scope_id != self._workspace_id:
            raise ResourceUriResolveError(
                "scope_mismatch",
                "session context URI 未绑定当前后端工作区: "
                f"scope={resource.scope!r} scope_id={resource.scope_id!r}",
            )
        if resource.kind == "workspace_sessions":
            expected_identity = workspace_session_inventory_identity(self._workspace_id)
        elif resource.kind == "session" and resource.session_id is not None:
            expected_identity = session_resource_identity(
                self._workspace_id,
                resource.session_id,
            )
        else:
            raise ResourceUriResolveError(
                "unsupported_view",
                f"session context 不支持该 URI resource kind: {resource.uri!r}",
            )
        if resource.resource_identity != expected_identity:
            raise ValueError(
                "resource_identity 与真实 Workspace 身份和 URI 路径不一致"
            )

    async def _load_session(
        self,
        resource: ParsedSessionContextResource,
    ) -> SessionDTO:
        if resource.session_id is None:
            raise ValueError(f"URI 不是单个 session 资源: {resource.uri}")
        session = await self._session_lookup.get(resource.session_id)
        if session.workspace_id != self._workspace_id:
            raise ValueError(
                "source-mismatch: session DTO 不属于当前 backend_workspace_id: "
                f"session_id={session.session_id} workspace_id={session.workspace_id}"
            )
        if session.workspace_id != resource.scope_id:
            raise ValueError(
                "source-mismatch: URI scope_id 与 SessionDTO.workspace_id 不一致: "
                f"uri_scope_id={resource.scope_id} session_workspace_id={session.workspace_id}"
            )
        expected_identity = session_resource_identity(
            session.workspace_id,
            session.session_id,
        )
        if resource.resource_identity != expected_identity:
            raise ValueError(
                "resource_identity 与 SessionDTO 所属工作区和 session_id 不一致"
            )
        return session

    @staticmethod
    def _compile_search_expression(
        request: SessionContextSearchRequest,
    ) -> re.Pattern[str]:
        pattern = (
            re.escape(request.query)
            if request.match_mode == "literal"
            else request.query
        )
        flags = 0 if request.case_sensitive else re.IGNORECASE
        try:
            return re.compile(pattern, flags)
        except re.error as error:
            raise ValueError(f"query 不是有效正则表达式: {error}") from error

    async def _list_sessions(self, workspace_id: str | None) -> list[SessionDTO]:
        result = await self._session_lookup.list(
            workspace_id=workspace_id,
            limit=100_000,
        )
        return result.items
