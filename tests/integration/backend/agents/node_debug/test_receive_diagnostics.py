"""真实 Inspector 接收与日志同时失败，仍结算原命令、动作和进程。"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.client import ClientConnection

from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 工作区按当前测试路径隔离
)


@pytest.mark.parametrize("failure", ["json", "native"])
async def test_receive_and_diagnostic_failure_preserve_primary_and_allow_product_retry(
    debug_receive_client: DebugReceiveClient, failure: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    native = PermissionError(errno.EACCES, "Inspector 接收故障", str(root / "receive-secret"))
    connect_original = websockets.connect
    opened: list[ClientConnection] = []
    received: list[int] = []
    blocker = root / "receive-log-secret"
    blocker.write_text("普通文件不能作为日志目录")
    destination = blocker / "service.log"
    sink_errors: list[OSError] = []
    call_handlers = logging.Logger.callHandlers

    class FailingReceiver:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket
            self.command_id: int | None = None

        async def send(self, message: str) -> None:
            payload = json.loads(message)
            if payload["method"] == "Runtime.enable":
                self.command_id = payload["id"]
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def messages() -> AsyncIterator[str | bytes]:
                async for message in self.socket:
                    payload = json.loads(message)
                    if self.command_id is not None and payload.get("id") == self.command_id:
                        received.append(self.command_id)
                        if failure == "native":
                            raise native
                        yield '{"receive-secret":'
                    else:
                        yield message
            return messages()

    async def connect(uri: str, *, ping_interval: float | None) -> FailingReceiver:
        socket = await connect_original(uri, ping_interval=ping_interval)
        opened.append(socket)
        return FailingReceiver(socket)

    def emit(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if record.getMessage().startswith("读取 Node Inspector 事件失败:"):
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                sink_errors.append(error)
                raise

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "接收与日志故障后原方案重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    request = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(websockets, "connect", connect)
        patch.setattr(logging.Logger, "callHandlers", emit)
        failed = await client.post("/api/v1/debug/node/start", json=request)
    assert failed.status_code == 409, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert len(received) == 1 and len(opened) == 1 and opened[0].close_code is not None
    assert len(sink_errors) == 1 and sink_errors[0].errno == errno.ENOTDIR
    records = [record for record in caplog.records
               if record.getMessage().startswith("读取 Node Inspector 事件失败:")]
    assert len(records) == 1 and records[0].exc_info is not None
    original = records[0].exc_info[1]
    assert original is not None
    if failure == "native":
        assert original is native
        assert "error_type=PermissionError, errno=13" in failed.text
    else:
        assert isinstance(original, json.JSONDecodeError)
        assert f"line={original.lineno}, column={original.colno}, position={original.pos}" in failed.text
    assert any(str(sink_errors[0]) in note for note in original.__notes__)
    assert "diagnostic_sink_error=NotADirectoryError" in failed.text

    state = await client.get("/api/v1/debug/node", params=owner)
    assert state.status_code == 200, state.text
    live = state.json()["data"]
    assert live["status"] == "failed" and isinstance(live["pid"], int)
    assert probe_process_identity(live["pid"]) is None
    assert live["error_message"] == failed.json()["detail"]
    failures = [action for action in live["actions"] if action["action"] == "start_failed"]
    assert len(failures) == 1 and failures[0]["message"] == live["error_message"]
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert [action.model_dump(mode="json") for action in manifest.actions] == live["actions"]
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid == live["pid"]
    for text in (failed.text, state.text, manifest_bytes.decode()):
        assert owner["session_id"] in text and owner["thread_id"] in text
        assert str(root) not in text and "receive-secret" not in text and "receive-log-secret" not in text
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    assert cold.json()["data"]["status"] == "idle" and cold.json()["data"]["pid"] is None
    assert cold.json()["data"]["actions"] == live["actions"]
    assert cold.json()["data"]["active_configuration_id"] == configuration_id
    assert str(root) not in cold.text and "receive-secret" not in cold.text
    retried = await client.post("/api/v1/debug/node/start", json=request)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
    assert probe_process_identity(retried.json()["data"]["pid"]) is None
    assert [action for action in stopped.json()["data"]["actions"]
            if action["action"] == "start_failed"] == failures
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / f"{failure}.json").write_text(json.dumps({
        "failed": failed.json(), "live": state.json(), "manifest": manifest.model_dump(mode="json"),
        "claim": claim.model_dump(mode="json"), "cold": cold.json(),
        "retried": retried.json(), "stopped": stopped.json(),
        "primary_type": type(original).__name__, "primary_notes": original.__notes__,
        "scope": "ASGI、真实Node/Inspector和产品持久claim；不代Gateway、独立进程重启或Windows",
    }, ensure_ascii=False, indent=2) + "\n")
