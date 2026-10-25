"""产品入口生成文件回执后，损坏的持久引用不能进入普通查询或重试响应。"""

from __future__ import annotations

import asyncio
import copy
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


@pytest.mark.asyncio
async def test_corrupt_receipt_stays_explicit_and_never_replays_file_effect(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    directory = workspace / "receipt-case"
    directory.mkdir()
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    backend = start_backend_process(workspace_root=str(workspace), port=port, log_name="receipt-backend")
    database = workspace / ".boxteam/navigation/workspace-files.sqlite"
    operation_id = "op_" + uuid4().hex
    evidence = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30,
            headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": backend.workspace_id},
        ) as client:
            listing = await client.get("/api/v1/workspace/files", params={"path": "receipt-case"})
            assert listing.status_code == 200, listing.text
            snapshot = listing.json()["data"]
            command = {"name": "created.txt", "kind": "file", "intent": {
                "client_operation_id": operation_id, "client_sequence": 1,
                "directory_key": snapshot["directory_key"],
                "expected_directory_revision": snapshot["directory_revision"],
            }}
            accepted = await client.post(
                "/api/v1/workspace/files/entries", params={"path": snapshot["path"]}, json=command,
            )
            assert accepted.status_code == 202, accepted.text
            query = {"operation_id": operation_id, "expected_workspace_id": backend.workspace_id}
            async with asyncio.timeout(30):
                while True:
                    status = await client.get("/api/v1/workspace/files/operations", params=query)
                    assert status.status_code == 200, status.text
                    receipt = status.json()["data"]["operations"][0]
                    if receipt["state"] == "committed":
                        with closing(sqlite3.connect(database)) as db:
                            if db.execute("SELECT cleanup_done FROM operations WHERE operation_id=?", (operation_id,)).fetchone() == (1,):
                                break
                    assert receipt["state"] in {"queued", "running", "committed"}, receipt
                    await asyncio.sleep(0.02)
            original_bytes = (directory / "created.txt").read_bytes()
            with closing(sqlite3.connect(database)) as db:
                original, = db.execute("SELECT receipt FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            assert json.loads(original) == receipt

            def rows():
                with closing(sqlite3.connect(database)) as db:
                    return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                            for name in ("owner", "directories", "operations", "events")}

            for native in ("/receipt-private/secret.txt", r"C:\receipt-private\secret.txt",
                           r"\\receipt-private\share\secret.txt", "file:///receipt-private/secret.txt"):
                for field in ("created_path", "item", "directory_key"):
                    damaged = copy.deepcopy(receipt)
                    if field == "created_path":
                        damaged["created_path"] = native
                    elif field == "item":
                        damaged["items"][0]["path"] = native
                    else:
                        damaged["directories"][0]["directory_key"][native] = "invalid"
                    encoded = json.dumps(damaged)
                    with closing(sqlite3.connect(database)) as db:
                        db.execute("UPDATE operations SET receipt=? WHERE operation_id=?", (encoded, operation_id))
                        db.commit()
                    before = rows()
                    try:
                        for action in ("query", "replay"):
                            request_id = f"req-receipt-{len(evidence)}"
                            headers = {"X-Request-ID": request_id}
                            failed = (
                                await client.get("/api/v1/workspace/files/operations", params=query, headers=headers)
                                if action == "query" else await client.post(
                                    "/api/v1/workspace/files/entries", params={"path": snapshot["path"]},
                                    json=command, headers=headers,
                                )
                            )
                            assert failed.status_code == 400, failed.text
                            body = failed.json()
                            assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                            assert "receipt-private" not in body["detail"]
                            assert "回执格式错误" in body["detail"] and operation_id in body["detail"]
                            assert rows() == before
                            assert (directory / "created.txt").read_bytes() == original_bytes
                            evidence.append({"field": field, "action": action, "body": body})
                    finally:
                        with closing(sqlite3.connect(database)) as db:
                            db.execute("UPDATE operations SET receipt=? WHERE operation_id=?", (original, operation_id))
                            db.commit()
                    reopened = await client.get("/api/v1/workspace/files/operations", params=query)
                    assert reopened.status_code == 200, reopened.text
                    assert reopened.json()["data"]["operations"] == [receipt]
            replay = await client.post(
                "/api/v1/workspace/files/entries", params={"path": snapshot["path"]}, json=command,
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["data"] == receipt
            assert [path.name for path in directory.iterdir()] == ["created.txt"]
            assert (directory / "created.txt").read_bytes() == original_bytes
        logs = (workspace / ".boxteam/logs/receipt-backend.stderr.log").read_text()
        assert "receipt-private" in logs and operation_id in logs
        (context.artifacts_dir / "receipt-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )
    finally:
        close_backend_process(backend)
