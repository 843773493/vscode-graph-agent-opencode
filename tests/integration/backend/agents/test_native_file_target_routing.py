"""原生路径经真实远端 Gateway 在固定目标解释；响应只使用资源 URI。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - fixture 按当前文件隔离
)

# TODO：目前只配置 Linux 目标；接入真实 Windows Gateway 后补验盘符、UNC 和盘符相对路径拒绝，不能用本机字符串测试代替。


async def test_remote_file_api_resolves_native_and_uri_targets_on_target_machine(communication_triad):
    nodes, model, artifacts = communication_triad
    source, target = nodes["a"], nodes["c"]
    relative = "src/原生 目标%.txt"
    originals = {}
    for name, node in nodes.items():
        path = node.workspace / relative
        originals[path] = (name + " OWNER Ω\n").encode()
        path.write_bytes(originals[path])
    native = target.workspace / relative
    uri = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", *native.relative_to(target.workspace).parts))
    # 旧去根回退会命中这些真实对照，不能把拒绝或成功误投到其它工作区。
    for node in (nodes["a"], nodes["b"]):
        decoy = node.workspace / str(native).lstrip("/")
        decoy.parent.mkdir(parents=True)
        originals[decoy] = b"REDIRECT DECOY\n"
        decoy.write_bytes(originals[decoy])
    registered = await source.client.get("/api/gateway/workspaces")
    assert registered.status_code == 200, registered.text
    routes = {row["backend_workspace_id"]: row["workspace_id"] for row in registered.json()["data"]["items"]}
    switched = await source.client.post(f"/api/gateway/workspaces/{routes[nodes['b'].workspace_id]}/activate")
    assert switched.status_code == 200, switched.text
    endpoint = f"/api/v1/workspaces/{target.workspace_id}/workspace/files/content"
    evidence = []

    async def request(method, path, *, status=200, body=None):
        response = await source.client.request(method, endpoint, params={"path": path}, json=body,
            headers={"X-BoxTeam-Workspace-Id": routes[nodes["b"].workspace_id]})
        assert response.status_code == status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        for node in nodes.values():
            assert str(node.workspace) not in response.text and str(node.gateway_home) not in response.text
        assert "local-dev-token" not in response.text and "federation_token" not in response.text
        evidence.append({"method": method, "status": status, "response": response.json()})
        return response.json().get("data")

    values = (relative, str(native), uri)
    reads = [await request("GET", value) for value in values]
    assert all(read["path"] == uri and read["content"] == "c OWNER Ω\n" for read in reads)
    assert {read["revision"] for read in reads} == {hashlib.sha256(originals[native]).hexdigest()}
    revision = reads[0]["revision"]
    for index, value in enumerate(values):
        content = f"c UPDATED {index} Ω\n"
        saved = await request("PUT", value, body={"content": content, "expected_revision": revision})
        assert saved["path"] == uri and saved["content"] == content
        assert native.read_bytes() == content.encode()
        revision = saved["revision"]
        for other in values:
            reread = await request("GET", other)
            assert (reread["path"], reread["content"], reread["revision"]) == (uri, content, revision)

    # 外部绝对路径保留原 Web 文件能力，由目标 Gateway 绑定，不要求注册为工作区。
    external = target.workspace.parent / "外部 目标%.txt"
    external.write_text("EXTERNAL original\n", encoding="utf-8")
    with closing(sqlite3.connect((target.gateway_root / "gateway.sqlite").as_uri() + "?mode=ro")) as database:
        before = database.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()
    outside = await request("GET", str(external))
    parsed = parse_resource_uri(outside["path"])
    assert (parsed.scope, parsed.scope_id, parsed.kind) == ("gateway", target.gateway_id, "files")
    saved = await request("PUT", str(external), body={"content": "EXTERNAL saved\n", "expected_revision": outside["revision"]})
    assert saved["path"] == outside["path"] and external.read_text(encoding="utf-8") == "EXTERNAL saved\n"
    assert (await request("GET", outside["path"]))["content"] == "EXTERNAL saved\n"
    with closing(sqlite3.connect((target.gateway_root / "gateway.sqlite").as_uri() + "?mode=ro")) as database:
        assert database.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall() == before

    await asyncio.to_thread(target.restart_backend, "kill")
    await request("GET", relative, status=502)
    await asyncio.to_thread(target.restart_backend, "start")
    assert (await request("GET", uri))["content"] == "c UPDATED 2 Ω\n"
    for path, content in originals.items():
        if path != native:
            assert path.read_bytes() == content
    assert not model.requests
    (artifacts / "native-file-target-routing.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
