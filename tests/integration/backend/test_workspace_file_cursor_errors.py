"""独立工作区后端通过真实 HTTP 验证文件树游标错误和正常分页。"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.mark.asyncio
async def test_native_cursor_input_stays_in_diagnostics_and_original_page_can_continue(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    directory = workspace / "cursor-case"
    directory.mkdir()
    originals = {"a.txt": b"a", "B.txt": b"B", "中文 %.txt": b"original"}
    for name, content in originals.items():
        (directory / name).write_bytes(content)
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    backend = start_backend_process(workspace_root=str(workspace), port=port, log_name="cursor-backend")
    evidence = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30,
            headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id},
        ) as client:
            response = await client.get("/api/v1/workspace/files", params={"path": "cursor-case", "limit": 1})
            assert response.status_code == 200, response.text
            first = response.json()["data"]
            cursor = first["next_cursor"]
            assert cursor is not None
            original = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            database = workspace / ".boxteam/navigation/workspace-files.sqlite"

            def rows():
                with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
                    return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                            for name in ("owner", "directories", "operations", "events")}

            before = rows()
            for native in ("/private-cursor/secret.txt", r"C:\private-cursor\secret.txt",
                           r"\\private-cursor\share\secret.txt", "file:///private-cursor/secret.txt"):
                for field in ("path", "root", "extra_key"):
                    altered = copy.deepcopy(original)
                    key = altered["directory_key"]
                    key[native if field == "extra_key" else field] = "invalid" if field == "extra_key" else native
                    invalid = base64.urlsafe_b64encode(json.dumps(altered).encode()).decode().rstrip("=")
                    request_id = f"req-cursor-{len(evidence)}"
                    failed = await client.get("/api/v1/workspace/files", params={
                        "path": first["path"], "limit": 1, "cursor": invalid,
                    }, headers={"X-Request-ID": request_id})
                    assert failed.status_code == 400, failed.text
                    body = failed.json()
                    assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                    assert "private-cursor" not in body["detail"]
                    assert body["detail"] == "目录分页游标内容错误"
                    assert rows() == before
                    evidence.append({"field": field, "body": body})
            names = [item["name"] for item in first["items"]]
            for _ in range(len(originals) - 1):
                assert cursor is not None
                page = await client.get("/api/v1/workspace/files", params={
                    "path": first["path"], "limit": 1, "cursor": cursor,
                })
                assert page.status_code == 200, page.text
                data = page.json()["data"]
                assert data["directory_key"] == first["directory_key"]
                assert data["directory_revision"] == first["directory_revision"]
                names.extend(item["name"] for item in data["items"])
                cursor = data["next_cursor"]
            assert cursor is None
            assert names == ["a.txt", "B.txt", "中文 %.txt"]
            assert {name: (directory / name).read_bytes() for name in originals} == originals
        logs = (workspace / ".boxteam/logs/cursor-backend.stderr.log").read_text()
        assert "private-cursor" in logs
        (context.artifacts_dir / "cursor-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )
    finally:
        close_backend_process(backend)
