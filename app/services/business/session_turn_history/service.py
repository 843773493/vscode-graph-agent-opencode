"""协调 rollout-backed 会话历史与活动 Job 状态。"""
from __future__ import annotations

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_read import SessionThreadReaderProtocol
from app.core.exceptions import NotFoundError
from app.core.history_loading import HistoryLoadingConfig
from app.core.phase_observer import observe_phase
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnJobSummaryDTO,
)
from app.schemas.internal_v2.turn_search import (
    CanonicalContextContentReference,
    ExecutionOutputContentReference,
    SealedContextContentReference,
    ThreadItemInspectionDTO,
    ThreadItemInspectionRequest,
    ThreadItemLocationDTO,
    ThreadToolResultListDTO,
    TurnMessageSearchRequest,
    TurnMessageSearchResultsDTO,
)
from app.services.infrastructure.llm_request_log_reader import LLMRequestLogReader
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_history.message_search import (
    MessageSearchBudget,
)
from app.services.infrastructure.session_trace.store import TraceEventStore
from app.services.infrastructure.session_trace.trace_cursor import bind_trace_cursor
from app.services.mapping.itemized.history_turns.item_content import (
    context_source_contents,
    item_contents,
)

_BOOTSTRAP_ACTIVE_JOB_LIMIT = 8


