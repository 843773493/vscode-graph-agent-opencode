"""Provider 输出格式校验库。"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk


__all__ = [
    "MessageFormatValidator",
    "FixtureStreamBuilder",
    "FormatCheckItem",
    "FormatCheckResult",
    "ReasoningOnlyFixture",
    "TextOnlyFixture",
    "MixedReasoningTextFixture",
    "ToolCallFixture",
    "ReasoningAndToolFixture",
    "build_default_fixtures",
    "check_chunks_are_aimessage_chunks",
    "check_no_private_stream_markers",
    "check_content_blocks_are_standard",
    "check_stream_merges_without_private_marker_noise",
    "check_tool_call_chunks_have_required_fields",
    "check_chunks",
    "ALL_CHECKS",
    "validate_provider_format",
    "check_history_messages_accepted",
]


SUPPORTED_CONTENT_BLOCK_TYPES = frozenset(
    {
        "text",
        "reasoning",
        "refusal",
        "tool_call_chunk",
        "tool_call",
    }
)


class MessageFormatValidator(Protocol):
    def self_check(self) -> "FormatCheckResult":
        ...


@dataclass
class FormatCheckItem:
    name: str
    passed: bool
    detail: str = ""
    remediation: str = ""

    def render(self) -> str:
        icon = "✅" if self.passed else "❌"
        lines = [f"{icon} {self.name}"]
        if self.detail:
            lines.append(f"   详情: {self.detail}")
        if not self.passed and self.remediation:
            lines.append(f"   修复: {self.remediation}")
        return "\n".join(lines)


@dataclass
class FormatCheckResult:
    provider: str
    items: list[FormatCheckItem] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(item.passed for item in self.items)

    @property
    def failed(self) -> list[FormatCheckItem]:
        return [item for item in self.items if not item.passed]

    def add(self, item: FormatCheckItem) -> None:
        self.items.append(item)

    def report(self) -> str:
        head = f"# FormatCheckReport(provider={self.provider!r})"
        summary = (
            f"通过 {sum(1 for item in self.items if item.passed)}/"
            f"{len(self.items)}，失败 {len(self.failed)}"
        )
        body = "\n".join(item.render() for item in self.items)
        return f"{head}\n{summary}\n{body}"


def _chunk_message(chunk: Any) -> AIMessageChunk | None:
    if chunk is None:
        return None
    message = getattr(chunk, "message", None)
    if isinstance(message, AIMessageChunk):
        return message
    if isinstance(chunk, AIMessageChunk):
        return chunk
    return None


def _message_content_blocks(message: AIMessageChunk) -> list[dict[str, Any]]:
    content = message.content
    if isinstance(content, str):
        if not content:
            return []
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if isinstance(item, dict):
            blocks.append(dict(item))
            continue
        blocks.append({"type": "text", "text": str(item)})
    return blocks


def _stream_has_block_type(
    chunks: Sequence[ChatGenerationChunk],
    block_type: str,
) -> bool:
    for chunk in chunks:
        message = _chunk_message(chunk)
        if message is None:
            continue
        if any(block.get("type") == block_type for block in _message_content_blocks(message)):
            return True
    return False


def check_chunks_are_aimessage_chunks(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    bad = []
    for index, chunk in enumerate(chunks):
        if _chunk_message(chunk) is None:
            bad.append(index)
    if bad:
        return FormatCheckItem(
            name="每个 chunk 必须承载 AIMessageChunk",
            passed=False,
            detail=f"以下索引位置不是 AIMessageChunk: {bad}",
            remediation="provider 应 yield ChatGenerationChunk(message=AIMessageChunk(...))。",
        )
    return FormatCheckItem(name="每个 chunk 必须承载 AIMessageChunk", passed=True)


def check_no_private_stream_markers(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    bad: list[int] = []
    for index, chunk in enumerate(chunks):
        message = _chunk_message(chunk)
        if message is None:
            continue
        additional_kwargs = message.additional_kwargs or {}
        if "kind" in additional_kwargs or "phase" in additional_kwargs:
            bad.append(index)
    if bad:
        return FormatCheckItem(
            name="流式分类不得写入 additional_kwargs.kind/phase",
            passed=False,
            detail=f"发现私有流式标记的 chunk 索引: {bad}",
            remediation=(
                "reasoning/text 必须使用 LangChain 标准 content blocks；"
                "UI 进度或阶段信息应走应用自己的事件流。"
            ),
        )
    return FormatCheckItem(
        name="流式分类不得写入 additional_kwargs.kind/phase",
        passed=True,
    )


def check_content_blocks_are_standard(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    bad: list[tuple[int, str]] = []
    for index, chunk in enumerate(chunks):
        message = _chunk_message(chunk)
        if message is None:
            continue
        for block in _message_content_blocks(message):
            block_type = block.get("type")
            if block_type not in SUPPORTED_CONTENT_BLOCK_TYPES:
                bad.append((index, str(block_type)))
                continue
            if block_type == "reasoning" and not (
                isinstance(block.get("reasoning"), str)
                or isinstance(block.get("summary"), list)
            ):
                bad.append((index, "reasoning_without_text"))
            if block_type == "text" and not isinstance(block.get("text"), str):
                bad.append((index, "text_without_text"))
    if bad:
        return FormatCheckItem(
            name="content 必须使用 LangChain 标准 content blocks",
            passed=False,
            detail=f"异常 content block: {bad[:5]}",
            remediation="使用 {'type':'reasoning','reasoning': ...} 或 {'type':'text','text': ...}。",
        )
    return FormatCheckItem(
        name="content 必须使用 LangChain 标准 content blocks",
        passed=True,
    )


def check_stream_merges_without_private_marker_noise(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    merged: AIMessageChunk | None = None
    for chunk in chunks:
        message = _chunk_message(chunk)
        if message is None:
            continue
        merged = message if merged is None else merged + message

    if merged is None:
        return FormatCheckItem(
            name="chunk 合并后不得产生私有标记噪声",
            passed=True,
            detail="空流跳过",
        )

    additional_kwargs = merged.additional_kwargs or {}
    if "kind" in additional_kwargs or "phase" in additional_kwargs:
        return FormatCheckItem(
            name="chunk 合并后不得产生私有标记噪声",
            passed=False,
            detail=f"合并后 additional_kwargs={additional_kwargs!r}",
            remediation="不要把 per-delta 分类信息放入 additional_kwargs。",
        )
    return FormatCheckItem(
        name="chunk 合并后不得产生私有标记噪声",
        passed=True,
    )


def check_tool_call_chunks_have_required_fields(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    bad: list[tuple[int, list[str]]] = []
    for index, chunk in enumerate(chunks):
        message = _chunk_message(chunk)
        if message is None:
            continue
        for tool_index, tool_call in enumerate(message.tool_call_chunks or []):
            if not isinstance(tool_call, dict):
                bad.append((index, [f"#{tool_index} not dict"]))
                continue
            missing = [
                key
                for key in ("name", "args", "id")
                if key not in tool_call or tool_call[key] is None
            ]
            if len(missing) == 3:
                bad.append((index, missing))
    if bad:
        return FormatCheckItem(
            name="tool_call_chunks 必须包含 name/args/id 至少一项",
            passed=False,
            detail=f"无效的 tool_call_chunks: {bad[:3]}",
            remediation="透传 OpenAI tool_calls.delta 时必须保留 name/args/id/index 字段。",
        )
    return FormatCheckItem(
        name="tool_call_chunks 必须包含 name/args/id 至少一项",
        passed=True,
    )


class StreamFixture(Protocol):
    name: str

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        ...


@dataclass
class ReasoningOnlyFixture:
    name: str = "reasoning_only"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="reasoning_only")


@dataclass
class TextOnlyFixture:
    name: str = "text_only"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="text_only")


@dataclass
class MixedReasoningTextFixture:
    name: str = "mixed_reasoning_text"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="mixed_reasoning_text")


@dataclass
class ToolCallFixture:
    name: str = "tool_call"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="tool_call")


@dataclass
class ReasoningAndToolFixture:
    name: str = "reasoning_then_tool"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="reasoning_then_tool")


def build_default_fixtures() -> list[StreamFixture]:
    return [
        ReasoningOnlyFixture(),
        TextOnlyFixture(),
        MixedReasoningTextFixture(),
        ToolCallFixture(),
        ReasoningAndToolFixture(),
    ]


class FixtureStreamBuilder(Protocol):
    async def build_stream(
        self, scenario: str
    ) -> AsyncIterator[ChatGenerationChunk]:
        ...


async def _build_provider_stream(
    provider: Any, scenario: str
) -> list[ChatGenerationChunk]:
    builder: FixtureStreamBuilder | None = getattr(provider, "build_stream", None)
    if builder is None:
        return []

    chunks: list[ChatGenerationChunk] = []
    async for chunk in builder(scenario):
        chunks.append(chunk)
    return chunks


ALL_CHECKS: list[Callable[[Sequence[ChatGenerationChunk]], FormatCheckItem]] = [
    check_chunks_are_aimessage_chunks,
    check_no_private_stream_markers,
    check_content_blocks_are_standard,
    check_stream_merges_without_private_marker_noise,
    check_tool_call_chunks_have_required_fields,
]


def check_chunks(
    chunks: Sequence[ChatGenerationChunk],
) -> list[FormatCheckItem]:
    return [check(chunks) for check in ALL_CHECKS]


def _check_fixture_expectations(
    fixture_name: str,
    chunks: Sequence[ChatGenerationChunk],
) -> list[FormatCheckItem]:
    items: list[FormatCheckItem] = []
    if fixture_name in {"reasoning_only", "mixed_reasoning_text", "reasoning_then_tool"}:
        has_reasoning = _stream_has_block_type(chunks, "reasoning")
        items.append(
            FormatCheckItem(
                name="需要 reasoning 的 fixture 必须产出 reasoning content block",
                passed=has_reasoning,
                detail="" if has_reasoning else f"fixture={fixture_name}",
                remediation="把 reasoning_content 转成 {'type':'reasoning','reasoning': delta}。",
            )
        )
    if fixture_name in {"text_only", "mixed_reasoning_text", "tool_call", "reasoning_then_tool"}:
        has_text = _stream_has_block_type(chunks, "text")
        items.append(
            FormatCheckItem(
                name="需要正文的 fixture 必须产出 text content block",
                passed=has_text,
                detail="" if has_text else f"fixture={fixture_name}",
                remediation="把可见正文转成 {'type':'text','text': delta}。",
            )
        )
    return items


async def validate_provider_format(
    provider: Any,
    *,
    fixtures: Sequence[StreamFixture] | None = None,
) -> FormatCheckResult:
    if fixtures is None:
        fixtures = build_default_fixtures()

    result = FormatCheckResult(provider=type(provider).__name__)
    for fixture in fixtures:
        try:
            chunks = await fixture.run(provider)
        except Exception as exc:
            result.add(
                FormatCheckItem(
                    name=f"[{fixture.name}] fixture 自身运行出错",
                    passed=False,
                    detail=str(exc),
                    remediation="检查 provider.build_stream(scenario) 本地 fixture。",
                )
            )
            continue
        if not chunks:
            result.add(
                FormatCheckItem(
                    name=f"[{fixture.name}] provider 未实现 build_stream，跳过",
                    passed=True,
                )
            )
            continue
        for item in check_chunks(chunks):
            result.add(
                FormatCheckItem(
                    name=f"[{fixture.name}] {item.name}",
                    passed=item.passed,
                    detail=item.detail,
                    remediation=item.remediation,
                )
            )
        for item in _check_fixture_expectations(fixture.name, chunks):
            result.add(
                FormatCheckItem(
                    name=f"[{fixture.name}] {item.name}",
                    passed=item.passed,
                    detail=item.detail,
                    remediation=item.remediation,
                )
            )
    return result


def check_history_messages_accepted(
    provider: Any,
    messages: Sequence[BaseMessage],
) -> FormatCheckItem:
    converter = getattr(provider, "_convert_messages_to_dicts", None)
    if converter is None:
        return FormatCheckItem(
            name="provider 必须实现 _convert_messages_to_dicts 以支持多轮对话",
            passed=False,
            detail="未找到 _convert_messages_to_dicts 方法",
            remediation=(
                "实现 _convert_messages_to_dicts：role 映射为 OpenAI 标准，"
                "并把历史 reasoning blocks 从 Chat Completions 请求正文中剥离。"
            ),
        )
    try:
        result_dicts = converter(list(messages))
    except Exception as exc:
        return FormatCheckItem(
            name="_convert_messages_to_dicts 不应抛异常",
            passed=False,
            detail=f"抛出了 {type(exc).__name__}: {exc}",
            remediation="历史消息转换失败时应显式修正 provider 适配逻辑。",
        )

    bad_roles: list[int] = []
    invalid_content_blocks: list[tuple[int, str]] = []
    for index, item in enumerate(result_dicts):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role in {"human", "ai"}:
            bad_roles.append(index)
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                invalid_content_blocks.append((index, "non_dict"))
                continue
            block_type = block.get("type")
            if block_type in {"reasoning", "output_text"}:
                invalid_content_blocks.append((index, str(block_type)))

    if bad_roles:
        return FormatCheckItem(
            name="历史消息 role 必须映射为 OpenAI 标准",
            passed=False,
            detail=f"以下索引仍是 LangChain role: {bad_roles}",
            remediation="human→user，ai→assistant。",
        )
    if invalid_content_blocks:
        return FormatCheckItem(
            name="历史消息 content blocks 必须适配 Chat Completions",
            passed=False,
            detail=f"以下历史消息仍含不可直传块: {invalid_content_blocks}",
            remediation="剥离 reasoning，把 output_text 转为 text。",
        )
    return FormatCheckItem(
        name="历史消息 role/content blocks 必须适配 Chat Completions",
        passed=True,
    )
