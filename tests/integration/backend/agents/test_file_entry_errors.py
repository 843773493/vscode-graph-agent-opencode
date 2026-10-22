"""真实文件 HTTP 入口区分目标路径与上传相对文件名，并保留拒绝的无副作用合同。"""

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


@pytest.fixture
def entry_target(integration_workspace_root_path: str) -> tuple[Path, Path]:
    root = Path(integration_workspace_root_path)
    target = root / "file-entry-errors"
    target.mkdir()
    return root, target


async def entry_intent(client: httpx.AsyncClient, target: str) -> dict[str, object]:
    listing = await client.get("/api/v1/workspace/files", params={"path": target})
    assert listing.status_code == 200, listing.text
    data = listing.json()["data"]
    return {"client_operation_id": "op_" + uuid4().hex, "client_sequence": 1,
            "directory_key": data["directory_key"], "expected_directory_revision": data["directory_revision"]}


@pytest.mark.asyncio
async def test_invalid_file_entry_names_never_echo_native_input_or_publish_an_operation(
    integration_client: httpx.AsyncClient, entry_target: tuple[Path, Path],
    request: pytest.FixtureRequest,
) -> None:
    root, target = entry_target
    client = integration_client
    invalid_uploads = (
        str(root.parent / "private-entry-marker/file.txt"), "private-entry-marker/",
        "private-entry-marker//file.txt", r"C:\private-entry-marker\file.txt",
        r"C:private-entry-marker.txt", r"\\private-entry-marker\share\file.txt",
        "../private-entry-marker/file.txt", "private-entry-marker/./file.txt",
        "private-entry-marker/../file.txt", "private-entry-marker\x00.txt", ".", "",
    )
    invalid_names = (invalid_uploads[0], invalid_uploads[3], invalid_uploads[5],
                     " private-entry-marker ", "private-entry-marker\x00.txt")
    evidence = []
    operation_ids = []
    for kind, invalids in (("upload", invalid_uploads), ("create", invalid_names)):
        for invalid in invalids:
            command = await entry_intent(client, target.name)
            operation_ids.append(command["client_operation_id"])
            if kind == "upload":
                response = await client.post("/api/v1/workspace/files/upload", params={"path": str(target)},
                    data={"relative_paths": ["first.txt", invalid], "intent": json.dumps(command)},
                    files=[("files", ("first.txt", b"first")), ("files", ("second.txt", b"invalid"))])
            else:
                response = await client.post("/api/v1/workspace/files/entries", params={"path": str(target)},
                    json={"name": invalid, "kind": "file", "intent": command})
            assert response.status_code == 400, response.text
            assert response.headers["X-Request-ID"]
            assert "文件" in response.json()["detail"]
            assert "private-entry-marker" not in response.text and str(root) not in response.text
            assert list(target.iterdir()) == []
            evidence.append({"kind": kind, "response": response.json()})
    staging = root / ".boxteam/navigation/workspace-files-staging"
    assert not staging.exists() or list(staging.iterdir()) == []
    with closing(sqlite3.connect(f"{(root / '.boxteam/navigation/workspace-files.sqlite').as_uri()}?mode=ro", uri=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM operations WHERE operation_id IN (" +
                          ",".join("?" for _ in operation_ids) + ")", operation_ids).fetchone()[0] == 0
    assert not (root.parent / "private-entry-marker").exists()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "file-entry-rejections.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_upload_target_accepts_native_paths_and_uri_while_names_remain_relative(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
    request: pytest.FixtureRequest,
) -> None:
    root = Path(integration_workspace_root_path)
    target = root / "upload-target-contract"
    target.mkdir()
    client = integration_client
    reference = await client.post("/api/v1/workspace/files/references", json={"paths": [str(target)]})
    assert reference.status_code == 200, reference.text
    uri, = reference.json()["data"]["uris"]
    evidence = []
    for index, destination in enumerate((target.name, str(target), uri)):
        command = await entry_intent(client, destination)
        relative = f"嵌套 目录/文件{index}.txt"
        content = f"第{index}份\r\n原始正文\n".encode()
        response = await client.post("/api/v1/workspace/files/upload", params={"path": destination},
            data={"relative_paths": relative.replace("/", "\\"), "intent": json.dumps(command)},
            files={"files": ("upload.txt", content)})
        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert accepted["operation_id"] == command["client_operation_id"]
        assert str(root) not in response.text
        for _ in range(200):
            status = await client.get("/api/v1/workspace/files/operations", params={
                "operation_id": command["client_operation_id"],
                "expected_workspace_id": command["directory_key"]["workspace_id"],
            })
            assert status.status_code == 200, status.text
            operation, = status.json()["data"]["operations"]
            if operation["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.05)
        assert operation["state"] == "committed" and operation["effect"] == "complete"
        assert str(root) not in status.text
        assert (target / relative).read_bytes() == content
        evidence.append({"accepted": response.json(), "terminal": status.json()})
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "file-upload-targets.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
