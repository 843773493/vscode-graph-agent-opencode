"""Provider 输出格式校验库。

背景:
    每个 provider（opencode_zen / responses / chat.completion / 未来的 anthropic /
    google-genai 等）都需要把后端返回的"方言"流转换为统一格式，供
    `agent_execution_service` 处理后通过 SSE 推送给前端，并写入 LangGraph
    checkpoint 用于多轮对话。

    历史上我们遇到过的隐性 bug 包括：
    - reasoning 字符串被 LangChain ChatOpenAI 静默丢弃（前端"LLM 空响应"）
    - tool_call_chunks 字段为空，工具调用决策被丢弃
    - 流结束时缺 `reasoning_end` 标记，reasoning 混入 text 流
    - 跨 provider 切换时 Responses API 的 `type: reasoning` 块在 Chat Completions
      后端 400 错误
    - 把 LangChain 风格 role（"human"/"ai"）直接发给 OpenAI 风格后端 400

    这些 bug 的共同特征是："格式"是隐性的，没有运行时校验。本模块把隐性规范
    变成显性 contract，由各 provider 在 `self_check()` 中调用，且由测试自动覆盖。

设计原则:
    - 不发起任何网络请求；只对**已经构造好的 chunk 序列**做断言式检查
    - 每个检查项都是独立纯函数，便于组合与测试
    - 失败时给出可执行的 `remediation` 提示，让新人能在 30 秒内知道"该改哪里"
    - 不强制 provider 必须以某种方式实现 `_astream`（duck typing），只要
      `_astream` 的输出通过这些检查就算"接入正确"

使用方式（provider 端）::

    class MyChatOpenAI(ChatOpenAI):
        def self_check(self) -> FormatCheckResult:
            return validate_provider_format(self, fixtures=build_default_fixtures())

使用方式（测试端）::

    def test_my_provider_format():
        provider = MyChatOpenAI(...)
        result = provider.self_check()
        assert result.all_passed, result.report()
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk


__all__ = [
    # 协议
    "MessageFormatValidator",
    "FixtureStreamBuilder",
    # 允许值集合
    "ALLOWED_KINDS",
    "ALLOWED_PHASES",
    # 数据结构
    "FormatCheckItem",
    "FormatCheckResult",
    # 默认 fixture
    "ReasoningOnlyFixture",
    "TextOnlyFixture",
    "MixedReasoningTextFixture",
    "ToolCallFixture",
    "ReasoningAndToolFixture",
    "build_default_fixtures",
    # 各项检查
    "check_chunks_are_aimessage_chunks",
    "check_kind_values_allowed",
    "check_phase_values_allowed",
    "check_reasoning_has_start_marker",
    "check_reasoning_has_end_marker",
    "check_text_only_after_reasoning_end",
    "check_text_chunk_content_is_string",
    "check_reasoning_chunk_content_is_string",
    "check_tool_call_chunks_have_required_fields",
    "check_no_unclosed_reasoning_at_stream_end",
    "check_chunks",
    "ALL_CHECKS",
    # 一键入口
    "validate_provider_format",
    "check_history_messages_accepted",
]


# -----------------------------------------------------------------------------
# 统一格式规范
# -----------------------------------------------------------------------------

# 允许的 kind 标记；None 表示"未分类，由上层按 tool_calls 自行判断"
ALLOWED_KINDS: frozenset[str | None] = frozenset({None, "reasoning", "text", "tool"})

# 允许的 phase 标记；None 表示普通 delta
ALLOWED_PHASES: frozenset[str | None] = frozenset(
    {None, "start", "delta", "end"}
)


# -----------------------------------------------------------------------------
# 协议：provider 必须实现 self_check()
# -----------------------------------------------------------------------------
class MessageFormatValidator(Protocol):
    """任何 ChatModel provider 都应实现的"格式自检"协议。

    `agent_factory` 装配 provider 后会调用 `self_check()` 验证接入正确；
    各 provider 也可以在自己的测试里调用它获得可读的失败报告。
    """

    def self_check(self) -> "FormatCheckResult":
        ...


# -----------------------------------------------------------------------------
# 数据结构
# -----------------------------------------------------------------------------
@dataclass
class FormatCheckItem:
    """单条检查结果。"""

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
    """一次完整自检的聚合结果。"""

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
        """生成可贴到 PR / 日志里的人类可读报告。"""
        head = f"# FormatCheckReport(provider={self.provider!r})"
        summary = (
            f"通过 {sum(1 for i in self.items if i.passed)}/"
            f"{len(self.items)}，"
            f"失败 {len(self.failed)}"
        )
        body = "\n".join(item.render() for item in self.items)
        return f"{head}\n{summary}\n{body}"


# -----------------------------------------------------------------------------
# 内部工具：把 stream 转成 chunk 列表
# -----------------------------------------------------------------------------
async def _collect_chunks(
    stream: AsyncIterator[ChatGenerationChunk],
) -> list[ChatGenerationChunk]:
    """把异步流收集为 list，方便做多次断言。"""
    chunks: list[ChatGenerationChunk] = []
    async for c in stream:
        chunks.append(c)
    return chunks


def _chunk_message(chunk: Any) -> AIMessageChunk | None:
    """ChatGenerationChunk → AIMessageChunk；容错处理 None。"""
    if chunk is None:
        return None
    msg = getattr(chunk, "message", None)
    if isinstance(msg, AIMessageChunk):
        return msg
    if isinstance(chunk, AIMessageChunk):
        return chunk
    return None


def _kind_of(msg: AIMessageChunk) -> str | None:
    """提取 additional_kwargs["kind"]，未设置则返回 None。"""
    return (msg.additional_kwargs or {}).get("kind")


def _phase_of(msg: AIMessageChunk) -> str | None:
    return (msg.additional_kwargs or {}).get("phase")


# -----------------------------------------------------------------------------
# 单项检查：所有"check_xxx"函数都接受"已经构造好的 chunk 序列"
# 失败时构造 FormatCheckItem(passed=False, ...)，通过时构造 passed=True
# 设计成纯函数，便于单元测试。
# -----------------------------------------------------------------------------


def check_chunks_are_aimessage_chunks(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 1: 每个 chunk 都必须承载一个 AIMessageChunk。

    失败影响: agent_execution_service 拿不到 message，整个流式链路断。
    """
    bad = []
    for i, c in enumerate(chunks):
        if _chunk_message(c) is None:
            bad.append(i)
    if bad:
        return FormatCheckItem(
            name="每个 chunk 必须承载 AIMessageChunk",
            passed=False,
            detail=f"以下索引位置不是 AIMessageChunk: {bad}",
            remediation=(
                "_astream 里 yield 的应该是 "
                "ChatGenerationChunk(message=AIMessageChunk(...))。"
                "如果直接 yield 字符串 / dict / 自定义对象，需要先包成 AIMessageChunk。"
            ),
        )
    return FormatCheckItem(name="每个 chunk 必须承载 AIMessageChunk", passed=True)


