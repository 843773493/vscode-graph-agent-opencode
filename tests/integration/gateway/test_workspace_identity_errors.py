"""通过真实 Gateway 生命周期验证身份 CLI 的公开错误与恢复。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
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


@pytest.fixture
def identity_service_ports(request: pytest.FixtureRequest) -> Iterator[tuple[int, int]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    with ExitStack() as leases:
        backend_lease, backend_probes = E2EProcessLease.acquire_available(
            context.runtime_root_for_node(request.node.nodeid) / "backend-port.json", range(ports.start, ports.port(50)),
        )
        leases.callback(backend_lease.release)
        gateway_lease, gateway_probes = E2EProcessLease.acquire_available(
            context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", range(ports.port(50), ports.end + 1),
        )
        leases.callback(gateway_lease.release)
        (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
            "backend": backend_probes, "gateway": gateway_probes,
        }) + "\n")
        yield backend_lease.ports[0], gateway_lease.ports[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True], ids=["managed", "external"])
async def test_duplicate_native_uuid_registration_keeps_original_binding(
    request: pytest.FixtureRequest, integration_workspace_root_path: str, external: bool,
    identity_service_ports: tuple[int, int],
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    primary = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=primary)
    backend_port, gateway_port = identity_service_ports
    with ExitStack() as processes:
        backend = start_backend_process(workspace_root=str(primary), port=backend_port, log_name="identity-original")
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=primary, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=45,
        ) as client:
            await acquire_gateway_guest(client)
            listed = (await client.get("/api/gateway/workspaces", params={"check_health": False})).raise_for_status().json()
            original, = listed["data"]["items"]
            identity = primary / ".boxteam/workspace-identity.json"
            original_identity = identity.read_bytes()
            assert json.loads(original_identity)["workspace_id"] == original["backend_workspace_id"]
            duplicate = primary / f"copied-identity-{'external' if external else 'managed'}"
            (duplicate / ".boxteam").mkdir(parents=True, exist_ok=True)
            duplicate_identity = duplicate / ".boxteam/workspace-identity.json"
            # 模拟用户复制目录形成重复 UUID；首份身份与登记仍由产品入口生成。
            shutil.copyfile(identity, duplicate_identity)
            state = context.boxteam_home / "state/gateway/gateway.sqlite"
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                before = tuple(connection.execute("SELECT workspace_id,payload_json FROM gateway_workspace_registry"))
                metadata = connection.execute(
                    "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                ).fetchone()
            payload: dict[str, object] = {"root_path": str(duplicate), "name": "UUID冲突副本"}
            if external:
                payload["backend_url"] = f"http://127.0.0.1:{backend.port}"
            response = await client.post("/api/gateway/workspaces/local", json=payload,
                                         headers={"X-Request-ID": "duplicate-native-uuid"})
            assert response.status_code == 400, response.text
            assert response.json()["detail"] == (
                "工作区创建失败: stage=workspace_create, error=WorkspaceIdentityConflictError"
            )
            assert response.json()["request_id"] == response.headers["X-Request-ID"] == "duplicate-native-uuid"
            assert duplicate_identity.read_bytes() == identity.read_bytes() == original_identity
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                assert tuple(connection.execute("SELECT workspace_id,payload_json FROM gateway_workspace_registry")) == before
                assert connection.execute(
                    "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                ).fetchone() == metadata
            current = (await client.get("/api/gateway/workspaces", params={"check_health": True})).raise_for_status().json()
            restored, = current["data"]["items"]
            assert restored["workspace_id"] == original["workspace_id"]
            assert restored["backend_workspace_id"] == original["backend_workspace_id"]
            assert restored["status"] == "ready"
            assert current["data"]["active_workspace_id"] == listed["data"]["active_workspace_id"]
            (context.artifacts_dir / "duplicate-native-uuid.json").write_text(json.dumps({
                "external": external, "rejection": response.json(), "retained": current,
            }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_known_root_reregistration_preserves_missing_or_changed_identity(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    identity_service_ports: tuple[int, int],
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=root)
    backend_port, gateway_port = identity_service_ports
    with ExitStack() as processes:
        backend = start_backend_process(workspace_root=str(root), port=backend_port, log_name="known-root-backend")
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=root, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=45,
        ) as client:
            await acquire_gateway_guest(client)
            initial = (await client.get("/api/gateway/workspaces", params={"check_health": False})).raise_for_status().json()
            original, = initial["data"]["items"]
            identity = root / ".boxteam/workspace-identity.json"
            original_identity = identity.read_bytes()
            state = context.boxteam_home / "state/gateway/gateway.sqlite"
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                rows = tuple(connection.execute("SELECT workspace_id,payload_json FROM gateway_workspace_registry"))
                metadata = connection.execute(
                    "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                ).fetchone()
            results = []
            for damage in ("missing", "mismatch"):
                if damage == "missing":
                    identity.unlink()
                    damaged = None
                else:
                    damaged = b'{"workspace_id":"00000000-0000-4000-8000-000000000099"}\n'
                    identity.write_bytes(damaged)
                try:
                    for external in (False, True):
                        payload: dict[str, object] = {"root_path": str(root)}
                        if external:
                            payload["backend_url"] = f"http://127.0.0.1:{backend.port}"
                        request_id = f"known-root-{damage}-{'external' if external else 'managed'}"
                        response = await client.post("/api/gateway/workspaces/local", json=payload,
                                                     headers={"X-Request-ID": request_id})
                        assert response.status_code == (500 if damage == "missing" else 400), response.text
                        assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                        assert (identity.read_bytes() if identity.exists() else None) == damaged
                        with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                            assert tuple(connection.execute("SELECT workspace_id,payload_json FROM gateway_workspace_registry")) == rows
                            assert connection.execute(
                                "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                            ).fetchone() == metadata
                        results.append({"damage": damage, "external": external, "error": response.json()})
                finally:
                    # 修复测试注入的外部缺件／篡改；不替产品补建任何登记。
                    identity.write_bytes(original_identity)
            current = (await client.get("/api/gateway/workspaces", params={"check_health": True})).raise_for_status().json()
            retained, = current["data"]["items"]
            assert retained["workspace_id"] == original["workspace_id"]
            assert retained["backend_workspace_id"] == original["backend_workspace_id"]
            assert retained["status"] == "ready"
            assert current["data"]["active_workspace_id"] == initial["data"]["active_workspace_id"]
            (context.artifacts_dir / "known-root-reregistration.json").write_text(
                json.dumps({"attempts": results, "retained": current}, ensure_ascii=False, indent=2) + "\n",
            )


@pytest.mark.asyncio
async def test_identity_cli_failure_is_path_free_and_recovery_preserves_binding(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    identity_service_ports: tuple[int, int],
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    primary = Path(integration_workspace_root_path)
    target = primary / "managed-private-root"
    target.mkdir()
    reset_gateway_persistent_state(workspace_root=primary)
    backend_port, gateway_port = identity_service_ports
    evidence = {}
    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(primary), port=backend_port, log_name="primary-backend",
        )
        processes.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=primary, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=120,
        ) as client:
            await acquire_gateway_guest(client)
            registered = await client.post(
                "/api/gateway/workspaces/local", json={"root_path": str(target), "name": "identity-test"},
            )
            assert registered.status_code == 200, registered.text
            item = next(item for item in registered.json()["data"]["items"] if item["name"] == "identity-test")
            workspace_id = item["workspace_id"]
            backend_id = item["backend_workspace_id"]
            identity = target / ".boxteam/workspace-identity.json"
            original = identity.read_bytes()
            assert json.loads(original)["workspace_id"] == backend_id
            stopped = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "stopped"
            stopped_item = next(
                item for item in stopped.json()["data"]["workspaces"]["items"]
                if item["workspace_id"] == workspace_id
            )
            assert stopped_item["status"] == "offline"
            state = context.boxteam_home / "state/gateway/gateway.sqlite"
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                metadata_row, = connection.execute(
                    "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                ).fetchone()
            original_contracts = json.loads(metadata_row)["managed_backends"]
            owner_contract, = [
                contract for contract in original_contracts
                if any(member["workspace_id"] == workspace_id for member in contract["members"])
            ]
            expected_error = (
                f"Backend 启动失败: owner={owner_contract['owner_id']}: stage=backend_start, error=RuntimeError"
            )

            # 外部损坏仅用于触发失败；目标、绑定和停机状态全部由产品入口建立。
            processes.callback(identity.write_bytes, original)
            damaged = json.dumps({"workspace_id": str(target / "private-identity-marker")}).encode()
            identity.write_bytes(damaged)
            failed = await client.post(
                f"/api/gateway/workspaces/{workspace_id}/runtime/start",
                headers={"X-Request-ID": "workspace-identity-failed-start"},
            )
            assert failed.status_code == 502, failed.text
            assert failed.json()["detail"] == "工作区启动失败: stage=workspace_start, error=RuntimeError"
            assert failed.json()["request_id"] == failed.headers["X-Request-ID"] == "workspace-identity-failed-start"
            assert str(target) not in failed.text
            assert "private-identity-marker" not in failed.text
            assert identity.read_bytes() == damaged, "身份损坏不能静默初始化替代 UUID"
            evidence["failed-start"] = {"status": failed.status_code, "body": failed.json()}
            for health in (False, True):
                response = await client.get("/api/gateway/workspaces", params={"check_health": health})
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["request_id"] == response.headers["X-Request-ID"]
                item = next(item for item in body["data"]["items"] if item["workspace_id"] == workspace_id)
                assert item["backend_workspace_id"] == backend_id
                assert item["status"] == "offline"
                assert item["connection_error"] == expected_error
                assert item["backend_generation"] == owner_contract["generation"]
                assert item["affected_workspace_ids"] == [workspace_id]
                assert str(target) not in response.text
                assert "private-identity-marker" not in response.text
                evidence[f"failed-list-{health}"] = item

            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                row, = connection.execute(
                    "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (workspace_id,),
                ).fetchone()
                metadata_row, = connection.execute(
                    "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
                ).fetchone()
                record = json.loads(row)
                assert record["root_path"] == str(target)
                assert record["backend_workspace_id"] == backend_id
                assert record["desired_running"] is False
                assert record["connection_error"] == expected_error, "启动失败由唯一Backend owner记录"
                assert json.loads(metadata_row)["managed_backends"] == original_contracts
                assert "private-identity-marker" not in str(record["connection_error"])
                assert str(target) not in str(record["connection_error"])
                evidence["registry-after-failure"] = record

            identity.write_bytes(original)
            recovered = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/start")
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["data"]["status"] == "started"
            assert identity.read_bytes() == original
            response = await client.get("/api/gateway/workspaces", params={"check_health": True})
            assert response.status_code == 200, response.text
            item = next(item for item in response.json()["data"]["items"] if item["workspace_id"] == workspace_id)
            assert item["backend_workspace_id"] == backend_id
            assert item["status"] == "ready"
            assert item["connection_error"] is None
            evidence["recovered"] = item
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(identity) in logs
        assert "private-identity-marker" in logs
        assert "stdout=" in logs and "stderr=" in logs
        assert "Workspace backend UUID CLI 执行失败: exit=1" in logs
        assert "workspace-identity-failed-start" in logs
        (context.artifacts_dir / "managed-identity-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
