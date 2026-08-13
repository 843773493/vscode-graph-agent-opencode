from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(slots=True)
class HTTPStubState:
    requests: list[dict[str, object]] = field(default_factory=list)

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


def _request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("集成测试 stub 只接受 JSON object 请求")
    return payload


@contextmanager
def generation_target_stub(
    port: int,
    *,
    output_workspace_id: str,
    catalog_items: list[dict[str, object]] | None = None,
    catalog_revision: str = "rev_e2e_generation_stub",
) -> Iterator[HTTPStubState]:
    state = HTTPStubState()
    exported_catalog_items = catalog_items or []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            state.requests.append(
                {"method": "GET", "path": self.path, "json": None}
            )
            if self.path == "/api/v1/health":
                _json_response(
                    self,
                    {
                        "data": {"status": "ok"},
                        "request_id": "req_e2e_generation_stub",
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

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def openai_chat_stub(port: int) -> Iterator[HTTPStubState]:
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
            if payload.get("stream") is True:
                chunks = [
                    {
                        "id": "chatcmpl-e2e",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "e2e-stub-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "E2E 生成器替身回复",
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-e2e",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "e2e-stub-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 4,
                            "total_tokens": 12,
                        },
                    },
                ]
                encoded = b"".join(
                    f"data: {json.dumps(chunk)}\n\n".encode()
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
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
                                "content": "E2E 生成器替身回复",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 4,
                        "total_tokens": 12,
                    },
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
