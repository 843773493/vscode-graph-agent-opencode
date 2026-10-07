"""真实 native dispatch 集成测试的 TCP 协议桩与 Saver fixture。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.agents.policy import ToolPolicyResolver
from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import (
    execution_admission_scope,
)
from app.core.identifier import create_uuid_hex
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.resource_activation.contracts import (
    reset_current_execution_resource_activation,
    set_current_execution_resource_activation,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

EMPTY_TOOL_SET = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@contextmanager
def dispatch_execution_scope(
    saver: RolloutCheckpointSaver, session_id: str, turn_id: str
) -> Iterator[str]:
    """真实测试 fixture 与 native dispatch 共用当前执行授权，不伪造 owner。"""
    thread = saver._storage.main_thread_id(session_id)
    admission = saver.execution_admission(
        session_id,
        thread_id=thread,
        execution_id=saver.execution_for_turn(
            session_id, thread_id=thread, turn_id=turn_id
        ),
    )
    token = set_current_job_id(turn_id)
    try:
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session_id, thread), langgraph_thread_id=thread
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            coordinator = ResourceActivationCoordinator(
                registry=ResourceRegistry(), saver=saver,
                body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
            )
            # 同步 scope 在独立线程运行真实 async freeze，并保留当前执行授权。
            with ThreadPoolExecutor(max_workers=1) as executor:
                parent = executor.submit(
                    copy_context().run, asyncio.run,
                    coordinator.freeze_execution(
                        admission=admission,
                        policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}),
                    ),
                ).result()
            activation_token = set_current_execution_resource_activation(parent, admission=admission)
            try:
                yield thread
            finally:
                reset_current_execution_resource_activation(activation_token)
    finally:
        reset_current_job_id(token)


def seed_dispatch_history(saver: RolloutCheckpointSaver, session_id: str) -> str:
    """正式 Saver ingress 创建 canonical history，不手工构造 sealed plan。"""
    thread = saver._storage.main_thread_id(session_id)
    saver.accept_turn(
        session_id,
        thread_id=thread,
        accepted_ingress_id="http-user",
        acceptance_idempotency_key="native-http-accept",
        job_id="native-http-job",
        admission_idempotency_key="native-http-admission",
        payload="HTTP canonical user",
        turn_id="http-turn",
        root_item_id="item-http-user",
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                content="HTTP canonical user",
                id="http-user",
                response_metadata={"turn_id": "http-turn"},
            ),
            AIMessage(content="HTTP canonical answer", id="http-answer"),
        ]
    }
    checkpoint["channel_versions"] = {"messages": "1"}
    with dispatch_execution_scope(saver, session_id, "http-turn"):
        saver.put(
            build_checkpoint_config(thread),
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {"messages": "1"},
        )
    return "http-turn"


def seed_dispatch_overlay(saver: RolloutCheckpointSaver, session_id: str) -> None:
    thread_id = saver._storage._path_resolver.main_thread_id(session_id)
    base = [{"type": "text", "text": "HTTP overlay base"}]
    delta = [{"type": "text", "text": "HTTP overlay delta"}]
    saver.register_source_overlay(
        SimpleNamespace(
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_ns="",
            overlay_id="http-overlay",
            source_kind="workspace_policy",
            source_revision="policy-B",
            source_overlay_epoch=0,
            base_ref="z-base",
            delta_ref="a-delta",
            base_source_revision="policy-A",
            delta_source_revision="policy-B",
            delta_from_revision="policy-A",
            delta_to_revision="policy-B",
            delta_diff_hash=sha256_jcs(delta),
            status="active",
            idempotency_key="http-overlay-A-B",
        ),
        session_id=session_id,
        thread_id=thread_id,
        base_content=base,
        delta_content=delta,
    )


@pytest.fixture(scope="module")
def native_dispatch_workspace(integration_workspace_root_path) -> Path:
    """Saver 合同与完整后端使用各自的工作区，避免最小 bundle 混入 Session API。"""
    return prepare_default_test_workspace(
        workspace_root=Path(integration_workspace_root_path) / "native-dispatch",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )


@pytest.fixture
def dispatch_saver(native_dispatch_workspace, session_bundle_factory):
    sessions = native_dispatch_workspace / ".boxteam/sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(saver, session_id)
        seed_dispatch_overlay(saver, session_id)
        with dispatch_execution_scope(saver, session_id, turn_id):
            yield saver, session_id, turn_id, sessions


@dataclass
class NativeHTTPState:
    artifacts: Path
    requests: list[dict[str, object]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    api_key: str = "native-contract-key"
    call_tool: bool = False
    response_text: str = "native-http-result"
    output_tool_name: str | None = None
    reasoning_item: dict[str, object] | None = None
    request_ready: Event = field(default_factory=Event)
    release_response: Event = field(default_factory=Event)

    def record(self, path: str, authorization: str, body: object) -> int:
        with self.lock:
            self.requests.append(
                {"path": path, "authorization": authorization, "body": body}
            )
            ordinal = len(self.requests)
            (self.artifacts / f"native-http-{ordinal}.json").write_text(
                json.dumps(self.requests[-1], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return ordinal


def _response_events(
    ordinal: int, *, call_tool: bool = False, text: str = "native-http-result",
    tool_name: str = "inspect_file", reasoning_item: dict[str, object] | None = None,
) -> bytes:
    message_id = f"msg-native-{ordinal}"
    part = {"type": "output_text", "text": text, "annotations": []}
    item = {
        "type": "message",
        "id": message_id,
        "role": "assistant",
        "status": "completed",
        "content": [part],
    }
    response = {
        "id": f"resp-native-{ordinal}",
        "object": "response",
        "created_at": 1,
        "model": "contract-model",
        "status": "completed",
        "output": [item],
        "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {**part, "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": part,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    if call_tool:
        arguments = json.dumps({"path": "README.md"} if tool_name == "inspect_file" else {"answer": text})
        call = {
            "type": "function_call",
            "id": "fc-native",
            "call_id": "call-native",
            "name": tool_name,
            "arguments": arguments,
            "status": "completed",
        }
        events = [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**call, "status": "in_progress", "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc-native",
                "output_index": 0,
                "delta": arguments,
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc-native",
                "output_index": 0,
                "arguments": arguments,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": call},
            {"type": "response.completed", "response": {**response, "output": [call]}},
        ]
    if reasoning_item is not None:
        # reasoning 与工具调用共享同一响应；真实字段原样参与下一次 HTTP 请求。
        for event in events:
            if "output_index" in event:
                event["output_index"] += 1
        events[1:1] = [
            {"type": "response.output_item.added", "output_index": 0, "item": {
                **reasoning_item, "status": "in_progress", "summary": [],
                "content": [], "encrypted_content": None,
            }},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                **reasoning_item, "status": "completed",
            }},
        ]
        events[-1]["response"]["output"].insert(0, reasoning_item)
    return "".join(
        "event: "
        + event["type"]
        + "\ndata: "
        + json.dumps({**event, "sequence_number": index}, ensure_ascii=False)
        + "\n\n"
        for index, event in enumerate(events)
    ).encode("utf-8")


def _chat_response_events(
    ordinal: int, *, text: str = "native-http-result", tool_name: str | None = None,
) -> bytes:
    """真实 Chat Completions SSE 必须包含上游 stop 和 DONE。"""
    common = {
        "id": f"chat-contract-{ordinal}",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "contract-model",
    }
    delta = {"role": "assistant", "content": text}
    if tool_name is not None:
        delta = {"role": "assistant", "tool_calls": [{
            "index": 0, "id": "call-native", "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps({"answer": text})},
        }]}
    chunks = [
        {**common, "choices": [{
            "index": 0, "delta": delta,
            "finish_reason": None,
        }]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_name is not None else "stop"}]},
    ]
    return (
        "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode("utf-8")


@pytest.fixture
def native_http_server(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[NativeHTTPState, str]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / f"http-{uuid4().hex}"
    artifacts.mkdir()
    state = NativeHTTPState(artifacts)
    state.release_response.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # 请求/认证信息已写入正式产物，不把正文散落到 pytest stderr。
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            ordinal = state.record(
                self.path, self.headers.get("Authorization", ""), body
            )
            state.request_ready.set()
            if not state.release_response.wait(timeout=40):
                self.send_error(504, "native contract response gate timeout")
                return
            if (
                self.path not in {"/v1/responses", "/v1/chat/completions"}
                or self.headers.get("Authorization") != f"Bearer {state.api_key}"
            ):
                self.send_error(400, "native contract endpoint/auth mismatch")
                return
            response = (
                _response_events(
                    ordinal, call_tool=(state.call_tool or state.output_tool_name is not None) and ordinal == 1,
                    text=state.response_text, tool_name=state.output_tool_name or "inspect_file",
                    reasoning_item=state.reasoning_item if ordinal == 1 else None,
                )
                if self.path == "/v1/responses"
                else _chat_response_events(ordinal, text=state.response_text, tool_name=state.output_tool_name)
            )
            (state.artifacts / f"native-http-{ordinal}.response.sse").write_bytes(
                response
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        state.release_response.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def create_dispatch_agent(model, *, middleware, **options):
    """真实图创建后显式绑定每个 Bridge，不替换框架注册表或 binding。"""
    agent = create_agent(model, middleware=middleware, **options)
    for item in middleware:
        if isinstance(item, SealedAssemblyDispatchBridge):
            item.bind_graph(agent)
    return agent


async def invoke_provider_dispatch(
    saver: RolloutCheckpointSaver,
    session_id: str,
    turn_id: str,
    endpoint: str,
    api_key: str,
    *,
    target_format: str,
) -> dict[str, object]:
    """父进程和重启子进程调用同一真实 LangChain/Provider 入口。"""
    model_type = {
        "responses": BoxteamOpenAIResponsesModel,
        "chat_completions": BoxteamLiteLLMChatModel,
    }[target_format]
    model = model_type(
        model="openai/gpt-4.1",
        api_key=api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    thread = saver._storage.main_thread_id(session_id)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session_id, thread),
        control_state_port=saver,
    )
    manager.register(
        ContextSourceDescriptor(
            source_id="instruction:native-http",
            source_kind="agent_instructions",
            name="HTTP instruction",
            description="HTTP协议桩的已登记说明",
            internal_locator="instruction-producer://native-http",
            root_placement="root_eligible",
        ),
        tracking_status="tracked",
    )
    manager.observe(
        "instruction:native-http", "HTTP restart prompt", revision="native-http:1"
    )
    producer = RegisteredToolSetProducer(
        agent_id="native-http-test",
        selection_reader=ToolSelectionStore(boxteam_root=saver._storage.sessions_dir.parent),
        policy_resolver=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={}),
        tools=(), metadata={},
    )
    coordinator = ResourceActivationCoordinator(
        registry=ResourceRegistry(), saver=saver,
        body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
    )
    agent = create_dispatch_agent(
        model,
        middleware=[
            SealedAssemblyDispatchBridge(
                checkpointer=saver,
                session_id=session_id,
                thread_id=thread,
                context_source_manager=manager,
                tool_set_producer=producer,
                resource_activation=coordinator,
            )
        ],
        checkpointer=saver,
    )
    with dispatch_execution_scope(saver, session_id, turn_id):
        return await agent.ainvoke({"messages": []}, build_checkpoint_config(thread))
