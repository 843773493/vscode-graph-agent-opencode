"""真实 native dispatch 集成测试的 TCP 协议桩与 Saver fixture。"""

from __future__ import annotations

import json
from collections.abc import Iterator
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

from app.agents.itemized_context_middleware import ItemizedContextProjectionMiddleware
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.core.checkpoint_config import build_checkpoint_config
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


def seed_dispatch_history(saver: RolloutCheckpointSaver, session_id: str) -> str:
    """正式 Saver ingress 创建 canonical history，不手工构造 sealed plan。"""
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
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        {"source": "input", "step": 0, "parents": {}},
        {"messages": "1"},
    )
    return "http-turn"


def seed_dispatch_overlay(saver: RolloutCheckpointSaver, session_id: str) -> None:
    base = [{"type": "text", "text": "HTTP overlay base"}]
    delta = [{"type": "text", "text": "HTTP overlay delta"}]
    saver.register_source_overlay(
        SimpleNamespace(
            session_id=session_id,
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
    session_id = f"ses_native_{uuid4().hex}"
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(saver, session_id)
        seed_dispatch_overlay(saver, session_id)
        yield saver, session_id, turn_id, sessions


@dataclass
class NativeHTTPState:
    artifacts: Path
    requests: list[dict[str, object]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    api_key: str = "native-contract-key"
    call_tool: bool = False
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


def _response_events(ordinal: int, *, call_tool: bool = False) -> bytes:
    message_id = f"msg-native-{ordinal}"
    text = "native-http-result"
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
        arguments = json.dumps({"path": "README.md"})
        call = {
            "type": "function_call",
            "id": "fc-native",
            "call_id": "call-native",
            "name": "inspect_file",
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
    return "".join(
        "event: "
        + event["type"]
        + "\ndata: "
        + json.dumps({**event, "sequence_number": index}, ensure_ascii=False)
        + "\n\n"
        for index, event in enumerate(events)
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
                self.path != "/v1/responses"
                or self.headers.get("Authorization") != f"Bearer {state.api_key}"
            ):
                self.send_error(400, "native contract endpoint/auth mismatch")
                return
            response = _response_events(
                ordinal, call_tool=state.call_tool and ordinal == 1
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


async def invoke_native_dispatch(
    saver: RolloutCheckpointSaver,
    session_id: str,
    turn_id: str,
    endpoint: str,
    api_key: str,
) -> dict[str, object]:
    """父进程和重启子进程调用同一真实 LangChain/Provider 入口。"""
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    agent = create_agent(
        model,
        system_prompt="HTTP restart prompt",
        middleware=[ItemizedContextProjectionMiddleware(checkpointer=saver)],
        checkpointer=saver,
    )
    token = set_current_job_id(turn_id)
    try:
        return await agent.ainvoke(
            {"messages": []}, build_checkpoint_config(session_id)
        )
    finally:
        reset_current_job_id(token)
