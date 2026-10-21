from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.testing.model_stream import (
    StreamFrame,
    chat_completion_frames,
    get_protocol_codec,
)


@dataclass(slots=True)
class HTTPStubState:
    requests: list[dict[str, object]] = field(default_factory=list)
    response_callback: Callable[[dict[str, object]], dict[str, object]] | None = None

    def requests_for(self, method: str, path: str) -> list[dict[str, object]]:
        return [
            request
            for request in self.requests
            if request["method"] == method and request["path"] == path
        ]


def _json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, object],
    *,
    status: int = 200,
) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _sse_response(handler: BaseHTTPRequestHandler, frames: Sequence[StreamFrame]) -> None:
    codec = get_protocol_codec("openai_chat_sse")
    codec.validate_stream(frames, label="HTTP Chat 替身")
    encoded = b"".join(codec.encode(frame) for frame in frames)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)
    handler.wfile.flush()


def _request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("集成测试 stub 只接受 JSON object 请求")
    return payload


@contextmanager
def _running_http_stub_server(
    handler: type[BaseHTTPRequestHandler], port: int,
) -> Iterator[None]:
    with ExitStack() as cleanup:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        cleanup.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def join_thread() -> None:
            thread.join(timeout=5)
            assert not thread.is_alive(), "HTTP替身服务线程未停止"

        # 仅在线程启动后登记停机，按 shutdown、join、关闭 socket 的顺序释放。
        cleanup.callback(join_thread)
        cleanup.callback(server.shutdown)
        yield


@contextmanager
def generation_target_stub(
    port: int,
    *,
    output_workspace_id: str,
    backend_workspace_id: str,
    catalog_items: list[dict[str, object]] | None = None,
    catalog_revision: str = "rev_e2e_generation_stub",
) -> Iterator[HTTPStubState]:
    state = HTTPStubState()
    exported_catalog_items = catalog_items or []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def has_expected_backend_identity(self) -> bool:
            if self.headers.get("X-BoxTeam-Workspace-Id") == backend_workspace_id:
                return True
            _json_response(
                self,
                {"detail": "backend workspace identity mismatch"},
                status=400,
            )
            return False

        def do_GET(self) -> None:
            state.requests.append(
                {"method": "GET", "path": self.path, "json": None}
            )
            if not self.has_expected_backend_identity():
                return
            if self.path == "/api/v1/health":
                _json_response(
                    self,
                    {
                        "status": "ok",
                        "workspace_id": backend_workspace_id,
                        "process_id": os.getpid(),
                    },
                )
                return
            if self.path == "/api/v1/session-catalog/export":
                _json_response(
                    self,
                    {
                        "data": {
                            "revision": catalog_revision,
                            "items": exported_catalog_items,
                        },
                        "request_id": "req_e2e_generation_stub",
                    },
                )
                return
            if self.path.startswith("/api/v1/session-catalog/breadcrumb/"):
                node_id = self.path.rsplit("/", maxsplit=1)[-1]
                node = next(
                    (
                        item
                        for item in exported_catalog_items
                        if item.get("node_id") == node_id
                    ),
                    None,
                )
                if node is None and node_id == "ses_stub_parent":
                    node = {
                        "node_id": node_id,
                        "kind": "session",
                        "name": "Stub 父会话",
                    }
                if node is None:
                    _json_response(
                        self,
                        {"detail": f"会话目录节点不存在: {node_id}"},
                        status=404,
                    )
                    return
                _json_response(
                    self,
                    {
                        "data": {
                            "revision": catalog_revision,
                            "items": [node],
                        },
                        "request_id": "req_e2e_generation_stub",
                    },
                )
                return
            if self.path == "/api/v1/session-generations/capabilities":
                _json_response(
                    self,
                    {
                        "data": {
                            "items": [
                                {
                                    "type_id": "builtin.agent_prompt",
                                    "supported_versions": ["1"],
                                    "config_schema": {
                                        "type": "object",
                                        "properties": {
                                            "prompt": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "session_title": {"type": "string"},
                                        },
                                        "required": ["prompt"],
                                        "additionalProperties": False,
                                    },
                                }
                            ]
                        },
                        "request_id": "req_e2e_generation_stub",
                    },
                )
                return
            if self.path.startswith("/api/v1/jobs/"):
                job_id = self.path.rsplit("/", maxsplit=1)[-1]
                _json_response(
                    self,
                    {
                        "data": {
                            "job_id": job_id,
                            "session_id": f"ses_{job_id.removeprefix('job_')}",
                            "status": "completed",
                            "error_message": None,
                        },
                        "request_id": "req_e2e_generation_stub",
                    },
                )
                return
            if self.path.startswith("/api/v1/session-generations/status?"):
                _json_response(
                    self,
                    {
                        "data": {
                            "run_id": "grun_stub_report",
                            "status": "completed",
                            "outputs": [],
                            "message_id": "msg_stub_child",
                            "job_id": "job_stub_child",
                            "report_back_job_id": "job_stub_report_back",
                            "error": None,
                        },
                        "request_id": "req_e2e_generation_stub",
                    },
                )
                return
            _json_response(self, {"detail": f"未知路径: {self.path}"}, status=404)

        def do_POST(self) -> None:
            if self.path != "/api/v1/session-generations/execute":
                _json_response(self, {"detail": f"未知路径: {self.path}"}, status=404)
                return
            if not self.has_expected_backend_identity():
                return
            payload = _request_json(self)
            state.requests.append(
                {"method": "POST", "path": self.path, "json": payload}
            )
            digest = hashlib.sha256(
                str(payload["idempotency_key"]).encode("utf-8")
            ).hexdigest()[:24]
            _json_response(
                self,
                {
                    "data": {
                        "run_id": payload["run_id"],
                        "status": "queued",
                        "message_id": f"msg_{digest}",
                        "job_id": f"job_{digest}",
                        "outputs": [
                            {
                                "kind": "session",
                                "workspace_id": output_workspace_id,
                                "session_id": f"ses_{digest}",
                                "title": payload["title"],
                                "navigation_path": payload["navigation_path"],
                            }
                        ],
                    },
                    "request_id": "req_e2e_generation_stub",
                },
            )

    with _running_http_stub_server(Handler, port):
        yield state


