from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import ExecutionInfo, Runtime

from app.agents import llm_logging_middleware
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.upstream_request_trace import UpstreamRequestTraceCallback


def test_llm_log_persists_request_and_tool_stats_without_prompt_replay(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_e6d2707870e54cab8c135193c0802532"
    session_dir = session_bundle_factory(tmp_path, session_id)
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            thread_id=session_id,
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
    middleware = LLMLoggingMiddleware(sessions_dir=tmp_path)

    middleware.wrap_model_call(
        request,
        lambda _: ModelResponse(result=[AIMessage(content="done")]),
    )

    log_file = next((session_dir / "logs" / "llm_requests").glob("*.json"))
    payload = json.loads(log_file.read_text(encoding="utf-8"))
    request_payload = payload["request"]
    assert "replay" not in request_payload
    assert request_payload["messages"][0]["content"] == "hello"
    assert request_payload["system_message"]["content"] == "默认指令"
    assert request_payload["tools"][0]["name"] == "read_file"
    assert request_payload["tools"][0]["args"]["path"]["type"] == "string"
    assert request.state == initial_state, "请求审计元信息不得写入 Agent 上下文状态"


def test_llm_logging_does_not_capture_prompt_replacement_side_channel(
    tmp_path: Path,
) -> None:
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="before"),
    )
    # R17：不再硬编码全局 /tmp（catalog 模式工厂会在其父目录建导航目录，
    # 触发权限错误，也违反测试工作区隔离）；改用测试专属临时目录。
    middleware = LLMLoggingMiddleware(sessions_dir=tmp_path)

    replaced_request = request.override(
        system_message=SystemMessage(content="after")
    )
    assert replaced_request.system_message.text == "after"
    assert not hasattr(middleware, "_build_request_replay")


def test_llm_log_merges_redacted_upstream_request_and_response(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_a03b3d8d67eb46e483f7e0c5fc296fae"
    session_dir = session_bundle_factory(tmp_path, session_id)
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            thread_id=session_id,
        )
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="hello")],
        runtime=runtime,
    )
    middleware = LLMLoggingMiddleware(sessions_dir=tmp_path)

    def invoke(_: ModelRequest) -> ModelResponse:
        callback = UpstreamRequestTraceCallback()
        callback.log_pre_api_call(
            "big-pickle",
            [{"role": "user", "content": "hello"}],
            {
                "litellm_call_id": "call-1",
                "call_type": "acompletion",
                "custom_llm_provider": "openai",
                "api_key": "secret",
                "additional_args": {
                    "api_base": "https://example.com/v1",
                    "headers": {"Authorization": "Bearer secret"},
                    "complete_input_dict": {
                        "model": "big-pickle",
                        "messages": [{"role": "user", "content": "hello"}],
                        "api_key": "secret",
                    },
                },
            },
        )
        callback.log_success_event(
            {"litellm_call_id": "call-1"},
            {"choices": [{"message": {"content": "done"}}]},
            None,
            None,
        )
        return ModelResponse(result=[AIMessage(content="done")])

    middleware.wrap_model_call(request, invoke)

    log_file = next((session_dir / "logs" / "llm_requests").glob("*.json"))
    payload = json.loads(log_file.read_text(encoding="utf-8"))
    attempts = payload["upstream"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["request"]["messages"][0]["role"] == "user"
    assert attempts[0]["request"]["api_key"] == "[REDACTED]"
    assert attempts[0]["response"]["choices"][0]["message"]["content"] == "done"


def test_llm_log_bounds_large_payload_and_keeps_valid_json(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_63b1e65e253d4e348ef2255613c8c915"
    session_dir = session_bundle_factory(tmp_path, session_id)
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            thread_id=session_id,
        )
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="payload-" + "x" * (1024 * 1024))],
        runtime=runtime,
    )
    middleware = LLMLoggingMiddleware(sessions_dir=tmp_path)

    middleware._save_log(
        session_id,
        request,
        ModelResponse(result=[AIMessage(content="done")]),
        [{"request": "y" * (1024 * 1024)}],
    )

    log_file = next((session_dir / "logs" / "llm_requests").glob("*.json"))
    assert log_file.stat().st_size <= 3 * 256 * 1024 + 1024
    json.loads(log_file.read_text(encoding="utf-8"))
    assert "BoxTeam 已截断" in log_file.read_text(encoding="utf-8")


def test_llm_log_pruning_enforces_file_count_and_total_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs" / "llm_requests"
    log_dir.mkdir(parents=True)
    monkeypatch.setattr(llm_logging_middleware, "LLM_LOG_MAX_FILES", 3)
    monkeypatch.setattr(llm_logging_middleware, "LLM_LOG_MAX_TOTAL_BYTES", 10)
    for name, size in (("001.json", 6), ("002.json", 5), ("003.json", 4), ("004.json", 3)):
        (log_dir / name).write_bytes(b"x" * size)

    LLMLoggingMiddleware._prune_session_logs(log_dir)

    assert sorted(path.name for path in log_dir.glob("*.json")) == [
        "003.json",
        "004.json",
    ]
    assert sum(path.stat().st_size for path in log_dir.glob("*.json")) <= 10


def test_llm_log_persists_failed_upstream_attempt(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_3785202dfada418b8c2fe1925714d0d7"
    session_dir = session_bundle_factory(tmp_path, session_id)
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task",
            thread_id=session_id,
        )
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="hello")],
        runtime=runtime,
    )
    middleware = LLMLoggingMiddleware(sessions_dir=tmp_path)

    def invoke(_: ModelRequest) -> ModelResponse:
        callback = UpstreamRequestTraceCallback()
        callback.log_pre_api_call(
            "failed-model",
            [{"role": "user", "content": "hello"}],
            {
                "litellm_call_id": "failed-call",
                "call_type": "acompletion",
                "custom_llm_provider": "openai",
                "additional_args": {
                    "complete_input_dict": {
                        "model": "failed-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                },
            },
        )
        callback.log_failure_event(
            {"litellm_call_id": "failed-call"},
            RuntimeError("upstream unavailable"),
            None,
            None,
        )
        raise RuntimeError("model call failed")

    with pytest.raises(RuntimeError, match="model call failed"):
        middleware.wrap_model_call(request, invoke)

    log_file = next((session_dir / "logs" / "llm_requests").glob("*.json"))
    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert payload["response"]["error"] == "model call failed"
    assert "upstream unavailable" in payload["upstream"]["attempts"][0]["error"]
