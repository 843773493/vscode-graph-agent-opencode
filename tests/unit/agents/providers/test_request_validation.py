"""R02 的最终系统范围合同不解释普通用户文本或其它路径语义。"""

from __future__ import annotations

import pytest

from app.agents.providers.wire_finalize import (
    finalize_chat_wire,
    finalize_responses_wire,
)
from app.domain.itemized.epoch.source_protocol import (
    SourceModelBlock,
    render_source_blocks,
)


@pytest.mark.parametrize("root", ["/home/example/project", r"C:\Users\example\project", "../other", ""])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "instructions"])
def test_final_system_scope_rejects_historical_or_nonlocal_r02_scope(root, protocol):
    content = (
        "## 当前运行时身份与路径边界（系统权威元数据）\n"
        f"- workspace 根目录（exec_command 未提供 workdir 时的 cwd）：`{root}`\n"
    )
    with pytest.raises(ValueError, match="real-path-not-allowed: R02"):
        if protocol == "chat_completions":
            finalize_chat_wire(messages=[{"role": "system", "content": content}], tools=None, params={})
        else:
            finalize_responses_wire(
                input_items=[{"role": "developer", "content": [{"type": "input_text", "text": content}]}],
                tools=None, params={}, include=(), store=False,
                merge_system_to_instructions=protocol == "instructions",
            )


def test_user_text_and_http_routes_keep_their_original_bytes():
    user = "- workspace 根目录（exec_command 未提供 workdir 时的 cwd）：`/user/example`"
    system = "访问 /api/v1/workspace；这是 HTTP route，并非 R02 的工作区配置。"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    assert finalize_chat_wire(messages=messages, tools=None, params={})["messages"] == messages
    inputs = [{"role": message["role"], "content": [{"type": "input_text", "text": message["content"]}]} for message in messages]
    assert finalize_responses_wire(input_items=inputs, tools=None, params={}, include=(), store=False)["input"] == inputs


@pytest.mark.parametrize("root", [".", "/home/example/project", r"C:\Users\example\project"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "instructions"])
def test_source_envelope_preserves_r02_scope_validation(root, protocol):
    body = (
        "## 当前运行时身份与路径边界（系统权威元数据）\n"
        f"- workspace 根目录（exec_command 未提供 workdir 时的 cwd）：`{root}`\n"
    )
    content = render_source_blocks((SourceModelBlock(
        source_identity="instruction:runtime-identity", name="运行时身份与路径",
        source_kind="runtime_identity", operation="activate", revision="r1",
        base_revision=None, representation="full", source_tracking_at_seal="tracked",
        root_placement="root_eligible", body=body,
    ),))

    def finalize():
        if protocol == "chat_completions":
            return finalize_chat_wire(messages=[{"role": "system", "content": content}], tools=None, params={})
        return finalize_responses_wire(
            input_items=[{"role": "developer", "content": [{"type": "input_text", "text": content}]}],
            tools=None, params={}, include=(), store=False,
            merge_system_to_instructions=protocol == "instructions",
        )

    if root == ".":
        wire = finalize()
        actual = wire["messages"][0]["content"] if protocol == "chat_completions" else (
            wire["instructions"] if protocol == "instructions" else wire["input"][0]["content"][0]["text"]
        )
        assert actual == content
    else:
        with pytest.raises(ValueError, match="real-path-not-allowed: R02"):
            finalize()