def check_kind_values_allowed(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 2: additional_kwargs["kind"] 必须是 ALLOWED_KINDS 之一。"""
    bad: list[tuple[int, str | None]] = []
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        k = _kind_of(msg)
        if k not in ALLOWED_KINDS:
            bad.append((i, k))
    if bad:
        return FormatCheckItem(
            name="kind 标记必须是 reasoning/text/tool 之一",
            passed=False,
            detail=f"非法 kind: {bad}",
            remediation=(
                "统一格式约定: kind ∈ {'reasoning', 'text', 'tool', None}。"
                "agent_execution_service 只识别这几个值，其它会被丢弃。"
                "常见错误: 写成 'thinking'（应改为 'reasoning'）或 'reason'。"
            ),
        )
    return FormatCheckItem(name="kind 标记必须是 reasoning/text/tool 之一", passed=True)


def check_phase_values_allowed(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 2b: phase 标记必须是 ALLOWED_PHASES 之一。"""
    bad: list[tuple[int, str | None]] = []
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        p = _phase_of(msg)
        if p not in ALLOWED_PHASES:
            bad.append((i, p))
    if bad:
        return FormatCheckItem(
            name="phase 标记必须是 start/delta/end 之一",
            passed=False,
            detail=f"非法 phase: {bad}",
            remediation="phase 字段仅用于 reasoning 边界标记：start → delta* → end。",
        )
    return FormatCheckItem(name="phase 标记必须是 start/delta/end 之一", passed=True)


def check_reasoning_has_start_marker(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 3: 第一次出现 kind=reasoning 之前必须有 phase=start 标记。"""
    saw_start = False
    bad_index: int | None = None
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        if _kind_of(msg) == "reasoning":
            if not saw_start and _phase_of(msg) != "start":
                bad_index = i
                break
            if _phase_of(msg) == "start":
                saw_start = True
    if bad_index is not None:
        return FormatCheckItem(
            name="reasoning 流必须有 phase=start 边界标记",
            passed=False,
            detail=f"在 chunk[{bad_index}] 处发现 reasoning 内容但缺 start 标记",
            remediation=(
                "在第一次发 reasoning 内容之前，先 yield 一个 "
                "phase='start'、content='' 的标记 chunk，"
                "这样前端能识别推理开始、agent_execution_service 能正确触发 TEXT_START。"
            ),
        )
    return FormatCheckItem(
        name="reasoning 流必须有 phase=start 边界标记", passed=True
    )


def check_reasoning_has_end_marker(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 4: reasoning 流必须以 phase=end 标记关闭。"""
    saw_reasoning = False
    saw_end = False
    for c in chunks:
        msg = _chunk_message(c)
        if msg is None:
            continue
        if _kind_of(msg) == "reasoning":
            saw_reasoning = True
            if _phase_of(msg) == "end":
                saw_end = True
    if saw_reasoning and not saw_end:
        return FormatCheckItem(
            name="reasoning 流必须有 phase=end 边界标记",
            passed=False,
            detail="流中出现 reasoning 内容，但未发现 phase=end 标记",
            remediation=(
                "在最后一个 reasoning delta 之后必须 yield phase='end'、content=''。"
                "如果流结束前 reasoning_started && !reasoning_finished，"
                "也要补一个 end 标记（参见 opencode_zen.py 的尾部处理）。"
            ),
        )
    return FormatCheckItem(
        name="reasoning 流必须有 phase=end 边界标记", passed=True
    )


def check_text_only_after_reasoning_end(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 5: text 必须在 reasoning end 之后出现（如果存在 reasoning）。

    例外: 流中完全没有 reasoning 时，这条不检查。
    """
    saw_reasoning = any(
        _kind_of(_chunk_message(c) or c) == "reasoning"  # type: ignore[arg-type]
        for c in chunks
        if _chunk_message(c) is not None
    )
    if not saw_reasoning:
        return FormatCheckItem(
            name="text 必须在 reasoning end 之后出现（流中无 reasoning 时跳过）",
            passed=True,
        )

    reasoning_closed = False
    bad_index: int | None = None
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        kind = _kind_of(msg)
        phase = _phase_of(msg)
        if kind == "reasoning" and phase == "end":
            reasoning_closed = True
        if kind == "text" and not reasoning_closed:
            bad_index = i
            break
    if bad_index is not None:
        return FormatCheckItem(
            name="text 必须在 reasoning end 之后出现",
            passed=False,
            detail=(
                f"在 chunk[{bad_index}] 处出现 kind=text，但之前的 reasoning 未关闭"
            ),
            remediation=(
                "发第一个 kind=text 之前必须先 yield phase='end' 的 reasoning 标记，"
                "否则 reasoning 内容会与 text 内容混合，污染前端展示。"
            ),
        )
    return FormatCheckItem(name="text 必须在 reasoning end 之后出现", passed=True)


def check_text_chunk_content_is_string(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 6: kind=text 的 chunk content 必须是字符串。

    失败影响: agent_execution_service 在拼接文本时直接 str() 强转，
    会把 dict 块序列化成 '[{}]' 形式，前端显示乱码。
    """
    bad: list[tuple[int, type]] = []
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        if _kind_of(msg) == "text" and not isinstance(msg.content, str):
            bad.append((i, type(msg.content)))
    if bad:
        return FormatCheckItem(
            name="text chunk 的 content 必须是字符串",
            passed=False,
            detail=f"非字符串 content 出现在: {bad}",
            remediation=(
                "把 content 累加成字符串（delta 形式），"
                "不要试图塞入 Responses API 风格的 [{'type':'text','text':...}] 块。"
                "如果是回环（消费历史）要发 Responses API 块，让 LangChain 的 "
                "_construct_responses_api_input 处理，不要在 provider 内部手发。"
            ),
        )
    return FormatCheckItem(name="text chunk 的 content 必须是字符串", passed=True)


def check_reasoning_chunk_content_is_string(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 7: kind=reasoning 的 chunk content 必须是字符串（同 6 的原因）。"""
    bad: list[tuple[int, type]] = []
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        if _kind_of(msg) == "reasoning" and not isinstance(msg.content, str):
            bad.append((i, type(msg.content)))
    if bad:
        return FormatCheckItem(
            name="reasoning chunk 的 content 必须是字符串",
            passed=False,
            detail=f"非字符串 content 出现在: {bad}",
            remediation=(
                "reasoning 阶段的 content 应当是 delta 字符串。"
                "整段累积请用 additional_kwargs['kind']='reasoning' 标记，"
                "不要尝试用 Responses API 风格的内容块。"
            ),
        )
    return FormatCheckItem(
        name="reasoning chunk 的 content 必须是字符串", passed=True
    )


def check_tool_call_chunks_have_required_fields(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 8: tool_call_chunks 每项至少要带 name 或 args。

    失败影响: LangChain 上层收不到工具调用信息，整次工具决策被静默丢弃。
    """
    bad: list[tuple[int, list[str]]] = []
    for i, c in enumerate(chunks):
        msg = _chunk_message(c)
        if msg is None:
            continue
        tcc = msg.tool_call_chunks or []
        for j, tc in enumerate(tcc):
            if not isinstance(tc, dict):
                bad.append((i, [f"#{j} not dict"]))
                continue
            missing = [k for k in ("name", "args", "id") if k not in tc or tc[k] is None]
            if len(missing) == 3:  # 三个字段全缺才算"无效"
                bad.append((i, missing))
    if bad:
        return FormatCheckItem(
            name="tool_call_chunks 必须包含 name/args/id 至少一项",
            passed=False,
            detail=f"无效的 tool_call_chunks: {bad[:3]}...",
            remediation=(
                "把 OpenAI 原始 tool_calls.delta 透传时必须保留 name/args/id 字段：\n"
                "    tool_call_chunks=[{\n"
                "        'name': function.name,\n"
                "        'args': function.arguments,\n"
                "        'id': rtc.id,\n"
                "        'index': rtc.index,\n"
                "    }]"
            ),
        )
    return FormatCheckItem(
        name="tool_call_chunks 必须包含 name/args/id 至少一项", passed=True
    )


def check_no_unclosed_reasoning_at_stream_end(
    chunks: Sequence[ChatGenerationChunk],
) -> FormatCheckItem:
    """检查 9: 流结束时 reasoning 必须已关闭。"""
    for c in chunks:
        msg = _chunk_message(c)
        if msg is None:
            continue
        if _kind_of(msg) == "reasoning" and _phase_of(msg) == "end":
            return FormatCheckItem(
                name="流结束时 reasoning 必须已关闭", passed=True
            )
    saw_reasoning = any(
        _kind_of(_chunk_message(c) or c) == "reasoning"  # type: ignore[arg-type]
        for c in chunks
        if _chunk_message(c) is not None
    )
    if saw_reasoning:
        return FormatCheckItem(
            name="流结束时 reasoning 必须已关闭",
            passed=False,
            detail="流中含 reasoning 内容但未发出 end 标记",
            remediation=(
                "在 _astream 的 for 循环结束后，加一个尾部守卫：\n"
                "    if reasoning_started and not reasoning_finished:\n"
                "        yield ChatGenerationChunk(\n"
                "            message=AIMessageChunk(\n"
                "                content='',\n"
                "                additional_kwargs={'kind':'reasoning','phase':'end'},\n"
                "            )\n"
                "        )"
            ),
        )
    return FormatCheckItem(
        name="流结束时 reasoning 必须已关闭（流中无 reasoning 时跳过）",
        passed=True,
    )


# -----------------------------------------------------------------------------
# Fixtures：构造"已知行为"的 chunk 序列，让 check_* 跑回归
# -----------------------------------------------------------------------------
class StreamFixture(Protocol):
    """一个 fixture 定义：给定一个 provider，运行后产出 chunk 列表。"""

    name: str

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        ...


@dataclass
class ReasoningOnlyFixture:
    """纯 reasoning 流：模拟模型只输出 reasoning，没有 text。"""

    name: str = "reasoning_only"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        # 直接调用 _astream 接口；但需要在没有真实 LLM 的情况下"造"出流。
        # 实际使用时由 provider 实现 _build_fixture_stream 钩子。
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
        return await _build_provider_stream(
            provider, scenario="mixed_reasoning_text"
        )


@dataclass
class ToolCallFixture:
    name: str = "tool_call"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(provider, scenario="tool_call")


@dataclass
class ReasoningAndToolFixture:
    name: str = "reasoning_then_tool"

    async def run(self, provider: Any) -> list[ChatGenerationChunk]:
        return await _build_provider_stream(
            provider, scenario="reasoning_then_tool"
        )


def build_default_fixtures() -> list[StreamFixture]:
    """返回推荐的默认 fixture 集合。"""
    return [
        ReasoningOnlyFixture(),
        TextOnlyFixture(),
        MixedReasoningTextFixture(),
        ToolCallFixture(),
        ReasoningAndToolFixture(),
    ]


# -----------------------------------------------------------------------------
# Provider 钩子：每个 provider 实现这个方法返回"造出来的流"
# -----------------------------------------------------------------------------
class FixtureStreamBuilder(Protocol):
    """Provider 通过实现这个协议来"造"出可重现的流。"""

    async def build_stream(
        self, scenario: str
    ) -> AsyncIterator[ChatGenerationChunk]:
        ...


async def _build_provider_stream(
    provider: Any, scenario: str
) -> list[ChatGenerationChunk]:
    """调用 provider 的 build_stream 钩子；如果不存在则跳过此 fixture。

    跳过不是失败 —— 我们在结果中标注 SKIPPED。
    """
    builder: FixtureStreamBuilder | None = getattr(provider, "build_stream", None)
    if builder is None:
        return []
    # 注意：builder 是绑定方法，调用它拿到 AsyncIterator[ChatGenerationChunk]
    # 不要再写 builder.build_stream(scenario)，那会查方法上的方法
    chunks: list[ChatGenerationChunk] = []
    async for c in builder(scenario):
        chunks.append(c)
    return chunks


# -----------------------------------------------------------------------------
# 一键入口
# -----------------------------------------------------------------------------
ALL_CHECKS: list[Callable[[Sequence[ChatGenerationChunk]], FormatCheckItem]] = [
    check_chunks_are_aimessage_chunks,
    check_kind_values_allowed,
    check_phase_values_allowed,
    check_reasoning_has_start_marker,
    check_reasoning_has_end_marker,
    check_text_only_after_reasoning_end,
    check_text_chunk_content_is_string,
    check_reasoning_chunk_content_is_string,
    check_tool_call_chunks_have_required_fields,
    check_no_unclosed_reasoning_at_stream_end,
]


def check_chunks(
    chunks: Sequence[ChatGenerationChunk],
) -> list[FormatCheckItem]:
    """对一组 chunk 跑所有检查，返回每项结果。"""
    return [check(chunks) for check in ALL_CHECKS]


async def validate_provider_format(
    provider: Any,
    *,
    fixtures: Sequence[StreamFixture] | None = None,
) -> FormatCheckResult:
    """一键运行入口：对 provider 跑所有 fixture × 所有检查项。

    Args:
        provider: 任何 ChatModel 实例（必须实现 self_check 或 build_stream）
        fixtures: 要跑的 fixture 列表；默认使用 build_default_fixtures()

    Returns:
        FormatCheckResult，调用 .all_passed / .report() 拿到结论
    """
    if fixtures is None:
        fixtures = build_default_fixtures()

    result = FormatCheckResult(provider=type(provider).__name__)

    for fixture in fixtures:
        try:
            chunks = await fixture.run(provider)
        except Exception as exc:  # fixture 运行本身出错
            result.add(FormatCheckItem(
                name=f"[{fixture.name}] fixture 自身运行出错",
                passed=False,
                detail=str(exc),
                remediation=(
                    "检查 provider 的 build_stream(scenario) 实现：\n"
                    "  - 是否正确处理了 scenario 字符串\n"
                    "  - 是否在测试场景下被替换为本地 fixture 而不是真发请求"
                ),
            ))
            continue
        if not chunks:
            # provider 没有 build_stream 钩子 → 视为 SKIPPED（不是失败）
            result.add(FormatCheckItem(
                name=f"[{fixture.name}] provider 未实现 build_stream，跳过",
                passed=True,
                detail=(
                    "如果想为此 fixture 启用格式校验，"
                    "请在 provider 上实现 build_stream(scenario) 钩子"
                ),
            ))
            continue
        for item in check_chunks(chunks):
            result.add(FormatCheckItem(
                name=f"[{fixture.name}] {item.name}",
                passed=item.passed,
                detail=item.detail,
                remediation=item.remediation,
            ))

    return result


# -----------------------------------------------------------------------------
# 历史消息回环检查：喂给 provider 的 AIMessage 必须能被它正确转换
# -----------------------------------------------------------------------------
def check_history_messages_accepted(
    provider: Any,
    messages: Sequence[BaseMessage],
) -> FormatCheckItem:
    """检查 provider 是否能处理"带 reasoning 块的 AIMessage 历史"。

    调 provider._convert_messages_to_dicts(messages)，看返回的 dict 是否符合
    通用 OpenAI Chat Completions 协议（key=role/value=content），
    且没有未识别的角色（"human"/"ai" 等 LangChain 风格 role 未被映射）。
    """
    converter = getattr(provider, "_convert_messages_to_dicts", None)
    if converter is None:
        return FormatCheckItem(
            name="provider 必须实现 _convert_messages_to_dicts 以支持多轮对话",
            passed=False,
            detail="未找到 _convert_messages_to_dicts 方法",
            remediation=(
                "实现 _convert_messages_to_dicts 时：\n"
                "  1. 把 LangChain 风格 role（human→user、ai→assistant）映射为 OpenAI 标准 role\n"
                "  2. 展平 Responses API 风格 [{'type':'reasoning',...}] 块为 <think>...</think> 文本\n"
                "  3. 保留 function_call / tool_calls 字段让 LangChain 上层处理"
            ),
        )
    try:
        result_dicts = converter(list(messages))
    except Exception as exc:
        return FormatCheckItem(
            name="_convert_messages_to_dicts 不应抛异常",
            passed=False,
            detail=f"抛出了 {type(exc).__name__}: {exc}",
            remediation=(
                "对未知块类型要 fallback，而不是 raise。"
                "建议 pattern: 跳过未知 dict 块、或注入默认 text 块。"
            ),
        )

    bad_roles: list[int] = []
    for i, d in enumerate(result_dicts):
        if not isinstance(d, dict):
            continue
        role = d.get("role")
        if role in ("human", "ai"):
            bad_roles.append(i)
    if bad_roles:
        return FormatCheckItem(
            name="历史消息 role 必须被映射为 OpenAI 标准（user/assistant/system/tool）",
            passed=False,
            detail=f"以下索引仍是 LangChain 风格 role: {bad_roles}",
            remediation=(
                "在 _convert_messages_to_dicts 中:\n"
                "    role = 'human' → 'user'\n"
                "    role = 'ai'    → 'assistant'"
            ),
        )
    return FormatCheckItem(
        name="历史消息 role 必须被映射为 OpenAI 标准（user/assistant/system/tool）",
        passed=True,
    )
