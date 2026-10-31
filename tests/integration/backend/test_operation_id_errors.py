"""真实后端操作 ID 拒绝不外泄原输入，也不改变已受理的文件或目录操作。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
async def operation_client(request: pytest.FixtureRequest, integration_workspace_root_path: str):
    workspace = Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    backend = start_backend_process(workspace_root=str(workspace), port=port, log_name="operation-id-backend")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30, headers={
            "X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id,
            "X-BoxTeam-Gateway-Id": "gateway_789f146c159aa74461af8dacca7341ca",
            "X-BoxTeam-Navigation-Actor": "user:operation-id-test",
        }) as client:
            yield client, workspace, backend.workspace_id
    finally:
        close_backend_process(backend)


@pytest.mark.asyncio
async def test_operation_id_rejection_keeps_original_status_and_accepted_history(operation_client) -> None:
    client, workspace, workspace_id = operation_client
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    file_base = "/api/v1/workspace/files"
    catalog_base = "/api/v1/session-catalog"
    listing = await client.get(file_base)
    assert listing.status_code == 200, listing.text
    directory = listing.json()["data"]
    snapshot = await client.get(catalog_base + "/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    file_id, catalog_id, unknown_id = ("op_" + uuid4().hex for _ in range(3))
    file_command = {"name": "operation-id-target.txt", "kind": "file", "intent": {
        "client_operation_id": file_id, "client_sequence": 1,
        "directory_key": directory["directory_key"],
        "expected_directory_revision": directory["directory_revision"],
    }}
    catalog_command = {"intents": [{
        "client_operation_id": catalog_id, "client_sequence": 1, "kind": "create_folder",
        "base_catalog_revision": snapshot.json()["data"]["catalog_revision"],
        "name": "operation-id-folder",
    }]}
    created = await client.post(file_base + "/entries", json=file_command)
    folder = await client.post(catalog_base + "/operations:enqueue", json=catalog_command)
    assert created.status_code == folder.status_code == 202, (created.text, folder.text)

    async def status(base: str, values: list[str], request_id: str):
        params = [("operation_id", value) for value in values]
        if base == file_base:
            params.append(("expected_workspace_id", workspace_id))
        return await client.get(base + "/operations", params=params, headers={"X-Request-ID": request_id})

    async with asyncio.timeout(30):
        while True:
            file = await status(file_base, [file_id], "req-operation-file-ready")
            catalog = await status(catalog_base, [catalog_id], "req-operation-catalog-ready")
            assert file.status_code == catalog.status_code == 200, (file.text, catalog.text)
            file_receipt = file.json()["data"]["operations"][0]
            catalog_receipt = catalog.json()["data"]["items"][0]
            assert file_receipt["state"] in {"queued", "running", "committed"}, file_receipt
            assert catalog_receipt["state"] in {"queued", "running", "committed"}, catalog_receipt
            if file_receipt["state"] == catalog_receipt["state"] == "committed":
                break
            await asyncio.sleep(0.02)
    original = (workspace / "operation-id-target.txt").read_bytes()
    settled_snapshot = await client.get(catalog_base + "/snapshot")
    assert settled_snapshot.status_code == 200, settled_snapshot.text

    def operations():
        rows = {}
        for database, table in (("workspace-files.sqlite", "operations"),
                                 ("session-catalog.sqlite", "navigation_mutation_records")):
            with closing(sqlite3.connect(workspace / ".boxteam/navigation" / database)) as connection:
                rows[database] = connection.execute(f"SELECT operation_id,state FROM {table} ORDER BY operation_id").fetchall()
        return rows

    before = operations()
    evidence = []
    for native in ("/operation-id-private/secret", r"C:\operation-id-private\secret",
                   r"\\operation-id-private\share\secret", "file:///operation-id-private/secret"):
        for base, accepted_id in ((file_base, file_id), (catalog_base, catalog_id)):
            for values in ([native, accepted_id], [accepted_id, native]):
                request_id = f"req-operation-id-{len(evidence)}"
                failed = await status(base, values, request_id)
                assert failed.status_code == 400, failed.text
                assert failed.json() == {
                    "detail": "operation_id 形态非法：必须为 op_ 加 32 位小写十六进制字符",
                    "request_id": request_id,
                }
                assert failed.headers["X-Request-ID"] == request_id
                evidence.append({"base": base, "request_id": request_id, "body": failed.json()})
        for base, command in ((file_base + "/entries", {**file_command, "intent": {
            **file_command["intent"], "client_operation_id": native,
        }}), (catalog_base + "/operations:enqueue", {"intents": [{
            **catalog_command["intents"][0], "client_operation_id": native,
        }]})):
            request_id = f"req-operation-input-{len(evidence)}"
            rejected = await client.post(base, json=command, headers={"X-Request-ID": request_id})
            assert rejected.status_code == 422, rejected.text
            assert "operation-id-private" not in rejected.text
            assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == request_id
            evidence.append({"base": base, "request_id": request_id, "body": rejected.json()})
    unknown_file = await status(file_base, [unknown_id], "req-operation-unknown-file")
    unknown_catalog = await status(catalog_base, [unknown_id], "req-operation-unknown-catalog")
    assert unknown_file.status_code == 404, unknown_file.text
    assert unknown_catalog.status_code == 200, unknown_catalog.text
    assert unknown_catalog.json()["data"]["unknown_operation_ids"] == [unknown_id]
    assert unknown_catalog.json()["data"]["items"] == []
    file_after = await status(file_base, [file_id], "req-operation-file-after")
    catalog_after = await status(catalog_base, [catalog_id], "req-operation-catalog-after")
    assert file_after.json()["data"]["operations"] == [file_receipt]
    assert catalog_after.json()["data"]["items"] == [catalog_receipt]
    assert (workspace / "operation-id-target.txt").read_bytes() == original
    assert operations() == before
    after_snapshot = await client.get(catalog_base + "/snapshot")
    assert after_snapshot.json()["data"] == settled_snapshot.json()["data"]
    roots = await client.get(catalog_base + "/roots")
    assert roots.status_code == 200, roots.text
    assert [node["name"] for node in roots.json()["data"]["items"]] == ["operation-id-folder"]
    for base, command, receipt in ((file_base + "/entries", file_command, file_receipt),
                                   (catalog_base + "/operations:enqueue", catalog_command, catalog_receipt)):
        replay = await client.post(base, json=command)
        assert replay.status_code == 202, replay.text
        data = replay.json()["data"]
        assert (data if base.startswith(file_base) else data["receipts"][0]) == receipt
    logs = (workspace / ".boxteam/logs/operation-id-backend.stderr.log").read_text()
    for item in evidence:
        assert item["request_id"] in logs
    assert logs.count("原始 operation_id=") == 16
    assert "operation-id-private" in logs
    (context.artifacts_dir / "operation-id-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
