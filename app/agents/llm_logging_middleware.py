from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from pydantic import JsonValue, TypeAdapter

from app.agents.upstream_request_trace import (
    begin_upstream_capture,
    end_upstream_capture,
    record_upstream_error,
)
from app.core.bounded_json import bound_json_value
from app.core.execution_admission_context import require_current_execution_admission
from app.core.failures import execution_failure_message
from app.core.path_utils import get_session_path_resolver, get_sessions_dir
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.domain.itemized.execution_admission import ExecutionAdmission

_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
LLM_LOG_MAX_PAYLOAD_BYTES = 256 * 1024
LLM_LOG_MAX_FILES = 128
LLM_LOG_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_LOG_WRITE_LOCK = Lock()


def _json_value(value: object) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_python(value)


def _json_object(value: object, *, label: str) -> dict[str, JsonValue]:
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise TypeError(
            f"{label} 序列化结果必须为 JSON object，"
            f"实际类型为 {type(normalized).__name__}"
        )
    return normalized


def _serialize_message(message: BaseMessage) -> dict[str, JsonValue]:
    # 消息模型已完成 JSON 转换，只补日志统一字段，不再套模型校验后重新展开。
    payload = message.model_dump(mode="json")
    return {
        "type": payload["type"],
        "content": payload.get("content", ""),
        "additional_kwargs": payload.get("additional_kwargs", {}),
        "response_metadata": payload.get("response_metadata", {}),
        "tool_calls": payload.get("tool_calls", []),
        "tool_call_id": payload.get("tool_call_id"),
        **payload,
    }


def _serialize_tool(tool: BaseTool | dict[str, Any]) -> dict[str, JsonValue]:
    if isinstance(tool, BaseTool):
        payload: object = {
            "type": "tool",
            "name": tool.name,
            "description": tool.description,
            "args": tool.args,
        }
    elif isinstance(tool, Mapping):
        payload = dict(tool)
    else:
        raise TypeError(f"不支持的模型工具定义类型: {type(tool).__name__}")
    return _json_object(payload, label="模型工具定义")


def _response_messages(
    response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any],
) -> list[BaseMessage]:
    if isinstance(response, AIMessage):
        return [response]
    if isinstance(response, ModelResponse):
        return list(response.result)
    if isinstance(response, ExtendedModelResponse):
        model_response = response.model_response
        if not isinstance(model_response, ModelResponse):
            raise TypeError(
                "ExtendedModelResponse.model_response 类型无效: "
                f"{type(model_response).__name__}"
            )
        return list(model_response.result)
    raise TypeError(f"不支持的模型响应类型: {type(response).__name__}")


