from __future__ import annotations

import asyncio
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
    parse_session_context_resource,
    require_session_context_revision,
    set_exact_returned_chars,
    validate_session_context_read_view,
)


@dataclass(frozen=True, slots=True)
class _SessionContextSnapshot:
    resource: str
    session_id: str
    revision: str
    records: list[dict[str, object]]
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None


@dataclass(frozen=True, slots=True)
class _SearchCandidate:
    locator: str
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
    ) -> None:
        self._message_source = message_source
        self._session_lookup = session_lookup
        self._assembly_source = assembly_source
        self._information_source: SessionInformationSourceProtocol | None = None

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
        resource = parse_session_context_resource(request.resource)
        validate_session_context_read_view(resource, request.view)

        if request.view == "assembly":
            return await self._read_assembly(resource, request)
        if request.view == "assemblies":
            return await self._read_assemblies(resource, request)

        if resource.kind == "workspace_sessions":
            return await self._read_inventory(resource, request)

        snapshot = await self._load_snapshot(resource)
        require_session_context_revision(request.expected_revision, snapshot.revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource=resource.canonical,
            revision=snapshot.revision,
            operation=f"read:{request.view}",
        )

        if resource.selector is not None:
            items = await self._selected_locator_items(snapshot, resource, request)
        elif request.view == "information":
            items = [await self._information_item(snapshot)]
        elif request.view == "overview":
            items = await self._overview_items(snapshot, request)
        else:
            items = project_record_items(
                resource=snapshot.resource,
                records=snapshot.records,
                include=set(request.include),
                messages_only=request.view == "messages",
            )
        return paginate_read_items(
            request=request,
            resource=resource.canonical,
            revision=snapshot.revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            compacted=snapshot.compacted,
            compaction_cutoff=snapshot.compaction_cutoff,
            raw_message_count=snapshot.raw_message_count,
            effective_record_count=len(snapshot.records),
        )

    async def _read_assemblies(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        """只列 Saver 已封存快照；列表顺序不改变快照内部 selection。"""
        if resource.session_id is None:
            raise ValueError("assemblies 查询缺少 session")
        session = await self._session_lookup.get(resource.session_id)
        if (
            resource.workspace_id is not None
            and session.workspace_id != resource.workspace_id
        ):
            raise ValueError("source-mismatch: context resource 不属于当前 workspace")
        snapshots = await asyncio.to_thread(
            self._assembly_source.list_context_assemblies, resource.session_id
        )
        revision = (
            "assemblies:"
            + hashlib.sha256(
                json.dumps(
                    [
                        (snapshot.assembly_id, snapshot.plan_hash)
                        for snapshot in snapshots
                    ]
                ).encode("utf-8")
            ).hexdigest()
        )
        require_session_context_revision(request.expected_revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource=resource.canonical,
            revision=revision,
            operation="read:assemblies",
        )
        items = [
            SessionContextItemDTO(
                kind="assembly",
                locator=f"{resource.base}#assembly={snapshot.assembly_id}",
                data={
                    "session_id": snapshot.session_id,
                    "assembly_id": snapshot.assembly_id,
                    "plan_id": snapshot.plan_id,
                    "plan_hash": snapshot.plan_hash,
                    "history_view_revision": snapshot.history_view_revision,
                    "source_overlay_epoch": snapshot.source_overlay_epoch,
                    "selection_count": len(snapshot.selection),
                    "target_format": snapshot.target_format,
                },
            )
            for snapshot in reversed(snapshots)
        ]
        return paginate_read_items(
            request=request,
            resource=resource.canonical,
            revision=revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            effective_record_count=len(items),
        )

    async def _read_assembly(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        """冻结检查面不读取 active view，也不物化 request-only 正文。"""
        if resource.session_id is None or resource.selector is None:
            raise ValueError("assembly 查询缺少 session/assembly selector")
        session = await self._session_lookup.get(resource.session_id)
        if (
            resource.workspace_id is not None
            and session.workspace_id != resource.workspace_id
        ):
            raise ValueError("source-mismatch: context resource 不属于当前 workspace")
        snapshot = await asyncio.to_thread(
            self._assembly_source.get_context_assembly,
            resource.session_id,
            assembly_id=resource.selector.removeprefix("assembly="),
        )
        plan = snapshot.as_sealed_plan()
        # include 属于 projection 参数；不能用另一 include 的 cursor 跳过记录。
        include_hash = hashlib.sha256(
            json.dumps(sorted(set(request.include))).encode("utf-8")
        ).hexdigest()
        revision = (
            f"assembly:{snapshot.assembly_id}:{snapshot.plan_hash}:{include_hash}"
        )
        require_session_context_revision(request.expected_revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource=resource.canonical,
            revision=revision,
            operation="read:assembly",
        )
        messages, losses = await asyncio.to_thread(
            self._assembly_source.project_context_plan_to_history_with_diagnostics,
            resource.session_id,
            plan,
        )
        records = [message.model_dump(mode="json") for message in messages]
        items = [
            SessionContextItemDTO(
                kind="assembly",
                locator=resource.canonical,
                data={
                    "session_id": snapshot.session_id,
                    "assembly_id": snapshot.assembly_id,
                    "plan_id": snapshot.plan_id,
                    "plan_hash": snapshot.plan_hash,
                    "request_hash": snapshot.request_hash,
                    "compiler_version": snapshot.compiler_version,
                    "provider_version": snapshot.provider_version,
                    "projector_id": snapshot.projector_id,
                    "projector_version": snapshot.projector_version,
                    "target_format": snapshot.target_format,
                    "history_view_revision": snapshot.history_view_revision,
                    "source_overlay_epoch": snapshot.source_overlay_epoch,
                    "selection_count": len(snapshot.selection),
                    "history_message_count": len(messages),
                    "history_loss_count": len(losses),
                    "assembly_loss_count": len(snapshot.loss),
                    "projection": "history",
                },
            )
        ]
        # selection 完整保留 tagged ref / omission / loss / typed detail owner；
        # ToolSet manifest 只是检查数据，不生成 history 工具定义 message。
        items.extend(
            SessionContextItemDTO(
                kind="selection",
                locator=resource.canonical,
                record_index=entry.plan_ordinal,
                data=entry.to_dict(),
            )
            for entry in snapshot.selection
        )
        for item in project_record_items(
            resource=resource.base,
            records=records,
            include=set(request.include),
            messages_only=True,
        ):
            if item.record_index is None:
                raise RuntimeError("history projection 缺少 message record_index")
            item.locator = resource.canonical
            item.data = {
                "projection": "history",
                "message_id": records[item.record_index]["id"],
            }
            items.append(item)
        items.extend(
            SessionContextItemDTO(
                kind="loss",
                locator=resource.canonical,
                data={"projection": projection, "ordinal": ordinal, "loss": loss},
            )
            for projection, values in (("assembly", snapshot.loss), ("history", losses))
            for ordinal, loss in enumerate(values)
        )
        return paginate_read_items(
            request=request,
            resource=resource.canonical,
            revision=revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            effective_record_count=len(snapshot.selection),
            raw_message_count=len(messages),
        )

    async def _selected_locator_items(
        self,
        snapshot: _SessionContextSnapshot,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> list[SessionContextItemDTO]:
        selector = resource.selector
        if selector == "information":
            return [await self._information_item(snapshot)]
        if selector is None or not selector.startswith("record="):
            raise ValueError(f"不支持的 context locator: {resource.canonical}")
        record_index = int(selector.removeprefix("record="))
        if record_index >= len(snapshot.records):
            raise ValueError(
                f"record locator 越界: index={record_index}, total={len(snapshot.records)}"
            )
        return project_record_items(
            resource=snapshot.resource,
            records=snapshot.records,
            include=set(request.include),
            messages_only=False,
            indexes=[record_index],
        )

    async def search_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        resource = parse_session_context_resource(request.resource)
        if resource.selector is not None:
            raise ValueError(
                "search_context 的 resource 必须是资源根，不接受 record locator"
            )
        candidates, revision = await self._search_candidates(resource, request.sources)
        require_session_context_revision(request.expected_revision, revision)
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
            resource=resource.canonical,
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

        selected = found[offset : offset + request.max_results]
        matches: list[SessionContextSearchMatchDTO] = []
        budget_truncated = False
        for candidate, match in selected:
            preview_start = max(0, match.start() - 180)
            preview_end = min(len(candidate.text), match.end() + 180)
            preview = candidate.text[preview_start:preview_end]
            dto = SessionContextSearchMatchDTO(
                locator=candidate.locator,
                preview=preview,
                source=candidate.source,
                revision=candidate.revision,
                record_index=candidate.record_index,
                match_start=match.start(),
                match_end=match.end(),
            )
            candidate_result = self._build_search_result(
                request=request,
                resource=resource.canonical,
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
                resource=resource.canonical,
                revision=revision,
                query_hash=query_hash,
                match=dto,
                offset=offset,
                total_matches=len(found),
            )
            if clipped is None:
                raise ValueError("max_chars 太小，无法返回包含 locator 的最小搜索结果")
            matches.append(clipped)
            budget_truncated = True
        result = self._build_search_result(
            request=request,
            resource=resource.canonical,
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
        resource: str,
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
                resource=resource,
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
        resource: str,
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
                resource=resource,
                revision=revision,
                operation=f"search:{query_hash}",
                offset=offset + consumed,
            )
        result = SessionContextSearchResultDTO(
            resource=resource,
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
                locator=snapshot.resource,
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
            resource=snapshot.resource,
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
                    locator=f"{snapshot.resource}#information/execution",
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
            locator=f"{snapshot.resource}#information",
            data=information.model_dump(mode="json"),
        )

    async def _read_inventory(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        # workspace_id 由 Gateway 路由决定；远程投影 ID 与目标后端本地 ID 不同，
        # 目标后端只查询自己的 Session，不再用投影 ID 二次过滤。
        sessions = await self._list_sessions(None)
        revision = sessions_revision(sessions)
        require_session_context_revision(request.expected_revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource=resource.canonical,
            revision=revision,
            operation="read:inventory",
        )
        items = [
            SessionContextItemDTO(
                kind="session",
                locator=(
                    f"boxteam://workspace/{resource.workspace_id}/session/"
                    f"{session.session_id}"
                ),
                data=public_session_data(session),
            )
            for session in sessions
        ]
        return paginate_read_items(
            request=request,
            resource=resource.canonical,
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
                        locator=snapshot.resource,
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
            # Gateway 已经把请求路由到目标工作区，公开的投影 workspace_id
            # 只用于稳定 locator，不能拿来过滤目标后端的本地 workspace_id。
            sessions = await self._list_sessions(None)
            revisions.append(sessions_revision(sessions))
            for session in sessions:
                session_id = session.session_id
                session_resource = parse_session_context_resource(
                    f"boxteam://workspace/{resource.workspace_id}/session/{session_id}"
                )
                snapshot = await self._load_snapshot(session_resource)
                revisions.append(snapshot.revision)
                if "session_catalog" in sources:
                    candidates.append(
                        _SearchCandidate(
                            locator=(
                                f"boxteam://workspace/{resource.workspace_id}/session/"
                                f"{session_id}"
                            ),
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
                            locator=f"{snapshot.resource}#record={index}",
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
                    locator=f"{snapshot.resource}#information",
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
            raise ValueError(f"资源不是 session: {resource.canonical}")
        await self._session_lookup.get(resource.session_id)
        state = await self._message_source.get_agent_context_state(resource.session_id)
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
        # revision 必须由查询面实际看到的快照计算，保证 read/search/locator
        # 之间可以安全复用 expected_revision 和 cursor。
        revision = f"content:{hashlib.sha256(encoded).hexdigest()}"
        return _SessionContextSnapshot(
            resource=resource.base,
            session_id=resource.session_id,
            revision=revision,
            records=state["records"],
            raw_message_count=state["raw_message_count"],
            compacted=state["compacted"],
            compaction_cutoff=state["compaction_cutoff"],
        )

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
