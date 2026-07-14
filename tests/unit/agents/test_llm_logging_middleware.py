from __future__ import annotations

import json
from pathlib import Path

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import ExecutionInfo, Runtime

from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.request_replay_middleware import (
    PromptReplayCaptureMiddleware,
    read_prompt_replay_components,
)


def test_llm_log_persists_prompt_replay_components_and_tool_stats(
    tmp_path: Path,
) -> None:
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            thread_id="ses_replay",
        )
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="hello")],
        system_message=SystemMessage(content="默认指令"),
        tools=[
            {
                "type": "tool",
                "name": "read_file",
                "description": "读取文件",
                "args": {"path": {"type": "string"}},
            }
        ],
        runtime=runtime,
    )
    initial_state = dict(request.state)
    default_capture = PromptReplayCaptureMiddleware(
        source="agent_factory",
        label="默认指令",
    )
    agents_capture = PromptReplayCaptureMiddleware(
        source="WorkspaceAgentsMiddleware",
        label="工作区 AGENTS.md",
    )
    middleware = LLMLoggingMiddleware(logs_dir=tmp_path)

    def after_default_capture(next_request: ModelRequest) -> ModelResponse:
        updated_request = next_request.override(
            system_message=SystemMessage(
                content_blocks=[
                    *next_request.system_message.content_blocks,
                    {"type": "text", "text": "AGENTS.md 内容"},
                ]
            )
        )
        return agents_capture.wrap_model_call(
            updated_request,
            lambda final_request: middleware.wrap_model_call(
                final_request,
                lambda _: ModelResponse(result=[AIMessage(content="done")]),
            ),
        )

    default_capture.wrap_model_call(request, after_default_capture)

    log_file = next((tmp_path / "llm_requests" / "ses_replay").glob("*.json"))
    payload = json.loads(log_file.read_text(encoding="utf-8"))
    replay = payload["request"]["replay"]
    assert [item["label"] for item in replay["prompt_components"]] == [
        "默认指令",
        "工作区 AGENTS.md",
    ]
    assert [item["operation"] for item in replay["prompt_components"]] == [
        "append",
        "append",
    ]
    assert replay["message_count"] == 1
    assert replay["tools"]["count"] == 1
    assert replay["tools"]["names"] == ["read_file"]
    assert replay["system_prompt_char_count"] > 0
    assert request.state == initial_state, "请求审计元信息不得写入 Agent 上下文状态"


def test_prompt_replay_records_non_append_system_prompt_replacement() -> None:
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="before"),
    )
    initial_state = dict(request.state)
    default_capture = PromptReplayCaptureMiddleware(
        source="agent_factory",
        label="默认指令",
    )
    memory_capture = PromptReplayCaptureMiddleware(
        source="MemoryMiddleware",
        label="Agent 记忆",
    )
    components: list[dict[str, object]] = []

    def after_default_capture(next_request: ModelRequest) -> ModelResponse:
        replaced_request = next_request.override(
            system_message=SystemMessage(content="after")
        )

        def observe_replay(_: ModelRequest) -> ModelResponse:
            components.extend(read_prompt_replay_components())
            return ModelResponse(result=[AIMessage(content="done")])

        return memory_capture.wrap_model_call(replaced_request, observe_replay)

    default_capture.wrap_model_call(request, after_default_capture)
    assert components[-1]["operation"] == "replace"
    assert components[-1]["label"] == "Agent 记忆"
    assert request.state == initial_state, "替换 Prompt 的审计信息也不得污染 Agent 上下文"
