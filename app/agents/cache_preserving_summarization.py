from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, NotRequired, cast

from deepagents.backends.protocol import BACKEND_TYPES
# TODO: DeepAgents 暴露公共的压缩扩展基类后，改用公共 API，避免依赖私有实现类。
from deepagents.middleware.summarization import (
    CompactConversationSchema,
    SummarizationState,
    SummarizationToolMiddleware,
    _DeepAgentsSummarizationMiddleware,
    compute_summarization_defaults,
)
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, PrivateStateAttr
from langchain.chat_models import BaseChatModel as RuntimeBaseChatModel
from langchain.tools import ToolRuntime
from langchain_core.exceptions import ContextOverflowError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.types import Command


CACHE_PRESERVING_STRATEGY = "cache_preserving"
CACHE_REPLACEMENT_STRATEGY = "cache_replacement"
_MIN_CACHE_PREFIX_MESSAGES = 2
_PREFERRED_CACHE_PREFIX_MESSAGES = 4
_MAX_CACHE_PREFIX_MESSAGES = 8
_MAX_SUMMARY_OVERFLOW_RETRIES = 3
_TOOL_PAYLOAD_RETRY_MAX_CHARS = 4096
_TOOL_PAYLOAD_RETRY_TOTAL_CHARS = 8192
_MEDIA_BLOCK_MARKERS = {
    "document": "[document]",
    "file": "[document]",
    "image": "[image]",
    "image_url": "[image]",
    "input_file": "[document]",
    "input_image": "[image]",
}


class SummaryToolCallError(RuntimeError):
    """摘要模型违反纯文本约束并尝试调用工具。"""


class CachePreservingSummarizationState(SummarizationState):
    """缓存压缩额外维护一次性强制压缩标记。"""

    _force_cache_compaction: Annotated[NotRequired[bool], PrivateStateAttr]


@dataclass(slots=True)
class CachePreservingPartition:
    prefix_messages: list[AnyMessage]
    messages_to_summarize: list[AnyMessage]
    preserved_messages: list[AnyMessage]
    state_cutoff: int

    @property
    def effective_messages(self) -> list[AnyMessage]:
        return [*self.prefix_messages, *self.preserved_messages]


def _event_int(event: Mapping[str, object], key: str) -> int:
    value = event.get(key)
    if not isinstance(value, int) or value < 0:
        raise TypeError(f"_summarization_event.{key} 必须是非负整数，实际值: {value!r}")
    return value


def _event_prefix_messages(event: Mapping[str, object]) -> list[AnyMessage]:
    value = event.get("cache_prefix_messages")
    if not isinstance(value, list):
        raise TypeError("cache_preserving 压缩事件缺少 cache_prefix_messages 列表")
    for index, message in enumerate(value):
        if not isinstance(message, BaseMessage):
            raise TypeError(
                "cache_prefix_messages 中出现不支持的消息类型: "
                f"index={index}, type={type(message).__name__}"
            )
    return list(value)


def apply_summarization_event(
    messages: list[AnyMessage],
    event: object,
) -> list[AnyMessage]:
    """把压缩事件投影为模型实际可见的消息。"""
    if event is None:
        return list(messages)
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    cutoff = _event_int(event, "cutoff_index")
    if cutoff > len(messages):
        raise ValueError(
            "_summarization_event.cutoff_index 超过消息数量: "
            f"cutoff={cutoff}, messages={len(messages)}"
        )
    summary_message = event.get("summary_message")
    if not isinstance(summary_message, BaseMessage):
        raise TypeError("_summarization_event.summary_message 必须是消息对象")

    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix = _event_prefix_messages(event)
        return [*prefix, summary_message, *messages[cutoff:]]
    return [summary_message, *messages[cutoff:]]


def effective_cutoff_to_state_cutoff(
    event: object,
    effective_cutoff: int,
) -> int:
    """将模型投影中的边界转换成原始 checkpoint 消息边界。"""
    if event is None:
        return effective_cutoff
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    previous_cutoff = _event_int(event, "cutoff_index")
    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix_count = len(_event_prefix_messages(event))
        dynamic_start = prefix_count + 1
        if effective_cutoff < dynamic_start:
            raise ValueError(
                "新的压缩边界位于缓存稳定前缀内部，无法保持缓存: "
                f"effective_cutoff={effective_cutoff}, prefix_count={prefix_count}"
            )
        return previous_cutoff + effective_cutoff - dynamic_start

    if effective_cutoff < 1:
        raise ValueError("已有摘要后的压缩边界必须至少越过摘要消息")
    return previous_cutoff + effective_cutoff - 1


