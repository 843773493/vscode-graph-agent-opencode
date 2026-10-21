"""文件树快捷方式的真实 HTTP 存储错误不发布物理位置或校验输入。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
async def shortcut_error_target(
    integration_client: httpx.AsyncClient, integration_workspace_root_path: str,
):
    root = Path(integration_workspace_root_path)
    directory = root / "shortcut-error-target"
    directory.mkdir(exist_ok=True)
    created = await integration_client.post("/api/v1/sessions", json={"title": "快捷方式诊断"})
    assert created.status_code == 200, created.text
    session = created.json()["data"]["session_id"]
    shortcut = {"path": str(directory), "label": "原始快捷方式"}
    added = await integration_client.post(f"/api/v1/sessions/{session}/file-tree-shortcuts", json=shortcut)
    assert added.status_code == 200, added.text
    uri = added.json()["data"]["session_shortcuts"][0]["path"]
    promoted = await integration_client.post(
        f"/api/v1/sessions/{session}/file-tree-shortcuts/apply-to-workspace", json=shortcut,
    )
    assert promoted.status_code == 200, promoted.text
    # 仅定位产品已经创建的控制文件，定点注入故障，不替产品创建快捷方式或快照。
    catalog = root / ".boxteam/navigation/session-catalog.sqlite"
    connection = sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
    try:
        (locator,) = connection.execute(
            "SELECT storage_relative_locator FROM nodes WHERE node_id=?", (session,),
        ).fetchone()
    finally:
        connection.close()
    paths = {
        "session": root / ".boxteam" / locator / "ui/file-tree-shortcuts.json",
        "workspace": root / ".boxteam/ui/file-tree-shortcuts.json",
    }
    originals = {scope: path.read_bytes() for scope, path in paths.items()}
    try:
        yield root, session, shortcut, uri, paths, originals
    finally:
        for scope, path in paths.items():
            path.parent.chmod(0o755)
            path.chmod(0o644)
            path.write_bytes(originals[scope])


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "workspace"])
@pytest.mark.parametrize("failure", ["shape", "validation", "permission", "write"])
async def test_file_tree_settings_errors_preserve_original_and_recover(
    integration_client: httpx.AsyncClient, shortcut_error_target,
    scope: str, failure: str, request: pytest.FixtureRequest,
):
    root, session, shortcut, uri, paths, originals = shortcut_error_target
    if failure in {"permission", "write"} and (os.name == "nt" or os.geteuid() == 0):
        pytest.skip("真实权限错误需要非 root POSIX 用户，Windows ACL 单独验收")
    path = paths[scope]
    original = originals[scope]
    corrupted = original
    if failure == "shape":
        corrupted = b"[]\n"
    elif failure == "validation":
        payload = json.loads(original)
        payload["shortcuts"] = [{"path": str(root / "private-input.txt"), "label": "损坏输入"}]
        corrupted = json.dumps(payload).encode()
    path.write_bytes(corrupted)
    if failure == "permission":
        path.chmod(0)
    elif failure == "write":
        path.parent.chmod(0o555)
    request_id = f"req_shortcut_{scope}_{failure}"
    headers = {"X-Request-ID": request_id}
    base = f"/api/v1/sessions/{session}"
    response = (
        await integration_client.post(
            base + "/file-tree-shortcuts" + ("/apply-to-workspace" if scope == "workspace" else ""),
            json=shortcut, headers=headers,
        ) if failure == "write" else
        await integration_client.get(base + "/file-tree-settings", headers=headers)
    )
    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert str(root) not in response.text
    assert "private-input.txt" not in response.text
    assert "artifact=file_tree_shortcuts" in response.json()["message"]
    if failure in {"permission", "write"}:
        assert "PermissionError" in response.text and "errno=13" in response.text
    path.parent.chmod(0o755)
    path.chmod(0o644)
    assert path.read_bytes() == corrupted
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    path.write_bytes(original)
    repaired = await integration_client.get(base + "/file-tree-settings")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["session_shortcuts"][0]["path"] == uri
    if failure == "write":
        retried = await integration_client.post(
            base + "/file-tree-shortcuts" + ("/apply-to-workspace" if scope == "workspace" else ""),
            json=shortcut,
        )
        assert retried.status_code == 200, retried.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{scope}-{failure}.json").write_text(
        json.dumps({"error": response.json(), "repaired": repaired.json()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
