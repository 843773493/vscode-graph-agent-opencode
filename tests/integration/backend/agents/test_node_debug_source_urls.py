"""真实 Gateway、Workspace 和 Node Inspector 将源码位置统一为文件 URI。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.mark.asyncio
async def test_native_source_urls_use_the_same_file_owner_as_file_urls(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = Path(integration_workspace_root_path)
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
    ports = integration_port_block_for_file(Path(request.node.fspath))
    reset_gateway_persistent_state(workspace_root=root)
    evidence = []
    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(root), port=ports.port(0), log_name="source-url-backend",
            env_overrides={"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(1)}"},
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
            await acquire_gateway_guest(client)
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
