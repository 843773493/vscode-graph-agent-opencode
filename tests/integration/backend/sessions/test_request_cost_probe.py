"""临时完整请求性能诊断；完成后将源码归档到本任务产物目录。"""
from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack

import httpx._content
import openai._base_client
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents import llm_logging_middleware
from app.agents.model_call_execution import ModelCallExecution
from app.core.checkpoint_config import build_checkpoint_config
from app.core.identifier import create_uuid_hex
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import (
    assembly,
    preparation,
)
from app.testing.model_stream import ModelStreamHTTPTransport, build_cassette
from tests.harness.performance.observers import (
    checkpoint_resources,
    observe_calls,
    observe_loop,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    chat_reply_frames,
    dispatch_execution_scope,
    invoke_provider_dispatch,
    responses_reply_frames,
    seed_dispatch_history,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    native_dispatch_workspace as native_dispatch_workspace,  # noqa: PLC0414
)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format,count,concurrency", [
    pytest.param("chat_completions", 20, 1, id="warmup-chat"),
    pytest.param("responses", 20, 1, id="warmup-responses"),
    pytest.param("chat_completions", 2000, 1, id="chat-2k-single"),
    pytest.param("responses", 2000, 1, id="responses-2k-single"),
    pytest.param("chat_completions", 20000, 1, id="chat-20k-single"),
    pytest.param("responses", 20000, 1, id="responses-20k-single"),
    pytest.param("chat_completions", 2000, 6, id="chat-2k-six"),
    pytest.param("chat_completions", 20000, 6, id="chat-20k-six"),
])
async def test_request_cost(
    native_dispatch_workspace, session_bundle_factory, inprocess_model_replay,
    perf, target_format, count, concurrency,
):
    replay, controller = inprocess_model_replay
    sessions = native_dispatch_workspace / ".boxteam/sessions"
    endpoint = "https://request-cost.invalid/v1"
    responses = target_format == "responses"
    protocol = "openai_responses_sse" if responses else "openai_chat_sse"
    suffix = "/responses" if responses else "/chat/completions"
    frames = responses_reply_frames if responses else chat_reply_frames
    cassette = build_cassette(asset_id="request-cost", url=endpoint + suffix,
                             model="gpt-4.1", protocol=protocol, frames=frames(1))
    interaction = {**cassette.raw["interactions"][0], "protocol": protocol}
    workload = {"format": target_format, "history_messages": count,
                "history_chars": 4_000_000 if count > 20 else 4000,
                "concurrency": concurrency}
    owners = []
    body = "abcdefghij" * (workload["history_chars"] // count // 10)
    with RolloutCheckpointSaver(sessions) as saver:
        with perf.stage("probe.seed", workload=workload):
            for ordinal in range(concurrency):
                session = f"ses_{create_uuid_hex()}"
                session_bundle_factory(sessions, session)
                turn = seed_dispatch_history(saver, session)
                thread = saver._storage.storage.main_thread_id(session)
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {"messages": [
                    HumanMessage(id="http-user", content="HTTP canonical user", response_metadata={"turn_id": turn}),
                    *(AIMessage(id=f"history-{i}", content=[{"type": "text", "text": body}],
                                response_metadata={"model_call_id": f"history-call-{i}"}) for i in range(count)),
                ]}
                checkpoint["channel_versions"] = {"messages": "2"}
                with dispatch_execution_scope(saver, session, turn):
                    saver.put(build_checkpoint_config(thread), checkpoint,
                              {"source": "input", "step": 1, "parents": {}}, {"messages": "2"})
                replay.control.plan_model_calls(session, thread, [interaction])
                owners.append((session, turn))
        controller.ensure_installed()
        with ExitStack() as stack:
            for owner, names in (
                (ModelCallExecution, ("_aprepare", "_frozen_preparation", "_settle_boundary", "_complete_preparation", "_ainvoke")),
                (RolloutCheckpointSaver, ("get_tuple", "put", "prepare_model_call_boundary", "freeze_context_for_provider", "seal_context_for_provider", "read_context_for_dispatch")),
                (preparation, ("freeze_registered_context", "seal_prepared_context")),
                (assembly, ("seal_snapshot", "_project_wire_snapshot")),
                (llm_logging_middleware.LLMLoggingMiddleware, ("_save_log",)),
                (llm_logging_middleware, ("bound_json_value",)),
                (httpx._content, ("encode_json",)),
                (openai._base_client, ("openapi_dumps",)),
                (ModelStreamHTTPTransport, ("_replay_response",)),
            ):
                stack.enter_context(observe_calls(owner, names, perf))
            checkpoint_resources(perf, "before-request", 0)
            async with observe_loop(perf):
                with perf.stage("probe.requests", workload=workload):
                    async def invoke(session, turn):
                        with perf.stage("probe.request", workload=workload, context={"session_id": session}):
                            result = await invoke_provider_dispatch(
                                saver, session, turn, endpoint, "request-cost-key",
                                target_format=target_format, mode="async",
                            )
                            assert result["messages"][-1].text == "native-http-result"
                    await asyncio.gather(*(invoke(*owner) for owner in owners))
            checkpoint_resources(perf, "after-request", 1)
        # 验证真实 transport 正文确实包含长历史，防止只测到短 checkpoint。
        for session, _ in owners:
            artifacts = [json.loads(path.read_text()) for path in replay.artifacts.glob("*.json")]
            actual = next(value["request"] for value in artifacts if value.get("identity", {}).get("session_id") == session)
            encoded = json.dumps(actual, ensure_ascii=False).encode()
            assert encoded.count(body.encode()) >= count
            perf.record("metric", "probe.wire", workload=workload, body_bytes=len(encoded),
                        history_occurrences=encoded.count(body.encode()), session_id=session)