class SessionTurnHistoryService:
    """历史内容只从 rollout 读取，活动 Job 仍由 JobService 提供。"""

    def __init__(
        self,
        *,
        checkpointer: RolloutCheckpointSaver,
        session_service: SessionThreadReaderProtocol,
        job_service: JobServiceProtocol,
        trace_event_store: TraceEventStore,
    ) -> None:
        self._checkpointer = checkpointer
        self._session_service = session_service
        self._job_service = job_service
        self._trace_event_store = trace_event_store

    async def bootstrap(
        self,
        session_id: str,
        *,
        thread_id: str | None = None,
        history_loading: HistoryLoadingConfig | None = None,
    ) -> SessionTurnBootstrapDTO:
        async with self._session_service.session_read_admission(session_id):
            session = await self._session_service.get(session_id)
            requested_thread_id = thread_id
            thread_id = session.thread_id if thread_id is None else await self._session_service.resolve_thread(session_id, thread_id)
            if thread_id is None:
                raise RuntimeError(
                    "SessionDTO 缺少权威 main thread 身份，无法解析历史归属（fail closed）: "
                    f"session_id={session_id}"
                )
            await observe_phase(thread_id, "history.read.guarded", details={
                "session_id": session_id, "thread_id": thread_id, "kind": "bootstrap",
            })
            latest, older_cursor, projection_epoch = await run_sync_shielded(
                self._checkpointer.bootstrap_history,
                session_id,
                thread_id,
                policy=history_loading,
            )
            pending = await self._job_service.list_pending_summaries(
                session_id,
                limit=_BOOTSTRAP_ACTIVE_JOB_LIMIT,
                thread_id=thread_id,
            )
            active_jobs: list[TurnJobSummaryDTO] = []
            if pending.active_job_id is not None:
                active_job = await self._job_service.get(pending.active_job_id)
                active_jobs.append(
                    TurnJobSummaryDTO(
                        job_id=active_job.job_id,
                        message_id=active_job.message_id,
                        status=active_job.status,
                        updated_at=active_job.updated_at,
                    )
                )
            active_jobs.extend(
                TurnJobSummaryDTO(
                    job_id=request.job_id,
                    message_id=request.message_id,
                    status=JobStatus.queued,
                    updated_at=request.updated_at,
                )
                for request in pending.requests
            )
            active_job_count = pending.request_count + (
                1 if pending.active_job_id is not None else 0
            )
            active_page = active_jobs[:_BOOTSTRAP_ACTIVE_JOB_LIMIT]
            return SessionTurnBootstrapDTO(
                session=session,
                thread_id=thread_id,
                latest_turn=latest,
                active_job_id=pending.active_job_id,
                active_jobs=active_page,
                active_job_count=active_job_count,
                active_jobs_truncated=len(active_page) < active_job_count,
                projection_state="ready",
                older_cursor=older_cursor,
                event_cursor=bind_trace_cursor(
                    self._trace_event_store.latest_event_cursor(session_id),
                    session_id=session_id, thread_id=requested_thread_id, kind="stream",
                ),
                projection_epoch=projection_epoch,
            )

    async def load_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        *,
        thread_id: str | None = None,
        history_loading: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        async with self._session_service.session_read_admission(session_id):
            thread_id = await self._session_service.resolve_main_thread(session_id) if thread_id is None else await self._session_service.resolve_thread(session_id, thread_id)
            await observe_phase(thread_id, "history.read.guarded", details={
                "session_id": session_id, "thread_id": thread_id, "kind": "history",
            })
            page = await run_sync_shielded(
                self._checkpointer.load_history,
                session_id,
                thread_id,
                request,
                policy=history_loading,
            )
            return page.model_copy(update={"thread_id": thread_id})

    async def inspect_item(self, session_id: str, thread_id: str, request: ThreadItemInspectionRequest,
                           *, request_logs: LLMRequestLogReader) -> ThreadItemInspectionDTO:
        async with self._session_service.session_read_admission(session_id):
            thread_id = await self._session_service.resolve_thread(session_id, thread_id)
            reference = request.content_ref
            if isinstance(reference, ExecutionOutputContentReference):
                if (reference.session_id, reference.thread_id) != (session_id, thread_id):
                    raise ValueError("执行输出引用不属于当前 SessionThread")
                return await run_sync_shielded(self._inspect_execution_output, reference, request.operation)
            if isinstance(reference, SealedContextContentReference):
                body = await run_sync_shielded(
                    self._checkpointer.read_context_contribution, session_id, thread_id,
                    historical_owner=(reference.session_id, reference.thread_id),
                    assembly_id=reference.assembly_id, contribution_id=reference.contribution_id,
                )
                return ThreadItemInspectionDTO(content_ref=reference, contents=context_source_contents(body))
            if isinstance(reference, CanonicalContextContentReference):
                request = request.model_copy(update={"item_id": reference.item_id, "content_ref": None})
            result = await run_sync_shielded(self._checkpointer.inspect_history_item, session_id, thread_id, request)
            location = result.location
            if location is None:
                raise RuntimeError("canonical Item inspection 缺少真实位置")
            result.content_ref = CanonicalContextContentReference(item_id=location.item_id)
            if request.operation == "trace":
                if not location.job_id:
                    raise NotFoundError("此 Item 没有所属执行 Trace")
                result.trace = await run_sync_shielded(self._trace_event_store.locate_job_start, session_id, thread_id, location.job_id)
            elif request.operation == "request":
                result.request_log = await run_sync_shielded(request_logs.find_item_request, session_id,
                    model_call_id=location.model_call_id, message_id=location.message_id, job_id=location.job_id)
            return result

    def _inspect_execution_output(self, reference: ExecutionOutputContentReference, operation: str) -> ThreadItemInspectionDTO:
        result = self._checkpointer.completed_derived_result(
            reference.session_id, reference.thread_id, reference.execution_id,
        )
        if result is None or not any(
            (ref["session_id"], ref["thread_id"], ref["ref_id"], ref["content_hash"]) ==
            (reference.session_id, reference.thread_id, reference.item_id, reference.content_hash)
            for ref in result["output_refs"]
        ):
            raise NotFoundError("引用不属于该完整执行的最终结果")
        item = self._checkpointer.get_canonical_item(
            reference.session_id, thread_id=reference.thread_id, item_id=reference.item_id,
        )
        if item is None or item.content_hash != reference.content_hash:
            raise RuntimeError("已发布执行输出的 canonical 正文缺失或 hash 不一致")
        byte_length = len(canonical_json_bytes(item.to_dict()))
        if byte_length > 8 * 1024 * 1024:
            raise ValueError("单个 Item 超过 8 MiB 内容读取上限，请打开其原始资源")
        contents = item_contents(item)
        if operation == "content_metadata":
            contents = [content.model_copy(update={"content": None}) for content in contents]
        return ThreadItemInspectionDTO(content_ref=reference, contents=contents, location=ThreadItemLocationDTO(
            item_id=item.item_id, execution_id=reference.execution_id, semantic_kind=item.semantic_kind,
            content_hash=item.content_hash, byte_length=byte_length,
            model_call_id=result["dispatch_ref"]["sealed_model_call_id"],
        ))

    async def list_tool_results(self, session_id: str, thread_id: str, *, after: int = 0,
                                limit: int = 100, view_id: str | None = None,
                                view_revision: int | None = None) -> ThreadToolResultListDTO:
        async with self._session_service.session_read_admission(session_id):
            thread_id = await self._session_service.resolve_thread(session_id, thread_id)
            return await run_sync_shielded(
                self._checkpointer.list_history_tool_results, session_id, thread_id,
                after=after, limit=limit, view_id=view_id, view_revision=view_revision,
            )

    async def search_messages(
        self, session_id: str, thread_id: str, request: TurnMessageSearchRequest,
        *, budget: MessageSearchBudget | None = None,
    ) -> TurnMessageSearchResultsDTO:
        async with self._session_service.session_read_admission(session_id):
            thread_id = await self._session_service.resolve_thread(session_id, thread_id)
            return await run_sync_shielded(
                self._checkpointer.search_history_messages, session_id, thread_id, request, budget=budget,
            )


__all__ = ["SessionTurnHistoryService"]
