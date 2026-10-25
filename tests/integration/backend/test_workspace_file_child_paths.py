"""通过产品复制输出验证后继路径定位，保留原目录名并拒绝绝对地址。"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.core.atomic_fs import native_io_path
from app.core.resource_uri import file_uri_child
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
async def child_case(request: pytest.FixtureRequest, integration_workspace_root_path: str):
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    source = workspace / ("source-" + uuid4().hex)
    spaced_name = getattr(request, "param", " 空格目录 ")
    (source / spaced_name).mkdir(parents=True)
    (source / spaced_name / "seed.txt").write_text("original", encoding="utf-8")
    target = workspace / ("target-" + uuid4().hex)
    target.mkdir()
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    backend = start_backend_process(workspace_root=str(workspace), port=port, log_name="child-backend")
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30,
            headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id},
        ) as client:
            listing = await client.get("/api/v1/workspace/files", params={"path": target.name})
            assert listing.status_code == 200, listing.text
            snapshot = listing.json()["data"]
            creator = "op_" + uuid4().hex
            copied = await client.post("/api/v1/workspace/files/copy", params={"path": snapshot["path"]}, json={
                "source_path": source.name, "source_scope": "workspace", "intent": {
                    "client_operation_id": creator, "client_sequence": 1,
                    "directory_key": snapshot["directory_key"],
                    "expected_directory_revision": snapshot["directory_revision"],
                },
            })
            assert copied.status_code == 202, copied.text
            async with asyncio.timeout(30):
                while True:
                    status = await client.get("/api/v1/workspace/files/operations", params={
                        "operation_id": creator, "expected_workspace_id": backend.workspace_id,
                    })
                    assert status.status_code == 200, status.text
                    receipt = status.json()["data"]["operations"][0]
                    assert receipt["state"] in {"queued", "running", "committed"}, receipt
                    if receipt["state"] == "committed":
                        break
                    await asyncio.sleep(0.02)
            copied_root = target / source.name
            keys = []
            for path in (copied_root, copied_root / spaced_name):
                listed = await client.get("/api/v1/workspace/files", params={"path": str(path)})
                assert listed.status_code == 200, listed.text
                keys.append(listed.json()["data"]["directory_key"])
            yield SimpleNamespace(
                client=client, workspace=workspace, workspace_id=backend.workspace_id,
                root=copied_root, spaced_name=spaced_name, keys=keys, creator=creator,
                artifacts=context.artifacts_dir,
            )
    finally:
        close_backend_process(backend)


def child_intent(case, *, relative, key):
    return {"client_operation_id": "op_" + uuid4().hex, "client_sequence": 2,
            "directory_key": key, "depends_on": [case.creator],
            "created_by_operation_id": case.creator,
            "directory_revision_from_operation_id": case.creator, "relative_child_path": relative}


@pytest.mark.asyncio
async def test_successor_preserves_spaces_in_original_output_directory(child_case):
    case = child_case
    intent = child_intent(case, relative=case.spaced_name, key=case.keys[1])
    created = await case.client.post("/api/v1/workspace/files/entries", json={
        "name": "child.txt", "kind": "file", "intent": intent,
    })
    assert created.status_code == 202, created.text
    async with asyncio.timeout(30):
        while True:
            status = await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": intent["client_operation_id"], "expected_workspace_id": case.workspace_id,
            })
            assert status.status_code == 200, status.text
            receipt = status.json()["data"]["operations"][0]
            assert receipt["state"] in {"queued", "running", "committed"}, receipt
            if receipt["state"] == "committed":
                break
            await asyncio.sleep(0.02)
    assert (case.root / case.spaced_name / "child.txt").is_file()
    assert not (case.root / case.spaced_name.strip()).exists()
    assert (case.root / case.spaced_name / "seed.txt").read_text(encoding="utf-8") == "original"
    (case.artifacts / "space-successor.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_successor_rejects_nonrelative_inputs_without_accepting_or_echoing_them(child_case):
    case = child_case
    database = case.workspace / ".boxteam/navigation/workspace-files.sqlite"

    def rows():
        with closing(sqlite3.connect(database)) as db:
            return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                    for name in ("owner", "directories", "operations", "events")}

    # 原操作的暂存回收完成后冻结对比，不把后台清理误报成非法请求的效果。
    async with asyncio.timeout(10):
        while True:
            with closing(sqlite3.connect(database)) as db:
                if db.execute("SELECT cleanup_done FROM operations WHERE operation_id=?", (case.creator,)).fetchone() == (1,):
                    break
            await asyncio.sleep(0.02)
    before = rows()
    evidence = []
    for value in ("/private-child/secret", r"C:\private-child\secret", r"C:private-child",
                  r"\\private-child\share", "file:///private-child/secret", "../private-child",
                  "safe/../private-child", "private-child\x00bad", case.keys[0]["path"]):
        intent = child_intent(case, relative=value, key=case.keys[0])
        request_id = f"req-child-{len(evidence)}"
        rejected = await case.client.post("/api/v1/workspace/files/entries", json={
            "name": "forbidden.txt", "kind": "file", "intent": intent,
        }, headers={"X-Request-ID": request_id})
        assert rejected.status_code == 400, rejected.text
        body = rejected.json()
        assert body["request_id"] == rejected.headers["X-Request-ID"] == request_id
        assert "private-child" not in body["detail"] and case.keys[0]["path"] not in body["detail"]
        assert "relative_child_path" in body["detail"]
        assert rows() == before
        assert [path.name for path in case.root.iterdir()] == [case.spaced_name]
        assert (case.root / case.spaced_name / "seed.txt").read_text(encoding="utf-8") == "original"
        evidence.append(body)
    logs = (case.workspace / ".boxteam/logs/child-backend.stderr.log").read_text()
    assert "private-child" in logs
    (case.artifacts / "invalid-successors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


async def _committed_file_operation(case, response):
    assert response.status_code == 202, response.text
    operation_id = response.json()["data"]["operation_id"]
    async with asyncio.timeout(30):
        while True:
            status = await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": operation_id, "expected_workspace_id": case.workspace_id,
            })
            assert status.status_code == 200, status.text
            receipt = status.json()["data"]["operations"][0]
            assert receipt["state"] in {"queued", "running", "committed"}, receipt
            if receipt["state"] == "committed":
                return receipt
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("child_case", ["源 目录%25"], indirect=True)
@pytest.mark.asyncio
async def test_native_long_file_paths_share_listing_edit_and_download_chain(child_case):
    case = child_case
    directory, directory_uri = case.root, case.keys[0]["path"]
    receipts = []
    sequence = 10
    for name in ("deep-a-" + "x" * 70, "deep-b-" + "y" * 70, "deep-c-" + "z" * 70):
        listed = await case.client.get("/api/v1/workspace/files", params={"path": directory_uri})
        assert listed.status_code == 200, listed.text
        snapshot = listed.json()["data"]
        intent = {"client_operation_id": "op_" + uuid4().hex, "client_sequence": sequence,
                  "directory_key": snapshot["directory_key"],
                  "expected_directory_revision": snapshot["directory_revision"]}
        created = await case.client.post("/api/v1/workspace/files/entries", params={"path": directory_uri},
                                        json={"name": name, "kind": "directory", "intent": intent})
        receipt = await _committed_file_operation(case, created)
        receipts.append(receipt)
        parent_uri = directory_uri
        directory, directory_uri = directory / name, receipt["created_path"]
        assert directory_uri == file_uri_child(snapshot["path"], name)
        sequence += 1
    filename = "文 件%25.txt"
    target, uri = directory / filename, file_uri_child(directory_uri, filename)
    assert len(str(target)) > 320
    listed = await case.client.get("/api/v1/workspace/files", params={"path": directory_uri})
    assert listed.status_code == 200, listed.text
    snapshot = listed.json()["data"]
    created = await case.client.post("/api/v1/workspace/files/entries", params={"path": directory_uri}, json={
        "name": filename, "kind": "file", "intent": {
            "client_operation_id": "op_" + uuid4().hex, "client_sequence": sequence,
            "directory_key": snapshot["directory_key"],
            "expected_directory_revision": snapshot["directory_revision"],
        },
    })
    receipts.append(await _committed_file_operation(case, created))
    opened = await case.client.get("/api/v1/workspace/files/content", params={"path": uri})
    assert opened.status_code == 200, opened.text
    content = "长路径原生文件，保留百分号与中文。\n"
    saved = await case.client.put("/api/v1/workspace/files/content", params={"path": uri}, json={
        "content": content, "expected_revision": opened.json()["data"]["revision"],
    })
    assert saved.status_code == 200, saved.text
    expected = saved.json()["data"]
    assert expected["path"] == uri and expected["content"] == content
    stale = await case.client.put("/api/v1/workspace/files/content", params={"path": str(target)}, json={
        "content": "stale write", "expected_revision": opened.json()["data"]["revision"],
    })
    assert stale.status_code == 409, stale.text
    observations = []
    for reference in (str(target.relative_to(case.workspace)), str(target), uri):
        result = await case.client.get("/api/v1/workspace/files/content", params={"path": reference})
        assert result.status_code == 200, result.text
        assert result.json()["data"] == expected
        observations.append(result.json())
    assert native_io_path(target).read_text(encoding="utf-8") == content
    for endpoint in ("raw", "download"):
        response = await case.client.get(f"/api/v1/workspace/files/{endpoint}", params={"path": uri})
        assert response.status_code == 200, response.text
        assert response.content == content.encode()
    ranged = await case.client.get("/api/v1/workspace/files/raw", params={"path": uri},
                                   headers={"Range": "bytes=0-5"})
    assert ranged.status_code == 206, ranged.text
    assert ranged.content == content.encode()[:6]
    archive = await case.client.get("/api/v1/workspace/files/download", params={"path": directory_uri})
    assert archive.status_code == 200, archive.text
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == [directory.name + "/" + filename]
        assert zipped.read(directory.name + "/" + filename) == content.encode()
    parent_archive = await case.client.get("/api/v1/workspace/files/download", params={"path": parent_uri})
    assert parent_archive.status_code == 200, parent_archive.text
    prefix = directory.parent.name + "/" + directory.name + "/"
    with zipfile.ZipFile(io.BytesIO(parent_archive.content)) as zipped:
        assert zipped.namelist() == [prefix, prefix + filename]
        assert zipped.read(prefix + filename) == content.encode()
    final = await case.client.get("/api/v1/workspace/files", params={"path": directory_uri})
    assert final.status_code == 200, final.text
    assert final.json()["data"]["items"][0]["path"] == uri
    for response in [*observations, *receipts, stale.json(), final.json()]:
        serialized = json.dumps(response, ensure_ascii=False)
        for native in (str(case.workspace), str(native_io_path(case.workspace)), "\\\\?\\"):
            assert json.dumps(native, ensure_ascii=False)[1:-1] not in serialized
    (case.artifacts / "native-long-files.json").write_text(json.dumps({
        "native_path_length": len(str(target)), "responses": observations,
        "listing": final.json(), "receipts": receipts, "stale": stale.json(),
        "archive_entries": [directory.name + "/" + filename],
        "nested_archive_entries": [prefix, prefix + filename],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
