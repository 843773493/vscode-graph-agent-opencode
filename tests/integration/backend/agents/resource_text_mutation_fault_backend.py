"""文件效果验收专用装配：在真实发布或回执交付处退出、阻塞目标。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.infrastructure.workspace_files import text_mutation
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from tests.support.e2e_control.backend_app import app

__all__ = ["app"]

_publish = text_mutation.publish_file_content
_transfer = WorkspaceTextMutationOwner.execute_transfer


class _FaultMutationResponse:
    """真实 owner 发布后破坏 HTTP 回包，单独验证目标 Gateway 的校验。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/api/v1/resources/text-mutations":
            await self.app(scope, receive, send)
            return
        start: Message = {}
        chunks: list[bytes] = []

        async def corrupt(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            if start["status"] == 200:
                payload = json.loads(body)
                result = payload["data"]
                target = result["receipt"].get("target", "")
                for field in ("target", "path", "uri"):
                    name = "invalid-" + field + ".txt"
                    if target.endswith("/" + name):
                        if (Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / ("repair-" + field)).exists():
                            break
                        _record("valid-" + field + ".json", payload)
                        native = "/private/text-mutation-" + name
                        if field == "target":
                            result["receipt"]["target"] = native
                        elif field == "path":
                            result["receipt"]["files"][0]["path"] = native
                        else:
                            result["snapshots"][0]["uri"] = native
                        _record("invalid-" + field + ".json", payload)
                        body = json.dumps(payload).encode("utf-8")
                        start["headers"] = [(key, value) for key, value in start["headers"] if key.lower() != b"content-length"]
                        start["headers"].append((b"content-length", str(len(body)).encode("ascii")))
                        break
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, corrupt)


def _record(name: str, value: object) -> None:
    path = Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _fault_publish(path, *args, **options):
    if path.name == "partial-second.txt":
        raise OSError("受控第二文件发布失败")
    result = _publish(path, *args, **options)
    if path.name == "publish-exit-first.txt":
        _record("publish-exit.json", {"stage": "first_file_published"})
        os._exit(72)
    if path.name in {"cancel-first.txt", "timeout-first.txt"}:
        phase = path.name.split("-", 1)[0]
        _record(phase + "-entered.json", {"stage": "first_file_published"})
        release = Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / (phase + "-release")
        while not release.is_file():
            time.sleep(0.01)
    return result


def _fault_transfer(owner, payload):
    command = payload.command
    if any(command.target.endswith("/invalid-" + field + ".txt") for field in ("target", "path", "uri")):
        with (Path(os.environ["BOXTEAM_TEST_MUTATION_ARTIFACTS"]) / "mutation-attempts.jsonl").open("a") as output:
            output.write(json.dumps(command.model_dump()) + "\n")
    result = _transfer(owner, payload)
    if "response-drop-first.txt" in getattr(command, "input", ""):
        _record("response-drop.json", result.model_dump(mode="json"))
        os._exit(73)
    for phase in ("cancel", "timeout"):
        if any(item.path.endswith("/" + phase + "-third.txt") for item in result.receipt.files):
            _record(phase + "-finished.json", result.model_dump(mode="json"))
    return result


text_mutation.publish_file_content = _fault_publish
WorkspaceTextMutationOwner.execute_transfer = _fault_transfer
app.add_middleware(_FaultMutationResponse)
