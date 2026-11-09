"""真实 Gateway 启动失败、持久动作冷读与同一配置重试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


async def _set_node_executable(
    client: httpx.AsyncClient, *, gateway_id: str, workspace_id: str, executable: str,
) -> None:
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    source = commentjson.loads(document["jsonc"])
    source.setdefault("runtime", {}).setdefault("debug", {}).setdefault("node", {})[
        "executable"
    ] = executable
    saved = await client.put(path, headers=headers, json={
        "gateway_id": gateway_id, "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(source, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": f"node-executable-{uuid4().hex}",
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["edit"]["state"] == "pending_restart", saved.text


@pytest.mark.asyncio
async def test_missing_node_executable_preserves_safe_history_and_retries_after_restart(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    prepared_debug_plugin_package: Path,
) -> None:
    ports = integration_port_block_for_file(Path(request.node.fspath))
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    gateway_root = Path(integration_workspace_root_path)
    root = prepare_default_test_workspace(
        workspace_root=gateway_root.parent / "runtime/managed-workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    config_path = install_test_workspace_config(
        workspace_root=root,
        config_path=Path(integration_workspace_config_path),
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    source = commentjson.loads(config_path.read_text(encoding="utf-8"))
    boxteam_home = gateway_root.resolve().parent / "boxteam-home"
    missing = context.runtime_root_for_node(request.node.nodeid) / "missing-node"
    assert not missing.exists()
    (root / "entry.mjs").write_text("setInterval(() => {}, 1000);\n")
    source.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
        "url"
    ] = f"http://127.0.0.1:{ports.port(1)}"
    source.setdefault("runtime", {}).setdefault("debug", {}).setdefault("node", {})[
        "executable"
    ] = str(missing)
    # 本场景不调用模型；显式提供配置依赖，避免借用宿主凭据。
    source["llm"]["providers"] = [{
        "id": "primary", "endpoint": "http://127.0.0.1:1/v1", "model": "unused-debug-model",
        "api_key": "unused-debug-test-key", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions",
            "model_info": {"supports_function_calling": True, "supports_reasoning": False},
            "supports_reasoning": {"reasoning_content": False}},
    }]
    for agent in source["agents"].values():
        if "model" in agent:
            agent["model"] = {"primary_provider": "primary", "fallback_providers": []}
    source["$schema"] = "./workspace_schema.jsonc"
    source["config_version"] = 2
    dummy_config = json.dumps(source, ensure_ascii=False, indent=2) + "\n"
    config_path.write_text(dummy_config, encoding="utf-8")
    Path(integration_workspace_config_path).write_text(dummy_config, encoding="utf-8")
    reset_gateway_persistent_state(workspace_root=gateway_root)
    copy_prepared_developer_tools_package(prepared_debug_plugin_package, boxteam_home)
    home_config = boxteam_home / "config"
    home_config.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, home_config / "workspace.jsonc")
    shutil.copy2(
        Path.cwd() / "configs/workspace_schema.jsonc",
        home_config / "workspace_schema.jsonc",
    )
    evidence = {}
    gateway = start_gateway_process(
        workspace_root=gateway_root, default_backend_url=None, port=ports.port(1),
        boxteam_home=boxteam_home, artifacts_dir=context.artifacts_dir,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            gateway_id = health.json()["data"]["gateway_id"]
            registered = await client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(root), "name": "Node debug launch errors", "create_directory": False,
            }, timeout=90)
            assert registered.status_code == 200, registered.text
            item = next(
                item for item in registered.json()["data"]["items"]
                if item["gateway_id"] == gateway_id and item["name"] == "Node debug launch errors"
            )
            workspace_id = item["workspace_id"]
            enabled = await enable_developer_tools_plugins(
                client, gateway_id=gateway_id, workspace_id=workspace_id,
                plugin_ids=("debugging",), artifacts_dir=context.artifacts_dir / "debug-plugin",
                prepared_package=True,
            )
            assert enabled["debugging"]["state"] == "ready"
            client.headers.update({
                "X-BoxTeam-Target-Gateway-Id": gateway_id,
                "X-BoxTeam-Gateway-Id": gateway_id,
                "X-BoxTeam-Workspace-Id": workspace_id,
            })
            contributions = await client.get(
                "/api/v1/debug/node/debugger-contributions",
                params={"workspace_id": workspace_id},
            )
            assert contributions.status_code == 200, contributions.text
            debuggers = contributions.json()["data"]["debuggers"]
            assert len(debuggers) == 1, debuggers
            registration_id = debuggers[0]["registration_id"]
            created = await client.post("/api/v1/sessions", json={"title": "调试启动错误"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main.status_code == 200, main.text
            owner = {"session_id": session_id, "thread_id": main.json()["data"]["thread_id"]}
            configured = await client.post("/api/v1/debug/node/configurations", json={
                **owner, "name": "失败后重试原方案", "script_path": "entry.mjs",
            })
            assert configured.status_code == 200, configured.text
            configuration_id = configured.json()["data"]["active_configuration_id"]
            payload = {**owner, "path": "entry.mjs", "configuration_id": configuration_id,
                       "debugger_registration_id": registration_id}
            failed = await client.post("/api/v1/debug/node/start", json=payload)
            assert failed.status_code == 409, failed.text
            assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
            assert "ENOENT" in failed.text and str(missing) in failed.text
            evidence["failed"] = failed.json()
            state_response = await client.get("/api/v1/debug/node", params=owner)
            assert state_response.status_code == 200, state_response.text
            state = state_response.json()["data"]
            assert state["status"] == "failed" and state["pid"] is None
            failures = [action for action in state["actions"] if action["action"] == "start_failed"]
            assert len(failures) == 1 and failures[0]["result"] == "error"
            assert failures[0]["message"] == failed.json()["detail"] == state["error_message"]
            store = NodeDebugSessionStore(get_session_path_resolver(root / ".boxteam/sessions"))
            manifest_bytes, manifest = store.read_manifest_payload(session_id, owner["thread_id"])
            assert manifest_bytes is not None and manifest is not None
            assert [action.model_dump(mode="json") for action in manifest.actions if action.action == "start_failed"] == failures
            claim = store.read_launch_claim(session_id, owner["thread_id"])
            assert claim is not None and claim.phase == "settled" and claim.pid is None
            evidence["live"] = state_response.json()
            evidence["manifest"] = manifest.model_dump(mode="json")
            (context.artifacts_dir / "failed-manifest.json").write_bytes(manifest_bytes)
            executable = shutil.which("node")
            assert executable is not None
            await _set_node_executable(
                client, gateway_id=gateway_id, workspace_id=workspace_id, executable=executable,
            )
            restart = await client.post(
                f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe",
                json=None,
                timeout=150,
            )
            assert restart.status_code == 200, restart.text
            cold = await client.get("/api/v1/debug/node", params=owner)
            assert cold.status_code == 200, cold.text
            restored = cold.json()["data"]
            assert restored["status"] == "idle" and restored["pid"] is None
            assert restored["active_configuration_id"] == configuration_id
            assert [action for action in restored["actions"] if action["action"] == "start_failed"] == failures
            evidence["cold"] = cold.json()
            contributions = await client.get(
                "/api/v1/debug/node/debugger-contributions",
                params={"workspace_id": workspace_id},
            )
            assert contributions.status_code == 200, contributions.text
            current_debuggers = contributions.json()["data"]["debuggers"]
            assert len(current_debuggers) == 1, current_debuggers
            current_debugger = current_debuggers[0]
            retried = await client.post(
                "/api/v1/debug/node/start",
                json={**payload, "debugger_registration_id": current_debugger["registration_id"]},
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["data"]["status"] == "running"
            assert retried.json()["data"]["active_configuration_id"] == configuration_id
            assert retried.json()["data"]["pid"] is not None
            evidence["retried"] = retried.json()
            stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "exited"
            claim = store.read_launch_claim(session_id, owner["thread_id"])
            assert claim is not None and claim.phase == "settled"
            evidence["stopped"] = stopped.json()
            (context.artifacts_dir / "launch-errors.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            )
    finally:
        close_gateway_process(gateway)
