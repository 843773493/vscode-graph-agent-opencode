"""真实 Gateway、Workspace 和 Node Inspector 将源码位置统一为文件 URI。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


@pytest.mark.asyncio
async def test_native_source_urls_use_the_same_file_owner_as_file_urls(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    prepared_developer_tools_package: Path,
) -> None:
    config_path = Path(integration_workspace_config_path)
    source = commentjson.loads(config_path.read_text(encoding="utf-8"))
    ports = integration_port_block_for_file(Path(request.node.fspath))
    source.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
        "url"
    ] = f"http://127.0.0.1:{ports.port(1)}"
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
    config_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = Path(integration_workspace_root_path)
    boxteam_home = root.resolve().parent / "boxteam-home"
    outside = context.runtime_root_for_node(request.node.nodeid) / "external-source"
    outside.mkdir(parents=True)
    cases = []
    for scope, directory in (("workspace", root), ("external", outside)):
        for form in ("native", "file-uri"):
            source = directory / f"{scope}-{form}-100%.js"
            label = str(source) if form == "native" else source.as_uri()
            code = f"function uriSourceMarker() {{ debugger; return 42; }} uriSourceMarker();\n//# sourceURL={label}\n"
            source.write_text(code, encoding="utf-8")
            entry = root / f"entry-{scope}-{form}.mjs"
            entry.write_text(
                "import vm from 'node:vm';\n"
                f"vm.runInThisContext({json.dumps(code, ensure_ascii=False)});\n",
                encoding="utf-8",
            )
            cases.append((scope, form, source, code, entry))
    reset_gateway_persistent_state(workspace_root=root)
    copy_prepared_developer_tools_package(prepared_developer_tools_package, boxteam_home)
    evidence = []
    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(root), port=ports.port(0), log_name="source-url-backend",
            env_overrides={
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(1)}",
                "BOXTEAM_RUNTIME_CONTROL_TOKEN": LOCAL_TOKEN_HEADERS["X-Local-Token"],
            },
        )
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=root, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            active = workspaces.json()["data"]["active_workspace"]
            gateway_id, workspace_id = active["gateway_id"], active["workspace_id"]
            installed = await enable_developer_tools_plugins(
                client, gateway_id=gateway_id, workspace_id=workspace_id,
                plugin_ids=("debugging",),
            )
            assert installed["debugging"]["state"] == "ready"
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
            debugger = contributions.json()["data"]["debuggers"]
            assert len(debugger) == 1, debugger
            registration_id = debugger[0]["registration_id"]
            created = await client.post("/api/v1/sessions", json={"title": "Inspector 源码寻址"})
            assert created.status_code == 200, created.text
            session_id = created.json()["data"]["session_id"]
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main.status_code == 200, main.text
            owner = {"session_id": session_id, "thread_id": main.json()["data"]["thread_id"]}
            for scope, form, source, code, entry in cases:
                configured = await client.post("/api/v1/debug/node/configurations", json={
                    **owner, "name": f"{scope}-{form}", "script_path": str(entry),
                })
                assert configured.status_code == 200, configured.text
                configuration_id = configured.json()["data"]["active_configuration_id"]
                assert configuration_id
                started = await client.post("/api/v1/debug/node/start", json={
                    **owner, "path": str(entry), "configuration_id": configuration_id,
                    "debugger_registration_id": registration_id,
                })
                assert started.status_code == 200, started.text
                try:
                    state = started.json()["data"]
                    evidence.append({"scope": scope, "form": form, "state": state})
                    (context.artifacts_dir / "source-url-states.json").write_text(
                        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                    )
                    assert state["status"] == "paused", state
                    assert state["script_path"] == entry.name
                    assert state["active_configuration_id"] == configuration_id
                    top = state["call_stack"][0]
                    assert top["function_name"] == "uriSourceMarker"
                    assert top["url"].startswith("boxteam://"), top
                    assert top["path"] == (source.name if scope == "workspace" else None)
                    assert (top["line"], top["column"]) == (1, 30)
                    assert state["last_stopped_frame"]["url"] == top["url"]
                    assert state["last_stopped_frame"]["path"] == top["path"]
                    serialized = json.dumps([state["call_stack"], state["last_stopped_frame"]])
                    assert str(root) not in serialized and str(outside) not in serialized
                    assert "file:" not in serialized
                    reference = await client.post(
                        "/api/v1/workspace/files/references", json={"paths": [str(source)]},
                    )
                    assert reference.status_code == 200, reference.text
                    assert reference.json()["data"]["uris"] == [top["url"]]
                    content = await client.get("/api/v1/workspace/files/content", params={"path": top["url"]})
                    assert content.status_code == 200, content.text
                    assert content.json()["data"]["content"] == code
                    read = await client.get("/api/v1/debug/node", params=owner)
                    assert read.status_code == 200, read.text
                    assert read.json()["request_id"] == read.headers["X-Request-ID"]
                    assert read.json()["data"]["call_stack"][0]["url"] == top["url"]
                finally:
                    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
                    assert stopped.status_code == 200, stopped.text
                    assert stopped.json()["data"]["status"] == "exited"
