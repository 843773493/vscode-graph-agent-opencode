"""缓存优先压缩中间件：同步与异步两条 wrap 链路。

保留已发送前缀、摘要中段、保留近期尾部；同步 wrap_model_call 与异步
awrap_model_call/_awrap_model_call_impl 是对等实现（语义差异只登记不合并）。"""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Mapping,
)
from dataclasses import replace
from typing import (
    Any,
    ClassVar,
)

# TODO: DeepAgents 暴露公共的压缩扩展基类后，改用公共 API，避免依赖私有实现类。
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
)
from langchain.agents.middleware.types import ExtendedModelResponse, hook_config
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
)
from langgraph.config import get_config
from langgraph.types import Command

from app.agents.model_call_execution import _runtime_checkpoint_ns
from app.agents.summarization.planning import (
    _prefix_cutoff_candidates,
    build_cache_preserving_event,
    build_safe_compaction_partition,
)
from app.agents.summarization.projection import (
    apply_summarization_event,
    effective_cutoff_to_state_cutoff,
)
from app.agents.summarization.responses import _summary_response_text
from app.agents.summarization.retry import (
    _forked_summary_messages,
    _overflow_retry_middle_messages,
    _summary_instruction,
)
from app.agents.summarization.state import (
    _MAX_SUMMARY_OVERFLOW_RETRIES,
    CACHE_PRESERVING_STRATEGY,
    CACHE_REPLACEMENT_STRATEGY,
    CachePreservingPartition,
    CachePreservingSummarizationState,
    CompactionReductionError,
    SummaryGenerationError,
    SummaryTextError,
    SummaryToolCallError,
)
from app.core.identifier import create_uuid_hex
from app.core.model_request_context import (
    SummaryCandidateScope,
    summary_candidate_scope,
)
from app.core.session_thread_context import require_current_session_thread_owner
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.mapping.itemized.canonical_langchain import (
    strip_media_from_summary_messages,
)
from app.services.orchestration.activity_runtime import (
    ActivityRuntime,
    current_activity_runtime,
)


