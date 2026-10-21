"""真实调试配置/启动入口的原生路径输入与错误响应合同，不启动 Node 或模型。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def debug_path_targets(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> tuple[Path, Path, Path]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    source = root / "debug-path-errors" / "entry.mjs"
    source.parent.mkdir(exist_ok=True)
    source.write_text("export {};\n", encoding="utf-8")
    outside = context.runtime_root_for_node(request.node.nodeid) / "outside-debug-directory"
    outside.mkdir(parents=True, exist_ok=True)
    return root, source, outside


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["configurations", "start"])
async def test_debug_path_errors_keep_uri_and_leave_configuration_unchanged(
    integration_client: httpx.AsyncClient,
    debug_path_targets: tuple[Path, Path, Path],
    request: pytest.FixtureRequest,
    entrypoint: str,
) -> None:
    root, source, outside = debug_path_targets
    client = integration_client
    created = await client.post("/api/v1/sessions", json={"title": "调试路径错误合同"})
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["session_id"]
    main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert main.status_code == 200, main.text
    thread_id = main.json()["data"]["thread_id"]
    params = {"session_id": session_id, "thread_id": thread_id}
    before = await client.get("/api/v1/debug/node", params=params)
    assert before.status_code == 200, before.text
    state = before.json()["data"]

    references = await client.post("/api/v1/workspace/files/references", json={
        "paths": [str(source), str(root / "missing.mjs"), str(root / "missing-directory")],
    })
    assert references.status_code == 200, references.text
    source_uri, script_uri, directory_uri = references.json()["data"]["uris"]
    receipts = []
    cases = [
        (value, "", 404, script_uri, False)
        for value in ("missing.mjs", str(root / "missing.mjs"), script_uri)
    ] + [
        (source_uri, value, 404, directory_uri, False)
        for value in ("missing-directory", str(root / "missing-directory"), directory_uri)
    ] + [(source_uri, str(outside), 400, None, False)]
    if entrypoint == "configurations":
        cases += [(source_uri, "", 404, script_uri, True)]
    for index, (script, directory, status, uri, missing_breakpoint) in enumerate(cases):
        payload = {**params, "working_directory": directory}
        if entrypoint == "start":
            payload["path"] = script
        else:
            payload.update({"name": "路径检查", "script_path": script})
            if missing_breakpoint:
                payload["breakpoints"] = [{"path": str(root / "missing.mjs"), "line": 1}]
        request_id = f"req_debug_path_{entrypoint}_{index}"
        response = await client.post(
            f"/api/v1/debug/node/{entrypoint}", json=payload,
            headers={"X-Request-ID": request_id},
        )
        assert response.status_code == status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        assert str(root) not in response.text and str(outside) not in response.text
        if uri is not None:
            assert uri in response.json()["detail"]
        else:
            assert response.json()["detail"] == "调试工作目录必须位于当前 workspace 内"
        after = await client.get("/api/v1/debug/node", params=params)
        assert after.status_code == 200, after.text
        assert after.json()["data"] == state
        receipts.append({"request_id": request_id, "status": status, "response": response.json()})

    # 拒绝无效目标后，有效原生绝对路径仍能正常创建配置。
    repaired = await client.post("/api/v1/debug/node/configurations", json={
        **params, "name": "可访问目标", "script_path": str(source),
        "working_directory": str(source.parent),
    })
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["active_configuration_id"] is not None
    assert repaired.json()["data"]["pid"] is None
    assert str(root) not in repaired.text
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{entrypoint}-path-errors.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