def replacement_effective_cutoff_to_state_cutoff(
    event: object,
    effective_cutoff: int,
) -> int:
    """允许替换稳定前缀时，将投影边界映射回原始 checkpoint。"""
    if event is None:
        return effective_cutoff
    if not isinstance(event, Mapping):
        raise TypeError("_summarization_event 必须是 mapping")

    previous_cutoff = _event_int(event, "cutoff_index")
    if event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        prefix_count = len(_event_prefix_messages(event))
        if effective_cutoff <= prefix_count:
            raise ValueError(
                "替换压缩必须同时吞并旧稳定前缀后的摘要消息: "
                f"effective_cutoff={effective_cutoff}, prefix_count={prefix_count}"
            )
        return previous_cutoff + effective_cutoff - prefix_count - 1
    if effective_cutoff < 1:
        raise ValueError("已有摘要后的替换边界必须至少越过摘要消息")
    return previous_cutoff + effective_cutoff - 1


def _initial_prefix_cutoff(
    summarization: _DeepAgentsSummarizationMiddleware,
    messages: list[AnyMessage],
    summarize_end: int,
) -> int:
    if summarize_end <= _MIN_CACHE_PREFIX_MESSAGES:
        return 0
    preferred_minimum = (
        _PREFERRED_CACHE_PREFIX_MESSAGES
        if summarize_end > _PREFERRED_CACHE_PREFIX_MESSAGES
        else _MIN_CACHE_PREFIX_MESSAGES
    )
    target = min(
        _MAX_CACHE_PREFIX_MESSAGES,
        max(preferred_minimum, summarize_end // 4),
        summarize_end - 1,
    )
    # 缓存前缀应停在一轮对话结束处；下一条 HumanMessage 是最稳定的轮次边界。
    for index in range(target, 0, -1):
        if isinstance(messages[index], HumanMessage):
            return index
    for index in range(target, 0, -1):
        if _is_safe_api_round_boundary(messages, index):
            return index
    return 0


def _is_safe_api_round_boundary(
    messages: list[AnyMessage],
    index: int,
) -> bool:
    if index <= 0 or index >= len(messages):
        return False
    if isinstance(messages[index], ToolMessage):
        return False
    return not (
        isinstance(messages[index - 1], AIMessage)
        and messages[index - 1].tool_calls
    )


def build_cache_preserving_partition(
    summarization: _DeepAgentsSummarizationMiddleware,
    effective_messages: list[AnyMessage],
    event: object,
    summarize_end: int,
) -> CachePreservingPartition | None:
    """保留已经发送过的前缀，只摘要中段并继续保留近期尾部。"""
    if (
        isinstance(event, Mapping)
        and event.get("strategy") == CACHE_PRESERVING_STRATEGY
    ):
        prefix_messages = _event_prefix_messages(event)
        middle_start = len(prefix_messages)
        # effective_messages 中紧随稳定前缀的是上一次摘要，也要滚入新摘要。
    else:
        middle_start = _initial_prefix_cutoff(
            summarization,
            effective_messages,
            summarize_end,
        )
        if middle_start == 0:
            return None
        prefix_messages = list(effective_messages[:middle_start])

    if summarize_end <= middle_start:
        return None
    messages_to_summarize = list(effective_messages[middle_start:summarize_end])
    if not messages_to_summarize:
        return None
    state_cutoff = effective_cutoff_to_state_cutoff(event, summarize_end)
    return CachePreservingPartition(
        prefix_messages=prefix_messages,
        messages_to_summarize=messages_to_summarize,
        preserved_messages=list(effective_messages[summarize_end:]),
        state_cutoff=state_cutoff,
    )


def build_safe_compaction_partition(
    summarization: _DeepAgentsSummarizationMiddleware,
    effective_messages: list[AnyMessage],
    event: object,
) -> CachePreservingPartition | None:
    """统一计算自动、HTTP 与工具入口使用的安全压缩分区。"""
    summarize_end = summarization._determine_cutoff_index(effective_messages)
    replacement_required = summarize_end <= 0
    if replacement_required:
        if len(effective_messages) <= 1:
            return None
        summarize_end = summarization._find_safe_cutoff_point(
            effective_messages,
            len(effective_messages) - 1,
        )
        if summarize_end <= 0:
            return None

    partition = None
    if not replacement_required:
        partition = build_cache_preserving_partition(
            summarization,
            effective_messages,
            event,
            summarize_end,
        )
    if partition is not None:
        return partition

    # 没有可保留的完整轮次边界时，最终允许替换旧前缀，避免超限会话永久卡死。
    if isinstance(event, Mapping) and event.get("strategy") == CACHE_PRESERVING_STRATEGY:
        summarize_end = max(
            summarize_end,
            len(_event_prefix_messages(event)) + 1,
        )
    return CachePreservingPartition(
        prefix_messages=[],
        messages_to_summarize=list(effective_messages[:summarize_end]),
        preserved_messages=list(effective_messages[summarize_end:]),
        state_cutoff=replacement_effective_cutoff_to_state_cutoff(
            event,
            summarize_end,
        ),
    )


def build_cache_preserving_event(
    partition: CachePreservingPartition,
    *,
    summary_message: AnyMessage,
    file_path: str,
    strategy: str = CACHE_PRESERVING_STRATEGY,
) -> dict[str, object]:
    return {
        "strategy": strategy,
        "cutoff_index": partition.state_cutoff,
        "cache_prefix_messages": partition.prefix_messages,
        "summary_message": summary_message,
        "file_path": file_path,
    }


def _summary_instruction(message_count: int) -> HumanMessage:
    return HumanMessage(
        content=(
            "CRITICAL: Return plain text only. Do not call tools; tool calls are "
            "rejected and make this compaction fail.\n\n"
            "Create a concise but complete summary of only the "
            f"{message_count} messages immediately before this instruction. "
            "Preserve user requests, completed work, files and code involved, "
            "decisions, identifiers, errors and fixes, constraints, unresolved "
            "work, and the precise next step. Do not summarize the earlier stable "
            "prefix. The summary must be substantially shorter than the messages it "
            "replaces: use compact factual bullets, omit conversational prose and "
            "repeated content, and never reproduce large source blocks. Return only "
            "the summary."
        ),
        additional_kwargs={"lc_source": "summarization_request"},
    )


def _forked_summary_messages(
    partition: CachePreservingPartition,
    messages_to_summarize: list[AnyMessage] | None = None,
) -> list[AnyMessage]:
    middle = (
        partition.messages_to_summarize
        if messages_to_summarize is None
        else messages_to_summarize
    )
    return [
        *partition.prefix_messages,
        *middle,
        _summary_instruction(len(middle)),
    ]


def _strip_media_content(content: object) -> tuple[object, bool]:
    if not isinstance(content, list):
        return content, False

    stripped: list[object] = []
    changed = False
    for item in content:
        if not isinstance(item, dict):
            stripped.append(item)
            continue
        block_type = item.get("type")
        if isinstance(block_type, str) and block_type in _MEDIA_BLOCK_MARKERS:
            stripped.append(
                {"type": "text", "text": _MEDIA_BLOCK_MARKERS[block_type]}
            )
            changed = True
            continue
        if block_type == "tool_result" and "content" in item:
            nested, nested_changed = _strip_media_content(item["content"])
            if nested_changed:
                stripped.append({**item, "content": nested})
                changed = True
                continue
        stripped.append(item)
    return stripped, changed


def strip_media_from_summary_messages(
    messages: list[AnyMessage],
) -> tuple[list[AnyMessage], bool]:
    """仅为溢出重试剥离媒体，不修改 checkpoint 中的原消息。"""
    stripped_messages: list[AnyMessage] = []
    changed = False
    for message in messages:
        stripped_content, message_changed = _strip_media_content(message.content)
        if not message_changed:
            stripped_messages.append(message)
            continue
        stripped_messages.append(
            message.model_copy(
                update={
                    "content": cast(
                        "str | list[str | dict[str, Any]]",
                        stripped_content,
                    )
                }
            )
        )
        changed = True
    return stripped_messages, changed


def _content_character_count(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_content_character_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_content_character_count(item) for item in value)
    return len(str(value))


def compact_large_tool_payloads_for_summary(
    messages: list[AnyMessage],
) -> tuple[list[AnyMessage], bool]:
    """仅压缩 overflow 重试副本中的大型工具载荷，并保留调用配对。"""
    payloads: list[tuple[str, int, int, int]] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call_index, tool_call in enumerate(message.tool_calls):
                payloads.append(
                    (
                        "args",
                        message_index,
                        call_index,
                        _content_character_count(tool_call.get("args", {})),
                    )
                )
        elif isinstance(message, ToolMessage):
            payloads.append(
                (
                    "result",
                    message_index,
                    -1,
                    _content_character_count(message.content),
                )
            )

    marked = {
        (kind, message_index, call_index)
        for kind, message_index, call_index, size in payloads
        if size > _TOOL_PAYLOAD_RETRY_MAX_CHARS
    }
    remaining_size = sum(
        size
        for kind, message_index, call_index, size in payloads
        if (kind, message_index, call_index) not in marked
    )
    for kind, message_index, call_index, size in payloads:
        key = (kind, message_index, call_index)
        if remaining_size <= _TOOL_PAYLOAD_RETRY_TOTAL_CHARS:
            break
        if key in marked:
            continue
        marked.add(key)
        remaining_size -= size

    if not marked:
        return list(messages), False

    compacted: list[AnyMessage] = []
    for message_index, message in enumerate(messages):
        if (
            isinstance(message, ToolMessage)
            and ("result", message_index, -1) in marked
        ):
            compacted.append(
                message.model_copy(
                    update={
                        "content": "[large tool result omitted for compaction retry]"
                    }
                )
            )
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_calls = []
            message_changed = False
            for call_index, tool_call in enumerate(message.tool_calls):
                if ("args", message_index, call_index) not in marked:
                    tool_calls.append(tool_call)
                    continue
                tool_calls.append(
                    {
                        **tool_call,
                        "args": {"_omitted": "large tool arguments"},
                    }
                )
                message_changed = True
            if message_changed:
                compacted.append(message.model_copy(update={"tool_calls": tool_calls}))
                continue
        compacted.append(message)
    return compacted, True


def _overflow_retry_middle_messages(
    messages: list[AnyMessage],
) -> list[list[AnyMessage]]:
    stripped, media_changed = strip_media_from_summary_messages(messages)
    retries: list[list[AnyMessage]] = [stripped] if media_changed else []
    compacted, tool_payload_changed = compact_large_tool_payloads_for_summary(stripped)
    if tool_payload_changed:
        retries.append(compacted)
    else:
        compacted = stripped
    boundaries = [
        index
        for index in range(2, len(compacted))
        if _is_safe_api_round_boundary(compacted, index)
    ]
    selected: set[int] = set()
    for attempt in range(1, _MAX_SUMMARY_OVERFLOW_RETRIES + 1):
        target = (len(compacted) * attempt) // (_MAX_SUMMARY_OVERFLOW_RETRIES + 1)
        boundary = next((index for index in boundaries if index >= target), None)
        if boundary is None or boundary in selected:
            continue
        selected.add(boundary)
        suffix = compacted[boundary:]
        marker = (
            []
            if suffix and isinstance(suffix[0], HumanMessage)
            else [
                HumanMessage(
                    content="[earlier conversation truncated for compaction retry]",
                    additional_kwargs={"lc_source": "summarization_retry"},
                )
            ]
        )
        retries.append(
            [
                *marker,
                *suffix,
            ]
        )
    return retries


def validate_summary_text(summary: str) -> str:
    """拒绝第三方摘要器用普通字符串伪装的失败结果。"""
    normalized = summary.strip()
    if not normalized:
        raise ValueError("压缩摘要模型返回了空文本")
    # TODO: DeepAgents 改为抛出摘要异常后，删除对其错误字符串的显式识别。
    if normalized.startswith("Error generating summary:"):
        raise RuntimeError(f"压缩摘要生成失败: {normalized}")
    if normalized == "Previous conversation was too long to summarize.":
        raise RuntimeError("压缩摘要输入在预处理后为空，无法生成有效摘要")
    return normalized


def _summary_response_text(response: ModelResponse | ExtendedModelResponse) -> str:
    model_response = (
        response.model_response
        if isinstance(response, ExtendedModelResponse)
        else response
    )
    if not isinstance(model_response, ModelResponse) or not model_response.result:
        raise TypeError("压缩摘要 handler 必须返回包含消息的 ModelResponse")
    message = model_response.result[-1]
    if not isinstance(message, AIMessage):
        raise TypeError(
            f"压缩摘要响应必须是 AIMessage，实际类型: {type(message).__name__}"
        )
    if message.tool_calls or message.invalid_tool_calls:
        names = [call.get("name", "<unknown>") for call in message.tool_calls]
        raise SummaryToolCallError(
            f"压缩摘要模型不得调用工具，实际请求: {names or ['<invalid>']}"
        )
    return validate_summary_text(message.text)


class CachePreservingSummarizationMiddleware(_DeepAgentsSummarizationMiddleware):
    """优先压缩中段，以保持已经发送给上游的消息前缀不变。"""

    serialized_name: ClassVar[str] = "SummarizationMiddleware"
    state_schema = CachePreservingSummarizationState

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"

    def _get_history_path(self) -> str:
        """将压缩历史归档到当前会话目录，而不是工作区共享目录。"""
        thread_id = self._get_thread_id()
        return f"/.boxteam/sessions/{thread_id}/context/history.md"

    @staticmethod
    def _apply_event_to_messages(
        messages: list[AnyMessage],
        event: object,
    ) -> list[AnyMessage]:
        return apply_summarization_event(messages, event)

    @staticmethod
    def _compute_state_cutoff(event: object, effective_cutoff: int) -> int:
        return effective_cutoff_to_state_cutoff(event, effective_cutoff)

    def _count_request_tokens(self, request: ModelRequest, messages: list[AnyMessage]) -> int:
        counted = [request.system_message, *messages] if request.system_message else messages
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
        partition = build_safe_compaction_partition(
            self,
            effective,
            request.state.get("_summarization_event"),
        )
        return effective, partition

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
            partition.messages_to_summarize
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
        summary = self._create_cache_preserving_summary(request, handler, partition)
        summary_message = self._build_new_messages_with_path(summary, file_path)[0]
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            file_path=file_path,
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
        self._ensure_compaction_reduces_tokens(request, before, modified)
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
        summary = await self._acreate_cache_preserving_summary(
            request,
            handler,
            partition,
        )
        summary_message = self._build_new_messages_with_path(summary, file_path)[0]
        event = build_cache_preserving_event(
            partition,
            summary_message=summary_message,
            file_path=file_path,
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
        self._ensure_compaction_reduces_tokens(request, before, modified)
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


class CachePreservingSummarizationToolMiddleware(SummarizationToolMiddleware):
    """让 compact_conversation 工具使用与自动压缩相同的缓存优先策略。"""

    def _partition_for_tool(
        self,
        runtime: ToolRuntime,
    ) -> tuple[CachePreservingPartition, object, list[AnyMessage]] | None:
        summarization = self._summarization
        messages = runtime.state.get("messages", [])
        event = runtime.state.get("_summarization_event")
        effective = summarization._apply_event_to_messages(messages, event)
        if not self._is_eligible_for_compaction(effective):
            return None
        partition = build_safe_compaction_partition(
            summarization,
            effective,
            event,
        )
        if partition is None:
            return None
        return partition, event, effective

    def _run_compact(self, runtime: ToolRuntime) -> Command:
        prepared = self._partition_for_tool(runtime)
        if prepared is None:
            return self._schedule_result(runtime, summarized_count=0)
        partition, _, _ = prepared
        return self._schedule_result(
            runtime,
            summarized_count=len(partition.messages_to_summarize),
        )

    async def _arun_compact(self, runtime: ToolRuntime) -> Command:
        prepared = self._partition_for_tool(runtime)
        if prepared is None:
            return self._schedule_result(runtime, summarized_count=0)
        partition, _, _ = prepared
        return self._schedule_result(
            runtime,
            summarized_count=len(partition.messages_to_summarize),
        )

    @staticmethod
    def _schedule_result(
        runtime: ToolRuntime,
        *,
        summarized_count: int,
    ) -> Command:
        if summarized_count > 0:
            content = (
                "Conversation compaction scheduled. The next model call will create "
                f"a summary of {summarized_count} messages, preserving the prompt "
                "cache when a safe stable prefix exists."
            )
        else:
            content = "Conversation does not contain enough history to compact."
        return Command(
            update={
                "_force_cache_compaction": summarized_count > 0,
                "messages": [
                    ToolMessage(
                        content=content,
                        tool_call_id=runtime.tool_call_id or "",
                    )
                ],
            }
        )


def create_cache_preserving_summarization_middleware(
    model: BaseChatModel,
    backend: BACKEND_TYPES,
) -> CachePreservingSummarizationMiddleware:
    if not isinstance(model, RuntimeBaseChatModel):
        raise TypeError("缓存优先压缩需要 BaseChatModel 实例")
    defaults = compute_summarization_defaults(model)
    return CachePreservingSummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=defaults["trigger"],
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=defaults["truncate_args_settings"],
    )


__all__ = [
    "CACHE_PRESERVING_STRATEGY",
    "CACHE_REPLACEMENT_STRATEGY",
    "CachePreservingPartition",
    "CachePreservingSummarizationMiddleware",
    "CachePreservingSummarizationToolMiddleware",
    "CompactConversationSchema",
    "apply_summarization_event",
    "build_cache_preserving_event",
    "build_cache_preserving_partition",
    "build_safe_compaction_partition",
    "create_cache_preserving_summarization_middleware",
    "strip_media_from_summary_messages",
]