class LLMLoggingMiddleware(AgentMiddleware[StateT, Any, Any]):
    """把每次模型调用的完整请求和响应写入当前工作区。"""

    def __init__(self, *, sessions_dir: Path | None = None) -> None:
        self._sessions_dir = sessions_dir
        self._path_resolver = get_session_path_resolver(
            self._sessions_dir or get_sessions_dir()
        )

    @staticmethod
    def _execution_admission(runtime: Runtime[Any]) -> ExecutionAdmission:
        execution_info = getattr(runtime, "execution_info", None)
        thread_id = getattr(execution_info, "thread_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("模型调用 runtime 缺少真实 thread_id，无法保存 LLM 日志")
        return require_current_execution_admission(langgraph_thread_id=thread_id)

    def _session_log_dir(self, session_id: str) -> Path:
        logs_dir = (
            self._path_resolver.resolve_session_node(session_id)
            / "logs"
            / "llm_requests"
        )
        logs_dir.mkdir(exist_ok=True, parents=True)
        return logs_dir

    def _save_log(
        self,
        session_id: str,
        request: ModelRequest[Any],
        response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any] | None,
        upstream_attempts: list[dict[str, Any]],
        error: BaseException | None = None,
    ) -> None:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法关联 LLM 日志")

        # handler 结束或失败后组装日志的 epoch 毫秒时刻。
        logged_at_ms = int(time.time() * 1000)
        serialized_tools = (
            [_serialize_tool(tool) for tool in request.tools]
            if request.tools
            else []
        )
        job_id = self._execution_admission(request.runtime).job_id
        request_log = {
            "timestamp": logged_at_ms,
            "session_id": session_id,
            "job_id": job_id,
            "model_name": (
                getattr(request.model, "model_name", None)
                or getattr(request.model, "model", None)
            ),
            "messages": [_serialize_message(message) for message in request.messages],
            "tools": serialized_tools or None,
            "system_message": (
                _serialize_message(request.system_message)
                if request.system_message is not None
                else None
            ),
        }
        response_messages = _response_messages(response) if response is not None else []
        response_log = {
            "result": [_serialize_message(message) for message in response_messages],
            "structured_response": _json_value(
                getattr(response, "structured_response", None) if response is not None else None
            ),
            "error": execution_failure_message(error) if error is not None else None,
        }

        log_filename_timestamp_ns = time.time_ns()
        log_file = (
            self._session_log_dir(session_id)
            / f"{log_filename_timestamp_ns}.json"
        )
        bounded_payload = {
            "timestamp": logged_at_ms,
            "session_id": session_id,
            "job_id": job_id,
            "request": bound_json_value(
                request_log, max_bytes=LLM_LOG_MAX_PAYLOAD_BYTES
            ),
            "response": bound_json_value(
                response_log, max_bytes=LLM_LOG_MAX_PAYLOAD_BYTES
            ),
            "upstream": bound_json_value(
                {"attempts": _json_value(upstream_attempts)}, max_bytes=LLM_LOG_MAX_PAYLOAD_BYTES
            ),
        }
        serialized_log = json.dumps(bounded_payload, ensure_ascii=False, separators=(",", ":"))
        # 序列化可并发，写入和轮转共用锁，避免另一 writer 删掉正在统计的文件。
        with _LOG_WRITE_LOCK:
            log_file.write_text(serialized_log, encoding="utf-8")
            self._prune_session_logs(log_file.parent)

    @staticmethod
    def _prune_session_logs(log_dir: Path) -> None:
        log_files = sorted(
            ((path, path.stat().st_size) for path in log_dir.glob("*.json")),
            key=lambda entry: entry[0].name,
        )
        total_bytes = sum(size for _, size in log_files)
        remaining_file_count = len(log_files)
        for stale_file, stale_size in log_files:
            if (
                remaining_file_count <= LLM_LOG_MAX_FILES
                and total_bytes <= LLM_LOG_MAX_TOTAL_BYTES
            ):
                break
            try:
                stale_file.unlink()
            except OSError:
                # 诊断日志清理失败不覆盖模型调用的真实结果，但必须留痕。
                import logging

                logging.getLogger(__name__).warning(
                    "LLM 日志轮转失败: path=%s",
                    stale_file,
                    exc_info=True,
                )
            else:
                remaining_file_count -= 1
                total_bytes -= stale_size

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法确定日志会话")
        session_id = self._execution_admission(request.runtime).session_id
        capture_token = begin_upstream_capture()
        try:
            response = handler(request)
            upstream_attempts = end_upstream_capture(capture_token)
        except BaseException as error:
            record_upstream_error(error)
            upstream_attempts = end_upstream_capture(capture_token)
            self._save_log(session_id, request, None, upstream_attempts, error)
            raise
        else:
            self._save_log(session_id, request, response, upstream_attempts)
            return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法确定日志会话")
        session_id = self._execution_admission(request.runtime).session_id
        capture_token = begin_upstream_capture()
        try:
            response = await handler(request)
            upstream_attempts = end_upstream_capture(capture_token)
        except BaseException as error:
            record_upstream_error(error)
            upstream_attempts = end_upstream_capture(capture_token)
            # 原 handler 已失败，后续取消不能打断日志结算或替换它的异常。
            await run_cleanup_shielded(
                partial(run_sync_shielded, self._save_log, session_id, request, None, upstream_attempts, error)
            )
            raise
        else:
            await run_sync_shielded(self._save_log, session_id, request, response, upstream_attempts)
            return response
