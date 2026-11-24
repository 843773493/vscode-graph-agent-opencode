"""完整 wire 预算不能漏工具调用、schema、root，也不能把媒体编码算正文。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.agents.providers.request_budget import estimate_wire_input
from app.agents.providers.wire_finalize import finalize_chat_wire


@pytest.mark.parametrize("field,extra", [
    ("instructions", "根规则" * 300),
    ("system", [{"type": "text", "text": "来源" * 300}]),
    ("tools", [{"type": "function", "function": {"name": "read", "description": "工具" * 300}}]),
    ("response_format", {"type": "json_schema", "json_schema": {"description": "结构" * 300}}),
])
def test_wire_budget_includes_actual_root_sources_tools_and_output_schema(field, extra):
    wire = {"messages": [{"role": "user", "content": "你好"}]}
    before = estimate_wire_input(wire)
    result = estimate_wire_input({**wire, field: extra})
    assert result.input_tokens > before.input_tokens + 100
    assert result.quality == "estimated"


def test_array_content_does_not_hide_adjacent_tool_call_arguments():
    wire = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "读取"}]}]}
    before = estimate_wire_input(wire)
    wire["messages"][0]["tool_calls"] = [{"id": "call", "type": "function", "function": {
        "name": "read", "arguments": '{"path":"' + "目录" * 600 + '"}',
    }}]
    assert estimate_wire_input(wire).input_tokens > before.input_tokens + 250


@pytest.mark.parametrize("input_field,block", [
    ("messages", {"type": "image_url", "image_url": {"url": "https://invalid.test/image"}}),
    ("input", {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 10000}),
    ("messages", {"type": "input_audio", "input_audio": {"data": "A" * 10000, "format": "wav"}}),
    ("messages", {"type": "document", "source": {"data": "A" * 10000}}),
    ("input", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "可见推理"}],
               "encrypted_content": "A" * 10000}),
])
def test_media_and_encrypted_reasoning_are_unknown_without_loading_or_counting_encoding(input_field, block):
    item = block if block["type"] == "reasoning" else {"role": "user", "content": [block]}
    wire = {input_field: [item]}
    original = deepcopy(wire)
    estimate = estimate_wire_input(wire)
    assert estimate.quality == "unknown"
    assert estimate.input_tokens is None
    assert estimate.unknown_parts
    assert estimate.known_tokens < 100
    assert wire == original


def test_responses_native_calls_results_and_instructions_are_counted_without_chat_conversion():
    wire = {"instructions": "真实根规则", "input": [
        {"type": "function_call", "call_id": "c", "name": "read", "arguments": "参数" * 400},
        {"type": "function_call_output", "call_id": "c", "output": "结果" * 400},
    ]}
    assert estimate_wire_input(wire).input_tokens > 400
    assert estimate_wire_input({**wire, "max_output_tokens": 100000, "model": "a" * 100000}) == estimate_wire_input(wire)


def test_schema_and_tool_arguments_named_image_are_plain_business_data():
    estimate = estimate_wire_input({"tools": [{"type": "function", "function": {
        "name": "image", "parameters": {"type": "object", "properties": {"type": {"const": "image"}}},
    }}], "input": [{"type": "function_call", "arguments": '{"type":"image","data":"text"}'}]})
    assert estimate.quality == "estimated"


@pytest.mark.parametrize("field", ["response_format", "output_format", "output_config"])
def test_anthropic_final_wire_counts_native_output_schema(field):
    schema = {"type": "object", "properties": {"answer": {"type": "string", "description": "结构" * 600}},
              "required": ["answer"], "additionalProperties": False}
    native_format = {"type": "json_schema", "schema": schema}
    option = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}} if field == "response_format" else (
        {"format": native_format} if field == "output_config" else native_format
    )
    arguments = {"messages": [{"role": "user", "content": "你好"}], "tools": None, "custom_llm_provider": "anthropic"}
    before = finalize_chat_wire(**arguments, params={"model": "claude-sonnet-4-5"})
    wire = finalize_chat_wire(**arguments, params={"model": "claude-sonnet-4-5", field: option})
    assert "output_config" in wire if field == "output_config" else "output_format" in wire
    assert estimate_wire_input(wire).input_tokens > estimate_wire_input(before).input_tokens + 250


def test_anthropic_final_wire_counts_thinking_text_but_not_authentication_signature():
    def wire(text: str, signature: str):
        return finalize_chat_wire(messages=[{"role": "user", "content": "你好"},
            {"role": "assistant", "content": "回答", "thinking_blocks": [{"type": "thinking", "thinking": text, "signature": signature}]}],
            tools=None, params={"model": "claude-sonnet-4-5"}, custom_llm_provider="anthropic")

    short = estimate_wire_input(wire("推理", "A"))
    assert short == estimate_wire_input(wire("推理", "A" * 10000))
    assert estimate_wire_input(wire("推理" * 600, "A")).input_tokens > short.input_tokens + 250


@pytest.mark.parametrize("source", [
    {"type": "text", "media_type": "text/plain", "data": "文档" * 600},
    {"type": "content", "content": [{"type": "text", "text": "文档" * 600}]},
])
def test_anthropic_plain_document_source_is_known_text(source):
    wire = finalize_chat_wire(messages=[{"role": "user", "content": [{"type": "document", "source": source}]}],
        tools=None, params={"model": "claude-sonnet-4-5"}, custom_llm_provider="anthropic")
    assert wire["messages"][0]["content"][0]["source"] == source
    result = estimate_wire_input(wire)
    assert result.quality == "estimated"
    assert result.input_tokens > 250


def test_anthropic_document_keeps_visible_text_and_marks_only_opaque_source_unknown():
    block = {"type": "document", "title": "标题", "context": "已知说明", "source": {"type": "base64", "media_type": "application/pdf", "data": "A"}}
    wire = {"messages": [{"role": "user", "content": [block]}]}
    original = deepcopy(wire)
    before = estimate_wire_input(wire)
    block["source"]["data"] = "A" * 10000
    assert estimate_wire_input(wire) == before
    block["context"] = "已知说明" * 600
    assert estimate_wire_input(wire).known_tokens > before.known_tokens + 500
    assert before.unknown_parts == ("document",)
    assert original["messages"][0]["content"][0]["source"]["data"] == "A"


def test_anthropic_content_document_retains_text_next_to_unknown_image():
    source = {"type": "content", "content": [{"type": "text", "text": "文档" * 600},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 10000}}]}
    result = estimate_wire_input({"messages": [{"role": "user", "content": [{"type": "document", "source": source}]}]})
    assert result.unknown_parts == ("image",)
    assert 250 < result.known_tokens < 1000


def test_anthropic_tool_input_and_nested_schemas_are_not_protocol_media_blocks():
    business = {"type": "image", "content": {"type": "document", "data": "业务值" * 600}}
    wire = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "c", "name": "draw", "input": business}]}],
        "tools": [{"name": "draw", "input_schema": {"type": "object", "properties": {"content": business}}}]}
    original = deepcopy(wire)
    estimate = estimate_wire_input(wire)
    assert estimate.quality == "estimated" and estimate.input_tokens > 500
    assert wire == original


def test_responses_encrypted_reasoning_keeps_business_extensions_and_visible_summary():
    item = {"type": "reasoning", "encrypted_content": "A", "summary": [{"type": "summary_text", "text": "可见推理"}],
        "extension": {"type": "image", "data": "业务值" * 600}}
    wire = {"input": [item]}
    before = estimate_wire_input(wire)
    item["encrypted_content"] = "A" * 10000
    assert estimate_wire_input(wire) == before
    item["summary"][0]["text"] = "可见推理" * 600
    assert estimate_wire_input(wire).known_tokens > before.known_tokens + 500
    assert before.unknown_parts == ("encrypted_reasoning",)