class CachePreservingSummarizationMiddleware(_DeepAgentsSummarizationMiddleware):
    """优先压缩中段，以保持已经发送给上游的消息前缀不变。"""

    serialized_name: ClassVar[str] = "SummarizationMiddleware"
    state_schema = CachePreservingSummarizationState

    def __init__(
        self,
        *args: Any,
        compaction_preflight: CompactionPreflightPort,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # compaction preflight port 必填：Agent 层不得持有 SQLite connection，
        # durable 闭包验证统一经该只读端口进入唯一 Saver owner。
        self._compaction_preflight = compaction_preflight

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    def _get_history_path(self) -> str:
        """通过显式 SessionThread owner 隔离每个 thread 的压缩历史。"""
        thread_id = get_config().get("configurable", {}).get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("压缩历史归档缺少 LangGraph thread_id")
        owner = require_current_session_thread_owner(langgraph_thread_id=thread_id)
        return f"/session-artifacts/{owner.session_id}/context/{owner.thread_id}/history.md"

    @staticmethod
    def _apply_event_to_messages(
        messages: list[AnyMessage],
        event: object,
    ) -> list[AnyMessage]:
        return apply_summarization_event(messages, event)

    @staticmethod
    def _compute_state_cutoff(event: object, effective_cutoff: int) -> int:
        return effective_cutoff_to_state_cutoff(event, effective_cutoff)

    def _count_request_tokens(
        self, request: ModelRequest, messages: list[AnyMessage]
    ) -> int:
        counted = (
            [request.system_message, *messages] if request.system_message else messages
        )
        try:
            return self.token_counter(counted, tools=request.tools)
        except TypeError:
            return self.token_counter(counted)

    def _prepare_cache_compaction(
        self,
        request: ModelRequest,
    ) -> tuple[list[AnyMessage], CachePreservingPartition | None] | None:
        effective = self._get_effective_messages(request)
        total_tokens = self._count_request_tokens(request, effective)
        force_compaction = request.state.get("_force_cache_compaction") is True
        if not force_compaction and not self._should_summarize(effective, total_tokens):
            return None
        state_messages = list(request.messages)
        event = request.state.get("_summarization_event")
        summarize_end = self._determine_cutoff_index(effective)
        # 已保留的稳定前缀由旧 event 冻结；不得将它内部的 effective
        # index 当成一次新的 raw cutoff。replacement 的候选只映射一次。
        prefix_candidates = (
            [] if isinstance(event, Mapping) and event.get("strategy") == CACHE_PRESERVING_STRATEGY
            else _prefix_cutoff_candidates(effective, summarize_end)
        )
        state_prefix_candidates = {
            index: effective_cutoff_to_state_cutoff(event, index)
            for index in prefix_candidates
        }
        safe_prefix = self._preflight_safe_cutoffs(
            request,
            state_messages,
            list(state_prefix_candidates.values()),
        )
        partition = build_safe_compaction_partition(
            self,
            effective,
            event,
            prefix_cutoff_candidates=[
                index for index, state_index in state_prefix_candidates.items()
                if state_index in safe_prefix
            ],
        )
        if partition is None:
            return effective, None
        partition = self._preflight_partition_boundaries(
            request,
            state_messages,
            partition,
        )
        return effective, partition

    def _preflight_safe_cutoffs(
        self,
        request: ModelRequest,
        state_messages: list[AnyMessage],
        candidates: list[int],
    ) -> frozenset[int]:
        """把边界候选交给唯一 compaction preflight port 验证。"""
        if not candidates:
            return frozenset()
        runtime = request.runtime
        if runtime is None:
            raise RuntimeError("compaction preflight 缺少 runtime")
        thread_id = getattr(runtime.execution_info, "thread_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("compaction preflight 缺少 runtime thread_id")
        owner = require_current_session_thread_owner(
            langgraph_thread_id=thread_id
        )
        return self._compaction_preflight.safe_compaction_prefix_cutoffs(
            owner.session_id,
            owner.thread_id,
            checkpoint_ns=_runtime_checkpoint_ns(request),
            state_messages=state_messages,
            cutoff_indexes=candidates,
        )

    def _preflight_partition_boundaries(
        self,
        request: ModelRequest,
        state_messages: list[AnyMessage],
        partition: CachePreservingPartition,
    ) -> CachePreservingPartition:
        """在 summary/offload/checkpoint mutation 前验证分区全部边界候选。

        稳定前缀边界与 state cutoff 是强制边界：任一不在安全集合内即抛
        tool-protocol-boundary-conflict，保证零 summary、零 offload、零
        transition。overflow retry 的中段候选切点经同一 port 过滤。
        """
        state_index_by_identity = {
            id(message): index for index, message in enumerate(state_messages)
        }
        mandatory: list[int] = []
        event = request.state.get("_summarization_event")
        frozen_prefix = isinstance(event, Mapping) and event.get("strategy") == CACHE_PRESERVING_STRATEGY
        if partition.prefix_messages and not frozen_prefix:
            mandatory.append(effective_cutoff_to_state_cutoff(event, len(partition.prefix_messages)))
        mandatory.append(partition.state_cutoff)
        retry_candidates: dict[int, int] = {}
        middle = partition.messages_to_summarize
        for middle_index in range(2, len(middle)):
            state_index = state_index_by_identity.get(id(middle[middle_index]))
            if state_index is None:
                # 摘要消息等 request-only 对象不构成 durable 边界候选。
                continue
            retry_candidates[state_index] = middle_index
        safe = self._preflight_safe_cutoffs(
            request,
            state_messages,
            [*mandatory, *retry_candidates],
        )
        conflicting = [index for index in mandatory if index not in safe]
        if conflicting:
            raise ToolProtocolBoundaryConflict(
                "tool-protocol-boundary-conflict: compaction 边界拆散 "
                f"assistant tool-call group 与 terminal result: {conflicting}",
                safe_anchors=(),
            )
        return replace(
            partition,
            safe_retry_middle_boundaries=frozenset(
                middle_index
                for state_index, middle_index in retry_candidates.items()
                if state_index in safe
            ),
        )

    def _handle_unavailable_forced_compaction(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
        effective: list[AnyMessage],
    ) -> ExtendedModelResponse:
        response = handler(request.override(messages=effective))
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={"_force_cache_compaction": False}),
        )

    async def _ahandle_unavailable_forced_compaction(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        effective: list[AnyMessage],
    ) -> ExtendedModelResponse:
        response = await handler(request.override(messages=effective))
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={"_force_cache_compaction": False}),
        )

    @staticmethod
    def _summary_request(
        request: ModelRequest,
        messages: list[AnyMessage],
        *,
        remove_tools: bool = False,
        minimal_system: bool = False,
    ) -> ModelRequest:
        if remove_tools or minimal_system:
            return request.override(
                messages=messages,
                tools=[],
                tool_choice=None,
                system_message=None,
                response_format=None,
            )
        return request.override(messages=messages, response_format=None)

    @staticmethod
    def _build_summary_message(request: ModelRequest, summary: str) -> HumanMessage:
        info = request.runtime.execution_info if request.runtime is not None else None
        identity = {
            "checkpoint_id": getattr(info, "checkpoint_id", None),
            "task_id": getattr(info, "task_id", None),
            "node_attempt": getattr(info, "node_attempt", None),
        }
        if not all(isinstance(identity[key], str) and identity[key] for key in ("checkpoint_id", "task_id")):
            raise RuntimeError("compaction summary 缺少持久 checkpoint/task 身份")
        if type(identity["node_attempt"]) is not int or identity["node_attempt"] < 1:
            raise RuntimeError("compaction summary 缺少合法 node_attempt")
        thread_id = getattr(info, "thread_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("compaction summary 缺少 LangGraph thread_id")
        require_current_session_thread_owner(langgraph_thread_id=thread_id)
        return HumanMessage(
            id=f"summary-{sha256_jcs(identity)}",
            content=(
                "You are in the middle of a conversation that has been summarized.\n\n"
                "Use read_compaction_history(line_offset=1, limit=200) to retrieve the "
                "original archived messages for this conversation thread. Continue with "
                "the next line_offset when more lines are needed.\n\n"
                f"A condensed summary follows:\n\n<summary>\n{summary}\n</summary>"
            ),
            additional_kwargs={"lc_source": "summarization"},
        )

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: CachePreservingSummarizationState, runtime: object) -> dict[str, object] | None:
        if state.get("_compaction_continuation") is True:
            return {"_compaction_continuation": False, "jump_to": "model"}
        return None

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: CachePreservingSummarizationState, runtime: object) -> dict[str, object] | None:
        if state.get("_compaction_continuation") is True:
            return {"_compaction_continuation": False, "jump_to": "model"}
        return None

    @staticmethod
    def _summary_retry_candidates(
        partition: CachePreservingPartition,
    ) -> list[tuple[list[AnyMessage], bool]]:
        middle_retries = _overflow_retry_middle_messages(
            partition.messages_to_summarize,
            partition.safe_retry_middle_boundaries,
        )
        retries = [
            (_forked_summary_messages(partition, middle), False)
            for middle in middle_retries[: _MAX_SUMMARY_OVERFLOW_RETRIES - 1]
        ]
        stripped_middle, _ = strip_media_from_summary_messages(
            partition.messages_to_summarize
        )
        if middle_retries:
            stripped_middle = middle_retries[-1]
        retries.append(
            (
                [
                    *stripped_middle,
                    _summary_instruction(len(stripped_middle)),
                ],
                True,
            )
        )
        return retries

    def _invoke_summary_candidate(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
        messages: list[AnyMessage],
        *,
        minimal_system: bool,
        candidate_ordinal: int,
        retry_source_message_ids: tuple[str, ...],
    ) -> str:
        summary_request = self._summary_request(
            request,
            messages,
            remove_tools=minimal_system,
            minimal_system=minimal_system,
        )
        with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "initial", not minimal_system, minimal_system, retry_source_message_ids)):
            response = handler(summary_request)
        try:
            return _summary_response_text(response)
        except SummaryToolCallError:
            with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "tool_free", False, minimal_system, retry_source_message_ids)):
                response = handler(
                    self._summary_request(
                        request,
                        messages,
                        remove_tools=True,
                        minimal_system=minimal_system,
                    )
                )
            return _summary_response_text(response)
        except SummaryTextError:
            # reasoning 模型可能在带完整工具和系统提示的摘要请求中只返回思考块。
            # 摘要正文不能暴露思考内容，改用无工具的最小请求重新生成可见摘要。
            with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "visible_text", False, True, retry_source_message_ids)):
                response = handler(
                    self._summary_request(
                        request,
                        messages,
                        remove_tools=True,
                        minimal_system=True,
                    )
                )
            return _summary_response_text(response)

    async def _ainvoke_summary_candidate(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        messages: list[AnyMessage],
        *,
        minimal_system: bool,
        candidate_ordinal: int,
        retry_source_message_ids: tuple[str, ...],
    ) -> str:
        summary_request = self._summary_request(
            request,
            messages,
            remove_tools=minimal_system,
            minimal_system=minimal_system,
        )
        with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "initial", not minimal_system, minimal_system, retry_source_message_ids)):
            response = await handler(summary_request)
        try:
            return _summary_response_text(response)
        except SummaryToolCallError:
            with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "tool_free", False, minimal_system, retry_source_message_ids)):
                response = await handler(
                    self._summary_request(
                        request,
                        messages,
                        remove_tools=True,
                        minimal_system=minimal_system,
                    )
                )
            return _summary_response_text(response)
        except SummaryTextError:
            # reasoning 模型可能在带完整工具和系统提示的摘要请求中只返回思考块。
            # 摘要正文不能暴露思考内容，改用无工具的最小请求重新生成可见摘要。
            with summary_candidate_scope(SummaryCandidateScope(candidate_ordinal, "visible_text", False, True, retry_source_message_ids)):
                response = await handler(
                    self._summary_request(
                        request,
                        messages,
                        remove_tools=True,
                        minimal_system=True,
                    )
                )
            return _summary_response_text(response)

    def _create_cache_preserving_summary(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
        partition: CachePreservingPartition,
    ) -> str:
        candidates = [
            (_forked_summary_messages(partition), False),
            *self._summary_retry_candidates(partition),
        ]
        last_error: ContextOverflowError | None = None
        for candidate_ordinal, (messages, minimal_system) in enumerate(candidates):
            try:
                return self._invoke_summary_candidate(
                    request,
                    handler,
                    messages,
                    minimal_system=minimal_system,
                    candidate_ordinal=candidate_ordinal,
                    retry_source_message_ids=tuple(message.id for message in partition.messages_to_summarize if message.id),
                )
            except ContextOverflowError as error:
                last_error = error
        if last_error is None:
            raise RuntimeError("缓存优先压缩没有生成任何摘要候选请求")
        raise last_error

    async def _acreate_cache_preserving_summary(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        partition: CachePreservingPartition,
    ) -> str:
        candidates = [
            (_forked_summary_messages(partition), False),
            *self._summary_retry_candidates(partition),
        ]
        last_error: ContextOverflowError | None = None
        for candidate_ordinal, (messages, minimal_system) in enumerate(candidates):
            try:
                return await self._ainvoke_summary_candidate(
                    request,
                    handler,
                    messages,
                    minimal_system=minimal_system,
                    candidate_ordinal=candidate_ordinal,
                    retry_source_message_ids=tuple(message.id for message in partition.messages_to_summarize if message.id),
                )
            except ContextOverflowError as error:
                last_error = error
        if last_error is None:
            raise RuntimeError("缓存优先压缩没有生成任何摘要候选请求")
        raise last_error

    def _ensure_compaction_reduces_tokens(
        self,
        request: ModelRequest,
        before: list[AnyMessage],
        after: list[AnyMessage],
    ) -> None:
        before_tokens = self._count_request_tokens(request, before)
        after_tokens = self._count_request_tokens(request, after)
        if after_tokens >= before_tokens:
            raise CompactionReductionError(
                "缓存优先压缩没有缩短模型上下文: "
                f"before_tokens={before_tokens}, after_tokens={after_tokens}"
            )

    @staticmethod
    def _compaction_activity_id(activity_runtime: ActivityRuntime) -> str:
        return (
            f"{activity_runtime.writer.turn_stream_id}:context-compaction:"
            f"{create_uuid_hex()}"
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        prepared = self._prepare_cache_compaction(request)
        if prepared is None:
            return handler(
                request.override(messages=self._get_effective_messages(request))
            )
        before, partition = prepared
        if partition is None:
            if request.state.get("_force_cache_compaction") is True:
                return self._handle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise RuntimeError("上下文需要压缩，但找不到不破坏消息边界的安全分区")
        backend = self._get_backend(request.state, request.runtime)
        file_path = self._offload_to_backend(backend, partition.messages_to_summarize)
        if file_path is None:
            raise RuntimeError("缓存优先压缩无法保存被摘要的历史消息")
        try:
            summary = self._create_cache_preserving_summary(
                request,
                handler,
                partition,
            )
        except (ContextOverflowError, SummaryToolCallError, SummaryTextError, SummaryGenerationError):
            if request.state.get("_force_cache_compaction") is True:
                return self._handle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        summary_message = self._build_summary_message(request, summary)
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            strategy=(
                CACHE_PRESERVING_STRATEGY
                if partition.prefix_messages
                else CACHE_REPLACEMENT_STRATEGY
            ),
        )
        modified = [
            *partition.prefix_messages,
            summary_message,
            *partition.preserved_messages,
        ]
        try:
            self._ensure_compaction_reduces_tokens(request, before, modified)
        except CompactionReductionError:
            if request.state.get("_force_cache_compaction") is True:
                return self._handle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        return ExtendedModelResponse(
            model_response=ModelResponse(result=[]),
            command=Command(
                update={
                    "_summarization_event": event,
                    "_force_cache_compaction": False,
                    "_compaction_continuation": True,
                }
            ),
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        prepared = self._prepare_cache_compaction(request)
        if prepared is None:
            return await handler(
                request.override(messages=self._get_effective_messages(request))
            )
        activity_runtime = current_activity_runtime()
        if activity_runtime is None:
            return await CachePreservingSummarizationMiddleware._awrap_model_call_impl(
                self,
                request,
                handler,
            )
        activity_id = CachePreservingSummarizationMiddleware._compaction_activity_id(
            activity_runtime
        )
        await activity_runtime.started(
            activity_id=activity_id,
            kind="context.compaction",
            summary="正在压缩会话上下文",
            cancellable=True,
            resumable=False,
            side_effect_policy="none",
            detail={
                "phase": "preparing",
                "summarized_message_count": len(prepared[1].messages_to_summarize)
                if prepared[1] is not None
                else 0,
            },
        )
        try:
            result = await CachePreservingSummarizationMiddleware._awrap_model_call_impl(
                self,
                request,
                handler,
            )
        except Exception as error:
            await activity_runtime.failed(
                activity_id=activity_id,
                kind="context.compaction",
                outcome="outcome_unknown",
                summary=str(error),
                detail={"phase": "failed"},
            )
            raise
        await activity_runtime.updated(
            activity_id=activity_id,
            kind="context.compaction",
            status="stopping",
            detail={"phase": "completed"},
        )
        await activity_runtime.completed(
            activity_id=activity_id,
            kind="context.compaction",
            summary="会话上下文压缩完成",
        )
        return result

    async def _awrap_model_call_impl(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        prepared = self._prepare_cache_compaction(request)
        if prepared is None:
            return await handler(
                request.override(messages=self._get_effective_messages(request))
            )
        before, partition = prepared
        if partition is None:
            if request.state.get("_force_cache_compaction") is True:
                return await self._ahandle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise RuntimeError("上下文需要压缩，但找不到不破坏消息边界的安全分区")
        backend = self._get_backend(request.state, request.runtime)
        file_path = await self._aoffload_to_backend(
            backend,
            partition.messages_to_summarize,
        )
        if file_path is None:
            raise RuntimeError("缓存优先压缩无法保存被摘要的历史消息")
        try:
            summary = await self._acreate_cache_preserving_summary(
                request,
                handler,
                partition,
            )
        except (ContextOverflowError, SummaryToolCallError, SummaryTextError, SummaryGenerationError):
            if request.state.get("_force_cache_compaction") is True:
                return await self._ahandle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        summary_message = self._build_summary_message(request, summary)
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            strategy=(
                CACHE_PRESERVING_STRATEGY
                if partition.prefix_messages
                else CACHE_REPLACEMENT_STRATEGY
            ),
        )
        modified = [
            *partition.prefix_messages,
            summary_message,
            *partition.preserved_messages,
        ]
        try:
            self._ensure_compaction_reduces_tokens(request, before, modified)
        except CompactionReductionError:
            if request.state.get("_force_cache_compaction") is True:
                return await self._ahandle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        return ExtendedModelResponse(
            model_response=ModelResponse(result=[]),
            command=Command(
                update={
                    "_summarization_event": event,
                    "_force_cache_compaction": False,
                    "_compaction_continuation": True,
                }
            ),
        )
