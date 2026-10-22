"""真实文件预览和文本 owner 的系统错误使用 URI，不暴露原生文件名。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from app.schemas.internal_v2.text_mutation import (
    EditText,
    PatchText,
    TextMutationRequest,
)
from tests.harness.python.run_context import TestRunContext


@pytest.mark.asyncio
async def test_patch_filename_errors_are_path_free_before_any_publication(
    integration_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
    request: pytest.FixtureRequest,
) -> None:
    root = Path(integration_workspace_root_path)
    source = root / "patch-path-source.txt"
    source.write_bytes(b"before\n")
    reference = await integration_client.post("/api/v1/workspace/files/references", json={"paths": [str(root)]})
    assert reference.status_code == 200, reference.text
    uri = reference.json()["data"]["uris"][0]
    journal_root = root / ".boxteam/cache/text_mutations"
    journals = set(journal_root.glob("*.json"))
    filenames = (
        str(root / "patch-path-marker.txt"),
        r"C:\Users\patch-path-marker\private.txt",
        r"\\private-host\patch-path-marker\private.txt",
        "../patch-path-marker/private.txt",
        "boxteam://workspace/patch-path-marker/files/root/private.txt",
        ".",
        "",
        "patch-path-marker\x00.txt",
    )
    outside = root.parent / "patch-path-marker/private.txt"
    assert not outside.exists()
    receipts = []
    for filename in filenames:
        for action in ("Add", "Update", "Delete", "Move"):
            versions = {"patch-safe-first.txt": "absent", filename: "absent"}
            if action == "Move":
                versions[source.name] = "sha256:" + hashlib.sha256(b"before\n").hexdigest()
                operation = f"*** Update File: {source.name}\n*** Move to: {filename}\n@@\n-before\n+after\n"
            else:
                operation = f"*** {action} File: {filename}\n" + ("+blocked\n" if action == "Add" else "")
            command = PatchText(target=uri, input="*** Begin Patch\n*** Add File: patch-safe-first.txt\n+first\n" + operation + "*** End Patch",
                                expected_versions=versions, explanation="验证非法补丁文件名的失败边界")
            response = await integration_client.post("/api/v1/resources/text-mutations", json=
                TextMutationRequest.from_command(command).model_dump(mode="json", exclude_none=True))
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            transfer = response.json()["data"]
            receipt = transfer["receipt"]
            assert receipt["status"] == "error" and receipt["effect"] == "not_executed"
            assert receipt["code"] == "invalid_patch" and receipt["error_type"] == "DiffError"
            assert receipt["owner"] == "resource_file_operations" and receipt["retryable"] is False
            assert "文件" in receipt["error"] and "路径" in receipt["error"]
            assert receipt["files"] == [] and transfer["snapshots"] == []
            assert "patch-path-marker" not in response.text and str(root) not in response.text
            assert source.read_bytes() == b"before\n"
            assert not (root / "patch-safe-first.txt").exists() and not (root / "patch-path-marker.txt").exists()
            assert not outside.exists()
            assert set(journal_root.glob("*.json")) == journals
            receipts.append({"action": action, "response": response.json()})
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "patch-invalid-filenames.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


@pytest.fixture
def text_error_target(integration_workspace_root_path: str):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("本场景需要非 root POSIX 文件权限，Windows ACL 另行验收")
    root = Path(integration_workspace_root_path)
    directory = root / "text-file-errors"
    directory.mkdir(exist_ok=True)
    target = directory / "notes.txt"
    target.write_bytes(b"before\n")
    try:
        yield root, target
    finally:
        directory.chmod(0o755)
        target.chmod(0o644)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "publish"])
async def test_text_file_errors_keep_uri_original_bytes_and_effects(
    integration_client: httpx.AsyncClient,
    text_error_target: tuple[Path, Path],
    failure: str,
    request: pytest.FixtureRequest,
) -> None:
    root, target = text_error_target
    client = integration_client
    reference = await client.post("/api/v1/workspace/files/references", json={"paths": [str(target)]})
    assert reference.status_code == 200, reference.text
    uri = reference.json()["data"]["uris"][0]
    original = b"before\n"
    revision = hashlib.sha256(original).hexdigest()
    journal_root = root / ".boxteam" / "cache" / "text_mutations"
    existing_journals = set(journal_root.glob("*.json"))
    if failure == "read":
        target.chmod(0)
    else:
        target.parent.chmod(0o555)
    receipts = []
    for index, value in enumerate((target.relative_to(root).as_posix(), str(target), uri)):
        request_id = f"req_text_file_{failure}_{index}"
        arguments = {"params": {"path": value}, "headers": {"X-Request-ID": request_id}}
        response = (
            await client.get("/api/v1/workspace/files/content", **arguments)
            if failure == "read" else
            await client.put("/api/v1/workspace/files/content", json={
                "content": "after\n", "expected_revision": revision,
            }, **arguments)
        )
        assert response.status_code == 500, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        assert str(root) not in response.text
        assert "PermissionError" in response.json()["message"]
        assert "errno=13" in response.json()["message"]
        assert uri in response.json()["message"]
        receipts.append({"preview": response.json()})

    command = EditText(target=uri, old_string="before", new_string="after",
                       expected_version="sha256:" + revision)
    result = await client.post("/api/v1/resources/text-mutations", json=
        TextMutationRequest.from_command(command).model_dump(mode="json", exclude_none=True))
    assert result.status_code == 200, result.text
    assert result.headers["X-Request-ID"] == result.json()["request_id"]
    receipt = result.json()["data"]["receipt"]
    assert receipt["status"] == "error" and receipt["effect"] == "not_executed"
    assert receipt["error_type"] == "PermissionError" and receipt["retryable"] is False
    assert uri in receipt["error"] and "errno=13" in receipt["error"]
    assert str(root) not in result.text
    if failure == "read":
        assert set(journal_root.glob("*.json")) == existing_journals
    else:
        file, = receipt["files"]
        assert file["path"] == uri and file["state"] == "not_published"
        snapshot, = result.json()["data"]["snapshots"]
        assert snapshot["uri"] == uri
        assert snapshot["before_content"] == snapshot["after_content"] == "before\n"
        assert list(target.parent.glob(".notes.txt.*.tmp")) == []
    receipts.append({"mutation": result.json()})

    target.parent.chmod(0o755)
    target.chmod(0o644)
    assert target.read_bytes() == original
    if failure == "read":
        repaired = await client.get("/api/v1/workspace/files/content", params={"path": value})
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["data"]["content"] == "before\n"
        assert repaired.json()["data"]["revision"] == revision
    else:
        repaired = await client.put("/api/v1/workspace/files/content", params={"path": value}, json={
            "content": "after\n", "expected_revision": revision,
        })
        assert repaired.status_code == 200, repaired.text
        assert target.read_bytes() == b"after\n"
    assert repaired.json()["data"]["path"] == uri
    assert str(root) not in repaired.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{failure}-file-errors.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
