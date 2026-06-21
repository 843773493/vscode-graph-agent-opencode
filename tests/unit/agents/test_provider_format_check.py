"""Provider 格式自检接口的单元测试。

覆盖：
- `_format_check.py` 中每条 check_* 的正反场景
- `validate_provider_format()` 一键入口
- `OpencodeZenChatOpenAI.self_check()` 真实接入
- 历史消息回环检查
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from app.agents.providers._format_check import (
    ALL_CHECKS,
    check_chunks_are_aimessage_chunks,
    check_chunks,
    check_history_messages_accepted,
    check_kind_values_allowed,
    check_no_unclosed_reasoning_at_stream_end,
    check_phase_values_allowed,
    check_reasoning_chunk_content_is_string,
    check_reasoning_has_end_marker,
    check_reasoning_has_start_marker,
    check_text_chunk_content_is_string,
    check_text_only_after_reasoning_end,
    check_tool_call_chunks_have_required_fields,
    validate_provider_format,
    FormatCheckItem,
    FormatCheckResult,
)
from app.agents.providers.opencode_zen import OpencodeZenChatOpenAI


# -----------------------------------------------------------------------------
# 工具
# -----------------------------------------------------------------------------
def _chunk(
    content: object = "",
    *,
    kind: str | None = None,
    phase: str | None = None,
    tool_call_chunks: list[dict] | None = None,
) -> ChatGenerationChunk:
    """构造一个 ChatGenerationChunk 的快捷方式。"""
    additional: dict = {}
    if kind is not None:
        additional["kind"] = kind
    if phase is not None:
        additional["phase"] = phase
    return ChatGenerationChunk(
        message=AIMessageChunk(
            content=content,
            additional_kwargs=additional,
            tool_call_chunks=tool_call_chunks or [],
        )
    )


# -----------------------------------------------------------------------------
# check_chunks_are_aimessage_chunks
# -----------------------------------------------------------------------------
def test_chunks_must_be_aimessage_chunks_passes():
    chunks = [_chunk("hi", kind="text")]
    item = check_chunks_are_aimessage_chunks(chunks)
    assert item.passed, item.detail


def test_chunks_must_be_aimessage_chunks_fails_on_raw_string():
    # 模拟 provider 把字符串当作 chunk 直接 yield
    class FakeChunk:
        pass
    item = check_chunks_are_aimessage_chunks([FakeChunk()])  # type: ignore[list-item]
    assert not item.passed
    assert "0" in item.detail  # 索引 0 失败
    assert "ChatGenerationChunk" in item.remediation


# -----------------------------------------------------------------------------
# check_kind_values_allowed
# -----------------------------------------------------------------------------
def test_kind_allowed_values():
    for kind in (None, "reasoning", "text", "tool"):
        chunks = [_chunk("x", kind=kind)]
        assert check_kind_values_allowed(chunks).passed


def test_kind_disallowed_values():
    chunks = [_chunk("x", kind="thinking")]
    item = check_kind_values_allowed(chunks)
    assert not item.passed
    assert "thinking" in item.detail
    assert "reasoning" in item.remediation  # 提示里应提到正确的 kind


# -----------------------------------------------------------------------------
# check_phase_values_allowed
# -----------------------------------------------------------------------------
def test_phase_allowed_values():
    for phase in (None, "start", "delta", "end"):
        chunks = [_chunk("x", kind="reasoning", phase=phase)]
        assert check_phase_values_allowed(chunks).passed


def test_phase_disallowed_values():
    chunks = [_chunk("x", kind="reasoning", phase="opening")]
    item = check_phase_values_allowed(chunks)
    assert not item.passed


# -----------------------------------------------------------------------------
# check_reasoning_has_start_marker
# -----------------------------------------------------------------------------
def test_reasoning_start_marker_passes_when_present():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("思考", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
    ]
    assert check_reasoning_has_start_marker(chunks).passed


def test_reasoning_start_marker_fails_when_missing():
    chunks = [
        # 直接 delta，没有 start
        _chunk("思考", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
    ]
    item = check_reasoning_has_start_marker(chunks)
    assert not item.passed
    assert "start" in item.remediation


# -----------------------------------------------------------------------------
# check_reasoning_has_end_marker
# -----------------------------------------------------------------------------
def test_reasoning_end_marker_passes_when_present():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("思考", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
    ]
    assert check_reasoning_has_end_marker(chunks).passed


def test_reasoning_end_marker_fails_when_missing():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("思考", kind="reasoning", phase="delta"),
        # 缺 end
    ]
    item = check_reasoning_has_end_marker(chunks)
    assert not item.passed
    assert "end" in item.remediation


# -----------------------------------------------------------------------------
# check_text_only_after_reasoning_end
# -----------------------------------------------------------------------------
def test_text_after_reasoning_end_passes():
    chunks = [
        _chunk("r", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
        _chunk("t", kind="text", phase="delta"),
    ]
    assert check_text_only_after_reasoning_end(chunks).passed


def test_text_before_reasoning_end_fails():
    chunks = [
        _chunk("r", kind="reasoning", phase="delta"),
        # 没发 end 就开始 text
        _chunk("t", kind="text", phase="delta"),
    ]
    item = check_text_only_after_reasoning_end(chunks)
    assert not item.passed
    # detail 应明确指出哪条 reasoning 没关、哪条 text 越界
    assert "未关闭" in item.detail
    assert "phase='end'" in item.remediation


def test_text_only_stream_skips_reasoning_check():
    chunks = [_chunk("hi", kind="text", phase="delta")]
    assert check_text_only_after_reasoning_end(chunks).passed


# -----------------------------------------------------------------------------
# check_text_chunk_content_is_string
# -----------------------------------------------------------------------------
def test_text_content_must_be_string_passes():
    chunks = [_chunk("hi", kind="text")]
    assert check_text_chunk_content_is_string(chunks).passed


def test_text_content_must_be_string_fails():
    chunks = [
        ChatGenerationChunk(
            message=AIMessageChunk(
                content=[{"type": "text", "text": "hi"}],
                additional_kwargs={"kind": "text"},
            )
        )
    ]
    item = check_text_chunk_content_is_string(chunks)
    assert not item.passed
    assert "dict" in item.detail or "list" in item.detail


# -----------------------------------------------------------------------------
# check_reasoning_chunk_content_is_string
# -----------------------------------------------------------------------------
def test_reasoning_content_must_be_string_passes():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("思考", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
    ]
    assert check_reasoning_chunk_content_is_string(chunks).passed


def test_reasoning_content_must_be_string_fails():
    chunks = [
        ChatGenerationChunk(
            message=AIMessageChunk(
                content=[{"type": "reasoning", "summary": []}],
                additional_kwargs={"kind": "reasoning", "phase": "delta"},
            )
        )
    ]
    item = check_reasoning_chunk_content_is_string(chunks)
    assert not item.passed


# -----------------------------------------------------------------------------
# check_tool_call_chunks_have_required_fields
# -----------------------------------------------------------------------------
def test_tool_call_chunks_with_valid_fields_passes():
    chunks = [
        _chunk(
            "",
            tool_call_chunks=[{"name": "ls", "args": "{}", "id": "call_1"}],
        )
    ]
    assert check_tool_call_chunks_have_required_fields(chunks).passed


def test_tool_call_chunks_without_any_field_fails():
    chunks = [
        _chunk("", tool_call_chunks=[{"index": 0}])
    ]
    item = check_tool_call_chunks_have_required_fields(chunks)
    assert not item.passed
    assert "name" in item.remediation


# -----------------------------------------------------------------------------
# check_no_unclosed_reasoning_at_stream_end
# -----------------------------------------------------------------------------
def test_unclosed_reasoning_fails():
    chunks = [
        _chunk("思考", kind="reasoning", phase="delta"),
        # 没有 end
    ]
    item = check_no_unclosed_reasoning_at_stream_end(chunks)
    assert not item.passed
    assert "尾部守卫" in item.remediation or "guard" in item.remediation.lower() or "end" in item.remediation


def test_unclosed_reasoning_passes_when_end_emitted():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("思考", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
    ]
    assert check_no_unclosed_reasoning_at_stream_end(chunks).passed


# -----------------------------------------------------------------------------
# check_chunks 总入口
# -----------------------------------------------------------------------------
def test_check_chunks_runs_all_checks():
    chunks = [
        _chunk("", kind="reasoning", phase="start"),
        _chunk("x", kind="reasoning", phase="delta"),
        _chunk("", kind="reasoning", phase="end"),
        _chunk("y", kind="text", phase="delta"),
    ]
    items = check_chunks(chunks)
    assert len(items) == len(ALL_CHECKS)
    # 这一组是"理想流"，应该全过
    for item in items:
        assert item.passed, f"{item.name}: {item.detail}"


# -----------------------------------------------------------------------------
# check_history_messages_accepted
# -----------------------------------------------------------------------------
def _make_test_provider() -> OpencodeZenChatOpenAI:
    return OpencodeZenChatOpenAI(
        model="big-pickle",
        api_key="test-key",
        base_url="https://example.com/v1",
    )


def test_history_roundtrip_with_responses_api_reasoning_passes():
    provider = _make_test_provider()
    msgs = [
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "历史推理"}],
                },
                {"type": "text", "text": "历史回答"},
            ]
        )
    ]
    item = check_history_messages_accepted(provider, msgs)
    assert item.passed, item.detail


def test_history_roundtrip_missing_converter_fails_with_clear_message():
    class NoConverter:
        pass

    item = check_history_messages_accepted(NoConverter(), [AIMessage(content="x")])
    assert not item.passed
    assert "_convert_messages_to_dicts" in item.remediation


# -----------------------------------------------------------------------------
# validate_provider_format 一键入口 + OpencodeZen self_check 集成
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_validate_provider_format_with_fixtures():
    provider = _make_test_provider()
    result = await validate_provider_format(provider)
    # opencode_zen 的 build_stream 已经正确产出所有 fixture
    # 每条 check 在所有 fixture 上都应通过
    failed = result.failed
    assert not failed, "\n".join(item.render() for item in failed)


def test_opencode_zen_self_check_passes():
    provider = _make_test_provider()
    result = provider.self_check()
    assert result.all_passed, result.report()


def test_opencode_zen_self_check_report_is_human_readable():
    provider = _make_test_provider()
    result = provider.self_check()
    report = result.report()
    # 报告应包含 provider 名 + 每条 check 的图标
    assert "FormatCheckReport" in report
    assert "OpencodeZenChatOpenAI" in report
    assert "✅" in report or "❌" in report


# -----------------------------------------------------------------------------
# FormatCheckItem / FormatCheckResult 数据结构
# -----------------------------------------------------------------------------
def test_format_check_item_render_includes_remediation_only_when_failed():
    failed = FormatCheckItem(
        name="x", passed=False, detail="bad", remediation="do Y"
    )
    rendered = failed.render()
    assert "❌" in rendered
    assert "bad" in rendered
    assert "do Y" in rendered


def test_format_check_item_render_no_remediation_when_passed():
    passed = FormatCheckItem(name="x", passed=True, detail="ok")
    rendered = passed.render()
    assert "✅" in rendered
    assert "修复" not in rendered


def test_format_check_result_summary():
    result = FormatCheckResult(provider="X")
    result.add(FormatCheckItem(name="a", passed=True))
    result.add(FormatCheckItem(name="b", passed=False, detail="bad"))
    assert not result.all_passed
    assert len(result.failed) == 1
    report = result.report()
    assert "通过 1/2" in report
    assert "失败 1" in report
