"""真实 Gateway 启动失败、持久动作冷读与同一配置重试。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

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
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.mark.asyncio
async def test_missing_node_executable_preserves_safe_history_and_retries_after_restart(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = Path(integration_workspace_root_path)
    missing = context.runtime_root_for_node(request.node.nodeid) / "native-launch-secret" / "missing-node"
    assert not missing.exists()
    (root / "entry.mjs").write_text("setInterval(() => {}, 1000);\n")
    ports = integration_port_block_for_file(Path(request.node.fspath))
    reset_gateway_persistent_state(workspace_root=root)
    evidence = {}
    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(root), port=ports.port(0), log_name="launch-error-backend",
            env_overrides={
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(1)}",
                "BOXTEAM_NODE_BIN": str(missing),
            },
        )
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=root, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
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
            payload = {**owner, "path": "entry.mjs", "configuration_id": configuration_id}
            failed = await client.post("/api/v1/debug/node/start", json=payload)
            assert failed.status_code == 409, failed.text
            assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
            assert "FileNotFoundError" in failed.text and "errno=2" in failed.text
            assert str(missing) not in failed.text and "native-launch-secret" not in failed.text
            evidence["failed"] = failed.json()
            state_response = await client.get("/api/v1/debug/node", params=owner)
            assert state_response.status_code == 200, state_response.text
            state = state_response.json()["data"]
            assert state["status"] == "failed" and state["pid"] is None
            failures = [action for action in state["actions"] if action["action"] == "start_failed"]
            assert len(failures) == 1 and failures[0]["result"] == "error"
            assert failures[0]["message"] == failed.json()["detail"] == state["error_message"]
            assert "native-launch-secret" not in state_response.text
            store = NodeDebugSessionStore(get_session_path_resolver(root / ".boxteam/sessions"))
            manifest_bytes, manifest = store.read_manifest_payload(session_id, owner["thread_id"])
            assert manifest_bytes is not None and manifest is not None
            assert "native-launch-secret" not in manifest_bytes.decode()
            assert [action.model_dump(mode="json") for action in manifest.actions if action.action == "start_failed"] == failures
            claim = store.read_launch_claim(session_id, owner["thread_id"])
            assert claim is not None and claim.phase == "settled" and claim.pid is None
            evidence["live"] = state_response.json()
            evidence["manifest"] = manifest.model_dump(mode="json")
            (context.artifacts_dir / "failed-manifest.json").write_bytes(manifest_bytes)
            stderr_path = Path(backend.stderr_file.name)
            stdout_path = Path(backend.stdout_file.name)
            close_backend_process(backend)
            diagnostics = stderr_path.read_text() + stdout_path.read_text()
            assert str(missing) in diagnostics and "FileNotFoundError" in diagnostics

            backend = start_backend_process(
                workspace_root=str(root), port=ports.port(0), log_name="launch-retry-backend",
                env_overrides={"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(1)}"},
                env_unset=("BOXTEAM_NODE_BIN",),
            )
            processes.callback(close_backend_process, backend)
            cold = await client.get("/api/v1/debug/node", params=owner)
            assert cold.status_code == 200, cold.text
            restored = cold.json()["data"]
            assert restored["status"] == "idle" and restored["pid"] is None
            assert restored["active_configuration_id"] == configuration_id
            assert [action for action in restored["actions"] if action["action"] == "start_failed"] == failures
            assert "native-launch-secret" not in cold.text
            evidence["cold"] = cold.json()
            retried = await client.post("/api/v1/debug/node/start", json=payload)
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
