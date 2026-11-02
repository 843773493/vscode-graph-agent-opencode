"""真实调试状态发布拒绝原生位置，失败不改变进程和持久调试事实。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_core import PydanticSerializationError

from app.agents.tools.debugging.payloads import _success
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugStateDTO,
)
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
        "working_directory": str(root),
        "breakpoints": [{"path": str(source), "line": 1}],
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    started = await client.post("/api/v1/debug/node/start", json={
        **owner, "configuration_id": configuration_id, "path": str(source),
        "working_directory": str(root),
    })
    assert started.status_code == 200, started.text
    initial = await client.get("/api/v1/debug/node", params=owner)
    assert initial.status_code == 200, initial.text
    data = initial.json()["data"]
    assert data["status"] == "paused" and data["call_stack"][0]["line"] == 1
    assert data["script_path"] == data["breakpoints"][0]["path"] == source.name
    assert data["configurations"][0]["script_path"] == source.name
    assert data["working_directory"] == "" and data["source_changed_paths"] == []
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
    cases = [(container, field) for container in ("call_stack", "last_stopped_frame")
             for field in ("url", "path")]
    cases.extend([("state", "script_path"), ("state", "working_directory"),
                  ("state", "source_changed_paths"), ("breakpoints", "path"),
                  ("configurations", "script_path")])
    for container, field in cases:
        for native in (str(source), r"C:\private\inspector-secret.mjs",
                       r"\\target\private\inspector-secret.mjs", source.as_uri()):
            snapshots: list[NodeDebugStateDTO] = []

            async def damaged_state(
                service: NodeDebugService, session_id: str, thread_id: str | None = None,
                *, field: str = field, native: str = native, container: str = container,
                snapshots: list[NodeDebugStateDTO] = snapshots,
            ) -> NodeDebugStateDTO:
                state = await read_state(service, session_id, thread_id)
                if container == "call_stack":
                    state.call_stack[0] = state.call_stack[0].model_copy(update={field: native})
                elif container == "last_stopped_frame":
                    assert state.last_stopped_frame is not None
                    state.last_stopped_frame = state.last_stopped_frame.model_copy(update={field: native})
                elif container == "breakpoints":
                    state.breakpoints[0] = state.breakpoints[0].model_copy(update={field: native})
                elif container == "configurations":
                    state.configurations[0] = state.configurations[0].model_copy(update={field: native})
                else:
                    state = state.model_copy(update={field: [native] if field == "source_changed_paths" else native})
                snapshots.append(state)
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
                _success("调试状态", snapshots[0])
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
    assert cold.json()["data"]["script_path"] == source.name
    assert cold.json()["data"]["working_directory"] == ""
    assert cold.json()["data"]["configurations"] == stopped.json()["data"]["configurations"]
    assert str(root) not in cold.text
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "publication.json").write_text(json.dumps({
        "initial": initial.json(), "failures": failures, "restored": restored.json(),
        "cleared": cleared.json(), "installed": installed.json(),
        "stopped": stopped.json(), "cold": cold.json(),
        "scope": "真实调试ASGI与Node/Inspector；不代Gateway、Backend进程重启、Web或Windows",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_configuration_input_publication_and_cold_read_share_target_resolution(
    debug_receive_client: DebugReceiveClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    source = root / "entry.mjs"
    worker = root / "lib/worker.mjs"
    worker.parent.mkdir()
    worker.write_text("export const value = 42;\n", encoding="utf-8")
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "方案正文发布", "script_path": str(source),
        "working_directory": str(root), "breakpoints": [{"path": str(worker), "line": 1}],
    })
    assert configured.status_code == 200, configured.text
    original_id = configured.json()["data"]["active_configuration_id"]
    exported = await client.get(f"/api/v1/debug/node/configurations/{original_id}", params=owner)
    assert exported.status_code == 200, exported.text
    original = exported.json()["data"]
    assert original["script_path"] == "entry.mjs" and original["working_directory"] == ""
    assert original["breakpoints"][0]["path"] == "lib/worker.mjs"
    assert str(root) not in exported.text
    imported_id = create_prefixed_id("dbgcfg")
    incoming = {**original, "configuration_id": imported_id, "name": "原生路径导入", "script_path": str(source),
                "working_directory": str(root),
                "breakpoints": [{**original["breakpoints"][0], "path": str(worker)}]}
    imported = await client.post("/api/v1/debug/node/configurations/import", json={
        **owner, "configuration": incoming,
    })
    assert imported.status_code == 200, imported.text
    assert imported.json()["data"]["active_configuration_id"] == original_id
    result = await client.get(f"/api/v1/debug/node/configurations/{imported_id}", params=owner)
    assert result.status_code == 200, result.text
    body = result.json()["data"]
    assert body["script_path"] == "entry.mjs" and body["working_directory"] == ""
    assert body["breakpoints"][0]["path"] == "lib/worker.mjs"
    assert str(root) not in result.text
    await reopen(True)
    manifest_before, manifest = store.read_manifest_payload(**owner)
    assert manifest_before is not None and manifest is not None
    assert set(manifest.configuration_ids) == {original_id, imported_id}
    registered = store.read_registered_configuration_payloads(**owner, configuration_ids=manifest.configuration_ids)
    assert {key: value[0].model_dump(mode="json") for key, value in registered.items()} == {
        original_id: original, imported_id: body,
    }
    path = store._configuration_path(configuration_id=imported_id, **owner)
    original_bytes = path.read_bytes()
    read_configuration = NodeDebugService.get_configuration
    failures: list[dict[str, object]] = []
    for field in ("script_path", "working_directory", "breakpoints"):
        for native in (str(source), r"C:\private\configuration-secret.mjs",
                       r"\\target\private\configuration-secret.mjs", source.as_uri()):
            def damaged_configuration(
                service: NodeDebugService, session_id: str, configuration_id: str, thread_id: str | None = None,
                *, field: str = field, native: str = native,
            ) -> NodeDebugConfigurationDTO:
                configuration = read_configuration(service, session_id, configuration_id, thread_id).model_copy(deep=True)
                if field == "breakpoints":
                    configuration.breakpoints[0] = configuration.breakpoints[0].model_copy(update={"path": native})
                    return configuration
                return configuration.model_copy(update={field: native})

            with monkeypatch.context() as patch:
                patch.setattr(NodeDebugService, "get_configuration", damaged_configuration)
                rejected = await client.get(f"/api/v1/debug/node/configurations/{imported_id}", params=owner)
            assert rejected.status_code == 500, rejected.text
            assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"]
            assert native not in rejected.text and "PydanticSerializationError" in rejected.text
            # 直接复用同一已解析方案副本，核对原 writer 也拒绝发布。
            candidate = registered[imported_id][0].model_copy(deep=True)
            if field == "breakpoints":
                candidate.breakpoints[0] = candidate.breakpoints[0].model_copy(update={"path": native})
            else:
                candidate = candidate.model_copy(update={field: native})
            with pytest.raises(PydanticSerializationError):
                store.write_configuration(configuration=candidate, **owner)
            assert path.read_bytes() == original_bytes
            assert store.read_manifest_payload(**owner)[0] == manifest_before
            await reopen(True)
            payload = json.loads(original_bytes)
            if field == "breakpoints":
                payload[field][0]["path"] = native
            else:
                payload[field] = native
            corrupted_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            path.write_bytes(corrupted_bytes)
            try:
                cold = await client.get("/api/v1/debug/node", params=owner)
                assert cold.status_code == 500, cold.text
                assert cold.json()["request_id"] == cold.headers["X-Request-ID"]
                assert native not in cold.text and "RuntimeError" in cold.text
                assert path.read_bytes() == corrupted_bytes
                with pytest.raises(RuntimeError, match="会话源码调试数据损坏"):
                    store.read_registered_configuration_payloads(**owner, configuration_ids=manifest.configuration_ids)
                assert store.read_manifest_payload(**owner)[0] == manifest_before
            finally:
                path.write_bytes(original_bytes)
            restored = await client.get(f"/api/v1/debug/node/configurations/{imported_id}", params=owner)
            assert restored.status_code == 200 and restored.json()["data"] == body, restored.text
            assert path.read_bytes() == original_bytes
            failures.append({"field": field, "publication": rejected.json(), "cold": cold.json()})
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "configuration-publication.json").write_text(json.dumps({
        "configured": configured.json(), "original": exported.json(), "imported": imported.json(),
        "result": result.json(), "failures": failures, "restored": restored.json(),
        "scope": "真实调试ASGI配置／导入及原存储；不启动Node，不代Gateway或Backend硬重启",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
