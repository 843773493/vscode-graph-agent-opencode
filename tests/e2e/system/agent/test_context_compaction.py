"""会话上下文压缩端到端测试。"""
from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import httpx
import pytest
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.agent_factory import build_model_from_provider
from app.agents.cache_preserving_summarization import (
    CachePreservingSummarizationMiddleware,
)
from app.agents.upstream_request_trace import (
    begin_upstream_capture,
    end_upstream_capture,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.config_service import ConfigService
from tests.support.api_waiters import wait_for_job_done


def _seed_checkpoint_messages(
    *,
    workspace_root: str,
    session_id: str,
    pair_count: int = 5,
) -> None:
    saver = FileSystemCheckpointSaver(
        sessions_dir=Path(workspace_root) / ".boxteam" / "sessions"
    )
    messages = []
    for index in range(pair_count):
        messages.append(
            HumanMessage(
                content=(
                    f"第 {index} 轮用户消息：请记住 compact-e2e-{index}。"
                    + "这一轮包含需要压缩的详细背景资料。" * 100
                ),
            )
        )
        messages.append(
            AIMessage(
                content=(
                    f"第 {index} 轮助手回复：已记录 compact-e2e-{index}。"
                    + "这是对应背景资料的详细处理记录。" * 100
                ),
            )
        )

    messages_version = saver.get_next_version(None, None)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = str(uuid.uuid4())
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": messages_version}
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        metadata={"source": "e2e_seed", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )


def _append_checkpoint_messages(
    *,
    workspace_root: str,
    session_id: str,
    pair_count: int,
) -> None:
    """保留中间件私有状态，仅向现有会话追加用于压缩的历史消息。"""
    saver = FileSystemCheckpointSaver(
        sessions_dir=Path(workspace_root) / ".boxteam" / "sessions"
    )
    checkpoint_tuple = saver.get_tuple(build_checkpoint_config(session_id))
    assert checkpoint_tuple is not None

    checkpoint = checkpoint_tuple.checkpoint.copy()
    channel_values = dict(checkpoint.get("channel_values", {}))
    messages = list(channel_values.get("messages", []))
    for index in range(pair_count):
        messages.append(
            HumanMessage(
                content=(
                    f"AGENTS 压缩测试用户消息 {index}。"
                    + "需要压缩的中段用户背景资料。" * 100
                )
            )
        )
        messages.append(
            AIMessage(
                content=(
                    f"AGENTS 压缩测试助手消息 {index}。"
                    + "需要压缩的中段助手处理记录。" * 100
                )
            )
        )
    channel_values["messages"] = messages
    checkpoint["channel_values"] = channel_values
    checkpoint["id"] = str(uuid.uuid4())

    channel_versions = dict(checkpoint.get("channel_versions", {}))
    messages_version = saver.get_next_version(
        channel_versions.get("messages"),
        None,
    )
    channel_versions["messages"] = messages_version
    checkpoint["channel_versions"] = channel_versions
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        checkpoint_tuple.config,
        checkpoint,
        metadata={"source": "e2e_append", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )


async def _send_message(
    client: httpx.AsyncClient,
    session_id: str,
    content: str,
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=90)
    assert job["status"] in {"completed", "succeeded"}
    return job_id


async def _job_request_log(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> dict:
    response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert response.status_code == 200
    matching_logs = [
        log for log in response.json()["data"] if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs[-1]["request"]


async def _job_llm_log(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> dict:
    response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert response.status_code == 200
    matching_logs = [
        log for log in response.json()["data"] if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs[-1]


async def _job_llm_logs(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> list[dict]:
    response = await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert response.status_code == 200
    matching_logs = [
        log for log in response.json()["data"] if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs


def _cache_read_tokens(log: dict) -> int:
    results = log.get("response", {}).get("result", [])
    assert results, "LLM 日志缺少响应消息"
    usage = results[-1].get("usage_metadata")
    assert isinstance(usage, dict), "LLM 响应缺少 usage_metadata"
    details = usage.get("input_token_details") or {}
    assert isinstance(details, dict), "input_token_details 类型无效"
    value = details.get("cache_read", 0)
    assert isinstance(value, int) and value >= 0
    return value


def _model_response_message(
    response: ModelResponse | ExtendedModelResponse,
) -> AIMessage:
    model_response = (
        response.model_response
        if isinstance(response, ExtendedModelResponse)
        else response
    )
    assert isinstance(model_response, ModelResponse)
    assert model_response.result
    message = model_response.result[-1]
    assert isinstance(message, AIMessage)
    return message


def _message_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError(f"模型日志消息 content 类型无效: {type(content).__name__}")
    texts: list[str] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)


@pytest.mark.asyncio
async def test_session_context_compact_writes_summarization_event(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Context Compact E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    tools_response = await client.get("/api/v1/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["data"]
    compact_tool = next(
        tool for tool in tools if tool["tool_id"] == "compact_conversation"
    )
    assert compact_tool["parameters"]["properties"] == {}

    _seed_checkpoint_messages(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        pair_count=5,
    )

    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
    assert compact_response.status_code == 200
    result = compact_response.json()["data"]

    assert result["status"] == "scheduled"
    assert result["strategy"] is None
    assert result["before_message_count"] == 10
    assert result["effective_message_count_before"] == 10
    assert result["effective_message_count_after"] == 10
    assert result["summarized_message_count"] == 0
    assert result["retained_message_count"] == 10
    assert result["summary"] is None
    assert result["history_file_path"] is None

    saver = FileSystemCheckpointSaver(
        sessions_dir=Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
    )
    scheduled_checkpoint = saver.get_tuple(build_checkpoint_config(session_id))
    assert scheduled_checkpoint is not None
    assert scheduled_checkpoint.checkpoint["channel_values"][
        "_force_cache_compaction"
    ] is True

    await _send_message(
        client,
        session_id,
        "执行已安排的压缩，只回复 COMPACTED_OK，不要调用工具。",
    )

    session_dir = get_session_path_resolver(
        Path(e2e_workspace_root_path) / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    history_file = (
        session_dir / "context" / "history.md"
    )
    assert history_file.exists()
    history_content = history_file.read_text(encoding="utf-8")
    assert "Summarized at" in history_content
    assert "compact-e2e-2" in history_content
    assert "compact-e2e-1" not in history_content

    checkpoint = saver.get_tuple(build_checkpoint_config(session_id))
    assert checkpoint is not None
    channel_values = checkpoint.checkpoint["channel_values"]
    compact_event = channel_values.get("_summarization_event")
    assert compact_event is not None
    assert compact_event["strategy"] == "cache_preserving"
    assert len(compact_event["cache_prefix_messages"]) >= 2
    assert compact_event["cutoff_index"] > len(compact_event["cache_prefix_messages"])
    assert compact_event["file_path"] == (
        f"session-artifacts/{session_id}/context/history.md"
    )
    assert compact_event["summary_message"].additional_kwargs.get("lc_source") == "summarization"
    assert channel_values["_force_cache_compaction"] is False


@pytest.mark.asyncio
async def test_workspace_agents_change_preserves_system_prompt_until_compaction(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    workspace_root = Path(e2e_workspace_root_path)
    agents_path = workspace_root / "AGENTS.md"
    initial_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-one。\n"
    changed_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-two。\n"
    agents_path.write_text(initial_content, encoding="utf-8")

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "AGENTS Prompt Cache E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_message(
        client,
        session_id,
        "请只回复 FIRST_OK，不要调用工具。",
    )
    first_request = await _job_request_log(client, session_id, first_job_id)
    first_system = first_request["system_message"]
    assert first_system is not None
    assert "agents-cache-version-one" in _message_content_text(first_system)

    agents_path.write_text(changed_content, encoding="utf-8")
    second_job_id = await _send_message(
        client,
        session_id,
        "请只回复 SECOND_OK，不要调用工具。",
    )
    second_request = await _job_request_log(client, session_id, second_job_id)
    assert second_request["system_message"] == first_system
    second_messages_text = "\n".join(
        _message_content_text(message) for message in second_request["messages"]
    )
    assert "<system_reminder>" in second_messages_text
    assert "workspace_agents_md_change" in second_messages_text
    assert "+始终遵循 agents-cache-version-two。" in second_messages_text

    _append_checkpoint_messages(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        pair_count=5,
    )
    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
    assert compact_response.status_code == 200
    assert compact_response.json()["data"]["status"] == "scheduled"

    third_job_id = await _send_message(
        client,
        session_id,
        "请只回复 THIRD_OK，不要调用工具。",
    )
    third_request = await _job_request_log(client, session_id, third_job_id)
    third_system = third_request["system_message"]
    assert third_system is not None
    third_system_text = _message_content_text(third_system)
    assert "agents-cache-version-one" in third_system_text

    fourth_job_id = await _send_message(
        client,
        session_id,
        "请只回复 FOURTH_OK，不要调用工具。",
    )
    fourth_request = await _job_request_log(client, session_id, fourth_job_id)
    fourth_system = fourth_request["system_message"]
    assert fourth_system is not None
    fourth_system_text = _message_content_text(fourth_system)
    assert "agents-cache-version-two" in fourth_system_text
    assert "agents-cache-version-one" not in fourth_system_text
    assert fourth_system != first_system


@pytest.mark.skipif(
    os.environ.get("OPENCODE_ZEN_API_KEY") is None,
    reason="需要 OPENCODE_ZEN_API_KEY 才能验证真实 Prompt Cache",
)
@pytest.mark.asyncio
async def test_cache_preserving_compaction_keeps_and_grows_upstream_cache_hit(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Cache Preserving Compact E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    stable_material = "\n".join(
        f"缓存稳定资料-{index:04d}：该行必须保持原样。" for index in range(260)
    )
    first_job_id = await _send_message(
        client,
        session_id,
        f"记住以下稳定资料，只回复 CACHE-FIRST，不要调用工具。\n{stable_material}",
    )
    first_log = await _job_llm_log(client, session_id, first_job_id)

    second_material = "，".join(f"第二轮-{index:04d}" for index in range(180))
    second_job_id = await _send_message(
        client,
        session_id,
        f"继续记住这些内容，只回复 CACHE-SECOND，不要调用工具：{second_material}",
    )
    second_log = await _job_llm_log(client, session_id, second_job_id)
    second_cached_tokens = _cache_read_tokens(second_log)
    second_upstream_request = second_log["upstream"]["attempts"][-1]["request"]

    _append_checkpoint_messages(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        pair_count=15,
    )
    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
    assert compact_response.status_code == 200
    compact_result = compact_response.json()["data"]
    assert compact_result["status"] == "scheduled"
    assert compact_result["strategy"] is None
    assert compact_result["effective_message_count_after"] == (
        compact_result["effective_message_count_before"]
    )
    assert compact_result["summarized_message_count"] == 0

    third_job_id = await _send_message(
        client,
        session_id,
        "压缩后继续对话，只回复 CACHE-THIRD，不要调用工具。",
    )
    third_logs = await _job_llm_logs(client, session_id, third_job_id)
    assert len(third_logs) == 2
    summary_log, third_log = third_logs
    summary_cached_tokens = _cache_read_tokens(summary_log)
    third_cached_tokens = _cache_read_tokens(third_log)
    third_upstream_request = third_log["upstream"]["attempts"][-1]["request"]

    fourth_job_id = await _send_message(
        client,
        session_id,
        "继续压缩后的对话，只回复 CACHE-FOURTH，不要调用工具。",
    )
    fourth_log = await _job_llm_log(client, session_id, fourth_job_id)
    fourth_cached_tokens = _cache_read_tokens(fourth_log)

    first_request_messages = first_log["request"]["messages"]
    second_request_messages = second_log["request"]["messages"]
    third_request_messages = third_log["request"]["messages"]
    assert third_request_messages[: len(second_request_messages)] == (
        second_request_messages
    )
    assert first_request_messages[0] == second_request_messages[0]
    upstream_prefix_size = len(second_upstream_request["messages"])
    assert third_upstream_request["messages"][:upstream_prefix_size] == (
        second_upstream_request["messages"]
    )
    third_extra_body = third_upstream_request.get("extra_body") or {}
    assert "prompt_cache_key" not in third_extra_body
    assert second_cached_tokens > 0
    assert summary_cached_tokens >= second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "summary_cached_tokens": summary_cached_tokens,
    }
    assert third_cached_tokens >= second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "first_post_compaction_cached_tokens": third_cached_tokens,
    }
    assert fourth_cached_tokens > second_cached_tokens, {
        "second_cached_tokens": second_cached_tokens,
        "first_post_compaction_cached_tokens": third_cached_tokens,
        "next_post_compaction_cached_tokens": fourth_cached_tokens,
    }


@pytest.mark.skipif(
    os.environ.get("OPENCODE_ZEN_API_KEY") is None,
    reason="需要 OPENCODE_ZEN_API_KEY 才能验证压缩摘要请求的 Prompt Cache",
)
@pytest.mark.asyncio
async def test_cache_preserving_middleware_forked_summary_hits_main_prompt_cache(
    e2e_workspace_root_path: str,
):
    config_service = ConfigService(
        config_path=Path.cwd() / "configs" / "tests" / "default.jsonc",
        workspace_root=e2e_workspace_root_path,
    )
    provider = config_service.get_llm_provider("primary")
    assert "prompt_cache_key" not in provider.get("capabilities", [])
    model = build_model_from_provider(provider, {})
    middleware = CachePreservingSummarizationMiddleware(
        model=model,
        backend=FilesystemBackend(
            root_dir=e2e_workspace_root_path,
            virtual_mode=True,
        ),
        trigger=("messages", 7),
        keep=("messages", 1),
        trim_tokens_to_summarize=None,
    )
    system_message = SystemMessage(
        content="\n".join(
            f"压缩摘要缓存固定系统资料-{index:04d}，所有请求必须保持原样。"
            for index in range(260)
        )
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件；压缩摘要期间不得调用。",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    calls: list[dict] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        capture_token = begin_upstream_capture()
        combined: AIMessageChunk | None = None
        try:
            input_messages = [request.system_message, *request.messages]
            bound_model = (
                request.model.bind_tools(
                    request.tools,
                    tool_choice=request.tool_choice,
                )
                if request.tools
                else request.model
            )
            async for chunk in bound_model.astream(input_messages):
                assert isinstance(chunk, AIMessageChunk)
                combined = chunk if combined is None else combined + chunk
            attempts = end_upstream_capture(capture_token)
        except BaseException:
            end_upstream_capture(capture_token)
            raise
        assert combined is not None
        message = message_chunk_to_message(combined)
        assert isinstance(message, AIMessage)
        usage = message.usage_metadata
        assert usage is not None
        details = usage.get("input_token_details") or {}
        calls.append(
            {
                "input_tokens": usage.get("input_tokens", 0),
                "cached_tokens": details.get("cache_read", 0),
                "upstream_request": attempts[-1]["request"],
            }
        )
        return ModelResponse(result=[message])

    first_user = HumanMessage(
        content="记住第一轮资料并只回复 FORK-FIRST。" + "甲乙丙丁" * 300
    )
    first_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=[first_user],
            system_message=system_message,
            tools=tools,
        ),
        handler,
    )
    first_ai = _model_response_message(first_response)

    second_user = HumanMessage(
        content="记住第二轮资料并只回复 FORK-SECOND。" + "戊己庚辛" * 300
    )
    second_messages = [first_user, first_ai, second_user]
    second_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=second_messages,
            system_message=system_message,
            tools=tools,
        ),
        handler,
    )
    second_ai = _model_response_message(second_response)

    third_messages = [
        *second_messages,
        second_ai,
        HumanMessage(content="需要压缩的中段问题一：" + "甲乙丙丁" * 300),
        AIMessage(content="需要压缩的中段回答一：" + "戊己庚辛" * 300),
        HumanMessage(content="需要压缩的中段问题二：" + "天地玄黄" * 300),
        AIMessage(content="需要压缩的中段回答二：" + "宇宙洪荒" * 300),
        HumanMessage(content="压缩后只回复 FORK-THIRD。"),
    ]
    third_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=third_messages,
            system_message=system_message,
            tools=tools,
        ),
        handler,
    )
    assert isinstance(third_response, ExtendedModelResponse)
    await handler(
        ModelRequest(
            model=model,
            messages=third_messages,
            system_message=system_message,
            tools=tools,
        )
    )
    assert len(calls) == 5

    second_call = calls[1]
    summary_call = calls[2]
    compacted_call = calls[3]
    uncompacted_call = calls[4]
    second_upstream_messages = second_call["upstream_request"]["messages"]
    uncompacted_upstream_messages = uncompacted_call["upstream_request"]["messages"]
    summary_upstream_messages = summary_call["upstream_request"]["messages"]
    compacted_upstream_messages = compacted_call["upstream_request"]["messages"]
    print(
        "\n[forked-summary-cache] "
        f"second={second_call['cached_tokens']}, "
        f"summary={summary_call['cached_tokens']}, "
        f"compacted={compacted_call['cached_tokens']}; "
        f"uncompacted_input={uncompacted_call['input_tokens']}, "
        f"compacted_input={compacted_call['input_tokens']}"
    )
    assert summary_upstream_messages[: len(second_upstream_messages)] == (
        second_upstream_messages
    )
    assert len(compacted_upstream_messages) < len(uncompacted_upstream_messages)
    assert compacted_call["input_tokens"] < uncompacted_call["input_tokens"], {
        "uncompacted_input_tokens": uncompacted_call["input_tokens"],
        "compacted_input_tokens": compacted_call["input_tokens"],
    }
    assert summary_call["cached_tokens"] >= second_call["cached_tokens"] > 0
    assert compacted_call["cached_tokens"] > second_call["cached_tokens"]
    for call in calls:
        extra_body = call["upstream_request"].get("extra_body") or {}
        assert "prompt_cache_key" not in extra_body


def _luna_test_image_data_url() -> str:
    image_path = (
        Path.cwd()
        / "asset"
        / "default_test_workspace"
        / "assets"
        / "test.jpg"
    )
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _nested_mapping_values(value: object, key: str) -> list[object]:
    values: list[object] = []
    if isinstance(value, dict):
        if key in value:
            values.append(value[key])
        for nested in value.values():
            values.extend(_nested_mapping_values(nested, key))
    elif isinstance(value, list):
        for nested in value:
            values.extend(_nested_mapping_values(nested, key))
    return values


def _encrypted_reasoning_items(message: AIMessage) -> list[dict]:
    return [
        block["extras"]["response_item"]
        for block in message.content
        if isinstance(block, dict)
        and block.get("type") == "reasoning"
        and isinstance(block.get("extras"), dict)
        and isinstance(block["extras"].get("response_item"), dict)
        and block["extras"]["response_item"].get("encrypted_content")
    ]


@pytest.mark.skipif(
    os.environ.get("CCTQ_API_KEY") is None,
    reason="需要 CCTQ_API_KEY 才能验证 Luna 图片压缩 Prompt Cache",
)
@pytest.mark.asyncio
async def test_luna_image_reasoning_compaction_hits_prompt_cache(
    e2e_workspace_root_path: str,
) -> None:
    config_service = ConfigService(
        config_path=Path.cwd() / "configs" / "tests" / "default.jsonc",
        workspace_root=e2e_workspace_root_path,
    )
    provider = config_service.get_llm_provider("backup_3")
    assert provider["model"] == "gpt-5.6-luna"
    assert provider["api_mode"] == "responses"
    assert "image_input" in provider["capabilities"]

    cache_key = f"luna-image-compact-e2e-{uuid.uuid4().hex}"
    model = build_model_from_provider(provider, {}, prompt_cache_key=cache_key)
    middleware = CachePreservingSummarizationMiddleware(
        model=model,
        backend=FilesystemBackend(
            root_dir=e2e_workspace_root_path,
            virtual_mode=True,
        ),
        trigger=("messages", 7),
        keep=("messages", 1),
        trim_tokens_to_summarize=None,
    )
    stable_nonce = uuid.uuid4().hex
    system_message = SystemMessage(
        content="\n".join(
            f"Luna 图片压缩缓存固定系统资料-{stable_nonce}-{index:04d}，必须逐字保持。"
            for index in range(260)
        )
    )
    calls: list[dict] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        capture_token = begin_upstream_capture()
        combined: AIMessageChunk | None = None
        try:
            async for chunk in request.model.astream(
                [request.system_message, *request.messages]
            ):
                assert isinstance(chunk, AIMessageChunk)
                combined = chunk if combined is None else combined + chunk
            attempts = end_upstream_capture(capture_token)
        except BaseException:
            end_upstream_capture(capture_token)
            raise
        assert combined is not None
        message = message_chunk_to_message(combined)
        assert isinstance(message, AIMessage)
        usage = message.usage_metadata
        assert usage is not None
        details = usage.get("input_token_details") or {}
        calls.append(
            {
                "input_tokens": usage.get("input_tokens", 0),
                "cached_tokens": details.get("cache_read", 0),
                "upstream_request": attempts[-1]["request"],
                "message": message,
                "request_messages": list(request.messages),
            }
        )
        return ModelResponse(result=[message])

    first_user = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "识别图片内容，记住第一轮资料并只回复 LUNA-FIRST。"
                    + "图片缓存资料甲乙丙丁" * 300
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": _luna_test_image_data_url()},
            },
        ]
    )
    first_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=[first_user],
            system_message=system_message,
        ),
        handler,
    )
    first_ai = _model_response_message(first_response)

    second_user = HumanMessage(
        content="记住第二轮资料并只回复 LUNA-SECOND。" + "戊己庚辛" * 400
    )
    second_messages = [first_user, first_ai, second_user]
    second_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=second_messages,
            system_message=system_message,
        ),
        handler,
    )
    second_ai = _model_response_message(second_response)

    third_messages = [
        *second_messages,
        second_ai,
        HumanMessage(content="需要压缩的 Luna 中段问题一：" + "天地玄黄" * 1200),
        AIMessage(content="需要压缩的 Luna 中段回答一：" + "宇宙洪荒" * 1200),
        HumanMessage(content="需要压缩的 Luna 中段问题二：" + "日月盈昃" * 1200),
        AIMessage(content="需要压缩的 Luna 中段回答二：" + "辰宿列张" * 1200),
        HumanMessage(content="压缩后只回复 LUNA-THIRD。"),
    ]
    third_response = await middleware.awrap_model_call(
        ModelRequest(
            model=model,
            messages=third_messages,
            system_message=system_message,
        ),
        handler,
    )
    assert isinstance(third_response, ExtendedModelResponse)
    compacted_request_messages = calls[3]["request_messages"]
    await handler(
        ModelRequest(
            model=model,
            messages=compacted_request_messages,
            system_message=system_message,
        )
    )
    await handler(
        ModelRequest(
            model=model,
            messages=third_messages,
            system_message=system_message,
        )
    )
    assert len(calls) == 6

    second_call = calls[1]
    summary_call = calls[2]
    compacted_call = calls[3]
    compacted_replay_call = calls[4]
    uncompacted_call = calls[5]
    second_input = second_call["upstream_request"]["input"]
    summary_input = summary_call["upstream_request"]["input"]
    compacted_input = compacted_call["upstream_request"]["input"]
    uncompacted_input = uncompacted_call["upstream_request"]["input"]
    print(
        "\n[luna-image-compaction-cache] "
        f"second={second_call['cached_tokens']}, "
        f"summary={summary_call['cached_tokens']}, "
        f"compacted={compacted_call['cached_tokens']}; "
        f"compacted_replay={compacted_replay_call['cached_tokens']}; "
        f"uncompacted_input={uncompacted_call['input_tokens']}, "
        f"compacted_input={compacted_call['input_tokens']}"
    )

    assert summary_input[: len(second_input)] == second_input
    assert compacted_input[: len(second_input)] == second_input
    assert len(compacted_input) < len(uncompacted_input)
    assert compacted_call["input_tokens"] < uncompacted_call["input_tokens"]
    assert compacted_replay_call["upstream_request"]["input"] == compacted_input
    assert compacted_replay_call["cached_tokens"] > 0
    assert summary_call["upstream_request"]["prompt_cache_key"] == cache_key
    assert compacted_replay_call["upstream_request"]["prompt_cache_key"] == cache_key
    assert "input_image" in _nested_mapping_values(
        calls[0]["upstream_request"]["input"],
        "type",
    )
    encrypted_replay = _nested_mapping_values(second_input, "encrypted_content")
    assert encrypted_replay, "Luna 第二轮请求没有回放 encrypted_content"
    assert _nested_mapping_values(
        summary_input,
        "encrypted_content",
    )[: len(encrypted_replay)] == encrypted_replay
    assert _nested_mapping_values(
        compacted_input,
        "encrypted_content",
    )[: len(encrypted_replay)] == encrypted_replay
    encrypted_items = [
        *_encrypted_reasoning_items(first_ai),
        *_encrypted_reasoning_items(second_ai),
        *_encrypted_reasoning_items(calls[3]["message"]),
        *_encrypted_reasoning_items(calls[4]["message"]),
    ]
    assert encrypted_items, "Luna 压缩链路没有返回 encrypted reasoning item"
    assert all(item.get("encrypted_content") for item in encrypted_items)
