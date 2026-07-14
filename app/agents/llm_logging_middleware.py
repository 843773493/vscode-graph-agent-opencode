from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, TypeAdapter

from app.agents.request_replay_middleware import read_prompt_replay_components
from app.core.job_context import get_current_job_id
from app.core.path_utils import get_logs_dir


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class _MessageLog(BaseModel):
    """一条已转换为 JSON 值的 LangChain 消息。"""

    model_config = ConfigDict(extra="allow")

    type: str
    content: JsonValue = ""
    additional_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    response_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    tool_calls: list[dict[str, JsonValue]] = Field(default_factory=list)
    tool_call_id: str | None = None


class _ToolLog(RootModel[dict[str, JsonValue]]):
    """一项保持原有字段结构的 JSON 工具定义。"""


class _PromptReplayComponentLog(BaseModel):
    source: str
    label: str
    operation: str
    order: int
    content_blocks: list[dict[str, JsonValue]]
    block_count: int
    char_count: int


class _ToolReplayLog(BaseModel):
    source: str = "ModelRequest.tools"
    count: int
    names: list[str]
    schema_char_count: int


class _LLMRequestReplayLog(BaseModel):
    schema_version: int = 1
    prompt_components: list[_PromptReplayComponentLog]
    tools: _ToolReplayLog
    message_count: int
    system_prompt_char_count: int


class _LLMRequestLog(BaseModel):
    timestamp: int
    session_id: str
    job_id: str | None = None
    model_name: str | None = None
    messages: list[_MessageLog]
    tools: list[_ToolLog] | None = None
    system_message: _MessageLog | None = None
    replay: _LLMRequestReplayLog


class _LLMResponseLog(BaseModel):
    result: list[_MessageLog]
    structured_response: JsonValue = None


class _LLMFullLog(BaseModel):
    timestamp: int
    session_id: str
    job_id: str | None = None
    request: _LLMRequestLog
    response: _LLMResponseLog


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


def _serialize_message(message: BaseMessage) -> _MessageLog:
    payload = message.model_dump(mode="json")
    return _MessageLog.model_validate(payload)


def _serialize_tool(tool: BaseTool | dict[str, Any]) -> _ToolLog:
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
    return _ToolLog(root=_json_object(payload, label="模型工具定义"))


def _json_char_count(value: object) -> int:
    return len(
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _build_request_replay(
    request: ModelRequest[Any],
    tools: list[_ToolLog],
) -> _LLMRequestReplayLog:
    component_logs: list[_PromptReplayComponentLog] = []
    for order, component in enumerate(
        read_prompt_replay_components(),
        start=1,
    ):
        content_blocks = [
            _json_object(block, label="Prompt replay content block")
            for block in component["content_blocks"]
        ]
        component_logs.append(
            _PromptReplayComponentLog(
                source=component["source"],
                label=component["label"],
                operation=component["operation"],
                order=order,
                content_blocks=content_blocks,
                block_count=len(content_blocks),
                char_count=_json_char_count(content_blocks),
            )
        )

    tool_payloads = [tool.root for tool in tools]
    tool_names = [
        name
        for tool in tool_payloads
        if isinstance((name := tool.get("name")), str) and name
    ]
    system_prompt_char_count = _json_char_count(
        request.system_message.content_blocks
        if request.system_message is not None
        else []
    )
    return _LLMRequestReplayLog(
        prompt_components=component_logs,
        tools=_ToolReplayLog(
            count=len(tools),
            names=tool_names,
            schema_char_count=_json_char_count(tool_payloads),
        ),
        message_count=len(request.messages),
        system_prompt_char_count=system_prompt_char_count,
    )


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

    def __init__(self, *, logs_dir: Path | None = None) -> None:
        self._logs_dir = logs_dir
        self._prepared_session_dirs: set[str] = set()

    def _get_session_id(self, runtime: Runtime[Any]) -> str:
        configurable = getattr(runtime, "configurable", None)
        if isinstance(configurable, dict):
            session_id = configurable.get("session_id")
            if isinstance(session_id, str) and session_id:
                return session_id

        execution_info = getattr(runtime, "execution_info", None)
        if execution_info is not None:
            thread_id = getattr(execution_info, "thread_id", None)
            if isinstance(thread_id, str) and thread_id:
                return thread_id

        raise RuntimeError("模型调用 runtime 缺少 session_id/thread_id，无法保存 LLM 日志")

    def _get_job_id(self, runtime: Runtime[Any]) -> str | None:
        context_job_id = get_current_job_id()
        if context_job_id:
            return context_job_id

        configurable = getattr(runtime, "configurable", None)
        if isinstance(configurable, dict):
            job_id = configurable.get("job_id")
            if isinstance(job_id, str) and job_id:
                return job_id

        execution_info = getattr(runtime, "execution_info", None)
        if execution_info is not None:
            configurable = getattr(execution_info, "configurable", None)
            if isinstance(configurable, dict):
                job_id = configurable.get("job_id")
                if isinstance(job_id, str) and job_id:
                    return job_id
            job_id = getattr(execution_info, "job_id", None)
            if isinstance(job_id, str) and job_id:
                return job_id

        return None

    def _ensure_session_dir(self, session_id: str) -> Path:
        logs_dir = (self._logs_dir or get_logs_dir()) / "llm_requests" / session_id
        if session_id not in self._prepared_session_dirs:
            logs_dir.mkdir(exist_ok=True, parents=True)
            self._prepared_session_dirs.add(session_id)
        return logs_dir

    def _save_log(
        self,
        session_id: str,
        request: ModelRequest[Any],
        response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any],
    ) -> None:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法关联 LLM 日志")

        timestamp = int(time.time() * 1000)
        serialized_tools = (
            [_serialize_tool(tool) for tool in request.tools]
            if request.tools
            else []
        )
        request_log = _LLMRequestLog(
            timestamp=timestamp,
            session_id=session_id,
            job_id=self._get_job_id(request.runtime),
            model_name=(
                getattr(request.model, "model_name", None)
                or getattr(request.model, "model", None)
            ),
            messages=[_serialize_message(message) for message in request.messages],
            tools=serialized_tools or None,
            system_message=(
                _serialize_message(request.system_message)
                if request.system_message is not None
                else None
            ),
            replay=_build_request_replay(request, serialized_tools),
        )
        response_log = _LLMResponseLog(
            result=[
                _serialize_message(message)
                for message in _response_messages(response)
            ],
            structured_response=_json_value(
                getattr(response, "structured_response", None)
            ),
        )
        full_log = _LLMFullLog(
            timestamp=timestamp,
            session_id=session_id,
            job_id=request_log.job_id,
            request=request_log,
            response=response_log,
        )

        log_file = self._ensure_session_dir(session_id) / f"{time.time_ns()}.json"
        log_file.write_text(full_log.model_dump_json(indent=2), encoding="utf-8")

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法确定日志会话")
        session_id = self._get_session_id(request.runtime)
        response = handler(request)
        self._save_log(session_id, request, response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        if request.runtime is None:
            raise RuntimeError("模型请求缺少 runtime，无法确定日志会话")
        session_id = self._get_session_id(request.runtime)
        response = await handler(request)
        self._save_log(session_id, request, response)
        return response