@contextmanager
def openai_chat_stub(
    port: int,
    *,
    failure_marker: str | None = None,
    failure_after_requests: int | None = None,
) -> Iterator[HTTPStubState]:
    state = HTTPStubState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                _json_response(self, {"detail": f"未知路径: {self.path}"}, status=404)
                return
            if self.headers.get("Authorization") != "Bearer e2e-local-model-key":
                _json_response(self, {"detail": "模型 API key 无效"}, status=401)
                return
            payload = _request_json(self)
            state.requests.append(
                {"method": "POST", "path": self.path, "json": payload}
            )
            should_fail = failure_after_requests is not None and len(
                state.requests
            ) > failure_after_requests
            should_fail = should_fail or (
                failure_marker is not None
                and failure_marker in json.dumps(payload, ensure_ascii=False)
            )
            if should_fail:
                _json_response(
                    self,
                    {"error": {"message": "集成测试模型替身故障"}},
                    status=500,
                )
                return
            # 仅替换外部模型响应；生产工具、Job 与 Provider 链路保持真实。
            reply = (
                state.response_callback(payload)
                if state.response_callback is not None
                else {"content": "E2E 生成器替身回复"}
            )
            finish_reason = str(reply.get("finish_reason", "tool_calls" if reply.get("tool_calls") else "stop"))
            reply = {key: value for key, value in reply.items() if key != "finish_reason"}
            if payload.get("stream") is True:
                _sse_response(self, chat_completion_frames(
                    response_id="chatcmpl-e2e", model="e2e-stub-model",
                    deltas=({"role": "assistant", **reply},), finish_reason=finish_reason,
                    usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                ))
                return
            _json_response(
                self,
                {
                    "id": "chatcmpl-e2e",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "e2e-stub-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                **reply,
                            },
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 4,
                        "total_tokens": 12,
                    },
                },
            )

    with _running_http_stub_server(Handler, port):
        yield state


@contextmanager
def openai_chat_tool_loop_stub(port: int) -> Iterator[HTTPStubState]:
    """提供一个确定性的 Chat Completions 工具循环 SSE 替身。"""

    state = HTTPStubState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                _json_response(self, {"detail": f"未知路径: {self.path}"}, status=404)
                return
            if self.headers.get("Authorization") != "Bearer e2e-local-model-key":
                _json_response(self, {"detail": "模型 API key 无效"}, status=401)
                return

            payload = _request_json(self)
            state.requests.append(
                {"method": "POST", "path": self.path, "json": payload}
            )
            if payload.get("stream") is not True:
                _json_response(
                    self,
                    {"error": {"message": "浏览器工具循环必须使用 stream=true"}},
                    status=400,
                )
                return

            messages = payload.get("messages")
            roles = [
                message.get("role")
                for message in messages
                if isinstance(message, dict)
            ] if isinstance(messages, list) else []
            request_number = len(state.requests)
            if roles == ["system", "user"]:
                _sse_response(self, chat_completion_frames(
                    response_id=f"chatcmpl-browser-tool-loop-{request_number}",
                    model="browser-chat-tool-loop-stub", finish_reason="tool_calls",
                    deltas=(
                        {"role": "assistant", "tool_calls": [{
                            "index": 0, "id": "call_browser_chat_tool_loop", "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"'},
                        }]},
                        {"tool_calls": [{"index": 0, "function": {"arguments": 'README.md"}'}}]},
                    ),
                ))
                return

            if roles == ["system", "user", "assistant", "tool"]:
                _sse_response(self, chat_completion_frames(
                    response_id=f"chatcmpl-browser-tool-loop-{request_number}",
                    model="browser-chat-tool-loop-stub", finish_reason="stop",
                    deltas=({"role": "assistant", "content": "浏览器 SSE 工具调用已完成。"},),
                ))
                return

            _json_response(
                self,
                {
                    "error": {
                        "message": f"工具循环请求角色不符合预期: {roles}",
                    }
                },
                status=400,
            )

    with _running_http_stub_server(Handler, port):
        yield state
