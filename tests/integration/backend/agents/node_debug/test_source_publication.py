"""真实暂停帧发布拒绝原生位置，失败不改变进程和持久调试事实。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_core import PydanticSerializationError

from app.agents.tools.debugging.payloads import _success
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 沿当前文件路径隔离工作区
)


async def test_paused_sources_reject_mutation_and_allow_same_process_retry(
    debug_receive_client: DebugReceiveClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    source = root / "entry.mjs"
    source.write_text("const value = 42;\nsetInterval(() => {}, 1000);\n", encoding="utf-8")
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "源码发布不变量", "script_path": str(source),
        "breakpoints": [{"path": str(source), "line": 1}],
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    started = await client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configuration_id, "path": str(source),
    })
    assert started.status_code == 200, started.text
    initial = await client.get("/api/v1/debug/node", params=owner)
    assert initial.status_code == 200, initial.text
    data = initial.json()["data"]
    assert data["status"] == "paused" and data["call_stack"][0]["line"] == 1
    assert data["call_stack"][0]["path"] == source.name
    assert data["call_stack"][0]["url"].startswith("boxteam://workspace/")
    assert data["breakpoints"][0]["verified"]
    assert all("inspector_id" not in breakpoint for breakpoint in data["breakpoints"])
    assert str(root) not in initial.text and "file:" not in initial.text
    pid = data["pid"]
    identity = probe_process_identity(pid)
    assert identity is not None
    manifest_before, _ = store.read_manifest_payload(**owner)
    claim_before = store.read_launch_claim(**owner)
    configuration_before = store.read_configuration(**owner, configuration_id=configuration_id)
    assert manifest_before is not None and claim_before is not None and configuration_before is not None
    read_state = NodeDebugService.get_state
    failures: list[dict[str, object]] = []
    for container in ("call_stack", "last_stopped_frame"):
        for field in ("url", "path"):
            for native in (str(source), r"C:\private\inspector-secret.mjs",
                           r"\\target\private\inspector-secret.mjs", source.as_uri()):
                snapshots: list[NodeDebugStateDTO] = []

                async def damaged_state(
                    service: NodeDebugService, session_id: str, thread_id: str | None = None,
                    *, field: str = field, native: str = native, container: str = container,
                    snapshots: list[NodeDebugStateDTO] = snapshots,
                ) -> NodeDebugStateDTO:
                    state = await read_state(service, session_id, thread_id)
                    snapshots.append(state)
                    frame = state.call_stack[0].model_copy(update={field: native})
                    if container == "call_stack":
                        state.call_stack[0] = frame
                    else:
                        state.last_stopped_frame = frame
                    return state

                with monkeypatch.context() as patch:
                    # 只损坏原 owner 生成后的输出，不代建暂停、配置或进程事实。
                    patch.setattr(NodeDebugService, "get_state", damaged_state)
                    rejected = await client.get("/api/v1/debug/node", params=owner)
                assert rejected.status_code == 500, rejected.text
                assert rejected.json()["data"] is None
                assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"]
                assert "PydanticSerializationError" in rejected.text and native not in rejected.text
                assert len(snapshots) == 1
                with pytest.raises(PydanticSerializationError):
                    _success("调用栈", snapshots[0])
                assert probe_process_identity(pid) == identity
                assert store.read_manifest_payload(**owner)[0] == manifest_before
                assert store.read_launch_claim(**owner) == claim_before
                assert store.read_configuration(**owner, configuration_id=configuration_id) == configuration_before
                failures.append({"container": container, "field": field, "response": rejected.json()})
    restored = await client.get("/api/v1/debug/node", params=owner)
    assert restored.status_code == 200 and restored.json()["data"] == data, restored.text
    cleared = await client.post("/api/v1/debug/node/action", json={
        **owner, "action": "clear_breakpoint",
        "params": {"breakpoint_id": data["breakpoints"][0]["breakpoint_id"]},
    })
    assert cleared.status_code == 200 and cleared.json()["data"]["breakpoints"] == [], cleared.text
    installed = await client.post("/api/v1/debug/node/action", json={
        **owner, "action": "set_breakpoint", "params": {"path": str(source), "line": 2},
    })
    assert installed.status_code == 200, installed.text
    assert installed.json()["data"]["breakpoints"][0]["verified"]
    assert installed.json()["data"]["breakpoints"][0]["actual_line"] == 2
    assert "inspector_id" not in installed.json()["data"]["breakpoints"][0]
    assert str(root) not in cleared.text + installed.text
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "exited", stopped.text
    assert probe_process_identity(pid) is None
    await reopen(True)
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    assert cold.json()["data"]["status"] == "idle"
    assert cold.json()["data"]["active_configuration_id"] == configuration_id
    assert str(root) not in cold.text
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "publication.json").write_text(json.dumps({
        "initial": initial.json(), "failures": failures, "restored": restored.json(),
        "cleared": cleared.json(), "installed": installed.json(),
        "stopped": stopped.json(), "cold": cold.json(),
        "scope": "真实调试ASGI与Node/Inspector；不代Gateway、Backend进程重启、Web或Windows",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
