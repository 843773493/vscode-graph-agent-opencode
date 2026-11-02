"""真实 PTY 展示元数据允许原生路径，显式 runtime cwd 合同仍由 owner 校验。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest

from app import main
from app.schemas.internal_v2.session_resource import (
    SessionResourceAction,
    SessionResourceDTO,
)
from app.schemas.internal_v2.workspace_resource import WorkspaceResourceDTO
from app.services.mapping.session_resource_mapper import SessionResourceMapper
from tests.integration.backend.sessions.test_attachment_directory_shell import (
    process_running,
)
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前文件路径隔离真实 Terminal。
)


@pytest.fixture
async def publication_client(source_case: RuntimeSourceCase) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
        base_url="http://workspace.test", headers=source_case.client.headers,
    ) as client:
        yield client


@pytest.mark.parametrize("source_case", [{"terminal": True, "browser": True, "managed_gateway": True}], indirect=True)
async def test_resource_publication_failure_keeps_same_pty_and_owner_facts(
    source_case: RuntimeSourceCase, publication_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    case, client = source_case, publication_client
    session_id, thread_id, _ = await _target(case)
    directory = case.context.runtime_root_for_node(request.node.nodeid) / "outside-workspace"
    directory.mkdir()
    relative = os.path.relpath(directory.resolve(), case.workspace)
    created = await client.post("/api/v1/resources/terminals", json={
        "session_id": session_id, "thread_id": thread_id, "retention_scope": "workspace",
        "cwd_relative": str(directory.resolve()), "title": "元数据发布校验",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    assert resource["metadata"]["cwd_relative"] == relative
    rid, raw_pid = resource["resource_id"], resource["metadata"]["os_pid"]
    # metadata 经 Protobuf Struct 传递数值；核对无损整数值，再用原生 PID 检查同一进程。
    assert type(raw_pid) in {int, float} and raw_pid > 0
    pid = int(raw_pid)
    assert pid == raw_pid
    os.kill(pid, 0)
    owner = case.container.terminal_resource_owner
    detail = f"/api/v1/resources/{rid}"
    session_list = f"/api/v1/sessions/{session_id}/resources"
    routes = (detail, "/api/v1/resources?kind=terminal", session_list)
    for route in routes:
        response = await client.get(route)
        assert response.status_code == 200, response.text
    facts = owner._store.path.read_bytes()
    ledger_path = owner._leases._state_path
    assert ledger_path is not None
    leases = ledger_path.read_bytes()
    original_projection = WorkspaceResourceDTO.from_owner
    original_mapping = SessionResourceMapper.terminal_to_resource
    failures: list[dict[str, object]] = []
    for native in (str(directory.resolve()), r"C:\private\runtime-secret",
                   r"\\target\private\runtime-secret", directory.resolve().as_uri()):
        def damaged_projection(
            cls: type[WorkspaceResourceDTO], snapshot: dict[str, object], *, native: str = native,
        ) -> WorkspaceResourceDTO:
            result = original_projection(snapshot)
            result.metadata["nested"] = {"checkpoint_path": native}
            return result

        def damaged_mapping(
            mapper: SessionResourceMapper, terminal: dict[str, object], *, available_actions: list[SessionResourceAction],
            native: str = native,
        ) -> SessionResourceDTO:
            result = original_mapping(mapper, terminal, available_actions=available_actions)
            result.metadata["log_path"] = native
            return result

        with monkeypatch.context() as patch:
            # 只补充原入口产生后的展示信息，不代建 PTY、Session、关联或 lease。
            patch.setattr(WorkspaceResourceDTO, "from_owner", classmethod(damaged_projection))
            patch.setattr(SessionResourceMapper, "terminal_to_resource", damaged_mapping)
            for route in routes:
                rejected = await client.get(route, headers={"X-Request-ID": "resource-publication-rejected"})
                assert rejected.status_code == 200, rejected.text
                published = rejected.json()["data"]
                if route == detail:
                    assert published["metadata"]["nested"]["checkpoint_path"] == native
                elif route == session_list:
                    assert next(item for item in published["items"] if item["resource_id"] == rid)["metadata"]["log_path"] == native
                else:
                    assert next(item for item in published["items"] if item["resource_id"] == rid)["metadata"]["nested"]["checkpoint_path"] == native
                assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == "resource-publication-rejected"
                failures.append({"route": route, "response": rejected.json()})
                os.kill(pid, 0)
                assert owner._store.path.read_bytes() == facts and ledger_path.read_bytes() == leases

    # 原生清单也必须拒绝 file URL；不能先发布该观察或改变持久资源状态。
    listing = owner._transport.list_resources
    async def damaged_listing() -> list[dict[str, object]]:
        result = await listing()
        result[0]["cwd_relative"] = directory.resolve().as_uri()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(owner._transport, "list_resources", damaged_listing)
        rejected = await client.get(detail)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"] == "ValueError: Node 运行时 工作目录不是工作区内相对目录"
    assert directory.resolve().as_uri() not in rejected.text
    assert owner._store.path.read_bytes() == facts and ledger_path.read_bytes() == leases
    for route in routes:
        restored = await client.get(route)
        assert restored.status_code == 200, restored.text
    restored_resource = (await client.get(detail)).json()["data"]
    assert restored_resource["metadata"]["os_pid"] == pid
    assert restored_resource["metadata"]["cwd_relative"] == relative
    assert restored_resource["owner_revision"] == resource["owner_revision"]
    async with owner.operation(session_id=session_id, thread_id=thread_id,
                               holder_id="request:publication-retry", operation_id="publication-retry") as operation:
        await operation.write_terminal(terminal_id=rid, data="printf 'SAME_PTY' > publication-proof.txt\r")
        async with asyncio.timeout(5):
            while not (directory / "publication-proof.txt").exists():
                await asyncio.sleep(0.02)
    assert (directory / "publication-proof.txt").read_text() == "SAME_PTY"
    stopped = await client.post(f"{detail}/stop", json={"expected_revision": resource["owner_revision"]})
    assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "terminated", stopped.text
    assert not process_running(pid)
    assert not owner._leases.leases_for_resource(rid)
    case.write_actual("resource-metadata-publication", {"created": resource, "rejected": failures,
        "runtime_rejected": rejected.json(), "restored": restored_resource, "stopped": stopped.json()["data"]})
