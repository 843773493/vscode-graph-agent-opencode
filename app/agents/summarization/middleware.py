"""缓存优先压缩中间件：同步与异步两条 wrap 链路。

保留已发送前缀、摘要中段、保留近期尾部；同步 wrap_model_call 与异步
awrap_model_call/_awrap_model_call_impl 是对等实现（语义差异只登记不合并）。"""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
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
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import (
    AnyMessage,
    SystemMessage,
)
from langgraph.types import Command

from app.agents.itemized_context_middleware import (
    _runtime_checkpoint_ns,
    _runtime_session_id,
)
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
    strip_media_from_summary_messages,
)
from app.agents.summarization.state import (
    _MAX_SUMMARY_OVERFLOW_RETRIES,
    CACHE_PRESERVING_STRATEGY,
    CACHE_REPLACEMENT_STRATEGY,
    CachePreservingPartition,
    CachePreservingSummarizationState,
    SummaryToolCallError,
)
from app.agents.workspace_tool_paths import backend_virtual_to_workspace_relative
from app.core.identifier import create_uuid_hex
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
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
        """将压缩历史归档到当前会话目录，而不是工作区共享目录。"""
        thread_id = self._get_thread_id()
        return f"/session-artifacts/{thread_id}/context/history.md"

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
        summarize_end = self._determine_cutoff_index(effective)
        prefix_candidates = _prefix_cutoff_candidates(effective, summarize_end)
        safe_prefix = self._preflight_safe_cutoffs(
            request,
            state_messages,
            prefix_candidates,
        )
        partition = build_safe_compaction_partition(
            self,
            effective,
            request.state.get("_summarization_event"),
            prefix_cutoff_candidates=[
                index for index in prefix_candidates if index in safe_prefix
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
        return self._compaction_preflight.safe_compaction_prefix_cutoffs(
            _runtime_session_id(request),
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
        if partition.prefix_messages:
            mandatory.append(len(partition.prefix_messages))
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
                system_message=(
                    SystemMessage(content="Summarize the supplied conversation.")
                    if minimal_system
                    else request.system_message
                ),
            )
        return request.override(messages=messages)

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
    ) -> str:
        summary_request = self._summary_request(
            request,
            messages,
            remove_tools=minimal_system,
            minimal_system=minimal_system,
        )
        response = handler(summary_request)
        try:
            return _summary_response_text(response)
        except SummaryToolCallError:
            response = handler(
                self._summary_request(
                    request,
                    messages,
                    remove_tools=True,
                    minimal_system=minimal_system,
                )
            )
            return _summary_response_text(response)
        except ValueError:
            # reasoning 模型可能在带完整工具和系统提示的摘要请求中只返回思考块。
            # 摘要正文不能暴露思考内容，改用无工具的最小请求重新生成可见摘要。
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
    ) -> str:
        summary_request = self._summary_request(
            request,
            messages,
            remove_tools=minimal_system,
            minimal_system=minimal_system,
        )
        response = await handler(summary_request)
        try:
            return _summary_response_text(response)
        except SummaryToolCallError:
            response = await handler(
                self._summary_request(
                    request,
                    messages,
                    remove_tools=True,
                    minimal_system=minimal_system,
                )
            )
            return _summary_response_text(response)
        except ValueError:
            # reasoning 模型可能在带完整工具和系统提示的摘要请求中只返回思考块。
            # 摘要正文不能暴露思考内容，改用无工具的最小请求重新生成可见摘要。
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
        for messages, minimal_system in candidates:
            try:
                return self._invoke_summary_candidate(
                    request,
                    handler,
                    messages,
                    minimal_system=minimal_system,
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
        for messages, minimal_system in candidates:
            try:
                return await self._ainvoke_summary_candidate(
                    request,
                    handler,
                    messages,
                    minimal_system=minimal_system,
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
            raise RuntimeError(
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
        except (ContextOverflowError, SummaryToolCallError, ValueError, RuntimeError):
            if request.state.get("_force_cache_compaction") is True:
                return self._handle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        model_file_path = backend_virtual_to_workspace_relative(file_path)
        summary_message = self._build_new_messages_with_path(summary, model_file_path)[
            0
        ]
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            file_path=model_file_path,
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
        except RuntimeError:
            if request.state.get("_force_cache_compaction") is True:
                return self._handle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        response = handler(request.override(messages=modified))
        return ExtendedModelResponse(
            model_response=response,
            command=Command(
                update={
                    "_summarization_event": event,
                    "_force_cache_compaction": False,
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
        except (ContextOverflowError, SummaryToolCallError, ValueError, RuntimeError):
            if request.state.get("_force_cache_compaction") is True:
                return await self._ahandle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        model_file_path = backend_virtual_to_workspace_relative(file_path)
        summary_message = self._build_new_messages_with_path(summary, model_file_path)[
            0
        ]
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            file_path=model_file_path,
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
        except RuntimeError:
            if request.state.get("_force_cache_compaction") is True:
                return await self._ahandle_unavailable_forced_compaction(
                    request,
                    handler,
                    before,
                )
            raise
        response = await handler(request.override(messages=modified))
        return ExtendedModelResponse(
            model_response=response,
            command=Command(
                update={
                    "_summarization_event": event,
                    "_force_cache_compaction": False,
                }
            ),
        )
