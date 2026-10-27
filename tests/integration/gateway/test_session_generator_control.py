"""通过真实 Gateway 显式移交旧定义；新任务始终归工作区 owner。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from configs.runtime import read_jsonc_object
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


@pytest.fixture(scope="module")
def integration_config_path(integration_workspace_root_path: str) -> str:
    """迁移只读写任务，不调用模型，也不依赖开发机的 Provider 凭据。"""
    configuration = read_jsonc_object(Path.cwd() / "configs/tests/workspace/default.jsonc")
    llm = configuration["llm"]
    assert isinstance(llm, dict)
    for provider in llm["providers"]:
        provider["api_key"] = "migration-test-unused-key"
        provider["endpoint"] = "http://127.0.0.1:1/v1"
    path = Path(integration_workspace_root_path).parent / "runtime/workspace_migration.jsonc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return str(path)


@pytest.fixture
def migration_gateway(request, integration_workspace_root_path):
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    home = workspace.parent / "boxteam-home"
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None,
                                    port=integration_port_block_for_file(Path(request.node.fspath)).port(0),
                                    boxteam_home=home, artifacts_dir=workspace.parent / "artifacts/gateway")
    try:
        yield gateway, workspace, home / "state/gateway"
    finally:
        close_gateway_process(gateway)


@pytest.mark.asyncio
async def test_generator_import_uses_workspace_owner_and_does_not_overwrite_later_edits(migration_gateway):
    gateway, workspace, control_root = migration_gateway
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
        await acquire_gateway_guest(client)
        targets = (await client.get("/api/gateway/workspaces")).json()["data"]
        alias = targets["active_workspace_id"]
        client.headers["X-BoxTeam-Workspace-Id"] = alias
        owner = (await client.get("/api/v1/workspace")).json()["data"]["workspace_id"]
        now = datetime.now(UTC).isoformat()
        definition = {
            "generator_id": "gen_migrated", "revision": 9, "name": "旧任务", "enabled": True,
            "trigger": {"type": "manual"}, "placement": {"kind": "workspace", "workspace_id": alias},
            "execution_workspace_id": alias, "config": {"prompt": "等待用户手动触发"},
            "created_at": now, "updated_at": now,
        }
        path = control_root / "generators/gen_migrated.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(definition, ensure_ascii=False).encode()
        path.write_bytes(original)
        endpoint = "/api/gateway/migrations/session-generators"
        preview = await client.post(endpoint, params={"preview": "true"})
        assert preview.status_code == 200, preview.text
        planned, = preview.json()["data"]
        assert planned["status"] == "planned" and path.read_bytes() == original
        assert (await client.get("/api/v1/persistent-tasks")).json()["data"]["items"] == []
        installed = await client.post(endpoint, params={"preview": "false"})
        assert installed.status_code == 200, installed.text
        assert installed.headers["X-Request-ID"] == installed.json()["request_id"]
        assert installed.json()["data"] == [{**planned, "status": "imported"}]
        task_response = await client.get("/api/v1/persistent-tasks/gen_migrated")
        assert task_response.status_code == 200, task_response.text
        task = task_response.json()["data"]
        assert task["workspace_id"] == owner and task["revision"] == 9
        assert task["configuration"]["action"]["type_id"] == "builtin.session_generation"
        assert task["configuration"]["action"]["generation"]["placement"]["workspace_id"] == owner
        assert task["configuration"]["condition"]["type_id"] == "builtin.always"
        assert (await client.get("/api/v1/persistent-tasks/gen_migrated/runs")).json()["data"]["items"] == []
        assert not path.exists()
        assert (control_root / "migration-backups/session-generators" / planned["source_hash"] / "generators/gen_migrated.json").read_bytes() == original
        configuration = task["configuration"]
        configuration["name"] = "已在工作区编辑"
        edited = await client.put("/api/v1/persistent-tasks/gen_migrated", json={"expected_revision": 9, "configuration": configuration})
        assert edited.status_code == 200, edited.text
        repeated = await client.post(endpoint, params={"preview": "false"})
        assert repeated.status_code == 200 and repeated.json()["data"] == installed.json()["data"]
        assert (await client.get("/api/v1/persistent-tasks/gen_migrated")).json()["data"] == edited.json()["data"]
        assert (workspace / ".boxteam/state/workspace.sqlite").is_file()
        assert not (workspace / ".boxteam/generation-runs").exists()
        assert (await client.get("/api/gateway/session-generators")).status_code == 404
