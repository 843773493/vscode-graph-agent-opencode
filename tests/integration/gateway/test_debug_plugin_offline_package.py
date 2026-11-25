"""验证开发工具离线包中的 Node SDK 经 Gateway 完成真实 Debug 生命周期。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import copy_prepared_developer_tools_package
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


def _data(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()["data"]


def test_debug_plugin_runs_from_prepared_offline_package(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    unused_tcp_port: int,
    prepared_developer_tools_package: Path,
) -> None:
    project_root = Path.cwd().resolve()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    template = project_root / "tests/fixtures/workspaces/default_test_workspace"
    gateway_workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "gateway-default",
        template_root=template,
        template_items=(),
    )
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "debug-workspace",
        template_root=template,
        template_items=(),
    )
    for root in (gateway_workspace, workspace):
        install_test_workspace_config(
            workspace_root=root,
            config_path=project_root / "configs/tests/workspace/default.jsonc",
            schema_path=project_root / "configs/workspace_schema.jsonc",
        )
        config_path = root / ".boxteam/workspace.jsonc"
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config["settings"]["workspace"].setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
            "url"
        ] = f"http://127.0.0.1:{unused_tcp_port}"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    boxteam_home = context.boxteam_home_for_node(request.node.nodeid)
    copy_prepared_developer_tools_package(prepared_developer_tools_package, boxteam_home)
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path),
        default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=boxteam_home,
        artifacts_dir=context.artifacts_dir,
    )
    evidence: list[dict[str, object]] = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=45,
            trust_env=False,
        ) as client:
            gateway_id = _data(client.get("/api/gateway/health"))["gateway_id"]
            managed = _data(client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(workspace),
                "name": "离线 SDK Debug 验收",
                "create_directory": False,
            }, timeout=75))
            workspace_id = next(
                item["workspace_id"] for item in managed["items"]
                if item["gateway_id"] == gateway_id and item["name"] == "离线 SDK Debug 验收"
            )
            headers = {"X-BoxTeam-Workspace-Id": workspace_id}
            source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
            document = _data(client.get(source_path, headers=headers))
            config = commentjson.loads(document["jsonc"])
            config["settings"]["workspace"].setdefault("plugins", {})["runtime"] = {"entries": [{
                "package_id": "boxteam.developer-tools",
                "package_version": "0.1.1",
                "plugin_id": "debugging",
                "enabled": True,
            }]}
            edited = _data(client.put(source_path, headers=headers, json={
                "gateway_id": gateway_id,
                "workspace_id": workspace_id,
                "source_key": "workspace_root_mutable_override",
                "jsonc": json.dumps(config, ensure_ascii=False),
                "base_file_digest": document["file_digest"],
                "base_layer_revision": document["layer_revision"],
                "base_layer_digest": document["layer_digest"],
                "expected_active_revision": document["active_revision"],
                "expected_active_digest": document["active_digest"],
                "idempotency_key": "offline-debug-sdk-bind",
            }))
            assert edited["edit"]["state"] == "active"

            deadline = time.monotonic() + 30
            plugin: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                status = _data(client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id}))
                plugin = next((item for item in status["plugins"]
                    if item["binding"].get("workspace_id") == workspace_id
                    and item["binding"]["plugin_id"] == "debugging"), None)
                if plugin is not None and plugin["state"] in {"ready", "failed"}:
                    break
                time.sleep(0.1)
            assert plugin is not None and plugin["state"] == "ready", plugin

            script = workspace / "offline-debug.mjs"
            script.write_text(
                "const state = { counter: 1 };\n"
                "state.counter += 2;\n"
                "state.counter += 3;\n"
                "console.log('offline-debug-result', state.counter);\n"
                "setInterval(() => {}, 1000);\n",
                encoding="utf-8",
            )
            owner = _data(client.post("/api/v1/sessions", headers=headers, json={
                "title": "离线 SDK Debug 验收",
            }))
            owner = {"session_id": owner["session_id"], "thread_id": owner["thread_id"]}
            configuration = _data(client.post("/api/v1/debug/node/configurations", headers=headers, json={
                **owner,
                "name": "离线 SDK Debug",
                "script_path": "offline-debug.mjs",
                "working_directory": ".",
                "breakpoints": [{"path": "offline-debug.mjs", "line": 2}],
            }))
            run = _data(client.post("/api/v1/debug/node/start", headers=headers, json={
                **owner,
                "path": "offline-debug.mjs",
                "configuration_id": configuration["active_configuration_id"],
            }))
            assert run["status"] == "paused" and run["call_stack"][0]["line"] == 2, run
            assert run["debugger_binding"]["instance_id"] == plugin["instance"]["instance_id"]
            assert Path(psutil.Process(run["pid"]).exe()).resolve() == Path(
                psutil.Process(plugin["instance"]["process_id"]).exe()
            ).resolve()

            evaluated = _data(client.post("/api/v1/debug/node/action", headers=headers, json={
                **owner,
                "action": "evaluate",
                "params": {"expression": "state.counter += 10"},
            }))
            assert evaluated["last_evaluation"]["value"] == "11"
            stepped = _data(client.post("/api/v1/debug/node/action", headers=headers, json={
                **owner,
                "action": "step_over",
            }))
            assert stepped["call_stack"][0]["line"] == 3
            _data(client.post("/api/v1/debug/node/action", headers=headers, json={**owner, "action": "continue"}))

            deadline = time.monotonic() + 10
            while True:
                observed = _data(client.get("/api/v1/debug/node", headers=headers, params=owner))
                if "offline-debug-result 16" in observed["output"]:
                    break
                assert time.monotonic() < deadline, observed
                time.sleep(0.1)
            stopped = _data(client.post("/api/v1/debug/node/action", headers=headers, json={**owner, "action": "stop"}))
            assert stopped["status"] == "exited" and not psutil.pid_exists(run["pid"]), stopped
            evidence.append({"gateway_id": gateway_id, "workspace_id": workspace_id,
                "instance_id": plugin["instance"]["instance_id"], "debug_run_id": run["run_id"],
                "debug_pid": run["pid"], "output": observed["output"]})
    finally:
        (context.artifacts_dir / "offline-debug-lifecycle.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        close_gateway_process(gateway)
